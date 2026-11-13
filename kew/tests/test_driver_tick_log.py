"""`epic_tick_log` — one row per tick ATTEMPT, not per journaled change (#2304).

`epic_ticks` is a change journal: it dedups on the snapshot hash, so about nine
ticks in ten leave no row (the merge-path epic ran 476 ticks and journaled 18),
and the ticks that never reach the journal at all — the raise, the lock-out, the
empty graph — reached no reader but stderr.

The control-flow table this file pins, beside the journal's own
(`test_driver_tick_journal.py`):

    lock not acquired            -> one `locked_out` row
    preflight_tools() raises     -> one `failed` row, carrying the class
    empty graph                  -> one `empty_graph` row
    invalid graph                -> one row, outcome from what the tick did
    normal tick                  -> one row + the journal row it appended

And the one row no tick writes about itself: a row its own driver died holding
open, closed as `abandoned` by the next tick to hold the epic's lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, FakeFactsReader, FakeSource, _facts, _node

from kew.graph.domain import Edge
from kew.graph.driver.lock import single_driver_lock
from kew.graph.driver.tick import EmptyEpicGraphError
from kew.run_store import (
    EPIC_TICK_LOG_CAP,
    EPIC_TICK_OUTCOME_EMPTY_GRAPH,
    SqliteRunStore,
)

# Re-exported by assignment (not `from x import y`) so ruff's F811 doesn't
# mistake the pytest-fixture-as-parameter pattern for a real redefinition.
driver_env = _fx.driver_env
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths


def _only_row(store: Any) -> Any:
    rows = store.list_epic_tick_log_tail(EPIC, limit=50)
    assert len(rows) == 1, rows
    return rows[0]


def test_locked_out_tick_is_logged_even_though_it_journals_nothing(
    driver_env: Any,
) -> None:
    with single_driver_lock(driver_env.lock_path) as got:
        assert got is True
        result = driver_env.advance()

    assert result.locked_out is True
    # The journal still records nothing — an observer tick changed no state.
    assert driver_env.store.count_epic_ticks(EPIC) == 0

    row = _only_row(driver_env.store)
    assert row.outcome == "locked_out"
    assert row.ts_finished is not None
    assert row.error_class is None
    assert row.journal_tick_id is None


def test_an_exception_inside_a_tick_still_writes_a_failed_row(
    driver_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2. The failure path is the one that used to reach only stderr."""

    def _boom() -> None:
        raise RuntimeError("no tools for you: /home/someone/secret-checkout")

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", _boom)

    with pytest.raises(RuntimeError, match="no tools for you"):
        driver_env.advance()

    assert driver_env.store.count_epic_ticks(EPIC) == 0
    row = _only_row(driver_env.store)
    assert row.outcome == "failed"
    assert row.error_class == "RuntimeError"
    assert row.ts_started is not None
    assert row.ts_finished is not None
    # The CLASS, never the message: an exception string carries checkout paths
    # and occasionally a token-bearing URL, and this row is shown in a browser.
    assert "secret-checkout" not in repr(row)


def test_an_empty_graph_is_its_own_outcome(driver_env: Any) -> None:
    driver_env.deps = FakeSource(nodes=[], edges=[])

    with pytest.raises(EmptyEpicGraphError):
        driver_env.advance()

    row = _only_row(driver_env.store)
    assert row.outcome == EPIC_TICK_OUTCOME_EMPTY_GRAPH
    assert row.error_class == "EmptyEpicGraphError"
    # Reviewer finding (PR #2438): the row is a diagnostic, NOT a membership
    # claim. An empty graph is raised for a typo'd epic name or a wrong-tracker
    # cwd, so a ref known only by rows like this must stay off the roster
    # `kew epic run --all` drives — otherwise one mistyped advance STALLS every
    # later `--all` run forever, since nothing deletes attempt rows.
    assert driver_env.store.list_epic_refs() == []


def test_an_invalid_graph_logs_the_attempt_and_names_its_journal_row(
    driver_env: Any,
) -> None:
    driver_env.deps = FakeSource(
        nodes=[_node("1"), _node("2")],
        edges=[Edge("1", "2"), Edge("2", "1")],
    )

    driver_env.advance()

    row = _only_row(driver_env.store)
    assert row.outcome == "nothing"
    assert row.signal == "stalled"
    assert row.n_stuck == 2
    tick = driver_env.store.latest_epic_tick(EPIC)
    assert tick is not None
    assert row.journal_tick_id == tick.id
    assert row.error_class is None


def test_a_dispatching_tick_records_its_counts_and_the_quiet_tick_after_it(
    driver_env: Any,
) -> None:
    driver_env.deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    driver_env.facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN"), 2: _facts(2, state="OPEN")}
    )

    first = driver_env.advance(max_parallel=1)
    assert len(first.dispatched) == 1

    # The second tick dispatches nothing — "1" is in flight and max_parallel is
    # 1 — which is the roughly nine-in-ten reading the change journal could
    # never distinguish from a tick that did not happen.
    second = driver_env.advance(max_parallel=1)
    assert second.dispatched == ()

    rows = driver_env.store.list_epic_tick_log_tail(EPIC, limit=50)
    assert [r.outcome for r in rows] == ["dispatched", "nothing"]
    assert rows[0].n_dispatched == 1
    assert rows[0].signal == "progressing"
    assert rows[1].n_dispatched == 0
    # Each attempt names the journal row it left (or matched), so a strip slot
    # can link to the snapshot that tick produced.
    assert rows[0].journal_tick_id is not None
    assert rows[1].journal_tick_id is not None


