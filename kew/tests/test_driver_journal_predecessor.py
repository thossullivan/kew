"""A malformed latest journal row never stops the driver's journal (#2949).

The driver reads the epic's latest ``epic_ticks`` row back as the previous
snapshot. A row that is valid JSON but not a snapshot this build can read
(``nodes: [null]``) must be read as no predecessor: the tick still journals and
still writes its finish heartbeat, whether or not the row's stored hash matches.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths


def _settled_epic(tmp_path: Path, store: Any) -> dict[str, Any]:
    """An epic ticked until a further tick changes nothing."""
    env = {
        "deps": FakeSource(nodes=[_node("1")], edges=[]),
        "facts": FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        "sink": FakeSink(),
        "lock_path": tmp_path / "epic.lock",
    }
    _advance(store=store, **env)
    _advance(store=store, **env)
    return env


def _malform_latest_row(store: Any, *, keep_hash: bool) -> int:
    latest = store.latest_epic_tick(EPIC)
    assert latest is not None
    broken = {**latest.snapshot, "nodes": [None]}
    store._conn.execute(
        "UPDATE epic_ticks SET snapshot = ?, snapshot_hash = ? WHERE id = ?",
        (
            json.dumps(broken),
            latest.snapshot_hash if keep_hash else "hash-of-another-build",
            latest.id,
        ),
    )
    store._conn.commit()
    return int(latest.id)


@pytest.mark.parametrize("keep_hash", [True, False], ids=["matching", "stale"])
def test_a_malformed_latest_row_still_journals_and_heartbeats(
    tmp_path: Path, store: Any, keep_hash: bool
) -> None:
    env = _settled_epic(tmp_path, store)
    broken_id = _malform_latest_row(store, keep_hash=keep_hash)
    before = store.get_epic_driver_state(EPIC)

    _advance(store=store, **env)

    after = store.get_epic_driver_state(EPIC)
    latest = store.latest_epic_tick(EPIC)
    assert latest is not None
    # The broken row gave no predecessor, so the tick's own snapshot (every
    # node read as new, nothing carried from the broken row) is the latest.
    assert latest.id > broken_id
    assert [n["id"] for n in latest.snapshot["nodes"]] == ["1"]
    assert after.tick_count == before.tick_count + 1
    assert after.last_tick_ts != before.last_tick_ts
    assert after.last_tick_id == latest.id
    (attempt,) = store.list_epic_tick_log_tail(EPIC, limit=1)
    assert attempt.outcome != "failed"
    assert attempt.journal_tick_id == latest.id


def _unjson_latest_row(store: Any, column: str) -> int:
    latest = store.latest_epic_tick(EPIC)
    assert latest is not None
    store._conn.execute(
        f"UPDATE epic_ticks SET {column} = '{{not json' WHERE id = ?",  # noqa: S608
        (latest.id,),
    )
    store._conn.commit()
    return int(latest.id)


def _journal_rows(store: Any) -> list[tuple[int, int, int]]:
    """(id, snapshot is JSON, actions is JSON) for each of the epic's rows."""
    rows = store._conn.execute(
        "SELECT id, json_valid(snapshot), json_valid(actions) FROM epic_ticks"
        " WHERE epic_ref = ? ORDER BY id",
        (EPIC,),
    ).fetchall()
    return [tuple(r) for r in rows]


def _assert_two_ticks_finished(
    store: Any, before: Any, journal_ids: list[int | None]
) -> None:
    after = store.get_epic_driver_state(EPIC)
    assert after.tick_count == before.tick_count + 2
    assert after.last_tick_ts != before.last_tick_ts
    assert after.last_tick_id == journal_ids[-1]
    attempts = store.list_epic_tick_log_tail(EPIC, limit=2)
    assert [(a.outcome, a.error_class) for a in attempts] == [("nothing", None)] * 2
    assert [a.journal_tick_id for a in attempts] == journal_ids


def test_a_latest_snapshot_that_is_not_json_is_no_predecessor(
    tmp_path: Path, store: Any
) -> None:
    """The unreadable row gives the tick no predecessor: the tick appends its
    own snapshot, draws it as the change, and the tick after it is quiet."""
    fixture = _settled_epic(tmp_path, store)
    broken_id = _unjson_latest_row(store, "snapshot")
    before = store.get_epic_driver_state(EPIC)

    _advance(store=store, **fixture)
    _advance(store=store, **fixture)

    rows = _journal_rows(store)
    assert rows[:-1] == [(broken_id, 0, 1)]
    new_id = rows[-1][0]
    assert rows[-1] == (new_id, 1, 1)
    _assert_two_ticks_finished(store, before, [new_id, new_id])
    first, _ = store.list_epic_tick_log_tail(EPIC, limit=2)
    assert (first.id, first.journal_tick_id) in {
        (m.id, m.journal_tick_id) for m in store.list_epic_tick_log_marks(EPIC)
    }


def test_latest_actions_that_are_not_json_never_stop_a_tick(
    tmp_path: Path, store: Any
) -> None:
    """The predecessor never reads ``actions``: the snapshot still matches, so
    both ticks are quiet ticks on the same row and both finish."""
    fixture = _settled_epic(tmp_path, store)
    broken_id = _unjson_latest_row(store, "actions")
    before = store.get_epic_driver_state(EPIC)

    _advance(store=store, **fixture)
    _advance(store=store, **fixture)

    assert _journal_rows(store) == [(broken_id, 1, 0)]
    _assert_two_ticks_finished(store, before, [broken_id, broken_id])
