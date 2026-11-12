"""Tests for kew.service.epics — epic DAG reads over the tick journal
(#454 Phase B). Seeds a real SqliteRunStore via append_epic_tick/begin_epic_
tick/finish_epic_tick/append_audit_event + RunRecord rows — no driver import,
service tests stay below the graph layer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.config import DispatchConfig, ProjectConfig
from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S
from kew.epic_archive import EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT
from kew.graph.driver import journal
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.watch import EpicWatchHub
from kew.service import dashboard
from kew.service.epics import (
    DRIVER_GONE,
    DRIVER_LIVE,
    DRIVER_UNKNOWN,
    NODE_STATUS_EVENT,
    DriverLockFacts,
    _approver_by_issue,
    _heartbeat_dto,
    driver_liveness,
    epic_member_ids,
    fetch_epic_frames,
    get_epic_detail,
    get_epic_timeline,
    get_node_detail,
    is_epic_member,
    known_epic_refs,
    list_epics,
    list_ticks,
    liveness_from_state,
    tick_strip,
    valid_node_id,
)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def _snapshot(
    *,
    signal: str = "progressing",
    counts: dict[str, int] | None = None,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[list[str]] | None = None,
    frontier: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": "e",
        "signal": signal,
        "counts": counts or {},
        "frontier": frontier or [],
        "nodes": nodes or [],
        "edges": edges or [],
    }


def _node(node_id: str, status: str = "todo", **overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "title": f"issue {node_id}",
        "status": status,
        "status_since": "2026-07-02T00:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    node.update(overrides)
    return node


def _actions(
    *,
    dispatched: list[str] | None = None,
    merged: list[str] | None = None,
    held: list[str] | None = None,
    stuck: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "dispatched": dispatched or [],
        "merged": merged or [],
        "held": held or [],
        "stuck": stuck or [],
    }


def _run(
    store: Any,
    *,
    run_id: str,
    issue_num: int,
    cost: float,
    started_at: datetime,
    finished_at: datetime | None,
    item_id: str | None = None,
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=f"issue-{issue_num}",
            status="success",
            exit_code=0,
            started_at=started_at,
            finished_at=finished_at,
            estimated_cost_usd=cost,
        )
    )


def test_list_epics_carries_the_archive_decision(store: Any) -> None:
    """#2206: the list is where Active and Finished/Archived are split, so the
    summary has to say which an epic is — and say who retired it and why, so
    the shelf is readable without a second request."""
    store.begin_epic_tick("kept", "2026-07-02T00:00:00+00:00")
    store.begin_epic_tick("retired", "2026-07-02T00:00:00+00:00")
    store.append_audit_event(
        timestamp="2026-09-29T10:00:00+00:00",
        event=EPIC_ARCHIVED_EVENT,
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": "retired", "reason": "abandoned in August"},
    )

    by_ref = {s.epic_ref: s for s in list_epics(store)}
    assert by_ref["kept"].archived is False
    assert by_ref["kept"].archived_at is None
    assert by_ref["kept"].archive_reason == ""
    assert by_ref["retired"].archived is True
    assert by_ref["retired"].archived_at == "2026-09-29T10:00:00+00:00"
    assert by_ref["retired"].archive_reason == "abandoned in August"


def test_list_epics_reports_an_unarchived_epic_as_active(store: Any) -> None:
    store.begin_epic_tick("revived", "2026-07-02T00:00:00+00:00")
    for event in (EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT):
        store.append_audit_event(
            timestamp="2026-09-29T10:00:00+00:00",
            event=event,
            actor_type="human",
            actor_id="owner",
            payload={"epic_ref": "revived"},
        )
    assert list_epics(store)[0].archived is False


def test_known_refs_and_list_epics(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="b",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(
            signal="progressing", counts={"todo": 2}, nodes=[_node("1"), _node("2")]
        ),
        actions=_actions(),
        snapshot_hash="hb1",
    )
    store.append_epic_tick(
        epic_ref="a",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(signal="blocked", counts={"stuck": 1}, nodes=[_node("3")]),
        actions=_actions(),
        snapshot_hash="ha1",
    )
    # Pre-journal epic: heartbeat exists, but no journal rows yet.
    store.begin_epic_tick("c", "2026-07-02T00:00:00+00:00")

    assert known_epic_refs(store) == {"a", "b", "c"}

    summaries = list_epics(store)
    assert [s.epic_ref for s in summaries] == ["a", "b", "c"]
    a, b, c = summaries
    assert a.signal == "blocked"
    assert a.counts == {"stuck": 1}
    assert a.change_count == 1
    assert b.signal == "progressing"
    assert b.counts == {"todo": 2}
    assert b.change_count == 1
    # Pre-journal epic: snapshot-less counts == {}.
    assert c.signal is None
    assert c.counts == {}
    assert c.change_count == 0


def test_list_epics_serves_the_tick_start_beside_the_tick_finish(store: Any) -> None:
    """#2283: the Epics list reads liveness off the START stamp, like
    ``driver_liveness`` does, so the summary has to carry it.

    ``last_tick_ts`` only lands when a tick FINISHES. Served the finish stamp
    alone, the list called a driver quiet for exactly as long as it was busy:
    a tick running longer than twice its interval drew "no heartbeat" while
    the dashboard and the epic page both called the same driver live.
    """
    tick_id, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:02:30+00:00",
        duration_s=150.0,
        interval_s=30.0,
        signal="progressing",
        tick_id=tick_id,
    )

    summary = list_epics(store)[0]
    assert summary.last_tick_started_at == "2026-07-02T00:00:00+00:00"
    assert summary.last_tick_ts == "2026-07-02T00:02:30+00:00"
    # Same two facts the heartbeat DTO the epic page reads reports.
    detail = get_epic_detail(store, "e")
    assert detail is not None and detail.heartbeat is not None
    assert detail.heartbeat.last_tick_started_at == summary.last_tick_started_at


def test_list_epics_leaves_the_tick_start_absent_with_no_driver(store: Any) -> None:
    # A pre-journal/pre-heartbeat epic: "we don't know when the tick began" is
    # None, not the journal's finish stamp standing in for it.
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(),
        actions=_actions(),
        snapshot_hash="h1",
    )
    summary = list_epics(store)[0]
    assert summary.last_tick_started_at is None
    assert summary.last_tick_ts == "2026-07-02T00:00:00+00:00"


def test_get_epic_detail_maps_snapshot_and_heartbeat(store: Any) -> None:
    nodes = [
        _node("1", status="todo"),
        # Malformed entry: missing most fields, plus an unknown key.
        {"id": "2", "status": "done", "unexpected_key": "x"},
    ]
    tick_id, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(
            nodes=nodes,
            counts={"todo": 1, "done": 1},
            frontier=["1"],
            edges=[["1", "2"]],
        ),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:00:05+00:00",
        duration_s=5.0,
        interval_s=30.0,
        signal="progressing",
        tick_id=tick_id,
    )

    detail = get_epic_detail(store, "e")
    assert detail is not None
    assert detail.snapshot is not None
    assert detail.snapshot.tick_id == tick_id
    assert detail.snapshot.tick_ts == "2026-07-02T00:00:00+00:00"
    assert [n.id for n in detail.snapshot.nodes] == ["1", "2"]

    malformed = detail.snapshot.nodes[1]
    assert malformed.status == "done"
    assert malformed.title == ""  # defaulted, not crashed
    assert malformed.status_since is None
    assert malformed.external is False

    # edges is typed list[tuple[str, str]] on the DTO, but must still
    # serialize as a JSON array of 2-arrays for the frontend (#458 review).
    assert detail.snapshot.edges == [("1", "2")]
    assert detail.snapshot.model_dump()["edges"] == [("1", "2")]
    assert json.loads(detail.snapshot.model_dump_json())["edges"] == [["1", "2"]]

    assert detail.heartbeat is not None
    assert detail.heartbeat.change_count == 1
    assert detail.heartbeat.tick_count == 1
    assert detail.heartbeat.last_signal == "progressing"


def test_get_epic_detail_pre_journal(store: Any) -> None:
    store.begin_epic_tick("only-hb", "2026-07-02T00:00:00+00:00")

    detail = get_epic_detail(store, "only-hb")

    assert detail is not None
    assert detail.snapshot is None
    assert detail.economics.window_start is None
    assert detail.economics.drain_total_usd == 0.0
    assert detail.economics.node_drain_usd == {}
    assert detail.heartbeat is not None
    assert detail.heartbeat.tick_count == 1
    assert detail.heartbeat.change_count == 0


def test_get_epic_detail_unknown_ref_returns_none(store: Any) -> None:
    assert get_epic_detail(store, "does-not-exist") is None


def test_list_ticks_cursor_pagination(store: Any) -> None:
    ids = []
    for i in range(5):
        tick_id, _ = store.append_epic_tick(
            epic_ref="e",
            tick_ts=f"2026-07-02T00:0{i}:00+00:00",
            snapshot=_snapshot(nodes=[_node("1")]),
            actions=_actions(),
            snapshot_hash=f"h{i}",
        )
        ids.append(tick_id)

    page = list_ticks(store, "e", after_id=ids[1], limit=2)

    assert [t.id for t in page] == ids[2:4]
    assert [t.tick_ts for t in page] == [
        "2026-07-02T00:02:00+00:00",
        "2026-07-02T00:03:00+00:00",
    ]


def test_drain_vs_lifetime_windows(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T12:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h1",
    )
    _run(
        store,
        run_id="r-before",
        issue_num=101,
        cost=5.0,
        started_at=datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 1, 0, tzinfo=UTC),  # before window
    )
    _run(
        store,
        run_id="r-after",
        issue_num=101,
        cost=2.0,
        started_at=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 14, 0, tzinfo=UTC),  # after window
    )

    detail = get_epic_detail(store, "e")
    assert detail is not None
    assert detail.economics.node_drain_usd["101"] == 2.0
    assert detail.economics.drain_total_usd == 2.0

    node_detail = get_node_detail(store, "e", "101")
    assert node_detail is not None
    assert node_detail.lifetime_usd == 7.0
    assert node_detail.drain_usd == 2.0
    assert len(node_detail.runs) == 2


def test_linear_node_drain_and_detail_keyed_on_item_id(store: Any) -> None:
    """A non-numeric tracker node (``SFD-12``) draws its drain badge and run
    history from the durable item_id — int() coercion would silently drop it.
    """
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T12:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("SFD-12")]),
        actions=_actions(),
        snapshot_hash="h1",
    )
    _run(
        store,
        run_id="r-linear",
        issue_num=0,  # 0-sentinel for a non-numeric tracker
        item_id="SFD-12",
        cost=4.0,
        started_at=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 14, 0, tzinfo=UTC),
    )

    detail = get_epic_detail(store, "e")
    assert detail is not None
    assert detail.economics.node_drain_usd["SFD-12"] == 4.0
    assert detail.economics.drain_total_usd == 4.0

    node_detail = get_node_detail(store, "e", "SFD-12")
    assert node_detail is not None
    assert node_detail.id == "SFD-12"
    assert node_detail.lifetime_usd == 4.0
    assert node_detail.drain_usd == 4.0
    assert len(node_detail.runs) == 1


def test_a_done_linear_node_with_no_journaled_pr_shows_the_ledgers(store: Any) -> None:
    """The Linear adapter reads no PR for a closed issue, so a done Linear node
    is journaled with ``pr_url`` None and the console said "no PR" on every
    node of a finished Linear epic. The detail read fills it from the newest
    run that carries one; a journaled PR is kept, and a node with no PR run
    stays None (Tom's report, 2026-10-03)."""
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T12:00:00+00:00",
        snapshot=_snapshot(
            nodes=[
                _node("SFD-12", status="done"),
                _node("SFD-13", status="done", pr_url="https://example.invalid/pr/7"),
                _node("SFD-14", status="pending"),
            ]
        ),
        actions=_actions(),
        snapshot_hash="h1",
    )
    seeded = (("r-old", 9, "pr/1"), ("r-new", 11, "pr/2"), ("r-none", 12, None))
    for run_id, hour, pr in seeded:
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=0,
                item_id="SFD-12" if run_id != "r-none" else "SFD-13",
                branch="agent/sfd-12",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 7, 2, hour, 0, tzinfo=UTC),
                finished_at=datetime(2026, 7, 2, hour, 30, tzinfo=UTC),
                pr_url=f"https://example.invalid/{pr}" if pr else None,
            )
        )

    detail = get_epic_detail(store, "e")
    assert detail is not None and detail.snapshot is not None
    prs = {n.id: n.pr_url for n in detail.snapshot.nodes}
    assert prs == {
        "SFD-12": "https://example.invalid/pr/2",
        "SFD-13": "https://example.invalid/pr/7",
        "SFD-14": None,
    }


