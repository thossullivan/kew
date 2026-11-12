"""Tests for the Phase 2 driver status overlay.

One test per precedence branch in ``overlay_status``, plus the critical
spec cases called out in the task brief (newest-run-wins, CANCELLED vs
DONE, reserved-with-no-run-row, HELD beating IN_REVIEW).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import (
    DriverStatus,
    has_successful_open_pr,
    overlay_status,
    starts_fresh_work,
    to_node_status,
)
from kew.graph.driver.tick import _FRONTIER_STATUS_MAP, _NON_FRONTIER_STATUSES
from kew.no_change import (
    ATTESTED,
    AUTHORIZED,
    CANDIDATE,
    COMPLETED,
    EFFECT_PENDING,
    PENDING_CONFIRMATION,
    REJECTED,
    SYNC_PENDING,
)
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    CI_FIX_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
    RunRecord,
)


def _facts(
    *,
    state: str = "OPEN",
    state_reason: str | None = None,
    labels: frozenset[str] = frozenset(),
    pr_number: int | None = None,
    admission_status: NodeStatus = NodeStatus.TODO,
    pr_state: str | None = None,
    pr_link_source: str | None = None,
) -> NodeFacts:
    return NodeFacts(
        issue_num=1,
        state=state,
        state_reason=state_reason,
        labels=labels,
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
        admission_status=admission_status,
        pr_state=pr_state,
        pr_link_source=pr_link_source,
    )


def _run(
    status: str,
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    run_kind: str = "dispatch",
) -> RunRecord:
    return RunRecord(
        run_id=f"run-{started_at.isoformat()}-{run_kind}",
        issue_num=1,
        branch="epic/1",
        status=status,  # type: ignore[arg-type]
        exit_code=0,
        started_at=started_at,
        finished_at=finished_at,
        run_kind=run_kind,
    )


_T1 = datetime(2026, 1, 1, tzinfo=UTC)
_T2 = datetime(2026, 1, 2, tzinfo=UTC)


def test_held_ci_pending_wins_over_everything() -> None:
    # HELD beats even an open PR (which would otherwise be IN_REVIEW).
    facts = _facts(state="OPEN", pr_number=42)
    assert (
        overlay_status(facts, [], reserved=False, held_reason="ci_pending")
        == DriverStatus.HELD
    )


def test_reserved_with_no_run_rows_is_in_progress() -> None:
    facts = _facts(state="OPEN")
    assert (
        overlay_status(facts, [], reserved=True, held_reason=None)
        == DriverStatus.IN_PROGRESS
    )


def test_newest_run_pending_is_in_progress() -> None:
    facts = _facts(state="OPEN")
    runs = [_run("pending", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_PROGRESS
    )


def test_completed_run_awaiting_publication_is_not_redispatchable() -> None:
    facts = _facts(state="OPEN")
    runs = [_run("awaiting_publication", started_at=_T1, finished_at=_T2)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.AWAITING_PUBLICATION
    )


def test_provider_completed_beats_stale_awaiting_publication_overlay() -> None:
    facts = _facts(state="CLOSED", state_reason="COMPLETED")
    runs = [_run("awaiting_publication", started_at=_T1, finished_at=_T2)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.DONE
    )


def test_newest_terminal_failure_is_failed() -> None:
    facts = _facts(state="OPEN")
    runs = [_run("failure", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.FAILED
    )


def test_newest_run_wins_success_supersedes_stale_failure() -> None:
    # Critical spec case: [failure@t1, success@t2] must NOT be FAILED.
    facts = _facts(state="OPEN")
    runs = [_run("failure", started_at=_T1), _run("success", started_at=_T2)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        != DriverStatus.FAILED
    )


def test_failed_ci_fix_does_not_supersede_successful_implementation() -> None:
    facts = _facts(state="OPEN", pr_number=7)
    runs = [
        _run("success", started_at=_T1, finished_at=_T1),
        _run(
            "failure",
            started_at=_T2,
            finished_at=_T2,
            run_kind=CI_FIX_RUN_KIND,
        ),
    ]

    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )
    assert has_successful_open_pr(facts, runs)


def test_agent_failed_label_with_no_failing_run_is_failed() -> None:
    facts = _facts(state="OPEN", labels=frozenset({"agent-failed"}))
    runs = [_run("success", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.FAILED
    )


def test_closed_completed_is_done() -> None:
    facts = _facts(state="CLOSED", state_reason="COMPLETED")
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None) == DriverStatus.DONE
    )


def test_closed_not_planned_is_cancelled_not_done() -> None:
    # Critical spec case: NOT_PLANNED close must not read as DONE.
    facts = _facts(state="CLOSED", state_reason="NOT_PLANNED")
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.CANCELLED
    )


def test_closed_no_state_reason_is_cancelled() -> None:
    facts = _facts(state="CLOSED", state_reason=None)
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.CANCELLED
    )


def test_blocked_label_is_blocked() -> None:
    facts = _facts(state="OPEN", labels=frozenset({"blocked"}))
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.BLOCKED
    )


def test_open_pr_is_in_review() -> None:
    facts = _facts(state="OPEN", pr_number=7)
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )


def test_success_with_open_pr_beats_stale_awaiting_continuation() -> None:
    """Remote publication truth must outrank an orphaned recovery handle."""
    facts = _facts(state="OPEN", pr_number=7)
    runs = [_run("success", started_at=_T1, finished_at=_T2)]

    assert (
        overlay_status(
            facts,
            runs,
            reserved=False,
            held_reason=None,
            continuation_state="awaiting_continuation",
        )
        == DriverStatus.IN_REVIEW
    )


def test_success_without_open_pr_does_not_hide_awaiting_continuation() -> None:
    """A run row alone is insufficient to discard genuinely preserved work."""
    facts = _facts(state="OPEN")
    runs = [_run("success", started_at=_T1, finished_at=_T2)]

    assert (
        overlay_status(
            facts,
            runs,
            reserved=False,
            held_reason=None,
            continuation_state="awaiting_continuation",
        )
        == DriverStatus.AWAITING_CONTINUATION
    )


def test_open_pr_with_pending_review_label_is_in_review() -> None:
    # With a PR actually open for the node, the label is corroborated.
    facts = _facts(state="OPEN", pr_number=7, labels=frozenset({"pr-pending-review"}))
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )


def test_stale_pending_review_label_without_open_pr_is_todo() -> None:
    # #753: a closed-unmerged PR leaves `pr-pending-review` behind on the
    # issue while its linked-PR reference (pr_number) drops to None. The
    # stale label alone must NOT wedge the node as IN_REVIEW — it falls
    # through to TODO so a retried node re-enters the dispatch frontier.
    facts = _facts(
        state="OPEN", pr_number=None, labels=frozenset({"pr-pending-review"})
    )
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None) == DriverStatus.TODO
    )


def test_default_is_todo() -> None:
    facts = _facts(state="OPEN")
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None) == DriverStatus.TODO
    )


def test_not_admitted_is_queued_and_not_blocked() -> None:
    facts = _facts(admission_status=NodeStatus.QUEUED)
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.QUEUED
    )


def test_blocked_label_wins_over_open_pr() -> None:
    facts = _facts(state="OPEN", pr_number=7, labels=frozenset({"blocked"}))
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        == DriverStatus.BLOCKED
    )


def test_newest_determined_by_started_at_not_list_order() -> None:
    # Out-of-order input list: success (t2) listed first, failure (t1) second.
    # Newest by started_at is still the success at t2.
    facts = _facts(state="OPEN")
    runs = [_run("success", started_at=_T2), _run("failure", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        != DriverStatus.FAILED
    )


def test_to_node_status_maps_shared_members() -> None:
    assert to_node_status(DriverStatus.TODO) == NodeStatus.TODO
    assert to_node_status(DriverStatus.IN_PROGRESS) == NodeStatus.IN_PROGRESS
    assert to_node_status(DriverStatus.IN_REVIEW) == NodeStatus.IN_REVIEW
    assert to_node_status(DriverStatus.DONE) == NodeStatus.DONE
    assert to_node_status(DriverStatus.BLOCKED) == NodeStatus.BLOCKED
    assert to_node_status(DriverStatus.FAILED) == NodeStatus.FAILED


def test_to_node_status_raises_for_cancelled() -> None:
    with pytest.raises(ValueError, match="CANCELLED"):
        to_node_status(DriverStatus.CANCELLED)


def test_to_node_status_raises_for_held() -> None:
    with pytest.raises(ValueError, match="HELD"):
        to_node_status(DriverStatus.HELD)


# ---------------------------------------------------------------------------
# retried_at: non-destructive retry-marker supersession (#427 Phase 2)
# ---------------------------------------------------------------------------


def test_failure_before_retry_marker_is_superseded_not_failed() -> None:
    # The operator retried (retried_at=_T2) AFTER the failure run at _T1 —
    # the stale failure must no longer report FAILED.
    facts = _facts(state="OPEN")
    runs = [_run("failure", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None, retried_at=_T2)
        != DriverStatus.FAILED
    )


def test_failure_after_retry_marker_is_still_failed() -> None:
    # A NEW failure run started AFTER the retry marker is a fresh failure,
    # not a superseded stale one — must still report FAILED.
    facts = _facts(state="OPEN")
    runs = [_run("failure", started_at=_T2)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None, retried_at=_T1)
        == DriverStatus.FAILED
    )


def test_retried_at_none_is_unchanged_behavior() -> None:
    facts = _facts(state="OPEN")
    runs = [_run("failure", started_at=_T1)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None, retried_at=None)
        == DriverStatus.FAILED
    )


# --- F4: status derivation filters review-kind runs (Task 1) ---


@pytest.mark.parametrize("review_kind", ["adversarial_review", "review_remediation"])
def test_pending_review_run_does_not_flip_node_in_progress(review_kind: str) -> None:
    # A live reviewer/remediation row on the node's item must not derive
    # IN_PROGRESS — the implementing agent is done; the node is IN_REVIEW.
    facts = _facts(state="OPEN", pr_number=7)
    runs = [_run("pending", started_at=_T2, run_kind=review_kind)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )


@pytest.mark.parametrize("review_kind", ["adversarial_review", "review_remediation"])
def test_killed_review_run_does_not_derive_failed(review_kind: str) -> None:
    # A killed reviewer must not read as a node failure — the crash table
    # owns that outcome, not status derivation (F4).
    facts = _facts(state="OPEN", pr_number=7)
    runs = [_run("killed", started_at=_T2, run_kind=review_kind)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )


def test_mixed_kind_rows_derive_from_implementation_run_only() -> None:
    # One item carries a terminal implementation success AND a later killed
    # reviewer row: status derives from the implementation run (the node is
    # in review), never the reviewer's terminal status.
    facts = _facts(state="OPEN", pr_number=7)
    runs = [
        _run("success", started_at=_T1, finished_at=_T1),
        _run("killed", started_at=_T2, run_kind="adversarial_review"),
    ]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_REVIEW
    )


def test_implementation_pending_still_flips_in_progress() -> None:
    # Control: an ordinary dispatch pending row is unaffected by the filter.
    facts = _facts(state="OPEN")
    runs = [_run("pending", started_at=_T2)]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.IN_PROGRESS
    )


def test_implementation_failure_still_derives_failed_with_review_noise() -> None:
    # Control: a real implementation failure derives FAILED even when a
    # newer review row exists (the review row is filtered out).
    facts = _facts(state="OPEN")
    runs = [
        _run("failure", started_at=_T1, finished_at=_T1),
        _run("success", started_at=_T2, run_kind="review_remediation", finished_at=_T2),
    ]
    assert (
        overlay_status(facts, runs, reserved=False, held_reason=None)
        == DriverStatus.FAILED
    )


# --- N3: HELD-reason routing for governed-review hold reasons (Task 4) ---

_REVIEW_HOLD_REASONS = [
    "review_crash",
    "review_budget",
    "review_unparseable",
    "review_rounds_exhausted",
    "review_pr_closed",
    "review_sandbox_refused",
    # The two reviewer-never-started holds also arrive WITH their detail — the
    # fix for the layer that refused rides in the reason (#2043), so the
    # routing has to match them by prefix.
    "review_sandbox_refused: the reviewer never started — run kew sandbox "
    "build --runner claude-code, or map [sandbox] images.claude-code",
    "review_launch_failed: the reviewer never started — start the container "
    "runtime [sandbox] runtime names",
]


@pytest.mark.parametrize("reason", _REVIEW_HOLD_REASONS)
def test_review_hold_reason_routes_to_held(reason: str) -> None:
    # Each governed-review hold reason must route to HELD, beating even an
    # open PR (which would otherwise read IN_REVIEW) — the same precedence
    # as ci_pending / budget_preflight (N3).
    facts = _facts(state="OPEN", pr_number=42)
    assert (
        overlay_status(facts, [], reserved=False, held_reason=reason)
        == DriverStatus.HELD
    )


def test_containment_hold_reason_routes_to_held() -> None:
    # #1144: a node whose runner could not be contained is HELD until the
    # operator maps an image and un-holds it. Falling through would put it
    # straight back on the frontier to be refused again on the next tick.
    facts = _facts(state="OPEN", pr_number=42)
    reason = (
        "sandbox_containment: Sandbox: runner 'opencode' has no container "
        "image mapped — un-hold with `kew epic unhold 42`"
    )
    assert (
        overlay_status(facts, [], reserved=False, held_reason=reason)
        == DriverStatus.HELD
    )


def test_detailed_review_budget_hold_reason_routes_to_held() -> None:
    # #2153: the review-pool hold is the one review reason that carries detail
    # (pool, spend, and the key to raise). Matched bare-only it routed nowhere,
    # so an exhausted node kept reading IN_REVIEW/REMEDIATING and was
    # re-dispatched and re-refused every tick.
    facts = _facts(state="OPEN", pr_number=42)
    reason = (
        "review_budget: remediation pool $40.00 exhausted (spent $43.97) — "
        "raise [review].max_remediation_usd"
    )
    assert (
        overlay_status(facts, [], reserved=False, held_reason=reason)
        == DriverStatus.HELD
    )


def test_unknown_hold_reason_does_not_route_to_held() -> None:
    # A hold reason the overlay does not recognize must NOT wedge the node
    # HELD — it falls through to the normal precedence (here: IN_REVIEW).
    facts = _facts(state="OPEN", pr_number=42)
    assert (
        overlay_status(facts, [], reserved=False, held_reason="something_else")
        == DriverStatus.IN_REVIEW
    )


# ---------------------------------------------------------------------------
# I15: which ready nodes human-review backpressure may hold back.
# ---------------------------------------------------------------------------


def test_a_node_this_host_has_never_run_is_fresh_independent_work() -> None:
    assert starts_fresh_work([]) is True


def test_a_node_with_a_prior_implementation_run_is_not_fresh() -> None:
    # A retry after a failure is recovery, not a new launch: the work (and any
    # review slot it already consumed) exists. Holding it back would strand it.
    assert (
        starts_fresh_work([_run("failure", started_at=_T1, finished_at=_T1)]) is False
    )


def test_completing_work_is_never_treated_as_fresh() -> None:
    assert starts_fresh_work([_run("pending", started_at=_T1)]) is False
    assert starts_fresh_work([_run("awaiting_publication", started_at=_T1)]) is False
    assert starts_fresh_work([_run("success", started_at=_T1)]) is False


@pytest.mark.parametrize(
    "review_kind", [ADVERSARIAL_REVIEW_RUN_KIND, REVIEW_REMEDIATION_RUN_KIND]
)
def test_a_review_run_alone_does_not_make_the_node_already_started(
    review_kind: str,
) -> None:
    # Governed-review runs are filtered out here exactly as everywhere else in
    # this module: a reviewer having run says nothing about whether the
    # implementation would be new work.
    assert starts_fresh_work([_run("success", started_at=_T1, run_kind=review_kind)])


# --- no-change judgment precedence (I10B / Task 45) -------------------------
#
# A zero-diff exit records a local judgment and writes NOTHING to the provider.
# Until that judgment settles, it is the freshest truth kew has about the item
# — fresher than a CLOSED the provider is reporting for some other reason — so
# it is projected ahead of provider closure.


@pytest.mark.parametrize(
    "no_change_state",
    [
        CANDIDATE,
        ATTESTED,
        PENDING_CONFIRMATION,
        AUTHORIZED,
        SYNC_PENDING,
        EFFECT_PENDING,
    ],
)
def test_a_live_no_change_judgment_outranks_provider_closed(
    no_change_state: str,
) -> None:
    ds = overlay_status(
        _facts(state="CLOSED", state_reason="COMPLETED"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=no_change_state,
    )
    assert ds is DriverStatus.AWAITING_NO_CHANGE


@pytest.mark.parametrize("no_change_state", [COMPLETED, REJECTED])
def test_a_settled_no_change_judgment_lets_provider_closed_stand(
    no_change_state: str,
) -> None:
    ds = overlay_status(
        _facts(state="CLOSED", state_reason="COMPLETED"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=no_change_state,
    )
    assert ds is DriverStatus.DONE


def test_a_hold_still_outranks_a_live_no_change_judgment() -> None:
    ds = overlay_status(
        _facts(),
        [],
        reserved=False,
        held_reason="ci_pending",
        no_change_state=CANDIDATE,
    )
    assert ds is DriverStatus.HELD


def test_a_candidate_earns_no_review_wip_credit() -> None:
    """A no-change candidate has no PR, so it is not review work in flight."""
    facts = _facts(labels=frozenset({"pr-pending-review"}))
    runs = [_run("success", started_at=datetime(2026, 8, 22, 9, tzinfo=UTC))]
    ds = overlay_status(
        facts, runs, reserved=False, held_reason=None, no_change_state=CANDIDATE
    )
    assert ds is DriverStatus.AWAITING_NO_CHANGE
    assert not has_successful_open_pr(facts, runs)


def test_a_candidate_survives_a_restart_as_non_frontier_work() -> None:
    """Restart-safety: the projection is read off the durable judgment, and it
    neither re-enters the dispatch frontier nor satisfies a dependent."""
    ds = overlay_status(
        _facts(), [], reserved=False, held_reason=None, no_change_state=CANDIDATE
    )
    assert ds is DriverStatus.AWAITING_NO_CHANGE
    assert ds in _NON_FRONTIER_STATUSES
    assert _FRONTIER_STATUS_MAP.get(ds) is not NodeStatus.DONE
    with pytest.raises(ValueError):
        to_node_status(ds)


def test_no_judgment_at_all_is_the_unchanged_precedence() -> None:
    ds = overlay_status(
        _facts(state="CLOSED", state_reason="COMPLETED"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=None,
    )
    assert ds is DriverStatus.DONE


# --- no-change dependency effects (I12 / Tasks 49-51) -----------------------


def test_an_authorized_no_change_is_not_done_however_the_provider_reads() -> None:
    """Local authorization is a decision about the item, not the item's state."""
    for state, reason in (("CLOSED", "COMPLETED"), ("CLOSED", None), ("OPEN", None)):
        ds = overlay_status(
            _facts(state=state, state_reason=reason),
            [],
            reserved=False,
            held_reason=None,
            no_change_state=AUTHORIZED,
        )
        assert ds is DriverStatus.AWAITING_NO_CHANGE


