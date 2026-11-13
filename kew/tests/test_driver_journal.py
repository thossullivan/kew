"""Snapshot builder + journal writer (#454 Phase A, spec Decision 4)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.journal import (
    build_actions,
    build_snapshot,
    patch_statuses,
    record_tick,
    snapshot_hash,
)
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import StuckNode, TerminalSignal, TickResult
from kew.run_store import SqliteRunStore
from kew.service.work import JOURNALED_MERGE_STATE_MAX_AGE_S

TS = "2026-07-02T09:00:00+00:00"
TS2 = "2026-07-02T09:01:00+00:00"
TS3 = "2026-07-02T09:02:00+00:00"


def _node(nid: str, *, external: bool = False) -> Node:
    return Node(
        id=nid,
        title=f"Issue {nid}",
        status=NodeStatus.TODO,
        epic_ref="e",
        url=f"https://github.com/o/r/issues/{nid}",
        external=external,
    )


def _graph() -> Graph:
    return Graph(
        [_node("1"), _node("2"), _node("9", external=True)],
        [Edge("1", "2"), Edge("9", "1")],
    )


def _result(**kw) -> TickResult:
    base = dict(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=2,
        signal=TerminalSignal.PROGRESSING,
    )
    base.update(kw)
    return TickResult(**base)


def _snap(
    statuses, *, frontier=(), stuck=None, prev=None, tick_ts=TS, signal="progressing"
):
    return build_snapshot(
        epic_ref="e",
        graph=_graph(),
        statuses=statuses,
        node_facts={},
        stuck=stuck or {},
        frontier=list(frontier),
        signal=signal,
        tick_ts=tick_ts,
        prev_snapshot=prev,
    )


def test_patch_statuses_actions_win() -> None:
    statuses = {"1": DriverStatus.TODO, "2": DriverStatus.IN_REVIEW}
    patched = patch_statuses(statuses, _result(dispatched=("1",), merged=("2",)))
    assert patched["1"] is DriverStatus.IN_PROGRESS  # dispatched -> in_progress
    assert patched["2"] is DriverStatus.DONE  # merged -> done
    held = patch_statuses({"3": DriverStatus.IN_REVIEW}, _result(held=("3",)))
    assert held["3"] is DriverStatus.HELD  # held this tick -> held
    assert statuses["1"] is DriverStatus.TODO  # input not mutated


def test_patch_statuses_out_of_band_closed_reads_done() -> None:
    # SFD-139: a node closed out-of-band this tick (kew didn't merge it, so it
    # is not in `merged`) still reads DONE in the patched snapshot.
    statuses = {"1": DriverStatus.IN_REVIEW}
    patched = patch_statuses(statuses, _result(closed=("1",)))
    assert patched["1"] is DriverStatus.DONE


def test_snapshot_shape_and_external_reduction() -> None:
    snap = _snap(
        {"1": DriverStatus.IN_PROGRESS, "2": DriverStatus.TODO}, frontier=["2"]
    )
    assert snap["schema"] == 1
    # external "9" keeps graph status
    assert snap["counts"] == {"in_progress": 1, "todo": 2}
    by_id = {n["id"]: n for n in snap["nodes"]}
    assert set(by_id["9"]) == {"id", "status", "external"}  # bare ref only
    assert by_id["1"]["title"] == "Issue 1"
    assert by_id["1"]["status_since"] == TS  # first observation
    assert snap["edges"] == [["1", "2"], ["9", "1"]]
    assert snap["frontier"] == ["2"]


def test_status_since_carry_forward() -> None:
    prev = _snap({"1": DriverStatus.IN_PROGRESS, "2": DriverStatus.TODO})
    nxt = _snap(
        {"1": DriverStatus.IN_PROGRESS, "2": DriverStatus.IN_REVIEW},
        prev=prev,
        tick_ts=TS2,
    )
    by_id = {n["id"]: n for n in nxt["nodes"]}
    assert by_id["1"]["status_since"] == TS  # unchanged -> carried forward
    assert by_id["2"]["status_since"] == TS2  # changed -> stamped now


def test_status_since_survives_external_flip() -> None:
    # "9" external in snap1 (bare entry, no status_since), internal in snap2
    snap1 = _snap({"1": DriverStatus.TODO, "2": DriverStatus.TODO})
    internal_graph = Graph(
        [_node("1"), _node("2"), _node("9")],
        [Edge("1", "2"), Edge("9", "1")],
    )
    snap2 = build_snapshot(
        epic_ref="e",
        graph=internal_graph,
        statuses={"1": DriverStatus.TODO, "2": DriverStatus.TODO},
        node_facts={},
        stuck={},
        frontier=[],
        signal="progressing",
        tick_ts=TS2,
        prev_snapshot=snap1,
    )  # must not raise despite prev entry lacking status_since
    by_id = {n["id"]: n for n in snap2["nodes"]}
    assert by_id["9"]["external"] is False
    assert by_id["9"]["status_since"] == TS2  # re-stamped at the flip tick


def test_hash_state_only_and_order_insensitive() -> None:
    a = _snap({"1": DriverStatus.TODO, "2": DriverStatus.TODO})
    b = _snap(
        {"1": DriverStatus.TODO, "2": DriverStatus.TODO}, tick_ts=TS2, prev=a
    )  # later ts, same state (status_since carries forward)
    assert snapshot_hash(a) == snapshot_hash(b)  # tick_ts is NOT a hash input
    # permuted array order, same state -> same hash
    c = dict(
        a,
        nodes=list(reversed(a["nodes"])),
        edges=list(reversed(a["edges"])),
        frontier=list(reversed(a["frontier"])),
    )
    assert snapshot_hash(c) == snapshot_hash(a)
    d = _snap({"1": DriverStatus.DONE, "2": DriverStatus.TODO})
    assert snapshot_hash(d) != snapshot_hash(a)  # state change -> new hash


def test_build_actions() -> None:
    # `closed` is the out-of-band-merged set (#2304): the snapshot already
    # paints those nodes DONE, so an `actions` column without them records a
    # tick that moved a node to done while claiming it did nothing.
    acts = build_actions(
        _result(
            dispatched=("5",),
            merged=("4",),
            closed=("7",),
            held=("3",),
            stuck=(StuckNode("6", "merge-failed", ()),),
        )
    )
    assert acts == {
        "schema": 1,
        "dispatched": ["5"],
        "merged": ["4"],
        "closed": ["7"],
        "held": ["3"],
        "stuck": [{"id": "6", "reason": "merge-failed"}],
    }


def test_record_tick_appends_and_audits(tmp_path: Path) -> None:
    store = SqliteRunStore(str(tmp_path / "d.db"))
    snap = _snap({"1": DriverStatus.IN_PROGRESS, "2": DriverStatus.TODO})
    tick_id, appended = record_tick(
        store,
        epic_ref="e",
        snapshot=snap,
        actions=build_actions(_result()),
        tick_ts=TS,
        prev_snapshot=None,
    )
    assert appended is True
    events = store.list_audit_events(limit=None, event="epic_node_status")
    # first-tick burst: one event per node, from null
    assert len(events) == 3
    assert all((e.payload or {}).get("from") is None for e in events)
    assert {(e.payload or {})["epic_ref"] for e in events} == {"e"}
    # int node ids populate issue_num
    assert {e.issue_num for e in events} == {1, 2, 9}
    # the durable node id is also addressable via the unchained item_id column
    assert {e.item_id for e in events} == {"1", "2", "9"}
    # unchanged state -> dedup, NO new events
    _, appended2 = record_tick(
        store,
        epic_ref="e",
        snapshot=_snap(
            {"1": DriverStatus.IN_PROGRESS, "2": DriverStatus.TODO},
            prev=snap,
            tick_ts=TS2,
        ),
        actions=build_actions(_result()),
        tick_ts=TS2,
        prev_snapshot=snap,
    )
    assert appended2 is False
    assert len(store.list_audit_events(limit=None, event="epic_node_status")) == 3
    # a change -> exactly the changed node's transition, with from set
    snap3 = _snap(
        {"1": DriverStatus.DONE, "2": DriverStatus.TODO}, prev=snap, tick_ts=TS2
    )
    record_tick(
        store,
        epic_ref="e",
        snapshot=snap3,
        actions=build_actions(_result(merged=("1",))),
        tick_ts=TS2,
        prev_snapshot=snap,
    )
    ev = store.list_audit_events(limit=None, event="epic_node_status")
    new = [e for e in ev if (e.payload or {}).get("from") == "in_progress"]
    assert len(new) == 1 and new[0].payload["to"] == "done"
    store.close()


def test_record_tick_raises_on_append_failure(tmp_path: Path) -> None:
    store = SqliteRunStore(str(tmp_path / "d.db"))
    snap = _snap({"1": DriverStatus.TODO, "2": DriverStatus.TODO})

    class Boom(SqliteRunStore):
        pass

    store.close()
    boom = Boom(str(tmp_path / "d.db"))

    def _raise(**kw):
        raise RuntimeError("db gone")

    boom.append_epic_tick = _raise  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        record_tick(
            boom,
            epic_ref="e",
            snapshot=snap,
            actions=build_actions(_result()),
            tick_ts=TS,
            prev_snapshot=None,
        )
    boom.close()


# --------------------------------------------------------------------------- #
# The driver's PR facts, journaled (#2303)
# --------------------------------------------------------------------------- #


def _pr_facts(
    *,
    head_oid: str | None = "9f8e7d1",
    merge_state: str | None = "BEHIND",
    pushed_at: str | None = "2026-07-02T08:59:00+00:00",
) -> NodeFacts:
    return NodeFacts(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
        review_decision=None,
        merge_state_status=merge_state,
        head_oid=head_oid,
        pr_pushed_at=pushed_at,
    )


def _snap_with_facts(facts: NodeFacts, *, prev=None, tick_ts=TS):
    return build_snapshot(
        epic_ref="e",
        graph=_graph(),
        statuses={"1": DriverStatus.IN_REVIEW, "2": DriverStatus.TODO},
        node_facts={"1": facts},
        stuck={},
        frontier=[],
        signal="progressing",
        tick_ts=tick_ts,
        prev_snapshot=prev,
    )


def test_the_snapshot_node_carries_the_drivers_pr_facts() -> None:
    """Read every tick and thrown away before #2303; a reader needs them to tell
    a verdict's head from the current one."""
    snap = _snap_with_facts(_pr_facts())

    node = {n["id"]: n for n in snap["nodes"]}["1"]
    assert node["head_oid"] == "9f8e7d1"
    assert node["merge_state"] == "BEHIND"
    assert node["pr_pushed_at"] == "2026-07-02T08:59:00+00:00"
    assert node["merge_state_since"] == TS


