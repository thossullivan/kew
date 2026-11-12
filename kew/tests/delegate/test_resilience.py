"""Watch-loop resilience to hard tracker errors (N3, #959).

The per-candidate process leg was unhandled: a deleted issue or an exhausted
rate-limit would kill the foreground loop, and — because the cursor never
advanced past the offending event — a restart would die on the same event
forever (a permanent crash loop). These pin the deleted-issue and rate-limited
paths: the loop survives, and a gone item advances the cursor while a transient
error defers with a bounded backoff.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from kew.delegate.claim import (
    DELEGATION_EPIC_REF,
    ClaimOutcome,
    claim_and_dispatch,
    load_stranded_rollbacks,
)
from kew.delegate.errors import ItemGone, TransientTrackerError
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import (
    WatchOutcome,
    make_is_terminally_decided,
    process_candidate,
    reconcile_stranded_rollbacks,
    run_watch_loop,
)
from kew.delegate.watch import Candidate
from kew.run_store import RunRecord, SqliteRunStore

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
LABEL = "kew:ready"
ITEM_ID = "SFD-42"
EPIC_REF = DELEGATION_EPIC_REF


class _Sink:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, item_id: str) -> bool:
        self.dispatched.append(item_id)
        return True


class _GoneTracker:
    def fetch_live(self, item_id: str) -> CandidateIssue:
        raise ItemGone(f"{item_id} not found")

    def transition_in_progress(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError("must not transition a gone item")

    def reset_to_ready(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError

    def comment(self, item_id: str, body: str) -> None:  # pragma: no cover
        raise AssertionError


class _RateLimitedTracker:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_live(self, item_id: str) -> CandidateIssue:
        self.calls += 1
        raise TransientTrackerError("rate limit not clearing")

    def transition_in_progress(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError

    def reset_to_ready(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError

    def comment(self, item_id: str, body: str) -> None:  # pragma: no cover
        raise AssertionError


def _candidate() -> Candidate:
    return Candidate(ITEM_ID, "local")


def test_deleted_issue_records_terminal_decision_and_advances(tmp_path):
    """A gone item is decided terminally so the watcher's cursor advances (N3)."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        decided = make_is_terminally_decided(store, EPIC_REF)
        assert decided(ITEM_ID) is False
        outcome = process_candidate(
            _candidate(),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=_GoneTracker(),
            sink=_Sink(),
            fleet_cap=0,
            dry_run=False,
        )
        assert outcome is WatchOutcome.SKIPPED_ITEM_GONE
        # A terminal decision now exists, so the cursor will advance past it.
        assert decided(ITEM_ID) is True
        rows = store.list_by_item(ITEM_ID)
        assert [r.error_category for r in rows] == ["delegation_item_gone"]
        # No reservation leaked.
        assert store.list_epic_reservations(EPIC_REF) == []


def test_rate_limited_candidate_defers_with_backoff(tmp_path):
    """A transient error backs off and defers — undecided, retried next tick."""
    backoffs: list[int] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        decided = make_is_terminally_decided(store, EPIC_REF)
        outcome = process_candidate(
            _candidate(),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=_RateLimitedTracker(),
            sink=_Sink(),
            fleet_cap=0,
            dry_run=False,
            backoff=lambda: backoffs.append(1),
        )
        assert outcome is WatchOutcome.DEFERRED_TRANSIENT
        assert backoffs == [1]  # bounded backoff fired once
        # Left undecided so a later tick retries it — no terminal row written.
        assert decided(ITEM_ID) is False
        assert store.list_by_item(ITEM_ID) == []


