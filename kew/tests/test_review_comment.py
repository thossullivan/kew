"""Tests for review_comment — edit-in-place PR verdict comment (issue #1046).

Covers:
- _build_body: body content for READY, NEEDS_CHANGES, DEGRADED, stale, blocking
- post_verdict_comment: gh API dispatch (create vs update), no-op on pr_number=None
- Integration: drive_review_stage calls post_verdict_comment on every verdict
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from support.gate_path_repo import make_pr_repo
from support.gh_fakes import FakeGh, GhProcessBoundary
from support.reviewer_artifact import commit_reviewer_artifact

from kew.review_comment import (
    _COMMENT_SENTINEL,
    NO_CRITERIA_NOTE,
    _build_body,
    post_verdict_comment,
)
from kew.review_contract import (
    CoverageEntry,
    CriterionResult,
    Finding,
    NotCheckedEntry,
)
from kew.review_criteria import UnjudgedCriteria

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(
    severity: str = "high",
    location: str = "code.py:42",
    summary: str = "unchecked path",
) -> Finding:
    return Finding(
        severity=severity,
        location=location,
        summary=summary,
        evidence_command="uv run pytest -q",
        evidence_output_digest="sha256:cafe1234",
    )


def _acceptance() -> list[dict[str, str]]:
    return [
        {"command": "uv run pytest -q", "output_digest": "sha256:deadbeef012345678"}
    ]


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode=0, stdout="{}", stderr="")


def _comments_response(
    sentinel_comment_id: int | None,
) -> subprocess.CompletedProcess[str]:
    """Fake gh api response listing zero or one kew verdict comment."""
    comments: list[dict] = []
    if sentinel_comment_id is not None:
        comments.append(
            {"id": sentinel_comment_id, "body": f"{_COMMENT_SENTINEL}\nsome content"}
        )
    return subprocess.CompletedProcess(
        [], returncode=0, stdout=json.dumps([comments]), stderr=""
    )


# ---------------------------------------------------------------------------
# _build_body tests
# ---------------------------------------------------------------------------


def test_build_body_sentinel_is_first_line():
    body = _build_body(
        verdict="READY",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:deadbeef",
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=True,
    )
    assert body.split("\n")[0] == _COMMENT_SENTINEL


def test_build_body_ready_verdict_contains_key_fields():
    body = _build_body(
        verdict="READY",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:deadbeef0123456789abcdef",
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=True,
    )
    assert "READY" in body
    assert "round 1" in body
    assert "abc123def456"[:12] in body
    # sidecar ID truncated to 16 chars
    assert "sha256:deadbeef" in body
    assert "machine-posted" in body
    assert "source of truth" in body


def test_build_body_needs_changes_includes_findings_table():
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=2,
        sidecar_id="sha256:cafe",
        findings=[
            _finding(severity="critical", location="auth.py:99", summary="sql inj")
        ],
        acceptance_evidence=[],
        blocking=True,
    )
    assert "NEEDS_CHANGES" in body
    assert "round 2" in body
    assert "Findings" in body
    assert "critical" in body
    assert "auth.py:99" in body
    assert "sql inj" in body


def test_build_body_ready_includes_acceptance_evidence():
    body = _build_body(
        verdict="READY",
        head_sha="aabbccddeeff",
        current_head_sha="aabbccddeeff",
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=False,
    )
    assert "Acceptance evidence" in body
    assert "uv run pytest -q" in body
    assert "sha256:deadbeef01234" in body


def test_build_body_ready_records_unexecuted_acceptance():
    body = _build_body(
        verdict="READY",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:deadbeef",
        findings=[],
        acceptance_evidence=[],
        acceptance_executed=False,
        acceptance_reason="gh and network-dependent checks are unavailable",
        blocking=False,
    )

    assert "Acceptance execution: not executed" in body
    assert "gh and network-dependent checks are unavailable" in body


def test_build_body_stale_marker_when_head_moved():
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="oldhead123456",
        current_head_sha="newhead789abc",
        review_round=1,
        sidecar_id=None,
        findings=[_finding()],
        acceptance_evidence=[],
        blocking=False,
    )
    assert "Stale" in body
    assert "newhead789a" in body  # current head (first 12)


def test_build_body_no_stale_marker_when_head_unchanged():
    body = _build_body(
        verdict="READY",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=False,
    )
    assert "Stale" not in body


def test_build_body_blocking_note_in_blocking_mode():
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="abc",
        current_head_sha="abc",
        review_round=1,
        sidecar_id=None,
        findings=[_finding()],
        acceptance_evidence=[],
        blocking=True,
    )
    assert "merge-pass invariant" in body
    assert "Blocking" in body


def test_build_body_no_blocking_note_in_advisory_mode():
    body = _build_body(
        verdict="READY",
        head_sha="abc",
        current_head_sha="abc",
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=False,
    )
    assert "merge-pass invariant" not in body


def test_build_body_no_sidecar_note():
    body = _build_body(
        verdict="READY",
        head_sha="abc",
        current_head_sha="abc",
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=False,
    )
    assert "no sidecar" in body


# ---------------------------------------------------------------------------
# post_verdict_comment — gh API dispatch
# ---------------------------------------------------------------------------


def test_post_creates_new_comment_when_none_exists():
    """No existing comment → POST to /issues/{pr}/comments."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        # list returns no matching comment; POST succeeds
        mock_run.side_effect = [_comments_response(None), _ok()]

        post_verdict_comment(
            "owner/repo",
            7,
            verdict="READY",
            head_sha="abc123def456",
            current_head_sha="abc123def456",
            review_round=1,
            sidecar_id="sha256:deadbeef",
            findings=[],
            acceptance_evidence=_acceptance(),
            blocking=True,
        )

    assert mock_run.call_count == 2
    list_call, post_call = mock_run.call_args_list
    assert "repos/owner/repo/issues/7/comments" in list_call.args[0]
    post_args = post_call.args[0]
    assert "-X" in post_args
    assert "POST" in post_args
    assert "repos/owner/repo/issues/7/comments" in post_args
    # body contains READY and sentinel
    body_arg = next(a for a in post_args if a.startswith("body="))
    assert _COMMENT_SENTINEL in body_arg
    assert "READY" in body_arg