def test_replay_frame_economics_upper_bound(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T02:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h2",
    )
    _run(
        store,
        run_id="r1",
        issue_num=101,
        cost=3.0,
        started_at=datetime(2026, 7, 2, 0, 30, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 1, 0, tzinfo=UTC),  # between T1, T2
    )

    tick1, tick2 = list_ticks(store, "e")

    assert tick1.economics is not None
    assert tick1.economics.node_drain_usd.get("101", 0.0) == 0.0
    assert tick2.economics is not None
    assert tick2.economics.node_drain_usd["101"] == 3.0


def test_merged_actions_enriched_with_approver(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101", status="merged")]),
        actions=_actions(merged=["101"]),
        snapshot_hash="h1",
    )
    store.append_audit_event(
        timestamp="2026-07-02T00:00:00+00:00",
        event="epic_merge",
        issue_num=101,
        actor_type="human",
        actor_id="alice",
        payload={"trigger": "approval", "actor": "alice", "sha": "abc", "pr": 1},
    )

    ticks = list_ticks(store, "e")

    assert ticks[0].actions["merged"] == [{"id": "101", "approved_by": "alice"}]


def test_approver_by_issue_newest_wins(store: Any) -> None:
    """list_audit_events is newest-first; two epic_merge events for the same
    issue (e.g. a re-merge after revert) must resolve to the LATEST actor,
    not the first one seen chronologically (#458 review — setdefault over
    a newest-first stream only does that if callers don't accidentally
    reverse the order)."""
    # Appended in chronological order: alice first (older), bob second
    # (newer) -- list_audit_events returns bob before alice (id DESC).
    store.append_audit_event(
        timestamp="2026-07-02T00:00:00+00:00",
        event="epic_merge",
        issue_num=101,
        actor_type="human",
        actor_id="alice",
        payload={"trigger": "approval", "actor": "alice", "sha": "abc", "pr": 1},
    )
    store.append_audit_event(
        timestamp="2026-07-02T01:00:00+00:00",
        event="epic_merge",
        issue_num=101,
        actor_type="human",
        actor_id="bob",
        payload={"trigger": "approval", "actor": "bob", "sha": "def", "pr": 2},
    )

    approvers = _approver_by_issue(store)

    assert approvers["101"] == "bob"


def test_merged_actions_enriched_with_most_recent_approver(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101", status="merged")]),
        actions=_actions(merged=["101"]),
        snapshot_hash="h1",
    )
    # First (older) approval, then a second (newer) one for the same issue.
    store.append_audit_event(
        timestamp="2026-07-02T00:00:00+00:00",
        event="epic_merge",
        issue_num=101,
        actor_type="human",
        actor_id="alice",
        payload={"trigger": "approval", "actor": "alice", "sha": "abc", "pr": 1},
    )
    store.append_audit_event(
        timestamp="2026-07-02T01:00:00+00:00",
        event="epic_merge",
        issue_num=101,
        actor_type="human",
        actor_id="bob",
        payload={"trigger": "approval", "actor": "bob", "sha": "def", "pr": 2},
    )

    ticks = list_ticks(store, "e")

    assert ticks[0].actions["merged"] == [{"id": "101", "approved_by": "bob"}]


def test_opaque_merged_action_keeps_approver_in_ticks_and_stream_frame() -> None:
    with SqliteRunStore(":memory:") as store:
        store.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-07-02T00:00:00+00:00",
            snapshot=_snapshot(nodes=[_node("SFD-12", status="merged")]),
            actions=_actions(merged=["SFD-12"]),
            snapshot_hash="h-sfd",
        )
        store.append_audit_event(
            timestamp="2026-07-02T00:00:00+00:00",
            event="epic_merge",
            issue_num=0,
            item_id="SFD-12",
            actor_type="human",
            actor_id="alice",
            payload={
                "trigger": "approval",
                "actor": "alice",
                "sha": "abc",
                "pr": 1,
            },
        )

        ticks = list_ticks(store, "e")
        _cursor, frames = fetch_epic_frames(store, "e", {})
        snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")

    expected = [{"id": "SFD-12", "approved_by": "alice"}]
    assert ticks[0].actions["merged"] == expected
    assert snapshot_frame["actions"]["merged"] == expected


def test_fetch_epic_frames_multi_type_cursor(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:00:05+00:00",
        duration_s=5.0,
        interval_s=30.0,
        signal="progressing",
        tick_id=1,
    )

    cursor, frames = fetch_epic_frames(store, "e", {})
    assert {k for k, _ in frames} == {
        "snapshot",
        "attention",
        "recovery",
        "heartbeat",
        "economics",
    }

    # Unchanged second call -> no frames, cursor stable.
    cursor2, frames2 = fetch_epic_frames(store, "e", cursor)
    assert frames2 == []
    assert cursor2 == cursor

    # New journal row only (heartbeat + economics unchanged) -> snapshot only.
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:01:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h2",
    )
    cursor3, frames3 = fetch_epic_frames(store, "e", cursor2)
    assert [k for k, _ in frames3] == ["snapshot"]

    # New journal row AND heartbeat advances (tick_count) -> snapshot+heartbeat.
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:02:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h3",
    )
    store.begin_epic_tick("e", "2026-07-02T00:02:00+00:00")
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:02:05+00:00",
        duration_s=5.0,
        interval_s=30.0,
        signal="progressing",
        tick_id=3,
    )
    cursor4, frames4 = fetch_epic_frames(store, "e", cursor3)
    assert {k for k, _ in frames4} == {"snapshot", "heartbeat"}

    # Run row cost change alone (no new tick, no heartbeat change) -> economics only.
    _run(
        store,
        run_id="r1",
        issue_num=101,
        cost=1.5,
        started_at=datetime(2026, 7, 2, 0, 3, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 0, 3, tzinfo=UTC),
    )
    _cursor5, frames5 = fetch_epic_frames(store, "e", cursor4)
    assert [k for k, _ in frames5] == ["economics"]


def test_fetch_epic_frames_emits_a_heartbeat_for_an_attempt_the_guards_refused(
    store: Any,
) -> None:
    """Reviewer finding (PR #2438, round 3). The epic page redraws its tick
    strip when the live channel hands it a heartbeat frame, and the cursor that
    decides whether to send one was ``(tick_count, last_tick_ts, last_signal)``
    — three fields ``begin``/``finish_epic_tick`` write, which ``advance`` only
    reaches past the lock, the preflight and the non-empty-graph guard. A
    driver locked out or failing every tick moved none of them, moved none of
    the other three frame hashes either, so the stream went silent and the
    strip sat frozen on its last reading through exactly the attempts it was
    added to show.
    """
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=30.0)
    cursor, _ = fetch_epic_frames(store, "e", {})
    assert fetch_epic_frames(store, "e", cursor)[1] == []

    # One locked-out attempt: it journals nothing and touches no heartbeat
    # column, exactly as the driver's lock-out path does.
    row_id = store.begin_epic_tick_log("e", "2026-07-02T00:00:30+00:00")
    cursor2, frames = fetch_epic_frames(store, "e", cursor)
    assert [k for k, _ in frames] == ["heartbeat"]
    beat = dict(frames[0][1])
    assert beat["tick_count"] == 1  # unmoved — this is the whole point
    assert beat["tick_attempt_stamp"] == store.epic_tick_log_stamp("e")

    # Closing it is a second change to the strip (the live slot gets its
    # outcome), so it is a second frame.
    store.finish_epic_tick_log(
        row_id, ts_finished="2026-07-02T00:00:31+00:00", outcome="locked_out"
    )
    cursor3, frames2 = fetch_epic_frames(store, "e", cursor2)
    assert [k for k, _ in frames2] == ["heartbeat"]
    assert dict(frames2[0][1])["tick_attempt_stamp"] == store.epic_tick_log_stamp("e")

    # And a quiet poll is still quiet: the stamp is a stamp, not a timer.
    assert fetch_epic_frames(store, "e", cursor3)[1] == []


def test_fetch_epic_frames_streams_an_attempt_only_epics_stamp(store: Any) -> None:
    """Reviewer finding (PR #2438, round 4). An epic whose every tick is
    refused before ``begin_epic_tick`` — locked out, failing preflight, empty
    graph — has attempt rows and NO ``epic_driver_state`` row, which is the
    epic this round's own ``list_epic_refs`` fix made reachable. Both the
    cursor tuple and the heartbeat DTO returned ``None`` on a missing driver
    row, so the stream emitted no frame for any new attempt: the epic page's
    strip keys on ``heartbeat.tick_attempt_stamp``, so it was read once at
    mount and then frozen through exactly the refused attempts it exists to
    show.
    """
    # No heartbeat seed at all: only the attempt log knows this epic.
    row_id = store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
    assert store.get_epic_driver_state("e") is None

    cursor, frames = fetch_epic_frames(store, "e", {})
    assert "heartbeat" in {k for k, _ in frames}
    beat = next(payload for kind, payload in frames if kind == "heartbeat")
    assert beat["tick_attempt_stamp"] == store.epic_tick_log_stamp("e")
    # Nothing is invented around the stamp: no tick has finished, so there is
    # no instant and no cadence, which the console reads as "no heartbeat yet".
    assert beat["last_tick_started_at"] is None
    assert beat["last_tick_ts"] is None
    assert beat["interval_s"] is None
    assert beat["tick_count"] == 0
    assert beat["driver_status"] == "unknown"

    # A synced subscriber stays quiet...
    assert fetch_epic_frames(store, "e", cursor)[1] == []

    # ...and every further attempt moves the stamp it redraws the strip on.
    store.finish_epic_tick_log(
        row_id, ts_finished="2026-07-02T00:00:01+00:00", outcome="locked_out"
    )
    cursor2, frames2 = fetch_epic_frames(store, "e", cursor)
    assert [k for k, _ in frames2] == ["heartbeat"]
    assert dict(frames2[0][1])["tick_attempt_stamp"] == store.epic_tick_log_stamp("e")

    store.begin_epic_tick_log("e", "2026-07-02T00:00:30+00:00")
    _cursor3, frames3 = fetch_epic_frames(store, "e", cursor2)
    assert [k for k, _ in frames3] == ["heartbeat"]
    assert dict(frames3[0][1])["tick_attempt_stamp"] == store.epic_tick_log_stamp("e")

    # The point read the page seeds from agrees with the frame, so the strip's
    # first reading and its re-reads are keyed on the same fact.
    detail = get_epic_detail(store, "e")
    assert detail is not None and detail.heartbeat is not None
    assert detail.heartbeat.tick_attempt_stamp == store.epic_tick_log_stamp("e")


