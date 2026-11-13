"""Tests for the runner-agnostic subprocess orchestrator."""

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import MockSelector as _MockSelector
from support.host_stream import run_host_stream

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.inference_transport import ResolvedInferenceTransport
from kew.orchestrator import (
    _is_timeout_exit,
    _kill_container_after_timeout,
    _should_trip_budget,
    _wrap_with_timeout,
    run_agent,
)
from kew.runners.protocol import ResultClassification, StreamEvent
from kew.sandbox import (
    MAX_LINE_BYTES,
    RESOURCE_CAP_EVENT,
    RUNTIME_CONTROL_TIMEOUT_SECONDS,
    ContainerHandle,
)
from kew.tracking import DispatchResult
from kew.transcript_metrics import FirstEditMetrics, read_derived_evidence


def _resolved_transport(mode: str = "switchyard") -> ResolvedInferenceTransport:
    return ResolvedInferenceTransport(
        mode=mode,  # type: ignore[arg-type]
        requested_model="original-model",
        model="exact-route" if mode == "switchyard" else "original-model",
        config_sha256="a" * 64,
        endpoint="https://switchyard.example" if mode == "switchyard" else None,
        route="exact-route" if mode == "switchyard" else None,
        expected_version="pinned" if mode == "switchyard" else "",
    )


def test_managed_context_passes_the_resolved_ingress_format(tmp_path: Path) -> None:
    from kew.orchestrator import managed_switchyard_context

    binary = tmp_path / "switchyard"
    binary.write_bytes(b"binary")
    config_path = tmp_path / "routes.toml"
    config_path.write_text("schema_version = 1\n")
    settings = SwitchyardConfig(
        endpoint="http://127.0.0.1:4000",
        route="switchyard/llm_classifier",
        expected_version="pinned",
        managed=True,
        config_path=config_path,
        binary=binary,
        binary_sha256="a" * 64,
    )
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            allow_adaptive=True,
            switchyard=settings,
        )
    )
    transport = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="original-model",
        model="switchyard/llm_classifier",
        config_sha256="a" * 64,
        endpoint=settings.endpoint,
        route=settings.route,
        api_format="openai-responses",
        managed=True,
        expected_version=settings.expected_version,
    )

    with patch("kew.orchestrator.ManagedSwitchyardSupervisor") as supervisor:
        managed_switchyard_context(config, transport)

    supervisor.assert_called_once_with(
        settings,
        classifier_ingress_format="openai-responses",
    )


def test_codex_preflight_rejects_before_spawn(tmp_path):
    from kew.runners.codex import CodexProjectConfigError, CodexRunner

    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("forbidden = true")
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.orchestrator.popen") as mock_popen,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        pytest.raises(CodexProjectConfigError, match=r"\.codex/config.toml"),
    ):
        run_agent(
            runner=CodexRunner(),
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="gpt-5.6-sol",
        )
    mock_popen.assert_not_called()