def test_post_updates_existing_comment_on_second_round():
    """Existing comment found → PATCH /issues/comments/{id}."""
    existing_id = 9999

    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [_comments_response(existing_id), _ok()]

        post_verdict_comment(
            "owner/repo",
            7,
            verdict="NEEDS_CHANGES",
            head_sha="abc123def456",
            current_head_sha="abc123def456",
            review_round=2,
            sidecar_id="sha256:cafe",
            findings=[_finding()],
            acceptance_evidence=[],
            blocking=True,
        )

    assert mock_run.call_count == 2
    _, patch_call = mock_run.call_args_list
    patch_args = patch_call.args[0]
    assert "-X" in patch_args
    assert "PATCH" in patch_args
    assert f"repos/owner/repo/issues/comments/{existing_id}" in patch_args
    body_arg = next(a for a in patch_args if a.startswith("body="))
    assert "NEEDS_CHANGES" in body_arg
    assert "round 2" in body_arg


def test_post_no_op_when_pr_number_is_none():
    """No pr_number → no gh calls at all."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        post_verdict_comment(
            "owner/repo",
            None,
            verdict="READY",
            head_sha="abc",
            current_head_sha="abc",
            review_round=1,
            sidecar_id=None,
            findings=[],
            acceptance_evidence=_acceptance(),
            blocking=False,
        )

    mock_run.assert_not_called()


def test_post_swallows_gh_list_failure_and_creates_new():
    """gh list failure → fall through to create (POST); never raises."""
    fail = subprocess.CompletedProcess(
        [], returncode=1, stdout="", stderr="network error"
    )
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [fail, _ok()]

        post_verdict_comment(
            "owner/repo",
            7,
            verdict="READY",
            head_sha="abc",
            current_head_sha="abc",
            review_round=1,
            sidecar_id=None,
            findings=[],
            acceptance_evidence=_acceptance(),
            blocking=False,
        )

    # List failed → None → POST (not PATCH)
    assert mock_run.call_count == 2
    _, post_call = mock_run.call_args_list
    assert "POST" in post_call.args[0]


def test_post_swallows_exception_never_raises():
    """An exception from subprocess_utils.run is swallowed (never raises)."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = RuntimeError("gh not found")

        # Must not raise — best-effort
        post_verdict_comment(
            "owner/repo",
            7,
            verdict="READY",
            head_sha="abc",
            current_head_sha="abc",
            review_round=1,
            sidecar_id=None,
            findings=[],
            acceptance_evidence=_acceptance(),
            blocking=False,
        )


