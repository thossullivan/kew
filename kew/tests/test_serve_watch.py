"""Tests for the shared per-epic poller + SSE fan-out (#454 Phase B, Task B2).

All tests use a fake FrameFetcher (no DB) except
test_fetch_runs_in_one_worker_thread, which uses a real SqliteRunStore to
prove the fetcher's open/query/close all happen in the same worker thread,
distinct from the event-loop thread.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable
from typing import Any

import pytest

from kew.run_store import SqliteRunStore
from kew.serve.watch import QUEUE_MAX, EpicWatchHub, epic_stream

POLL_S = 0.01


def _always_authorized() -> bool:
    return True


class _ScriptedFetcher:
    """A FrameFetcher whose return values are pulled from a fixed script.

    Each call pops the next (cursor, frames) pair; once the script is
    exhausted, the last entry repeats forever (steady state).
    """

    def __init__(
        self,
        script: list[tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]],
    ):
        self.script = script
        self.calls = 0

    def __call__(
        self, cursor: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll `predicate` until true or raise on timeout. Avoids bare sleeps."""

    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_spin(), timeout=timeout)


# Real wall clock (2 * POLL_S + 1 s). Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_new_frame_fans_out_within_poll_s() -> None:
    fetcher = _ScriptedFetcher(
        [
            ({"tick_id": 0}, []),
            ({"tick_id": 1}, [("snapshot", {"tick_id": 1})]),
        ]
    )
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)
    q, latest = hub.subscribe("e1")
    assert latest == []

    ftype, payload = await asyncio.wait_for(q.get(), timeout=2 * POLL_S + 1.0)
    assert ftype == "snapshot"
    assert payload == {"tick_id": 1}

    hub.unsubscribe("e1", q)


@pytest.mark.asyncio
async def test_no_change_no_frame() -> None:
    fetcher = _ScriptedFetcher([({}, [])])
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)
    q, _latest = hub.subscribe("e1")

    # Let several polls happen, then assert nothing landed in the queue.
    await _wait_until(lambda: fetcher.calls >= 5)
    assert q.empty()

    hub.unsubscribe("e1", q)


@pytest.mark.asyncio
async def test_one_poller_per_epic_and_stop_on_last_unsubscribe() -> None:
    fetcher = _ScriptedFetcher([({}, [])])
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)

    q1, _ = hub.subscribe("e1")
    q2, _ = hub.subscribe("e1")
    assert hub.poller_count() == 1

    await _wait_until(lambda: fetcher.calls >= 2)
    calls_with_two_subs = fetcher.calls

    hub.unsubscribe("e1", q1)
    assert hub.poller_count() == 1  # one subscriber remains

    hub.unsubscribe("e1", q2)
    # Registry entry drops immediately on last unsubscribe.
    assert hub.poller_count() == 0

    # The underlying poll loop should stop making new fetch calls shortly
    # after (it exits on its next `while self.subscribers` check).
    await asyncio.sleep(3 * POLL_S)
    calls_after_stop = fetcher.calls
    await asyncio.sleep(3 * POLL_S)
    assert fetcher.calls == calls_after_stop
    assert calls_after_stop >= calls_with_two_subs


@pytest.mark.asyncio
async def test_reconnect_resends_latest_frames() -> None:
    fetcher = _ScriptedFetcher(
        [
            ({"tick_id": 0}, []),
            (
                {"tick_id": 1},
                [
                    ("snapshot", {"tick_id": 1}),
                    ("heartbeat", {"tick_count": 1}),
                    ("economics", {"drain_total_usd": 1.5}),
                ],
            ),
        ]
    )
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)
    q1, _ = hub.subscribe("e1")

    # Wait for all three frame types to have been seen by the poller.
    async def _got_all() -> bool:
        seen: set[str] = set()
        while not q1.empty():
            ftype, _ = q1.get_nowait()
            seen.add(ftype)
        return {"snapshot", "heartbeat", "economics"} <= seen

    await _wait_until(lambda: fetcher.calls >= 2)
    # Drain q1's queue fully (may need a couple of polling iterations).
    for _ in range(50):
        if await _got_all():
            break
        await asyncio.sleep(0.005)

    # A brand-new subscriber immediately gets the latest of each type back
    # from subscribe()'s second return value, without waiting on a poll.
    q2, latest2 = hub.subscribe("e1")
    latest_by_type = dict(latest2)
    assert latest_by_type.get("snapshot") == {"tick_id": 1}
    assert latest_by_type.get("heartbeat") == {"tick_count": 1}
    assert latest_by_type.get("economics") == {"drain_total_usd": 1.5}

    hub.unsubscribe("e1", q1)
    hub.unsubscribe("e1", q2)