def test_codex_spawn_receives_sanitized_environment(tmp_path, monkeypatch):
    from kew.runners.codex import CodexRunner

    monkeypatch.setenv("HOME", "/synthetic/home")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("NOVEL_API_KEY", "secret")
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    process = TestRunAgent._mock_popen(
        exit_code=0,
        lines=[
            '{"type":"thread.started","thread_id":"t1"}\n',
            '{"type":"turn.completed","usage":{}}\n',
        ],
    )
    with (
        patch("kew.orchestrator.popen", return_value=process) as mock_popen,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        run_agent(
            runner=CodexRunner(),
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="gpt-5.6-sol",
        )
    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env["HOME"] == "/synthetic/home"
    assert "OPENAI_API_KEY" not in child_env
    assert "NOVEL_API_KEY" not in child_env


def test_every_spawned_agent_carries_the_assignment_marker(tmp_path, monkeypatch):
    """`run_agent` must always set AGENT_ASSIGNED_WORKTREE on the child.

    This is the control that makes the security guard's operator exemption
    sound. The guard treats the marker's ABSENCE as "no dispatch is running,
    so there is no assignment to be outside of" — which is only true while
    `run_agent` is the single spawn point and never forgets to set it. A new
    spawn path that skipped this would hand a dispatched agent the operator's
    permissions silently, so pin it here rather than in the guard, which cannot
    see kew's side of the contract.
    """
    from kew.runners.codex import CodexRunner

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    process = TestRunAgent._mock_popen(
        exit_code=0,
        lines=[
            '{"type":"thread.started","thread_id":"t1"}\n',
            '{"type":"turn.completed","usage":{}}\n',
        ],
    )
    with (
        patch("kew.orchestrator.popen", return_value=process) as mock_popen,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        run_agent(
            runner=CodexRunner(),
            prompt="fix",
            worktree_dir=worktree,
            config=config,
            issue_num=42,
            model="gpt-5.6-sol",
        )
    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env.get("AGENT_ASSIGNED_WORKTREE") == str(worktree.resolve()), (
        "run_agent did not mark the child as a dispatched agent; the security "
        "guard would treat it as the operator and skip the assigned-worktree "
        "rule entirely."
    )


def test_codex_resume_revalidates_before_second_spawn(tmp_path):
    from kew.runners.codex import CodexProjectConfigError, CodexRunner

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    runner = CodexRunner()
    session_id = "12345678-1234-5678-9abc-123456789abc"
    process = TestRunAgent._mock_popen(
        exit_code=0,
        lines=[
            json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n",
            '{"type":"turn.completed","usage":{}}\n',
        ],
    )
    with (
        patch("kew.orchestrator.popen", return_value=process) as mock_popen,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        run_agent(
            runner=runner,
            prompt="first",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="gpt-5.6-sol",
        )
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "config.toml").write_text("forbidden = true")
        with pytest.raises(CodexProjectConfigError):
            run_agent(
                runner=runner,
                prompt="resume",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                resume_session_id=session_id,
                model="gpt-5.6-sol",
            )
    assert mock_popen.call_count == 1


def test_non_codex_runner_skips_codex_preflight(tmp_path):
    runner = FakeRunner(exit_code=0)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    process = TestRunAgent._mock_popen(exit_code=0, lines=[])
    with (
        patch("kew.runners.codex.validate_codex_project_tree") as validator,
        patch("kew.orchestrator.popen", return_value=process),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        run_agent(
            runner=runner,
            prompt="ordinary",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )
    validator.assert_not_called()


class FakeRunner:
    """Test double implementing AgentRunner protocol."""

    required_credentials = ()

    def __init__(
        self,
        *,
        lines: list[str] | None = None,
        exit_code: int = 0,
        classification: ResultClassification | None = None,
        stream_events: list[StreamEvent] | None = None,
    ):
        self._lines = lines or []
        self._exit_code = exit_code
        self._classification = classification or ResultClassification(
            category="permanent"
        )
        self._stream_events = stream_events or []
        self.build_command_called = False
        self.parse_line_calls: list[str] = []
        self.classify_result_called = False
        self.enrich_result_called = False

    @property
    def name(self) -> str:
        return "fake"

    def build_command(self, *, prompt, config, env, model=""):
        self.build_command_called = True
        self.received_model = model
        return ["echo", prompt]

    def parse_line(self, line):
        self.parse_line_calls.append(line)
        if self._stream_events:
            return [self._stream_events.pop(0)]
        return []

    def classify_result(self, *, exit_code, last_output, config):
        self.classify_result_called = True
        return self._classification

    def supports_resume(self):
        return False

    def build_resume_command(self, *, session_id, prompt, config, env, model=""):
        self.received_model = model
        raise NotImplementedError

    def enrich_result(self, result, config):
        self.enrich_result_called = True


def test_transport_loss_overrides_runner_classification(tmp_path):
    runner = FakeRunner(classification=ResultClassification(category="transient"))
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    mock_proc = TestRunAgent._mock_popen(exit_code=1)

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            inference_transport=_resolved_transport(),
            transport_health_check=lambda: False,
        )

    assert result.error_category == "transport_unavailable"
    assert result.subtype == "proxy_lost"


def test_direct_transport_does_not_run_health_check(tmp_path):
    runner = FakeRunner()
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    health_check = MagicMock(return_value=False)
    mock_proc = TestRunAgent._mock_popen(exit_code=0)

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
    ):
        run_agent(
            runner=runner,
            prompt="unchanged",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="original-model",
            inference_transport=_resolved_transport("direct"),
            transport_health_check=health_check,
        )

    health_check.assert_not_called()
    assert runner.received_model == "original-model"
    assert mock_proc.method_calls


class TestRunAgent:
    @staticmethod
    def _mock_popen(*, exit_code=0, lines=None):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.stdout = MagicMock()
        mock_proc.returncode = exit_code
        mock_proc.poll.return_value = exit_code
        stdout_lines = list(lines or [])
        stdout_lines.append("")  # EOF
        mock_proc.stdout.readline.side_effect = stdout_lines
        return mock_proc

    def test_success_calls_all_hooks(self, tmp_path):
        runner = FakeRunner(exit_code=0)
        mock_proc = self._mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        assert runner.build_command_called
        assert runner.classify_result_called
        assert runner.enrich_result_called
        assert result.exit_code == 0

    def test_accumulates_usage_from_stream_events(self, tmp_path):
        events = [
            StreamEvent(
                raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
            ),
            StreamEvent(
                raw_line="l2", event_type="usage", input_tokens=200, output_tokens=100
            ),
        ]
        runner = FakeRunner(
            lines=["line1\n", "line2\n"],
            exit_code=0,
            stream_events=events,
        )
        mock_proc = self._mock_popen(exit_code=0, lines=["line1\n", "line2\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        assert result.input_tokens == 300
        assert result.output_tokens == 150

    def test_process_cleanup_on_exception(self, tmp_path):
        runner = FakeRunner()
        mock_proc = self._mock_popen(exit_code=1)
        mock_proc.poll.return_value = None  # Process still running
        mock_proc.stdout.readline.side_effect = OSError("read failed")
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            pytest.raises(OSError, match="read failed"),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        mock_proc.terminate.assert_called_once()

    def test_sets_error_category_from_classification(self, tmp_path):
        runner = FakeRunner(
            exit_code=1,
            classification=ResultClassification(
                category="transient",
                session_id="sess-123",
                subtype="api_error",
            ),
        )
        mock_proc = self._mock_popen(exit_code=1)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        assert result.error_category == "transient"
        assert result.session_id == "sess-123"
        assert result.subtype == "api_error"

    def test_model_forwarded_to_runner(self, tmp_path):
        """run_agent passes the model param through to build_command."""
        runner = FakeRunner(exit_code=0)
        mock_proc = self._mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-opus-4",
            )

        assert runner.received_model == "claude-opus-4"

    def test_budget_check_trip_terminates_and_flags(self, tmp_path):
        """When budget_check returns True (no compaction grace), the group
        is terminated and the run is flagged budget_exceeded (#401)."""
        usage_ev = StreamEvent(
            raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
        )
        events = [usage_ev]
        lines = ["line1\n"]
        runner = FakeRunner(lines=lines, exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=lines)
        mock_proc.poll.return_value = None  # still running when the watchdog trips
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch("kew.orchestrator._terminate_group") as mock_term,
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-sonnet-4-6",
                budget_check=lambda run_cost: run_cost > 0.0,  # trip after debounce
            )

        mock_term.assert_called_once()
        assert result.budget_exceeded is True
        assert result.error_category == "budget_exceeded"
        assert result.subtype == "watchdog_budget"

    def test_budget_trip_uses_graceful_grace_window(self, tmp_path):
        """A budget watchdog kill terminates with the longer wrap-up grace so the
        agent can commit WIP before SIGKILL (graceful budget stop, #859)."""
        events = [
            StreamEvent(
                raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
            )
        ]
        runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=["line1\n"])
        mock_proc.poll.return_value = None
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.agent.budget_stop_grace_seconds = 45

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch("kew.orchestrator._terminate_group") as mock_term,
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-sonnet-4-6",
                budget_check=lambda run_cost: run_cost > 0.0,
            )

        mock_term.assert_called_once()
        assert mock_term.call_args.kwargs["grace_seconds"] == 45

    def test_compaction_event_increments_count(self, tmp_path):
        """An autocompaction boundary event bumps result.compaction_count so the
        anomaly rule can later discount the compaction overhead (#859)."""
        events = [
            StreamEvent(raw_line="c1", event_type="compaction"),
            StreamEvent(
                raw_line="u1", event_type="usage", input_tokens=100, output_tokens=50
            ),
        ]
        runner = FakeRunner(lines=["c1\n", "u1\n"], exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=["c1\n", "u1\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-sonnet-4-6",
            )

        assert result.compaction_count == 1
        assert result.exit_code == 0

    def test_cost_anomaly_classification_survives_classify_result(self, tmp_path):
        """A cost-anomaly kill must keep error_category='circuit_breaker' —
        the runner's post-hoc classify_result (here 'permanent') must NOT
        overwrite it, or dispatch_core would store the run as a failure,
        not killed (#245)."""
        usage_ev = StreamEvent(
            raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
        )
        events = [usage_ev]
        lines = ["line1\n"]
        runner = FakeRunner(lines=lines, exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=lines)
        mock_proc.poll.return_value = None  # still running when the watchdog trips
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch("kew.orchestrator._terminate_group") as mock_term,
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-sonnet-4-6",
                anomaly_threshold_usd=0.0,  # trip after debounce
            )

        mock_term.assert_called_once()
        assert result.breaker_rule == "cost_anomaly"
        assert result.error_category == "circuit_breaker"  # NOT "permanent"
        assert result.subtype == "cost_anomaly"
        assert result.budget_exceeded is False

    def test_breaker_kills_attempt_and_preserves_dispatch_owner(self, tmp_path):
        import os
        import signal
        from datetime import UTC, datetime
        from functools import partial

        from kew.dispatch_core import _record_retry_pgid
        from kew.run_store import RunRecord, SqliteRunStore

        kernel_birth = "kernel-token"
        events = [
            StreamEvent(
                raw_line="usage", event_type="usage", input_tokens=100, output_tokens=50
            )
        ]
        runner = FakeRunner(lines=["usage\n"], exit_code=0, stream_events=events)
        proc = self._mock_popen(exit_code=0, lines=["usage\n"])
        proc.pid = 12345
        proc.poll.return_value = None
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            SqliteRunStore(str(tmp_path / "breaker.db")) as store,
            patch("kew.orchestrator.popen", return_value=proc),
            patch(
                "kew.run_store.process_start_time",
                return_value="Tue Sep 15 10:00:00 2026",
            ),
            patch("kew.run_store.process_start_token", return_value="kernel-token"),
            patch(
                "kew.process_groups.process_start_token", return_value="kernel-token"
            ),
            patch("kew.run_store.owner_alive", return_value=True),
            patch("kew.process_groups.owner_alive", return_value=True),
            patch("kew.run_store.attempt_descends_from", return_value=True),
            patch("kew.run_store.birth_matches", return_value=True),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch("kew.orchestrator.os.getpgid", return_value=proc.pid) as getpgid,
            patch("kew.orchestrator.os.killpg") as killpg,
        ):
            store.save(
                RunRecord(
                    run_id="breaker",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    pgid=os.getpgrp(),
                    owner_pid=os.getpgrp(),
                    owner_start_token=kernel_birth,
                )
            )
            result = run_agent(
                runner=runner,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                model="claude-sonnet-4-6",
                anomaly_threshold_usd=0.0,
                on_spawn=partial(_record_retry_pgid, store, "breaker"),
            )
            assert result.breaker_rule == "cost_anomaly"
            getpgid.assert_called_once_with(proc.pid)
            killpg.assert_called_once_with(proc.pid, signal.SIGTERM)
            record = store.get("breaker")
            assert record.pgid == os.getpgrp()
            assert record.attempt_pgid == proc.pid

    def test_budget_check_under_threshold_runs_to_completion(self, tmp_path):
        """A budget_check that never returns True does not terminate the run."""
        events = [
            StreamEvent(
                raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
            ),
        ]
        runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=["line1\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch("kew.orchestrator._terminate_group") as mock_term,
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                model="claude-sonnet-4-6",
                budget_check=lambda run_cost: run_cost > 1e9,  # never trips
            )

        mock_term.assert_not_called()
        assert result.budget_exceeded is False
        assert result.exit_code == 0

    def test_model_forwarded_to_runner_resume(self, tmp_path):
        """run_agent passes the model param through to build_resume_command."""
        runner = FakeRunner(exit_code=0)
        # Override to support resume for this test
        runner.supports_resume = lambda: True
        runner.build_resume_command = (
            lambda *, session_id, prompt, config, env, model="": (
                setattr(runner, "received_model", model) or ["echo", prompt]
            )
        )
        mock_proc = self._mock_popen(exit_code=0, lines=["output\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                resume_session_id="sess-123",
                model="claude-opus-4",
            )

        assert runner.received_model == "claude-opus-4"

    def test_captures_reported_model_from_stream_event(self, tmp_path):
        events = [
            StreamEvent(
                raw_line="l1",
                event_type="usage",
                input_tokens=10,
                output_tokens=5,
                metadata={"model": "anthropic/claude-sonnet-4-6"},
            ),
        ]
        runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=["line1\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )
        assert result.resolved_model == "anthropic/claude-sonnet-4-6"

    def test_resolved_model_stays_empty_when_runner_reports_none(self, tmp_path):
        # Honesty guard: opencode reports no model -> null, not the config model.
        events = [StreamEvent(raw_line="l1", event_type="usage", input_tokens=10)]
        runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=events)
        mock_proc = self._mock_popen(exit_code=0, lines=["line1\n"])
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )
        assert result.resolved_model == ""


class TestProcessGroupTracking:
    """run_agent launches the agent as a process-group leader and
    reports the pid via the on_spawn callback."""

    @staticmethod
    def _mock_popen_proc(*, pid=4242, exit_code=0):
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = pid
        mock_proc.stdout = MagicMock()
        mock_proc.returncode = exit_code
        mock_proc.poll.return_value = exit_code
        mock_proc.stdout.readline.side_effect = ["output\n", ""]
        return mock_proc

    def test_popen_called_with_start_new_session(self, tmp_path):
        runner = FakeRunner(exit_code=0)
        mock_proc = self._mock_popen_proc()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc) as mock_popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        assert mock_popen.call_args.kwargs["start_new_session"] is True

    def test_on_spawn_receives_pid(self, tmp_path):
        runner = FakeRunner(exit_code=0)
        mock_proc = self._mock_popen_proc(pid=31337)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        seen_pids: list[int] = []

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                on_spawn=seen_pids.append,
            )

        assert seen_pids == [31337]

    def test_on_spawn_exception_does_not_kill_dispatch(self, tmp_path):
        runner = FakeRunner(exit_code=0)
        mock_proc = self._mock_popen_proc()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        def _boom(pid: int) -> None:
            raise RuntimeError("db locked")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            result = run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
                on_spawn=_boom,
            )

        assert result.exit_code == 0