def test_post_stale_marker_in_comment_body_when_heads_differ():
    """The body sent to gh contains the stale marker when heads differ."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [_comments_response(None), _ok()]

        post_verdict_comment(
            "owner/repo",
            7,
            verdict="NEEDS_CHANGES",
            head_sha="oldhead000000",
            current_head_sha="newhead111111",
            review_round=1,
            sidecar_id=None,
            findings=[_finding()],
            acceptance_evidence=[],
            blocking=False,
        )

    _, post_call = mock_run.call_args_list
    body_arg = next(a for a in post_call.args[0] if a.startswith("body="))
    assert "Stale" in body_arg
    assert "newhead111" in body_arg


# ---------------------------------------------------------------------------
# Integration: drive_review_stage wires up post_verdict_comment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as _sp

    from kew.subprocess_utils import TOOL_PATHS

    result = _sp.run(["which", "git"], capture_output=True, text=True)
    monkeypatch.setitem(TOOL_PATHS, "git", result.stdout.strip())


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    from kew.subprocess_utils import run

    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main", str(repo)], check=True)
    run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.py").write_text("x = 1\n")
    run(["git", "-C", str(repo), "add", "."], check=True)
    run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
    head = run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True).stdout.strip()
    return repo, head


def _config(tmp_path: Path) -> object:
    from kew.config import DispatchConfig, LoggingConfig, ReviewConfig

    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            publish_status=False,
            publish_comment=True,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(head: str) -> object:
    from kew.graph.driver.facts import NodeFacts

    return NodeFacts(
        issue_num=42,
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


def _ready_launcher(ctx: object) -> object:
    from support.review_stream import stream_verdict

    sidecar = {
        "verdict": "READY",
        "findings": [],
        "acceptance_evidence": [
            {"command": "uv run pytest -q", "output_digest": "sha256:deadbeef"}
        ],
    }
    return stream_verdict(ctx, sidecar)


def _needs_changes_launcher(ctx: object) -> object:
    from support.review_stream import stream_verdict

    sidecar = {
        "verdict": "NEEDS_CHANGES",
        "findings": [
            {
                "severity": "high",
                "location": "code.py:1",
                "summary": "unchecked path",
                "evidence": {
                    "command": "uv run pytest",
                    "output_digest": "sha256:cafe",
                },
            }
        ],
    }
    return stream_verdict(ctx, sidecar)


def test_drive_review_stage_calls_post_verdict_comment_on_ready(
    tmp_path: Path,
) -> None:
    """A READY verdict triggers post_verdict_comment with correct args."""
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.post_verdict_comment") as mock_post,
    ):
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": facts},
            epic_ref="epic:x",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
        )

    mock_post.assert_called_once()
    kwargs = mock_post.call_args
    assert kwargs.args[0] == "owner/repo"
    assert kwargs.args[1] == 7
    assert kwargs.kwargs["verdict"] == "READY"
    assert kwargs.kwargs["head_sha"] == head
    assert kwargs.kwargs["current_head_sha"] == head
    assert kwargs.kwargs["review_round"] == 1
    assert kwargs.kwargs["blocking"] is True
    assert kwargs.kwargs["findings"] == []
    assert len(kwargs.kwargs["acceptance_evidence"]) == 1


def test_drive_review_stage_calls_post_verdict_comment_on_needs_changes(
    tmp_path: Path,
) -> None:
    """A NEEDS_CHANGES verdict triggers post_verdict_comment with findings."""
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(head)

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.post_verdict_comment") as mock_post,
    ):
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": facts},
            epic_ref="epic:x",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
        )

    mock_post.assert_called_once()
    kwargs = mock_post.call_args
    assert kwargs.kwargs["verdict"] == "NEEDS_CHANGES"
    assert len(kwargs.kwargs["findings"]) == 1
    assert kwargs.kwargs["findings"][0].severity == "high"
    assert kwargs.kwargs["findings"][0].location == "code.py:1"


def test_drive_review_stage_skips_comment_when_publish_comment_false(
    tmp_path: Path,
) -> None:
    """``publish_comment=False`` suppresses the comment call."""
    from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    repo, head = _make_repo(tmp_path)
    config = DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            publish_status=False,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    facts = _facts(head)

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.post_verdict_comment") as mock_post,
    ):
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": facts},
            epic_ref="epic:x",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
        )

    mock_post.assert_not_called()


def test_drive_review_stage_second_round_updates_comment(
    tmp_path: Path,
) -> None:
    """Second verdict round calls post_verdict_comment with round=2."""
    from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
    from kew.graph.driver.facts import NodeFacts
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore
    from kew.subprocess_utils import run

    repo, head1 = _make_repo(tmp_path)

    # Commit a second revision so round 2 has a distinct head sha.
    (tmp_path / "repo" / "f.py").write_text("x = 2\n")
    run(["git", "-C", str(repo), "add", "."], check=True)
    run(["git", "-C", str(repo), "commit", "-m", "fix"], check=True)
    head2 = run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True
    ).stdout.strip()

    config = DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            publish_status=False,
            publish_comment=True,
            max_rounds=2,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )

    def facts_for(head: str) -> NodeFacts:
        return NodeFacts(
            issue_num=42,
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

    captured: list[dict] = []

    def capture_post(*args: object, **kwargs: object) -> None:
        captured.append({"args": args, "kwargs": kwargs})

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.post_verdict_comment", side_effect=capture_post),
    ):
        # Round 1: NEEDS_CHANGES on head1
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": facts_for(head1)},
            epic_ref="epic:x",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
        )
        # Round 2: READY on head2 (simulates remediation pushed a new commit)
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": facts_for(head2)},
            epic_ref="epic:x",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
        )

    assert len(captured) == 2
    assert captured[0]["kwargs"]["review_round"] == 1
    assert captured[0]["kwargs"]["verdict"] == "NEEDS_CHANGES"
    assert captured[1]["kwargs"]["review_round"] == 2
    assert captured[1]["kwargs"]["verdict"] == "READY"
    # Same PR number both times — second round updates the same comment
    assert captured[0]["args"][1] == captured[1]["args"][1] == 7


# ---------------------------------------------------------------------------
# #2291: the per-criterion table
# ---------------------------------------------------------------------------


def _criterion(
    cid: str = "AC1",
    result: str = "pass",
    *,
    command: str = "uv run pytest -q",
    digest: str = "sha256:deadbeef0123456789abcdef",
    reason: str = "the gate refuses",
) -> CriterionResult:
    return CriterionResult(
        id=cid,
        result=result,
        evidence_command=command,
        evidence_output_digest=digest,
        reason=reason,
    )


def test_body_renders_a_row_per_criterion_with_evidence_and_reason() -> None:
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id="d" * 64,
        findings=[_finding()],
        acceptance_evidence=[],
        plan_sha="e" * 64,
        criteria_results=[
            _criterion("AC1", "pass"),
            _criterion("AC2", "fail", command="", digest="", reason="never rendered"),
            _criterion("AC3", "unverifiable", reason="offline"),
        ],
        blocking=True,
    )

    assert f"### Acceptance criteria — plan `{'e' * 12}`" in body
    assert "| Criterion | Result | Evidence | Reason |" in body
    assert "| AC1 | pass ✓ | `uv run pytest -q` → `sha256:deadbeef01234…` |" in body
    # A criterion with no evidence says so rather than printing empty backticks.
    assert "| AC2 | fail ✗ | — | never rendered |" in body
    assert "| AC3 | unverifiable ? |" in body
    assert "offline" in body


def test_body_says_so_when_the_item_has_no_recorded_criteria() -> None:
    """An ABSENT table is indistinguishable from a reviewer that ignored the
    criteria, and the two mean opposite things before a merge."""
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        blocking=False,
    )

    assert NO_CRITERIA_NOTE in body
    assert "| Criterion |" not in body


def test_the_criteria_table_sits_above_the_findings() -> None:
    """What the work was FOR reads before the defects in how it was done."""
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=2,
        sidecar_id="d" * 64,
        findings=[_finding()],
        acceptance_evidence=[],
        plan_sha="e" * 64,
        criteria_results=[_criterion("AC1", "fail")],
        blocking=True,
    )

    assert body.index("Acceptance criteria") < body.index("### Findings")


def test_post_verdict_comment_carries_the_criteria_table_to_github() -> None:
    """The table must reach the surface a human reads, not just ``_build_body``."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [_comments_response(None), _ok()]
        post_verdict_comment(
            "o/r",
            7,
            verdict="READY",
            head_sha="a" * 40,
            current_head_sha="a" * 40,
            review_round=1,
            sidecar_id="d" * 64,
            findings=[],
            acceptance_evidence=_acceptance(),
            acceptance_executed=True,
            plan_sha="e" * 64,
            criteria_results=[_criterion("AC1", "pass")],
            blocking=True,
        )

    posted = mock_run.call_args_list[-1].args[0]
    body = posted[posted.index("-f") + 1]
    assert "| AC1 | pass ✓ |" in body


