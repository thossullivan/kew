"""Admission parity between the dispatch dry run and the real run (#2058).

``kew dispatch N --dry-run`` printed the route, the budget and "Would dispatch"
for an issue that the real ``kew dispatch N`` then refused for a missing
``ready`` label — and that refusal exited 0, so a script (or a ``kew wait``-style
caller) could not tell that nothing had been dispatched.

These tests bind both halves: the dry run reports the same admission verdict the
claim applies, and a refusal to dispatch the one issue named on the command line
returns the could-not-run class (2) under the W4.2 scheme.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from kew.cli import (
    DispatchCommandResult,
    _dispatch_exit_code,
    _dispatch_one,
    app,
)
from kew.config import ApprovalConfig, DispatchConfig
from kew.labels import ClaimResult, classify_admission


def _issue(
    number: int = 2055,
    title: str = "Some issue",
    labels: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": "Issue body",
        "labels": labels if labels is not None else [],
    }


def _ready() -> list[dict[str, str]]:
    return [{"name": "ready"}]


def _run_dry(
    config: DispatchConfig,
    issue: dict[str, Any],
    *,
    force_issue: int | None = 2055,
    approve: bool = False,
    branch_exists: bool = False,
) -> DispatchCommandResult:
    with (
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.build_prompt", return_value="prompt"),
        patch("kew.cli.remote_branch_exists", return_value=branch_exists),
        patch("kew.cli.execute_dispatch") as launch,
        patch("kew.cli.try_claim_issue") as claim,
    ):
        result = _dispatch_one(
            config, force_issue=force_issue, dry_run=True, approve=approve
        )
    launch.assert_not_called()
    claim.assert_not_called()
    return result


# ---------------------------------------------------------------------------
# classify_admission — the pure half of the pre-claim check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"ready"}, ClaimResult.CLAIMED),
        ({"ready", "complex"}, ClaimResult.CLAIMED),
        (set(), ClaimResult.NOT_READY),
        ({"complex"}, ClaimResult.NOT_READY),
        ({"needs-approval"}, ClaimResult.NOT_READY),
        # in-progress is tested BEFORE ready, exactly as try_claim_issue does:
        # an in-flight issue must never read as "add the label first".
        ({"in-progress"}, ClaimResult.ALREADY_CLAIMED),
        ({"agent-working"}, ClaimResult.ALREADY_CLAIMED),
        ({"ready", "in-progress"}, ClaimResult.ALREADY_CLAIMED),
    ],
)
def test_classify_admission_matches_the_pre_claim_check(
    labels: set[str], expected: ClaimResult
) -> None:
    assert classify_admission(labels, DispatchConfig()) is expected


def test_classify_admission_admits_an_approval_hold_only_with_approve() -> None:
    config = DispatchConfig(approval=ApprovalConfig(label="needs-approval"))
    held = {"needs-approval"}
    assert classify_admission(held, config) is ClaimResult.NOT_READY
    # --approve releases the hold back to 'ready' before claiming, so the
    # preview must not report a refusal the real run would never make.
    assert classify_admission(held, config, approve=True) is ClaimResult.CLAIMED
    # A claimed issue stays claimed: --approve is not a claim override.
    assert (
        classify_admission({"needs-approval", "in-progress"}, config, approve=True)
        is ClaimResult.ALREADY_CLAIMED
    )


def test_try_claim_issue_delegates_its_pre_claim_check(capsys: Any) -> None:
    """The real claim refuses through the same classifier the preview uses."""
    config = DispatchConfig()
    from kew.labels import try_claim_issue

    with (
        patch("kew.labels._fetch_issue_labels", return_value={"chore"}),
        patch(
            "kew.labels.classify_admission", return_value=ClaimResult.NOT_READY
        ) as classify,
    ):
        assert try_claim_issue(7, config) is ClaimResult.NOT_READY
    classify.assert_called_once_with({"chore"}, config)
    assert "'ready' label not present" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --dry-run reports the admission checks
# ---------------------------------------------------------------------------


def test_dry_run_names_the_missing_ready_label(capsys: Any) -> None:
    result = _run_dry(DispatchConfig(), _issue(labels=[{"name": "complex"}]))

    err = capsys.readouterr().err
    assert "not labeled 'ready'" in err
    assert "Add the label first." in err
    # The preview must not promise a dispatch the real run refuses.
    assert "Would dispatch" not in err
    assert result == DispatchCommandResult("refused")
    assert _dispatch_exit_code(result) == 2


def test_dry_run_previews_a_ready_issue_as_before(capsys: Any) -> None:
    result = _run_dry(DispatchConfig(), _issue(labels=_ready()))

    err = capsys.readouterr().err
    assert "[DRY RUN] Would dispatch: #2055 — Some issue" in err
    assert result == DispatchCommandResult("executed")
    assert _dispatch_exit_code(result) == 0


def test_dry_run_reports_an_already_claimed_issue(capsys: Any) -> None:
    result = _run_dry(DispatchConfig(), _issue(labels=[{"name": "in-progress"}]))

    err = capsys.readouterr().err
    assert "another dispatch already claimed it" in err
    assert "Would dispatch" not in err
    assert _dispatch_exit_code(result) == 2


def test_dry_run_with_approve_previews_a_held_issue(capsys: Any) -> None:
    config = DispatchConfig(approval=ApprovalConfig(label="needs-approval"))
    issue = _issue(labels=[{"name": "needs-approval"}])

    refused = _run_dry(config, issue)
    assert _dispatch_exit_code(refused) == 2

    approved = _run_dry(config, issue, approve=True)
    assert "[DRY RUN] Would dispatch: #2055" in capsys.readouterr().err
    assert _dispatch_exit_code(approved) == 0


def test_dry_run_reports_a_surviving_remote_branch(capsys: Any) -> None:
    """The preview reads the duplicate-dispatch guard the real run reads (#2058).

    A ready issue whose branch still exists on the remote previewed as "Would
    dispatch" and exited 0 while the real run refused it and exited 2.
    """
    result = _run_dry(DispatchConfig(), _issue(labels=_ready()), branch_exists=True)

    err = capsys.readouterr().err
    assert "remote branch 'agent/issue-2055' already exists" in err
    assert "Would dispatch" not in err
    assert result == DispatchCommandResult("refused")
    assert _dispatch_exit_code(result) == 2


def test_dry_run_prefers_the_branch_guard_over_the_label_verdict(capsys: Any) -> None:
    """The real run checks the branch before the claim, so the preview does too."""
    result = _run_dry(
        DispatchConfig(), _issue(labels=[{"name": "in-progress"}]), branch_exists=True
    )

    err = capsys.readouterr().err
    assert "remote branch 'agent/issue-2055' already exists" in err
    assert "another dispatch already claimed it" not in err
    assert _dispatch_exit_code(result) == 2


def test_queue_dry_run_branch_refusal_is_not_a_command_failure() -> None:
    """A queue-selected issue whose branch survives is still an empty cycle."""
    result = _run_dry(
        DispatchConfig(), _issue(labels=_ready()), force_issue=None, branch_exists=True
    )
    assert result == DispatchCommandResult("empty")
    assert _dispatch_exit_code(result) == 0


def test_queue_dry_run_refusal_is_not_a_command_failure() -> None:
    """Only an issue named on the command line turns a refusal into exit 2."""
    result = _run_dry(
        DispatchConfig(), _issue(labels=[{"name": "in-progress"}]), force_issue=None
    )
    assert result == DispatchCommandResult("empty")
    assert _dispatch_exit_code(result) == 0


# ---------------------------------------------------------------------------
# the real run's refusals exit non-zero for a named issue
# ---------------------------------------------------------------------------


def _run_real(
    *,
    force_issue: int | None,
    claim: ClaimResult = ClaimResult.CLAIMED,
    issue: dict[str, Any] | None = None,
    branch_exists: bool = False,
) -> DispatchCommandResult:
    with (
        patch("kew.cli.select_next_issue", return_value=issue),
        patch("kew.cli.build_prompt", return_value="prompt"),
        patch("kew.cli._preflight_runner"),
        patch("kew.cli.remote_branch_exists", return_value=branch_exists),
        patch("kew.cli.try_claim_issue", return_value=claim),
        patch("kew.cli.execute_dispatch") as launch,
    ):
        result = _dispatch_one(DispatchConfig(), force_issue=force_issue, dry_run=False)
    launch.assert_not_called()
    return result


@pytest.mark.parametrize("claim", [ClaimResult.NOT_READY, ClaimResult.ALREADY_CLAIMED])
def test_named_issue_claim_refusal_exits_two(claim: ClaimResult) -> None:
    result = _run_real(force_issue=2055, claim=claim, issue=_issue(labels=_ready()))
    assert result == DispatchCommandResult("refused")
    assert _dispatch_exit_code(result) == 2


def test_named_issue_with_an_existing_remote_branch_exits_two() -> None:
    result = _run_real(
        force_issue=2055, issue=_issue(labels=_ready()), branch_exists=True
    )
    assert _dispatch_exit_code(result) == 2


def test_dry_run_and_real_run_refuse_a_surviving_branch_alike(capsys: Any) -> None:
    """Same verdict, same words, same exit code — the parity docs/cli claims."""
    issue = _issue(labels=_ready())
    previewed = _run_dry(DispatchConfig(), issue, branch_exists=True)
    preview_err = capsys.readouterr().err

    real = _run_real(force_issue=2055, issue=issue, branch_exists=True)
    real_err = capsys.readouterr().err

    refusal = (
        "Skipping #2055 — remote branch 'agent/issue-2055' already exists "
        "(another dispatch may be in progress)"
    )
    assert refusal in " ".join(preview_err.split())
    assert refusal in " ".join(real_err.split())
    assert _dispatch_exit_code(previewed) == _dispatch_exit_code(real) == 2


def test_an_unknown_named_issue_exits_two(capsys: Any) -> None:
    result = _run_real(force_issue=2055, issue=None)
    assert "#2055 not found or not dispatchable" in capsys.readouterr().err
    assert _dispatch_exit_code(result) == 2


@pytest.mark.parametrize("claim", [ClaimResult.NOT_READY, ClaimResult.ALREADY_CLAIMED])
def test_queue_claim_refusal_still_exits_zero(claim: ClaimResult) -> None:
    """A lost race on a queue-selected issue is an empty cycle, not a failure."""
    result = _run_real(force_issue=None, claim=claim, issue=_issue(labels=_ready()))
    assert result == DispatchCommandResult("empty")
    assert _dispatch_exit_code(result) == 0


def test_empty_queue_still_exits_zero() -> None:
    result = _run_real(force_issue=None, issue=None)
    assert result == DispatchCommandResult("empty")
    assert _dispatch_exit_code(result) == 0


# ---------------------------------------------------------------------------
# the shipped Action template survives the new exit code
# ---------------------------------------------------------------------------

ACTION_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "create-kew" / "templates" / "kew-action.yml"
)


def test_action_template_skips_an_already_claimed_issue() -> None:
    """A named dispatch refusal exits 2, so the label job must not invite one.

    The template dispatches on ``issues: labeled ready``. Before #2058 a benign
    refusal exited 0; now it exits 2 and reds the operator's job, so the job
    guards the case the event payload can already see — the issue carries a
    claim label — and skips instead of dispatching an issue kew would refuse.
    """
    workflow = yaml.safe_load(ACTION_TEMPLATE.read_text(encoding="utf-8"))
    job = workflow["jobs"]["dispatch-issue"]
    condition = " ".join(job["if"].split())
    labels = DispatchConfig().labels

    assert f"github.event.label.name == '{labels.ready}'" in condition
    for claim in (labels.in_progress, labels.agent_working):
        assert f"!contains(github.event.issue.labels.*.name, '{claim}')" in condition, (
            claim
        )
    # The guard replaces nothing: the dispatch itself is unchanged and still
    # fails the step when it refuses.
    assert [step.get("run") for step in job["steps"] if "run" in step] == [
        'kew dispatch "$ISSUE_NUMBER"'
    ]


def test_action_template_documents_the_could_not_run_exit_code() -> None:
    """An operator copying the template learns what a red dispatch step means."""
    header = ACTION_TEMPLATE.read_text(encoding="utf-8").split("\nname: kew", 1)[0]
    assert "Exit codes" in header
    assert "2 - nothing ran" in header
    assert "docs/cli/exit-codes.md" in header


# ---------------------------------------------------------------------------
# the exit-code table's dispatch row matches the command that exists
# ---------------------------------------------------------------------------

EXIT_CODES_DOC = Path(__file__).resolve().parents[2] / "docs" / "cli" / "exit-codes.md"


def _exit_row_cells(family: str) -> list[list[str]]:
    """Outcome labels of *family*'s row, indexed by public exit code."""
    row = next(
        line
        for line in EXIT_CODES_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| `{family}` |")
    )
    return [cell.strip().split("; ") for cell in row.strip("|").split("|")][1:]


def test_dispatch_has_no_queue_mode_to_document() -> None:
    """``kew dispatch`` always names an issue, so it has no empty-queue outcome."""
    result = CliRunner().invoke(app, ["dispatch"])

    assert result.exit_code == 2
    assert "Missing argument" in " ".join(result.output.split())


def test_dispatch_exit_row_never_promises_an_empty_queue() -> None:
    """The empty cycle is ``kew run``'s outcome; a named refusal is dispatch's (#2058).

    A round of review once moved "no dispatchable issue in the queue" back into
    the dispatch family's exit-0 column, where no invocation can reach it: every
    ``kew dispatch`` names an issue, and a named issue that is not dispatchable
    exits 2.
    """
    refused = _run_real(
        force_issue=2055, claim=ClaimResult.NOT_READY, issue=_issue(labels=_ready())
    )
    empty = _run_real(force_issue=None, issue=None)

    dispatch_cells = _exit_row_cells("dispatch")
    assert (
        "admission refusal for a named issue"
        in dispatch_cells[_dispatch_exit_code(refused)]
    )
    for label in dispatch_cells[0]:
        assert "queue" not in label.lower(), label
        assert "dispatchable" not in label.lower(), label

    # The outcome still belongs to the family that can reach it.
    assert "empty queue" in _exit_row_cells("run")[_dispatch_exit_code(empty)]


# ---------------------------------------------------------------------------
# --loop is exempt from admission, and the page says so
# ---------------------------------------------------------------------------

DISPATCH_DOC = Path(__file__).resolve().parents[2] / "docs" / "cli" / "dispatch.md"


def test_loop_dispatch_applies_no_admission_check(monkeypatch, tmp_path) -> None:
    """``--loop`` reads none of the three admission facts (#2191 review).

    ``_run_loop_dispatch`` loads the named issue through
    ``select_next_issue(force_issue=...)``, which is a bare ``gh issue view``:
    no ``ready`` read, no claim label read, no remote branch read, and no claim
    taken. The Admission section of ``docs/cli/dispatch.md`` is scoped to
    single-shot dispatch for exactly that reason — if a guard is added here,
    this test fails and the page's ``--loop`` carve-out has to go with it.
    """
    from unittest.mock import MagicMock

    from test_cli_dispatch_loop import _common_patches

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    captured: dict[str, Any] = {}
    # Every admission fact points at a refusal: not ready, already claimed, and
    # a surviving remote branch. Single-shot dispatch refuses all three.
    _common_patches(monkeypatch, captured=captured, labels=[{"name": "in-progress"}])
    branch = MagicMock(return_value=True)
    claim = MagicMock(return_value=ClaimResult.ALREADY_CLAIMED)
    monkeypatch.setattr("kew.cli.remote_branch_exists", branch)
    monkeypatch.setattr("kew.cli.try_claim_issue", claim)

    result = CliRunner().invoke(app, ["dispatch", "7", "--loop"])

    assert result.exit_code == 0, result.output
    assert captured["loop"].issue_num == 7, "the loop ran despite every refusal"
    branch.assert_not_called()
    claim.assert_not_called()


def test_dispatch_page_scopes_admission_away_from_loop() -> None:
    """The Admission section must not promise ``--loop`` a guard it lacks."""
    text = " ".join(DISPATCH_DOC.read_text(encoding="utf-8").split())

    assert "## Admission (single-shot dispatch only)" in text
    assert "Single-shot `kew dispatch <issue>` admits a named issue only when" in text
    for claim in (
        "**`kew dispatch <issue> --loop` applies none of these three checks.**",
        "it never reads the remote branch",
        "It also takes no label claim",
        "the host-local checkout claim",
        "a loop started on another host is not guarded at all",
    ):
        assert claim in text, claim
