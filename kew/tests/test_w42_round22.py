"""Round 22 host startup regressions against 2fcb7b45."""

import shutil
import signal
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from test_w42_round17 import _record
from test_w42_round18 import _ci_fix_scene
from test_w42_round21 import _receipt
from typer.testing import CliRunner

import kew.cli as cli
import kew.dispatch_core as core
import kew.orchestrator as orchestrator
from kew.dispatch_admission import PendingAdmission
from kew.run_store import SqliteRunStore
from kew.runners import get_runner
from kew.subprocess_utils import TOOL_PATHS
from kew.tracking import command_outcome

EVENT = '{"type":"system","subtype":"init","session_id":"round22"}'


class HostLaunchScene:
    """Use real timeout, allowlist resolution, Popen, streams, and receipts."""

    def __init__(self, stack, tmp_path, monkeypatch, store):
        self.store = store
        self.tmp_path = tmp_path
        self.config = _ci_fix_scene(stack, tmp_path, monkeypatch, store)
        self.config.retry.max_attempts = 3
        self.config.retry.retriable_exit_codes = [125, 126, 127]
        self.config.retry.backoff_base_seconds = 0
        self.argv = [str(tmp_path / "definitely-missing-kew-agent")]
        self.duration = "2s"
        timeout = shutil.which("timeout") or shutil.which("gtimeout")
        if timeout is None:
            pytest.skip("GNU timeout or gtimeout is required")
        self.timeout_cmd = Path(timeout).name
        monkeypatch.setitem(TOOL_PATHS, self.timeout_cmd, timeout)
        monkeypatch.setitem(TOOL_PATHS, sys.executable, sys.executable)
        self.results = []
        self.settled = []
        self.provisional = []
        self.accept = orchestrator._accept_process_ownership
        stack.enter_context(patch.object(core, "_admit_attempt_route_profile"))
        stack.enter_context(
            patch.object(core, "_contained_execution_for", return_value=None)
        )
        stack.enter_context(patch.object(orchestrator, "_preflight_runner_workspace"))
        stack.enter_context(
            patch.object(
                orchestrator, "find_timeout_cmd", side_effect=self.find_timeout
            )
        )
        stack.enter_context(
            patch.object(orchestrator, "_build_agent_cmd", side_effect=self.build)
        )
        stack.enter_context(
            patch.object(
                orchestrator, "_accept_process_ownership", side_effect=self.own
            )
        )
        stack.enter_context(patch.object(core, "run_agent", side_effect=self.run))

    def find_timeout(self):
        return self.timeout_cmd

    def build(self, runner, prompt, config, env, resume, model, budget, timeout):
        cmd, tool, seconds = orchestrator._wrap_with_timeout(self.argv, config, timeout)
        if tool:
            cmd[4] = self.duration
        return cmd, tool, seconds

    def own(self, admission, handle, result):
        self.accept(admission, handle, result)
        if admission is not None and handle is None:
            self.provisional.append(
                (admission.cleanup_owned, admission._settled_started)
            )

    def run(self, **kwargs):
        result = orchestrator.run_agent(**kwargs)
        self.results.append(result)
        self.settled.append(kwargs["admission"]._settled_started)
        return result

    def stub(self, code, *, event=False, log=False, sleep=False):
        script = f"print({EVENT!r}, flush=True); " if event else ""
        if log:
            script += "print('Agent initialized', flush=True); "
        if sleep:
            script += "import time; time.sleep(5); "
        self.argv = [sys.executable, "-c", script + f"raise SystemExit({code})"]

    def invoke(self):
        return CliRunner().invoke(cli.app, ["ci-fix", "87"])

    def assert_provisional(self):
        assert self.provisional
        assert all(owned and not settled for owned, settled in self.provisional), (
            "Popen must transfer cleanup ownership without settling agent startup."
        )


@pytest.fixture
def host_scene(tmp_path, monkeypatch):
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        yield HostLaunchScene(stack, tmp_path, monkeypatch, store)


def _assert_refused_and_reusable(scene, code):
    command = scene.invoke()
    assert command.exit_code == 2, command.output
    result = scene.results[-1]
    assert result.exit_code == code
    assert result.launched is False
    assert result.error_category == "sandbox_startup_refused"
    assert f"exit {code}" in result.stop_reason
    assert command_outcome(result).kind == "not_run"
    assert scene.store.count_launched_ci_fix_attempts("o/r", 87) == 0
    assert len(scene.results) == 1, "A refused wrapper must end the released slot."
    row = scene.store.list_all()[0]
    assert row.error_category == "sandbox_startup_refused"
    receipt = _receipt(scene.store, row.run_id)
    assert receipt["state"] == "refused"
    assert receipt["ordinal"] is None
    assert receipt["launched_at"] is None
    assert not scene.store.list_pending()
    # A later command must obtain a fresh slot, even at an attempt limit of one.
    scene.config.triggers.ci_failure_max_retries = 1
    scene.stub(1, event=True)
    later = scene.invoke()
    assert later.exit_code == 1, later.output
    assert len(scene.results) == 2
    assert scene.store.count_launched_ci_fix_attempts("o/r", 87) == 1
    assert scene.settled[-1]
    started = _receipt(scene.store, scene.results[-1].run_id)
    assert started["state"] == "started"
    assert started["ordinal"] == 1
    assert started["run_id"] != receipt["run_id"]
    scene.assert_provisional()


