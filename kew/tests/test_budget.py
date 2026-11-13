"""Tests for budget types and check_budget() guard."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from kew.budget import (
    BudgetExceeded,
    BudgetStatus,
    check_budget,
    check_pr_subject_budget,
    check_review_budget,
)
from kew.config import BudgetConfig
from kew.run_store import RunRecord, SqliteRunStore


@pytest.fixture
def store():
    """Yield an in-memory SqliteRunStore and close it after the test."""
    with SqliteRunStore(":memory:") as s:
        yield s


# ── Task 1: BudgetExceeded & BudgetStatus ──────────────────────────────


class TestBudgetStatus:
    """BudgetStatus dataclass and is_within_budget property."""

    def test_all_within_budget(self):
        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=20.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is True

    def test_issue_over_budget(self):
        status = BudgetStatus(
            issue_spent_usd=16.0,
            issue_limit_usd=15.0,
            daily_spent_usd=20.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is False

    def test_daily_over_budget(self):
        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=55.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is False

    def test_weekly_over_budget(self):
        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=20.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=210.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is False

    def test_exactly_at_limit_is_within(self):
        status = BudgetStatus(
            issue_spent_usd=15.0,
            issue_limit_usd=15.0,
            daily_spent_usd=50.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=200.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is True

    def test_zero_spend_is_within(self):
        status = BudgetStatus(
            issue_spent_usd=0.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        assert status.is_within_budget is True


class TestBudgetExceeded:
    """BudgetExceeded exception carries context."""

    def test_attributes(self):
        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=20.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=20.0,
            weekly_limit_usd=200.0,
        )
        exc = BudgetExceeded(
            which_limit="per_issue",
            limit_usd=15.0,
            spent_usd=20.0,
            status=status,
        )
        assert exc.which_limit == "per_issue"
        assert exc.limit_usd == 15.0
        assert exc.spent_usd == 20.0
        assert exc.status is status

    def test_is_exception(self):
        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        exc = BudgetExceeded(
            which_limit="per_issue",
            limit_usd=15.0,
            spent_usd=20.0,
            status=status,
        )
        assert isinstance(exc, Exception)

    def test_message_contains_limit_info(self):
        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        exc = BudgetExceeded(
            which_limit="per_issue",
            limit_usd=15.0,
            spent_usd=20.0,
            status=status,
        )
        msg = str(exc)
        assert "per_issue" in msg
        assert "15.0" in msg or "15.00" in msg

    def test_can_be_raised_and_caught(self):
        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        with pytest.raises(BudgetExceeded) as exc_info:
            raise BudgetExceeded(
                which_limit="per_issue",
                limit_usd=15.0,
                spent_usd=20.0,
                status=status,
            )
        assert exc_info.value.which_limit == "per_issue"


# ── Task 2: check_budget() ─────────────────────────────────────────────


def _make_record(
    run_id: str,
    issue_num: int,
    cost: float,
    started_at: datetime,
    status: str = "success",
    item_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status=status,
        exit_code=0,
        started_at=started_at,
        estimated_cost_usd=cost,
        item_id=item_id,
    )


def _make_item_record(
    run_id: str,
    item_id: str,
    cost: float,
    started_at: datetime,
    status: str = "success",
) -> RunRecord:
    """A non-GitHub (Linear) run: ``issue_num`` rides the ``0`` sentinel while
    the durable ``item_id`` carries the real key."""
    return RunRecord(
        run_id=run_id,
        issue_num=0,
        branch=f"agent/issue-{item_id.lower()}",
        status=status,
        exit_code=0,
        started_at=started_at,
        estimated_cost_usd=cost,
        item_id=item_id,
    )


class TestCheckBudgetNoRuns:
    """check_budget with empty store returns all-zero status."""

    def test_empty_store_within_budget(self, store):
        budget = BudgetConfig()
        status = check_budget(store, budget, issue_num=1)
        assert status.is_within_budget is True
        assert status.issue_spent_usd == pytest.approx(0.0)
        assert status.daily_spent_usd == pytest.approx(0.0)
        assert status.weekly_spent_usd == pytest.approx(0.0)


class TestCheckBudgetIssueLimit:
    """check_budget raises when per-issue limit is exceeded."""

    def test_under_issue_limit(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 5.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(store, budget, issue_num=42, now=now)
        assert status.is_within_budget is True
        assert status.issue_spent_usd == pytest.approx(5.0)

    def test_over_issue_limit_raises(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 10.0, now))
        store.save(_make_record("r2", 42, 8.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=42, now=now)
        assert exc_info.value.which_limit == "per_issue"
        assert exc_info.value.spent_usd == pytest.approx(18.0)
        assert exc_info.value.limit_usd == pytest.approx(15.0)

    def test_different_issue_not_counted(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 99, 20.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(store, budget, issue_num=42, now=now)
        assert status.issue_spent_usd == pytest.approx(0.0)


class TestCheckBudgetPerItem:
    """Per-item caps key on the durable ``item_id`` (#809): non-GitHub items
    all ride ``issue_num=0`` but must NOT share the ``0`` sentinel bucket."""

    def test_non_github_items_do_not_share_a_bucket(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Two distinct Linear items, each $10, both on the ``0`` sentinel.
        store.save(_make_item_record("r1", "SFD-1", 10.0, now))
        store.save(_make_item_record("r2", "SFD-2", 10.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        # Keyed on item_id, SFD-1 alone is $10 < $15 — must NOT trip even though
        # the combined ``0``-bucket spend ($20) exceeds the cap.
        status = check_budget(store, budget, issue_num=0, item_id="SFD-1", now=now)
        assert status.is_within_budget is True
        assert status.issue_spent_usd == pytest.approx(10.0)

    def test_per_item_over_cap_raises(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_item_record("r1", "SFD-1", 10.0, now))
        store.save(_make_item_record("r2", "SFD-1", 8.0, now))
        store.save(_make_item_record("r3", "SFD-2", 20.0, now))  # noise
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=0, item_id="SFD-1", now=now)
        assert exc_info.value.which_limit == "per_issue"
        assert exc_info.value.spent_usd == pytest.approx(18.0)

    def test_github_item_id_matches_issue_keying(self, store):
        # A GitHub row backfills ``item_id = str(issue_num)``; passing that id
        # yields the same per-item spend as the classic issue-keyed scan.
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 5.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(store, budget, issue_num=42, item_id="42", now=now)
        assert status.issue_spent_usd == pytest.approx(5.0)

    def test_pending_item_rows_valued_at_reservation(self, store):
        # Two in-flight pending rows on ONE Linear item trip the per-item
        # reservation gate — pending-row valuation survives item keying.
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_item_record("r1", "SFD-1", 0.0, now, status="pending"))
        store.save(_make_item_record("r2", "SFD-1", 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(
                store,
                budget,
                issue_num=0,
                item_id="SFD-1",
                now=now,
                pending_reservation_usd=10.0,
            )
        assert exc_info.value.which_limit == "per_issue"


class TestCheckBudgetDailyLimit:
    """check_budget raises when daily limit is exceeded."""

    def test_over_daily_limit_raises(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Multiple issues, same day
        store.save(_make_record("r1", 1, 30.0, now))
        store.save(_make_record("r2", 2, 25.0, now))
        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=50.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=3, now=now)
        assert exc_info.value.which_limit == "daily"
        assert exc_info.value.spent_usd == pytest.approx(55.0)

    def test_yesterday_not_counted_in_daily(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        yesterday = now - timedelta(days=1)
        store.save(_make_record("r1", 1, 45.0, yesterday))
        store.save(_make_record("r2", 2, 5.0, now))
        budget = BudgetConfig(daily_usd=50.0)
        status = check_budget(store, budget, issue_num=3, now=now)
        assert status.daily_spent_usd == pytest.approx(5.0)
        assert status.is_within_budget is True


class TestCheckBudgetWeeklyLimit:
    """check_budget raises when weekly limit is exceeded."""

    def test_over_weekly_limit_raises(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Spread costs across the week
        for i in range(5):
            day = now - timedelta(days=i)
            store.save(_make_record(f"r{i}", i + 1, 45.0, day))
        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=500.0, weekly_usd=200.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=99, now=now)
        assert exc_info.value.which_limit == "weekly"
        assert exc_info.value.spent_usd == pytest.approx(225.0)

    def test_older_than_7_days_not_counted(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        old = now - timedelta(days=8)
        store.save(_make_record("r1", 1, 190.0, old))
        store.save(_make_record("r2", 2, 5.0, now))
        budget = BudgetConfig(weekly_usd=200.0)
        status = check_budget(store, budget, issue_num=3, now=now)
        assert status.weekly_spent_usd == pytest.approx(5.0)
        assert status.is_within_budget is True


class TestCheckBudgetPriority:
    """When multiple limits exceeded, first checked wins."""

    def test_issue_checked_before_daily(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Both issue and daily over budget
        store.save(_make_record("r1", 42, 20.0, now))
        store.save(_make_record("r2", 42, 40.0, now))
        budget = BudgetConfig(per_issue_usd=15.0, daily_usd=50.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=42, now=now)
        assert exc_info.value.which_limit == "per_issue"


class TestCheckBudgetPendingReservation:
    """check_budget values in-flight ``pending`` rows at a worst-case reservation.

    A pending row carries a $0 estimate until its real cost lands (H2). Passed
    the runtime kill cap via ``pending_reservation_usd``, check_budget values
    each pending row at its worst case so N parallel admits cannot jointly
    overshoot a nearly-exhausted cap. A *lone* in-flight run is still allowed to
    overshoot (that is the warned-but-allowed CS-7 posture); only genuine
    concurrency (>= 2 in-flight) trips the reservation gate. Left at the default
    0.0 the pending row keeps its stored estimate (the cheap read-only preflight).
    """

    def test_pending_row_ignored_without_reservation(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("done", 42, 10.0, now))
        # A pending row for the same issue carries $0 until its cost lands.
        store.save(_make_record("live", 42, 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=15.0)
        # Default reservation 0.0: only the $10 committed run counts → within.
        status = check_budget(store, budget, issue_num=42, now=now)
        assert status.issue_spent_usd == pytest.approx(10.0)
        assert status.is_within_budget is True

    def test_lone_pending_run_allowed_to_overshoot(self, store):
        """A single in-flight run whose worst case exceeds the cap is NOT blocked.

        Mirrors the default config trap (CS-7): a $25 kill cap under a $15
        per-issue cap must still dispatch the first run — the runtime cap bounds
        it — rather than refuse every dispatch.
        """
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("live", 42, 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(
            store, budget, issue_num=42, now=now, pending_reservation_usd=25.0
        )
        # Worst case is reported, but a lone run does not trip the gate.
        assert status.issue_spent_usd == pytest.approx(25.0)

    def test_two_pending_runs_trip_issue_reservation(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("done", 42, 5.0, now))
        store.save(_make_record("live1", 42, 0.0, now, status="pending"))
        store.save(_make_record("live2", 42, 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=15.0)
        # 5 committed + 6 + 6 worst-case = 17 > 15, and two runs are in flight.
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(
                store, budget, issue_num=42, now=now, pending_reservation_usd=6.0
            )
        assert exc_info.value.which_limit == "per_issue"
        assert exc_info.value.spent_usd == pytest.approx(17.0)

    def test_two_pending_runs_trip_daily_reservation(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("done", 1, 35.0, now))
        store.save(_make_record("live1", 2, 0.0, now, status="pending"))
        store.save(_make_record("live2", 3, 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=50.0)
        # 35 + 10 + 10 = 55 > 50 daily, across two in-flight runs.
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(
                store, budget, issue_num=4, now=now, pending_reservation_usd=10.0
            )
        assert exc_info.value.which_limit == "daily"
        assert exc_info.value.spent_usd == pytest.approx(55.0)

    def test_two_pending_runs_trip_weekly_reservation(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        for i in range(4):
            day = now - timedelta(days=i)
            store.save(_make_record(f"r{i}", i + 1, 45.0, day))
        store.save(_make_record("live1", 98, 0.0, now, status="pending"))
        store.save(_make_record("live2", 99, 0.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=500.0, weekly_usd=200.0)
        # 180 committed + 10 + 10 = 200 → not over; bump reservation to trip it.
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(
                store, budget, issue_num=99, now=now, pending_reservation_usd=11.0
            )
        assert exc_info.value.which_limit == "weekly"
        assert exc_info.value.spent_usd == pytest.approx(202.0)

    def test_recorded_overspend_trips_regardless_of_pending_count(self, store):
        """Committed spend over the cap always raises — the >=2 carve-out only
        forgives worst-case reservation overshoot, never real recorded spend."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 16.0, now))  # committed, over cap
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded):
            check_budget(
                store, budget, issue_num=42, now=now, pending_reservation_usd=25.0
            )

    def test_reservation_never_lowers_a_recorded_estimate(self, store):
        """A tiny reservation cannot value a pending row *below* its real cost."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Two pending rows that already recorded a partial cost each.
        store.save(_make_record("live1", 42, 10.0, now, status="pending"))
        store.save(_make_record("live2", 42, 10.0, now, status="pending"))
        budget = BudgetConfig(per_issue_usd=15.0)
        # Reservation of 1.0 is below the recorded 10.0 → worst case stays 20.0.
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(
                store, budget, issue_num=42, now=now, pending_reservation_usd=1.0
            )
        assert exc_info.value.spent_usd == pytest.approx(20.0)

    def test_negative_reservation_clamped_to_zero(self, store):
        """A negative reservation cannot reduce totals and bypass guards."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 16.0, now))
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded):
            check_budget(
                store, budget, issue_num=42, now=now, pending_reservation_usd=-5.0
            )


