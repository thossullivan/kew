"""Read a kew serve stream the way a same-host reverse proxy relays it.

The proxy reaches kew from loopback and adds a forwarding header. A stream
that never re-checks the reader's session would follow its source until the
route's 30-minute cap, so the read is bounded here: a stream that is still
open at ``deadline_s`` fails the caller's test instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import httpx

_LOOPBACK_PEER = ("127.0.0.1", 50000)
_PROXY_HEADERS = {"X-Forwarded-For": "203.0.113.9"}


def session_clock(live_calls: int, *, expired_at: float) -> Any:
    """A ``SessionStore`` clock that reads 0.0 for ``live_calls`` reads, then
    ``expired_at`` forever: the session lives through sign-in and the opening
    gate, then runs out while the stream is open."""
    ticks: Iterator[float] = iter([0.0] * live_calls)
    return lambda: next(ticks, expired_at)


async def read_proxied_stream(
    app: Any, path: str, *, session_id: str, deadline_s: float = 10.0
) -> tuple[httpx.Response, float]:
    """The whole response body and how long the stream stayed open."""
    transport = httpx.ASGITransport(app=app, client=_LOOPBACK_PEER)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
        cookies={"kew_session": session_id},
    ) as client:
        started = time.monotonic()
        response = await asyncio.wait_for(
            client.get(path, headers=_PROXY_HEADERS), timeout=deadline_s
        )
        return response, time.monotonic() - started