def test_a_driven_change_is_drawn_as_the_attempt_that_journaled_it(
    driver_env: Any,
) -> None:
    """#2949: the driver closes each attempt saying whether it inserted its
    journal row, so the change strip draws the tick that made each change and
    never a quiet tick that only matched it."""
    for _ in range(4):
        driver_env.advance()

    rows = driver_env.store.list_epic_tick_log_tail(EPIC, limit=50)
    changes: dict[int, int] = {}
    for row in rows:
        assert row.journal_tick_id is not None
        changes.setdefault(row.journal_tick_id, row.id)
    assert len(changes) < len(rows)  # at least one tick was quiet

    marks = driver_env.store.list_epic_tick_log_marks(EPIC)
    assert [(m.id, m.journal_tick_id) for m in marks] == [
        (attempt, journal_id) for journal_id, attempt in changes.items()
    ]


def test_an_attempt_blocked_before_its_tick_never_reads_as_reached(
    driver_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2882. The attempt row opens before the lock, the preflight and the
    graph build, so an open row read as a heartbeat while preflight blocked
    and no tick ran. The row says it reached its tick only once
    ``begin_epic_tick`` has committed — never while blocked, and not after the
    preflight raised either.
    """
    seen: list[Any] = []

    def _blocked() -> None:
        seen.append(_only_row(driver_env.store))
        raise RuntimeError("preflight")

    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", _blocked)
    with pytest.raises(RuntimeError):
        driver_env.advance()

    assert [(r.ts_finished, r.reached_tick) for r in seen] == [(None, False)]
    row = _only_row(driver_env.store)
    assert (row.outcome, row.reached_tick) == ("failed", False)
    assert driver_env.store.get_epic_driver_state(EPIC) is None


@dataclass
class _ReadsTheLogMidTick(FakeFactsReader):
    """A facts reader that reads the tick log while the tick asks GitHub — the
    one point a test sees an attempt that is past ``begin_epic_tick`` and still
    open — and then, if asked, fails the way an unreachable GitHub does."""

    store: Any = None
    seen: list[Any] = field(default_factory=list)
    fail: bool = False

    def issue_facts(self, issue_num: int) -> Any:
        self.seen.extend(self.store.list_epic_tick_log_tail(EPIC, limit=50))
        if self.fail:
            raise ConnectionError("github")
        return super().issue_facts(issue_num)


@pytest.mark.parametrize("fail", [False, True])
def test_an_attempt_reads_as_reached_once_its_tick_began(
    driver_env: Any, fail: bool
) -> None:
    """#2882. Past ``begin_epic_tick`` the OPEN row says it reached its tick,
    so a long tick is still a heartbeat; the mark is no signal, and it outlives
    a tick that raised after it began."""
    driver_env.deps = FakeSource(nodes=[_node("1")], edges=[])
    driver_env.facts = _ReadsTheLogMidTick(
        issue_facts_by_num={1: _facts(1, state="OPEN")},
        store=driver_env.store,
        fail=fail,
    )

    if fail:
        with pytest.raises(ConnectionError):
            driver_env.advance()
    else:
        driver_env.advance()

    mid = driver_env.facts.seen[-1]
    assert (mid.ts_finished, mid.reached_tick, mid.signal) == (None, True, None)
    row = _only_row(driver_env.store)
    assert row.reached_tick is True
    if fail:
        assert (row.outcome, row.signal) == ("failed", None)
    else:
        assert row.signal == "progressing"


def test_a_row_a_dead_driver_left_open_is_closed_by_the_next_tick(
    driver_env: Any,
) -> None:
    """Reviewer finding (PR #2438). A driver killed mid-tick — TaskStop, OOM, a
    host reboot — closes nothing, and no reaper can see its row from outside,
    so every strip painted that attempt as a tick still running, permanently.

    The next tick of the same epic closes it, and the inference is only sound
    there: that tick HOLDS the epic's lock, so no other process can be mid-tick
    on this epic.
    """
    dead = driver_env.store.begin_epic_tick_log(EPIC, "2026-10-07T00:00:00+00:00")
    assert driver_env.store.list_epic_tick_log_tail(EPIC)[0].ts_finished is None

    driver_env.advance()

    rows = {r.id: r for r in driver_env.store.list_epic_tick_log_tail(EPIC, limit=50)}
    assert rows[dead].outcome == "abandoned"
    assert rows[dead].ts_finished is not None
    # The error class stays empty: nobody knows why that driver stopped, and a
    # row this console shows may not invent one.
    assert rows[dead].error_class is None
    # The sweeping tick still closed its own row with what it actually did.
    swept_by = [r for r in rows.values() if r.id != dead]
    assert len(swept_by) == 1
    assert swept_by[0].outcome not in (None, "abandoned")


def test_a_locked_out_driver_abandons_nothing(driver_env: Any) -> None:
    """Only the lock HOLDER may call another row abandoned.

    A locked-out driver cannot: the row it can see open belongs to the driver
    that holds the epic right now, and marking that one abandoned would paint
    the live tick dead on every strip.
    """
    holder_row = driver_env.store.begin_epic_tick_log(EPIC, "2026-10-07T00:00:00+00:00")
    with single_driver_lock(driver_env.lock_path) as got:
        assert got is True
        result = driver_env.advance()

    assert result.locked_out is True
    rows = {r.id: r for r in driver_env.store.list_epic_tick_log_tail(EPIC, limit=50)}
    assert rows[holder_row].outcome is None
    assert rows[holder_row].ts_finished is None
    assert [r.outcome for r in rows.values() if r.id != holder_row] == ["locked_out"]


def test_a_locked_out_tick_moves_the_strip_stamp_the_heartbeat_cannot(
    driver_env: Any,
) -> None:
    """Reviewer finding (PR #2438, round 3). The strip readers re-read on a
    stamp, and every stamp on offer was written by ``begin_epic_tick`` — which
    this path never reaches. End to end through ``advance``: the lock-out moves
    ``epic_tick_log_stamp`` and leaves every heartbeat column exactly where it
    was, so the attempt-log stamp is the only signal a reader could have used.
    """
    store = driver_env.store
    before_stamp = store.epic_tick_log_stamp(EPIC)
    before_state = store.get_epic_driver_state(EPIC)

    with single_driver_lock(driver_env.lock_path) as got:
        assert got is True
        assert driver_env.advance().locked_out is True

    assert store.epic_tick_log_stamp(EPIC) != before_stamp
    after_state = store.get_epic_driver_state(EPIC)
    for column in ("tick_count", "last_tick_started_at", "last_tick_ts"):
        assert getattr(after_state, column, None) == getattr(before_state, column, None)


def test_tick_log_is_capped_per_epic_and_prunes_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kew.run_store.EPIC_TICK_LOG_CAP", 3)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        ids = [
            store.begin_epic_tick_log("e", f"2026-10-07T00:00:0{i}+00:00")
            for i in range(6)
        ]
        # Another epic's rows are untouched by the cap: it is per epic.
        other = store.begin_epic_tick_log("other", "2026-10-07T00:00:00+00:00")

        kept = store.list_epic_tick_log_tail("e", limit=10)
        assert [r.id for r in kept] == ids[-3:]
        assert [r.id for r in store.list_epic_tick_log_tail("other", limit=10)] == [
            other
        ]


def test_open_row_reads_as_in_progress_until_it_is_closed(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        row_id = store.begin_epic_tick_log("e", "2026-10-07T00:00:00+00:00")
        open_row = store.list_epic_tick_log_tail("e")[0]
        # No ts_finished and no outcome IS the live state — there is no separate
        # "running" flag that could be left behind by a killed driver.
        assert open_row.ts_finished is None
        assert open_row.outcome is None

        store.finish_epic_tick_log(
            row_id,
            ts_finished="2026-10-07T00:00:05+00:00",
            outcome="held",
            signal="progressing",
            n_held=2,
        )
        closed = store.list_epic_tick_log_tail("e")[0]
        assert (closed.outcome, closed.n_held, closed.ts_finished) == (
            "held",
            2,
            "2026-10-07T00:00:05+00:00",
        )

        with pytest.raises(ValueError, match="unknown epic tick outcome"):
            store.finish_epic_tick_log(
                row_id, ts_finished="2026-10-07T00:00:06+00:00", outcome="exploded"
            )
        for limit in (0, 501):
            with pytest.raises(ValueError, match="between 1 and 500"):
                store.list_epic_tick_log_tail("e", limit=limit)


def test_cap_constant_is_a_bound_not_a_suggestion() -> None:
    assert EPIC_TICK_LOG_CAP >= 200


def test_two_ticks_over_an_unchanged_graph_journal_one_row(driver_env: Any) -> None:
    """#2879: the CHANGE column counts journal rows, so a tick that changed
    nothing must match the latest row rather than append one.

    The node carries a PR whose merge state GitHub answers every tick: the
    tick's own reading instant (``merge_state_at``) is the one field that moved
    each tick, and it made every quiet tick a new row.
    """
    facts = replace(
        _facts(
            1,
            state="CLOSED",
            pr_number=11,
            pr_url="https://github.com/o/r/pull/11",
            pr_state="MERGED",
            head_oid="9f8e7d1",
        ),
        merge_state_status="CLEAN",
    )
    driver_env.facts = FakeFactsReader(issue_facts_by_num={1: facts})

    driver_env.advance()
    driver_env.advance()

    journal = driver_env.store.list_epic_ticks(EPIC)
    assert len(journal) == 1, journal
    first, second = driver_env.store.list_epic_tick_log_tail(EPIC, limit=50)
    assert first.journal_tick_id == journal[0].id
    # The quiet tick names the row it matched: dedup, inserted=False.
    assert second.journal_tick_id == first.journal_tick_id
