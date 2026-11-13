"""The four needs-you surfaces count the same set (#2207).

The header said 16, Monitor's holds list said 4, a go-live epic's legend said
``needs you 0`` over six nodes each waiting on a human, and the Work screen drew
one row under a banner apologising for the rest. Four surfaces, four readings of
one English phrase, and at most one of them could have been right.

So there is one query — :func:`kew.service.needs_you.needs_you_set` — and this
module holds the surfaces to it over a fixture with every kind of wait in it:

* the **header**'s count and **Monitor**'s holds list and **Work**'s list all
  read ``DashboardDTO.rows``, which ``assemble`` carries from the set verbatim;
* the **epic legend** counts the snapshot nodes carrying ``attention``, joined by
  ``compose_node_attention`` from the same ``kew.waits`` table the set uses.

The assertions are over the payload FIELDS each surface reads, not over a
re-derivation of them: a test that recomputed the number four ways could agree
with itself while the served payload disagreed with all four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from test_service_dashboard import (
    FakeAuditEvent,
    FakeEpicTick,
    FakeRun,
    FakeStore,
    _config,
)

from kew.epic_holds import (
    BUDGET_HOLD_PREFIX,
    CI_PENDING_HOLD_REASON,
    CONTAINMENT_HOLD_PREFIX,
    REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
)
from kew.run_store import EpicHold, OrphanPRRecord
from kew.service import dashboard, needs_you
from kew.service.attention import UNREADABLE_EPIC_HOLDS
from kew.service.epics import fetch_epic_frames, get_epic_detail

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
EPIC = "go-live"


@dataclass
class _DriverState:
    """A live driver heartbeat, with every column the detail route reads."""

    epic_ref: str
    last_tick_started_at: str | None
    last_tick_ts: str | None = None
    last_tick_duration_s: float | None = 1.0
    tick_count: int = 12
    interval_s: float | None = 60.0
    last_signal: str = "awaiting_merges"


@dataclass
class _Tick:
    """A journaled tick, with the id/ts the detail route reads off it, and the
    empty ``actions`` a streamed snapshot frame enriches."""

    snapshot: dict[str, Any]
    id: int = 1
    tick_ts: str = "2026-09-29T11:00:00+00:00"
    epic_ref: str = EPIC
    actions: dict[str, Any] = field(default_factory=dict)


def _node(node_id: str, status: str, **extra: Any) -> dict[str, Any]:
    node = {
        "id": node_id,
        "title": f"node {node_id}",
        "status": status,
        "status_since": "2026-09-28T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    node.update(extra)
    return node


# Every kind of wait the set is meant to name, in one epic:
#
# 2001 awaiting a merge (in_review)        -> merge_ready
# 2002 awaiting a continuation             -> held
# 2003 awaiting host publication           -> publication_held
# 2004 awaiting a no-change decision       -> held
# 2005 held on the budget                  -> budget_exhausted (an epic hold too)
# 2006 held on a refused review sandbox     -> sandbox_refused
#
# and three states that are NOT a person's move, which must stay out of every
# count: a node still in flight, a done node, and a node held on the CI gate
# (``epic_holds.CI_PENDING_HOLD_REASON`` — a wait, not a person).
_WAITING_NODES = (
    _node("2001", "in_review", pr_url="https://example.invalid/pr/1"),
    _node("2002", "awaiting_continuation"),
    _node("2003", "awaiting_publication"),
    _node("2004", "awaiting_no_change"),
    _node("2005", "held", stuck=f"{BUDGET_HOLD_PREFIX}: epic cap reached at $40.00"),
    _node(
        "2006",
        "held",
        stuck=f"{REVIEW_SANDBOX_REFUSED_HOLD_PREFIX}: image never built on this host",
    ),
)
_QUIET_NODES = (
    _node("2010", "in_progress"),
    _node("2011", "done"),
    _node("2012", "held", stuck=CI_PENDING_HOLD_REASON),
)
WAITING_IDS = frozenset(n["id"] for n in _WAITING_NODES)


def _snapshot() -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": "awaiting_merges",
        "counts": {"in_review": 1, "held": 3, "done": 1, "in_progress": 1},
        "frontier": [],
        "nodes": [*_WAITING_NODES, *_QUIET_NODES],
        "edges": [],
    }


def _store(cls: type[FakeStore] = FakeStore) -> FakeStore:
    """One epic with every kind of wait, plus the waits that are not nodes.

    The driver is ticking (so the epic is not stalled), there is an open spend
    approval, an orphan PR nobody has acked, epic holds on 2005 and 2006 — the
    same nodes the snapshot records as held, which is ONE thing a person does
    each, not two — and a syncable run on 2001 whose open PR the work projection
    reads as awaiting a human decision, again the same wait the node's
    ``in_review`` records.

    Both held nodes carry a LIVE hold row because that is what a host looks
    like: every hold the driver honours is written to ``epic_holds``
    (``store.hold_epic_node``) before the tick that journals the node as held,
    and the needs-you set reads that table back to tell a still-held node from
    one whose hold an operator has since cleared
    (``kew.service.attention.stale_hold_wait``).
    """
    return cls(
        audit_events=[
            FakeAuditEvent(
                event="approval_hold",
                issue_num=2020,
                payload={"estimate_usd": 12.5, "basis": "over the per-issue cap"},
                id=1,
                timestamp="2026-09-29T10:00:00+00:00",
            )
        ],
        epic_holds=[
            EpicHold(
                2005,
                EPIC,
                f"{BUDGET_HOLD_PREFIX}: epic cap reached at $40.00",
                datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC),
                "2005",
            ),
            EpicHold(
                2006,
                EPIC,
                f"{REVIEW_SANDBOX_REFUSED_HOLD_PREFIX}: image never built on this host",
                datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC),
                "2006",
            ),
        ],
        orphan_prs=[
            OrphanPRRecord(
                77,
                "https://example.invalid/pr/77",
                "agent/77",
                "branch has no run",
                datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC),
                None,
            )
        ],
        syncable=[
            FakeRun(
                run_id="r-2001",
                issue_num=2001,
                item_id="2001",
                status="success",
                pr_state="open",
                pr_url="https://example.invalid/pr/1",
                started_at=datetime(2026, 9, 28, 8, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC),
            )
        ],
        epic_refs=[EPIC],
        driver_states={
            EPIC: _DriverState(
                epic_ref=EPIC,
                last_tick_started_at=NOW.isoformat(),
                last_tick_ts=NOW.isoformat(),
            )
        },
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot())},
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gh-backed ready queue so unit tests never shell out."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])


class DetailStore(FakeStore):
    """The dashboard's fake store, plus the two readers the epic detail route
    needs that the judgment queue does not: the tick count the economics row
    carries, and ticks whose rows have an id and a timestamp."""

    def latest_epic_tick(self, epic_ref: str) -> Any:
        return _Tick(snapshot=_snapshot()) if epic_ref == EPIC else None

    def count_epic_ticks(self, epic_ref: str) -> int:
        return 1

    def first_epic_tick_ts(self, epic_ref: str) -> str:
        return "2026-09-28T08:00:00+00:00"

    def list_epic_ticks(self, epic_ref: str, *, after_id: int = 0, limit: int = 500):
        return [_Tick(snapshot=_snapshot())] if epic_ref == EPIC else []


def test_the_four_surfaces_count_one_needs_you_set() -> None:
    store = _store(DetailStore)
    config = _config()

    composed = needs_you.needs_you_set(store, config, now=NOW)
    payload = dashboard.assemble(store, config, now=NOW)
    detail = get_epic_detail(store, EPIC, config=config, now=NOW)
    assert detail is not None and detail.snapshot is not None

    # Nothing failed to read, so every count below is a count and not a floor.
    assert composed.unavailable == []
    assert payload.unavailable == []
    assert detail.snapshot.unavailable == []

    # The header's counter, Monitor's holds list and the Work screen's list are
    # three readings of ONE served field — `rows` (see header-state.ts,
    # monitor/holds.ts and work/list.ts, held to it in
    # frontend/src/lib/needs-you-surfaces.test.ts). So what this half has to
    # hold is that the field IS the set: entry for entry, in order, with the
    # wording and the action each surface prints. Asserting three identical
    # `len(payload.rows)` expressions against each other instead proves only
    # that `==` works, and would still pass with `assemble` serving a filtered,
    # re-sorted or re-worded list.
    def _identity(row: Any) -> tuple[Any, ...]:
        return (row.type, row.item_id, row.epic_ref, row.reason, row.primary_action)

    assert [_identity(r) for r in payload.rows] == [
        _identity(e) for e in composed.entries
    ]

    # ...and that the one number the three of them read off it is the set's.
    served = len(payload.rows)
    legend = len([n for n in detail.snapshot.nodes if n.attention is not None])

    assert served == len(composed.entries)
    # The legend is the epic's own slice of the same set, and in this fixture
    # every node-addressable wait belongs to this epic.
    assert legend == len(
        [e for e in needs_you.epic_entries(composed.entries, EPIC) if e.item_id]
    )
    assert legend == len(WAITING_IDS)


def test_every_kind_of_wait_reaches_the_set_with_an_epic_item_and_action() -> None:
    composed = needs_you.needs_you_set(_store(), _config(), now=NOW)
    by_type: dict[str, list[Any]] = {}
    for entry in composed.entries:
        by_type.setdefault(entry.type, []).append(entry)

    # Every source contributed; a missing type is a surface going silent.
    assert set(by_type) == {
        "approval_hold",
        "budget_hold",
        "pr_review",
        "epic_wait",
        "orphan_pr",
    }
    # Each entry names what it takes to act: the action, and an item or an epic.
    for entry in composed.entries:
        assert entry.primary_action, f"{entry.type} names no action"
        assert entry.item_id or entry.epic_ref or entry.pr_number, (
            f"{entry.type} names neither an item nor an epic nor an artifact"
        )
    # The four node waits that no other reader can see, plus the two that two
    # readers each see — collapsed, so six waiting nodes are six entries.
    node_entries = {e.item_id for e in composed.entries if e.item_id in WAITING_IDS}
    assert node_entries == WAITING_IDS
    # 2005 is an epic hold AND a journaled `held` node: one entry, the hold's,
    # and it keeps the epic the node knew it belonged to.
    held = next(e for e in composed.entries if e.item_id == "2005")
    assert (held.type, held.epic_ref) == ("budget_hold", EPIC)
    # 2001 is the projection's merge decision AND a journaled `in_review` node:
    # one entry, the projection's (it knows the artifact), carrying the epic.
    merge = next(e for e in composed.entries if e.item_id == "2001")
    assert (merge.type, merge.epic_ref) == ("pr_review", EPIC)


def test_a_wait_that_is_nobody_s_move_stays_out_of_every_count() -> None:
    """In flight, done, and the CI gate are waits without a person in them.

    The CI-pending hold is the one hold that is deliberately silent
    (``kew.waits.classify_wait``): an operator cannot make a check finish, and a
    magenta diamond over it would spend the colour the UI rations for a human
    move on a machine's own clock.
    """
    composed = needs_you.needs_you_set(_store(), _config(), now=NOW)
    quiet = {n["id"] for n in _QUIET_NODES}
    assert {e.item_id for e in composed.entries} & quiet == set()


def test_an_archived_epic_s_waits_leave_the_set() -> None:
    """A retired drain is nobody's outstanding work (#2206), node waits included."""
    store = _archived(_store())
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    assert [e for e in composed.entries if e.type == "epic_wait"] == []