@pytest.mark.parametrize("merge_state", ["UNKNOWN", "unknown", "", None])
def test_an_unreadable_merge_state_is_never_written_as_a_state(
    merge_state: str | None,
) -> None:
    """GitHub answers UNKNOWN for a mergeability it has not computed; with no
    previous reading there is nothing to report, and no key is invented."""
    snap = _snap_with_facts(_pr_facts(merge_state=merge_state))

    node = {n["id"]: n for n in snap["nodes"]}["1"]
    assert "merge_state" not in node
    assert "merge_state_since" not in node
    assert "merge_state_at" not in node


@pytest.mark.parametrize("unreadable", ["UNKNOWN", "", None])
def test_an_unknown_mergeability_neither_churns_the_hash_nor_restarts_the_clock(
    unreadable: str | None,
) -> None:
    """The flip is GitHub's, not this host's (reviewer finding, #2303).

    GitHub recomputes mergeability on its own schedule, so a node flips
    BEHIND -> UNKNOWN -> BEHIND with nothing happening here. Dropping the key
    for the middle tick made that a DIFFERENT snapshot: the journal dedups on
    the hash, so it appended a tick row for each flip and an operator read churn
    as movement — and on the way back ``merge_state_since`` restarted, so
    ``base.since`` reported a shorter duration than the state had held.
    """
    first = _snap_with_facts(_pr_facts(merge_state="BEHIND"))
    blind = _snap_with_facts(_pr_facts(merge_state=unreadable), prev=first, tick_ts=TS2)
    again = _snap_with_facts(_pr_facts(merge_state="BEHIND"), prev=blind, tick_ts=TS3)

    nodes = [{n["id"]: n for n in snap["nodes"]}["1"] for snap in (first, blind, again)]
    assert [n["merge_state"] for n in nodes] == ["BEHIND"] * 3
    # The duration the state has actually held, through all three ticks.
    assert [n["merge_state_since"] for n in nodes] == [TS] * 3
    # And the reading is aged by when it was last READ, not by the tick that
    # reported it: the middle tick read nothing, so it keeps the first's stamp.
    assert [n["merge_state_at"] for n in nodes] == [TS, TS, TS3]
    # The reading instant is an observation, not state: none of it moves the
    # hash the journal dedups on (#2879).
    assert snapshot_hash(first) == snapshot_hash(blind) == snapshot_hash(again)


