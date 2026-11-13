"""Pi runner implementation.

Pi (https://pi.dev, npm `@earendil-works/pi-coding-agent`) is a minimal,
self-extensible, multi-provider terminal coding agent by Mario Zechner. This
runner shells out to::

    pi --mode json --session-id <uuid> -p <prompt>

In ``--mode json`` pi streams every session event as one JSON object per line
(see the package's ``docs/json.md``). The first line is a ``session`` header
carrying the session id; assistant turns arrive as ``message_end`` events whose
``message`` is an ``AssistantMessage`` with ``usage`` (input/output/cache tokens
and native ``cost``) and a ``stopReason``.

Provider credentials are pi's own concern (``ANTHROPIC_API_KEY``/etc. in the
environment, or ``pi /login`` OAuth) — kew does not manage them. Missing
credentials surface as permanent failures with pi's own error message.

Two pi-specific wrinkles drive the design:

1. **Session id is on the *first* line.** kew only hands ``classify_result`` the
   *last* meaningful line, so we cannot recover the id from there. Instead the
   runner generates the id up front, passes it via ``--session-id`` (pi creates
   the session if it does not exist), and reports that same id back from
   ``classify_result``. One ``PiRunner`` instance handles exactly one dispatch,
   so this per-instance id is stable for that run and feeds resume cleanly.

2. **``--mode json`` exits 0 even on a model error.** pi only maps a failure to
   a non-zero exit in *text* mode. The authoritative signal is the final
   assistant message's ``stopReason`` (``error``/``aborted``), which this runner
   inspects in ``classify_result``.

Known v1 gaps (deferred):
- No native ``--max-budget-usd`` flag; the outer dispatch budget gate applies
  (same as the OpenCode runner).
"""

from __future__ import annotations

