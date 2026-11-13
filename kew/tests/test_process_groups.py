"""Bound untrusted event evidence without interrupting fleet stop."""

import json
from datetime import UTC, datetime

import pytest
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table

from kew.config import DispatchConfig
from kew.fleet import stop_fleet
from kew.run_store import RunRecord, SqliteRunStore


@pytest.mark.parametrize(
    "damage", ["oversized", "depth", "utf8", "json", "recursion", "memory"]
)
def test_round6_bad_event_retains_row_and_stops_other_run(
    tmp_path, monkeypatch, caplog, damage, process_table
):
    payloads = {
        "oversized": b"[" * 10000 + b"]" * 10000,
        "depth": b"[" * 65 + b"]" * 65,
        "utf8": b"\xff",
        "json": b"{broken",
        "recursion": b'"raise-recursion"',
        "memory": b'"raise-memory"',
    }
    loads = json.loads

    def decode(line, *args, **kwargs):
        if line == '"raise-recursion"\n':
            raise RecursionError("injected decoder recursion")
        if line == '"raise-memory"\n':
            raise MemoryError("injected decoder allocation failure")
        return loads(line, *args, **kwargs)

    monkeypatch.setattr("kew.process_groups.json.loads", decode)
    monkeypatch.setattr("kew.fleet.reset_issue_to_ready", lambda *args: None)
    with SqliteRunStore(":memory:") as store:
        for number in (1, 2):
            row = RunRecord(
                run_id=f"run-{number}",
                issue_num=number,
                branch=f"agent/{number}",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=2**31 - 1,
                owner_pid=2**31 - 1,
                owner_start_time=BIRTH,
                attempt_pgid=None if number == 1 else 2**31 - 2,
                log_file=tmp_path / f"run-{number}.log",
            )
            store.save(row)
            if number == 1:
                row.log_file.with_suffix(".events.jsonl").write_bytes(
                    payloads[damage] + b"\n"
                )
        report = stop_fleet(store, DispatchConfig(), grace_seconds=0)
        assert store.get("run-2").status == "killed", (
            "another row must receive and complete stop"
        )
        assert store.get("run-1").status == "pending"
        assert "event evidence unavailable" in report.retained[0].note
        assert "Skipping malformed" in caplog.text
