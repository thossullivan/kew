"""Held-out runs before a reviewer launches (#3212).

Real git (a bare origin, a working clone, a pushed agent branch), a real run
store and the real review stage. Two boundaries are faked: the forge, which
answers the PR body, and the remote verify host's process
(:mod:`support.fake_verify_host`), which answers the held-out phase's log as
the host prints it. Everything kew does between them — the throwaway merge
with the base, the job script, the control run on the base, the verdict
against the PR body, the round and the brief — is kew's own. The local host is
never used: the check does not run head code on this machine.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.container_runtime import runtime_present
from support.fake_verify_host import FakeVerifyHost, install
from support.held_out_log import opened, report_line
from support.review_stream import stream_verdict

from kew.audit import Actor, emit, resolve_actor
from kew.behavior_declarations import load_held_out_request, requested_lines
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig, VerifyConfig
from kew.graph.driver import review_stage
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.pre_review_gate import (
    FULL_SELECTION_REASON,
    HELD_OUT_GATE,
    HELD_OUT_HALF,
    STATIC_HALF,
    gate_skip_reason,
    settled_halves,
)
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.hosts import HostError, Job, LocalProvider
from kew.hosts.lock import host_lock
from kew.loops import verification
from kew.main_health import NIGHTLY_KIND
from kew.merge_verify import (
    HELD_OUT_CHECK_LOCAL_REFUSAL,
    VERIFY_EVENT,
    declared_behavior_changes,
)
from kew.review import build_review_prompt
from kew.review_contract import CollectedSidecar
from kew.review_delta import REVIEW_MODE_PRE_REVIEW_GATE
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import WORK_MOUNT_PATH
from kew.subprocess_utils import TOOL_PATHS
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

EPIC = "epic:x"
ITEM = "42"
BRANCH = "agent/issue-42"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

#: The base test the agent's head changes the behaviour of.
PINNED = "tests/test_pinned.py::test_the_greeting_is_pinned"

#: What the verify host prints when the base's copy of *PINNED* fails.
_HELD_OUT_RED = opened(
    f"{report_line(PINNED)}kew-held-out: exit 1 seconds 2\n"
    f"{report_line()}kew-held-out: exit 0 seconds 1\n"
)

#: What it prints when every base test it ran passed.
_HELD_OUT_CLEAN = opened(
    f"{report_line(passed=[PINNED])}kew-held-out: exit 0 seconds 2\n"
    f"{report_line()}kew-held-out: exit 0 seconds 1\n"
)

#: The control run on the base names the one test it re-runs by node id; the
#: run on the merge candidate names the changed test file only.
_CONTROL = PINNED

#: The host's answers when the base passes the test the head breaks.
_HEADS_RED = ((_CONTROL, 0, _HELD_OUT_CLEAN), ("", 0, _HELD_OUT_RED))

_DECLARED_BODY = (
    "Closes #42\n\n**Behavior changes**\n\n"
    f"- {PINNED} — the greeting is now formal, as the issue asks\n"
)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(work: Path, message: str) -> str:
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _repo(tmp_path: Path, extra: dict[str, str] | None = None) -> tuple[Path, str]:
    """A bare origin with ``main`` and an agent branch that changes a pinned
    behaviour, edits the pin to match, and declares nothing."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    _write(work, "kew/src/app/__init__.py", "GREETING = 'hi'\n")
    _write(
        work,
        "kew/tests/test_pinned.py",
        "from app import GREETING\n\n\ndef test_the_greeting_is_pinned():\n"
        "    assert GREETING == 'hi'\n",
    )
    _commit(work, "base")
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-qb", BRANCH, cwd=work)
    _write(work, "kew/src/app/__init__.py", "GREETING = 'good day'\n")
    _write(
        work,
        "kew/tests/test_pinned.py",
        "from app import GREETING\n\n\ndef test_the_greeting_is_pinned():\n"
        "    assert GREETING == 'good day'\n",
    )
    for rel, text in (extra or {}).items():
        _write(work, rel, text)
    head = _commit(work, "agent work")
    _git("push", "-q", "origin", BRANCH, cwd=work)
    _git("checkout", "-q", "main", cwd=work)
    return work, head


def _config(tmp_path: Path, *, host: str = "gcp", static: str = "") -> DispatchConfig:
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    config.verify.held_out_before_review = True
    config.verify.host = host
    if static:
        config.commands.static = static
        config.sandbox.images = {"claude-code": "approved:tag"}
    return config


