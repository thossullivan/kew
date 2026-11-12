"""Task 12 (#980) — human-action races with a LIVE reviewer (F6, F7).

Both races are driven against a live (fake) reviewer run: a real reservation
under the ``review`` pseudo-epic plus a pending run row. The driver's race
handler (`_handle_review_races`) must:

- F6: detect a PR merged out-of-band while the reviewer is live → kill +
  reconcile the reviewer, attest MERGED_UNREVIEWED;
- F7: detect a PR closed/superseded mid-review → kill + reconcile the reviewer,
  HELD review_pr_closed, and NEVER let the node re-derive to TODO (#753 class).

Plus the mandatory `concurrent-state-change` false-positive guards: a reviewer
whose PR is still OPEN survives every pass, and — the Finding 1 hardening — a
reviewer survives a *transient `gh` read failure* on either the issue or the PR
(a read that fails closed must never look like a vanished/closed PR).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kew.config import DispatchConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.graph.driver.tick import _handle_review_races, _review_race_for_node
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)

EPIC = "epic:x"
ITEM = "42"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _facts(*, state: str, pr_number: int | None) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state=state,
        state_reason="COMPLETED" if state == "CLOSED" else None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid="head1",
        pr_pushed_at=None,
    )


def _issue_dict(*, state: str, pr_number: int | None) -> dict[str, Any]:
    """A raw `gh issue view --json` payload (what `fetch_issue_or_none` returns
    on a successful read). ``pr_number=None`` models a *positively read* issue
    with no linked PR — the #753 vanished-reference wedge — NOT a failed read."""
    issue: dict[str, Any] = {"number": int(ITEM), "state": state, "labels": []}
    if pr_number is not None:
        issue["closedByPullRequestsReferences"] = [{"number": pr_number, "url": None}]
    return issue


class _FakeReader:
    """Models the two raw reads the race handler makes, each able to fail closed.

    ``issue=None`` → the ``gh issue view`` read failed (transient hiccup).
    ``pr=None`` → the ``gh pr view`` read failed. Otherwise the dicts are the
    positively-read payloads.
    """

    def __init__(
        self,
        *,
        issue: dict[str, Any] | None,
        pr: dict[str, Any] | None,
        branch_prs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._issue = issue
        self._pr = pr
        self._branch_prs = branch_prs or []
        self._branch_prefix = "agent/issue-"

    def fetch_issue_or_none(self, issue_num: int) -> dict[str, Any] | None:
        return self._issue

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        return self._pr or {}

    def pr_candidates_for_branch(self, branch: str) -> list[dict[str, Any]]:
        return self._branch_prs

    @staticmethod
    def pr_number_from_url(url: str) -> int | None:
        try:
            return int(url.rpartition("/pull/")[2])
        except ValueError:
            return None


def _seed_live_reviewer(
    store: SqliteRunStore, *, run_kind: str = ADVERSARIAL_REVIEW_RUN_KIND
) -> str:
    """A live reviewer: a reservation under the review pseudo-epic + a pending
    run row of the reviewer's run_kind. Returns the run_id."""
    run_id = "reviewer-run-1"
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, run_id)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(ITEM),
            item_id=ITEM,
            branch="epic/42",
            status="pending",
            exit_code=0,
            started_at=NOW,
            finished_at=None,
            run_kind=run_kind,
        )
    )
    return run_id


def _run_races(store: SqliteRunStore, reader: _FakeReader):
    return _handle_review_races(
        [ITEM],
        epic_ref=EPIC,
        provider="github",
        facts=reader,
        store=store,
        config=DispatchConfig(),
        repo="o/r",
        now=NOW,
    )


# --- F6: out-of-band merge -------------------------------------------------


def test_out_of_band_merge_kills_reviewer_and_attests(tmp_path) -> None:
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        # Human merged the PR; the issue auto-closed via `Closes #N`.
        reader = _FakeReader(
            issue=_issue_dict(state="CLOSED", pr_number=99),
            pr={"state": "MERGED", "headRefOid": "head1"},
        )

        outcome = _run_races(store, reader)

        assert outcome.merged_unreviewed == [ITEM]
        # Reviewer killed + reconciled: reservation released, run terminal.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        killed = store.get(run_id)
        assert killed is not None and killed.status == "killed"
        # MERGED_UNREVIEWED attested — the chain never goes silent on a bypass.
        events = store.list_audit_events(item_id=ITEM)
        verdicts = [e for e in events if e.event == "adversarial_review_verdict"]
        assert len(verdicts) == 1
        assert verdicts[0].payload["verdict"] == "MERGED_UNREVIEWED"
        assert verdicts[0].payload["actor"] == "out_of_band"


def test_out_of_band_merge_kills_live_remediator(tmp_path) -> None:
    """Finding 3: the same race with a live *remediator* (review_remediation
    run_kind) — the SIGTERM-during-remediating case. The kill preserves the
    reservation's run_kind, so the terminal row is bucketed as remediation."""
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store, run_kind=REVIEW_REMEDIATION_RUN_KIND)
        reader = _FakeReader(
            issue=_issue_dict(state="CLOSED", pr_number=99),
            pr={"state": "MERGED", "headRefOid": "head1"},
        )

        outcome = _run_races(store, reader)

        assert outcome.merged_unreviewed == [ITEM]
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        killed = store.get(run_id)
        assert killed is not None and killed.status == "killed"
        # run_kind preserved so the F4 status filter still ignores the kill row.
        assert killed.run_kind == REVIEW_REMEDIATION_RUN_KIND