def test_a_reviewer_reason_cannot_break_the_criteria_table() -> None:
    """One cell per criterion, whatever prose the reviewer wrote.

    A newline splits the row and a bare pipe adds a column, so an unsanitized
    reason garbles the table a human reads before approving the merge.
    """
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        plan_sha="e" * 64,
        criteria_results=[
            _criterion(
                "AC1", "unverifiable", command="", digest="", reason="one | two\nthree"
            )
        ],
        blocking=False,
    )

    rows = [line for line in body.splitlines() if line.startswith("| AC1 ")]
    assert rows == ["| AC1 | unverifiable ? | — | one \\| two three |"]


def test_a_reviewer_summary_cannot_forge_findings_table_rows() -> None:
    """The findings table is reviewer prose too, cell for cell.

    A summary carrying a newline and pipes would otherwise close its row and
    open rows of its own — inventing findings, severities and locations in the
    table a human reads before approving the merge.
    """
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[
            _finding(
                summary="real defect |\n| low | ok.py:1 | nothing to see here",
                location="code.py:42",
            )
        ],
        acceptance_evidence=[],
        blocking=False,
    )

    rows = [line for line in body.splitlines() if line.startswith("| high ")]
    assert rows == [
        "| high | `code.py:42` | real defect \\| \\| low \\| ok.py:1 \\| "
        "nothing to see here |"
    ]
    # One row per finding: nothing the reviewer wrote became a second one.
    assert len([line for line in body.splitlines() if line.startswith("| ")]) == 2


# ---------------------------------------------------------------------------
# What the round did NOT judge (#2291)
# ---------------------------------------------------------------------------