def test_a_failed_source_is_named_rather_than_counted_as_quiet() -> None:
    """One flaky reader degrades the set; it never reports a healthy world."""

    class Broken(FakeStore):
        def latest_epic_tick(self, epic_ref: str) -> Any:
            raise RuntimeError("ticks unreadable")

    store = Broken(
        epic_refs=[EPIC],
        driver_states={
            EPIC: _DriverState(epic_ref=EPIC, last_tick_started_at=NOW.isoformat())
        },
    )
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    assert "epic_waits" in composed.unavailable
    assert "epic_waits" not in composed.as_of
    # And the payload carries the confession through, which is what blanks the
    # header's counter instead of printing a confident 0.
    payload = dashboard.assemble(store, _config(), now=NOW)
    assert "epic_waits" in payload.unavailable


def _hold(node_id: str, reason: str, epic_ref: str = EPIC) -> EpicHold:
    return EpicHold(
        int(node_id),
        epic_ref,
        reason,
        datetime(2026, 9, 28, 9, 0, 0, tzinfo=UTC),
        node_id,
    )


def test_a_ci_pending_epic_hold_raises_no_diamond_and_displaces_nothing() -> None:
    """The CI gate is the one hold ``kew.waits`` classifies to nobody.

    An epic hold on it used to reach the node join through the generic HELD
    fallback, which put a magenta diamond reading "held" with "fix the cause,
    then kew epic unhold" on a node the driver's own attention stream is silent
    about — and, because the holds are merged after the projection, it did it
    OVER a real merge decision on the same node (2001's open PR).
    """
    store = _store(DetailStore)
    store._epic_holds.extend(  # noqa: SLF001
        [_hold("2012", CI_PENDING_HOLD_REASON), _hold("2001", CI_PENDING_HOLD_REASON)]
    )
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    by_node = {n.id: n.attention for n in detail.snapshot.nodes}
    assert by_node["2012"] is None
    # The node the CI hold would have overwritten keeps the projection's reading.
    assert by_node["2001"] is not None
    assert by_node["2001"].kind == "merge_decision"
    # And the legend still counts exactly the six waits the fixture has.
    assert len([a for a in by_node.values() if a is not None]) == len(WAITING_IDS)


