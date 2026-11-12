"""Defer SIGINT across short ownership and persistence transitions."""

from __future__ import annotations

import signal
import threading
from types import FrameType, TracebackType
from typing import Literal


class DeferredSIGINT:
    """Restore the signal handler before propagating a deferred interruption.

    Python delivers SIGINT to the main thread. Worker threads need no handler.
    An existing exception stays primary; a deferred interrupt becomes its cause.
    """

    def __init__(self) -> None:
        self.received = False
        self._previous: signal._HANDLER = None
        self._installed = False

    def _record(self, signum: int, frame: FrameType | None) -> None:
        self.received = True

    def __enter__(self) -> DeferredSIGINT:
        if threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGINT, self._record)
            self._installed = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self._installed:
            signal.signal(signal.SIGINT, self._previous)
        if self.received:
            if exc is None:
                raise KeyboardInterrupt
            exc.__cause__ = KeyboardInterrupt("SIGINT deferred until cleanup completed")
        return False
