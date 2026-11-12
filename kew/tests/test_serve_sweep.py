"""Tests for the serve-side outcome sweep worker (Group 6, Task 1/3).

The sweep is a fail-soft background thread run inside the serve lifecycle
that periodically drives `sync_outcomes` + `reconcile_orphan_prs`. These
tests inject a deterministic clock (a fake `sleep`) so scheduling is exact
with no real threads or wall-clock waits, and prove the three contract
points: it runs on a schedule, a cycle error skips (never crashes) and the
worker keeps going, and it never runs per-request (disabled by default).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app
from kew.serve.sweep import SweepCycleResult, SweepWorker


def _stop_after(worker: SweepWorker, n: int, sink: list[float]) -> Any:
    """A fake sleep that records the interval and stops the worker after n
    scheduled sleeps — the injectable clock that makes scheduling deterministic."""

    def _sleep(interval: float) -> None:
        sink.append(interval)
        if len(sink) >= n:
            worker.request_stop()

    return _sleep


def test_sweep_runs_on_schedule() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def sweep_once() -> None:
        calls.append(1)

    worker = SweepWorker(sweep_once, interval_s=60.0)
    worker._sleep = _stop_after(worker, 3, sleeps)  # inject the clock

    worker.run_until_stopped()

    assert len(calls) == 3  # one cycle per scheduled tick
    assert sleeps == [60.0, 60.0, 60.0]  # slept the configured interval each time
    assert worker.last_sweep_at is not None  # "as of" stamp is set


def test_last_sweep_at_uses_injected_now() -> None:
    # One stamp per clock read: each cycle reads it twice (the attempt, then the
    # completion), so the second cycle completes at 00:03.
    stamps = [datetime(2026, 7, 16, 0, minute, tzinfo=UTC) for minute in (0, 1, 2, 3)]

    def now() -> datetime:
        return stamps.pop(0)

    worker = SweepWorker(lambda: None, interval_s=1.0, now=now)
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 2, sleeps)

    worker.run_until_stopped()

    # Last successful cycle stamped its completion time from the injected clock.
    assert worker.last_sweep_at == datetime(2026, 7, 16, 0, 3, tzinfo=UTC)
    assert worker.last_attempt_at == datetime(2026, 7, 16, 0, 2, tzinfo=UTC)


def test_sweep_is_fail_soft_on_error() -> None:
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        raise RuntimeError("gh boom")  # simulate a gh outage mid-cycle

    worker = SweepWorker(flaky, interval_s=5.0)
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 3, sleeps)

    # A raising cycle must never propagate out of the worker loop.
    worker.run_until_stopped()

    assert len(calls) == 3  # kept sweeping across failures
    assert worker.last_sweep_at is None  # a failed cycle does not stamp "as of"


def _mk_app(tmp_path: Path, *, sweep: SweepWorker | None = None) -> Any:
    db = tmp_path / "dispatch.db"
    SqliteRunStore(str(db)).close()
    return create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        sweep=sweep,
    )


def test_nonpositive_interval_rejected() -> None:
    with pytest.raises(ValueError, match="interval_s"):
        SweepWorker(lambda: None, interval_s=0)
    with pytest.raises(ValueError, match="interval_s"):
        SweepWorker(lambda: None, interval_s=-5.0)


def test_restart_after_stop_launches_a_live_worker() -> None:
    # stop() leaves the stop event set; start() must clear it or the
    # restarted worker exits on its first loop check without cycling.
    first = threading.Event()
    second = threading.Event()

    def sweep_once() -> None:
        if not first.is_set():
            first.set()
        else:
            second.set()

    worker = SweepWorker(sweep_once, interval_s=60.0)
    worker.start()
    assert first.wait(5.0)
    worker.stop()

    worker.start()
    assert second.wait(5.0)  # a dead restarted worker never cycles again
    worker.stop()


def test_stop_preserves_thread_ref_when_join_times_out() -> None:
    release = threading.Event()
    started = threading.Event()

    def sweep_once() -> None:
        started.set()
        release.wait(10.0)  # hold the worker mid-cycle past the join timeout

    worker = SweepWorker(sweep_once, interval_s=60.0)
    worker.start()
    assert started.wait(5.0)
    worker.stop(timeout=0.05)
    # Worker is still terminating: the reference must survive so a
    # subsequent start() can't stack an overlapping worker.
    assert worker._thread is not None
    release.set()
    worker.stop()
    assert worker._thread is None


def test_sweep_disabled_by_default(tmp_path: Path) -> None:
    app = _mk_app(tmp_path)
    assert app.state.sweep is None  # tests get no sweep unless one is injected


def test_sweep_never_runs_per_request(tmp_path: Path) -> None:
    calls: list[int] = []
    worker = SweepWorker(lambda: calls.append(1), interval_s=60.0)
    app = _mk_app(tmp_path, sweep=worker)
    assert app.state.sweep is worker

    # No lifespan context => the worker thread is never started; a burst of
    # requests must not drive a single sweep cycle (it is scheduled, not
    # request-triggered).
    client = TestClient(app)
    for _ in range(5):
        client.get("/api/meta")

    assert calls == []


def test_lifespan_starts_and_stops_worker(tmp_path: Path) -> None:
    events: list[str] = []

    class _SpyWorker(SweepWorker):
        def start(self) -> None:
            events.append("start")

        def stop(self, timeout: float = 5.0) -> None:
            events.append("stop")

    worker = _SpyWorker(lambda: None)
    app = _mk_app(tmp_path, sweep=worker)

    with TestClient(app):
        assert events == ["start"]
    assert events == ["start", "stop"]


# --- #2297: a sweep cycle that can actually report a problem ----------------


def test_sweep_records_partial_when_the_sync_had_query_failures() -> None:
    worker = SweepWorker(
        lambda: SweepCycleResult(synced=2, query_failures=1), interval_s=5.0
    )
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "partial"  # some gh calls failed, some worked
    assert worker.query_failures == 1
    assert worker.last_attempt_at is not None
    assert worker.last_ok_at is None  # a partial cycle is not an ok cycle


def test_sweep_records_failed_when_every_query_failed() -> None:
    worker = SweepWorker(
        lambda: SweepCycleResult(synced=0, no_pr=0, query_failures=3), interval_s=5.0
    )
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "failed"
    assert worker.query_failures == 3


def test_sweep_records_failed_when_the_cycle_raised() -> None:
    def boom() -> SweepCycleResult:
        raise RuntimeError("gh boom")

    worker = SweepWorker(boom, interval_s=5.0)
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "failed"
    assert worker.last_attempt_at is not None  # the attempt is recorded
    assert worker.last_ok_at is None


def test_sweep_records_ok_and_keeps_the_rate_limit_reading() -> None:
    reading = {"core": {"limit": 5000, "remaining": 4999, "reset_at": None}}
    worker = SweepWorker(
        lambda: SweepCycleResult(synced=1, rate_limit=reading),
        interval_s=5.0,
        now=lambda: datetime(2026, 10, 6, tzinfo=UTC),
    )
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "ok"
    assert worker.query_failures == 0
    assert worker.last_ok_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert worker.rate_limit == reading
    assert worker.health()["last_result"] == "ok"


def test_the_health_reading_shows_the_cycles_graphql_spend() -> None:
    worker = SweepWorker(
        lambda: SweepCycleResult(synced=3, graphql_calls=1),
        interval_s=5.0,
        now=lambda: datetime(2026, 10, 6, tzinfo=UTC),
    )
    worker._sleep = _stop_after(worker, 1, [])

    worker.run_until_stopped()

    assert worker.health()["graphql_calls"] == 1


def test_a_401_rate_limit_reading_is_never_a_clean_cycle() -> None:
    # The review regression: the auth probe failing used to read `ok` and stamp
    # last_ok_at, so the one call that proves the credential is broken proved
    # nothing at all.
    reading = {"ok": False, "status": 401, "reason": "unauthorized"}
    worker = SweepWorker(
        lambda: SweepCycleResult(rate_limit=reading),
        interval_s=5.0,
        now=lambda: datetime(2026, 10, 6, tzinfo=UTC),
    )
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "failed"
    assert worker.last_ok_at is None
    assert worker.last_attempt_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert worker.health()["rate_limit"] == reading


def test_a_401_alongside_resolved_outcome_work_reads_partial() -> None:
    cycle = SweepCycleResult(
        synced=2, rate_limit={"ok": False, "status": 401, "reason": "unauthorized"}
    )
    assert cycle.result == "partial"
    assert cycle.rate_limit_ok is False
    assert cycle.rate_limit_reason == "unauthorized"


def test_a_cycle_that_took_no_rate_limit_reading_is_unaffected() -> None:
    cycle = SweepCycleResult(synced=1)
    assert cycle.result == "ok"
    assert cycle.rate_limit_ok is None
    assert cycle.rate_limit_reason == ""


def test_a_sweep_callable_that_returns_none_still_reads_ok() -> None:
    # The legacy shape (a callable returning None) must keep working.
    worker = SweepWorker(lambda: None, interval_s=5.0)
    sleeps: list[float] = []
    worker._sleep = _stop_after(worker, 1, sleeps)

    worker.run_until_stopped()

    assert worker.last_result == "ok"


def test_sweep_cycle_records_the_heartbeat_and_the_rate_limit_reading(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The cycle `kew serve` schedules: it reports failures, stamps the
    scheduler heartbeat nothing used to write, and caches the free reading."""
    from kew.cli import serve as serve_cli
    from kew.github_rate_limit import RateLimitBucket, RateLimitReading
    from kew.outcomes import SyncResult

    db = tmp_path / "dispatch.db"
    SqliteRunStore(str(db)).close()
    config = DispatchConfig()
    reading = RateLimitReading(
        observed_at="2026-10-06T12:00:00+00:00",
        core=RateLimitBucket(limit=5000, remaining=4987, reset_at=None),
        ok=True,
        status=200,
    )
    monkeypatch.setattr(
        "kew.outcomes.sync_outcomes",
        lambda store, cfg, **k: SyncResult(synced=0, no_pr=0, query_failures=4),
    )
    monkeypatch.setattr("kew.outcomes.reconcile_orphan_prs", lambda store, cfg: None)
    monkeypatch.setattr("kew.github_rate_limit.read_rate_limit", lambda **k: reading)

    result = serve_cli.sweep_cycle(lambda: SqliteRunStore(str(db)), config)

    assert result.result == "failed"  # every query failed: not a clean cycle
    assert result.query_failures == 4
    with SqliteRunStore(str(db)) as store:
        assert store.get_meta("scheduler_heartbeat") is not None
        cached = json.loads(store.get_meta("github_rate_limit") or "{}")
    assert cached["core"]["remaining"] == 4987
    assert cached["ok"] is True


