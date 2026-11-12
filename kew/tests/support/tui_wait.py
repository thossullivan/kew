"""Bounded, deterministic waits for a Textual app driven by its pilot (#2342).

The pilot's idle wait returns when the message pump happens to be empty. It
does not advance a clock and it does not know what the assertion is about, so
a test that pumps a fixed number of times really asserts "the app got there
within N idle points" — which stops being true under load. Two flakes came
from exactly that (#1772, #2178):

* the state the assertion is about had not landed yet, and
* a debounced ``set_timer(0.15, ...)`` refresh was still armed at teardown,
  where it dereferenced ``app.screen`` after the harness had unwound the
  screen stack — ``ScreenStackError("No screens on stack")``.

Every wait here carries a deadline and names what it waited for, so
contention makes a wait longer, never wrong. That is what lets a pilot
module leave the serial phase: ``tests/conftest.py`` keeps any pilot module
that does not *call* one of these waits in the serial phase (the rule is
``tests/support/serial_phase.py``, read off the syntax tree so an unused
import buys nothing), and ``tests/test_serial_phase.py`` pins both halves of
it, including that a module which claims the escape pumps the app through
this helper alone.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

# A deadline for the state an assertion is about, never a sleep: a wait that
# reaches it is a test failure naming what never arrived.
WAIT_TIMEOUT = 15.0
# Pumps that must agree before the widget tree counts as settled. One pump can
# return between two mounts; two agreeing pumps cannot straddle a single one.
QUIET_PUMPS = 2
# How long to yield between pumps. Long enough that a 0.15s debounce drains in
# a bounded number of iterations, short enough to add nothing to a settled app.
_YIELD_S = 0.01


async def wait_for(
    pilot: Any,
    predicate: Callable[[], bool],
    what: str,
    timeout: float = WAIT_TIMEOUT,
) -> None:
    """Pump the app until ``predicate()`` holds; fail naming ``what`` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        await pilot.pause()
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")
        await asyncio.sleep(_YIELD_S)


def _debounce_timers(app: Any) -> Iterator[Any]:
    yield getattr(app, "_detail_timer", None)
    yield getattr(app, "_poll_debounce_timer", None)
    if app.screen_stack:
        yield getattr(app.screen, "_detail_timer", None)


def debounce_pending(app: Any) -> bool:
    """True while any debounced refresh is still armed on the app or screen."""
    return any(timer is not None for timer in _debounce_timers(app))


async def settle(pilot: Any, timeout: float = WAIT_TIMEOUT) -> None:
    """Drain armed debounce timers so none fires during harness teardown."""
    await wait_for(
        pilot,
        lambda: not debounce_pending(pilot.app),
        "the debounced detail-pane refresh to drain",
        timeout=timeout,
    )


def _widget_tree(app: Any) -> tuple[tuple[int, str, int], ...]:
    """A fingerprint of the active screen: identity, type and child count.

    A rebuilt widget is a new object, so a clear-and-rebuild changes the
    fingerprint even when it ends with the same number of rows.
    """
    if not app.screen_stack:
        return ()
    return tuple(
        (id(node), type(node).__name__, len(getattr(node, "_nodes", ())))
        for node in app.screen.walk_children(with_self=True)
    )


async def quiesce(pilot: Any, timeout: float = WAIT_TIMEOUT) -> None:
    """Pump until the screen stops changing and no debounced refresh is armed.

    The wait an assertion about rendered content wants: it does not encode
    what that content should be, so the assertion keeps its own teeth, and a
    slow mount under contention lengthens the wait instead of defeating it.
    """
    deadline = time.monotonic() + timeout
    previous: tuple[tuple[int, str, int], ...] | None = None
    agreed = 0
    while True:
        await pilot.pause()
        current = _widget_tree(pilot.app)
        if current == previous and not debounce_pending(pilot.app):
            agreed += 1
            if agreed >= QUIET_PUMPS:
                return
        else:
            agreed = 0
        previous = current
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout:.1f}s waiting for the screen to settle"
            )
        await asyncio.sleep(_YIELD_S)


@pytest.fixture(autouse=True)
def no_ambient_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start the app's own interval polls paused, in every test of the module.

    The TUI app polls SQLite every 2s and GitHub every 30s. A test that sets
    ``app.state`` by hand owns it, and an ambient poll that fires mid-test
    replaces that state under the assertion — or rebuilds a list the test is
    proving was left in place. Invisible while a test finishes inside the
    cadence, which is why it only shows up under contention (reproduced at
    twice the worker count). The timers are still created, still returned;
    they just never fire unless the test resumes them, and every test that
    wants a poll already calls it directly.

    Only those two callbacks are paused: the screens' own intervals (the logs
    tail) and anything Textual schedules for itself are untouched.
    """
    from kew.tui.app import KewApp

    scheduled = KewApp.set_interval

    def ambient(self: Any, interval: float, callback: Any = None, **kwargs: Any) -> Any:
        if callback in (self._poll_local, self._poll_remote):
            kwargs["pause"] = True
        return scheduled(self, interval, callback, **kwargs)

    monkeypatch.setattr(KewApp, "set_interval", ambient)