def test_body_distinguishes_criteria_kew_skipped_from_an_item_with_none() -> None:
    """A recorded plan this round never judged must not read as no plan at all.

    The round legitimately records ``plan_sha=None`` — nothing was judged — but
    the ledger holds criteria for the item. Printing ``NO_CRITERIA_NOTE`` there
    tells the human approving the merge that the issue asked for nothing
    checkable, which is the opposite of what happened.
    """
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        unjudged_criteria=UnjudgedCriteria(
            plan_sha="e" * 64, criteria_count=3, reason="the artifact is stale"
        ),
        blocking=False,
    )

    assert NO_CRITERIA_NOTE not in body
    assert "3 recorded acceptance criteria" in body
    assert "NOT judged in this round" in body
    assert f"plan `{'e' * 12}`" in body
    assert "the artifact is stale" in body
    # No table: there are no results to show, only a gap to report.
    assert "| Criterion |" not in body


def test_a_single_unjudged_criterion_reads_as_one() -> None:
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        unjudged_criteria=UnjudgedCriteria(
            plan_sha="e" * 64, criteria_count=1, reason="the artifact is stale"
        ),
        blocking=False,
    )

    assert "1 recorded acceptance criterion" in body
    assert "was NOT judged in this round" in body


def test_body_names_the_criteria_the_prompt_could_not_carry() -> None:
    """A partial table must not read as the whole plan.

    The prompt's untrusted-body budget drops criteria the id set then excludes,
    so a 20-criterion plan can produce 10 all-pass rows. Under a sha that names
    the full plan, that reads as a contract fully met.
    """
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        plan_sha="e" * 64,
        criteria_results=[_criterion("AC1", "pass")],
        omitted_criterion_ids=("AC2", "AC3"),
        blocking=False,
    )

    assert f"### Acceptance criteria — plan `{'e' * 12}`" in body
    assert "2 further criteria" in body
    assert "were not judged" in body
    assert "AC2, AC3" in body
    assert len([line for line in body.splitlines() if line.startswith("| AC")]) == 1


def test_a_table_covering_the_whole_plan_carries_no_omission_note() -> None:
    body = _build_body(
        verdict="READY",
        head_sha="a" * 40,
        current_head_sha="a" * 40,
        review_round=1,
        sidecar_id=None,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        plan_sha="e" * 64,
        criteria_results=[_criterion("AC1", "pass")],
        blocking=False,
    )

    assert "not judged" not in body


def test_post_verdict_comment_carries_the_unjudged_plan_to_github() -> None:
    """The gap must reach the surface the merge is approved from."""
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [_comments_response(None), _ok()]
        post_verdict_comment(
            "o/r",
            7,
            verdict="READY",
            head_sha="a" * 40,
            current_head_sha="a" * 40,
            review_round=1,
            sidecar_id="d" * 64,
            findings=[],
            acceptance_evidence=_acceptance(),
            acceptance_executed=True,
            unjudged_criteria=UnjudgedCriteria(
                plan_sha="e" * 64, criteria_count=2, reason="the artifact is stale"
            ),
            blocking=True,
        )

    posted = mock_run.call_args_list[-1].args[0]
    body = posted[posted.index("-f") + 1]
    assert "2 recorded acceptance criteria" in body
    assert NO_CRITERIA_NOTE not in body


def test_drive_review_stage_reports_a_plan_the_round_could_not_judge(
    tmp_path: Path,
) -> None:
    """#2291 regression: a stale deployed reviewer artifact must not make the
    verdict comment assert the item recorded no criteria.

    ``previous_sha256`` accepts an older deployed artifact and the round is
    judged against nothing — correctly recording ``plan_sha=None``. The ledger
    still holds the item's plan, so the comment a human approves the merge from
    has to report a skipped contract, not an issue that asked for nothing.
    """
    import hashlib

    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore
    from kew.triage.plan import canonical_plan_json, plan_sha
    from kew.triage.schema import AcceptanceCriterion, ItemPlan

    repo, head = _make_repo(tmp_path)
    commit_reviewer_artifact(repo, "an older deployed reviewer prompt\n")
    plan = ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", statement="the flag turns it off")
        ]
    )

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.post_verdict_comment") as mock_post,
    ):
        store.record_item_plan(
            "42",
            body_digest=hashlib.sha256(b"body").hexdigest(),
            source="extracted",
            plan_sha=plan_sha(plan),
            plan_json=canonical_plan_json(plan),
            criteria_count=1,
        )
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": _facts(head)},
            epic_ref="epic:x",
            config=_config(tmp_path),
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
        )

    kwargs = mock_post.call_args.kwargs
    # The round really was judged against nothing: the stored fact stays None …
    assert kwargs["plan_sha"] is None
    # … and the plan it skipped travels with it.
    unjudged = kwargs["unjudged_criteria"]
    assert unjudged is not None
    assert unjudged.plan_sha == plan_sha(plan)
    assert unjudged.criteria_count == 1
    assert "predates the recorded-criteria contract" in unjudged.reason

    body = _build_body(
        verdict="READY",
        head_sha=head,
        current_head_sha=head,
        review_round=1,
        sidecar_id="d" * 64,
        findings=[],
        acceptance_evidence=_acceptance(),
        acceptance_executed=True,
        plan_sha=kwargs["plan_sha"],
        unjudged_criteria=unjudged,
        blocking=True,
    )
    assert NO_CRITERIA_NOTE not in body
    assert "1 recorded acceptance criterion" in body


