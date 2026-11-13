"""A runner killed by SIGKILL or SIGABRT says so, and is not ``permanent`` (#2887).

An OOM kill (137) or an abort (134) prints no protocol result. The PR #2885
review died with exit 134 and recorded ``failed (exit 134):`` with nothing
after the colon, classified ``permanent``. The run now records the signal and
the runner's last output lines, and reads as ``resource``: retried once.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import MockSelector

from kew.config import DispatchConfig
from kew.dispatch_core import _resource_retry_spent
from kew.orchestrator import run_agent
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.codex import CodexRunner
from kew.runners.opencode import OpenCodeRunner
from kew.runners.protocol import AgentRunner
from kew.sandbox import ResourceCapCode
from kew.tracking import DispatchResult

_INIT_EVENT = json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"})


def _popen(exit_code: int, lines: list[str]) -> MagicMock:
    """A runner process that wrote *lines* and then exited *exit_code*."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = MagicMock()
    proc.returncode = exit_code
    proc.poll.return_value = exit_code
    proc.stdout.readline.side_effect = [*lines, ""]
    return proc


def _run(tmp_path: Path, exit_code: int, lines: list[str]) -> DispatchResult:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.orchestrator.popen", return_value=_popen(exit_code, lines)),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=MockSelector()
        ),
    ):
        return run_agent(
            runner=ClaudeCodeRunner(),
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=2887,
            timeout_override_minutes=60,
        )


@pytest.mark.parametrize(("exit_code", "signal"), [(134, "SIGABRT"), (137, "SIGKILL")])
def test_a_signal_exit_records_the_signal_and_the_last_output_lines(
    tmp_path: Path, exit_code: int, signal: str
) -> None:
    lines = [
        _INIT_EVENT + "\n",
        *(f"noise {n}\n" for n in range(4)),
        "FATAL ERROR: Reached heap limit Allocation failed\n",
        "Aborted (core dumped)\n",
    ]

    result = _run(tmp_path, exit_code, lines)

    assert result.exit_code == exit_code
    assert result.error_category == "resource"
    assert result.error_category != "permanent"
    assert result.stop_reason.startswith(f"killed by {signal} (exit {exit_code})")
    # The LAST lines, in order; the protocol event and the oldest noise are not.
    assert result.stop_reason.endswith(
        "last output: noise 1 | noise 2 | noise 3 | "
        "FATAL ERROR: Reached heap limit Allocation failed | Aborted (core dumped)"
    )
    assert "noise 0" not in result.stop_reason
    assert '"type"' not in result.stop_reason


def test_a_silent_signal_exit_still_names_its_signal(tmp_path: Path) -> None:
    result = _run(tmp_path, 134, [])

    assert result.stop_reason == "killed by SIGABRT (exit 134)"
    assert result.error_category == "resource"


def test_an_ordinary_failure_exit_is_unchanged(tmp_path: Path) -> None:
    result = _run(tmp_path, 2, ["something broke\n"])

    assert result.error_category == "permanent"
    assert result.stop_reason == ""


def test_a_long_runtime_line_is_kept_bounded(tmp_path: Path) -> None:
    result = _run(tmp_path, 134, ["x" * 10_000 + "\n"])

    assert len(result.stop_reason) < 400


def _attempt(category: str) -> DispatchResult:
    return DispatchResult(
        exit_code=134, issue_num=2887, log_file=Path("run.log"), error_category=category
    )


def test_a_resource_exhaustion_is_retried_once_not_twice() -> None:
    first = _attempt("resource")
    assert _resource_retry_spent(first, None) is False
    assert _resource_retry_spent(_attempt("resource"), first) is True
    # A resource death after some other failure still earns its one retry.
    assert _resource_retry_spent(_attempt("resource"), _attempt("transient")) is False


def _run_with(
    tmp_path: Path,
    runner: AgentRunner,
    exit_code: int,
    lines: list[str],
    model: str,
) -> DispatchResult:
    """*runner*'s binary wrote *lines* and then exited *exit_code*."""
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.orchestrator.popen", return_value=_popen(exit_code, lines)),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=MockSelector()
        ),
    ):
        return run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=3191,
            timeout_override_minutes=60,
            model=model,
        )


@pytest.mark.parametrize(
    ("runner", "model"),
    [
        (ClaudeCodeRunner, "claude-sonnet-4-6"),
        (CodexRunner, "gpt-5-codex"),
        (OpenCodeRunner, "anthropic/claude-sonnet-4-6"),
    ],
    ids=["claude-code", "codex", "opencode"],
)
@pytest.mark.parametrize(("exit_code", "signal"), [(134, "SIGABRT"), (137, "SIGKILL")])
def test_every_runner_names_a_signal_exit_never_a_placeholder(
    tmp_path: Path,
    runner: type[AgentRunner],
    model: str,
    exit_code: int,
    signal: str,
) -> None:
    # Codex reports a run that saw no turn complete as ``incomplete`` (#3191):
    # the signal and the runner's last stderr line say more, and replace it.
    lines = ["<--- JS stacktrace --->\n", "Aborted\n"]
    result = _run_with(tmp_path, runner(), exit_code, lines, model)

    assert result.stop_reason == (
        f"killed by {signal} (exit {exit_code}); "
        "last output: <--- JS stacktrace ---> | Aborted"
    )
    assert result.error_category == "resource"


def test_a_classified_oom_earns_its_one_retry_before_the_cap_stops_it(
    tmp_path: Path,
) -> None:
    """A confirmed OOM (137 + the runtime's OOM event) reads as ``resource``."""
    from kew.exit_signal import name_signal_exit

    def oom_killed() -> DispatchResult:
        result = DispatchResult(exit_code=137, issue_num=3191, log_file=tmp_path)
        result.hard_stop = "resource_cap"
        result.resource_cap_code = ResourceCapCode.OOM.value
        result.resource_cap_reason = "kew-3191: OOM-killed"
        result.diagnostic_tail = ["Killed"]
        name_signal_exit(result)
        return result

    first = oom_killed()
    assert first.error_category == "resource"
    assert first.stop_reason == (
        "killed by SIGKILL (exit 137, out of memory); last output: Killed"
    )
    assert _resource_retry_spent(first, None) is False
    assert _resource_retry_spent(oom_killed(), first) is True
