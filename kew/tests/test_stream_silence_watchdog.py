"""A contained run whose model call goes silent is stopped and named (#2240).

The stream consumer blocks reading the container's stdout, so the watchdog runs
beside it. These drive the real ``_stream_sandbox_log`` (and, at the end, the
real ``run_container_and_stream``) over a fake container stream, an injected
clock, and a pacer that lets the watchdog check only when the test says so and
waits until it has: a test cannot pass without the check having run.

Silence counts only while a model call is outstanding (Tom, 2026-10-31): an
agent that ended a turn, or waits on a tool call or a background task, can say
nothing for an hour and still be working.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import kew.dispatch_core as dispatch_core
from kew.config import AgentConfig, DispatchConfig
from kew.dispatch_core import _finalize_sandbox_run, _stream_sandbox_log
from kew.dispatch_sandbox import (
    SilencePacing,
    StreamSilenceWatch,
    run_container_and_stream,
)
from kew.run_store import RunRecord, SqliteRunStore
from kew.sandbox import KillStatus
from kew.tracking import STREAM_SILENT_CATEGORY, DispatchResult

_LIMIT = 15 * 60.0
#: Generous bound on any wait for the watchdog thread; never reached on a pass.
_DEADLINE = 30.0


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event)


_INIT = _line({"type": "system", "subtype": "init", "session_id": "s1", "model": "m"})
_TALK = _line(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
)
_RESULT = _line({"type": "result", "subtype": "success", "result": "done"})
_NOTICE = _line({"type": "system", "subtype": "hook_response"})


def _tool_use(tool_id: str) -> str:
    block = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}
    return _line({"type": "assistant", "message": {"content": [block]}})


def _tool_result(tool_id: str) -> str:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
    return _line({"type": "user", "message": {"content": [block]}})


def _task(subtype: str, task_id: str) -> str:
    return _line({"type": "system", "subtype": subtype, "task_id": task_id})


class _Clock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 1_000.0

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _Pacer:
    """The watchdog's pause between checks, driven by the test.

    The watch parks here; :meth:`tick` lets it make exactly one check and
    returns once it has — when the watch parks again, or its thread is over
    (it stopped the run, or gave up). A watch that never checked fails the
    tick instead of passing the test."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self.parked = 0
        self._released = 0
        self._thread: threading.Thread | None = None

    def pause(self, done: threading.Event, _seconds: float) -> bool:
        with self._cond:
            self._thread = threading.current_thread()
            self.parked += 1
            ticket = self.parked
            self._cond.notify_all()
            while self._released < ticket:
                if done.is_set():
                    return True
                self._cond.wait(0.01)
            return False

    def _over(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    def _until(self, ready: Callable[[], bool]) -> bool:
        """Wait (holding the condition) until *ready*, re-reading it often: a
        watch thread that ends notifies nobody, so its end is polled."""
        deadline = time.monotonic() + _DEADLINE
        while not ready():
            if time.monotonic() >= deadline:
                return False
            self._cond.wait(0.005)
        return True

    def tick(self) -> None:
        with self._cond:
            assert self._until(lambda: self.parked > self._released or self._over()), (
                "the watchdog never parked"
            )
            if self._over():
                return
            self._released += 1
            checked = self._released
            self._cond.notify_all()
            assert self._until(lambda: self.parked > checked or self._over()), (
                "the watchdog never finished its check"
            )


Step = str | Callable[[], None]


class _Container:
    """A container stream: its script of lines and actions, then — until
    killed — nothing at all, the way a stalled provider stream reads.

    *kills* scripts what each kill call answers (ACCEPTED once the list runs
    out); *settles* what reading the runtime after an unknown kill says."""

    def __init__(
        self,
        script: list[Step],
        *,
        hold: bool = False,
        kills: list[KillStatus] | None = None,
        settles: KillStatus = KillStatus.UNKNOWN,
        on_wait: Callable[[], None] = lambda: None,
    ) -> None:
        self._script = script
        self._hold = hold
        self._answers = list(kills or [])
        self._settles = settles
        self._on_wait = on_wait
        self._killed = threading.Event()
        self.kills = 0

    @property
    def kill_accepted(self) -> bool:
        return self._killed.is_set()

    def kill(self) -> KillStatus:
        self.kills += 1
        if self._killed.is_set():
            # As the runtime handles do: a second kill is refused.
            return KillStatus.REFUSED
        status = self._answers.pop(0) if self._answers else KillStatus.ACCEPTED
        if status is KillStatus.ACCEPTED:
            self._killed.set()
        return status

    def note_oom_kill(self) -> None:
        """A 137 is probed for an OOM kill; this container was not one."""
        return None

    def _resolve_unknown_kill(self) -> KillStatus:
        if self._settles is KillStatus.ACCEPTED:
            self._killed.set()
        return self._settles

    def __iter__(self) -> Iterator[str]:
        for step in self._script:
            if callable(step):
                step()
            else:
                yield step + "\n"
        if self._hold:
            assert self._killed.wait(_DEADLINE), (
                "the silent container was never stopped"
            )

    def wait(self) -> int:
        self._on_wait()
        return 137 if self._killed.is_set() else 0


def _pending_row(store: SqliteRunStore, log_file: Path) -> None:
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            log_file=log_file,
        )
    )