def _seed_branch_runs(store: object, count: int) -> None:
    """*count* runs on item 42's branch, as successive dispatch attempts."""
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    for n in range(count):
        store.save(  # type: ignore[attr-defined]
            RunRecord(
                run_id=f"run-{n}",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 7, 20, 0, n % 60, tzinfo=UTC),
            )
        )


@pytest.mark.parametrize(("runs", "warned"), [(23, False), (24, True)])
def test_verdict_comment_warns_when_the_branch_nears_the_run_cap(
    tmp_path: Path, runs: int, warned: bool
) -> None:
    """From 24 branch runs the posted verdict comment warns before the cap (#3193).

    At 32 kew can no longer bind a head it pushes to the branch, so the
    operator is told while there is still room to finish the PR.
    """
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    repo, head = _make_repo(tmp_path)
    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.subprocess_utils.run") as gh,
    ):
        gh.side_effect = [_comments_response(None), _ok()]
        _seed_branch_runs(store, runs)
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": _facts(head)},
            epic_ref="epic:x",
            config=_config(tmp_path),
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
        )

    body = next(a for a in gh.call_args_list[-1].args[0] if a.startswith("body="))
    assert "NEEDS_CHANGES" in body
    assert (f"this branch carries {runs} of the 32 runs" in body) is warned


# --- #3419: what the reviewer checked, and what it could not ---


def _covered(n: int) -> list[CoverageEntry]:
    return [
        CoverageEntry(
            target=f"src/kew/m{i}.py:{i + 1}",
            tried=f"case {i} | forged",
            how="executed",
            evidence_command="uv run pytest -q",
            evidence_output_digest="abc",
        )
        for i in range(n)
    ]


def _unchecked(n: int) -> list[NotCheckedEntry]:
    return [
        NotCheckedEntry(check=f"ui-check {i}", kind="browser", reason="no Chromium")
        for i in range(n)
    ]


def _coverage_body(verdict: str, **kwargs: object) -> str:
    return _build_body(
        verdict=verdict,
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:cafe",
        findings=[] if verdict == "READY" else [_finding()],
        acceptance_evidence=_acceptance() if verdict == "READY" else [],
        blocking=False,
        **kwargs,  # type: ignore[arg-type]
    )


def test_ready_body_shows_checked_and_not_checked_lists():
    body = _coverage_body(
        "READY",
        coverage=[
            *_covered(1),
            CoverageEntry(target="src/kew/x.py:9", tried="read the lock", how="read"),
        ],
        not_checked=_unchecked(1),
    )
    assert "### Checked and cleared" in body
    assert (
        "- `src/kew/m0.py:1` — executed: case 0 | forged (`uv run pytest -q`)" in body
    )
    assert "- `src/kew/x.py:9` — read: read the lock" in body
    assert "### Not checked in the review sandbox" in body
    assert "- **browser** — ui-check 0: no Chromium" in body


def test_ready_body_lists_at_most_thirty_entries_each():
    body = _coverage_body("READY", coverage=_covered(33), not_checked=_unchecked(31))
    assert "src/kew/m29.py:30" in body
    assert "src/kew/m30.py:31" not in body
    assert "- (+3 more)" in body
    assert "ui-check 29:" in body
    assert "ui-check 30:" not in body
    assert "- (+1 more)" in body


def test_empty_not_checked_says_none_reported():
    body = _coverage_body("READY", coverage=_covered(1), not_checked=[])
    assert "### Not checked in the review sandbox\n\n- none reported" in body


def test_needs_changes_body_shows_only_not_checked():
    body = _coverage_body(
        "NEEDS_CHANGES", coverage=_covered(1), not_checked=_unchecked(1)
    )
    assert "Checked and cleared" not in body
    assert "- **browser** — ui-check 0: no Chromium" in body


def test_round_outside_the_coverage_contract_shows_neither_list():
    body = _coverage_body("READY")
    assert "Checked and cleared" not in body
    assert "Not checked in the review sandbox" not in body


GITHUB_COMMENT_LIMIT = 65_536
"""The longest body GitHub accepts for an issue comment, in characters."""


def _huge_coverage(n: int = 30) -> list[CoverageEntry]:
    return [
        CoverageEntry(
            target=f"src/kew/m{i}.py:{i + 1}",
            tried="t" * 300,
            how="executed",
            evidence_command="pytest " + "x" * 100_000,
            evidence_output_digest="abc",
        )
        for i in range(n)
    ]


def _assert_verdict_line_intact(body: str, verdict: str) -> None:
    assert body.splitlines()[0] == _COMMENT_SENTINEL
    assert f"**Verdict: {verdict} " in body
    assert "round 1 · head `abc123def456`" in body


