"""Bounded host-side lifecycle helpers for live runner canaries."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kew import work_dirs
from kew.canary import (
    CONTRACTS,
    GovernanceFields,
    PassthroughCanaryEvidence,
    RunnerOutcome,
    canary_checks,
    passthrough_canary_checks,
    reduce_run,
)
from kew.config import DispatchConfig
from kew.health import CheckResult
from kew.inference_transport import (
    TransportResolutionError,
    resolve_inference_transport,
)
from kew.pricing import build_pricing_overrides
from kew.progress import display_suppressed
from kew.runners.codex import (
    CodexRunner,
    codex_process_env,
    validate_codex_project_tree,
)
from kew.runners.protocol import AgentRunner, StreamEvent
from kew.subprocess_utils import RunnerReachability, inspect_runner, popen, run
from kew.switchyard import probe_switchyard_endpoint
from kew.switchyard_metrics import SwitchyardMetricsError, observe_switchyard_metrics

_FRESH_PROMPT = "Run `sleep 30`, then reply exactly RESUME_STAGE_ONE."
_RESUME_TIMEOUT_SECONDS = 120
_FRESH_DEADLINE_SECONDS = 15
_MAX_BYTES = 1_000_000
_MAX_LINES = 10_000
_MAX_LINE_LENGTH = 100_000


class _CanaryLifecycleError(RuntimeError):
    """Sanitized, session-free live canary failure."""


class CanaryRecordError(ValueError):
    """A raw-output directory is absent or not private enough for evidence."""


class _ManagedLifecycle(Protocol):
    def close(self) -> None: ...


def validate_canary_record_directory(record_dir: str | os.PathLike[str]) -> Path:
    """Resolve an existing, owner-only directory without creating it."""
    directory = Path(record_dir)
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise CanaryRecordError("--record DIR must already exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CanaryRecordError("--record DIR must be a real directory")
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != os.geteuid() or mode & 0o077 or mode & 0o700 != 0o700:
        raise CanaryRecordError("--record DIR must be owner-only (mode 0700)")
    try:
        return directory.resolve(strict=True)
    except OSError as exc:
        raise CanaryRecordError("--record DIR must already exist") from exc


def write_canary_record(record_dir: Path, filename: str, lines: list[str]) -> Path:
    """Atomically write one raw record with owner-only file permissions."""
    directory = validate_canary_record_directory(record_dir)
    if Path(filename).name != filename:
        raise CanaryRecordError("raw record filename is invalid")
    target = directory / filename
    fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        try:
            output = os.fdopen(fd, "w")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with output:
            output.write("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return target


def codex_reachability_reason(reach: RunnerReachability) -> str | None:
    """Return a sanitized refusal reason, or ``None`` when Codex is usable."""
    if not reach.binary_present:
        return "codex binary missing"
    if reach.version_supported is not True:
        return "codex version unsupported or unparseable"
    if reach.credential_detected is not True:
        return "codex login unavailable"
    return None


def delete_codex_session(session_id: str | None) -> CheckResult:
    """Best-effort bounded deletion that never returns or logs the session id."""
    if not session_id:
        return CheckResult(
            "session_deleted", "fail", "no session available for deletion", {}
        )
    try:
        result = run(
            ["codex", "delete", "--force", session_id],
            check=False,
            capture=True,
            timeout=5,
            env=codex_process_env(),
        )
        deleted = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, ValueError):
        deleted = False
    return CheckResult(
        "session_deleted",
        "pass" if deleted else "fail",
        "session deleted" if deleted else "session deletion failed",
        {"deleted": deleted},
    )


def _write_resume_record(record_dir: str | None, lines: list[str]) -> None:
    if record_dir is None:
        return
    directory = Path(record_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "codex-resume.jsonl"
    temporary = directory / ".codex-resume.jsonl.tmp"
    temporary.write_text("\n".join(lines) + "\n")
    os.replace(temporary, target)


def _reader(stream: Iterable[str], output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(str(line).rstrip("\n"))
    finally:
        output.put(None)


@dataclass
class _CaptureState:
    lines: list[str]
    byte_count: int = 0
    session_id: str | None = None
    turn_started: bool = False
    failure: str | None = None


def _fresh_line_within_bounds(state: _CaptureState, line: str) -> bool:
    if len(line) > _MAX_LINE_LENGTH:
        state.failure = "codex output line exceeded bound"
        return False
    state.lines.append(line)
    state.byte_count += len(line.encode())
    if len(state.lines) > _MAX_LINES or state.byte_count > _MAX_BYTES:
        state.failure = "codex output exceeded bound"
        return False
    return True


def _capture_fresh_event(state: _CaptureState, event: StreamEvent) -> None:
    if event.event_type == "init":
        candidate = event.metadata.get("session_id")
        state.session_id = candidate if isinstance(candidate, str) else None
    if event.event_type == "result":
        state.failure = "codex completed before interruption"


def _read_fresh_message(
    messages: queue.Queue[str | None],
    proc: subprocess.Popen[str],
    deadline: float,
) -> tuple[str | None, str | None]:
    try:
        line = messages.get(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
    except queue.Empty:
        failure = (
            "codex completed before interruption" if proc.poll() is not None else None
        )
        return None, failure
    if line is None:
        return None, "codex stream ended before thread start"
    return line, None


def _consume_fresh_line(state: _CaptureState, line: str, runner: AgentRunner) -> None:
    if not _fresh_line_within_bounds(state, line):
        return
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("type") == "turn.started":
        state.turn_started = True
    with display_suppressed():
        events = runner.parse_line(line)
    for event in events:
        _capture_fresh_event(state, event)


def _reject_completed_fresh_capture(
    state: _CaptureState, proc: subprocess.Popen[str], runner: AgentRunner
) -> None:
    if state.session_id is None:
        return
    if proc.poll() is None and not getattr(runner, "turn_completed", False):
        return
    state.failure = "codex completed before interruption"
    state.session_id = None


def _capture_started_thread(
    proc: subprocess.Popen[str], runner: AgentRunner
) -> tuple[str | None, list[str], str | None, threading.Thread | None]:
    """Read bounded output until a live thread starts or the deadline expires."""
    if proc.stdout is None:
        return None, [], "codex stdout unavailable", None
    state = _CaptureState(lines=[])
    messages: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(target=_reader, args=(proc.stdout, messages), daemon=True)
    reader.start()
    deadline = time.monotonic() + _FRESH_DEADLINE_SECONDS
    needs_turn_started = runner.name == "codex"
    while time.monotonic() < deadline and (
        state.session_id is None or (needs_turn_started and not state.turn_started)
    ):
        line, failure = _read_fresh_message(messages, proc, deadline)
        if failure is not None:
            state.failure = failure
            break
        if line is None:
            continue
        _consume_fresh_line(state, line, runner)
        if state.failure is not None:
            break
    _reject_completed_fresh_capture(state, proc, runner)
    return state.session_id, state.lines, state.failure, reader


def _terminate_and_reap(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


def _require_codex_reachable(config: DispatchConfig) -> None:
    reason = codex_reachability_reason(inspect_runner(config, runner_name="codex"))
    if reason:
        raise _CanaryLifecycleError(reason)


def _initialize_resume_repo(temp_dir: Path) -> None:
    if run(["git", "init"], cwd=temp_dir, check=False).returncode != 0:
        raise _CanaryLifecycleError("git init failed")
    validate_codex_project_tree(temp_dir)


def _interrupt_fresh_codex(
    runner: CodexRunner,
    config: DispatchConfig,
    model: str,
    temp_dir: Path,
    env: dict[str, str],
) -> tuple[str, list[str]]:
    command = runner.build_command(
        prompt=_FRESH_PROMPT, config=config, env=env, model=model
    )
    proc = popen(command, cwd=temp_dir, env=env, start_new_session=True)
    reader: threading.Thread | None = None
    try:
        session_id, lines, failure, reader = _capture_started_thread(proc, runner)
    finally:
        _terminate_and_reap(proc)
        if reader is not None:
            reader.join(timeout=2)
    if failure or session_id is None:
        raise _CanaryLifecycleError(failure or "codex session missing")
    return session_id, lines


def _resume_result_checks(
    runner: CodexRunner,
    config: DispatchConfig,
    model: str,
    session_id: str,
    temp_dir: Path,
    lines: list[str],
    exit_code: int,
) -> list[CheckResult]:
    events = [event for line in lines for event in runner.parse_line(line)]
    last_output = next((line for line in reversed(lines) if line), None)
    classification = runner.classify_result(
        exit_code=exit_code, last_output=last_output, config=config
    )
    fields = reduce_run(events, classification)
    checks = canary_checks(
        fields,
        CONTRACTS["codex"],
        model=model,
        overrides=build_pricing_overrides(config.pricing.overrides),
    )
    clean = run(["git", "status", "--porcelain"], cwd=temp_dir, check=False)
    completed = classification.category == ""
    tree_clean = clean.returncode == 0 and not clean.stdout
    checks.extend(_resume_lifecycle_checks(session_id, completed, tree_clean))
    return checks


def _resume_lifecycle_checks(
    session_id: str, completed: bool, tree_clean: bool
) -> list[CheckResult]:
    return [
        CheckResult(
            "resume_session_captured",
            "pass" if session_id else "fail",
            "fresh session captured",
            {"present": bool(session_id)},
        ),
        CheckResult(
            "resume_completed",
            "pass" if completed else "fail",
            "resume completed" if completed else "resume failed",
            {"completed": completed},
        ),
        CheckResult(
            "resume_tree_clean",
            "pass" if tree_clean else "fail",
            "tree clean" if tree_clean else "tree dirty",
            {"clean": tree_clean},
        ),
    ]


def _run_resumed_codex(
    runner: CodexRunner,
    config: DispatchConfig,
    model: str,
    session_id: str,
    temp_dir: Path,
    env: dict[str, str],
) -> tuple[list[CheckResult], list[str]]:
    validate_codex_project_tree(temp_dir)
    command = runner.build_resume_command(
        session_id=session_id,
        prompt="ignored",
        config=config,
        env=env,
        model=model,
    )
    proc = popen(command, cwd=temp_dir, env=env, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=_RESUME_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_and_reap(proc)
        proc.communicate()
        raise _CanaryLifecycleError("codex resume timed out") from exc
    lines = (stdout or "").splitlines()
    checks = _resume_result_checks(
        runner, config, model, session_id, temp_dir, lines, proc.returncode or 0
    )
    return checks, lines


def _append_deletion(outcome: RunnerOutcome | None, deletion: CheckResult) -> None:
    if outcome is None:
        return
    outcome.checks.append(
        CheckResult(
            "resume_session_deleted",
            deletion.status,
            deletion.summary,
            deletion.detail,
        )
    )


def run_codex_resume_canary(
    config: DispatchConfig, model: str, *, record_dir: str | None
) -> RunnerOutcome:
    """Exercise a bounded fresh-thread interruption and official resume."""
    runner = CodexRunner()
    session_id: str | None = None
    raw_lines: list[str] = []
    temp_dir = work_dirs.make_work_dir("kew-canary-resume-")
    outcome: RunnerOutcome | None = None
    try:
        _require_codex_reachable(config)
        _initialize_resume_repo(temp_dir)
        env = codex_process_env()
        session_id, fresh_lines = _interrupt_fresh_codex(
            runner, config, model, temp_dir, env
        )
        raw_lines.extend(fresh_lines)
        _require_codex_reachable(config)
        checks, resume_lines = _run_resumed_codex(
            runner, config, model, session_id, temp_dir, env
        )
        raw_lines.extend(resume_lines)
        outcome = RunnerOutcome("codex", True, checks)
        return outcome
    except _CanaryLifecycleError as exc:
        return RunnerOutcome("codex", False, [], str(exc))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = f"codex resume lifecycle failed: {type(exc).__name__}"
        return RunnerOutcome("codex", False, [], reason)
    finally:
        _append_deletion(outcome, delete_codex_session(session_id))
        _write_resume_record(record_dir, raw_lines)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _passthrough_reachability_reason(reach: RunnerReachability) -> str | None:
    if not reach.binary_present:
        return f"{reach.runner_name} binary missing"
    if reach.runner_name == "codex" and reach.version_supported is not True:
        return "codex version unsupported or unparseable"
    if reach.credential_detected is False:
        return f"{reach.runner_name} login unavailable"
    return None


def _proxy_observable(config: DispatchConfig) -> bool:
    settings = config.inference_transport.switchyard
    try:
        probe = probe_switchyard_endpoint(
            endpoint=settings.endpoint,
            route=settings.route,
            timeout_seconds=1.5,
        )
        metrics = observe_switchyard_metrics(
            endpoint=settings.endpoint,
            timeout_seconds=1.5,
        )
    except (OSError, TimeoutError, ValueError, TypeError, SwitchyardMetricsError):
        return False
    return bool(
        probe.admitted
        and probe.endpoint == settings.endpoint
        and probe.route == settings.route
        and metrics.scope == "process_wide_proxy"
    )


def _passthrough_process_env(runner: AgentRunner) -> dict[str, str]:
    return codex_process_env()


def _interrupt_passthrough_runner(
    runner: AgentRunner,
    config: DispatchConfig,
    model: str,
    temp_dir: Path,
    env: dict[str, str],
) -> tuple[str, list[str], bool]:
    command = runner.build_command(
        prompt=_FRESH_PROMPT, config=config, env=env, model=model
    )
    proc = popen(command, cwd=temp_dir, env=env, start_new_session=True)
    reader: threading.Thread | None = None
    was_running = False
    try:
        session_id, lines, failure, reader = _capture_started_thread(proc, runner)
        was_running = proc.poll() is None
    finally:
        _terminate_and_reap(proc)
        if reader is not None:
            reader.join(timeout=2)
    if failure or session_id is None:
        raise _CanaryLifecycleError(failure or "runner session missing")
    cancellation_observed = was_running and proc.poll() is not None
    return session_id, lines, cancellation_observed


def _run_resumed_passthrough(
    runner: AgentRunner,
    config: DispatchConfig,
    model: str,
    session_id: str,
    temp_dir: Path,
    env: dict[str, str],
) -> tuple[GovernanceFields, list[str], bool]:
    command = runner.build_resume_command(
        session_id=session_id,
        prompt="ignored",
        config=config,
        env=env,
        model=model,
    )
    proc = popen(command, cwd=temp_dir, env=env, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=_RESUME_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_and_reap(proc)
        proc.communicate()
        raise _CanaryLifecycleError("runner resume timed out") from exc
    lines = (stdout or "").splitlines()
    with display_suppressed():
        events = [event for line in lines for event in runner.parse_line(line)]
    last_output = next((line for line in reversed(lines) if line), None)
    classification = runner.classify_result(
        exit_code=proc.returncode or 0,
        last_output=last_output,
        config=config,
    )
    fields = reduce_run(events, classification)
    resumed = (
        classification.category == ""
        and classification.session_id == session_id
        and fields.saw_result_event
    )
    return fields, lines, resumed


def _start_managed_switchyard(config: DispatchConfig) -> _ManagedLifecycle | None:
    settings = config.inference_transport.switchyard
    if not settings.managed:
        return None
    from kew.switchyard_supervisor import ManagedSwitchyardSupervisor

    supervisor = ManagedSwitchyardSupervisor(settings)
    supervisor.start()
    return supervisor


def _close_managed_switchyard(supervisor: _ManagedLifecycle | None) -> bool:
    if supervisor is None:
        return True
    try:
        supervisor.close()
    except (OSError, RuntimeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class _PassthroughRequest:
    record_dir: Path
    model: str


@dataclass
class _PassthroughState:
    temp_dir: Path
    raw_lines: list[str]
    session_id: str | None = None
    fields: GovernanceFields | None = None
    cancellation_observed: bool = False
    resume_completed: bool = False
    proxy_healthy: bool = False
    managed_supervisor: _ManagedLifecycle | None = None
    failure: str | None = None
    cleanup_complete: bool = False


def _prepare_passthrough_request(
    runner: AgentRunner,
    config: DispatchConfig,
    model: str,
    record_dir: str | None,
) -> _PassthroughRequest:
    if record_dir is None:
        raise _CanaryLifecycleError("Switchyard canary requires --record DIR")
    private_records = validate_canary_record_directory(record_dir)
    settings = config.inference_transport.switchyard
    if config.inference_transport.mode != "switchyard":
        raise _CanaryLifecycleError("Switchyard mode is required")
    if settings.route != "switchyard/passthrough":
        raise _CanaryLifecycleError("Switchyard passthrough route is required")
    try:
        resolved = resolve_inference_transport(config, runner, model)
    except TransportResolutionError as exc:
        raise _CanaryLifecycleError(str(exc)) from exc
    reason = _passthrough_reachability_reason(
        inspect_runner(config, runner_name=runner.name)
    )
    if reason is not None:
        raise _CanaryLifecycleError(reason)
    return _PassthroughRequest(private_records, resolved.model)


def _execute_passthrough(
    state: _PassthroughState,
    runner: AgentRunner,
    config: DispatchConfig,
    model: str,
) -> None:
    state.managed_supervisor = _start_managed_switchyard(config)
    if not _proxy_observable(config):
        raise _CanaryLifecycleError("Switchyard proxy health unavailable")
    _initialize_resume_repo(state.temp_dir)
    env = _passthrough_process_env(runner)
    state.session_id, fresh_lines, state.cancellation_observed = (
        _interrupt_passthrough_runner(runner, config, model, state.temp_dir, env)
    )
    state.raw_lines.extend(fresh_lines)
    state.fields, resume_lines, state.resume_completed = _run_resumed_passthrough(
        runner, config, model, state.session_id, state.temp_dir, env
    )
    state.raw_lines.extend(resume_lines)
    state.proxy_healthy = _proxy_observable(config)


def _cleanup_passthrough(
    state: _PassthroughState,
    runner: AgentRunner,
    record_dir: Path,
) -> None:
    deletion = (
        delete_codex_session(state.session_id)
        if runner.name == "codex"
        else CheckResult("session_deleted", "pass", "not required", {})
    )
    proxy_clean = _close_managed_switchyard(state.managed_supervisor)
    shutil.rmtree(state.temp_dir, ignore_errors=True)
    try:
        if state.raw_lines:
            write_canary_record(
                record_dir, f"{runner.name}-passthrough.jsonl", state.raw_lines
            )
        record_written = True
    except (OSError, ValueError):
        record_written = False
    state.cleanup_complete = (
        deletion.status == "pass"
        and proxy_clean
        and not state.temp_dir.exists()
        and record_written
    )


def _passthrough_outcome(
    state: _PassthroughState, runner: AgentRunner
) -> RunnerOutcome:
    if state.failure is not None or state.fields is None:
        return RunnerOutcome(
            runner.name,
            False,
            [],
            state.failure or "passthrough lifecycle incomplete",
        )
    evidence = PassthroughCanaryEvidence(
        proxy_healthy=state.proxy_healthy,
        cancellation_observed=state.cancellation_observed,
        resume_completed=state.resume_completed,
        cleanup_complete=state.cleanup_complete,
    )
    checks = passthrough_canary_checks(
        state.fields, CONTRACTS[runner.name], evidence=evidence
    )
    return RunnerOutcome(runner.name, True, checks)


def run_switchyard_passthrough_canary(
    runner: AgentRunner,
    config: DispatchConfig,
    model: str,
    *,
    record_dir: str | None,
) -> RunnerOutcome:
    """Run a human-gated passthrough lifecycle without exposing raw evidence."""
    try:
        request = _prepare_passthrough_request(runner, config, model, record_dir)
    except (CanaryRecordError, _CanaryLifecycleError) as exc:
        return RunnerOutcome(runner.name, False, [], str(exc))
    state = _PassthroughState(
        temp_dir=work_dirs.make_work_dir("kew-canary-passthrough-"),
        raw_lines=[],
    )
    try:
        _execute_passthrough(state, runner, config, request.model)
    except _CanaryLifecycleError as exc:
        state.failure = str(exc)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        state.failure = f"passthrough lifecycle failed: {type(exc).__name__}"
    finally:
        _cleanup_passthrough(state, runner, request.record_dir)
    return _passthrough_outcome(state, runner)
