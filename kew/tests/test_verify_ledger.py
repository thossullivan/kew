"""What every red merge verify was, layer by layer (#3215).

The verifies are recorded the way ``kew verify`` records them
(:func:`record_verify_event`) and resolved the way it resolves them
(:func:`resolve_after`); ``gh`` is :class:`FakeGh`, which files nothing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import FakeGh
from support.verify_switches import environ_without_switches
from typer.testing import CliRunner

from kew.audit import emit, resolve_actor
from kew.cli import app
from kew.config import DispatchConfig
from kew.merge_verify import PHASE_MARK as JOB_PHASE_MARK
from kew.merge_verify import (
    PHASE_UI_CHECK,
    PREFLIGHT_PASSED_MARK,
    VERIFY_EVENT,
    HeldOutReport,
    ShardReport,
    VerifyCandidate,
    VerifyResult,
    record_verify_event,
    selected_run_lines,
    sharded_result,
    suite_command,
    suite_job_script,
)
from kew.run_store import SqliteRunStore
from kew.verify_frontend import MODE_SCOPED, FrontendSelection
from kew.verify_ledger import (
    FLAKY_EVENT,
    LEDGER_FAILED_EVENT,
    RESOLVED_EVENT,
    SETUP,
    git_changed,
    ledger_payload,
    parse_since,
    phase_line,
    read_phases,
    report,
    resolve_after,
)
from kew.verify_suite import MODE_SELECTED, SuiteSelection

HEAD = "a" * 40
BASE = "b" * 40
PR = 7
TEST_ID = "tests/test_widget.py::test_spins"
HELD_ID = "tests/test_held.py::test_old_rule"

#: A PR body with no **Behavior changes** section, and the same body after an
#: edit that adds one declaring the held-out failure.
BODY = "## Summary\nA change.\n"
DECLARING_BODY = BODY + "\n## Behavior changes\n" + f"- {HELD_ID} — the rule moved\n"


def _payload(event: Any) -> dict[str, Any]:
    payload = event.payload
    return payload if isinstance(payload, dict) else json.loads(payload)


def _log(*phases: tuple[str, int, bool], start: int = 1_000) -> str:
    """A job log: ``setup`` first, then each (phase, seconds, ended)."""
    lines = [PREFLIGHT_PASSED_MARK, f"kew-phase: setup start {start}"]
    clock = start
    for name, seconds, ended in phases:
        lines.append(f"kew-phase: {name} start {clock}")
        clock += seconds
        if ended:
            lines.append(f"kew-phase: {name} end {clock}")
    return "\n".join(lines) + "\n"


def _candidate(**overrides: Any) -> VerifyCandidate:
    fields: dict[str, Any] = {
        "ref": "HEAD",
        "ref_sha": HEAD,
        "base_ref": "origin/main",
        "base_sha": BASE,
        "frontend_changed": False,
        "changed_paths": ("kew/src/kew/widget.py",),
        "image_inputs_changed": False,
    }
    fields.update(overrides)
    return VerifyCandidate(**fields)


def _summary(*lines: str, failed: int = 1) -> str:
    """pytest's short test summary: its header, *lines*, its final counts."""
    rule = "=" * 20
    return "\n".join(
        [f"{rule} short test summary info {rule}", *lines]
        + [f"{rule} {failed} failed in 1.00s {rule}", ""]
    )


def _red(seconds: float = 40.0) -> VerifyResult:
    log = _log(("static", 10, True), ("xdist", 25, False))
    log += _summary(f"FAILED {TEST_ID} - AssertionError")
    return VerifyResult(host="local", exit_code=1, seconds=seconds, log=log)


def _green(seconds: float = 60.0) -> VerifyResult:
    log = _log(("static", 10, True), ("xdist", 30, True), ("serial", 15, True))
    return VerifyResult(host="local", exit_code=0, seconds=seconds, log=log)