def test_no_heartbeat_is_invented_without_an_attempt_behind_it(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the round-4 fix: the stamp is what makes a
    driver-row-less heartbeat honest, so an epic no attempt row mentions still
    has nothing to report — and neither does a store that cannot read the
    attempt log at all, whose ``""`` must not become a heartbeat either.
    """
    assert _heartbeat_dto(store, "no-such-epic") is None
    cursor, frames = fetch_epic_frames(store, "no-such-epic", {})
    assert "heartbeat" not in {k for k, _ in frames}
    assert cursor["hb"] is None

    # A store predating ``epic_tick_log`` answers "" for the stamp (see
    # ``_tick_attempt_stamp``); that is a read that cannot say, not an attempt.
    store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
    monkeypatch.setattr(store, "epic_tick_log_stamp", None, raising=False)
    assert _heartbeat_dto(store, "e") is None
    assert "heartbeat" not in {k for k, _ in fetch_epic_frames(store, "e", {})[1]}


def test_list_epics_stamps_a_row_on_attempts_its_heartbeat_cannot_see(
    store: Any,
) -> None:
    """Reviewer finding (PR #2438, round 3), roster half. ``epicStripRows``
    keys a row's strip re-read on this stamp; before it the only stamps on
    offer were the two heartbeat instants, so the strip of a row whose driver
    is locked out, failing preflight or finding an empty graph was never
    re-read at all.
    """
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=30.0)
    before = list_epics(store)[0]
    assert before.epic_ref == "e"

    store.begin_epic_tick_log("e", "2026-07-02T00:00:30+00:00")
    after = list_epics(store)[0]
    assert after.tick_attempt_stamp != before.tick_attempt_stamp
    # Neither heartbeat instant moved — the stamp is the only signal there was.
    assert after.last_tick_started_at == before.last_tick_started_at
    assert after.last_tick_ts == before.last_tick_ts

    # The roster reads it batched; the point read the epic page makes must
    # agree, or the two surfaces re-read on different signals.
    detail_beat = _heartbeat_dto(store, "e")
    assert detail_beat is not None
    assert detail_beat.tick_attempt_stamp == after.tick_attempt_stamp


def test_list_epics_forwards_the_liveness_reading_a_long_tick_depends_on(
    store: Any,
) -> None:
    """Reviewer finding (PR #2438, round 5), roster half.

    The row's tick strip bounds "running" on the age of the OPEN attempt row's
    start, because that is the only instant such a row has. A tick that
    legitimately runs longer than that bound — what ``_lock_keepalive`` exists
    to support — then draws quiet and words itself "abandoned" while the driver
    is demonstrably beating. This reading is the other half: it thresholds the
    NEWEST heartbeat the driver writes, the lock's own included, and the roster
    already composed it per row and dropped it on the floor.
    """
    started = "2026-07-02T00:00:00+00:00"
    _seed_heartbeat(store, "e", started_at=started, interval_s=30.0)
    # A second tick opens and then blocks for far longer than its own cadence
    # (the merge queue's combined-tree verification, #2148) — so the only tick
    # stamp that moves is a start, and it ages.
    store.begin_epic_tick("e", "2026-07-02T00:10:00+00:00")
    now = datetime(2026, 7, 2, 0, 16, 40, tzinfo=UTC)  # start + 400s
    # ... while the driver keeps beating its lock mid-tick.
    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=True,
        heartbeat_at="2026-07-02T00:16:35+00:00",
        observed_at=now.isoformat(),
    )

    summary = list_epics(store, now=now)[0]
    assert summary.driver_status == DRIVER_LIVE
    # The same reading the epic page's heartbeat carries: the roster forwards
    # it rather than deriving a second one that could disagree.
    beat = _heartbeat_dto(store, "e", now=now)
    assert beat is not None and beat.driver_status == summary.driver_status

    # And it is a reading, not a constant: the same aged start stamp with no
    # lock evidence behind it is the dead-driver case, and says so.
    _seed_heartbeat(store, "f", started_at=started, interval_s=30.0)
    store.begin_epic_tick("f", "2026-07-02T00:10:00+00:00")
    rows = {row.epic_ref: row for row in list_epics(store, now=now)}
    assert rows["f"].driver_status == DRIVER_GONE


def test_the_live_stream_resends_the_heartbeat_when_the_reading_goes_stale(
    store: Any,
) -> None:
    """Reviewer finding (PR #2438, round 5), streaming half.

    A subscriber's tick strip reads an OPEN attempt row against the served
    ``driver_status``. Every other member of the frame's change tuple is
    written BY a tick, so a driver that opens a row and then dies mid-tick
    moves none of them again — and the strip would hold the ``live`` reading
    taken when that row opened, which is the forever-running bar the strip's
    own floor exists to prevent. The reading itself is in the tuple, so the
    frame re-sends when it lapses.
    """
    # Against the real clock, because that is what the reading is taken
    # against: a tick that has only just opened and has not closed.
    started = datetime.now(UTC).isoformat()
    _seed_heartbeat(store, "e", started_at=started, interval_s=30.0)
    store.begin_epic_tick_log("e", started)
    store.begin_epic_tick("e", started)

    cursor, _ = fetch_epic_frames(store, "e", {})
    assert cursor["hb"][-1] == DRIVER_LIVE
    _, settled_frames = fetch_epic_frames(store, "e", cursor)
    assert "heartbeat" not in dict(settled_frames)
    settled = cursor

    # The member moves with the CLOCK and nothing else: the same row, read an
    # hour on, has lapsed its 30s cadence.
    state = store.get_epic_driver_state("e")
    an_hour_on = datetime.now(UTC) + timedelta(hours=1)
    assert liveness_from_state(state, "e", now=an_hour_on).status == DRIVER_GONE

    # And a flip in it re-sends the frame with nothing a tick writes having
    # moved. Driven here through the other road to ``gone`` — a dead holder pid,
    # the #970 shape the serve sweep stores — because that one needs no wait.
    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=False,
        heartbeat_at=started,
        observed_at=datetime.now(UTC).isoformat(),
    )
    lapsed, frames = fetch_epic_frames(store, "e", settled)
    assert lapsed["hb"][-1] == DRIVER_GONE
    assert lapsed["hb"][:-1] == settled["hb"][:-1]
    assert dict(frames)["heartbeat"]["driver_status"] == DRIVER_GONE


def test_list_epics_reads_an_epic_with_no_driver_row_as_unknown(store: Any) -> None:
    """``unknown`` is not ``not gone``: an epic with journal rows and no driver
    row has no heartbeat to threshold, and the summary must say so rather than
    let a strip spend cyan on it."""
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(),
        actions=_actions(),
        snapshot_hash="h1",
    )
    assert list_epics(store)[0].driver_status == DRIVER_UNKNOWN


def test_fetch_epic_frames_caps_snapshot_frames_but_cursor_advances_past_all(
    store: Any,
) -> None:
    """_MAX_SNAPSHOT_FRAMES (50) caps how many snapshot frames one poll
    emits when a subscriber falls behind, but the cursor must still
    advance past every new journal row -- not just the emitted ones --
    or the next poll would re-emit the older, already-skipped rows."""
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("101")]),
        actions=_actions(),
        snapshot_hash="h0",
    )
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:00:05+00:00",
        duration_s=5.0,
        interval_s=30.0,
        signal="progressing",
        tick_id=1,
    )
    # Establish a baseline cursor synced past the first tick/heartbeat/
    # economics, so the next poll is isolated to the snapshot cap alone.
    cursor0, _ = fetch_epic_frames(store, "e", {})

    for i in range(55):
        store.append_epic_tick(
            epic_ref="e",
            tick_ts=f"2026-07-02T00:{i + 1:02d}:00+00:00",
            snapshot=_snapshot(nodes=[_node("101")]),
            actions=_actions(),
            snapshot_hash=f"h{i + 1}",
        )

    cursor1, frames1 = fetch_epic_frames(store, "e", cursor0)

    assert [k for k, _ in frames1] == ["snapshot"] * 50

    cursor2, frames2 = fetch_epic_frames(store, "e", cursor1)

    assert frames2 == []
    assert cursor2 == cursor1


def test_hostile_node_id_does_not_crash(store: Any) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("other-repo#5", external=True)]),
        actions=_actions(),
        snapshot_hash="h1",
    )

    detail = get_node_detail(store, "e", "other-repo#5")

    assert detail is not None
    assert detail.runs == []
    assert detail.lifetime_usd == 0.0
    assert detail.drain_usd == 0.0

    assert get_node_detail(store, "no-such-epic", "1") is None


def _seed_heartbeat(
    store: Any, epic_ref: str, *, started_at: str, interval_s: float | None
) -> None:
    """Seed one epic_driver_state row via the two-phase tick commit."""
    tick_id, _ = store.append_epic_tick(
        epic_ref=epic_ref,
        tick_ts=started_at,
        snapshot=_snapshot(),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.begin_epic_tick(epic_ref, started_at)
    store.finish_epic_tick(
        epic_ref,
        tick_ts=started_at,
        duration_s=1.0,
        interval_s=interval_s,
        signal="progressing",
        tick_id=tick_id,
    )


def _seed_expectation(
    store: Any, epic_ref: str, *, started_at: str, expected: float
) -> None:
    """Seed one tick that has NO cadence of its own, only the configured
    expectation a one-shot ``kew epic advance`` records (#2297)."""
    tick_id, _ = store.append_epic_tick(
        epic_ref=epic_ref,
        tick_ts=started_at,
        snapshot=_snapshot(),
        actions=_actions(),
        snapshot_hash="h1",
    )
    store.begin_epic_tick(epic_ref, started_at)
    store.finish_epic_tick(
        epic_ref,
        tick_ts=started_at,
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=expected,
        signal="progressing",
        tick_id=tick_id,
    )


def test_driver_liveness_absent_when_no_state(store: Any) -> None:
    live = driver_liveness(store, "nope", now=datetime(2026, 7, 16, tzinfo=UTC))
    assert live.present is False
    assert live.driver_gone is False
    assert live.age_s is None


def test_driver_gone_past_two_intervals(store: Any) -> None:
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=30.0)
    # 61s of silence > 2 * 30s interval => driver-gone.
    now = datetime(2026, 7, 16, 0, 1, 1, tzinfo=UTC)
    live = driver_liveness(store, "e", now=now)
    assert live.present is True
    assert live.interval_s == 30.0
    assert live.age_s == 61.0
    assert live.driver_gone is True


def test_driver_alive_within_two_intervals(store: Any) -> None:
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=30.0)
    # Exactly 2 intervals of silence is still alive (strictly-greater threshold).
    now = datetime(2026, 7, 16, 0, 1, 0, tzinfo=UTC)
    live = driver_liveness(store, "e", now=now)
    assert live.age_s == 60.0
    assert live.driver_gone is False


def test_driver_liveness_unknown_when_interval_nonpositive(store: Any) -> None:
    # interval_s <= 0 is unthresholdable (age > 2*0 would flag every live
    # driver as gone) — fail safe like a missing interval.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=0.0)
    now = datetime(2026, 7, 16, 1, 0, 0, tzinfo=UTC)
    live = driver_liveness(store, "e", now=now)
    assert live.present is True
    assert live.status == "unknown"
    assert live.cadence_source == "none"
    assert live.driver_gone is False


def test_driver_liveness_unknown_when_interval_missing(store: Any) -> None:
    # One-shot run with no recorded cadence and no expectation passed in: the
    # heartbeat's age is reported, but nothing can declare the driver gone.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=None)
    now = datetime(2026, 7, 16, 1, 0, 0, tzinfo=UTC)
    live = driver_liveness(store, "e", now=now)
    assert live.present is True
    assert live.interval_s is None
    assert live.age_s == 3600.0
    assert live.status == "unknown"
    assert live.driver_gone is False


# --- I02: opaque epic-node ids, validated by bounds and exact membership ----


def _seed_members(store: Any, *node_ids: str) -> None:
    _seed_members_with(store, *(_node(n) for n in node_ids))


def _seed_members_with(store: Any, *nodes: dict[str, Any]) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=list(nodes)),
        actions=_actions(),
        snapshot_hash="h-members",
    )


def test_epic_member_ids_reads_the_latest_snapshot(store: Any) -> None:
    _seed_members(store, "101", "SFD-12")

    assert epic_member_ids(store, "e") == {"101", "SFD-12"}
    assert epic_member_ids(store, "no-such-epic") == set()


def test_opaque_member_id_is_accepted_by_membership_not_by_digits(
    store: Any,
) -> None:
    """SFD-12 is a member; it must not be rejected merely for being nonnumeric."""
    _seed_members(store, "SFD-12")

    assert is_epic_member(store, "e", "SFD-12")
    detail = get_node_detail(store, "e", "SFD-12")
    assert detail is not None
    assert detail.id == "SFD-12"


def test_punctuation_member_id_is_accepted_by_exact_membership(store: Any) -> None:
    _seed_members(store, "TEAM:12")

    assert is_epic_member(store, "e", "TEAM:12")
    detail = get_node_detail(store, "e", "TEAM:12")
    assert detail is not None
    assert detail.id == "TEAM:12"


def test_nonmember_id_is_not_found_even_though_it_is_well_formed(
    store: Any,
) -> None:
    """A charset-valid id from another epic is not this epic's node. Returning
    an empty detail for it would fabricate a node that does not exist here."""
    _seed_members(store, "SFD-12")

    assert not is_epic_member(store, "e", "SFD-99")
    assert get_node_detail(store, "e", "SFD-99") is None
    assert get_node_detail(store, "e", "101") is None


@pytest.mark.parametrize(
    "node_id",
    [
        "",
        " ",
        "SFD-12\n",
        "../x",
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "%2e%2e",
        "x" * 65,
        "x" * 300,
    ],
)
def test_malformed_and_path_shaped_node_ids_are_rejected_by_bounds(
    node_id: str,
) -> None:
    assert not valid_node_id(node_id)


@pytest.mark.parametrize(
    "node_id",
    ["101", "SFD-12", "TEAM:12", "other-repo#5", "a.b_c-1", "SFD 12", "-1", ".hidden"],
)
def test_bounded_opaque_node_ids_are_accepted(node_id: str) -> None:
    assert valid_node_id(node_id)


def test_malformed_node_id_never_reaches_a_membership_read(store: Any) -> None:
    """Bounds are checked first, so a path-shaped id cannot become a lookup key."""
    _seed_members(store, "101")

    assert get_node_detail(store, "e", "../../etc/passwd") is None
    assert get_node_detail(store, "e", "x" * 65) is None


# --------------------------------------------------------------------------- #
# FUI-10 — needs-you (attention) and blocked reason on epic nodes
# --------------------------------------------------------------------------- #


def _config(repo: str = "acme/widgets") -> Any:
    return DispatchConfig(project=ProjectConfig(repo=repo))


def _awaiting_decision(store: Any, item_id: str = "2055") -> None:
    """One member node whose artifact is an open PR awaiting a human."""
    _seed_members(store, item_id)
    store.save(
        RunRecord(
            run_id="r-2055",
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
            pr_url="https://example.invalid/pr/2059",
            pr_state="OPEN",
        )
    )


class _BrokenReader:
    """A real store with ONE read replaced by a failure, nothing else.

    ``only_event`` narrows the failure further, to the calls asking for one
    audit ``event=``. ``list_audit_events`` is the ledger read behind several
    unrelated facts, and a test about one of them wants the others answering
    normally — otherwise it passes on whichever read happens to raise first.
    """

    def __init__(
        self,
        store: Any,
        method: str,
        only_item: str | None = None,
        *,
        only_event: str | None = None,
    ) -> None:
        self._store = store
        self._method = method
        self._only_item = only_item
        self._only_event = only_event

    def __getattr__(self, name: str) -> Any:
        inner = getattr(self._store, name)
        if name != self._method:
            return inner

        def _raise(*args: Any, **kwargs: Any) -> Any:
            if self._only_item is not None and self._only_item not in args:
                return inner(*args, **kwargs)
            if self._only_event is not None and kwargs.get("event") != self._only_event:
                return inner(*args, **kwargs)
            raise RuntimeError("reader down")

        return _raise


def test_epic_node_awaiting_a_human_decision_carries_a_merge_decision(
    store: Any,
) -> None:
    """The needs-you join names the artifact to decide on, with its url."""
    _awaiting_decision(store)

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    node = detail.snapshot.nodes[0]
    assert node.attention is not None
    assert node.attention.kind == "merge_decision"
    assert node.attention.url == "https://example.invalid/pr/2059"
    assert node.attention.reason == "PR open for #2055 — awaiting review"
    # A clean attention=None elsewhere is only trustworthy while this is empty.
    assert detail.snapshot.unavailable == []


def test_epic_node_with_nothing_waiting_on_a_person_has_no_attention(
    store: Any,
) -> None:
    _seed_members(store, "101")

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == []


def test_the_dag_and_the_judgment_queue_say_the_same_sentence(
    store: Any, monkeypatch: Any
) -> None:
    """One rule, read twice: the node's attention reason IS the console row's."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])
    _awaiting_decision(store)

    detail = get_epic_detail(store, "e", config=_config())
    rows = dashboard.assemble(store, _config()).rows

    assert detail is not None and detail.snapshot is not None
    node = detail.snapshot.nodes[0]
    row = next(r for r in rows if r.type == "pr_review")
    assert node.attention is not None
    assert node.attention.reason == row.reason
    assert node.attention.url == row.url


def test_an_unreadable_projection_is_named_never_a_clean_no_one_is_waiting(
    store: Any,
) -> None:
    """The silence-on-failure rule: attention=None only alongside a reason."""
    _awaiting_decision(store)

    detail = get_epic_detail(
        _BrokenReader(store, "list_syncable_runs"), "e", config=_config()
    )

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == ["attention"]


def test_one_unreadable_item_is_named_and_the_rest_still_join(store: Any) -> None:
    _awaiting_decision(store)

    detail = get_epic_detail(
        _BrokenReader(store, "list_by_item", only_item="2055"), "e", config=_config()
    )

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == ["attention:2055"]


def test_an_unreadable_hold_reader_is_named_too(store: Any) -> None:
    """An unreadable audit ledger degrades the route and names what it cost.

    ``list_audit_events`` is where the open approval holds live AND where the
    archive decision that decides whether this epic is retired lives (#2206),
    so one broken reader takes both. The archive read is the one that is NAMED,
    because it is the one that decided the answer: it degrades to retired, and
    a retired epic asks nobody for anything, so no later source is consulted at
    all. Naming the approval read as well would claim a read that never
    happened. Neither may escape as an exception: this is a read route, and a
    500 is not a degraded answer.
    """
    _seed_members(store, "101")

    detail = get_epic_detail(
        _BrokenReader(store, "list_audit_events"), "e", config=_config()
    )

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert sorted(detail.snapshot.unavailable) == ["attention:epic_archive"]


def test_an_unreadable_ledger_drops_the_journaled_waits_and_says_so(
    store: Any,
) -> None:
    """The retirement gate is fail-soft: it degrades the route, never 500s it.

    Whether an epic is retired (#2206) is an audit fact and nothing else, so the
    gate on its journaled waits is a LEDGER read — and a read route may not
    raise out of one. It degrades the way every other source here does: the
    waits it could not place drop out, and the read is NAMED, because guessing
    "nothing is archived" would silently resurrect a retired epic's diamonds.
    """
    _seed_members_with(store, _node("2061", status="awaiting_continuation"))

    detail = get_epic_detail(
        _BrokenReader(store, "list_audit_events"), "e", config=_config()
    )

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert "attention:epic_archive" in detail.snapshot.unavailable
    # And with the ledger readable, the same node IS a wait — so the assertion
    # above is the degraded reading, not a node nobody was ever waiting on.
    live = get_epic_detail(store, "e", config=_config())
    assert live is not None and live.snapshot is not None
    assert live.snapshot.nodes[0].attention is not None
    assert live.snapshot.unavailable == []


def test_a_complete_epic_s_stream_drops_the_waits_its_detail_read_drops(
    store: Any,
) -> None:
    """A finished epic's live frames agree with its detail read (#2207).

    ``complete`` retires an epic from the active views exactly as an operator's
    archive decision does, and the gate reads the snapshot's own signal for it.
    The stream has to apply the same gate: serve's watch hub replaces a tab's
    whole snapshot with a streamed one, so an ungated frame would re-light every
    node the detail route just dropped and win.
    """
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(
            signal="complete", nodes=[_node("2062", status="awaiting_continuation")]
        ),
        actions=_actions(),
        snapshot_hash="h-complete",
    )

    detail = get_epic_detail(store, "e", config=_config())
    _cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")
    assert snapshot_frame["snapshot"] == detail.snapshot.model_dump()
    assert snapshot_frame["snapshot"]["unavailable"] == []


def test_a_streamed_frame_survives_an_unreadable_ledger(store: Any) -> None:
    """The stream's gate is fail-soft too — a poll degrades, it does not raise.

    A live subscriber polls this every second, and an exception here does not
    degrade one frame: it kills the poll loop the whole view is drawn from.
    """
    _seed_members_with(store, _node("2063", status="awaiting_continuation"))

    _cursor, frames = fetch_epic_frames(
        _BrokenReader(store, "list_audit_events", only_event=EPIC_ARCHIVED_EVENT),
        "e",
        {},
        config=_config(),
    )

    snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")
    assert all(n["attention"] is None for n in snapshot_frame["snapshot"]["nodes"])
    assert "attention:epic_archive" in snapshot_frame["snapshot"]["unavailable"]


def test_an_open_approval_hold_is_the_node_attention(store: Any) -> None:
    _seed_members(store, "77")
    store.append_audit_event(
        timestamp="2026-09-24T10:00:00+00:00",
        event="approval_hold",
        issue_num=77,
        payload={"estimate_usd": 12.0, "basis": "estimate"},
    )

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    attention = detail.snapshot.nodes[0].attention
    assert attention is not None
    assert attention.kind == "approval"
    assert attention.reason == "awaiting approval — estimate"
    assert attention.url is None


def test_without_a_config_the_snapshot_says_the_join_did_not_happen(
    store: Any,
) -> None:
    """No config is not an epic where nobody is waiting — it is no reading."""
    _awaiting_decision(store)

    detail = get_epic_detail(store, "e")

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == ["attention"]


def test_a_journaled_tick_never_claims_a_live_needs_you_reading(store: Any) -> None:
    """History is a record of the DAG, not of who needs a person now."""
    _awaiting_decision(store)

    ticks = list_ticks(store, "e")

    assert [t.snapshot.unavailable for t in ticks] == [["attention"]]
    assert all(n.attention is None for t in ticks for n in t.snapshot.nodes)


def test_a_streamed_frame_carries_the_same_needs_you_join_as_the_detail_read(
    store: Any,
) -> None:
    """Live replaces the detail read's whole snapshot: the frame for tick N
    must say exactly what GET /api/epics/{ref} says about tick N, or a
    ticking epic would contradict the judgment queue it just agreed with."""
    _awaiting_decision(store)

    detail = get_epic_detail(store, "e", config=_config())
    _cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())

    assert detail is not None and detail.snapshot is not None
    snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")
    assert snapshot_frame["snapshot"] == detail.snapshot.model_dump()
    assert snapshot_frame["snapshot"]["unavailable"] == []
    node = snapshot_frame["snapshot"]["nodes"][0]
    assert node["attention"]["kind"] == "merge_decision"
    assert node["attention"]["reason"] == "PR open for #2055 — awaiting review"


def test_every_frame_of_one_poll_carries_the_join(store: Any) -> None:
    """One join per poll, applied to every frame it emits — a batch must not
    hand the view an annotated frame followed by a blanked one."""
    _awaiting_decision(store)
    for i in range(3):
        store.append_epic_tick(
            epic_ref="e",
            tick_ts=f"2026-07-02T00:0{i + 1}:00+00:00",
            snapshot=_snapshot(nodes=[_node("2055")]),
            actions=_actions(),
            snapshot_hash=f"h-live-{i}",
        )

    _cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())

    snapshots = [payload for kind, payload in frames if kind == "snapshot"]
    assert len(snapshots) == 4
    assert all(f["snapshot"]["unavailable"] == [] for f in snapshots)
    assert all(
        n["attention"] is not None for f in snapshots for n in f["snapshot"]["nodes"]
    )


def test_a_streamed_frame_without_a_config_names_the_join_unavailable(
    store: Any,
) -> None:
    """No config is still no reading — never a live epic where nobody waits."""
    _awaiting_decision(store)

    _cursor, frames = fetch_epic_frames(store, "e", {})

    snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")
    assert snapshot_frame["snapshot"]["unavailable"] == ["attention"]
    assert all(n["attention"] is None for n in snapshot_frame["snapshot"]["nodes"])


def test_an_unreadable_projection_is_named_on_a_streamed_frame_too(
    store: Any,
) -> None:
    _awaiting_decision(store)

    _cursor, frames = fetch_epic_frames(
        _BrokenReader(store, "list_syncable_runs"), "e", {}, config=_config()
    )

    snapshot_frame = next(payload for kind, payload in frames if kind == "snapshot")
    assert snapshot_frame["snapshot"]["unavailable"] == ["attention"]
    assert all(n["attention"] is None for n in snapshot_frame["snapshot"]["nodes"])


def test_blocked_node_reports_the_reason_the_driver_recorded(store: Any) -> None:
    _seed_members_with(
        store, _node("2053", status="blocked", stuck="needs a design decision")
    )

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].blocked_reason == "needs a design decision"


def test_blocked_node_with_nothing_recorded_invents_no_reason(store: Any) -> None:
    _seed_members_with(store, _node("2053", status="blocked", stuck=None))

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].blocked_reason is None


def test_a_waiting_node_is_not_a_blocked_node(store: Any) -> None:
    """The driver records a reason for waiting nodes too; that is not a block."""
    _seed_members_with(store, _node("2054", status="todo", stuck="waiting on 2053"))

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    node = detail.snapshot.nodes[0]
    assert node.blocked_reason is None
    assert node.stuck == "waiting on 2053"  # the raw record is still carried


def test_a_terminal_node_reports_its_blocking_reason(store: Any) -> None:
    """FAILED/CANCELLED are terminal-but-blocking: the driver records their
    blast radius under `stuck` precisely so an operator sees what a dead node
    holds up (tick.py::_terminal_ds_stuck)."""
    _seed_members_with(
        store,
        _node("2053", status="failed", stuck="failed-terminal (exit 1, timeout)"),
        _node("2054", status="cancelled", stuck="cancelled"),
    )

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    reasons = {n.id: n.blocked_reason for n in detail.snapshot.nodes}
    assert reasons == {
        "2053": "failed-terminal (exit 1, timeout)",
        "2054": "cancelled",
    }


def test_a_held_node_is_a_wait_not_a_block(store: Any) -> None:
    """A hold's driver-recorded reason is a wait ("the merge pass closes the
    issue on this tick"), and rendering it as a block would invent a cause."""
    _seed_members_with(
        store,
        _node(
            "2055",
            status="held",
            stuck="merged_unclosed — the merge pass closes the issue on this tick",
        ),
    )

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    node = detail.snapshot.nodes[0]
    assert node.blocked_reason is None
    assert node.stuck is not None  # the raw record is still carried


# --------------------------------------------------------------------------- #
# The live needs-you join refreshes without a new journal row
# --------------------------------------------------------------------------- #


def _merge_the_pr(store: Any, item_id: str = "2055") -> None:
    """What a non-driver actor (serve's own SweepWorker) does to a run row when
    it observes the PR merged — no epic tick is journaled by that path."""
    store.save(
        RunRecord(
            run_id=f"r-{item_id}",
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
            pr_url="https://example.invalid/pr/2059",
            pr_state="MERGED",
        )
    )


def test_attention_change_without_a_new_tick_emits_an_attention_frame(
    store: Any,
) -> None:
    """An attention change driven by a non-driver actor must reach a live
    subscriber. Waiting for the next journal row would leave the diamond lit
    over work nobody is waiting on any more."""
    _awaiting_decision(store)
    cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())
    first = dict(next(p for k, p in frames if k == "attention")["by_node"])
    assert first["2055"]["kind"] == "merge_decision"

    _merge_the_pr(store)
    cursor2, frames2 = fetch_epic_frames(store, "e", cursor, config=_config())

    assert [k for k, _ in frames2] == ["attention"]
    assert next(p for k, p in frames2 if k == "attention") == {
        "by_node": {},
        "unavailable": [],
    }
    # And the cursor holds, so the next quiet poll says nothing at all.
    assert fetch_epic_frames(store, "e", cursor2, config=_config())[1] == []


def test_an_unchanged_join_re_emits_nothing(store: Any) -> None:
    """The attention frame is change-driven like economics — a quiet poll on a
    quiet epic stays silent."""
    _awaiting_decision(store)
    cursor, _frames = fetch_epic_frames(store, "e", {}, config=_config())

    cursor2, frames2 = fetch_epic_frames(store, "e", cursor, config=_config())

    assert frames2 == []
    assert cursor2 == cursor


def test_a_newly_unreadable_projection_is_streamed_as_unavailable(
    store: Any,
) -> None:
    """Silence-on-failure travels over the stream too: a join that STOPS being
    readable must replace a live subscriber's annotations with "we cannot
    tell", never leave the last good reading standing as if it were current."""
    _awaiting_decision(store)
    cursor, _frames = fetch_epic_frames(store, "e", {}, config=_config())

    _cursor2, frames2 = fetch_epic_frames(
        _BrokenReader(store, "list_syncable_runs"), "e", cursor, config=_config()
    )

    assert next(p for k, p in frames2 if k == "attention") == {
        "by_node": {},
        "unavailable": ["attention"],
    }


def test_the_attention_frame_agrees_with_the_snapshot_frame_beside_it(
    store: Any,
) -> None:
    """One join per poll: a poll that emits both must not tell two stories,
    and the snapshot must come first so a replayed pair settles on the join."""
    _awaiting_decision(store)

    _cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())

    kinds = [k for k, _ in frames]
    assert kinds.index("snapshot") < kinds.index("attention")
    snapshot_frame = next(p for k, p in frames if k == "snapshot")
    attention_frame = next(p for k, p in frames if k == "attention")
    node = snapshot_frame["snapshot"]["nodes"][0]
    assert attention_frame["by_node"][node["id"]] == node["attention"]
    assert attention_frame["unavailable"] == snapshot_frame["snapshot"]["unavailable"]


# --------------------------------------------------------------------------- #
# A re-subscriber's replay settles on the current needs-you join
# --------------------------------------------------------------------------- #


def _applied_attention(frames: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """The needs-you each node shows after a fresh viewer applies *frames*.

    Mirrors frontend/src/lib/epic/live.ts applyFrame for a viewer holding no
    snapshot yet: a snapshot frame replaces the view with its embedded join,
    an attention frame re-annotates the view on screen (and is dropped while
    there is none)."""
    view: dict[str, Any] | None = None
    for kind, payload in frames:
        if kind == "snapshot":
            view = {n["id"]: n["attention"] for n in payload["snapshot"]["nodes"]}
        elif kind == "attention" and view is not None:
            view = {node_id: payload["by_node"].get(node_id) for node_id in view}
    assert view is not None, "the replay carried no snapshot"
    return view


async def _replay_after_polls(
    polls: list[tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Run *polls* through a real EpicWatchHub poller, then re-subscribe and
    return what the new subscriber is replayed."""
    calls = 0

    def fetch(_cursor: dict[str, Any]) -> Any:
        nonlocal calls
        result = polls[min(calls, len(polls) - 1)]
        if calls >= len(polls):
            result = (result[0], [])
        calls += 1
        return result

    hub = EpicWatchHub(lambda _ref: fetch, poll_s=0.001)
    q, _ = hub.subscribe("e")

    async def _drained() -> None:
        while calls <= len(polls):
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_drained(), timeout=5.0)
    _q2, replay = hub.subscribe("e")
    hub.unsubscribe("e", _q2)
    hub.unsubscribe("e", q)
    return replay


@pytest.mark.asyncio
async def test_a_replay_on_a_tickless_epic_shows_the_current_needs_you(
    store: Any,
) -> None:
    """The poller's first poll on an epic with no journaled tick emits an
    attention frame and no snapshot, so attention is recorded before snapshot.
    A later tick then a PR merge (attention-only) must replay to "nobody
    waiting", not to the older join the snapshot frame embeds."""
    polls = []
    cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())
    assert [k for k, _ in frames if k in ("snapshot", "attention")] == ["attention"]
    polls.append((cursor, frames))

    _awaiting_decision(store)
    cursor, frames = fetch_epic_frames(store, "e", cursor, config=_config())
    assert {"snapshot", "attention"} <= {k for k, _ in frames}
    polls.append((cursor, frames))

    _merge_the_pr(store)
    cursor, frames = fetch_epic_frames(store, "e", cursor, config=_config())
    assert [k for k, _ in frames] == ["attention"]
    polls.append((cursor, frames))

    replay = await _replay_after_polls(polls)

    assert _applied_attention(replay) == {"2055": None}


@pytest.mark.asyncio
async def test_a_replay_on_a_ticking_epic_shows_the_current_needs_you(
    store: Any,
) -> None:
    """The ordinary order — the first poll already emits snapshot then
    attention — still replays to the newest join, both while someone is
    waiting and after the wait clears."""
    _awaiting_decision(store)
    cursor, frames = fetch_epic_frames(store, "e", {}, config=_config())
    first = [(cursor, frames)]

    replay = await _replay_after_polls(first)
    applied = _applied_attention(replay)
    assert applied["2055"] is not None
    assert applied["2055"]["kind"] == "merge_decision"

    _merge_the_pr(store)
    cursor, frames = fetch_epic_frames(store, "e", cursor, config=_config())

    replay = await _replay_after_polls([*first, (cursor, frames)])

    assert _applied_attention(replay) == {"2055": None}


# --------------------------------------------------------------------------- #
# The join is scoped to the epic, not to the whole store
# --------------------------------------------------------------------------- #


class _ItemReadRecorder:
    """A real store that records what the join asked it to read.

    ``list_by_item`` is the per-item read every projected view costs,
    ``list_syncable_runs`` is the one bulk read before them, and
    ``list_audit_events`` is the approval-hold read beside them, so between them
    they ARE the join's scope — recorded as ids, not as elapsed time, so the
    guard cannot flake on a slow machine.

    ``merge_scopes`` records the other audit read on the same poll: the
    ``epic_merge`` attestations the ledger reconciliation weighs (#2392).
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self.items_read: list[str] = []
        self.syncable_scopes: list[list[str] | None] = []
        self.approval_scopes: list[list[int] | None] = []
        self.merge_scopes: list[list[str] | None] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def list_by_item(self, item_id: str) -> Any:
        self.items_read.append(item_id)
        return self._store.list_by_item(item_id)

    def list_syncable_runs(self, *, item_ids: Any = None) -> Any:
        self.syncable_scopes.append(None if item_ids is None else list(item_ids))
        return self._store.list_syncable_runs(item_ids=item_ids)

    def list_audit_events(self, limit: Any = 100, **filters: Any) -> Any:
        if filters.get("event_prefix") == "approval_":
            scope = filters.get("issue_nums")
            self.approval_scopes.append(None if scope is None else list(scope))
        if filters.get("event") == "epic_merge":
            ids = filters.get("item_ids")
            self.merge_scopes.append(None if ids is None else list(ids))
        return self._store.list_audit_events(limit, **filters)


def _unrelated_open_prs(store: Any, count: int) -> list[str]:
    """Syncable runs with open PRs that belong to no epic on this host."""
    item_ids = [str(9000 + n) for n in range(count)]
    for item_id in item_ids:
        store.save(
            RunRecord(
                run_id=f"r-{item_id}",
                issue_num=int(item_id),
                item_id=item_id,
                branch=f"agent/issue-{item_id}",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
                finished_at=datetime(2026, 9, 24, 10, 30, tzinfo=UTC),
                pr_url=f"https://example.invalid/pr/{item_id}",
                pr_state="OPEN",
            )
        )
    return item_ids


def test_the_epic_join_reads_work_facts_only_for_the_epic_s_nodes(
    store: Any,
) -> None:
    """A 1s poll on a 3-node epic must not compose the whole store's work.

    The join is composed on EVERY poll, so its cost scales with whatever it is
    scoped to: unscoped, a host with thousands of open PRs spent most of the
    poll budget building views that the node filter then discarded (#2096).
    """
    _awaiting_decision(store)
    unrelated = _unrelated_open_prs(store, 50)
    recorder = _ItemReadRecorder(store)

    _cursor, frames = fetch_epic_frames(recorder, "e", {}, config=_config())

    assert not set(recorder.items_read) & set(unrelated)
    assert recorder.syncable_scopes == [["2055"]]
    # ...and scoping did not cost the epic its own reading.
    attention_frame = next(p for k, p in frames if k == "attention")
    assert attention_frame["by_node"]["2055"]["kind"] == "merge_decision"


def test_a_quiet_poll_reads_merge_attestations_only_for_the_epic_s_nodes(
    store: Any,
) -> None:
    """The ledger reconciliation (#2392) runs on every poll, so it is scoped on
    every poll.

    A mid-flight epic has non-terminal nodes by definition, so there is nothing
    to short-circuit: unscoped, each quiet 1s poll of any ticking epic paid an
    `event="epic_merge"`, `limit=None` read of the whole audit ledger — a read
    the base path never made, growing without bound with the host's merge
    history (#2392 review). `idx_audit_events_item_event` serves the scoped one.
    """
    _awaiting_decision(store)
    for issue_num in range(9200, 9250):
        store.append_audit_event(
            timestamp="2026-09-24T10:00:00+00:00",
            event="epic_merge",
            issue_num=issue_num,
            item_id=str(issue_num),
            payload={"trigger": "approval", "actor": "tom"},
        )
    recorder = _ItemReadRecorder(store)
    cursor, _ = fetch_epic_frames(recorder, "e", {}, config=_config())
    recorder.merge_scopes.clear()

    # The quiet poll: the cursor is already past every journaled row.
    _cursor, _frames = fetch_epic_frames(recorder, "e", cursor, config=_config())

    assert recorder.merge_scopes == [["2055"]]


def test_the_epic_join_reads_approval_holds_only_for_the_epic_s_nodes(
    store: Any,
) -> None:
    """Same poll, the other unscoped read: the approval-hold join asked the
    audit log for EVERY approval event on the host, which grows without bound
    as a host accumulates audit rows (#2129). Scope it to the epic's nodes.
    """
    _awaiting_decision(store)
    for issue_num in range(9100, 9150):
        store.append_audit_event(
            timestamp="2026-09-24T10:00:00+00:00",
            event="approval_hold",
            issue_num=issue_num,
            payload={"estimate_usd": 1.0, "basis": "estimate"},
        )
    store.append_audit_event(
        timestamp="2026-09-24T11:00:00+00:00",
        event="approval_hold",
        issue_num=2055,
        payload={"estimate_usd": 12.0, "basis": "estimate"},
    )
    recorder = _ItemReadRecorder(store)

    _cursor, frames = fetch_epic_frames(recorder, "e", {}, config=_config())

    assert recorder.approval_scopes == [[2055]]
    # ...and scoping did not cost the epic its own hold.
    attention_frame = next(p for k, p in frames if k == "attention")
    assert attention_frame["by_node"]["2055"]["kind"] == "approval"


def test_a_node_that_cannot_be_an_issue_number_skips_the_hold_read(
    store: Any,
) -> None:
    """Holds are keyed by issue number, so a non-numeric node id could never
    match one. Asking the audit log anyway is a whole read for a known-empty
    answer — and the answer stays empty either way."""
    _seed_members(store, "ENG-42")
    recorder = _ItemReadRecorder(store)

    detail = get_epic_detail(recorder, "e", config=_config())

    assert recorder.approval_scopes == []
    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == []


def test_a_node_id_too_wide_for_an_integer_is_still_just_no_hold(
    store: Any,
) -> None:
    """A node id may be 64 chars, so an all-digit one can exceed what SQLite
    binds as an integer. That is not an issue number and not an unreadable
    reader either — it drops out with the non-numeric ids."""
    _seed_members(store, "9" * 40)

    detail = get_epic_detail(store, "e", config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.nodes[0].attention is None
    assert detail.snapshot.unavailable == []


def test_the_scoped_join_still_says_the_judgment_queue_s_sentence(
    store: Any, monkeypatch: Any
) -> None:
    """Narrowing is a scope change, not a rule change. With work the epic does
    not own also in the store — the case scoping actually skips — the node's
    reading must still be the console row's, word for word."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])
    _awaiting_decision(store)
    _unrelated_open_prs(store, 5)

    detail = get_epic_detail(store, "e", config=_config())
    rows = dashboard.assemble(store, _config()).rows

    assert detail is not None and detail.snapshot is not None
    node = detail.snapshot.nodes[0]
    row = next(r for r in rows if r.type == "pr_review" and "#2055" in r.reason)
    assert node.attention is not None
    assert node.attention.reason == row.reason
    assert node.attention.url == row.url


# --- epic timeline (#2270) -------------------------------------------------
#
# Where an epic's time went, per node: status bands off the audit chain, runs
# and the merge off `runs`, verdicts off `review_rounds`. Seeded the same way
# as the rest of this module — a real store, no driver import.


def _transition(
    store: Any, node_id: str, *, frm: str, to: str, at: str, epic_ref: str = "e"
) -> None:
    store.append_audit_event(
        timestamp=at,
        event=NODE_STATUS_EVENT,
        issue_num=int(node_id) if node_id.isdigit() else None,
        item_id=node_id,
        actor_type="policy",
        actor_id="driver",
        payload={"epic_ref": epic_ref, "from": frm, "to": to},
    )


def _timeline_run(
    store: Any,
    *,
    run_id: str,
    item_id: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    status: str = "success",
    run_kind: str = "dispatch",
    cost: float = 0.0,
    pr_merged_at: str | None = None,
    attempt_number: int = 1,
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id) if item_id.isdigit() else 0,
            item_id=item_id,
            branch=f"issue-{item_id}",
            status=status,
            exit_code=0,
            started_at=started_at,
            finished_at=finished_at,
            estimated_cost_usd=cost,
            run_kind=run_kind,
            pr_merged_at=pr_merged_at,
            attempt_number=attempt_number,
        )
    )


def test_timeline_bands_follow_the_transitions_and_the_last_one_stays_open(
    store: Any,
) -> None:
    """Each transition closes the open band and opens the next; the newest
    band has no end, because the node is in that status still."""
    _seed_members(store, "101")
    _transition(store, "101", frm="todo", to="in_progress", at="2026-07-02T01:00:00Z")
    _transition(store, "101", frm="in_progress", to="held", at="2026-07-02T02:00:00Z")
    _transition(store, "101", frm="held", to="done", at="2026-07-02T03:00:00Z")

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    bands = timeline.nodes[0].bands
    assert [(b.status, b.from_, b.to) for b in bands] == [
        ("in_progress", "2026-07-02T01:00:00+00:00", "2026-07-02T02:00:00+00:00"),
        ("held", "2026-07-02T02:00:00+00:00", "2026-07-02T03:00:00+00:00"),
        ("done", "2026-07-02T03:00:00+00:00", None),
    ]


def test_timeline_orders_nodes_by_first_activity_quiet_ones_last(store: Any) -> None:
    """A Gantt reads top-down in the order the work happened. A node that has
    neither moved nor run has no place on that axis, so it keeps the
    snapshot's order behind the ones that do."""
    _seed_members(store, "101", "102", "103", "104")
    _transition(store, "103", frm="todo", to="in_progress", at="2026-07-02T01:00:00Z")
    _timeline_run(
        store,
        run_id="r-102",
        item_id="102",
        started_at=datetime(2026, 7, 2, 0, 30, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 0, 45, tzinfo=UTC),
    )

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    assert [n.id for n in timeline.nodes] == ["102", "103", "101", "104"]
    assert timeline.span.start == "2026-07-02T00:30:00+00:00"
    assert timeline.span.end == timeline.now


def test_timeline_carries_runs_rounds_and_the_merge(store: Any) -> None:
    _seed_members(store, "101")
    _timeline_run(
        store,
        run_id="r-1",
        item_id="101",
        started_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 1, 30, tzinfo=UTC),
        status="failed",
        cost=0.25,
    )
    _timeline_run(
        store,
        run_id="r-2",
        item_id="101",
        started_at=datetime(2026, 7, 2, 2, tzinfo=UTC),
        finished_at=None,
        status="pending",
        run_kind="adversarial_review",
        pr_merged_at="2026-07-02T02:30:00+00:00",
    )
    _timeline_run(
        store,
        run_id="r-0",
        item_id="101",
        started_at=datetime(2026, 7, 2, 0, 30, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 0, 40, tzinfo=UTC),
        pr_merged_at="2026-07-01T00:00:00+00:00",
    )
    store.record_review_round("e", "101", head_sha="abc1234", verdict="NEEDS_CHANGES")
    store.record_review_round("e", "101", head_sha="abc1234", verdict="READY")

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    node = timeline.nodes[0]
    assert [(r.run_id, r.kind, r.status) for r in node.runs] == [
        ("r-0", "dispatch", "success"),
        ("r-1", "dispatch", "failed"),
        ("r-2", "adversarial_review", "pending"),
    ]
    assert node.runs[1].cost_usd == 0.25
    assert node.runs[2].finished_at is None  # still in flight
    assert [(r.round, r.verdict) for r in node.rounds] == [
        (1, "NEEDS_CHANGES"),
        (2, "READY"),
    ]
    # Newest merge wins; the older PR's stamp is history, not the landing.
    assert node.merged_at == "2026-07-02T02:30:00+00:00"


def test_timeline_names_each_bar_s_attempt_so_a_link_can_select_it(
    store: Any,
) -> None:
    """C3/#2300 — a Gantt bar opens the run page at the attempt it draws.

    A run page given no attempt selects the LATEST one, which is the wrong
    evidence for an earlier bar on the lane; the bar carries its own attempt so
    the link can name it.
    """
    _seed_members(store, "101")
    _timeline_run(
        store,
        run_id="retry-1",
        item_id="101",
        started_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 1, 10, tzinfo=UTC),
        status="failed",
    )
    _timeline_run(
        store,
        run_id="retry-2",
        item_id="101",
        started_at=datetime(2026, 7, 2, 2, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 2, 10, tzinfo=UTC),
        attempt_number=3,
    )

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    assert [(r.run_id, r.attempt_number) for r in timeline.nodes[0].runs] == [
        ("retry-1", 1),
        ("retry-2", 3),
    ]


def test_timeline_plots_the_accepted_verdict_not_the_reviewer_s(store: Any) -> None:
    """The stop rule accepts a late low/medium-only NEEDS_CHANGES round as
    READY_WITH_RESIDUALS (#2151), and that acceptance lands in
    ``accepted_verdict`` while ``verdict`` keeps what the reviewer returned.
    Plotting the raw verdict draws the lane as NEEDS_CHANGES beside its own
    merge marker, and disagrees with the review cell
    ``/api/items/{id}/pipeline`` serves for the same node off
    ``effective_verdict``.
    """
    _seed_members(store, "101")
    round_num = store.record_review_round(
        "e", "101", head_sha="abc1234", verdict="NEEDS_CHANGES"
    )
    assert store.accept_review_round(
        "101",
        round_num=round_num,
        head_sha="abc1234",
        sidecar_digest=None,
        verdict="READY_WITH_RESIDUALS",
    )

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    assert [(r.round, r.verdict) for r in timeline.nodes[0].rounds] == [
        (1, "READY_WITH_RESIDUALS")
    ]


def test_timeline_never_mixes_in_another_epic_s_node(store: Any) -> None:
    """Runs, bands and rounds are all keyed to this epic's own nodes. A
    same-named read on a neighbouring epic would quietly double a lane."""
    _seed_members(store, "101")
    store.append_epic_tick(
        epic_ref="other",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=_snapshot(nodes=[_node("999")]),
        actions=_actions(),
        snapshot_hash="h-other",
    )
    _transition(
        store, "999", frm="todo", to="done", at="2026-07-02T01:00:00Z", epic_ref="other"
    )
    _timeline_run(
        store,
        run_id="r-999",
        item_id="999",
        started_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
        finished_at=datetime(2026, 7, 2, 1, 5, tzinfo=UTC),
    )
    store.record_review_round("other", "999", head_sha="def5678", verdict="READY")

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    assert [n.id for n in timeline.nodes] == ["101"]
    assert timeline.nodes[0].bands == []
    assert timeline.nodes[0].runs == []
    assert timeline.nodes[0].rounds == []
    assert timeline.span.start is None


def test_timeline_leaves_out_external_boundary_nodes(store: Any) -> None:
    _seed_members_with(store, _node("101"), _node("77", external=True))
    _transition(store, "77", frm="todo", to="done", at="2026-07-02T01:00:00Z")

    timeline = get_epic_timeline(store, "e")

    assert timeline is not None
    assert [n.id for n in timeline.nodes] == ["101"]


def test_timeline_is_none_for_an_epic_the_store_never_journaled(store: Any) -> None:
    _seed_members(store, "101")

    assert get_epic_timeline(store, "no-such-epic") is None


def test_timeline_reads_each_table_once_for_the_whole_epic(store: Any) -> None:
    """One query per table, never one per node (#2270). Counted on the live
    connection, so a per-node read reintroduced later fails here."""
    _seed_members(store, "101", "102", "103", "104", "105")
    for node_id in ("101", "102", "103", "104", "105"):
        _transition(
            store, node_id, frm="todo", to="in_progress", at="2026-07-02T01:00:00Z"
        )
        _timeline_run(
            store,
            run_id=f"r-{node_id}",
            item_id=node_id,
            started_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
            finished_at=datetime(2026, 7, 2, 1, 5, tzinfo=UTC),
        )

    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        timeline = get_epic_timeline(store, "e")
    finally:
        store._conn.set_trace_callback(None)

    assert timeline is not None and len(timeline.nodes) == 5
    assert sum("FROM runs" in sql for sql in seen) == 1
    assert sum("FROM review_rounds" in sql for sql in seen) == 1
    assert sum("FROM audit_events" in sql for sql in seen) == 1


# --- #2297: liveness signals that can actually fire -------------------------


def test_driver_liveness_status_is_gone_past_two_cadences(store: Any) -> None:
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=30.0)
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 1, 1, tzinfo=UTC))
    assert live.status == "gone"
    assert live.driver_gone is True
    assert live.cadence_source == "recorded"