class _Run:
    def __init__(self, tmp_path: Path, *, limit: float = _LIMIT) -> None:
        self.tmp_path = tmp_path
        self.limit = limit
        self.clock = _Clock()
        self.pacer = _Pacer()
        self.store = SqliteRunStore(str(tmp_path / "runs.db"))
        self.log_file = tmp_path / "sandbox.log"
        _pending_row(self.store, self.log_file)

    def silence(self, seconds: float) -> Callable[[], None]:
        """A step: *seconds* pass with nothing written, and the watch checks."""

        def _step() -> None:
            self.clock.advance(seconds)
            self.pacer.tick()

        return _step

    def stream(
        self,
        container: _Container,
        *,
        runner: str = "claude-code",
        result: DispatchResult | None = None,
        watch_store: Any = None,
    ) -> tuple[int, RunRecord, StreamSilenceWatch]:
        watch = StreamSilenceWatch(
            container,
            limit_seconds=self.limit,
            store=watch_store or self.store,
            run_id="run-1",
            runner=runner,
            result=result,
            pacing=SilencePacing(now=self.clock, pause=self.pacer.pause),
        )
        with watch.watching():
            code = _stream_sandbox_log(
                watch.observe(container),
                self.log_file,
                self.store,
                "run-1",
                model="m",
                overrides=None,
                runner=runner,
                _now=self.clock,
            )
        row = self.store.get("run-1")
        assert row is not None
        return code, row, watch


@pytest.fixture
def run(tmp_path: Path) -> Iterator[_Run]:
    harness = _Run(tmp_path)
    yield harness
    harness.store.close()


def _assert_never_stopped(
    container: _Container, code: int, row: RunRecord, watch: StreamSilenceWatch
) -> None:
    assert container.kills == 0
    assert watch.stopped is False
    assert code == 0
    assert row.error_category == ""


# --------------------------------------------------------------------------- #
# A model call that goes silent                                                #
# --------------------------------------------------------------------------- #
def test_a_stream_silent_past_the_limit_is_stopped_and_named(run: _Run) -> None:
    container = _Container([_INIT, run.silence(_LIMIT + 1)], hold=True)

    code, row, watch = run.stream(container)

    assert container.kills == 1
    assert watch.stopped is True
    assert code == 137
    assert row.error_category == STREAM_SILENT_CATEGORY
    # The reviewer's terminal update passes no category: the cause survives it.
    _finalize_sandbox_run(run.store, "run-1", exit_code=code)
    final = run.store.get("run-1")
    assert final is not None
    assert final.status == "failure"
    assert final.error_category == STREAM_SILENT_CATEGORY


