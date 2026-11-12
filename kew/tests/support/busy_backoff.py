"""A one-shot busy-retry back-off for one ``SqliteRunStore``.

Install it on the store under test only (``monkeypatch.setattr(store,
"busy_sleep", hook)``), never over ``kew.run_store.time.sleep``: that is the
process-wide ``time.sleep``, so any other sleeper — another store, another
thread — would re-enter the callback after the state it set up has moved on.

The hook runs its action on the first back-off and only records later ones. It
records what the action returned rather than asserting inside it: a failed
assert in a back-off is raised from the store's retry loop, where it reads as a
store error, not as the test's finding. The test body asserts on ``outcomes``.
"""

from __future__ import annotations

from collections.abc import Callable


class OneShotBackoff[T]:
    """Run *action* on the first back-off; record every delay and the outcome."""

    def __init__(self, action: Callable[[], T]) -> None:
        self._action = action
        self.delays: list[float] = []
        self.outcomes: list[T] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) == 1:
            self.outcomes.append(self._action())
