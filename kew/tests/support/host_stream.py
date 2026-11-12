"""Drive the host orchestrator's real stream loop over scripted agent output.

The agent process is faked at the process boundary (``popen``, the selector,
the process group a stop signals), and stays alive for the whole stream, so
only kew's own decision can stop it. Each line is parsed by a real runner's
parser, so the events kew judges are the ones that runner would produce.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig
from kew.orchestrator import run_agent
from kew.runners import get_runner
from kew.runners.protocol import ResultClassification, StreamEvent
from kew.tracking import DispatchResult


class StreamRunner:
    """An agent runner whose lines are parsed by the real *parser_name* runner.

    It launches nothing (the process is faked) and classifies every exit as
    the runner's own clean verdict, so whatever the result carries afterwards
    is kew's stream handling, not the classification.
    """

    required_credentials = ()

    def __init__(self, parser_name: str) -> None:
        self._parser = get_runner(parser_name)

    @property
    def name(self) -> str:
        return "fake"

    def build_command(self, *, prompt, config, env, model=""):
        return ["echo", prompt]

    def parse_line(self, line: str) -> list[StreamEvent]:
        return self._parser.parse_line(line)

    def classify_result(self, *, exit_code, last_output, config):
        return ResultClassification(category="")

    def supports_resume(self) -> bool:
        return False

    def build_resume_command(self, *, session_id, prompt, config, env, model=""):
        raise NotImplementedError

    def enrich_result(self, result, config) -> None:
        return None


class _ReadySelector:
    """Every read is ready, so the loop reads the next scripted line at once."""

    def register(self, fileobj, events, data=None):
        return None

    def unregister(self, fileobj):
        return None

    def select(self, timeout=None):
        return [True]

    def close(self):
        return None


@dataclass
class HostStreamRun:
    """What the faked process saw: the result, the process and the group kill."""

    result: DispatchResult
    proc: MagicMock
    killpg: MagicMock


def _live_process(lines: list[str]) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = MagicMock()
    proc.stdout.readline.side_effect = [*lines, ""]
    proc.returncode = 0
    proc.pid = 4321
    proc.poll.return_value = None  # still running while the stream is read

    def _exits(timeout=None):
        proc.poll.return_value = 0  # a waited-on process has exited
        return 0

    proc.wait.side_effect = _exits
    return proc


def run_host_stream(
    tmp_path: Path,
    lines: list[str],
    *,
    parser: str = "claude-code",
    config: DispatchConfig | None = None,
    **run_agent_kwargs: Any,
) -> HostStreamRun:
    """Run the host stream loop over *lines* (each ending in a newline)."""
    proc = _live_process(lines)
    if config is None:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
    kwargs: dict[str, Any] = {
        "prompt": "fix it",
        "worktree_dir": tmp_path,
        "issue_num": 1798,
        "model": "claude-sonnet-4-6",
        **run_agent_kwargs,
    }
    with (
        patch("kew.orchestrator.popen", return_value=proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_ReadySelector(),
        ),
        patch("kew.orchestrator.os.getpgid", return_value=proc.pid),
        patch("kew.orchestrator.os.killpg") as killpg,
    ):
        result = run_agent(runner=StreamRunner(parser), config=config, **kwargs)
    return HostStreamRun(result=result, proc=proc, killpg=killpg)