def test_rereading_the_same_merge_state_does_not_move_the_hash() -> None:
    """#2879: every tick GitHub answers stamps its own ``merge_state_at``, and
    hashing that stamp made every quiet tick a journal row."""
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    again = _snap_with_facts(_pr_facts(merge_state="CLEAN"), prev=first, tick_ts=TS2)

    node = {n["id"]: n for n in again["nodes"]}["1"]
    assert node["merge_state_at"] == TS2
    assert node["merge_state_since"] == TS
    assert snapshot_hash(first) == snapshot_hash(again)


def _record(store: SqliteRunStore, snap, *, prev, tick_ts: str) -> tuple[int, bool]:
    return record_tick(
        store,
        epic_ref="e",
        snapshot=snap,
        actions=build_actions(_result()),
        tick_ts=tick_ts,
        prev_snapshot=prev,
    )


def _merge_state_at(store: SqliteRunStore) -> str | None:
    (row,) = store.latest_node_pr_facts(epic_ref="e")
    return row.merge_state_at


def test_an_unchanged_graph_hours_apart_journals_one_fresh_row(
    tmp_path: Path,
) -> None:
    """#2879: two ticks well past the merge-state readers' one-hour bound
    (``JOURNALED_MERGE_STATE_MAX_AGE_S``) over an unchanged graph leave ONE
    journal row, so the served change count holds still, and that row carries
    the second tick's reading instant, so the merge state stays usable."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    later = (
        datetime.fromisoformat(TS)
        + timedelta(seconds=2 * JOURNALED_MERGE_STATE_MAX_AGE_S)
    ).isoformat()
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    first_id, first_inserted = _record(store, first, prev=None, tick_ts=TS)
    again = _snap_with_facts(_pr_facts(merge_state="CLEAN"), prev=first, tick_ts=later)

    again_id, again_inserted = _record(store, again, prev=first, tick_ts=later)

    assert first_inserted is True
    assert (again_id, again_inserted) == (first_id, False)
    assert store.count_epic_ticks("e") == 1
    assert _merge_state_at(store) == later
    latest = store.latest_epic_tick("e")
    assert latest is not None
    # The row is never rewritten: it still holds the tick it recorded, so a
    # reader that cached it by id, and a replay of it, see what that tick saw.
    assert latest.tick_ts == TS
    assert latest.snapshot == first
    assert latest.snapshot_hash == snapshot_hash(first)
    store.close()


def test_a_quiet_tick_leaves_the_journal_row_byte_identical(
    tmp_path: Path,
) -> None:
    """#2879: ``epic_ticks`` is an immutable change journal. The read time a
    quiet tick learns goes to separate mutable state, never into the row."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    _record(store, first, prev=None, tick_ts=TS)
    before = store._conn.execute("SELECT * FROM epic_ticks").fetchall()
    again = _snap_with_facts(_pr_facts(merge_state="CLEAN"), prev=first, tick_ts=TS3)

    _record(store, again, prev=first, tick_ts=TS3)

    after = store._conn.execute("SELECT * FROM epic_ticks").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert _merge_state_at(store) == TS3
    store.close()


