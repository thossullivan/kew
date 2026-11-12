"""One item, one row — and a merge decision only on a verdict for THIS head (#2576).

Photographed on the live ledger on 2026-10-09: ``GET /api/dashboard`` served
#2499 and #2555 as two needs-you rows, though #2555 is the pull request FOR
#2499 (and #2516/#2565 the same). The PR-numbered row also read no review
round — the rounds are keyed by the item — so it asked for a merge decision
while #2516's latest round was NEEDS_FULL.

Two rules, held here:

* every PR-derived row is keyed by the item its pull request closes (the
  ``agent/issue-<n>`` branch), and the item's own view is the one judged;
* a ``merge_decision`` exists only when the latest round on the pull request's
  CURRENT head ended the cycle — no round yet on a governed epic node, or a
  READY on a head the pull request has since moved past, is kew's move.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from test_needs_you_between_rounds import _round
from test_needs_you_one_set import (
    EPIC,
    NOW,
    _node,
    _review_run,
    _run,
    _store,
    _view,
)
from test_needs_you_one_set import (
    _no_network as _no_network,  # noqa: PLC0414 - autouse fixture, re-exported
)
from test_service_dashboard import _config

from kew.run_store import NodePrFacts
from kew.run_store_work import AdmissionIdentity
from kew.service import dashboard, needs_you
from kew.service.attention import needs_a_person

ISSUE = "2516"
PULL = "2565"
BRANCH = f"agent/issue-{ISSUE}"
URL = f"https://example.invalid/pr/{PULL}"
REVIEWED = "a" * 40  # the head ``_round`` records
MOVED = "b" * 40


def _issue_and_its_pr(rounds: tuple[Any, ...] = (), **extra: Any) -> Any:
    """Issue #ISSUE and a run recorded under its PR number #PULL, one branch.

    Both rows carry the same open pull request; only the issue's carries the
    review rounds, as the ledger does.
    """
    return _store(
        nodes=(_node(ISSUE, "adversarial_review", pr_url=URL),),
        syncable=(
            _run(ISSUE, pr_state="open", pr_url=URL, branch=BRANCH),
            _run(PULL, pr_state="open", pr_url=URL, branch=BRANCH),
        ),
        rounds=rounds,
        **extra,
    )


def _journal_head(store: Any, item_id: str, head: str) -> None:
    """The driver's journaled view of the pull request: its current head."""
    store.latest_node_pr_facts = lambda **_kw: [
        NodePrFacts(
            epic_ref=EPIC,
            item_id=item_id,
            observed_at=NOW.isoformat(),
            status="in_review",
            pr_url=URL,
            head_oid=head,
            merge_state=None,
            merge_state_since=None,
            merge_state_at=None,
            pr_pushed_at=None,
        )
    ]


def _epic_admitted(store: Any, item_id: str) -> None:
    """Every run on *item_id* was admitted as a node of EPIC."""
    store.get_admission_identity = lambda run_id: AdmissionIdentity(
        run_id=run_id, item_id=item_id, epic_ref=EPIC, node_id=item_id
    )


def _governed() -> Any:
    config = _config()
    config.review.adversarial = True
    return config


# --------------------------------------------------------------------------- #
# AC1 — one item, one row
# --------------------------------------------------------------------------- #


def test_an_issue_and_its_pull_request_are_one_row_keyed_by_the_issue() -> None:
    store = _issue_and_its_pr(rounds=(_round(ISSUE, "READY", number=1),))

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    rows = [e for e in composed.entries if e.item_id in (ISSUE, PULL)]
    assert [(r.item_id, r.attention_kind, r.epic_ref) for r in rows] == [
        (ISSUE, "merge_decision", EPIC)
    ]
    assert rows[0].reason == f"PR open for #{ISSUE} — awaiting review"
    assert rows[0].url == URL


