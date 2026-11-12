"""Serve-side outcome sweep worker (#683, Group 6 Task 1).

A single fail-soft daemon thread run inside the serve lifecycle that
periodically drives outcome sync + orphan reconciliation, so `pr_state`
and the orphan table keep advancing even when no terminal CLI command
runs (driver death would otherwise freeze the journal at its last signal).

Dependency-injected by design: the actual work is a `sweep_once` callable
the caller closes over (`sync_outcomes` + `reconcile_orphan_prs` against a
fresh store). This keeps kew.serve free of any kew.run_store / kew.outcomes
import (import-linter contract — see watch.py's header), and lets the
scheduling be tested with an injected clock, no real threads or wall-clock
waits.

The last successful sweep's completion time is exposed as `last_sweep_at`
for "as of" stamps; a failed cycle logs and is skipped (never crashes
serve, never stamps a refresh that did not happen).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger(__name__)

# Default cadence: outcome data is not latency-sensitive and each cycle makes
# a batched GraphQL read plus the free rate-limit reading, so ~a minute keeps
# API pressure low.
DEFAULT_INTERVAL_S = 60.0

#: The three readings a finished cycle can have. ``ok`` = every query the cycle
#: made answered, INCLUDING the free rate-limit reading that doubles as the auth
#: probe; ``partial`` = some answered and some failed; ``failed`` = the cycle
#: raised, or every query failed. A cycle where every ``gh`` call failed used to
#: record exactly what a clean one did (#2297).
RESULT_OK = "ok"
RESULT_PARTIAL = "partial"
RESULT_FAILED = "failed"


@dataclass(frozen=True)
class SweepCycleResult:
    """What ONE sweep cycle saw, as plain data.

    Returned by the injected ``sweep_once`` callable so the worker can judge the
    cycle without importing ``kew.outcomes`` (the import-linter contract keeps
    ``kew.serve`` the far side of ``kew.service``) — the counts mirror
    ``outcomes.SyncResult``, and ``rate_limit`` is the serialized free
    rate-limit reading the cycle took. A callable returning ``None`` (the
    pre-#2297 shape) is read as a clean cycle that reported nothing.
    ``graphql_calls`` is the GraphQL requests the cycle's outcome sync made —
    its quota spend, shown on the health page (#2750).
    """

    synced: int = 0
    no_pr: int = 0
    query_failures: int = 0
    rate_limit: dict[str, Any] | None = field(default=None)
    graphql_calls: int = 0

    @property
    def rate_limit_ok(self) -> bool | None:
        """Whether the cycle's rate-limit reading answered — None when it took
        none, or when the payload does not say. That reading doubles as the auth
        probe, so a 401 lands here; only an explicit ``ok: false`` counts
        against the cycle, so a payload without the field never false-alarms.
        """
        if self.rate_limit is None or "ok" not in self.rate_limit:
            return None
        return bool(self.rate_limit.get("ok"))

    @property
    def rate_limit_reason(self) -> str:
        """Why the rate-limit reading failed (``unauthorized``, ``http_…``), or
        empty when it answered or was not taken."""
        if self.rate_limit is None or self.rate_limit_ok:
            return ""
        return str(self.rate_limit.get("reason") or "")

    @property
    def result(self) -> str:
        """This cycle's reading — see ``RESULT_*``.

        The rate-limit reading counts as one of the cycle's queries: a cycle
        whose auth probe came back 401 is NOT a clean cycle, however little
        outcome work it happened to have (#2297). It used to read ``ok`` and
        stamp ``last_ok_at``, so the one call that proves the credential is
        broken proved nothing at all.
        """
        failures = self.query_failures + (1 if self.rate_limit_ok is False else 0)
        if failures <= 0:
            return RESULT_OK
        resolved = self.synced + self.no_pr
        return RESULT_PARTIAL if resolved > 0 else RESULT_FAILED


class SweepWorker:
    """Scheduled, fail-soft runner for one injected sweep callable."""

    def __init__(
        self,
        sweep_once: Callable[[], SweepCycleResult | None],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0, got {interval_s}")
        self._sweep_once = sweep_once
        self._interval_s = interval_s
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sweep_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_ok_at: datetime | None = None
        self._last_result: str | None = None
        self._query_failures = 0
        self._graphql_calls = 0
        self._rate_limit: dict[str, Any] | None = None

    @property
    def last_sweep_at(self) -> datetime | None:
        """Completion time of the last cycle that did not raise, or None yet."""
        return self._last_sweep_at

    @property
    def last_attempt_at(self) -> datetime | None:
        """When the last cycle was ATTEMPTED — set even when it failed, so a
        health reader can tell a dead scheduler from a failing one."""
        return self._last_attempt_at

    @property
    def last_ok_at(self) -> datetime | None:
        """Completion time of the last cycle whose every query answered."""
        return self._last_ok_at

    @property
    def last_result(self) -> str | None:
        """The last cycle's reading (``ok``/``partial``/``failed``), None yet."""
        return self._last_result

    @property
    def query_failures(self) -> int:
        """Failed queries in the last cycle (0 for a clean one)."""
        return self._query_failures

    @property
    def graphql_calls(self) -> int:
        """GraphQL requests the last cycle's outcome sync made."""
        return self._graphql_calls

    @property
    def rate_limit(self) -> dict[str, Any] | None:
        """The last free GitHub rate-limit reading, or None if never taken."""
        return self._rate_limit

    def health(self) -> dict[str, Any]:
        """The sweep's own liveness, for a health reader (no GitHub call)."""
        return {
            "interval_s": self._interval_s,
            "last_attempt_at": _iso(self._last_attempt_at),
            "last_ok_at": _iso(self._last_ok_at),
            "last_sweep_at": _iso(self._last_sweep_at),
            "last_result": self._last_result,
            "query_failures": self._query_failures,
            "graphql_calls": self._graphql_calls,
            "rate_limit": self._rate_limit,
        }

    def _sleep(self, interval: float) -> None:
        # Interruptible sleep: returns immediately once stop is requested, so
        # shutdown never blocks for a full interval. Overridable for tests
        # (the injected clock).
        self._stop.wait(interval)

    def _run_cycle(self) -> None:
        self._last_attempt_at = self._now()
        try:
            outcome = self._sweep_once()
        except Exception:  # noqa: BLE001 — a cycle must never crash the worker
            _logger.warning("sweep cycle failed; skipping", exc_info=True)
            self._last_result = RESULT_FAILED
            return
        outcome = outcome or SweepCycleResult()
        self._last_result = outcome.result
        self._query_failures = outcome.query_failures
        self._graphql_calls = outcome.graphql_calls
        if outcome.rate_limit is not None:
            self._rate_limit = outcome.rate_limit
        # Only a clean cycle stamps the "as of" time — a skipped cycle did
        # not refresh anything.
        self._last_sweep_at = self._now()
        if self._last_result == RESULT_OK:
            self._last_ok_at = self._last_sweep_at

    def run_until_stopped(self) -> None:
        """The worker loop (also the thread target). Runs one cycle, then
        sleeps the interval, until `request_stop` is called."""
        while not self._stop.is_set():
            self._run_cycle()
            if self._stop.is_set():
                break
            self._sleep(self._interval_s)

    def request_stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # A prior stop() leaves the event set; clear it so a restarted
        # worker actually loops instead of exiting on its first check.
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_until_stopped, name="kew-sweep", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            # Keep the reference while the worker is still terminating so a
            # subsequent start() can't launch an overlapping worker.
            if not thread.is_alive():
                self._thread = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