@pytest.mark.asyncio
async def test_replay_follows_last_emission_order_not_first_seen() -> None:
    """A kind first seen early but re-emitted later replays after the kinds
    emitted before that re-emission — the order live subscribers applied."""
    fetcher = _ScriptedFetcher(
        [
            ({"n": 1}, [("attention", {"v": 1})]),
            ({"n": 2}, [("snapshot", {"v": 2}), ("attention", {"v": 2})]),
            ({"n": 3}, [("attention", {"v": 3})]),
            ({"n": 3}, []),
        ]
    )
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)
    q1, _ = hub.subscribe("e1")
    await _wait_until(lambda: fetcher.calls >= 4)

    q2, replay = hub.subscribe("e1")

    assert replay == [("snapshot", {"v": 2}), ("attention", {"v": 3})]
    hub.unsubscribe("e1", q1)
    hub.unsubscribe("e1", q2)


# Real wall clock. Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_stream_terminates_on_deauth_within_poll_s() -> None:
    fetcher = _ScriptedFetcher([({}, [])])
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)

    authorized = {"value": True}

    def is_authorized() -> bool:
        return authorized["value"]

    async def _consume() -> list[str]:
        frames = []
        async for frame in epic_stream(
            hub, "e1", is_authorized=is_authorized, poll_s=POLL_S, max_seconds=1800.0
        ):
            frames.append(frame)
        return frames

    task = asyncio.create_task(_consume())
    await asyncio.sleep(2 * POLL_S)
    authorized["value"] = False

    await asyncio.wait_for(task, timeout=2 * POLL_S + 1.0)
    # Stream ended; the poller's subscriber was cleaned up.
    await _wait_until(lambda: hub.poller_count() == 0)


@pytest.mark.asyncio
async def test_stream_frames_are_data_only_json() -> None:
    fetcher = _ScriptedFetcher(
        [
            ({"tick_id": 0}, []),
            (
                {"tick_id": 1},
                [
                    ("snapshot", {"tick_id": 1}),
                    ("heartbeat", {"tick_count": 1}),
                    ("economics", {"drain_total_usd": 1.5}),
                ],
            ),
        ]
    )
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)

    frames: list[str] = []

    async def _consume() -> None:
        async for frame in epic_stream(
            hub,
            "e1",
            is_authorized=_always_authorized,
            poll_s=POLL_S,
            max_seconds=1800.0,
        ):
            frames.append(frame)
            if len(frames) >= 3:
                return

    await asyncio.wait_for(_consume(), timeout=5.0)

    assert len(frames) >= 3
    pattern = re.compile(r"^data: \{.*\}\n\n$", re.DOTALL)
    for frame in frames:
        assert pattern.match(frame), frame
        assert "event:" not in frame
        payload = json.loads(frame[len("data: ") : -2])
        assert payload["type"] in {"snapshot", "economics", "heartbeat"}
        assert "data" in payload


@pytest.mark.asyncio
async def test_stream_ends_at_max_seconds_cap() -> None:
    fetcher = _ScriptedFetcher([({}, [])])  # silent: never emits a frame
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)

    frames = [
        frame
        async for frame in epic_stream(
            hub,
            "e1",
            is_authorized=_always_authorized,
            poll_s=POLL_S,
            max_seconds=3 * POLL_S,
        )
    ]
    assert frames == []
    assert hub.poller_count() == 0

    # A fresh call (simulating EventSource auto-retry) immediately
    # re-receives the latest frames — none in this silent-fetcher case, but
    # the important thing is it doesn't error and starts a new poller.
    second_frames: list[str] = []

    async def _consume_second() -> None:
        async for frame in epic_stream(
            hub,
            "e1",
            is_authorized=_always_authorized,
            poll_s=POLL_S,
            max_seconds=2 * POLL_S,
        ):
            second_frames.append(frame)

    await asyncio.wait_for(_consume_second(), timeout=5.0)
    assert second_frames == []