@pytest.mark.serial
def test_round22_missing_inner_executable_refuses_without_attempt(host_scene):
    """Base returns public 1 instead of 2 and consumes the repair slot."""
    _assert_refused_and_reusable(host_scene, 127)


@pytest.mark.serial
def test_round22_nonexecutable_inner_file_refuses_without_attempt(host_scene):
    """Base counts a wrapper exit 126 as an executed repair."""
    inner = host_scene.tmp_path / "nonexecutable-agent"
    inner.write_text("#!/bin/sh\nexit 0\n")
    inner.chmod(0o600)
    host_scene.argv = [str(inner)]
    _assert_refused_and_reusable(host_scene, 126)


@pytest.mark.serial
def test_round22_wrapper_failure_125_refuses_without_attempt(host_scene):
    """Base consumes a slot when timeout itself rejects its duration."""
    host_scene.duration = "invalid-duration"
    command = host_scene.invoke()
    assert command.exit_code == 2, command.output
    result = host_scene.results[-1]
    assert result.exit_code == 125
    assert "exit 125" in result.stop_reason
    assert result.error_category == "sandbox_startup_refused"
    assert host_scene.store.count_launched_ci_fix_attempts("o/r", 87) == 0
    assert _receipt(host_scene.store, result.run_id)["state"] == "refused"
    host_scene.assert_provisional()


def _assert_started(scene, code):
    scene.config.retry.max_attempts = 1
    command = scene.invoke()
    assert command.exit_code == 1, command.output
    result = scene.results[-1]
    assert result.exit_code == code
    assert result.launched is True
    assert scene.settled[-1], "Workload evidence must settle the provisional receipt."
    assert result.error_category != "sandbox_startup_refused"
    assert _receipt(scene.store, result.run_id)["state"] == "started"
    assert scene.store.count_launched_ci_fix_attempts("o/r", 87) == 1
    scene.assert_provisional()


@pytest.mark.serial
def test_round22_agent_event_then_exit_one_starts(host_scene):
    """Base settles startup at Popen, before the event is observed."""
    host_scene.stub(1, event=True)
    _assert_started(host_scene, 1)


@pytest.mark.serial
def test_round22_silent_timeout_124_starts(host_scene):
    """Base settles startup before the timeout proves execution."""
    host_scene.duration = "0.1s"
    host_scene.stub(0, sleep=True)
    _assert_started(host_scene, 124)


@pytest.mark.serial
@pytest.mark.parametrize("output", ["event", "log"])
def test_round22_output_then_exit_127_starts(host_scene, output):
    """Base settles before workload evidence; output must win over exit 127."""
    host_scene.stub(127, event=output == "event", log=output == "log")
    _assert_started(host_scene, 127)


@pytest.mark.serial
def test_round22_direct_missing_raises_before_ownership_without_receipt(host_scene):
    """Base prematurely settles the real direct-launch control's ownership."""
    host_scene.timeout_cmd = None
    host_scene.stub(1, event=True)
    # Establish the direct path's provisional handoff before its failure control.
    host_scene.config.retry.max_attempts = 1
    assert host_scene.invoke().exit_code == 1
    before = list(host_scene.provisional)
    missing = str(host_scene.tmp_path / "missing-direct-agent")
    TOOL_PATHS[missing] = missing
    host_scene.argv = [missing]
    host_scene.store.save(_record("direct-missing"))
    admission = PendingAdmission(host_scene.store, "direct-missing")
    with admission, pytest.raises(FileNotFoundError):
        orchestrator.run_agent(
            runner=get_runner("claude-code"),
            prompt="repair",
            worktree_dir=host_scene.tmp_path,
            config=host_scene.config,
            issue_num=42,
            admission=admission,
        )
    assert not admission.cleanup_owned
    assert not admission.startup_started
    assert not _receipt(host_scene.store, "direct-missing")
    assert host_scene.provisional == before
    host_scene.assert_provisional()


@pytest.mark.serial
def test_round22_signal_before_output_keeps_startup_unknown(host_scene):
    """Base falsely confirms execution after an eventless signal death."""
    host_scene.config.retry.max_attempts = 1
    host_scene.argv = [
        sys.executable,
        "-c",
        f"import os; os.kill(os.getpid(), {signal.SIGTERM})",
    ]
    command = host_scene.invoke()
    assert command.exit_code == 1, command.output
    result = host_scene.results[-1]
    assert result.launched is None, "A signal without output cannot confirm startup."
    assert result.error_category == "sandbox_startup_unknown"
    assert host_scene.store.count_launched_ci_fix_attempts("o/r", 87) == 1
    assert not host_scene.settled[-1]
    host_scene.assert_provisional()
