"""Fleet-wide `[parallel] max_workers` cap across concurrently-driven epics (F4/#864).

The fleet-wide cap is shared across every epic ticking concurrently: a node
reserved by ANY epic consumes a slot, counted fleet-wide off the item-keyed
reservation rows (post-#839). These tests pin two behaviors at the driver-tick
level (`advance` -> `_dispatch_frontier`):

1. Aggregate cap — an epic won't reserve past the fleet's free slots even when
   its own per-epic `max_parallel` would allow more, because a *different*
   epic's live reservations already fill the fleet.
2. Per-epic fairness — when several epics are concurrently active, no single
   epic may grab every slot; each is bounded to its fair share so another
   active epic keeps room for its ready nodes.

The atomic last-slot guarantee lives in `dispatch_core.try_admit_run`; this
driver-level budget is the up-front, fairness-aware gate that keeps an epic
from spawning children that would only requeue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

from kew.config import DispatchConfig, EpicConfig, ParallelConfig
from kew.run_store import RunRecord

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths


@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `gh`/`git` preflight in these tick tests — the FakeSink never spawns
    a real child and `remote_branch_exists` is stubbed (mirrors the existing
    tick tests that drive `advance` without the tool preflight)."""
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)


def _fleet_config(*, max_workers: int) -> DispatchConfig:
    return DispatchConfig(
        epic=EpicConfig(stall_days=3),
        parallel=ParallelConfig(max_workers=max_workers),
    )


def _open_source(*ids: str) -> tuple[FakeSource, FakeFactsReader]:
    """Independent (dep-free) OPEN nodes — every id is on the frontier."""
    deps = FakeSource(nodes=[_node(i) for i in ids], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={int(i): _facts(int(i), state="OPEN") for i in ids}
    )
    return deps, facts


# ---------------------------------------------------------------------------
# run_store: per-epic reservation aggregation
# ---------------------------------------------------------------------------


def test_live_reservation_counts_group_across_epics(store: Any) -> None:
    store.reserve_epic_node_item("alpha", "1", "r1")
    store.reserve_epic_node_item("alpha", "2", "r2")
    store.reserve_epic_node_item("beta", "3", "r3")

    assert store.live_reservation_counts_by_epic() == {"alpha": 2, "beta": 1}

    # Releasing a node drops it from its epic's live count (no drift).
    store.release_epic_node_item("2")
    assert store.live_reservation_counts_by_epic() == {"alpha": 1, "beta": 1}


def test_live_reservation_counts_empty_when_no_reservations(store: Any) -> None:
    assert store.live_reservation_counts_by_epic() == {}


# ---------------------------------------------------------------------------
# run_store: contending-epic roster (fairness denominator, #864 cold-start)
# ---------------------------------------------------------------------------


def _ticked(store: Any, epic_ref: str, *, signal: str, ago: timedelta) -> None:
    """Register `epic_ref` and record a finished tick `ago` in the past."""
    store.register_epic(epic_ref)
    store.finish_epic_tick(
        epic_ref,
        tick_ts=(datetime.now(UTC) - ago).isoformat(),
        duration_s=0.0,
        interval_s=None,
        signal=signal,
        tick_id=1,
    )


def test_contending_epic_refs_excludes_terminal(store: Any) -> None:
    """A registered epic contends while fresh (never ticked) or PROGRESSING;
    COMPLETE/STALLED/AWAITING_MERGES epics hold no dispatchable frontier and are
    excluded so they don't shrink other epics' fair share and idle fleet slots."""
    store.register_epic("alpha")  # fresh: last_signal IS NULL -> contends
    for name, signal in (
        ("beta", "progressing"),
        ("gamma", "complete"),
        ("delta", "stalled"),
        ("omega", "awaiting_merges"),
    ):
        _ticked(store, name, signal=signal, ago=timedelta(minutes=5))

    assert store.contending_epic_refs() == {"alpha", "beta"}


def test_contending_epic_refs_ages_out_a_never_again_ticked_epic(store: Any) -> None:
    """An epic whose last tick was PROGRESSING but which is never ticked again
    (finished by hand, abandoned) stops contending once its row goes stale
    (#2094). Left counted, four such rows made the fair share ceil(6/6)=1 and
    the one epic still being driven trickled a single node per tick."""
    _ticked(store, "live", signal="progressing", ago=timedelta(minutes=5))
    _ticked(store, "abandoned", signal="progressing", ago=timedelta(days=2))

    assert store.contending_epic_refs() == {"live"}


def test_contending_epic_refs_keeps_a_stale_epic_holding_a_reservation(
    store: Any,
) -> None:
    """Staleness is about the DRIVER, not the work: an epic whose tick loop
    stopped while a node is still reserved holds a live fleet slot, so it keeps
    contending regardless of how old its last tick is (#2094)."""
    _ticked(store, "stale-but-live", signal="progressing", ago=timedelta(days=30))
    store.reserve_epic_node_item("stale-but-live", "100", "r1")

    assert store.contending_epic_refs() == {"stale-but-live"}