class _Ledger:
    """A ledger on disk, and the verifies recorded into it."""

    def __init__(self, store: SqliteRunStore, root: Path) -> None:
        self.store = store
        self.root = root
        self.gh = FakeGh()
        self.classes: list[str | None] = []

    def verify(
        self,
        result: VerifyResult,
        *,
        candidate: VerifyCandidate | None = None,
        body: str | None = BODY,
        pr: int | None = PR,
    ) -> int:
        """Record one verify bound to *pr* — as the drive's merge pass binds
        it — and resolve the one before it. Every test file is taken to exist
        at the head; the git answer has tests of its own."""
        receipt = record_verify_event(
            self.store,
            candidate or _candidate(),
            result,
            config=DispatchConfig(),
            pr_body=body,
            pr=pr,
        )
        assert receipt is not None
        self.classes.append(
            resolve_after(
                self.store,
                receipt,
                repo_root=self.root,
                repo="o/r",
                gh=self.gh,
                changed=lambda _old, _new: ["kew/src/kew/widget.py"],
                present=lambda _head, _path: True,
            )
        )
        return receipt

    def events(self, name: str) -> list[dict[str, Any]]:
        rows = self.store.list_audit_events(event=name)
        return [_payload(row) for row in reversed(rows)]


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        yield _Ledger(store, tmp_path)


def test_a_red_then_a_green_of_one_head_files_one_flaky_issue(ledger: Any) -> None:
    red = ledger.verify(_red())
    green = ledger.verify(_green())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["red"] == red
    assert resolved["next"] == green
    assert resolved["class"] == "flake"
    assert resolved["phase"] == "xdist"
    assert resolved["failing_ids"] == [TEST_ID]
    [create] = ledger.gh.creates()
    title = create[create.index("--title") + 1]
    body = create[create.index("--body") + 1]
    assert title == f"flaky: `{TEST_ID}`"
    assert create[create.index("--label") + 1] == "flaky"
    logs = [p["log_path"] for p in ledger.events(VERIFY_EVENT)]
    assert all(Path(path).is_file() for path in logs)
    # The logs by their place beside the ledger: no home path, no user name.
    shown = [f"verify-logs/{Path(path).name}" for path in logs]
    for text in (TEST_ID, f"audit row {red}", f"audit row {green}", *shown):
        assert text in body
    assert str(ledger.root) not in body
    assert str(Path.home()) not in body
    [filed] = ledger.events(FLAKY_EVENT)
    assert filed == {"test": TEST_ID, "issue": 101, "red": red, "green": green}


def test_a_third_verify_of_that_head_files_none(ledger: Any) -> None:
    ledger.verify(_red())
    ledger.verify(_green())
    ledger.verify(_green())

    assert len(ledger.gh.creates()) == 1
    assert len(ledger.events(RESOLVED_EVENT)) == 1
    assert ledger.classes == [None, "flake", None]


def test_a_second_flake_of_the_same_test_files_no_second_issue(ledger: Any) -> None:
    for _ in range(2):
        ledger.verify(_red())
        ledger.verify(_green())

    assert [p["class"] for p in ledger.events(RESOLVED_EVENT)] == ["flake", "flake"]
    assert len(ledger.gh.creates()) == 1


def test_a_body_edit_adding_behavior_changes_then_a_green_is_declared(
    ledger: Any,
) -> None:
    held_red = HeldOutReport(outcome="failed", failed=(HELD_ID,), undeclared=(HELD_ID,))
    red = VerifyResult(
        host="local",
        exit_code=1,
        seconds=20.0,
        log=_log(("static", 10, True), ("held_out", 8, True)),
        held_out=held_red,
    )
    held_green = HeldOutReport(outcome="failed", failed=(HELD_ID,), declared=(HELD_ID,))
    green = VerifyResult(
        host="local",
        exit_code=0,
        seconds=60.0,
        log=_green().log,
        held_out=held_green,
    )

    ledger.verify(red, body=BODY)
    ledger.verify(green, body=DECLARING_BODY)

    [first, _second] = ledger.events(VERIFY_EVENT)
    assert first["failing_phase"] == "held_out"
    assert first["failing_ids"] == [HELD_ID]
    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "declared"
    assert resolved["phase"] == "held_out"
    assert ledger.gh.creates() == []