def test_driver_liveness_status_unknown_when_no_cadence_recorded(store: Any) -> None:
    # A pre-#2297 row with a NULL interval: nothing to threshold against, so the
    # reading says so instead of reporting a dead driver as "not gone".
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=None)
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 1, 0, 0, tzinfo=UTC))
    assert live.status == "unknown"
    assert live.driver_gone is False
    assert live.cadence_source == "none"


def test_driver_liveness_status_unknown_when_absent(store: Any) -> None:
    live = driver_liveness(store, "nope", now=datetime(2026, 7, 16, tzinfo=UTC))
    assert live.status == "unknown"
    assert live.present is False


def test_driver_liveness_reads_the_lock_files_dead_pid_as_gone(store: Any) -> None:
    # Fresh heartbeat, but the lock file's holder pid is not a live process:
    # the lock is the stronger signal, so the driver reads gone regardless.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=30.0)
    facts = DriverLockFacts(pid=4242, pid_alive=False, heartbeat_at=None)
    live = driver_liveness(
        store, "e", now=datetime(2026, 7, 16, 0, 0, 5, tzinfo=UTC), lock=facts
    )
    assert live.status == "gone"
    assert live.pid == 4242
    assert live.pid_alive is False


def test_driver_lock_facts_from_a_lock_holder_record(store: Any) -> None:
    class _Holder:
        pid = 77
        heartbeat = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)

        def is_alive(self) -> bool:
            return True

    facts = DriverLockFacts.from_holder(_Holder())
    assert facts is not None
    assert (facts.pid, facts.pid_alive) == (77, True)
    assert facts.heartbeat_at == "2026-07-16T00:00:00+00:00"
    assert DriverLockFacts.from_holder(None) is None