import json as _json
import uuid
from typing import Any

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runner_fields import (
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

# Substrings (matched case-insensitively against pi's errorMessage) that mark a
# failure as worth retrying rather than giving up on.
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

# stopReason values that mean "the run failed", as opposed to a normal stop or
# an intermediate tool-call turn.
_FAILURE_STOP_REASONS = frozenset({"error", "aborted"})
# stopReason values that end a run successfully.
_SUCCESS_STOP_REASONS = frozenset({"stop", "length"})
# Every stopReason that ends a turn for good (success or failure); the
# intermediate "toolUse" is deliberately excluded.
_TERMINAL_STOP_REASONS = _SUCCESS_STOP_REASONS | _FAILURE_STOP_REASONS

_RESUME_PROMPT = (
    "The previous run was interrupted by a transient error. "
    "Please continue where you left off. Check what progress was made "
    "(commits, file changes) and resume the task."
)


def _resolve_tool_target(args: object) -> str:
    """Extract a target path/command from a tool call's args dict."""
    if not isinstance(args, dict):
        return ""
    return runner_text(
        args.get("file_path") or args.get("path") or args.get("command", "") or ""
    )


class PiRunner:
    """Runner for the pi CLI (earendil-works/pi)."""

    def __init__(self, session_id: str | None = None) -> None:
        # Lazily assigned in build_command when not provided, so the registry
        # can construct PiRunner() with no args and still get a stable id.
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "pi"

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
        """Refuse Switchyard until pi has a proven isolated override contract."""
        return RunnerTransportCapability()

    @property
    def required_credentials(self) -> tuple[str, ...]:
        """OpenRouter — the only namespace the registry routes to pi (#1146).

        pi's ``/login`` OAuth session lives in its host-side config and never
        reaches a container, so an env key is the only in-container auth path.
        ``openrouter/`` is the sole prefix ``runner_accepts_model`` grants pi,
        so that is the sole key forwarded.
        """
        return ("OPENROUTER_API_KEY",)

    def _ensure_session_id(self) -> str:
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
        return self._session_id

    def _model_flag(self, config: DispatchConfig, model: str) -> list[str]:
        effective_model = model or config.agent.model
        return ["--model", effective_model] if effective_model else []

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """Sandbox ``"$@"`` args — see :meth:`AgentRunner.container_args`.

        ``--session-id`` is deliberately dropped: it is a HOST-side resume
        handle (``_ensure_session_id`` pins this instance for a later
        ``build_resume_command``), and the contained path has no host process to
        resume into — minting one here would only put a dead uuid in argv.
        """
        args = ["--mode", "json"]
        if model:
            args.extend(["--model", model])
        args.extend(config.agent.pi.extra_args)
        return args

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        cmd = ["pi", "--mode", "json"]
        cmd.extend(self._model_flag(config, model))
        cmd.extend(["--session-id", self._ensure_session_id()])
        cmd.extend(config.agent.pi.extra_args)
        cmd.extend(["-p", prompt])
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
        # Resuming locks this instance to the prior session id so that
        # classify_result reports it for any further retries.
        self._session_id = session_id
        cmd = ["pi", "--mode", "json"]
        cmd.extend(self._model_flag(config, model))
        cmd.extend(["--session", session_id])
        cmd.extend(config.agent.pi.extra_args)
        cmd.extend(["-p", _RESUME_PROMPT])
        return cmd

    # -- streaming ---------------------------------------------------------

    def parse_line(self, line: str) -> list[StreamEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        try:
            event = _json.loads(stripped)
        except (ValueError, TypeError):
            return []
        if not isinstance(event, dict):
            return []

        event_type = event.get("type", "")
        if event_type == "session":
            return self._parse_session_header(line, event)
        if event_type == "tool_execution_start":
            return self._parse_tool_start(line, event)
        if event_type == "message_end":
            return self._parse_message_end(line, event)
        return []

    def _parse_session_header(
        self, line: str, event: dict[str, Any]
    ) -> list[StreamEvent]:
        session_id = reported_identity(runner_text(event.get("id", "")), "session_id")
        if session_id and self._session_id is None:
            self._session_id = session_id
        return [
            StreamEvent(
                raw_line=line,
                event_type="init",
                metadata={"session_id": session_id},
            )
        ]

    def _parse_tool_start(self, line: str, event: dict[str, Any]) -> list[StreamEvent]:
        tool_name = reported_identity(
            runner_text(event.get("toolName", "")), "tool_name"
        )
        if not tool_name:
            return []
        target = _resolve_tool_target(event.get("args"))
        return [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message=tool_name,
                metadata={"target": target},
            )
        ]

    def _parse_message_end(self, line: str, event: dict[str, Any]) -> list[StreamEvent]:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []

        events: list[StreamEvent] = []

        text = self._assistant_text(message)
        if text:
            events.append(
                StreamEvent(
                    raw_line=line,
                    event_type="activity",
                    message=self._truncate(text),
                )
            )

        model_str = reported_model_identity(message.get("model"))
        input_tokens, output_tokens = self._token_counts(message)
        events.append(
            StreamEvent(
                raw_line=line,
                event_type="usage",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                metadata={"model": model_str} if model_str else {},
            )
        )
        # Paired step per usage-bearing turn → step_count/spent_usd (parity with
        # claude-code). cost_usd carries pi's native per-message figure when
        # present (#1082); the orchestrator sums these into the run's spend and
        # falls back to a token estimate only when it is None.
        if input_tokens or output_tokens:
            events.append(
                StreamEvent(
                    raw_line=line,
                    event_type="step",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    metadata={"cost_usd": self._native_step_cost(message)},
                )
            )

        stop_reason = str(message.get("stopReason", ""))
        if stop_reason in _TERMINAL_STOP_REASONS:
            events.append(
                StreamEvent(
                    raw_line=line,
                    event_type="result",
                    metadata={
                        "session_id": self._session_id or "",
                        "stop_reason": stop_reason,
                        "cost_usd": self._cost(message),
                    },
                )
            )
        return events

    @staticmethod
    def _assistant_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts = [
            runner_text(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts).strip()

    @staticmethod
    def _token_counts(message: dict[str, Any]) -> tuple[int, int]:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return 0, 0
        input_tokens = sum(
            protocol_int(usage.get(key), f"usage.{key}")
            for key in ("input", "cacheRead", "cacheWrite")
        )
        output_tokens = protocol_int(usage.get("output"), "usage.output")
        return input_tokens, output_tokens

    @staticmethod
    def _cost(message: dict[str, Any]) -> float:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return 0.0
        cost = usage.get("cost")
        if not isinstance(cost, dict):
            return 0.0
        return protocol_float(cost.get("total"), "usage.cost.total")

    @classmethod
    def _native_step_cost(cls, message: dict[str, Any]) -> float | None:
        """pi's native per-message cost (``usage.cost.total``), or None.

        Returns ``None`` when the message reports no native cost so the
        orchestrator distinguishes "no figure" from "free" — the summed
        per-message figures are the provider-billed spend (#1082).
        """
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        cost = usage.get("cost")
        if not isinstance(cost, dict) or "total" not in cost:
            return None
        return cls._cost(message)

    @staticmethod
    def _truncate(text: str, limit: int = 200) -> str:
        text = runner_text(text)
        return text[:limit] + "..." if len(text) > limit else text

    # -- classification ----------------------------------------------------

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification:
        stop_reason, error_message = self._parse_final_message(last_output)
        category = self._classify(exit_code, stop_reason, error_message, config)
        return ResultClassification(
            category=category,
            session_id=self._session_id,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _parse_final_message(last_output: str | None) -> tuple[str, str]:
        """Return (stop_reason, error_message) from the final message_end line."""
        if not last_output:
            return "", ""
        try:
            event = _json.loads(last_output.strip())
        except (ValueError, TypeError):
            return "", ""
        if not isinstance(event, dict) or event.get("type") != "message_end":
            return "", ""
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return "", ""
        return str(message.get("stopReason", "")), str(message.get("errorMessage", ""))

    @classmethod
    def _classify(
        cls,
        exit_code: int,
        stop_reason: str,
        error_message: str,
        config: DispatchConfig,
    ) -> str:
        if exit_code == 124:
            return "transient"

        # json mode reports model failures via stopReason, not the exit code.
        if stop_reason in _FAILURE_STOP_REASONS:
            return cls._classify_error_message(error_message)

        if exit_code == 0:
            # A real run ends with a terminal assistant turn (stop/length). A
            # clean exit without one means pi never completed the task — most
            # commonly a startup/credential error, which pi prints as plain text
            # while still exiting 0. Treat that as a permanent failure rather
            # than a successful no-op.
            if stop_reason in _SUCCESS_STOP_REASONS:
                return ""
            return "permanent"

        if exit_code not in config.retry.retriable_exit_codes:
            return "permanent"
        # Retriable exit code with no usable failure signal: assume transient.
        return "transient"

    @staticmethod
    def _classify_error_message(error_message: str) -> str:
        lowered = error_message.lower()
        if any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS):
            return "transient"
        return "permanent"

    def supports_resume(self) -> bool:
        return True

    def enrich_result(
        self,
        result: DispatchResult,
        config: DispatchConfig,
    ) -> None:
        pass  # No-op: message_end events already carry tokens and native cost.