class TestChildEnvIsolation:
    """The agent subprocess must not inherit the orchestrator's KEW_* config.

    A leaked KEW_LOG_DIR once pointed a worktree kew at the live dispatch.db and
    auto-migrated it mid-drive (#688). The child env is built by dropping every
    KEW_* var while passing PATH/HOME/credentials through unchanged.
    """

    def test_build_child_env_strips_kew_vars_keeps_essentials(self, monkeypatch):
        from kew.orchestrator import _build_child_env

        monkeypatch.setenv("KEW_LOG_DIR", "/live/kew/logs")
        monkeypatch.setenv("KEW_ACTOR", "orchestrator")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/agent")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        env = _build_child_env()

        assert not any(k.startswith("KEW_") for k in env)
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/agent"
        assert env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_spawned_process_env_has_no_kew_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEW_LOG_DIR", "/live/kew/logs")
        monkeypatch.setenv("KEW_MAX_BUDGET", "99")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/agent")

        runner = FakeRunner(exit_code=0)
        mock_proc = TestProcessGroupTracking._mock_popen_proc()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with (
            patch("kew.orchestrator.popen", return_value=mock_proc) as mock_popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

        env = mock_popen.call_args.kwargs["env"]
        assert not any(k.startswith("KEW_") for k in env)
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/agent"

    def test_detached_publication_token_stays_out_of_agent_env(self, monkeypatch):
        from kew.orchestrator import _build_child_env
        from kew.publication import DETACHED_PUBLICATION_TOKEN_ENV

        monkeypatch.setenv(DETACHED_PUBLICATION_TOKEN_ENV, "gho_host_only")
        monkeypatch.setenv("GH_TOKEN", "gho_host_only")
        monkeypatch.setenv("GITHUB_TOKEN", "stale_fallback")

        env = _build_child_env()

        assert DETACHED_PUBLICATION_TOKEN_ENV not in env
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env

    def test_github_token_reaches_the_agent_without_detached_publication(
        self, monkeypatch
    ):
        """The GH_TOKEN filter is scoped to detached publication, not global.

        Foreground dispatch deliberately leaves the agent's GitHub credential
        intact; an unconditional filter would silently break it.
        """
        from kew.orchestrator import _build_child_env
        from kew.publication import DETACHED_PUBLICATION_TOKEN_ENV

        monkeypatch.delenv(DETACHED_PUBLICATION_TOKEN_ENV, raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_foreground")

        env = _build_child_env()

        assert env["GH_TOKEN"] == "gho_foreground"


def _anomaly_result(compaction_count: int = 0) -> DispatchResult:
    r = DispatchResult(exit_code=-1, issue_num=1, log_file=Path("/tmp/l"))
    r.compaction_count = compaction_count
    return r


def test_should_trip_budget_anomaly_trips_without_compaction():
    r = _anomaly_result(compaction_count=0)
    assert _should_trip_budget(10.0, None, 6.0, r) is True
    assert r.error_category == "circuit_breaker"


def test_should_trip_budget_anomaly_discounts_compaction_allowance():
    # Raw cost $10 > $6 threshold, but one compaction with a $5 allowance
    # discounts to $5 <= $6 — not a genuine anomaly (#859).
    r = _anomaly_result(compaction_count=1)
    assert (
        _should_trip_budget(10.0, None, 6.0, r, anomaly_compaction_allowance_usd=5.0)
        is False
    )
    assert r.breaker_rule == ""


def test_should_trip_budget_genuine_anomaly_survives_compaction_discount():
    r = _anomaly_result(compaction_count=1)
    assert (
        _should_trip_budget(20.0, None, 6.0, r, anomaly_compaction_allowance_usd=5.0)
        is True
    )
    assert r.error_category == "circuit_breaker"


def test_should_trip_budget_host_discounts_every_compaction():
    # Ten compactions at $1 each discount $10 off $15: $5 <= $6 on the host,
    # which counts compactions itself and caps none of them (#859).
    r = _anomaly_result(compaction_count=10)
    assert (
        _should_trip_budget(15.0, None, 6.0, r, anomaly_compaction_allowance_usd=1.0)
        is False
    )
    assert r.breaker_rule == ""


def test_should_pause_terminates_and_marks_paused(tmp_path):
    """should_pause() true on the first (immediate) check -> result.paused and a
    graceful terminate (NOT an error / not kill-first)."""
    runner = FakeRunner(exit_code=0)
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["a\n", "b\n", "c\n"])
    mock_proc.poll.return_value = None  # still running when paused
    mock_proc.pid = 4242
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
        patch("kew.orchestrator.os.getpgid", return_value=4242),
        patch("kew.orchestrator.os.killpg") as mock_killpg,
    ):
        result = run_agent(
            runner=runner,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            should_pause=lambda: True,  # fires on the immediate first check
        )

    assert result.paused is True
    # Graceful pause SIGTERMs the agent's whole process GROUP (once; wait succeeds so
    # no SIGKILL escalation), not just the leader.
    mock_killpg.assert_called_once()


def test_should_pause_is_throttled(tmp_path):
    """First check fires; further checks are throttled. With a FROZEN clock,
    should_pause is called exactly ONCE across many lines (not per-line)."""
    runner = FakeRunner(exit_code=0)
    mock_proc = TestRunAgent._mock_popen(
        exit_code=0, lines=[f"{i}\n" for i in range(50)]
    )
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    calls = {"n": 0}

    def _should_pause():
        calls["n"] += 1
        return False

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
    ):
        run_agent(
            runner=runner,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            should_pause=_should_pause,
            _now=lambda: 100.0,
        )  # frozen

    # last_check inits to now-3, so the first check fires (n>=1); the frozen clock
    # then keeps now-last_check at 0 < 3, so NO further checks -> exactly 1.
    assert calls["n"] == 1  # NOT 50 — throttled


def test_should_pause_exception_is_swallowed(tmp_path):
    """A raising should_pause must not crash the run nor pause it."""
    runner = FakeRunner(exit_code=0)
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["a\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    def _should_pause():
        raise RuntimeError("db hiccup")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            should_pause=_should_pause,
            _now=lambda: 100.0,
        )

    assert result.paused is False  # swallowed, run completed normally


def test_emit_stream_event_writes_step_line(tmp_path):
    import json

    from kew.orchestrator import _emit_stream_event
    from kew.runners.protocol import StreamEvent

    ef = tmp_path / "ev.jsonl"
    with open(ef, "w") as fh:
        _emit_stream_event(
            fh,
            StreamEvent(
                raw_line="",
                event_type="step",
                input_tokens=120,
                output_tokens=30,
            ),
            # Accumulated totals are deliberately DISTINCT from the per-step
            # tokens so the assertions below would fail if the step record ever
            # sourced its input/output from the accumulated totals.
            accumulated_input=500,
            accumulated_output=200,
            model="claude-sonnet-4-6",
        )
    lines = [json.loads(line) for line in ef.read_text().splitlines()]
    step = next(line for line in lines if line["type"] == "step")
    assert step["input"] == 120  # per-step, not accumulated 500
    assert step["output"] == 30  # per-step, not accumulated 200
    assert "cost_usd" in step


def test_emit_activity_writes_the_sanitized_line_not_provider_prose(tmp_path):
    """An `activity` event must carry only the sanitized progress line, never
    the provider's own message — that message is where a leaked secret rides."""
    import json

    from kew.orchestrator import _emit_stream_event
    from kew.runners.protocol import StreamEvent

    secret = "sk-live-SUPERSECRET-abc123"
    ef = tmp_path / "ev.jsonl"
    with open(ef, "w") as fh:
        _emit_stream_event(
            fh,
            StreamEvent(
                raw_line="", event_type="activity", message=f"leaking {secret}"
            ),
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4-6",
            safe_line="step 3 tokens=120 tools=Edit",
        )
    body = ef.read_text()
    assert secret not in body
    lines = [json.loads(line) for line in body.splitlines()]
    activity = next(line for line in lines if line["type"] == "activity")
    assert activity["msg"] == "step 3 tokens=120 tools=Edit"


def test_emit_activity_without_a_sanitized_line_writes_nothing(tmp_path):
    """With no vetted progress line, an activity event is dropped entirely
    rather than falling back to the raw provider message."""
    import io

    from kew.orchestrator import _emit_stream_event
    from kew.runners.protocol import StreamEvent

    fh = io.StringIO()
    _emit_stream_event(
        fh,
        StreamEvent(raw_line="", event_type="activity", message="raw provider prose"),
        accumulated_input=0,
        accumulated_output=0,
        model="claude-sonnet-4-6",
        safe_line=None,
    )
    assert fh.getvalue() == ""


def test_emit_tool_use_drops_the_raw_target_and_sanitizes_the_name(tmp_path):
    """A tool_use event keeps the sanitized tool NAME but never the raw target —
    the target is provider-controlled input that can carry a secret or a path."""
    import json

    from kew.orchestrator import _emit_stream_event
    from kew.runners.protocol import StreamEvent

    secret = "sk-live-INTARGET-xyz"
    ef = tmp_path / "ev.jsonl"
    with open(ef, "w") as fh:
        _emit_stream_event(
            fh,
            StreamEvent(
                raw_line="",
                event_type="tool_use",
                message="Bash",
                metadata={"target": f"cat /etc/passwd; echo {secret}"},
            ),
            accumulated_input=0,
            accumulated_output=0,
            model="claude-sonnet-4-6",
        )
    body = ef.read_text()
    assert secret not in body
    assert "/etc/passwd" not in body
    tool = next(json.loads(line) for line in body.splitlines())
    assert tool["type"] == "tool_use"
    assert tool["tool"] == "Bash"
    assert tool["target"] == ""


def test_emit_step_event_uses_per_step_cost_not_cumulative():
    """A 'step' event's cost must be computed from its OWN per-step tokens, not the
    accumulated totals — otherwise _finalize's sum(step costs) over-counts."""
    import io
    import json

    from kew.orchestrator import _cost_from_accumulated, _emit_stream_event
    from kew.runners.protocol import StreamEvent

    fh = io.StringIO()
    # per-step tokens (200/40); accumulated is larger (300/60) — cost MUST use per-step.
    ev = StreamEvent(
        raw_line="x", event_type="step", input_tokens=200, output_tokens=40
    )
    _emit_stream_event(
        fh,
        ev,
        accumulated_input=300,
        accumulated_output=60,
        model="claude-sonnet-4-6",
    )
    line = json.loads(fh.getvalue().strip().splitlines()[-1])
    assert line["type"] == "step"
    assert line["input"] == 200
    per_step = _cost_from_accumulated("claude-sonnet-4-6", 200, 40, None)
    cumulative = _cost_from_accumulated("claude-sonnet-4-6", 300, 60, None)
    assert line["cost_usd"] == pytest.approx(per_step)
    assert line["cost_usd"] != pytest.approx(cumulative)  # not the accumulated cost


def test_emit_step_event_prices_cache_tokens_at_discount():
    """A step's per-step cache tokens are priced at cache rates, not full input."""
    import io
    import json

    from kew.orchestrator import _cost_from_accumulated, _emit_stream_event
    from kew.runners.protocol import StreamEvent

    fh = io.StringIO()
    ev = StreamEvent(
        raw_line="x",
        event_type="step",
        input_tokens=100,
        output_tokens=40,
        cache_creation_tokens=2000,
        cache_read_tokens=8000,
    )
    _emit_stream_event(
        fh, ev, accumulated_input=100, accumulated_output=40, model="claude-sonnet-4-6"
    )
    line = json.loads(fh.getvalue().strip().splitlines()[-1])
    cache_aware = _cost_from_accumulated(
        "claude-sonnet-4-6",
        100,
        40,
        None,
        cache_creation_tokens=2000,
        cache_read_tokens=8000,
    )
    folded = _cost_from_accumulated("claude-sonnet-4-6", 100 + 2000 + 8000, 40, None)
    assert line["cost_usd"] == pytest.approx(cache_aware)
    assert line["cost_usd"] < folded  # cache discount applied
    # The cache split is logged alongside cost so the record is self-reconcilable.
    assert line["cache_creation"] == 2000
    assert line["cache_read"] == 8000


