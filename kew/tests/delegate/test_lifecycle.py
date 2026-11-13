"""Delegation reservation lifecycle tests (N1, #959, CRITICAL).

The delegation seam parks reservations under the ``DELEGATION_EPIC_REF``
pseudo-epic. No epic driver ever ticks that pseudo-epic, so unless every
terminal outcome releases the reservation the fleet slot leaks permanently and
``live_run_count()`` never returns to baseline — after ``max_workers`` delegated
completions every dispatch path in the fleet deadlocks.

These pin: (1) a REFUSED claim releases its reservation and leaves the item
re-delegatable, and (2) the watch-loop reconcile pass releases a reservation
whose delegated run has terminated, returning ``live_run_count()`` to baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kew.config import DispatchConfig
from kew.delegate.claim import DELEGATION_EPIC_REF, ClaimOutcome, claim_and_dispatch
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import reconcile_delegation_reservations
from kew.run_store import RunRecord, SqliteRunStore

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
ITEM_ID = "SFD-42"
EPIC_REF = DELEGATION_EPIC_REF


class _FakeTracker:
    def __init__(self, *, assignee_id=MEMBER_ID, state="Todo", labels=()):
        self._issue = CandidateIssue(
            assignee_id=assignee_id, labels=tuple(labels), state=state, state_id=state
        )
        self.comments: list[tuple[str, str]] = []
        self.transitioned: list[str] = []

    def fetch_live(self, item_id: str) -> CandidateIssue:
        return self._issue

    def transition_in_progress(self, item_id: str) -> None:
        self.transitioned.append(item_id)

    def comment(self, item_id: str, body: str) -> None:
        self.comments.append((item_id, body))

    def reset_to_ready(self, item_id: str) -> None:
        pass


class _FakeSink:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, item_id: str) -> bool:
        self.dispatched.append(item_id)
        return True


def _tight_weekly_toml(tmp_path):
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    return toml


def _spend(store, usd, *, run_id, when):
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=when,
            estimated_cost_usd=usd,
        )
    )


def test_refused_claim_releases_reservation_and_returns_slot(tmp_path):
    """A budget REFUSED claim must release its reservation (N1).

    Otherwise the pseudo-epic slot leaks: ``live_run_count()`` stays pinned and
    the item can only ever re-delegate as SKIPPED_LOST_CLAIM.
    """
    toml = _tight_weekly_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        baseline = store.live_run_count()
        _spend(store, 50.0, run_id="prior", when=datetime.now(UTC))
        outcome = claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        assert outcome is ClaimOutcome.REFUSED
        # The reservation is gone: no leaked slot.
        assert store.list_epic_reservations(EPIC_REF) == []
        assert store.live_run_count() == baseline
        # The item is re-delegatable — a fresh reservation takes, not lost-claim.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-retry") is True


def test_reconcile_releases_terminated_delegated_run(tmp_path):
    """The reconcile pass releases a reservation whose run terminated (N1).

    A DISPATCHED delegated run holds its reservation until its child run row
    reaches a terminal status; the pseudo-epic is never driver-reconciled, so
    the watch loop's reconcile pass must return the slot to baseline.
    """
    config = DispatchConfig()
    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        baseline = store.live_run_count()
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-A") is True
        # While the child run is pending the slot is legitimately held.
        assert store.live_run_count() == baseline + 1
        assert reconcile_delegation_reservations(store, config, now=now) == {ITEM_ID}
        assert store.live_run_count() == baseline + 1

        # The delegated child terminates: a terminal run row lands for the item.
        store.save(
            RunRecord(
                run_id="run-A",
                issue_num=0,
                item_id=ITEM_ID,
                branch="agent/sfd-42",
                status="success",
                exit_code=0,
                started_at=now,
                finished_at=now,
            )
        )
        assert reconcile_delegation_reservations(store, config, now=now) == set()
        assert store.list_epic_reservations(EPIC_REF) == []
        assert store.live_run_count() == baseline


def test_reconcile_reclaims_dead_child_past_grace(tmp_path):
    """A reservation whose child never wrote a run row is reclaimed past grace."""
    config = DispatchConfig()
    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-dead") is True
        old = (now - timedelta(minutes=20)).isoformat()
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            (old, ITEM_ID),
        )
        store._conn.commit()
        assert reconcile_delegation_reservations(store, config, now=now) == set()
        assert store.list_epic_reservations(EPIC_REF) == []