def test_a_completed_no_change_is_done_even_before_the_close_is_observed() -> None:
    """Completion required an ACKNOWLEDGED synchronization, so the item is
    terminal here whether or not this tick's provider snapshot caught up —
    otherwise a stale OPEN would re-dispatch work a person already settled."""
    ds = overlay_status(
        _facts(state="OPEN"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=COMPLETED,
    )
    assert ds is DriverStatus.DONE


def test_a_completed_no_change_never_overrides_a_provider_cancellation() -> None:
    ds = overlay_status(
        _facts(state="CLOSED", state_reason="NOT_PLANNED"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=COMPLETED,
    )
    assert ds is DriverStatus.CANCELLED


def test_a_rejected_no_change_leaves_the_open_item_exactly_as_it_was() -> None:
    """Rejection closes nothing and unblocks nothing: no DONE is manufactured."""
    ds = overlay_status(
        _facts(state="OPEN"),
        [],
        reserved=False,
        held_reason=None,
        no_change_state=REJECTED,
    )
    assert ds is DriverStatus.TODO


# --- Merged PR + open item (W2.6/#1741) -----------------------------------


def test_merged_pr_with_an_open_item_is_held_not_todo() -> None:
    """The PR landed; only the body keyword that would have closed the item is
    missing. Reading this as TODO put six merged nodes back on the frontier."""
    assert (
        overlay_status(
            _facts(state="OPEN", pr_number=77, pr_state="MERGED"),
            [],
            reserved=False,
            held_reason=None,
        )
        is DriverStatus.HELD
    )


def test_merged_pr_with_an_unreadable_item_is_held_not_todo() -> None:
    assert (
        overlay_status(
            _facts(state="", pr_number=77, pr_state="MERGED"),
            [],
            reserved=False,
            held_reason=None,
        )
        is DriverStatus.HELD
    )


def test_a_merged_pr_outranks_a_stale_failure_run() -> None:
    assert (
        overlay_status(
            _facts(state="OPEN", pr_number=77, pr_state="MERGED"),
            [_run("failure", started_at=_T1, finished_at=_T1)],
            reserved=False,
            held_reason=None,
        )
        is DriverStatus.HELD
    )


def test_a_merged_pr_never_reaches_the_dispatch_frontier() -> None:
    ds = overlay_status(
        _facts(state="OPEN", pr_number=77, pr_state="MERGED"),
        [],
        reserved=False,
        held_reason=None,
    )
    assert ds is DriverStatus.HELD
    assert _FRONTIER_STATUS_MAP.get(ds, NodeStatus.BLOCKED) is not NodeStatus.TODO


def test_a_merged_pr_outranks_an_agent_failed_label() -> None:
    facts = _facts(
        state="OPEN",
        pr_number=77,
        pr_state="MERGED",
        labels=frozenset({"agent-failed"}),
    )
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None) is DriverStatus.HELD
    )