def test_budget_tripped_fires_on_cost_anomaly():
    """When live cost exceeds anomaly_threshold_usd (no compaction grace),
    the watchdog trips immediately."""
    import subprocess
    from unittest.mock import MagicMock

    from kew.orchestrator import _budget_tripped
    from kew.tracking import DispatchResult

    result = DispatchResult(exit_code=-1, issue_num=1, log_file=Path("/tmp/x"))
    result.accumulate_usage(input_tokens=0, output_tokens=1_000_000)
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None  # still running

    tripped = _budget_tripped(
        result,
        "claude-sonnet-4-6",
        None,
        None,
        proc,
        anomaly_threshold_usd=6.0,
    )
    assert tripped is True
    assert result.breaker_rule == "cost_anomaly"
    assert result.error_category == "circuit_breaker"
    assert result.subtype == "cost_anomaly"


def test_budget_tripped_no_anomaly_when_under_threshold():
    import subprocess
    from unittest.mock import MagicMock

    from kew.orchestrator import _budget_tripped
    from kew.tracking import DispatchResult

    result = DispatchResult(exit_code=-1, issue_num=1, log_file=Path("/tmp/x"))
    result.accumulate_usage(input_tokens=0, output_tokens=1_000_000)  # $15
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None

    tripped = _budget_tripped(
        result,
        "claude-sonnet-4-6",
        None,
        None,
        proc,
        anomaly_threshold_usd=100.0,  # $15 < $100 => no trip
    )
    assert tripped is False
    assert result.breaker_rule == ""


def test_session_id_captured_from_init_event(tmp_path):
    """session_id from the init stream event is captured on result, surviving
    a mid-flight SIGTERM where the terminal result JSON is never emitted (#401)."""
    init_ev = StreamEvent(
        raw_line="init",
        event_type="init",
        metadata={"model": "claude-sonnet-4-6", "session_id": "sess-early"},
    )
    runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=[init_ev])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )

    assert result.session_id == "sess-early"


@pytest.mark.parametrize("event_type", ["system", "result"])
def test_runner_proxy_session_id_is_rejected(tmp_path, monkeypatch, event_type):
    from kew.runners.claude_code import ClaudeCodeRunner

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    line = json.dumps({"type": event_type, "subtype": "init", "session_id": secret})
    process = TestRunAgent._mock_popen(exit_code=0, lines=[line + "\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.orchestrator.popen", return_value=process),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
    ):
        result = run_agent(
            runner=ClaudeCodeRunner(),
            prompt="go",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )
    assert result.session_id is None
    assert result.session_id_rejection == "credential-bearing session identifier"
    assert "s3cret" not in result.log_file.read_text()


def test_session_id_not_clobbered_by_none_classification(tmp_path):
    """When classify_result returns session_id=None (e.g. SIGTERM killed before
    result JSON), the stream-captured session_id is preserved (#401)."""
    init_ev = StreamEvent(
        raw_line="init",
        event_type="init",
        metadata={"model": "claude-sonnet-4-6", "session_id": "sess-stream"},
    )
    runner = FakeRunner(
        lines=["line1\n"],
        exit_code=143,
        stream_events=[init_ev],
        classification=ResultClassification(category="watchdog_kill", session_id=None),
    )
    mock_proc = TestRunAgent._mock_popen(exit_code=143, lines=["line1\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )

    assert result.session_id == "sess-stream"
    assert result.error_category == "watchdog_kill"


def test_total_cost_usd_captured_from_result_event(tmp_path):
    """When the result event carries total_cost_usd, reported_cost_usd is set."""
    result_ev = StreamEvent(
        raw_line="result",
        event_type="result",
        metadata={
            "session_id": "sess-1",
            "total_cost_usd": 7.13,
        },
    )
    runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=[result_ev])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )

    assert result.reported_cost_usd == pytest.approx(7.13)


def test_finished_event_uses_reported_cost_when_present(tmp_path):
    """The 'finished' event in events.jsonl must carry the authoritative
    reported_cost_usd (matching the DB), not the inflated token estimate."""
    import json

    result_ev = StreamEvent(
        raw_line="result",
        event_type="result",
        metadata={
            "session_id": "sess-1",
            "total_cost_usd": 7.13,
        },
    )
    usage_ev = StreamEvent(
        raw_line="usage",
        event_type="usage",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    runner = FakeRunner(
        lines=["line1\n", "line2\n"],
        exit_code=0,
        stream_events=[usage_ev, result_ev],
    )
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n", "line2\n"])
    mock_proc.pid = 4242
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="claude-sonnet-4-6",
            emit_events=True,
        )

    assert result.reported_cost_usd == pytest.approx(7.13)

    events_file = result.event_file
    assert events_file is not None
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    finished = [e for e in events if e["type"] == "finished"]
    assert len(finished) == 1
    assert finished[0]["cost_usd"] == pytest.approx(7.13)


def test_native_step_costs_sum_into_reported_cost(tmp_path):
    """opencode/pi carry a native per-step cost on each 'step' event; the
    orchestrator sums them into reported_cost_usd so compute_cost records the
    provider-billed truth instead of the ~10x token-bound estimate (#1082)."""
    step_a = StreamEvent(
        raw_line="s1",
        event_type="step",
        input_tokens=2_000_000,
        output_tokens=8_000,
        metadata={"cost_usd": 0.06},
    )
    step_b = StreamEvent(
        raw_line="s2",
        event_type="step",
        input_tokens=2_000_000,
        output_tokens=8_000,
        metadata={"cost_usd": 0.0464},
    )
    runner = FakeRunner(
        lines=["line1\n", "line2\n"],
        exit_code=0,
        stream_events=[step_a, step_b],
    )
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n", "line2\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="openrouter/deepseek/deepseek-chat",
        )

    # Summed native spend, not the token×bound estimate over 4M input tokens.
    assert result.reported_cost_usd == pytest.approx(0.1064)
    result.compute_cost(model="openrouter/deepseek/deepseek-chat")
    assert result.estimated_cost_usd == pytest.approx(0.1064)


def test_step_without_native_cost_leaves_reported_cost_none(tmp_path):
    """claude-code sends cost_usd=None on 'step' events (its total arrives on
    the result event), so step accumulation must not fabricate a reported cost
    that would shadow the token estimate (#1082)."""
    step_ev = StreamEvent(
        raw_line="s1",
        event_type="step",
        input_tokens=1_000,
        output_tokens=500,
        metadata={"cost_usd": None},
    )
    runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=[step_ev])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="claude-sonnet-4-6",
        )

    assert result.reported_cost_usd is None


def test_emit_step_event_prefers_native_cost_over_token_estimate():
    """When a 'step' event carries a native cost, the events-file step line
    records it verbatim so the loop's spent_usd sums billed truth (#1082)."""
    import io
    import json

    from kew.orchestrator import _cost_from_accumulated, _emit_stream_event

    fh = io.StringIO()
    ev = StreamEvent(
        raw_line="x",
        event_type="step",
        input_tokens=2_000_000,
        output_tokens=8_000,
        metadata={"cost_usd": 0.06},
    )
    _emit_stream_event(
        fh, ev, accumulated_input=0, accumulated_output=0, model="claude-sonnet-4-6"
    )
    line = json.loads(fh.getvalue().strip().splitlines()[-1])
    assert line["type"] == "step"
    assert line["cost_usd"] == pytest.approx(0.06)
    # And definitely not the (much larger) token estimate over 2M input tokens.
    token_estimate = _cost_from_accumulated("claude-sonnet-4-6", 2_000_000, 8_000, None)
    assert token_estimate > 0.06
    assert line["cost_usd"] != pytest.approx(token_estimate)


def test_finished_event_falls_back_to_token_estimate(tmp_path):
    """Without reported_cost_usd, the finished event uses the token estimate."""
    import json

    usage_ev = StreamEvent(
        raw_line="usage",
        event_type="usage",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    runner = FakeRunner(
        lines=["line1\n"],
        exit_code=0,
        stream_events=[usage_ev],
    )
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    mock_proc.pid = 4242
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="claude-sonnet-4-6",
            emit_events=True,
        )

    assert result.reported_cost_usd is None

    events_file = result.event_file
    assert events_file is not None
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    finished = [e for e in events if e["type"] == "finished"]
    assert len(finished) == 1
    assert finished[0]["cost_usd"] == pytest.approx(18.0)


def test_reported_cost_stays_none_without_total_cost_usd(tmp_path):
    """Without total_cost_usd in the result event, reported_cost_usd stays None."""
    result_ev = StreamEvent(
        raw_line="result",
        event_type="result",
        metadata={"session_id": "sess-1"},
    )
    runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=[result_ev])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
        )

    assert result.reported_cost_usd is None


def test_watchdog_kills_container_not_pgid(tmp_path):
    """Sandboxed run: budget watchdog calls ContainerHandle.kill(), not os.killpg."""
    usage_ev = StreamEvent(
        raw_line="l1", event_type="usage", input_tokens=100, output_tokens=50
    )
    runner = FakeRunner(lines=["line1\n"], exit_code=0, stream_events=[usage_ev])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["line1\n"])
    mock_proc.poll.return_value = None  # still running when watchdog trips
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    mock_handle = MagicMock()

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
        patch("kew.orchestrator.os.killpg") as mock_killpg,
    ):
        result = run_agent(
            runner=runner,
            prompt="fix it",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            model="claude-sonnet-4-6",
            budget_check=lambda run_cost: run_cost > 0.0,
            container_handle=mock_handle,
        )

    mock_handle.kill.assert_called_once()
    mock_killpg.assert_not_called()
    assert result.budget_exceeded is True


