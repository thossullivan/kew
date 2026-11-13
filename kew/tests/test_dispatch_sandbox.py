"""Container launch evidence and proxy environment policy across dispatch.

Workload evidence across dispatch lifecycle failures, and the proxy environment
policy at container launch and audit boundaries.
"""

import json
import os
import subprocess
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig
from kew.dispatch_core import _DispatchContext, _emit_sandbox_resolved
from kew.dispatch_sandbox import build_agent_container_argv, prepare_sidecar_worker
from kew.run_store import SqliteRunStore
from kew.sandbox import CONTAINER_PROXY_NAMES, attest_flow_posture
from kew.tracking import DispatchResult


def _round_nine_run_container(
    tmp_path, proc, argv, path, result, events=None, store=None
):
    from test_orchestrator import FakeRunner, _MockSelector

    from kew.dispatch_sandbox import run_container_and_stream
    from kew.orchestrator import _run_process
    from kew.sandbox import ContainedExecution, launch_container
    from kew.transcript_metrics import StreamDerivation

    if path == "dispatch":
        handle = launch_container(argv, container_name="kew-42", container_id="a" * 64)
        with patch(
            "kew.dispatch_sandbox._launch_streaming_container", return_value=handle
        ):
            run_container_and_stream(
                DispatchConfig(),
                store,
                image="image",
                runner="claude-code",
                checkout_dir=tmp_path,
                slug="42",
                run_id="run-42",
                model="model",
                log_file=result.log_file,
                run_kind="dispatch",
                result=result,
            )
    else:
        with patch(
            "kew.orchestrator.selectors.DefaultSelector", return_value=_MockSelector()
        ):
            _run_process(
                argv,
                tmp_path,
                {},
                result.log_file,
                events,
                FakeRunner(),
                result,
                "model",
                None,
                None,
                60,
                StreamDerivation(),
                contained=ContainedExecution(
                    tuple(argv),
                    "kew-42",
                    "docker",
                    container_id="a" * 64,
                    prepared=True,
                ),
            )


# Round 9's exec-window test is gone with the daemon exec_start evidence it
# exercised: it built ["docker", "exec", <id>, "claude", "--print"] by hand,
# a shape kew never produces — every contained agent is exec'd through
# /usr/local/bin/kew-agent-entrypoint, whose acknowledgement is the startup
# evidence (governed review round 2, 2026-09-21;
# tests/test_governed_round2_startup_evidence.py pins the argv shape).