def test_run_watch_loop_survives_a_crashing_candidate():
    """The loop last-resort keeps one bad candidate from killing the watch (N3)."""
    ticks = {"n": 0}
    processed: list[str] = []

    class _Watcher:
        def tick(self):
            ticks["n"] += 1
            if ticks["n"] == 1:
                return [Candidate("boom", "local"), Candidate("ok", "local")]
            raise KeyboardInterrupt  # end the loop after one real tick

    def _process(candidate):
        if candidate.item_id == "boom":
            raise RuntimeError("unmapped tracker explosion")
        processed.append(candidate.item_id)
        return WatchOutcome.DISPATCHED

    with contextlib.suppress(KeyboardInterrupt):
        run_watch_loop(
            watcher=_Watcher(),
            process_one=_process,
            sleep=lambda _s: None,
            fast_interval_seconds=0.0,
        )

    # The crashing candidate did not abort the tick: the next candidate ran.
    assert processed == ["ok"]


class _DelegatedTracker:
    """A tracker whose live fields are delegated-to-kew and actionable."""

    def fetch_live(self, item_id: str) -> CandidateIssue:
        return CandidateIssue(
            assignee_id=MEMBER_ID, labels=(LABEL,), state="Todo", state_id="Todo"
        )

    def transition_in_progress(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError

    def reset_to_ready(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError

    def comment(self, item_id: str, body: str) -> None:  # pragma: no cover
        raise AssertionError


def _save_refusal(store, *, when) -> None:
    store.save(
        RunRecord(
            run_id="refuse-1",
            issue_num=0,
            item_id=ITEM_ID,
            branch=ITEM_ID,
            status="refused",
            exit_code=1,
            started_at=when,
            finished_at=when,
            error_category="budget_refused",
        )
    )


def test_recent_refusal_skips_reclaim_to_damp_churn(tmp_path):
    """A budget refusal within the cooldown skips the re-claim (N1 damping).

    Releasing the reservation on a refusal (correct for N1) removes the damping
    the leak accidentally provided, so a label-triggered item — whose trigger
    label survives the unclaim — is re-surfaced by the poll floor every interval
    and would re-refuse + re-comment indefinitely. Inside the cooldown the
    watcher must skip the re-claim entirely: no claim, no comment, no new row.
    """
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
    claimed: list[str] = []

    def _claim(item_id, **kwargs):  # pragma: no cover - must never run
        claimed.append(item_id)
        return ClaimOutcome.REFUSED

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _save_refusal(store, when=now - timedelta(seconds=10))
        outcome = process_candidate(
            _candidate(),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=_DelegatedTracker(),
            sink=_Sink(),
            fleet_cap=0,
            dry_run=False,
            refusal_cooldown_seconds=300,
            now=lambda: now,
            claim=_claim,
        )
        assert outcome is WatchOutcome.SKIPPED_RECENT_REFUSAL
        assert claimed == []  # never re-claimed -> no comment, no new refusal row
        # Only the original refusal row exists; the tick added nothing.
        assert len(store.list_by_item(ITEM_ID)) == 1


def test_refusal_past_cooldown_retries(tmp_path):
    """Once the cooldown elapses the item retries (a raised budget is picked up)."""
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
    claimed: list[str] = []

    def _claim(item_id, **kwargs):
        claimed.append(item_id)
        return ClaimOutcome.DISPATCHED

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _save_refusal(store, when=now - timedelta(seconds=400))  # past 300s cooldown
        outcome = process_candidate(
            _candidate(),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=_DelegatedTracker(),
            sink=_Sink(),
            fleet_cap=0,
            dry_run=False,
            refusal_cooldown_seconds=300,
            now=lambda: now,
            claim=_claim,
        )
        assert outcome is WatchOutcome.DISPATCHED
        assert claimed == [ITEM_ID]


def test_gone_during_claim_reverify_is_handled(tmp_path):
    """If the claim's live re-verify sees the item vanish, it's decided gone."""
    now = datetime.now(UTC)
    assert now.tzinfo is UTC

    def _boom_claim(item_id, **kwargs):
        raise ItemGone(f"{item_id} vanished mid-claim")

    tracker_live = CandidateIssue(
        assignee_id=MEMBER_ID, labels=(), state="Todo", state_id="Todo"
    )

    class _OkThenNothing:
        def fetch_live(self, item_id: str) -> CandidateIssue:
            return tracker_live

        def transition_in_progress(self, item_id: str) -> None:
            pass

        def reset_to_ready(self, item_id: str) -> None:
            pass

        def comment(self, item_id: str, body: str) -> None:
            pass

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            _candidate(),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=_OkThenNothing(),
            sink=_Sink(),
            fleet_cap=0,
            dry_run=False,
            claim=_boom_claim,
        )
        assert outcome is WatchOutcome.SKIPPED_ITEM_GONE
        assert make_is_terminally_decided(store, EPIC_REF)(ITEM_ID) is True


def _over_budget_toml(tmp_path):
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 10.0\n"
    )
    return toml


