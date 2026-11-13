"""Loop/Iteration storage: schema V10 migration + CRUD."""

from datetime import UTC, datetime

from kew.run_store import (
    CURRENT_SCHEMA_VERSION,
    IterationRecord,
    LoopRecord,
    SqliteRunStore,
)


def test_migration_creates_loops_and_iterations_tables():
    store = SqliteRunStore(":memory:")
    tables = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "loops" in tables
    assert "iterations" in tables


def test_schema_version_is_current():
    store = SqliteRunStore(":memory:")
    row = store._conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()
    assert row["version"] == CURRENT_SCHEMA_VERSION


def _loop(loop_id: str = "L1", **kw: object) -> LoopRecord:
    fields: dict[str, object] = {
        "loop_id": loop_id,
        "source": "issue",
        "goal_text": "make CI green",
        "runner_name": "claude-code",
        "created_at": datetime(2026, 6, 23, tzinfo=UTC),
        "issue_num": 281,
    }
    fields.update(kw)
    return LoopRecord(**fields)  # type: ignore[arg-type]


def test_save_and_get_loop_round_trips():
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop())
    got = store.get_loop("L1")
    assert got is not None
    assert got.goal_text == "make CI green"
    assert got.status == "pursuing"
    assert got.issue_num == 281
    assert got.wall_clock_secs == 1800  # mandatory non-null default
    assert got.budget_usd is None


def test_loop_all_fields_round_trip():
    # Every non-default field set to a distinct value, asserted on read-back —
    # guards the hand-written 16-column positional _LOOP_UPSERT_SQL / _row_to_loop.
    store = SqliteRunStore(":memory:")
    rec = LoopRecord(
        loop_id="LX",
        source="issue",
        goal_text="ship it",
        runner_name="claude-code",
        created_at=datetime(2026, 6, 23, 0, 0, tzinfo=UTC),
        issue_num=99,
        repo="thossullivan/kew",
        status="needs_human",
        status_reason="gate_failed: 2 tests red",
        budget_usd=5.0,
        wall_clock_secs=600,
        max_steps=12,
        spent_usd=1.25,
        step_count=7,
        updated_at=datetime(2026, 6, 23, 0, 5, tzinfo=UTC),
        finished_at=datetime(2026, 6, 23, 0, 9, tzinfo=UTC),
    )
    store.save_loop(rec)
    got = store.get_loop("LX")
    assert got == rec


def test_get_loop_returns_none_when_absent():
    store = SqliteRunStore(":memory:")
    assert store.get_loop("nope") is None


def test_save_loop_upserts_status():
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop())
    store.save_loop(
        _loop(status="achieved", finished_at=datetime(2026, 6, 23, 1, tzinfo=UTC))
    )
    got = store.get_loop("L1")
    assert got is not None
    assert got.status == "achieved"
    assert got.finished_at is not None


def test_list_loops_filters_and_orders():
    store = SqliteRunStore(":memory:")
    # Distinct created_at so default ordering is deterministic.
    store.save_loop(
        _loop(
            "L1", status="pursuing", created_at=datetime(2026, 6, 23, 0, 1, tzinfo=UTC)
        )
    )
    store.save_loop(
        _loop(
            "L2", status="achieved", created_at=datetime(2026, 6, 23, 0, 2, tzinfo=UTC)
        )
    )
    assert [loop.loop_id for loop in store.list_loops(status="pursuing")] == ["L1"]
    assert [loop.loop_id for loop in store.list_loops()] == ["L1", "L2"]


def test_add_and_list_iterations_ordered_by_idx():
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop())
    store.add_iteration(
        IterationRecord(
            "I2",
            "L1",
            1,
            started_at=datetime(2026, 6, 23, 0, 2, tzinfo=UTC),
            kind="assistant_turn",
            run_id="run-xyz",
            cost_usd=0.5,
            tokens=1200,
        )
    )
    store.add_iteration(
        IterationRecord(
            "I1",
            "L1",
            0,
            started_at=datetime(2026, 6, 23, 0, 1, tzinfo=UTC),
            kind="assistant_turn",
            run_id="run-xyz",
        )
    )
    its = store.list_iterations("L1")
    assert [i.iteration_id for i in its] == ["I1", "I2"]
    assert its[0].idx == 0
    assert its[1].run_id == "run-xyz"  # parent run is shared across iterations
    assert its[1].cost_usd == 0.5


def test_iteration_all_fields_round_trip():
    # Every non-default field set distinctly — guards the positional INSERT /
    # _row_to_iteration column order.
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop())
    rec = IterationRecord(
        iteration_id="IX",
        loop_id="L1",
        idx=3,
        started_at=datetime(2026, 6, 23, 0, 1, tzinfo=UTC),
        run_id="run-abc",
        kind="assistant_turn",
        status="pursuing",
        cost_usd=0.42,
        tokens=900,
        finished_at=datetime(2026, 6, 23, 0, 2, tzinfo=UTC),
    )
    store.add_iteration(rec)
    got = store.list_iterations("L1")[0]
    assert got == rec


def test_iteration_run_id_may_be_null():
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop())
    store.add_iteration(
        IterationRecord("I1", "L1", 0, started_at=datetime(2026, 6, 23, tzinfo=UTC))
    )
    it = store.list_iterations("L1")[0]
    assert it.run_id is None


def test_list_iterations_empty_for_unknown_loop():
    store = SqliteRunStore(":memory:")
    assert store.list_iterations("nope") == []


def test_last_iteration_at_by_loop_reports_the_newest_stamp_per_loop():
    # add_iteration is the write a working loop makes between session start and
    # terminal, and it touches no loops.updated_at — so this read is what lets a
    # staleness reading see a long-running loop still moving (#2297).
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop("L1"))
    store.save_loop(_loop("L2"))
    store.add_iteration(
        IterationRecord(
            "I1",
            "L1",
            0,
            started_at=datetime(2026, 6, 23, 0, 1, tzinfo=UTC),
            finished_at=datetime(2026, 6, 23, 0, 2, tzinfo=UTC),
        )
    )
    store.add_iteration(
        IterationRecord(
            "I2",
            "L1",
            1,
            started_at=datetime(2026, 6, 23, 0, 9, tzinfo=UTC),
        )
    )
    store.add_iteration(
        IterationRecord(
            "I3", "L2", 0, started_at=datetime(2026, 6, 22, 0, 1, tzinfo=UTC)
        )
    )
    assert store.last_iteration_at_by_loop() == {
        "L1": "2026-06-23T00:09:00+00:00",  # newest of L1's two, unfinished
        "L2": "2026-06-22T00:01:00+00:00",
    }


def test_last_iteration_at_by_loop_is_empty_without_iterations():
    store = SqliteRunStore(":memory:")
    store.save_loop(_loop("L1"))
    assert store.last_iteration_at_by_loop() == {}


def test_migration_creates_loop_indexes():
    store = SqliteRunStore(":memory:")
    indexes = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_loops_status" in indexes
    assert "idx_loops_issue_num" in indexes
    assert "idx_iterations_loop_id" in indexes
