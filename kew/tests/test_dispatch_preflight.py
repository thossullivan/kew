"""Shared pre-admission refusal evidence survives migration and transactions."""

from datetime import UTC, datetime

import pytest

from kew.run_store import RunRecord, SqliteRunStore


def test_round17_refusal_marker_migrates_and_round_trips(tmp_path):
    import sqlite3

    from kew.config import DispatchConfig
    from kew.dispatch_preflight import _record_refusal_row

    path = tmp_path / "legacy.db"
    with SqliteRunStore(str(path)) as writer:
        writer.save(
            RunRecord(
                run_id="legacy",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        if "pre_admission_refusal" in columns:
            connection.execute("ALTER TABLE runs DROP COLUMN pre_admission_refusal")
        connection.execute("UPDATE schema_version SET version = 49 WHERE id = 1")
    with SqliteRunStore(str(path)) as writer:
        assert not getattr(writer.get("legacy"), "pre_admission_refusal", False)
        row = _record_refusal_row(
            writer,
            DispatchConfig(),
            "alpha",
            7,
            "7",
            error_category="future_gate_refused",
            exit_code=3,
        )
    with SqliteRunStore(str(path), read_only=True) as reader:
        retained = reader.get(row.run_id)
        assert getattr(retained, "pre_admission_refusal", False) is True
        import kew.publication_credentials as credentials

        assert credentials.prelaunch_refusal_without_launch(reader, retained)


def test_round17_failed_refusal_transaction_leaves_no_marked_row(tmp_path, monkeypatch):
    from kew.config import DispatchConfig
    from kew.dispatch_preflight import _record_refusal_row

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        assert store.reserve_epic_node_item("alpha", "7", "A")
        admit = store.try_admit_run

        def fail_after_owner(record, **kwargs):
            require_owner = kwargs["on_admit"]

            def fail():
                require_owner()
                raise RuntimeError("injected refusal transaction failure")

            return admit(record, **{**kwargs, "on_admit": fail})

        monkeypatch.setattr(store, "try_admit_run", fail_after_owner)
        with pytest.raises(RuntimeError, match="injected refusal transaction failure"):
            _record_refusal_row(
                store,
                DispatchConfig(),
                "alpha",
                7,
                "7",
                error_category="future_gate_refused",
                expected_reservation_run_id="A",
            )
        assert store.get("A") is None
        assert store.list_by_item("7") == []
        assert [r.run_id for r in store.list_epic_reservations("alpha")] == ["A"]