@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
@pytest.mark.parametrize("client", ["run", "exec"])
@pytest.mark.parametrize("refusal", ["prompt", "catalog", "preparation", "silent"])
def test_round_nine_entrypoint_refusals_require_agent_evidence(
    tmp_path, path, client, refusal
):
    import os
    from pathlib import Path

    from test_orchestrator import TestRunAgent

    from kew.cli import _dispatch_exit_code
    from kew.sandbox import ContainerHandle

    scripts = Path(__file__).resolve().parents[2] / "docker"
    missing = subprocess.run(
        ["/bin/bash", str(scripts / "agent-entrypoint.sh")],
        env={**os.environ, "KEW_PROMPT_FILE": str(tmp_path / "nonexistent")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 64
    prompt = tmp_path / "prompt"
    prompt.write_text("fix it")
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in {
        "cp": "exit 0",
        "opencode": "echo openrouter/available-model",
    }.items():
        binary = bins / name
        binary.write_text(f"#!/bin/sh\n{body}\n")
        binary.chmod(0o755)
    catalog = subprocess.run(
        [
            "/bin/bash",
            str(scripts / "agent-opencode-entrypoint.sh"),
            "run",
            "-m",
            "openrouter/missing-model",
        ],
        env={
            **os.environ,
            "KEW_PROMPT_FILE": str(prompt),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PATH": f"{bins}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert catalog.returncode == 78
    argv = (
        ["docker", "exec", "a" * 64, "/usr/local/bin/kew-agent-entrypoint"]
        if client == "exec"
        else ["docker", "run", "--name", "kew-42", "image"]
    )

    def runtime(args, **kwargs):
        if "event=exec_start" in args:
            raw = json.dumps(
                {
                    "Action": "exec_start: /usr/local/bin/kew-agent-entrypoint",
                    "Actor": {"ID": "a" * 64},
                }
            )
        else:
            raw = json.dumps({"Running": True, "StartedAt": "2026-09-15T01:00:00Z"})
        return subprocess.CompletedProcess(args, 0, raw, "")

    refused = {
        "prompt": (64, missing.stderr, False, "prompt file missing"),
        "catalog": (78, catalog.stderr, False, "OpenCode catalog gap"),
        "preparation": (
            1,
            "ERROR: git preparation failed\n",
            False,
            "git preparation failed",
        ),
        "silent": (0, "", False, "no agent startup acknowledgement observed"),
    }
    argv[2:2] = ["-e", f"KEW_AGENT_START_NONCE={'a' * 32}"]
    cases = [
        refused[refusal],
        (64, f"KEW_AGENT_START {'a' * 32}\n", True, ""),
        (78, f"KEW_AGENT_START {'a' * 32}\n", True, ""),
    ]
    for code, output, started, reason in cases:
        proc = TestRunAgent._mock_popen(
            exit_code=code, lines=output.splitlines(keepends=True)
        )
        proc.wait.return_value = code
        result = DispatchResult(-1, 42, tmp_path / f"{code}-{started}.log")
        with (
            patch("kew.subprocess_utils.popen", return_value=proc),
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.subprocess_utils.run", side_effect=runtime),
            patch("kew.orchestrator._sweep_stale_container"),
            patch.object(ContainerHandle, "start_watchdog"),
            patch.object(ContainerHandle, "stop_watchdog"),
            patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
            patch("kew.orchestrator._record_container_hard_stop"),
            patch("kew.orchestrator._remove_container_after_stop"),
        ):
            _round_nine_run_container(tmp_path, proc, argv, path, result)
        if path == "orchestrator":
            from kew.orchestrator import _apply_classification
            from kew.runners.protocol import ResultClassification

            _apply_classification(result, ResultClassification(category="failure"))
        assert result.launched is started
        command = SimpleNamespace(
            kind="executed" if result.launched is not False else "not_launched",
            exit_code=code,
        )
        assert _dispatch_exit_code(command) == (1 if started else 2)
        if not started:
            assert reason in result.stop_reason
        else:
            assert result.error_category != "sandbox_startup_refused"
            assert not result.stop_reason


@pytest.mark.parametrize(
    ("situation", "started", "failure"),
    [
        ("empty", False, None),
        ("empty-string", False, None),
        ("workload", True, None),
        ("stream-failure", True, "stream failed"),
        ("wait-failure", True, "wait failed"),
        ("explicit-start", True, None),
        ("created", False, None),
        ("runtime-diagnostic", False, None),
        ("truthy-observer", False, None),
    ],
)
def test_optional_handle_capabilities_preserve_workload_evidence(
    tmp_path, situation, started, failure
):
    """Minimal handles work without making client output or identity a launch."""
    from kew.dispatch_sandbox import run_container_and_stream

    result = DispatchResult(-1, 42, tmp_path / "container.log")

    class StreamHandle:
        _started = False

        def __iter__(self):
            assert result.launched is (situation == "explicit-start")
            if situation in {"empty", "explicit-start", "created"}:
                return
            self._started = started
            yield "" if situation == "empty-string" else "workload output\n"
            # Evidence must reach the result before the next read can fail.
            assert result.launched is started
            if situation == "stream-failure":
                raise OSError("stream failed")

        def wait(self):
            if situation == "wait-failure":
                raise OSError("wait failed")
            return 1

    handle = StreamHandle()
    if situation in {"created", "runtime-diagnostic"}:
        handle.container_id = "a" * 64
    if started and situation != "explicit-start":
        handle.observe_workload_start = lambda: handle._started
    if situation == "explicit-start":
        handle.observe_workload_start = lambda: True
    elif situation == "runtime-diagnostic":
        handle.observe_workload_start = lambda: False
    elif situation == "truthy-observer":
        handle.observe_workload_start = MagicMock()

    with (
        patch("kew.dispatch_sandbox._launch_streaming_container", return_value=handle),
        patch("kew.dispatch_sandbox._finalize_sandbox_run"),
    ):
        try:
            exit_code = run_container_and_stream(
                DispatchConfig(),
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
            )
        except OSError as exc:
            assert str(exc) == failure
            assert exc.dispatch_result is result
            assert result.error_category == "sandbox_stream_error"
            assert result.exit_code == -1
        else:
            assert failure is None
            assert exit_code == result.exit_code == 1

    assert result.launched is started
    assert result.attempt_number == int(started)
    assert result.container_id == (
        "a" * 64 if situation in {"created", "runtime-diagnostic"} else None
    )


@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
def test_round_seven_container_requires_workload_start(tmp_path, path):
    from test_orchestrator import FakeRunner, TestRunAgent

    from kew.cli import _dispatch_exit_code
    from kew.dispatch_sandbox import run_container_and_stream
    from kew.orchestrator import _run_process
    from kew.sandbox import ContainedExecution, ContainerHandle
    from kew.transcript_metrics import StreamDerivation

    for situation in (
        "rejected",
        "created",
        "client",
        "streaming",
        "finished",
        "delayed",
        "removed",
        "bootstrap",
        "exec_streaming",
    ):
        started = situation in {
            "streaming",
            "finished",
            "delayed",
            "removed",
            "exec_streaming",
        }
        proc = TestRunAgent._mock_popen(exit_code=125 if not started else 1)
        proc.wait.return_value = proc.returncode
        proc.pid = 1234
        cidfile = tmp_path / "cid"
        cidfile.unlink(missing_ok=True)
        if situation in {
            "created",
            "streaming",
            "finished",
            "removed",
            "bootstrap",
            "exec_streaming",
        }:
            cidfile.write_text("a" * 64)
        argv = (
            ["docker", "exec", "a" * 64, "/usr/local/bin/kew-agent-entrypoint"]
            if situation in {"bootstrap", "exec_streaming"}
            else None
        )
        argv = argv or ["docker", "run"]
        argv[2:2] = ["-e", f"KEW_AGENT_START_NONCE={'a' * 32}"]
        handle = ContainerHandle(proc, "kew-42", cidfile=cidfile, launch_argv=argv)
        result = DispatchResult(-1, 42, tmp_path / "log")
        state = {
            "Running": situation in {"streaming", "bootstrap", "exec_streaming"},
            "StartedAt": "2026-09-15T01:00:00Z" if started else "0001-01-01T00:00:00Z",
        }

        def runtime(argv, *, state=state, situation=situation, **kwargs):
            assert argv[0] == "docker"
            if "event=exec_start" in argv:
                event = {
                    "Action": "exec_start",
                    "Actor": {
                        "ID": "a" * 64,
                        "Attributes": {
                            "execCommand": "/usr/local/bin/kew-agent-entrypoint"
                            if situation == "exec_streaming"
                            else "bootstrap-check"
                        },
                    },
                }
                return subprocess.CompletedProcess(argv, 0, json.dumps(event), "")
            if situation == "removed":
                return subprocess.CompletedProcess(
                    argv, 0, "a" * 64 if "events" in argv else "", ""
                )
            output = json.dumps(state) if "inspect" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        stream_error = (
            OSError(28, "stream failed")
            if situation in {"streaming", "client", "delayed", "exec_streaming"}
            else None
        )

        def stream(
            *args,
            situation=situation,
            cidfile=cidfile,
            stream_error=stream_error,
            proc=proc,
            started=started,
            handle=handle,
            **kwargs,
        ):
            if situation == "delayed":
                cidfile.write_text("a" * 64)
            if started:
                handle.observe_workload_output(f"KEW_AGENT_START {'a' * 32}\n")
            if stream_error is not None:
                raise stream_error
            return proc.returncode if path == "dispatch" else (None, "model", None)

        on_spawn = MagicMock()
        with (
            patch("kew.subprocess_utils.run", side_effect=runtime),
            patch(
                "kew.dispatch_sandbox._launch_streaming_container", return_value=handle
            ),
            patch(
                "kew.dispatch_core._stream_sandbox_log",
                side_effect=stream,
            ),
            patch("kew.dispatch_sandbox._finalize_sandbox_run"),
            patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
            patch("kew.dispatch_sandbox.write_container_evidence"),
            patch("kew.orchestrator.popen", return_value=proc),
            patch("kew.orchestrator._sweep_stale_container"),
            patch("kew.orchestrator._kill_handle", return_value=handle),
            patch.object(handle, "start_watchdog"),
            patch.object(handle, "stop_watchdog"),
            patch(
                "kew.orchestrator._stream_output",
                side_effect=stream,
            ),
            patch("kew.orchestrator._record_container_hard_stop"),
            patch("kew.orchestrator._remove_container_after_stop"),
        ):
            try:
                if path == "dispatch":
                    run_container_and_stream(
                        DispatchConfig(),
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
                    )
                else:
                    _run_process(
                        ["docker", "run"],
                        tmp_path,
                        {},
                        result.log_file,
                        None,
                        FakeRunner(),
                        result,
                        "model",
                        None,
                        None,
                        60,
                        StreamDerivation(),
                        on_spawn=on_spawn,
                        contained=ContainedExecution(
                            ("docker", "run"), "kew-42", "docker"
                        ),
                    )
            except OSError:
                assert stream_error is not None
        expected = None if situation == "client" else started
        assert result.launched is expected, situation
        if path == "orchestrator":
            on_spawn.assert_called_once_with(proc.pid)
        command = SimpleNamespace(
            kind="executed" if result.launched is not False else "not_launched",
            exit_code=1,
        )
        assert _dispatch_exit_code(command) == (1 if expected is not False else 2), (
            situation
        )


@pytest.mark.parametrize(
    "failure", ["finalize", "publication", "teardown", "prompt_cleanup", "late_refusal"]
)
def test_round_seven_container_lifecycle_retains_launch(tmp_path, failure):
    from kew.cli import _dispatch_exit_code
    from kew.dispatch_core import _launch_runner, _terminal_outcome
    from kew.dispatch_sandbox import _run_sandboxed, _run_sandboxed_container
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.runners import get_runner
    from kew.sandbox import SandboxRuntimeUnavailable

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    transport = ResolvedInferenceTransport(
        mode="direct", requested_model="model", model="model", config_sha256="a" * 64
    )
    ctx = SimpleNamespace(
        config=config,
        complexity="medium",
        model_override=None,
        issue_num=42,
        store=None,
        worktree_dir=tmp_path,
        item_id="42",
        run_id="run-42",
        start_monotonic=0,
        epic_ref=None,
        refuse_rollback=MagicMock(),
        expected_reservation_run_id=None,
        expected_reservation_generation=None,
        admitted_run_id=None,
        on_admitted=None,
        claim_launch_started=False,
        container_cleanup_pending=False,
    )
    captured = []

    def stream(*args, result, **kwargs):
        result.launched = True
        result.exit_code = 0
        result.container_id = "a" * 64
        result.attempt_number = 1
        captured.append(result)
        return 0

    @contextmanager
    def sidecar(*args, **kwargs):
        yield SimpleNamespace(endpoint="http://sidecar", config_sha256="b" * 64)
        raise RuntimeError("sidecar teardown failed")

    def workspace(_ctx, setup, **kwargs):
        setup.worktree_dir = tmp_path
        setup.run_sandboxed = True
        run_container = (
            _run_sandboxed if failure == "prompt_cleanup" else _run_sandboxed_container
        )
        return run_container(
            prompt="fix",
            checkout_dir=tmp_path,
            config=config,
            image="image",
            runner="claude-code",
            issue_num=42,
            item_id="42",
            branch="agent/42",
            title="Fix",
            model="model",
            store=MagicMock(get_route_profile=MagicMock(return_value=None)),
            run_id="run-42",
        )

    with (
        patch("kew.dispatch_core.resolve_runner"),
        patch("kew.dispatch_core._preflight_pricing"),
        patch("kew.dispatch_core._require_dispatch_transport_admission"),
        patch("kew.dispatch_core._launch_workspace", side_effect=workspace),
        patch("kew.dispatch_core.mark_failed"),
        patch("kew.dispatch_core._emit_early_exit"),
        patch("kew.dispatch_core._detect_sandbox_runtime", return_value="docker"),
        patch("kew.dispatch_sandbox.write_prompt_handoff"),
        patch(
            "kew.dispatch_sandbox.remove_prompt_handoff",
            side_effect=OSError("prompt cleanup failed"),
        ),
        patch(
            "kew.dispatch_sandbox._resolve_launch_inputs",
            return_value=("image", transport, "model", None, get_runner("claude-code")),
        ),
        patch("kew.dispatch_sandbox._record_sandbox_start", return_value="run-42"),
        patch("kew.dispatch_sandbox._require_initial_sandbox_publication_credentials"),
        patch("kew.dispatch_sandbox._require_checkout_generation_or_finalize"),
        patch("kew.dispatch_sandbox._capture_launch_evidence_or_finalize"),
        patch("kew.dispatch_sandbox._emit_sandbox_hard_stop"),
        patch(
            "kew.dispatch_sandbox._checkout_has_no_changes",
            return_value=failure in {"finalize", "prompt_cleanup", "late_refusal"},
        ),
        patch(
            "kew.dispatch_sandbox._finalize_sandbox_run",
            side_effect=(
                SandboxRuntimeUnavailable("late refusal")
                if failure == "late_refusal"
                else OSError("terminal ledger failed")
            )
            if failure in {"finalize", "late_refusal"}
            else None,
        ),
        patch("kew.dispatch_sandbox._run_sandbox_shadow"),
        patch(
            "kew.dispatch_sandbox._publish_completed_sandbox",
            side_effect=OSError("publication bookkeeping failed"),
        ),
        patch("kew.dispatch_sandbox.sidecar_switchyard_context", side_effect=sidecar),
        patch(
            "kew.dispatch_sandbox.run_container_and_stream", side_effect=stream
        ) as run,
    ):
        if failure == "teardown":
            from kew.dispatch_sandbox import _run_sidecar_container_and_stream

            def teardown_stream(*args, **kwargs):
                # Exercise the real sidecar wrapper and its failing __exit__.
                with patch(
                    "kew.dispatch_sandbox.run_container_and_stream", side_effect=stream
                ):
                    return _run_sidecar_container_and_stream(
                        *args,
                        **{
                            k: v
                            for k, v in kwargs.items()
                            if k
                            not in {
                                "inference_transport",
                                "_admission",
                                "credential",
                                "_credentials_resolved",
                            }
                        },
                        transport=transport,
                        overrides=None,
                        admission=kwargs["_admission"],
                    )

            run.side_effect = teardown_stream
        launched = _launch_runner(ctx, None)
    outcome = launched.early or _terminal_outcome(launched.result, exit_code=1)
    assert outcome.launched is True
    assert launched.result is captured[0]
    assert launched.result.exit_code != 0
    assert launched.worktree_dir == tmp_path
    ctx.refuse_rollback.assert_not_called()
    command = SimpleNamespace(
        kind="executed" if outcome.launched else "not_launched",
        exit_code=outcome.exit_code,
    )
    assert _dispatch_exit_code(command) == 1


@pytest.mark.parametrize("signal", ["bootstrap", "stale", "matching"])
@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
def test_round_ten_attempt_ack_is_the_only_stream_launch_evidence(
    tmp_path, signal, path
):
    from io import StringIO

    from test_orchestrator import TestRunAgent

    from kew.sandbox import ContainerHandle

    events = StringIO()
    nonce = "a" * 32
    lines = {
        "bootstrap": ['{"type":"bootstrap","ready":false}\n'],
        "stale": [f"KEW_AGENT_START {'b' * 32}\n", '{"type":"bootstrap"}\n'],
        "matching": [f"KEW_AGENT_START {nonce}\n"],
    }
    argv = [
        "docker",
        "run",
        "-e",
        f"KEW_AGENT_START_NONCE={nonce}",
        "--name",
        "kew-42",
        "image",
    ]
    proc = TestRunAgent._mock_popen(exit_code=0, lines=lines[signal])
    proc.pid = 1234
    proc.wait.return_value = 0
    result = DispatchResult(-1, 42, tmp_path / "run.log")
    with (
        patch("kew.subprocess_utils.popen", return_value=proc),
        patch("kew.orchestrator.popen", return_value=proc),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch.object(ContainerHandle, "start_watchdog"),
        patch.object(ContainerHandle, "stop_watchdog"),
        patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
        patch("kew.orchestrator._record_container_hard_stop"),
        patch("kew.orchestrator._remove_container_after_stop"),
    ):
        _round_nine_run_container(tmp_path, proc, argv, path, result, events)
    assert result.launched is (signal == "matching")
    assert "KEW_AGENT_START" not in result.log_file.read_text()
    assert "KEW_AGENT_START" not in events.getvalue()


@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
def test_round_ten_missing_ack_stream_failure_retains_unknown_work(tmp_path, path):
    from datetime import UTC, datetime

    from test_orchestrator import TestRunAgent

    from kew.cli import _dispatch_exit_code
    from kew.dispatch_core import _launch_runner, _terminal_outcome
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.sandbox import ContainerHandle

    proc = TestRunAgent._mock_popen(exit_code=1)
    proc.stdout.readline.side_effect = OSError("pipe failed before acknowledgement")
    result = DispatchResult(-1, 42, tmp_path / "run.log")
    argv = [
        "docker",
        "run",
        "-e",
        f"KEW_AGENT_START_NONCE={'a' * 32}",
        "--name",
        "kew-42",
        "image",
    ]
    store = SqliteRunStore(":memory:")
    store.save(
        RunRecord(
            run_id="run-42",
            issue_num=42,
            branch="agent/42",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
            log_file=result.log_file,
        )
    )
    ctx = SimpleNamespace(
        config=DispatchConfig(),
        complexity="medium",
        model_override=None,
        issue_num=42,
        store=None,
        worktree_dir=tmp_path,
        item_id="42",
        run_id="run-42",
        start_monotonic=0,
        epic_ref=None,
        refuse_rollback=MagicMock(),
        expected_reservation_run_id=None,
        expected_reservation_generation=None,
        admitted_run_id=None,
        on_admitted=None,
        claim_launch_started=False,
        container_cleanup_pending=False,
    )

    def workspace(_ctx, setup, **kwargs):
        setup.worktree_dir = tmp_path
        setup.run_sandboxed = True
        from kew.tracking import preserve_dispatch_result

        with preserve_dispatch_result(result):
            _round_nine_run_container(tmp_path, proc, argv, path, result, store=store)

    with (
        patch("kew.subprocess_utils.popen", return_value=proc),
        patch("kew.orchestrator.popen", return_value=proc),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch.object(ContainerHandle, "start_watchdog"),
        patch.object(ContainerHandle, "stop_watchdog"),
        patch("kew.orchestrator._remove_container_after_stop"),
        patch("kew.dispatch_core.resolve_runner"),
        patch("kew.dispatch_core._preflight_pricing"),
        patch("kew.dispatch_core._require_dispatch_transport_admission"),
        patch("kew.dispatch_core._launch_workspace", side_effect=workspace),
        patch("kew.dispatch_core.mark_failed"),
        patch("kew.dispatch_core._emit_early_exit"),
    ):
        launched = _launch_runner(ctx, None)
    assert result.launched is None
    assert result.error_category == "sandbox_startup_unknown"
    assert "Missing startup acknowledgement" in result.stop_reason
    assert "pipe failed" in result.stop_reason
    ctx.refuse_rollback.assert_not_called()
    assert launched.result is result
    assert launched.worktree_dir == tmp_path
    outcome = _terminal_outcome(result, exit_code=1)
    assert "Missing startup acknowledgement" in outcome.reason
    command = SimpleNamespace(
        kind="executed" if outcome.launched else "not_launched",
        exit_code=outcome.exit_code,
    )
    assert _dispatch_exit_code(command) == 1

    if path == "dispatch":
        assert store.get("run-42").status == "failure"
        (event,) = store.list_audit_events(event="sandbox_execution_failed")
        assert event.run_id == "run-42"
        assert "Missing startup acknowledgement" in event.payload["reason"]
        assert event.payload["error_category"] == "sandbox_startup_unknown"
    store.close()


def test_round_ten_generic_stream_without_observer_never_confirms(tmp_path):
    from kew.sandbox import LaunchObservedStream

    class GenericStream:
        def __iter__(self):
            yield '{"type":"bootstrap","ready":false}\n'

        def wait(self):
            return 0

    result = DispatchResult(0, 42, tmp_path / "run.log")
    assert list(LaunchObservedStream(GenericStream(), result))
    assert result.launched is False


PROXY_NAMES = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "FTP_PROXY",
    "ALL_PROXY",
    "ftp_proxy",
    "all_proxy",
]
PROXY_SECRET = "http://user:s3cret@proxy.invalid:3128"


def test_telemetry_parser_suppresses_raw_assistant_display(monkeypatch, capsys):
    from kew.dispatch_sandbox import _accumulate_stream_line, _SandboxTelemetry
    from kew.runners.claude_code import ClaudeCodeRunner

    monkeypatch.setenv("HTTP_PROXY", PROXY_SECRET)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": PROXY_SECRET}],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        }
    )
    live = _SandboxTelemetry()
    _accumulate_stream_line(live, line, ClaudeCodeRunner())

    assert "s3cret" not in capsys.readouterr().err
    assert live.input_tokens == 3
    assert live.output_tokens == 2


@pytest.fixture
def proxy_host(monkeypatch):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "HTTP_PROXY": PROXY_SECRET,
            "no_proxy": "",
            "ANTHROPIC_API_KEY": "provider-secret",
        },
    )