def test_a_silent_model_call_after_a_tool_result_is_stopped(run: _Run) -> None:
    """The clock pauses for the tool and runs again once its result went back
    to the model: a model call that then stalls is still caught."""
    container = _Container(
        [
            _INIT,
            _tool_use("t1"),
            run.silence(2 * _LIMIT),
            _tool_result("t1"),
            run.silence(_LIMIT + 1),
        ],
        hold=True,
    )

    code, row, watch = run.stream(container)

    assert container.kills == 1
    assert watch.stopped is True
    assert code == 137
    assert row.error_category == STREAM_SILENT_CATEGORY


def test_a_silence_short_of_the_limit_is_not_stopped(run: _Run) -> None:
    container = _Container([_INIT, run.silence(_LIMIT - 1)])

    _assert_never_stopped(container, *run.stream(container))


def test_a_stream_that_keeps_talking_is_never_stopped(run: _Run) -> None:
    # Most of the limit passes before every line, and the watch checks each
    # time; ten lines span far more than the limit in total.
    script: list[Step] = [_INIT]
    for _ in range(10):
        script += [run.silence(_LIMIT * 0.9), _TALK]

    _assert_never_stopped(*_with(script, run))


def _with(
    script: list[Step], run: _Run, **kw: Any
) -> tuple[_Container, int, RunRecord, StreamSilenceWatch]:
    container = _Container(script)
    return (container, *run.stream(container, **kw))


# --------------------------------------------------------------------------- #
# Waiting on something other than the model                                    #
# --------------------------------------------------------------------------- #
def test_a_turn_waiting_on_an_open_tool_call_is_never_stopped(run: _Run) -> None:
    script: list[Step] = [
        _INIT,
        _tool_use("t1"),
        run.silence(4 * _LIMIT),
        _line({"type": "tool_progress", "tool_use_id": "t1"}),
        run.silence(4 * _LIMIT),
        _tool_result("t1"),
        _TALK,
        _RESULT,
    ]

    _assert_never_stopped(*_with(script, run))


def test_a_run_waiting_after_its_result_line_is_never_stopped(run: _Run) -> None:
    """The run history's shape: a turn ends (``result``), a few ``system``
    lines, an 87-minute wait, then the next turn."""
    script: list[Step] = [
        _INIT,
        _TALK,
        _RESULT,
        _NOTICE,
        _NOTICE,
        run.silence(87 * 60.0),
        _line({"type": "command_lifecycle", "uuid": "u1", "state": "completed"}),
        run.silence(87 * 60.0),
        _TALK,
        _RESULT,
    ]

    _assert_never_stopped(*_with(script, run))


def test_a_run_waiting_on_a_background_task_is_never_stopped(run: _Run) -> None:
    script: list[Step] = [
        _INIT,
        _tool_use("t1"),
        _tool_result("t1"),  # a background Bash returns at once
        _task("task_started", "b1"),
        run.silence(4 * _LIMIT),
        _task("task_notification", "b1"),
        _TALK,
        _RESULT,
    ]

    _assert_never_stopped(*_with(script, run))


def test_a_model_call_silent_after_its_background_task_ended_is_stopped(
    run: _Run,
) -> None:
    """The task's end (here a ``task_updated`` patch) restarts the clock."""
    container = _Container(
        [
            _INIT,
            _tool_use("t1"),
            _tool_result("t1"),
            _task("task_started", "b1"),
            run.silence(4 * _LIMIT),
            _line(
                {
                    "type": "system",
                    "subtype": "task_updated",
                    "task_id": "b1",
                    "patch": {"status": "completed"},
                }
            ),
            run.silence(_LIMIT + 1),
        ],
        hold=True,
    )

    code, row, watch = run.stream(container)

    assert container.kills == 1
    assert watch.stopped is True
    assert code == 137
    assert row.error_category == STREAM_SILENT_CATEGORY


def test_a_runner_whose_stream_cannot_say_is_never_stopped(run: _Run) -> None:
    """Only claude-code's stream says when its model is called; any other
    runner reads as never waiting on it — the safe side of not knowing."""
    script: list[Step] = [
        _line({"type": "turn.started"}),
        run.silence(10 * _LIMIT),
    ]

    _assert_never_stopped(*_with(script, run, runner="codex"))