def test_thirty_huge_coverage_commands_keep_the_body_under_github_limit():
    body = _coverage_body("READY", coverage=_huge_coverage(), not_checked=_unchecked(1))
    assert len(body) < GITHUB_COMMENT_LIMIT
    _assert_verdict_line_intact(body, "READY")
    assert "comment truncated" in body


def test_oversize_findings_are_truncated_with_a_visible_marker():
    findings = [
        _finding(location=f"code.py:{i}", summary="s" * 5_000) for i in range(40)
    ]
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:cafe",
        findings=findings,
        acceptance_evidence=[],
        not_checked=_unchecked(30),
        blocking=True,
    )
    assert len(body) < GITHUB_COMMENT_LIMIT
    _assert_verdict_line_intact(body, "NEEDS_CHANGES")
    assert "`code.py:0`" in body
    assert "comment truncated" in body
    assert body.endswith("this is the kew merge-pass invariant_")


def test_body_within_the_limit_is_not_truncated():
    body = _coverage_body("READY", coverage=_covered(30), not_checked=_unchecked(30))
    assert "comment truncated" not in body


def test_oversize_body_never_reaches_gh():
    with patch("kew.review_comment.subprocess_utils.run") as gh:
        gh.side_effect = [_comments_response(None), _ok()]
        post_verdict_comment(
            "owner/repo",
            7,
            verdict="READY",
            head_sha="abc123def456",
            current_head_sha="abc123def456",
            review_round=1,
            sidecar_id="sha256:cafe",
            findings=[],
            acceptance_evidence=_acceptance(),
            coverage=_huge_coverage(),
            not_checked=[],
            blocking=False,
        )
    body = next(a for a in gh.call_args_list[-1].args[0] if a.startswith("body="))
    assert len(body) - len("body=") < GITHUB_COMMENT_LIMIT
    assert "**Verdict: READY " in body


def test_pipe_in_a_coverage_list_item_renders_unescaped():
    body = _coverage_body(
        "READY",
        coverage=[
            CoverageEntry(
                target="src/kew/x.py:1",
                tried="a | b",
                how="executed",
                evidence_command="pytest -q | tail",
                evidence_output_digest="abc",
            )
        ],
        not_checked=[NotCheckedEntry(check="ui | check", kind="browser", reason="r|s")],
    )
    assert "- `src/kew/x.py:1` — executed: a | b (`pytest -q | tail`)" in body
    assert "- **browser** — ui | check: r|s" in body
    assert "\\|" not in body


def test_backticks_in_a_coverage_command_keep_the_code_span_whole():
    body = _coverage_body(
        "READY",
        coverage=[
            CoverageEntry(
                target="src/kew/x.py:1",
                tried="ran it",
                how="executed",
                evidence_command="echo `date`",
                evidence_output_digest="abc",
            )
        ],
        not_checked=[],
    )
    assert "(`` echo `date` ``)" in body


def test_pipe_in_a_findings_table_cell_is_still_escaped():
    body = _build_body(
        verdict="NEEDS_CHANGES",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id=None,
        findings=[_finding(summary="a | b")],
        acceptance_evidence=[],
        blocking=False,
    )
    assert "| a \\| b |" in body


def test_stage_posts_needs_changes_with_a_malformed_not_checked_entry(
    tmp_path: Path,
) -> None:
    """One malformed not-checked entry neither voids the NEEDS_CHANGES nor
    vanishes: the posted comment keeps the findings and counts the drop."""
    from support.review_stream import stream_verdict

    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    def launcher(ctx: object) -> object:
        sidecar = {
            "verdict": "NEEDS_CHANGES",
            "findings": [
                {
                    "severity": "high",
                    "location": "code.py:1",
                    "summary": "unchecked path",
                    "evidence": {"command": "uv run pytest", "output_digest": "c"},
                }
            ],
            "not_checked": [
                {"check": "ui-check", "kind": "Browser ", "reason": "no Chromium"},
                {"check": "docker build", "kind": "docker"},
            ],
        }
        return stream_verdict(ctx, sidecar)

    repo, head = _make_repo(tmp_path)
    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        patch("kew.review_comment.subprocess_utils.run") as gh,
    ):
        gh.side_effect = [_comments_response(None), _ok()]
        drive_review_stage(
            {"42": DriverStatus.ADVERSARIAL_REVIEW},
            {"42": _facts(head)},
            epic_ref="epic:x",
            config=_config(tmp_path),
            store=store,
            repo="owner/repo",
            repo_root=repo,
            base="main",
            reviewer_launcher=launcher,
        )

    body = next(a for a in gh.call_args_list[-1].args[0] if a.startswith("body="))
    assert "**Verdict: NEEDS_CHANGES " in body
    assert "unchecked path" in body
    assert "- **browser** — ui-check: no Chromium" in body
    assert "1 malformed not-checked entry dropped" in body


