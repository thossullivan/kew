"""Hermes runner implementation.

Hermes is a headless coding agent (installed via its ``install.sh``) that runs
against OpenRouter. This runner shells out to::

    hermes -z "<prompt>" --usage-file <run-scoped path> \
        --provider openrouter --model <vendor/model>

Unlike pi/opencode there is **no mid-run event stream** — hermes writes the
final response text to stdout and, on completion, drops a single JSON *usage
file* carrying tokens and its own models-API cost estimate. That file, not the
exit code, is the authoritative signal:

1. **The usage file outranks the exit code.** hermes exits ``0`` even on a
   credential/config failure (it prints "No LLM provider configured…" as plain
   text) while the usage file has ``failed: true`` with null cost/tokens. So
   ``classify_result`` reads the usage file: ``failed`` → permanent (or
   transient on a rate-limit/network marker), ``completed`` → success. Only a
   missing/ambiguous file falls back to the exit code.
2. **enrich_result** reads the usage file for tokens + ``estimated_cost_usd``
   (the runner-reported native cost feeding #1082). hermes's figure is its own
   models-API estimate — the same fidelity class as opencode's, framed honestly
   via the ``openrouter:via-hermes`` endpoint (#251).
3. **A SIGTERM/SIGKILL leaves no usage file.** enrich tolerates a missing or
   partial file (killed runs meter $0 native; bounded by budget + the #291
   watchdog) rather than crashing.

Policy (Tom, 2026-07-25): hermes runs **OpenRouter only**. kew model strings are
``openrouter/<vendor>/<model>``; the ``openrouter/`` prefix is stripped and the
rest handed to ``--model`` under ``--provider openrouter``. Any non-openrouter
model fails closed — anthropic is deliberately not wired.

The usage-file path is run-scoped under the log dir (never cwd-relative inside
the worktree, so it can't land in the PR branch) and is cleaned up on every exit
path, mirroring the sandbox prompt-file precedent (#725).

Known v1 gaps (deferred):
- No native ``--max-budget-usd`` flag; the outer dispatch budget gate applies
  (same as the OpenCode/pi runners).
- No sandbox image (#779); no ``hermes chat -q`` transcript activity lines (v2).
"""

from __future__ import annotations

import contextlib
import json as _json
import uuid
from pathlib import Path
from typing import Any

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runner_fields import (
    REDACTED_IDENTITY,
    protocol_float,
    protocol_int,
    reported_identity,
    reported_model_identity,
    runner_text,
)
from kew.runners.protocol import (
    ContainmentPosture,
    LoopStyle,
    PublicationStyle,
    ResultClassification,
    StreamEvent,
)
from kew.tracking import DispatchResult

_OPENROUTER_PREFIX = "openrouter/"

# Substrings (matched case-insensitively against hermes's error text) that mark
# a failure as worth retrying rather than giving up on.
_TRANSIENT_ERROR_MARKERS = (
    "rate limit",
    "ratelimit",
    "overloaded",
    "overload",
    "429",
    "503",
    "service unavailable",
    "timeout",
    "timed out",
    "network",
    "connection",
    "econnreset",
)

# Exit codes that mean the process was killed (timeout / SIGTERM) → transient.
_KILL_EXIT_CODES = frozenset({124, 143})

_RESUME_PROMPT = (
    "The previous run was interrupted by a transient error. "
    "Please continue where you left off. Check what progress was made "
    "(commits, file changes) and resume the task."
)


