"""The contained dispatch CPU-cap retry runs INSIDE the run's timeout window.

A refusal near the deadline must not buy a second full window: the ``timeout``
wrapper prefix is rebuilt with the remaining seconds, the watchdog gets the
same remainder, and no remaining time means no retry (#1822, round 5).
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from kew.orchestrator import (
    _contained_cpu_retry_command,
    _timeout_prefix_with_remaining,
    _TimeoutEnforcement,
)
from kew.sandbox import ContainedExecution, build_run_argv

_PREFIX = ["/usr/bin/timeout", "--foreground", "-k", "30s", "5m"]


def _agent_argv(tmp_path: Path) -> list[str]:
    return build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=tmp_path / "checkout",
        slug="42",
        network="allowlist",
        runner="claude-code",
    )


class TestTimeoutPrefix:
    def test_the_wrapper_duration_is_rebuilt_from_the_remainder(self):
        assert _timeout_prefix_with_remaining(_PREFIX, 100.2) == [
            "/usr/bin/timeout",
            "--foreground",
            "-k",
            "30s",
            "101s",
        ]

    def test_an_unknown_prefix_is_left_alone(self):
        assert _timeout_prefix_with_remaining([], 100) == []
        assert _timeout_prefix_with_remaining(["nice", "-n", "5"], 100) == [
            "nice",
            "-n",
            "5",
        ]


class TestRetryCommand:
    def _refused(self) -> MagicMock:
        proc = MagicMock(spec=subprocess.Popen)
        proc.wait.return_value = 125
        return proc

    def test_retry_returns_the_prefix_unassembled_for_a_fresh_remainder(self, tmp_path):
        # The caller rebuilds the prefix from the time left AFTER validation,
        # so the helper must hand back the raw prefix, not a bounded command.
        argv = _agent_argv(tmp_path)
        contained = ContainedExecution.from_argv(argv)
        retry_argv = [*argv]
        retry_argv[argv.index("--cpus") + 1] = "1"

        with patch("kew.orchestrator._cpu_cap_retry_argv", return_value=retry_argv):
            retry = _contained_cpu_retry_command(
                self._refused(),
                [*_PREFIX, *argv],
                contained,
                Decimal("2"),
                remaining_seconds=42.5,
            )

        assert retry is not None
        prefix, returned_argv, execution = retry
        assert prefix == _PREFIX

        def without_nonce(argv):
            return [arg for arg in argv if not arg.startswith("KEW_AGENT_START_NONCE=")]

        assert without_nonce(returned_argv) == without_nonce(retry_argv)
        assert execution.argv == tuple(returned_argv)
        old_nonce = next(
            arg for arg in retry_argv if arg.startswith("KEW_AGENT_START_NONCE=")
        )
        assert old_nonce not in returned_argv
        assert _timeout_prefix_with_remaining(prefix, 42.5)[4] == "43s"

    def test_no_remaining_time_means_no_retry(self, tmp_path):
        argv = _agent_argv(tmp_path)
        contained = ContainedExecution.from_argv(argv)
        proc = self._refused()

        with patch("kew.orchestrator._cpu_cap_retry_argv") as build:
            retry = _contained_cpu_retry_command(
                proc, [*_PREFIX, *argv], contained, Decimal("2"), remaining_seconds=0.0
            )

        assert retry is None
        build.assert_not_called()
        proc.wait.assert_not_called()


class TestLaunchPlan:
    def _retry(self, tmp_path):
        argv = _agent_argv(tmp_path)
        retry_argv = [*argv]
        retry_argv[argv.index("--cpus") + 1] = "1"
        return (_PREFIX, retry_argv, ContainedExecution.from_argv(retry_argv))

    def test_validation_that_spends_the_window_cancels_the_relaunch(self, tmp_path):
        from kew.orchestrator import _cpu_retry_launch_plan

        # launch at 1000; validation took until 1301: the 300s window is spent.
        clock = iter([1000.0, 1301.0])
        enforcement = _TimeoutEnforcement(
            "/usr/bin/timeout", 300, now=lambda: next(clock)
        )
        with patch("kew.orchestrator.popen", return_value=MagicMock()):
            enforcement.launch(["a"], cwd=".", env={})

        assert _cpu_retry_launch_plan(self._retry(tmp_path), enforcement) is None

    def test_relaunch_is_bounded_by_the_time_left_after_validation(self, tmp_path):
        from kew.orchestrator import _cpu_retry_launch_plan

        # launch at 1000; validation ended at 1290: 10s remain, not 300.
        clock = iter([1000.0, 1290.0])
        enforcement = _TimeoutEnforcement(
            "/usr/bin/timeout", 300, now=lambda: next(clock)
        )
        with patch("kew.orchestrator.popen", return_value=MagicMock()):
            enforcement.launch(["a"], cwd=".", env={})

        plan = _cpu_retry_launch_plan(self._retry(tmp_path), enforcement)

        assert plan is not None
        cmd, _contained, remaining = plan
        assert remaining == 10.0
        assert cmd[:5] == ["/usr/bin/timeout", "--foreground", "-k", "30s", "10s"]

    def test_python_fallback_relaunch_keeps_an_empty_prefix(self, tmp_path):
        from kew.orchestrator import _cpu_retry_launch_plan

        clock = iter([1000.0, 1100.0])
        enforcement = _TimeoutEnforcement(None, 300, now=lambda: next(clock))
        with patch("kew.orchestrator.popen", return_value=MagicMock()):
            enforcement.launch(["a"], cwd=".", env={})
        _prefix, retry_argv, contained = self._retry(tmp_path)

        plan = _cpu_retry_launch_plan(([], retry_argv, contained), enforcement)

        assert plan is not None
        assert plan[0] == retry_argv
        assert plan[2] == 200.0


class TestTimeoutEnforcementWindow:
    def test_relaunch_keeps_the_first_launch_window(self):
        # launch, remaining(), remaining() after relaunch, elapsed()
        clock = iter([1000.0, 1200.0, 1250.0, 1250.0])
        enforcement = _TimeoutEnforcement(
            "/usr/bin/timeout", 300, now=lambda: next(clock)
        )
        with patch("kew.orchestrator.popen") as popen:
            popen.return_value = MagicMock(spec=subprocess.Popen)
            enforcement.launch(["a"], cwd=".", env={})
            assert enforcement.remaining() == 100.0
            enforcement.relaunch(["b"], cwd=".", env={})
            # The relaunch did not reopen the window: 250s elapsed, 50 left.
            assert enforcement.remaining() == 50.0
            assert enforcement.elapsed() == 250.0

    def test_python_fallback_wait_uses_the_remainder(self):
        clock = iter([1000.0, 1000.0, 1100.0, 1100.0])
        enforcement = _TimeoutEnforcement(None, 300, now=lambda: next(clock))
        proc = MagicMock(spec=subprocess.Popen)
        with patch("kew.orchestrator.popen", return_value=proc):
            enforcement.launch(["a"], cwd=".", env={})
        enforcement.wait(proc)
        proc.wait.assert_called_once_with(timeout=200.0)