def test_a_ci_pending_epic_hold_reaches_no_surface_s_count() -> None:
    """The same hold, through the OTHER door into the set (#2207).

    The node join classifies an epic hold through ``kew.waits`` and so stays
    silent on the CI gate; the needs-you set reads the same holds table for its
    ``budget_hold`` entries, and counting every hold there put the gate back on
    the three surfaces that read ``rows`` — the header, Monitor and Work — over
    an epic legend showing no diamond for it. Money outranks review in
    ``_collapse``, so on a node with an open PR the invented entry also rewrote
    a real merge decision as "budget_hold / ci_pending / approve".
    """
    store = _store(DetailStore)
    store._epic_holds.extend(  # noqa: SLF001
        [_hold("2012", CI_PENDING_HOLD_REASON), _hold("2001", CI_PENDING_HOLD_REASON)]
    )
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    payload = dashboard.assemble(store, _config(), now=NOW)
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None

    # The gate is nobody's move, so it is nobody's entry either.
    assert [e for e in composed.entries if e.reason == CI_PENDING_HOLD_REASON] == []
    assert "2012" not in {e.item_id for e in composed.entries}
    # 2001 keeps the reading the legend shows for it, not the hold's.
    merge = next(e for e in composed.entries if e.item_id == "2001")
    assert (merge.type, merge.primary_action) == ("pr_review", "review")
    # And the three surfaces reading `rows` still agree with the legend.
    legend = len([n for n in detail.snapshot.nodes if n.attention is not None])
    assert legend == len(WAITING_IDS)
    assert len(payload.rows) == len(composed.entries)
    assert {r.item_id for r in payload.rows} & {n["id"] for n in _QUIET_NODES} == set()