class TestConcurrentBudgetAdmit:
    """Prove the parallel-overshoot race is CLOSED when the spend check is
    folded into ``try_admit_run``'s ``BEGIN IMMEDIATE`` transaction (H2).

    The old ``check_budget``-then-dispatch path was a read-only snapshot with a
    TOCTOU window: two workers could both read the same committed spend, both
    see headroom, and both admit — jointly blowing a nearly-exhausted cap. This
    test drives the real admit path (each thread its own connection, the
    production pattern) against a cap sized to fit exactly one worst-case
    reservation and asserts at most one admit survives.
    """

    @pytest.mark.parametrize("n_workers", [3, 5])
    def test_concurrent_admits_never_exceed_cap(self, tmp_path, n_workers):
        """N (>= 3) racing admits against a near-exhausted cap → exactly one wins.

        The proving test for issue #933: launch ``n_workers`` concurrent
        admissions — each on its own connection, the production per-worker
        pattern — against a daily cap sized to fit exactly ONE worst-case
        reservation on top of committed spend. Because the spend check is folded
        into ``try_admit_run``'s ``BEGIN IMMEDIATE`` transaction, the contenders
        serialize: the first inserts its pending row and admits, and every later
        contender sees >= 2 in-flight reservations and is refused. The aggregate
        worst-case spend (committed + persisted reservations) must never exceed
        the cap no matter how many workers race.
        """
        db_path = str(tmp_path / "concurrent.db")
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Committed spend leaves room for ONE worst-case reservation, not two:
        # 35 + 10 = 45 <= 50, but 35 + 10 + 10 = 55 > 50.
        committed = 35.0
        reservation = 10.0
        daily_cap = 50.0
        setup = SqliteRunStore(db_path)
        setup.save(_make_record("committed", 1, committed, now))
        setup.close()

        budget = BudgetConfig(
            per_issue_usd=100.0, daily_usd=daily_cap, weekly_usd=500.0
        )
        results: list[bool | Exception] = []
        barrier = threading.Barrier(n_workers)

        def worker(issue_num: int) -> None:
            store = SqliteRunStore(db_path)
            record = RunRecord(
                run_id=f"r-{issue_num}",
                issue_num=issue_num,
                item_id=str(issue_num),
                branch=f"agent/issue-{issue_num}",
                status="pending",
                exit_code=-1,
                started_at=now,
                estimated_cost_usd=0.0,
            )

            def _reserve() -> None:
                check_budget(
                    store,
                    budget,
                    issue_num=issue_num,
                    now=now,
                    pending_reservation_usd=reservation,
                )

            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                results.append(RuntimeError("Barrier timeout"))
                return
            try:
                admitted = store.try_admit_run(record, cap=0, on_admit=_reserve)
                results.append(admitted)
            except BudgetExceeded as exc:
                results.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=worker, args=(10 + i,)) for i in range(n_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for i, t in enumerate(threads):
            assert not t.is_alive(), f"worker {i} still alive after timeout"

        assert len(results) == n_workers
        admits = [r for r in results if r is True]
        refusals = [r for r in results if isinstance(r, BudgetExceeded)]
        # Exactly one admit — the race is closed and the cap fits a single
        # worst-case reservation; every other contender is refused.
        assert len(admits) == 1, f"expected exactly one admit, got {results}"
        assert len(refusals) == n_workers - 1, (
            f"expected {n_workers - 1} budget refusals, got {results}"
        )
        assert all(r.which_limit == "daily" for r in refusals)

        # Only the winner's pending row persisted, so the aggregate worst-case
        # spend stays under the cap regardless of how many workers raced.
        verify = SqliteRunStore(db_path)
        pending = [r for r in verify.list_since(now) if r.status == "pending"]
        verify.close()
        assert len(pending) == 1
        assert committed + len(pending) * reservation <= daily_cap

    def test_concurrent_check_raises_when_over_limit(self, tmp_path):
        """Both threads see over-budget state when spend already exceeds the limit."""
        db_path = str(tmp_path / "concurrent2.db")
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Pre-load spend well over the daily limit
        setup = SqliteRunStore(db_path)
        setup.save(_make_record("r1", 1, 60.0, now))
        setup.close()

        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=50.0, weekly_usd=500.0)
        results: list[BudgetStatus | Exception] = []
        barrier = threading.Barrier(2)

        def worker(issue_num: int) -> None:
            store = SqliteRunStore(db_path)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                results.append(RuntimeError("Barrier timeout"))
                return
            try:
                status = check_budget(store, budget, issue_num=issue_num, now=now)
                results.append(status)
            except BudgetExceeded as exc:
                results.append(exc)
            finally:
                store.close()

        t1 = threading.Thread(target=worker, args=(20,))
        t2 = threading.Thread(target=worker, args=(21,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive(), "Thread t1 still alive after timeout"
        assert not t2.is_alive(), "Thread t2 still alive after timeout"

        assert len(results) == 2
        for result in results:
            assert isinstance(result, BudgetExceeded)
            assert result.which_limit == "daily"


class TestCheckBudgetUsesListSince:
    """check_budget should use list_since (not list_all) for date-bounded queries."""

    def test_check_budget_uses_list_since_not_list_all(self, store):
        """check_budget should call list_since for date filtering, not list_all."""
        from unittest.mock import patch

        budget = BudgetConfig(per_issue_usd=100.0, daily_usd=100.0, weekly_usd=100.0)
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=6)

        with (
            patch.object(store, "list_since", wraps=store.list_since) as mock_since,
            patch.object(store, "list_all") as mock_all,
        ):
            check_budget(store, budget, issue_num=1, now=now)
            mock_since.assert_called_once_with(week_start)
            assert not mock_all.called, "list_all should not be called (inefficient)"


# ── #649 Task 1: effective_budget_view + banner render + warnings ────────


from kew.budget import (  # noqa: E402
    BudgetView,
    effective_budget_view,
    per_issue_overspend_note,
    render_budget_line,
    runtime_cap_conflict_warning,
)
from kew.config import DispatchConfig  # noqa: E402


class TestEffectiveBudgetView:
    """budget.effective_budget_view returns cap + provenance + spend math."""

    def test_default_provenance_and_zero_spend(self, store):
        config = DispatchConfig()  # nothing configured -> all defaults
        view = effective_budget_view(config, store, issue=42)
        assert view.runtime_cap_usd == pytest.approx(25.0)
        assert view.runtime_cap_source == "default"
        assert view.per_issue_cap_usd == pytest.approx(15.0)
        assert view.issue_spent_usd == pytest.approx(0.0)
        assert view.daily_cap_usd == pytest.approx(50.0)
        assert view.daily_spent_usd == pytest.approx(0.0)
        assert view.issue_num == 42

    def test_kew_toml_provenance_when_cap_configured(self, store):
        config = DispatchConfig(agent={"max_budget_usd": 30.0})
        view = effective_budget_view(config, store, issue=1)
        assert view.runtime_cap_usd == pytest.approx(30.0)
        assert view.runtime_cap_source == "kew.toml"

    def test_env_override_provenance(self, store, monkeypatch):
        monkeypatch.setenv("KEW_MAX_BUDGET", "12.5")
        config = DispatchConfig(agent={"max_budget_usd": 12.5})
        view = effective_budget_view(config, store, issue=1)
        assert view.runtime_cap_source == "override"

    def test_removed_dispatch_max_budget_is_not_an_override(self, store, monkeypatch):
        """W4.3 row 18: DISPATCH_MAX_BUDGET is not read, so it is not provenance."""
        monkeypatch.delenv("KEW_MAX_BUDGET", raising=False)
        monkeypatch.setenv("DISPATCH_MAX_BUDGET", "12.5")
        view = effective_budget_view(DispatchConfig(), store, issue=1)
        assert view.runtime_cap_source == "default"

    def test_issue_and_daily_spend_summed(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 42, 0.30, now))
        store.save(_make_record("r2", 42, 0.11, now))
        store.save(_make_record("r3", 99, 5.00, now))  # other issue, same day
        config = DispatchConfig(budget={"per_issue_usd": 0.40, "daily_usd": 100.0})
        view = effective_budget_view(config, store, issue=42, now=now)
        assert view.issue_spent_usd == pytest.approx(0.41)
        # daily aggregates across all issues today
        assert view.daily_spent_usd == pytest.approx(5.41)

    def test_daily_excludes_prior_days(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        yesterday = now - timedelta(days=1)
        store.save(_make_record("r1", 42, 9.00, yesterday))
        store.save(_make_record("r2", 42, 1.00, now))
        config = DispatchConfig()
        view = effective_budget_view(config, store, issue=42, now=now)
        assert view.daily_spent_usd == pytest.approx(1.00)

    def test_store_none_leaves_spend_unknown(self):
        config = DispatchConfig()
        view = effective_budget_view(config, None, issue=42)
        assert view.issue_spent_usd is None
        assert view.daily_spent_usd is None

    def test_issue_none_leaves_issue_spend_unknown_but_daily_known(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r1", 7, 2.00, now))
        config = DispatchConfig()
        view = effective_budget_view(config, store, issue=None, now=now)
        assert view.issue_spent_usd is None
        assert view.daily_spent_usd == pytest.approx(2.00)


class TestBudgetViewProperties:
    """Derived headroom + conflict predicate on BudgetView."""

    def _view(self, **kw) -> BudgetView:
        base = dict(
            runtime_cap_usd=25.0,
            runtime_cap_source="default",
            per_issue_cap_usd=0.40,
            issue_spent_usd=0.0,
            daily_cap_usd=100.0,
            daily_spent_usd=12.30,
            issue_num=42,
        )
        base.update(kw)
        return BudgetView(**base)

    def test_per_issue_remaining(self):
        assert self._view(
            issue_spent_usd=0.10
        ).per_issue_remaining_usd == pytest.approx(0.30)

    def test_per_issue_remaining_none_when_spend_unknown(self):
        assert self._view(issue_spent_usd=None).per_issue_remaining_usd is None

    def test_daily_remaining(self):
        assert self._view().daily_remaining_usd == pytest.approx(87.70)

    def test_runtime_cap_exceeds_headroom_true(self):
        assert (
            self._view(issue_spent_usd=0.0).runtime_cap_exceeds_issue_headroom is True
        )

    def test_runtime_cap_within_headroom_false(self):
        v = self._view(runtime_cap_usd=0.20, issue_spent_usd=0.0)
        assert v.runtime_cap_exceeds_issue_headroom is False

    def test_headroom_uses_full_cap_when_spend_unknown(self):
        v = self._view(
            runtime_cap_usd=25.0, per_issue_cap_usd=0.40, issue_spent_usd=None
        )
        assert v.runtime_cap_exceeds_issue_headroom is True


class TestRenderBudgetLine:
    """render_budget_line produces the shared compact banner block."""

    def test_full_line_matches_spec_example(self):
        view = BudgetView(
            runtime_cap_usd=25.0,
            runtime_cap_source="default",
            per_issue_cap_usd=0.40,
            issue_spent_usd=0.0,
            daily_cap_usd=100.0,
            daily_spent_usd=12.30,
            issue_num=42,
        )
        assert render_budget_line(view) == (
            "Budget: run cap $25 (default) · issue $0.00/$0.40 · today $12.30/$100"
        )

    def test_unknown_spend_renders_dash(self):
        view = BudgetView(
            runtime_cap_usd=25.0,
            runtime_cap_source="kew.toml",
            per_issue_cap_usd=15.0,
            issue_spent_usd=None,
            daily_cap_usd=50.0,
            daily_spent_usd=None,
            issue_num=None,
        )
        line = render_budget_line(view)
        assert "run cap $25 (kew.toml)" in line
        assert "issue —/$15" in line
        assert "today —/$50" in line


class TestRuntimeCapConflictWarning:
    """runtime_cap_conflict_warning fires exactly when cap > per-issue headroom."""

    def _view(self, **kw) -> BudgetView:
        base = dict(
            runtime_cap_usd=25.0,
            runtime_cap_source="default",
            per_issue_cap_usd=0.40,
            issue_spent_usd=0.0,
            daily_cap_usd=100.0,
            daily_spent_usd=0.0,
            issue_num=42,
        )
        base.update(kw)
        return BudgetView(**base)

    def test_warns_when_cap_over_headroom(self):
        msg = runtime_cap_conflict_warning(self._view())
        assert msg is not None
        assert "max_budget_usd" in msg
        assert "per_issue_usd" in msg

    def test_silent_when_cap_within_headroom(self):
        assert runtime_cap_conflict_warning(self._view(runtime_cap_usd=0.20)) is None


class TestPerIssueOverspendNote:
    """per_issue_overspend_note fires when recorded issue spend exceeds the cap."""

    def _view(self, **kw) -> BudgetView:
        base = dict(
            runtime_cap_usd=25.0,
            runtime_cap_source="default",
            per_issue_cap_usd=0.40,
            issue_spent_usd=0.41,
            daily_cap_usd=100.0,
            daily_spent_usd=0.41,
            issue_num=42,
        )
        base.update(kw)
        return BudgetView(**base)

    def test_note_when_over_cap(self):
        note = per_issue_overspend_note(self._view())
        assert note is not None
        assert "#42" in note
        assert "next dispatch" in note.lower()

    def test_no_note_when_within_cap(self):
        assert per_issue_overspend_note(self._view(issue_spent_usd=0.10)) is None

    def test_no_note_when_spend_unknown(self):
        assert per_issue_overspend_note(self._view(issue_spent_usd=None)) is None


# ── Task 9 (F5): review pool separation ────────────────────────────────


def _make_review_record(
    run_id: str,
    item_id: str,
    cost: float,
    started_at: datetime,
    run_kind: str = "review_remediation",
    status: str = "success",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=42,
        branch="agent/issue-42",
        status=status,
        exit_code=0,
        started_at=started_at,
        estimated_cost_usd=cost,
        item_id=item_id,
        run_kind=run_kind,
    )


class TestReviewPoolSeparation:
    """check_budget filters review runs from the implementation cap (F5/#978).

    Implementation-kind budget checks must NOT count review-pool spend, so a
    remediation run burning the review pool cannot block a fresh implementation
    dispatch, and vice versa.  Two pools, both enforced, both independent.
    """

    def test_review_remediation_runs_excluded_from_impl_cap(self, store):
        """check_budget on an item with a review_remediation run must NOT count
        that spend toward the per-issue implementation cap."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # $8 review_remediation run — must NOT count toward $15 impl cap.
        store.save(_make_review_record("r-rev", "42", 8.0, now, "review_remediation"))
        # $6 implementation run — well within $15 cap.
        store.save(_make_record("r-impl", 42, 6.0, now, item_id="42"))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(store, budget, issue_num=42, item_id="42", now=now)
        # Only the $6 impl run counts; the $8 review run must be invisible.
        assert status.is_within_budget is True
        assert status.issue_spent_usd == pytest.approx(6.0)

    def test_adversarial_review_runs_excluded_from_impl_cap(self, store):
        """adversarial_review runs are also excluded from the implementation cap."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("r-adv", "42", 12.0, now, "adversarial_review"))
        store.save(_make_record("r-impl", 42, 5.0, now, item_id="42"))
        budget = BudgetConfig(per_issue_usd=15.0)
        status = check_budget(store, budget, issue_num=42, item_id="42", now=now)
        assert status.is_within_budget is True
        assert status.issue_spent_usd == pytest.approx(5.0)

    def test_ci_fix_runs_excluded_from_impl_cap(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("ci-1", "42", 50.0, now, "ci_fix"))
        store.save(_make_record("r-impl", 42, 5.0, now, item_id="42"))

        status = check_budget(
            store,
            BudgetConfig(per_issue_usd=15.0, daily_usd=100.0),
            issue_num=42,
            item_id="42",
            now=now,
        )

        assert status.issue_spent_usd == pytest.approx(5.0)

    def test_pending_ci_fix_rows_stay_out_of_the_implementation_pool(self, store):
        """#1932: an in-flight ci_fix row is not implementation work, even at
        the kill-cap reservation."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        budget = BudgetConfig(per_issue_usd=15.0, daily_usd=100.0)
        store.save(_make_record("r-impl", 42, 5.0, now, item_id="42"))
        store.save(
            _make_review_record("ci-own", "42", 0.0, now, "ci_fix", status="pending")
        )

        status = check_budget(
            store,
            budget,
            issue_num=42,
            item_id="42",
            now=now,
            pending_reservation_usd=25.0,
        )

        assert status.issue_spent_usd == pytest.approx(5.0)
        assert status.daily_spent_usd == pytest.approx(30.0)


class TestPrSubjectBudget:
    """check_pr_subject_budget: the ci-fix gate binds daily and weekly caps,
    never the per-issue implementation cap (#1932)."""

    NOW = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)

    def test_issue_implementation_spend_does_not_bind(self, store):
        store.save(_make_record("r-impl", 42, 40.0, self.NOW, item_id="42"))
        store.save(
            _make_record("r-live", 42, 0.0, self.NOW, status="pending", item_id="42")
        )

        status = check_pr_subject_budget(
            store,
            BudgetConfig(per_issue_usd=15.0, daily_usd=100.0),
            now=self.NOW,
            pending_reservation_usd=25.0,
            count_admitting_run=True,
        )

        assert status.issue_spent_usd == 0.0
        assert status.daily_spent_usd == pytest.approx(90.0)

    def test_own_reservation_over_the_daily_cap_is_refused(self, store):
        """One in-flight run plus the admitting run's own row, both valued at
        the $25 kill cap, breach a $40 daily cap."""
        store.save(
            _make_record("r-live", 7, 0.0, self.NOW, status="pending", item_id="7")
        )
        budget = BudgetConfig(daily_usd=40.0)

        # Before the row exists (the driver) and after (inside try_admit_run).
        with pytest.raises(BudgetExceeded) as predicted:
            check_pr_subject_budget(
                store,
                budget,
                now=self.NOW,
                pending_reservation_usd=25.0,
                count_admitting_run=True,
            )
        store.save(
            _make_review_record("ci-own", "42", 0.0, self.NOW, "ci_fix", "pending")
        )
        with pytest.raises(BudgetExceeded) as admitted:
            check_pr_subject_budget(
                store, budget, now=self.NOW, pending_reservation_usd=25.0
            )
        for refusal in (predicted.value, admitted.value):
            assert refusal.which_limit == "daily"
            assert refusal.spent_usd == pytest.approx(50.0)

    def test_weekly_cap_binds(self, store):
        store.save(
            _make_record("r-old", 7, 30.0, self.NOW - timedelta(days=3), item_id="7")
        )

        with pytest.raises(BudgetExceeded) as refusal:
            check_pr_subject_budget(
                store, BudgetConfig(daily_usd=100.0, weekly_usd=20.0), now=self.NOW
            )

        assert refusal.value.which_limit == "weekly"

    def test_own_reservation_over_the_weekly_cap_is_refused(self, store):
        """The weekly twin of the daily reservation test (#2049). An in-flight
        run from three days ago and the admitting run's own row, both valued at
        the $25 kill cap, breach a $40 weekly cap while today's window stays
        far inside the daily cap. Mutation caught: dropping the weekly
        reservation bump, or leaving the admitting row out of the weekly
        pending count."""
        store.save(
            _make_record(
                "r-live",
                7,
                0.0,
                self.NOW - timedelta(days=3),
                status="pending",
                item_id="7",
            )
        )
        budget = BudgetConfig(daily_usd=100.0, weekly_usd=40.0)

        # Before the row exists (the driver) and after (inside try_admit_run).
        with pytest.raises(BudgetExceeded) as predicted:
            check_pr_subject_budget(
                store,
                budget,
                now=self.NOW,
                pending_reservation_usd=25.0,
                count_admitting_run=True,
            )
        store.save(
            _make_review_record("ci-own", "42", 0.0, self.NOW, "ci_fix", "pending")
        )
        with pytest.raises(BudgetExceeded) as admitted:
            check_pr_subject_budget(
                store, budget, now=self.NOW, pending_reservation_usd=25.0
            )
        for refusal in (predicted.value, admitted.value):
            assert refusal.which_limit == "weekly"
            assert refusal.spent_usd == pytest.approx(50.0)

    def test_admitting_run_reservation_is_reported_in_weekly_spend(self, store):
        """The weekly twin of the status assertion in
        ``test_issue_implementation_spend_does_not_bind`` (#2049): the
        predicted reservation shows up in the reported weekly worst case, not
        just in the refusal arithmetic."""
        store.save(
            _make_record("r-old", 7, 30.0, self.NOW - timedelta(days=3), item_id="7")
        )

        status = check_pr_subject_budget(
            store,
            BudgetConfig(daily_usd=100.0, weekly_usd=200.0),
            now=self.NOW,
            pending_reservation_usd=25.0,
            count_admitting_run=True,
        )

        assert status.weekly_spent_usd == pytest.approx(55.0)
        assert status.daily_spent_usd == pytest.approx(25.0)

    def test_lone_admitting_run_may_overshoot_the_weekly_cap(self, store):
        """The weekly twin of the CS-7 posture (#2049): the admitting run's own
        $25 reservation alone passes a $10 weekly cap, because a single
        in-flight run is bounded by its kill cap rather than refused."""
        check_pr_subject_budget(
            store,
            BudgetConfig(daily_usd=100.0, weekly_usd=10.0),
            now=self.NOW,
            pending_reservation_usd=25.0,
            count_admitting_run=True,
        )

    def test_lone_admitting_run_may_overshoot_like_dispatch(self, store):
        """A single in-flight run is bounded by its kill cap, not refused
        (CS-7): the $25 reservation alone passes a $10 daily cap."""
        check_pr_subject_budget(
            store,
            BudgetConfig(daily_usd=10.0),
            now=self.NOW,
            pending_reservation_usd=25.0,
            count_admitting_run=True,
        )

    def test_impl_cap_can_trip_independently_of_review_spend(self, store):
        """An over-cap implementation run raises even when review spend is low."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("r-rev", "42", 2.0, now))
        store.save(_make_record("r-impl", 42, 16.0, now, item_id="42"))
        budget = BudgetConfig(per_issue_usd=15.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, budget, issue_num=42, item_id="42", now=now)
        assert exc_info.value.which_limit == "per_issue"
        assert exc_info.value.spent_usd == pytest.approx(16.0)


class TestCheckReviewBudget:
    """check_review_budget enforces the per-item review pool cap (F5/#978)."""

    def test_within_limit_returns_without_raising(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("r1", "42", 3.0, now))
        check_review_budget(store, "42", max_review_usd=10.0)

    def test_exceeded_raises_budget_exceeded(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("r1", "42", 8.0, now))
        store.save(_make_review_record("r2", "42", 5.0, now))
        with pytest.raises(BudgetExceeded) as exc_info:
            check_review_budget(store, "42", max_review_usd=10.0)
        assert exc_info.value.which_limit == "review"
        assert exc_info.value.spent_usd == pytest.approx(13.0)

    def test_impl_runs_not_counted_in_review_pool(self, store):
        """Implementation runs must NOT count toward the review pool cap."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_record("r-impl", 42, 12.0, now, item_id="42"))
        check_review_budget(store, "42", max_review_usd=10.0)

    @pytest.mark.parametrize("pool_limit", [10.0, 40.0], ids=["review", "remediation"])
    def test_ci_fix_runs_do_not_count_against_review_pools(self, store, pool_limit):
        """CI fixes use their own budget pool, not either governed review pool."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("ci-1", "42", 50.0, now, "ci_fix"))

        status = check_review_budget(store, "42", max_review_usd=pool_limit)

        assert status.issue_spent_usd == 0.0

    @pytest.mark.parametrize(
        "spending_kind", ["adversarial_review", "review_remediation"]
    )
    @pytest.mark.parametrize("pool_limit", [10.0, 40.0], ids=["review", "remediation"])
    def test_both_review_kinds_draw_down_both_caps(
        self, store, spending_kind, pool_limit
    ):
        """One pool, two caps (#2153 review): whichever cap is handed in, the
        sum behind it counts *both* review kinds — so remediation spend alone
        can exhaust ``max_review_usd`` and vice versa. The docs must not sell
        the caps as two independent pools."""
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(
            _make_review_record("r1", "42", pool_limit + 1.0, now, spending_kind)
        )

        with pytest.raises(BudgetExceeded) as exc_info:
            check_review_budget(store, "42", max_review_usd=pool_limit)

        assert exc_info.value.spent_usd == pytest.approx(pool_limit + 1.0)

    def test_different_item_not_counted(self, store):
        now = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        store.save(_make_review_record("r1", "99", 20.0, now))
        check_review_budget(store, "42", max_review_usd=10.0)

    def test_zero_limit_always_passes_when_no_spend(self, store):
        check_review_budget(store, "42", max_review_usd=0.0)


class TestReviewBudgetHoldReason:
    """The hold an exhausted review pool writes, and reading it back (#2153)."""

    def test_reason_names_the_pool_the_spend_and_the_key_to_raise(self):
        from kew.budget import REMEDIATION_POOL_KEY, review_budget_hold_reason

        reason = review_budget_hold_reason(
            limit_usd=40.0, spent_usd=43.97, config_key=REMEDIATION_POOL_KEY
        )

        assert reason == (
            "review_budget: remediation pool $40.00 exhausted (spent $43.97) "
            "— raise [review].max_remediation_usd"
        )

    @pytest.mark.parametrize(
        ("config_key", "expected"),
        [("max_remediation_usd", 40.0), ("max_review_usd", 10.0)],
    )
    def test_cap_round_trips_to_the_pool_that_wrote_it(self, config_key, expected):
        from kew.budget import review_budget_hold_cap, review_budget_hold_reason
        from kew.config import ReviewConfig

        review = ReviewConfig(max_review_usd=10.0, max_remediation_usd=40.0)
        reason = review_budget_hold_reason(
            limit_usd=expected, spent_usd=99.0, config_key=config_key
        )

        assert review_budget_hold_cap(reason, review) == expected

    @pytest.mark.parametrize(
        "reason",
        [
            "budget_preflight: spent $17.08 > per_issue cap $15.00",
            "review_rounds_exhausted",
            "review_budget: some future pool — raise [review].max_future_usd",
        ],
    )
    def test_a_hold_this_version_cannot_price_is_left_alone(self, reason):
        """Never guess a cap: a hold whose pool we cannot identify stays held
        and waits for `kew epic unhold` rather than being cleared wrongly."""
        from kew.budget import review_budget_hold_cap
        from kew.config import ReviewConfig

        assert review_budget_hold_cap(reason, ReviewConfig()) is None


class TestDailyCapIsDirectoryIndependent:
    """The daily cap sums one ledger from any directory in the project (#1156)."""

    @staticmethod
    def _project(tmp_path):
        project = (tmp_path / "repo").resolve()
        (project / ".git").mkdir(parents=True)
        (project / "kew.toml").write_text(
            '[budget]\ndaily_usd = 250.0\n[logging]\ndir = "./logs/dispatch"\n',
            encoding="utf-8",
        )
        (project / "pkg").mkdir()
        return project

    def _daily_spent(self, now):
        """Load config the way the CLI does, then read spend from the DB it picks."""
        from pathlib import Path

        from kew.budget import effective_budget_view
        from kew.config import load_config

        config = load_config()
        db_path = Path(config.logging.dir).resolve() / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            return effective_budget_view(
                config, store, issue=None, now=now
            ).daily_spent_usd

    def test_same_spend_from_root_and_subdirectory(self, tmp_path, monkeypatch):
        project = self._project(tmp_path)
        now = datetime(2026, 7, 26, 18, 0, 0, tzinfo=UTC)
        db_dir = project / "logs" / "dispatch"
        db_dir.mkdir(parents=True)
        with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
            store.save(_make_record("r1", 1126, 205.17, now))

        monkeypatch.chdir(project)
        from_root = self._daily_spent(now)
        monkeypatch.chdir(project / "pkg")
        from_subdir = self._daily_spent(now)

        assert from_root == pytest.approx(205.17)
        assert from_subdir == pytest.approx(from_root)