# --------------------------------------------------------------------------- #
# Off, and stops that are not this watch's                                     #
# --------------------------------------------------------------------------- #
def test_a_zero_limit_turns_the_watchdog_off(tmp_path: Path) -> None:
    run = _Run(tmp_path, limit=0.0)
    container = _Container([_INIT, lambda: run.clock.advance(1_000_000)])

    code, row, watch = run.stream(container)

    # Armed, the watch would have parked at least once before its join.
    assert run.pacer.parked == 0
    _assert_never_stopped(container, code, row, watch)
    run.store.close()


def test_a_container_another_path_stopped_is_not_named_silent(run: _Run) -> None:
    container = _Container([_INIT])
    # A cap breach or cancel landed first; then the silence passes.
    container._script.append(container._killed.set)
    container._script.append(run.silence(_LIMIT + 1))

    _code, row, watch = run.stream(container)

    assert container.kills == 0
    assert watch.stopped is False
    assert row.error_category == ""


def test_a_cost_stopped_run_is_not_renamed_silent(run: _Run) -> None:
    result = DispatchResult(-1, 7, run.log_file, budget_exceeded=True)
    container = _Container([_INIT, run.silence(_LIMIT + 1)], hold=True)

    _code, row, watch = run.stream(container, result=result)

    assert container.kills == 1
    assert watch.silenced() is False
    assert row.error_category == ""


def test_a_slow_exit_after_the_last_line_is_never_called_silent(run: _Run) -> None:
    """The watch disarms when the lines run out, not when the exit is read: a
    container that wrote its last line and takes past the limit to exit is
    not stopped."""
    container = _Container([_INIT, _TALK], on_wait=run.silence(10 * _LIMIT))

    _assert_never_stopped(container, *run.stream(container))


# --------------------------------------------------------------------------- #
# A kill whose outcome is unknown                                              #
# --------------------------------------------------------------------------- #
def test_an_unknown_kill_settled_by_the_runtime_is_the_stop(run: _Run) -> None:
    container = _Container(
        [_INIT, run.silence(_LIMIT + 1)],
        hold=True,
        kills=[KillStatus.UNKNOWN],
        settles=KillStatus.ACCEPTED,
    )

    code, row, watch = run.stream(container)

    assert container.kills == 1
    assert watch.stopped is True
    assert code == 137
    assert row.error_category == STREAM_SILENT_CATEGORY


def test_a_run_that_talks_again_after_an_unsettled_kill_is_not_killed(
    run: _Run,
) -> None:
    """The kill timed out and the runtime cannot say; then the model answers.
    Every retry checks the silence first, so the run is left alone."""
    container = _Container(
        [_INIT, run.silence(_LIMIT + 1), _TALK, run.silence(1.0), _RESULT],
        kills=[KillStatus.UNKNOWN],
    )

    code, row, watch = run.stream(container)

    assert container.kills == 1  # the first attempt only; no retry
    assert watch.stopped is False
    assert code == 0
    assert row.error_category == ""


def test_another_paths_kill_after_an_unsettled_silence_kill_is_not_renamed(
    run: _Run,
) -> None:
    """The silence kill timed out and the runtime cannot say; then the run
    timeout (or a cap breach, or a cancel) lands its own kill. That stop is
    not this watch's, so the run is not renamed ``stream_silent``."""
    container = _Container([_INIT, run.silence(_LIMIT + 1)], kills=[KillStatus.UNKNOWN])
    container._script += [container._killed.set, run.silence(1.0)]

    code, row, watch = run.stream(container)

    assert container.kills == 1  # no second attempt once another kill landed
    assert watch.stopped is False
    assert watch.silenced() is False
    assert code == 137
    assert row.error_category == ""


class _KilledRightAfterTheCheck(_Container):
    """Another path's kill lands the moment after the watch first asks
    whether the container was already stopped."""

    @property
    def kill_accepted(self) -> bool:
        landed = self._killed.is_set()
        self._killed.set()
        return landed