def test_driver_liveness_thresholds_the_lock_heartbeat_when_state_has_none(
    store: Any,
) -> None:
    # An epic registered but never finished a tick has no state heartbeat; the
    # lock file's own heartbeat is still a liveness signal. The holder pid is
    # unproven here: a PROVEN-alive holder behind a lapsed heartbeat is `busy`.
    store.register_epic("e")
    facts = DriverLockFacts(
        pid=4242, pid_alive=None, heartbeat_at="2026-07-16T00:00:00+00:00"
    )
    live = driver_liveness(
        store,
        "e",
        now=datetime(2026, 7, 16, 0, 20, 0, tzinfo=UTC),
        lock=facts,
        expected_interval_s=300.0,
    )
    assert live.status == "gone"
    assert live.cadence_source == "expected"


def test_a_lapsed_heartbeat_with_a_live_holder_reads_busy_not_gone(
    store: Any,
) -> None:
    # #2534: the host-contract loop was alive and streaming one long foreground
    # dispatch, so it could not tick — and the console said "driver gone". A
    # holder pid proven alive behind a lapsed heartbeat is a driver at work.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    # 4 minutes: past the 2x-cadence lapse, inside the lock's own stale line.
    now = datetime(2026, 7, 16, 0, 4, 0, tzinfo=UTC)
    stale = "2026-07-16T00:00:00+00:00"
    busy = driver_liveness(store, "e", now=now, lock=DriverLockFacts(4242, True, stale))
    assert busy.status == "busy"
    assert busy.driver_gone is False
    # Neither a dead holder nor an unproven one inherits that benefit.
    for pid_alive in (False, None):
        gone = driver_liveness(
            store, "e", now=now, lock=DriverLockFacts(4242, pid_alive, stale)
        )
        assert gone.status == "gone"
        assert gone.driver_gone is True