def test_a_carried_reading_never_moves_the_read_time_backwards(
    tmp_path: Path,
) -> None:
    """A tick GitHub answers ``UNKNOWN`` on carries the journaled row's
    (older) stamp; it must not undo the later read a quiet tick recorded."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    _record(store, first, prev=None, tick_ts=TS)
    again = _snap_with_facts(_pr_facts(merge_state="CLEAN"), prev=first, tick_ts=TS2)
    _record(store, again, prev=first, tick_ts=TS2)
    blind = _snap_with_facts(_pr_facts(merge_state="UNKNOWN"), prev=first, tick_ts=TS3)

    _, inserted = _record(store, blind, prev=first, tick_ts=TS3)

    assert inserted is False
    assert _merge_state_at(store) == TS2
    store.close()


def test_a_row_hashed_by_an_older_build_still_dedups_an_unchanged_tick(
    tmp_path: Path,
) -> None:
    """Rows written before #2879 hashed ``merge_state_at``, so their stored
    hash never equals the current one. The latest row is re-hashed with the
    current :func:`snapshot_hash` before comparing, or the first upgraded tick
    (and any tick under version skew) appends a false change."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    legacy_id, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts=TS,
        snapshot=first,
        actions=build_actions(_result()),
        snapshot_hash="0" * 64,
    )
    again = _snap_with_facts(_pr_facts(merge_state="CLEAN"), prev=first, tick_ts=TS2)

    assert _record(store, again, prev=first, tick_ts=TS2) == (legacy_id, False)
    assert store.count_epic_ticks("e") == 1
    assert _merge_state_at(store) == TS2
    store.close()


