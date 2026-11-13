"""Runner-agnostic subprocess lifecycle engine.

Owns: Popen, heartbeat, logging, timeout, process cleanup.
Delegates: command building, line parsing, result classification to the runner.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import IO

from kew import subprocess_utils
from kew.breaker import compaction_adjusted_cost
from kew.cancellation import DeferredSIGINT
from kew.config import DispatchConfig
from kew.credential_issuer import PROVISIONING_ENV, BoundedRunKey
from kew.dispatch_admission import PendingAdmission, execution_cancellation
from kew.exit_signal import name_signal_exit, remember_line
from kew.inference_transport import ResolvedInferenceTransport
from kew.pricing import build_pricing_overrides
from kew.progress import _error, _warn
from kew.publication import DETACHED_PUBLICATION_TOKEN_ENV
from kew.run_credentials import launch_environment, require_credential_lifetime
from kew.runner_fields import (
    REDACTED_IDENTITY,
    exception_text,
    reported_identity,
    reported_model_identity,
    require_public_model,
    sanitize_exception_args,
    service_tier_token,
)
from kew.runner_quota import (
    QUOTA_EVENT_TYPE,
    QUOTA_EXHAUSTED_CATEGORY,
    QUOTA_WARNING_EVENT_TYPE,
    QuotaRejection,
    QuotaWarning,
    is_quota_exhausted,
)
from kew.runners.protocol import AgentRunner, ResultClassification, StreamEvent
from kew.sandbox import (
    RUNTIME_CONTROL_TIMEOUT_SECONDS,
    ContainedExecution,
    ContainerHandle,
    ContainerStartFailed,
    _cpu_cap_rejection_maximum,
    _cpu_cap_retry_argv,
    _launched_container_id,
    _readline_bytes,
    capture_container_launch,
    capture_container_stream_failure,
    classify_hard_stop,
    collect_container_hard_stop_evidence,
    is_startup_ack,
    proxy_redaction_scope,
    remove_prompt_handoff,
    renew_startup_nonce,
    settle_never_started_start,
    write_prompt_handoff,
)
from kew.session_ids import capture_session_id, require_session_id
from kew.subprocess_utils import find_timeout_cmd, popen, release_spawn
from kew.switchyard_metrics import SwitchyardSelectionEvidence
from kew.switchyard_supervisor import (
    ManagedSwitchyardSupervisor,
    SidecarAttemptSupervisor,
    SwitchyardSupervisorError,
)
from kew.tracking import DispatchResult, preserve_dispatch_result, startup_refused
from kew.transcript_metrics import (
    StreamDerivation,
    _safe_token,
    sanitized_log_banner,
    write_derived_evidence,
)
from kew.usage import event_contributes_usage

_logger = logging.getLogger(__name__)


def _write_event(fh: IO[str] | None, event_type: str, **data: object) -> None:
    """Write a single event to the event file. No-op if fh is None."""
    if fh is None:
        return
    event = {"ts": time.time(), "type": event_type, **data}
    fh.write(_json.dumps(event) + "\n")
    fh.flush()


_PAUSE_POLL_SECS = 3.0


def _cost_from_accumulated(
    model: str,
    accumulated_input: int,
    accumulated_output: int,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    *,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Best-effort run cost from accumulated tokens (shared by usage + step emits)."""
    from kew.pricing import compute_cost_from_tokens

    return compute_cost_from_tokens(
        model=model,
        input_tokens=accumulated_input,
        output_tokens=accumulated_output,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        overrides=pricing_overrides,
    )


def _native_cost_usd(metadata: dict[str, object]) -> float | None:
    """A runner's native per-step cost from event metadata, or None.

    None means "no native figure reported" (claude-code) — distinct from a
    genuine $0 — so callers fall back to a token estimate (#1082). ``bool`` is
    rejected explicitly since it is an ``int`` subclass.
    """
    cost = metadata.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


def _step_event_cost(
    event: StreamEvent,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
) -> float:
    """Cost to record for a 'step' event: native per-step figure when the
    runner reports one (opencode/pi), else a per-step token estimate (#1082)."""
    native = _native_cost_usd(event.metadata)
    if native is not None:
        return native
    return _cost_from_accumulated(
        model,
        event.input_tokens,
        event.output_tokens,
        pricing_overrides,
        cache_creation_tokens=event.cache_creation_tokens,
        cache_read_tokens=event.cache_read_tokens,
    )


def _emit_stream_event(
    fh: IO[str] | None,
    event: StreamEvent,
    *,
    accumulated_input: int,
    accumulated_output: int,
    accumulated_cache_creation: int = 0,
    accumulated_cache_read: int = 0,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None = None,
    safe_line: str | None = None,
) -> None:
    """Map a StreamEvent to an event file entry.

    The event file is durable evidence, so no raw provider text reaches it
    (I06T): an ``activity`` line carries only *safe_line* — the one sanitized
    progress string the derivation already vetted for the log — and a
    ``tool_use`` carries the sanitized tool name with its raw target dropped.
    """
    if fh is None:
        return
    if event.event_type in ("init", "result"):
        return
    if event.event_type == "usage":
        cost = _cost_from_accumulated(
            model,
            accumulated_input,
            accumulated_output,
            pricing_overrides,
            cache_creation_tokens=accumulated_cache_creation,
            cache_read_tokens=accumulated_cache_read,
        )
        _write_event(
            fh,
            "tokens",
            input=accumulated_input,
            output=accumulated_output,
            cache_creation=accumulated_cache_creation,
            cache_read=accumulated_cache_read,
            cost_usd=cost,
        )
    elif event.event_type == "step":
        cost = _step_event_cost(event, model, pricing_overrides)
        _write_event(
            fh,
            "step",
            input=event.input_tokens,
            output=event.output_tokens,
            cache_creation=event.cache_creation_tokens,
            cache_read=event.cache_read_tokens,
            cost_usd=cost,
        )
    elif event.event_type == "activity":
        # Never event.message — that is the provider's own prose. Only the
        # sanitized progress line the derivation produced may be persisted.
        if safe_line:
            _write_event(fh, "activity", msg=safe_line)
    elif event.event_type == "tool_use":
        # The tool NAME is sanitized the same way the log's `tools=` list is;
        # the raw target is dropped — it is provider-controlled input that can
        # carry a secret, an absolute path, or file contents.
        _write_event(
            fh, "tool_use", tool=_safe_token(event.message, "tool_name"), target=""
        )
    elif event.event_type != "result":
        # Unknown event types written as activity (per design spec) — again the
        # sanitized progress line only, never the raw message.
        if safe_line:
            _write_event(fh, "activity", msg=safe_line)


