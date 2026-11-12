"""Round-11 migration replay proof for the branch's unrenumbered sequence."""

from datetime import UTC, datetime

import pytest

from kew.run_store import CURRENT_SCHEMA_VERSION, RunRecord, SqliteRunStore


def test_round11_upgrade_and_reopen_are_idempotent(tmp_path):
    path = str(tmp_path / "migration.db")
    expected = {
        "attempt_pgid",
        "owner_pid",
        "attempt_start_time",
        "owner_start_time",
        "attempt_owner_verified",
        "termination_members",
        "container_id",
        "container_cidfile",
    }
    with SqliteRunStore(path) as store:
        columns = [r[1] for r in store._conn.execute("PRAGMA table_info(runs)")]
        assert expected <= set(columns)
        for column in expected:
            store._conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        store._conn.execute("DROP TABLE stop_label_outbox")
        store._conn.execute("UPDATE schema_version SET version = 49")
        store._conn.commit()
    snapshots = []
    for _ in range(2):
        with SqliteRunStore(path) as store:
            # Replaying these exact branch migrations must also be harmless.
            cursor = store._conn.cursor()
            for version in range(50, CURRENT_SCHEMA_VERSION + 1):
                getattr(store, f"_apply_v{version}")(cursor)
            store._conn.commit()
            snapshots.append(_schema_snapshot(store, expected))
    assert snapshots[0] == snapshots[1]


def _schema_snapshot(store, expected):
    cursor = store._conn.cursor()
    columns = [r[1] for r in cursor.execute("PRAGMA table_info(runs)")]
    assert all(columns.count(column) == 1 for column in expected)
    indexes = list(
        cursor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' ORDER BY name"
        )
    )
    assert len([r for r in indexes if r[0] == "idx_stop_label_pending"]) == 1
    assert (
        cursor.execute("SELECT version FROM schema_version").fetchone()[0]
        == CURRENT_SCHEMA_VERSION
    )
    return [tuple(r) for r in indexes]


@pytest.mark.parametrize("pgid", [None, 99991], ids=["null-pgid", "owner-equal-pgid"])
def test_round12_v50_owner_shapes_preserve_attempt(tmp_path, pgid):
    """V49 PGIDs remain attempt anchors even when they equal an owner's PID."""
    path = str(tmp_path / "v50.db")
    with SqliteRunStore(path) as store:
        store.save(
            RunRecord(
                run_id="v50",
                issue_num=42,
                branch="agent/42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=pgid,
            )
        )
        for column in (
            "attempt_pgid",
            "owner_pid",
            "owner_start_time",
            "attempt_start_time",
            "attempt_owner_verified",
            "termination_members",
            "owner_start_token",
            "attempt_start_token",
            "owner_ppid",
            "owner_command",
            "container_runtime",
            "cleanup_runtime",
            "container_id",
            "container_cidfile",
        ):
            store._conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        store._conn.execute("DROP TABLE stop_label_outbox")
        store._conn.execute("UPDATE schema_version SET version = 49")
        store._conn.commit()
    snapshots = []
    for _ in range(2):
        with SqliteRunStore(path) as store:
            for version in range(50, CURRENT_SCHEMA_VERSION + 1):
                getattr(store, f"_apply_v{version}")(store._conn.cursor())
            store._conn.commit()
            row = store.get("v50")
            assert (row.pgid, row.attempt_pgid) == (None, pgid)
            assert row.owner_pid is None, "migration must never invent owner identity"
            assert row.owner_start_time is None
            assert row.owner_start_token is None
            assert row.attempt_start_time is None
            assert row.attempt_start_token is None
            assert (
                store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
                == CURRENT_SCHEMA_VERSION
            )
            snapshots.append(row)
    assert snapshots[0] == snapshots[1]
