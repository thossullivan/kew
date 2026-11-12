"""An archived epic leaves the needs-you set — live holds included (#2243).

`kew epic archive` promises that a retired epic "is excluded from the console's
needs-you count". Only the JOURNALED waits honoured it: ``_live_epic_refs``
gated the stalled row and the node waits, while ``_budget_rows`` read
``list_all_epic_holds()`` straight through, so four ``budget_hold`` rows on two
abandoned epics went on counting against the header (6 where the operator
expected 2) with no legend diamond behind them.

So the open hold is gated the same way, on the same read: one archive query per
set, shared with the live-epic walk. The three things that can go wrong, each
held here:

* the hold must LEAVE for an archived or ``complete`` epic, and come back on
  ``epic_unarchived`` — an archive decision is a fact nobody retracts;
* an epic the archive has never heard of keeps its hold — "I have no decision
  about this epic" is not "this epic is retired";
* an unreadable ledger must neither leak the holds nor drop them in silence:
  ``budget_holds`` names itself in ``unavailable`` so the count reads as a
  floor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from test_service_dashboard import (
    FakeAuditEvent,
    FakeEpicTick,
    FakeStore,
    _config,
)

from kew.epic_archive import EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT
from kew.epic_holds import BUDGET_HOLD_PREFIX
from kew.run_store import EpicHold
from kew.service import dashboard, needs_you
from kew.service.attention import UNREADABLE_ARCHIVE, compose_node_attention

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
EPIC = "containment"
HELD_AT = datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC)
REASON = f"{BUDGET_HOLD_PREFIX}: epic cap reached at $40.00"


def _node() -> dict[str, Any]:
    """The held node, journaled as `in_progress` so its only needs-you entry is
    the live hold row — the one thing #2243 is about."""
    return {
        "id": "1144",
        "title": "node 1144",
        "status": "in_progress",
        "status_since": "2026-10-01T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }


def _snapshot(signal: str = "awaiting_merges") -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": signal,
        "counts": {"in_progress": 1},
        "frontier": [],
        "nodes": [_node()],
        "edges": [],
    }


def _store(
    *,
    signal: str = "awaiting_merges",
    ticked: bool = True,
    archive: list[FakeAuditEvent] | None = None,
) -> FakeStore:
    """One epic with one open budget hold on it, and nothing else waiting."""
    return FakeStore(
        audit_events=list(archive or []),
        epic_holds=[EpicHold(1144, EPIC, REASON, HELD_AT, "1144")],
        epic_refs=[EPIC],
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot(signal))} if ticked else {},
    )


def _archive_event(name: str, event_id: int) -> FakeAuditEvent:
    return FakeAuditEvent(
        event=name,
        issue_num=None,
        payload={"epic_ref": EPIC, "reason": "abandoned: no tick since 2026-09-01"},
        id=event_id,
        timestamp="2026-10-01T18:40:37+00:00",
    )


def _hold_rows(entries: Any) -> list[Any]:
    return [e for e in entries if e.type == "budget_hold"]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gh-backed ready queue so unit tests never shell out."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])


def test_a_live_epic_s_hold_is_the_baseline_entry() -> None:
    """Without an archive decision the hold is exactly one row, as it always was."""
    composed = needs_you.needs_you_set(_store(), _config(), now=NOW)
    assert composed.unavailable == []
    assert [(e.item_id, e.epic_ref) for e in _hold_rows(composed.entries)] == [
        ("1144", EPIC)
    ]


def test_an_archived_epic_s_open_hold_reaches_no_row() -> None:
    """The header counted it; the legend never did. Now neither does (#2243)."""
    store = _store(archive=[_archive_event(EPIC_ARCHIVED_EVENT, 1)])

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    payload = dashboard.assemble(store, _config(), now=NOW)

    assert composed.unavailable == []
    assert _hold_rows(composed.entries) == []
    # The served field is what the header counts, so it is the one asserted on.
    assert _hold_rows(payload.rows) == []
    assert needs_you.epic_entries(payload.rows, EPIC) == []


def test_unarchiving_brings_the_same_hold_back() -> None:
    """Archiving is a decision, not a deletion: the later event wins."""
    store = _store(
        archive=[
            _archive_event(EPIC_ARCHIVED_EVENT, 1),
            _archive_event(EPIC_UNARCHIVED_EVENT, 2),
        ]
    )
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    assert [e.item_id for e in _hold_rows(composed.entries)] == ["1144"]