class TestNoRawTranscriptRetention:
    """I06T: the host stream derives evidence and DISCARDS the provider bytes.

    The per-run log used to be the raw stream-json transcript, and two
    consumers (first-edit metrics, the agent's Dispatch Report) replayed it
    afterwards. That made every byte a provider echoed back — its private
    reasoning, the source it quoted, a credential in an error string, the
    operator's absolute paths — durable, and re-ingestible as free text.
    """

    _SECRETS = (
        "sk-ant-api03-NOTAREALKEY",
        "https://internal.example.test/runbook",
        "/Users/operator/secrets/id_rsa",
        "reasoning: I should read the private key first",
    )

    @staticmethod
    def _hostile_lines() -> list[str]:
        """Provider output carrying every category the node names."""
        return [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": (
                                    "reasoning: I should read the private key first"
                                ),
                            },
                            {
                                "type": "text",
                                "text": (
                                    "found sk-ant-api03-NOTAREALKEY at "
                                    "https://internal.example.test/runbook"
                                ),
                            },
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {
                                    "file_path": "/Users/operator/secrets/id_rsa"
                                },
                            },
                        ],
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    },
                }
            )
            + "\n",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/Users/operator/secrets/id_rsa"
                                },
                            }
                        ],
                        "usage": {"input_tokens": 40, "output_tokens": 10},
                    },
                }
            )
            + "\n",
            json.dumps(
                {
                    "type": "result",
                    "result": (
                        "## Dispatch Report\n\n**Issue:** #1\n\n"
                        "### What was done\n- redacted"
                    ),
                }
            )
            + "\n",
        ]

    def _run(self, tmp_path, lines):
        runner = FakeRunner(exit_code=0)
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.stdout = MagicMock()
        mock_proc.returncode = 0
        mock_proc.poll.return_value = 0
        mock_proc.stdout.readline.side_effect = [*lines, ""]
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
        ):
            return run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )

    def test_log_keeps_structural_progress_and_nothing_else(self, tmp_path):
        result = self._run(tmp_path, self._hostile_lines())
        written = result.log_file.read_text(encoding="utf-8")
        for secret in self._SECRETS:
            assert secret not in written
        assert "thinking" not in written
        assert '"type"' not in written
        assert "step 1 tokens=120 tools=Read" in written
        assert "step 2 tokens=170 tools=Edit" in written

    def test_evidence_survives_without_the_transcript(self, tmp_path):
        result = self._run(tmp_path, self._hostile_lines())
        evidence = read_derived_evidence(result.log_file)
        assert evidence is not None
        assert evidence.first_edit == FirstEditMetrics(2, 170)
        assert dict(evidence.tool_uses) == {"Read": 1, "Edit": 1}
        assert evidence.terminal_type == "result"
        assert evidence.report is not None
        assert evidence.report.startswith("## Dispatch Report")
        serialized = json.dumps(evidence.as_json())
        for secret in self._SECRETS:
            assert secret not in serialized

    def test_evidence_is_written_even_when_the_run_explodes(self, tmp_path):
        """A run that dies mid-stream demonstrably had facts; losing them
        would report an absence the run did not have."""
        runner = FakeRunner(exit_code=0)
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.stdout = MagicMock()
        mock_proc.returncode = 0
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [
            *self._hostile_lines()[:1],
            OSError("stream died"),
        ]
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            patch("kew.orchestrator.popen", return_value=mock_proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            pytest.raises(OSError),
        ):
            run_agent(
                runner=runner,
                prompt="fix it",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1,
            )
        [log_file] = sorted((tmp_path / "logs").glob("dispatch-1-*.log"))
        evidence = read_derived_evidence(log_file)
        assert evidence is not None
        assert evidence.steps == 1


