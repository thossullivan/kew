"""A PR between kew's own review rounds is kew's move, not a needs-you (#2328).

The follow-up to #2279/D8. That node stopped a round kew is READING from
counting as somebody's move; this one closes the gap it left — the state
BETWEEN two rounds, where the last verdict was NEEDS_CHANGES or NEEDS_FULL,
nothing is pending, and kew's own remediation or next full round is due.

Photographed on 2026-10-04 in the operator's ledger: #2278, after round 4
returned NEEDS_FULL with round 5 not yet started, counted in the header's
needs-you beside #2279 — which was READY and really awaiting a merge decision.
Two rows, one colour, two completely different asks.

The rule is read in ONE place (``_judgment_state`` in kew/service/work.py) and
drawn from ONE table (``QUIET_EXCEPTIONS`` in kew/service/attention.py), so the
judgment row, the DAG diamond and the Work screen's band cannot disagree about
whether anybody was asked. What this module holds is the line itself: the
cycle's own phases are quiet, the verdict that ENDS the cycle is a person's,
and a HELD node is a needs-you however quiet its projection reads — because the
hold is what a person clears, with the command that clears it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from test_needs_you_one_set import (
    EPIC,
    NOW,
    PR,
    _join,
    _node,
    _run,
    _store,
    _view,
)
from test_needs_you_one_set import (
    _no_network as _no_network,  # noqa: PLC0414 - autouse fixture, re-exported
)
from test_service_dashboard import _config

from kew.epic_holds import ROUNDS_EXHAUSTED_HOLD_REASON
from kew.run_store import EpicHold, ReviewRound
from kew.service import dashboard, needs_you
from kew.service.attention import QUIET_EXCEPTIONS, needs_a_person

ITEM = "2278"
READY_ITEM = "2279"
READY_PR = "https://example.invalid/pr/2279"


def _round(
    item_id: str,
    verdict: str,
    *,
    number: int = 4,
    accepted: str | None = None,
) -> ReviewRound:
    """One recorded round — a FINISHED one, which is the whole point.

    A ``review_rounds`` row is only ever written when a round is over, so the
    newest row plus no pending review run is exactly "between rounds".
    ``accepted`` is the stop rule's own disposition (``READY_WITH_RESIDUALS``,
    #2151), which the projection reads through ``effective_verdict``.
    """
    return ReviewRound(
        epic_ref=EPIC,
        item_id=item_id,
        round=number,
        head_sha="a" * 40,
        verdict=verdict,
        sidecar_digest=None,
        recorded_at=datetime(2026, 10, 4, 9, 0, 0, tzinfo=UTC),
        accepted_verdict=accepted,
    )


def _between_rounds_store(verdict: str, status: str, **extra: Any) -> Any:
    """One item with an open PR whose latest recorded round returned *verdict*.

    *status* is what the driver journals for that verdict — ``remediating``
    after findings, ``adversarial_review`` when a full round is due
    (``_persisted_verdict_status`` in graph/driver/overlay.py). Neither is a
    ``kew.waits`` bucket, so the epic snapshot contributes no row of its own
    and what the set serves for this item is the work projection's, which is
    the row #2278 was counted as.
    """
    nodes = (_node(ITEM, status, pr_url=PR),)
    return nodes, _store(
        nodes=nodes,
        syncable=(_run(ITEM, pr_state="open", pr_url=PR),),
        rounds=(_round(ITEM, verdict),),
        **extra,
    )


# --------------------------------------------------------------------------- #
# Between rounds — kew owes the next move
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "status", "exception", "detail"),
    [
        ("NEEDS_CHANGES", "remediating", "review_remediation_due", "kew is revising"),
        (
            "NEEDS_FULL",
            "adversarial_review",
            "review_round_due",
            "kew re-reviews next",
        ),
    ],
)
def test_a_verdict_kew_owes_the_next_round_on_is_not_a_needs_you(
    verdict: str, status: str, exception: str, detail: str
) -> None:
    """The reproduction, both halves: no judgment row, and no diamond.

    The judgment field is asserted too, not only the exception: the Work
    screen's needs-you band raises on ``awaiting_human_decision`` whatever the
    exception says (``needsYouBand`` in frontend/src/lib/work/evidence.ts), so
    a fix that reached the exception alone would leave the magenta band up over
    a row nobody counts.
    """
    nodes, store = _between_rounds_store(verdict, status)

    view = _view(store, ITEM)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert view.judgment.state == "between_review_rounds"
    assert (view.exception.state, view.exception.detail) == (exception, detail)
    assert view.exception.state in QUIET_EXCEPTIONS
    assert view.safest_action.action == "wait"
    assert not needs_a_person(view)
    assert [e for e in composed.entries if e.item_id == ITEM] == []
    assert join.by_node == {}
    assert composed.unavailable == []


@pytest.mark.parametrize(
    ("verdict", "accepted"),
    [("READY", None), ("NEEDS_CHANGES", "READY_WITH_RESIDUALS")],
)
def test_the_verdict_that_ends_the_cycle_is_a_merge_decision(
    verdict: str, accepted: str | None
) -> None:
    """The other half of the rule, which is what keeps it from being "never".

    ``READY_WITH_RESIDUALS`` is the case a reader of the raw ``verdict`` gets
    wrong (#2151): the reviewer said NEEDS_CHANGES and the stop rule accepted
    the round anyway, so the item is awaiting a merge and nothing is owed.
    """
    nodes = (_node(ITEM, "in_review", pr_url=PR),)
    store = _store(
        nodes=nodes,
        syncable=(_run(ITEM, pr_state="open", pr_url=PR),),
        rounds=(_round(ITEM, verdict, accepted=accepted),),
    )

    view = _view(store, ITEM)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert view.judgment.state == "awaiting_human_decision"
    assert view.exception.state == "awaiting_human_decision"
    assert needs_a_person(view)
    row = next(e for e in composed.entries if e.item_id == ITEM)
    assert (row.type, row.reason) == (
        "pr_review",
        f"PR open for #{ITEM} — awaiting review",
    )


def test_a_pull_request_outside_any_epic_is_unchanged() -> None:
    """No recorded round means no kew review applies — so the rule says nothing.

    The absence of a verdict is not a verdict: an item nobody reviews is a pull
    request waiting on a person exactly as it was before this node, and reading
    "nothing recorded" as "kew owes a round" would hide every PR kew's review
    never governs.
    """
    store = _store(syncable=(_run("3100", pr_state="open", pr_url=PR),))

    view = _view(store, "3100")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert view.judgment.state == "awaiting_human_decision"
    assert view.exception.state == "awaiting_human_decision"
    row = next(e for e in composed.entries if e.item_id == "3100")
    assert (row.type, row.epic_ref) == ("pr_review", None)


def test_a_held_node_between_rounds_is_still_a_needs_you() -> None:
    """The round cap is where the cycle STOPS owing the next move.

    A held node is the one case where "between rounds" is not kew's turn: the
    remediation it is between never runs until somebody releases the hold. The
    needs-you is the hold's own row, with the command that clears it — the
    projection's row is quiet, and the count is still one.
    """
    nodes, store = _between_rounds_store(
        "NEEDS_CHANGES",
        "held",
        holds=(
            EpicHold(
                int(ITEM),
                EPIC,
                ROUNDS_EXHAUSTED_HOLD_REASON,
                datetime(2026, 10, 4, 10, 0, 0, tzinfo=UTC),
                ITEM,
            ),
        ),
    )

    view = _view(store, ITEM)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert not needs_a_person(view)
    held = [e for e in composed.entries if e.item_id == ITEM]
    assert [(e.type, e.command) for e in held] == [
        ("budget_hold", f"kew epic unhold {EPIC} {ITEM}")
    ]
    assert held[0].label == "the review round cap was reached"
    # The DAG draws the same one diamond, off the same hold.
    assert join.by_node[ITEM].command == f"kew epic unhold {EPIC} {ITEM}"


# --------------------------------------------------------------------------- #
# The live check (#2328's acceptance): one READY, one between rounds -> 1
# --------------------------------------------------------------------------- #


def test_the_header_counts_the_merge_decision_and_not_the_item_between_rounds() -> None:
    """The ledger of 2026-10-04, as a fixture: #2278 and #2279 side by side.

    One number, asserted through ``dashboard.assemble`` — the payload the
    header's counter, Monitor's column and Work's filter all read — because the
    defect was never one surface's: every one of them counted the same two.
    """
    nodes = (
        _node(ITEM, "adversarial_review", pr_url=PR),
        _node(READY_ITEM, "in_review", pr_url=READY_PR),
    )
    store = _store(
        nodes=nodes,
        syncable=(
            _run(ITEM, pr_state="open", pr_url=PR),
            _run(READY_ITEM, pr_state="open", pr_url=READY_PR),
        ),
        rounds=(_round(ITEM, "NEEDS_FULL"), _round(READY_ITEM, "READY", number=1)),
    )

    payload = dashboard.assemble(store, _config(), now=NOW)

    assert payload.unavailable == []
    assert [(r.type, r.item_id) for r in payload.rows] == [("pr_review", READY_ITEM)]
    assert payload.needs_you_by_epic == {EPIC: 1}
