"""Tests for analytics query functions."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kew.run_store import RunRecord, SqliteRunStore


def _make_run(
    run_id: str,
    *,
    started_at: datetime,
    prefix_key: str | None = None,
    cache_read_tokens: int = 0,
    status: str = "success",
    log_file: Path | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=1,
        branch=f"b-{run_id}",
        status=status,
        exit_code=0 if status == "success" else 1,
        started_at=started_at,
        prefix_key=prefix_key,
        cache_read_tokens=cache_read_tokens,
        log_file=log_file,
    )


def _write_transcript(
    path: Path, *, first_turn_cache_read: int, later_turn_cache_read: int = 99999
) -> Path:
    """Write a minimal stream-json transcript whose first usage-bearing
    assistant turn reports ``first_turn_cache_read`` cache reads and a later
    turn reports a large self-cache total (which the probe must NOT report)."""
    lines = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 40,
                    "cache_read_input_tokens": first_turn_cache_read,
                }
            },
        },
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 60,
                    "cache_read_input_tokens": later_turn_cache_read,
                }
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


class TestAnalyticsStats:
    """aggregate_stats returns correct totals."""

    def _seed(
        self, store, count=5, success_count=4, cost=0.30, duration=360.0, now=None
    ):
        # Accept a caller-supplied reference time so a since-window test can
        # filter against the SAME `now` it seeded with (no wall-clock race).
        if now is None:
            now = datetime.now(UTC)
        for i in range(count):
            store.save(
                RunRecord(
                    run_id=f"a-{i}",
                    issue_num=i + 1,
                    branch=f"b{i}",
                    status="success" if i < success_count else "failure",
                    exit_code=0 if i < success_count else 1,
                    started_at=now - timedelta(hours=count - i),
                    finished_at=now - timedelta(hours=count - i - 1),
                    duration_seconds=duration,
                    estimated_cost_usd=cost,
                    error_category="" if i < success_count else "test_failure",
                    model="claude-sonnet-4-6",
                    complexity="simple" if i < 3 else "medium",
                )
            )
        return now

    def test_aggregate_stats(self, tmp_path):
        from kew.analytics import aggregate_stats

        store = SqliteRunStore(str(tmp_path / "test.db"))
        self._seed(store)
        stats = aggregate_stats(store, since=None)
        assert stats["runs"] == 5
        assert stats["success_rate"] == 80.0
        assert abs(stats["avg_cost"] - 0.30) < 0.01
        assert abs(stats["avg_duration"] - 360.0) < 0.1
        assert abs(stats["total_spend"] - 1.50) < 0.01
        store.close()

    def test_aggregate_stats_with_since(self, tmp_path):
        from kew.analytics import aggregate_stats

        store = SqliteRunStore(str(tmp_path / "test.db"))
        # Seed and filter against a single reference time: row i=3 is seeded at
        # now-2h and the window opens at now-2h-1s, so it is always inside —
        # deterministic regardless of how long seeding takes.
        now = self._seed(store)
        since = now - timedelta(hours=2, seconds=1)
        stats = aggregate_stats(store, since=since)
        assert stats["runs"] == 2
        store.close()

    def test_aggregate_stats_empty(self, tmp_path):
        from kew.analytics import aggregate_stats

        store = SqliteRunStore(str(tmp_path / "test.db"))
        stats = aggregate_stats(store, since=None)
        assert stats["runs"] == 0
        assert stats["success_rate"] == 0.0
        store.close()

    def test_failure_breakdown(self, tmp_path):
        from kew.analytics import failure_breakdown

        store = SqliteRunStore(str(tmp_path / "test.db"))
        self._seed(store, count=6, success_count=3)
        breakdown = failure_breakdown(store, since=None)
        assert len(breakdown) >= 1
        assert breakdown[0] == ("test_failure", 3)
        store.close()

    def test_model_breakdown(self, tmp_path):
        from kew.analytics import model_breakdown

        store = SqliteRunStore(str(tmp_path / "test.db"))
        self._seed(store)
        breakdown = model_breakdown(store, since=None)
        assert len(breakdown) == 1
        row = breakdown[0]
        assert row["model"] == "claude-sonnet-4-6"
        assert row["runs"] == 5
        store.close()

    def test_complexity_breakdown(self, tmp_path):
        from kew.analytics import complexity_breakdown

        store = SqliteRunStore(str(tmp_path / "test.db"))
        self._seed(store)
        breakdown = complexity_breakdown(store, since=None)
        assert len(breakdown) == 2  # simple + medium
        store.close()

    def test_daily_activity(self, tmp_path):
        from kew.analytics import daily_activity

        store = SqliteRunStore(str(tmp_path / "test.db"))
        self._seed(store)
        activity = daily_activity(store, days=7)
        assert isinstance(activity, list)
        assert len(activity) == 7
        for date_str, successes, failures in activity:
            assert isinstance(date_str, str)
            assert isinstance(successes, int)
            assert isinstance(failures, int)
        store.close()


class TestPrefixPairProbe:
    """prefix_pair_probe reports run-2 cache_read_tokens for equal-prefix_key pairs."""

    def test_prefix_pair_probe_reports_first_turn_cache_reads(self, tmp_path):
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        key = "abc123"

        # run2's transcript: first turn served 8500 cross-agent cache reads,
        # a later turn self-cached 99999. The probe must report the FIRST turn's
        # 8500 — not the run-level total (which cache_read_tokens=200000 would
        # falsely inflate), proving the probe can actually falsify D3.
        transcript = _write_transcript(
            tmp_path / "r2.jsonl",
            first_turn_cache_read=8500,
            later_turn_cache_read=99999,
        )

        run1 = _make_run("r1", started_at=now, prefix_key=key, cache_read_tokens=0)
        run2 = _make_run(
            "r2",
            started_at=now + timedelta(seconds=120),
            prefix_key=key,
            cache_read_tokens=200000,
            log_file=transcript,
        )
        store.save(run1)
        store.save(run2)

        results = prefix_pair_probe(store, ttl_seconds=300)

        assert len(results) == 1
        pair = results[0]
        assert pair["run1_id"] == "r1"
        assert pair["run2_id"] == "r2"
        assert pair["prefix_key"] == key
        assert pair["delta_seconds"] == 120.0
        assert pair["ttl_seconds"] == 300
        assert pair["run2_first_turn_cache_read_tokens"] == 8500
        store.close()

    def test_probe_reports_zero_first_turn_cache_miss(self, tmp_path):
        # The D3 kill scenario: run2's first turn served ZERO cross-agent cache
        # reads even though later turns self-cached heavily. Reporting the run
        # total would hide this; the probe must surface the falsifying 0.
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        key = "miss-key"

        transcript = _write_transcript(
            tmp_path / "r2.jsonl",
            first_turn_cache_read=0,
            later_turn_cache_read=120000,
        )

        store.save(_make_run("r1", started_at=now, prefix_key=key))
        store.save(
            _make_run(
                "r2",
                started_at=now + timedelta(seconds=60),
                prefix_key=key,
                cache_read_tokens=120000,
                log_file=transcript,
            )
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert len(results) == 1
        assert results[0]["run2_first_turn_cache_read_tokens"] == 0
        store.close()

    def test_probe_reports_none_when_transcript_missing(self, tmp_path):
        # No transcript → the first-turn value is unknowable; the probe reports
        # None rather than falling back to the (misleading) run-level total.
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        key = "no-log"

        store.save(_make_run("r1", started_at=now, prefix_key=key))
        store.save(
            _make_run(
                "r2",
                started_at=now + timedelta(seconds=60),
                prefix_key=key,
                cache_read_tokens=50000,
                log_file=None,
            )
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert len(results) == 1
        assert results[0]["run2_first_turn_cache_read_tokens"] is None
        store.close()

    def test_pending_run_is_excluded(self, tmp_path):
        # Finding 2: a still-pending run carries a placeholder cache_read_tokens
        # of 0 and must never enter a pair, or it injects a spurious cache-miss.
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        key = "pending-key"

        store.save(_make_run("r1", started_at=now, prefix_key=key))
        store.save(
            _make_run(
                "r2",
                started_at=now + timedelta(seconds=60),
                prefix_key=key,
                status="pending",
            )
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert results == []
        store.close()

    def test_pair_outside_ttl_is_excluded(self, tmp_path):
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        key = "def456"

        store.save(_make_run("r1", started_at=now, prefix_key=key))
        store.save(
            _make_run(
                "r2",
                started_at=now + timedelta(seconds=400),
                prefix_key=key,
                cache_read_tokens=9000,
            )
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert results == []
        store.close()

    def test_null_prefix_key_runs_are_excluded(self, tmp_path):
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        store.save(_make_run("r1", started_at=now, prefix_key=None))
        store.save(
            _make_run("r2", started_at=now + timedelta(seconds=60), prefix_key=None)
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert results == []
        store.close()

    def test_multiple_pairs_different_keys(self, tmp_path):
        from kew.analytics import prefix_pair_probe

        store = SqliteRunStore(str(tmp_path / "test.db"))
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        a2_log = _write_transcript(tmp_path / "a2.jsonl", first_turn_cache_read=1000)
        b2_log = _write_transcript(tmp_path / "b2.jsonl", first_turn_cache_read=2000)

        store.save(_make_run("a1", started_at=now, prefix_key="key-a"))
        store.save(
            _make_run(
                "a2",
                started_at=now + timedelta(seconds=60),
                prefix_key="key-a",
                log_file=a2_log,
            )
        )
        store.save(_make_run("b1", started_at=now, prefix_key="key-b"))
        store.save(
            _make_run(
                "b2",
                started_at=now + timedelta(seconds=90),
                prefix_key="key-b",
                log_file=b2_log,
            )
        )

        results = prefix_pair_probe(store, ttl_seconds=300)
        assert len(results) == 2
        by_key = {r["prefix_key"]: r for r in results}
        assert by_key["key-a"]["run2_first_turn_cache_read_tokens"] == 1000
        assert by_key["key-b"]["run2_first_turn_cache_read_tokens"] == 2000
        store.close()
