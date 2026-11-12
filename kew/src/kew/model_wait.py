"""Is a contained agent waiting on its model provider right now? (#2240)

The no-output watchdog stops a run whose provider went quiet, and only that:
an agent that ended a turn and waits on a tool call or a background task can
say nothing for an hour and still be working (34 of 834 successful contained
runs in October 2026 went more than 15 minutes between lines, up to 87). So
the silence clock runs only while a model call is outstanding, read off the
runner's own stream lines.

Only claude-code's ``stream-json`` says when that is. Every other runner reads
as never waiting on its model, so the watchdog never stops it: the safe side of
not knowing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

#: Statuses a ``task_updated`` patch carries once a background task is over.
_TASK_OVER = frozenset({"completed", "failed", "killed", "stopped", "cancelled"})


class ModelWait(Protocol):
    """Folds a runner's stream lines into "is a model call outstanding?"."""

    @property
    def outstanding(self) -> bool: ...

    def observe(self, line: str) -> None: ...


class NeverWaiting:
    """A runner whose stream does not say when its model is called."""

    outstanding = False

    def observe(self, line: str) -> None:
        return None


class ClaudeTurns:
    """claude-code's ``stream-json``, folded into model-call state.

    A model call is outstanding from a line that opens or continues a turn
    (``system``/``init``, an ``assistant`` message, a ``user`` line returning
    the last open tool's result) until a line that hands the run to something
    else: an ``assistant`` ``tool_use`` opens a tool call until its
    ``tool_result``; a ``result`` line ends the turn until the next ``init`` or
    ``assistant`` line; a ``system``/``task_started`` background task runs
    until its ``task_notification``. Every other line (``tool_progress``,
    ``command_lifecycle``, rate-limit and hook notices) is output that changes
    none of this. A tool or task whose id is missing cannot be closed, so it
    keeps the clock stopped: unknown reads as not waiting.
    """

    def __init__(self) -> None:
        self._in_turn = False
        self._tools: set[str] = set()
        self._tasks: set[str] = set()
        self._unclosable = False

    @property
    def outstanding(self) -> bool:
        return (
            self._in_turn
            and not self._tools
            and not self._tasks
            and not self._unclosable
        )

    def observe(self, line: str) -> None:
        event = _event(line)
        if event is None:
            return
        kind = event.get("type")
        if kind == "assistant":
            self._in_turn = True
            self._open_tools(event)
        elif kind == "user":
            self._close_tools(event)
        elif kind == "result":
            # The turn is over; any call it left open went with it.
            self._in_turn = False
            self._tools.clear()
        elif kind == "system":
            self._system(event)

    def _system(self, event: dict[str, Any]) -> None:
        subtype = event.get("subtype")
        task_id = event.get("task_id")
        if subtype == "init":
            self._in_turn = True
        elif subtype == "task_started":
            self._track(self._tasks, task_id)
        elif subtype == "task_notification":
            self._tasks.discard(str(task_id))
        elif subtype == "task_updated":
            patch = event.get("patch")
            status = patch.get("status") if isinstance(patch, dict) else None
            if status in _TASK_OVER:
                self._tasks.discard(str(task_id))

    def _open_tools(self, event: dict[str, Any]) -> None:
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                self._track(self._tools, block.get("id"))

    def _close_tools(self, event: dict[str, Any]) -> None:
        for block in _blocks(event):
            if block.get("type") == "tool_result":
                self._tools.discard(str(block.get("tool_use_id")))

    def _track(self, open_ids: set[str], ident: object) -> None:
        if isinstance(ident, str) and ident:
            open_ids.add(ident)
        else:
            self._unclosable = True


def model_wait_for(runner: str) -> ModelWait:
    """The fold that reads *runner*'s stream; never-waiting when unknown."""
    if runner == "claude-code":
        return ClaudeTurns()
    return NeverWaiting()


def _event(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        # Not an event (a stray diagnostic line): output, but no turn state.
        _logger.log(
            logging.DEBUG, "model wait: a stream line that is not JSON changes no state"
        )
        return None
    return event if isinstance(event, dict) else None


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]