class TestWallClockKillReachesTheContainer:
    """#1431: a timed-out CONTAINED run must actually stop.

    Both timeout paths stop the local ``docker run`` client and neither stops
    what it launched — ``timeout`` signals the CLI, whose signal proxying the
    container's PID 1 is free to ignore, and the Python-level path calls
    ``proc.terminate()`` on the CLI. Either way the agent kept its cores, its
    memory and its write access to the checkout with nothing watching it.
    """

    def test_a_timeout_kills_the_container_by_name(self):
        handle = MagicMock()
        _kill_container_after_timeout(handle, 124, 124)
        handle.kill.assert_called_once()

    def test_teardown_removes_a_container_left_in_created_state(self, tmp_path: Path):
        # The sweep runs once the client is gone; a Created leftover under the
        # per-item name would make the next attempt's `docker run` fail 125.
        # It is removed by the id the launch recorded, never by name (#1963).
        from kew.orchestrator import _remove_container_after_stop

        proc = MagicMock()
        cidfile = tmp_path / "created.cid"
        cidfile.write_text("b" * 64, encoding="utf-8")
        handle = ContainerHandle(
            proc, "kew-created", runtime="/usr/bin/docker", cidfile=cidfile
        )
        result = DispatchResult(exit_code=0, issue_num=42, log_file=Path("l"))
        result.paused = True
        with patch(
            "kew.subprocess_utils.run", return_value=MagicMock(returncode=0)
        ) as runtime:
            _remove_container_after_stop(handle, result, 137)
        assert runtime.call_args_list[0].args[0] == [
            "docker",
            "rm",
            "-f",
            "b" * 64,
        ]
        assert runtime.call_args_list[0].kwargs == {
            "check": False,
            "timeout": RUNTIME_CONTROL_TIMEOUT_SECONDS,
        }

    def test_a_normal_exit_is_not_swept(self):
        from kew.orchestrator import _remove_container_after_stop

        handle = ContainerHandle(MagicMock(), "kew-clean")
        result = DispatchResult(exit_code=0, issue_num=42, log_file=Path("l"))
        with patch("kew.subprocess_utils.run") as runtime:
            _remove_container_after_stop(handle, result, 0)
        runtime.assert_not_called()

    def test_the_shell_timeout_path_kills_it_too(self):
        # `timeout` returns 124 through the client's own exit code, so
        # _wait_for_process reports None and only the returncode says so.
        handle = MagicMock()
        _kill_container_after_timeout(handle, None, 124)
        handle.kill.assert_called_once()

    def test_exit_137_is_classified_as_timeout_like_124(self):
        assert _is_timeout_exit(124)
        assert _is_timeout_exit(137)
        assert not _is_timeout_exit(0)

    def test_timeout_wrapper_has_a_hard_kill_grace(self):
        config = DispatchConfig()
        with patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"):
            argv, timeout_cmd, _ = _wrap_with_timeout(["agent"], config, 1)
        assert timeout_cmd == "timeout"
        assert argv[:6] == [
            "timeout",
            "--foreground",
            "-k",
            "30s",
            "1m",
            "agent",
        ]

    def test_silent_timeout_uses_named_kill_before_wrapper_backstop(
        self, tmp_path: Path
    ):
        runner = FakeRunner(lines=[], exit_code=137, stream_events=[])
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        proc.poll.return_value = None
        cidfile = tmp_path / "orchestrated.cid"
        cidfile.write_text("a" * 64, encoding="utf-8")
        handle = ContainerHandle(proc, "kew-orchestrated-silent", cidfile=cidfile)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        class SilentSelector(_MockSelector):
            def select(self, timeout=None):
                time.sleep(0.01)
                return []

        def record_kill(argv, **kwargs):
            if argv[1] == "events":
                return MagicMock(returncode=0, stdout="")
            proc.returncode = 137
            proc.poll.return_value = 137
            return MagicMock(returncode=0)

        with (
            patch.object(handle, "observe_workload_start", return_value=True),
            patch("kew.orchestrator.popen", return_value=proc) as popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=SilentSelector(),
            ),
            patch("kew.subprocess_utils.run", side_effect=record_kill) as killer,
        ):
            run_agent(
                runner=runner,
                prompt="silent",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1431,
                timeout_override_minutes=0,
                container_handle=handle,
            )

        argvs = [c.args[0] for c in killer.call_args_list]
        assert argvs[0] == ["docker", "kill", "a" * 64]
        assert argvs[1][:2] == ["docker", "events"]
        assert f"container={'a' * 64}" in argvs[1]
        assert "event=oom" in argvs[1]
        assert argvs[2] == ["docker", "rm", "-f", "a" * 64]
        wrapped = popen.call_args.args[0]
        assert wrapped[:4] == ["timeout", "--foreground", "-k", "30s"]

    @staticmethod
    def _silent_137_run(
        tmp_path: Path,
        handle,
        *,
        timeout_minutes: int,
        event_stdout: str = "",
    ):
        runner = FakeRunner(lines=[], exit_code=137, stream_events=[])
        proc = handle._proc
        if handle._cidfile is None:
            cidfile = tmp_path / f"{handle._name}.cid"
            cidfile.write_text("a" * 64, encoding="utf-8")
            handle._cidfile = cidfile
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        class SilentSelector(_MockSelector):
            def select(self, timeout=None):
                time.sleep(0.01)
                return []

        def record_kill(argv, **kwargs):
            if argv[1] == "events":
                assert argv[:2] == ["docker", "events"]
                assert "event=oom" in argv
                return MagicMock(returncode=0, stdout=event_stdout)
            assert argv[:2] in (["docker", "kill"], ["docker", "rm"])
            proc.returncode = 137
            proc.poll.return_value = 137
            return MagicMock(returncode=0)

        with (
            patch.object(handle, "observe_workload_start", return_value=True),
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=SilentSelector(),
            ),
            patch("kew.subprocess_utils.run", side_effect=record_kill),
        ):
            return run_agent(
                runner=runner,
                prompt="silent",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1431,
                timeout_override_minutes=timeout_minutes,
                container_handle=handle,
            )

    def test_a_quota_kill_is_named_not_reported_as_a_timeout(
        self, tmp_path: Path, capsys
    ):
        # The watchdog's 137 carries the cap that fired; "Timed out after N
        # minutes" for a checkout cap misdirects the operator.
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "blob").write_bytes(b"x" * 2048)
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc,
            "kew-capped",
            checkout_dir=checkout,
            checkout_quota_bytes=1024,
            watchdog_interval_seconds=0.01,
        )
        result = self._silent_137_run(tmp_path, handle, timeout_minutes=60)
        assert result.exit_code == 137
        assert "checkout exceeded 1024 bytes" in result.resource_cap_reason
        assert result.hard_stop == "resource_cap"
        err = capsys.readouterr().err
        assert "Killed by host cap" in err
        assert "Timed out after" not in err

    def test_the_watchdogs_wall_clock_kill_is_a_timeout(self, tmp_path: Path, capsys):
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        proc.poll.return_value = None
        handle = ContainerHandle(proc, "kew-deadline", watchdog_interval_seconds=0.01)
        result = self._silent_137_run(tmp_path, handle, timeout_minutes=0)
        assert result.hard_stop == "wall_clock"
        err = capsys.readouterr().err
        assert "Timed out after 0 minutes (host watchdog" in err

    def test_accepted_free_space_kill_wins_and_keeps_the_oom_observation(
        self, tmp_path: Path
    ):
        from kew.transcript_metrics import read_derived_evidence

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        proc.poll.return_value = None
        handle = ContainerHandle(
            proc,
            "kew-race",
            checkout_dir=checkout,
            host_free_floor=1024,
            watchdog_interval_seconds=0.01,
        )
        usage = shutil.disk_usage(checkout)._replace(free=1)
        with patch("kew.sandbox.shutil.disk_usage", return_value=usage):
            result = self._silent_137_run(
                tmp_path,
                handle,
                timeout_minutes=60,
                event_stdout="container oom current-id\n",
            )
        assert result.resource_cap_code == "host_free_space"
        assert "host free space" in result.resource_cap_reason
        assert result.hard_stop == "resource_cap"
        assert [item["code"] for item in result.resource_cap_observations or []] == [
            "host_free_space",
            "oom",
        ]
        evidence = read_derived_evidence(result.log_file)
        assert evidence is not None
        assert [code for code, _ in evidence.resource_cap_observations] == [
            "host_free_space",
            "oom",
        ]

    def test_a_137_long_before_the_deadline_is_not_a_timeout(
        self, tmp_path: Path, capsys
    ):
        # A cgroup OOM or an in-container SIGKILL exits 137 within seconds of
        # a long budget; reporting that as a timeout (and auditing wall_clock)
        # is the mislabel round 3 executed live.
        runner = FakeRunner(lines=[], exit_code=137, stream_events=[])
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        cidfile = tmp_path / "oom.cid"
        cidfile.write_text("a" * 64, encoding="utf-8")
        handle = ContainerHandle(
            proc,
            "kew-oom",
            watchdog_interval_seconds=0.01,
            cidfile=cidfile,
        )
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        class SilentSelector(_MockSelector):
            def select(self, timeout=None):
                time.sleep(0.01)
                return []

        with (
            patch.object(handle, "observe_workload_start", return_value=True),
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=SilentSelector(),
            ),
            patch("kew.subprocess_utils.run") as killer,
        ):
            result = run_agent(
                runner=runner,
                prompt="oom",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1431,
                timeout_override_minutes=60,
                container_handle=handle,
            )
        assert result.exit_code == 137
        assert result.hard_stop == "hard_kill"
        assert result.resource_cap_reason == ""
        err = capsys.readouterr().err
        assert "Killed (exit 137)" in err and "not a timeout" in err
        assert "Timed out after" not in err
        # The kill starts before identity capture. Removal uses the exact ID
        # after the daemon probe loads it from the cidfile.
        argvs = [c.args[0] for c in killer.call_args_list]
        # The daemon asked whether this 137 was an OOM (#1767 — it was not).
        assert [argv[:2] for argv in argvs] == [
            ["docker", "kill"],
            ["docker", "events"],
            ["docker", "rm"],
        ]
        assert argvs[0] == ["docker", "kill", "a" * 64]
        assert argvs[2] == ["docker", "rm", "-f", "a" * 64]

    def test_orchestrated_137_uses_the_late_watchdogs_enforced_window(
        self, tmp_path: Path
    ):
        from kew.orchestrator import classify_hard_stop as classify_once
        from kew.sandbox import OOMProbeResult, OOMProbeStatus

        clock = {"now": 0.0}
        runner = FakeRunner(lines=[], exit_code=137, stream_events=[])
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])

        def finish() -> int:
            clock["now"] = 64.0
            return 137

        proc.wait.side_effect = finish

        def watchdog_clock() -> float:
            if clock["now"] == 3.0:
                clock["now"] = 6.0
            return clock["now"]

        handle = ContainerHandle(proc, "kew-delayed-probe", now=watchdog_clock)

        def delayed_absent_probe() -> OOMProbeResult:
            clock["now"] = 69.0
            return OOMProbeResult(OOMProbeStatus.ABSENT)

        class ManagedSetup:
            def __enter__(self) -> None:
                clock["now"] = 2.0

            def __exit__(self, *_args: object) -> None:
                return None

        def preflight(*_args: object) -> None:
            clock["now"] = 3.0

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with (
            patch("kew.orchestrator.time.monotonic", side_effect=lambda: clock["now"]),
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.managed_switchyard_context",
                return_value=ManagedSetup(),
            ),
            patch(
                "kew.orchestrator._preflight_runner_workspace",
                side_effect=preflight,
            ),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            patch.object(handle, "note_oom_kill", side_effect=delayed_absent_probe),
            patch("kew.subprocess_utils.run", return_value=MagicMock(returncode=0)),
            patch(
                "kew.orchestrator.classify_hard_stop", wraps=classify_once
            ) as classify,
        ):
            result = run_agent(
                runner=runner,
                prompt="oom",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1767,
                timeout_override_minutes=1,
                container_handle=handle,
            )

        assert result.duration_seconds == 61.0
        assert result.enforced_duration_seconds == 58.0
        assert clock["now"] == 69.0
        assert result.hard_stop == "hard_kill"
        assert classify.call_count == 1

    def test_a_cgroup_oom_is_named_a_memory_cap_not_an_anonymous_hard_kill(
        self, tmp_path: Path, capsys
    ):
        # The gap #1767 closes: an OOM-killed container exits 137 like every
        # other hard kill, `--rm` has already removed it, and the operator was
        # told "memory cap (OOM) or an in-container SIGKILL" — a guess. The
        # daemon's `oom` event settles it, and names the limit that fired.
        runner = FakeRunner(lines=[], exit_code=137, stream_events=[])
        proc = TestRunAgent._mock_popen(exit_code=137, lines=[])
        cidfile = tmp_path / "oomed.cid"
        cidfile.write_text("a" * 64, encoding="utf-8")
        handle = ContainerHandle(
            proc,
            "kew-oomed",
            watchdog_interval_seconds=0.01,
            cidfile=cidfile,
        )
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        class SilentSelector(_MockSelector):
            def select(self, timeout=None):
                time.sleep(0.01)
                return []

        def answer(argv, **kwargs):
            if argv[1] == "events":
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "2026-08-28T19:05:00.000000000Z container oom "
                        "3f1c9a (image=kew-agent:latest, name=kew-oomed)\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="")

        with (
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=SilentSelector(),
            ),
            patch("kew.subprocess_utils.run", side_effect=answer),
        ):
            result = run_agent(
                runner=runner,
                prompt="oom",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1767,
                timeout_override_minutes=60,
                container_handle=handle,
            )
        assert result.exit_code == 137
        # resource_cap, so the breaker event names it and the retry ladder
        # stops instead of re-entering the same over-quota container.
        assert result.hard_stop == "resource_cap"
        assert "OOM-killed" in result.resource_cap_reason
        assert "memory cap (8g)" in result.resource_cap_reason
        err = capsys.readouterr().err
        assert "Killed by host cap" in err
        assert "Timed out after" not in err
        # The cap is evidence, not just a stderr message: the run's log
        # carries the same terminal cap event a watchdog breach writes (the
        # reason itself stays out of the sanitized log, as it does there).
        log = result.log_file.read_text(encoding="utf-8")
        assert RESOURCE_CAP_EVENT in log

    def test_an_oom_probe_does_not_run_for_a_clean_exit(self, tmp_path: Path):
        # The probe is a per-run daemon call; a run that exited 0 has nothing
        # to explain and must not pay for it.
        runner = FakeRunner(lines=[], exit_code=0, stream_events=[])
        proc = TestRunAgent._mock_popen(exit_code=0, lines=[])
        handle = ContainerHandle(proc, "kew-clean", watchdog_interval_seconds=0.01)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        class SilentSelector(_MockSelector):
            def select(self, timeout=None):
                time.sleep(0.01)
                return []

        with (
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator.find_timeout_cmd", return_value="timeout"),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=SilentSelector(),
            ),
            patch("kew.subprocess_utils.run") as run,
        ):
            result = run_agent(
                runner=runner,
                prompt="clean",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1767,
                timeout_override_minutes=60,
                container_handle=handle,
            )
        assert result.hard_stop == ""
        assert [c.args[0][1] for c in run.call_args_list] == []

    def test_kews_own_stops_are_not_hard_kills(self, tmp_path: Path):
        # pause / budget / cost-anomaly all docker-kill the container (137,
        # before the deadline, no watchdog reason); they are kew's decision,
        # not an OOM, and must not be reported or audited as hard_kill.
        from kew.orchestrator import classify_hard_stop

        for field in ("paused", "budget_exceeded"):
            result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path)
            result.duration_seconds = 1.0
            setattr(result, field, True)
            assert classify_hard_stop(result, timeout_seconds=3600) == "", field
        result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path)
        result.duration_seconds = 1.0
        result.breaker_rule = "cost_anomaly"
        assert classify_hard_stop(result, timeout_seconds=3600) == ""

    def test_a_137_at_or_past_the_budget_is_a_timeout(self, tmp_path: Path):
        # The wrapper records its own enforced window. Exactly-at counts
        # (>=, not >).
        from kew.orchestrator import classify_hard_stop

        for elapsed in (60.0, 90.019):
            result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path)
            result.duration_seconds = elapsed
            result.enforced_duration_seconds = elapsed
            result.enforced_timeout_seconds = 60
            assert classify_hard_stop(result, timeout_seconds=60) == "wall_clock"
        result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path)
        result.duration_seconds = 59.9
        result.enforced_duration_seconds = 59.9
        result.enforced_timeout_seconds = 60
        assert classify_hard_stop(result, timeout_seconds=60) == "hard_kill"

    def test_a_124_requires_its_recorded_enforcement_deadline(self, tmp_path: Path):
        from kew.orchestrator import classify_hard_stop

        result = DispatchResult(exit_code=124, issue_num=1, log_file=tmp_path)
        result.duration_seconds = 1.0
        result.enforced_duration_seconds = 1.0
        result.enforced_timeout_seconds = 60
        assert classify_hard_stop(result, timeout_seconds=60) == ""

        result.duration_seconds = 60.0
        result.enforced_duration_seconds = 60.0
        assert classify_hard_stop(result, timeout_seconds=60) == "wall_clock"

    def test_a_137_without_an_enforcement_owner_is_never_a_timeout(
        self, tmp_path: Path
    ):
        from kew.orchestrator import classify_hard_stop

        result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path)
        result.duration_seconds = 600.0

        assert result.enforced_duration_seconds is None
        assert classify_hard_stop(result, timeout_seconds=60) == "hard_kill"

    def test_a_normal_exit_leaves_the_container_alone(self):
        handle = MagicMock()
        _kill_container_after_timeout(handle, None, 0)
        handle.kill.assert_not_called()

    def test_a_host_run_has_nothing_to_kill(self):
        _kill_container_after_timeout(None, 124, 124)  # must not raise