def test_not_planned_with_a_merged_pr_remains_cancelled() -> None:
    facts = _facts(
        state="CLOSED",
        state_reason="NOT_PLANNED",
        pr_number=77,
        pr_state="MERGED",
    )
    assert (
        overlay_status(facts, [], reserved=False, held_reason=None)
        is DriverStatus.CANCELLED
    )


def test_an_open_pr_keeps_the_node_in_review_across_a_branch_push() -> None:
    """Trap 10: pushing to a PR'd branch must not re-frontier the node."""
    runs = [_run("success", started_at=_T1, finished_at=_T1)]
    linked = _facts(
        state="OPEN", pr_number=77, pr_state="OPEN", pr_link_source="branch"
    )
    before = overlay_status(linked, runs, reserved=False, held_reason=None)
    pushed = replace(linked, head_oid="deadbee", pr_pushed_at="2026-08-27T00:00:00Z")
    after = overlay_status(pushed, runs, reserved=False, held_reason=None)
    assert before is after is DriverStatus.IN_REVIEW


def test_an_unread_pr_stays_in_review_and_off_the_frontier() -> None:
    facts = _facts(state="OPEN", pr_number=77, pr_state="UNKNOWN")

    status = overlay_status(facts, [], reserved=False, held_reason=None)

    assert status is DriverStatus.IN_REVIEW
    assert _FRONTIER_STATUS_MAP.get(status, NodeStatus.BLOCKED) is not NodeStatus.TODO