def _setup_log_paths(
    config: DispatchConfig,
    issue_num: int,
    log_file_path: Path | None,
    emit_events: bool,
) -> tuple[Path, Path | None]:
    """Resolve log file and optional event file paths."""
    if log_file_path is not None:
        log_file = log_file_path
        log_file.parent.mkdir(parents=True, exist_ok=True)
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        log_dir = Path(config.logging.dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"dispatch-{issue_num}-{timestamp}.log"
    event_file = (
        log_file.with_suffix("").with_suffix(".events.jsonl") if emit_events else None
    )
    return log_file, event_file


# KEW_* variables are the orchestrator's OWN config (KEW_LOG_DIR, KEW_ACTOR,
# KEW_MAX_BUDGET, KEW_SERVE_TOKEN, ...). An agent subprocess — and any nested
# `kew` it invokes — must NOT inherit them: a leaked KEW_LOG_DIR once pointed a
# worktree kew at the LIVE dispatch.db and auto-migrated it to a newer schema
# mid-drive, locking out every other consumer (#688). The child env is built by
# allowlist: every KEW_* var is dropped unless it is named here (none are), and
# everything non-KEW (PATH, HOME, provider credentials, locale, ...) normally
# passes through unchanged so runners keep what they need. The one narrower
# exception is a GitHub token activated from the detached-publication handoff:
# that capability belongs to the supervisor's later push/PR phase, not the
# agent subprocess.
_KEW_ENV_ALLOWLIST: frozenset[str] = frozenset()


def _build_child_env(*, host_publishes: bool = False) -> dict[str, str]:
    """Construct the agent subprocess environment from the parent's.

    Strips every ``KEW_*`` variable except those in :data:`_KEW_ENV_ALLOWLIST`
    (currently none) so a dispatched agent cannot pick up the orchestrator's
    own config. Other variables are inherited verbatim, except host GitHub
    credentials activated specifically for detached publication, and, when
    the host publishes this run (canonical mode or a host-style runner),
    every publication credential by name: an agent that is told not to push
    must also be unable to (review round 6 of #1440).
    """
    detached_publication = bool(os.environ.get(DETACHED_PUBLICATION_TOKEN_ENV))
    host_only = {"GH_TOKEN", "GITHUB_TOKEN"} if detached_publication else set()
    if host_publishes:
        from kew.subprocess_utils import PUBLICATION_CREDENTIAL_ENVS

        host_only |= set(PUBLICATION_CREDENTIAL_ENVS)
    return {
        key: value
        for key, value in os.environ.items()
        if (not key.startswith("KEW_") or key in _KEW_ENV_ALLOWLIST)
        and key != PROVISIONING_ENV
        and key not in host_only
        # GIT_CONFIG_COUNT / GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n is how the
        # publication credential travels; the whole family goes with it.
        and not (host_publishes and key.startswith("GIT_CONFIG_"))
    }


def _preflight_runner_workspace(runner: AgentRunner, worktree_dir: Path) -> None:
    """The host lane's workspace admission, asked of the shared rule."""
    from kew.runners.registry import preflight_runner_workspace

    preflight_runner_workspace(runner.name, worktree_dir)


def _build_agent_cmd(
    runner: AgentRunner,
    prompt: str,
    config: DispatchConfig,
    env: dict[str, str],
    resume_session_id: str | None,
    model: str,
    budget_override_usd: float | None = None,
    timeout_override_minutes: int | None = None,
) -> tuple[list[str], str | None, int]:
    """Build the agent command, wrapping with timeout if available.

    Returns (cmd, timeout_cmd, timeout_seconds).
    """
    extra: dict[str, float] = {}
    if resume_session_id is not None:
        resume_session_id = require_session_id(resume_session_id, runner.name)
    if budget_override_usd is not None and runner.name == "claude-code":
        extra["budget_override_usd"] = budget_override_usd
    if resume_session_id and runner.supports_resume():
        cmd = runner.build_resume_command(
            session_id=resume_session_id,
            prompt=prompt,
            config=config,
            env=env,
            model=model,
            **extra,
        )
    else:
        cmd = runner.build_command(
            prompt=prompt, config=config, env=env, model=model, **extra
        )

    return _wrap_with_timeout(cmd, config, timeout_override_minutes)


def _wrap_with_timeout(
    cmd: list[str],
    config: DispatchConfig,
    timeout_override_minutes: int | None,
) -> tuple[list[str], str | None, int]:
    """Wrap *cmd* with ``timeout`` when one is available.

    Shared by the host and contained paths so a container gets the same wall
    clock an agent does. ``docker run`` proxies signals to the container by
    default, so the timeout reaches the agent rather than orphaning it.
    """
    timeout_minutes = (
        timeout_override_minutes
        if timeout_override_minutes is not None
        else config.agent.timeout_minutes
    )
    timeout_cmd = find_timeout_cmd()
    if timeout_cmd:
        cmd = [
            timeout_cmd,
            "--foreground",
            "-k",
            "30s",
            f"{timeout_minutes}m",
        ] + cmd
    else:
        _warn("Neither timeout nor gtimeout found — using Python-level timeout")
    return cmd, timeout_cmd, timeout_minutes * 60


def _accumulate_native_step_cost(result: DispatchResult, ev: StreamEvent) -> None:
    """Sum a 'step' event's native per-step cost into ``reported_cost_usd``.

    opencode/pi carry the provider-billed figure per step; summing them makes
    it the run's authoritative spend (``compute_cost`` prefers
    ``reported_cost_usd`` over the ~10x token-bound estimate). claude-code sends
    ``cost_usd=None`` and reports its total on the result event, so this leaves
    its ``reported_cost_usd`` untouched (#1082).
    """
    native = _native_cost_usd(ev.metadata)
    if native is not None:
        result.reported_cost_usd = (result.reported_cost_usd or 0.0) + native


def _capture_reported_service_tier(ev: StreamEvent, result: DispatchResult) -> None:
    """Validate a runner-reported tier before persistence or report serialization."""
    if "service_tier" in ev.metadata:
        result.reported_service_tier = service_tier_token(ev.metadata["service_tier"])


def _capture_stream_model(ev: StreamEvent, result: DispatchResult, model: str) -> str:
    """Separate the safe reported identity from the live accounting identity."""
    if not result.configured_model:
        result.configured_model = model
    meta_model = reported_model_identity(ev.metadata.get("model"))
    if meta_model:
        model = (
            result.configured_model if meta_model == REDACTED_IDENTITY else meta_model
        )
        result.resolved_model = meta_model
        result.resolved_model_source = "runner_reported"
    result.pricing_model = model
    return model


def _note_spending(result: DispatchResult, ev: StreamEvent) -> None:
    """A usage or step event after a terminal event proves the run went on.

    Only a line that carries a terminal event is spared (codex reports its
    last usage on ``turn.completed`` itself, before the terminal event on that
    line). Spending after it means that terminal event did not end the run, so
    the crossing is judged normally until another terminal event arrives
    (#1798).
    """
    if ev.event_type == "step" or event_contributes_usage(ev):
        result.terminal_observed = False


def _handle_stream_event(
    ev: StreamEvent,
    result: DispatchResult,
    ef_typed: IO[str] | None,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    safe_line: str | None = None,
    runner_name: str = "",
) -> str:
    """Process one stream event, returning the (possibly updated) model string."""
    model = _capture_stream_model(ev, result, model)
    _capture_reported_service_tier(ev, result)
    # Capture session_id as early as possible so it survives a mid-flight
    # SIGTERM (the final result JSON is never emitted on a kill) (#401).
    meta_session = ev.metadata.get("session_id")
    if meta_session is not None:
        capture_session_id(result, meta_session, runner_name)
    if ev.event_type == "init":
        return model
    if ev.event_type == "compaction":
        # Autocompaction inflates the next step's cache-creation cost; count it
        # so the anomaly rule can discount the overhead (#859).
        result.compaction_count += 1
        return model
    if ev.event_type == "result":
        # The runner's typed terminal event: the agent's turn is over (#1798).
        result.terminal_observed = True
        reported = ev.metadata.get("total_cost_usd")
        if isinstance(reported, (int, float)):
            result.reported_cost_usd = float(reported)
        return model
    _note_spending(result, ev)
    if ev.event_type == "step":
        _accumulate_native_step_cost(result, ev)
    if event_contributes_usage(ev):
        result.accumulate_usage(
            input_tokens=ev.input_tokens,
            output_tokens=ev.output_tokens,
            cache_creation_tokens=ev.cache_creation_tokens,
            cache_read_tokens=ev.cache_read_tokens,
            tool_uses=ev.tool_uses,
        )
    _emit_stream_event(
        ef_typed,
        ev,
        accumulated_input=result.input_tokens,
        accumulated_output=result.output_tokens,
        accumulated_cache_creation=result.cache_creation_tokens,
        accumulated_cache_read=result.cache_read_tokens,
        model=model,
        pricing_overrides=pricing_overrides,
        safe_line=safe_line,
    )
    return model


def _should_trip_budget(
    run_cost: float,
    budget_check: Callable[[float], bool] | None,
    anomaly_threshold_usd: float | None,
    result: DispatchResult,
    anomaly_compaction_allowance_usd: float = 0.0,
    *,
    max_discounted_compactions: int | None = None,
) -> bool:
    """Budget/anomaly trip decision. True when the watchdog should kill the run.

    *max_discounted_compactions* caps the compactions discounted; only the
    contained governor passes one, since its count comes from the agent's own
    stream (#1462). The host discounts every compaction it counted."""
    if budget_check is not None and budget_check(run_cost):
        _withdraw_breaker_flag(result)
        result.budget_exceeded = True
        result.error_category = "budget_exceeded"
        result.subtype = "watchdog_budget"
        result.estimated_cost_usd = run_cost
        return True

    if anomaly_threshold_usd is not None:
        # Discount autocompaction overhead so a run that legitimately compacted
        # several times isn't mistaken for a genuine cost anomaly (#859).
        adjusted = compaction_adjusted_cost(
            run_cost,
            result.compaction_count,
            anomaly_compaction_allowance_usd,
            max_compactions=max_discounted_compactions,
        )
        if adjusted > anomaly_threshold_usd:
            return _trip_cost_anomaly(run_cost, anomaly_threshold_usd, result)

    return False


def _trip_cost_anomaly(
    run_cost: float, threshold_usd: float, result: DispatchResult
) -> bool:
    """Record a cost-anomaly crossing; True when the run must be stopped.

    A crossing on the line that carries the runner's typed terminal event
    happens on a run that has finished: killing it would discard paid-for work
    (#1798). It is flagged instead and left to exit on its own, and its work
    takes the normal publication path. Any other crossing, including one on a
    line that spent after a terminal event, stops the run as before.
    """
    result.breaker_rule = "cost_anomaly"
    result.breaker_threshold_usd = threshold_usd
    result.estimated_cost_usd = run_cost
    result.breaker_after_terminal = result.terminal_observed
    if result.breaker_after_terminal:
        return False
    result.error_category = "circuit_breaker"
    result.subtype = "cost_anomaly"
    return True


def _withdraw_breaker_flag(result: DispatchResult) -> None:
    """A budget stop on a flagged run is the budget's stop, not the breaker's.

    The flag said the run had finished; a later budget trip proves it had not,
    so the flag no longer describes the run and must not name its stop.
    """
    if result.breaker_after_terminal:
        result.breaker_rule = ""
        result.breaker_threshold_usd = None
        result.breaker_after_terminal = False


def _budget_tripped(
    result: DispatchResult,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    budget_check: Callable[[float], bool] | None,
    proc: subprocess.Popen[str],
    *,
    anomaly_threshold_usd: float | None = None,
    anomaly_compaction_allowance_usd: float = 0.0,
) -> bool:
    """Return True (and mutate result) if the watchdog fires; False otherwise.

    Two ceilings, checked each poll while the process is still running: the
    caller-provided ``budget_check`` (the existing max_budget_usd watchdog) and
    the circuit breaker's ``anomaly_threshold_usd`` (3x the (model, complexity)
    median, #245). The anomaly ceiling is compaction-adjusted (#859).
    """
    if proc.poll() is not None:
        return False
    if budget_check is None and anomaly_threshold_usd is None:
        return False
    run_cost = _cost_from_accumulated(
        model,
        result.input_tokens,
        result.output_tokens,
        pricing_overrides,
        cache_creation_tokens=result.cache_creation_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )
    return _should_trip_budget(
        run_cost,
        budget_check,
        anomaly_threshold_usd,
        result,
        anomaly_compaction_allowance_usd,
    )


def _check_pause(should_pause: Callable[[], bool]) -> bool:
    """Invoke should_pause, swallowing any exception (flaky DB read -> not-paused)."""
    with contextlib.suppress(Exception):
        return bool(should_pause())
    return False


def _poll_pause(
    should_pause: Callable[[], bool] | None, last_check: float, now: float
) -> tuple[bool, float]:
    """Throttled pause check: at most one real should_pause() call per
    _PAUSE_POLL_SECS. Returns (paused, updated_last_check)."""
    if should_pause is None or now - last_check < _PAUSE_POLL_SECS:
        return False, last_check
    return _check_pause(should_pause), now


def _terminate_group(
    proc: subprocess.Popen[str],
    container_handle: ContainerHandle | None = None,
    *,
    grace_seconds: int = 10,
) -> None:
    """Gracefully stop the agent's whole process GROUP, then SIGKILL on grace timeout.

    For sandboxed runs, delegates to container_handle.kill() (docker kill).
    For host runs: signals the session GROUP (start_new_session=True) to ensure no
    child keeps mutating the worktree after a pause; falls back to leader-only kill.

    ``grace_seconds`` is the wrap-up window between SIGTERM and SIGKILL. A budget
    watchdog kill passes a longer window so the agent can commit WIP before the
    hard kill — a graceful, non-destructive budget stop (#859).
    """
    if container_handle is not None:
        container_handle.kill()  # removed after the client exits (the sweep)
        return
    pgid: int | None
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        proc.terminate()
        pgid = None
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if pgid is not None:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()


#: Runner notices about the provider's usage window, never a run's terminal line.
_QUOTA_NOTICE_TYPES = frozenset({QUOTA_EVENT_TYPE, QUOTA_WARNING_EVENT_TYPE})


def _process_line(
    line: str,
    lf: IO[str],
    ef_typed: IO[str] | None,
    runner: AgentRunner,
    result: DispatchResult,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    last_meaningful_line: str | None,
    derivation: StreamDerivation,
) -> tuple[str | None, str]:
    """Fold a raw provider line into evidence, then discard it (I06T).

    The raw line is never written anywhere: *derivation* extracts the
    structured facts (first edit, tool use, terminal type, the bounded Dispatch
    Report) and hands back the one sanitized progress line the operational log
    may keep. Whatever the provider echoed back — its private reasoning, the
    source it quoted, a credential, an absolute path — leaves this function
    only as a step/tool counter.

    *last_meaningful_line* stays the RAW line, in memory only: the runner's
    ``classify_result`` reads the terminal JSON to decide a stop reason, and
    that classification happens before this function's caller returns. It is
    never persisted.

    Returns (last_meaningful_line, model).
    """
    if is_startup_ack(line):
        return last_meaningful_line, model
    safe_line = derivation.observe(line)
    if safe_line is not None:
        lf.write(safe_line + "\n")
        lf.flush()
    try:
        stream_events = runner.parse_line(line)
    except Exception:  # noqa: BLE001 - untrusted agent output, never fatal
        # The contained path ignores an unparsable line; the host path must
        # not let one malformed protocol field end a live run (governed
        # review round 2, 2026-09-21).
        _logger.warning("host telemetry: unparsable stream line ignored")
        stream_events = []
    if safe_line is None and not stream_events:
        # Not protocol: a runtime's own stderr, merged into this pipe. Kept
        # redacted and bounded so a signal exit can say what preceded it.
        remember_line(result.diagnostic_tail, line)
    # A quota notice is not the terminal line: claude-code can emit it after
    # the result event, and classify_result must still read that result.
    if any(ev.event_type not in _QUOTA_NOTICE_TYPES for ev in stream_events):
        last_meaningful_line = line.strip()
    for ev in stream_events:
        derivation.record_identity(ev.metadata)
        derivation.record_message(ev.event_type, ev.message, ev.metadata)
        model = _handle_stream_event(
            ev,
            result,
            ef_typed,
            model,
            pricing_overrides,
            safe_line=safe_line,
            runner_name=runner.name,
        )
    return last_meaningful_line, model


def _latest_cpu_cap_rejection_maximum(
    current: Decimal | None, line: str
) -> Decimal | None:
    """Retain the latest validated CPU maximum from streamed output."""
    maximum = _cpu_cap_rejection_maximum(line)
    return current if maximum is None else maximum


def _no_launch_observer(line: str = "") -> None:
    """Native streams already recorded their workload at process startup."""


def _stream_output(
    proc: subprocess.Popen[str],
    lf: IO[str],
    ef_typed: IO[str] | None,
    runner: AgentRunner,
    result: DispatchResult,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    derivation: StreamDerivation,
    should_pause: Callable[[], bool] | None = None,
    budget_check: Callable[[float], bool] | None = None,
    _now: Callable[[], float] = time.monotonic,
    anomaly_threshold_usd: float | None = None,
    anomaly_compaction_allowance_usd: float = 0.0,
    observe_launch: Callable[[str], None] = _no_launch_observer,
) -> tuple[str | None, str, Decimal | None]:
    """Read process stdout via selector, updating result and event file.

    Returns (last_meaningful_line, resolved_model, rejected CPU maximum).
    """
    last_meaningful_line: str | None = None
    cpu_cap_rejection_maximum: Decimal | None = None
    if proc.stdout is None:
        return None, model, cpu_cap_rejection_maximum
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    poll_timeout = _PAUSE_POLL_SECS if should_pause is not None else 60
    last_check = _now() - _PAUSE_POLL_SECS  # let the FIRST check fire immediately
    try:
        while True:
            ready = sel.select(timeout=poll_timeout)
            observe_launch("")
            # Natural completion wins over a coincident pause: if the process has
            # already exited, take the completion path before honoring a pause.
            if not ready and proc.poll() is not None:
                break
            paused, last_check = _poll_pause(should_pause, last_check, _now())
            if paused:
                result.paused = True
                break
            if not ready:
                continue
            # Bounded: the agent (contained or not) chooses how many bytes it
            # writes before a newline, and an unbounded readline turns that
            # choice into unbounded HOST memory (#1431).
            line = _readline_bytes(proc.stdout)
            if not line:
                break
            observe_launch(line)
            cpu_cap_rejection_maximum = _latest_cpu_cap_rejection_maximum(
                cpu_cap_rejection_maximum, line
            )
            last_meaningful_line, model = _process_line(
                line,
                lf,
                ef_typed,
                runner,
                result,
                model,
                pricing_overrides,
                last_meaningful_line,
                derivation,
            )
            if _budget_tripped(
                result,
                model,
                pricing_overrides,
                budget_check,
                proc,
                anomaly_threshold_usd=anomaly_threshold_usd,
                anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
            ):
                break
    finally:
        sel.unregister(proc.stdout)
        sel.close()
    return last_meaningful_line, model, cpu_cap_rejection_maximum


class _TimeoutEnforcement:
    """Own one timeout mechanism and its authoritative enforced window."""

    def __init__(
        self,
        timeout_cmd: str | None,
        timeout_seconds: int,
        *,
        now: Callable[[], float] | None = None,
        gate: bool = False,
    ) -> None:
        self._timeout_cmd = timeout_cmd
        self._timeout_seconds = timeout_seconds
        self._now = now or time.monotonic
        self._gate = gate
        self._started_at: float | None = None
        self._launched_at: float | None = None

    def launch(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        credential: BoundedRunKey | None = None,
    ) -> subprocess.Popen[str]:
        """Launch the wrapper and record when its enforcement can begin."""
        proc = popen(
            cmd,
            cwd=cwd,
            env=env,
            start_new_session=True,
            credential=credential,
            gate=self._gate,
        )
        self._launched_at = self._now()
        if self._timeout_cmd is not None:
            self._started_at = self._launched_at
        return proc

    def relaunch(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        credential: BoundedRunKey | None = None,
    ) -> subprocess.Popen[str]:
        """Launch the CPU-cap retry inside the window the first launch opened.

        Neither timestamp moves: the operator's timeout is one wall-clock
        budget per run, and a refusal near its end must not buy a second one.
        """
        return popen(
            cmd,
            cwd=cwd,
            env=env,
            start_new_session=True,
            credential=credential,
            gate=self._gate,
        )

    def remaining(self) -> float:
        """Seconds of the run's wall-clock budget still unspent."""
        if self._launched_at is None:
            return float(self._timeout_seconds)
        return max(0.0, self._timeout_seconds - (self._now() - self._launched_at))

    def wait(self, proc: subprocess.Popen[str]) -> int | None:
        """Wait for exit and enforce the Python fallback when required."""
        if self._timeout_cmd:
            proc.wait()
            return None
        if self._started_at is None:
            self._started_at = self._now()
        try:
            proc.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return 124
        return None

    def elapsed(self) -> float | None:
        """Return elapsed enforcement time, or None before enforcement."""
        if self._started_at is None:
            return None
        return max(0.0, self._now() - self._started_at)


def _wait_for_process(
    proc: subprocess.Popen[str],
    timeout_cmd: str | None,
    timeout_seconds: int,
) -> int | None:
    """Compatibility wrapper around one Python or command timeout owner."""
    return _TimeoutEnforcement(timeout_cmd, timeout_seconds).wait(proc)


def _cleanup_process(
    proc: subprocess.Popen[str],
    stop_heartbeat: threading.Event,
    heartbeat_thread: threading.Thread,
) -> None:
    """Ensure process is terminated and heartbeat thread joined."""
    stop_heartbeat.set()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if heartbeat_thread.ident is not None:
        heartbeat_thread.join(timeout=5)


def _write_finished_event(
    ef_typed: IO[str] | None,
    result: DispatchResult,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Write the 'finished' event with final cost to the event file."""
    if ef_typed is None:
        return

    if result.reported_cost_usd is not None:
        final_cost = result.reported_cost_usd
    else:
        from kew.pricing import compute_cost_from_tokens

        final_cost = compute_cost_from_tokens(
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
            overrides=pricing_overrides,
        )
    _write_event(
        ef_typed,
        "finished",
        exit_code=result.exit_code,
        total_tokens=result.total_tokens,
        cost_usd=final_cost,
    )


def _terminate_after_stream(
    proc: subprocess.Popen[str],
    result: DispatchResult,
    *,
    container_handle: ContainerHandle | None,
    budget_stop_grace_seconds: int,
) -> None:
    """Terminate a still-running agent after a pause/budget/breaker break.

    A budget watchdog kill gets a longer wrap-up grace so the agent can commit
    WIP before SIGKILL — a graceful, non-destructive budget stop (#859).
    Pauses and anomaly kills keep the default terminate grace.
    """
    if proc.poll() is not None:
        return
    grace = budget_stop_grace_seconds if result.budget_exceeded else 10
    # SIGTERM the session group, SIGKILL on grace timeout.
    _terminate_group(proc, container_handle=container_handle, grace_seconds=grace)


def _heartbeat(stop: threading.Event) -> None:
    """Emit periodic heartbeat messages to stderr."""
    elapsed = 0
    while not stop.wait(timeout=60):
        elapsed += 1
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        msg = f"[heartbeat] {ts} — still running ({elapsed}m elapsed)\n"
        sys.stderr.write(msg)
        sys.stderr.flush()


def _kill_handle(
    proc: subprocess.Popen[str],
    container_handle: ContainerHandle | None,
    contained: ContainedExecution | None,
    *,
    event_window_start: float | None = None,
) -> ContainerHandle | None:
    """How a watchdog must stop this run: by container name, or not at all.

    A contained run's *proc* is the local runtime client, not the agent —
    terminating it leaves the container up — so it gets a handle that issues
    ``docker kill <name>``. A caller-supplied handle wins (it already knows its
    own container); a host run gets None and is stopped by process group.
    """
    if container_handle is not None:
        return container_handle
    return (
        contained.handle_for(proc, event_window_start=event_window_start)
        if contained is not None
        else None
    )


def _contained_cpu_retry_command(
    proc: subprocess.Popen[str],
    cmd: list[str],
    contained: ContainedExecution | None,
    cpu_cap_rejection_maximum: Decimal | None,
    *,
    remaining_seconds: float,
) -> tuple[list[str], list[str], ContainedExecution] | None:
    """Validate the one-CPU retry after an exited contained launch was refused.

    Returns the wrapper prefix, the retry argv and its execution — NOT an
    assembled command: validation here (waiting for the runtime, the absence
    check, the CPU re-probe) spends real time, so the caller rebuilds the
    prefix from the remainder it measures immediately before relaunch. No time
    left going in means no retry at all.
    """
    if contained is None or cpu_cap_rejection_maximum is None:
        return None
    if contained.prepared:
        return None
    if remaining_seconds <= 0:
        _warn("Sandbox: CPU-cap retry skipped because the run's timeout is spent")
        return None
    try:
        returncode = proc.wait(timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _warn("Sandbox: CPU-cap retry skipped because the runtime did not exit")
        return None
    original_argv = list(contained.argv)
    if len(cmd) < len(original_argv) or cmd[-len(original_argv) :] != original_argv:
        return None
    retry_argv = _cpu_cap_retry_argv(
        original_argv, returncode, cpu_cap_rejection_maximum
    )
    if retry_argv is None:
        return None
    retry_argv = renew_startup_nonce(retry_argv)
    prefix = cmd[: -len(original_argv)]
    return prefix, retry_argv, ContainedExecution.from_argv(retry_argv)


def _cpu_retry_launch_plan(
    retry: tuple[list[str], list[str], ContainedExecution],
    enforcement: _TimeoutEnforcement,
) -> tuple[list[str], ContainedExecution, float] | None:
    """The relaunch command, bounded by the time left NOW, or None when spent.

    Measured after retry validation: the runtime wait, the absence check and
    the CPU re-probe spend real time, and the relaunch must fit what is left
    at this instant, not what was left when validation began.
    """
    remaining = enforcement.remaining()
    if remaining <= 0:
        _warn("Sandbox: CPU-cap retry skipped because the run's timeout is spent")
        return None
    prefix, retry_argv, contained = retry
    cmd = [*_timeout_prefix_with_remaining(prefix, remaining), *retry_argv]
    return cmd, contained, remaining


def _timeout_prefix_with_remaining(prefix: list[str], remaining: float) -> list[str]:
    """The ``timeout --foreground -k 30s <duration>`` prefix, re-bounded."""
    if len(prefix) == 5 and prefix[1:4] == ["--foreground", "-k", "30s"]:
        return [*prefix[:4], f"{math.ceil(remaining)}s"]
    return list(prefix)


def _kill_unreaped_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL a spawned group whose leader this launch has not reaped yet.

    A fleet stop that raced the spawn may already have killed the group: macOS
    then reports EPERM for a group whose only member is a zombie, which is not
    a permission failure. Only a still-running leader re-raises it.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        _warn(f"Group {proc.pid} was already reaped before the registration cleanup")
    except PermissionError:
        if proc.poll() is None:
            raise
        _warn(f"Group {proc.pid} already exited before the registration cleanup")


def _announce_spawn(
    proc: subprocess.Popen[str],
    ef_typed: IO[str] | None,
    on_spawn: Callable[[int], Callable[[], None] | None] | None,
) -> None:
    """Register the waiting group before allowing the command to execute."""
    from kew.process_groups import (
        HostIdentityUnavailable,
        process_start_time,
        process_start_token,
    )

    try:
        if ef_typed is not None:
            _write_event(
                ef_typed,
                "pid",
                pid=proc.pid,
                owner_pid=os.getpid(),
                start_time=process_start_time(proc.pid),
                start_token=process_start_token(proc.pid),
            )
        before_release = on_spawn(proc.pid) if on_spawn is not None else None
        release_spawn(proc, registered=True, before_release=before_release)
    except Exception as exc:
        # This unreaped child belongs to this launch. Its PID cannot be reused.
        # SIGKILL also stops descendants from callers without a launch gate.
        _warn(f"Spawn registration failed for group {proc.pid}: {exc}; terminating")
        try:
            if proc.returncode is None:
                _kill_unreaped_group(proc)
            proc.wait()
        finally:
            release_spawn(proc, registered=False)
        _write_event(
            ef_typed, "spawn_registration_failed", pid=proc.pid, reason=str(exc)
        )
        if isinstance(exc, HostIdentityUnavailable):
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            raise


def _kill_container_after_timeout(
    container_handle: ContainerHandle | None,
    timeout_exit: int | None,
    returncode: int | None,
) -> None:
    """Make the wall-clock kill reach the CONTAINER, not just the client (#1431).

    Both timeout paths stop the local ``docker run`` process and neither stops
    what it launched: ``timeout`` signals the CLI (whose signal proxying a
    container's PID 1 is free to ignore), and the Python-level path calls
    ``proc.terminate()`` on the CLI. Either way the timed-out agent kept its
    cores, its memory and its write access to the checkout, with nothing left
    watching it. ``docker kill`` by name is the only stop that lands.

    Exit 124 is ``timeout``'s own convention and the code the Python-level path
    reports, so it identifies a timeout from either path. Killing an already
    finished container is a no-op ``check=False`` run.
    """
    if container_handle is None:
        return
    if _is_timeout_exit(timeout_exit) or _is_timeout_exit(returncode):
        container_handle.kill()


def _is_timeout_exit(exit_code: int | None) -> bool:
    """Return whether an exit code denotes a soft or hard timeout."""
    return exit_code in (124, 137)


def _record_resource_cap_event(
    container_handle: ContainerHandle | None,
    lf: IO[str],
    derivation: StreamDerivation,
    result: DispatchResult,
) -> None:
    """Record a host cap as evidence without sending it to a runner parser."""
    if container_handle is None:
        return
    cap_line = container_handle.resource_cap_line()
    if not isinstance(cap_line, str):
        return
    result.resource_cap_reason = container_handle.resource_cap_reason or ""
    safe_line = derivation.observe(cap_line)
    if safe_line is not None:
        lf.write(safe_line + "\n")


def _record_container_hard_stop(
    container_handle: ContainerHandle | None,
    lf: IO[str],
    derivation: StreamDerivation,
    result: DispatchResult,
    returncode: int | None,
    timeout_seconds: float,
) -> None:
    """Retain every daemon and watchdog observation for a container exit."""
    if container_handle is None:
        return
    separated_stderr = getattr(container_handle, "diagnostic_lines", None)
    for line in separated_stderr if isinstance(separated_stderr, list) else ():
        remember_line(result.diagnostic_tail, line)
    probe = collect_container_hard_stop_evidence(
        container_handle,
        result,
        returncode=returncode,
        timeout_seconds=timeout_seconds,
    )
    derivation.record_container_evidence(
        oom_probe_status=result.oom_probe_status or None,
        oom_probe_diagnostic=result.oom_probe_diagnostic or None,
        resource_cap_observations=result.resource_cap_observations,
        resource_cap_observations_truncated=(
            result.resource_cap_observations_truncated
        ),
    )
    if probe is not None and probe.status == "seen":
        _record_resource_cap_event(container_handle, lf, derivation, result)


def _finish_process_after_stream(
    proc: subprocess.Popen[str],
    result: DispatchResult,
    container_handle: ContainerHandle | None,
    timeout_enforcement: _TimeoutEnforcement,
    budget_stop_grace_seconds: int,
) -> None:
    """Apply the post-stream stop or wait policy."""
    if result.paused or result.budget_exceeded or result.breaker_stopped:
        _terminate_after_stream(
            proc,
            result,
            container_handle=container_handle,
            budget_stop_grace_seconds=budget_stop_grace_seconds,
        )
        return
    timeout_exit = timeout_enforcement.wait(proc)
    if timeout_exit is not None:
        result.exit_code = timeout_exit
    _kill_container_after_timeout(container_handle, timeout_exit, proc.returncode)


def _sweep_stale_container(contained: ContainedExecution | None) -> None:
    """Free what this launch's own cidfile records before `docker run`.

    The post-stop sweep can miss: a client that dies before the daemon
    answers leaves a container the next `docker run` would collide with. Only
    the id this launch recorded is removed (#1963): ``kew-<item>`` is
    host-wide, and on a loaded host the container under it may be another
    launch's. With no recorded id nothing is removed, and the reason is logged.
    """
    if contained is None:
        return
    if contained.prepared:
        return
    container_id = _launched_container_id(list(contained.argv))
    if container_id is None:
        _logger.info(
            "pre-launch sweep of %s: no container id recorded; nothing removed",
            contained.name,
        )
        return
    try:
        result = subprocess_utils.run(
            [Path(contained.runtime).name, "rm", "-f", container_id],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn(
            f"pre-launch removal of container {contained.name} failed: "
            f"{exception_text(exc)}"
        )
        return
    if getattr(result, "returncode", 0) != 0:
        # rc 1 = "removal already in progress" or a daemon hiccup; the launch
        # may then fail 125 on the name, so the reason must be on record.
        _warn(
            f"pre-launch removal of container {contained.name} returned "
            f"{result.returncode}: {(getattr(result, 'stderr', '') or '').strip()}"
        )


def _remove_container_after_stop(
    container_handle: ContainerHandle | None,
    result: DispatchResult,
    returncode: int | None,
) -> None:
    """Once the client is gone, remove what an abnormal stop may have left.

    A timeout during a slow `docker create` leaves the container in Created
    under its per-item name, and the next attempt's `docker run --name` fails
    125. Only abnormal stops are swept: a normal exit was removed by --rm.
    The removal names only the id the launch recorded, never the name (#1963).
    """
    if container_handle is None:
        return
    abnormal = (
        _is_timeout_exit(returncode)
        or _is_timeout_exit(result.exit_code)
        or result.paused
        or result.budget_exceeded
        or result.breaker_stopped
        or container_handle.resource_cap_reason is not None
    )
    if abnormal:
        container_handle.remove()


def _stop_container_watchdog(handle: ContainerHandle | None) -> None:
    """Stop the attempt watchdog before replacing or releasing its handle."""
    if handle is not None:
        handle.stop_watchdog()


@dataclass
class _HostStartupObservation:
    """Separate host wrapper ownership from evidence that its agent executed."""

    result: DispatchResult
    admission: PendingAdmission | None
    timeout_cmd: str | None
    output_observed: bool = False

    def observe_workload_output(self, line: str) -> None:
        """Latch agent output before parsing or logging can fail."""
        if not line.strip() or self._wrapper_diagnostic(line):
            return
        self.output_observed = True
        if self.admission is not None:
            self.admission.record_startup(settled=True)

    def _wrapper_diagnostic(self, line: str) -> bool:
        """Timeout's own stderr does not establish agent execution."""
        if self.timeout_cmd is None:
            return False
        prefix, separator, _ = line.partition(":")
        name = Path(self.timeout_cmd).name
        return (bool(separator) and Path(prefix).name == name) or (
            line.startswith("Try '") and f"{name} --help'" in line
        )

    def finish(self, returncode: int) -> None:
        """Settle only after draining output and waiting for process exit."""
        if self.output_observed:
            return
        if self.timeout_cmd is not None and returncode in (125, 126, 127):
            self.result.launched = False
            self.result.error_category = "sandbox_startup_refused"
            self.result.stop_reason = (
                f"Host timeout wrapper failed before agent startup (exit {returncode})"
            )
            if self.admission is not None:
                self.admission.record_startup(refused=True, settled=True)
        elif _host_signal_exit(returncode, wrapped=self.timeout_cmd is not None):
            self.result.launched = None
            self.result.error_category = "sandbox_startup_unknown"
            self.result.stop_reason = (
                f"Host process died before agent output (exit {returncode}); "
                "agent startup is unknown"
            )
        elif self.admission is not None:
            self.admission.record_startup(settled=True)


def _host_signal_exit(returncode: int, *, wrapped: bool) -> bool:
    """Popen reports direct signals negatively; timeout can encode them as 128+n."""
    return returncode < 0 or (wrapped and 128 < returncode < 128 + signal.NSIG)


def _accept_process_ownership(
    admission: PendingAdmission | None,
    handle: ContainerHandle | None,
    result: DispatchResult,
) -> None:
    """Transfer cleanup custody; host Popen supplies only provisional startup."""
    if handle is None:
        result.launched = True
    if admission is None:
        return
    if handle is None:
        admission.take_ownership()
        admission.record_startup()
    else:
        handle.accept_admission(admission)


def _observe_admitted_workload(
    handle: ContainerHandle | None,
    result: DispatchResult,
    admission: PendingAdmission | None,
    line: str = "",
    *,
    host_startup: _HostStartupObservation | None = None,
) -> None:
    with DeferredSIGINT():
        if handle is not None:
            _accept_process_ownership(admission, handle, result)
            if line:
                handle.observe_workload_output(line)
        elif host_startup is not None:
            host_startup.observe_workload_output(line)
        capture_container_launch(handle, result)
        if admission is not None:
            admission.persist_receipt()


def _cleanup_owned_process(
    proc: subprocess.Popen[str],
    stop_heartbeat: threading.Event,
    heartbeat_thread: threading.Thread,
    handle: ContainerHandle | None,
    result: DispatchResult,
) -> None:
    _stop_container_watchdog(handle)
    _cleanup_process(proc, stop_heartbeat, heartbeat_thread)
    _remove_container_after_stop(handle, result, proc.returncode)
    if handle is not None:
        settle_never_started_start(handle, result)
        handle.cleanup_identity()


def _raise_unspawned_start(
    contained: ContainedExecution | None, result: DispatchResult, exc: BaseException
) -> None:
    """A ``start`` client that could not be spawned left its container (#2973).

    Nothing owns that ``created`` container once the launch unwinds, so it is
    removed by its own id here and the failure is named for the container.
    """
    if contained is None or not isinstance(exc, Exception):
        return
    try:
        removed = contained.remove_if_never_started()
    except Exception as cleanup_exc:
        _warn(
            "Sandbox: never-started container removal unconfirmed: "
            f"{exception_text(cleanup_exc)}"
        )
        return
    if not removed:
        return
    failure = ContainerStartFailed(
        contained.name, f"start could not be spawned: {exception_text(exc)}"
    )
    result.launched = False
    result.error_category = failure.category
    result.stop_reason = str(failure)
    raise failure from exc


def _capture_process_failure(
    proc: subprocess.Popen[str] | None,
    handle: ContainerHandle | None,
    result: DispatchResult,
    admission: PendingAdmission | None,
    exc: BaseException,
    contained: ContainedExecution | None = None,
) -> None:
    if proc is None:
        _raise_unspawned_start(contained, result, exc)
    else:
        from kew.process_groups import HostIdentityUnavailable

        if isinstance(exc, HostIdentityUnavailable):
            result.launched = False
            result.accounting_status = "not-run"
            if admission is not None:
                admission.record_startup(refused=True, settled=True)
        _observe_admitted_workload(handle, result, admission)
        if isinstance(exc, Exception):
            capture_container_stream_failure(handle, result, exc)
        elif handle is not None:
            handle.cancel_launch()


def _cleanup_possible_process(
    proc: subprocess.Popen[str] | None,
    stop_heartbeat: threading.Event,
    heartbeat_thread: threading.Thread,
    handle: ContainerHandle | None,
    result: DispatchResult,
    admission: PendingAdmission | None,
) -> None:
    if proc is not None:
        with DeferredSIGINT():
            try:
                _observe_admitted_workload(handle, result, admission)
            finally:
                _cleanup_owned_process(
                    proc, stop_heartbeat, heartbeat_thread, handle, result
                )


def _run_process(
    cmd: list[str],
    worktree_dir: Path,
    env: dict[str, str],
    log_file: Path,
    ef_typed: IO[str] | None,
    runner: AgentRunner,
    result: DispatchResult,
    model: str,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    timeout_cmd: str | None,
    timeout_seconds: int,
    derivation: StreamDerivation,
    on_spawn: Callable[[int], Callable[[], None] | None] | None = None,
    should_pause: Callable[[], bool] | None = None,
    budget_check: Callable[[float], bool] | None = None,
    anomaly_threshold_usd: float | None = None,
    anomaly_compaction_allowance_usd: float = 0.0,
    budget_stop_grace_seconds: int = 30,
    _now: Callable[[], float] = time.monotonic,
    container_handle: ContainerHandle | None = None,
    contained: ContainedExecution | None = None,
    admission: PendingAdmission | None = None,
    credential: BoundedRunKey | None = None,
) -> tuple[str | None, str, int]:
    """Launch the agent subprocess, stream its output, and wait for exit.

    Returns (last_meaningful_line, resolved_model, returncode).

    When *contained* is set, *cmd* is a container-run argv and the process
    launched here is the local runtime client, not the agent. Killing that
    client would leave the container running, so the kill handle is built from
    it and every watchdog terminates by container name instead (#1462).
    """
    stop_heartbeat = threading.Event()
    with open(log_file, "a") as lf:
        if lf.tell() > 0:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            lf.write(f"\n=== retry at {ts} ===\n")
        else:
            lf.write(sanitized_log_banner())
        lf.flush()
        _preflight_runner_workspace(runner, worktree_dir)
        _sweep_stale_container(contained)
        timeout_enforcement = _TimeoutEnforcement(
            timeout_cmd, timeout_seconds, gate=on_spawn is not None
        )
        host_startup = _HostStartupObservation(result, admission, timeout_cmd)
        event_window_start = time.time()
        require_credential_lifetime(credential, timeout_seconds)
        proc = None
        heartbeat_thread = threading.Thread(
            target=_heartbeat, args=(stop_heartbeat,), daemon=True
        )
        try:
            with DeferredSIGINT():
                proc = timeout_enforcement.launch(
                    cmd,
                    cwd=str(worktree_dir),
                    env=env,
                    credential=credential,
                )
                container_handle = _kill_handle(
                    proc,
                    container_handle,
                    contained,
                    event_window_start=event_window_start,
                )
                _accept_process_ownership(admission, container_handle, result)
            _announce_spawn(proc, ef_typed, on_spawn)
            if container_handle is not None:
                container_handle.start_watchdog(wall_clock_seconds=timeout_seconds)
            observe_launch = partial(
                _observe_admitted_workload,
                container_handle,
                result,
                admission,
                host_startup=host_startup,
            )
            observe_launch()
            heartbeat_thread.start()
            last_meaningful_line, model, cpu_cap_rejection_maximum = _stream_output(
                proc,
                lf,
                ef_typed,
                runner,
                result,
                model,
                pricing_overrides,
                derivation,
                observe_launch=observe_launch,
                should_pause=should_pause,
                budget_check=budget_check,
                _now=_now,
                anomaly_threshold_usd=anomaly_threshold_usd,
                anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
            )
            retry = _contained_cpu_retry_command(
                proc,
                cmd,
                contained,
                cpu_cap_rejection_maximum,
                remaining_seconds=timeout_enforcement.remaining(),
            )
            launch_plan = (
                _cpu_retry_launch_plan(retry, timeout_enforcement)
                if retry is not None
                else None
            )
            if launch_plan is not None:
                _stop_container_watchdog(container_handle)
                cmd, contained, remaining_seconds = launch_plan
                event_window_start = time.time()
                with DeferredSIGINT():
                    proc = timeout_enforcement.relaunch(
                        cmd,
                        cwd=str(worktree_dir),
                        env=env,
                        credential=credential,
                    )
                    container_handle = contained.handle_for(
                        proc, event_window_start=event_window_start
                    )
                    _accept_process_ownership(admission, container_handle, result)
                container_handle.start_watchdog(wall_clock_seconds=remaining_seconds)
                _announce_spawn(proc, ef_typed, on_spawn)
                observe_launch = partial(
                    _observe_admitted_workload,
                    container_handle,
                    result,
                    admission,
                )
                observe_launch()
                last_meaningful_line, model, _ = _stream_output(
                    proc,
                    lf,
                    ef_typed,
                    runner,
                    result,
                    model,
                    pricing_overrides,
                    derivation,
                    observe_launch=observe_launch,
                    should_pause=should_pause,
                    budget_check=budget_check,
                    _now=_now,
                    anomaly_threshold_usd=anomaly_threshold_usd,
                    anomaly_compaction_allowance_usd=(anomaly_compaction_allowance_usd),
                )
            _record_resource_cap_event(container_handle, lf, derivation, result)
            _finish_process_after_stream(
                proc,
                result,
                container_handle,
                timeout_enforcement,
                budget_stop_grace_seconds,
            )
            if container_handle is not None:
                container_handle.finish_startup_observation()
            else:
                host_startup.finish(124 if result.exit_code == 124 else proc.returncode)
            # Capture execution time before the OOM event query. A successful
            # empty query can wait for its future --until bound.
            timeout_duration = timeout_enforcement.elapsed()
            result.duration_seconds = timeout_duration or 0.0
            if container_handle is None:
                result.enforced_duration_seconds = timeout_duration
                result.enforced_timeout_seconds = timeout_seconds
            _record_container_hard_stop(
                container_handle,
                lf,
                derivation,
                result,
                proc.returncode,
                timeout_seconds,
            )
        except BaseException as exc:
            _capture_process_failure(
                proc, container_handle, result, admission, exc, contained
            )
            raise
        finally:
            _cleanup_possible_process(
                proc,
                stop_heartbeat,
                heartbeat_thread,
                container_handle,
                result,
                admission,
            )

    return last_meaningful_line, model, proc.returncode


def _get_pricing_overrides(
    config: DispatchConfig,
) -> dict[str, tuple[float, float]] | None:
    """Build pricing overrides from config, if available."""
    if hasattr(config, "pricing"):
        return build_pricing_overrides(config.pricing.overrides)
    return None


def _apply_classification(
    result: DispatchResult, classification: ResultClassification, runner_name: str = ""
) -> None:
    """Fold the runner's post-hoc classification into the result.

    A circuit-breaker kill (cost_anomaly) or a budget watchdog kill has already
    classified the run; don't let classify_result overwrite that verdict (#245).
    """
    if startup_refused(result) or result.error_category == "sandbox_startup_unknown":
        return
    if not result.budget_exceeded and not result.breaker_stopped:
        # Exact vocabulary entries remain internal kinds for retries, records
        # and exit mapping. Free-text displays redact them at their boundary.
        result.error_category = reported_identity(
            classification.category, "error_category"
        )
        result.subtype = reported_identity(classification.subtype, "subtype")
    # Prefer the classification's session_id (from the terminal result JSON)
    # but keep the stream-captured value when the classification has none
    # (e.g. mid-flight SIGTERM before the result event) (#401).
    capture_session_id(result, classification.session_id, runner_name)
    result.stop_reason = reported_identity(classification.stop_reason, "stop_reason")


def _classify_transport_health(
    classification: ResultClassification,
    inference_transport: ResolvedInferenceTransport | None,
    health_check: Callable[[], bool] | None,
) -> ResultClassification:
    """Prefer a distinct fail-closed verdict when an admitted proxy is lost."""
    if (
        inference_transport is None
        or inference_transport.mode == "direct"
        or health_check is None
        or health_check()
    ):
        return classification
    return ResultClassification(category="transport_unavailable", subtype="proxy_lost")


def managed_switchyard_context(
    config: DispatchConfig,
    inference_transport: ResolvedInferenceTransport | None,
) -> contextlib.AbstractContextManager[ManagedSwitchyardSupervisor | None]:
    """Create one run-owned supervisor only for an exact managed identity."""
    if inference_transport is None or inference_transport.mode != "switchyard":
        return contextlib.nullcontext(None)
    settings = config.inference_transport.switchyard
    if not inference_transport.managed:
        if settings.managed:
            raise SwitchyardSupervisorError(
                "managed Switchyard transport identity mismatch"
            )
        return contextlib.nullcontext(None)
    if (
        not settings.managed
        or settings.endpoint != inference_transport.endpoint
        or settings.route != inference_transport.route
        or settings.expected_version != inference_transport.expected_version
    ):
        raise SwitchyardSupervisorError(
            "managed Switchyard transport identity mismatch"
        )
    return ManagedSwitchyardSupervisor(
        settings,
        classifier_ingress_format=inference_transport.api_format,
    )


def sidecar_switchyard_context(
    config: DispatchConfig,
    inference_transport: ResolvedInferenceTransport,
    *,
    worker_image_id: str,
) -> contextlib.AbstractContextManager[SidecarAttemptSupervisor]:
    """Create one Docker-sidecar owner for exactly one contained attempt."""
    settings = config.inference_transport.switchyard
    sidecar = settings.sidecar
    if (
        inference_transport.mode != "switchyard"
        or not inference_transport.sidecar
        or inference_transport.managed
        or sidecar is None
        or not sidecar.enabled
        or inference_transport.sidecar_image != sidecar.image
        or inference_transport.sidecar_egress_network != sidecar.egress_network
    ):
        raise SwitchyardSupervisorError(
            "sidecar Switchyard transport identity mismatch"
        )
    return SidecarAttemptSupervisor(
        settings,
        runtime=config.sandbox.runtime,
        worker_image_id=worker_image_id,
        proxy_environment=config.sandbox.proxy_environment,
    )


def _health_check_from_verdict(healthy: bool) -> Callable[[], bool]:
    """Capture liveness before final release removes the managed endpoint."""

    def health_check() -> bool:
        return healthy

    return health_check


def _requested_service_tier(config: DispatchConfig, runner_name: str) -> str | None:
    """Return the governed Codex tier request, never a prompt-derived value."""
    if runner_name != "codex":
        return None
    tier = config.agent.codex.service_tier
    return tier or None


def _apply_proxy_served_model(
    result: DispatchResult,
    evidence: SwitchyardSelectionEvidence,
    *,
    route: str,
) -> None:
    """Fill the served-model slot from proxy evidence, never over the runner.

    A distinct structured model the runner reported wins outright.  Only the
    empty slot, the admitted route alias, or another ``switchyard/`` alias is
    open to proxy evidence.
    """
    current = result.resolved_model
    if current and current != route and not current.startswith("switchyard/"):
        return
    if evidence.selected_model:
        result.resolved_model = evidence.selected_model
        result.resolved_model_source = "switchyard_proxy_metrics"
    elif current and current.startswith("switchyard/"):
        # Keep the admitted alias in transport metadata, never in the
        # served-model slot when no trustworthy identity was observed.
        # The source must clear with it, or persistence would attest a
        # runner-reported identity behind an empty served model.
        result.resolved_model = ""
        result.resolved_model_source = ""


def _reconcile_switchyard_evidence(
    result: DispatchResult,
    evidence: SwitchyardSelectionEvidence,
    *,
    route: str | None,
) -> None:
    """Attach sanitized proxy identity evidence to an adaptive result.

    The admitted route remains the transport identity held by the caller.  A
    proxy-selected model is copied into ``resolved_model`` only for a
    non-passthrough route and only when the runner did not provide a distinct
    structured model.  Proxy observations never replace runner token usage.
    """
    result.switchyard_proxy_observation = evidence.observation.to_json()
    if not route or route == "switchyard/passthrough":
        return

    _apply_proxy_served_model(result, evidence, route=route)
    if evidence.selected_tier:
        result.selected_tier = evidence.selected_tier
    if evidence.classifier_model:
        result.classifier_overhead_model = evidence.classifier_model
        result.classifier_overhead_input_tokens = evidence.classifier_input_tokens
        result.classifier_overhead_output_tokens = evidence.classifier_output_tokens


def _attach_classifier_evidence(
    result: DispatchResult,
    records: tuple[object, ...],
    *,
    route: str | None,
) -> None:
    """Persist only the classifier contract's sanitized JSON projection."""
    if not records:
        return
    serialized: list[dict[str, object]] = []
    for record in records:
        to_json = getattr(record, "to_json", None)
        if not callable(to_json):
            continue
        value = to_json()
        if isinstance(value, dict):
            serialized.append(value)
    if serialized:
        result.classifier_evidence = serialized
    if route and route != "switchyard/passthrough" and not result.selected_tier:
        selected = getattr(records[-1], "selected_tier", None)
        if isinstance(selected, str):
            result.selected_tier = selected


def _collect_managed_evidence(
    managed: object,
    result: DispatchResult,
) -> SwitchyardSelectionEvidence | None:
    """Snapshot proxy selection evidence while the managed endpoint is alive.

    Best-effort by construction: a supervisor without the hook, a malformed
    return, or any exception from the metrics path yields ``None`` rather than
    propagating, because proxy evidence must never turn a successful runner
    result into a failed dispatch.
    """
    collect_evidence = getattr(managed, "accounting_evidence", None)
    if not callable(collect_evidence):
        return None
    try:
        candidate = collect_evidence(
            served_model=result.resolved_model or None,
            # The proxy counts the WHOLE prompt: switchyard.prompt_tokens is
            # recorded as input + cached + cache_creation. Runners report those
            # legs separately (#1564), so the uncached leg alone would never
            # equal the proxy's counter and the exact usage match could not
            # fire. Recombine them here rather than teaching the matcher about
            # cache shapes.
            runner_input_tokens=(
                result.input_tokens
                + result.cache_read_tokens
                + result.cache_creation_tokens
            ),
            runner_output_tokens=result.output_tokens,
        )
    except Exception:
        # Proxy evidence is diagnostic only.  No metrics failure may reach the
        # outer handler that writes a `failed` event and re-raises.
        return None
    if isinstance(candidate, SwitchyardSelectionEvidence):
        return candidate
    return None


def _capture_managed_state(
    managed: ManagedSwitchyardSupervisor | None,
    result: DispatchResult,
    fallback_health_check: Callable[[], bool] | None,
) -> tuple[
    SwitchyardSelectionEvidence | None, object | None, Callable[[], bool] | None
]:
    """Read everything the supervisor can only answer before teardown.

    Returns the proxy evidence, the supervisor itself (its classifier records
    are drained later, after the context exits), and a health check that
    captures the liveness verdict before final release removes the endpoint.
    """
    if managed is None:
        return None, None, fallback_health_check
    return (
        _collect_managed_evidence(managed, result),
        managed,
        _health_check_from_verdict(managed.is_healthy()),
    )


def _managed_classifier_records(managed: object | None) -> tuple[object, ...]:
    """Drain the supervisor's classifier records once teardown has completed.

    The supervisor drains classifier stderr during context teardown, so reading
    the records earlier would race the drainer.
    """
    if managed is None:
        return ()
    records = getattr(managed, "classifier_evidence", ())
    return records if isinstance(records, tuple) else ()


def _proxy_request_count(
    evidence: SwitchyardSelectionEvidence,
    route: str,
) -> int:
    """Count requests attributed to the adaptive route in the managed delta."""
    return round(
        sum(
            sample.value
            for sample in evidence.observation.samples
            if sample.family == "requests"
            and sample.labels.get("route") in {None, route}
        )
    )


def _classifier_accounting_reason(
    result: DispatchResult,
    evidence: SwitchyardSelectionEvidence | None,
    *,
    route: str,
) -> str | None:
    """Return the classifier-record completeness failure, if any."""
    if not result.classifier_evidence:
        return "classifier_evidence_missing"
    if evidence is None:
        return None
    request_count = _proxy_request_count(evidence, route)
    if request_count and len(result.classifier_evidence) != request_count:
        return "classifier_evidence_incomplete"
    return None


def _adaptive_accounting_reasons(
    result: DispatchResult,
    evidence: SwitchyardSelectionEvidence | None,
    *,
    route: str,
) -> list[str]:
    """Build stable bounded reasons for an incomplete adaptive result."""
    reasons: list[str] = []
    if evidence is None or evidence.observation.unavailable_reason is not None:
        reasons.append("proxy_metrics_unavailable")
    classifier_reason = _classifier_accounting_reason(
        result,
        evidence,
        route=route,
    )
    if classifier_reason is not None:
        reasons.append(classifier_reason)
    if not result.resolved_model or result.resolved_model.startswith("switchyard/"):
        reasons.append("served_model_missing")
    elif result.classifier_evidence and not result.classifier_overhead_model:
        reasons.append("classifier_overhead_missing")
    return reasons


def _mark_adaptive_accounting_status(
    result: DispatchResult,
    evidence: SwitchyardSelectionEvidence | None,
    *,
    route: str | None,
) -> None:
    """Make incomplete adaptive accounting an explicit terminal fact."""
    if not route or route == "switchyard/passthrough":
        return
    reasons = _adaptive_accounting_reasons(result, evidence, route=route)
    result.accounting_status = "unreconciled" if reasons else "reconciled"
    result.accounting_unreconciled_reasons = reasons or None


def reconcile_managed_switchyard_lifecycle(
    managed: object,
    result: DispatchResult,
    *,
    route: str | None,
) -> None:
    """Reconcile evidence only available after the managed owner tears down.

    The retry layer owns the proxy across every attempt.  Its context exit is
    therefore the first point at which stderr drainers and the final metrics
    snapshot are both complete.
    """
    evidence = _collect_managed_evidence(managed, result)
    if evidence is not None:
        _reconcile_switchyard_evidence(result, evidence, route=route)
    _attach_classifier_evidence(
        result,
        _managed_classifier_records(managed),
        route=route,
    )
    _mark_adaptive_accounting_status(result, evidence, route=route)


def _write_started_event(
    ef_typed: IO[str] | None,
    *,
    runner_name: str,
    model: str,
    issue_num: int,
) -> None:
    """Emit the run's opening event when event emission is enabled."""
    if ef_typed is not None:
        _write_event(
            ef_typed, "started", runner=runner_name, model=model, issue=issue_num
        )


@contextlib.contextmanager
def _contained_prompt_handoff(
    worktree_dir: Path, prompt: str, contained: ContainedExecution | None
) -> Iterator[None]:
    """Place the prompt where a contained agent can read it; remove it after.

    #695: the container entrypoint reads this file from the mounted checkout and
    feeds it to the agent on stdin, so the prompt never travels in argv.

    Removed on EVERY exit path (#725) — success, failure, or kill — because the
    caller may publish this checkout to a PR branch immediately afterwards, and
    a surviving handoff file lands in somebody's pull request.

    A no-op on the host path, where the prompt is simply an argument.
    """
    if contained is None:
        yield
        return
    write_prompt_handoff(worktree_dir, prompt)
    try:
        yield
    finally:
        remove_prompt_handoff(worktree_dir)


def host_publishes_for(config: DispatchConfig, runner: AgentRunner) -> bool:
    """Does the host publish this run, so the agent must neither push nor hold
    the credential? True under canonical publication for every runner, and for
    a host-style runner in any mode."""
    return (
        getattr(config.publication, "publish", "canonical") == "canonical"
        or runner.publication_style == "host"
    )


def _agent_process_env(
    runner: AgentRunner, *, host_publishes: bool = False
) -> dict[str, str]:
    """Build the child environment, applying any runner-specific overlay."""
    env = _build_child_env(host_publishes=host_publishes)
    if runner.name == "codex":
        from kew.runners.codex import codex_process_env

        return codex_process_env(env)
    return env


def _finalize_agent_result(
    result: DispatchResult,
    *,
    runner: AgentRunner,
    config: DispatchConfig,
    last_output: str | None,
    inference_transport: ResolvedInferenceTransport | None,
    health_check: Callable[[], bool] | None,
    evidence: SwitchyardSelectionEvidence | None,
    classifier_records: tuple[object, ...],
    timeout_override_minutes: int | None,
    needs_human: str | None = None,
    quota_rejection: QuotaRejection | None = None,
    quota_warning: QuotaWarning | None = None,
) -> None:
    """Classify original output, then retain sanitized fields and accounting."""
    # Never sanitize last_output before the adapter derives its fixed decision.
    # _apply_classification preserves fixed kinds and redacts unknown identities.
    classification = runner.classify_result(
        exit_code=result.exit_code,
        last_output=last_output,
        config=config,
    )
    classification = _honor_needs_human(classification, needs_human)
    classification = _classify_transport_health(
        classification, inference_transport, health_check
    )
    _apply_classification(result, classification, runner.name)
    _honor_quota_exhausted(result, quota_rejection, runner.name)
    if quota_warning is not None:
        # Kept whatever the run's outcome: dispatch records it for the token.
        result.quota_warning = quota_warning.with_runner(
            quota_warning.runner or runner.name
        )

    runner.enrich_result(result, config)
    capture_session_id(result, result.session_id, runner.name)

    route = inference_transport.route if inference_transport is not None else None
    if evidence is not None:
        _reconcile_switchyard_evidence(result, evidence, route=route)
    _attach_classifier_evidence(result, classifier_records, route=route)
    _mark_adaptive_accounting_status(result, evidence, route=route)

    eff_minutes = (
        timeout_override_minutes
        if timeout_override_minutes is not None
        else config.agent.timeout_minutes
    )
    result.hard_stop = classify_hard_stop(result, timeout_seconds=eff_minutes * 60)
    _report_hard_stop(result, eff_minutes)
    _name_signal_exit(result)


def _name_signal_exit(result: DispatchResult) -> None:
    """Name a 134/137 death, and say so when it reads as ``resource``."""
    reason = name_signal_exit(result)
    if reason:
        _error(f"Runner {reason}")


def _honor_quota_exhausted(
    result: DispatchResult, rejection: QuotaRejection | None, runner_name: str
) -> None:
    """Read a run the provider refused for quota as ``quota_exhausted`` (#2643).

    Such a run ends in seconds with zero tokens and a generic error exit, so
    the adapter's own classification calls it a failure. The stream said why;
    the category says it too, so dispatch holds the runner's lane instead of
    failing the node.
    """
    if rejection is None or not is_quota_exhausted(
        category=result.error_category,
        exit_code=result.exit_code,
        rejection=rejection,
    ):
        return
    result.error_category = QUOTA_EXHAUSTED_CATEGORY
    result.quota_rejection = rejection.with_runner(rejection.runner or runner_name)
    _warn(f"Provider refused the run: {result.quota_rejection.describe()}")


def _honor_needs_human(
    classification: ResultClassification, reason: str | None
) -> ResultClassification:
    """Turn a run the agent stopped for a human into a refusal (#2601).

    The prompts tell the agent to end ``KEW_NEEDS_HUMAN: <reason>`` instead of
    publishing — a policy gate it could not make pass, say. Such a run usually
    exits 0, and only the Codex adapter read the marker, so on every other
    runner the exit code published the change the agent refused. Read here,
    from the parsed stream, every runner refuses it the same way. A run that
    already failed keeps its own category.
    """
    if reason is None or classification.category:
        return classification
    return replace(
        classification, category="needs_human", subtype=reason or "unspecified"
    )


def _report_hard_stop(result: DispatchResult, eff_minutes: int) -> None:
    if result.hard_stop == "wall_clock":
        suffix = (
            f" (host watchdog: {result.resource_cap_reason})"
            if result.resource_cap_reason
            else ""
        )
        _error(f"Timed out after {eff_minutes} minutes{suffix}")
    elif result.hard_stop == "resource_cap":
        _error(f"Killed by host cap: {result.resource_cap_reason}")
    elif result.hard_stop == "hard_kill":
        _error(
            f"Killed (exit 137) after {result.duration_seconds:.0f}s of a "
            f"{eff_minutes}-minute budget: memory cap (OOM) or an in-container "
            "SIGKILL, not a timeout"
        )


def _agent_proxy_redaction(
    config: DispatchConfig,
    contained: ContainedExecution | None,
    environment: Mapping[str, str],
) -> contextlib.AbstractContextManager[None]:
    policy = "inherit" if contained is None else config.sandbox.proxy_environment
    return proxy_redaction_scope(policy, environment=environment)


def run_agent(
    *,
    runner: AgentRunner,
    prompt: str,
    worktree_dir: Path,
    config: DispatchConfig,
    issue_num: int,
    resume_session_id: str | None = None,
    model: str = "",
    emit_events: bool = False,
    log_file_path: Path | None = None,
    on_spawn: Callable[[int], Callable[[], None] | None] | None = None,
    budget_override_usd: float | None = None,
    timeout_override_minutes: int | None = None,
    should_pause: Callable[[], bool] | None = None,
    budget_check: Callable[[float], bool] | None = None,
    anomaly_threshold_usd: float | None = None,
    anomaly_compaction_allowance_usd: float = 0.0,
    _now: Callable[[], float] = time.monotonic,
    container_handle: ContainerHandle | None = None,
    contained: ContainedExecution | None = None,
    inference_transport: ResolvedInferenceTransport | None = None,
    transport_health_check: Callable[[], bool] | None = None,
    managed_switchyard: ManagedSwitchyardSupervisor | None = None,
    admission: PendingAdmission | None = None,
    credential: BoundedRunKey | None = None,
) -> DispatchResult:
    """Run an agent in the worktree. Runner-agnostic subprocess lifecycle.

    *contained* switches the EXECUTION STEP to a container and changes nothing
    else: the same streaming, budget watchdog, circuit breaker, pause check,
    classification and :class:`DispatchResult` accounting apply either way. That
    is the point of putting it here rather than beside this function — a
    parallel contained lifecycle is what left contained runs with no per-run
    cost governor at all (#1462).

    Two things differ inside the container, both forced by the boundary: the
    prompt travels as a mounted file rather than in argv (#695), and resume is
    unavailable, so a retry restarts the agent instead of continuing its
    session.
    """
    configured_model = model or config.agent.model
    require_public_model(configured_model)
    log_file, event_file = _setup_log_paths(
        config, issue_num, log_file_path, emit_events
    )
    result = DispatchResult(
        exit_code=-1,
        issue_num=issue_num,
        log_file=log_file,
        requested_service_tier=_requested_service_tier(config, runner.name) or "",
        event_file=event_file,
        configured_model=configured_model,
    )

    derivation = StreamDerivation()

    env = launch_environment(
        _agent_process_env(runner, host_publishes=host_publishes_for(config, runner)),
        credential,
        contained=contained is not None,
    )
    # Non-secret capability marker used by the managed guard to distinguish
    # the assigned worktree from the operator checkout. Set after KEW_*
    # filtering so the orchestrator's private environment stays private.
    env["AGENT_ASSIGNED_WORKTREE"] = str(worktree_dir.resolve())
    if contained is None:
        cmd, timeout_cmd, timeout_seconds = _build_agent_cmd(
            runner,
            prompt,
            config,
            env,
            resume_session_id,
            model,
            budget_override_usd,
            timeout_override_minutes,
        )
    else:
        # The prompt does not go in argv here; _contained_prompt_handoff below
        # places it in the mounted checkout for the entrypoint to read (#695).
        cmd, timeout_cmd, timeout_seconds = _wrap_with_timeout(
            list(contained.argv), config, timeout_override_minutes
        )
    pricing_overrides = _get_pricing_overrides(config)
    effective_health_check = transport_health_check
    managed_evidence: SwitchyardSelectionEvidence | None = None
    managed_for_classifier: object | None = None

    _ef_ctx = (
        open(event_file, "a")  # noqa: SIM115 — wrapped in explicit try/finally
        if event_file is not None
        else contextlib.nullcontext(None)
    )
    with (
        preserve_dispatch_result(result),
        execution_cancellation(admission),
        _agent_proxy_redaction(config, contained, env),
    ):
        with (
            _contained_prompt_handoff(worktree_dir, prompt, contained),
            _ef_ctx as ef,
        ):
            ef_typed: IO[str] | None = ef if event_file is not None else None
            _write_started_event(
                ef_typed, runner_name=runner.name, model=model, issue_num=issue_num
            )

            try:
                managed_context = (
                    contextlib.nullcontext(managed_switchyard)
                    if managed_switchyard is not None
                    else managed_switchyard_context(config, inference_transport)
                )
                with managed_context as managed:
                    require_credential_lifetime(credential, timeout_seconds)
                    last_meaningful_line, model, returncode = _run_process(
                        cmd,
                        worktree_dir,
                        env,
                        log_file,
                        ef_typed,
                        runner,
                        result,
                        model,
                        pricing_overrides,
                        timeout_cmd,
                        timeout_seconds,
                        derivation,
                        on_spawn,
                        should_pause=should_pause,
                        budget_check=budget_check,
                        anomaly_threshold_usd=anomaly_threshold_usd,
                        anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
                        budget_stop_grace_seconds=config.agent.budget_stop_grace_seconds,
                        _now=_now,
                        container_handle=container_handle,
                        contained=contained,
                        admission=admission,
                        credential=credential,
                    )
                    if not _is_timeout_exit(result.exit_code):
                        result.exit_code = returncode
                    _write_finished_event(ef_typed, result, model, pricing_overrides)
                    (
                        managed_evidence,
                        managed_for_classifier,
                        effective_health_check,
                    ) = _capture_managed_state(managed, result, effective_health_check)
            except Exception as exc:
                sanitize_exception_args(exc)
                if ef_typed is not None:
                    _write_event(ef_typed, "failed", error=exception_text(exc))
                raise
            finally:
                # The sidecar is this run's ONLY durable evidence — the log holds
                # sanitized progress and nothing to re-derive from. Write it on
                # every exit path, including the raising one, or a crashed run
                # loses facts it demonstrably had (#1517).
                write_derived_evidence(log_file, derivation.evidence())

        _finalize_agent_result(
            result,
            runner=runner,
            config=config,
            last_output=last_meaningful_line,
            inference_transport=inference_transport,
            health_check=effective_health_check,
            evidence=managed_evidence,
            classifier_records=_managed_classifier_records(managed_for_classifier),
            timeout_override_minutes=timeout_override_minutes,
            needs_human=derivation.needs_human,
            quota_rejection=derivation.quota_exhausted,
            quota_warning=derivation.quota_warning,
        )
    return result