def _blow_weekly_budget(store, *, now) -> None:
    store.save(
        RunRecord(
            run_id="prior-spend",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=now,
            estimated_cost_usd=50.0,
        )
    )


class _FlakyRollbackTracker:
    """Delegated + actionable; the first ``reset_to_ready`` fails, the rest pass."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self._fail_next = True

    def fetch_live(self, item_id: str) -> CandidateIssue:
        return CandidateIssue(
            assignee_id=MEMBER_ID, labels=(LABEL,), state="Todo", state_id="Todo"
        )

    def transition_in_progress(self, item_id: str) -> None:
        pass

    def reset_to_ready(self, item_id: str) -> None:
        self.reset_calls += 1
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("Linear 429 rate limited")

    def comment(self, item_id: str, body: str) -> None:
        pass


def test_round19_reconcile_retires_legacy_rollback_without_reset(tmp_path):
    """Retire legacy rollback requests without tracker writes."""
    toml = _over_budget_toml(tmp_path)
    now = datetime.now(UTC)
    tracker, sink = _FlakyRollbackTracker(), _Sink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _blow_weekly_budget(store, now=now)
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
        assert tracker.reset_calls == 0
        import json

        store.set_meta("delegation_stranded_rollbacks", json.dumps([ITEM_ID]))

        # The next reconcile tick retires the legacy reset request.
        still_stranded = reconcile_stranded_rollbacks(store, tracker)
        assert still_stranded == set()
        assert tracker.reset_calls == 0
        assert load_stranded_rollbacks(store) == []


def test_redelegate_with_prior_terminal_row_keeps_live_reservation(tmp_path):
    """Re-delegating an item keeps the new live reservation until the NEW run
    terminates (Task 6 guard).

    Bug: list_by_item returns old terminal rows from a prior delegation, so the
    reconciler would see 'terminal' and release the new live reservation. Fix:
    keyed on run_id (primary) with item_id as fallback, filtered to runs that
    started at or after the new reservation — old terminal rows are excluded.
    Must FAIL if the started_at filter or run_id keying is removed.
    """
    from kew.config import DispatchConfig
    from kew.graph.driver.tick import reconcile_epic_reservations

    OLD_RUN_ID = "run-old"
    NEW_RUN_ID = "run-new"
    config = DispatchConfig()

    # Use a fixed base timestamp so all comparisons are deterministic. The new
    # reservation's reserved_at is set via a DB update (mirrors the pattern in
    # test_driver_tick.py) so the filter ``started_at >= reserved_at`` is
    # controlled rather than subject to wall-clock race conditions.
    base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    old_start = base - timedelta(hours=1)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # First delegation: reserve, run terminates, reservation released.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, OLD_RUN_ID) is True
        store.save(
            RunRecord(
                run_id=OLD_RUN_ID,
                issue_num=0,
                item_id=ITEM_ID,
                branch=ITEM_ID,
                status="success",
                exit_code=0,
                started_at=old_start,
                finished_at=old_start + timedelta(minutes=30),
            )
        )
        store.release_epic_node_item(ITEM_ID)

        # Re-delegation: new reservation, reserved_at pinned to base.
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, NEW_RUN_ID) is True
        store._conn.execute(
            "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
            (base.isoformat(), ITEM_ID),
        )
        store._conn.commit()

        # Reconciler must NOT release the live reservation — the old terminal
        # row started 1h before the new reservation and must be excluded.
        still_reserved = reconcile_epic_reservations(EPIC_REF, store, config, now=base)
        assert ITEM_ID in still_reserved, (
            "live reservation wrongly released by old terminal row"
        )

        # New run starts (pending) at the same time as the reservation.
        store.save(
            RunRecord(
                run_id="child-uuid",
                issue_num=0,
                item_id=ITEM_ID,
                branch=ITEM_ID,
                status="pending",
                exit_code=-1,
                started_at=base,
            )
        )
        still_reserved = reconcile_epic_reservations(EPIC_REF, store, config, now=base)
        assert ITEM_ID in still_reserved, "pending new run reservation wrongly released"

        # New run terminates — reservation must now be released.
        store.save(
            RunRecord(
                run_id="child-uuid",
                issue_num=0,
                item_id=ITEM_ID,
                branch=ITEM_ID,
                status="success",
                exit_code=0,
                started_at=base,
                finished_at=base + timedelta(minutes=5),
            )
        )
        still_reserved = reconcile_epic_reservations(EPIC_REF, store, config, now=base)
        assert ITEM_ID not in still_reserved, (
            "terminal new run did not release reservation"
        )


def test_generation_conditional_release_spares_a_relive_reservation(tmp_path):
    """A reconciler releasing by a stale generation must not delete a live
    re-claimed reservation (Task 6 / #972 review Finding 2 TOCTOU).

    The driver-tick sweep now runs concurrently with the watch loop and other
    drivers. Interleave: the reconciler reads generation run-A as terminal;
    before it releases, a concurrent watcher releases run-A and re-reserves a
    live run-B. A plain item_id delete would delete run-B (a live delegation
    left with no reservation → fleet under-count + lost double-dispatch guard).
    Generation-conditional release deletes only the run-A it verified.
    """
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-A") is True
        # Concurrent watcher: releases run-A, re-reserves a live run-B.
        store.release_epic_node_item(ITEM_ID)
        assert store.reserve_epic_node_item(EPIC_REF, ITEM_ID, "run-B") is True
        # The stale reconciler now releases keyed to the generation it verified.
        store.release_epic_node_item(ITEM_ID, run_id="run-A")
        # run-B survives — the live delegation keeps its reservation.
        assert [r.run_id for r in store.list_epic_reservations(EPIC_REF)] == ["run-B"]
        # A correctly-keyed release still clears run-B.
        store.release_epic_node_item(ITEM_ID, run_id="run-B")
        assert store.list_epic_reservations(EPIC_REF) == []


def test_reconcile_skips_a_relive_delegated_item(tmp_path):
    """A stranded item that was re-delegated (fresh pending row) is left alone."""
    toml = _over_budget_toml(tmp_path)
    now = datetime.now(UTC)
    tracker, sink = _FlakyRollbackTracker(), _Sink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _blow_weekly_budget(store, now=now)
        claim_and_dispatch(
            ITEM_ID,
            member_id=MEMBER_ID,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=toml,
            epic_ref=EPIC_REF,
        )
        import json

        store.set_meta("delegation_stranded_rollbacks", json.dumps([ITEM_ID]))
        # A live re-delegation writes a fresh pending run row (newest = pending).
        store.save(
            RunRecord(
                run_id="relive",
                issue_num=0,
                item_id=ITEM_ID,
                branch=ITEM_ID,
                status="pending",
                exit_code=0,
                started_at=now + timedelta(seconds=60),
            )
        )
        before = tracker.reset_calls
        still_stranded = reconcile_stranded_rollbacks(store, tracker)
        # Never unclaim a live re-delegation out from under it: no reset, marker
        # cleared so it stops being tracked as stranded.
        assert still_stranded == set()
        assert tracker.reset_calls == before
        assert load_stranded_rollbacks(store) == []