def _facts(head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


class _Reviewer:
    """A reviewer launcher that records each launch and answers READY."""

    def __init__(self) -> None:
        self.launched: list[str] = []

    def __call__(self, ctx: ReviewerLaunchContext) -> CollectedSidecar:
        self.launched.append(ctx.head_sha)
        evidence = {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
        return stream_verdict(
            ctx, {"verdict": "READY", "findings": [], "acceptance_evidence": [evidence]}
        )


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


def _host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: Any = _HEADS_RED,
    **behaviour: bool,
) -> FakeVerifyHost:
    return install(
        monkeypatch, FakeVerifyHost(tmp_path / "locks", answers, **behaviour)
    )


def _tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    work: Path,
    head: str,
    reviewer: _Reviewer,
    remediations: list[RemediationContext],
) -> Any:
    return drive_review_stage(
        {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=work,
        base="main",
        reviewer_launcher=reviewer,
        remediator=remediations.append,
        worker_cap=0,
        now=NOW,
    )


def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str | None,
    answers: Any = _HEADS_RED,
    extra: dict[str, str] | None = None,
    seed: Any = None,
    static: str = "",
    **behaviour: bool,
) -> tuple[Any, _Reviewer, list[RemediationContext], FakeVerifyHost, SqliteRunStore]:
    work, head = _repo(tmp_path, extra)
    host = _host(tmp_path, monkeypatch, answers, **behaviour)
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _repo, _number: body)
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []
    store = SqliteRunStore(str(tmp_path / "d.db"))
    if seed is not None:
        seed(store, work)
    config = _config(tmp_path, static=static)
    outcome = _tick(store, config, work, head, reviewer, remediations)
    return (outcome, head), reviewer, remediations, host, store


def test_an_undeclared_held_out_failure_is_a_remediation_round_and_no_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body="Closes #42\n"
    )
    with store:
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.head_sha, latest.review_mode) == (
            "NEEDS_CHANGES",
            head,
            REVIEW_MODE_PRE_REVIEW_GATE,
        )
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert reviewer.launched == []
    assert outcome.remediating == [ITEM]
    # The held-out phase ran on the verify host, alone, on the merge candidate,
    # and once more on the base for the failing test alone: the base passed it.
    candidate_run, control_run = host.commands
    assert "kew-held-out: begin" in candidate_run
    assert "scripts/verify.sh" not in candidate_run
    assert PINNED not in candidate_run and PINNED in control_run
    assert all(job.timeout_seconds == 1200.0 for job in host.jobs)
    # The round carries the failing id, the body's section and the rule.
    [ctx] = remediations
    assert ctx.head_sha == head
    assert ctx.declaration is not None and ctx.declaration.held_out
    assert ctx.declaration.node_ids == (PINNED,)
    assert ctx.sidecar_path is not None
    findings = _sidecar_findings(ctx.sidecar_path)
    assert [f["location"] for f in findings] == ["kew/tests/test_pinned.py:1"]
    assert HELD_OUT_GATE in findings[0]["summary"]
    assert PINNED in findings[0]["summary"]
    assert "Behavior changes" in findings[0]["summary"]


def test_the_same_pr_with_the_declaration_goes_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body=_DECLARED_BODY
    )
    with store:
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.review_mode) == ("READY", "full")
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)

    assert len(host.jobs) == 1
    assert reviewer.launched == [head]
    assert remediations == []


def test_an_unreadable_pr_body_runs_no_held_out_and_reviews_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that does not read cannot say what is declared: the check sits
    the head out rather than send declared failures back."""
    (_outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body=None
    )
    with store:
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert host.jobs == []
    assert reviewer.launched == [head]
    assert remediations == []


def test_a_host_that_reported_nothing_is_not_a_defect_in_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No report is no answer: the head goes to review, and the check is not
    remembered for it, so the next review of the head runs it again."""
    (_outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body="Closes #42\n", answers=[("", 0, "nothing\n")]
    )
    with store:
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert len(host.jobs) == 1
    assert reviewer.launched == [head]
    assert remediations == []