def test_dropped_not_checked_entries_are_shown_not_hidden():
    body = _coverage_body("NEEDS_CHANGES", not_checked=[], not_checked_dropped=2)
    assert "- none reported" not in body
    assert "2 malformed not-checked entries dropped" in body


def _posted_body(gate_skipped: str | None = None) -> str:
    with patch("kew.review_comment.subprocess_utils.run") as mock_run:
        mock_run.side_effect = [_comments_response(None), _ok()]
        post_verdict_comment(
            "owner/repo",
            7,
            verdict="READY",
            head_sha="abc123def456",
            current_head_sha="abc123def456",
            review_round=1,
            sidecar_id="sha256:deadbeef",
            findings=[],
            acceptance_evidence=_acceptance(),
            blocking=True,
            gate_skipped=gate_skipped,
        )
    post_args = mock_run.call_args_list[-1].args[0]
    return next(a for a in post_args if a.startswith("body=")).removeprefix("body=")


def test_the_verdict_comment_says_the_pre_review_gate_was_skipped_and_why():
    """A head the gate skipped carries one line naming the skip and its reason;
    a head the gate passed carries none."""
    reason = "static checks: they fail on the base too:\nE999 the image is broken"

    skipped = _posted_body(gate_skipped=reason)
    passed = _posted_body()

    (line,) = [ln for ln in skipped.splitlines() if "pre-review gate" in ln]
    assert "did not check this head" in line
    assert "static checks: they fail on the base too: E999 the image is broken" in line
    assert "pre-review gate" not in passed


# ---------------------------------------------------------------------------
# A READY on a merge-gate-path head awaits a second full review (#3422)
# ---------------------------------------------------------------------------


def _gate_body(verdict: str, gate_paths: tuple[str, ...]) -> str:
    return _build_body(
        verdict=verdict,
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=1,
        sidecar_id="sha256:deadbeef",
        findings=[],
        acceptance_evidence=_acceptance(),
        blocking=True,
        gate_paths=gate_paths,
    )


def test_first_gate_path_ready_comment_says_why_a_second_review_runs():
    body = _gate_body("NEEDS_FULL", ("scripts/verify.sh", "kew.toml"))
    assert "a second, independent full review must confirm it" in body
    assert "this diff touches merge gate paths" in body
    assert "> - `scripts/verify.sh`" in body
    assert "> - `kew.toml`" in body


def test_gate_path_residuals_comment_does_not_claim_a_ready():
    """A residual-only round awaiting its confirming review is recorded
    NEEDS_FULL too; its note says what the reviewer actually returned."""
    body = _build_body(
        verdict="NEEDS_FULL",
        head_sha="abc123def456",
        current_head_sha="abc123def456",
        review_round=2,
        sidecar_id="sha256:deadbeef",
        findings=[_finding(severity="low")],
        acceptance_evidence=[],
        blocking=True,
        gate_paths=("scripts/verify.sh",),
    )
    assert "returned READY" not in body
    assert "only findings kew would accept as residuals" in body
    assert "a second, independent full review must confirm it" in body


def test_gate_path_note_is_absent_without_gate_paths_or_once_ready():
    assert "merge gate paths" not in _gate_body("NEEDS_FULL", ())
    assert "merge gate paths" not in _gate_body("READY", ("scripts/verify.sh",))


def test_gate_path_note_bounds_and_fences_adversary_named_paths():
    named = tuple(f"scripts/s{n}.sh" for n in range(25))
    body = _gate_body("NEEDS_FULL", ("scripts/`x`\nfake.sh", *named))
    assert "> - ``scripts/`x` fake.sh``" in body
    assert "> - `scripts/s18.sh`" in body
    assert "scripts/s19.sh" not in body
    assert "> - (+6 more)" in body


def test_drive_review_stage_comments_the_gate_paths_on_the_first_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the stage and the real gh argv: the first full READY on a head
    that touches gate paths posts the second-review note naming them."""
    from kew import subprocess_utils
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import drive_review_stage
    from kew.run_store import SqliteRunStore

    gh = FakeGh()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(gh, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh", "src/app.py")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for _ in range(2):
            drive_review_stage(
                {"42": DriverStatus.ADVERSARIAL_REVIEW},
                {"42": _facts(head)},
                epic_ref="epic:x",
                config=_config(tmp_path),
                store=store,
                repo="owner/repo",
                repo_root=repo,
                base="main",
                reviewer_launcher=_ready_launcher,
            )
    bodies = [
        arg.removeprefix("body=")
        for call in gh.calls
        for arg in call
        if arg.startswith("body=")
    ]
    assert len(bodies) == 2
    first, second = bodies
    assert "**Verdict: NEEDS_FULL" in first
    assert "this diff touches merge gate paths" in first
    assert "> - `scripts/verify.sh`" in first
    assert "src/app.py" not in first
    # The confirming round's READY stands, and its comment drops the note.
    assert "**Verdict: READY" in second
    assert "merge gate paths" not in second