def _config(mode, runtime):
    config = DispatchConfig()
    # Isolate launch behavior from validation, which test_config covers.
    config.sandbox = config.sandbox.model_copy(
        update={"proxy_environment": mode, "runtime": runtime}
    )
    return config


def _proxy_actions(mode):
    inherited = ["HTTP_PROXY", "no_proxy"] if mode == "inherit" else []
    return {
        "inherited": inherited,
        "blanked": [name for name in PROXY_NAMES if name not in inherited],
    }


def _expected_proxy_args(mode):
    inherited = _proxy_actions(mode)["inherited"]
    return [name if name in inherited else f"{name}=" for name in PROXY_NAMES]


def _proxy_args(argv):
    return [
        argv[i + 1]
        for i, arg in enumerate(argv)
        if arg == "-e" and argv[i + 1].split("=", 1)[0] in PROXY_NAMES
    ]


@pytest.mark.usefixtures("proxy_host")
@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_dispatch_proxy_argv_matches_attestation(tmp_path: Path, runtime, mode):
    config = _config(mode, runtime)
    argv = build_agent_container_argv(
        config,
        image="kew-agent:test",
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="1933",
        model="sonnet",
        inference_transport=None,
    )
    expected = PROXY_NAMES if mode == "none" else ["HTTP_PROXY", "no_proxy"]
    assert _proxy_args(argv) == _expected_proxy_args(mode)
    assert "HTTPS_PROXY=" in _proxy_args(argv)
    assert PROXY_SECRET not in " ".join(argv)

    with SqliteRunStore(str(tmp_path / "audit.db")) as store:
        ctx = MagicMock(spec=_DispatchContext)
        ctx.config = config
        ctx.store = store
        ctx.run_id, ctx.issue_num, ctx.item_id = "run-1933", 1933, "1933"
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime=runtime,
            image="kew-agent:test",
        )
        payload = store.list_audit_events(event="sandbox_resolved")[0].payload
    assert payload.get("proxy_environment") == mode
    assert payload["proxy_variables"] == _proxy_actions(mode)
    assert payload["credential_names"] == ["ANTHROPIC_API_KEY"] + (
        expected if mode == "inherit" else []
    )
    assert PROXY_SECRET not in json.dumps(payload)


