"""Two-cadence delegation watcher (D3).

Each tick unions two independent sources into a deduped stream of fresh
candidate issue ids:

  (a) a **fast** read of ``tracker_events`` rows newer than a stored cursor,
      filtered to ``Issue`` events whose action touches assignee/labels — the
      low-latency local path fed by inbound Linear webhooks; and
  (b) a **throttled** (``poll_interval_seconds``) ``list_delegated`` call against
      Linear — the poll floor that still surfaces work when the webhook tunnel
      is down and no ``tracker_events`` arrive.

The two cadences are deliberately independent: a remote-poll failure never
starves the fast local read, and an empty local read never suppresses the poll
floor. Union → dedup by item id → emit. The watcher **only emits candidates**;
recognition (D2) filters them and claim (D4) decides — no dispatch happens here.

Cursor advancement is crash-safe. The cursor advances only *past* events whose
issue has reached a terminal claim decision, stopping at the oldest still-
undecided event. A crash therefore re-reads (and re-emits) undecided events
while never re-emitting decided ones — mirroring how epic-driver reservations
survive a restart. The terminal-decision test is injected (``is_terminally_
decided``) so this module stays decoupled from claim internals.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from kew.delegate.errors import FatalTrackerError
from kew.run_store import TrackerEvent

_logger = logging.getLogger(__name__)

# Oldest cursor value — used when no cursor has been stored yet, so the first
# tick reads every retained tracker_events row.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Namespaced meta key for the durable watch cursor (MetaCursorStore).
_CURSOR_META_KEY = "delegation_watch_cursor"

# Linear webhook ``updatedFrom`` keys that mark an assignee/label change — the
# only field mutations that make an Issue update a delegation trigger.
_ASSIGNEE_LABEL_KEYS = ("assigneeId", "labelIds")


@dataclass(frozen=True)
class Candidate:
    """A fresh candidate issue id emitted for recognition/claim downstream.

    ``item_id`` is the canonical Linear identifier (for example ``SFD-12``).
    ``source`` is provenance for logging:
    ``"local"`` (fast tracker_events read) or ``"poll"`` (throttled poll floor).
    """

    item_id: str
    source: str


class _LocalEvents(Protocol):
    """The one run_store accessor the fast local read depends on."""

    def list_tracker_events_since(self, since: datetime) -> list[TrackerEvent]: ...


class _DelegatedPoll(Protocol):
    """The one Linear adapter method the poll floor depends on."""

    def list_delegated(self, assignee_id: str, label: str) -> list[str]: ...


class CursorStore(Protocol):
    """Durable store for the replay-safe watch cursor (an ISO received_at)."""

    def load(self) -> str | None: ...
    def store(self, value: str) -> None: ...


class InMemoryCursorStore:
    """Process-local cursor store. Persists across watcher instances within a
    process (used by tests to simulate a crash + restart); production wires a
    durable store (:class:`MetaCursorStore`)."""

    def __init__(self, initial: str | None = None) -> None:
        self._value = initial

    def load(self) -> str | None:
        return self._value

    def store(self, value: str) -> None:
        self._value = value


class _MetaStore(Protocol):
    """The generic run_store meta KV the durable cursor is backed by."""

    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


class MetaCursorStore:
    """Durable cursor backed by the run_store ``meta`` table, so the cursor
    survives a real process crash (the fast-path optimization; correctness
    also holds via the poll floor + claim reservation even without it)."""

    def __init__(self, store: _MetaStore) -> None:
        self._store = store

    def load(self) -> str | None:
        return self._store.get_meta(_CURSOR_META_KEY)

    def store(self, value: str) -> None:
        self._store.set_meta(_CURSOR_META_KEY, value)


def touches_assignee_or_labels(event: TrackerEvent) -> bool:
    """True iff ``event`` is an Issue create/update touching assignee/labels.

    Update triggers require an assignee/label change (per the webhook's
    ``updatedFrom``); a create triggers only when the issue lands already
    assigned or labeled. Everything else — non-Issue entities, removes, and
    updates to unrelated fields (e.g. title) — is not a delegation candidate.
    """
    if event.event_type != "Issue" or event.action not in ("create", "update"):
        return False
    try:
        payload = json.loads(event.payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if event.action == "update":
        updated_from = payload.get("updatedFrom") or {}
        return any(k in updated_from for k in _ASSIGNEE_LABEL_KEYS)
    data = payload.get("data") or {}
    return bool(data.get("assigneeId")) or bool(data.get("labelIds"))


def _parse_cursor(raw: str | None) -> datetime:
    if not raw:
        return _EPOCH
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return _EPOCH


class DelegationWatcher:
    """Unions a fast local tracker_events read with a throttled Linear poll
    into a deduped candidate stream, advancing a replay-safe cursor.

    ``tick()`` is driven by the ``kew delegate watch`` loop (D5) at roughly
    ``fast_interval_seconds``; the remote poll is throttled to ``poll_interval_seconds``
    inside the watcher so the two cadences stay independent.
    """

    def __init__(
        self,
        *,
        events: _LocalEvents,
        poll: _DelegatedPoll,
        cursor_store: CursorStore,
        assignee_id: str,
        label: str,
        is_terminally_decided: Callable[[str], bool],
        poll_interval_seconds: float,
        clock: Callable[[], float],
        label_id: str | None = None,
    ) -> None:
        self._events = events
        self._poll = poll
        self._cursor_store = cursor_store
        self._assignee_id = assignee_id
        self._label = label
        # The trigger label's resolved UUID (N4). When known, the fast local
        # read decides human-assignee churn on a *labeled* issue by comparing
        # payload label UUIDs; None keeps the conservative assignee-only filter.
        self._label_id = label_id
        self._is_terminally_decided = is_terminally_decided
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._last_poll_at: float | None = None

    def tick(self) -> list[Candidate]:
        """Run one tick: fast local read ∪ throttled poll, deduped, cursor
        advanced past terminally-decided events. Returns fresh candidates."""
        cursor = self._cursor_store.load()
        since = _parse_cursor(cursor)
        relevant = [
            e
            for e in self._events.list_tracker_events_since(since)
            if touches_assignee_or_labels(e) and e.entity_id
        ]
        # Emit only events that might concern kew: an event the payload alone
        # proves is not-for-kew (a human-assignee change) is dropped here, so it
        # never becomes a candidate and never triggers a live fetch (N4). Such
        # events are also "decided" for the cursor below, so they can't pin it.
        local_ids = [
            item_id
            for e in relevant
            if not self._locally_not_ours(e)
            if (item_id := _event_item_id(e))
        ]

        poll_ids = self._maybe_poll()
        candidates = _dedup(local_ids, poll_ids)
        self._advance_cursor(relevant)
        return candidates

    def _locally_not_ours(self, event: TrackerEvent) -> bool:
        """True when the event's own payload proves it is not-for-kew (N4).

        Cheap and live-fetch-free. When the trigger label's UUID is resolved
        (``label_id``), a full Linear Issue webhook carries the issue's current
        assignee id AND its full label UUID set, so the decision is direct: the
        event is not-for-kew exactly when the current assignee is not the kew
        seat AND the current labels do not include the trigger label. This
        decides the human-assignee churn on a *labeled* issue that the old
        assignee-only filter could never rule on (its ``labelIds`` were always
        non-empty, so it pinned the cursor and live-fetched every tick), plus
        the unassigned/unlabeled case — without ever dropping a genuine kew
        delegation (a kew assignee or the trigger label short-circuits to keep).

        Without a resolved ``label_id`` it falls back to the conservative
        assignee-only rule: decide not-ours only on a positive non-kew assignee
        with no label in play, and let every label case fall through to the live
        fetch (a label mutation could be adding the kew trigger label, whose
        *name* can't be matched against payload label *UUIDs*).
        """
        try:
            payload = json.loads(event.payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        data = payload.get("data") or {}
        assignee = data.get("assigneeId")
        if self._label_id is not None:
            return self._label_decides_not_ours(data, assignee)
        return self._assignee_only_not_ours(payload, data, assignee)

    def _label_decides_not_ours(
        self, data: dict[str, Any], assignee: str | None
    ) -> bool:
        """Decide not-for-kew from a full webhook payload (label_id known).

        Trigger label present -> could be ours; assigned to the kew seat ->
        ours; otherwise provably neither, so decided not-for-kew (assignee None
        or a human both count as "not the kew seat"). Missing labelIds reads as
        no labels.
        """
        label_ids = data.get("labelIds") or []
        if self._label_id in label_ids:
            return False
        return assignee != self._assignee_id

    def _assignee_only_not_ours(
        self, payload: dict[str, Any], data: dict[str, Any], assignee: str | None
    ) -> bool:
        """Conservative fallback (label_id unresolved): any label in play could
        be the kew trigger label, so those fall through to the live fetch and
        only a positive non-kew assignee with no label decides not-for-kew."""
        updated_from = payload.get("updatedFrom") or {}
        if "labelIds" in updated_from:
            return False
        if data.get("labelIds"):
            return False
        return assignee is not None and assignee != self._assignee_id

    def _maybe_poll(self) -> list[str]:
        now = self._clock()
        due = self._last_poll_at is None or (
            now - self._last_poll_at >= self._poll_interval_seconds
        )
        if not due:
            return []
        self._last_poll_at = now
        try:
            return self._poll.list_delegated(self._assignee_id, self._label)
        except FatalTrackerError:
            # An expired/revoked key is fatal, not a transient blip: it must
            # propagate so the watch loop terminates loudly instead of being
            # swallowed and retried forever, silently doing nothing (Task 3, #971).
            raise
        except Exception:
            # Cadence independence: a down tunnel / unavailable Linear must not
            # starve the fast local read. Next tick re-polls. Log so an operator
            # can tell "nothing delegated" from "Linear polling is failing".
            _logger.warning(
                "delegation poll failed for assignee=%s label=%s; "
                "returning no poll candidates, will retry next interval",
                self._assignee_id,
                self._label,
                exc_info=True,
            )
            return []

    def _advance_cursor(self, relevant: list[TrackerEvent]) -> None:
        new_cursor = self._next_cursor(relevant)
        if new_cursor is None:
            return
        current = self._cursor_store.load()
        if current is None or new_cursor >= current:
            self._cursor_store.store(new_cursor)

    def _next_cursor(self, relevant: list[TrackerEvent]) -> str | None:
        """The cursor after this tick: the oldest still-undecided received_at
        (so a crash re-reads it), or — when every event is decided — strictly
        past the newest event seen. None when there is nothing to advance.

        ``list_tracker_events_since`` uses an inclusive ``received_at >= since``
        bound, so an all-decided tick must land the cursor *one tick past* the
        newest received_at — returning ``max(stamps)`` verbatim would re-fetch
        and re-emit that decided event on every subsequent tick, forever.
        """
        stamps = [e.received_at for e in relevant if e.received_at]
        if not stamps:
            return None
        undecided = [
            e.received_at for e in relevant if e.received_at and self._is_undecided(e)
        ]
        if undecided:
            return min(undecided)
        newest = datetime.fromisoformat(max(stamps))
        return (newest + timedelta(microseconds=1)).isoformat()

    def _is_undecided(self, event: TrackerEvent) -> bool:
        item_id = _event_item_id(event)
        if not item_id:
            return False
        # A locally-decidable not-for-kew event (N4) is decided the moment we
        # read it — it must not pin the cursor waiting on a claim that will never
        # come, or every later event re-reads per tick in an unbounded storm.
        if self._locally_not_ours(event):
            return False
        return not self._is_terminally_decided(item_id)


def _event_item_id(event: TrackerEvent) -> str | None:
    """Return the canonical identifier carried by a Linear webhook event.

    Linear's ``data.id``/``entity_id`` is an internal UUID, while dispatch and
    durable run identity require the human-facing identifier (for example
    ``SFD-12``). Events without that field are left to the mandatory poll floor
    instead of risking a non-canonical dispatch.
    """
    try:
        payload = json.loads(event.payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    identifier = (payload.get("data") or {}).get("identifier")
    return str(identifier) if identifier else None


def _dedup(local_ids: list[str], poll_ids: list[str]) -> list[Candidate]:
    """Union both id lists, first occurrence wins; local provenance precedes
    poll so an id seen in both reads as ``"local"``."""
    seen: set[str] = set()
    out: list[Candidate] = []
    for item_id in local_ids:
        if item_id not in seen:
            seen.add(item_id)
            out.append(Candidate(item_id, "local"))
    for item_id in poll_ids:
        if item_id not in seen:
            seen.add(item_id)
            out.append(Candidate(item_id, "poll"))
    return out