@pytest.mark.asyncio
async def test_fetch_runs_in_one_worker_thread(tmp_path: Any) -> None:
    idents: list[int] = []
    event_loop_ident = threading.get_ident()

    class _RecordingStore(SqliteRunStore):
        def __init__(self, db_path: str) -> None:
            idents.append(threading.get_ident())
            super().__init__(db_path)

        def query(self) -> list[str]:
            idents.append(threading.get_ident())
            return self.list_epic_refs()

        def close(self) -> None:
            idents.append(threading.get_ident())
            super().close()

    db_path = str(tmp_path / "watch_thread_test.db")

    def fetch_factory(
        _epic_ref: str,
    ) -> Callable[
        [dict[str, Any]], tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]
    ]:
        def fetch(
            cursor: dict[str, Any],
        ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
            store = _RecordingStore(db_path)
            try:
                store.query()
            finally:
                store.close()
            return cursor, []

        return fetch

    hub = EpicWatchHub(fetch_factory, poll_s=POLL_S)
    q, _latest = hub.subscribe("e1")

    await _wait_until(lambda: len(idents) >= 3)
    hub.unsubscribe("e1", q)

    open_ident, query_ident, close_ident = idents[0], idents[1], idents[2]
    assert open_ident == query_ident == close_ident
    assert open_ident != event_loop_ident


@pytest.mark.asyncio
async def test_fetch_exception_ends_streams_and_self_heals() -> None:
    class _DyingFetcher:
        """Returns nothing on the first call, raises on every call after."""

        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self, cursor: dict[str, Any]
        ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("db exploded")
            return cursor, []

    healthy = _ScriptedFetcher([({}, [("snapshot", {"healed": True})])])
    fetchers: list[Any] = [_DyingFetcher(), healthy]

    def fetch_factory(_epic_ref: str) -> Any:
        return fetchers.pop(0)

    hub = EpicWatchHub(fetch_factory, poll_s=POLL_S)

    async def _consume() -> list[str]:
        return [
            frame
            async for frame in epic_stream(
                hub,
                "e1",
                is_authorized=_always_authorized,
                poll_s=POLL_S,
                max_seconds=1800.0,
            )
        ]

    task = asyncio.create_task(_consume())
    # The fetch raises on its second call (~poll_s in); the death sentinel
    # must end the active stream promptly — well under the 30 min cap.
    frames = await asyncio.wait_for(task, timeout=2.0)
    assert frames == []  # stream ended cleanly, no frames, no exception

    # The dead poller deregistered itself (stream's unsubscribe is a no-op).
    assert hub.poller_count() == 0

    # A new subscribe (the EventSource auto-retry) builds a fresh, working
    # poller from the factory — the self-heal path.
    q2, _latest = hub.subscribe("e1")
    assert hub.poller_count() == 1
    item = await asyncio.wait_for(q2.get(), timeout=2.0)
    assert item == ("snapshot", {"healed": True})
    hub.unsubscribe("e1", q2)


@pytest.mark.asyncio
async def test_queue_bounded_latest_wins() -> None:
    # Each poll emits well over QUEUE_MAX frames; the subscriber never
    # drains. The queue must stay bounded and keep the NEWEST frames
    # (latest-wins drop-oldest), ending with the newest of all.
    total = QUEUE_MAX + 50
    burst = [("snapshot", {"n": i}) for i in range(total)]
    fetcher = _ScriptedFetcher([({}, []), ({}, burst)])
    hub = EpicWatchHub(lambda _ref: fetcher, poll_s=POLL_S)
    q, _latest = hub.subscribe("e1")

    await _wait_until(lambda: q.qsize() >= QUEUE_MAX)
    hub.unsubscribe("e1", q)

    assert q.qsize() <= QUEUE_MAX
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert len(items) == QUEUE_MAX
    # Oldest frames were dropped; the last queued frame is the newest one.
    assert items[-1] == ("snapshot", {"n": total - 1})
    assert items[0] == ("snapshot", {"n": total - QUEUE_MAX})