def test_the_node_join_reads_holds_scoped_to_the_epic() -> None:
    """The epic-hold read is narrowed to this epic (#2129's rule, #2207's join).

    Unscoped, the join asked for every hold on the host on every 1s epic-frames
    poll — and it also let another epic's hold on an id this epic merely carries
    raise a diamond on the boundary copy, which is the claim only the OWNING
    epic can make.
    """

    reads: list[str] = []

    class Watched(DetailStore):
        def list_all_epic_holds(self) -> Any:
            reads.append("*")
            return super().list_all_epic_holds()

        def list_epic_holds(self, epic_ref: str) -> Any:
            reads.append(epic_ref)
            return super().list_epic_holds(epic_ref)

    store = _store(Watched)
    # Another epic holding an id THIS epic also carries. 2004's own journaled
    # wait is `awaiting_no_change` (a plain `held`), so a leaked foreign hold
    # shows up as the kind it would overwrite that with.
    store._epic_holds.append(  # noqa: SLF001
        _hold("2004", f"{CONTAINMENT_HOLD_PREFIX}: no image", epic_ref="other-epic")
    )
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    assert reads == [EPIC]  # one scoped read, and no cross-epic read at all
    by_node = {n.id: n.attention for n in detail.snapshot.nodes}
    # 2005's own hold still lands; the foreign epic's hold on 2004 does not.
    assert by_node["2005"] is not None and by_node["2005"].kind == "budget_exhausted"
    assert by_node["2004"] is not None and by_node["2004"].kind == "held"


def test_the_live_epic_walk_is_read_once_per_set() -> None:
    """Two sources need it; the snapshot deserialization is paid once (#2207).

    ``_stalled_rows`` and ``_epic_wait_rows`` each used to walk the epics
    themselves, so one /api/dashboard poll called ``latest_epic_tick`` twice per
    epic — on a 5-10s poll, double the full-snapshot JSON parse for one answer.
    """
    reads: list[str] = []

    class Counting(FakeStore):
        def latest_epic_tick(self, epic_ref: str) -> Any:
            reads.append(epic_ref)
            return FakeEpicTick(snapshot=_snapshot()) if epic_ref == EPIC else None

    store = _store(Counting)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    assert composed.unavailable == []
    assert reads == [EPIC]