def test_the_pr_numbered_run_alone_is_keyed_by_the_issue_it_closes() -> None:
    """With no view of the issue's own, the row survives — under the issue."""
    store = _store(syncable=(_run(PULL, pr_state="open", pr_url=URL, branch=BRANCH),))

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [(e.item_id, e.issue_num) for e in composed.entries] == [(ISSUE, int(ISSUE))]
    assert composed.entries[0].reason == f"PR open for #{ISSUE} — awaiting review"


def test_the_pr_numbered_row_does_not_outvote_the_issue_s_own_verdict() -> None:
    """#2565 asked for a merge while #2516's own latest round was NEEDS_FULL."""
    store = _issue_and_its_pr(rounds=(_round(ISSUE, "NEEDS_FULL"),))

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [e for e in composed.entries if e.item_id in (ISSUE, PULL)] == []


@pytest.mark.parametrize("earlier", ["closed", "merged"])
def test_the_issue_s_view_of_another_pr_does_not_hide_this_one(earlier: str) -> None:
    """The issue's own view is about an earlier pull request: it says nothing
    about the open one, which still awaits a merge decision — whether or not
    the earlier one is itself a row (a decline asks a person; it does not
    outrank the live PR on the same branch)."""
    earlier_url = "https://example.invalid/pr/2400"
    store = _store(
        syncable=(
            _run(ISSUE, pr_state=earlier, pr_url=earlier_url, branch=BRANCH),
            _run(PULL, pr_state="open", pr_url=URL, branch=BRANCH),
        ),
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [(e.item_id, e.attention_kind, e.url) for e in composed.entries] == [
        (ISSUE, "merge_decision", URL)
    ]


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("READY", [(ISSUE, "merge_decision", URL)]), ("NEEDS_FULL", [])],
)
def test_an_older_pr_numbered_run_does_not_hide_the_issue_s_live_pr(
    verdict: str, expected: list[tuple[str, str, str]]
) -> None:
    """The PR-numbered run is the STALE one: an earlier pull request on the
    item's branch, closed. The issue's own view carries the live pull request,
    and its verdict is the one served — not a row about the dead one."""
    stale = "https://example.invalid/pr/2400"
    store = _store(
        nodes=(_node(ISSUE, "adversarial_review", pr_url=URL),),
        syncable=(
            _run(ISSUE, pr_state="open", pr_url=URL, branch=BRANCH),
            _run(
                PULL,
                pr_state="closed",
                pr_url=stale,
                branch=BRANCH,
                started_at=datetime(2026, 9, 20, 8, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 9, 20, 9, 0, 0, tzinfo=UTC),
            ),
        ),
        rounds=(_round(ISSUE, verdict, number=1),),
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    rows = [e for e in composed.entries if e.item_id in (ISSUE, PULL)]
    assert [(e.item_id, e.attention_kind, e.url) for e in rows] == expected


def test_a_branch_outside_kew_s_prefix_keeps_its_own_key() -> None:
    store = _store(
        syncable=(_run(PULL, pr_state="open", pr_url=URL, branch="feature/x-2516"),)
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [e.item_id for e in composed.entries] == [PULL]


# --------------------------------------------------------------------------- #
# AC2 — a merge decision only on a verdict for the current head
# --------------------------------------------------------------------------- #


def test_needs_full_is_not_a_merge_decision() -> None:
    store = _issue_and_its_pr(rounds=(_round(ISSUE, "NEEDS_FULL"),))
    _journal_head(store, ISSUE, REVIEWED)

    view = _view(store, ISSUE)

    assert view.exception.state == "review_round_due"
    assert not needs_a_person(view)


def test_a_round_in_progress_is_not_a_merge_decision() -> None:
    store = _store(
        nodes=(_node(ISSUE, "adversarial_review", pr_url=URL),),
        syncable=(_run(ISSUE, pr_state="open", pr_url=URL, branch=BRANCH),),
        runs=(_review_run(ISSUE),),
        rounds=(_round(ISSUE, "READY", number=1),),
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [e for e in composed.entries if e.item_id == ISSUE] == []


def test_ready_on_the_current_head_is_a_merge_decision() -> None:
    store = _issue_and_its_pr(rounds=(_round(ISSUE, "READY", number=1),))
    _journal_head(store, ISSUE, REVIEWED)

    composed = needs_you.needs_you_set(store, _governed(), now=NOW)

    assert [(e.item_id, e.attention_kind) for e in composed.entries] == [
        (ISSUE, "merge_decision")
    ]


@pytest.mark.parametrize("verdict", ["READY", "NEEDS_CHANGES"])
def test_a_verdict_on_a_head_the_pr_has_moved_past_is_kew_s_move(
    verdict: str,
) -> None:
    """READY said nothing about code pushed after it; kew reviews that next."""
    store = _issue_and_its_pr(rounds=(_round(ISSUE, verdict, number=1),))
    _journal_head(store, ISSUE, MOVED)

    view = _view(store, ISSUE)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert view.judgment.state == "between_review_rounds"
    assert view.exception.state == "review_round_due"
    assert "no round has read the current head" in view.safest_action.reason
    assert [e for e in composed.entries if e.item_id == ISSUE] == []


def test_an_unread_head_keeps_the_ready_verdict_a_person_s() -> None:
    """No head in hand is no proof the code moved; quieting on it would hide
    a real merge decision for good."""
    store = _issue_and_its_pr(rounds=(_round(ISSUE, "READY", number=1),))

    assert needs_a_person(_view(store, ISSUE))


def test_a_governed_epic_node_with_no_round_yet_is_kew_s_move() -> None:
    store = _store(
        nodes=(_node(ISSUE, "adversarial_review", pr_url=URL),),
        syncable=(_run(ISSUE, pr_state="open", pr_url=URL, branch=BRANCH),),
    )
    _epic_admitted(store, ISSUE)

    composed = needs_you.needs_you_set(store, _governed(), now=NOW)
    ungoverned = needs_you.needs_you_set(store, _config(), now=NOW)

    assert [e for e in composed.entries if e.item_id == ISSUE] == []
    # Without the review stage no round is ever coming: a person is next.
    assert [e.item_id for e in ungoverned.entries] == [ISSUE]


# --------------------------------------------------------------------------- #
# AC3 — the header counts distinct items a person must act on
# --------------------------------------------------------------------------- #


def test_the_header_counts_each_item_once_and_only_its_merge_decisions() -> None:
    """The 2026-10-09 ledger in miniature: #2499/#2555 READY (one person-move),
    #2516/#2565 NEEDS_FULL (kew's), #2536 mid-review (kew's)."""
    ready_url = "https://example.invalid/pr/2555"
    review_url = "https://example.invalid/pr/2536"
    store = _store(
        nodes=(
            _node(ISSUE, "adversarial_review", pr_url=URL),
            _node("2536", "adversarial_review", pr_url=review_url),
        ),
        syncable=(
            _run("2499", pr_state="open", pr_url=ready_url, branch="agent/issue-2499"),
            _run("2555", pr_state="open", pr_url=ready_url, branch="agent/issue-2499"),
            _run(ISSUE, pr_state="open", pr_url=URL, branch=BRANCH),
            _run(PULL, pr_state="open", pr_url=URL, branch=BRANCH),
            _run("2536", pr_state="open", pr_url=review_url, branch="agent/issue-2536"),
        ),
        runs=(_review_run("2536"),),
        rounds=(
            _round("2499", "READY", number=1),
            _round(ISSUE, "NEEDS_FULL"),
        ),
    )

    payload = dashboard.assemble(store, _config(), now=NOW)

    assert payload.unavailable == []
    assert [(r.item_id, r.attention_kind) for r in payload.rows] == [
        ("2499", "merge_decision")
    ]
    assert payload.needs_you_by_epic == {}


def test_round_fixture_head_is_the_one_journaled_as_current() -> None:
    """Guard on this module's own fixture: the READY-on-current-head test only
    means something if ``_round`` records the head it journals."""
    assert _round(ISSUE, "READY").head_sha == REVIEWED