def test_contending_epic_refs_honours_an_explicit_staleness_window(
    store: Any,
) -> None:
    """The age-out window is a parameter, and `now` is injectable so the rule is
    testable without sleeping."""
    _ticked(store, "alpha", signal="progressing", ago=timedelta(hours=2))

    assert store.contending_epic_refs(stale_after=timedelta(hours=3)) == {"alpha"}
    assert store.contending_epic_refs(stale_after=timedelta(hours=1)) == set()
    assert store.contending_epic_refs(now=datetime.now(UTC) - timedelta(hours=2)) == {
        "alpha"
    }


def test_contending_epic_refs_empty_when_no_driver_state(store: Any) -> None:
    assert store.contending_epic_refs() == set()


# ---------------------------------------------------------------------------
# driver tick: aggregate fleet cap across epics
# ---------------------------------------------------------------------------


def test_other_epic_reservations_fill_fleet_cap(tmp_path: Path, store: Any) -> None:
    """max_workers=2 is fleet-wide: a *different* epic already holding 2 live
    reservations leaves no slot, so this epic dispatches nothing even though its
    own per-epic budget (max_parallel=5) has room and its nodes are ready."""
    store.reserve_epic_node_item("beta", "100", "rb1")
    store.reserve_epic_node_item("beta", "101", "rb2")

    deps, facts = _open_source("1", "2")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=2),
    )

    assert result.dispatched == ()
    assert sink.calls == []


def test_partial_fleet_room_dispatches_only_the_free_slots(
    tmp_path: Path, store: Any
) -> None:
    """One free fleet slot (beta holds 1 of max_workers=2) => at most one
    dispatch this tick, regardless of the larger per-epic budget."""
    store.reserve_epic_node_item("beta", "100", "rb1")

    deps, facts = _open_source("1", "2", "3")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=2),
    )

    assert len(result.dispatched) == 1


# ---------------------------------------------------------------------------
# driver tick: per-epic fairness
# ---------------------------------------------------------------------------


def test_lone_active_epic_gets_the_whole_cap(tmp_path: Path, store: Any) -> None:
    """No other epic active => this epic may fill the whole fleet cap (3)."""
    deps, facts = _open_source("1", "2", "3", "4")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=3),
    )

    assert len(result.dispatched) == 3


def test_fairness_caps_epic_below_free_slots(tmp_path: Path, store: Any) -> None:
    """max_workers=4, beta already active with 1 reservation. Three fleet slots
    are free, but fairness bounds this epic to its share ceil(4/2)=2 so beta
    keeps room to grow to its own share — this epic takes 2, not 3."""
    store.reserve_epic_node_item("beta", "100", "rb1")

    deps, facts = _open_source("1", "2", "3", "4")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=4),
    )

    assert len(result.dispatched) == 2


def test_two_cold_epics_split_the_cap(tmp_path: Path, store: Any) -> None:
    """Cold-start fairness (the F4 contract): both epics are registered by
    `kew epic plan` (a driver-state row via `register_epic`) but neither has
    dispatched, so neither holds a reservation yet. The first epic to tick must
    take only its fair share ceil(4/2)=2 — NOT the whole cap — so the sibling's
    ready nodes are not permanently starved by tick ordering."""
    store.register_epic(EPIC)  # the epic we drive
    store.register_epic("beta")  # a registered, cold sibling with ready nodes

    deps, facts = _open_source("1", "2", "3", "4")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=4),
    )

    assert len(result.dispatched) == 2


def test_budget_helper_counts_cold_contending_sibling(store: Any) -> None:
    """A registered, cold sibling (ready nodes, no reservation yet) still
    divides the cap: with two contenders and cap=4 the fair share is 2, even
    though the whole fleet is free (#864 cold-start starvation fix)."""
    from kew.graph.driver.tick import _fleet_dispatch_budget

    store.register_epic(EPIC)
    store.register_epic("beta")  # cold: contends via the driver-state roster

    assert (
        _fleet_dispatch_budget(store, epic_ref=EPIC, fleet_cap=4, per_epic_budget=5)
        == 2
    )


def test_stale_siblings_do_not_shrink_the_fair_share(
    tmp_path: Path, store: Any
) -> None:
    """Three epics that stopped being ticked a day ago no longer divide the cap
    (#2094): the one epic still being driven takes the whole fleet cap of 4
    instead of the ceil(4/4)=1 node per tick the stale rows used to allow."""
    store.register_epic(EPIC)
    for name in ("canary-hygiene", "publication-dogfood", "product-coherence"):
        _ticked(store, name, signal="progressing", ago=timedelta(days=2))

    deps, facts = _open_source("1", "2", "3", "4")
    sink = FakeSink()
    result = _advance(
        deps=deps,
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        max_parallel=5,
        config=_fleet_config(max_workers=4),
    )

    assert len(result.dispatched) == 4