@pytest.mark.usefixtures("proxy_host")
@pytest.mark.parametrize("mode", ["none", "inherit"])
@pytest.mark.parametrize("posture", ["sandboxed", "native", "refused"])
def test_flow_proxy_attestation(tmp_path: Path, mode, posture):
    with SqliteRunStore(str(tmp_path / "flow.db")) as store:
        attest_flow_posture(
            store,
            flow="review",
            runner="claude-code",
            config=_config(mode, "docker").sandbox,
            posture=posture,
        )
        payload = store.list_audit_events(event="sandbox_resolved")[0].payload
    assert payload.get("proxy_environment") == mode
    expected = PROXY_NAMES if mode == "none" else ["HTTP_PROXY", "no_proxy"]
    assert payload["proxy_variables"] == (
        _proxy_actions(mode)
        if posture == "sandboxed"
        else {"inherited": [], "blanked": []}
    )
    if posture == "sandboxed":
        assert payload["credential_names"] == ["ANTHROPIC_API_KEY"] + (
            expected if mode == "inherit" else []
        )
    assert PROXY_SECRET not in json.dumps(payload)


@pytest.mark.usefixtures("proxy_host")
@pytest.mark.parametrize("runtime", ["docker", "podman"])
@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_sidecar_worker_proxy_environment(tmp_path: Path, monkeypatch, runtime, mode):
    supervisor = MagicMock()
    supervisor.network_name = "attempt-internal"
    supervisor.worker_name = "attempt-worker"
    supervisor.owner = "0" * 32
    supervisor.create_worker.return_value = "a" * 64
    supervisor.worker_environment.return_value = {"ANTHROPIC_API_KEY": "synthetic"}
    transport = MagicMock()
    transport.endpoint = "http://172.30.0.2:4000"
    transport.endpoint_env_var = "ANTHROPIC_BASE_URL"
    filter_install = MagicMock()
    monkeypatch.setattr(
        "kew.dispatch_sandbox.install_worker_netns_filter", filter_install
    )
    prepare_sidecar_worker(
        _config(mode, runtime),
        supervisor=supervisor,
        image_id="sha256:" + "c" * 64,
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="1933",
        model="sonnet",
        inference_transport=transport,
        packet_evidence=MagicMock(),
    )
    assert filter_install.call_args.kwargs["proxy_environment"] == mode
    argv = supervisor.create_worker.call_args.args[0]
    expected = _expected_proxy_args(mode)
    assert _proxy_args(argv) == expected
    assert argv[:2] == [runtime, "create"]
    assert PROXY_SECRET not in " ".join(argv)


