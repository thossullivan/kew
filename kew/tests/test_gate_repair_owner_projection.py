"""Stored owner reads stay exact without per-record audit queries."""

from datetime import UTC, datetime

import pytest

from kew.run_store import RunRecord, SqliteRunStore


def _seed_owners(store):
    for run_id, owner, birth in (
        ("first", 2, "new"),
        ("second", 4, None),
        ("absent", None, None),
        ("outside", 5, "outside"),
    ):
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=1,
                branch="outside" if run_id == "outside" else "page",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                pgid=owner,
                owner_pid=owner,
                owner_start_time=birth,
            )
        )
    for run_id, payload in (
        ("first", {"owner_pid": 1, "owner_start_time": "old"}),
        ("first", {"owner_pid": 2, "owner_start_time": "new"}),
        ("second", {"owner_pid": 3, "owner_start_time": "old"}),
        ("second", {"owner_pid": 4}),
        ("outside", {"owner_pid": 5, "owner_start_time": "outside"}),
    ):
        store.append_audit_event(
            timestamp="2026-01-01T00:00:00+00:00",
            event="dispatch_owner_recorded",
            run_id=run_id,
            payload=payload,
        )


def _assert_indexed_owner_lookup(connection, sql):
    plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    details = "\n".join(row["detail"] for row in plan)
    assert "USING INDEX" in details
    assert "audit_events" not in sql
    assert "SCAN audit_events" not in details


def test_owner_projection_batches_only_the_requested_page(tmp_path):
    with SqliteRunStore(tmp_path / "owners.db") as store:
        _seed_owners(store)
        statements = []
        store._conn.set_trace_callback(statements.append)
        records = store.list_runs_by_branch("page")
        store._conn.set_trace_callback(None)

        assert len(statements) == 1
        assert "outside" not in statements[0]
        _assert_indexed_owner_lookup(store._conn, statements[0])
        assert {
            row.run_id: (row.owner_pid, row.owner_start_time) for row in records
        } == {"first": (2, "new"), "second": (4, None), "absent": (None, None)}


@pytest.mark.parametrize("committed", [False, True], ids=["writer", "committed"])
@pytest.mark.parametrize(
    ("run_id", "expected"),
    [("first", (2, "new")), ("second", (4, None)), ("absent", (None, None))],
)
def test_owner_point_projection_is_one_indexed_statement(
    tmp_path, committed, run_id, expected
):
    with SqliteRunStore(tmp_path / "owners.db") as store:
        _seed_owners(store)
        connection = store._committed_connection() if committed else store._conn
        statements = []
        connection.set_trace_callback(statements.append)
        if committed:
            with store.read_committed() as reader:
                record = reader.get(run_id)
        else:
            record = store.get(run_id)
        connection.set_trace_callback(None)

        queries = [sql for sql in statements if sql.startswith("SELECT")]
        assert len(queries) == 1
        _assert_indexed_owner_lookup(connection, queries[0])
        assert (record.owner_pid, record.owner_start_time) == expected


def test_owner_columns_ignore_a_conflicting_audit_event(tmp_path):
    with SqliteRunStore(tmp_path / "owners.db") as store:
        _seed_owners(store)
        assert store.get("first").owner_pid == 2
        store.append_audit_event(
            timestamp="2026-01-01T00:00:00+00:00",
            event="dispatch_owner_recorded",
            run_id="first",
            payload={"owner_pid": 8, "owner_start_time": "replacement"},
        )
        with store.read_committed() as reader:
            record = reader.get("first")
        assert (record.owner_pid, record.owner_start_time) == (2, "new")
        assert store.get("missing") is None
