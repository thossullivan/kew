"""One check-run decision for the loop gate and the epic driver (#2033)."""

from __future__ import annotations

import pytest

from kew import ci_checks
from kew.ci_checks import CheckRun, classify_check_runs
from kew.graph.driver import facts
from kew.loops import ci as loops_ci


def test_the_loop_gate_and_the_driver_share_one_decision() -> None:
    assert loops_ci.classify_check_runs is ci_checks.classify_check_runs
    assert facts.classify_check_runs is ci_checks.classify_check_runs
    assert loops_ci.check_runs_command is ci_checks.check_runs_command
    assert facts.check_runs_command is ci_checks.check_runs_command


@pytest.mark.parametrize(
    ("runs", "state"),
    [
        ([], "no_checks"),
        ([CheckRun("test", "in_progress", None)], "pending"),
        ([CheckRun("test", "completed", "skipped")], "awaiting_approval"),
        (
            [
                CheckRun("test", "completed", "skipped", id=1),
                CheckRun("test", "completed", "success", id=2),
            ],
            "success",
        ),
        (
            [
                CheckRun("test", "completed", "success", id=1),
                CheckRun("test", "completed", "skipped", id=2),
            ],
            "success",
        ),
        (
            [
                CheckRun("lint", "completed", "failure"),
                CheckRun("test", "completed", "skipped"),
            ],
            "failure",
        ),
        ([CheckRun("docs", "completed", "neutral")], "no_checks"),
    ],
)
def test_classify_check_runs(runs: list[CheckRun], state: str) -> None:
    assert (
        classify_check_runs(runs, gated_checks=["test", "agent-image-scan"])[0] == state
    )


def test_awaiting_evidence_names_the_checks_that_did_not_run() -> None:
    state, evidence = classify_check_runs(
        [
            CheckRun("lint", "completed", "success"),
            CheckRun("test", "completed", "skipped"),
        ],
        gated_checks=["test", "agent-image-scan"],
    )
    assert state == "awaiting_approval"
    assert evidence == "awaiting ci-approved: tests did not run (test skipped)"


# ---------------------------------------------------------------------------
# Only the approval-gated checks (ci.approval_gated_checks) wait for a human.
# ---------------------------------------------------------------------------

GATED = ["test", "agent-image-scan"]


def test_a_push_only_skipped_job_beside_a_green_test_run_is_green() -> None:
    state, _ = classify_check_runs(
        [
            CheckRun("test", "completed", "success"),
            CheckRun("publish-docs", "completed", "skipped"),
        ],
        gated_checks=GATED,
    )
    assert state == "success"


def test_a_skipped_gated_test_job_still_awaits_approval() -> None:
    state, evidence = classify_check_runs(
        [
            CheckRun("lint", "completed", "success"),
            CheckRun("test", "completed", "skipped"),
            CheckRun("publish-docs", "completed", "skipped"),
        ],
        gated_checks=GATED,
    )
    assert state == "awaiting_approval"
    assert evidence == "awaiting ci-approved: tests did not run (test skipped)"


def test_a_skipped_non_gated_job_alone_is_no_checks() -> None:
    state, evidence = classify_check_runs(
        [CheckRun("publish-docs", "completed", "skipped")], gated_checks=GATED
    )
    assert state == "no_checks"
    assert evidence == "no check run concluded success"


def test_a_skipped_matrix_leg_of_a_gated_job_awaits_approval() -> None:
    state, evidence = classify_check_runs(
        [
            CheckRun("lint", "completed", "success"),
            CheckRun("test (3.12)", "completed", "skipped"),
        ],
        gated_checks=GATED,
    )
    assert state == "awaiting_approval"
    assert "test (3.12) skipped" in evidence


@pytest.mark.parametrize(
    ("name", "gated"),
    [
        ("test", True),
        ("test (3.12)", True),
        ("agent-image-scan", True),
        ("testing", False),
        ("test-e2e", False),
        ("lint", False),
    ],
)
def test_is_approval_gated(name: str, gated: bool) -> None:
    assert ci_checks.is_approval_gated(name, GATED) is gated


def test_the_gated_list_decides_not_a_built_in_one() -> None:
    runs = [
        CheckRun("lint", "completed", "success"),
        CheckRun("test", "completed", "skipped"),
    ]
    assert classify_check_runs(runs, gated_checks=["e2e"])[0] == "success"


@pytest.mark.parametrize(
    ("state", "total", "expected", "evidence"),
    [
        (
            "success",
            3,
            "no_evidence",
            "commit statuses green but no check runs: merge by hand",
        ),
        ("pending", 1, "pending", None),
        ("failure", 1, "failure", None),
        ("error", 2, "failure", None),
        ("success", 0, "no_evidence", "no CI evidence on head: merge by hand"),
    ],
)
def test_a_commit_status_never_makes_a_head_green(
    state: str, total: int, expected: str, evidence: str | None
) -> None:
    decided, text = ci_checks.classify_commit_status(
        ci_checks.CommitStatus(state, total)
    )
    assert decided == expected
    if evidence is not None:
        assert text == evidence