def test_the_brief_asks_to_fix_or_declare_and_the_declared_line_is_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remediation sees the failing ids, the body's current section and
    the rule; the line its report declares for a requested id is the one kew
    carries to the PR body, and a line for any other id is not."""
    body = (
        "Closes #42\n\n**Behavior changes**\n\n- tests/test_other.py::test_x — kept\n"
    )
    _outcome, _reviewer, remediations, _host, store = _drive(
        tmp_path, monkeypatch, body=body
    )
    store.close()
    [ctx] = remediations
    request = load_held_out_request(ctx.sidecar_path)
    assert request == ctx.declaration

    prompt = build_review_prompt(
        pr_number=7,
        issue_number=42,
        branch_name=BRANCH,
        comments=[],
        config=_config(tmp_path),
        declaration=request,
    )
    assert "Base tests that fail against this head" in prompt
    assert f"- `{PINNED}`" in prompt
    assert "tests/test_other.py::test_x — kept" in prompt
    assert "fix the source so the base test passes" in prompt
    assert "no code change" not in prompt.lower()

    report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {PINNED} — the greeting is now formal\n"
        "- tests/test_unasked.py::test_y — not asked for\n"
    )
    assert request is not None
    assert requested_lines(request, report) == [
        f"- {PINNED} — the greeting is now formal"
    ]


def test_a_head_that_edits_a_gate_definition_runs_no_held_out_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head that edits the lock would have the verify host install what it
    names before any person released its merge-gate hold: it runs nothing."""
    (_outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path,
        monkeypatch,
        body="Closes #42\n",
        extra={"kew/uv.lock": "version = 1\n"},
    )
    with store:
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)

    assert host.jobs == []
    assert reviewer.launched == [head]
    assert remediations == []


def test_a_declaration_the_remediation_landed_sends_the_head_on_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round is answered once the PR body carries the line: the next tick
    runs held-out again against that body, and a reviewer launches. It is
    never remediated a second time at the same head."""
    body = {"text": "Closes #42\n"}
    work, head = _repo(tmp_path)
    host = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(
        review_stage, "read_pr_body", lambda _repo, _number: body["text"]
    )
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []
    config = _config(tmp_path)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, config, work, head, reviewer, remediations)
        assert len(remediations) == 1 and reviewer.launched == []
        # kew carried the remediation's line into the PR body.
        body["text"] = _DECLARED_BODY
        _tick(store, config, work, head, reviewer, remediations)
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.review_mode) == ("READY", "full")

    assert len(remediations) == 1
    assert reviewer.launched == [head]
    # The first tick: the candidate and the control run on the base. The
    # second: the candidate alone, whose failure the body now declares.
    assert len(host.jobs) == 3


def _due_again(store: SqliteRunStore) -> None:
    """A READY on another head: this head is due a reviewer once more."""
    store.record_review_round(
        EPIC, ITEM, head_sha="0" * 40, verdict="READY", review_mode="full"
    )


def _said(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def test_on_the_local_host_no_head_code_runs_and_the_skip_is_said_once_per_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check imports the head's src before any review, so it never runs
    on this machine: it is skipped with the reason, which is said once for
    the head, and the head goes to review."""
    work, head = _repo(tmp_path)
    ran: list[str] = []

    def execute(_provider: LocalProvider, job: Job) -> tuple[int, str]:
        ran.append(job.command)
        return 0, _HELD_OUT_RED

    monkeypatch.setattr(LocalProvider, "_execute", execute)
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _r, _n: "Closes #42\n")
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []
    config = _config(tmp_path, host="local")

    with caplog.at_level("WARNING"), SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, config, work, head, reviewer, remediations)
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)
        _due_again(store)
        _tick(store, config, work, head, reviewer, remediations)

    assert ran == []
    assert remediations == []
    assert reviewer.launched == [head, head]
    assert len(_said(caplog, HELD_OUT_CHECK_LOCAL_REFUSAL)) == 1


def test_a_busy_verify_host_skips_the_check_without_remembering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host another job holds answers nothing about the head: it goes to
    review, and the next review of the head runs the check again."""
    work, head = _repo(tmp_path)
    host = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _r, _n: "Closes #42\n")
    reviewer = _Reviewer()
    remediations: list[RemediationContext] = []

    with (
        host_lock(tmp_path / "locks", host.lock_key),
        SqliteRunStore(str(tmp_path / "d.db")) as store,
    ):
        _tick(store, _config(tmp_path), work, head, reviewer, remediations)
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert host.jobs == []
    assert remediations == []
    assert reviewer.launched == [head]


def test_a_base_test_that_fails_on_the_base_too_is_mains_and_never_sent_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure the base's own src shares is not the agent's: no remediation
    round, no hold, and the log says whose it is."""
    with caplog.at_level("WARNING"):
        (outcome, head), reviewer, remediations, host, store = _drive(
            tmp_path, monkeypatch, body="Closes #42\n", answers=[("", 0, _HELD_OUT_RED)]
        )
    with store:
        assert store.latest_review_round(ITEM) is not None
        assert _nothing_sent_back(store)
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)

    assert len(host.jobs) == 2 and PINNED in host.commands[1]
    assert remediations == []
    assert outcome.held == []
    assert reviewer.launched == [head]
    [said] = _said(caplog, "red on main too: not the agent's")
    assert PINNED in said


