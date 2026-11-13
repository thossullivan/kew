"""Factory health read API (#2298) — ``/api/health`` and ``/api/health/env``,
plus ``/api/readiness`` (#2514), the ``kew doctor`` checks as JSON.

Both routes are read-only, sit behind ``read_guard``, and make no GitHub call:
the tracker reading is whatever the serve sweep already cached in the ledger,
and the host probes (container runtime, image presence) sit behind the reader's
TTL cache, which the sweep warms. A tab left open on ``/health`` therefore costs
one SQLite read per refresh and nothing else.

Neither response carries the value of any environment variable. Credentials and
env vars travel as ``{name, set}`` — ``tests/test_serve_api_health.py`` exports a
sentinel secret and greps the whole payload for it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from kew.readiness import CheckResult, readiness_report
from kew.serve.security import read_guard
from kew.service.health import HealthReader, env_report

if TYPE_CHECKING:
    from kew.service.health import ProbeCache

health_router = APIRouter(prefix="/api/health", tags=["health"])
readiness_router = APIRouter(
    prefix="/api/readiness", tags=["health"], dependencies=[Depends(read_guard)]
)

#: How long one run of the doctor checks answers ``/api/readiness``.
READINESS_TTL_SECONDS = 60.0


def health_reader(request: Request) -> HealthReader:
    """The app's health reader, created on first use.

    Public because the dispatch launchpad reads through it too (#2302): the
    probe cache behind it is the factory's one answer to "is this image here,
    is this credential set", and a second cache would let the two pages
    disagree about the same host.

    Held on ``app.state`` rather than rebuilt per request so the probe cache
    survives between reads — a reader rebuilt each time would re-probe docker
    for every image on every refresh, which is the cost this cache exists to
    avoid.

    The lazy create is deliberately unlocked. Two concurrent first requests can
    build two readers and one loses; the cost is one extra round of probes, and
    nothing here fronts a verification, so a duplicate cannot produce a wrong
    answer. ``kew serve`` injects a reader at startup, so this path is the
    sweep-less case (every test, and a serve started without one).
    """
    reader = getattr(request.app.state, "health_reader", None)
    if reader is None:
        reader = HealthReader()
        request.app.state.health_reader = reader
    return reader


def _probe_cache(request: Request) -> ProbeCache | None:
    return health_reader(request).cache


def _sweep_health(request: Request) -> dict[str, Any] | None:
    """The sweep worker's own liveness, or None when serve runs without one."""
    sweep = getattr(request.app.state, "sweep", None)
    if sweep is None:
        return None
    return dict(sweep.health())


def _epic_pollers(request: Request) -> int | None:
    """How many epic SSE pollers are live, or None when there is no hub."""
    hub = getattr(request.app.state, "epic_hub", None)
    if hub is None:
        return None
    count = getattr(hub, "poller_count", None)
    return count() if callable(count) else None


@health_router.get("", dependencies=[Depends(read_guard)])
def get_health(request: Request) -> dict[str, Any]:
    """Can the factory work? Runtimes, images, posture, runners, daemons."""
    with request.app.state.store_factory() as store:
        return health_reader(request).read(
            store,
            request.app.state.config,
            sweep=_sweep_health(request),
            epic_pollers=_epic_pollers(request),
        )


@health_router.get("/env", dependencies=[Depends(read_guard)])
def get_health_env(request: Request) -> dict[str, Any]:
    """``[{name, purpose, required_by, set}]``, as seen by the serve process.

    The label matters as much as the list: these are the variables THIS process
    inherited, which is not the same set a driver started from a different shell
    sees — an operator debugging "but I exported it" needs to be told whose
    environment is being described.
    """
    return {
        "as_seen_by": "the serve process",
        "vars": env_report(_probe_cache(request)),
    }


ReadinessChecks = Callable[[], list[CheckResult]]
"""``kew doctor``'s ``run_checks`` bound to the serve config.

Injected by ``kew serve`` rather than imported: ``kew.cli.doctor`` reaches
``kew.graph``, which ``kew.serve`` must never import, and the console must read
the doctor's own checks rather than re-implement any of them.
"""


class ReadinessCache:
    """One run of the doctor checks, served for ``ttl`` seconds.

    The lock is held across the run, so requests that arrive while the checks
    are running wait for that run instead of starting their own: the checks
    probe docker, gh and every runner, and a console polling from several tabs
    must not multiply that. A run that raises caches nothing.
    """

    def __init__(
        self,
        checks: ReadinessChecks,
        *,
        ttl: float = READINESS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._checks = checks
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._report: dict[str, Any] | None = None
        self._ran_at = 0.0

    def read(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._report is None or now - self._ran_at >= self._ttl:
                results = self._checks()
                self._report = {
                    **readiness_report(results),
                    "checked_at": datetime.now(UTC).isoformat(),
                }
                self._ran_at = now
            return dict(self._report)


@readiness_router.get("")
def get_readiness(request: Request) -> dict[str, Any]:
    """The ``kew doctor --json`` document, at most 60 seconds old.

    Read-only: it reports each check's ``fix`` command and never runs one.
    """
    cache: ReadinessCache | None = getattr(request.app.state, "readiness_cache", None)
    if cache is None:
        raise HTTPException(
            status_code=503, detail="readiness checks are not wired into this server"
        )
    return cache.read()