class _Claim:
    """The merge claim ``kew verify --pr`` holds, as the event records it."""

    def __init__(self, pr: int) -> None:
        self.pr = pr
        self.heads = (HEAD,)

    def lapsed(self) -> bool:
        return False


def test_a_new_head_of_the_pr_that_passes_is_real(ledger: Any) -> None:
    receipt = record_verify_event(
        ledger.store, _candidate(), _red(), config=DispatchConfig(), claim=_Claim(7)
    )
    green = record_verify_event(
        ledger.store,
        _candidate(ref_sha="d" * 40),
        _green(),
        config=DispatchConfig(),
        claim=_Claim(7),
    )
    found = resolve_after(
        ledger.store,
        green,
        repo_root=ledger.root,
        repo="o/r",
        gh=ledger.gh,
        changed=lambda _old, _new: ["kew/tests/test_widget.py"],
    )

    assert found == "real"
    [resolved] = ledger.events(RESOLVED_EVENT)
    assert (resolved["red"], resolved["pr"]) == (receipt, 7)
    assert ledger.gh.creates() == []


def test_a_new_base_under_the_same_head_is_main(ledger: Any) -> None:
    ledger.verify(_red())
    ledger.verify(_green(), candidate=_candidate(base_sha="c" * 40))

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "main"
    assert ledger.gh.creates() == []


def test_an_incomplete_verify_is_infra(ledger: Any) -> None:
    gone = VerifyResult(
        host="gcp",
        exit_code=None,
        seconds=5.0,
        log="",
        error="the host went away",
        error_category="host_connection_lost",
    )
    ledger.verify(gone)
    ledger.verify(_green())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "infra"
    assert ledger.gh.creates() == []


def test_a_red_still_red_on_the_same_head_is_unknown(ledger: Any) -> None:
    ledger.verify(_red())
    ledger.verify(_red())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "unknown"


def test_only_a_change_in_code_tests_or_fixtures_is_real() -> None:
    from kew.verify_ledger import classify

    red = {"outcome": "failed", "gate_ran": True, "head": HEAD, "base_sha": BASE}
    green = {"outcome": "passed", "head": "f" * 40, "base_sha": BASE}
    assert classify(red, green, lambda _a, _b: ["kew/tests/test_x.py"]) == "real"
    assert classify(red, green, lambda _a, _b: ["docs/x.md"]) == "unknown"
    assert classify(red, green, lambda _a, _b: None) == "real"