def test_busy_ends_where_the_lock_layer_would_reclaim_the_holder(
    store: Any,
) -> None:
    # #2534 review: `is_alive` answers True for a pid it cannot prove dead (a
    # legacy pid 0, another user's pid, a recycled one), so "alive" alone must
    # not keep a long-silent driver `busy` forever. Past the lock layer's stale
    # line the holder is reclaimable, and the reading is `gone` again.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    stale = "2026-07-16T00:00:00+00:00"
    start = datetime(2026, 7, 16, tzinfo=UTC)
    at_line = start + timedelta(seconds=LOCK_HEARTBEAT_STALE_S)
    past_line = start + timedelta(seconds=LOCK_HEARTBEAT_STALE_S + 1)
    six_hours = start + timedelta(hours=6)
    lock = DriverLockFacts(0, True, stale)
    assert driver_liveness(store, "e", now=at_line, lock=lock).status == "busy"
    for now in (past_line, six_hours):
        gone = driver_liveness(store, "e", now=now, lock=lock)
        assert gone.status == "gone"
        assert gone.driver_gone is True


def test_the_busy_bound_is_the_lock_layers_stale_line() -> None:
    from kew.graph.driver.lock import HEARTBEAT_STALE_SECONDS

    assert LOCK_HEARTBEAT_STALE_S == HEARTBEAT_STALE_SECONDS