@pytest.mark.usefixtures("proxy_host")
def test_synthetic_credential_attestation_includes_inherited_proxies(tmp_path: Path):
    with SqliteRunStore(str(tmp_path / "synthetic.db")) as store:
        ctx = MagicMock(spec=_DispatchContext)
        ctx.config = _config("inherit", "docker")
        ctx.store = store
        ctx.run_id, ctx.issue_num, ctx.item_id = "run-1933", 1933, "1933"
        _emit_sandbox_resolved(
            ctx,
            "claude-code",
            posture="sandboxed",
            runtime="docker",
            image="kew-agent:test",
            synthetic_credential_name="ANTHROPIC_API_KEY",
        )
        payload = store.list_audit_events(event="sandbox_resolved")[0].payload
    assert payload["credential_names"] == [
        "ANTHROPIC_API_KEY",
        "HTTP_PROXY",
        "no_proxy",
    ]
    assert payload["proxy_environment"] == "inherit"
    assert payload["proxy_variables"] == _proxy_actions("inherit")
    assert PROXY_SECRET not in json.dumps(payload)


@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_filter_install_threads_proxy_policy(monkeypatch, mode):
    from kew import dispatch_sandbox
    from kew.sandbox import WorkerNetnsFilterError

    secret = "http://user:s3cret@proxy.invalid:3128"
    for name in PROXY_NAMES:
        monkeypatch.setenv(name, secret)
    command = MagicMock(return_value=MagicMock(returncode=1))
    monkeypatch.setattr(dispatch_sandbox, "_run", command)
    with pytest.raises(WorkerNetnsFilterError):
        dispatch_sandbox.install_worker_netns_filter(
            runtime="docker",
            image_id="sha256:" + "a" * 64,
            worker_id="b" * 64,
            owner="c" * 32,
            proxy_address="172.30.0.2",
            proxy_port=4000,
            proxy_environment=mode,
        )
    # Main's helper ownership (#1943) stops the failed helper after creation,
    # so the creation argv is the first runtime call, not the last.
    argv = command.call_args_list[0].args[0]
    assert argv[:2] == ["docker", "create"]
    # Only the six inheritable names pass by name under inherit; FTP_PROXY,
    # ALL_PROXY and their lowercase forms are blank under either policy.
    assert _proxy_args(argv) == [
        name if mode == "inherit" and name in CONTAINER_PROXY_NAMES else f"{name}="
        for name in PROXY_NAMES
    ]
    for call in command.call_args_list:
        assert secret not in " ".join(call.args[0])


@pytest.mark.parametrize("binary", [False, True])
def test_filter_failure_redacts_proxy_before_truncation(monkeypatch, binary):
    import subprocess

    from kew import dispatch_sandbox
    from kew.sandbox import WorkerNetnsFilterError

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    output = secret + "a" * 480
    check = dispatch_sandbox._require_filter_command
    if binary:
        output = output.encode()
        check = dispatch_sandbox._require_filter_bytes_command
    with pytest.raises(WorkerNetnsFilterError) as error:
        check(subprocess.CompletedProcess([], 1, output, None), "filter")
    assert secret not in str(error.value)
    assert "proxy.invalid" not in str(error.value)


def test_direct_breaker_audit_redacts_inherited_proxy(tmp_path, monkeypatch):
    from kew import dispatch_sandbox
    from kew.tracking import DispatchResult

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    result = DispatchResult(exit_code=137, issue_num=1933, log_file=tmp_path / "l")
    result.hard_stop = "resource_cap"
    result.resource_cap_reason = secret
    result.oom_probe_diagnostic = secret
    result.resource_cap_observations = [{"code": "oom", "reason": secret}]
    with SqliteRunStore(str(tmp_path / "proxy.db")) as store:
        dispatch_sandbox._emit_sandbox_hard_stop(
            store,
            _config("inherit", "docker"),
            result,
            run_id="1933",
            issue_num=1933,
            item_id="1933",
            model="sonnet",
        )
        [event] = store.list_audit_events(event="circuit_breaker")
    assert secret not in json.dumps(event.payload)
    assert event.payload["oom_probe_diagnostic"] == "<HTTP_PROXY redacted>"


def test_a_sidecar_attempt_auth_path_outranks_a_brokered_provider():
    """The narrowest credential owner names the auth path (#2318 review).

    On the OpenShell runtime a sidecar launch brokers nothing: the supervisor
    mints its worker credential for this attempt and no gateway provider holds
    it. Reading the broker first attested a sidecar attempt as authenticating
    through the operator's long-lived provider, dropping the per-attempt
    credential that actually authenticated it.
    """
    from kew.dispatch_sandbox import _attempt_auth_path
    from kew.inference_transport import ResolvedInferenceTransport

    sidecar = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="sonnet",
        model="route",
        config_sha256="d" * 64,
        sidecar=True,
        worker_credential_env_name="ANTHROPIC_API_KEY",
    )
    direct = ResolvedInferenceTransport(
        mode="direct",
        requested_model="sonnet",
        model="sonnet",
        config_sha256="d" * 64,
    )
    # Defence in depth: production never hands both in, because
    # `launch_provider` returns no provider for a sidecar launch at all.
    assert (
        _attempt_auth_path("claude-code", sidecar, "kew-claude-code")
        == "synthetic:per-attempt"
    )
    assert _attempt_auth_path("claude-code", sidecar, None) == "synthetic:per-attempt"
    assert (
        _attempt_auth_path("claude-code", direct, "kew-claude-code")
        == "broker:openshell:kew-claude-code"
    )


def test_a_sidecar_launch_carries_no_broker_provider_into_its_profile():
    """`launch_provider` is the one place the sidecar precedence is decided."""
    from kew.config import OpenShellConfig, SandboxConfig
    from kew.openshell_broker import launch_provider

    config = SandboxConfig(
        runtime="openshell",
        enabled=True,
        openshell=OpenShellConfig(
            gateway="http://gateway.invalid:8080",
            providers={"claude-code": "kew-claude-code"},
        ),
    )
    assert (
        launch_provider(config, "claude-code", run_id="run-2318", issued=False)
        == "kew-claude-code"
    )
    assert (
        launch_provider(
            config, "claude-code", run_id="run-2318", issued=False, sidecar=True
        )
        is None
    )
    assert (
        launch_provider(
            config, "claude-code", run_id="run-2318", issued=True, sidecar=True
        )
        is None
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("direct", "provider:kew-run-run-2318"), ("switchyard", "provider:kew-opencode")],
)
def test_dispatch_attests_the_transport_mode_not_the_egress_route(
    tmp_path: Path, monkeypatch, mode, expected
):
    """The production wiring carries the transport's own `direct` fact (#2318).

    `_attest_direct_sandbox_resolution` attests every launch without a sidecar,
    switchyard included — so "direct egress" cannot stand in for "talks
    straight to its upstream". It did, and a switchyard launch was attested
    with the per-run provider of a bounded key nothing had minted.
    """
    from kew.config import OpenShellConfig
    from kew.dispatch_core import _attest_direct_sandbox_resolution
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.sandbox import SandboxDecision

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-2318")
    config = _config("none", "openshell")
    config.sandbox = config.sandbox.model_copy(
        update={
            "openshell": OpenShellConfig(
                gateway="http://gateway.invalid:8080",
                providers={"opencode": "kew-opencode"},
            )
        }
    )
    transport = ResolvedInferenceTransport(
        mode=mode,
        requested_model="sonnet",
        model="sonnet",
        config_sha256="d" * 64,
    )
    decision = SandboxDecision(
        sandboxed=True,
        runtime="openshell",
        posture="sandboxed",
        notice="",
        image="kew-opencode:pinned",
    )
    with SqliteRunStore(str(tmp_path / "attest.db")) as store:
        ctx = MagicMock(spec=_DispatchContext)
        ctx.config = config
        ctx.store = store
        ctx.run_id, ctx.issue_num, ctx.item_id = "run-2318", 2318, "2318"
        _attest_direct_sandbox_resolution(
            ctx, "opencode", decision, transport, tmp_path
        )
        payload = store.list_audit_events(event="sandbox_resolved")[0].payload
    assert payload["credential_names"][0] == expected