def test_stream_reads_are_bounded_so_one_line_cannot_exhaust_host_memory(
    tmp_path: Path,
):
    # The agent chooses how many bytes it writes before a newline; an unbounded
    # readline turns that choice into unbounded host memory (#1431).
    runner = FakeRunner(lines=[], exit_code=0, stream_events=[])
    mock_proc = TestRunAgent._mock_popen(exit_code=0, lines=["a\n"])
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    with (
        patch("kew.orchestrator.popen", return_value=mock_proc),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector",
            return_value=_MockSelector(),
        ),
    ):
        run_agent(
            runner=runner,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="m",
        )

    assert mock_proc.stdout.readline.call_args_list
    for call in mock_proc.stdout.readline.call_args_list:
        assert call.args == (MAX_LINE_BYTES,), (
            "every stdout read must carry the per-line byte bound"
        )


@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_sidecar_context_threads_proxy_policy(mode):
    from unittest.mock import MagicMock

    from kew.config import SwitchyardSidecarConfig
    from kew.orchestrator import sidecar_switchyard_context

    config = DispatchConfig()
    config.sandbox.proxy_environment = mode
    config.inference_transport.switchyard.sidecar = SwitchyardSidecarConfig(
        enabled=True, image="switchyard:pinned", egress_network="egress"
    )
    transport = MagicMock(
        mode="switchyard",
        sidecar=True,
        managed=False,
        sidecar_image="switchyard:pinned",
        sidecar_egress_network="egress",
    )
    supervisor = sidecar_switchyard_context(
        config, transport, worker_image_id="sha256:" + "a" * 64
    )
    assert supervisor.proxy_environment == mode


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_round_four_launch_state_tracks_successful_spawn(tmp_path, spawn_fails):
    from contextlib import nullcontext

    from kew.tracking import DispatchResult

    runner = FakeRunner()
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    proc = TestRunAgent()._mock_popen(exit_code=137)
    captured = []

    def result_factory(**kwargs):
        result = DispatchResult(**kwargs)
        captured.append(result)
        return result

    with (
        patch("kew.orchestrator.DispatchResult", side_effect=result_factory),
        patch(
            "kew.orchestrator.popen",
            side_effect=FileNotFoundError("missing runner") if spawn_fails else None,
            return_value=proc,
        ),
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch("kew.orchestrator._stream_output", return_value=("", "", None)),
        pytest.raises(FileNotFoundError) if spawn_fails else nullcontext(),
    ):
        run_agent(
            runner=runner,
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
        )
    assert getattr(captured[0], "launched", None) is (not spawn_fails)


def test_round_five_spawn_evidence_precedes_event_write():
    from kew.dispatch_admission import PendingAdmission
    from kew.orchestrator import _accept_process_ownership, _announce_spawn

    on_spawn = MagicMock()
    proc = MagicMock(pid=1234)
    admission = PendingAdmission(None, "spawn")
    result = DispatchResult(0, 42, Path("spawn.log"))
    _accept_process_ownership(admission, None, result)
    with (
        patch("kew.orchestrator._write_event", side_effect=OSError(28, "disk full")),
        pytest.raises(OSError),
    ):
        _announce_spawn(proc, MagicMock(), on_spawn)
    assert admission.cleanup_owned
    assert not admission.startup_started
    assert result.launched is True
    on_spawn.assert_not_called()


@pytest.mark.parametrize("contained", [False, True])
def test_bounded_key_reaches_actual_process_environment(
    tmp_path, monkeypatch, contained
):
    import os

    from kew.credential_issuer import BoundedRunKey
    from kew.runners import get_runner
    from kew.sandbox import ContainedExecution, build_run_argv

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-secret-1944")
    key = BoundedRunKey("run-secret-1944", "a" * 64, 2, "2099-01-01T00:00:00Z")
    execution = None
    if contained:
        execution = ContainedExecution.from_argv(
            build_run_argv(
                runtime="docker",
                image="test",
                checkout_dir=tmp_path,
                slug="1944",
                runner="opencode",
                network="allowlist",
            )
        )
    process = TestRunAgent._mock_popen(exit_code=0, lines=[])
    with (
        patch("kew.orchestrator.popen", return_value=process) as spawn,
        patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ),
        patch("kew.orchestrator._sweep_stale_container"),
        patch("kew.sandbox.ContainedExecution.handle_for", return_value=None),
    ):
        run_agent(
            runner=get_runner("opencode"),
            prompt="test",
            worktree_dir=tmp_path,
            config=DispatchConfig(),
            issue_num=1944,
            model="openrouter/test",
            credential=key,
            contained=execution,
        )
    env = spawn.call_args.kwargs["env"]
    assert env["OPENROUTER_API_KEY"] == "run-secret-1944"
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    assert "secret-1944" not in str(spawn.call_args.args)
    assert os.environ["OPENROUTER_API_KEY"] == "host-secret-1944"


@pytest.mark.parametrize("contained", [False, True])
@pytest.mark.parametrize("setup_seconds", [0, 301])
def test_bounded_attempt_refuses_key_expiring_before_timeout(
    tmp_path, monkeypatch, contained, setup_seconds
):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from kew import run_credentials
    from kew.credential_issuer import BoundedRunKey, IssuerRefused
    from kew.runners import get_runner
    from kew.sandbox import ContainedExecution

    class Clock(datetime):
        current = datetime(2026, 9, 15, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    def setup(_contained):
        Clock.current += timedelta(seconds=setup_seconds)

    expiry = Clock.current + timedelta(seconds=420 if setup_seconds else 119)
    key = BoundedRunKey("run-secret-1944", "a" * 64, 7, expiry.isoformat())
    process = Mock(return_value=TestRunAgent._mock_popen(exit_code=0, lines=[]))
    monkeypatch.setattr("kew.orchestrator.popen", process)
    monkeypatch.setattr(run_credentials, "datetime", Clock)
    monkeypatch.setattr("kew.orchestrator.find_timeout_cmd", lambda: None)
    monkeypatch.setattr("kew.orchestrator._sweep_stale_container", setup)
    monkeypatch.setattr(
        "kew.orchestrator.selectors.DefaultSelector", lambda: _MockSelector()
    )
    monkeypatch.setattr("kew.sandbox.ContainedExecution.handle_for", lambda *a: None)
    execution = (
        ContainedExecution.from_argv(["docker", "run", "--name", "test", "test-image"])
        if contained
        else None
    )
    with pytest.raises(IssuerRefused):
        run_agent(
            runner=get_runner("opencode"),
            prompt="test",
            worktree_dir=tmp_path,
            config=DispatchConfig(),
            issue_num=1944,
            model="openrouter/test",
            timeout_override_minutes=2,
            credential=key,
            contained=execution,
        )
    process.assert_not_called()


def test_round6_verified_key_reaches_agent_through_generic_filter(
    tmp_path, monkeypatch
):
    from kew import credential_issuer, subprocess_utils
    from kew.graph.driver.credentials import issue_driver_key
    from kew.run_credentials import bounded_run_credentials
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
            credential_issuer.BoundedRunKey(
                "bounded-secret-1944", "a" * 64, limit, expiry.isoformat()
            )
        ),
    )
    config = DispatchConfig()
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        key = issue_driver_key(store, config, "agent-run", "1944")
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
        monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "opencode", "/mock/opencode")
        process = TestRunAgent._mock_popen(exit_code=0, lines=[])
        with (
            patch(
                "kew.subprocess_utils.subprocess.Popen", return_value=process
            ) as spawn,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator.selectors.DefaultSelector",
                return_value=_MockSelector(),
            ),
            bounded_run_credentials(
                run_id="agent-run",
                config=config,
                store=store,
                model="openrouter/test",
                credential_names=("OPENROUTER_API_KEY",),
                timeout_seconds=60,
            ) as inherited,
        ):
            run_agent(
                runner=get_runner("opencode"),
                prompt="test",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1944,
                model="openrouter/test",
                credential=inherited,
            )
        env = spawn.call_args.kwargs["env"]
        assert env["OPENROUTER_API_KEY"] == key.secret
        assert "KEW_ISSUED_RUN_KEY_HASH" not in env
        assert "OPENROUTER_PROVISIONING_KEY" not in env
        helper_env = subprocess_utils.child_environment()
        assert "OPENROUTER_API_KEY" not in helper_env
        assert "KEW_ISSUED_RUN_KEY_HASH" not in helper_env


_POLICY_STOP = (
    "KEW_NEEDS_HUMAN: policy gate failed: "
    "tests/test_silence_on_failure.py::test_no_new_silent_clean_defaults"
)


def _stop_stream(runner_name: str, text: str) -> list[str]:
    """One tool call, then a closing message, in *runner_name*'s own protocol."""
    if runner_name == "claude-code":
        return [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": text}),
        ]
    if runner_name == "opencode":
        return [
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "bash", "state": {"status": "completed"}},
                }
            ),
            json.dumps({"type": "text", "part": {"text": text}}),
        ]
    if runner_name in ("pi", "lemonade"):
        return [
            json.dumps(
                {"type": "tool_execution_start", "toolName": "bash", "args": {}}
            ),
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            ),
        ]
    return [text, "I could not make it pass."]  # hermes: stdout is the reply


