"""Unit tests for the two-cadence delegation watcher (D3).

Proves the three properties the spec calls out: dedup across both sources in a
single tick, cursor replay-safety across a simulated crash, and the poll floor
surfacing an issue when no ``tracker_events`` exist (the tunnel-down path).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from kew.delegate.errors import FatalTrackerError
from kew.delegate.run import run_watch_loop
from kew.delegate.watch import (
    Candidate,
    DelegationWatcher,
    InMemoryCursorStore,
    MetaCursorStore,
    touches_assignee_or_labels,
)
from kew.run_store import TrackerEvent

ASSIGNEE_ID = "11111111-2222-3333-4444-555555555555"
LABEL = "kew:ready"
_T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _event(
    entity_id: str,
    *,
    offset_s: float = 0.0,
    action: str = "update",
    event_type: str = "Issue",
    updated_from: dict | None = None,
    data: dict | None = None,
) -> TrackerEvent:
    event_data = {"identifier": entity_id, **(data or {"id": entity_id})}
    payload = {
        "action": action,
        "type": event_type,
        "data": event_data,
    }
    if updated_from is not None:
        payload["updatedFrom"] = updated_from
    received = (_T0 + timedelta(seconds=offset_s)).isoformat()
    return TrackerEvent(
        delivery_id=f"d-{entity_id}-{offset_s}",
        source="linear",
        event_type=event_type,
        action=action,
        entity_id=entity_id,
        webhook_timestamp=None,
        payload=json.dumps(payload),
        received_at=received,
    )


class FakeEvents:
    """Fake local source honouring the ``since`` bound like the real store."""

    def __init__(self, events: list[TrackerEvent]) -> None:
        self.events = events

    def list_tracker_events_since(self, since: datetime) -> list[TrackerEvent]:
        since_iso = since.isoformat()
        return sorted(
            (e for e in self.events if (e.received_at or "") >= since_iso),
            key=lambda e: e.received_at or "",
        )


class FakePoll:
    """Fake throttled Linear poll returning a fixed id list; counts calls."""

    def __init__(self, ids: list[str], *, error: Exception | None = None) -> None:
        self.ids = ids
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def list_delegated(self, assignee_id: str, label: str) -> list[str]:
        self.calls.append((assignee_id, label))
        if self.error is not None:
            raise self.error
        return list(self.ids)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


LABEL_ID = "label-0000-1111-2222-3333"


def _watcher(
    events,
    poll,
    cursor_store,
    *,
    decided=(),
    clock=None,
    poll_interval_seconds=20,
    label_id=None,
):
    decided_set = set(decided)
    return DelegationWatcher(
        events=events,
        poll=poll,
        cursor_store=cursor_store,
        assignee_id=ASSIGNEE_ID,
        label=LABEL,
        is_terminally_decided=lambda item_id: item_id in decided_set,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock or FakeClock(),
        label_id=label_id,
    )


# --- the filter ------------------------------------------------------------


def test_filter_keeps_assignee_and_label_updates() -> None:
    assert touches_assignee_or_labels(_event("A", updated_from={"assigneeId": "x"}))
    assert touches_assignee_or_labels(_event("A", updated_from={"labelIds": ["l"]}))


def test_filter_drops_unrelated_updates_and_non_issue_events() -> None:
    # An update that only changed the title is not a delegation trigger.
    assert not touches_assignee_or_labels(_event("A", updated_from={"title": "new"}))
    # A comment event is not an Issue event at all.
    assert not touches_assignee_or_labels(
        _event("A", event_type="Comment", updated_from={"assigneeId": "x"})
    )
    # A remove is never a candidate.
    assert not touches_assignee_or_labels(_event("A", action="remove"))


def test_filter_keeps_create_that_lands_assigned_or_labeled() -> None:
    assert touches_assignee_or_labels(
        _event("A", action="create", data={"id": "A", "assigneeId": ASSIGNEE_ID})
    )
    assert touches_assignee_or_labels(
        _event("A", action="create", data={"id": "A", "labelIds": ["l"]})
    )
    # A bare create with neither assignee nor labels is not a candidate.
    assert not touches_assignee_or_labels(
        _event("A", action="create", data={"id": "A"})
    )


# --- dedup across both sources in one tick ---------------------------------


def test_dedup_across_both_sources_in_one_tick() -> None:
    events = FakeEvents(
        [
            _event("A", offset_s=1, updated_from={"assigneeId": ASSIGNEE_ID}),
            _event("B", offset_s=2, updated_from={"labelIds": ["l"]}),
        ]
    )
    poll = FakePoll(["B", "C"])  # B overlaps the local read; C is poll-only.
    watcher = _watcher(events, poll, InMemoryCursorStore())

    candidates = watcher.tick()

    # B appears once (local wins provenance), C from the poll floor.
    assert candidates == [
        Candidate("A", "local"),
        Candidate("B", "local"),
        Candidate("C", "poll"),
    ]


def test_webhook_uses_canonical_identifier_not_entity_uuid() -> None:
    event = _event(
        "e42df23e-ca96-4483-a5dd-2f83e150b7f5",
        updated_from={"assigneeId": None},
        data={
            "id": "e42df23e-ca96-4483-a5dd-2f83e150b7f5",
            "identifier": "SFD-157",
            "assigneeId": ASSIGNEE_ID,
        },
    )
    cursor = InMemoryCursorStore()
    watcher = _watcher(
        FakeEvents([event]),
        FakePoll([]),
        cursor,
        decided=("SFD-157",),
    )

    assert watcher.tick() == [Candidate("SFD-157", "local")]
    assert cursor.load() > event.received_at


# --- poll floor / tunnel-down path -----------------------------------------


def test_poll_floor_surfaces_issue_with_no_tracker_events() -> None:
    events = FakeEvents([])  # tunnel down: nothing local ever arrived.
    poll = FakePoll(["X"])
    watcher = _watcher(events, poll, InMemoryCursorStore())

    assert watcher.tick() == [Candidate("X", "poll")]


def test_poll_is_throttled_then_refreshes_after_the_interval() -> None:
    events = FakeEvents([])
    poll = FakePoll(["X"])
    clock = FakeClock()
    watcher = _watcher(
        events, poll, InMemoryCursorStore(), clock=clock, poll_interval_seconds=20
    )

    watcher.tick()  # first tick polls (floor establishes)
    clock.now = 5.0
    watcher.tick()  # within the interval -> no re-poll
    clock.now = 25.0
    watcher.tick()  # past the interval -> re-poll

    assert len(poll.calls) == 2


def test_poll_failure_is_isolated_from_the_fast_local_read(caplog) -> None:
    # Tunnel up (local events present) but the remote poll is unavailable: the
    # two cadences are independent, so the fast read still yields candidates.
    events = FakeEvents([_event("A", updated_from={"assigneeId": ASSIGNEE_ID})])
    poll = FakePoll([], error=RuntimeError("linear down"))
    watcher = _watcher(events, poll, InMemoryCursorStore())

    with caplog.at_level("WARNING", logger="kew.delegate.watch"):
        assert watcher.tick() == [Candidate("A", "local")]

    # A swallowed poll failure still leaves an operator signal in the log so a
    # down tunnel is distinguishable from "nothing delegated".
    assert any(
        r.levelname == "WARNING" and "delegation poll failed" in r.getMessage()
        for r in caplog.records
    )


def test_fatal_poll_error_is_not_swallowed() -> None:
    # An expired/revoked key (mapped to FatalTrackerError) from the poll leg must
    # NOT be swallowed like a transient blip — it propagates out of tick() so the
    # watch loop can terminate instead of retrying forever (Task 3, #971).
    events = FakeEvents([])
    poll = FakePoll([], error=FatalTrackerError("Linear authentication failed"))
    watcher = _watcher(events, poll, InMemoryCursorStore())

    with pytest.raises(FatalTrackerError):
        watcher.tick()


def test_run_watch_loop_terminates_on_a_fatal_poll_error(caplog) -> None:
    # A FatalTrackerError surfacing from the poll leg terminates the loop with the
    # fatal message and a non-zero exit (Task 3, #971) — never a silent retry.
    class _AuthFailWatcher:
        def tick(self):
            raise FatalTrackerError("Linear authentication failed")

    with (
        caplog.at_level("ERROR", logger="kew.delegate.run"),
        pytest.raises(SystemExit) as exc_info,
    ):
        run_watch_loop(
            watcher=_AuthFailWatcher(),
            process_one=lambda _c: None,
            sleep=lambda _s: None,
            fast_interval_seconds=0.0,
        )

    assert exc_info.value.code != 0
    assert any(
        r.levelname == "ERROR" and "fatal tracker error" in r.getMessage().lower()
        for r in caplog.records
    )


# --- cursor replay-safety across a simulated crash -------------------------


def test_cursor_advances_only_past_terminally_decided_events() -> None:
    # A decided (older) then B undecided (newer): cursor stops before B.
    events = FakeEvents(
        [
            _event("A", offset_s=1, updated_from={"assigneeId": ASSIGNEE_ID}),
            _event("B", offset_s=2, updated_from={"assigneeId": ASSIGNEE_ID}),
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store, decided=["A"])

    watcher.tick()

    # Cursor pinned to B's received_at (the oldest still-undecided event).
    assert store.load() == (_T0 + timedelta(seconds=2)).isoformat()


def test_cursor_replay_safety_across_a_simulated_crash() -> None:
    events_list = [
        _event("A", offset_s=1, updated_from={"assigneeId": ASSIGNEE_ID}),
        _event("B", offset_s=2, updated_from={"assigneeId": ASSIGNEE_ID}),
    ]
    store = InMemoryCursorStore()  # survives the "crash" (durable in prod)

    # Tick 1: A reaches a terminal claim decision, B does not.
    w1 = _watcher(FakeEvents(events_list), FakePoll([]), store, decided=["A"])
    first = w1.tick()
    assert [c.item_id for c in first] == ["A", "B"]

    # Crash + restart: a brand-new watcher instance reading the same cursor.
    # B (undecided) is re-processed; A (decided, below the cursor) is not.
    w2 = _watcher(FakeEvents(events_list), FakePoll([]), store, decided=["A"])
    second = w2.tick()
    assert [c.item_id for c in second] == ["B"]


def test_all_decided_advances_strictly_past_the_newest_event() -> None:
    events_list = [
        _event("A", offset_s=1, updated_from={"assigneeId": ASSIGNEE_ID}),
        _event("B", offset_s=2, updated_from={"assigneeId": ASSIGNEE_ID}),
    ]
    store = InMemoryCursorStore()
    w1 = _watcher(FakeEvents(events_list), FakePoll([]), store, decided=["A", "B"])
    first = w1.tick()
    assert [c.item_id for c in first] == ["A", "B"]

    # Everything decided -> cursor strictly past the newest received_at, so the
    # inclusive `received_at >= since` read does NOT re-fetch the boundary event.
    newest = (_T0 + timedelta(seconds=2)).isoformat()
    assert store.load() > newest

    # A second tick (crash/restart, same durable cursor) re-emits nothing —
    # the decided boundary event is not re-surfaced forever.
    w2 = _watcher(FakeEvents(events_list), FakePoll([]), store, decided=["A", "B"])
    assert w2.tick() == []


# --- N4: non-kew events don't pin the cursor and cost zero live fetches ----


def test_human_assignee_event_is_not_emitted_and_advances_the_cursor() -> None:
    """A human-assignee change is decided locally (N4): no candidate, no fetch.

    The event carries a current assignee that is not the kew seat. The watcher
    must neither emit it (a candidate would trigger a live GraphQL fetch in
    ``process_candidate``) nor let it pin the cursor — otherwise every later
    event re-reads per 2s tick in an unbounded query storm.
    """
    human = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    events = FakeEvents(
        [
            _event(
                "H",
                offset_s=1,
                updated_from={"assigneeId": ASSIGNEE_ID},
                data={"id": "H", "assigneeId": human},
            )
        ]
    )
    poll = FakePoll([])
    store = InMemoryCursorStore()
    # No item is ever terminally decided: if the local filter didn't fire the
    # event would pin the cursor forever.
    watcher = _watcher(events, poll, store)

    candidates = watcher.tick()

    assert candidates == []  # not emitted -> zero live fetches downstream
    # Cursor advanced strictly past the event: it is decided, not pending.
    assert store.load() > (_T0 + timedelta(seconds=1)).isoformat()

    # A second tick (same durable cursor) re-surfaces nothing — no storm.
    assert _watcher(FakeEvents(events.events), FakePoll([]), store).tick() == []


def test_kew_assignee_event_still_emitted_and_pins_until_decided() -> None:
    """An event assigned TO kew is undecidable locally — emitted, pins cursor."""
    events = FakeEvents(
        [
            _event(
                "K",
                offset_s=1,
                updated_from={"assigneeId": "old"},
                data={"id": "K", "assigneeId": ASSIGNEE_ID},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store)  # nothing decided yet

    assert watcher.tick() == [Candidate("K", "local")]
    # Undecided kew work: cursor stays at the event so a crash re-reads it.
    assert store.load() == (_T0 + timedelta(seconds=1)).isoformat()


def test_label_change_is_never_decided_locally() -> None:
    """A label change could add the kew trigger label — never decided locally.

    Even when the current assignee is a human, a label mutation might be the
    kew label being added, so the event must fall through to the live fetch
    (emitted) rather than being dropped on the assignee alone.
    """
    human = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    events = FakeEvents(
        [
            _event(
                "L",
                offset_s=1,
                updated_from={"labelIds": ["old"]},
                data={"id": "L", "assigneeId": human, "labelIds": ["new"]},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store)

    assert watcher.tick() == [Candidate("L", "local")]


# --- N4 with a resolved trigger-label UUID (the label-decidable path) ------

_HUMAN = "99999999-aaaa-bbbb-cccc-dddddddddddd"


def test_human_assignee_on_labeled_issue_decided_with_label_id() -> None:
    """The N4 core: human-assignee churn on a *labeled* issue is now decided.

    The old assignee-only filter could never rule on a labeled event (its
    ``labelIds`` were always non-empty), so every human-assignee change on any
    labeled issue pinned the cursor and cost a live fetch every tick. With the
    trigger label's UUID resolved, an issue whose current labels do NOT include
    it and whose assignee is a human is decided not-for-kew locally.
    """
    events = FakeEvents(
        [
            _event(
                "H",
                offset_s=1,
                updated_from={"assigneeId": "old"},
                data={"id": "H", "assigneeId": _HUMAN, "labelIds": ["some-other"]},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store, label_id=LABEL_ID)

    assert watcher.tick() == []  # decided -> not emitted, zero live fetches
    assert store.load() > (_T0 + timedelta(seconds=1)).isoformat()
    # Crash/restart on the same durable cursor re-surfaces nothing (no storm).
    assert _watcher(FakeEvents(events.events), FakePoll([]), store).tick() == []


def test_trigger_label_present_is_still_emitted_with_label_id() -> None:
    """An issue carrying the trigger label is kept even under a human assignee."""
    events = FakeEvents(
        [
            _event(
                "K",
                offset_s=1,
                updated_from={"assigneeId": "old"},
                data={"id": "K", "assigneeId": _HUMAN, "labelIds": [LABEL_ID]},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store, label_id=LABEL_ID)

    assert watcher.tick() == [Candidate("K", "local")]
    # Undecidable-as-not-ours -> pins the cursor until a claim decides it.
    assert store.load() == (_T0 + timedelta(seconds=1)).isoformat()


def test_unassigned_unlabeled_is_decided_with_label_id() -> None:
    """assignee cleared and no labels: provably not-for-kew, decided locally."""
    events = FakeEvents(
        [
            _event(
                "U",
                offset_s=1,
                updated_from={"assigneeId": "old"},
                data={"id": "U", "assigneeId": None, "labelIds": []},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store, label_id=LABEL_ID)

    assert watcher.tick() == []
    assert store.load() > (_T0 + timedelta(seconds=1)).isoformat()


def test_kew_assignee_kept_even_with_label_id() -> None:
    """A kew-assigned issue is kept regardless of labels (never dropped)."""
    events = FakeEvents(
        [
            _event(
                "K",
                offset_s=1,
                updated_from={"assigneeId": "old"},
                data={"id": "K", "assigneeId": ASSIGNEE_ID, "labelIds": []},
            )
        ]
    )
    store = InMemoryCursorStore()
    watcher = _watcher(events, FakePoll([]), store, label_id=LABEL_ID)

    assert watcher.tick() == [Candidate("K", "local")]


# --- durable meta-backed cursor store --------------------------------------


class FakeMetaStore:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.kv.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.kv[key] = value


def test_meta_cursor_store_round_trips_through_the_backing_store() -> None:
    backing = FakeMetaStore()
    cursor = MetaCursorStore(backing)

    assert cursor.load() is None
    cursor.store("2026-07-21T12:00:00+00:00")
    assert cursor.load() == "2026-07-21T12:00:00+00:00"
    # Persisted under a namespaced key, not clobbering unrelated meta.
    assert list(backing.kv) == ["delegation_watch_cursor"]