def test_each_wait_is_classified_by_the_driver_s_own_vocabulary() -> None:
    """A node's kind and action are ``kew.waits``'s, not a second opinion.

    The epic's DAG, Monitor's row and ``kew epic run``'s own tick line read one
    table (``STALL_BUCKETS``), so what an operator is told to do about a hold
    does not change with the surface they happen to be looking at.
    """
    store = _store(DetailStore)
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    kinds = {
        n.id: (n.attention.kind, n.attention.action)
        for n in detail.snapshot.nodes
        if n.attention is not None
    }
    assert kinds["2001"][0] == "merge_decision"  # the projection knows the PR
    assert kinds["2002"] == ("held", "kew epic continue <name> <node>")
    assert kinds["2003"][0] == "publication_held"
    assert kinds["2004"][0] == "held"
    assert kinds["2005"][0] == "budget_exhausted"
    assert kinds["2006"][0] == "sandbox_refused"


def test_a_cleared_hold_leaves_every_count_without_waiting_for_a_tick() -> None:
    """``kew epic unhold`` ends the wait, not the next snapshot the driver writes.

    The journaled snapshot is a RECORD of the tick that wrote it: clearing a
    hold deletes the ``epic_holds`` row and leaves that record untouched, so a
    set read off the snapshot alone kept serving the cleared reason — and kept
    counting it on the header, Monitor, Work and the epic legend — until the
    driver journaled again, which never happens on an epic nobody restarted.
    The live hold table is the authority for the waits that HAVE a row.
    """
    store = _store(DetailStore)
    store._epic_holds = [h for h in store._epic_holds if h.item_id != "2005"]  # noqa: SLF001

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    by_node = {n.id: n.attention for n in detail.snapshot.nodes}

    assert composed.unavailable == []
    # The cleared hold is gone from the set and from the legend, together.
    assert "2005" not in {e.item_id for e in composed.entries}
    assert by_node["2005"] is None
    # The waits with no hold row behind them are untouched: a node parked
    # awaiting a continuation or a publication has nothing to re-read, and
    # dropping those would answer a stale entry with a missing one.
    assert {"2002", "2003", "2004", "2006"} <= {e.item_id for e in composed.entries}
    assert by_node["2002"] is not None and by_node["2006"] is not None
    # And the two surfaces still count the same thing.
    legend = len([n for n in detail.snapshot.nodes if n.attention is not None])
    assert legend == len(
        [e for e in needs_you.epic_entries(composed.entries, EPIC) if e.item_id]
    )


def test_an_unreadable_hold_table_keeps_the_journaled_wait() -> None:
    """A read that did not happen cannot prove a hold was cleared.

    The suppression above is made of a POSITIVE fact — this item is not in the
    live hold table. A hold table that raised says nothing, so the journaled
    wait stands and the reader that needed the table names itself instead: a
    count that is a floor, never a count that is a lie.
    """

    class Broken(DetailStore):
        def list_all_epic_holds(self) -> Any:
            raise RuntimeError("holds unreadable")

        def list_epic_holds(self, epic_ref: str) -> Any:
            raise RuntimeError("holds unreadable")

    store = _store(Broken)
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None

    assert composed.unavailable == ["budget_holds"]
    assert "epic_waits" not in composed.unavailable
    held = next(e for e in composed.entries if e.item_id == "2005")
    assert held.type == "epic_wait"
    by_node = {n.id: n.attention for n in detail.snapshot.nodes}
    assert by_node["2005"] is not None
    assert UNREADABLE_EPIC_HOLDS in detail.snapshot.unavailable