def test_heartbeat_dto_carries_the_tracker_and_driver_status(store: Any) -> None:
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=30.0)
    store.record_epic_tracker("e", "linear")
    dto = _heartbeat_dto(store, "e")
    assert dto is not None
    assert dto.tracker == "linear"


def test_heartbeat_dto_serves_the_cadence_the_reading_used(store: Any) -> None:
    # An externally-driven epic has no cadence of its own; the DTO serves the
    # expectation the reading thresholded, so a client reads the same number.
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:00:00+00:00", expected=300.0
    )
    dto = _heartbeat_dto(store, "e", now=datetime(2026, 7, 16, 0, 1, tzinfo=UTC))
    assert dto is not None
    assert dto.interval_s == 300.0
    assert dto.cadence_source == "expected"
    assert dto.driver_status == "live"


def test_a_long_tick_with_a_beaten_lock_reads_live_not_gone(store: Any) -> None:
    # The regression the lock keepalive exists for: one tick has been running
    # for 10 minutes (so the tick stamps are old), but the driver has beaten its
    # own lock heartbeat a second ago and its pid is alive. Consulting the lock
    # only when the store had no stamp read this provably-live driver as gone.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    now = datetime(2026, 7, 16, 0, 10, 0, tzinfo=UTC)
    facts = DriverLockFacts(
        pid=4242, pid_alive=True, heartbeat_at="2026-07-16T00:09:59+00:00"
    )
    live = driver_liveness(store, "e", now=now, lock=facts)
    assert live.status == "live"
    assert live.driver_gone is False
    assert live.age_s == 1.0
    assert live.last_heartbeat_at == "2026-07-16T00:09:59+00:00"


def test_the_tick_stamp_still_wins_when_it_is_the_fresher_one(store: Any) -> None:
    # Freshest-wins, not lock-wins: a stale lock record (the driver crashed
    # mid-tick and a later tick re-stamped) must not drag the age backwards.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:09:30+00:00", interval_s=60.0)
    facts = DriverLockFacts(
        pid=4242, pid_alive=True, heartbeat_at="2026-07-16T00:00:00+00:00"
    )
    live = driver_liveness(
        store, "e", now=datetime(2026, 7, 16, 0, 10, 0, tzinfo=UTC), lock=facts
    )
    assert live.last_heartbeat_at == "2026-07-16T00:09:30+00:00"
    assert live.age_s == 30.0
    assert live.status == "live"


def test_a_dead_holder_pid_still_outranks_a_fresh_lock_heartbeat(store: Any) -> None:
    # Freshest-wins changes which stamp is thresholded, never the conclusive
    # evidence: a dead pid is still gone (#970).
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    facts = DriverLockFacts(
        pid=4242, pid_alive=False, heartbeat_at="2026-07-16T00:09:59+00:00"
    )
    live = driver_liveness(
        store, "e", now=datetime(2026, 7, 16, 0, 10, 0, tzinfo=UTC), lock=facts
    )
    assert live.status == "gone"


def test_an_unreadable_stamp_falls_back_to_the_readable_one(store: Any) -> None:
    _seed_heartbeat(store, "e", started_at="not a timestamp", interval_s=60.0)
    facts = DriverLockFacts(
        pid=4242, pid_alive=True, heartbeat_at="2026-07-16T00:09:59+00:00"
    )
    live = driver_liveness(
        store, "e", now=datetime(2026, 7, 16, 0, 10, 0, tzinfo=UTC), lock=facts
    )
    assert live.last_heartbeat_at == "2026-07-16T00:09:59+00:00"
    assert live.status == "live"


def test_a_recorded_expectation_reads_as_expected_not_recorded(store: Any) -> None:
    # The cadence a one-shot advance recorded is a GUESS: it thresholds the
    # heartbeat, but it is never reported as the driver's own cadence, and a
    # caller's live config expectation outranks the stored one.
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:00:00+00:00", expected=300.0
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 1, 0, tzinfo=UTC))
    assert (live.interval_s, live.cadence_source) == (300.0, "expected")
    assert live.status == "live"

    fresher = driver_liveness(
        store,
        "e",
        now=datetime(2026, 7, 16, 0, 1, 0, tzinfo=UTC),
        expected_interval_s=20.0,
    )
    assert (fresher.interval_s, fresher.cadence_source) == (20.0, "expected")
    assert fresher.status == "gone"


def test_a_later_expectation_replaces_the_first_one(store: Any) -> None:
    # The frozen-guess regression: the first weakly-recorded expectation used to
    # win forever, so an epic driven slower than that guess read gone for good
    # and editing the config changed nothing.
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:00:00+00:00", expected=300.0
    )
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:10:00+00:00", expected=3600.0
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 20, 0, tzinfo=UTC))
    assert (live.interval_s, live.cadence_source) == (3600.0, "expected")
    assert live.status == "live"


def test_a_retired_pollers_cadence_gives_way_to_the_expectation(store: Any) -> None:
    # #2297 review: the recorded cadence used to be unclearable, and it always
    # outranks the expectation — so an epic moved off `epic run --interval 60`
    # onto a slow cron read `gone` two minutes after a SUCCESSFUL drive, forever.
    # The cron's own ten-minute gap is what retires that 60s: nothing polled at
    # it while the cron was away (`finish_epic_tick`).
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:10:00+00:00", expected=3600.0
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 10, 30, tzinfo=UTC))
    assert (live.interval_s, live.cadence_source) == (3600.0, "expected")
    assert live.status == "live"

    # A poller's own cadence still wins while it is the one driving — and a
    # cadence-less drive that lands inside an interval the poller is honouring
    # leaves it alone rather than blanking it (test_run_store_epic_ticks).
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:11:00+00:00", interval_s=60.0)
    polled = driver_liveness(
        store, "e", now=datetime(2026, 7, 16, 0, 11, 10, tzinfo=UTC)
    )
    assert (polled.interval_s, polled.cadence_source) == (60.0, "recorded")


# --- #2297 review: one reading, every surface -------------------------------


def test_the_epics_list_serves_the_cadence_the_reading_thresholded(store: Any) -> None:
    """The roster row and the epic page must threshold the SAME number.

    The C3 roster batch composed the list's heartbeat straight off the
    ``epic_driver_state`` columns, so an externally-driven epic — which records
    no cadence of its own, only the configured expectation — was served
    ``interval_s=None``. The browser's own 2x threshold then withdrew the live
    claim and the row printed "no heartbeat" while ``/api/epics/<ref>`` called
    the same driver live (#2297 review).
    """
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:00:00+00:00", expected=300.0
    )
    now = datetime(2026, 7, 16, 0, 1, 0, tzinfo=UTC)

    summary = list_epics(store, now=now)[0]
    detail = get_epic_detail(store, "e", now=now)

    assert summary.interval_s == 300.0
    assert detail is not None and detail.heartbeat is not None
    assert detail.heartbeat.interval_s == summary.interval_s
    assert detail.heartbeat.cadence_source == "expected"
    assert detail.heartbeat.driver_status == "live"
    # The row's own 2x window over the start stamp now agrees with the server.
    assert summary.last_tick_started_at == "2026-07-16T00:00:00+00:00"


def test_the_list_row_and_the_epic_page_agree_a_driver_is_gone(store: Any) -> None:
    # The same composition, pointing the other way: a stale beat against the
    # cadence the server used reads gone on both surfaces, not green on one.
    _seed_expectation(
        store, "e", started_at="2026-07-16T00:00:00+00:00", expected=300.0
    )
    now = datetime(2026, 7, 16, 1, 0, 0, tzinfo=UTC)

    summary = list_epics(store, now=now)[0]
    detail = get_epic_detail(store, "e", now=now)

    assert summary.interval_s == 300.0
    assert detail is not None and detail.heartbeat is not None
    assert detail.heartbeat.driver_status == "gone"


def test_a_serve_side_reading_picks_up_the_stored_lock_snapshot(store: Any) -> None:
    """#2297 review: every serve-side reading called ``driver_liveness`` with no
    lock facts at all, so a driver provably alive mid-long-tick (its lock beaten
    a second ago) read "gone" on the console while ``kew epic status``, which can
    open the lock file, read the same driver "live".

    No serve-side caller can read that file — ``kew.serve``/``kew.service`` are
    both forbidden from importing ``kew.graph`` — so the sweep snapshots it onto
    the driver row and the reading picks it up with no caller help.
    """
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    now = datetime(2026, 7, 16, 0, 10, 0, tzinfo=UTC)

    # Before the snapshot: a ten-minute tick on a 60s cadence reads gone.
    assert driver_liveness(store, "e", now=now).status == "gone"

    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=True,
        heartbeat_at="2026-07-16T00:09:59+00:00",
        observed_at="2026-07-16T00:09:59+00:00",
    )

    live = driver_liveness(store, "e", now=now)
    assert live.status == "live"
    assert live.age_s == 1.0
    assert live.pid == 4242
    # And every surface that reads through this function, not just the CLI.
    detail = get_epic_detail(store, "e", now=now)
    assert detail is not None and detail.heartbeat is not None
    assert detail.heartbeat.driver_status == "live"
    assert list_epics(store, now=now)[0].interval_s == 60.0


def test_a_dead_holder_pid_in_the_snapshot_reads_gone(store: Any) -> None:
    # The #970 shape as the sweep sees it: the lock is still held by a pid that
    # is no longer a process. That is conclusive, exactly as it is for the CLI.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:09:50+00:00", interval_s=60.0)
    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=False,
        heartbeat_at="2026-07-16T00:09:55+00:00",
        observed_at="2026-07-16T00:09:59+00:00",
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 10, tzinfo=UTC))
    assert live.status == "gone"
    assert live.driver_gone is True


