"""Journal/heartbeat wiring through advance()/_run_tick (#454 Phase A).

Fakes follow test_driver_tick.py's existing conventions (extracted to
`tests/support/driver_fakes.py`). Control-flow table under test (see the
Phase A plan / task brief):

    lock not acquired            -> NO journal, NO heartbeat
    preflight_tools() raises     -> NO journal, NO heartbeat
    invalid graph                -> journal all-stuck STALLED (dedup'd)
                                     + BOTH heartbeat writes
    normal tick                  -> _run_tick journals at its END
                                     + BOTH heartbeat writes
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    NOW,
    REPO,
    FakeFactsReader,
    FakeSource,
    _facts,
    _node,
)
from typer.testing import CliRunner

from kew.cli import epic_app
from kew.config import DispatchConfig
from kew.graph.build import build_graph
from kew.graph.domain import Edge
from kew.graph.driver.lock import single_driver_lock
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import summarize
from kew.run_store import RunRecord

# Re-exported by assignment (not `from x import y`) so ruff's F811 doesn't
# mistake the pytest-fixture-as-parameter pattern for a real redefinition.
driver_env = _fx.driver_env
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths


def _pin_reservation(store, item_id: str) -> None:
    """Pin a reservation's `reserved_at` into the frozen `NOW` clock domain.

    `reserve_epic_node_item` stamps `reserved_at` with real wall-clock, but the
    scripted timelines here pin run rows to the frozen `NOW`; without this the
    reconciler's generation filter drops the terminal row and the node never
    releases (#972 review Finding 1; mirrors test_driver_tick.py).
    """
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (NOW.isoformat(), item_id),
    )
    store._conn.commit()


def test_locked_out_writes_nothing(tmp_path, driver_env) -> None:
    with single_driver_lock(driver_env.lock_path) as got:
        assert got is True
        result = driver_env.advance()

    assert result.locked_out is True
    assert driver_env.store.count_epic_ticks(EPIC) == 0
    assert driver_env.store.get_epic_driver_state(EPIC) is None


def test_preflight_raise_writes_nothing(tmp_path, driver_env, monkeypatch) -> None:
    def _boom() -> None:
        raise RuntimeError("no tools for you")

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", _boom)

    with pytest.raises(RuntimeError, match="no tools for you"):
        driver_env.advance()

    assert driver_env.store.count_epic_ticks(EPIC) == 0
    assert driver_env.store.get_epic_driver_state(EPIC) is None


def test_invalid_graph_journals_all_stuck_stalled(tmp_path, driver_env) -> None:
    # A 2-cycle: 1 <-> 2.
    driver_env.deps = FakeSource(
        nodes=[_node("1"), _node("2")],
        edges=[Edge("1", "2"), Edge("2", "1")],
    )

    result = driver_env.advance()

    assert result.signal == TerminalSignal.STALLED
    assert driver_env.store.count_epic_ticks(EPIC) == 1
    tick = driver_env.store.latest_epic_tick(EPIC)
    assert tick is not None
    assert tick.snapshot["signal"] == "stalled"
    assert tick.snapshot["frontier"] == []
    assert len(tick.snapshot["nodes"]) == 2
    for node in tick.snapshot["nodes"]:
        assert node["stuck"] is not None
        assert node["stuck"].startswith("invalid-graph:")

    state = driver_env.store.get_epic_driver_state(EPIC)
    assert state is not None
    assert state.last_tick_started_at is not None
    assert state.last_tick_ts is not None
    assert state.tick_count == 1

    # A second, identical tick dedups against the journal (still 1 row) but
    # the heartbeat still records that a tick ran.
    driver_env.advance()
    assert driver_env.store.count_epic_ticks(EPIC) == 1
    state2 = driver_env.store.get_epic_driver_state(EPIC)
    assert state2 is not None
    assert state2.tick_count == 2


def test_normal_tick_journals_patched_statuses(tmp_path, driver_env) -> None:
    driver_env.deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    driver_env.facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )

    result = driver_env.advance(max_parallel=1)

    assert result.dispatched == ("1",)
    assert driver_env.store.count_epic_ticks(EPIC) == 1
    tick = driver_env.store.latest_epic_tick(EPIC)
    assert tick is not None
    by_id = {n["id"]: n for n in tick.snapshot["nodes"]}
    assert by_id["1"]["status"] == "in_progress"  # action-patched (overlay said todo)
    assert by_id["2"]["status"] == "todo"
    assert tick.actions["dispatched"] == ["1"]
    assert "1" not in tick.snapshot["frontier"]

    state = driver_env.store.get_epic_driver_state(EPIC)
    assert state is not None
    assert state.last_tick_id == tick.id
    assert state.interval_s is None

    driver_env.advance(max_parallel=1, interval_s=42.0)
    state2 = driver_env.store.get_epic_driver_state(EPIC)
    assert state2 is not None
    assert state2.interval_s == 42.0


def test_journal_append_failure_fails_tick(tmp_path, driver_env, monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(driver_env.store, "append_epic_tick", _boom)

    with pytest.raises(RuntimeError, match="db exploded"):
        driver_env.advance()

    # Heartbeat phase 1 (begin_epic_tick) already ran before journaling —
    # phase 2 (finish_epic_tick) must NOT have run since the tick raised.
    state = driver_env.store.get_epic_driver_state(EPIC)
    assert state is not None
    assert state.last_tick_started_at is not None
    assert state.last_tick_ts is None


def test_journal_append_failure_fails_tick_on_invalid_graph(
    tmp_path, driver_env, monkeypatch
) -> None:
    # Same cyclic graph as the invalid-graph test: 1 <-> 2.
    driver_env.deps = FakeSource(
        nodes=[_node("1"), _node("2")],
        edges=[Edge("1", "2"), Edge("2", "1")],
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(driver_env.store, "append_epic_tick", _boom)

    with pytest.raises(RuntimeError, match="db exploded"):
        driver_env.advance()

    # The invalid-graph branch journals too, so the same loud-failure
    # contract applies: phase-1 heartbeat ran, phase-2 must not have.
    state = driver_env.store.get_epic_driver_state(EPIC)
    assert state is not None
    assert state.last_tick_started_at is not None
    assert state.last_tick_ts is None


def test_summarize_and_status_are_pure_reads(tmp_path, driver_env) -> None:
    driver_env.advance()
    count_before = driver_env.store.count_epic_ticks(EPIC)
    state_before = driver_env.store.get_epic_driver_state(EPIC)
    assert state_before is not None

    graph = build_graph(driver_env.deps, EPIC)
    summarize(
        EPIC,
        graph,
        config=driver_env.config,
        store=driver_env.store,
        facts=driver_env.facts,
        now=NOW,
    )
    summarize(
        EPIC,
        graph,
        config=driver_env.config,
        store=driver_env.store,
        facts=driver_env.facts,
        now=NOW,
    )

    assert driver_env.store.count_epic_ticks(EPIC) == count_before
    state_mid = driver_env.store.get_epic_driver_state(EPIC)
    assert state_mid is not None
    assert state_mid.tick_count == state_before.tick_count

    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(tmp_path)
    runner = CliRunner()
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.GitHubFactsReader", return_value=driver_env.facts),
    ):
        invoked = runner.invoke(epic_app, ["status", EPIC])
    assert invoked.exit_code == 0, invoked.stdout

    assert driver_env.store.count_epic_ticks(EPIC) == count_before
    state_after = driver_env.store.get_epic_driver_state(EPIC)
    assert state_after is not None
    assert state_after.tick_count == state_before.tick_count


def _snapshot_transitions(
    prev_snapshot: dict | None, snapshot: dict
) -> set[tuple[int, str | None, str]]:
    """Independent re-derivation of node-status transitions between two
    consecutive journal snapshots (does not reuse journal._transitions)."""
    prev_status = {n["id"]: n["status"] for n in (prev_snapshot or {}).get("nodes", [])}
    out: set[tuple[int, str | None, str]] = set()
    for n in snapshot["nodes"]:
        nid, new = n["id"], n["status"]
        old = prev_status.get(nid)
        if old != new:
            out.add((int(nid), old, new))
    return out


def test_audit_corroboration_journal_superset_of_chain(tmp_path, driver_env) -> None:
    driver_env.deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[Edge("1", "2")])
    driver_env.facts = FakeFactsReader(
        issue_facts_by_num={
            1: _facts(1, state="OPEN"),
            2: _facts(2, state="OPEN"),
        }
    )

    # Tick 1: dispatch "1".
    result1 = driver_env.advance()
    assert result1.dispatched == ("1",)

    # `reserve_epic_node_item` stamps `reserved_at` with real wall-clock, but
    # this scripted timeline pins every run row to the frozen `NOW`. Pin the
    # reservation into the same clock domain so the reconciler admits the
    # terminal `run-1` row and "1" reads DONE (#972 review Finding 1).
    _pin_reservation(driver_env.store, "1")

    # Tick 2: "1" completes -> DONE, unblocking "2" into dispatch.
    driver_env.store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    driver_env.facts.issue_facts_by_num[1] = _facts(
        1, state="CLOSED", state_reason="COMPLETED"
    )
    result2 = driver_env.advance()
    assert result2.done == ("1",)
    assert result2.dispatched == ("2",)
    _pin_reservation(driver_env.store, "2")

    # Tick 3: "2" completes -> COMPLETE.
    driver_env.store.save(
        RunRecord(
            run_id="run-2",
            issue_num=2,
            branch="agent/issue-2",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    driver_env.facts.issue_facts_by_num[2] = _facts(
        2, state="CLOSED", state_reason="COMPLETED"
    )
    result3 = driver_env.advance()
    assert set(result3.done) == {"1", "2"}
    assert result3.signal == TerminalSignal.COMPLETE

    rows = driver_env.store.list_epic_ticks(EPIC)
    assert len(rows) == 3  # each tick changed state -> no dedup

    journal_transitions: set[tuple[int, str | None, str]] = set()
    prev_snapshot: dict | None = None
    for row in rows:
        journal_transitions |= _snapshot_transitions(prev_snapshot, row.snapshot)
        prev_snapshot = row.snapshot

    events = driver_env.store.list_audit_events(limit=None, event="epic_node_status")
    chain_transitions = {
        (e.issue_num, (e.payload or {}).get("from"), (e.payload or {}).get("to"))
        for e in events
        if (e.payload or {}).get("epic_ref") == EPIC
    }

    # Emission is best-effort, so in general chain <= journal; nothing drops
    # in this test, so assert equality.
    assert chain_transitions <= journal_transitions
    assert chain_transitions == journal_transitions