@pytest.mark.parametrize(
    "facts",
    [
        _pr_facts(merge_state="DIRTY"),
        _pr_facts(head_oid="0a1b2c3", merge_state="CLEAN"),
    ],
    ids=["merge-state-changed", "head-moved"],
)
def test_a_real_merge_state_or_head_change_still_journals_a_row(
    tmp_path: Path, facts: NodeFacts
) -> None:
    store = SqliteRunStore(str(tmp_path / "d.db"))
    first = _snap_with_facts(_pr_facts(merge_state="CLEAN"))
    _record(store, first, prev=None, tick_ts=TS)
    changed = _snap_with_facts(facts, prev=first, tick_ts=TS2)

    _, inserted = _record(store, changed, prev=first, tick_ts=TS2)

    assert inserted is True
    assert store.count_epic_ticks("e") == 2
    assert _merge_state_at(store) == TS2
    store.close()


def test_a_carried_merge_state_says_when_it_was_last_read() -> None:
    """``merge_state_at`` is the half that keeps the carry-forward honest: a
    reader bounds the value by it instead of reading it as this tick's own."""
    first = _snap_with_facts(_pr_facts(merge_state="DIRTY"))
    blind = _snap_with_facts(_pr_facts(merge_state="UNKNOWN"), prev=first, tick_ts=TS2)

    node = {n["id"]: n for n in blind["nodes"]}["1"]
    assert node["merge_state_at"] == TS
    assert node["merge_state_at"] != TS2


def test_nothing_is_carried_for_a_node_that_never_had_a_merge_state() -> None:
    """An epic whose driver has never read one must not gain a fact from a tick."""
    first = _snap_with_facts(_pr_facts(merge_state=None))
    second = _snap_with_facts(_pr_facts(merge_state=None), prev=first, tick_ts=TS2)

    assert "merge_state" not in {n["id"]: n for n in second["nodes"]}["1"]


def test_a_node_with_no_pr_facts_gains_no_keys() -> None:
    snap = _snap({"1": DriverStatus.TODO, "2": DriverStatus.TODO})

    node = {n["id"]: n for n in snap["nodes"]}["1"]
    assert not {
        "head_oid",
        "merge_state",
        "merge_state_since",
        "merge_state_at",
        "pr_pushed_at",
    } & set(node)


def test_merge_state_since_carries_forward_until_the_state_changes() -> None:
    first = _snap_with_facts(_pr_facts(merge_state="BEHIND"))
    same = _snap_with_facts(_pr_facts(merge_state="BEHIND"), prev=first, tick_ts=TS2)
    moved = _snap_with_facts(_pr_facts(merge_state="DIRTY"), prev=same, tick_ts=TS2)

    assert {n["id"]: n for n in same["nodes"]}["1"]["merge_state_since"] == TS
    assert {n["id"]: n for n in moved["nodes"]}["1"]["merge_state_since"] == TS2
    assert {n["id"]: n for n in moved["nodes"]}["1"]["merge_state_at"] == TS2


def test_a_moved_head_changes_the_snapshot_hash() -> None:
    """The journal dedups on the hash, so a new head has to produce a new row."""
    before = _snap_with_facts(_pr_facts(head_oid="9f8e7d1"))
    after = _snap_with_facts(_pr_facts(head_oid="a1b2c3d"))

    assert snapshot_hash(before) != snapshot_hash(after)


def test_a_merge_state_is_not_carried_onto_a_head_that_moved() -> None:
    """The reviewer finding (#2303): a pushed head is the canonical reason
    GitHub answers UNKNOWN next, so carrying the previous reading across it
    journals the OLD head's merge state as a fact about the new one — a
    blocking ``merge_conflict`` on a branch that was just rebased clean."""
    conflicted = _snap_with_facts(_pr_facts(head_oid="9f8e7d1", merge_state="DIRTY"))
    pushed = _snap_with_facts(
        _pr_facts(head_oid="a1b2c3d", merge_state="UNKNOWN"),
        prev=conflicted,
        tick_ts=TS2,
    )

    node = {n["id"]: n for n in pushed["nodes"]}["1"]
    assert node["head_oid"] == "a1b2c3d"
    assert "merge_state" not in node
    assert "merge_state_since" not in node
    assert "merge_state_at" not in node