def test_a_head_git_cannot_diff_reads_as_unread_and_says_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Not a repository: the diff is unread, never an empty one, and the
    warning names the heads it could not compare."""
    with caplog.at_level(logging.WARNING, logger="kew.verify_ledger"):
        assert git_changed(tmp_path)(HEAD, "f" * 40) is None
    assert f"cannot diff {HEAD}..{'f' * 40}" in caplog.text


def _job(label: str, log: str, *, exit_code: int, seconds: float) -> ShardReport:
    result = VerifyResult(host="gcp", exit_code=exit_code, seconds=seconds, log=log)
    return ShardReport(job=label, instance=f"vm-{label}", result=result, selected=1)


def test_a_sharded_verify_reads_its_parallel_jobs_side_by_side(ledger: Any) -> None:
    """Shards run at once: no phase outlasts the wall clock, and a shard that
    failed its slice of xdist is the failing phase, whatever job came after."""
    red_shard = _log(("xdist", 280, False), start=2_000)
    red_shard += _summary(f"FAILED {TEST_ID} - AssertionError")
    pool = sharded_result(
        [
            _job("1/2", red_shard, exit_code=1, seconds=300.0),
            _job("2/2", _log(("xdist", 290, True)), exit_code=0, seconds=300.0),
            _job("single", _log(("serial", 200, True)), exit_code=0, seconds=250.0),
        ],
        host="gcp",
        seconds=400.0,
    )
    gate = _log(("static", 60, True), ("held_out", 20, True), start=500)
    receipt = ledger.verify(replace(pool, log=gate + pool.log, seconds=400.0))

    [payload] = [
        _payload(row)
        for row in ledger.store.list_audit_events(event=VERIFY_EVENT)
        if row.id == receipt
    ]
    phases = payload["phases"]
    assert payload["failing_phase"] == "xdist"
    assert phases["xdist"] == {"seconds": 300.0, "outcome": "failed"}
    assert phases["static"] == {"seconds": 60.0, "outcome": "passed"}
    assert phases["serial"]["outcome"] == "passed"
    assert phases["setup"]["outcome"] == "passed"
    assert all(phase["seconds"] <= 400.0 for phase in phases.values())
    assert payload["failing_ids"] == [TEST_ID]


def test_phase_seconds_add_up_to_the_wall_time() -> None:
    log = _log(("static", 10, True), ("held_out", 5, True))
    log += _log(("xdist", 30, True), ("serial", 12, False), start=5_000)

    phases = read_phases(log, seconds=70.0, outcome="failed")

    assert list(phases) == ["setup", "static", "held_out", "xdist", "serial"]
    assert phases["static"] == {"seconds": 10.0, "outcome": "passed"}
    assert phases["xdist"] == {"seconds": 30.0, "outcome": "passed"}
    assert phases["serial"]["outcome"] == "failed"
    assert sum(p["seconds"] for p in phases.values()) == pytest.approx(70.0, rel=0.05)


def _fixture_ledger(store: SqliteRunStore, root: Path) -> None:
    """Four hand-run verifies of three heads, bound to no PR: each head is
    its own PR in the report's share of PRs changing a phase's inputs."""
    fixture = _Ledger(store, root)
    fixture.verify(_red(), pr=None)
    fixture.verify(_green(seconds=60.0), pr=None)
    fixture.verify(
        _green(seconds=80.0), candidate=_candidate(ref_sha="9" * 40), pr=None
    )
    fixture.verify(
        VerifyResult(
            host="local",
            exit_code=1,
            seconds=12.0,
            log=_log(("static", 12, False)) + "error: ruff\n",
        ),
        candidate=_candidate(ref_sha="8" * 40, changed_paths=("frontend/a.ts",)),
        pr=None,
    )


#: What the report prints for :func:`_fixture_ledger`, every column in a fixed
#: order: phases in the order a verify runs them, classes in theirs.
_REPORT_TABLES = """\
phase   real  declared  flake  main  infra  unknown  pending  total
static     0         0      0     0      0        0        1      1
xdist      0         0      1     0      0        0        0      1