def test_a_closed_unmerged_pr_state_does_not_wedge_the_node_in_review() -> None:
    """#753 stays fixed now that a linked PR can outlive its closing ref."""
    assert (
        overlay_status(
            _facts(state="OPEN", pr_number=77, pr_state="CLOSED"),
            [],
            reserved=False,
            held_reason=None,
        )
        is DriverStatus.TODO
    )


@pytest.mark.parametrize(
    "reason",
    [
        "ci_pending",
        "review_unparseable",
        "review_budget: pool $40.00 spent $43.97",
        "budget_preflight: over cap",
        "sandbox_containment: no mapped image",
        "admission_refused: gh auth failed",
        # Written by ``tick._hold_refused_frontier_node``, matched by nothing
        # here: the node keeps reading FAILED/TODO and stays dispatchable.
        "credential_issuer_refused: issuer route unsupported",
        "preexisting_worktree_refused: dirty worktree on disk",
        "something_nobody_writes",
    ],
)
def test_shared_hold_predicate_is_exactly_what_routes_a_node_to_held(
    reason: str,
) -> None:
    """`kew.epic_holds` is read by the overlay AND by `kew epic retry` (#2154).

    Two readers, one vocabulary: the overlay decides whether a hold takes the
    node out of the frontier, and the retry verb refuses on exactly that. Drift
    either way strands a node — retry refused on a hold the driver ignores, or
    retry reporting success on one it honors.
    """
    from kew.epic_holds import holds_node_out_of_frontier

    routed_to_held = (
        overlay_status(_facts(), [], reserved=False, held_reason=reason)
        is DriverStatus.HELD
    )
    assert holds_node_out_of_frontier(reason) is routed_to_held