def _nothing_sent_back(store: SqliteRunStore) -> bool:
    """No round the pre-review step recorded: nothing was sent back."""
    return all(
        round_.review_mode != REVIEW_MODE_PRE_REVIEW_GATE
        for round_ in store.review_rounds_in_cycle(ITEM)
    )


def _red_nightly(store: SqliteRunStore, work: Path) -> None:
    """Main's health reads red at main's tip, with the pinned test failing."""
    emit(
        store,
        VERIFY_EVENT,
        actor=resolve_actor(),
        payload={
            "kind": NIGHTLY_KIND,
            "suite": "full",
            "outcome": "failed",
            "ref_sha": _git("rev-parse", "origin/main", cwd=work),
            "failing_tests": [PINNED],
        },
    )


def test_a_test_mains_health_lists_as_red_is_never_the_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test the nightly found red on main is main's: it is not sent back,
    and needs no control run to say so."""
    (outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body="Closes #42\n", seed=_red_nightly
    )
    with store:
        assert _nothing_sent_back(store)

    assert len(host.jobs) == 1
    assert remediations == []
    assert outcome.held == []
    assert reviewer.launched == [head]


def test_a_change_that_selects_the_whole_base_tree_is_left_to_the_merge_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A change under tests/support/ selects every base test: the merge verify
    runs that tree anyway, so the check before review does not, on this head
    or on any later review of it."""
    with caplog.at_level("WARNING"):
        (_outcome, head), reviewer, remediations, host, store = _drive(
            tmp_path,
            monkeypatch,
            body="Closes #42\n",
            extra={"kew/tests/support/helpers.py": "X = 1\n"},
        )
    with store:
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)

    assert host.jobs == []
    assert remediations == []
    assert reviewer.launched == [head]
    assert _said(caplog, FULL_SELECTION_REASON)


def test_a_run_past_its_own_ceiling_is_skipped_not_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs under its own 20-minute ceiling, not the merge verify's
    two hours; a run that outruns it says nothing about the head."""
    (_outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path, monkeypatch, body="Closes #42\n", expire=True
    )
    with store:
        assert _nothing_sent_back(store)
        assert HELD_OUT_HALF in settled_halves(store, ITEM, head)

    [job] = host.jobs
    assert job.timeout_seconds == 1200.0
    assert remediations == []
    assert reviewer.launched == [head]


def test_a_host_that_starts_too_slowly_skips_the_check_without_remembering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A gcloud call that timed out bringing the instance up is a host that
    did not answer, not a run past its ceiling: the next review runs it."""
    with caplog.at_level("WARNING"):
        (_outcome, head), reviewer, remediations, host, store = _drive(
            tmp_path, monkeypatch, body="Closes #42\n", slow_start=True
        )
    with store:
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert host.jobs == []
    assert remediations == []
    assert reviewer.launched == [head]
    assert _said(caplog, "did not come up or take the bundle in time")
    assert not _said(caplog, "ceiling")


#: A case the head's src parametrizes into being: the base has no such case.
_NEW_CASE = "tests/test_pinned.py::test_reg[new]"

#: What the merge candidate's run prints: the new case and the pinned test fail.
_TWO_RED = opened(
    f"{report_line(_NEW_CASE, PINNED)}kew-held-out: exit 1 seconds 2\n"
    f"{report_line()}kew-held-out: exit 5 seconds 1\n"
)

#: The control run on the base, given the test functions: the pinned test is
#: red on main too, and the base has no ``[new]`` case to fail.
_PINNED_RED_ON_BASE = opened(
    f"{report_line(PINNED)}kew-held-out: exit 1 seconds 2\n"
    f"{report_line()}kew-held-out: exit 5 seconds 1\n"
)