Passing verifies by phase (2):
phase   p50 s  p90 s  PRs changing inputs
setup       5     25                    -
static     10     10                  67%
xdist      30     30                  67%
serial     15     15                  67%
"""


def test_the_report_prints_reds_by_phase_and_class_in_a_stable_order(
    tmp_path: Path,
) -> None:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _fixture_ledger(store, tmp_path)
        text = report(store, since=parse_since("30d"))
        again = report(store, since=parse_since("30d"))

    assert text == again
    first, rest = text.split("\n", 1)
    assert first.startswith("Red verifies by phase and class since ")
    assert first.endswith("UTC (2 of 4):")
    assert rest == _REPORT_TABLES


def test_kew_verify_report_prints_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = {str(tmp_path / "logs")!r}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        _fixture_ledger(store, tmp_path)

    result = CliRunner().invoke(app, ["verify", "report", "--since", "30d"])

    assert result.exit_code == 0, result.output
    assert result.output.endswith(_REPORT_TABLES)


def test_kew_verify_report_refuses_a_span_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["verify", "report", "--since", "a month"])

    assert result.exit_code == 2
    assert "30d" in result.output


# ── what a pair of verifies may be read as ──────────────────────────────────


def _selected(*files: str) -> SuiteSelection:
    return SuiteSelection(
        mode=MODE_SELECTED, reason="tiered", files=tuple(files), tiered=True
    )


def test_a_green_selection_that_never_ran_the_failing_test_files_nothing(
    ledger: Any,
) -> None:
    """A full verify of the head fails a test; a selected verify of the same
    head, whose selection does not hold that test's file, passes. The test
    never ran the second time, so nothing says it flaked."""
    ledger.verify(_red())
    ledger.verify(
        _green(), candidate=_candidate(suite=_selected("tests/test_other.py"))
    )

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "unknown"
    assert ledger.gh.creates() == []
    assert ledger.events(FLAKY_EVENT) == []


def test_a_flake_needs_both_verifies_to_have_run_the_full_suite(ledger: Any) -> None:
    """A selected red that ran the test, then a full green: still no flake."""
    ledger.verify(_red(), candidate=_candidate(suite=_selected("tests/test_widget.py")))
    ledger.verify(_green())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "unknown"
    assert ledger.gh.creates() == []


def _red_of(count: int) -> VerifyResult:
    """A red whose xdist phase failed *count* tests."""
    log = _log(("static", 10, True), ("xdist", 25, False))
    log += _summary(
        *(
            f"FAILED tests/test_disk.py::test_{n} - OSError: [Errno 28] No space"
            for n in range(count)
        ),
        failed=count,
    )
    return VerifyResult(host="local", exit_code=1, seconds=40.0, log=log)


@pytest.mark.parametrize(
    ("failing", "found", "issues"),
    [(3, "flake", 3), (5, "flake", 5), (6, "infra", 0), (300, "infra", 0)],
)
def test_a_red_with_more_than_five_failing_ids_is_infra_and_files_nothing(
    ledger: Any, failing: int, found: str, issues: int
) -> None:
    """Hundreds of ids failing together (a full disk) are one cause, not
    hundreds of flakes: no issue at all. A few each get their own."""
    ledger.verify(_red_of(failing))
    ledger.verify(_green())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == found
    creates = ledger.gh.creates()
    assert len(creates) == issues
    titles = [create[create.index("--title") + 1] for create in creates]
    assert titles == [f"flaky: `tests/test_disk.py::test_{n}`" for n in range(issues)]


def test_a_drive_red_fixed_by_a_push_to_the_same_pr_is_real(ledger: Any) -> None:
    """The drive holds no merge claim but names its PR: its red on one head
    and its green on the PR's next head pair, and the code change between
    them makes the red real."""
    red = ledger.verify(_red())
    ledger.verify(_green(), candidate=_candidate(ref_sha="d" * 40))

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert (resolved["red"], resolved["class"], resolved["pr"]) == (red, "real", PR)
    first, second = ledger.events(VERIFY_EVENT)
    assert (first["pr"], second["pr"]) == (PR, PR)
    assert "merge_claim" not in first
    assert ledger.gh.creates() == []


def test_a_body_edit_declaring_the_red_is_declared_though_main_moved(
    ledger: Any,
) -> None:
    """Same head, the **Behavior changes** section now declares the held-out
    failure, held-out passed: declared, whatever main did meanwhile."""
    held_red = HeldOutReport(outcome="failed", failed=(HELD_ID,), undeclared=(HELD_ID,))
    red = VerifyResult(
        host="local",
        exit_code=1,
        seconds=20.0,
        log=_log(("static", 10, True), ("held_out", 8, True)),
        held_out=held_red,
    )
    held_green = HeldOutReport(outcome="failed", failed=(HELD_ID,), declared=(HELD_ID,))
    green = replace(_green(), held_out=held_green)

    ledger.verify(red, body=BODY)
    ledger.verify(green, candidate=_candidate(base_sha="c" * 40), body=DECLARING_BODY)

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "declared"


def test_a_verify_given_no_body_reads_the_section_as_unknown_not_changed(
    ledger: Any,
) -> None:
    """A hand-run verify without ``--pr-body`` records no digest; the PR's
    next verify, given a body with a section, is not a body change."""
    ledger.verify(_red(), body=None)
    ledger.verify(_green(), body=DECLARING_BODY)

    first, second = ledger.events(VERIFY_EVENT)
    assert first["behavior_changes"] is None
    assert isinstance(second["behavior_changes"], str)
    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "flake"


def test_a_selected_verify_whose_policy_run_fails_records_the_policy_phase(
    tmp_path: Path,
) -> None:
    """The suite job's own command, run against a stand-in verify.sh and a
    ``uv`` whose ``-m policy`` run fails: the red is the policy phase's, not
    setup's, and xdist and serial passed."""
    suite = _selected("tests/test_widget.py")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "verify.sh").write_text("exit 0\n", encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    uv = tools / "uv"
    summary = _summary("FAILED tests/test_rules.py::test_rule - AssertionError")
    uv.write_text(
        '#!/bin/sh\ncase "$*" in *"-m policy"*)\n'
        f"  cat <<'EOF'\n{summary}EOF\n"
        "  exit 1;;\nesac\nexit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    script = "\n".join(
        [phase_line(SETUP, "start"), *selected_run_lines(suite), suite_command(suite)]
    )

    done = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **environ_without_switches(),
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 1, done.stderr
    result = VerifyResult(
        host="local",
        exit_code=1,
        seconds=1.0,
        log=f"{PREFLIGHT_PASSED_MARK}\n{done.stdout}",
    )
    payload = ledger_payload(_candidate(suite=suite), result, pr_body=BODY)
    assert payload["failing_phase"] == "policy"
    assert payload["failing_ids"] == ["tests/test_rules.py::test_rule"]
    assert payload["phases"]["xdist"]["outcome"] == "passed"
    assert payload["phases"]["serial"]["outcome"] == "passed"


def test_a_kept_log_older_than_thirty_days_is_pruned_on_the_next_write(
    ledger: Any,
) -> None:
    folder = ledger.root / "logs" / "verify-logs"
    folder.mkdir()
    old, recent = folder / "old.log", folder / "recent.log"
    now = time.time()
    for path, days in ((old, 31), (recent, 29)):
        path.write_text("a log\n", encoding="utf-8")
        os.utime(path, (now - days * 86400, now - days * 86400))

    ledger.verify(_green())

    [payload] = ledger.events(VERIFY_EVENT)
    assert not old.exists()
    assert recent.exists()
    assert Path(payload["log_path"]).parent == folder.resolve()
    assert Path(payload["log_path"]).is_file()


def test_a_bare_verify_bound_to_no_pr_files_nothing(ledger: Any) -> None:
    """A hand-run ``kew verify <ref>`` names no PR: its flake is classed, and
    nothing reaches GitHub."""
    ledger.verify(_red(), pr=None)
    ledger.verify(_green(), pr=None)

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "flake"
    assert ledger.gh.calls == []
    assert ledger.events(FLAKY_EVENT) == []


def test_the_report_leaves_out_verifies_recorded_before_the_ledger(
    tmp_path: Path,
) -> None:
    """A verify recorded before kew kept phases has no head and no phases:
    it is not a pending red, and the report says how many it left out."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        emit(
            store,
            VERIFY_EVENT,
            actor=resolve_actor(),
            payload={
                "kind": "pr",
                "ref_sha": HEAD,
                "exit_code": 1,
                "outcome": "failed",
            },
        )
        _fixture_ledger(store, tmp_path)
        text = report(store, since=parse_since("30d"))

    first, second, rest = text.split("\n", 2)
    assert first.endswith("UTC (2 of 4):")
    assert second == "(left out: 1 verify recorded before kew kept phases)"
    assert rest == _REPORT_TABLES


# ── what may reach a public issue ───────────────────────────────────────────

#: A summary line a candidate's test output can forge: a mention and an
#: image outside the code span the id would sit in.
_HOSTILE = "tests/t.py::x` @octocat ![p](https://evil.example/p.png) `y"


def _red_failing(*ids: str, captured: str = "") -> VerifyResult:
    log = _log(("static", 10, True), ("xdist", 25, False)) + captured
    log += _summary(*(f"FAILED {test}" for test in ids), failed=len(ids))
    return VerifyResult(host="local", exit_code=1, seconds=40.0, log=log)


def _skips(ledger: Any) -> list[dict[str, Any]]:
    return [p for p in ledger.events(LEDGER_FAILED_EVENT) if p["step"] == "skip"]


def test_a_forged_id_with_a_mention_and_an_image_is_never_filed(ledger: Any) -> None:
    ledger.verify(_red_failing(_HOSTILE))
    ledger.verify(_green())

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "flake"
    assert ledger.gh.calls == []
    [skip] = _skips(ledger)
    assert (skip["test"], skip["reason"]) == (_HOSTILE, "the id is not a plain test id")


def test_an_id_only_in_captured_output_is_never_a_failing_id(ledger: Any) -> None:
    """What a test printed comes before pytest's summary: a FAILED line in it
    is not read, and only the summary's own id is filed."""
    captured = (
        "----- Captured stdout call -----\n"
        "FAILED tests/test_injected.py::test_planted - AssertionError\n"
    )
    ledger.verify(_red_failing(TEST_ID, captured=captured))
    ledger.verify(_green())

    first, _second = ledger.events(VERIFY_EVENT)
    assert first["failing_ids"] == [TEST_ID]
    titles = [c[c.index("--title") + 1] for c in ledger.gh.creates()]
    assert titles == [f"flaky: `{TEST_ID}`"]


def _git_head(repo: Path, files: dict[str, str]) -> str:
    """A repository at *repo* whose one commit holds *files*; its sha."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(repo)], check=True)
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding="utf-8")
    subprocess.run([*git, "-C", str(repo), "add", "--all"], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "head"], check=True)
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _verify_in_git(
    store: SqliteRunStore, repo: Path, head: str, result: VerifyResult, gh: FakeGh
) -> None:
    """Record a PR-bound verify of *head* and resolve the one before it, the
    test file read with git in *repo*, as a real verify reads it."""
    from kew import subprocess_utils

    subprocess_utils._register_optional_tool("git")
    receipt = record_verify_event(
        store,
        _candidate(ref_sha=head),
        result,
        config=DispatchConfig(),
        pr_body=BODY,
        pr=PR,
    )
    resolve_after(store, receipt, repo_root=repo, repo="o/r", gh=gh)


def test_a_test_file_missing_at_the_head_is_never_filed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _git_head(repo, {"kew/tests/test_widget.py": "def test_spins(): ...\n"})
    gone = "tests/test_gone.py::test_spins"
    gh = FakeGh()
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _verify_in_git(store, repo, head, _red_failing(gone), gh)
        _verify_in_git(store, repo, head, _green(), gh)
        skips = [
            _payload(row)
            for row in store.list_audit_events(event=LEDGER_FAILED_EVENT)
            if _payload(row)["step"] == "skip"
        ]

    assert gh.calls == []
    assert [(s["test"], s["reason"]) for s in skips] == [
        (gone, "its test file does not exist at the head")
    ]


def test_a_plain_id_whose_file_exists_at_the_head_is_filed_fenced(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    head = _git_head(repo, {"kew/tests/test_widget.py": "def test_spins(): ...\n"})
    gh = FakeGh()
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _verify_in_git(store, repo, head, _red_failing(TEST_ID), gh)
        _verify_in_git(store, repo, head, _green(), gh)

    [create] = gh.creates()
    assert create[create.index("--title") + 1] == f"flaky: `{TEST_ID}`"
    body = create[create.index("--body") + 1]
    assert body.startswith(f"`{TEST_ID}` failed, then passed")


def test_whole_second_stamps_that_outlast_the_wall_time_are_scaled_to_it() -> None:
    """Two jobs whose stamps round up: 10 marked seconds in an 8.5-second
    verify. The phases still add up to the wall time, in the same ratio."""
    log = _log(("static", 4, True)) + _log(("xdist", 6, True), start=5_000)

    phases = read_phases(log, seconds=8.5, outcome="passed")

    assert sum(p["seconds"] for p in phases.values()) == pytest.approx(8.5)
    assert phases["xdist"]["seconds"] == pytest.approx(
        1.5 * phases["static"]["seconds"]
    )


def test_an_id_with_backticks_stays_inside_its_code_span() -> None:
    """The fence is longer than any backtick run in the id, so a regression
    issue's failing id renders as code, mention and image included."""
    from kew.main_health import Regression, code_span, regression_body

    assert code_span(TEST_ID) == f"`{TEST_ID}`"
    assert code_span(_HOSTILE) == f"``{_HOSTILE}``"
    assert code_span("`x`") == "`` `x` ``"
    body = regression_body(Regression("c" * 40, (_HOSTILE,)))
    assert f"- ``{_HOSTILE}``" in body.splitlines()


# ── a scoped frontend (#3213) ───────────────────────────────────────────────


def _scoped() -> FrontendSelection:
    return FrontendSelection(
        mode=MODE_SCOPED,
        reason="scoped",
        modules=("console",),
        related=("src/lib/a.ts",),
        scoped_setting=True,
    )


def test_a_green_with_a_scoped_frontend_is_no_flake(ledger: Any) -> None:
    """A scoped frontend ran only what the change owns: the pair is not two
    full verifies, so it is not a flake and files nothing."""
    ledger.verify(_red())
    ledger.verify(_green(), candidate=_candidate(frontend=_scoped()))

    [resolved] = ledger.events(RESOLVED_EVENT)
    assert resolved["class"] == "unknown"
    assert ledger.gh.creates() == []


def _ui_check_part(candidate: VerifyCandidate) -> str:
    """The suite job's ui-check phase, as the job script has it."""
    lines = suite_job_script(candidate).splitlines()
    start = lines.index(f'echo "{JOB_PHASE_MARK} {PHASE_UI_CHECK}"')
    end = lines.index(phase_line("ui_check", "end"))
    return "\n".join(["set -eu", *lines[start : end + 1]]) + "\n"


@pytest.mark.parametrize(
    ("related_exit", "failing", "ran"),
    [(1, "ui_check", {"ui_check"}), (0, None, {"ui_check"})],
)
def test_a_scoped_vitest_related_run_is_part_of_the_ui_check_phase(
    tmp_path: Path, related_exit: int, failing: str | None, ran: set[str]
) -> None:
    """The suite job runs ``vitest related`` inside its ui-check phase: its
    seconds and its failure are ui_check's, never setup's."""
    candidate = _candidate(frontend_changed=True, frontend=_scoped())
    for folder in ("frontend", "kew", "bin"):
        (tmp_path / folder).mkdir()
    for tool, status in (("npx", related_exit), ("npm", 0)):
        stub = tmp_path / "bin" / tool
        stub.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
        stub.chmod(0o755)

    done = subprocess.run(
        ["bash", "-c", _ui_check_part(candidate)],
        cwd=tmp_path / "kew",
        env={
            **environ_without_switches(),
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    # The job opened setup at its first line; this part runs after it.
    log = f"{PREFLIGHT_PASSED_MARK}\nkew-phase: setup start 0\n{done.stdout}"
    result = VerifyResult(host="local", exit_code=done.returncode, seconds=1.0, log=log)
    payload = ledger_payload(candidate, result, pr_body=BODY)
    assert done.returncode == related_exit, done.stderr
    assert payload["failing_phase"] == failing
    assert ran <= set(payload["phases"])
