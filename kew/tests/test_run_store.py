"""Tests for RunRecord dataclass, RunStore protocol, and SqliteRunStore."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table

from kew.ids import MAX_ISSUE_NUM_DIGITS
from kew.run_store import (
    CURRENT_SCHEMA_VERSION,
    PR_SUBJECT_RUN_KINDS,
    LoopRecord,
    ReadSnapshotWriteError,
    RunRecord,
    RunStore,
    SqliteRunStore,
)


@pytest.fixture
def branch_history_store(tmp_path):
    with SqliteRunStore(str(tmp_path / "branch-history.db")) as store:
        yield store


def test_branch_keyset_keeps_order_after_cursor_row_deletion(branch_history_store):
    store = branch_history_store
    for run_id, day in (
        ("oldest", 5),
        ("tie-first", 6),
        ("newest", 7),
        ("tie-second", 6),
    ):
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 9, day, tzinfo=UTC),
            )
        )
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        first, cursor = store.list_runs_by_branch_after("agent/issue-7", limit=2)
    finally:
        store._conn.set_trace_callback(None)
    assert len(statements) == 1
    assert statements[0].startswith("SELECT ")
    assert not store._conn.in_transaction
    assert [row.run_id for row in first] == ["newest", "tie-second"]
    assert cursor == ("2026-09-06T00:00:00+00:00", 4)

    # The cursor remains usable even when its row disappears.
    assert store.delete("tie-second")
    for run_id, day in (("inserted-newer", 8), ("inserted-older", 4)):
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 9, day, tzinfo=UTC),
            )
        )
    second, cursor = store.list_runs_by_branch_after(
        "agent/issue-7", limit=2, after=cursor
    )
    assert [row.run_id for row in second] == ["tie-first", "oldest"]
    third, cursor = store.list_runs_by_branch_after(
        "agent/issue-7", limit=2, after=cursor
    )
    assert [row.run_id for row in third] == ["inserted-older"]
    assert store.list_runs_by_branch_after("agent/issue-7", after=cursor) == ([], None)
    assert store.list_runs_by_branch_after("agent/issue-70") == ([], None)
    assert not store._conn.in_transaction


def test_branch_keyset_preserves_stored_timestamp_and_validates_limit(
    branch_history_store,
):
    store = branch_history_store
    store.save(
        RunRecord(
            run_id="raw-timestamp",
            issue_num=7,
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 6, tzinfo=UTC),
        )
    )
    # Cursor text must retain SQLite's lexical order without datetime conversion.
    store._conn.execute("UPDATE runs SET started_at = '2026-09-06T00:00:00Z'")
    store._conn.commit()
    page, cursor = store.list_runs_by_branch_after("agent/issue-7", limit=1)
    assert [row.run_id for row in page] == ["raw-timestamp"]
    assert cursor == ("2026-09-06T00:00:00Z", 1)
    assert store.list_runs_by_branch_after("agent/issue-7", after=cursor) == ([], None)
    for limit in (0, 34):
        with pytest.raises(ValueError, match="limit must be between 1 and 33"):
            store.list_runs_by_branch_after("agent/issue-7", limit=limit)


@pytest.mark.parametrize("owner_alive", [True, False])
def test_retry_attempt_does_not_decide_live_count(tmp_path, owner_alive, monkeypatch):
    from kew.config import DispatchConfig

    monkeypatch.setattr(
        "kew.run_store.process_start_time", lambda pid: "registered birth"
    )
    from kew.graph.driver.tick import default_run_liveness

    monkeypatch.setattr("kew.graph.driver.tick._owner_alive", lambda row: owner_alive)
    owner = os.getpgrp() if owner_alive else 2**31 - 1
    attempt = 2**31 - 1 if owner_alive else os.getpgrp()
    with SqliteRunStore(str(tmp_path / "retry.db")) as store:
        store.save(
            RunRecord(
                run_id="retry",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=owner,
                owner_pid=owner,
            )
        )
        store.set_attempt_pgid("retry", attempt)
        assert store.live_run_count(
            liveness=default_run_liveness(DispatchConfig())
        ) == int(owner_alive)


def test_v50_attempt_pgid_migration_is_idempotent(tmp_path):
    db = str(tmp_path / "v49.db")
    with SqliteRunStore(db) as store:
        store.save(
            RunRecord(
                run_id="legacy",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=1234,
            )
        )
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(runs)")}
        if "attempt_pgid" in columns:
            store._conn.execute("ALTER TABLE runs DROP COLUMN attempt_pgid")
        store._conn.execute("UPDATE schema_version SET version = 49")
        store._conn.commit()

    with SqliteRunStore(db) as store:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(runs)")}
        assert "attempt_pgid" in columns
        legacy = store.get("legacy")
        assert legacy is not None
        assert legacy.pgid is None
        assert legacy.attempt_pgid == 1234
        from kew.config import DispatchConfig
        from kew.graph.driver.tick import default_run_liveness

        probe = default_run_liveness(DispatchConfig())
        assert probe(legacy) is None
        assert store.live_run_count(liveness=probe) == 1
        store.set_pgid("legacy", 5678)
        store.set_attempt_pgid("legacy", 9012)
        store._apply_v50(store._conn.cursor())
        store._conn.commit()

    with SqliteRunStore(db) as store:
        legacy = store.get("legacy")
        assert legacy is not None
        assert (legacy.pgid, legacy.attempt_pgid) == (5678, 9012)
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )


class TestRunRecordRunnerName:
    """RunRecord.runner_name field."""

    def test_run_record_runner_name(self):
        record = RunRecord(
            run_id="test-1",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            runner_name="claude-code",
        )
        assert record.runner_name == "claude-code"

    def test_save_and_get_with_runner_name(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            record = RunRecord(
                run_id="test-2",
                issue_num=1,
                branch="agent/issue-1",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                runner_name="opencode",
            )
            store.save(record)
            loaded = store.get("test-2")
            assert loaded is not None
            assert loaded.runner_name == "opencode"

    def test_runner_name_defaults_to_empty_string(self):
        record = RunRecord(
            run_id="test-3",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
        assert record.runner_name == ""

    def test_schema_migration_v2_preserves_existing_data(self, tmp_path):
        """A DB created at V1 can be migrated to V2 without data loss."""
        import sqlite3

        db_path = str(tmp_path / "v1.db")

        # Simulate a V1 database without runner_name column
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                issue_num INTEGER NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL NOT NULL DEFAULT 0.0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                error_category TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL DEFAULT 1,
                session_id TEXT,
                log_file TEXT,
                pr_url TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_issue_num ON runs(issue_num);
        """)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
        started = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO runs"
            " (run_id, issue_num, branch, status, exit_code, started_at)"
            " VALUES ('v1-run', 99, 'agent/issue-99', 'success', 0, ?)",
            (started,),
        )
        conn.commit()
        conn.close()

        # Open with SqliteRunStore — should migrate to V2
        with SqliteRunStore(db_path) as store:
            record = store.get("v1-run")
            assert record is not None
            assert record.runner_name == ""  # default for migrated rows