def test_budget_helper_disabled_cap_falls_back_to_per_epic(store: Any) -> None:
    """A non-positive fleet cap disables the fleet limit: the helper returns the
    per-epic budget untouched even with a busy fleet (mirrors dispatch_core's
    `cap <= 0` unbounded contract)."""
    from kew.graph.driver.tick import _fleet_dispatch_budget

    store.reserve_epic_node_item("beta", "100", "rb1")
    store.reserve_epic_node_item("beta", "101", "rb2")

    assert (
        _fleet_dispatch_budget(store, epic_ref=EPIC, fleet_cap=0, per_epic_budget=3)
        == 3
    )


def test_budget_helper_never_negative(store: Any) -> None:
    """An epic already over its fair share (e.g. cap shrank as a new epic became
    active) yields a floor of 0, never a negative budget."""
    from kew.graph.driver.tick import _fleet_dispatch_budget

    store.reserve_epic_node_item(EPIC, "1", "r1")
    store.reserve_epic_node_item(EPIC, "2", "r2")
    store.reserve_epic_node_item("beta", "100", "rb1")

    assert (
        _fleet_dispatch_budget(store, epic_ref=EPIC, fleet_cap=2, per_epic_budget=5)
        == 0
    )


# ---------------------------------------------------------------------------
# run_store + driver tick: the reservation exemption is liveness-aware (#2094)
# ---------------------------------------------------------------------------


def _hard_killed_reservation(store: Any, epic_ref: str, item_id: str) -> None:
    """Reserve `item_id` for `epic_ref` behind a pending run that never finalized.

    This is the leak a hard kill leaves: the `pending` row is only closed by the
    dispatch child's in-process `finally`, so a process-group kill / crash /
    driver restart strands both the row and the reservation it backs.
    """
    store.save(
        RunRecord(
            run_id=f"dead-{item_id}",
            issue_num=int(item_id),
            branch=f"agent/issue-{item_id}",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC) - timedelta(days=2),
            pgid=2**31 - 1,
            owner_pid=2**31 - 1,
            item_id=item_id,
        )
    )
    store.reserve_epic_node_item(epic_ref, item_id, f"dead-{item_id}")


def test_contending_epic_refs_drops_a_reservation_whose_owner_is_dead(
    store: Any,
) -> None:
    """The age-out's reservation exemption must PROBE the slot, not just read the
    row (#2094). An epic abandoned mid-drive leaves a hard-killed run holding a
    reservation nobody reconciles — that leaked row would exempt the epic from
    the age-out forever and keep dividing the fair share, which is the exact
    failure the age-out retires."""
    _ticked(store, "abandoned", signal="progressing", ago=timedelta(days=2))
    _hard_killed_reservation(store, "abandoned", "100")

    # No probe: unprobed rows keep contending (prove dead, never infer).
    assert store.contending_epic_refs() == {"abandoned"}
    # Probed dead: the slot is a leak, not contention.
    assert store.contending_epic_refs(liveness=lambda run: False) == set()


def test_contending_epic_refs_keeps_an_unprobeable_stale_reservation(
    store: Any,
) -> None:
    """An undeterminable probe (`None`) never frees a slot: a mid-startup or
    unprobeable owner keeps its epic contending, same as `live_run_count`."""
    _ticked(store, "stale-but-maybe-live", signal="progressing", ago=timedelta(days=2))
    _hard_killed_reservation(store, "stale-but-maybe-live", "100")

    assert store.contending_epic_refs(liveness=lambda run: None) == {
        "stale-but-maybe-live"
    }


def test_live_reservation_counts_by_epic_drop_a_dead_owner(store: Any) -> None:
    """The per-epic footprint is liveness-aware under a probe, so a leaked slot
    neither divides the cap nor eats its own epic's fair share."""
    _hard_killed_reservation(store, "abandoned", "100")
    store.reserve_epic_node_item("live", "101", "r-live")

    assert store.live_reservation_counts_by_epic() == {"abandoned": 1, "live": 1}
    assert store.live_reservation_counts_by_epic(liveness=lambda run: False) == {
        "live": 1
    }


def test_budget_helper_ignores_a_stale_epics_dead_reservation(store: Any) -> None:
    """End of the #2094 failure mode: a stale epic whose only claim on the fleet
    is a hard-killed run's leaked reservation stops dividing the cap. Removing
    the age-out exemption alone is not enough — `_compute_dispatch_budget` also
    unions the raw reservation roster, so both reads take the probe."""
    from kew.graph.driver.tick import _compute_dispatch_budget

    store.register_epic(EPIC)
    _ticked(store, "abandoned", signal="progressing", ago=timedelta(days=2))
    _hard_killed_reservation(store, "abandoned", "100")

    budget = _compute_dispatch_budget(
        store,
        epic_ref=EPIC,
        fleet_cap=4,
        per_epic_budget=5,
        liveness=lambda run: False,
    )

    assert budget.active_epics == 1
    assert budget.fair_share == 4
    assert budget.budget == 4