def test_a_case_the_base_lacks_is_the_heads_and_a_test_red_on_the_base_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control run names test functions, so the case only the head has
    neither breaks it nor hides: it stays a finding, and the test that fails
    on the base too is main's."""
    (_outcome, head), reviewer, remediations, host, store = _drive(
        tmp_path,
        monkeypatch,
        body="Closes #42\n",
        answers=[("::", 0, _PINNED_RED_ON_BASE), ("", 0, _TWO_RED)],
    )
    store.close()

    candidate_run, control_run = host.commands
    assert "tests/test_pinned.py::test_reg" in control_run
    assert "[new]" not in control_run
    assert reviewer.launched == []
    [ctx] = remediations
    assert ctx.declaration is not None
    assert ctx.declaration.node_ids == (_NEW_CASE,)
    assert ctx.sidecar_path is not None
    [finding] = _sidecar_findings(ctx.sidecar_path)
    assert _NEW_CASE in finding["summary"] and PINNED not in finding["summary"]


def test_a_control_run_that_breaks_sends_nothing_back_and_is_not_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A control run whose runs exit 4 says nothing about whose the failure
    is: no remediation round, no hold, and the next review runs it again."""
    broken = opened("kew-held-out: exit 4 seconds 1\n" * 2)
    with caplog.at_level("WARNING"):
        (outcome, head), reviewer, remediations, host, store = _drive(
            tmp_path,
            monkeypatch,
            body="Closes #42\n",
            answers=[("::", 0, broken), ("", 0, _HELD_OUT_RED)],
        )
    with store:
        assert _nothing_sent_back(store)
        assert HELD_OUT_HALF not in settled_halves(store, ITEM, head)

    assert len(host.jobs) == 2
    assert remediations == []
    assert outcome.held == []
    assert reviewer.launched == [head]
    assert _said(caplog, "reached no verdict")


@pytest.mark.parametrize("ceiling", [0, -1])
def test_the_pre_review_run_always_has_a_ceiling(ceiling: int) -> None:
    with pytest.raises(ValueError, match="held_out_before_review_timeout_seconds"):
        VerifyConfig(held_out_before_review_timeout_seconds=ceiling)


def test_a_round_with_no_held_out_record_asks_for_nothing_and_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reviewer's round records no held-out request: that is no request,
    and nothing to warn about."""
    sidecar = tmp_path / "round.json"
    sidecar.write_text("{}", encoding="utf-8")

    with caplog.at_level("WARNING", logger="kew.behavior_declarations"):
        assert load_held_out_request(str(sidecar)) is None

    assert caplog.records == []


def _write_bad_json(record: Path) -> None:
    record.write_text("{not json", encoding="utf-8")


def _write_without_node_ids(record: Path) -> None:
    record.write_text(json.dumps({"section": []}), encoding="utf-8")


def _make_a_directory(record: Path) -> None:
    record.mkdir()


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(_write_bad_json, id="json"),
        pytest.param(_write_without_node_ids, id="no-node-ids"),
        pytest.param(_make_a_directory, id="directory"),
    ],
)
def test_an_unreadable_held_out_record_is_logged_with_its_path_and_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, write: Any
) -> None:
    """A record that is there but does not read is no request, as a record
    the pre-review step failed to write is, and the warning names the record
    and the error, so the round never reads as a clean "nothing asked"."""
    sidecar = tmp_path / "round.json"
    record = tmp_path / "round.json.held-out"
    write(record)

    with caplog.at_level("WARNING", logger="kew.behavior_declarations"):
        assert load_held_out_request(str(sidecar)) is None

    [warning] = caplog.records
    assert warning.levelname == "WARNING"
    message = warning.getMessage()
    assert str(record) in message
    assert "unreadable" in message
    assert "Error" in message


def _sidecar_findings(sidecar_path: str) -> list[dict[str, Any]]:
    """The findings the round's sidecar carries to the remediation agent."""
    findings: list[dict[str, Any]] = json.loads(Path(sidecar_path).read_text())[
        "findings"
    ]
    return findings


PINNED_FILE = "tests/test_pinned.py"
FORMAL = "tests/test_pinned.py::test_the_greeting_is_formal"

#: The head's import breaks the pinned file's collection: the file fails, and
#: both of its tests the base collects never run.
_COLLECTION_RED = opened(
    f"{report_line(PINNED_FILE, uncollected=[PINNED, FORMAL])}"
    "kew-held-out: exit 1 seconds 2\n"
    f"{report_line()}kew-held-out: exit 0 seconds 1\n"
)