def _openshell_config():
    """`runtime = "openshell"` with the provider a claude-code launch brokers."""
    from kew.config import OpenShellConfig

    config = _config("none", "openshell")
    config.sandbox = config.sandbox.model_copy(
        update={
            "openshell": OpenShellConfig(
                gateway="http://gateway.invalid:8080",
                providers={"claude-code": "kew-claude-code"},
            )
        }
    )
    return config


def test_a_stop_during_the_openshell_upload_never_reaches_an_exec(
    tmp_path, monkeypatch
):
    """The creation permit is rechecked once the sandbox exists (#2401 review).

    The docker path rechecks between `docker create` and `docker start`
    (`prepare_registered_container`), and the OpenShell docstring claimed that
    parity while checking once, before `sandbox create`. Create, the id
    read-back and a whole-checkout upload all sit inside that window — a far
    wider one than docker's — so a durable stop landing in it was ignored and
    the agent was exec'd anyway.
    """
    from test_openshell_launch import FakeGateway

    import kew.openshell_launch as openshell_launch
    from kew.dispatch_sandbox import _launch_openshell_sandbox, _record_sandbox_start
    from kew.openshell import OpenShellClient

    # The certification match each launch makes (#2624) is not this test's
    # subject; tests/test_openshell_launch.py::TestTheLaunchGate runs it.
    monkeypatch.setattr(
        openshell_launch, "require_launch_certification", lambda *_a, **_k: None
    )

    config = _openshell_config()
    log = tmp_path / "openshell.log"
    with SqliteRunStore(str(tmp_path / "openshell.db")) as store:
        run_id = _record_sandbox_start(
            store,
            issue_num=2401,
            item_id="2401",
            branch="agent/2401",
            title="test",
            model="sonnet",
            runner_name="claude-code",
            log_file=log,
            repo=config.project.repo,
            worker_cap=1,
        )

        class StopDuringUpload(FakeGateway):
            """The operator stops the run while its checkout is uploading."""

            def sandbox_upload(self, name, source, destination):
                super().sandbox_upload(name, source, destination)
                store.request_run_stop(run_id)

        gateway = StopDuringUpload()
        monkeypatch.setattr(
            OpenShellClient,
            "from_settings",
            classmethod(lambda cls, settings, environment=None: gateway),
        )

        with pytest.raises(RuntimeError, match="refused after stop"):
            _launch_openshell_sandbox(
                config,
                store,
                run_id=run_id,
                log_file=log,
                image="sha256:" + "a" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug="2401-openshell",
                model="sonnet",
                admission=None,
                credential=None,
                transport=None,
                sidecar_supervisor=None,
                run_kind="dispatch",
            )

    assert gateway.names == [
        "sandbox_create",
        "sandbox_get",
        "sandbox_upload",
        "sandbox_delete",
    ], "the stopped run was exec'd, or its sandbox was left on the gateway"


def test_a_sidecar_supervisor_is_refused_rather_than_handed_a_docker_argv(tmp_path):
    """`runtime = "openshell"` with a sidecar has no launch path (#2401 review).

    The branch was gated on `sidecar_supervisor is None`, so a switchyard
    sidecar transport on this runtime fell through to `prepare_sidecar_worker`
    and built `openshell create --init --pull=never ...` — a docker argv handed
    to a CLI with no `create` subcommand.
    """
    from kew.dispatch_sandbox import _launch_streaming_container
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.openshell_launch import OpenShellLaunchRefused

    transport = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="sonnet",
        model="sonnet",
        config_sha256="d" * 64,
        sidecar=True,
        endpoint="http://127.0.0.1:9/",
        worker_credential_env_name="ANTHROPIC_API_KEY",
    )
    supervisor = MagicMock()

    with pytest.raises(OpenShellLaunchRefused, match="docker topology"):
        _launch_streaming_container(
            _openshell_config(),
            None,
            run_id="run-2401",
            log_file=tmp_path / "sidecar.log",
            image="sha256:" + "b" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="2401-openshell",
            model="sonnet",
            transport=transport,
            sidecar_supervisor=supervisor,
            sidecar_ready=None,
            run_kind="dispatch",
        )

    supervisor.create_worker.assert_not_called()


def test_a_sidecarless_switchyard_transport_is_refused_rather_than_unwired(tmp_path):
    """Shown to fail: it launched, with the endpoint nowhere (#2401 review).

    The branch was gated on `sidecar_supervisor is None` alone, so a Switchyard
    transport without a sidecar went to `launch_openshell_container` — which
    builds the exec from `openshell_launch_environment` and the runner's
    `container_args`, neither of which carries claude-code's
    `ANTHROPIC_BASE_URL`. The docker path assigns it in the container argv
    (`sandbox._container_environment_args`); this runtime has no argv to assign
    it in, and no packaged egress policy permits a switchyard destination
    anyway. So the agent ran against the upstream its policy does permit while
    the audit chain attested a switchyard transport.

    The admission gate refuses this first
    (`sandbox._refuse_unlaunchable_openshell`); this backstop is what makes the
    rule hold for a caller that resolved containment some other way.
    """
    from kew.dispatch_sandbox import _launch_streaming_container
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.openshell_launch import OpenShellLaunchRefused

    transport = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="sonnet",
        model="sonnet",
        config_sha256="d" * 64,
        sidecar=False,
        endpoint="http://127.0.0.1:9/",
    )

    with pytest.raises(OpenShellLaunchRefused, match="no endpoint override"):
        _launch_streaming_container(
            _openshell_config(),
            None,
            run_id="run-2401",
            log_file=tmp_path / "switchyard.log",
            image="sha256:" + "c" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="2401-openshell",
            model="sonnet",
            transport=transport,
            sidecar_supervisor=None,
            sidecar_ready=None,
            run_kind="dispatch",
        )


def test_the_argv_build_site_refuses_the_gateway_runtime(tmp_path):
    """No docker command line is ever spelled for `runtime = "openshell"`.

    `build_agent_container_argv` is what `dispatch_core._contained_execution_for`
    calls for every tracked remediation flow (`kew review`, `kew resolve`,
    `kew ci-fix`, graph `plan_run`), and it took the configured runtime as
    `argv[0]`: the result was `openshell run --rm --init --pull=never --name
    kew-... ` handed to a CLI with no `run` subcommand (PR #2401 review).
    Admission refuses those flows first; this is the seam that holds even for a
    caller that resolved containment some other way.
    """
    from kew.dispatch_sandbox import build_agent_container_argv
    from kew.sandbox import SandboxRuntimeUnavailable

    with pytest.raises(SandboxRuntimeUnavailable) as refused:
        build_agent_container_argv(
            _openshell_config(),
            image="sha256:" + "c" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="2401-openshell",
            model="sonnet",
            inference_transport=None,
        )
    assert refused.value.reason == "openshell-runtime-unavailable"
    assert refused.value.runtime == "openshell"


def test_a_sidecar_worker_bootstrap_cannot_be_built_for_the_gateway(tmp_path):
    """The other `build_run_argv` caller is refused at the same seam.

    `prepare_sidecar_worker` builds its worker with `operation="create"`, which
    is the subcommand the gateway CLI most conspicuously does not have.
    """
    from kew.sandbox import SandboxRuntimeUnavailable, build_run_argv

    with pytest.raises(SandboxRuntimeUnavailable):
        build_run_argv(
            runtime="openshell",
            image="sha256:" + "c" * 64,
            checkout_dir=tmp_path,
            slug="2401-openshell",
            network="none",
            runner="claude-code",
            operation="create",
            entrypoint="/bin/sleep",
            command=["infinity"],
        )