def _write_v1_db(db_path: str) -> None:
    """Hand-build a schema-v1 dispatch.db with one row (pre-migration state)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            issue_num INTEGER NOT NULL,
            branch TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
            error_category TEXT NOT NULL DEFAULT '',
            attempt_number INTEGER NOT NULL DEFAULT 1,
            session_id TEXT,
            log_file TEXT,
            pr_url TEXT
        );
    """)
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
    conn.execute(
        "INSERT INTO runs"
        " (run_id, issue_num, branch, status, exit_code, started_at)"
        " VALUES ('v1-run', 99, 'agent/issue-99', 'success', 0, ?)",
        ("2026-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()


def _db_version(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def test_v33_backfills_linear_era_epics_and_preserves_state(tmp_path):
    """#927/N3: opening a pre-v33 DB adds ``epic_driver_state.tracker`` and
    backfills the four complete Linear-era epics (numeric GitHub-twin item ids)
    to ``linear`` — without touching an unrelated epic's row or its tick_count.

    Simulates a v32 DB by dropping the new column and rewinding the version, so
    the real ``_apply_v33`` runs on reopen (the same path a live upgrade takes).
    """
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as store:
        store._conn.execute("ALTER TABLE epic_driver_state DROP COLUMN tracker")
        store._conn.execute(
            "INSERT INTO epic_driver_state (epic_ref, tick_count)"
            " VALUES ('driver-polish', 4)"
        )
        store._conn.execute(
            "INSERT INTO epic_driver_state (epic_ref, tick_count)"
            " VALUES ('assign-to-kew', 1)"
        )
        store._conn.execute("UPDATE schema_version SET version = 32")
        store._conn.commit()

    with SqliteRunStore(db) as store:
        backfilled = store._conn.execute(
            "SELECT tracker, tick_count FROM epic_driver_state"
            " WHERE epic_ref = 'driver-polish'"
        ).fetchone()
        assert backfilled["tracker"] == "linear"  # repaired
        assert backfilled["tick_count"] == 4  # existing state preserved
        # An epic not on the known Linear-era list is untouched (NULL): its
        # tracker is still governed by inference / the active tracker.
        other = store._conn.execute(
            "SELECT tracker FROM epic_driver_state WHERE epic_ref = 'assign-to-kew'"
        ).fetchone()
        assert other["tracker"] is None
        assert _db_version(db) == CURRENT_SCHEMA_VERSION


def test_v35_adds_loop_route_columns_and_preserves_existing_loops(tmp_path):
    """#1111: a pre-v35 DB gains ``loops.model``/``complexity``, losing no loop.

    Simulates a v34 DB by dropping the new columns and rewinding the version, so
    the real ``_apply_v35`` runs on reopen (the same path a live upgrade takes).
    A migrated loop reads both as ``""`` — "no recorded route / no recorded tier"
    — which resume treats as "fall back to the config route at the default tier",
    exactly as it behaved pre-v35.
    """
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as store:
        store.save_loop(
            LoopRecord(
                loop_id="L-old",
                source="issue",
                goal_text="#7: pre-v35 loop",
                runner_name="claude-code",
                created_at=datetime(2026, 7, 25, tzinfo=UTC),
                issue_num=7,
            )
        )
        store._conn.execute("ALTER TABLE loops DROP COLUMN model")
        store._conn.execute("ALTER TABLE loops DROP COLUMN complexity")
        store._conn.execute("UPDATE schema_version SET version = 34")
        store._conn.commit()

    with SqliteRunStore(db) as store:
        migrated = store.get_loop("L-old")
        assert migrated is not None
        assert migrated.runner_name == "claude-code"  # existing state preserved
        assert migrated.model == ""  # no recorded route -> config fallback
        assert migrated.complexity == ""  # no recorded tier -> default tier
        assert _db_version(db) == CURRENT_SCHEMA_VERSION
        # The new columns are writable on the migrated DB.
        migrated.model = "openrouter/x/cheap"
        migrated.complexity = "simple"
        store.save_loop(migrated)
        reloaded = store.get_loop("L-old")
        assert reloaded is not None
        assert reloaded.model == "openrouter/x/cheap"
        assert reloaded.complexity == "simple"


def test_continuation_record_persists_canonical_worktree_identity(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    worktree = tmp_path / "worktrees" / ".." / "preserved"

    with SqliteRunStore(db_path) as store:
        created = store.record_continuation(
            "epic:recovery",
            "SFD-179",
            "original-run",
            "agent/issue-sfd-179",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )

        assert created.state == "awaiting_continuation"
        assert created.generation == 0
        assert created.continuation_run_id is None
        assert created.worktree_identity == str(worktree.resolve())
        assert (
            store.get_continuation(
                "epic:recovery",
                "SFD-179",
                "original-run",
                "agent/issue-sfd-179",
                worktree,
            )
            == created
        )

        # The full recovery identity is part of the key: a second original run
        # gets its own durable record even when the epic and item are reused.
        other = store.record_continuation(
            "epic:recovery",
            "SFD-179",
            "different-original-run",
            "agent/issue-sfd-179",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )
        assert [
            row.original_run_id for row in store.list_continuations("epic:recovery")
        ] == [
            "different-original-run",
            "original-run",
        ]
        assert other.generation == 0


def test_v38_adds_continuation_table_without_losing_existing_state(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.reserve_epic_node_item("epic:recovery", "SFD-179", "original-run")
        store._conn.execute("DROP TABLE epic_continuations")
        store._conn.execute("UPDATE schema_version SET version = 37 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        table = store._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'epic_continuations'"
        ).fetchone()
        assert table is not None
        assert _db_version(db_path) == CURRENT_SCHEMA_VERSION
        assert [
            row.run_id for row in store.list_epic_reservations("epic:recovery")
        ] == ["original-run"]


def test_v39_backfills_one_continuation_dedup_key_without_rewriting_chain(tmp_path):
    from kew.audit import Actor, continuation_evidence, emit_continuation_evidence

    db_path = str(tmp_path / "dispatch.db")
    payload = continuation_evidence(
        phase="detection",
        epic_ref="epic:recovery",
        item_id="SFD-179",
        original_run_id="original-run",
        continuation_run_id=None,
        branch="agent/issue-sfd-179",
        worktree_identity=tmp_path / "preserved",
        generation=0,
        state="awaiting_continuation",
        outcome="crash_with_dirty_work",
    )
    with SqliteRunStore(db_path) as store:
        # A v38 kew wrote its rows before the chain-format column (v77)
        # existed, so they hash in the legacy format the v39 backfill keeps.
        store._conn.execute("DROP TRIGGER audit_chain_format_no_downgrade")
        store._conn.execute("ALTER TABLE audit_events DROP COLUMN chain_format")
        store._conn.commit()
        for _ in range(2):
            store.append_audit_event(
                timestamp="2026-08-03T00:00:00+00:00",
                event="continuation_detected",
                item_id="SFD-179",
                payload=payload,
            )
        assert verify_chain(store.read_chain_rows()).ok
        store._conn.execute("DROP INDEX idx_audit_events_dedup_key")
        store._conn.execute("ALTER TABLE audit_events DROP COLUMN dedup_key")
        store._conn.execute("UPDATE schema_version SET version = 38 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        rows = store._conn.execute(
            "SELECT id, dedup_key FROM audit_events ORDER BY id"
        ).fetchall()
        assert rows[0]["dedup_key"].startswith("continuation:v1:sha256:")
        assert rows[1]["dedup_key"] is None
        assert _db_version(db_path) == CURRENT_SCHEMA_VERSION
        assert verify_chain(store.read_chain_rows()).ok
        emit_continuation_evidence(
            store,
            "continuation_detected",
            actor=Actor("policy", "driver_reconcile"),
            phase="detection",
            epic_ref="epic:recovery",
            item_id="SFD-179",
            original_run_id="original-run",
            continuation_run_id=None,
            branch="agent/issue-sfd-179",
            worktree_identity=tmp_path / "preserved",
            generation=0,
            state="awaiting_continuation",
            outcome="crash_with_dirty_work",
        )
        assert len(store.list_audit_events(limit=None)) == 2
        assert verify_chain(store.read_chain_rows()).ok


class TestBackupBeforeMigrate:
    """Opening an older-schema DB snapshots it before applying migrations (#688)."""

    def test_backup_written_before_version_bump(self, tmp_path):
        from kew.run_store import CURRENT_SCHEMA_VERSION

        db_path = str(tmp_path / "dispatch.db")
        _write_v1_db(db_path)

        with SqliteRunStore(db_path) as store:
            assert store.get("v1-run") is not None  # migrated live DB

        backups = list(tmp_path.glob("dispatch.db.bak-pre-v*"))
        assert len(backups) == 1, f"expected one backup, got {backups}"

        backup = backups[0]
        assert f"bak-pre-v{CURRENT_SCHEMA_VERSION}-" in backup.name
        # The backup preserves the PRE-migration schema and data.
        assert _db_version(str(backup)) == 1
        bconn = sqlite3.connect(str(backup))
        try:
            row = bconn.execute(
                "SELECT run_id FROM runs WHERE run_id = 'v1-run'"
            ).fetchone()
        finally:
            bconn.close()
        assert row is not None
        # Live DB reached the current version.
        assert _db_version(db_path) == CURRENT_SCHEMA_VERSION

    def test_fresh_db_writes_no_backup(self, tmp_path):
        db_path = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db_path):
            pass
        assert list(tmp_path.glob("dispatch.db.bak-pre-v*")) == []

    def test_reopening_current_db_writes_no_backup(self, tmp_path):
        db_path = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db_path):
            pass
        with SqliteRunStore(db_path):
            pass
        assert list(tmp_path.glob("dispatch.db.bak-pre-v*")) == []

    def test_backup_failure_aborts_migration(self, tmp_path):
        db_path = str(tmp_path / "dispatch.db")
        _write_v1_db(db_path)

        with (
            patch.object(
                SqliteRunStore,
                "_snapshot_for_migrate",
                side_effect=OSError("disk full"),
            ),
            pytest.raises(RuntimeError),
        ):
            SqliteRunStore(db_path)

        # Migration must NOT have advanced the live DB past its old version.
        assert _db_version(db_path) == 1


class TestLiveRunCount:
    """live_run_count(): fleet-wide concurrency = pending runs + active epic
    reservations, deduped by issue_num (#721)."""

    @staticmethod
    def _run(issue_num: int, run_id: str, status: str = "pending") -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status=status,
            exit_code=-1 if status == "pending" else 0,
            started_at=datetime.now(UTC),
            finished_at=None if status == "pending" else datetime.now(UTC),
        )

    def test_counts_pending_and_reservations_deduped(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert store.live_run_count() == 0
            store.save(self._run(101, "r-101"))
            store.save(self._run(102, "r-102"))
            assert store.live_run_count() == 2
            # a finished run is not live
            store.save(self._run(103, "r-103", status="success"))
            assert store.live_run_count() == 2
            # an epic reservation on a fresh issue adds one
            store.reserve_epic_node("epic-x", 104, "r-104")
            assert store.live_run_count() == 3
            # a reservation that ALSO has a pending run counts once, not twice
            store.save(self._run(104, "r-104"))
            assert store.live_run_count() == 3

    def test_exclude_issue_drops_self(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(1, "r-1"))
            # The run being dispatched holds a reservation, not yet a row.
            store.reserve_epic_node("epic-x", 2, "r-2")
            assert store.live_run_count(exclude_issue=2) == 1
            # excluding an issue that is not live is a no-op
            assert store.live_run_count(exclude_issue=999) == 2

    def test_cap_releases_on_completion(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(7, "r-7"))
            assert store.live_run_count() == 1
            # same run_id flips pending -> success (upsert): no longer live
            store.save(self._run(7, "r-7", status="success"))
            assert store.live_run_count() == 0

    def test_reservation_release_drops_count(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node("epic-y", 55, "r-55")
            assert store.live_run_count() == 1
            store.release_epic_node(55, run_id="r-55")
            assert store.live_run_count() == 0

    def test_liveness_excludes_provably_dead_owner(self, tmp_path):
        """A ``pending`` row whose owner is provably dead (probe -> False) is
        dropped from the fleet count when a liveness probe is supplied (#1059):
        a killed/crashed run orphans its pending row (no in-process ``finally``),
        and without this it would permanently steal a worker slot. The dead
        orphan's REVIEW-style reservation is dropped too, so the item frees its
        slot entirely. A mutation that restores "orphan counts as live forever"
        (ignoring the probe) fails this test."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            # Two live pending runs + one orphan that ALSO holds a reservation
            # (the reviewer shape: pending row + REVIEW_EPIC_REF reservation).
            store.save(self._run(101, "r-101"))
            store.save(self._run(102, "r-102"))
            store.reserve_epic_node("review", 103, "r-103")
            store.save(self._run(103, "r-103"))
            assert store.live_run_count() == 3  # plain count: orphan steals a slot

            dead_run_id = "r-103"
            probe = lambda rec: rec.run_id != dead_run_id  # noqa: E731
            # The dead orphan is excluded from BOTH the runs and reservation
            # sides — the fleet is back to its two live real runs.
            assert store.live_run_count(liveness=probe) == 2

    def test_liveness_keeps_undeterminable_and_live_owners(self, tmp_path):
        """The probe frees a slot ONLY on a positive dead verdict: an
        undeterminable owner (probe -> None, e.g. a mid-startup run) and a live
        owner (probe -> True) both keep counting (#1059 concurrent-state
        invariant: prove dead, never infer from state-shape). A reservation with
        no run row (a claim before its child's pending row) is unprobeable and
        always counts."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(201, "r-201"))  # live
            store.save(self._run(202, "r-202"))  # undeterminable
            store.reserve_epic_node("review", 203, "r-203")  # reservation, no row
            probe = lambda rec: True if rec.run_id == "r-201" else None  # noqa: E731
            assert store.live_run_count(liveness=probe) == 3


class TestTryAdmitRun:
    """try_admit_run(): atomic worker-slot admission — count + pending-row
    insert in one transaction, closing the #721 read-then-admit overshoot
    window (#745)."""

    @staticmethod
    def _run(issue_num: int, run_id: str) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
        )

    def test_admits_below_cap_and_inserts_slot(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            admitted = store.try_admit_run(self._run(1, "r-1"), 2, exclude_issue=1)
            assert admitted is True
            # the pending row IS the slot — it now counts as live
            assert store.live_run_count() == 1
            assert store.get("r-1") is not None

    def test_rejects_at_cap_without_inserting(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(1, "r-1"))
            assert store.live_run_count() == 1
            admitted = store.try_admit_run(self._run(2, "r-2"), 1, exclude_issue=2)
            assert admitted is False
            # rejected: no slot row was written, count is unchanged
            assert store.get("r-2") is None
            assert store.live_run_count() == 1

    def test_cap_zero_or_less_always_admits(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(1, "r-1"))
            store.save(self._run(2, "r-2"))
            # cap <= 0 disables the check — unconditional insert
            assert store.try_admit_run(self._run(3, "r-3"), 0, exclude_issue=3)
            assert store.try_admit_run(self._run(4, "r-4"), -1, exclude_issue=4)
            assert store.get("r-3") is not None
            assert store.get("r-4") is not None

    def test_exclude_issue_ignores_own_reservation(self, tmp_path):
        """The run's own reserved slot must not count against it, exactly like
        the preflight (#721)."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node("epic-z", 1, "r-1")
            store.save(self._run(2, "r-2"))
            assert store.live_run_count() == 2
            # admitting issue 1 at cap 2: excluding its reservation, live = 1 < 2
            admitted = store.try_admit_run(self._run(1, "r-1"), 2, exclude_issue=1)
            assert admitted is True

    def test_exclude_issue_still_counts_its_pending_runs(self, tmp_path):
        """#2628: a second run of an already-pending issue takes its own slot —
        excluding the issue never hides a run that is already live."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run(1, "r-1"))
            store.save(self._run(2, "r-2"))
            admitted = store.try_admit_run(self._run(1, "r-1b"), 2, exclude_issue=1)
            assert admitted is False
            assert store.get("r-1b") is None

    def test_reservation_counts_toward_cap(self, tmp_path):
        """An epic reservation with no run row yet still fills a slot — the
        atomic admit UNIONs reservations exactly like live_run_count."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node("epic-z", 9, "r-9")
            admitted = store.try_admit_run(self._run(3, "r-3"), 1, exclude_issue=3)
            assert admitted is False
            assert store.get("r-3") is None

    def test_two_connection_barrier_only_one_admits_at_cap_one(self, tmp_path):
        """Two dispatchers racing at cap 1 — BEGIN IMMEDIATE serializes them so
        exactly one admits; the loser sees the winner's slot and is rejected.

        This is the invariant #721 could not hold: with a plain read-then-insert
        both contenders read live=0 and both admitted (transient overshoot)."""
        db_path = str(tmp_path / "barrier.db")
        SqliteRunStore(db_path).close()  # migrate up front so threads don't race it

        barrier = threading.Barrier(2)
        results: dict[int, bool] = {}
        errors: list[Exception] = []

        def contender(issue_num: int, run_id: str) -> None:
            store = SqliteRunStore(db_path)
            try:
                barrier.wait(timeout=5)
                results[issue_num] = store.try_admit_run(
                    self._run(issue_num, run_id), 1, exclude_issue=issue_num
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        t1 = threading.Thread(target=contender, args=(1, "r-1"))
        t2 = threading.Thread(target=contender, args=(2, "r-2"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive()
        assert errors == [], f"unexpected errors: {errors}"
        # exactly one contender admitted — never both (the overshoot #745 fixes)
        assert sorted(results.values()) == [False, True]
        # and the fleet holds at the cap: exactly one live pending row
        check = SqliteRunStore(db_path)
        try:
            assert check.live_run_count() == 1
        finally:
            check.close()


class TestItemIdKeying:
    """A3: item_id is the durable fleet-concurrency currency. GitHub rows
    backfill item_id from issue_num; non-numeric items ride the issue_num=0
    sentinel with a distinct item_id so the worker cap counts them separately.
    """

    @staticmethod
    def _run(
        run_id: str,
        *,
        issue_num: int,
        item_id: str | None = None,
        branch: str | None = None,
        status: str = "pending",
    ) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch if branch is not None else f"agent/issue-{issue_num}",
            status=status,
            exit_code=-1 if status == "pending" else 0,
            started_at=datetime.now(UTC),
            finished_at=None if status == "pending" else datetime.now(UTC),
        )

    def test_github_row_backfills_item_id_from_issue_num(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run("r-42", issue_num=42))
            loaded = store.get("r-42")
            assert loaded is not None
            assert loaded.item_id == "42"
            assert loaded.issue_num == 42

    def test_explicit_item_id_round_trips_with_sentinel_issue_num(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run("r-lin", issue_num=0, item_id="SFD-12"))
            loaded = store.get("r-lin")
            assert loaded is not None
            assert loaded.item_id == "SFD-12"
            # The 0 sentinel is preserved on the NOT NULL column.
            assert loaded.issue_num == 0

    def test_loop_record_carries_item_id(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            loop = LoopRecord(
                loop_id="L-1",
                source="issue",
                goal_text="do the thing",
                runner_name="claude-code",
                created_at=datetime.now(UTC),
                issue_num=7,
                item_id="SFD-7",
            )
            store.save_loop(loop)
            loaded = store.get_loop("L-1")
            assert loaded is not None
            # Loops store item_id as-is (no backfill; delegate sets it).
            assert loaded.item_id == "SFD-7"
            # A loop that omits item_id keeps it None (round-trip contract).
            loop2 = LoopRecord(
                loop_id="L-2",
                source="issue",
                goal_text="x",
                runner_name="claude-code",
                created_at=datetime.now(UTC),
                issue_num=8,
            )
            store.save_loop(loop2)
            reloaded = store.get_loop("L-2")
            assert reloaded is not None
            assert reloaded.item_id is None

    def test_list_by_item(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run("a", issue_num=0, item_id="SFD-1"))
            store.save(self._run("b", issue_num=0, item_id="SFD-1", status="success"))
            store.save(self._run("c", issue_num=0, item_id="SFD-2"))
            records = store.list_by_item("SFD-1")
            assert len(records) == 2
            assert {r.run_id for r in records} == {"a", "b"}
            assert store.list_by_item("SFD-9") == []

    def test_find_run_by_branch_returns_latest_exact_match(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            old = self._run("old", issue_num=5, branch="agent/issue-SFD-5")
            old.started_at = datetime(2026, 1, 1, tzinfo=UTC)
            new = self._run("new", issue_num=5, branch="agent/issue-SFD-5")
            new.started_at = datetime(2026, 6, 1, tzinfo=UTC)
            store.save(old)
            store.save(new)
            # A near-miss branch must not match (exact only).
            store.save(self._run("other", issue_num=5, branch="agent/issue-SFD-50"))
            found = store.find_run_by_branch("agent/issue-SFD-5")
            assert found is not None
            assert found.run_id == "new"
            assert store.find_run_by_branch("agent/issue-nope") is None

    def test_sum_costs_by_item(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            for run_id, item_id, cost in [
                ("a", "SFD-1", 1.0),
                ("b", "SFD-1", 2.0),
                ("c", "SFD-2", 4.0),
            ]:
                rec = self._run(run_id, issue_num=0, item_id=item_id, status="success")
                rec.finished_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
                rec.estimated_cost_usd = cost
                store.save(rec)
            since = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
            totals = store.sum_costs_by_item(["SFD-1", "SFD-2"], since=since)
            assert totals == {"SFD-1": 3.0, "SFD-2": 4.0}
            assert store.sum_costs_by_item([], since=since) == {}

    def test_live_count_keys_on_item_not_sentinel_issue_num(self, tmp_path):
        """The core A3 correctness fix: two non-numeric items both write
        issue_num=0, but their distinct item_ids must count as TWO live slots —
        keying on issue_num would collapse the whole fleet into one."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run("r1", issue_num=0, item_id="SFD-1"))
            store.save(self._run("r2", issue_num=0, item_id="SFD-2"))
            assert store.live_run_count() == 2

    def test_live_count_exclude_item_and_issue_shim(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-1", "r1")
            store.reserve_epic_node("epic-x", 42, "g")
            # exclude_item drops the matching non-numeric item's reservation.
            assert store.live_run_count(exclude_item="SFD-1") == 1
            # exclude_issue shim stringifies to match the backfilled item_id.
            assert store.live_run_count(exclude_issue=42) == 1

    def test_live_count_exclude_item_keeps_that_items_pending_runs(self, tmp_path):
        """#2628: the exclusion is the admitting run's own reserved slot, never
        another pending run of the same item — the cap counts runs."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-1", "r1")
            store.save(self._run("r1", issue_num=0, item_id="SFD-1"))
            store.save(self._run("r2", issue_num=0, item_id="SFD-1"))
            assert store.live_run_count() == 2
            assert store.live_run_count(exclude_item="SFD-1") == 2
            assert (
                store.live_run_count(exclude_item="SFD-1", liveness=lambda _r: True)
                == 2
            )

    def test_try_admit_run_exclude_item_skips_its_own_reservation(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-1", "r1")
            # SFD-1's own reservation is its slot: live = 0 < 1.
            admitted = store.try_admit_run(
                self._run("r1", issue_num=0, item_id="SFD-1"),
                1,
                exclude_item="SFD-1",
            )
            assert admitted is True

    def test_try_admit_run_cap_counts_runs_not_items(self, tmp_path):
        """#2628: two runs of one item under cap 1 — the first admits, the
        second refuses, even though both exclude their own item."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            first = store.try_admit_run(
                self._run("r1", issue_num=0, item_id="SFD-1"),
                1,
                exclude_item="SFD-1",
            )
            second = store.try_admit_run(
                self._run("r2", issue_num=0, item_id="SFD-1"),
                1,
                exclude_item="SFD-1",
            )
            assert (first, second) == (True, False)
            assert store.get("r2") is None

    def test_admission_count_with_dead_proofs_counts_runs_not_items(self, tmp_path):
        """#2628 on the in-transaction path a dead-pending proof selects: both
        live runs of SFD-1 count even though SFD-1 is excluded, its own backed
        reservation does not, a proven-dead item drops out, and an unbacked
        reservation still holds its slot."""
        from kew.run_store_liveness import DeadPending, count_admission_runs

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-1", "r1")
            store.save(self._run("r1", issue_num=0, item_id="SFD-1"))
            store.save(self._run("r2", issue_num=0, item_id="SFD-1"))
            store.reserve_epic_node_item("epic-x", "SFD-9", "dead-9")
            store.save(self._run("dead-9", issue_num=0, item_id="SFD-9"))
            store.reserve_epic_node_item("epic-x", "SFD-3", "r3")
            proof = DeadPending(store.get("dead-9"), reservation_run_id="dead-9")
            assert count_admission_runs(store, "SFD-1", [proof]) == 3
            assert count_admission_runs(store, None, [proof]) == 3

    def test_reservation_dedups_against_github_item_id(self, tmp_path):
        """An epic reservation (issue_num int) and a pending run with the
        backfilled item_id for the same GitHub issue must count ONCE."""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node("epic-x", 104, "r-104")
            assert store.live_run_count() == 1
            store.save(self._run("r-104", issue_num=104))
            assert store.live_run_count() == 1

    def test_sentinel_without_item_id_falls_back_to_run_id(self, tmp_path):
        """A 0-sentinel row with no item_id (e.g. an ``epic plan`` run) must
        NOT persist as the literal ``"0"`` — that would collapse every such row
        onto one live-count slot. It backfills from the unique run_id instead.
        """
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(self._run("plan-a", issue_num=0))
            store.save(self._run("plan-b", issue_num=0))
            assert store.get("plan-a").item_id == "plan-a"
            assert store.get("plan-b").item_id == "plan-b"
            # Two distinct sentinel rows count as two live slots, not one.
            assert store.live_run_count() == 2


class TestRunRecord:
    """RunRecord should capture all fields of a dispatch run."""

    def test_required_fields(self):
        record = RunRecord(
            run_id="run-001",
            issue_num=42,
            branch="agent/issue-42",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert record.run_id == "run-001"
        assert record.issue_num == 42
        assert record.branch == "agent/issue-42"
        assert record.status == "success"
        assert record.exit_code == 0
        assert record.started_at == datetime(2026, 1, 1, tzinfo=UTC)

    def test_default_values(self):
        record = RunRecord(
            run_id="run-002",
            issue_num=1,
            branch="agent/issue-1",
            status="pending",
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert record.finished_at is None
        assert record.duration_seconds == 0.0
        assert record.total_tokens == 0
        assert record.input_tokens == 0
        assert record.output_tokens == 0
        assert record.estimated_cost_usd == 0.0
        assert record.error_category == ""
        assert record.attempt_number == 1
        assert record.session_id is None
        assert record.log_file is None
        assert record.pr_url is None

    def test_all_fields_can_be_set(self):
        now = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)
        later = datetime(2026, 3, 17, 12, 5, 0, tzinfo=UTC)
        record = RunRecord(
            run_id="run-003",
            issue_num=7,
            branch="agent/issue-7",
            status="failure",
            exit_code=1,
            started_at=now,
            finished_at=later,
            duration_seconds=300.0,
            total_tokens=15000,
            input_tokens=10000,
            output_tokens=5000,
            estimated_cost_usd=0.50,
            error_category="transient",
            attempt_number=2,
            session_id="sess-abc",
            log_file=Path("/tmp/logs/issue-7.log"),
            pr_url="https://github.com/owner/repo/pull/42",
        )
        assert record.finished_at == later
        assert record.duration_seconds == 300.0
        assert record.total_tokens == 15000
        assert record.input_tokens == 10000
        assert record.output_tokens == 5000
        assert record.estimated_cost_usd == 0.50
        assert record.error_category == "transient"
        assert record.attempt_number == 2
        assert record.session_id == "sess-abc"
        assert record.log_file == Path("/tmp/logs/issue-7.log")
        assert record.pr_url == "https://github.com/owner/repo/pull/42"


class TestRunStoreProtocol:
    """RunStore protocol should define the expected interface."""

    def test_sqlite_store_implements_protocol(self):
        """SqliteRunStore must satisfy the RunStore protocol."""
        store: RunStore = SqliteRunStore(":memory:")
        assert isinstance(store, RunStore)


class TestSqliteRunStoreInit:
    """SqliteRunStore initialization and WAL mode."""

    def test_creates_database_file(self, tmp_path: Path):
        db_path = tmp_path / "runs.db"
        SqliteRunStore(str(db_path))
        assert db_path.exists()

    def test_wal_mode_enabled(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "runs.db"
        SqliteRunStore(str(db_path))
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_schema_version_table_exists(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "runs.db"
        SqliteRunStore(str(db_path))
        conn = sqlite3.connect(str(db_path))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        assert "schema_version" in tables
        assert "runs" in tables

    def test_in_memory_database(self):
        store = SqliteRunStore(":memory:")
        # Should work without errors
        assert store.list_all() == []

    def test_check_same_thread_false(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "runs.db"), check_same_thread=False)
        assert store.list_all() == []


class TestSqliteRunStoreSave:
    """SqliteRunStore.save() persists RunRecord."""

    def _make_record(self, **overrides: object) -> RunRecord:
        defaults: dict[str, object] = {
            "run_id": "run-100",
            "issue_num": 42,
            "branch": "agent/issue-42",
            "status": "success",
            "exit_code": 0,
            "started_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        defaults.update(overrides)
        return RunRecord(**defaults)  # type: ignore[arg-type]

    def test_save_and_get(self):
        store = SqliteRunStore(":memory:")
        record = self._make_record()
        store.save(record)
        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.run_id == "run-100"
        assert retrieved.issue_num == 42
        assert retrieved.branch == "agent/issue-42"
        assert retrieved.status == "success"
        assert retrieved.exit_code == 0

    def test_transport_identity_round_trips_without_config_contents(self):
        store = SqliteRunStore(":memory:")
        secret_config = "provider_api_key = 'do-not-persist'"
        record = self._make_record(
            transport_mode="switchyard",
            transport_endpoint=(
                "https://user:token@switchyard.example:8443/private?secret=x"
            ),
            transport_route="switchyard/passthrough",
            transport_managed=True,
            transport_expected_version="6ff491a",
            transport_observed_version="6ff491a",
            transport_config_sha256="a" * 64,
            transport_preflight_verdict="ready",
        )

        store.save(record)

        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.transport_mode == "switchyard"
        assert retrieved.transport_endpoint == "https://switchyard.example:8443"
        assert retrieved.transport_route == "switchyard/passthrough"
        assert retrieved.transport_managed is True
        assert retrieved.transport_expected_version == "6ff491a"
        assert retrieved.transport_observed_version == "6ff491a"
        assert retrieved.transport_config_sha256 == "a" * 64
        assert retrieved.transport_preflight_verdict == "ready"
        persisted = store._conn.serialize().decode(errors="ignore")
        assert "user:token" not in persisted
        assert "private?secret" not in persisted
        assert secret_config not in persisted

    def test_save_preserves_initial_transport_admission_evidence(self):
        store = SqliteRunStore(":memory:")
        admitted = self._make_record(
            status="pending",
            transport_mode="switchyard",
            transport_endpoint="https://switchyard.example:8443",
            transport_route="switchyard/passthrough",
            transport_managed=True,
            transport_expected_version="0.1.0",
            transport_observed_version="0.1.0",
            transport_config_sha256="a" * 64,
            transport_preflight_verdict="ready",
        )
        store.save(admitted)

        store.save(self._make_record(status="success"))

        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.status == "success"
        assert retrieved.transport_mode == "switchyard"
        assert retrieved.transport_endpoint == "https://switchyard.example:8443"
        assert retrieved.transport_route == "switchyard/passthrough"
        assert retrieved.transport_managed is True
        assert retrieved.transport_expected_version == "0.1.0"
        assert retrieved.transport_observed_version == "0.1.0"
        assert retrieved.transport_config_sha256 == "a" * 64
        assert retrieved.transport_preflight_verdict == "ready"

    def test_managed_transport_admission_is_atomic_and_single_use(self):
        store = SqliteRunStore(":memory:")
        store.save(
            self._make_record(
                status="pending",
                transport_mode="switchyard",
                transport_managed=True,
                transport_preflight_verdict="managed_pending",
            )
        )

        assert store.record_managed_transport_admission(
            "run-100", observed_version="0.1.0", config_sha256="b" * 64
        )
        assert not store.record_managed_transport_admission(
            "run-100", observed_version="other", config_sha256="c" * 64
        )
        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.transport_observed_version == "0.1.0"
        assert retrieved.transport_config_sha256 == "b" * 64
        assert retrieved.transport_preflight_verdict == "ready"

    def test_managed_transport_admission_cannot_overwrite_terminal_run(self):
        store = SqliteRunStore(":memory:")
        store.save(
            self._make_record(
                status="killed",
                transport_mode="switchyard",
                transport_managed=True,
                transport_preflight_verdict="managed_pending",
            )
        )

        assert not store.record_managed_transport_admission(
            "run-100", observed_version="0.1.0", config_sha256="b" * 64
        )

    def test_transport_identity_exports_to_csv(self):
        import csv
        import io

        from kew.run_store import export_runs_csv

        record = self._make_record(
            transport_mode="switchyard",
            transport_endpoint="https://switchyard.example",
            transport_route="switchyard/passthrough",
            transport_preflight_verdict="ready",
        )

        row = next(csv.DictReader(io.StringIO(export_runs_csv([record]))))
        assert row["transport_mode"] == "switchyard"
        assert row["transport_endpoint"] == "https://switchyard.example"
        assert row["transport_route"] == "switchyard/passthrough"
        assert row["transport_preflight_verdict"] == "ready"

    def test_transport_migration_preserves_old_rows_as_direct(self, tmp_path):
        db_path = tmp_path / "pre-transport.db"
        record = self._make_record(run_id="historical")
        with SqliteRunStore(str(db_path)) as store:
            store.save(record)
            for column in (
                "transport_mode",
                "transport_endpoint",
                "transport_route",
                "transport_managed",
                "transport_expected_version",
                "transport_observed_version",
                "transport_config_sha256",
                "transport_preflight_verdict",
            ):
                store._conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
            store._conn.execute("UPDATE schema_version SET version = 36 WHERE id = 1")
            store._conn.commit()

        with SqliteRunStore(str(db_path)) as migrated:
            historical = migrated.get("historical")
            assert historical is not None
            assert historical.transport_mode == "direct"
            assert historical.transport_managed is False
            assert historical.transport_endpoint is None
            assert historical.transport_route is None
            assert historical.transport_expected_version is None
            assert historical.transport_observed_version is None
            assert historical.transport_config_sha256 is None
            assert historical.transport_preflight_verdict is None

    def test_save_preserves_all_fields(self):
        store = SqliteRunStore(":memory:")
        now = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)
        later = datetime(2026, 3, 17, 12, 5, 0, tzinfo=UTC)
        record = self._make_record(
            finished_at=later,
            started_at=now,
            duration_seconds=300.0,
            total_tokens=25000,  # input + output + cache write + cache read
            input_tokens=10000,
            output_tokens=5000,
            cache_creation_tokens=2000,
            cache_read_tokens=8000,
            estimated_cost_usd=0.50,
            error_category="transient",
            attempt_number=2,
            session_id="sess-abc",
            log_file=Path("/tmp/logs/issue-42.log"),
            pr_url="https://github.com/owner/repo/pull/42",
        )
        store.save(record)
        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.finished_at == later
        assert retrieved.duration_seconds == 300.0
        assert retrieved.total_tokens == 25000
        assert retrieved.input_tokens == 10000
        assert retrieved.output_tokens == 5000
        assert retrieved.cache_creation_tokens == 2000
        assert retrieved.cache_read_tokens == 8000
        assert retrieved.estimated_cost_usd == 0.50
        assert retrieved.error_category == "transient"
        assert retrieved.attempt_number == 2
        assert retrieved.session_id == "sess-abc"
        assert retrieved.log_file == Path("/tmp/logs/issue-42.log")
        assert retrieved.pr_url == "https://github.com/owner/repo/pull/42"

    def test_save_updates_existing_record(self):
        store = SqliteRunStore(":memory:")
        record = self._make_record(status="pending")
        store.save(record)
        record.status = "success"
        record.exit_code = 0
        store.save(record)
        retrieved = store.get("run-100")
        assert retrieved is not None
        assert retrieved.status == "success"
        # Should still be only one record
        assert len(store.list_all()) == 1

    def test_get_nonexistent_returns_none(self):
        store = SqliteRunStore(":memory:")
        assert store.get("nonexistent") is None

    def test_median_cost_for_bucket(self):
        store = SqliteRunStore(":memory:")
        # 3 completed opus/medium runs: costs 1.0, 2.0, 9.0 -> median 2.0
        for i, cost in enumerate((1.0, 2.0, 9.0)):
            store.save(
                self._make_record(
                    run_id=f"m{i}",
                    status="success",
                    model="claude-opus-4-6",
                    complexity="medium",
                    estimated_cost_usd=cost,
                )
            )
        # a killed run in the same bucket must be excluded
        store.save(
            self._make_record(
                run_id="k",
                status="killed",
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=99.0,
            )
        )
        # a run in a different bucket must be excluded
        store.save(
            self._make_record(
                run_id="h",
                status="success",
                model="claude-haiku-4-5",
                complexity="medium",
                estimated_cost_usd=50.0,
            )
        )
        med, count = store.median_cost_for_bucket("claude-opus-4-6", "medium")
        assert med == 2.0
        assert count == 3

    def test_median_cost_for_bucket_empty(self):
        store = SqliteRunStore(":memory:")
        assert store.median_cost_for_bucket("nope", "medium") == (None, 0)

    def test_sum_costs_by_issue_window_inclusive_and_grouped(self):
        """Aggregate query backing kew.service.epics._drain_costs (#458
        review): windowing is inclusive on both ends, results are grouped
        per issue_num, and runs with no finished_at (still pending) are
        excluded regardless of the window."""
        store = SqliteRunStore(":memory:")
        store.save(
            self._make_record(
                run_id="a-in",
                issue_num=1,
                finished_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                estimated_cost_usd=1.0,
            )
        )
        # Exactly on the since/until boundaries -> both inclusive.
        store.save(
            self._make_record(
                run_id="a-since-edge",
                issue_num=1,
                finished_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                estimated_cost_usd=2.0,
            )
        )
        store.save(
            self._make_record(
                run_id="a-until-edge",
                issue_num=1,
                finished_at=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC),
                estimated_cost_usd=4.0,
            )
        )
        # Before the window -> excluded.
        store.save(
            self._make_record(
                run_id="a-before",
                issue_num=1,
                finished_at=datetime(2025, 12, 31, tzinfo=UTC),
                estimated_cost_usd=99.0,
            )
        )
        # After the window -> excluded.
        store.save(
            self._make_record(
                run_id="a-after",
                issue_num=1,
                finished_at=datetime(2026, 1, 2, tzinfo=UTC),
                estimated_cost_usd=99.0,
            )
        )
        # Still pending (no finished_at) -> excluded regardless of window.
        store.save(
            self._make_record(
                run_id="a-pending",
                issue_num=1,
                finished_at=None,
                estimated_cost_usd=50.0,
            )
        )
        # A different issue in the window and requested -> its own bucket.
        store.save(
            self._make_record(
                run_id="b-in",
                issue_num=2,
                finished_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                estimated_cost_usd=5.0,
            )
        )
        # A third issue in the window but NOT requested -> must not leak in.
        store.save(
            self._make_record(
                run_id="c-in",
                issue_num=3,
                finished_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                estimated_cost_usd=7.0,
            )
        )

        since = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
        until = datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC).isoformat()

        totals = store.sum_costs_by_issue([1, 2], since=since, until=until)

        assert totals == {1: 7.0, 2: 5.0}

    def test_sum_costs_by_issue_until_none_is_open_ended(self):
        store = SqliteRunStore(":memory:")
        store.save(
            self._make_record(
                run_id="a1",
                issue_num=1,
                finished_at=datetime(2026, 1, 1, tzinfo=UTC),
                estimated_cost_usd=1.0,
            )
        )
        store.save(
            self._make_record(
                run_id="a2",
                issue_num=1,
                finished_at=datetime(2027, 1, 1, tzinfo=UTC),
                estimated_cost_usd=2.0,
            )
        )
        since = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

        assert store.sum_costs_by_issue([1], since=since) == {1: 3.0}

    def test_sum_costs_by_issue_issue_without_runs_is_absent(self):
        store = SqliteRunStore(":memory:")
        since = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
        assert store.sum_costs_by_issue([999], since=since) == {}

    def test_sum_costs_by_issue_empty_issue_list_short_circuits(self):
        store = SqliteRunStore(":memory:")
        since = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
        assert store.sum_costs_by_issue([], since=since) == {}

    def test_get_meta_absent_and_present(self):
        store = SqliteRunStore(":memory:")
        assert store.get_meta("scheduler_heartbeat") is None
        store._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("scheduler_heartbeat", "2026-06-25T00:00:00+00:00"),
        )
        assert store.get_meta("scheduler_heartbeat") == "2026-06-25T00:00:00+00:00"

    def test_audit_parse_report_counts_malformed_payload(self):
        store = SqliteRunStore(":memory:")
        # one good event (valid JSON object payload)
        store.append_audit_event(
            timestamp="2026-06-25T00:00:00+00:00",
            event="dispatch_completed",
            actor_type="human",
            actor_id="alice",
            payload={"ok": True},
        )
        # one malformed payload, inserted raw (bypasses the typed writer) in
        # the chain format a current writer stores after the first row
        store._conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, payload,"
            " chain_format) VALUES (?, ?, ?, ?, 2)",
            ("2026-06-25T00:00:01+00:00", "dispatch_completed", "human", "{not json"),
        )
        # one envelope-malformed row: null actor_type
        store._conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, payload,"
            " chain_format) VALUES (?, ?, ?, ?, 2)",
            ("2026-06-25T00:00:02+00:00", "dispatch_completed", None, '{"ok": true}'),
        )
        total, malformed = store.audit_parse_report()
        assert total == 3
        assert malformed == 2

    def test_audit_parse_report_empty(self):
        store = SqliteRunStore(":memory:")
        assert store.audit_parse_report() == (0, 0)


class TestSqliteRunStoreList:
    """SqliteRunStore listing operations."""

    def _make_record(
        self, run_id: str, issue_num: int, **overrides: object
    ) -> RunRecord:
        defaults: dict[str, object] = {
            "run_id": run_id,
            "issue_num": issue_num,
            "branch": f"agent/issue-{issue_num}",
            "status": "success",
            "exit_code": 0,
            "started_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        defaults.update(overrides)
        return RunRecord(**defaults)  # type: ignore[arg-type]

    def test_list_all_empty(self):
        store = SqliteRunStore(":memory:")
        assert store.list_all() == []

    def test_list_all_returns_all_records(self):
        store = SqliteRunStore(":memory:")
        store.save(self._make_record("run-1", 1))
        store.save(self._make_record("run-2", 2))
        store.save(self._make_record("run-3", 3))
        records = store.list_all()
        assert len(records) == 3

    def test_list_by_issue(self):
        store = SqliteRunStore(":memory:")
        store.save(self._make_record("run-1", 42))
        store.save(self._make_record("run-2", 42))
        store.save(self._make_record("run-3", 99))
        records = store.list_by_issue(42)
        assert len(records) == 2
        assert all(r.issue_num == 42 for r in records)

    def test_list_by_issue_empty(self):
        store = SqliteRunStore(":memory:")
        store.save(self._make_record("run-1", 42))
        records = store.list_by_issue(99)
        assert records == []


class TestSqliteRunStoreDelete:
    """SqliteRunStore.delete() removes records."""

    def _make_record(self, run_id: str = "run-del") -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_delete_existing_returns_true(self):
        store = SqliteRunStore(":memory:")
        store.save(self._make_record())
        assert store.delete("run-del") is True
        assert store.get("run-del") is None

    def test_delete_nonexistent_returns_false(self):
        store = SqliteRunStore(":memory:")
        assert store.delete("nonexistent") is False

    def test_delete_does_not_affect_other_records(self):
        store = SqliteRunStore(":memory:")
        store.save(self._make_record("run-1"))
        store.save(self._make_record("run-2"))
        store.delete("run-1")
        assert store.get("run-2") is not None
        assert len(store.list_all()) == 1


class TestSqliteRunStorePersistence:
    """SqliteRunStore persists data across instances."""

    def test_data_survives_reopen(self, tmp_path: Path):
        db_path = str(tmp_path / "runs.db")
        store1 = SqliteRunStore(db_path)
        store1.save(
            RunRecord(
                run_id="persist-1",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        del store1

        store2 = SqliteRunStore(db_path)
        record = store2.get("persist-1")
        assert record is not None
        assert record.issue_num == 7


@pytest.mark.usefixtures("process_table")
class TestCleanupStalePending:
    """SqliteRunStore.cleanup_stale_pending() marks stale pending records as failed."""

    def _make_pending(
        self, run_id: str, issue_num: int, started_at: datetime
    ) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="pending",
            exit_code=0,
            started_at=started_at,
            pgid=2**31 - 1,
            owner_pid=2**31 - 1,
            owner_start_time=BIRTH,
            attempt_pgid=2**31 - 2,
        )

    def test_stale_pending_is_marked_as_failure(self):
        """A pending record older than the threshold is marked failure."""
        store = SqliteRunStore(":memory:")
        old_started_at = datetime.now(UTC) - timedelta(hours=25)
        store.save(self._make_pending("run-stale", 1, old_started_at))

        count = store.cleanup_stale_pending(max_age_hours=24.0)

        assert count == 1
        record = store.get("run-stale")
        assert record is not None
        assert record.status == "failure"
        assert record.error_category == "stale_pending"
        assert record.finished_at is not None

    def test_fresh_pending_is_not_touched(self):
        """A pending record newer than the threshold is left unchanged."""
        store = SqliteRunStore(":memory:")
        recent_started_at = datetime.now(UTC) - timedelta(hours=1)
        store.save(self._make_pending("run-fresh", 2, recent_started_at))

        count = store.cleanup_stale_pending(max_age_hours=24.0)

        assert count == 0
        record = store.get("run-fresh")
        assert record is not None
        assert record.status == "pending"

    def test_non_pending_records_are_not_touched(self):
        """Success and failure records are never modified."""
        store = SqliteRunStore(":memory:")
        old_time = datetime.now(UTC) - timedelta(hours=48)
        store.save(
            RunRecord(
                run_id="run-success",
                issue_num=3,
                branch="agent/issue-3",
                status="success",
                exit_code=0,
                started_at=old_time,
            )
        )
        store.save(
            RunRecord(
                run_id="run-failure",
                issue_num=4,
                branch="agent/issue-4",
                status="failure",
                exit_code=1,
                started_at=old_time,
            )
        )

        count = store.cleanup_stale_pending(max_age_hours=24.0)

        assert count == 0
        assert store.get("run-success").status == "success"  # type: ignore[union-attr]
        assert store.get("run-failure").status == "failure"  # type: ignore[union-attr]

    def test_returns_count_of_cleaned_records(self):
        """Returns the number of records that were cleaned up."""
        store = SqliteRunStore(":memory:")
        old_time = datetime.now(UTC) - timedelta(hours=30)
        store.save(self._make_pending("run-stale-1", 10, old_time))
        store.save(self._make_pending("run-stale-2", 11, old_time))
        store.save(self._make_pending("run-stale-3", 12, old_time))
        recent_time = datetime.now(UTC) - timedelta(hours=1)
        store.save(self._make_pending("run-fresh", 13, recent_time))

        count = store.cleanup_stale_pending(max_age_hours=24.0)

        assert count == 3

    def test_empty_store_returns_zero(self):
        """No records → returns 0."""
        store = SqliteRunStore(":memory:")
        assert store.cleanup_stale_pending() == 0

    def test_custom_threshold(self):
        """max_age_hours is respected: a 2-hour-old record is stale at 1h threshold."""
        store = SqliteRunStore(":memory:")
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
        store.save(self._make_pending("run-custom", 20, two_hours_ago))

        count = store.cleanup_stale_pending(max_age_hours=1.0)

        assert count == 1
        assert store.get("run-custom").status == "failure"  # type: ignore[union-attr]

    def test_retries_under_momentary_write_contention(self, tmp_path):
        """The age-gated sweep is the fleet's backstop for reclaiming a crashed
        run's stolen worker slot; it must NOT silently return 0 when the DB is
        momentarily busy under a peer's concurrent write (#1059). A peer holding
        the write lock briefly is waited/retried through, and the orphan IS
        reclaimed — never a silent no-op that leaves the slot pinned."""
        import time as _time

        db_path = str(tmp_path / "busy.db")
        with SqliteRunStore(db_path) as seed:
            old = datetime.now(UTC) - timedelta(hours=30)
            seed.save(self._make_pending("run-orphan", 1, old))

        # A peer connection grabs the RESERVED write lock and holds it, so the
        # sweep's BEGIN IMMEDIATE contends the moment it starts.
        blocker = SqliteRunStore(db_path)
        blocker._conn.execute("BEGIN IMMEDIATE")

        result: dict[str, int] = {}
        errors: list[Exception] = []

        def _sweep() -> None:
            store = SqliteRunStore(db_path)
            try:
                result["count"] = store.cleanup_stale_pending(max_age_hours=24.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        t = threading.Thread(target=_sweep)
        t.start()
        _time.sleep(0.3)  # the sweep is now blocked on the peer's write lock
        blocker._conn.commit()  # release: the sweep's BEGIN IMMEDIATE proceeds
        blocker.close()
        t.join(timeout=10)

        assert not t.is_alive()
        assert errors == [], f"sweep raised instead of waiting it out: {errors}"
        assert result["count"] == 1  # reclaimed the orphan, NOT a silent 0
        with SqliteRunStore(db_path) as store:
            assert store.get("run-orphan").status == "failure"  # type: ignore[union-attr]

    def test_finished_at_is_set_on_cleaned_records(self):
        """Cleaned records get a non-null finished_at timestamp."""
        store = SqliteRunStore(":memory:")
        old_time = datetime.now(UTC) - timedelta(hours=25)
        store.save(self._make_pending("run-ts", 30, old_time))

        before = datetime.now(UTC)
        store.cleanup_stale_pending(max_age_hours=24.0)
        after = datetime.now(UTC)

        record = store.get("run-ts")
        assert record is not None
        assert record.finished_at is not None
        assert before <= record.finished_at <= after


class TestRunRecordModel:
    def test_model_field_default(self):
        record = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
        )
        assert record.model == ""

    def test_model_field_set(self):
        record = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            model="claude-sonnet-4",
        )
        assert record.model == "claude-sonnet-4"


class TestListPending:
    def test_list_pending_returns_only_pending(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        now = datetime.now(UTC)

        pending = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=now,
            model="claude-sonnet-4",
        )
        success = RunRecord(
            run_id="r2",
            issue_num=2,
            branch="b2",
            status="success",
            exit_code=0,
            started_at=now,
            model="claude-opus-4",
        )
        store.save(pending)
        store.save(success)

        result = store.list_pending()
        assert len(result) == 1
        assert result[0].run_id == "r1"
        assert result[0].model == "claude-sonnet-4"

    def test_list_pending_empty(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        assert store.list_pending() == []


class TestModelPersistence:
    def test_model_round_trips_through_sqlite(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        now = datetime.now(UTC)

        record = RunRecord(
            run_id="r1",
            issue_num=1,
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=now,
            model="claude-opus-4",
            runner_name="claude-code",
        )
        store.save(record)

        loaded = store.get("r1")
        assert loaded is not None
        assert loaded.model == "claude-opus-4"
        assert loaded.runner_name == "claude-code"


def test_run_record_title_field():
    r = RunRecord(
        run_id="t1",
        issue_num=1,
        branch="b",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        title="Fix bug",
    )
    assert r.title == "Fix bug"


def test_title_round_trips_through_sqlite(tmp_path):
    db = str(tmp_path / "test.db")
    store = SqliteRunStore(db)
    r = RunRecord(
        run_id="t1",
        issue_num=1,
        branch="b",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        title="My title",
    )
    store.save(r)
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.title == "My title"
    store.close()


def test_compound_index_exists_via_v4_migration(tmp_path):
    """Schema migration from v4 → v5 creates compound index on (status, started_at)."""
    import sqlite3

    from kew.run_store import SqliteRunStore

    # Seed a v4 database so the v4→v5 migration path is exercised
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        INSERT INTO schema_version (id, version) VALUES (1, 4);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            issue_num INTEGER NOT NULL,
            branch TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
            error_category TEXT NOT NULL DEFAULT '',
            attempt_number INTEGER NOT NULL DEFAULT 1,
            session_id TEXT,
            log_file TEXT,
            pr_url TEXT,
            runner_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_runs_issue_num ON runs(issue_num);
        """
    )
    conn.close()

    # Open store — triggers v4→v5 migration
    store = SqliteRunStore(db)

    conn = sqlite3.connect(db)
    # Verify index exists with correct columns
    cols = conn.execute("PRAGMA index_info('idx_runs_status_started')").fetchall()
    col_names = [row[2] for row in cols]
    assert col_names == ["status", "started_at"]

    # Verify schema version bumped to at least 6 (V6 adds complexity column)
    version = conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    assert version >= 6

    # Verify the complexity column was added by V6 migration
    col_names_all = [
        row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    ]
    assert "complexity" in col_names_all

    conn.close()
    store.close()


def test_cache_token_columns_added_via_v11_migration(tmp_path):
    """A pre-V11 DB gains cache_creation/read columns (default 0) and round-trips."""
    import sqlite3
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    # Seed a V10-era runs table lacking the cache columns, plus one legacy row.
    conn.executescript(
        """
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        INSERT INTO schema_version (id, version) VALUES (1, 10);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            issue_num INTEGER NOT NULL,
            branch TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_seconds REAL NOT NULL DEFAULT 0.0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
            error_category TEXT NOT NULL DEFAULT '',
            attempt_number INTEGER NOT NULL DEFAULT 1,
            session_id TEXT,
            log_file TEXT,
            pr_url TEXT,
            runner_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            complexity TEXT NOT NULL DEFAULT '',
            pgid INTEGER,
            actor_type TEXT,
            actor_id TEXT,
            repo TEXT,
            endpoint TEXT,
            policy_hash TEXT,
            pr_state TEXT,
            pr_merged_at TEXT,
            review_cycles INTEGER,
            diff_additions INTEGER,
            diff_deletions INTEGER,
            diff_files INTEGER
        );
        INSERT INTO runs (run_id, issue_num, branch, status, exit_code, started_at)
        VALUES ('legacy', 1, 'b', 'success', 0, '2026-01-01T00:00:00+00:00');
        """
    )
    conn.close()

    store = SqliteRunStore(db)  # triggers migration to V12

    # Legacy row reads back with cache tokens defaulted to 0.
    legacy = store.get("legacy")
    assert legacy is not None
    assert legacy.cache_creation_tokens == 0
    assert legacy.cache_read_tokens == 0

    # A new save persists cache tokens.
    store.save(
        RunRecord(
            run_id="fresh",
            issue_num=2,
            branch="b2",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 6, 24, tzinfo=UTC),
            cache_creation_tokens=111,
            cache_read_tokens=222,
        )
    )
    fresh = store.get("fresh")
    assert fresh is not None
    assert fresh.cache_creation_tokens == 111
    assert fresh.cache_read_tokens == 222
    store.close()

    conn = sqlite3.connect(db)
    version = conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    assert version >= 11
    cols = [row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    assert "cache_creation_tokens" in cols
    assert "cache_read_tokens" in cols
    conn.close()


class TestSqliteBusyRetry:
    """SqliteRunStore retries on SQLITE_BUSY errors."""

    def _make_record(self, run_id: str = "run-busy") -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def test_save_retries_on_busy_then_succeeds(self, tmp_path):
        """save() retries when SQLITE_BUSY is raised, then succeeds."""
        store = SqliteRunStore(str(tmp_path / "busy.db"))
        record = self._make_record()

        original_execute = store._execute
        call_count = 0

        def flaky_execute(sql, params=None):
            nonlocal call_count
            if "INSERT" in sql:
                call_count += 1
                if call_count <= 2:
                    raise sqlite3.OperationalError("database is locked")
            return original_execute(sql, params)

        with patch.object(store, "_execute", side_effect=flaky_execute):
            store.save(record)

        loaded = store.get("run-busy")
        assert loaded is not None
        assert loaded.run_id == "run-busy"

    def test_save_gives_up_after_max_retries(self, tmp_path):
        """save() raises after exhausting retries."""
        from kew.run_store import _BUSY_MAX_RETRIES

        store = SqliteRunStore(str(tmp_path / "busy2.db"))
        record = self._make_record()
        mock_execute = MagicMock(
            side_effect=sqlite3.OperationalError("database is locked"),
        )

        with (
            patch.object(store, "_execute", mock_execute),
            patch.object(store, "busy_sleep"),
            pytest.raises(sqlite3.OperationalError, match="database is locked"),
        ):
            store.save(record)

        assert mock_execute.call_count == _BUSY_MAX_RETRIES + 1


class TestSqliteConcurrentWrites:
    """Concurrent writers using separate store instances must not raise SQLITE_BUSY.

    In production, the orchestrator and worker processes each open their own
    database connection. SQLite WAL mode allows concurrent writes from multiple
    connections by serializing them internally. This class verifies that WAL
    mode prevents SQLITE_BUSY from reaching callers.

    Note: sharing a single sqlite3 Connection across threads is not safe (see
    CPython docs). Each thread must open its own SqliteRunStore (connection).
    """

    def _make_record(self, run_id: str, issue_num: int) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 3, 25, 12, 0, 0, tzinfo=UTC),
        )

    def test_two_connections_write_without_busy_error(self, tmp_path: Path):
        """Two threads each with their own connection write without SQLITE_BUSY."""
        db_path = str(tmp_path / "concurrent.db")
        # Create schema in advance so both threads skip migration
        init_store = SqliteRunStore(db_path)
        init_store.close()

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(run_id: str, issue_num: int) -> None:
            # Each thread opens its own connection (production pattern)
            store = SqliteRunStore(db_path)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                errors.append(RuntimeError("Barrier timeout"))
                return
            try:
                record = self._make_record(run_id, issue_num)
                store.save(record)
            except Exception as exc:
                errors.append(exc)
            finally:
                store.close()

        t1 = threading.Thread(target=worker, args=("concurrent-1", 101))
        t2 = threading.Thread(target=worker, args=("concurrent-2", 102))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive(), "Thread t1 still alive after timeout"
        assert not t2.is_alive(), "Thread t2 still alive after timeout"

        assert errors == [], f"Unexpected errors during concurrent writes: {errors}"

        # Verify both records were persisted
        store2 = SqliteRunStore(db_path)
        run_ids = {r.run_id for r in store2.list_all()}
        store2.close()
        assert "concurrent-1" in run_ids
        assert "concurrent-2" in run_ids

    def test_many_connections_write_without_busy_error(self, tmp_path: Path):
        """Five concurrent writers each with their own connection all succeed."""
        db_path = str(tmp_path / "concurrent_many.db")
        init_store = SqliteRunStore(db_path)
        init_store.close()

        num_threads = 5
        errors: list[Exception] = []
        barrier = threading.Barrier(num_threads)

        def worker(idx: int) -> None:
            store = SqliteRunStore(db_path)
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                errors.append(RuntimeError("Barrier timeout"))
                return
            try:
                record = self._make_record(f"run-{idx}", idx)
                store.save(record)
            except Exception as exc:
                errors.append(exc)
            finally:
                store.close()

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            assert not t.is_alive(), f"Thread {t.name} still alive after timeout"

        assert errors == [], f"Unexpected errors: {errors}"

        store2 = SqliteRunStore(db_path)
        assert len(store2.list_all()) == num_threads
        store2.close()


def _make_record(
    run_id: str,
    *,
    issue_num: int = 1,
    status: str = "success",
    exit_code: int = 0,
    runner_name: str = "claude-code",
    model: str = "claude-sonnet-4-6",
    complexity: str = "medium",
    duration_seconds: float = 60.0,
    estimated_cost_usd: float = 0.10,
    error_category: str = "",
) -> "RunRecord":
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        duration_seconds=duration_seconds,
        estimated_cost_usd=estimated_cost_usd,
        runner_name=runner_name,
        model=model,
        complexity=complexity,
        error_category=error_category,
    )


class TestRunStoreAnalytics:
    """SqliteRunStore.analytics() computes aggregate metrics from stored runs."""

    def test_analytics_overall_success_rate(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(_make_record("r1", status="success", exit_code=0))
            store.save(_make_record("r2", status="success", exit_code=0))
            store.save(
                _make_record(
                    "r3", status="failure", exit_code=1, error_category="transient"
                )
            )

            stats = store.analytics()

        assert stats["overall"]["total"] == 3
        assert stats["overall"]["success"] == 2
        assert abs(stats["overall"]["success_rate"] - 2 / 3) < 0.001

    def test_analytics_excludes_pending_runs(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(_make_record("r1", status="success", exit_code=0))
            store.save(_make_record("r2", status="pending", exit_code=-1))

            stats = store.analytics()

        # pending runs should not be counted
        assert stats["overall"]["total"] == 1

    def test_analytics_by_complexity(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                _make_record(
                    "r1", complexity="low", status="success", duration_seconds=30.0
                )
            )
            store.save(
                _make_record(
                    "r2",
                    complexity="low",
                    status="failure",
                    exit_code=1,
                    error_category="transient",
                    duration_seconds=40.0,
                )
            )
            store.save(
                _make_record(
                    "r3", complexity="high", status="success", duration_seconds=120.0
                )
            )

            stats = store.analytics()

        by_complexity = stats["by_complexity"]
        assert "low" in by_complexity
        assert by_complexity["low"]["total"] == 2
        assert by_complexity["low"]["success"] == 1
        assert abs(by_complexity["low"]["avg_duration_seconds"] - 35.0) < 0.1
        assert "high" in by_complexity
        assert by_complexity["high"]["total"] == 1

    def test_analytics_by_runner(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                _make_record("r1", runner_name="claude-code", estimated_cost_usd=0.10)
            )
            store.save(
                _make_record("r2", runner_name="claude-code", estimated_cost_usd=0.20)
            )
            store.save(
                _make_record("r3", runner_name="opencode", estimated_cost_usd=0.05)
            )

            stats = store.analytics()

        by_runner = stats["by_runner"]
        assert "claude-code" in by_runner
        assert by_runner["claude-code"]["total"] == 2
        assert abs(by_runner["claude-code"]["avg_cost_usd"] - 0.15) < 0.001
        assert "opencode" in by_runner
        assert by_runner["opencode"]["total"] == 1

    def test_analytics_by_model(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                _make_record("r1", model="claude-sonnet-4-6", estimated_cost_usd=0.10)
            )
            store.save(
                _make_record("r2", model="claude-opus-4-6", estimated_cost_usd=0.50)
            )

            stats = store.analytics()

        by_model = stats["by_model"]
        assert "claude-sonnet-4-6" in by_model
        assert "claude-opus-4-6" in by_model
        assert abs(by_model["claude-opus-4-6"]["avg_cost_usd"] - 0.50) < 0.001

    def test_analytics_failure_categories(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                _make_record(
                    "r1", status="failure", exit_code=1, error_category="transient"
                )
            )
            store.save(
                _make_record(
                    "r2", status="failure", exit_code=1, error_category="transient"
                )
            )
            store.save(
                _make_record(
                    "r3", status="failure", exit_code=1, error_category="permanent"
                )
            )
            store.save(_make_record("r4", status="success", exit_code=0))

            stats = store.analytics()

        failure_cats = stats["failure_categories"]
        assert failure_cats.get("transient", 0) == 2
        assert failure_cats.get("permanent", 0) == 1

    def test_analytics_empty_store(self, tmp_path):
        db_path = str(tmp_path / "analytics.db")
        with SqliteRunStore(db_path) as store:
            stats = store.analytics()

        assert stats["overall"]["total"] == 0
        assert stats["overall"]["success_rate"] == 0.0
        assert stats["by_complexity"] == {}
        assert stats["by_runner"] == {}
        assert stats["by_model"] == {}
        assert stats["failure_categories"] == {}


class TestListSince:
    """SqliteRunStore.list_since returns only records at or after the cutoff."""

    def _make_record(self, run_id: str, started_at: datetime) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=started_at,
        )

    def test_list_since_returns_records_at_or_after_cutoff(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        now = datetime.now(UTC)
        old = now - timedelta(days=10)
        recent = now - timedelta(hours=1)

        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("old-run", old))
            store.save(self._make_record("recent-run", recent))

            cutoff = now - timedelta(days=7)
            results = store.list_since(cutoff)

        run_ids = [r.run_id for r in results]
        assert "recent-run" in run_ids
        assert "old-run" not in run_ids

    def test_list_since_includes_record_exactly_at_cutoff(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("at-cutoff", cutoff))
            results = store.list_since(cutoff)

        assert any(r.run_id == "at-cutoff" for r in results)

    def test_list_since_empty_when_all_records_older(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        now = datetime.now(UTC)
        old = now - timedelta(days=30)

        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("old-run", old))
            results = store.list_since(now - timedelta(days=7))

        assert results == []


class TestRunRecordComplexity:
    """RunRecord.complexity field (V6 migration)."""

    def test_complexity_defaults_to_empty_string(self):
        record = RunRecord(
            run_id="cx-1",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
        assert record.complexity == ""

    def test_save_and_get_with_complexity(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            record = RunRecord(
                run_id="cx-2",
                issue_num=1,
                branch="agent/issue-1",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                complexity="medium",
            )
            store.save(record)
            loaded = store.get("cx-2")
            assert loaded is not None
            assert loaded.complexity == "medium"

    def test_schema_migration_v6_preserves_existing_data(self, tmp_path):
        """A DB created at V5 can be migrated to V6 without data loss."""
        import sqlite3

        db_path = str(tmp_path / "v5.db")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 5);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                issue_num INTEGER NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL NOT NULL DEFAULT 0.0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                error_category TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL DEFAULT 1,
                session_id TEXT,
                log_file TEXT,
                pr_url TEXT,
                runner_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_runs_issue_num ON runs(issue_num);
            CREATE INDEX idx_runs_status_started ON runs(status, started_at);
        """)
        conn.execute(
            "INSERT INTO runs"
            " (run_id, issue_num, branch, status, exit_code, started_at)"
            " VALUES ('old-1', 42, 'b42', 'success', 0, '2026-03-20T12:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        with SqliteRunStore(db_path) as store:
            old = store.get("old-1")
            assert old is not None
            assert old.issue_num == 42
            assert old.complexity == ""

            store.save(
                RunRecord(
                    run_id="new-1",
                    issue_num=99,
                    branch="b99",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    complexity="simple",
                )
            )
            new = store.get("new-1")
            assert new is not None
            assert new.complexity == "simple"


class TestPurgeOlderThan:
    """SqliteRunStore.purge_older_than deletes old records."""

    def _make_record(self, run_id: str, started_at: datetime) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=started_at,
        )

    def test_purge_deletes_old_records(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        now = datetime.now(UTC)
        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("old-1", now - timedelta(days=60)))
            store.save(self._make_record("old-2", now - timedelta(days=40)))
            store.save(self._make_record("new-1", now - timedelta(days=5)))
            store.save(self._make_record("new-2", now))

            deleted = store.purge_older_than(30)
            assert deleted == 2
            assert store.get("old-1") is None
            assert store.get("old-2") is None
            assert store.get("new-1") is not None
            assert store.get("new-2") is not None

    def test_purge_returns_zero_when_nothing_to_delete(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        now = datetime.now(UTC)
        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("recent", now))
            deleted = store.purge_older_than(30)
            assert deleted == 0

    def test_purge_raises_on_invalid_days(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            with pytest.raises(ValueError, match="days must be > 0"):
                store.purge_older_than(0)
            with pytest.raises(ValueError, match="days must be > 0"):
                store.purge_older_than(-1)


class TestExportRuns:
    """export_runs_json and export_runs_csv produce valid output."""

    def _make_records(self) -> list[RunRecord]:
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        return [
            RunRecord(
                run_id="run-1",
                issue_num=10,
                branch="agent/issue-10",
                status="success",
                exit_code=0,
                started_at=now,
                finished_at=now + timedelta(minutes=5),
                duration_seconds=300.0,
                estimated_cost_usd=1.50,
                runner_name="claude-code",
                model="opus",
                title="Test issue",
                complexity="simple",
            ),
            RunRecord(
                run_id="run-2",
                issue_num=11,
                branch="agent/issue-11",
                status="failure",
                exit_code=1,
                started_at=now + timedelta(hours=1),
                error_category="timeout",
                runner_name="opencode",
                model="sonnet",
                complexity="medium",
            ),
        ]

    def test_export_json_valid(self):
        import json

        from kew.run_store import export_runs_json

        records = self._make_records()
        result = export_runs_json(records)
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["run_id"] == "run-1"
        assert data[0]["status"] == "success"
        assert data[0]["estimated_cost_usd"] == 1.50
        assert data[1]["run_id"] == "run-2"
        assert data[1]["error_category"] == "timeout"

    def test_export_csv_valid(self):
        import csv
        import io

        from kew.run_store import export_runs_csv

        records = self._make_records()
        result = export_runs_csv(records)
        reader = csv.DictReader(io.StringIO(result))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["status"] == "success"
        assert rows[1]["run_id"] == "run-2"
        assert rows[1]["error_category"] == "timeout"

    def test_export_empty_records(self):
        import json

        from kew.run_store import export_runs_csv, export_runs_json

        assert json.loads(export_runs_json([])) == []
        # CSV with no rows should still have a header
        csv_result = export_runs_csv([])
        assert "run_id" in csv_result


class TestAnalyticsInsufficientData:
    """analytics() sets insufficient_data flag for sparse history."""

    def _make_record(self, run_id: str, status: str = "success") -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status=status,
            exit_code=0 if status == "success" else 1,
            started_at=datetime.now(UTC),
        )

    def test_insufficient_data_true_when_few_runs(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            for i in range(4):
                store.save(self._make_record(f"run-{i}"))
            stats = store.analytics()
            assert stats["insufficient_data"] is True

    def test_insufficient_data_false_when_enough_runs(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            for i in range(5):
                store.save(self._make_record(f"run-{i}"))
            stats = store.analytics()
            assert stats["insufficient_data"] is False

    def test_insufficient_data_true_for_empty_store(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            stats = store.analytics()
            assert stats["insufficient_data"] is True


class TestRetentionDaysConfig:
    """LoggingConfig.retention_days field validation."""

    def test_default_retention_days(self):
        from kew.config import LoggingConfig

        cfg = LoggingConfig()
        assert cfg.retention_days == 0

    def test_valid_retention_days(self):
        from kew.config import LoggingConfig

        cfg = LoggingConfig(retention_days=90)
        assert cfg.retention_days == 90

    def test_negative_retention_days_raises(self):
        from kew.config import LoggingConfig

        with pytest.raises(ValueError, match="retention_days must be >= 0"):
            LoggingConfig(retention_days=-1)


class TestSchemaV7PgidAndKilled:
    """V7 adds runs.pgid, the meta table, and the killed status."""

    def test_killed_status_round_trip(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            record = RunRecord(
                run_id="killed-1",
                issue_num=7,
                branch="agent/issue-7",
                status="killed",
                exit_code=-15,
                started_at=datetime.now(UTC),
                error_category="killed",
            )
            store.save(record)
            loaded = store.get("killed-1")
            assert loaded is not None
            assert loaded.status == "killed"
            assert loaded.error_category == "killed"

    def test_pgid_defaults_to_none(self):
        record = RunRecord(
            run_id="r-1",
            issue_num=1,
            branch="agent/issue-1",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
        )
        assert record.pgid is None

    def test_pgid_round_trip(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            record = RunRecord(
                run_id="r-2",
                issue_num=2,
                branch="agent/issue-2",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=43210,
            )
            store.save(record)
            loaded = store.get("r-2")
            assert loaded is not None
            assert loaded.pgid == 43210

    def test_set_pgid_updates_existing_record(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            record = RunRecord(
                run_id="r-3",
                issue_num=3,
                branch="agent/issue-3",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
            store.save(record)
            store.set_pgid("r-3", 9999)
            loaded = store.get("r-3")
            assert loaded is not None
            assert loaded.pgid == 9999

    def test_migration_v6_to_v7_preserves_data(self, tmp_path):
        """A V6 database gains pgid + meta without data loss."""
        db_path = str(tmp_path / "v6.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                issue_num INTEGER NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL NOT NULL DEFAULT 0.0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                error_category TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL DEFAULT 1,
                session_id TEXT,
                log_file TEXT,
                pr_url TEXT,
                runner_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                complexity TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 6)")
        conn.execute(
            "INSERT INTO runs"
            " (run_id, issue_num, branch, status, exit_code, started_at)"
            " VALUES ('v6-run', 5, 'agent/issue-5', 'pending', -1,"
            " '2026-06-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        with SqliteRunStore(db_path) as store:
            record = store.get("v6-run")
            assert record is not None
            assert record.pgid is None  # default for migrated rows
            # meta table exists and is empty
            assert store.get_fleet_stopped() is None

    def test_export_csv_includes_pgid(self):
        from kew.run_store import export_runs_csv

        record = RunRecord(
            run_id="r-4",
            issue_num=4,
            branch="agent/issue-4",
            status="killed",
            exit_code=-15,
            started_at=datetime(2026, 6, 9, tzinfo=UTC),
            pgid=1234,
        )
        csv_out = export_runs_csv([record])
        header = csv_out.splitlines()[0]
        assert "pgid" in header
        assert "1234" in csv_out


class TestFleetStoppedLatch:
    """The fleet_stopped latch lives in the meta table."""

    def test_not_stopped_by_default(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            assert store.get_fleet_stopped() is None

    def test_set_and_get(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("runaway spend")
            latch = store.get_fleet_stopped()
            assert latch is not None
            assert latch["reason"] == "runaway spend"
            parsed = datetime.fromisoformat(latch["stopped_at"])
            assert parsed.tzinfo is not None

    def test_set_with_empty_reason(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped()
            latch = store.get_fleet_stopped()
            assert latch is not None
            assert latch["reason"] == ""

    def test_clear(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("oops")
            store.clear_fleet_stopped()
            assert store.get_fleet_stopped() is None

    def test_clear_when_not_set_is_noop(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.clear_fleet_stopped()  # must not raise
            assert store.get_fleet_stopped() is None

    def test_set_twice_overwrites(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("first")
            store.set_fleet_stopped("second")
            latch = store.get_fleet_stopped()
            assert latch is not None
            assert latch["reason"] == "second"

    def test_latch_visible_across_connections(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with SqliteRunStore(db_path) as store:
            store.set_fleet_stopped("halt")
        with SqliteRunStore(db_path) as other:
            latch = other.get_fleet_stopped()
            assert latch is not None
            assert latch["reason"] == "halt"


class TestSchemaV8AuditEnvelope:
    """V8 adds envelope + #261 outcome columns to runs, and audit_events."""

    def _record(self, **overrides):
        base = dict(
            run_id="v8-1",
            issue_num=8,
            branch="agent/issue-8",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
        base.update(overrides)
        return RunRecord(**base)

    def test_envelope_fields_default_to_none(self):
        record = self._record()
        assert record.actor_type is None
        assert record.actor_id is None
        assert record.repo is None
        assert record.endpoint is None
        assert record.policy_hash is None

    def test_outcome_261_fields_default_to_none(self):
        record = self._record()
        assert record.pr_state is None
        assert record.pr_merged_at is None
        assert record.review_cycles is None
        assert record.diff_additions is None
        assert record.diff_deletions is None
        assert record.diff_files is None

    def test_envelope_round_trip(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                self._record(
                    actor_type="human",
                    actor_id="alice",
                    repo="octo/widgets",
                    endpoint="anthropic:default",
                )
            )
            loaded = store.get("v8-1")
            assert loaded is not None
            assert loaded.actor_type == "human"
            assert loaded.actor_id == "alice"
            assert loaded.repo == "octo/widgets"
            assert loaded.endpoint == "anthropic:default"
            assert loaded.policy_hash is None  # null until #250

    def test_outcome_261_columns_exist_and_stay_null(self, tmp_path):
        """#261 columns are schema-only: exist, nullable, nothing writes them."""
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            store.save(self._record())
            row = store._conn.execute(
                "SELECT pr_state, pr_merged_at, review_cycles, diff_additions,"
                " diff_deletions, diff_files, policy_hash"
                " FROM runs WHERE run_id = 'v8-1'"
            ).fetchone()
            assert all(row[k] is None for k in row.keys())  # noqa: SIM118

    def test_audit_events_table_and_index_exist(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            tables = {
                r["name"]
                for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "audit_events" in tables
            indexes = {
                r["name"]
                for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert "idx_audit_events_event_ts" in indexes

    def test_fresh_db_is_version_10(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            assert (
                row["version"] == CURRENT_SCHEMA_VERSION
            )  # latest: V25 runs on fresh DB

    def test_migration_v7_to_v8_preserves_data(self, tmp_path):
        """A V7 database gains the envelope columns without data loss."""
        db_path = str(tmp_path / "v7.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                issue_num INTEGER NOT NULL,
                branch TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL NOT NULL DEFAULT 0.0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                error_category TEXT NOT NULL DEFAULT '',
                attempt_number INTEGER NOT NULL DEFAULT 1,
                session_id TEXT,
                log_file TEXT,
                pr_url TEXT,
                runner_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                complexity TEXT NOT NULL DEFAULT '',
                pgid INTEGER
            );
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 7)")
        conn.execute(
            "INSERT INTO runs"
            " (run_id, issue_num, branch, status, exit_code, started_at, pgid)"
            " VALUES ('v7-run', 5, 'agent/issue-5', 'success', 0,"
            " '2026-06-01T00:00:00+00:00', 4321)"
        )
        conn.commit()
        conn.close()

        with SqliteRunStore(db_path) as store:
            record = store.get("v7-run")
            assert record is not None
            assert record.pgid is None  # the legacy owner is unknown
            assert record.attempt_pgid == 4321  # prior kill target intact
            assert record.actor_type is None  # pre-V8 rows read as null
            assert record.endpoint is None
            assert record.diff_files is None
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            assert (
                row["version"] == CURRENT_SCHEMA_VERSION
            )  # latest: V25 also runs (additive)

    def test_double_migration_is_idempotent(self, tmp_path):
        """Opening the same DB twice (concurrent-migrator shape) is harmless."""
        db_path = str(tmp_path / "test.db")
        with SqliteRunStore(db_path) as store:
            store.save(self._record(actor_type="human", actor_id="bob"))
        # Second open re-runs _migrate(); _try_alter swallows duplicates,
        # CREATE TABLE/INDEX IF NOT EXISTS no-op.
        with SqliteRunStore(db_path) as store:
            loaded = store.get("v8-1")
            assert loaded is not None
            assert loaded.actor_id == "bob"

    def test_export_includes_envelope_and_does_not_break(self):
        from kew.run_store import export_runs_csv, export_runs_json

        record = self._record(
            actor_type="human",
            actor_id="alice",
            repo="octo/widgets",
            endpoint="anthropic:default",
            started_at=datetime(2026, 6, 10, tzinfo=UTC),
        )
        csv_out = export_runs_csv([record])
        header = csv_out.splitlines()[0]
        for col in (
            "actor_type",
            "actor_id",
            "repo",
            "endpoint",
            "policy_hash",
            "pr_state",
            "pr_merged_at",
            "review_cycles",
            "diff_additions",
            "diff_deletions",
            "diff_files",
        ):
            assert col in header
        assert "alice" in csv_out
        json_out = json.loads(export_runs_json([record]))
        assert json_out[0]["actor_id"] == "alice"
        assert json_out[0]["pr_state"] is None

    def test_export_all_none_envelope_does_not_break(self):
        """Pre-V8-shaped records (all-null envelope) still export cleanly."""
        from kew.run_store import export_runs_csv, export_runs_json

        record = self._record(started_at=datetime(2026, 6, 10, tzinfo=UTC))
        assert "v8-1" in export_runs_csv([record])
        assert json.loads(export_runs_json([record]))[0]["actor_type"] is None


class TestAuditEventsStore:
    """append_audit_event / list_audit_events on SqliteRunStore."""

    def _append(self, store, event="dispatch_completed", **overrides):
        kwargs = dict(
            timestamp="2026-06-10T12:00:00+00:00",
            event=event,
            run_id="run-1",
            issue_num=42,
            repo="octo/widgets",
            actor_type="human",
            actor_id="alice",
            endpoint="anthropic:default",
            payload={"exit_code": 0},
        )
        kwargs.update(overrides)
        store.append_audit_event(**kwargs)

    def test_append_and_list_round_trip(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store)
            events = store.list_audit_events()
            assert len(events) == 1
            ev = events[0]
            assert ev.id == 1
            assert ev.timestamp == "2026-06-10T12:00:00+00:00"
            assert ev.event == "dispatch_completed"
            assert ev.run_id == "run-1"
            assert ev.issue_num == 42
            assert ev.repo == "octo/widgets"
            assert ev.actor_type == "human"
            assert ev.actor_id == "alice"
            assert ev.endpoint == "anthropic:default"
            assert ev.policy_hash is None  # null until #250
            assert ev.prev_hash == "0" * 64  # GENESIS_PREV for first row (#249)
            assert ev.payload == {"exit_code": 0}

    def test_minimal_event_all_optionals_null(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T12:00:00+00:00", event="fleet_resume"
            )
            ev = store.list_audit_events()[0]
            assert ev.event == "fleet_resume"
            assert ev.run_id is None
            assert ev.issue_num is None
            assert ev.payload is None

    def test_list_newest_first_with_limit(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for i in range(5):
                self._append(store, payload={"n": i})
            events = store.list_audit_events(limit=3)
            assert len(events) == 3
            assert [e.payload["n"] for e in events] == [4, 3, 2]
            assert [e.id for e in events] == [5, 4, 3]

    def test_list_limit_none_returns_all(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for i in range(150):
                self._append(store, payload={"n": i})
            assert len(store.list_audit_events(limit=None)) == 150
            assert len(store.list_audit_events()) == 100  # default limit

    def test_list_filters_by_event(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="dispatch_completed")
            self._append(store, event="fleet_stop")
            self._append(store, event="dispatch_completed")
            events = store.list_audit_events(event="fleet_stop")
            assert len(events) == 1
            assert events[0].event == "fleet_stop"

    def test_list_filters_by_run_id(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, run_id="run-1")
            self._append(store, run_id="run-2")
            events = store.list_audit_events(run_id="run-1")
            assert len(events) == 1
            assert events[0].run_id == "run-1"

    def test_list_filters_by_since(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, timestamp="2026-06-01T00:00:00+00:00")
            self._append(store, timestamp="2026-06-09T00:00:00+00:00")
            events = store.list_audit_events(since=datetime(2026, 6, 5, tzinfo=UTC))
            assert len(events) == 1
            assert events[0].timestamp == "2026-06-09T00:00:00+00:00"

    def test_malformed_payload_reads_as_none(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store._conn.execute(
                "INSERT INTO audit_events (timestamp, event, payload)"
                " VALUES ('2026-06-10T00:00:00+00:00', 'x', 'not-json')"
            )
            store._conn.commit()
            assert store.list_audit_events()[0].payload is None

    def test_visible_across_connections(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        with SqliteRunStore(db_path) as writer:
            self._append(writer)
        with SqliteRunStore(db_path) as reader:
            assert len(reader.list_audit_events()) == 1

    def test_protocol_includes_audit_methods(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            assert isinstance(store, RunStore)
            assert hasattr(store, "append_audit_event")
            assert hasattr(store, "list_audit_events")

    def _captured_prefix_sql(self, store, **filters):
        """The statement list_audit_events really ran, from sqlite3's own trace.

        Reading it back beats restating the SQL here, where a test could go on
        asserting a plan for a query the implementation no longer issues.
        """
        seen: list[str] = []
        store._conn.set_trace_callback(seen.append)
        try:
            store.list_audit_events(limit=None, **filters)
        finally:
            store._conn.set_trace_callback(None)
        return next(s for s in seen if "FROM audit_events" in s)

    def test_event_prefix_seeks_the_index_instead_of_scanning(self, tmp_path):
        """`event LIKE ? ESCAPE '\\'` is never index-served, so the prefix read
        full-scanned audit_events — one 1s epic poll went from 0.09ms to 26.79ms
        at 300k rows, growing without bound (#2129). A half-open range on the
        same column seeks idx_audit_events_event_ts."""
        with SqliteRunStore(str(tmp_path / "plan.db")) as store:
            sql = self._captured_prefix_sql(store, event_prefix="approval_")
            plan = " ".join(
                str(row[3])
                for row in store._conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")
                )
            )
        assert "idx_audit_events_event_ts" in plan
        assert "SCAN audit_events" not in plan
        assert "LIKE" not in sql

    def test_event_prefix_is_an_exact_prefix_not_a_like_pattern(self, tmp_path):
        """The range must match exactly what the escaped LIKE matched: the
        wildcard chars stay literal, and a near-miss event is excluded."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for event in ("approval_hold", "approvalXhold", "approval", "a_b%c_d"):
                self._append(store, event=event)
            assert [
                e.event
                for e in store.list_audit_events(limit=None, event_prefix="approval_")
            ] == ["approval_hold"]
            assert [
                e.event
                for e in store.list_audit_events(limit=None, event_prefix="a_b%c")
            ] == ["a_b%c_d"]

    def test_issue_nums_scopes_the_read_without_changing_the_rule(self, tmp_path):
        """A caller that wants a handful of issues (an epic's nodes on a 1s
        poll) must not pay to parse every matching event on the host (#2129)."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for num in (7, 8, 9):
                self._append(store, event="approval_hold", issue_num=num)
            self._append(store, event="approval_hold", issue_num=None)
            scoped = store.list_audit_events(
                limit=None, event_prefix="approval_", issue_nums=[9, 7, 9]
            )
            assert [e.issue_num for e in scoped] == [9, 7]
            assert scoped == [
                e
                for e in store.list_audit_events(limit=None, event_prefix="approval_")
                if e.issue_num in (7, 9)
            ]

    def test_empty_issue_nums_scope_reads_nothing(self, tmp_path):
        """No issues in scope is an empty answer, not the whole table."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="approval_hold", issue_num=7)
            assert store.list_audit_events(limit=None, issue_nums=[]) == []

    def test_item_ids_scopes_the_read_by_durable_id(self, tmp_path):
        """`issue_nums`' sibling for a tracker whose ids are not numbers, and
        for a caller holding ids: an epic's unsettled nodes, read on a 1s poll,
        must not pay for every `epic_merge` the host ever wrote (#2392)."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for item in ("SFD-1", "SFD-2", "SFD-3"):
                self._append(store, event="epic_merge", issue_num=0, item_id=item)
            scoped = store.list_audit_events(
                limit=None, event="epic_merge", item_ids=["SFD-3", "SFD-1", "SFD-3"]
            )
            assert [e.item_id for e in scoped] == ["SFD-3", "SFD-1"]

    def test_item_ids_also_matches_the_issue_num_column(self, tmp_path):
        """A numeric id is the issue number on the classic GitHub path, where a
        pre-v24 row carries no ``item_id`` at all — so the scope must reach it
        there too, or narrowing the read would start losing attestations."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="epic_merge", issue_num=103, item_id=None)
            self._append(store, event="epic_merge", issue_num=999, item_id=None)
            scoped = store.list_audit_events(
                limit=None, event="epic_merge", item_ids=["103"]
            )
            assert [e.issue_num for e in scoped] == [103]

    def test_empty_item_ids_scope_reads_nothing(self, tmp_path):
        """No items in scope is an empty answer, not the whole table."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="epic_merge", issue_num=7, item_id="7")
            assert store.list_audit_events(limit=None, item_ids=[]) == []

    def test_item_ids_seeks_the_index_instead_of_scanning(self, tmp_path):
        """The whole point of the scope: `idx_audit_events_item_event` serves
        it, so the cost tracks the caller's question and not the ledger's age.

        The index is named, not just the absence of a SCAN: an `event=` read
        with the whole scope clause REMOVED is served by
        `idx_audit_events_event_ts` and is not a SCAN either, so "not a SCAN"
        alone passed identically either way and verified nothing about the
        scope (#2392 review).
        """
        with SqliteRunStore(str(tmp_path / "plan.db")) as store:
            sql = self._captured_prefix_sql(
                store, event="epic_merge", item_ids=["SFD-1", "SFD-2"]
            )
            plan = " ".join(
                str(row[3])
                for row in store._conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")
                )
            )
        assert "idx_audit_events_item_event" in plan
        assert "SCAN audit_events" not in plan

    def test_several_event_prefixes_or_together(self, tmp_path):
        """Two prefixes are a union, not an impossible conjunction (A3)."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for event in ("approval_hold", "dispatch_started", "epic_tick"):
                self._append(store, event=event)
            events = store.list_audit_events(
                limit=None, event_prefixes=["approval_", "dispatch_"]
            )
            assert sorted(e.event for e in events) == [
                "approval_hold",
                "dispatch_started",
            ]
            # The legacy single-prefix arg still composes, as the same union.
            both = store.list_audit_events(
                limit=None, event_prefix="epic_", event_prefixes=["approval_"]
            )
            assert sorted(e.event for e in both) == ["approval_hold", "epic_tick"]

    def test_several_prefixes_still_seek_the_index(self, tmp_path):
        """The OR of ranges must stay index-served — the whole point of #2129.

        SQLite's OR optimization runs one index seek per arm; a plan that
        SCANs audit_events instead would reintroduce the 1s-poll regression at
        a larger table.
        """
        with SqliteRunStore(str(tmp_path / "plan.db")) as store:
            sql = self._captured_prefix_sql(
                store, event_prefixes=["approval_", "dispatch_"]
            )
            plan = " ".join(
                str(row[3])
                for row in store._conn.execute(
                    "EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")
                )
            )
        assert "idx_audit_events_event_ts" in plan
        assert "SCAN audit_events" not in plan

    def test_until_is_exclusive_and_pairs_with_since(self, tmp_path):
        """``[since, until)`` — so adjacent windows tile the log exactly once."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for day in (4, 5, 6):
                self._append(
                    store, timestamp=datetime(2026, 6, day, tzinfo=UTC).isoformat()
                )
            window = store.list_audit_events(
                limit=None,
                since=datetime(2026, 6, 4, tzinfo=UTC),
                until=datetime(2026, 6, 6, tzinfo=UTC),
            )
            assert [e.timestamp[:10] for e in window] == ["2026-06-05", "2026-06-04"]

    def test_item_filter_matches_either_key_column(self, tmp_path):
        """``item=`` is the durable ``item_id``, or ``issue_num`` when numeric."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="a", item_id="SFD-232")
            self._append(store, event="b", issue_num=2219)
            self._append(store, event="c", item_id="2219")
            assert [
                e.event
                for e in store.list_audit_events(limit=None, item_or_issue="SFD-232")
            ] == ["a"]
            assert sorted(
                e.event
                for e in store.list_audit_events(limit=None, item_or_issue="2219")
            ) == ["b", "c"]

    def test_epic_filter_ors_node_ids_with_the_payload_attribution(self, tmp_path):
        """An epic reaches its nodes' events AND its own epic-level events."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="node-by-issue", issue_num=2219)
            self._append(store, event="node-by-item", item_id="SFD-232")
            self._append(store, event="epic-level", payload={"epic_ref": "ui-refine"})
            self._append(store, event="other-epic", payload={"epic_ref": "nope"})
            self._append(store, event="unrelated", issue_num=4040)
            events = store.list_audit_events(
                limit=None,
                epic_ref="ui-refine",
                epic_node_ids=["2219", "SFD-232"],
            )
            assert sorted(e.event for e in events) == [
                "epic-level",
                "node-by-issue",
                "node-by-item",
            ]

    def test_an_or_group_is_parenthesized_against_the_other_filters(self, tmp_path):
        """Every OR group narrows as a whole when a second filter is AND-ed.

        The groups are AND-joined by the caller, so a bare ``' OR '.join``
        would bind looser than that AND: the narrowing filter would attach to
        the group's LAST arm only and every other arm would come back
        unfiltered, silently widening the read.
        """
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="epic_tick", issue_num=2219)
            self._append(store, event="epic_node_status", issue_num=2219)
            self._append(store, event="epic_tick", issue_num=None, item_id="2219")
            self._append(
                store, event="epic_node_status", issue_num=None, item_id="SFD-232"
            )

            def ids(**filters):
                return sorted(
                    e.id for e in store.list_audit_events(limit=None, **filters)
                )

            # item=: `item_id = ? OR issue_num = ?` — `event=` narrows both.
            assert ids(item_or_issue="2219") == [1, 2, 3]
            assert ids(item_or_issue="2219", event="epic_tick") == [1, 3]
            # epic=: the two key columns OR the `$.epic_ref` attribution.
            assert ids(epic_ref="e", epic_node_ids=["2219", "SFD-232"]) == [1, 2, 3, 4]
            assert ids(
                epic_ref="e", epic_node_ids=["2219", "SFD-232"], event="epic_tick"
            ) == [1, 3]
            # event_prefix=: one half-open range per prefix.
            assert ids(event_prefixes=["epic_t", "epic_n"]) == [1, 2, 3, 4]
            assert ids(event_prefixes=["epic_t", "epic_n"], issue_num=2219) == [1, 2]

    def test_a_numeric_filter_too_wide_for_int64_is_not_bound_as_one(self, tmp_path):
        """A filter value past the int64 digit bound reads as an item id only.

        sqlite3 refuses to bind an int wider than int64 (``OverflowError``) and
        CPython refuses ``int()`` on a very long digit string at all
        (``ValueError``) — both a 500 on a value no ``issue_num`` could hold.
        It drops out of the numeric arm instead and matches ``item_id`` (#2247).
        """
        wide = "9" * (MAX_ISSUE_NUM_DIGITS + 1)
        huge = "9" * 5000  # past CPython's int()-from-str digit limit
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._append(store, event="wide-item", issue_num=None, item_id=wide)
            self._append(store, event="narrow", issue_num=999999999999999999)

            assert [
                e.event for e in store.list_audit_events(limit=None, item_or_issue=wide)
            ] == ["wide-item"]
            assert store.list_audit_events(limit=None, item_or_issue=huge) == []
            assert [
                e.event
                for e in store.list_audit_events(
                    limit=None, epic_ref="e", epic_node_ids=[wide, huge]
                )
            ] == ["wide-item"]
            # The widest value that DOES fit still binds as an issue number.
            assert [
                e.event
                for e in store.list_audit_events(
                    limit=None, item_or_issue="9" * MAX_ISSUE_NUM_DIGITS
                )
            ] == ["narrow"]

    def test_histogram_counts_match_a_full_read_of_the_window(self, tmp_path):
        """Bucket counts are the same rows ``list_audit_events`` would return."""
        base = datetime(2026, 6, 4, tzinfo=UTC)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            for minutes, event in ((0, "a"), (5, "a"), (70, "b"), (200, "c")):
                self._append(
                    store,
                    event=event,
                    timestamp=(base + timedelta(minutes=minutes)).isoformat(),
                )
            until = base + timedelta(minutes=180)
            rows = store.audit_event_histogram(since=base, until=until, bucket_s=3600)
            page = store.list_audit_events(limit=None, since=base, until=until)

            assert rows == [(0, "a", 2), (1, "b", 1)]  # the 200-minute row is out
            assert sum(n for _, _, n in rows) == len(page)
            assert store.audit_event_histogram(
                since=base, until=until, bucket_s=3600, event="b"
            ) == [(1, "b", 1)]


class TestMigrationConcurrencySafety:
    """Version writes are monotonic; fresh-DB bootstrap insert is idempotent."""

    def test_apply_v1_is_idempotent(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            cursor = store._conn.cursor()
            store._apply_v1(cursor)  # re-run: INSERT OR IGNORE must not raise
            store._conn.commit()
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            assert (
                row["version"] == CURRENT_SCHEMA_VERSION
            )  # V25 is now the latest version

    def test_stale_migrator_cannot_regress_version(self, tmp_path):
        """A lagging concurrent migrator re-applying an old step is harmless."""
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            cursor = store._conn.cursor()
            store._apply_v2(cursor)  # stale step finishing after v10
            store._conn.commit()
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            assert (
                row["version"] == CURRENT_SCHEMA_VERSION
            )  # MAX() keeps the version monotonic


class TestAuditTimestampNormalization:
    """append_audit_event canonicalizes timestamps for TEXT comparisons."""

    def test_naive_timestamp_stored_as_utc(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T12:00:00", event="fleet_resume"
            )
            ev = store.list_audit_events()[0]
            assert ev.timestamp == "2026-06-10T12:00:00+00:00"

    def test_offset_timestamp_converted_to_utc(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T14:00:00+02:00", event="fleet_resume"
            )
            ev = store.list_audit_events()[0]
            assert ev.timestamp == "2026-06-10T12:00:00+00:00"

    def test_offset_timestamps_filter_consistently(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            # 14:00+02:00 == 12:00 UTC; without normalization the TEXT
            # comparison against a 13:00 UTC cutoff would wrongly match.
            store.append_audit_event(
                timestamp="2026-06-10T14:00:00+02:00", event="fleet_resume"
            )
            cutoff = datetime(2026, 6, 10, 13, 0, tzinfo=UTC)
            assert store.list_audit_events(since=cutoff) == []

    def test_unparseable_timestamp_stored_as_given(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.append_audit_event(timestamp="not-a-date", event="x")
            assert store.list_audit_events()[0].timestamp == "not-a-date"


# ---------------------------------------------------------------------------
# Loop pause helpers — schema V10 (#303)
# ---------------------------------------------------------------------------


def _make_loop_record(loop_id: str, *, status: str = "pursuing") -> LoopRecord:
    return LoopRecord(
        loop_id=loop_id,
        source="issue",
        goal_text="do the thing",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, 12, 0, 0, tzinfo=UTC),
        wall_clock_secs=1800,
        max_steps=200,
        status=status,  # type: ignore[arg-type]
    )


def test_loop_pause_requested_roundtrips(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    loop = _make_loop_record("L1", status="pursuing")
    store.save_loop(loop)
    assert store.get_loop("L1").pause_requested is False
    assert store.request_pause("L1") is True
    assert store.get_loop("L1").pause_requested is True


def test_request_pause_only_on_pursuing(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    loop = _make_loop_record("L1", status="needs_human")
    store.save_loop(loop)
    assert store.request_pause("L1") is False  # not pursuing
    assert store.request_pause("nope") is False  # unknown
    assert store.get_loop("L1").pause_requested is False


def test_clear_pause(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    loop = _make_loop_record("L1", status="pursuing")
    store.save_loop(loop)
    store.request_pause("L1")
    store.clear_pause("L1")
    assert store.get_loop("L1").pause_requested is False


def test_paused_is_a_valid_loop_status(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    loop = _make_loop_record("L1", status="paused")
    store.save_loop(loop)
    assert store.get_loop("L1").status == "paused"


def test_request_pause_is_idempotent_on_pursuing(tmp_path):
    # The CLI contract is "False == not running"; a 2nd request on a pursuing loop
    # must still return True (SQLite rowcount counts matched rows, not changed).
    store = SqliteRunStore(str(tmp_path / "d.db"))
    store.save_loop(_make_loop_record("L1", status="pursuing"))
    assert store.request_pause("L1") is True
    assert store.request_pause("L1") is True


def test_pause_flag_visible_across_connections(tmp_path):
    # THE mechanism test: process A (one connection) sets the flag; process B
    # (a separate connection over the SAME on-disk db, like the running loop's
    # process) must SEE it. A :memory: store cannot catch this — needs a file.
    db = str(tmp_path / "shared.db")
    writer = SqliteRunStore(db)
    writer.save_loop(_make_loop_record("L1", status="pursuing"))
    reader = SqliteRunStore(db)  # separate connection (simulates process B)
    assert reader.get_loop("L1").pause_requested is False
    assert writer.request_pause("L1") is True
    # B must observe A's committed write on its next read.
    assert reader.get_loop("L1").pause_requested is True


def test_v10_adds_pause_requested_column(tmp_path):
    db = str(tmp_path / "d.db")
    store = SqliteRunStore(db)  # migrates to current
    loop = _make_loop_record("L1", status="pursuing")
    store.save_loop(loop)
    # Column is queryable and defaults false.
    assert store.get_loop("L1").pause_requested is False


# ---------------------------------------------------------------------------
# Outcome persistence (#261) — set_outcome + list_syncable_runs
# ---------------------------------------------------------------------------


def _a_run(run_id: str, *, status: str = "success") -> RunRecord:
    """Minimal RunRecord factory for outcome-persistence tests."""
    return _make_record(run_id, status=status)


def test_set_outcome_updates_only_outcome_columns(tmp_path):
    store = SqliteRunStore(":memory:")
    run = _a_run(run_id="r1", status="success")
    store.save(run)
    store.set_outcome(
        "r1",
        pr_url="https://github.com/o/r/pull/5",
        pr_state="MERGED",
        pr_merged_at="2026-06-24T10:00:00Z",
        review_cycles=3,
        diff_additions=40,
        diff_deletions=5,
        diff_files=2,
    )
    got = store.get("r1")
    assert got.pr_state == "MERGED"
    assert got.pr_url.endswith("/pull/5")
    assert got.review_cycles == 3
    assert got.diff_additions == 40 and got.diff_files == 2
    assert got.status == "success"  # untouched


def test_set_outcome_does_not_downgrade_terminal_state(tmp_path):
    """A terminal MERGED/CLOSED outcome must survive a stale OPEN write
    (guards the concurrent-sync downgrade race)."""
    store = SqliteRunStore(":memory:")
    store.save(_a_run(run_id="r1", status="success"))
    # First sync resolves the PR as MERGED (terminal).
    store.set_outcome(
        "r1",
        pr_url="https://github.com/o/r/pull/5",
        pr_state="MERGED",
        pr_merged_at="2026-06-24T10:00:00Z",
        review_cycles=3,
        diff_additions=40,
        diff_deletions=5,
        diff_files=2,
    )
    # A stale concurrent worker tries to write an older OPEN snapshot.
    store.set_outcome(
        "r1",
        pr_url="https://github.com/o/r/pull/5",
        pr_state="OPEN",
        pr_merged_at=None,
        review_cycles=1,
        diff_additions=1,
        diff_deletions=0,
        diff_files=1,
    )
    got = store.get("r1")
    assert got.pr_state == "MERGED"  # not downgraded
    assert got.pr_merged_at == "2026-06-24T10:00:00Z"
    assert got.review_cycles == 3  # the terminal snapshot's values survive intact


def test_list_syncable_runs_excludes_terminal_and_non_success(tmp_path):
    store = SqliteRunStore(":memory:")
    store.save(_a_run(run_id="open", status="success"))  # pr_state None -> syncable
    store.set_outcome(
        "open",
        pr_url=None,
        pr_state="OPEN",
        pr_merged_at=None,
        review_cycles=None,
        diff_additions=None,
        diff_deletions=None,
        diff_files=None,
    )
    store.save(_a_run(run_id="merged", status="success"))
    store.set_outcome(
        "merged",
        pr_url=None,
        pr_state="MERGED",
        pr_merged_at=None,
        review_cycles=None,
        diff_additions=None,
        diff_deletions=None,
        diff_files=None,
    )
    store.save(_a_run(run_id="failed", status="failure"))  # not success -> excluded
    store.save(_a_run(run_id="fresh", status="success"))  # pr_state None -> syncable
    ids = {r.run_id for r in store.list_syncable_runs()}
    # "failed" shares branch agent/issue-1 with successful siblings -> superseded.
    assert ids == {"open", "fresh"}


def test_list_syncable_runs_includes_orphan_failure(tmp_path):
    """A failed run whose branch has NO successful sibling is syncable — its PR
    may have merged out-of-band (a rescued run that failed its own gate, #611).
    """
    store = SqliteRunStore(":memory:")
    # issue_num 595 -> branch agent/issue-595, no successful run on that branch.
    store.save(_make_record("orphan_fail", issue_num=595, status="failure"))
    ids = {r.run_id for r in store.list_syncable_runs()}
    assert ids == {"orphan_fail"}


def test_list_syncable_runs_excludes_failure_superseded_by_success(tmp_path):
    """A failed attempt on a branch that later succeeded stays excluded: the
    successful run already carries that branch's PR, so the failure is noise."""
    store = SqliteRunStore(":memory:")
    store.save(_make_record("fail_then", issue_num=42, status="failure"))
    store.save(_make_record("then_ok", issue_num=42, status="success"))
    ids = {r.run_id for r in store.list_syncable_runs()}
    assert ids == {"then_ok"}


def test_list_syncable_runs_scoped_to_items_keys_off_item_id_then_issue_num():
    """The ``item_ids`` scope resolves a record's key the way every reader
    does — the durable item_id when it has one, else the issue number — so a
    scoped caller and an unscoped one never disagree about which row is whose.
    """
    from dataclasses import replace

    store = SqliteRunStore(":memory:")
    store.save(_make_record("legacy", issue_num=101))  # no item_id -> keyed "101"
    store.save(replace(_make_record("durable", issue_num=102), item_id="SFD-12"))
    store.save(_make_record("other", issue_num=103))

    assert {r.run_id for r in store.list_syncable_runs(item_ids=["101"])} == {"legacy"}
    assert {r.run_id for r in store.list_syncable_runs(item_ids=["SFD-12"])} == {
        "durable"
    }
    # The issue number does NOT address a record that carries an item_id.
    assert store.list_syncable_runs(item_ids=["102"]) == []
    assert store.list_syncable_runs(item_ids=[]) == []


def test_list_syncable_runs_scope_narrows_without_changing_the_rule():
    """Scoping must not smuggle in a second syncability rule: a superseded
    failure stays excluded even when its own item id is the only one asked
    for, because the orphan-failure subquery still sees the whole branch."""
    store = SqliteRunStore(":memory:")
    store.save(_make_record("fail_then", issue_num=42, status="failure"))
    store.save(_make_record("then_ok", issue_num=42, status="success"))

    scoped = store.list_syncable_runs(item_ids=["42"])

    assert {r.run_id for r in scoped} == {"then_ok"}
    assert scoped == [r for r in store.list_syncable_runs() if r.issue_num == 42]


# ---------------------------------------------------------------------------
# Audit chain tests (#249)
# ---------------------------------------------------------------------------
from kew.audit_chain import GENESIS_PREV, verify_chain  # noqa: E402


def _append(store, event, **kw):
    store.append_audit_event(timestamp="2026-06-25T00:00:00+00:00", event=event, **kw)


def test_append_audit_event_builds_verifiable_chain(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    for i in range(5):
        _append(store, f"e{i}", actor_type="human", actor_id="tom")
    rows = store.read_chain_rows()
    assert len(rows) == 5
    assert rows[0].prev_hash == GENESIS_PREV
    assert verify_chain(rows).ok
    store.close()


def test_append_ignores_caller_prev_hash(tmp_path):
    store = SqliteRunStore(str(tmp_path / "d.db"))
    _append(store, "e0", actor_type="human", actor_id="tom", prev_hash="f" * 64)
    rows = store.read_chain_rows()
    assert rows[0].prev_hash == GENESIS_PREV  # caller value ignored
    store.close()


def test_verify_chain_holds_over_malformed_payload_text(tmp_path):
    # A row whose stored payload TEXT is not a valid JSON object must still
    # verify: the chain hashes the verbatim TEXT, never a re-parsed dict
    # (which read_chain_rows must NOT coerce to None the way AuditEvent does).
    from kew.audit_chain import entry_hash

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _append(store, "e0", actor_type="human", actor_id="tom")
        prev = entry_hash(store._read_chain_tail())  # hash of the genuine tail
        # Insert a correctly-chained row whose payload is non-dict/non-JSON TEXT,
        # in the chain format every row after the v77 step is written in.
        store._conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, actor_id,"
            " prev_hash, payload, chain_format) VALUES (?, ?, ?, ?, ?, ?, 2)",
            (
                "2026-06-25T00:00:00+00:00",
                "e1",
                "human",
                "tom",
                prev,
                "not-a-json-object",
            ),
        )
        store._conn.commit()
        rows = store.read_chain_rows()
        assert rows[-1].payload == "not-a-json-object"  # verbatim TEXT, not None
        assert verify_chain(rows).ok  # hashed verbatim ⇒ chain still valid


def test_append_rolls_back_and_recovers_on_midtxn_failure(tmp_path):
    import sqlite3 as _sqlite

    store = SqliteRunStore(str(tmp_path / "d.db"))
    _append(store, "e0", actor_type="human", actor_id="tom")
    # Force a non-"locked" failure mid-transaction by monkeypatching _execute.
    # (sqlite3.Connection.execute is read-only; _execute is the patchable wrapper.)
    real_execute = store._execute
    calls = {"n": 0}

    def boom(sql, *a, **k):
        if sql.startswith("INSERT INTO audit_events") and calls["n"] == 0:
            calls["n"] += 1
            raise _sqlite.IntegrityError("boom")
        return real_execute(sql, *a, **k)

    store._execute = boom  # type: ignore[assignment]
    with pytest.raises(_sqlite.IntegrityError):
        _append(store, "e1", actor_type="human", actor_id="tom")
    store._execute = real_execute  # type: ignore[assignment]
    # Transaction must be closed, and the next append must succeed.
    assert store._conn.in_transaction is False
    _append(store, "e2", actor_type="human", actor_id="tom")
    rows = store.read_chain_rows()
    assert [r.event for r in rows] == ["e0", "e2"]
    assert verify_chain(rows).ok
    store.close()


def test_concurrent_appends_produce_one_linear_chain(tmp_path):
    import threading

    db = str(tmp_path / "d.db")
    SqliteRunStore(db).close()  # create schema once
    N_THREADS, M_EACH = 8, 25
    barrier = threading.Barrier(N_THREADS)
    errors: list[Exception] = []

    def worker(t):
        s = SqliteRunStore(db)
        try:
            barrier.wait()  # maximise contention
            for i in range(M_EACH):
                s.append_audit_event(
                    timestamp="2026-06-25T00:00:00+00:00",
                    event=f"t{t}-e{i}",
                    actor_type="human",
                    actor_id="tom",
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    store = SqliteRunStore(db)
    rows = store.read_chain_rows()
    assert len(rows) == N_THREADS * M_EACH  # no loss
    assert verify_chain(rows).ok  # no fork
    store.close()


def test_v12_backfill_chains_existing_null_rows(tmp_path):
    from kew.audit_chain import GENESIS_PREV  # noqa: F811

    db = str(tmp_path / "d.db")
    store = SqliteRunStore(db)
    # Simulate pre-#249 rows: insert directly with prev_hash NULL, bypassing
    # the chained append, then force version back to 11 and re-open.
    for i in range(4):
        store._conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, actor_id,"
            " prev_hash, payload) VALUES (?, ?, ?, ?, NULL, NULL)",
            (f"2026-06-25T00:0{i}:00+00:00", f"e{i}", "human", "tom"),
        )
    store._conn.execute("UPDATE schema_version SET version = 11 WHERE id = 1")
    store._conn.commit()
    store.close()
    # Re-open triggers _migrate → v12 backfill.
    store = SqliteRunStore(db)
    rows = store.read_chain_rows()
    assert rows[0].prev_hash == GENESIS_PREV
    assert verify_chain(rows).ok
    assert (
        store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        >= 12
    )
    store.close()


def test_concurrent_v12_migrators_produce_one_chain(tmp_path):
    import threading

    db = str(tmp_path / "d.db")
    seed = SqliteRunStore(db)
    for i in range(6):
        seed._conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, actor_id,"
            " prev_hash, payload) VALUES (?, ?, ?, ?, NULL, NULL)",
            (f"2026-06-25T00:0{i}:00+00:00", f"e{i}", "human", "tom"),
        )
    seed._conn.execute("UPDATE schema_version SET version = 11 WHERE id = 1")
    seed._conn.commit()
    seed.close()
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def opener():
        try:
            barrier.wait()
            SqliteRunStore(db).close()  # construction runs _migrate → v12
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    store = SqliteRunStore(db)
    assert verify_chain(store.read_chain_rows()).ok
    store.close()


def test_record_and_get_policy_snapshot_round_trip(tmp_path):
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.record_policy_snapshot("abc123", '{"v":1}', "2026-06-25T00:00:00+00:00")
        row = store.get_policy_snapshot("abc123")
    assert row == ('{"v":1}', "2026-06-25T00:00:00+00:00")


def test_get_policy_snapshot_miss_returns_none(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.get_policy_snapshot("nope") is None


def test_record_policy_snapshot_is_store_once(tmp_path):
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.record_policy_snapshot("h", '{"a":1}', "2026-06-25T00:00:00+00:00")
        # INSERT OR IGNORE: a second write with the SAME hash keeps the first row.
        store.record_policy_snapshot("h", '{"a":2}', "2026-06-26T00:00:00+00:00")
        row = store.get_policy_snapshot("h")
        count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM policy_snapshots"
        ).fetchone()["n"]
    assert row == ('{"a":1}', "2026-06-25T00:00:00+00:00")
    assert count == 1


def test_two_stores_one_process_both_retain_same_hash(tmp_path):
    # F4: no process-level guard — each store retains independently.
    from kew.run_store import SqliteRunStore

    db_a = tmp_path / "a" / "dispatch.db"
    db_b = tmp_path / "b" / "dispatch.db"
    db_a.parent.mkdir()
    db_b.parent.mkdir()
    with SqliteRunStore(str(db_a)) as a, SqliteRunStore(str(db_b)) as b:
        a.record_policy_snapshot("shared", '{"x":1}', "2026-06-25T00:00:00+00:00")
        b.record_policy_snapshot("shared", '{"x":1}', "2026-06-25T00:00:00+00:00")
        assert a.get_policy_snapshot("shared") is not None
        assert b.get_policy_snapshot("shared") is not None


def test_policy_snapshots_table_created_at_v13(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        tbl = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name='policy_snapshots'"
        ).fetchone()
        assert tbl is not None
        row = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
    assert row["version"] == CURRENT_SCHEMA_VERSION


def test_v13_migration_is_idempotent(tmp_path):
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.record_policy_snapshot("k", "{}", "2026-06-25T00:00:00+00:00")
    # Re-open (re-runs _migrate); the row and version survive, no error.
    with SqliteRunStore(str(db)) as store:
        assert store.get_policy_snapshot("k") == ("{}", "2026-06-25T00:00:00+00:00")
        row = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
    assert row["version"] == CURRENT_SCHEMA_VERSION


def test_reported_model_round_trips(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        rec = RunRecord(
            run_id="r-rm",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            reported_model="anthropic/claude-sonnet-4-6",
        )
        store.save(rec)
        got = store.get("r-rm")
        assert got is not None
        assert got.reported_model == "anthropic/claude-sonnet-4-6"


def test_reported_model_column_added_by_v14(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "reported_model" in cols
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_run_kind_column_added_by_v26_backfills_dispatch(tmp_path):
    """A pre-v26 runs row (no run_kind column) gains run_kind='dispatch' on
    reopen, so heavyweight review runs stop polluting the dispatch bucket
    (#445)."""
    db_path = str(tmp_path / "dispatch.db")
    # Build a current DB with a run, then simulate a pre-v26 state: drop the
    # run_kind column/index and stamp the version back to 25.
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-legacy",
                issue_num=42,
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=0.50,
            )
        )
        store._conn.execute("DROP INDEX IF EXISTS idx_runs_kind_bucket")
        store._conn.execute("ALTER TABLE runs DROP COLUMN run_kind")
        store._conn.execute("UPDATE schema_version SET version = 25 WHERE id = 1")
        store._conn.commit()

    # Re-open: v26 migration re-runs, adds run_kind, backfills 'dispatch'.
    with SqliteRunStore(db_path) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "run_kind" in cols
        rec = store.get("r-legacy")
        assert rec is not None
        assert rec.run_kind == "dispatch"  # backfilled default
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_v27_creates_tracker_events_preserves_data(tmp_path):
    """Reopening a pre-v27 DB creates the off-chain tracker_events table
    without disturbing existing rows (W1/#893)."""
    import sqlite3 as _sqlite3

    from kew.run_store import TrackerEvent

    db_path = str(tmp_path / "dispatch.db")
    # Build a current DB with a run, then simulate a pre-v27 state: drop the
    # tracker_events table and stamp the version back to 26.
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=0.25,
            )
        )
        store._conn.execute("DROP TABLE IF EXISTS tracker_events")
        store._conn.execute("UPDATE schema_version SET version = 26 WHERE id = 1")
        store._conn.commit()

    # Confirm the table is really gone at v26.
    conn = _sqlite3.connect(db_path)
    got = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tracker_events'"
    ).fetchone()
    conn.close()
    assert got is None

    # Reopen: v27 migration re-runs, creates tracker_events, preserves the run.
    with SqliteRunStore(db_path) as store:
        assert store.get("r-keep") is not None
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION
        assert store.record_tracker_event(
            TrackerEvent(
                delivery_id="d1",
                source="linear",
                event_type="Issue",
                action="create",
                entity_id="issue-1",
                webhook_timestamp=1_700_000_000_000,
                payload='{"type":"Issue"}',
            )
        )
        rows = store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        assert rows[0].delivery_id == "d1"


def test_v28_creates_epic_graph_cache_preserves_data(tmp_path):
    """Reopening a pre-v28 DB creates the off-chain epic_graph_cache table
    without disturbing existing rows (W2/#894)."""
    import sqlite3 as _sqlite3

    db_path = str(tmp_path / "dispatch.db")
    # Build a current DB with a run, then simulate a pre-v28 state: drop the
    # epic_graph_cache table and stamp the version back to 27.
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=0.25,
            )
        )
        store._conn.execute("DROP TABLE IF EXISTS epic_graph_cache")
        store._conn.execute("UPDATE schema_version SET version = 27 WHERE id = 1")
        store._conn.commit()

    # Confirm the table is really gone at v27.
    conn = _sqlite3.connect(db_path)
    got = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='epic_graph_cache'"
    ).fetchone()
    conn.close()
    assert got is None

    # Reopen: v28 migration re-runs, creates epic_graph_cache, preserves the run.
    with SqliteRunStore(db_path) as store:
        assert store.get("r-keep") is not None
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION
        assert store.get_epic_graph_cache("epic-x") is None
        fetched_at = datetime(2026, 7, 20, tzinfo=UTC)
        store.put_epic_graph_cache(
            "epic-x", "linear", fetched_at, '{"nodes": [], "edges": []}'
        )
        cached = store.get_epic_graph_cache("epic-x")
        assert cached == (fetched_at, '{"nodes": [], "edges": []}')


def test_v30_adds_first_edit_columns_preserves_data(tmp_path):
    """Reopening a pre-v30 DB adds the first-edit metric columns as NULL
    without disturbing existing rows (D2)."""
    db_path = str(tmp_path / "dispatch.db")
    # Build a current DB with a run, then simulate a pre-v30 state: drop the
    # two first-edit columns and stamp the version back to 29.
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=0.25,
            )
        )
        store._conn.execute("ALTER TABLE runs DROP COLUMN steps_to_first_edit")
        store._conn.execute("ALTER TABLE runs DROP COLUMN tokens_to_first_edit")
        store._conn.execute("UPDATE schema_version SET version = 29 WHERE id = 1")
        store._conn.commit()

    # Reopen: v30 migration re-runs, adds the columns (NULL), preserves the run.
    with SqliteRunStore(db_path) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "steps_to_first_edit" in cols
        assert "tokens_to_first_edit" in cols
        rec = store.get("r-keep")
        assert rec is not None
        # Existing row migrates to NULL — never measured, distinct from 0.
        assert rec.steps_to_first_edit is None
        assert rec.tokens_to_first_edit is None
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_first_edit_metrics_round_trip(tmp_path):
    """A run persisted with first-edit metrics reads them back unchanged (D2)."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="r-fe",
                issue_num=5,
                branch="agent/issue-5",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                steps_to_first_edit=3,
                tokens_to_first_edit=705,
            )
        )
        got = store.get("r-fe")
        assert got is not None
        assert got.steps_to_first_edit == 3
        assert got.tokens_to_first_edit == 705


def test_v30_adds_injected_context_columns_preserves_data(tmp_path):
    """Reopening a pre-v30 DB adds the injected-context size columns as NULL
    without disturbing existing rows (Task 2, spec D2 / R1-8)."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep-ctx",
                issue_num=9,
                branch="agent/issue-9",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-6",
                complexity="medium",
                estimated_cost_usd=0.25,
            )
        )
        store._conn.execute("ALTER TABLE runs DROP COLUMN map_tokens")
        store._conn.execute("ALTER TABLE runs DROP COLUMN lesson_corpus_tokens")
        store._conn.execute("UPDATE schema_version SET version = 29 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "map_tokens" in cols
        assert "lesson_corpus_tokens" in cols
        rec = store.get("r-keep-ctx")
        assert rec is not None
        # Existing row migrates to NULL — never measured, distinct from 0.
        assert rec.map_tokens is None
        assert rec.lesson_corpus_tokens is None


def test_v31_repairs_v30_db_missing_injected_context_columns(tmp_path):
    """A DB stamped v30 by Task 1's merged code (before Task 2 amended
    _apply_v30) lacks map_tokens/lesson_corpus_tokens; because the v30 step is
    gated on version < 30 it never re-runs, so without a v31 step every
    store.save() raises OperationalError (PR #1033 adversarial-review finding,
    the amended-v30 bricking window). v31 must add the columns idempotently."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-v30-window",
                issue_num=11,
                branch="agent/issue-11",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        store._conn.execute("ALTER TABLE runs DROP COLUMN map_tokens")
        store._conn.execute("ALTER TABLE runs DROP COLUMN lesson_corpus_tokens")
        # Version stays 30 — the live-DB state Task 1's code left behind.
        store._conn.execute("UPDATE schema_version SET version = 30 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "map_tokens" in cols
        assert "lesson_corpus_tokens" in cols
        rec = store.get("r-v30-window")
        assert rec is not None
        assert rec.map_tokens is None
        # save() must work again — the bricked operation in the live incident.
        store.save(
            RunRecord(
                run_id="r-post-repair",
                issue_num=12,
                branch="agent/issue-12",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
                map_tokens=7,
            )
        )
        got = store.get("r-post-repair")
        assert got is not None
        assert got.map_tokens == 7


def test_injected_context_sizes_round_trip(tmp_path):
    """A run persisted with injected-context sizes reads them back unchanged."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="r-ctx",
                issue_num=6,
                branch="agent/issue-6",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                map_tokens=1234,
                lesson_corpus_tokens=567,
            )
        )
        got = store.get("r-ctx")
        assert got is not None
        assert got.map_tokens == 1234
        assert got.lesson_corpus_tokens == 567


def test_current_schema_version_matches_apply_method_count():
    import re

    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    n = len([m for m in dir(SqliteRunStore) if re.fullmatch(r"_apply_v\d+", m)])
    assert n == CURRENT_SCHEMA_VERSION


def test_open_rejects_newer_schema(tmp_path):
    import sqlite3 as _sqlite3

    from kew.run_store import (
        CURRENT_SCHEMA_VERSION,
        SchemaTooNewError,
        SqliteRunStore,
    )

    db = str(tmp_path / "d.db")
    with SqliteRunStore(db):
        pass  # creates the current schema
    conn = _sqlite3.connect(db)
    conn.execute(
        "UPDATE schema_version SET version = ? WHERE id = 1",
        (CURRENT_SCHEMA_VERSION + 1,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchemaTooNewError):
        SqliteRunStore(db)


def test_open_accepts_current_and_fresh(tmp_path):
    from kew.run_store import SqliteRunStore

    db = str(tmp_path / "d.db")
    with SqliteRunStore(db):  # fresh (version 0 -> migrates) must not raise
        pass
    with SqliteRunStore(db):  # current version must not raise
        pass


def _usage(**over):
    from kew.run_store import InteractiveUsage

    base = dict(
        runner="claude-code",
        model="claude-opus-4-8",
        observed_at="2026-06-26T12:00:00+00:00",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
        cost_usd=0.42,
        session_id="sess-1",
        ingest_source="file:x.json",
        dedup_key="key-1",
    )
    base.update(over)
    return InteractiveUsage(**base)


def test_interactive_usage_roundtrip(tmp_path):
    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.record_interactive_usage(_usage()) is True
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        assert rows[0].model == "claude-opus-4-8"
        assert rows[0].cost_usd == 0.42
        assert rows[0].input_tokens == 100


def test_interactive_usage_dedups_on_key(tmp_path):
    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.record_interactive_usage(_usage(dedup_key="same")) is True
        # identical key, even with different values, is dropped
        assert (
            store.record_interactive_usage(_usage(dedup_key="same", cost_usd=9.9))
            is False
        )
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        assert rows[0].cost_usd == 0.42  # the first write wins


def test_interactive_usage_distinct_keys_both_kept(tmp_path):
    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_interactive_usage(_usage(dedup_key="a"))
        store.record_interactive_usage(_usage(dedup_key="b"))
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert len(rows) == 2


def test_interactive_usage_window_filters(tmp_path):
    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_interactive_usage(
            _usage(dedup_key="old", observed_at="2020-01-01T00:00:00+00:00")
        )
        store.record_interactive_usage(
            _usage(dedup_key="new", observed_at="2026-06-26T12:00:00+00:00")
        )
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert [r.dedup_key for r in rows] == ["new"]


def test_ingestion_does_not_touch_audit_chain(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        before = store.list_audit_events(limit=None)
        store.record_interactive_usage(_usage())
        after = store.list_audit_events(limit=None)
        assert len(before) == len(after) == 0  # off-chain by construction


# ── interactive_usage per-subagent attribution (v18 migration) (#382) ────


def test_v18_adds_query_source_and_agent_name_columns(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        cols = {
            row[1]
            for row in store._conn.execute(
                "PRAGMA table_info(interactive_usage)"
            ).fetchall()
        }
        assert "query_source" in cols
        assert "agent_name" in cols


def test_interactive_usage_roundtrip_with_attribution(tmp_path):
    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_interactive_usage(_usage(dedup_key="main-1", query_source="main"))
        store.record_interactive_usage(
            _usage(
                dedup_key="sub-1",
                query_source="subagent",
                agent_name="custom",
            )
        )
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert len(rows) == 2
        by_key = {r.dedup_key: r for r in rows}
        assert by_key["main-1"].query_source == "main"
        assert by_key["main-1"].agent_name is None
        assert by_key["sub-1"].query_source == "subagent"
        assert by_key["sub-1"].agent_name == "custom"


def test_v18_migration_pre_existing_rows_read_null(tmp_path):
    import sqlite3 as _sqlite3

    db = str(tmp_path / "d.db")
    conn = _sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)"
    )
    conn.execute("INSERT INTO schema_version VALUES (1, 17)")
    conn.execute(
        "CREATE TABLE interactive_usage ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " runner TEXT, model TEXT, observed_at TEXT,"
        " input_tokens INTEGER, output_tokens INTEGER,"
        " cache_creation_tokens INTEGER, cache_read_tokens INTEGER,"
        " cost_usd REAL, session_id TEXT, ingest_source TEXT,"
        " ingested_at TEXT, dedup_key TEXT UNIQUE,"
        " prefix_key TEXT, provider TEXT)"
    )
    conn.execute(
        "INSERT INTO interactive_usage"
        " (runner, model, observed_at, input_tokens, output_tokens,"
        "  cache_creation_tokens, cache_read_tokens, cost_usd,"
        "  session_id, ingest_source, ingested_at, dedup_key)"
        " VALUES ('claude-code', 'opus', '2026-06-26T00:00:00+00:00',"
        "  100, 50, 0, 0, 0.42, 's1', 'file:x', '2026-06-26T00:00:00', 'k1')"
    )
    conn.commit()
    conn.close()

    from datetime import UTC, datetime

    from kew.run_store import SqliteRunStore

    with SqliteRunStore(db) as store:
        rows = store.list_interactive_usage_since(datetime(2026, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        assert rows[0].query_source is None
        assert rows[0].agent_name is None


# ── config_overrides (v17 migration + CRUD) (#368) ──────────────────


def test_v17_creates_config_overrides_table(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "config_overrides" in tables
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_v17_migration_is_idempotent(tmp_path):
    from kew.run_store import SqliteRunStore

    db = str(tmp_path / "d.db")
    with SqliteRunStore(db) as store:
        store.set_config_override(
            field="budget.per_issue_usd",
            value_json="25.0",
            file_value_json="15.0",
            set_at="2026-06-29T00:00:00+00:00",
            set_by="test",
        )
    with SqliteRunStore(db) as store:
        row = store.get_config_override("budget.per_issue_usd")
        assert row is not None
        assert row["value_json"] == "25.0"
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_pre_v17_upgrade_clean(tmp_path):
    """A pre-v17 DB (no config_overrides table) upgrades cleanly."""
    import sqlite3 as _sqlite3

    from kew.run_store import SqliteRunStore

    db = str(tmp_path / "d.db")
    with SqliteRunStore(db):
        pass
    conn = _sqlite3.connect(db)
    conn.execute("UPDATE schema_version SET version = 16 WHERE id = 1")
    conn.execute("DROP TABLE IF EXISTS config_overrides")
    conn.commit()
    conn.close()
    with SqliteRunStore(db) as store:
        assert store.list_config_overrides() == []
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION


def test_config_override_crud_round_trip(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.list_config_overrides() == []
        assert store.get_config_override("budget.daily_usd") is None

        store.set_config_override(
            field="budget.daily_usd",
            value_json="100.0",
            file_value_json="50.0",
            set_at="2026-06-29T12:00:00+00:00",
            set_by="human:tom",
            reason="testing",
            audit_event_id=42,
        )
        row = store.get_config_override("budget.daily_usd")
        assert row is not None
        assert row["value_json"] == "100.0"
        assert row["file_value_json"] == "50.0"
        assert row["set_by"] == "human:tom"
        assert row["reason"] == "testing"
        assert row["audit_event_id"] == 42

        assert len(store.list_config_overrides()) == 1

        store.set_config_override(
            field="budget.daily_usd",
            value_json="200.0",
            file_value_json="50.0",
            set_at="2026-06-29T13:00:00+00:00",
            set_by="human:tom",
        )
        row = store.get_config_override("budget.daily_usd")
        assert row is not None
        assert row["value_json"] == "200.0"
        assert len(store.list_config_overrides()) == 1

        assert store.revert_config_override("budget.daily_usd") is True
        assert store.get_config_override("budget.daily_usd") is None
        assert store.revert_config_override("budget.daily_usd") is False


class TestMedianCostForBucket:
    """median_cost_for_bucket drops degenerate near-zero-cost samples (#668)."""

    def test_excludes_near_zero_cost_rows_from_median_and_count(self, tmp_path):
        db_path = str(tmp_path / "median.db")
        with SqliteRunStore(db_path) as store:
            # Three legitimate samples.
            for i, cost in enumerate((4.0, 6.0, 8.0)):
                store.save(
                    _make_record(
                        f"real{i}",
                        model="opus",
                        complexity="medium",
                        estimated_cost_usd=cost,
                    )
                )
            # Two degenerate $0.00 lemonade-batch rows in the same bucket.
            for i in range(2):
                store.save(
                    _make_record(
                        f"zero{i}",
                        model="opus",
                        complexity="medium",
                        status="failure",
                        exit_code=1,
                        estimated_cost_usd=0.0,
                    )
                )

            med, count = store.median_cost_for_bucket("opus", "medium")

            # Median is over the three real rows (6.0), not poisoned to 0.0.
            assert med == 6.0
            # Count reflects only the filtered (non-degenerate) rows.
            assert count == 3

    def test_excludes_a_success_flagged_as_a_cost_anomaly(self, tmp_path):
        """#1798: a run flagged on its closing line finished as a success, but
        its cost was anomalous, so it does not raise the median it crossed."""
        with SqliteRunStore(str(tmp_path / "median.db")) as store:
            for i, cost in enumerate((4.0, 6.0)):
                store.save(
                    _make_record(
                        f"real{i}",
                        model="opus",
                        complexity="medium",
                        estimated_cost_usd=cost,
                    )
                )
            store.save(
                _make_record(
                    "flagged",
                    model="opus",
                    complexity="medium",
                    estimated_cost_usd=90.0,
                    error_category="cost_anomaly_flagged",
                )
            )

            assert store.median_cost_for_bucket("opus", "medium") == (5.0, 2)

    def test_all_near_zero_bucket_is_empty(self, tmp_path):
        db_path = str(tmp_path / "median_empty.db")
        with SqliteRunStore(db_path) as store:
            for i in range(4):
                store.save(
                    _make_record(
                        f"zero{i}",
                        model="opus",
                        complexity="high",
                        estimated_cost_usd=0.005,
                    )
                )

            med, count = store.median_cost_for_bucket("opus", "high")

            assert med is None
            assert count == 0


class TestOrphanPrsMigration:
    """v23 adds the orphan_prs table (schema-migration skill: data-preserving)."""

    def test_v22_db_migrates_up_and_preserves_runs(self, tmp_path):
        """A pre-v23 DB (no orphan_prs table) migrates without losing runs data."""
        db_path = str(tmp_path / "v22.db")
        # Build a current DB, then simulate a pre-v23 state: drop orphan_prs and
        # stamp the version back to 22 with a runs row present.
        with SqliteRunStore(db_path) as store:
            store.save(
                RunRecord(
                    run_id="pre-v23",
                    issue_num=7,
                    branch="agent/issue-7",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
            store._conn.execute("DROP TABLE IF EXISTS orphan_prs")
            store._conn.execute("UPDATE schema_version SET version = 22 WHERE id = 1")
            store._conn.commit()

        # Re-open: v23 migration re-runs, recreating orphan_prs, runs intact.
        with SqliteRunStore(db_path) as store:
            record = store.get("pre-v23")
            assert record is not None
            assert record.branch == "agent/issue-7"
            tbl = store._conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='orphan_prs'"
            ).fetchone()
            assert tbl is not None
            cols = {
                r["name"] for r in store._conn.execute("PRAGMA table_info(orphan_prs)")
            }
            assert {
                "pr_number",
                "url",
                "branch",
                "reason",
                "detected_at",
                "acked_at",
            }.issubset(cols)
            row = store._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            assert row["version"] == CURRENT_SCHEMA_VERSION

    def test_fresh_db_has_orphan_prs_table(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "fresh.db")) as store:
            tbl = store._conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='orphan_prs'"
            ).fetchone()
            assert tbl is not None


class TestItemIdMigrationV24:
    """v24 adds item_id (backfilled from issue_num) + item_id/branch indexes."""

    @staticmethod
    def _indexes(store: SqliteRunStore) -> set[str]:
        return {
            r["name"]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

    def test_fresh_db_has_item_id_columns_and_indexes(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "fresh.db")) as store:
            for table in ("runs", "loops", "audit_events"):
                cols = {
                    r["name"]
                    for r in store._conn.execute(f"PRAGMA table_info({table})")
                }
                assert "item_id" in cols, f"{table} missing item_id"
            assert {
                "idx_runs_item_id",
                "idx_loops_item_id",
                "idx_runs_branch",
            }.issubset(self._indexes(store))

    def test_migration_is_idempotent_across_reconnects(self, tmp_path):
        """Reopening the same DB three times must not raise or corrupt state."""
        db_path = str(tmp_path / "d.db")
        for _ in range(3):
            with SqliteRunStore(db_path) as store:
                row = store._conn.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()
                assert row["version"] == CURRENT_SCHEMA_VERSION
                assert {
                    "idx_runs_item_id",
                    "idx_loops_item_id",
                    "idx_runs_branch",
                }.issubset(self._indexes(store))

    def test_rewind_and_reopen_backfills_and_backs_up(self, tmp_path):
        """A pre-v24 DB backfills item_id from issue_num and lands a backup."""
        from kew.run_store import CURRENT_SCHEMA_VERSION

        db_path = str(tmp_path / "dispatch.db")
        # Build a current DB with a run + loop, then simulate a pre-v24 state:
        # drop the item_id columns/indexes and stamp the version back to 23.
        with SqliteRunStore(db_path) as store:
            store.save(
                RunRecord(
                    run_id="r-42",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
            store.append_audit_event(
                timestamp=datetime.now(UTC).isoformat(),
                event="test.event",
                issue_num=9,
            )
            store._conn.execute(
                "INSERT INTO loops (loop_id, source, issue_num, goal_text, created_at)"
                " VALUES ('L-7', 'issue', 7, 'do the thing', ?)",
                (datetime.now(UTC).isoformat(),),
            )
            store._conn.execute("DROP INDEX IF EXISTS idx_runs_item_id")
            store._conn.execute("DROP INDEX IF EXISTS idx_loops_item_id")
            store._conn.execute("DROP INDEX IF EXISTS idx_runs_branch")
            store._conn.execute("DROP INDEX IF EXISTS idx_audit_events_item_event")
            for table in ("runs", "loops", "audit_events"):
                store._conn.execute(f"ALTER TABLE {table} DROP COLUMN item_id")
            store._conn.execute("UPDATE schema_version SET version = 23 WHERE id = 1")
            store._conn.commit()

        # Re-open: v24 migration re-runs, backfills item_id, snapshots a backup.
        with SqliteRunStore(db_path) as store:
            run_item = store._conn.execute(
                "SELECT item_id FROM runs WHERE run_id = 'r-42'"
            ).fetchone()["item_id"]
            assert run_item == "42"
            loop_item = store._conn.execute(
                "SELECT item_id FROM loops WHERE loop_id = 'L-7'"
            ).fetchone()["item_id"]
            assert loop_item == "7"
            audit_item = store._conn.execute(
                "SELECT item_id FROM audit_events WHERE issue_num = 9"
            ).fetchone()["item_id"]
            assert audit_item == "9"
            assert (
                store._conn.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()["version"]
                == CURRENT_SCHEMA_VERSION
            )

        backups = list(
            tmp_path.glob(f"dispatch.db.bak-pre-v{CURRENT_SCHEMA_VERSION}-*")
        )
        assert len(backups) == 1, f"expected one backup, got {backups}"


class TestEpicItemIdMigrationV25:
    """v25 keys the epic tables on item_id (int shims for GitHub callers)."""

    @staticmethod
    def _indexes(store: SqliteRunStore) -> set[str]:
        return {
            r["name"]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

    def test_fresh_db_has_item_id_columns_and_unique_indexes(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "fresh.db")) as store:
            for table in ("epic_reservations", "epic_holds", "epic_retries"):
                cols = {
                    r["name"]
                    for r in store._conn.execute(f"PRAGMA table_info({table})")
                }
                assert "item_id" in cols, f"{table} missing item_id"
            assert {
                "idx_epic_res_item",
                "idx_epic_holds_item",
                "idx_epic_retries_item",
            }.issubset(self._indexes(store))

    def test_github_reservation_round_trips_via_int_shim(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            assert store.reserve_epic_node("epic-a", 42, "run-42") is True
            res = store.list_epic_reservations("epic-a")
            assert len(res) == 1
            assert res[0].issue_num == 42
            assert res[0].item_id == "42"
            # A second reservation of the same node conflicts (UNIQUE item_id).
            assert store.reserve_epic_node("epic-a", 42, "run-42b") is False
            store.release_epic_node(42, run_id="run-42")
            assert store.list_epic_reservations("epic-a") == []

    def test_multiple_linear_items_do_not_collide_on_sentinel(self, tmp_path):
        """Two non-numeric ids both map to issue_num=0 on read, but must be
        distinct reservations — the 0 sentinel must NOT collapse them."""
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            assert store.reserve_epic_node_item("epic-a", "SFD-1", "run-1") is True
            assert store.reserve_epic_node_item("epic-a", "SFD-2", "run-2") is True
            res = store.list_epic_reservations("epic-a")
            assert {r.item_id for r in res} == {"SFD-1", "SFD-2"}
            assert [r.issue_num for r in res] == [0, 0]
            # re-reserving SFD-1 conflicts; releasing it drops only that one.
            assert store.reserve_epic_node_item("epic-a", "SFD-1", "r1b") is False
            store.release_epic_node_item("SFD-1")
            assert {r.item_id for r in store.list_epic_reservations("epic-a")} == {
                "SFD-2"
            }

    def test_holds_and_retries_key_on_item_id(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.hold_epic_node_item("epic-a", "SFD-9", "ci_pending")
            store.hold_epic_node_item("epic-a", "SFD-9", "budget_preflight")  # upsert
            holds = store.list_epic_holds("epic-a")
            assert len(holds) == 1
            assert holds[0].item_id == "SFD-9"
            assert holds[0].reason == "budget_preflight"
            store.clear_epic_hold_item("SFD-9")
            assert store.list_epic_holds("epic-a") == []

            store.mark_epic_retry_item("epic-a", "SFD-9")
            retries = store.list_epic_retries("epic-a")
            assert [r.item_id for r in retries] == ["SFD-9"]

    def test_clear_epic_hold_keys_on_item_id_not_issue_num(self, tmp_path):
        """The epic-hold API is item-id currency: ``clear_epic_hold`` and
        ``hold_epic_node`` declare ``item: str`` and coerce a legacy int at the
        boundary. Regression for the 2026-07-19 incident where an operator
        cleared a hold row ``{issue_num: 1, item_id: '792'}`` by its rowid and
        it silently matched nothing."""
        import inspect

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            # Signature honesty: the holds surface speaks item ids, not ints.
            assert "item" in inspect.signature(store.clear_epic_hold).parameters
            assert "item" in inspect.signature(store.hold_epic_node).parameters

            # item-keyed hold clears by its string id...
            store.hold_epic_node_item("epic-a", "792", "ci_pending")
            store.clear_epic_hold("792")
            assert store.list_epic_holds("epic-a") == []

            # ...and by a legacy int coerced at the boundary.
            store.hold_epic_node_item("epic-a", "792", "ci_pending")
            store.clear_epic_hold(792)
            assert store.list_epic_holds("epic-a") == []

            # A non-numeric Linear id clears by that exact string.
            store.hold_epic_node("epic-a", "SFD-133", "ci_pending")
            assert [h.item_id for h in store.list_epic_holds("epic-a")] == ["SFD-133"]
            store.clear_epic_hold("SFD-133")
            assert store.list_epic_holds("epic-a") == []

    def test_linear_reservation_counts_as_distinct_live_slots(self, tmp_path):
        """A Linear reservation must count toward the fleet cap under its own
        item_id, not collapse onto the 0 sentinel (worker-cap correctness)."""
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.reserve_epic_node_item("epic-a", "SFD-1", "run-1")
            store.reserve_epic_node_item("epic-a", "SFD-2", "run-2")
            assert store.live_run_count() == 2

    def test_linear_reservation_does_not_wedge_a_later_github_issue(self, tmp_path):
        """Regression: a Linear reservation must not consume a GitHub issue
        number in the PK column — reserving a GitHub node whose number equals a
        prior Linear row's auto-rowid must still succeed (issue_num is a
        vestigial auto-rowid; item_id is the only atomic key)."""
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            # A Linear reservation lands first; its auto-rowid would be 1.
            assert store.reserve_epic_node_item("epic-a", "SFD-9", "run-l") is True
            # GitHub issue #1 must still reserve cleanly (no PK collision).
            assert store.reserve_epic_node("epic-a", 1, "run-g") is True
            items = {r.item_id for r in store.list_epic_reservations("epic-a")}
            assert items == {"SFD-9", "1"}

    def test_rewind_to_v24_backfills_item_id_from_issue_num(self, tmp_path):
        """A genuine pre-v25 epic row (issue_num-keyed, no item_id column)
        backfills item_id from issue_num on reopen, staying resolvable."""
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as store:
            store._conn.execute("DROP INDEX IF EXISTS idx_epic_res_item")
            store._conn.execute("ALTER TABLE epic_reservations DROP COLUMN item_id")
            # Old code wrote the real issue number into the PK column.
            store._conn.execute(
                "INSERT INTO epic_reservations (issue_num, epic_ref, run_id,"
                " reserved_at) VALUES (7, 'epic-a', 'run-7', ?)",
                (datetime.now(UTC).isoformat(),),
            )
            store._conn.execute("UPDATE schema_version SET version = 24 WHERE id = 1")
            store._conn.commit()
        with SqliteRunStore(db_path) as store:
            res = store.list_epic_reservations("epic-a")
            assert len(res) == 1
            assert res[0].item_id == "7"
            assert res[0].issue_num == 7


class TestSyncOrphanPrs:
    """store.sync_orphan_prs upserts detected orphans and clears resolved ones."""

    def test_upsert_inserts_new_orphans(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.sync_orphan_prs(
                [
                    (10, "https://u/10", "agent/issue-1", "failed-run"),
                    (12, "https://u/12", "agent/issue-3", "no-record"),
                ]
            )
            got = {o.pr_number: o for o in store.list_orphan_prs()}
            assert set(got) == {10, 12}
            assert got[10].url == "https://u/10"
            assert got[10].branch == "agent/issue-1"
            assert got[10].reason == "failed-run"
            assert got[10].detected_at is not None
            assert got[10].acked_at is None

    def test_upsert_preserves_detected_at_and_acked_at(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.sync_orphan_prs([(10, "https://u/10", "agent/issue-1", "no-record")])
            first = {o.pr_number: o for o in store.list_orphan_prs()}[10]
            # Simulate an ack (Group 3 owns the verb; here we set the column).
            store._conn.execute(
                "UPDATE orphan_prs SET acked_at = ? WHERE pr_number = 10",
                ("2026-07-16T00:00:00+00:00",),
            )
            store._conn.commit()
            # Re-sync: reason may change, detected_at/acked_at must survive.
            store.sync_orphan_prs([(10, "https://u/10", "agent/issue-1", "failed-run")])
            again = {o.pr_number: o for o in store.list_orphan_prs(include_acked=True)}[
                10
            ]
            assert again.reason == "failed-run"
            assert again.detected_at == first.detected_at
            assert again.acked_at is not None

    def test_sync_clears_rows_no_longer_orphaned(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.sync_orphan_prs(
                [
                    (10, "https://u/10", "agent/issue-1", "failed-run"),
                    (12, "https://u/12", "agent/issue-3", "no-record"),
                ]
            )
            # Next sweep: PR 10 resolved (closed or branch gained success).
            store.sync_orphan_prs([(12, "https://u/12", "agent/issue-3", "no-record")])
            got = {o.pr_number for o in store.list_orphan_prs()}
            assert got == {12}


class TestListOrphanPrsFilter:
    """list_orphan_prs hides acked rows unless include_acked=True."""

    def test_acked_filtering(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.sync_orphan_prs(
                [
                    (10, "https://u/10", "agent/issue-1", "failed-run"),
                    (12, "https://u/12", "agent/issue-3", "no-record"),
                ]
            )
            store._conn.execute(
                "UPDATE orphan_prs SET acked_at = ? WHERE pr_number = 10",
                ("2026-07-16T00:00:00+00:00",),
            )
            store._conn.commit()
            unacked = {o.pr_number for o in store.list_orphan_prs()}
            assert unacked == {12}
            allrows = {o.pr_number for o in store.list_orphan_prs(include_acked=True)}
            assert allrows == {10, 12}


class TestListAllEpicHolds:
    """list_all_epic_holds returns holds across every epic_ref."""

    def test_returns_holds_from_multiple_epics(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.hold_epic_node("epic-a", 1, "ci_pending")
            store.hold_epic_node("epic-b", 2, "review_pending")
            store.hold_epic_node("epic-a", 3, "ci_pending")
            holds = store.list_all_epic_holds()
            by_issue = {h.issue_num: h for h in holds}
            assert set(by_issue) == {1, 2, 3}
            assert by_issue[1].epic_ref == "epic-a"
            assert by_issue[2].epic_ref == "epic-b"
            assert by_issue[2].reason == "review_pending"

    def test_empty_when_no_holds(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            assert store.list_all_epic_holds() == []


class TestSharedConnectionSerialization:
    """I7: a single connection shared across threads (check_same_thread=False,
    as the TUI opens it) needs an in-process lock so two threads cannot
    interleave a BEGIN IMMEDIATE...COMMIT section — a second BEGIN IMMEDIATE
    mid-transaction raises "cannot start a transaction within a transaction"."""

    def _run(self, issue_num: int, run_id: str) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
        )

    def test_concurrent_writes_on_shared_connection_do_not_error(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        # One store / one connection, shared across every worker thread.
        store = SqliteRunStore(db_path, check_same_thread=False)
        try:
            barrier = threading.Barrier(6)
            errors: list[Exception] = []

            def admit(i: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    for j in range(5):
                        store.try_admit_run(
                            self._run(i * 100 + j, f"r-{i}-{j}"),
                            0,  # cap<=0 disables the count gate; always inserts
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            def audit(i: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    for j in range(5):
                        store.append_audit_event(
                            timestamp=f"2026-06-10T12:00:{j:02d}",
                            event=f"e-{i}-{j}",
                        )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=admit, args=(i,)) for i in range(3)]
            threads += [threading.Thread(target=audit, args=(i,)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert all(not t.is_alive() for t in threads)
            assert errors == [], f"unexpected errors: {errors}"
            # All writes landed: 3 threads * 5 pending rows, 3 * 5 audit events.
            assert len(store.list_pending()) == 15
            assert len(store.list_audit_events()) == 15
        finally:
            store.close()


class TestListSinceIndex:
    """I8: list_since's started_at range must seek an index, not full-scan."""

    def _make_record(self, run_id: str, started_at: datetime) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=started_at,
        )

    def test_started_at_index_exists(self, tmp_path):
        db_path = str(tmp_path / "idx.db")
        with SqliteRunStore(db_path):
            pass
        conn = sqlite3.connect(db_path)
        try:
            cols = conn.execute("PRAGMA index_info('idx_runs_started')").fetchall()
            assert [row[2] for row in cols] == ["started_at"]
        finally:
            conn.close()

    def test_list_since_uses_index_seek_not_full_scan(self, tmp_path):
        db_path = str(tmp_path / "plan.db")
        since = datetime(2026, 1, 1, tzinfo=UTC)
        with SqliteRunStore(db_path) as store:
            plan = store._conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at",
                ("2026-01-01T00:00:00",),
            ).fetchall()
        detail = " ".join(str(row[3]) for row in plan)
        assert "idx_runs_started" in detail
        assert "SEARCH" in detail  # a range seek, not SCAN
        assert "USE TEMP B-TREE" not in detail  # index provides the ordering
        del since

    def test_list_since_returns_legacy_naive_and_tz_rows(self, tmp_path):
        """The single-bound query still returns legacy 19-char rows and
        tz-suffixed rows alike (the OR branch it replaced covered both)."""
        db_path = str(tmp_path / "legacy.db")
        with SqliteRunStore(db_path) as store:
            store.save(self._make_record("tz-row", datetime(2026, 6, 1, tzinfo=UTC)))
            # Hand-write a legacy row whose started_at lacks a tz suffix.
            store._conn.execute(
                "UPDATE runs SET started_at = '2026-06-02T00:00:00'"
                " WHERE run_id = 'tz-row'"
            )
            store.save(self._make_record("newer", datetime(2026, 6, 3, tzinfo=UTC)))
            store._conn.commit()

            results = store.list_since(datetime(2026, 1, 1, tzinfo=UTC))
            ids = {r.run_id for r in results}
        assert ids == {"tz-row", "newer"}


def test_v34_adds_planned_cost_column_preserves_data(tmp_path):
    """Reopening a pre-v34 DB adds ``planned_cost_usd`` as NULL without
    disturbing existing rows — historical runs have no recorded planned bound,
    which is distinct from a bound of $0 (#1092)."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep-cost",
                issue_num=351,
                branch="agent/issue-351",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="qwen/qwen3-coder",
                estimated_cost_usd=0.1064,
            )
        )
        store._conn.execute("ALTER TABLE runs DROP COLUMN planned_cost_usd")
        store._conn.execute("UPDATE schema_version SET version = 33 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        cols = {
            row[1] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "planned_cost_usd" in cols
        rec = store.get("r-keep-cost")
        assert rec is not None
        assert rec.estimated_cost_usd == 0.1064  # actual untouched
        assert rec.planned_cost_usd is None  # backfilled NULL, not 0.0


def test_planned_actual_cost_pair_round_trips(tmp_path):
    """A run persisted with both halves of the pair reads them back unchanged
    (the #351 baseline: planned bound $1.0787 vs native actual $0.1064)."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="r-pair",
                issue_num=351,
                branch="agent/issue-351",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                estimated_cost_usd=0.1064,
                planned_cost_usd=1.0787,
            )
        )
        got = store.get("r-pair")
        assert got is not None
        assert got.estimated_cost_usd == 0.1064
        assert got.planned_cost_usd == 1.0787


class TestAdmissionIdentity:
    """I01: the immutable composite identity persisted at admission.

    Every assertion here is about the question the run store could not answer
    before: *what work was this run admitted for* — without parsing a title, a
    branch, a numeric shim, or the inference-provider column.
    """

    def _record(self, run_id: str, **overrides):
        fields = {
            "run_id": run_id,
            "issue_num": 0,
            "branch": f"agent/{run_id}",
            "status": "pending",
            "exit_code": -1,
            "started_at": datetime.now(UTC),
        }
        fields.update(overrides)
        return RunRecord(**fields)

    def test_admission_persists_tracker_item_repo_run_and_attempt(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            identity = AdmissionIdentity(
                run_id="r1",
                attempt_number=1,
                source_tracker="linear",
                item_id="SFD-12",
                delivery_repo="acme/widgets",
            )
            assert store.admit_run_with_identity(self._record("r1"), identity)

            loaded = store.get_admission_identity("r1")
            assert loaded == identity

    def test_github_numeric_item_round_trips_without_a_shim(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.admit_run_with_identity(
                self._record("r2", issue_num=42, item_id="42"),
                AdmissionIdentity(
                    run_id="r2",
                    source_tracker="github",
                    item_id="42",
                    delivery_repo="acme/widgets",
                ),
            )
            loaded = store.get_admission_identity("r2")
            assert loaded is not None
            assert loaded.source_tracker == "github"
            assert loaded.item_id == "42"
            assert not loaded.is_synthetic

    def test_same_item_id_under_two_trackers_stays_distinct(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.admit_run_with_identity(
                self._record("gh"),
                AdmissionIdentity(run_id="gh", source_tracker="github", item_id="42"),
            )
            store.admit_run_with_identity(
                self._record("lin"),
                AdmissionIdentity(
                    run_id="lin", source_tracker="linear", item_id="SFD-42"
                ),
            )
            assert [
                i.run_id for i in store.find_admissions_for_item("github", "42")
            ] == ["gh"]
            assert [
                i.run_id for i in store.find_admissions_for_item("linear", "SFD-42")
            ] == ["lin"]

    def test_synthetic_run_records_a_nullable_identity_not_a_fake_item(self, tmp_path):
        """An ``epic plan``-shaped run has no work item; the row must say so."""
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.admit_run_with_identity(
                self._record("synthetic"),
                AdmissionIdentity(run_id="synthetic"),
            )
            loaded = store.get_admission_identity("synthetic")
            assert loaded is not None
            assert loaded.is_synthetic
            assert loaded.item_id is None
            assert loaded.source_tracker is None
            # runs.item_id backfills the fleet key with the run_id; that shim
            # must not leak into the identity table as a work item.
            assert store.get("synthetic").item_id == "synthetic"

    def test_inference_provider_is_rejected_as_a_tracker(self, tmp_path):
        from kew.run_store import (
            AdmissionIdentity,
            AdmissionIdentityError,
        )

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            with pytest.raises(AdmissionIdentityError, match="not a work tracker"):
                store.admit_run_with_identity(
                    self._record("bad"),
                    AdmissionIdentity(
                        run_id="bad", source_tracker="anthropic", item_id="42"
                    ),
                )
            assert store.get("bad") is None

    def test_item_id_must_be_canonical_for_its_tracker(self, tmp_path):
        from kew.run_store import AdmissionIdentity, AdmissionIdentityError

        with (
            SqliteRunStore(str(tmp_path / "d.db")) as store,
            pytest.raises(AdmissionIdentityError),
        ):
            store.admit_run_with_identity(
                self._record("bad2"),
                AdmissionIdentity(
                    run_id="bad2", source_tracker="github", item_id="SFD-12"
                ),
            )

    def test_item_id_without_a_tracker_is_rejected(self, tmp_path):
        from kew.run_store import AdmissionIdentity, AdmissionIdentityError

        with (
            SqliteRunStore(str(tmp_path / "d.db")) as store,
            pytest.raises(AdmissionIdentityError, match="no source_tracker"),
        ):
            store.admit_run_with_identity(
                self._record("bad3"),
                AdmissionIdentity(run_id="bad3", item_id="42"),
            )

    def test_identity_is_immutable_but_idempotent(self, tmp_path):
        from kew.run_store import (
            AdmissionIdentity,
            AdmissionIdentityConflictError,
        )

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            identity = AdmissionIdentity(
                run_id="r3", source_tracker="github", item_id="7"
            )
            store.admit_run_with_identity(self._record("r3"), identity)
            # Byte-identical re-admission is a no-op, not a conflict.
            assert store.admit_run_with_identity(self._record("r3"), identity)
            with pytest.raises(AdmissionIdentityConflictError):
                store.admit_run_with_identity(
                    self._record("r3"),
                    AdmissionIdentity(
                        run_id="r3", source_tracker="github", item_id="8"
                    ),
                )
            assert store.get_admission_identity("r3").item_id == "7"

    def test_attempts_are_recorded_separately_and_latest_wins(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            for attempt in (1, 2):
                store.admit_run_with_identity(
                    self._record("r4", attempt_number=attempt),
                    AdmissionIdentity(
                        run_id="r4",
                        attempt_number=attempt,
                        source_tracker="github",
                        item_id="9",
                    ),
                )
            assert [
                i.attempt_number for i in store.list_admission_identities("r4")
            ] == [
                1,
                2,
            ]
            assert store.get_admission_identity("r4").attempt_number == 2

    def test_attempt_number_below_one_is_rejected(self, tmp_path):
        from kew.run_store import AdmissionIdentity, AdmissionIdentityError

        with (
            SqliteRunStore(str(tmp_path / "d.db")) as store,
            pytest.raises(AdmissionIdentityError, match="attempt_number"),
        ):
            store.admit_run_with_identity(
                self._record("r5"),
                AdmissionIdentity(run_id="r5", attempt_number=0),
            )

    def test_epic_node_is_joined_from_the_reservation_not_guessed(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.reserve_epic_node_item("epic-alpha", "SFD-12", "reserver")
            identity = store.build_admission_identity(
                "r6", tracker="linear", item_id="SFD-12", repo="acme/widgets"
            )
            assert identity.epic_ref == "epic-alpha"
            assert identity.node_id == "SFD-12"

    def test_non_epic_run_has_no_epic_identity(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            identity = store.build_admission_identity(
                "r7", tracker="github", item_id="3", repo="acme/widgets"
            )
            assert identity.epic_ref is None
            assert identity.node_id is None

    def test_admission_respects_the_worker_cap_and_writes_no_identity(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.admit_run_with_identity(
                self._record("first", item_id="1"),
                AdmissionIdentity(run_id="first", source_tracker="github", item_id="1"),
                cap=1,
                exclude_item="1",
            )
            assert not store.admit_run_with_identity(
                self._record("second", item_id="2"),
                AdmissionIdentity(
                    run_id="second", source_tracker="github", item_id="2"
                ),
                cap=1,
                exclude_item="2",
            )
            assert store.get("second") is None
            assert store.get_admission_identity("second") is None

    def test_enricher_failure_rolls_back_the_run_and_its_identity(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:

            def _boom(_cursor):
                raise RuntimeError("enricher said no")

            with pytest.raises(RuntimeError, match="enricher said no"):
                store.admit_run_with_identity(
                    self._record("r8"),
                    AdmissionIdentity(
                        run_id="r8", source_tracker="github", item_id="5"
                    ),
                    enrich=_boom,
                )
            assert store.get("r8") is None
            assert store.get_admission_identity("r8") is None

    def test_enricher_writes_join_the_same_transaction(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            seen = {}

            def _enrich(cursor):
                cursor.execute(
                    "SELECT item_id FROM run_admission_identity WHERE run_id = ?",
                    ("r9",),
                )
                seen["item_id"] = cursor.fetchone()["item_id"]
                cursor.execute(
                    "UPDATE runs SET title = ? WHERE run_id = ?", ("t", "r9")
                )

            store.admit_run_with_identity(
                self._record("r9"),
                AdmissionIdentity(run_id="r9", source_tracker="github", item_id="6"),
                enrich=_enrich,
            )
            assert seen["item_id"] == "6"
            assert store.get("r9").title == "t"

    def test_legacy_rows_migrate_to_honest_partial_identity(self, tmp_path):
        """A pre-v42 DB keeps its rows and decodes as partial, never invented."""
        db_path = str(tmp_path / "legacy.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                RunRecord(
                    run_id="old-item",
                    issue_num=0,
                    branch="agent/SFD-1",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    item_id="SFD-1",
                    repo="acme/widgets",
                    provider="anthropic",
                )
            )
            store.save(
                RunRecord(
                    run_id="old-synthetic",
                    issue_num=0,
                    branch="epic/plan",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
            store.save(
                RunRecord(
                    run_id="old-standalone-review",
                    issue_num=87,
                    branch="review/pr-87",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    run_kind="review",
                )
            )
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE run_admission_identity")
        conn.execute("UPDATE schema_version SET version = 41 WHERE id = 1")
        conn.commit()
        conn.close()

        with SqliteRunStore(db_path) as store:
            item = store.get_admission_identity("old-item")
            assert item is not None
            # Never inferred: the tracker was not persisted before v42, an
            # item without a tracker to interpret it is not an identity, and
            # v24/v26 relabeling makes old review shims indistinguishable from
            # dispatches — so legacy rows keep NO item here. runs.item_id
            # still holds the legacy key for accounting.
            assert item.item_id is None
            assert item.is_synthetic
            assert item.delivery_repo == "acme/widgets"
            assert item.source_tracker is None
            assert item.epic_ref is None

            synthetic = store.get_admission_identity("old-synthetic")
            assert synthetic is not None
            assert synthetic.is_synthetic
            assert synthetic.item_id is None

            standalone_review = store.get_admission_identity("old-standalone-review")
            assert standalone_review is not None
            assert standalone_review.is_synthetic
            assert standalone_review.item_id is None

    def test_identity_survives_reopening_the_store(self, tmp_path):
        from kew.run_store import AdmissionIdentity

        db_path = str(tmp_path / "d.db")
        identity = AdmissionIdentity(
            run_id="r10",
            attempt_number=3,
            source_tracker="linear",
            item_id="SFD-99",
            delivery_repo="acme/widgets",
            epic_ref="epic-alpha",
            node_id="SFD-99",
        )
        with SqliteRunStore(db_path) as store:
            store.admit_run_with_identity(
                self._record("r10", attempt_number=3), identity
            )
        with SqliteRunStore(db_path) as store:
            assert store.get_admission_identity("r10") == identity


class TestReadSnapshot:
    """I02: one consistent read snapshot, so a composed view cannot be torn.

    ``WorkView`` composition reads several tables (runs, admission identity,
    epic reservations). Without a held read transaction a concurrent
    reconciliation could land between two of those reads and the composed view
    would show half of one world and half of the next — the exact torn state
    this primitive exists to make impossible.
    """

    def _record(self, run_id: str, **overrides):
        fields = {
            "run_id": run_id,
            "issue_num": 0,
            "branch": f"agent/{run_id}",
            "status": "pending",
            "exit_code": -1,
            "started_at": datetime.now(UTC),
        }
        fields.update(overrides)
        return RunRecord(**fields)

    def test_snapshot_hides_a_concurrent_writers_row_until_it_closes(self, tmp_path):
        """A separate connection (a real second process' shape) commits mid-read.

        Inside the block both reads must agree; after it the new row appears —
        proving the snapshot is held, not merely "reads happen to be fast".
        """
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as reader, SqliteRunStore(db_path) as writer:
            reader.save(self._record("r-before"))

            with reader.read_snapshot():
                first = [r.run_id for r in reader.list_all()]
                writer.save(self._record("r-mid"))
                second = [r.run_id for r in reader.list_all()]

            assert first == second == ["r-before"]
            assert "r-mid" in {r.run_id for r in reader.list_all()}

    def test_snapshot_sees_everything_committed_before_it_opened(self, tmp_path):
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as store:
            store.save(self._record("r-old"))
            with store.read_snapshot():
                assert {r.run_id for r in store.list_all()} == {"r-old"}

    def test_nested_snapshot_reuses_the_outer_one_without_deadlocking(self, tmp_path):
        """Composers nest freely: an inner block joins the outer snapshot."""
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as reader, SqliteRunStore(db_path) as writer:
            reader.save(self._record("r-before"))
            with reader.read_snapshot():
                with reader.read_snapshot():
                    writer.save(self._record("r-mid"))
                    inner = [r.run_id for r in reader.list_all()]
                outer = [r.run_id for r in reader.list_all()]
            assert inner == outer == ["r-before"]

    def test_overlapping_reader_threads_each_hold_an_untorn_snapshot(self):
        """A second thread cannot join the first thread's connection snapshot.

        The writer is ordered between the first reader closing and the second
        reader's second read. Each reader must still observe exactly one world.
        """

        class _WriterFirstLock:
            def __init__(
                self,
                second_reader_ready: threading.Event,
                writer_committed: threading.Event,
            ) -> None:
                self._lock = threading.Lock()
                self._second_reader_ready = second_reader_ready
                self._writer_committed = writer_committed

            def __enter__(self):
                if threading.current_thread().name == "snapshot-reader-b":
                    self._second_reader_ready.set()
                    assert self._writer_committed.wait(timeout=5)
                assert self._lock.acquire(timeout=5)
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self._lock.release()

        store = SqliteRunStore(":memory:", check_same_thread=False)
        try:
            store.save(self._record("r-before"))
            first_reader_pinned = threading.Event()
            second_reader_ready = threading.Event()
            writer_started = threading.Event()
            writer_committed = threading.Event()
            store._txn_lock = _WriterFirstLock(second_reader_ready, writer_committed)
            views: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
            errors: list[BaseException] = []

            def _state() -> tuple[str, ...]:
                return tuple(sorted(r.run_id for r in store.list_all()))

            def _reader_a() -> None:
                try:
                    with store.read_snapshot():
                        first = _state()
                        first_reader_pinned.set()
                        assert second_reader_ready.wait(timeout=5)
                        views["a"] = (first, _state())
                except BaseException as exc:
                    errors.append(exc)

            def _reader_b() -> None:
                try:
                    with store.read_snapshot():
                        first = _state()
                        second_reader_ready.set()
                        assert writer_committed.wait(timeout=5)
                        views["b"] = (first, _state())
                except BaseException as exc:
                    errors.append(exc)

            def _writer() -> None:
                try:
                    writer_started.set()
                    assert store.try_admit_run(self._record("r-after"), cap=0)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_committed.set()

            reader_a = threading.Thread(target=_reader_a, name="snapshot-reader-a")
            writer = threading.Thread(target=_writer, name="snapshot-writer")
            reader_b = threading.Thread(target=_reader_b, name="snapshot-reader-b")
            reader_a.start()
            assert first_reader_pinned.wait(timeout=5)
            writer.start()
            assert writer_started.wait(timeout=5)
            reader_b.start()
            for thread in (reader_a, writer, reader_b):
                thread.join(timeout=5)
                assert not thread.is_alive()

            assert errors == []
            worlds = {("r-before",), ("r-after", "r-before")}
            assert set(views) == {"a", "b"}
            assert all(
                first == second and first in worlds for first, second in views.values()
            ), views
        finally:
            store.close()

    def test_snapshot_releases_the_read_transaction_when_the_body_raises(
        self, tmp_path
    ):
        """A raise must not leave the read txn (and the txn lock) held —
        that would wedge every later writer on this connection."""
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as store:
            with pytest.raises(ZeroDivisionError), store.read_snapshot():
                store.list_all()
                raise ZeroDivisionError("boom")

            assert not store._conn.in_transaction
            store.save(self._record("r-after"))
            assert store.get("r-after") is not None

    def test_snapshot_never_commits_a_write_it_did_not_make(self, tmp_path):
        """Read-only by construction: the block ends in ROLLBACK, so the
        snapshot can never become an accidental writer."""
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as store:
            store.save(self._record("r1"))
            with store.read_snapshot():
                assert store._conn.in_transaction
            assert not store._conn.in_transaction

    def _writer_thread_store(self, tmp_path, before_commit):
        """A file-backed store shared across threads, as the TUI and serve open it.

        The writer connection is wrapped at the connection boundary: on the
        thread named ``snapshot-writer`` its ``commit`` first runs
        *before_commit*, so the test can order another thread between the
        writer's INSERT and its COMMIT.
        """

        class _CommitGate:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self.conn = conn

            def commit(self) -> None:
                if threading.current_thread().name == "snapshot-writer":
                    before_commit()
                self.conn.commit()

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def __enter__(self):
                return self.conn.__enter__()

            def __exit__(self, *exc):
                return self.conn.__exit__(*exc)

        store = SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False)
        store._conn = _CommitGate(store._conn)  # type: ignore[assignment]
        return store

    def test_save_on_another_thread_survives_a_snapshot_closing(self, tmp_path):
        """A ``save()`` that returned is never undone by another thread's
        snapshot ending: its INSERT lands while the snapshot is open, and the
        snapshot closes before the save commits (#1958)."""
        snapshot_open = threading.Event()
        writer_paused = threading.Event()
        snapshot_closed = threading.Event()
        errors: list[BaseException] = []

        def _pause_before_commit() -> None:
            writer_paused.set()
            if not snapshot_closed.wait(timeout=5):
                raise AssertionError("snapshot never closed")

        store = self._writer_thread_store(tmp_path, _pause_before_commit)
        try:
            store.save(self._record("r-before"))

            def _reader() -> None:
                try:
                    with store.read_snapshot():
                        store.list_all()
                        snapshot_open.set()
                        if not writer_paused.wait(timeout=5):
                            raise AssertionError("writer never reached commit")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    snapshot_open.set()
                    snapshot_closed.set()

            def _writer() -> None:
                try:
                    assert snapshot_open.wait(timeout=5)
                    store.save(self._record("r-mid"))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_paused.set()

            threads = [
                threading.Thread(target=_reader, name="snapshot-reader"),
                threading.Thread(target=_writer, name="snapshot-writer"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                assert not thread.is_alive()

            assert errors == []
            assert store.get("r-mid") is not None
        finally:
            store.close()
        with SqliteRunStore(str(tmp_path / "d.db")) as fresh:
            assert fresh.get("r-mid") is not None

    def test_commit_on_another_thread_does_not_end_an_open_snapshot(self, tmp_path):
        """Two reads inside one block agree while another thread sharing the
        store commits between them (#1958)."""
        snapshot_open = threading.Event()
        writer_done = threading.Event()
        views: list[list[str]] = []
        errors: list[BaseException] = []
        store = self._writer_thread_store(tmp_path, lambda: None)
        try:
            store.save(self._record("r-before"))

            def _reader() -> None:
                try:
                    with store.read_snapshot():
                        views.append([r.run_id for r in store.list_all()])
                        snapshot_open.set()
                        if not writer_done.wait(timeout=5):
                            raise AssertionError("writer never committed")
                        views.append([r.run_id for r in store.list_all()])
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    snapshot_open.set()

            def _writer() -> None:
                try:
                    assert snapshot_open.wait(timeout=5)
                    store.save(self._record("r-mid"))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_done.set()

            threads = [
                threading.Thread(target=_reader, name="snapshot-reader"),
                threading.Thread(target=_writer, name="snapshot-writer"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                assert not thread.is_alive()

            assert errors == []
            assert views == [["r-before"], ["r-before"]]
            assert {r.run_id for r in store.list_all()} == {"r-before", "r-mid"}
        finally:
            store.close()

    def test_save_on_another_thread_survives_a_rollback_journal_snapshot_closing(
        self, tmp_path
    ):
        """Without WAL the snapshot still reads through its own connection: a
        save on another thread whose INSERT lands while the block is open, and
        whose COMMIT runs after it closes, is kept, not rolled back with the
        snapshot (#1958). Its commit may wait for the block; it is never lost."""
        snapshot_open = threading.Event()
        writer_paused = threading.Event()
        snapshot_closed = threading.Event()
        errors: list[BaseException] = []

        def _pause_before_commit() -> None:
            writer_paused.set()
            if not snapshot_closed.wait(timeout=5):
                raise AssertionError("snapshot never closed")

        store = self._writer_thread_store(tmp_path, _pause_before_commit)
        try:
            mode = store._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            assert mode == "delete"
            store.save(self._record("r-before"))

            def _reader() -> None:
                try:
                    with store.read_snapshot():
                        store.list_all()
                        snapshot_open.set()
                        if not writer_paused.wait(timeout=5):
                            raise AssertionError("writer never reached commit")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    snapshot_open.set()
                    snapshot_closed.set()

            def _writer() -> None:
                try:
                    assert snapshot_open.wait(timeout=5)
                    store.save(self._record("r-mid"))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_paused.set()

            threads = [
                threading.Thread(target=_reader, name="snapshot-reader"),
                threading.Thread(target=_writer, name="snapshot-writer"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                assert not thread.is_alive()

            assert errors == []
        finally:
            store.close()
        with SqliteRunStore(str(tmp_path / "d.db")) as fresh:
            assert fresh.get("r-mid") is not None

    def test_snapshot_reads_through_a_crashed_writers_hot_journal(self, tmp_path):
        """A writer process that dies mid-commit without WAL leaves a hot
        rollback journal that only a read-write connection can roll back. The
        snapshot must still open, and read the data from before the crash."""
        db_path = tmp_path / "d.db"
        store = SqliteRunStore(str(db_path))
        try:
            mode = store._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            assert mode == "delete"
            for n in range(50):
                store.save(self._record(f"r-{n:02d}", title="before"))
            crash = (
                "import os, sqlite3, sys\n"
                "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
                "c.execute('PRAGMA cache_size=1')\n"
                "c.execute('BEGIN')\n"
                'c.execute("UPDATE runs SET title = hex(randomblob(3000))")\n'
                "os._exit(0)\n"
            )
            subprocess.run(
                [sys.executable, "-I", "-c", crash, str(db_path)],
                check=True,
                timeout=60,
            )
            assert Path(f"{db_path}-journal").exists(), "no hot journal left"

            with store.read_snapshot():
                titles = {r.run_id: r.title for r in store.list_all()}

            assert titles == {f"r-{n:02d}": "before" for n in range(50)}
        finally:
            store.close()

    def test_a_write_inside_a_rollback_journal_snapshot_raises_promptly(self, tmp_path):
        """Without WAL a write in the block fails with the snapshot error, as
        under WAL, instead of committing or waiting on a lock the block holds."""
        store = SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False)
        outcomes: list[BaseException | None] = []
        writes = (
            lambda: store.save(self._record("r-saved")),
            lambda: store.try_admit_run(self._record("r-admitted"), cap=0),
        )

        def _write_inside_snapshots() -> None:
            for write in writes:
                try:
                    with store.read_snapshot():
                        write()
                    outcomes.append(None)
                except BaseException as exc:
                    outcomes.append(exc)

        try:
            mode = store._conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            assert mode == "delete"
            store.save(self._record("r-before"))
            worker = threading.Thread(target=_write_inside_snapshots, daemon=True)
            worker.start()
            worker.join(timeout=10)

            assert not worker.is_alive(), "a write inside the snapshot hung"
            assert len(outcomes) == len(writes)
            assert all(isinstance(o, ReadSnapshotWriteError) for o in outcomes), (
                outcomes
            )
            assert {r.run_id for r in store.list_all()} == {"r-before"}
        finally:
            if not store._txn_lock.locked():
                store.close()

    def test_snapshot_connection_closes_when_its_setup_fails(
        self, tmp_path, monkeypatch
    ):
        store = SqliteRunStore(str(tmp_path / "d.db"))
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []

        class _SetupFails:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, *params):
                if sql.startswith("PRAGMA"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.connection.execute(sql, *params)

        def _connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return _SetupFails(connection)

        try:
            monkeypatch.setattr(sqlite3, "connect", _connect)
            with (
                pytest.raises(sqlite3.OperationalError, match="disk I/O"),
                store.read_snapshot(),
            ):
                pass
            monkeypatch.undo()

            assert len(opened) == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                opened[0].execute("SELECT 1")
        finally:
            store.close()

    def test_close_inside_a_snapshot_closes_the_writer(self, tmp_path):
        db_path = str(tmp_path / "d.db")
        store = SqliteRunStore(db_path)
        store.save(self._record("r-before"))

        with store.read_snapshot():
            store.close()

        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            store.save(self._record("r-after"))
        with SqliteRunStore(db_path) as fresh:
            assert [r.run_id for r in fresh.list_all()] == ["r-before"]

    def test_snapshot_closed_on_another_thread_releases_its_connection(self, tmp_path):
        """A generator holding a snapshot can be finalized on another thread;
        its read-only connection must still close rather than leak."""
        store = SqliteRunStore(str(tmp_path / "d.db"), check_same_thread=False)
        errors: list[BaseException] = []

        def _held():
            with store.read_snapshot():
                yield store._conn

        def _finalize(held) -> None:
            try:
                held.close()
            except BaseException as exc:
                errors.append(exc)

        try:
            held = _held()
            snapshot = next(held)
            closer = threading.Thread(target=_finalize, args=(held,))
            closer.start()
            closer.join(timeout=10)

            assert errors == []
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                snapshot.execute("SELECT 1")
        finally:
            store.close()

    def test_a_write_inside_a_snapshot_names_read_snapshot(self, tmp_path):
        """The block reads through a read-only connection, so a write in it
        fails with an error that names the cause, and writes nothing."""
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.save(self._record("r-before"))

            with (
                pytest.raises(ReadSnapshotWriteError, match="read_snapshot"),
                store.read_snapshot(),
            ):
                store.save(self._record("r-saved"))
            with (
                pytest.raises(ReadSnapshotWriteError, match="read_snapshot"),
                store.read_snapshot(),
            ):
                store.try_admit_run(self._record("r-admitted"), cap=0)

            assert {r.run_id for r in store.list_all()} == {"r-before"}
            store.save(self._record("r-after"))
            assert store.get("r-after") is not None

    def test_read_committed_inside_a_snapshot_reads_committed_rows(self, tmp_path):
        """A committed read nested in a snapshot sees what is committed now,
        work-store reads included, and leaves the outer snapshot pinned."""
        db_path = str(tmp_path / "d.db")
        with SqliteRunStore(db_path) as store, SqliteRunStore(db_path) as writer:
            store.save(self._record("r-before", item_id="item-1"))

            with store.read_snapshot():
                pinned = {r.run_id for r in store.list_by_item("item-1")}
                writer.save(self._record("r-mid", item_id="item-1"))
                with store.read_committed() as committed:
                    seen = committed.get("r-mid")
                    work = {
                        r.run_id for r in committed.work_store.list_by_item("item-1")
                    }
                after = {r.run_id for r in store.list_by_item("item-1")}

            assert pinned == after == {"r-before"}
            assert seen is not None
            assert work == {"r-before", "r-mid"}


class TestKnownRunKinds:
    """``KNOWN_RUN_KINDS`` answers "is this a run kind at all".

    Config validates a key that *names* a run kind against it (``[sandbox]
    hosts``), so a kind this build launches and the set does not list would be
    refused at load — a working override rejected rather than a typo caught.
    """

    def test_it_covers_every_role_set_this_module_defines(self):
        from kew.run_store import (
            KNOWN_RUN_KINDS,
            PR_SUBJECT_RUN_KINDS,
            REVIEW_RUN_KINDS,
        )
        from kew.run_store_work import TRACKER_WORK_RUN_KINDS

        covered = TRACKER_WORK_RUN_KINDS | PR_SUBJECT_RUN_KINDS | REVIEW_RUN_KINDS

        assert covered <= KNOWN_RUN_KINDS

    def test_it_covers_the_single_kind_constants_too(self):
        from kew import run_store

        singles = {
            run_store.ADVERSARIAL_REVIEW_RUN_KIND,
            run_store.REVIEW_REMEDIATION_RUN_KIND,
            run_store.CI_FIX_RUN_KIND,
            run_store.RESOLVE_RUN_KIND,
            run_store.READINESS_PROBE_RUN_KIND,
            run_store.TRIAGE_RUN_KIND,
        }

        assert singles <= run_store.KNOWN_RUN_KINDS

    def test_the_default_run_kind_a_record_carries_is_one_of_them(self):
        import dataclasses

        from kew.run_store import KNOWN_RUN_KINDS

        fields = {f.name: f for f in dataclasses.fields(RunRecord)}

        assert fields["run_kind"].default in KNOWN_RUN_KINDS


class TestReadinessProbeAccounting:
    """I05A: probe spend is real money that never becomes epic economics."""

    def _probe(self, store, probe_id="probe-1", fingerprint="f" * 64):
        from kew.readiness import ProbeCost

        return store.lease_readiness_probe(
            probe_id=probe_id,
            fingerprint=fingerprint,
            planned_cost=ProbeCost.planned(0.02, "readiness.probe_ceiling"),
            model="claude-opus-5",
            runner="claude-code",
        )

    def test_probe_run_kind_is_a_single_canonical_string(self):
        from kew.readiness import READINESS_PROBE_RUN_KIND as domain_kind
        from kew.run_store import READINESS_PROBE_RUN_KIND as store_kind

        assert store_kind == domain_kind == "readiness_probe"

    def test_probe_spend_is_excluded_from_epic_item_economics(self, tmp_path):
        from kew.readiness import ProbeCost

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            probe = self._probe(store)
            store.settle_readiness_probe(
                probe.probe_id,
                succeeded=True,
                recorded_cost=ProbeCost.observed(0.02, "runner.usage"),
            )
            store.save(
                RunRecord(
                    run_id="work-1",
                    issue_num=7,
                    item_id="7",
                    branch="agent/issue-7",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    estimated_cost_usd=5.0,
                )
            )
            since = "2000-01-01T00:00:00+00:00"
            by_item = store.sum_costs_by_item(["7", probe.run_id], since=since)
            by_issue = store.sum_costs_by_issue([7, 0], since=since)

        assert by_item == {"7": 5.0}
        assert by_issue == {7: 5.0}

    def test_probe_spend_is_not_a_cost_anomaly_baseline_sample(self, tmp_path):
        from kew.readiness import ProbeCost

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            probe = self._probe(store)
            store.settle_readiness_probe(
                probe.probe_id,
                succeeded=True,
                recorded_cost=ProbeCost.observed(0.02, "runner.usage"),
            )
            # The probe row carries no complexity, so it shares no baseline
            # bucket with any dispatched work.
            assert store.median_cost_for_bucket("claude-opus-5", "complex") == (
                None,
                0,
            )
            assert store.median_cost_for_bucket(
                "claude-opus-5", "complex", other_kinds=True
            ) == (None, 0)

    def test_probe_spend_is_visible_to_the_budget_window(self, tmp_path):
        """Task 18: a probe cannot disappear from spend. ``list_since`` is what
        kew.budget folds into the daily/weekly caps, so the probe row must be
        in it — before it settles (as pending) and after."""
        from kew.readiness import ProbeCost

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            since = datetime.now(UTC) - timedelta(days=1)
            probe = self._probe(store)
            assert [r.run_id for r in store.list_since(since)] == [probe.run_id]

            store.settle_readiness_probe(
                probe.probe_id,
                succeeded=True,
                recorded_cost=ProbeCost.observed(0.0134, "runner.usage"),
            )
            (row,) = store.list_since(since)
            assert row.estimated_cost_usd == pytest.approx(0.0134)
            assert row.run_kind == "readiness_probe"

    def test_a_probe_cannot_reuse_an_id_and_pay_twice(self, tmp_path):
        """Task 18: the spend row is the probe row's twin, one for one."""
        from kew.run_store_readiness import ProbeLeaseHeldError

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._probe(store, probe_id="probe-1")
            with pytest.raises(ProbeLeaseHeldError):
                self._probe(store, probe_id="probe-1")
            assert len(store.list_all()) == 1
            assert len(store.list_readiness_probes()) == 1


def test_store_unavailable_certainty_literal_matches_the_domain_enum() -> None:
    """run_store_readiness keeps certainty flat (no domain import); guard that
    its _UNAVAILABLE_CERTAINTY literal never drifts from Provenance.UNAVAILABLE."""
    from kew.readiness import Provenance
    from kew.run_store_readiness import _UNAVAILABLE_CERTAINTY

    assert str(Provenance.UNAVAILABLE) == _UNAVAILABLE_CERTAINTY


class TestPromptArtifactRows:
    """I06: normalized artifact rows hold metadata and a digest — never bytes."""

    KIND = "compiled_prompt"
    DIGEST = "sha256:" + "a" * 64

    def _record(self, store, *, digest=None, run_id="run-1"):
        return store.record_artifact(
            run_id=run_id,
            attempt_number=1,
            kind=self.KIND,
            digest=digest or self.DIGEST,
            byte_length=4096,
            locator="0" * 32 + ".prompt",
            created_at=datetime(2026, 8, 18, tzinfo=UTC),
        )

    def test_the_artifact_table_has_no_column_that_could_hold_bytes(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            cols = {
                row["name"]
                for row in store._conn.execute(
                    "PRAGMA table_info(run_artifact)"
                ).fetchall()
            }
        assert cols
        assert not cols & {"content", "text", "body", "prompt", "bytes", "blob"}

    def test_a_recorded_artifact_round_trips_as_metadata(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            stored = store.get_artifact(
                run_id="run-1", attempt_number=1, kind=self.KIND
            )
        assert stored is not None
        assert stored.digest == self.DIGEST
        assert stored.byte_length == 4096
        assert stored.storage == "local_protected"
        assert stored.purged_at is None

    def test_recording_the_same_digest_twice_is_idempotent(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            self._record(store)
            assert len(store.list_artifacts(run_id="run-1")) == 1

    def test_a_conflicting_digest_for_the_same_artifact_is_refused(self, tmp_path):
        from kew.run_store_artifacts import ArtifactConflictError

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            with pytest.raises(ArtifactConflictError):
                self._record(store, digest="sha256:" + "b" * 64)

    def test_a_non_digest_reference_is_refused(self, tmp_path):
        from kew.run_store_artifacts import ArtifactValueError

        with (
            SqliteRunStore(str(tmp_path / "d.db")) as store,
            pytest.raises(ArtifactValueError),
        ):
            self._record(store, digest="You are dispatched on issue #7")

    def test_retention_is_terminal_plus_thirty_days(self, tmp_path):
        terminal = datetime(2026, 8, 18, tzinfo=UTC)
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            store.set_artifact_retention(
                run_id="run-1",
                attempt_number=1,
                kind=self.KIND,
                terminal_at=terminal,
            )
            before = store.list_expired_artifacts(
                now=terminal + timedelta(days=29), kind=self.KIND
            )
            after = store.list_expired_artifacts(
                now=terminal + timedelta(days=31), kind=self.KIND
            )
        assert before == []
        assert [row.run_id for row in after] == ["run-1"]

    def test_an_artifact_with_no_terminal_never_expires(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            expired = store.list_expired_artifacts(
                now=datetime(2099, 1, 1, tzinfo=UTC), kind=self.KIND
            )
        assert expired == []

    def test_purging_keeps_the_row_and_marks_it_unavailable(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            assert (
                store.mark_artifact_purged(
                    run_id="run-1",
                    attempt_number=1,
                    kind=self.KIND,
                    purged_at=datetime(2026, 9, 30, tzinfo=UTC),
                    reason="retention",
                )
                is True
            )
            stored = store.get_artifact(
                run_id="run-1", attempt_number=1, kind=self.KIND
            )
        assert stored is not None
        assert stored.storage == "unavailable"
        assert stored.purge_reason == "retention"
        assert stored.digest == self.DIGEST

    def test_a_purged_artifact_is_no_longer_listed_as_expired(self, tmp_path):
        terminal = datetime(2026, 8, 18, tzinfo=UTC)
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            self._record(store)
            store.set_artifact_retention(
                run_id="run-1",
                attempt_number=1,
                kind=self.KIND,
                terminal_at=terminal,
            )
            store.mark_artifact_purged(
                run_id="run-1",
                attempt_number=1,
                kind=self.KIND,
                purged_at=terminal + timedelta(days=31),
                reason="retention",
            )
            expired = store.list_expired_artifacts(
                now=terminal + timedelta(days=40), kind=self.KIND
            )
        assert expired == []

    def test_migrating_an_older_database_preserves_rows_and_adds_the_table(
        self, tmp_path
    ):
        db = str(tmp_path / "old.db")
        with SqliteRunStore(db) as store:
            store.save(
                RunRecord(
                    run_id="legacy-1",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE run_artifact")
        conn.execute("UPDATE schema_version SET version = 44 WHERE id = 1")
        conn.commit()
        conn.close()
        with SqliteRunStore(db) as reopened:
            assert reopened.get("legacy-1") is not None
            assert (
                reopened.get_artifact(
                    run_id="legacy-1", attempt_number=1, kind=self.KIND
                )
                is None
            )


class TestThePublicationJobTableArrivesWithoutDisturbingAnything:
    """I08 v47: a new table, no backfill, and legacy rows read as they are."""

    def test_migrating_to_v47_preserves_rows_and_adds_publication_job(self, tmp_path):
        db = str(tmp_path / "v46.db")
        with SqliteRunStore(db) as store:
            store.save(
                RunRecord(
                    run_id="legacy-1",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="awaiting_publication",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    error_category="github_api",
                )
            )
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE publication_job")
        conn.execute("UPDATE schema_version SET version = 46 WHERE id = 1")
        conn.commit()
        conn.close()

        with SqliteRunStore(db) as reopened:
            assert reopened.get("legacy-1") is not None
            assert reopened.list_publication_jobs() == []
            version = reopened._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]

        assert version == CURRENT_SCHEMA_VERSION

    def test_the_v47_migration_backfills_no_publication_job_row(self, tmp_path):
        """A legacy awaiting_publication run is decoded, never given a row."""
        db = str(tmp_path / "legacy.db")
        with SqliteRunStore(db) as store:
            store.save(
                RunRecord(
                    run_id="legacy-2",
                    issue_num=2,
                    branch="agent/issue-2",
                    status="awaiting_publication",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
            backlog = store.legacy_publication_backlog()

            assert [view.run_id for view in backlog] == ["legacy-2"]
            assert store.list_publication_jobs() == []

    def test_one_lineage_admits_one_live_publication_job_row(self, tmp_path):
        """The partial unique index, asserted against raw SQL rather than the API."""
        db = str(tmp_path / "index.db")
        with SqliteRunStore(db) as store:
            columns = (
                "job_id, lineage_key, source_kind, source_id, run_id,"
                " attempt_number, branch, base_sha, tree_sha, receipt_digest,"
                " state, created_at, updated_at"
            )
            insert = (
                f"INSERT INTO publication_job ({columns})"
                " VALUES (?, 'github:acme/widgets:agent/issue-1', 'github',"
                " 'acme/widgets', ?, 1, 'agent/issue-1', 'b', 'c', 'd', ?,"
                " '2026-08-18T12:00:00+00:00', '2026-08-18T12:00:00+00:00')"
            )
            store._conn.execute(insert, ("pj-1", "run-1", "pending"))

            with pytest.raises(sqlite3.IntegrityError):
                store._conn.execute(insert, ("pj-2", "run-2", "pending"))

            store._conn.execute(
                "UPDATE publication_job SET state = 'superseded' WHERE job_id = 'pj-1'"
            )
            store._conn.execute(insert, ("pj-3", "run-3", "pending"))
            store._conn.commit()

            assert len(store.list_publication_jobs()) == 2


class TestTheHumanReviewPolicyAndRiskRideTheExistingJob:
    """I13 v48: risk, effective policy and the review slot live on the job row.

    Three things are proved here that the pure policy module cannot prove on
    its own, because they are properties of the *store*:

    * **Occupancy belongs to one delivery repository.** Repository A at its
      limit leaves repository B free, and there is no call that counts holds
      without naming whose they are.
    * **Lowering evicts nothing.** Dropping the limit under existing holds
      leaves every hold exactly where it was; what it changes is that no new
      one may be taken.
    * **A confirmation cannot follow a successor SHA.** Remediation opens a new
      job with a new tree, and yesterday's approval does not match it.
    """

    _BASE = "b" * 40
    _TREE = "c" * 40
    _SUCCESSOR = "e" * 40
    _PACKAGE = "sha256:" + "a" * 64
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    _LATER = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)

    def _open_job(self, store, *, repo="acme/widgets", run_id="run-1", tree=None):
        """Seal a receipt and open the publication job it authorizes."""
        from kew.publication_job import PublicationIntent
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        tree_sha = tree or self._TREE
        receipt = store.seal_artifact_receipt(
            lineage=ArtifactLineage(
                source_kind="github",
                source_id=repo,
                run_id=run_id,
                attempt_number=1,
                base_sha=self._BASE,
                package_digest=self._PACKAGE,
                tree_sha=tree_sha,
                verification=VERIFICATION_EXACT_TREE,
                locator="packages/" + "a" * 64,
            ),
            sealed_at=self._NOW,
        )
        intent = PublicationIntent(
            source_kind="github",
            source_id=repo,
            run_id=run_id,
            attempt_number=1,
            branch=f"agent/{run_id}",
            base_sha=self._BASE,
            tree_sha=tree_sha,
            receipt_digest=receipt.receipt_digest,
            message="feat: the change",
        )
        return store.open_publication_job(intent=intent, now=self._NOW), intent

    def _risky(self, tree=None):
        from kew.human_review import PathChange, measure_risk

        return measure_risk(
            base_sha=self._BASE,
            tree_sha=tree or self._TREE,
            changes=[PathChange("src/big.py", added=900)],
        )

    def _hold(self, store, *, repo="acme/widgets", run_id="run-1", policy=None):
        """Measure a risky job under *policy* and take its review slot."""
        from kew.human_review import ReviewPolicy, decide_publication

        policy = policy or ReviewPolicy(delivery_repo=repo)
        job, _ = self._open_job(store, repo=repo, run_id=run_id)
        risk = self._risky()
        decision = decide_publication(
            policy=policy,
            risk=risk,
            pending_reviews=store.pending_review_count(repo),
        )
        store.record_publication_review(
            job_id=job.job_id,
            policy=policy,
            risk=risk,
            decision=decision,
            now=self._NOW,
        )
        return job, policy, decision

    def test_migrating_to_v48_preserves_rows_and_adds_the_review_policy_table(
        self, tmp_path
    ):
        db = str(tmp_path / "v47.db")
        with SqliteRunStore(db) as store:
            store.save(
                RunRecord(
                    run_id="legacy-1",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE human_review_policy")
        conn.execute("DROP INDEX idx_publication_job_review")
        for column in (
            "delivery_repo",
            "review_state",
            "risk_digest",
            "policy_hash",
            "policy_generation",
            "reserved_at",
        ):
            conn.execute(f"ALTER TABLE publication_job DROP COLUMN {column}")
        conn.execute("UPDATE schema_version SET version = 47 WHERE id = 1")
        conn.commit()
        conn.close()

        with SqliteRunStore(db) as reopened:
            assert reopened.get("legacy-1") is not None
            assert reopened.get_human_review_policy("acme/widgets") is None
            version = reopened._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]

        assert version == CURRENT_SCHEMA_VERSION

    def test_the_v48_migration_backfills_no_policy_onto_an_existing_job(self, tmp_path):
        """A job opened before any policy existed claims none afterwards."""
        with SqliteRunStore(str(tmp_path / "backfill.db")) as store:
            job, _ = self._open_job(store)

            review = store.publication_review(job.job_id)

            assert review.delivery_repo == ""
            assert review.review_state == "not_required"
            assert review.policy_hash is None
            assert review.risk_digest is None

    def test_a_measured_job_carries_its_risk_policy_and_delivery_repository(
        self, tmp_path
    ):
        with SqliteRunStore(str(tmp_path / "measure.db")) as store:
            job, policy, decision = self._hold(store)

            review = store.publication_review(job.job_id)

            assert review.delivery_repo == "acme/widgets"
            assert review.review_state == "required"
            assert review.risk_added_lines == 900
            assert review.risk_paths == 1
            assert review.risk_digest == decision.risk_digest
            assert review.policy_hash == policy.policy_hash
            assert review.policy_generation == 0
            assert review.risk_reasons == ("diff_lines",)

    def test_measuring_alone_takes_no_slot(self, tmp_path):
        """Recording what the artifact is, and taking capacity, are two acts."""
        with SqliteRunStore(str(tmp_path / "noslot.db")) as store:
            self._hold(store)

            assert store.pending_review_count("acme/widgets") == 0

    def test_a_risk_measured_on_another_tree_is_refused(self, tmp_path):
        from kew.human_review import ReviewPolicy, decide_publication
        from kew.publication_job import PublicationJobValueError

        with SqliteRunStore(str(tmp_path / "wrongtree.db")) as store:
            job, _ = self._open_job(store)
            policy = ReviewPolicy(delivery_repo="acme/widgets")
            risk = self._risky(tree=self._SUCCESSOR)
            decision = decide_publication(policy=policy, risk=risk, pending_reviews=0)

            with pytest.raises(PublicationJobValueError, match="different base/tree"):
                store.record_publication_review(
                    job_id=job.job_id,
                    policy=policy,
                    risk=risk,
                    decision=decision,
                    now=self._NOW,
                )

    def test_capacity_is_counted_per_delivery_repository_never_globally(self, tmp_path):
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        with SqliteRunStore(str(tmp_path / "scoped.db")) as store:
            one = ReviewPolicy(delivery_repo="acme/widgets", max_pending_reviews=1)
            other = ReviewPolicy(delivery_repo="acme/gadgets", max_pending_reviews=1)
            held, _, _ = self._hold(
                store, repo="acme/widgets", run_id="run-a", policy=one
            )
            store.reserve_publication_review(
                job_id=held.job_id, policy=one, actor="ada", now=self._NOW
            )
            crowded, _, _ = self._hold(
                store, repo="acme/widgets", run_id="run-b", policy=one
            )
            quiet, _, _ = self._hold(
                store, repo="acme/gadgets", run_id="run-c", policy=other
            )

            with pytest.raises(ReviewCapacityError, match="acme/widgets"):
                store.reserve_publication_review(
                    job_id=crowded.job_id, policy=one, actor="ada", now=self._LATER
                )
            store.reserve_publication_review(
                job_id=quiet.job_id, policy=other, actor="ada", now=self._LATER
            )

            assert store.pending_review_count("acme/widgets") == 1
            assert store.pending_review_count("acme/gadgets") == 1

    def test_there_is_no_way_to_ask_for_a_global_count(self, tmp_path):
        from kew.human_review import ReviewPolicyError

        with (
            SqliteRunStore(str(tmp_path / "global.db")) as store,
            pytest.raises(ReviewPolicyError, match="no global count"),
        ):
            store.pending_review_count("")

    def test_lowering_the_limit_below_occupancy_evicts_nothing(self, tmp_path):
        from kew.human_review import ReviewCapacityError, ReviewPolicy

        with SqliteRunStore(str(tmp_path / "lower.db")) as store:
            roomy = ReviewPolicy(delivery_repo="acme/widgets", max_pending_reviews=3)
            for index in range(3):
                job, _, _ = self._hold(store, run_id=f"run-{index}", policy=roomy)
                store.reserve_publication_review(
                    job_id=job.job_id, policy=roomy, actor="ada", now=self._NOW
                )
            tight = ReviewPolicy(
                delivery_repo="acme/widgets", max_pending_reviews=1, generation=1
            )
            fresh, _, _ = self._hold(store, run_id="run-9", policy=tight)

            with pytest.raises(ReviewCapacityError, match="holding 3 of 1"):
                store.reserve_publication_review(
                    job_id=fresh.job_id, policy=tight, actor="ada", now=self._LATER
                )

            assert store.pending_review_count("acme/widgets") == 3

    def test_a_job_measured_under_a_stale_policy_may_not_reserve(self, tmp_path):
        from kew.human_review import ReviewPolicy
        from kew.publication_job import PublicationJobValueError

        with SqliteRunStore(str(tmp_path / "stale.db")) as store:
            job, _, _ = self._hold(store)
            edited = ReviewPolicy(
                delivery_repo="acme/widgets", max_diff_lines=10, generation=1
            )

            with pytest.raises(PublicationJobValueError, match="different policy"):
                store.reserve_publication_review(
                    job_id=job.job_id, policy=edited, actor="ada", now=self._LATER
                )

    def test_a_confirmation_releases_the_slot_it_was_holding(self, tmp_path):
        from kew.human_review import confirm_risk

        with SqliteRunStore(str(tmp_path / "confirm.db")) as store:
            job, policy, decision = self._hold(store)
            store.reserve_publication_review(
                job_id=job.job_id, policy=policy, actor="ada", now=self._NOW
            )
            confirmation = confirm_risk(
                job_id=job.job_id,
                decision=decision,
                policy=policy,
                actor="ada",
                now=self._LATER,
            )

            review = store.confirm_publication_review(
                job_id=job.job_id, confirmation=confirmation, now=self._LATER
            )

            assert review.review_state == "confirmed"
            assert review.confirmed_by == "ada"
            assert store.pending_review_count("acme/widgets") == 0

    def test_a_successor_sha_hands_back_the_slot_and_starts_over(self, tmp_path):
        """Remediation invalidates the prior confirmation by construction."""
        from dataclasses import replace

        from kew.human_review import confirm_risk, decide_publication
        from kew.publication_job import PublicationIntent, PublicationJobValueError
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        with SqliteRunStore(str(tmp_path / "successor.db")) as store:
            job, policy, decision = self._hold(store)
            store.reserve_publication_review(
                job_id=job.job_id, policy=policy, actor="ada", now=self._NOW
            )
            confirmation = confirm_risk(
                job_id=job.job_id,
                decision=decision,
                policy=policy,
                actor="ada",
                now=self._NOW,
            )
            receipt = store.seal_artifact_receipt(
                lineage=ArtifactLineage(
                    source_kind="github",
                    source_id="acme/widgets",
                    run_id="run-1",
                    attempt_number=2,
                    base_sha=self._BASE,
                    package_digest=self._PACKAGE,
                    tree_sha=self._SUCCESSOR,
                    verification=VERIFICATION_EXACT_TREE,
                    locator="packages/" + "b" * 64,
                ),
                sealed_at=self._LATER,
            )
            successor = store.open_successor_publication_job(
                intent=PublicationIntent(
                    source_kind="github",
                    source_id="acme/widgets",
                    run_id="run-1",
                    attempt_number=2,
                    branch="agent/run-1",
                    base_sha=self._BASE,
                    tree_sha=self._SUCCESSOR,
                    receipt_digest=receipt.receipt_digest,
                    message="fix: remediated",
                ),
                now=self._LATER,
            )

            assert store.publication_review(job.job_id).review_state == "released"
            assert store.pending_review_count("acme/widgets") == 0
            successor_risk = self._risky(tree=self._SUCCESSOR)
            store.record_publication_review(
                job_id=successor.job_id,
                policy=policy,
                risk=successor_risk,
                decision=decide_publication(
                    policy=policy, risk=successor_risk, pending_reviews=0
                ),
                now=self._LATER,
            )
            store.reserve_publication_review(
                job_id=successor.job_id, policy=policy, actor="ada", now=self._LATER
            )

            # Replaying the predecessor's approval onto the successor — the one
            # shape that would slip a never-reviewed tree past a real person.
            replayed = replace(confirmation, job_id=successor.job_id)
            with pytest.raises(PublicationJobValueError, match="different tree"):
                store.confirm_publication_review(
                    job_id=successor.job_id, confirmation=replayed, now=self._LATER
                )

    def test_a_confirmation_for_another_job_is_refused(self, tmp_path):
        from kew.human_review import confirm_risk
        from kew.publication_job import PublicationJobValueError

        with SqliteRunStore(str(tmp_path / "otherjob.db")) as store:
            job, policy, decision = self._hold(store)
            store.reserve_publication_review(
                job_id=job.job_id, policy=policy, actor="ada", now=self._NOW
            )
            foreign = confirm_risk(
                job_id="pj-somebody-else",
                decision=decision,
                policy=policy,
                actor="ada",
                now=self._NOW,
            )

            with pytest.raises(PublicationJobValueError, match="different publication"):
                store.confirm_publication_review(
                    job_id=job.job_id, confirmation=foreign, now=self._LATER
                )

    def test_an_unreserved_job_has_no_hold_to_confirm(self, tmp_path):
        from kew.human_review import confirm_risk
        from kew.publication_job import PublicationJobStateError

        with SqliteRunStore(str(tmp_path / "unheld.db")) as store:
            job, policy, decision = self._hold(store)
            confirmation = confirm_risk(
                job_id=job.job_id,
                decision=decision,
                policy=policy,
                actor="ada",
                now=self._NOW,
            )

            with pytest.raises(PublicationJobStateError, match="not being held"):
                store.confirm_publication_review(
                    job_id=job.job_id, confirmation=confirmation, now=self._LATER
                )


class TestTheDurableHumanReviewPolicyOverride:
    """I13: an override is a durable row with a generation and an expiry."""

    _REPO = "acme/widgets"
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _override(self, **overrides):
        from kew.human_review import ReviewPolicy

        fields = {"delivery_repo": self._REPO, "generation": 1}
        fields.update(overrides)
        return ReviewPolicy(**fields)

    def test_an_override_round_trips_with_its_generation_and_expiry(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "override.db")) as store:
            store.set_human_review_policy(
                policy=self._override(
                    max_diff_lines=50,
                    approvers=("ada",),
                    expires_at="2026-09-01T00:00:00+00:00",
                ),
                actor="ada",
                reason="big refactor week",
                now=self._NOW,
            )

            stored = store.get_human_review_policy(self._REPO)

            assert stored.max_diff_lines == 50
            assert stored.approvers == ("ada",)
            assert stored.generation == 1
            assert stored.expires_at == "2026-09-01T00:00:00+00:00"

    def test_a_non_advancing_generation_is_refused(self, tmp_path):
        from kew.human_review import ReviewPolicyError

        with SqliteRunStore(str(tmp_path / "gen.db")) as store:
            store.set_human_review_policy(
                policy=self._override(generation=2), actor="ada", now=self._NOW
            )

            with pytest.raises(ReviewPolicyError, match="does not supersede"):
                store.set_human_review_policy(
                    policy=self._override(generation=2), actor="ada", now=self._NOW
                )

    def test_an_override_is_scoped_to_its_delivery_repository(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "scope.db")) as store:
            store.set_human_review_policy(
                policy=self._override(max_paths=2), actor="ada", now=self._NOW
            )

            assert store.get_human_review_policy("acme/gadgets") is None

    def test_a_live_override_is_the_effective_policy(self, tmp_path):
        from kew.human_review import ReviewPolicy

        with SqliteRunStore(str(tmp_path / "live.db")) as store:
            store.set_human_review_policy(
                policy=self._override(
                    max_diff_lines=50, expires_at="2026-09-01T00:00:00+00:00"
                ),
                actor="ada",
                now=self._NOW,
            )

            effective = store.effective_human_review_policy(
                system=ReviewPolicy(delivery_repo=self._REPO), now=self._NOW
            )

            assert effective.max_diff_lines == 50

    def test_an_expired_override_reverts_and_is_attested_before_the_effect(
        self, tmp_path
    ):
        """Attest, then act: the chained event precedes clearing the row."""
        from kew.human_review import ReviewPolicy

        with SqliteRunStore(str(tmp_path / "expiry.db")) as store:
            store.set_human_review_policy(
                policy=self._override(
                    max_diff_lines=50, expires_at="2026-08-01T00:00:00+00:00"
                ),
                actor="ada",
                now=self._NOW,
            )

            effective = store.effective_human_review_policy(
                system=ReviewPolicy(delivery_repo=self._REPO), now=self._NOW
            )

            assert effective.max_diff_lines == 500
            assert effective.generation == 2
            assert store.get_human_review_policy(self._REPO) is None
            events = store.list_audit_events(event="human_review_policy_reverted")
            assert len(events) == 1
            assert events[0].payload["reason"] == "expired"
            assert events[0].payload["from"]["max_diff_lines"] == 50
            assert events[0].payload["to"]["max_diff_lines"] == 500
            assert events[0].repo == self._REPO

    def test_the_reversion_event_is_on_the_verifiable_chain(self, tmp_path):
        from kew.audit_chain import verify_chain
        from kew.human_review import ReviewPolicy

        with SqliteRunStore(str(tmp_path / "chain.db")) as store:
            store.set_human_review_policy(
                policy=self._override(expires_at="2026-08-01T00:00:00+00:00"),
                actor="ada",
                now=self._NOW,
            )
            store.effective_human_review_policy(
                system=ReviewPolicy(delivery_repo=self._REPO), now=self._NOW
            )

            assert verify_chain(store.read_chain_rows()).ok

    def test_reverting_twice_appends_one_event(self, tmp_path):
        """The second read finds no override, so it attests nothing."""
        from kew.human_review import ReviewPolicy

        system = ReviewPolicy(delivery_repo=self._REPO)
        with SqliteRunStore(str(tmp_path / "twice.db")) as store:
            store.set_human_review_policy(
                policy=self._override(expires_at="2026-08-01T00:00:00+00:00"),
                actor="ada",
                now=self._NOW,
            )
            store.effective_human_review_policy(system=system, now=self._NOW)
            store.effective_human_review_policy(system=system, now=self._NOW)

            assert (
                len(store.list_audit_events(event="human_review_policy_reverted")) == 1
            )

    def test_an_expired_override_blocks_new_publication_under_the_old_numbers(
        self, tmp_path
    ):
        """Reverting is a policy change, so holds taken before it no longer match."""
        from kew.human_review import (
            PathChange,
            ReviewPolicy,
            decide_publication,
            measure_risk,
        )

        system = ReviewPolicy(delivery_repo=self._REPO)
        with SqliteRunStore(str(tmp_path / "blocked.db")) as store:
            store.set_human_review_policy(
                policy=self._override(
                    max_diff_lines=9_000, expires_at="2026-08-01T00:00:00+00:00"
                ),
                actor="ada",
                now=self._NOW,
            )

            effective = store.effective_human_review_policy(
                system=system, now=self._NOW
            )
            risk = measure_risk(
                base_sha="b" * 40,
                tree_sha="c" * 40,
                changes=[PathChange("src/big.py", added=900)],
            )
            decision = decide_publication(
                policy=effective, risk=risk, pending_reviews=0
            )

            assert decision.outcome == "confirmation_required"
            assert decision.policy_generation == 2


class TestAMeasurementSomeoneActedOnIsNotRewritten:
    """I13: re-measuring after a hold is taken would orphan the approval."""

    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def test_a_held_job_refuses_a_second_review_measurement(self, tmp_path):
        from kew.publication_job import PublicationJobStateError

        helper = TestTheHumanReviewPolicyAndRiskRideTheExistingJob()
        with SqliteRunStore(str(tmp_path / "remeasure.db")) as store:
            job, policy, decision = helper._hold(store)
            store.reserve_publication_review(
                job_id=job.job_id, policy=policy, actor="ada", now=self._NOW
            )

            with pytest.raises(PublicationJobStateError, match="re-measuring"):
                store.record_publication_review(
                    job_id=job.job_id,
                    policy=policy,
                    risk=helper._risky(),
                    decision=decision,
                    now=self._NOW,
                )

    def test_an_unheld_job_may_be_measured_again_under_a_newer_policy(self, tmp_path):
        from kew.human_review import ReviewPolicy, decide_publication

        helper = TestTheHumanReviewPolicyAndRiskRideTheExistingJob()
        with SqliteRunStore(str(tmp_path / "again.db")) as store:
            job, _, _ = helper._hold(store)
            newer = ReviewPolicy(
                delivery_repo="acme/widgets", max_diff_lines=9_000, generation=1
            )
            risk = helper._risky()

            review = store.record_publication_review(
                job_id=job.job_id,
                policy=newer,
                risk=risk,
                decision=decide_publication(policy=newer, risk=risk, pending_reviews=0),
                now=self._NOW,
            )

            assert review.review_state == "not_required"
            assert review.policy_generation == 1


def test_v49_creates_the_no_change_tables_without_losing_existing_state(tmp_path):
    """I10A: the no-change projection, sync jobs and dependency effects (v49).

    Not backfilled. A legacy successful run with no pull request recorded no
    trusted base, sealed work package or named check, so a judgment written on
    its behalf would attest to evidence that never existed — those rows stay
    ``unknown`` and are decoded on read instead.
    """
    import sqlite3 as _sqlite3

    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    db_path = tmp_path / "dispatch.db"
    legacy = _sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER);"
        "INSERT INTO schema_version (id, version) VALUES (1, 48);"
        "CREATE TABLE runs ("
        "run_id TEXT PRIMARY KEY, issue_num INTEGER, branch TEXT, status TEXT,"
        "exit_code INTEGER, started_at TEXT);"
        "INSERT INTO runs VALUES ('old-run', 7, 'agent/issue-7', 'success', 0,"
        " '2026-01-01T00:00:00+00:00');"
    )
    legacy.commit()
    legacy.close()

    with SqliteRunStore(str(db_path)) as store:
        version = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION
        survivor = store._conn.execute(
            "SELECT branch FROM runs WHERE run_id = 'old-run'"
        ).fetchone()
        assert survivor["branch"] == "agent/issue-7"
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "no_change_judgment",
            "outcome_sync_jobs",
            "no_change_dependency_effect",
        } <= tables
        assert store.list_no_change_judgments() == ()
        assert [v.state for v in store.legacy_no_change_views()] == ["unknown"]


def test_v49_one_live_no_change_judgment_per_item_is_a_schema_invariant(tmp_path):
    import sqlite3 as _sqlite3

    from kew.run_store import SqliteRunStore

    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path)) as store:
        cols = (
            "judgment_id, lineage_key, generation, source_kind, source_id, run_id,"
            " attempt_number, base_sha, state, policy, decision_reason, created_at,"
            " updated_at"
        )
        values = (
            "?, ?, ?, 'github', '1', ?, 1, 'a', 'candidate', 'confirm', '', 'x', 'x'"
        )
        store._conn.execute(
            f"INSERT INTO no_change_judgment ({cols}) VALUES ({values})",  # nosec B608
            ("nc-1", "github:1", 1, "run-1"),
        )
        with pytest.raises(_sqlite3.IntegrityError):
            store._conn.execute(
                f"INSERT INTO no_change_judgment ({cols}) VALUES ({values})",  # nosec B608
                ("nc-2", "github:1", 2, "run-2"),
            )


def test_find_run_by_branch_looks_past_pr_subject_runs(tmp_path):
    """The PR->item join returns the run that opened the PR, not the newest
    review or remediation that later worked on its branch (their issue_num is
    the PR number)."""
    started = datetime(2026, 9, 13, tzinfo=UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="opened",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=started,
            )
        )
        for offset, kind in enumerate(sorted(PR_SUBJECT_RUN_KINDS), start=1):
            for branch in ("agent/issue-42", "agent/issue-7"):
                store.save(
                    RunRecord(
                        run_id=f"{kind}-{offset}-{branch[-1]}",
                        issue_num=178,
                        item_id="42",
                        branch=branch,
                        status="success",
                        exit_code=0,
                        started_at=started + timedelta(hours=offset),
                        run_kind=kind,
                    )
                )

        found = store.find_run_by_branch("agent/issue-42")
        assert found is not None and found.run_id == "opened"
        assert store.find_run_by_branch("agent/issue-7") is None


@pytest.mark.parametrize("pr_subject_kind", sorted(PR_SUBJECT_RUN_KINDS))
def test_continuation_origins_ignores_pr_subject_runs_on_a_continued_branch(
    tmp_path, pr_subject_kind
):
    """PR-subject work never becomes a retained continuation family member."""
    started = datetime(2026, 9, 13, tzinfo=UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        for run_id, kind in (
            ("original", "dispatch"),
            ("continued", "dispatch"),
            ("fresh", "dispatch"),
            ("subject", pr_subject_kind),
        ):
            store.save(
                RunRecord(
                    run_id=run_id,
                    issue_num=1,
                    item_id="1",
                    branch="agent/issue-1",
                    status="awaiting_publication",
                    exit_code=0,
                    started_at=started,
                    run_kind=kind,
                )
            )
        preserved = tmp_path / "preserved"
        preserved.mkdir()
        store.record_continuation(
            "epic:1",
            "1",
            "original",
            "agent/issue-1",
            preserved,
            checkout_generation=fixture_checkout_generation(preserved),
        )
        assert (
            store.claim_continuation_under_cap(
                "epic:1",
                "1",
                "original",
                "agent/issue-1",
                preserved,
                "continued",
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:1",
                    "1",
                    "original",
                    "agent/issue-1",
                    preserved,
                    "continued",
                ),
            )
            == "claimed"
        )

        # A fresh dispatch-kind row on the continued branch is a possible
        # requeued claim without evidence and stays refused.
        with pytest.raises(
            RuntimeError, match="continuation claim evidence is missing"
        ):
            store.continuation_origins("fresh")
        assert store.continuation_origins("subject") == []


def test_v66_records_a_runs_placement_and_reads_history_as_local(tmp_path):
    """A pre-v66 row ran on the operator machine: that is a fact, not a default.

    The migration must not lose the row, and it must not invent an unknown host
    for one — there was exactly one host when it was written, so ``local`` is
    what it says, and the per-host capacity check and the idle-stop guard both
    read that answer (#2398).
    """
    path = tmp_path / "placement.db"
    with SqliteRunStore(str(path)) as store:
        store.save(
            RunRecord(
                run_id="legacy",
                issue_num=7,
                branch="agent/issue-7",
                status="pending",
                exit_code=0,
                started_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        store._conn.execute("ALTER TABLE runs DROP COLUMN run_host")
        store._conn.execute("ALTER TABLE runs DROP COLUMN run_host_instance")
        store._conn.execute("UPDATE schema_version SET version = 64 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(str(path)) as store:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(runs)")}
        assert {"run_host", "run_host_instance"} <= columns
        legacy = store.get("legacy")
        assert legacy is not None
        assert legacy.branch == "agent/issue-7"
        assert legacy.run_host == "local"
        assert legacy.run_host_instance is None
        store.set_run_site("legacy", "gcp", "vm-one")
        moved = store.get("legacy")
        assert (moved.run_host, moved.run_host_instance) == ("gcp", "vm-one")


def test_v57_adds_retention_independent_continuation_expiries(tmp_path):
    # Renumbered v55 -> v57 at the merge with main (main owns v55 and v56), so
    # the ledger starts one step below this migration and must land on the
    # current version (60 since the item_plans table, #2290).
    from kew.run_store import CURRENT_SCHEMA_VERSION

    path = tmp_path / "migration.db"
    with SqliteRunStore(str(path)) as store:
        store._conn.execute("DROP TABLE IF EXISTS continuation_expiries")
        store._conn.execute("UPDATE schema_version SET version = 56 WHERE id = 1")
        store._conn.commit()
        record = store.record_continuation(
            "epic:x",
            "42",
            "original",
            "agent/issue-42",
            tmp_path / "checkout",
            checkout_generation=fixture_checkout_generation(tmp_path / "checkout"),
        )
        before = store.read_chain_rows()
    with SqliteRunStore(str(path)) as store:
        # The literal is deliberate (tests/test_schema_version_literal_gate.py):
        # a schema bump has to be an explicit edit here, not a tautology
        # against the constant. 73 = ... v62 the driver's expected cadence,
        # v63 its lock snapshot (#2297), v64 the epic_tick_log (#2304), v65 the
        # prompt's self-check section (#2454), v66 a run's fleet placement
        # (#2398), v67 the acceptance_contracts table (#2726), v68 its plan
        # binding (#2774), v69 the boot a run's owner ran under (#2812), v70 the
        # epic_merge_state_reads table (#2879), and v71 the merge queue (#2814).
        # v72 binds a host-restart retry marker to the superseded run (#2969).
        # v73 adds the queue stop's kind and failure (#2883).
        # v74 adds prompt profiles (#3020).
        # v75 marks the tick attempt that journaled each change (#2949).
        # v76 adds the merge claims an operator verify holds (#3198).
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
            == 77
        )
        assert store.list_continuations("epic:x") == [record]
        assert store.read_chain_rows() == before
        from kew.run_store import ContinuationExpiry

        expiry = ContinuationExpiry(
            "authority",
            0,
            "checkout-token",
            "purgeable-run",
            datetime.now(UTC).isoformat(),
        )
        store.record_continuation_expiry(record, expiry)
        store.record_continuation_expiry(record, expiry)
        assert store.continuation_expiry(record) == expiry
        assert (
            store._conn.execute(
                "PRAGMA foreign_key_list(continuation_expiries)"
            ).fetchall()
            == []
        )
    with SqliteRunStore(str(path)) as store:
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
            == 77
        )
        assert (
            store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'continuation_expiries'"
            ).fetchone()[0]
            == "continuation_expiries"
        )
        assert store.continuation_expiry(record) == expiry
        assert store.read_chain_rows() == before


def _round11_label_task(store, request_id="first"):
    from datetime import UTC, datetime

    record = RunRecord(
        run_id="round11",
        issue_num=42,
        item_id="42",
        repo="owner/repo",
        branch="agent/42",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
    )
    store.save(record)
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="stop_label_reset_requested",
        run_id=record.run_id,
        payload={"request_id": request_id, "tracker": "github"},
    )
    store._conn.execute(
        "UPDATE runs SET status = 'killed' WHERE run_id = ?", (record.run_id,)
    )
    store._conn.commit()
    return record


def test_round11_label_reset_concurrent_claim(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = str(tmp_path / "label.db")
    entered, release = threading.Event(), threading.Event()
    calls = []
    with SqliteRunStore(path) as store:
        _round11_label_task(store)

    def reset(*args):
        calls.append(42)
        entered.set()
        assert release.wait(3)

    def drain():
        with SqliteRunStore(path) as store:
            store.retry_stop_label_resets(reset_labels=reset)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(drain)
        assert entered.wait(3)
        second = pool.submit(drain)
        # Keep the first provider operation open while the second drainer enters.
        threading.Event().wait(0.1)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    with SqliteRunStore(path) as store:
        assert calls == [42]
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and events[0].payload["request_id"] == "first"


def test_round11_label_reset_crash_retries_expired_lease(tmp_path, monkeypatch):
    import time

    path, calls = str(tmp_path / "crash.db"), []
    crashed = False

    def reset(*args):
        nonlocal crashed
        calls.append(42)
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("process died after provider effect")

    with SqliteRunStore(path) as store:
        _round11_label_task(store)
        with pytest.raises(KeyboardInterrupt):
            store.retry_stop_label_resets(reset_labels=reset)
    with SqliteRunStore(path) as store:
        store.retry_stop_label_resets(reset_labels=reset)
        assert calls == [42], "the unexpired lease must prevent repeat delivery"
        expiry = time.time() + 120
        monkeypatch.setattr("kew.run_store_stop.time.time", lambda: expiry)
        store.retry_stop_label_resets(reset_labels=reset)
        store.retry_stop_label_resets(reset_labels=reset)
        assert calls == [42, 42]
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and events[0].payload["request_id"] == "first"


@pytest.mark.parametrize("source", ["dispatch", "admission"])
def test_round11_label_reset_superseded(tmp_path, source):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    calls = []
    with SqliteRunStore(str(tmp_path / "superseded.db")) as store:
        row = _round11_label_task(store)
        # Delivery waits for finalization while the newer run acquires labels.
        store._conn.execute(
            "UPDATE runs SET status = 'pending' WHERE run_id = 'round11'"
        )
        store._conn.commit()
        newer = replace(
            row,
            run_id="newer",
            status="pending",
            started_at=row.started_at - timedelta(seconds=1),
        )
        store.save(newer)
        if source == "dispatch":
            store.append_audit_event(
                timestamp=datetime.now(UTC).isoformat(),
                event="dispatch_started",
                run_id="newer",
                issue_num=42,
            )
        else:
            identity = store.build_admission_identity(
                "newer", tracker="github", item_id="42", repo="owner/repo"
            )
            store.admit_run_with_identity(newer, identity, cap=0)
        store._conn.execute(
            "UPDATE runs SET status = 'killed' WHERE run_id = 'round11'"
        )
        store._conn.commit()
        store.retry_stop_label_resets(reset_labels=lambda *a: calls.append(42))
        assert calls == [], "a delayed reset must not change the newer dispatch labels"
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and events[0].payload["superseded"]


def test_round11_label_reset_superseded_by_a_contained_dispatch(tmp_path):
    """Supersede matches on ``runs.repo``, so both dispatch writers must set it.

    The contained writer stamped the delivery repo on the admission identity
    but left the run row's column NULL, so a newer CONTAINED run of the same
    item was invisible to this predicate and a stopped run's delayed reset
    stripped that live run's labels (#2097).
    """
    from kew.dispatch_sandbox import _record_sandbox_start

    calls = []
    with SqliteRunStore(str(tmp_path / "contained.db")) as store:
        _round11_label_task(store)  # host-dispatched row, repo="owner/repo"
        newer = _record_sandbox_start(
            store,
            issue_num=42,
            item_id="42",
            branch="agent/42",
            title="t",
            model="claude-sonnet-4-6",
            runner_name="claude-code",
            log_file=tmp_path / "sandbox.log",
            tracker="github",
            repo="owner/repo",
        )
        assert store.get(newer).repo == "owner/repo"
        store.retry_stop_label_resets(reset_labels=lambda *a: calls.append(42))
        assert calls == [], "a newer contained dispatch owns the item's labels"
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and events[0].payload["superseded"]


def test_round11_label_reset_supersede_tolerates_a_null_repo(tmp_path):
    """A pre-#2097 row's NULL repo must not hide the run that superseded it.

    Before #2097 the host writer stored ``project.repo`` verbatim, so a run
    dispatched while the slug was "auto-detected from gh" left ``runs.repo``
    NULL. A reset requested on such a row straddles the upgrade: the run
    dispatched after it now carries a resolved slug, and an equality match on
    the column read that as a different item, so the delayed reset stripped the
    live run's labels. An unknown repo on either side matches every repo —
    suppressing a stale reset costs one leftover label, while missing a
    supersede strips a live run's (#2097).
    """
    from dataclasses import replace
    from datetime import timedelta

    calls = []
    with SqliteRunStore(str(tmp_path / "straddle.db")) as store:
        row = _round11_label_task(store)
        store._conn.execute(
            "UPDATE runs SET repo = NULL WHERE run_id = ?", ("round11",)
        )
        store._conn.commit()
        newer = replace(
            row,
            run_id="newer",
            repo="owner/repo",
            started_at=row.started_at + timedelta(seconds=5),
        )
        store.save(newer)
        store.retry_stop_label_resets(reset_labels=lambda *a: calls.append(42))
        assert calls == [], "a newer run with a resolved slug owns the item's labels"
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and events[0].payload["superseded"]


def test_round11_label_reset_supersede_ignores_a_different_repo(tmp_path):
    """Two known, different slugs still mean two different items (#2097)."""
    from dataclasses import replace
    from datetime import timedelta

    calls = []
    with SqliteRunStore(str(tmp_path / "other-repo.db")) as store:
        row = _round11_label_task(store)
        newer = replace(
            row,
            run_id="newer",
            repo="other/repo",
            started_at=row.started_at + timedelta(seconds=5),
        )
        store.save(newer)
        store.retry_stop_label_resets(reset_labels=lambda *a: calls.append(42))
        assert calls == [42], "another repo's run cannot own this item's labels"
        events = store.list_audit_events(event="stop_label_reset_completed")
        assert len(events) == 1 and not events[0].payload["superseded"]


def test_round11_label_reset_duplicate_requests_are_independent(tmp_path):
    from datetime import UTC, datetime

    calls = []
    with SqliteRunStore(str(tmp_path / "duplicates.db")) as store:
        row = _round11_label_task(store)
        store.append_audit_event(
            timestamp=datetime.now(UTC).isoformat(),
            event="stop_label_reset_completed",
            run_id=row.run_id,
            payload={"request_id": "first"},
        )
        store.append_audit_event(
            timestamp=datetime.now(UTC).isoformat(),
            event="stop_label_reset_requested",
            run_id=row.run_id,
            payload={"request_id": "second"},
        )
        store.retry_stop_label_resets(reset_labels=lambda *a: calls.append(42))
        assert calls == [42]
        assert {
            e.payload["request_id"]
            for e in store.list_audit_events(event="stop_label_reset_completed")
        } == {"first", "second"}


def test_round11_label_requests_have_distinct_ids(tmp_path):
    from dataclasses import replace

    with SqliteRunStore(str(tmp_path / "requests.db")) as store:
        row = _round11_label_task(store)
        for run_id in ("second", "third"):
            store.save(replace(row, run_id=run_id))
            store.request_run_stop(run_id, label_reset={"tracker": "github"})
            store.request_run_stop(run_id, label_reset={"tracker": "github"})
        events = store.list_audit_events(event="stop_label_reset_requested")
        identities = [e.payload.get("request_id") for e in events]
        assert len(identities) == 3
        assert all(isinstance(value, str) and value for value in identities)
        assert len(set(identities)) == 3


@pytest.mark.parametrize("check_same_thread", [True, False])
def test_round8_committed_connection_is_distinct_readonly_and_reused(
    tmp_path, check_same_thread
):
    import sqlite3

    # URI punctuation must identify the same file, without changing URI options.
    with SqliteRunStore(
        str(tmp_path / "read?#only.db"), check_same_thread=check_same_thread
    ) as store:
        with store.read_committed() as reader:
            connection = reader._cursor.connection
            assert connection is not store._conn
            assert connection.in_transaction
            assert not store._conn.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("DELETE FROM runs")
        assert not connection.in_transaction
        with store.read_committed() as reader:
            assert reader._cursor.connection is connection
        store.close_committed_reader()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("check_same_thread", [True, False])
def test_round8_committed_reader_thread_ownership(tmp_path, check_same_thread):
    from concurrent.futures import ThreadPoolExecutor

    with SqliteRunStore(
        str(tmp_path / "reader-threads.db"), check_same_thread=check_same_thread
    ) as store:
        with store.read_committed() as reader:
            main_connection = reader._cursor.connection

        def threaded_read():
            with store.read_committed() as reader:
                connection = reader._cursor.connection
                assert reader.get("missing") is None
            if check_same_thread:
                store.close_committed_reader()
            return connection

        with ThreadPoolExecutor(max_workers=1) as executor:
            connection = executor.submit(threaded_read).result(timeout=10)
        assert (connection is main_connection) == (not check_same_thread)