def test_the_sweep_snapshots_the_epic_driver_locks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """#2297 review: the lock's evidence has to reach the readings that cannot
    open it.

    Every serve-side liveness reading (``/api/epics``, the dashboard banner, the
    needs-you "driver gone" row) sits the far side of the import-linter boundary
    forbidding ``kew.serve``/``kew.service`` from importing ``kew.graph``, so none
    of them can read the lock file. The sweep is CLI-side, so it looks once a
    cycle and records what it saw — and a driver provably alive mid-long-tick
    stops reading "gone" on the console while ``kew epic status`` reads it live.
    """
    from kew.cli import serve as serve_cli
    from kew.graph.driver.lock import LockHolder
    from kew.outcomes import SyncResult
    from kew.service.epics import driver_liveness

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as seed:
        seed.begin_epic_tick("e", "2026-07-16T00:00:00+00:00")
        seed.finish_epic_tick(
            "e",
            tick_ts="2026-07-16T00:00:30+00:00",
            duration_s=30.0,
            interval_s=60.0,
            signal="progressing",
            tick_id=None,
        )
    config = DispatchConfig()
    beat = datetime(2026, 7, 16, 0, 9, 59, tzinfo=UTC)
    monkeypatch.setattr(
        "kew.outcomes.sync_outcomes",
        lambda store, cfg, **k: SyncResult(synced=0, no_pr=0, query_failures=0),
    )
    monkeypatch.setattr("kew.outcomes.reconcile_orphan_prs", lambda store, cfg: None)
    monkeypatch.setattr("kew.github_rate_limit.read_rate_limit", lambda **k: _reading())
    # A REAL live pid: the sweep asks the holder whether it is alive, and this
    # test is about a driver that provably is.
    driver_pid = os.getpid()
    monkeypatch.setattr(
        serve_cli._c,
        "lock_holder",
        lambda path: LockHolder("e", driver_pid, held_since=beat, heartbeat=beat),
    )
    monkeypatch.setattr(serve_cli, "_now_iso", lambda: beat.isoformat())

    serve_cli.sweep_cycle(lambda: SqliteRunStore(str(db)), config)

    with SqliteRunStore(str(db)) as store:
        state = store.get_epic_driver_state("e")
        assert state is not None
        assert (state.lock_pid, state.lock_pid_alive) == (driver_pid, True)
        assert state.lock_heartbeat_at == beat.isoformat()
        assert state.lock_observed_at == beat.isoformat()
        # The ten-minute tick read gone off the tick stamps alone; the beat the
        # sweep copied over makes the same driver live, with no caller help.
        live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 10, tzinfo=UTC))
        assert live.status == "live"
        assert live.pid == driver_pid