def test_a_store_backed_openshell_launch_registers_its_own_client(
    tmp_path, monkeypatch
):
    """Shown to fail: every store-backed OpenShell launch refused its own spawn.

    The run row carries the sandbox NAME and no container id by design
    (`openshell_launch._record_identity`), and `_check_spawn_permit` demanded a
    64-hex id of every runtime. So `_record_retry_pgid` raised for each launch,
    `_announce_spawn` swallowed the raise and SIGKILLed the agent's client, and
    what the caller got back was a handle over a dead client instead of the
    streaming one it waits on (PR #2401 review).

    The client here is a real child in its own session, because the two facts
    asserted belong to the OS: the registered `attempt_pgid` has to be a group
    a stop can signal, and the client has to still be running once the launch
    has returned.
    """
    import signal
    import sys

    from test_openshell_launch import SANDBOX_ID, FakeGateway

    import kew.openshell_launch as openshell_launch
    from kew import process_groups
    from kew.dispatch_sandbox import _launch_openshell_sandbox, _record_sandbox_start
    from kew.openshell import OpenShellClient
    from kew.openshell_launch import OpenShellSandboxHandle
    from kew.runtime_names import OPENSHELL_RUNTIME

    # The certification match each launch makes (#2624) is not this test's
    # subject; tests/test_openshell_launch.py::TestTheLaunchGate runs it.
    monkeypatch.setattr(
        openshell_launch, "require_launch_certification", lambda *_a, **_k: None
    )

    config = _openshell_config()
    log = tmp_path / "openshell.log"
    client = subprocess.Popen(  # noqa: S603 — fixed interpreter, no shell
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        gateway = FakeGateway()
        gateway.process = client
        monkeypatch.setattr(
            OpenShellClient,
            "from_settings",
            classmethod(lambda cls, settings, environment=None: gateway),
        )
        with SqliteRunStore(str(tmp_path / "openshell.db")) as store:
            run_id = _record_sandbox_start(
                store,
                issue_num=2401,
                item_id="2401",
                branch="agent/2401",
                title="test",
                model="sonnet",
                runner_name="claude-code",
                log_file=log,
                repo=config.project.repo,
                worker_cap=1,
                runtime=OPENSHELL_RUNTIME,
            )

            handle = _launch_openshell_sandbox(
                config,
                store,
                run_id=run_id,
                log_file=log,
                image="sha256:" + "a" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug="2401-openshell",
                model="sonnet",
                admission=None,
                credential=None,
                transport=None,
                sidecar_supervisor=None,
                run_kind="dispatch",
            )

            assert isinstance(handle, OpenShellSandboxHandle)
            assert gateway.names == [
                "sandbox_create",
                "sandbox_get",
                "sandbox_upload",
                "sandbox_exec",
            ], "a registration refusal deleted the sandbox mid-launch"
            row = store.get(run_id)
            assert row.attempt_pgid == client.pid
            assert row.container_name == gateway.calls[0].detail["name"]
            assert row.container_name.startswith("kew-2401-openshell-")
            assert row.container_id == SANDBOX_ID  # the gateway id, read back
            assert process_groups.group_alive(client.pid) is True
            assert process_groups.attempts_are_dead(row.attempt_pgid, log) is False
            assert client.poll() is None, "the client was killed by its own launch"

        events = [
            json.loads(line)
            for line in log.with_suffix(".events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        kinds = [event.get("type") for event in events]
        assert "spawn_registration_failed" not in kinds
        identity = events[kinds.index("container_identity")]
        assert identity["runtime"] == OPENSHELL_RUNTIME
        assert identity["sandbox_id"] == SANDBOX_ID
        assert identity["container_name"] == row.container_name
        assert identity["pid"] == client.pid
    finally:
        with suppress(ProcessLookupError):
            os.killpg(client.pid, signal.SIGKILL)
        client.wait(timeout=5)
        if client.stdout is not None:
            client.stdout.close()


def test_the_openshell_lane_refuses_a_repo_supplied_codex_config(tmp_path, monkeypatch):
    """Shown to fail: the gateway uploaded and exec'd a tree the host refuses.

    `orchestrator._preflight_runner_workspace` is the HOST lane's `.codex/`
    admission check, and it was the only copy. Codex declining `"native"` on
    `openshell` (node N10) is what first routed a codex dispatch into the
    contained lane, so the one runtime this PR adds was also the one lane where
    a repo could ship its own `.codex/config.toml` — `sandbox_upload` put it in
    the sandbox and `sandbox_exec` ran codex on top of it (PR #2461 review).
    """
    from test_openshell_launch import FakeGateway

    from kew.dispatch_sandbox import _launch_streaming_container
    from kew.openshell import OpenShellClient
    from kew.runners.codex import CodexProjectConfigError

    config = _openshell_config()
    checkout = tmp_path / "checkout"
    (checkout / ".codex").mkdir(parents=True)
    (checkout / ".codex" / "config.toml").write_text("[sandbox]\nmode = 'off'\n")

    gateway = FakeGateway()
    monkeypatch.setattr(
        OpenShellClient,
        "from_settings",
        classmethod(lambda cls, settings, environment=None: gateway),
    )

    with pytest.raises(CodexProjectConfigError, match="entry is not allowed"):
        _launch_streaming_container(
            config,
            None,
            run_id="run-2461",
            log_file=tmp_path / "codex.log",
            image="sha256:" + "e" * 64,
            runner="codex",
            checkout_dir=checkout,
            slug="2461-openshell",
            model="gpt-5.6-sol",
            transport=None,
            sidecar_supervisor=None,
            sidecar_ready=None,
            run_kind="dispatch",
        )

    assert gateway.names == [], "the refused tree reached the gateway"


def test_the_contained_lane_admits_a_workspace_without_project_config(tmp_path):
    """The check is an admission, not a refusal of the lane itself.

    A tree with nothing repo-supplied in it goes on to the launch it would have
    reached before — asserted here on the docker argv build rather than a
    gateway, so a preflight that raised on every workspace could not pass.
    """
    from kew.dispatch_sandbox import _launch_streaming_container

    checkout = tmp_path / "clean"
    (checkout / ".codex" / "rules").mkdir(parents=True)
    (checkout / ".codex" / "rules" / "house.md").write_text("be careful\n")

    with patch("kew.dispatch_sandbox.build_agent_container_argv") as build:
        build.side_effect = RuntimeError("reached the argv build")
        with pytest.raises(RuntimeError, match="reached the argv build"):
            _launch_streaming_container(
                _config("none", "docker"),
                None,
                run_id="run-2461",
                log_file=tmp_path / "codex.log",
                image="sha256:" + "f" * 64,
                runner="codex",
                checkout_dir=checkout,
                slug="2461-docker",
                model="gpt-5.6-sol",
                transport=None,
                sidecar_supervisor=None,
                sidecar_ready=None,
                run_kind="dispatch",
            )


@pytest.mark.policy
def test_every_launch_call_in_the_tree_names_its_run_kind():
    """A hand-built launch that omits ``run_kind`` raises before it asserts.

    The argument is keyword-only and defaultless on purpose (``_launch_
    streaming_container``'s docstring: the value an absent one would take is
    the DISPATCH boundary). Nothing checks it until the call runs, so the two
    ``.codex/config.toml`` admission tests above — written on one branch while
    the argument became required on another — raised ``TypeError`` before
    reaching a single assertion, and the containment guard they are the only
    cover for was uncovered with a green-looking module (PR #2520 review).

    Repo-wide rather than per-module because the direct callers are spread
    across a dozen test files; ``**kwargs`` forwarding is exempt, since the
    keyword travels in the caller's dict.
    """
    import ast

    root = Path(__file__).parent.parent
    violations = []
    for path in [
        *sorted((root / "tests").rglob("*.py")),
        *sorted((root / "src").rglob("*.py")),
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = (
                called.attr
                if isinstance(called, ast.Attribute)
                else getattr(called, "id", None)
            )
            if name != "_launch_streaming_container":
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            if "run_kind" in keywords or None in keywords:
                continue
            violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, (
        "these launches omit run_kind and raise TypeError before asserting:\n"
        + "\n".join(violations)
    )


class TestWhatAPlacedAttemptAttests:
    """A placed container's boundary is the INSTANCE's bytes (#2526 review).

    The containment profile is defined as the boundary resolved immediately
    before one paid child launch, and #1550 F1 is that capture and execution
    cannot describe different bytes. A placed run's container is launched from
    the mutable tag against the instance's own image store, so the ID this
    machine resolved for that tag names an image no container there reads —
    which is why the profile is not written at admission for a placed run and
    is written from the instance's own answer instead.
    """

    #: The ID this machine resolved, which a placed attempt may not claim.
    LOCAL_IMAGE_ID = "sha256:" + "a" * 64

    #: The ID the instance resolved for the same tag.
    INSTANCE_IMAGE_ID = "sha256:" + "b" * 64

    def test_only_a_deferred_or_sidecar_attempt_waits(self) -> None:
        from kew.dispatch_sandbox import _profile_recorded_after_launch
        from kew.inference_transport import ResolvedInferenceTransport

        direct = ResolvedInferenceTransport(
            mode="direct",
            requested_model="sonnet",
            model="sonnet",
            config_sha256="0" * 64,
        )
        sidecar = ResolvedInferenceTransport(
            mode="switchyard",
            requested_model="sonnet",
            model="sonnet",
            config_sha256="0" * 64,
            sidecar=True,
        )

        assert _profile_recorded_after_launch(False, direct) is False
        assert _profile_recorded_after_launch(False, sidecar) is True
        assert _profile_recorded_after_launch(True, direct) is True

    def test_a_placed_attempt_writes_no_profile_at_admission(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_sandbox import _record_sandbox_start
        from kew.run_store_readiness import RouteProfileMissingError

        with SqliteRunStore(str(tmp_path / "placed.db")) as store:
            run_id = _record_sandbox_start(
                store,
                issue_num=2526,
                item_id="2526",
                branch="agent/issue-2526",
                title="test",
                model="sonnet",
                runner_name="claude-code",
                log_file=tmp_path / "sandbox.log",
                image="kew-agent:docker-test",
                image_id=self.LOCAL_IMAGE_ID,
                runtime="docker",
                profile_deferred=True,
            )

            with pytest.raises(RouteProfileMissingError):
                store.require_route_profile(run_id, 1)

    def test_the_recorded_boundary_names_the_instances_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew.dispatch_sandbox import _record_placed_profile, _record_sandbox_start

        # The attempt's auth path, which the publication gate this records
        # through reads: a placed launch forwards the runner's declared
        # variable by name exactly as a local contained one does.
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "kew-fixture-model-credential")
        with SqliteRunStore(str(tmp_path / "placed.db")) as store:
            run_id = _record_sandbox_start(
                store,
                issue_num=2526,
                item_id="2526",
                branch="agent/issue-2526",
                title="test",
                model="sonnet",
                runner_name="claude-code",
                log_file=tmp_path / "sandbox.log",
                image="kew-agent:docker-test",
                image_id=self.LOCAL_IMAGE_ID,
                runtime="docker",
                profile_deferred=True,
            )

            _record_placed_profile(
                self.INSTANCE_IMAGE_ID,
                store=store,
                run_id=run_id,
                runner_name="claude-code",
                model="sonnet",
                tracker="github",
                image="kew-agent:docker-test",
                transport=None,
                service_tier=None,
                credential=None,
                broker_provider=None,
            )

            recorded = store.require_route_profile(run_id, 1).profile_json
            assert self.INSTANCE_IMAGE_ID in recorded
            assert self.LOCAL_IMAGE_ID not in recorded


# ── an OpenShell sandbox whose delete is unconfirmed (#2625) ─────────────────


def test_a_launch_unwind_that_left_its_sandbox_keeps_the_run_pending() -> None:
    """The unwind's unconfirmed delete becomes UNCONFIRMED cleanup evidence.

    That evidence is what every launch path reads before finalizing: unconfirmed,
    the row and its reservation are kept rather than finalized
    ``sandbox_launch_error`` beside a sandbox that may still hold the run's
    provider.
    """
    from kew.dispatch_sandbox import (
        _record_unreleased_sandbox,
        container_launch_failure,
    )
    from kew.openshell_launch import UNRELEASED_SANDBOX_ATTR

    left = RuntimeError("create timed out")
    setattr(left, UNRELEASED_SANDBOX_ATTR, "kew-42-abc")
    clean = RuntimeError("create refused")

    _record_unreleased_sandbox(left)
    _record_unreleased_sandbox(clean)

    evidence = container_launch_failure(left)
    assert evidence is not None and evidence.cleanup_confirmed is False
    assert ("openshell", "kew-42-abc") in evidence.owners
    assert container_launch_failure(clean) is None


def test_a_finished_run_whose_sandbox_is_not_released_is_not_finished() -> None:
    """A run may not end while its sandbox delete is unconfirmed."""
    from test_openshell_launch import FakeGateway, _handle, _StuckGateway

    from kew.dispatch_sandbox import (
        OpenShellSandboxUnreleased,
        _require_sandbox_released,
        container_launch_failure,
    )

    stuck = _handle(_StuckGateway())
    stuck.cleanup_identity()
    released = _handle(FakeGateway())
    released.cleanup_identity()

    with pytest.raises(OpenShellSandboxUnreleased) as raised:
        _require_sandbox_released(stuck)

    evidence = container_launch_failure(raised.value)
    assert evidence is not None and evidence.cleanup_confirmed is False
    _require_sandbox_released(released)
    _require_sandbox_released(MagicMock())  # docker handles have no such fact


def test_a_failed_stream_releases_its_sandbox_before_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream failure may not finalize a run whose sandbox delete is unconfirmed.

    The failure arm used to finalize the row terminal first and release the
    sandbox only in the cleanup after it, so an unconfirmed delete was left
    behind a terminal row with no cleanup evidence (PR #2692 review).
    """
    from test_openshell_launch import FakeGateway, _handle, _StuckGateway

    import kew.dispatch_sandbox as dispatch_sandbox
    from kew.dispatch_sandbox import _container_stream_failed, container_launch_failure

    finalized: list[bool] = []
    monkeypatch.setattr(
        dispatch_sandbox,
        "_finalize_sandbox_run",
        lambda *_a, **_k: finalized.append(released.released),
    )

    released = stuck = _handle(_StuckGateway())
    failure = RuntimeError("stream broke")
    permitted = _container_stream_failed(
        None,
        "run-42",
        stuck,
        DispatchResult(-1, 42, Path("x.log")),
        failure,
        "dispatch",
        "m",
        None,
    )

    assert finalized == [] and permitted is False
    evidence = container_launch_failure(failure)
    assert evidence is not None and evidence.cleanup_confirmed is False
    assert ("openshell", stuck.sandbox_name) in evidence.owners

    released = _handle(FakeGateway())
    failure = RuntimeError("stream broke")
    permitted = _container_stream_failed(
        None,
        "run-43",
        released,
        DispatchResult(-1, 42, Path("x.log")),
        failure,
        "dispatch",
        "m",
        None,
    )

    assert finalized == [True] and permitted is True
    assert container_launch_failure(failure) is None


def test_the_store_records_a_gateway_id_only_for_openshell(tmp_path: Path) -> None:
    """The 64-hex rule docker readers rely on is not widened for them."""
    store = SqliteRunStore(tmp_path / "runs.db")

    store.set_container_identity(
        "run-1", "kew-42-abc", "sbx_01HN4Q8ZJ2WME5T", runtime="openshell"
    )
    with pytest.raises(ValueError, match="full container id"):
        store.set_container_identity("run-1", "kew-42-abc", "sbx_01HN4Q8ZJ2WME5T")
    with pytest.raises(ValueError, match="full container id"):
        store.set_container_identity(
            "run-1", "kew-42-abc", "--gateway=evil", runtime="openshell"
        )
