"""Tail a run's .events.jsonl as a Server-Sent Events stream.

Follows appends until the run emits a terminal (finished/failed) event, a safety
cap elapses, or the client disconnects (CancelledError). It does NOT give up on
idle — live agents routinely pause several seconds between tool calls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


def _always_authorized() -> bool:
    return True


def _is_terminal(line: str) -> bool:
    # Parse JSON so a nested or quoted "type":"failed" in a payload value
    # cannot falsely trigger stream termination — only the top-level type field
    # on the event object itself ends the stream.
    try:
        return json.loads(line).get("type") in ("finished", "failed")
    except (json.JSONDecodeError, AttributeError):
        return False


def _drain(path: Path, pos: int) -> tuple[list[str], int, bool]:
    """Read new whole lines from `pos`. Returns (frames, new_pos, hit_terminal)."""
    frames: list[str] = []
    terminal = False
    with path.open() as fh:
        fh.seek(pos)
        lines = fh.readlines()  # read-to-EOF; tell() after a for-iter is illegal
        new_pos = fh.tell()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        frames.append(f"data: {line}\n\n")
        if _is_terminal(line):
            terminal = True
            break
    return frames, new_pos, terminal


async def event_stream(
    event_file: Path,
    *,
    replay: bool = True,
    poll_s: float = 0.5,
    max_seconds: float = 1800.0,
    is_authorized: Callable[[], bool] = _always_authorized,
) -> AsyncIterator[str]:
    path = Path(event_file)
    pos = path.stat().st_size if (not replay and path.exists()) else 0
    waited = 0.0
    try:
        while waited <= max_seconds:
            if not is_authorized():
                return
            if path.exists() and path.stat().st_size > pos:
                frames, pos, terminal = _drain(path, pos)
                for frame in frames:
                    yield frame
                if terminal:
                    return
                waited = 0.0
            else:
                await asyncio.sleep(poll_s)
                waited += poll_s
    except asyncio.CancelledError:  # client disconnected
        return


def _journal_frame(event: Mapping[str, Any]) -> str:
    """One SSE frame, with the ledger position as the SSE event id.

    The id is what a browser sends back as ``Last-Event-ID`` after a dropped
    connection, so making it the ``audit_events`` row id is what lets a
    reconnect — or a reconnect across a server restart — resume at the next
    fact instead of replaying the run from the beginning.
    """
    return f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n"


async def journal_stream(
    fetch: Callable[[int], Sequence[Mapping[str, Any]]],
    *,
    after_seq: int = 0,
    poll_s: float = 0.5,
    max_seconds: float = 1800.0,
    is_authorized: Callable[[], bool] = _always_authorized,
) -> AsyncIterator[str]:
    """Tail one run's evidence journal as SSE, resuming after *after_seq*.

    ``fetch`` is a blocking read of durable rows after a cursor; it runs in a
    worker thread so a poll never blocks the event loop, and it opens and
    closes its own store there rather than sharing a connection across threads.

    The stream ends when the run's evidence is closed by a terminal
    attestation, when the safety cap elapses, when the client disconnects, or
    — as with :func:`event_stream` — the moment authorization stops holding:
    a session can expire or be revoked during a connection that may last half
    an hour, so every poll re-checks rather than trusting the opening gate.
    """
    cursor = after_seq
    waited = 0.0
    try:
        while waited <= max_seconds:
            if not is_authorized():
                return
            reached = cursor
            for event in await asyncio.to_thread(fetch, cursor):
                reached = max(reached, int(event["seq"]))
                yield _journal_frame(event)
                if event.get("terminal"):
                    return
            # Sleep unless the cursor actually MOVED, not merely because rows
            # came back. A fetch that re-served what we already sent would
            # otherwise reset the idle timer forever and poll the store in a
            # loop, so advancement — the thing that makes progress — is what
            # counts as activity.
            if reached > cursor:
                cursor, waited = reached, 0.0
                continue
            await asyncio.sleep(poll_s)
            waited += poll_s
    except asyncio.CancelledError:  # client disconnected
        return