def test_a_kill_that_lands_elsewhere_during_the_stop_is_not_this_watchs(
    run: _Run,
) -> None:
    """Another path's kill lands between the watch's check and its own kill:
    the handle refuses the second kill, and the run is not renamed."""
    container = _KilledRightAfterTheCheck([_INIT, run.silence(_LIMIT + 1)])

    code, row, watch = run.stream(container)

    assert container.kills == 1
    assert watch.stopped is False
    assert code == 137
    assert row.error_category == ""


# --------------------------------------------------------------------------- #
# A watchdog that fails                                                        #
# --------------------------------------------------------------------------- #
def test_a_failed_watchdog_is_logged_and_the_stream_runs_on(
    run: _Run, caplog: pytest.LogCaptureFixture
) -> None:
    container = _Container([_INIT, run.silence(_LIMIT + 1), _TALK, _RESULT])

    def _kill_raises() -> KillStatus:
        raise RuntimeError("runtime client crashed")

    container.kill = _kill_raises  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="kew.dispatch_sandbox"):
        code, row, watch = run.stream(container)

    assert code == 0
    assert watch.stopped is False
    assert row.error_category == ""
    assert any("watchdog failed" in record.getMessage() for record in caplog.records)


class _RowWriteFails:
    def get(self, _run_id: str) -> Any:
        raise OSError("disk full")


def test_the_streams_own_failure_stays_primary(run: _Run) -> None:
    """A stream that dies after the watch stopped it raises its own error, not
    the failed write of the silence onto the run row."""

    def _dies() -> None:
        raise ConnectionError("stream died")

    container = _Container([_INIT, run.silence(_LIMIT + 1), _dies])

    with pytest.raises(ConnectionError, match="stream died"):
        run.stream(container, watch_store=_RowWriteFails())
    assert container.kills == 1


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def test_no_output_minutes_zero_is_accepted_and_defaults_to_fifteen() -> None:
    assert AgentConfig().no_output_minutes == 15
    assert AgentConfig(no_output_minutes=0).no_output_minutes == 0


def test_a_zero_floor_refuses_a_negative_no_output_minutes() -> None:
    with pytest.raises(ValidationError, match="no_output_minutes"):
        AgentConfig(no_output_minutes=-1)


# --------------------------------------------------------------------------- #
# The container primitive reads the limit from config                          #
# --------------------------------------------------------------------------- #
def _run_primitive(
    tmp_path: Path, minutes: int, container: _Container, pacer: _Pacer, clock: _Clock
) -> tuple[int, DispatchResult]:
    config = DispatchConfig()
    config.agent.no_output_minutes = minutes
    result = DispatchResult(-1, 42, tmp_path / "container.log")
    # Faked at the runtime client, the one process boundary of a launch.
    with patch.object(dispatch_core, "_launch_container", return_value=container):
        code = run_container_and_stream(
            config,
            None,
            image="image",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="42",
            run_id="run-42",
            model="model",
            log_file=result.log_file,
            run_kind="dispatch",
            result=result,
            # The re-entry after credential issuance, which is where every
            # contained launch arrives with its config unchanged.
            _credentials_resolved=True,
            _silence_pacing=SilencePacing(now=clock, pause=pacer.pause),
        )
    return code, result


def test_the_primitive_stops_a_run_silent_past_its_configured_minutes(
    tmp_path: Path,
) -> None:
    clock, pacer = _Clock(), _Pacer()

    def _silent_a_minute() -> None:
        clock.advance(61.0)
        pacer.tick()

    container = _Container([_INIT, _silent_a_minute], hold=True)

    code, result = _run_primitive(tmp_path, 1, container, pacer, clock)

    assert container.kills == 1
    assert code == result.exit_code == 137
    assert result.error_category == STREAM_SILENT_CATEGORY


def test_the_primitive_never_watches_a_run_configured_off(tmp_path: Path) -> None:
    clock, pacer = _Clock(), _Pacer()
    container = _Container([_INIT, lambda: clock.advance(1_000_000)])

    code, result = _run_primitive(tmp_path, 0, container, pacer, clock)

    assert pacer.parked == 0
    assert container.kills == 0
    assert code == result.exit_code == 0
    assert result.error_category != STREAM_SILENT_CATEGORY