def test_the_sweep_records_a_free_lock_as_the_absence_it_is(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # `epic run` takes the lock per TICK, so between ticks it is free. That is
    # the normal state and must be recorded as evidence of nothing, never as a
    # dead driver — and the previous observation must not linger as if current.
    from kew.cli import serve as serve_cli
    from kew.outcomes import SyncResult

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as seed:
        seed.begin_epic_tick("e", "2026-07-16T00:00:00+00:00")
        seed.record_epic_driver_lock(
            "e",
            pid=4242,
            pid_alive=True,
            heartbeat_at="2026-07-16T00:00:01+00:00",
            observed_at="2026-07-16T00:00:01+00:00",
        )
    monkeypatch.setattr(
        "kew.outcomes.sync_outcomes",
        lambda store, cfg, **k: SyncResult(synced=0, no_pr=0, query_failures=0),
    )
    monkeypatch.setattr("kew.outcomes.reconcile_orphan_prs", lambda store, cfg: None)
    monkeypatch.setattr("kew.github_rate_limit.read_rate_limit", lambda **k: _reading())
    monkeypatch.setattr(serve_cli._c, "lock_holder", lambda path: None)

    serve_cli.sweep_cycle(lambda: SqliteRunStore(str(db)), DispatchConfig())

    with SqliteRunStore(str(db)) as store:
        state = store.get_epic_driver_state("e")
        assert state is not None
        assert (state.lock_pid, state.lock_pid_alive) == (None, None)
        assert state.lock_heartbeat_at is None
        assert state.lock_observed_at is not None


def _reading() -> Any:
    from kew.github_rate_limit import RateLimitBucket, RateLimitReading

    return RateLimitReading(
        observed_at="2026-10-06T12:00:00+00:00",
        core=RateLimitBucket(limit=5000, remaining=4987, reset_at=None),
        ok=True,
        status=200,
    )


def test_the_sweep_leaves_a_journal_only_epic_without_a_driver_row(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # An epic with journal ticks but no heartbeat row (pre-heartbeat history) must
    # not be seeded with an empty one: the Epics list reads its last tick off that
    # row, and an all-NULL row would hide the journal's own stamp behind
    # "never ticked".
    from kew.cli import serve as serve_cli
    from kew.outcomes import SyncResult
    from kew.service.epics import list_epics

    db = tmp_path / "dispatch.db"
    snapshot = {
        "schema": 1,
        "epic_ref": "e",
        "signal": "progressing",
        "counts": {},
        "frontier": [],
        "nodes": [],
        "edges": [],
    }
    with SqliteRunStore(str(db)) as seed:
        seed.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-07-16T00:00:00+00:00",
            snapshot=snapshot,
            actions={},
            snapshot_hash="h1",
        )
    monkeypatch.setattr(
        "kew.outcomes.sync_outcomes",
        lambda store, cfg, **k: SyncResult(synced=0, no_pr=0, query_failures=0),
    )
    monkeypatch.setattr("kew.outcomes.reconcile_orphan_prs", lambda store, cfg: None)
    monkeypatch.setattr("kew.github_rate_limit.read_rate_limit", lambda **k: _reading())
    monkeypatch.setattr(serve_cli._c, "lock_holder", lambda path: None)

    serve_cli.sweep_cycle(lambda: SqliteRunStore(str(db)), DispatchConfig())

    with SqliteRunStore(str(db)) as store:
        assert store.get_epic_driver_state("e") is None
        assert list_epics(store)[0].last_tick_ts == "2026-07-16T00:00:00+00:00"
