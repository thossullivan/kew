"""Real-process races at the managed ledger-open boundary."""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path

from kew.ledger_identity import RegistryRecord, open_managed_store, registry_paths


def _open(project: str, db: str, anchors: str, result: multiprocessing.Queue) -> None:
    try:
        with open_managed_store(
            project_root=project, db_path=db, anchors_path=anchors
        ) as store:
            ledger_id = store._conn.execute(
                "SELECT ledger_id FROM ledger_identity WHERE id = 1"
            ).fetchone()[0]
        result.put(("opened", ledger_id))
    except Exception as exc:  # noqa: BLE001 - subprocess reports the verdict
        result.put(("refused", str(exc)))


def test_same_path_first_openers_converge_on_one_identity(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()  # Bound discovery in spawned processes too.
    db = tmp_path / "dispatch.db"
    anchors = tmp_path / "anchors.jsonl"
    result: multiprocessing.Queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_open, args=(str(tmp_path), str(db), str(anchors), result)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    outcomes = [result.get(timeout=2) for _ in processes]
    assert {outcome[0] for outcome in outcomes} == {"opened"}
    assert len({outcome[1] for outcome in outcomes}) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger_identity").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE event = 'ledger_identity_bound'"
            ).fetchone()[0]
            == 1
        )


def test_different_path_first_openers_create_one_and_refuse_one(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()  # Spawned processes do not inherit fixture patches.
    anchors = tmp_path / "anchors.jsonl"
    result: multiprocessing.Queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_open,
            args=(str(tmp_path), str(tmp_path / name), str(anchors), result),
        )
        for name in ("one.db", "two.db")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    outcomes = [result.get(timeout=2) for _ in processes]
    assert sorted(outcome[0] for outcome in outcomes) == ["opened", "refused"]
    assert "WRONG-LEDGER" in next(
        outcome[1] for outcome in outcomes if outcome[0] == "refused"
    )
    record = RegistryRecord.from_json(registry_paths(tmp_path).registry.read_text())
    assert record.db_path in {tmp_path / "one.db", tmp_path / "two.db"}