#: Every module that documents a piece of the shared hold vocabulary. The
#: comments here are the cross-references that keep it from drifting, so a
#: reference naming a symbol that no longer exists is the drift, not a guard
#: against it (#2154: the vocabulary moved to ``kew.epic_holds`` and three
#: comments kept pointing at the overlay privates it replaced).
_HOLD_VOCABULARY_MODULES = (
    "kew/epic_holds.py",
    "kew/budget.py",
    "kew/dispatch_preflight.py",
    "kew/cli/epic.py",
    "kew/service/verbs.py",
    "kew/graph/driver/overlay.py",
    "kew/graph/driver/review_stage.py",
)

#: A ``kew.…`` cross-reference in the two forms the codebase writes them:
#: double-backtick literals and Sphinx roles.
_CROSS_REFERENCE = re.compile(
    r"``(kew\.[A-Za-z0-9_.]+)``|:(?:mod|class|meth|data|func|attr):`~?(kew\.[A-Za-z0-9_.]+)`"
)


def _resolves(dotted: str) -> bool:
    """Whether ``dotted`` names a real module, or an attribute chain on one."""
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attr in parts[split:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    return False


def test_hold_vocabulary_cross_references_name_symbols_that_exist() -> None:
    """Every ``kew.…`` reference in the hold-vocabulary modules must resolve.

    These comments are load-bearing: they are how a reader of one hold-reason
    writer finds the predicate that decides whether the reason takes the node
    out of the frontier. A reference to a deleted symbol sends that reader
    nowhere, which is how the two ends of the vocabulary drift apart.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    stale = [
        (module, dotted)
        for module in _HOLD_VOCABULARY_MODULES
        for match in _CROSS_REFERENCE.finditer((src / module).read_text())
        if not _resolves(dotted := match.group(1) or match.group(2))
    ]
    assert stale == [], f"cross-references naming symbols that do not exist: {stale}"
