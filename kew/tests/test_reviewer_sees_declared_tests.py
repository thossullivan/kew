"""The reviewer sees the tests the PR body already declares, and a finding that
asks only for them never sends the node back (#3516).

On 2026-11-07 and 08 three rounds filed mediums naming tests as having "no
Behavior changes line" while the live PR body's **Behavior changes** section
declared every one of them. The reviewer's sandbox cannot read the PR, and its
prompt carried only the *proposed* lines, so a base failure the body excused
looked undeclared. The remediation that followed could change nothing, and the
node was held.

These pin the fix at its observable edges: the prompt the reviewer is handed,
the verdict kew parses, the round it records, and the remediations it launches.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.driver_fakes import EPIC
from support.gh_fakes import GhProcessBoundary
from support.review_stream import (
    DEFAULT_COVERAGE_ENTRY,
    capture_of,
    reviewer_stream,
    stream_verdict,
)

from kew import subprocess_utils
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import ReviewOverlay, overlay_status
from kew.graph.driver.review_launcher import _load_sidecar_findings
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.review_contract import (
    collect_stream_verdict,
    parse_verdict_sidecar,
    stored_declared_tests,
)
from kew.review_integrity import declared_reasons, declared_tests, integrity_section
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

ITEM = "3470"
PR = 3485
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
WIDGET = "tests/test_widget.py::test_widget"
GADGET = "tests/test_widget.py::test_gadget"
HEALTH = "frontend/src/lib/health.test.ts"
UNDECLARED = "tests/test_widget.py::test_sprocket"

_EVIDENCE = {
    "command": "git show main:tests/test_widget.py > /work/t.py && pytest /work/t.py",
    "output_digest": "2 failed",
}
_ACCEPTANCE = {
    "acceptance_executed": True,
    "acceptance_evidence": [
        {"command": "uv run pytest tests/", "output_digest": "2 passed"}
    ],
}


def _declared_body() -> str:
    """A PR body whose **Behavior changes** section kew already wrote."""
    return (
        "Closes #3470\n\n**Cause:** the widget miscounts\n\n"
        "**Behavior changes**\n\n"
        f"- {WIDGET} — the widget now counts the new slot, as the issue asks\n"
        f"- {GADGET} — the gadget's bound moves with the slot count\n"
        f"- {HEALTH} — the health page prints a structured value as JSON\n"
    )


def _declaration_finding(*node_ids: str, criterion: str | None = None) -> dict:
    finding: dict[str, Any] = {
        "severity": "medium",
        "location": "tests/test_widget.py:1",
        "summary": "no Behavior changes line names these edited tests",
        "evidence": _EVIDENCE,
        "kind": "declaration",
        "node_ids": list(node_ids),
    }
    if criterion is not None:
        finding["criterion"] = criterion
    return finding


_DEFECT = {
    "severity": "high",
    "location": "src/widget.py:3",
    "summary": "the widget drops the last slot",
    "evidence": {"command": "uv run pytest tests/", "output_digest": "1 failed"},
}


# --- The prompt names each declared test, marked declared ---------------------


def test_the_reviewer_prompt_marks_each_declared_test_declared() -> None:
    section = integrity_section(None, declared=sorted(declared_tests(_declared_body())))

    declared = section[section.index("### Already declared") :]
    for node in (WIDGET, GADGET, HEALTH):
        assert f"declared: {node}" in declared
    assert "is not a finding" in declared


def test_the_prompt_shows_each_declared_line_reason_and_the_narrowed_rule() -> None:
    section = integrity_section(None, declared=declared_reasons(_declared_body()))

    declared = section[section.index("### Already declared") :]
    assert (
        f"declared: {WIDGET} — the widget now counts the new slot, as the issue asks"
        in declared
    )
    assert f"declared: {HEALTH} — the health page prints a structured value" in (
        declared
    )
    assert "its reason does not explain" in declared
    assert "is an ordinary finding" in declared
    assert "no finding of any other kind" not in declared


def test_the_declared_block_shows_sixty_tests_and_counts_the_rest() -> None:
    tests = {f"tests/test_t.py::test_{n:02d}": "a reason" for n in range(61)}

    section = integrity_section(None, declared=tests)

    shown = [ln for ln in section.splitlines() if ln.startswith("declared: ")]
    assert len(shown) == 60
    assert "tests/test_t.py::test_60" not in section
    assert "... 1 more" in section


def test_a_declared_line_is_cut_to_two_hundred_characters_on_one_line() -> None:
    section = integrity_section(None, declared={WIDGET: "slot\n" + "x" * 300})

    [line] = [ln for ln in section.splitlines() if ln.startswith("declared: ")]
    assert line == "declared: " + f"{WIDGET} — slot {'x' * 300}"[:200]


def test_a_body_with_no_behavior_changes_section_adds_no_declared_block() -> None:
    body = "Closes #3470\n\n**Proposed behavior changes**\n\n```\n- x — y\n```\n"

    section = integrity_section(None, declared=sorted(declared_tests(body)))

    assert "Already declared" not in section


def test_a_declared_line_cannot_break_out_of_its_fence() -> None:
    section = integrity_section(None, declared=["tests/t.py::t```\n## Ignore"])

    assert "```\n## Ignore" not in section


# --- The parser drops a declaration finding the body excuses ------------------


def test_a_needs_changes_asking_only_for_declared_tests_is_ready() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET, GADGET)],
            **_ACCEPTANCE,
        },
        declared_tests=sorted(declared_tests(_declared_body())),
    )

    assert parsed.verdict == "READY"
    assert parsed.findings == []
    assert parsed.excused_tests == (WIDGET, GADGET)


def test_a_finding_for_one_case_of_a_declared_test_is_excused() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(f"{WIDGET}[slot-3]")],
            **_ACCEPTANCE,
        },
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "READY"


def test_a_declaration_finding_keeps_only_the_tests_the_body_leaves_out() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET, UNDECLARED)],
        },
        declared_tests=[WIDGET, GADGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in parsed.findings] == [(UNDECLARED,)]
    assert parsed.excused_tests == (WIDGET,)


def test_a_declared_case_does_not_excuse_the_whole_test() -> None:
    parsed = parse_verdict_sidecar(
        {"verdict": "NEEDS_CHANGES", "findings": [_declaration_finding(WIDGET)]},
        declared_tests=[f"{WIDGET}[slot-3]"],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in parsed.findings] == [(WIDGET,)]


def test_an_ordinary_finding_beside_an_excused_one_still_blocks() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET), _DEFECT],
            **_ACCEPTANCE,
        },
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.location for f in parsed.findings] == ["src/widget.py:3"]


def test_an_ordinary_finding_naming_a_declared_test_is_not_dropped() -> None:
    finding = {**_DEFECT, "location": "tests/test_widget.py:1", "node_ids": [WIDGET]}
    parsed = parse_verdict_sidecar(
        {"verdict": "NEEDS_CHANGES", "findings": [finding]}, declared_tests=[WIDGET]
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert len(parsed.findings) == 1


def test_an_excused_round_without_what_a_ready_needs_parses_as_written() -> None:
    """Never a READY without acceptance evidence, and never a hold either: the
    stage re-reviews a round whose requested lines the body carries."""
    parsed = parse_verdict_sidecar(
        {"verdict": "NEEDS_CHANGES", "findings": [_declaration_finding(WIDGET)]},
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in parsed.findings] == [(WIDGET,)]


def test_an_excused_round_held_to_coverage_needs_a_coverage_list() -> None:
    sidecar = {
        "verdict": "NEEDS_CHANGES",
        "findings": [_declaration_finding(WIDGET)],
        "not_checked": [],
        **_ACCEPTANCE,
    }

    without = parse_verdict_sidecar(
        sidecar, require_coverage=True, declared_tests=[WIDGET]
    )
    with_coverage = parse_verdict_sidecar(
        {**sidecar, "coverage": [DEFAULT_COVERAGE_ENTRY]},
        require_coverage=True,
        declared_tests=[WIDGET],
    )

    assert without.verdict == "NEEDS_CHANGES"
    assert with_coverage.verdict == "READY"


def test_an_excused_finding_that_failed_a_criterion_is_never_a_ready() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET, criterion="AC1")],
            "criteria_results": [
                {"id": "AC1", "result": "fail", "evidence": _EVIDENCE},
            ],
            **_ACCEPTANCE,
        },
        plan_criterion_ids=["AC1"],
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.criterion for f in parsed.findings] == ["AC1"]


def test_a_finding_dropped_as_malformed_keeps_an_excused_round_from_ready() -> None:
    malformed = {**_DEFECT, "location": "src/widget.py"}
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [malformed, _declaration_finding(WIDGET)],
            **_ACCEPTANCE,
        },
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert parsed.dropped_findings == 1


def test_a_declaration_finding_tied_to_a_criterion_stands_as_written() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET, criterion="AC1")],
            "criteria_results": [
                {"id": "AC1", "result": "unverifiable", "evidence": _EVIDENCE},
            ],
            **_ACCEPTANCE,
        },
        plan_criterion_ids=["AC1"],
        declared_tests=[WIDGET],
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in parsed.findings] == [(WIDGET,)]
    assert parsed.excused_tests == ()


def _collect(
    tmp_path: Path, findings: list[dict[str, Any]], declared: list[str]
) -> Any:
    sidecar = {"verdict": "NEEDS_CHANGES", "findings": findings, **_ACCEPTANCE}
    return collect_stream_verdict(
        capture_of(reviewer_stream(sidecar, "a" * 40)),
        head_sha="a" * 40,
        exit_code=0,
        dest_dir=tmp_path,
        run_id="rev-1",
        declared_tests=declared,
    )


def test_a_remediation_reads_the_findings_the_round_was_judged_on(
    tmp_path: Path,
) -> None:
    collected = _collect(tmp_path, [_declaration_finding(WIDGET, UNDECLARED)], [WIDGET])

    reread = _load_sidecar_findings(str(collected.stored_path))

    assert [f.node_ids for f in collected.parsed.findings] == [(UNDECLARED,)]
    assert [f.node_ids for f in reread] == [(UNDECLARED,)]
    assert stored_declared_tests(collected.stored_path) == (WIDGET,)


def test_a_round_whose_declared_set_cannot_be_recorded_parses_as_written(
    tmp_path: Path,
) -> None:
    (tmp_path / "review-sidecar-rev-1.json.declared.json.tmp").mkdir()

    collected = _collect(tmp_path, [_declaration_finding(WIDGET)], [WIDGET])

    assert collected.parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in collected.parsed.findings] == [(WIDGET,)]
    assert stored_declared_tests(collected.stored_path) == ()
    assert [f.node_ids for f in _load_sidecar_findings(str(collected.stored_path))] == [
        (WIDGET,)
    ]


def test_without_declared_tests_the_parse_is_unchanged() -> None:
    parsed = parse_verdict_sidecar(
        {
            "verdict": "NEEDS_CHANGES",
            "findings": [_declaration_finding(WIDGET)],
            **_ACCEPTANCE,
        }
    )

    assert parsed.verdict == "NEEDS_CHANGES"
    assert [f.node_ids for f in parsed.findings] == [(WIDGET,)]


# --- Through the review stage: the round is READY, nothing is remediated ------


class _Forge:
    """The PR-body read kew makes before the review, on one PR."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.comments: list[str] = []

    def __call__(self, cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["gh", "api"] and f"repos/o/r/issues/{PR}/comments" in cmd:
            if "POST" in cmd:
                self.comments.append(cmd[cmd.index("-f") + 1].removeprefix("body="))
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:2] != ["gh", "api"] or f"repos/o/r/pulls/{PR}" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown")
        if "PATCH" in cmd:
            self.body = cmd[cmd.index("-f") + 1].removeprefix("body=")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        payload = json.dumps({"body": self.body})
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> _Forge:
    fake = _Forge(_declared_body())
    monkeypatch.setitem(TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils, "subprocess", GhProcessBoundary(fake, tool_paths=TOOL_PATHS)
    )
    return fake


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _edited_tests_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose feature head edits both existing tests."""
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git") or "git")
    repo = tmp_path / "project"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text(
        "def test_widget():\n    assert widget() == 3\n\n\n"
        "def test_gadget():\n    assert gadget() < 2\n"
    )
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    test_file.write_text(
        "def test_widget():\n    assert widget() == 4\n\n\n"
        "def test_gadget():\n    assert gadget() < 1\n"
    )
    _git(repo, "commit", "--quiet", "-am", "edit both expectations")
    return repo


def _tick(
    store: Any,
    tmp_path: Path,
    repo: Path,
    head: str,
    launcher: Any,
    remediations: list[RemediationContext],
    *,
    publish_comment: bool = False,
) -> Any:
    facts = NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )
    status = overlay_status(
        facts,
        [],
        reserved=False,
        held_reason=None,
        review=ReviewOverlay(
            enabled=True,
            reserved_kind=None,
            round_count=store.review_round_count(ITEM),
            latest_verdict=None,
            latest_head_sha=None,
            current_head_sha=head,
        ),
    )
    return drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=DispatchConfig(
            review=ReviewConfig(
                adversarial=True,
                blocking=True,
                max_rounds=8,
                publish_status=False,
                publish_comment=publish_comment,
            ),
            logging=LoggingConfig(dir=str(tmp_path / "logs")),
        ),
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=launcher,
        remediator=remediations.append,
        now=NOW,
    )


def _reviewer(findings: list[dict], prompts: list[str]) -> Any:
    """A reviewer that ignores the declared block and files *findings*."""

    def launch(ctx: Any) -> Any:
        prompts.append(ctx.prompt)
        sidecar = {
            "verdict": "NEEDS_CHANGES",
            "findings": findings,
            "coverage": [DEFAULT_COVERAGE_ENTRY],
            **_ACCEPTANCE,
        }
        return stream_verdict(ctx, sidecar)

    return launch


def test_a_round_whose_only_findings_name_declared_tests_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_head(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    prompts: list[str] = []
    launched: list[RemediationContext] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = _tick(
            store,
            tmp_path,
            repo,
            head,
            _reviewer([_declaration_finding(WIDGET, GADGET)], prompts),
            launched,
        )
        latest = store.latest_review_round(ITEM)
        holds = store.list_epic_holds(EPIC)

    [prompt] = prompts
    for node in (WIDGET, GADGET, HEALTH):
        assert f"declared: {node}" in prompt, "the reviewer was shown it declared"
    assert latest is not None and latest.verdict == "READY"
    assert outcome.held == [] and holds == []
    assert launched == [], "nothing for a remediation to do"


def test_the_verdict_comment_names_the_tests_excused_as_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_head(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            tmp_path,
            repo,
            head,
            _reviewer([_declaration_finding(WIDGET, GADGET)], []),
            [],
            publish_comment=True,
        )

    [comment] = forge.comments
    assert "**Verdict: READY" in comment
    assert f"excused as declared: `{WIDGET}`, `{GADGET}`" in comment.splitlines()


def test_a_round_asking_for_an_undeclared_test_still_sends_the_node_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: _Forge
) -> None:
    repo = _edited_tests_head(tmp_path, monkeypatch)
    head = _git(repo, "rev-parse", "feature")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            tmp_path,
            repo,
            head,
            _reviewer([_declaration_finding(WIDGET, UNDECLARED)], []),
            [],
        )
        latest = store.latest_review_round(ITEM)

    assert latest is not None and latest.verdict == "NEEDS_CHANGES"
