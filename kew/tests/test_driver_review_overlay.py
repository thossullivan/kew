"""Transition-table tests for the governed adversarial-review overlay states.

Task 10 (#977/#980): `overlay_status` places an open-PR node of a
review-enabled epic into the adversarial-review lifecycle
(ADVERSARIAL_REVIEW / REMEDIATING) and back out to IN_REVIEW only on an
evidenced READY verdict bound to the current head. These tests pin the whole
transition table, including the two spec-critical properties:

- flag-flip mid-epic: an already-approved PR is grandfathered (PRE_POLICY) and
  never re-enters review (F9);
- verdict-void-on-head-move: a READY verdict bound to a stale head is void and
  the node re-enters ADVERSARIAL_REVIEW (F1).
"""

from __future__ import annotations

from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
)


def _facts(
    *,
    review_decision: str | None = None,
    head_oid: str | None = "head1",
) -> NodeFacts:
    return NodeFacts(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=review_decision,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=None,
    )


def _overlay(facts: NodeFacts, review: ReviewOverlay) -> DriverStatus:
    return overlay_status(facts, [], reserved=False, held_reason=None, review=review)


# --- disabled / grandfathering -------------------------------------------


def test_review_disabled_open_pr_is_in_review() -> None:
    ds = _overlay(_facts(), ReviewOverlay(enabled=False))
    assert ds is DriverStatus.IN_REVIEW


def test_none_review_open_pr_is_in_review() -> None:
    ds = overlay_status(_facts(), [], reserved=False, held_reason=None, review=None)
    assert ds is DriverStatus.IN_REVIEW


def test_enabled_no_rounds_unapproved_enrols_into_review() -> None:
    # PR opened under an enabled epic, not yet approved, no reviewer has run:
    # enrol → ADVERSARIAL_REVIEW (awaiting the first reviewer dispatch).
    ds = _overlay(_facts(review_decision=None), ReviewOverlay(enabled=True))
    assert ds is DriverStatus.ADVERSARIAL_REVIEW


def test_enabled_no_rounds_already_approved_is_grandfathered() -> None:
    # Flag flipped while the PR was already human-approved: PRE_POLICY grandfather
    # — it must NOT be thrown back into review (F9).
    ds = _overlay(_facts(review_decision="APPROVED"), ReviewOverlay(enabled=True))
    assert ds is DriverStatus.IN_REVIEW


def test_attempted_review_without_a_round_is_not_grandfathered() -> None:
    """#2147: a reviewer that ran without producing a round (a pre-launch
    sandbox refusal deferred for a bounded retry, a crashed launch) enrolled the
    node — it owes a verdict, so a human approval landing meanwhile must not
    grandfather it back to IN_REVIEW, where nothing would ever re-review it."""
    ds = _overlay(
        _facts(review_decision="APPROVED"),
        ReviewOverlay(enabled=True, attempted=True),
    )
    assert ds is DriverStatus.ADVERSARIAL_REVIEW


def test_observed_in_review_without_current_head_verdict_enrols_approved_pr() -> None:
    """An externally observed open PR must not hide behind PRE_POLICY.

    The driver may first see an ``IN_REVIEW`` node after an operator dispatch or
    a publication recovery.  With adversarial review enabled and no verdict for
    the current head, that observation is enough to schedule the first review.
    """
    ds = _overlay(
        _facts(review_decision="APPROVED"),
        ReviewOverlay(enabled=True, observed_status=NodeStatus.IN_REVIEW),
    )
    assert ds is DriverStatus.ADVERSARIAL_REVIEW


# --- live reviewer / remediator reservations ------------------------------


def test_live_reviewer_reservation_is_adversarial_review() -> None:
    ds = _overlay(
        _facts(review_decision="APPROVED"),  # approval is irrelevant mid-review
        ReviewOverlay(enabled=True, reserved_kind=ADVERSARIAL_REVIEW_RUN_KIND),
    )
    assert ds is DriverStatus.ADVERSARIAL_REVIEW


def test_live_remediation_reservation_is_remediating() -> None:
    ds = _overlay(
        _facts(),
        ReviewOverlay(enabled=True, reserved_kind=REVIEW_REMEDIATION_RUN_KIND),
    )
    assert ds is DriverStatus.REMEDIATING


# --- verdict-driven states ------------------------------------------------


def test_ready_verdict_bound_to_current_head_is_in_review() -> None:
    ds = _overlay(
        _facts(head_oid="head1"),
        ReviewOverlay(
            enabled=True,
            round_count=1,
            latest_verdict="READY",
            latest_head_sha="head1",
            current_head_sha="head1",
        ),
    )
    assert ds is DriverStatus.IN_REVIEW


def test_ready_verdict_on_stale_head_reenters_review() -> None:
    # Force-push after a READY verdict → verdict void → re-review (F1).
    ds = _overlay(
        _facts(head_oid="head2"),
        ReviewOverlay(
            enabled=True,
            round_count=1,
            latest_verdict="READY",
            latest_head_sha="head1",
            current_head_sha="head2",
        ),
    )
    assert ds is DriverStatus.ADVERSARIAL_REVIEW


def test_needs_changes_verdict_is_remediating() -> None:
    ds = _overlay(
        _facts(),
        ReviewOverlay(
            enabled=True,
            round_count=1,
            latest_verdict="NEEDS_CHANGES",
            latest_head_sha="head1",
            current_head_sha="head1",
        ),
    )
    assert ds is DriverStatus.REMEDIATING


def test_rounds_exhausted_hold_wins_over_review_lifecycle() -> None:
    # The driver writes the review_rounds_exhausted hold at the cap; it routes
    # to HELD (precedence 1) even though review is enabled with a NEEDS_CHANGES
    # verdict that would otherwise read REMEDIATING.
    ds = overlay_status(
        _facts(),
        [],
        reserved=False,
        held_reason="review_rounds_exhausted",
        review=ReviewOverlay(
            enabled=True,
            round_count=2,
            latest_verdict="NEEDS_CHANGES",
            latest_head_sha="head1",
            current_head_sha="head1",
        ),
    )
    assert ds is DriverStatus.HELD
