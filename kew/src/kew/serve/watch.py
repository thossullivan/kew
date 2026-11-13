"""Shared per-epic poller + SSE fan-out (#454 Decision 1 — also the
transport #453's live tiles will reuse).

Pattern is deliberately NEW, not `api_runs.py`'s request-scoped
store-per-request: no serve code reads the DB inside a stream generator
before this module existed. Instead, one poller task runs per epic
(shared across every subscriber, so tab count doesn't multiply DB
pressure) and each poll runs entirely inside ONE worker thread via
`anyio.to_thread.run_sync` — the injected fetcher opens a fresh store,
queries it, and closes it there (SQLite's `check_same_thread` guard
holds: the connection is created and used in the same thread). This
module never imports `kew.run_store` or `kew.service` directly; callers
inject a `fetch_factory` that closes over those.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio.to_thread

_logger = logging.getLogger(__name__)

POLL_S = 1.0
MAX_STREAM_SECONDS = 1800.0
# Bound on each subscriber's frame queue. On overflow the oldest frame is
# dropped in favor of the newest (latest-wins): snapshots are idempotent
# full-state, reconnects may skip intermediate frames, and the stream makes
# no gap-free claim — history replay is the /ticks endpoint's job.
QUEUE_MAX = 256

# One poll's worth of new frames for a given cursor: (new_cursor, frames).
FrameFetcher = Callable[
    [dict[str, Any]], tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]
]

# Queue item: a (frame_type, payload) pair, or None — the poller-death
# sentinel telling the stream generator to end cleanly. The client's
# EventSource auto-retry then re-subscribes, which builds a fresh poller
# (the self-heal path).
_QueueItem = tuple[str, dict[str, Any]] | None
_FrameQueue = asyncio.Queue[_QueueItem]


def _frame(ftype: str, payload: dict[str, Any]) -> str:
    # Data-only: no "event:" line. EventSource.onmessage only fires for
    # unnamed "message" events, and the frontend's api.ts dispatches on the
    # "type" field that rides inside the JSON payload, not the SSE event name.
    return f"data: {json.dumps({'type': ftype, 'data': payload})}\n\n"


class _Poller:
    """One epic's shared poll loop. Runs while >=1 subscriber remains."""

    def __init__(
        self,
        fetch: FrameFetcher,
        poll_s: float,
        on_death: Callable[[], None],
    ) -> None:
        self.fetch = fetch
        self.poll_s = poll_s
        self.on_death = on_death
        self.subscribers: set[_FrameQueue] = set()
        self.cursor: dict[str, Any] = {}
        self.task: asyncio.Task[None] | None = None
        self.latest: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _offer(q: _FrameQueue, item: _QueueItem) -> None:
        # Latest-wins on overflow: drop the oldest queued frame to make
        # room. Safe because snapshots are idempotent full-state and the
        # stream makes no gap-free claim (see QUEUE_MAX comment).
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            q.get_nowait()
            q.put_nowait(item)

    async def run(self) -> None:
        while self.subscribers:
            try:
                self.cursor, frames = await anyio.to_thread.run_sync(
                    self.fetch, self.cursor
                )
            except Exception:
                # Self-healing death path: an unhandled fetch error must not
                # leave a dead poller registered with subscribers waiting
                # blind. End every stream (None sentinel), deregister via
                # on_death, and exit; the next subscribe builds a fresh one.
                _logger.warning(
                    "epic poller fetch failed; ending streams", exc_info=True
                )
                for q in list(self.subscribers):
                    self._offer(q, None)
                self.on_death()
                return
            for ftype, payload in frames:
                # Re-insert, never update in place: `latest` is replayed in
                # insertion order, so it must be the order the frames were
                # last EMITTED. A dict update keeps a key's first slot, and a
                # frame that embeds another kind's state (a snapshot carrying
                # the needs-you join) would then replay after the newer frame
                # that superseded it. Emission order is what live subscribers
                # already applied, so a reconnect settles where they did.
                self.latest.pop(ftype, None)
                self.latest[ftype] = payload
                for q in list(self.subscribers):
                    self._offer(q, (ftype, payload))
            await asyncio.sleep(self.poll_s)


