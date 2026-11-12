"""When is a claude-code run waiting on its model? (#2240)

``ClaudeTurns`` folds claude-code ``stream-json`` lines into "a model call is
outstanding", the only time the no-output watchdog counts silence. The
fixtures are hand-written lines in the shapes recorded from real claude-code
runs (types, subtypes and the keys the fold reads; ids shortened, no content).
"""

from __future__ import annotations

import json
from typing import Any

from kew.model_wait import ClaudeTurns, NeverWaiting, model_wait_for


def _line(**event: Any) -> str:
    return json.dumps(event)


def _system(subtype: str, **fields: Any) -> str:
    return _line(type="system", subtype=subtype, **fields)


def _assistant(*blocks: dict[str, Any], parent: str | None = None) -> str:
    return _line(
        type="assistant",
        message={"content": list(blocks)},
        parent_tool_use_id=parent,
    )


def _user(*blocks: dict[str, Any], parent: str | None = None) -> str:
    return _line(
        type="user", message={"content": list(blocks)}, parent_tool_use_id=parent
    )


def _tool_use(tool_id: str | None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_use", "name": "Bash", "input": {}}
    if tool_id is not None:
        block["id"] = tool_id
    return block


def _tool_result(tool_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


_INIT = _system("init", model="m", claude_code_version="2")
_TEXT = _assistant({"type": "text", "text": "hi"})
_THINKING = _assistant({"type": "thinking", "thinking": ""})
_RESULT = _line(type="result", subtype="success", result="done")
_TASKS_CHANGED = _system("background_tasks_changed", tasks=[])
_THINKING_TOKENS = _system("thinking_tokens", estimated_tokens=10)
_RATE_LIMIT = _line(type="rate_limit_event", rate_limit_info={"status": "allowed"})


def _fold(lines: list[str]) -> list[bool]:
    """``outstanding`` after each line."""
    turns = ClaudeTurns()
    states = []
    for line in lines:
        turns.observe(line)
        states.append(turns.outstanding)
    return states


def test_nothing_is_outstanding_before_the_run_starts() -> None:
    assert ClaudeTurns().outstanding is False


def test_one_result_of_two_parallel_tools_keeps_the_clock_paused() -> None:
    states = _fold(
        [
            _INIT,
            _assistant(_tool_use("a")),
            _assistant(_tool_use("b")),
            _user(_tool_result("a")),
            _user(_tool_result("b")),
        ]
    )

    assert states == [True, False, False, False, True]


def test_two_tools_in_one_message_are_both_open() -> None:
    states = _fold(
        [
            _INIT,
            _assistant(_tool_use("a"), _tool_use("b")),
            _user(_tool_result("b")),
            _user(_tool_result("a")),
        ]
    )

    assert states == [True, False, False, True]


def test_a_tool_without_an_id_turns_the_clock_off_for_the_rest_of_the_run() -> None:
    """It can never be closed, so the safe reading is "not waiting" from then
    on — through the turn's end and every later turn."""
    states = _fold([_INIT, _assistant(_tool_use(None)), _RESULT, _INIT, _TEXT])

    assert states == [True, False, False, False, False]


def test_a_task_without_an_id_turns_the_clock_off_for_the_rest_of_the_run() -> None:
    states = _fold([_INIT, _system("task_started"), _RESULT, _INIT, _TEXT])

    assert states == [True, False, False, False, False]


def test_a_subagent_working_under_an_open_tool_never_restarts_the_clock() -> None:
    """A subagent's own lines carry ``parent_tool_use_id``; its tool results
    close only its own tools, and the parent's tool stays open until its
    result comes back to the main agent."""
    states = _fold(
        [
            _INIT,
            _assistant(_tool_use("task")),
            _assistant({"type": "text", "text": "sub"}, parent="task"),
            _assistant(_tool_use("sub1"), parent="task"),
            _user(_tool_result("sub1"), parent="task"),
            _assistant({"type": "text", "text": "sub done"}, parent="task"),
            _user(_tool_result("task")),
        ]
    )

    assert states == [True, False, False, False, False, False, True]


def test_a_line_that_is_not_json_changes_no_state() -> None:
    turns = ClaudeTurns()
    turns.observe("npm warn deprecated something\n")
    assert turns.outstanding is False

    turns.observe(_INIT)
    turns.observe("stray diagnostic {not json\n")
    turns.observe("")
    assert turns.outstanding is True

    turns.observe(_assistant(_tool_use("a")))
    turns.observe("Traceback (most recent call last):\n")
    assert turns.outstanding is False


def test_a_background_bash_after_a_result_waits_until_init() -> None:
    """The recorded shape of a turn that ends on a background command: the
    task outlives the turn, and the next turn begins with ``init``."""
    states = _fold(
        [
            _INIT,
            _THINKING_TOKENS,
            _THINKING,
            _assistant(_tool_use("bg")),
            _RATE_LIMIT,
            _TASKS_CHANGED,
            _system(
                "task_started",
                task_id="t1",
                task_type="local_bash",
                tool_use_id="bg",
                is_backgrounded=True,
            ),
            _user(_tool_result("bg")),
            _TEXT,
            _RESULT,
            _system("task_updated", task_id="t1", patch={"status": "completed"}),
            _system(
                "task_notification", task_id="t1", status="completed", tool_use_id="bg"
            ),
            _TASKS_CHANGED,
            _INIT,
            _TEXT,
            _RESULT,
        ]
    )

    assert states == [
        True,  # init: the first model call
        True,
        True,
        False,  # the backgrounded tool call is open
        False,
        False,
        False,
        False,  # its result returns at once, but the task now runs
        False,
        False,  # the turn is over
        False,
        False,
        False,  # the task is done; no turn yet
        True,  # the next turn calls the model
        True,
        False,
    ]


def test_a_command_lifecycle_line_after_a_result_is_not_a_turn() -> None:
    states = _fold(
        [
            _INIT,
            _assistant(_tool_use("a")),
            _user(_tool_result("a")),
            _TEXT,
            _RESULT,
            _line(type="command_lifecycle", uuid="u1", state="completed"),
            _INIT,
            _TEXT,
            _RESULT,
        ]
    )

    assert states == [True, False, True, True, False, False, True, True, False]


def test_a_killed_background_agent_closes_its_task() -> None:
    """A background agent's task ends as ``killed`` (``task_updated``) and
    ``stopped`` (``task_notification``); either ends it."""
    for closing in (
        _system("task_updated", task_id="ag", patch={"status": "killed"}),
        _system("task_notification", task_id="ag", status="stopped"),
    ):
        states = _fold(
            [
                _INIT,
                _assistant(_tool_use("spawn")),
                _system(
                    "task_started",
                    task_id="ag",
                    task_type="local_agent",
                    tool_use_id="spawn",
                ),
                _user(_tool_result("spawn")),
                _system("task_progress", task_id="ag", tool_use_id="spawn"),
                closing,
            ]
        )

        assert states == [True, False, False, False, False, True]


def test_a_task_update_that_is_not_an_end_keeps_the_task_open() -> None:
    states = _fold(
        [
            _INIT,
            _system("task_started", task_id="t1"),
            _system("task_updated", task_id="t1", patch={"description": "x"}),
            _system("task_updated", task_id="t1", patch={"status": "running"}),
        ]
    )

    assert states == [True, False, False, False]


def test_only_claude_code_says_when_its_model_is_called() -> None:
    assert isinstance(model_wait_for("claude-code"), ClaudeTurns)
    for runner in ("codex", "opencode", "pi", "hermes", "lemonade", ""):
        fold = model_wait_for(runner)
        assert isinstance(fold, NeverWaiting)
        fold.observe(_INIT)
        fold.observe(_TEXT)
        assert fold.outstanding is False