def test_a_collection_error_is_asked_for_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = ((FORMAL, 0, _HELD_OUT_CLEAN), ("", 0, _COLLECTION_RED))
    (_outcome, _head), reviewer, remediations, _host_, store = _drive(
        tmp_path, monkeypatch, body="Closes #42\n", answers=answers
    )
    store.close()

    assert reviewer.launched == []
    [ctx] = remediations
    assert ctx.declaration is not None
    assert ctx.declaration.node_ids == (PINNED_FILE,), "the file, once"
    assert ctx.sidecar_path is not None
    [finding] = _sidecar_findings(ctx.sidecar_path)
    summary = finding["summary"]
    assert (
        f"- {PINNED_FILE} failed to collect; 2 of its tests never ran; one line "
        "for the file declares them"
    ) in summary
    assert f"- {PINNED}" not in summary and f"- {FORMAL}" not in summary

    # The one line the report proposes for the file is the one kew carries.
    report = (
        "## Dispatch Report\n\n### Behavior changes\n"
        f"- {PINNED_FILE} — the module now imports the formal greeting\n"
    )
    assert requested_lines(ctx.declaration, report) == [
        f"- {PINNED_FILE} — the module now imports the formal greeting"
    ]


class _BodyForge:
    """The PR-body REST calls a carry makes: a read and a PATCH."""

    def __init__(self, body: str) -> None:
        self.body = body

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] != ["gh", "api"] or "repos/o/r/pulls/7" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown")
        if "PATCH" in cmd:
            self.body = cmd[cmd.index("-f") + 1].removeprefix("body=")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        payload = json.dumps({"body": self.body})
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")


def _forge(monkeypatch: pytest.MonkeyPatch, body: str) -> _BodyForge:
    forge = _BodyForge(body)
    monkeypatch.setattr("kew.review_integrity.run", forge)
    monkeypatch.setattr("kew.behavior_declarations.run", forge)
    return forge


def _published_by_implementer(*proposals: str) -> Any:
    """A seed: the dispatch run kew recorded as publishing the agent branch's
    head, its stored Dispatch Report proposing *proposals*."""

    def seed(store: SqliteRunStore, work: Path) -> None:
        head = _git("rev-parse", BRANCH, cwd=work)
        report = "## Dispatch Report\n\n### Behavior changes\n" + "".join(
            f"{line}\n" for line in proposals
        )
        log = work.parent / "impl-1.log"
        log.write_text("")
        derivation = StreamDerivation()
        derivation.observe(
            json.dumps({"type": "result", "subtype": "success", "result": report})
        )
        write_derived_evidence(log, derivation.evidence())
        started = NOW - timedelta(hours=1)
        store.save(
            RunRecord(
                run_id="impl-1",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=BRANCH,
                status="success",
                exit_code=0,
                started_at=started,
                finished_at=started,
                log_file=log,
            )
        )
        emit(
            store,
            "publication_intent",
            actor=Actor("policy", "host-publication"),
            run_id="impl-1",
            item_id=ITEM,
            payload={
                "branch": BRANCH,
                "scope": "item",
                "commit_sha": head,
                "tree_sha": "d" * 40,
                "parent_sha": "c" * 40,
            },
        )

    return seed


_PINNED_LINE = f"- {PINNED} — the greeting is now formal, as the issue asks"


#: The static check the gate runs before review, played by the container
#: runtime below: it fails on a tree whose greeting is the head's.
_LINT = "lint src"
_LINT_FINDING = "kew/src/app/__init__.py:1:1: G001 the greeting is not the pinned one"


def _lint_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved image the runtime resolves, and a runtime that plays
    ``_LINT`` against the checkout kew mounted into it: red on the head, whose
    greeting changed, and green on the base."""
    for tool in ("git", "sh", "timeout", "gtimeout"):
        found = shutil.which(tool)
        if found:
            monkeypatch.setitem(TOOL_PATHS, tool, found)
    runtime_present(monkeypatch)
    monkeypatch.setattr(
        verification, "require_image_id", lambda *a, **k: "sha256:" + "d" * 64
    )

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        mount = next(
            arg.split(":")[0]
            for arg in argv
            if arg.endswith((f":{WORK_MOUNT_PATH}:rw", f":{WORK_MOUNT_PATH}:ro"))
        )
        greeting = Path(mount, "kew/src/app/__init__.py").read_text()
        if argv[-1] == _LINT and "good day" in greeting:
            return subprocess.CompletedProcess(argv, 1, f"{_LINT_FINDING}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", run)


def test_a_gate_round_that_also_fails_a_static_check_carries_no_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The implementer proposed a line for every held-out failure, but the
    round's static finding is not answered by a line: nothing is carried, and
    the remediation gets the whole round, the static finding included."""
    _lint_runtime(monkeypatch)
    forge = _forge(monkeypatch, "Closes #42\n")
    (_outcome, _head), reviewer, remediations, _host_, store = _drive(
        tmp_path,
        monkeypatch,
        body="Closes #42\n",
        seed=_published_by_implementer(_PINNED_LINE),
        static=_LINT,
    )
    store.close()

    assert reviewer.launched == []
    assert declared_behavior_changes(forge.body) == frozenset()
    [ctx] = remediations
    assert ctx.declaration is not None and ctx.declaration.node_ids == (PINNED,)
    assert ctx.sidecar_path is not None
    summaries = [f["summary"] for f in _sidecar_findings(ctx.sidecar_path)]
    assert any(_LINT_FINDING in summary for summary in summaries)
    assert any(HELD_OUT_GATE in summary for summary in summaries)