def test_an_expired_lock_snapshot_settles_nothing(store: Any) -> None:
    # A snapshot is only evidence while the sweep that took it is still running:
    # believed forever, an hour-old "pid alive" would paint a dead driver green
    # and an hour-old "pid dead" would bury a driver that has since restarted.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:59:50+00:00", interval_s=60.0)
    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=False,
        heartbeat_at="2026-07-16T00:00:00+00:00",
        observed_at="2026-07-16T00:00:00+00:00",
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 1, 0, tzinfo=UTC))
    # Thresholded on the fresh tick stamp alone, and the stale dead-pid claim
    # neither flips the verdict nor is reported as a reading.
    assert live.status == "live"
    assert live.pid_alive is None


def test_a_dead_pid_observed_before_the_last_beat_is_not_conclusive(store: Any) -> None:
    # A driver restarted between the sweep's look and now: the pid the sweep
    # found dead has been replaced, and something has beaten since. Honoring it
    # would report a ticking driver gone for up to one sweep cycle.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:02:00+00:00", interval_s=60.0)
    store.record_epic_driver_lock(
        "e",
        pid=4242,
        pid_alive=False,
        heartbeat_at=None,
        observed_at="2026-07-16T00:01:00+00:00",
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 2, 30, tzinfo=UTC))
    assert live.status == "live"


def test_a_free_lock_observation_is_evidence_of_nothing(store: Any) -> None:
    # Between ticks the lock IS free — `epic run` acquires it per tick — so an
    # all-None observation must not read as a dead driver.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:09:30+00:00", interval_s=60.0)
    store.record_epic_driver_lock(
        "e",
        pid=None,
        pid_alive=None,
        heartbeat_at=None,
        observed_at="2026-07-16T00:09:59+00:00",
    )
    live = driver_liveness(store, "e", now=datetime(2026, 7, 16, 0, 10, tzinfo=UTC))
    assert live.status == "live"
    assert (live.pid, live.pid_alive) == (None, None)


def test_a_caller_read_lock_still_outranks_the_stored_snapshot(store: Any) -> None:
    # `kew epic status` reads the file live; that read wins over any snapshot.
    _seed_heartbeat(store, "e", started_at="2026-07-16T00:00:00+00:00", interval_s=60.0)
    store.record_epic_driver_lock(
        "e",
        pid=1,
        pid_alive=True,
        heartbeat_at="2026-07-16T00:09:59+00:00",
        observed_at="2026-07-16T00:09:59+00:00",
    )
    live = driver_liveness(
        store,
        "e",
        now=datetime(2026, 7, 16, 0, 10, tzinfo=UTC),
        lock=DriverLockFacts(pid=4242, pid_alive=False, heartbeat_at=None),
    )
    assert live.status == "gone"
    assert live.pid == 4242


def _attempt(
    store: Any,
    epic_ref: str,
    started_at: str,
    *,
    finished_at: str | None = None,
    outcome: str = "nothing",
    journal_tick_id: int | None = None,
    journaled: bool = False,
) -> int:
    """One tick ATTEMPT row, closed when ``finished_at`` is given."""
    row = store.begin_epic_tick_log(epic_ref, started_at)
    if finished_at is not None:
        store.finish_epic_tick_log(
            row,
            ts_finished=finished_at,
            outcome=outcome,
            signal="progressing",
            journal_tick_id=journal_tick_id,
            journaled=journaled,
        )
    return row


def test_list_epics_serves_the_heartbeat_the_epic_page_reads(store: Any) -> None:
    """#2844 N18: the Epics row and the epic page read one heartbeat.

    The page widens its freshness window with the last tick's duration and
    takes a newer attempt from the tick log over the driver state's stamp. The
    row could do neither: the summary carried no duration, and the newest
    attempt only reached the rows whose strip the list had fetched — at most
    twenty per shelf. Both are now on every row of the summary.
    """
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0)
    _attempt(
        store,
        "e",
        "2026-07-02T00:01:00+00:00",
        finished_at="2026-07-02T00:01:40+00:00",
    )
    summary = list_epics(store)[0]
    detail = get_epic_detail(store, "e")
    assert detail is not None and detail.heartbeat is not None
    assert summary.last_tick_duration_s == detail.heartbeat.last_tick_duration_s == 1.0
    assert summary.last_attempt_started_at == "2026-07-02T00:01:00+00:00"
    assert summary.last_attempt_finished_at == "2026-07-02T00:01:40+00:00"

    # A newer attempt still running: its start, and no finish yet.
    _attempt(store, "e", "2026-07-02T00:02:00+00:00")
    running = list_epics(store)[0]
    assert running.last_attempt_started_at == "2026-07-02T00:02:00+00:00"
    assert running.last_attempt_finished_at is None


def test_list_epics_serves_the_newest_attempts_outcome(store: Any) -> None:
    """#2882: only an attempt that reached a tick may advance the heartbeat,
    so the row carries the newest attempt's outcome — a refusal (failed,
    locked out, empty graph) is told apart from a beat, and a running
    attempt has none yet."""
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0)
    _attempt(
        store,
        "e",
        "2026-07-02T00:10:00+00:00",
        finished_at="2026-07-02T00:10:01+00:00",
        outcome="locked_out",
    )
    assert list_epics(store)[0].last_attempt_outcome == "locked_out"

    _attempt(store, "e", "2026-07-02T00:11:00+00:00")
    assert list_epics(store)[0].last_attempt_outcome is None


def test_an_open_attempt_is_served_as_reached_only_once_its_tick_began(
    store: Any,
) -> None:
    """#2882: an attempt row opens before the lock, the preflight and the
    graph build, so the list row and the strip both say whether the open
    attempt got past ``begin_epic_tick`` — and the mark is never served as the
    attempt's signal."""
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0)
    row = _attempt(store, "e", "2026-07-02T00:00:10+00:00")
    assert list_epics(store)[0].last_attempt_reached_tick is False
    assert [s.reached_tick for s in tick_strip(store, "e")] == [False]

    store.mark_epic_tick_log_reached(row)
    assert list_epics(store)[0].last_attempt_reached_tick is True
    [served] = tick_strip(store, "e")
    assert (served.reached_tick, served.signal, served.outcome) == (True, None, None)
    marks = {m.id: m for m in tick_strip(store, "e", changes=True)}
    assert marks[row].reached_tick is True


def test_list_epics_leaves_the_attempt_absent_for_an_epic_with_no_log(
    store: Any,
) -> None:
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0)
    _seed_heartbeat(store, "f", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0)
    _attempt(store, "f", "2026-07-02T00:05:00+00:00")
    rows = {row.epic_ref: row for row in list_epics(store)}
    assert rows["e"].last_attempt_started_at is None
    assert rows["e"].last_attempt_finished_at is None
    # One epic's attempt is never another's.
    assert rows["f"].last_attempt_started_at == "2026-07-02T00:05:00+00:00"


def test_list_epics_reads_no_attempt_of_an_archived_epic(store: Any) -> None:
    """#2900 C4: the roster's attempt read is bounded to the epics not
    archived. The archived epic's newest attempt is made unreadable — a count
    that is not a number — so reading it at all would raise; the list still
    answers, the archived row carries no attempt facts, and the live row
    keeps its own."""
    _seed_heartbeat(
        store, "live", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0
    )
    _seed_heartbeat(
        store, "retired", started_at="2026-07-02T00:00:00+00:00", interval_s=60.0
    )
    _attempt(store, "live", "2026-07-02T00:05:00+00:00")
    unreadable = _attempt(store, "retired", "2026-07-02T00:06:00+00:00")
    store.append_audit_event(
        timestamp="2026-09-29T10:00:00+00:00",
        event=EPIC_ARCHIVED_EVENT,
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": "retired"},
    )
    store._conn.execute(
        "UPDATE epic_tick_log SET n_dispatched = 'not a number' WHERE id = ?",
        (unreadable,),
    )

    rows = {row.epic_ref: row for row in list_epics(store)}

    assert rows["retired"].archived is True
    assert rows["retired"].last_attempt_started_at is None
    assert rows["retired"].last_attempt_outcome is None
    assert rows["live"].last_attempt_started_at == "2026-07-02T00:05:00+00:00"


class _StampReadRecorder:
    """The store, recording which refs each batched stamp read is asked for."""

    def __init__(self, store: SqliteRunStore) -> None:
        self._store = store
        self.asked: list[list[str]] = []

    def epic_tick_log_stamps(self, refs: list[str]) -> dict[str, str]:
        self.asked.append(list(refs))
        return self._store.epic_tick_log_stamps(refs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def test_list_epics_serves_an_archived_epic_no_attempt_stamp(store: Any) -> None:
    """#2900 C4: the roster's stamp read is bounded to the epics not archived,
    like its attempt read. The archived row serves an empty stamp although it
    has attempt rows; the live row keeps its own; and the stamp query is asked
    for the live ref alone."""
    _attempt(store, "live", "2026-07-02T00:05:00+00:00")
    _attempt(store, "retired", "2026-07-02T00:06:00+00:00")
    store.append_audit_event(
        timestamp="2026-09-29T10:00:00+00:00",
        event=EPIC_ARCHIVED_EVENT,
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": "retired"},
    )
    recorder = _StampReadRecorder(store)

    rows = {row.epic_ref: row for row in list_epics(recorder)}

    assert rows["retired"].archived is True
    assert store.epic_tick_log_stamp("retired") != ""
    assert rows["retired"].tick_attempt_stamp == ""
    assert rows["live"].tick_attempt_stamp == store.epic_tick_log_stamp("live")
    assert recorder.asked == [["live"]]


def _journaled_attempt(store: Any, epic_ref: str, stamp: str, status: str) -> int:
    """One tick attempt closed the way the driver closes it: the journal id is
    whatever ``journal.record_tick`` returns, appended or not."""
    tick_id, inserted = journal.record_tick(
        store,
        epic_ref=epic_ref,
        snapshot={
            "epic_ref": epic_ref,
            "signal": "progressing",
            "counts": {status: 1},
            "frontier": [],
            "nodes": [{"id": "1", "status": status}],
            "edges": [],
        },
        actions={},
        tick_ts=stamp,
        prev_snapshot=None,
    )
    _attempt(
        store,
        epic_ref,
        stamp,
        finished_at=stamp,
        journal_tick_id=tick_id,
        journaled=inserted,
    )
    return tick_id


def test_the_change_strip_draws_one_mark_per_change_not_per_repeated_journal_id(
    store: Any,
) -> None:
    """#2844 D23: an unchanged snapshot's attempt carries the PREVIOUS change's
    journal id (``append_epic_tick`` returns the existing row), so a journal id
    alone does not mean the attempt changed anything. A change is the first
    attempt carrying its id.
    """
    stamp = "2026-07-02T00:00:01+00:00"
    first = _journaled_attempt(store, "e", stamp, "ready")
    assert _journaled_attempt(store, "e", stamp, "ready") == first
    assert _journaled_attempt(store, "e", stamp, "ready") == first

    marks = tick_strip(store, "e", changes=True)
    assert [row.journal_tick_id for row in marks] == [first]

    second = _journaled_attempt(store, "e", stamp, "done")
    _journaled_attempt(store, "e", stamp, "done")
    marks = tick_strip(store, "e", changes=True)
    assert [row.journal_tick_id for row in marks] == [first, second]
    # The plain strip still serves every attempt, repeats included.
    assert len(tick_strip(store, "e")) == 5


def test_the_change_strip_keeps_a_change_past_two_hundred_no_op_attempts(
    store: Any,
) -> None:
    """#2844 D23: the change sparkline read the last 200 attempts and filtered
    them, so one change followed by 201 no-op ticks drew no mark while CHANGE
    still said 1. ``changes=True`` reads the marks themselves — the changes,
    the failed ticks and the tick still running — however many no-ops sit
    between them.
    """
    _seed_heartbeat(store, "e", started_at="2026-07-02T00:00:00+00:00", interval_s=30.0)
    stamp = "2026-07-02T00:00:01+00:00"
    _attempt(store, "e", stamp, finished_at=stamp, journal_tick_id=1, journaled=True)
    for _ in range(201):
        _attempt(store, "e", stamp, finished_at=stamp)
    _attempt(store, "e", stamp, finished_at=stamp, outcome="failed")
    _attempt(store, "e", stamp)

    assert all(row.journal_tick_id is None for row in tick_strip(store, "e"))
    marks = tick_strip(store, "e", changes=True)
    assert [(row.journal_tick_id, row.outcome) for row in marks] == [
        (1, "nothing"),
        (None, "failed"),
        (None, None),
    ]
    # Bounded like the plain strip, and the newest marks are the ones kept.
    assert [row.outcome for row in tick_strip(store, "e", limit=2, changes=True)] == [
        "failed",
        None,
    ]