# --- F7: PR closed/superseded mid-review -----------------------------------


def test_pr_closed_mid_review_holds_and_never_reenters_frontier(tmp_path) -> None:
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        # PR positively read CLOSED-unmerged while the reviewer was live.
        reader = _FakeReader(
            issue=_issue_dict(state="OPEN", pr_number=99),
            pr={"state": "CLOSED", "headRefOid": "head1"},
        )

        outcome = _run_races(store, reader)

        assert outcome.held_pr_closed == [ITEM]
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        killed = store.get(run_id)
        assert killed is not None and killed.status == "killed"
        # HELD review_pr_closed — and the overlay routes it to HELD, so the node
        # stays OUT of the dispatch frontier and never derives back to TODO.
        holds = {h.item_id: h.reason for h in store.list_epic_holds(EPIC)}
        assert holds.get(ITEM) == "review_pr_closed"
        ds = overlay_status(
            _facts(state="OPEN", pr_number=None),
            [],
            reserved=False,
            held_reason="review_pr_closed",
            review=ReviewOverlay(enabled=True),
        )
        assert ds is DriverStatus.HELD


def test_vanished_pr_reference_holds(tmp_path) -> None:
    """The #753 wedge: the issue reads back cleanly but its linked-PR reference
    is gone. A *positively read* issue with no PR ref is a confirmed
    supersession → HELD, distinct from a failed read (below)."""
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        reader = _FakeReader(issue=_issue_dict(state="OPEN", pr_number=None), pr=None)

        outcome = _run_races(store, reader)

        assert outcome.held_pr_closed == [ITEM]
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        killed = store.get(run_id)
        assert killed is not None and killed.status == "killed"
        holds = {h.item_id: h.reason for h in store.list_epic_holds(EPIC)}
        assert holds.get(ITEM) == "review_pr_closed"


# --- false-positive guards: a live reviewer survives -----------------------


def test_live_reviewer_with_open_pr_survives(tmp_path) -> None:
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        # PR still OPEN: the reviewer is legitimately in flight.
        reader = _FakeReader(
            issue=_issue_dict(state="OPEN", pr_number=99),
            pr={"state": "OPEN", "headRefOid": "head1"},
        )

        outcome = _run_races(store, reader)

        assert outcome.merged_unreviewed == []
        assert outcome.held_pr_closed == []
        # Reservation + run untouched.
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            run_id
        ]
        live = store.get(run_id)
        assert live is not None and live.status == "pending"
        assert store.list_epic_holds(EPIC) == []


def test_keywordless_branch_pr_keeps_live_reviewer(tmp_path) -> None:
    """A removed closing keyword does not create a vanished-PR race."""
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        reader = _FakeReader(
            issue=_issue_dict(state="OPEN", pr_number=None),
            pr=None,
            branch_prs=[
                {
                    "number": 99,
                    "url": "https://github.com/o/r/pull/99",
                    "state": "OPEN",
                    "headRefName": "agent/issue-42",
                    "headRefOid": "head1",
                }
            ],
        )

        race = _review_race_for_node(
            ITEM,
            ITEM,
            provider="github",
            facts=reader,
            store=store,
            config=DispatchConfig(),
            epic_ref=EPIC,
            repo="o/r",
            now=NOW,
        )

        assert race is None
        assert store.list_epic_holds(EPIC) == []
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            run_id
        ]
        live = store.get(run_id)
        assert live is not None and live.status == "pending"


def test_transient_issue_read_failure_keeps_reviewer(tmp_path) -> None:
    """Finding 1: a failed `gh issue view` (fetch_issue_or_none → None) must NOT
    look like a vanished PR. The reviewer survives; retried next tick."""
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        reader = _FakeReader(issue=None, pr=None)  # issue read failed closed

        outcome = _run_races(store, reader)

        assert outcome.merged_unreviewed == []
        assert outcome.held_pr_closed == []
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            run_id
        ]
        live = store.get(run_id)
        assert live is not None and live.status == "pending"
        assert store.list_epic_holds(EPIC) == []


def test_transient_pr_read_failure_keeps_reviewer(tmp_path) -> None:
    """Finding 1: the issue reads fine (PR linked) but `gh pr view` fails
    (empty pr_facts, no `state`). A failed PR read must not be read as CLOSED —
    the reviewer survives; retried next tick."""
    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        run_id = _seed_live_reviewer(store)
        reader = _FakeReader(
            issue=_issue_dict(state="OPEN", pr_number=99), pr=None
        )  # PR read failed closed

        outcome = _run_races(store, reader)

        assert outcome.merged_unreviewed == []
        assert outcome.held_pr_closed == []
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            run_id
        ]
        live = store.get(run_id)
        assert live is not None and live.status == "pending"
        assert store.list_epic_holds(EPIC) == []