def _red_static_in_a_container(
    monkeypatch: pytest.MonkeyPatch, config: DispatchConfig
) -> None:
    """A static command, in an approved image, that fails on the head and on
    the base alike: a red base, which settles for the head as a skip."""
    config.commands.static = "ruff check ."
    config.sandbox.images = {"claude-code": "approved:tag"}
    runtime_present(monkeypatch)
    monkeypatch.setattr(
        verification, "require_image_id", lambda *_a, **_k: "sha256:" + "d" * 64
    )

    def red(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "E999 the image is broken\n", "")

    monkeypatch.setattr(verification, "run_container_with_cpu_cap_retry", red)


def test_a_settled_static_skip_stands_when_the_held_out_retry_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The static half settles as skipped on the head's first review, and the
    held-out half gets no answer from the host. On the next review only the
    held-out half runs, and passes: the static skip still stands for the head,
    because a retry replaces only the halves it ran."""
    work, head = _repo(tmp_path)
    host = _host(tmp_path, monkeypatch, answers=[("", 0, "nothing\n")])
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _r, _n: "Closes #42\n")
    config = _config(tmp_path)
    _red_static_in_a_container(monkeypatch, config)
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _tick(store, config, work, head, reviewer, [])
        assert settled_halves(store, ITEM, head) == {STATIC_HALF}
        host.answers = [("", 0, _HELD_OUT_CLEAN)]
        _due_again(store)
        second = _tick(store, config, work, head, reviewer, [])
        settled = settled_halves(store, ITEM, head)
        reason = gate_skip_reason(store, ITEM, head)

    assert reviewer.launched == [head, head]
    assert settled == {STATIC_HALF, HELD_OUT_HALF}
    assert "E999 the image is broken" in first.gate_skipped[ITEM]
    assert HELD_OUT_GATE in first.gate_skipped[ITEM]
    assert reason is not None
    assert "E999 the image is broken" in reason and HELD_OUT_GATE not in reason
    assert second.gate_skipped == {ITEM: reason}


#: What a verify host that cannot be reached says: addresses (one ending each
#: sentence), a key path on the operator's machine, a URL with userinfo and a
#: signed query, and a token.
_LEAKY_HOST_ERROR = (
    "instance kew-verify cannot be reached over ssh: ssh: connect to host "
    "10.128.0.7 port 22: Connection refused. Last tried 10.128.0.8. The host "
    "at 198.51.100.2. Then 10.0.0.9... identity "
    "/Users/operator/.ssh/google_compute_engine; see "
    "https://kew:not-a-secret@verify.example.invalid/run?sig=abc; token=swordfish"
)


class _UnreachableHost(FakeVerifyHost):
    """A verify host that cannot be brought up, saying *message* why."""

    def __init__(self, lock_dir: Path, message: str) -> None:
        super().__init__(lock_dir)
        self.message = message

    def _ensure_up(self) -> None:
        raise HostError(self.message)


def _skip_reason_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> str:
    """The skip reason kept for a head the held-out half could not check
    because the verify host failed with *message*."""
    work, head = _repo(tmp_path)
    install(monkeypatch, _UnreachableHost(tmp_path / "locks", message))
    monkeypatch.setattr(review_stage, "read_pr_body", lambda _r, _n: "Closes #42\n")
    config = _config(tmp_path)
    config.hosts.gcp.project = "example-proj-12345"
    config.hosts.gcp.zone = "us-central1-a"
    config.hosts.gcp.instance = "kew-verify"
    reviewer = _Reviewer()

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(store, config, work, head, reviewer, [])
        reason = gate_skip_reason(store, ITEM, head)

    assert reviewer.launched == [head]
    assert reason is not None and outcome.gate_skipped == {ITEM: reason}
    assert reason.startswith(f"{HELD_OUT_GATE}: ")
    return reason


def test_a_host_error_is_redacted_before_it_is_kept_as_the_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip reason is printed on the PR's verdict comment, so what the
    host said is kept only after the publication evidence redaction."""
    reason = _skip_reason_for(tmp_path, monkeypatch, _LEAKY_HOST_ERROR)

    for leaked in (
        "10.128.0.7",
        "10.128.0.8",
        "198.51.100.2",
        "10.0.0.9",
        "/Users/operator",
        "swordfish",
        "not-a-secret",
        "sig=abc",
    ):
        assert leaked not in reason
    assert "connect to host <ip> port 22" in reason
    assert "Last tried <ip>. The host at <ip>. Then <ip>..." in reason
    assert "<path>/google_compute_engine" in reason
    assert "see https://verify.example.invalid/run; token=<redacted>" in reason