def test_a_clean_read_is_not_carried_onto_a_head_nobody_merged() -> None:
    """The same finding the other way round: a carried CLEAN would report "the
    head merges into its base" for a head that has never been checked."""
    clean = _snap_with_facts(_pr_facts(head_oid="9f8e7d1", merge_state="CLEAN"))
    pushed = _snap_with_facts(
        _pr_facts(head_oid="a1b2c3d", merge_state=None), prev=clean, tick_ts=TS2
    )

    assert "merge_state" not in {n["id"]: n for n in pushed["nodes"]}["1"]


def test_nothing_is_carried_when_this_tick_read_no_head() -> None:
    """A tick that read no head cannot pin the carried state to anything — and
    it has already omitted ``head_oid``, so the hash the carry protects moved
    regardless."""
    first = _snap_with_facts(_pr_facts(head_oid="9f8e7d1", merge_state="BEHIND"))
    blind = _snap_with_facts(
        _pr_facts(head_oid=None, merge_state=None), prev=first, tick_ts=TS2
    )

    node = {n["id"]: n for n in blind["nodes"]}["1"]
    assert "head_oid" not in node
    assert "merge_state" not in node


def test_a_state_read_on_the_moved_head_is_journaled_as_this_ticks_own() -> None:
    """Pinning the CARRY to the head does not hold back a fresh reading: the
    tick that does read a merge state for the new head journals it, stamped
    now, with its own first-seen clock."""
    conflicted = _snap_with_facts(_pr_facts(head_oid="9f8e7d1", merge_state="DIRTY"))
    rebased = _snap_with_facts(
        _pr_facts(head_oid="a1b2c3d", merge_state="CLEAN"), prev=conflicted, tick_ts=TS2
    )

    node = {n["id"]: n for n in rebased["nodes"]}["1"]
    assert node["merge_state"] == "CLEAN"
    assert node["merge_state_since"] == TS2
    assert node["merge_state_at"] == TS2


def test_snapshot_keeps_the_frontier_in_dispatch_order() -> None:
    """#2575: "next up" reads the order the driver will take, so the journal
    must not re-sort it — and reordering alone is not a state change."""
    statuses = {"1": DriverStatus.TODO, "2": DriverStatus.TODO}
    ordered = _snap(statuses, frontier=["2", "1"])
    assert ordered["frontier"] == ["2", "1"]
    assert snapshot_hash(ordered) == snapshot_hash(_snap(statuses, frontier=["1", "2"]))


def test_a_latest_row_this_build_cannot_hash_journals_the_tick(
    tmp_path: Path,
) -> None:
    """A latest row whose snapshot lacks the hashed fields is no match: the
    tick appends a row rather than raising or claiming nothing changed."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    store.append_epic_tick(
        epic_ref="e",
        tick_ts=TS,
        snapshot={},
        actions=build_actions(_result()),
        snapshot_hash="0" * 64,
    )
    snap = _snap_with_facts(_pr_facts(merge_state="CLEAN"), tick_ts=TS2)

    _, inserted = _record(store, snap, prev=None, tick_ts=TS2)

    assert inserted is True
    assert store.count_epic_ticks("e") == 2
    store.close()


def test_a_latest_row_with_a_malformed_node_list_journals_the_tick(
    tmp_path: Path,
) -> None:
    """A latest row that is valid JSON but holds a non-object node is no
    match either: re-hashing it must not raise, or the tick rolls back and
    every later tick meets the same row and the epic's journal stops."""
    store = SqliteRunStore(str(tmp_path / "d.db"))
    snap = _snap_with_facts(_pr_facts(merge_state="CLEAN"), tick_ts=TS2)
    store.append_epic_tick(
        epic_ref="e",
        tick_ts=TS,
        snapshot={**snap, "nodes": [None]},
        actions=build_actions(_result()),
        snapshot_hash="0" * 64,
    )

    _, inserted = _record(store, snap, prev=None, tick_ts=TS2)

    assert inserted is True
    assert store.count_epic_ticks("e") == 2
    store.close()