def test_a_complete_epic_s_hold_is_out_too() -> None:
    """``epic_is_retired`` is one predicate: a finished epic has no outstanding
    work for an entry to be about, whatever the archive ledger says."""
    composed = needs_you.needs_you_set(_store(signal="complete"), _config(), now=NOW)
    assert composed.unavailable == []
    assert _hold_rows(composed.entries) == []


def test_a_hold_on_an_epic_with_no_ticks_stays_in() -> None:
    """No journaled tick is no decision — dropping it would hide a real hold.

    A driver that was killed before its first tick still wrote its holds, and
    those are precisely the ones somebody has to clear.
    """
    composed = needs_you.needs_you_set(_store(ticked=False), _config(), now=NOW)
    assert composed.unavailable == []
    assert [e.item_id for e in _hold_rows(composed.entries)] == ["1144"]


def test_an_unreadable_archive_names_itself_and_serves_no_hold() -> None:
    """Neither a leak nor a silent drop: the count becomes "at least N"."""

    class Broken(FakeStore):
        def list_audit_events(self, **kwargs: Any) -> Any:
            if kwargs.get("event") in (EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT):
                raise RuntimeError("ledger unreadable")
            return super().list_audit_events(**kwargs)

    store = Broken(
        epic_holds=[EpicHold(1144, EPIC, REASON, HELD_AT, "1144")],
        epic_refs=[EPIC],
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot())},
    )
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    payload = dashboard.assemble(store, _config(), now=NOW)

    assert "budget_holds" in composed.unavailable
    assert "budget_holds" not in composed.as_of
    assert _hold_rows(composed.entries) == []
    assert "budget_holds" in payload.unavailable


def test_the_archive_is_read_once_per_set() -> None:
    """Three sources need the decision; the ledger is queried once for it."""
    reads: list[Any] = []

    class Counting(FakeStore):
        def list_audit_events(self, **kwargs: Any) -> Any:
            reads.append(kwargs.get("event"))
            return super().list_audit_events(**kwargs)

    store = Counting(
        audit_events=[_archive_event(EPIC_ARCHIVED_EVENT, 1)],
        epic_holds=[EpicHold(1144, EPIC, REASON, HELD_AT, "1144")],
        epic_refs=[EPIC],
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot())},
    )
    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert composed.unavailable == []
    # Two event-scoped queries make ONE archive decision (#2206); the approval
    # read asks by prefix, so it is not one of these.
    assert reads.count(EPIC_ARCHIVED_EVENT) == 1
    assert reads.count(EPIC_UNARCHIVED_EVENT) == 1


def test_an_archived_epic_s_node_join_shows_no_hold_attention() -> None:
    """The legend agrees with ``rows``, which is the whole point of the gate.

    The diamond and the header's count are composed from two different reads of
    one set, so a hold gated out of the set and left in the join would put the
    disagreement #2207 removed straight back — this time on an archived epic.
    """
    store = _store(archive=[_archive_event(EPIC_ARCHIVED_EVENT, 1)])
    config = _config()

    join = compose_node_attention(
        store,
        config,
        ["1144"],
        nodes=[_node()],
        epic_ref=EPIC,
        signal="awaiting_merges",
        now=NOW,
    )
    rows = needs_you.needs_you_set(store, config, now=NOW).entries

    assert join.by_node == {}
    assert join.unavailable == []
    assert len(join.by_node) == len(needs_you.epic_entries(rows, EPIC))


def test_a_live_epic_s_node_join_still_shows_the_hold() -> None:
    """The gate is the archive decision, not the hold read."""
    join = compose_node_attention(
        _store(),
        _config(),
        ["1144"],
        nodes=[_node()],
        epic_ref=EPIC,
        signal="awaiting_merges",
        now=NOW,
    )
    assert join.by_node["1144"].kind == "budget_exhausted"


def test_an_unreadable_archive_in_the_join_drops_the_hold_and_says_so() -> None:
    """Degrading to retired is the safe reading; being silent about it is not."""

    class Broken(FakeStore):
        def list_audit_events(self, **kwargs: Any) -> Any:
            if kwargs.get("event") in (EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT):
                raise RuntimeError("ledger unreadable")
            return super().list_audit_events(**kwargs)

    store = Broken(
        epic_holds=[EpicHold(1144, EPIC, REASON, HELD_AT, "1144")],
        epic_refs=[EPIC],
        epic_ticks={EPIC: FakeEpicTick(snapshot=_snapshot())},
    )
    join = compose_node_attention(
        store,
        _config(),
        ["1144"],
        nodes=[_node()],
        epic_ref=EPIC,
        signal="awaiting_merges",
        now=NOW,
    )
    assert join.by_node == {}
    assert UNREADABLE_ARCHIVE in join.unavailable