def test_an_archived_epic_s_legend_counts_what_its_rows_count() -> None:
    """A retired epic leaves the legend exactly as far as it leaves the count.

    ``_epic_roster`` (#2206) drops a retired epic's JOURNALED node waits from
    the set, so the header, Monitor and Work stop counting them. The detail
    route handed those same waits to the join regardless, which left a diamond
    and a legend number on nodes no surface reading ``rows`` counted — #2207's
    disagreement, one epic over. Its driver's own OPEN HOLDS go with them
    (#2243): they were exempt as live reads, which kept four `budget_hold` rows
    on two abandoned drains counting against the header with no diamond behind
    them.

    The ITEM-LEVEL readings go too, which is the last exemption to fall. The
    work projection and the approval holds were left in on the grounds that
    neither reaches ``rows`` carrying an epic, so the header counts them either
    way — and that is exactly how a retired epic came to print a confident
    ``0 needs you`` in its legend (counted from the set, where no entry can be
    filed under a retired epic) over a DAG still drawing the magenta diamond.
    One page, two answers. So the gate is the whole join: an archived epic is
    not a place a person is asked to look, and 2001's open pull request goes on
    being counted where it actually is — the header, Monitor, Work — carrying no
    epic at all.
    """
    store = _archived(_store(DetailStore))

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    attending = {n.id for n in detail.snapshot.nodes if n.attention is not None}

    # The journaled waits and the two held nodes are gone from both readings...
    gated = {"2002", "2003", "2004", "2005", "2006"}
    assert attending & gated == set()
    assert {e.item_id for e in composed.entries} & gated == set()
    # ...and so is every diamond: the legend counts the epic's bucket of the
    # set, which is empty, so the DAG must draw nothing to disagree with.
    assert needs_you.epic_entries(composed.entries, EPIC) == []
    assert attending == set()
    # 2001's open PR is still a real wait and the header still counts it. What
    # it no longer does is name this epic, on either surface.
    assert "2001" in {e.item_id for e in composed.entries}
    assert needs_you.needs_you_by_epic(composed.entries).get(EPIC, 0) == 0
    # Empty, not unavailable: a retired drain is a definite "nobody is waiting"
    # about its journal, never "we could not tell".
    assert detail.snapshot.unavailable == []


def test_an_archived_epic_s_live_stream_lights_what_its_detail_read_lights() -> None:
    """The retirement gate is on the STREAM too, or the stream undoes it.

    ``fetch_epic_frames`` serves the same join to a live subscriber, and
    ``kew.serve.app``'s watch hub replaces a tab's whole snapshot with a streamed
    one — so a stream that handed the journaled nodes over ungated would re-light
    the exact diamonds the detail route and the needs-you set both drop, and win.
    Both frame kinds carry the one join, so both are held to it here.
    """
    store = _archived(_store(DetailStore))

    detail = get_epic_detail(store, EPIC, config=_config(), now=NOW)
    assert detail is not None and detail.snapshot is not None
    _cursor, frames = fetch_epic_frames(store, EPIC, {}, config=_config())

    expected = {n.id for n in detail.snapshot.nodes if n.attention is not None}
    assert expected == set()  # a retired epic asks nobody for anything

    attention = next(body for kind, body in frames if kind == "attention")
    assert set(attention["by_node"]) == expected
    assert attention["unavailable"] == []
    streamed = next(body for kind, body in frames if kind == "snapshot")["snapshot"]
    assert {
        n["id"] for n in streamed["nodes"] if n.get("attention") is not None
    } == expected
    assert streamed["unavailable"] == []


def test_an_unreadable_item_keeps_saying_so_under_a_higher_entry() -> None:
    """Collapsing an item's entries must not lose "we could not read this" (I19).

    ``work_unreadable`` is the lowest consequence there is, so an item that is
    both unreadable and waiting on something else loses that entry to the other
    one — and the payload stopped naming the item whose work facts were never
    read. The console's per-item dismissal guard is made of exactly that fact
    (``unreadItems`` in frontend/src/lib/dashboard.ts): without it, the review
    an operator just decided is drawn again the moment the projection recovers.
    """
    store = _store()
    # 2001's work facts fail per ITEM (the projection itself still answers),
    # and 2001 is also the snapshot's `in_review` node — two entries, one item.
    store.list_orphan_prs = _boom  # type: ignore[method-assign]

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    assert composed.projection is not None
    assert composed.projection.unreadable == ["2001"]

    entries = [e for e in composed.entries if e.item_id == "2001"]
    assert len(entries) == 1  # still ONE thing a person has to do
    assert entries[0].type == "epic_wait"
    assert entries[0].work_unread is True
    # And an item nothing was wrong with never claims it.
    assert all(not e.work_unread for e in composed.entries if e.item_id != "2001")


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("unreadable")


def _archived(store: FakeStore) -> FakeStore:
    """*store*, with ``EPIC`` retired by an operator (#2206)."""
    from kew.epic_archive import EPIC_ARCHIVED_EVENT

    store._audit.append(  # noqa: SLF001
        FakeAuditEvent(
            event=EPIC_ARCHIVED_EVENT,
            issue_num=None,
            payload={"epic_ref": EPIC, "reason": "superseded"},
            id=2,
            timestamp="2026-09-29T11:30:00+00:00",
        )
    )
    return store