class HermesRunner:
    """Runner for the hermes CLI (OpenRouter-only)."""

    def __init__(self, session_id: str | None = None) -> None:
        self._session_id = session_id
        # Set by build_command/build_resume_command; read by classify/enrich.
        self._usage_file: Path | None = None

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def loop_style(self) -> LoopStyle:
        return "native"

    @property
    def publication_style(self) -> PublicationStyle:
        return "agent"

    def containment_posture(
        self, *, runtime: str | None = None
    ) -> ContainmentPosture | None:
        return None

    @property
    def uncontainable_reason(self) -> str | None:
        return None

    @property
    def transport_capability(self) -> RunnerTransportCapability:
        """Refuse Switchyard until Hermes proves an isolated override contract."""
        return RunnerTransportCapability()

    @property
    def required_credentials(self) -> tuple[str, ...]:
        """OpenRouter only, matching the runner's OpenRouter-only policy (#1146)."""
        return ("OPENROUTER_API_KEY",)

    # -- command construction ---------------------------------------------

    def _provider_model_flags(self, config: DispatchConfig, model: str) -> list[str]:
        """`--provider openrouter --model <vendor/model>`; fail closed otherwise.

        hermes runs OpenRouter only. kew model strings are
        ``openrouter/<vendor>/<model>``; strip the prefix for ``--model``.
        """
        effective = model or config.agent.model
        if not effective.startswith(_OPENROUTER_PREFIX):
            raise ValueError(
                f"HermesRunner supports OpenRouter models only "
                f"(model must start with {_OPENROUTER_PREFIX!r}); got {effective!r}. "
                f"Route non-OpenRouter models to another runner."
            )
        stripped = effective[len(_OPENROUTER_PREFIX) :]
        if not stripped:
            raise ValueError(
                f"HermesRunner got a bare {_OPENROUTER_PREFIX!r} with no model id."
            )
        return ["--provider", "openrouter", "--model", stripped]

    def _fresh_usage_file(self, config: DispatchConfig) -> Path:
        """A run-scoped usage-file path under the log dir (never cwd-relative).

        Living under the log dir keeps it out of the worktree, so it can never
        land in the PR branch (#725 precedent). A fresh id per invocation gives
        resume its own file, as the contract requires.
        """
        log_dir = Path(config.logging.dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"hermes-usage-{uuid.uuid4().hex}.json"
        self._usage_file = path
        return path

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """Sandbox ``"$@"`` args — see :meth:`AgentRunner.container_args`.

        ``-z <prompt>`` is the prompt handoff (the image's job) and
        ``--usage-file`` is a HOST log-dir path the container cannot write, so
        neither crosses the boundary. In-container hermes telemetry therefore
        needs the stdout parser path (#779 N4) before it is routable.
        """
        return [
            *self._provider_model_flags(config, model),
            *config.agent.hermes.extra_args,
        ]

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        usage_file = self._fresh_usage_file(config)
        cmd = ["hermes", "-z", prompt, "--usage-file", str(usage_file)]
        cmd.extend(self._provider_model_flags(config, model))
        cmd.extend(config.agent.hermes.extra_args)
        return cmd

    def build_resume_command(
        self,
        *,
        session_id: str,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        # Lock this instance to the prior session so classify_result reports it
        # for any further retries (mirrors pi).
        self._session_id = session_id
        usage_file = self._fresh_usage_file(config)
        cmd = [
            "hermes",
            "-z",
            _RESUME_PROMPT,
            "--resume",
            session_id,
            "--usage-file",
            str(usage_file),
        ]
        cmd.extend(self._provider_model_flags(config, model))
        cmd.extend(config.agent.hermes.extra_args)
        return cmd

    # -- streaming ---------------------------------------------------------

    def parse_line(self, line: str) -> list[StreamEvent]:
        """hermes has no event stream; stdout is the final response text.

        Emit a single activity event for a nonempty line so the run is not
        silent; tokens/cost come from the usage file in ``enrich_result``.
        """
        stripped = line.strip()
        if not stripped:
            return []
        return [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message=self._truncate(stripped),
            )
        ]

    @staticmethod
    def _truncate(text: str, limit: int = 200) -> str:
        text = runner_text(text)
        return text[:limit] + "..." if len(text) > limit else text

    # -- usage file --------------------------------------------------------

    def _read_usage(self) -> dict[str, Any] | None:
        """Parse the current usage file, or None if missing/partial/malformed."""
        path = self._usage_file
        if path is None:
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = _json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def _cleanup_usage_file(self) -> None:
        path = self._usage_file
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    # -- classification ----------------------------------------------------

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification:
        usage = self._read_usage()
        session_id = (
            reported_identity(runner_text(usage.get("session_id") or ""), "session_id")
            if usage
            else ""
        )
        if session_id:
            self._session_id = session_id
        category, stop_reason = self._classify(exit_code, usage, last_output, config)
        return ResultClassification(
            category=category,
            session_id=self._session_id,
            stop_reason=stop_reason,
        )

    def _classify(
        self,
        exit_code: int,
        usage: dict[str, Any] | None,
        last_output: str | None,
        config: DispatchConfig,
    ) -> tuple[str, str]:
        """Return (category, stop_reason). The usage file outranks the exit code."""
        if usage is not None:
            # hermes exits 0 even on a credential/config failure, so the usage
            # file's flags are authoritative when present.
            if usage.get("failed"):
                return self._classify_failure(usage, last_output), "failed"
            if usage.get("completed"):
                return "", "completed"

        # No usage file (SIGTERM/kill leaves none) or an ambiguous one — fall
        # back to the exit code.
        if exit_code in _KILL_EXIT_CODES:
            return "transient", ""
        if exit_code == 0:
            # Clean exit with no completed usage file: hermes never ran the task
            # (most often a startup/credential error printed as plain text).
            return "permanent", ""
        if exit_code in config.retry.retriable_exit_codes:
            return "transient", ""
        return "permanent", ""

    def _classify_failure(self, usage: dict[str, Any], last_output: str | None) -> str:
        """A ``failed`` usage file → transient on a network/rate-limit marker."""
        error_text = " ".join(
            part
            for part in (
                str(usage.get("error") or ""),
                str(usage.get("error_message") or ""),
                str(usage.get("message") or ""),
                last_output or "",
            )
            if part
        ).lower()
        if any(marker in error_text for marker in _TRANSIENT_ERROR_MARKERS):
            return "transient"
        return "permanent"

    def supports_resume(self) -> bool:
        return True

    # -- enrichment --------------------------------------------------------

    def enrich_result(
        self,
        result: DispatchResult,
        config: DispatchConfig,
    ) -> None:
        """Read tokens + native cost from the usage file, then clean it up.

        hermes emits no stream usage events, so this is the sole source of the
        run's tokens and cost. A killed run leaves no file — meter $0 native and
        do not crash (#291 watchdog bounds it).
        """
        try:
            usage = self._read_usage()
            if usage is not None:
                self._apply_usage(result, usage)
        finally:
            self._cleanup_usage_file()

    @staticmethod
    def _apply_usage(result: DispatchResult, usage: dict[str, Any]) -> None:
        result.accumulate_usage(
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens"))
            + _as_int(usage.get("reasoning_tokens")),
            cache_creation_tokens=_as_int(usage.get("cache_write_tokens")),
            cache_read_tokens=_as_int(usage.get("cache_read_tokens")),
        )
        # hermes's own models-API estimate is the runner-reported native cost
        # (#1082); honest fidelity is framed by the endpoint (#251). Leave it
        # None when absent (failed/killed) so the run meters $0 native.
        cost = usage.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            result.reported_cost_usd = protocol_float(cost, "estimated_cost_usd")
        # Reconstruct the kew-side model string for #251's resolved_model.
        model = reported_model_identity(usage.get("model"))
        provider = reported_identity(
            runner_text(usage.get("provider") or ""), "provider"
        )
        if REDACTED_IDENTITY in (model, provider):
            result.resolved_model = REDACTED_IDENTITY
        elif model and provider == "openrouter":
            result.resolved_model = f"{_OPENROUTER_PREFIX}{model}"


def _as_int(value: object) -> int:
    """Coerce a usage-file numeric field to int, tolerating null/garbage."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return protocol_int(value, "usage.tokens")
    return 0