#: The operator machine's name, as ``socket.gethostname`` answers on it.
_OPERATOR_HOST = "operator-laptop.lan"


@pytest.mark.parametrize(
    ("message", "leaks"),
    [
        pytest.param(
            "ssh: Could not resolve hostname "
            "kew-verify-1.us-central1-a.c.example-proj-12345.internal",
            ("example-proj-12345", "us-central1-a", "kew-verify-1"),
            id="internal-host",
        ),
        pytest.param(
            "ERROR: (gcloud.compute.instances.start) The resource "
            "'projects/example-proj-12345/zones/us-central1-a/instances/kew-verify' "
            "was not found",
            ("example-proj-12345", "us-central1-a", "instances/kew-verify'"),
            id="gcloud-resource",
        ),
        pytest.param(
            "ssh: connect to host 2001:db8:4000:abcd::1 port 22: refused; "
            "then fe80::1%ens4 port 22: refused",
            ("2001:db8", "abcd::1", "fe80::1", "ens4"),
            id="ipv6",
        ),
        pytest.param(
            "dev@kew-verify-1: Permission denied (publickey)",
            ("dev@", "kew-verify-1"),
            id="user-at-host",
        ),
        pytest.param(
            "dev@kew-verify: Permission denied (publickey)",
            ("dev@", "kew-verify:"),
            id="user-at-configured-instance",
        ),
        pytest.param(
            "dev@203.0.113.9: Permission denied (publickey)",
            ("dev@", "203.0.113.9"),
            id="user-at-ipv4",
        ),
        pytest.param(
            "dev@2001:db8:4000:abcd::1: Permission denied (publickey)",
            ("dev@", "2001:db8", "abcd::1"),
            id="user-at-ipv6",
        ),
        pytest.param(
            "dev@fe80::1%ens4: Permission denied (publickey)",
            ("dev@", "fe80", "::1", "ens4"),
            id="user-at-ipv6-zone",
        ),
        pytest.param(
            "the instance's service account "
            "123456789012-compute@developer.gserviceaccount.com lacks "
            "roles/compute.osLogin",
            ("123456789012", "gserviceaccount.com"),
            id="service-account",
        ),
        pytest.param(
            "the account dev@example.com has no access to the project",
            ("dev@", "example.com"),
            id="email",
        ),
        pytest.param(
            f"instance kew-verify is leased to {_OPERATOR_HOST}:4242:abc123, "
            "so kew will not ship a job onto it",
            (_OPERATOR_HOST, "operator-laptop", "instance kew-verify "),
            id="lease-holder",
        ),
        pytest.param(
            "the bundle gs://example-proj-12345-bucket/x did not upload",
            ("example-proj-12345-bucket",),
            id="bucket",
        ),
    ],
)
def test_a_host_error_names_no_host_project_account_or_operator_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    leaks: tuple[str, ...],
) -> None:
    """What gcloud and ssh print about the verify host names the GCP project,
    zone and instance, the host's addresses, accounts, buckets and the
    operator machine that holds the lease; none of it reaches the PR."""
    monkeypatch.setattr(socket, "gethostname", lambda: _OPERATOR_HOST)

    reason = _skip_reason_for(tmp_path, monkeypatch, message)

    for leaked in leaks:
        assert leaked not in reason, reason