def _run_stream(
    runner_name: str, lines: list[str], tmp_path: Path | None = None
) -> DispatchResult:
    from io import StringIO

    from kew.orchestrator import _finalize_agent_result, _process_line
    from kew.runners import get_runner
    from kew.transcript_metrics import StreamDerivation

    runner = get_runner(runner_name)
    if runner_name == "hermes":
        # hermes reports a clean finish only through its usage file.
        assert tmp_path is not None
        usage = tmp_path / "usage.json"
        usage.write_text(json.dumps({"completed": True}))
        runner._usage_file = usage
    result = DispatchResult(exit_code=0, issue_num=2601, log_file=None)
    derivation = StreamDerivation()
    last: str | None = None
    for line in lines:
        last, _ = _process_line(
            line, StringIO(), None, runner, result, "", None, last, derivation
        )
    _finalize_agent_result(
        result,
        runner=runner,
        config=DispatchConfig(),
        last_output=last,
        inference_transport=None,
        health_check=None,
        evidence=None,
        classifier_records=(),
        timeout_override_minutes=None,
        needs_human=derivation.needs_human,
    )
    return result


@pytest.mark.parametrize(
    "runner_name", ["claude-code", "opencode", "pi", "lemonade", "hermes"]
)
def test_needs_human_stop_refuses_on_every_runner(runner_name, tmp_path):
    """#2601: the policy-gate stop the prompts prescribe exits 0 on every
    runner. Only the Codex adapter read the marker, so on the rest the run
    classified as a success and was published. The stop now classifies as
    needs_human whichever runner ran it, with the failing test as the reason."""
    result = _run_stream(runner_name, _stop_stream(runner_name, _POLICY_STOP), tmp_path)
    assert result.error_category == "needs_human"
    assert result.subtype == (
        "policy gate failed: "
        "tests/test_silence_on_failure.py::test_no_new_silent_clean_defaults"
    )


@pytest.mark.parametrize("runner_name", ["claude-code", "opencode", "pi"])
def test_needs_human_then_a_tool_call_is_not_a_stop(runner_name):
    """An agent that wrote the marker and then carried on working did not stop:
    only words after its last tool call count as its closing message."""
    stop, *_ = _stop_stream(runner_name, _POLICY_STOP)[1:]
    tool, *_ = _stop_stream(runner_name, "Fixed it; the gate passes now.")
    done = _stop_stream(runner_name, "Fixed it; the gate passes now.")[1:]
    result = _run_stream(runner_name, [stop, tool, *done])
    assert result.error_category == ""


def test_needs_human_stop_never_reaches_publication(tmp_path):
    """The claude-code stop reaches dispatch's collect step as needs_human,
    which holds the change instead of publishing it."""
    from kew.dispatch_core import _collect_result

    result = _run_stream("claude-code", _stop_stream("claude-code", _POLICY_STOP))
    with (
        patch("kew.dispatch_core._root_checkout_integrity_check", return_value=None),
        patch("kew.dispatch_core._collect_special_category", return_value=None),
        patch("kew.dispatch_core._collect_needs_human") as human,
        patch("kew.dispatch_core._collect_success") as publish,
    ):
        ctx = MagicMock()
        ctx.checkout_generation = ""
        _collect_result(
            ctx, result=result, worktree_dir=tmp_path, run_sandboxed_path=False
        )
    publish.assert_not_called()
    human.assert_called_once()


def test_a_failed_run_keeps_its_own_category():
    """A marker never relabels a run that already failed for another reason."""
    from kew.orchestrator import _honor_needs_human

    failed = ResultClassification(category="transient", subtype="api_error")
    assert _honor_needs_human(failed, "policy gate failed: x") is failed
    clean = ResultClassification(category="")
    assert _honor_needs_human(clean, None) is clean
    assert _honor_needs_human(clean, "").subtype == "unspecified"


_SONNET = "claude-sonnet-4-6"
_TURN_INPUT = 250_000


def _turn_usd(turns: float = 1.0) -> float:
    """What *turns* claude turns of ``_TURN_INPUT`` input cost at list price."""
    from kew.pricing import compute_cost_from_tokens

    return turns * compute_cost_from_tokens(
        model=_SONNET, input_tokens=_TURN_INPUT, output_tokens=0
    )


def _claude_turn() -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": _SONNET,
                    "content": [{"type": "text", "text": "working"}],
                    "usage": {"input_tokens": _TURN_INPUT, "output_tokens": 0},
                },
            }
        )
        + "\n"
    )


def _claude_result(cost: float, input_tokens: int = 0) -> str:
    return (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": cost,
                "usage": {"input_tokens": input_tokens},
            }
        )
        + "\n"
    )


def _codex_turn_completed(input_tokens: int = _TURN_INPUT) -> str:
    return (
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                },
            }
        )
        + "\n"
    )


def test_spending_after_a_terminal_event_is_judged_and_stops_the_run(tmp_path):
    """#1798: a terminal event spares only the line that carries it. A usage
    line after it proves the run had not finished, so its crossing stops the
    run as any other crossing does."""
    import signal

    run = run_host_stream(
        tmp_path, [_claude_result(0.0), _claude_turn()], anomaly_threshold_usd=0.0
    )

    run.killpg.assert_called_once_with(run.proc.pid, signal.SIGTERM)
    assert run.result.breaker_rule == "cost_anomaly"
    assert run.result.error_category == "circuit_breaker"
    assert run.result.breaker_stopped is True
    assert run.result.terminal_observed is False


def test_a_forged_early_result_does_not_spare_the_rest_of_the_run(tmp_path):
    """A result line early in the stream claimed the run was over and nearly
    free, then 20 more turns followed. The crossing on the third turn stops
    the run where it would have stopped without the forged line."""
    import signal

    forged = _claude_result(0.0001, input_tokens=1)
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": _SONNET}) + "\n",
        _claude_turn(),
        forged,
        *[_claude_turn()] * 20,
        _claude_result(_turn_usd(21)),
    ]
    # Five runs at a $0.675 median arm the ceiling at 3x: $2.025.
    run = run_host_stream(tmp_path, lines, anomaly_threshold_usd=_turn_usd(2.7))

    run.killpg.assert_called_once_with(run.proc.pid, signal.SIGTERM)
    assert run.result.error_category == "circuit_breaker"
    assert run.result.estimated_cost_usd == pytest.approx(_turn_usd(3))


def test_every_closing_line_is_the_end_of_the_run(tmp_path):
    """codex reports usage only on ``turn.completed``, the line that ends the
    run. A repeated one is that closing line again: a crossing on it flags the
    run and never kills it, whatever came before."""
    run = run_host_stream(
        tmp_path,
        [_codex_turn_completed(), _codex_turn_completed()],
        parser="codex",
        anomaly_threshold_usd=_turn_usd(1.5),
    )

    run.killpg.assert_not_called()
    assert run.result.breaker_rule == "cost_anomaly"
    assert run.result.breaker_stopped is False
    assert run.result.estimated_cost_usd == pytest.approx(_turn_usd(2))


def _pi_message_end(stop_reason: str, input_tokens: int) -> str:
    message = {
        "role": "assistant",
        "model": _SONNET,
        "content": [{"type": "text", "text": "x"}],
        "stopReason": stop_reason,
        "usage": {"input": input_tokens, "output": 0},
    }
    return json.dumps({"type": "message_end", "message": message}) + "\n"


def test_an_earlier_terminal_event_does_not_cost_a_finished_run_its_work(tmp_path):
    """A pi run hit a provider error mid-run (a terminal ``stopReason``), then
    carried on and finished; its last line crosses the bar. That last line
    ends the run, so the run is flagged, not killed."""
    lines = [
        _pi_message_end("toolUse", 100_000),
        _pi_message_end("error", 0),
        _pi_message_end("toolUse", 100_000),
        _pi_message_end("stop", 1_000_000),
    ]

    run = run_host_stream(
        tmp_path, lines, parser="pi", anomaly_threshold_usd=_turn_usd(2.4)
    )

    run.killpg.assert_not_called()
    assert run.result.breaker_rule == "cost_anomaly"
    assert run.result.breaker_stopped is False
    assert run.result.error_category == ""


def test_anomaly_crossing_on_the_terminal_usage_line_does_not_terminate_the_agent(
    tmp_path,
):
    """Codex reports its last usage on ``turn.completed`` itself — the
    2026-09-01 kill fired in that same second. The crossing and the terminal
    event arrive on one line, and the finished run is flagged, not killed."""
    run = run_host_stream(
        tmp_path,
        [_codex_turn_completed(200_000)],
        parser="codex",
        anomaly_threshold_usd=0.0,
    )

    run.killpg.assert_not_called()
    run.proc.terminate.assert_not_called()
    assert run.result.breaker_rule == "cost_anomaly"
    assert run.result.breaker_stopped is False
    assert run.result.terminal_observed is True
    assert run.result.estimated_cost_usd > 0
    assert run.result.exit_code == 0


def test_anomaly_crossing_before_the_terminal_event_still_terminates(tmp_path):
    """No terminal event yet means the run's state is unknown, never finished:
    the crossing SIGTERMs the agent's group with the 10 s anomaly grace."""
    import signal

    run = run_host_stream(
        tmp_path, [_claude_turn(), _claude_result(0.0)], anomaly_threshold_usd=0.0
    )

    run.killpg.assert_called_once_with(run.proc.pid, signal.SIGTERM)
    run.proc.wait.assert_any_call(timeout=10)
    assert run.result.breaker_rule == "cost_anomaly"
    assert run.result.error_category == "circuit_breaker"
    assert run.result.terminal_observed is False  # the stream stopped at the trip


def test_a_budget_stop_after_a_flag_is_recorded_as_the_budget_stop(tmp_path):
    """The closing line crossed the anomaly bar and flagged the run; the run
    then kept spending until the budget cap stopped it. The flag described a
    finished run, and this one was not: the stop is the budget's alone."""
    run = run_host_stream(
        tmp_path,
        [_codex_turn_completed(), _codex_turn_completed()],
        parser="codex",
        anomaly_threshold_usd=_turn_usd(0.5),
        budget_check=lambda cost: cost > _turn_usd(1.5),
    )

    run.killpg.assert_called()
    assert run.result.budget_exceeded is True
    assert run.result.error_category == "budget_exceeded"
    assert run.result.breaker_rule == ""
    assert run.result.breaker_threshold_usd is None