class EpicWatchHub:
    """Registry of per-epic pollers.

    Not thread-safe by design: every call happens on the event loop (route
    handlers and stream generators), never off it.
    """

    def __init__(
        self,
        fetch_factory: Callable[[str], FrameFetcher],
        poll_s: float = POLL_S,
    ) -> None:
        self._fetch_factory = fetch_factory
        self._poll_s = poll_s
        self._pollers: dict[str, _Poller] = {}

    def poller_count(self) -> int:
        return len(self._pollers)

    def subscribe(
        self, epic_ref: str
    ) -> tuple[_FrameQueue, list[tuple[str, dict[str, Any]]]]:
        poller = self._pollers.get(epic_ref)
        if poller is None:
            # on_death deregisters THIS poller only if it is still the
            # registered one, so it can never clobber a replacement poller
            # created after (e.g.) a last-unsubscribe already dropped it.
            holder: list[_Poller] = []

            def _on_death() -> None:
                if holder and self._pollers.get(epic_ref) is holder[0]:
                    self._pollers.pop(epic_ref, None)

            poller = _Poller(
                self._fetch_factory(epic_ref), self._poll_s, on_death=_on_death
            )
            holder.append(poller)
            self._pollers[epic_ref] = poller
        q: _FrameQueue = asyncio.Queue(maxsize=QUEUE_MAX)
        poller.subscribers.add(q)
        if poller.task is None or poller.task.done():
            poller.task = asyncio.get_running_loop().create_task(poller.run())
        # Reconnects immediately re-receive the latest frame of each type,
        # in the order those frames were last emitted, so a fresh tab isn't
        # blank until the next poll tick and settles on the newest reading.
        return q, list(poller.latest.items())

    def unsubscribe(self, epic_ref: str, q: _FrameQueue) -> None:
        poller = self._pollers.get(epic_ref)
        if poller is None:
            return
        poller.subscribers.discard(q)
        if not poller.subscribers:
            # Last unsubscribe: the poller's `while self.subscribers` loop
            # will see an empty set and exit on its own within one poll
            # (or immediately, if it's mid-sleep); drop the registry entry
            # now so a fresh subscribe() spins up a brand new poller/task
            # rather than racing the old one's shutdown.
            self._pollers.pop(epic_ref, None)


async def epic_stream(
    hub: EpicWatchHub,
    epic_ref: str,
    *,
    is_authorized: Callable[[], bool],
    poll_s: float = POLL_S,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[str]:
    """SSE body for one client connection to one epic.

    Re-receives the latest frame of each type immediately (so a reconnect
    via EventSource's auto-retry isn't blank), then live frames as the
    shared poller emits them. Authorization is re-checked every iteration
    so a revoked session under `--expose` terminates the stream within
    ~poll_s. An absolute `max_seconds` cap bounds the connection even if a
    client never disconnects; the browser's EventSource auto-retry makes
    the resulting reconnect invisible to the user.
    """
    q, latest = hub.subscribe(epic_ref)
    # Wall-clock deadline: the cap is truly absolute (~30 min default),
    # not an idle timeout or an iteration-count approximation.
    deadline = time.monotonic() + max_seconds
    try:
        for ftype, payload in latest:
            yield _frame(ftype, payload)
        while time.monotonic() < deadline:
            if not is_authorized():
                return
            try:
                item = await asyncio.wait_for(q.get(), timeout=poll_s)
            except TimeoutError:
                continue
            if item is None:  # poller died — end cleanly; auto-retry self-heals
                return
            ftype, payload = item
            yield _frame(ftype, payload)
    except asyncio.CancelledError:  # client disconnected
        return
    finally:
        hub.unsubscribe(epic_ref, q)
