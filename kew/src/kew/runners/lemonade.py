"""Lemonade runner — kew's first-class local-model backend.

Unlike ``claude-code``/``opencode``/``pi`` (which wrap third-party agent CLIs),
the Lemonade runner drives kew's **own** agent loop, shipped as the
``kew-lemonade`` console script (see :mod:`kew.runners.lemonade_agent`). That
loop talks to a local OpenAI-compatible server — AMD Lemonade at ``:13305/v1``
by default, or any compatible endpoint (llama-server, LM Studio, vLLM,
Ollama's ``/v1``).

The runner shells out to::

    kew-lemonade --base-url <url> --model <name> --session-id <uuid> -p <prompt>

``kew-lemonade`` streams one JSON object per line; the schema mirrors pi's, so
``parse_line`` stays small: a ``session`` header, ``tool_execution_start``
events, and ``message_end`` events carrying ``usage`` and a ``stopReason``.

Local models have no per-token dollar price, so no cost is reported; kew's
``BillingConfig`` frames local inference as ≈free and the outer dispatch budget
gate still applies. Session-resume re-issues the task with the same session id
(the loop is stateless across processes in v1 — see the deferred-gaps note).

Known v1 gaps (deferred):
- Resume re-runs from the task prompt rather than replaying transcript state.
- No mid-run cost/budget passthrough; the outer dispatch budget gate applies
  (same as the OpenCode and pi runners).
"""

from __future__ import annotations

import json as _json
import uuid
from typing import Any

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runner_fields import (
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

_MODEL_PREFIX = "lemonade/"

# Substrings (matched case-insensitively against the loop's errorMessage) that
# mark a failure as worth retrying rather than giving up on.
_TRANSIENT_ERROR_MARKERS = (
    "connection",
    "timed out",
    "timeout",
    "temporarily",
    "503",
    "502",
    "429",
    "overloaded",
    "unavailable",
)

_FAILURE_STOP_REASONS = frozenset({"error", "aborted"})
_SUCCESS_STOP_REASONS = frozenset({"stop", "length"})
_TERMINAL_STOP_REASONS = _SUCCESS_STOP_REASONS | _FAILURE_STOP_REASONS

_RESUME_PROMPT = (
    "The previous run was interrupted before finishing. Inspect the worktree "
    "for progress already made (edited files, partial commits) and continue "
    "until the task is complete, then call finish."
)


def _resolve_tool_target(args: object) -> str:
    """Extract a target path/command from a tool call's args dict."""
    if not isinstance(args, dict):
        return ""
    return runner_text(
        args.get("path") or args.get("file_path") or args.get("command", "") or ""
    )


def _strip_prefix(model: str) -> str:
    """Drop the ``lemonade/`` routing prefix so the endpoint sees a bare name."""
    return model[len(_MODEL_PREFIX) :] if model.startswith(_MODEL_PREFIX) else model


class LemonadeRunner:
    """Runner for the native ``kew-lemonade`` loop over a local endpoint."""

    def __init__(self, session_id: str | None = None) -> None:
        # One runner instance handles one dispatch; the id is stable across the
        # run so classify_result can report it for resume (mirrors PiRunner).
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "lemonade"

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
        """Lemonade talks to a HOST-LOCAL inference server it cannot reach.

        The endpoint lives on the host loopback and the contained network
        profile does not route to it, so a container would come up unable to
        reach its own model server. This is a property of where the server
        runs, not a missing image — mapping one would produce a container that
        starts and then fails every request.
        """
        return (
            "lemonade drives a host-local inference server that a contained "
            "network cannot reach, so it can only run on the host"
        )

    @property
    def transport_capability(self) -> RunnerTransportCapability:
        """Refuse layering Switchyard over Kew's native endpoint in v1."""
        return RunnerTransportCapability()

    @property
    def required_credentials(self) -> tuple[str, ...]:
        """None — and therefore not containable (#1146).

        Lemonade authenticates to a HOST-LOCAL endpoint with a key that comes
        from ``[agent.lemonade] api_key`` in the config, not from the host
        environment, and that endpoint is not reachable from the restricted
        container network anyway. An empty tuple is the honest declaration, and
        it makes ``resolve_effective_sandbox`` refuse to contain lemonade rather
        than launch a container that cannot reach its own server. Wiring
        containment for a local endpoint is a separate change, not a key here.
        """
        return ()

    def _ensure_session_id(self) -> str:
        if self._session_id is None:
            self._session_id = str(uuid.uuid4())
        return self._session_id

    def _base_cmd(
        self, config: DispatchConfig, model: str, env: dict[str, str]
    ) -> list[str]:
        lemo = config.agent.lemonade
        effective_model = _strip_prefix(model or config.agent.model)
        cmd = [
            "kew-lemonade",
            "--base-url",
            lemo.base_url,
            "--max-steps",
            str(lemo.max_steps),
        ]
        if effective_model:
            cmd.extend(["--model", effective_model])
        if lemo.api_key:
            # Pass the key via the environment, not argv, so it never lands in
            # the host process list. The agent reads LEMONADE_API_KEY.
            env["LEMONADE_API_KEY"] = lemo.api_key
        cmd.extend(lemo.extra_args)
        return cmd

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """Sandbox ``"$@"`` args — see :meth:`AgentRunner.container_args`.

        ``--session-id`` is dropped for the same reason as pi's (a host-side
        resume handle), and the API key still travels by env, never argv — the
        per-runner in-container env allowlist is #779 N5.
        """
        lemo = config.agent.lemonade
        args = ["--base-url", lemo.base_url, "--max-steps", str(lemo.max_steps)]
        stripped = _strip_prefix(model)
        if stripped:
            args.extend(["--model", stripped])
        args.extend(lemo.extra_args)
        return args

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        cmd = self._base_cmd(config, model, env)
        cmd.extend(["--session-id", self._ensure_session_id()])
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
        self._session_id = session_id
        cmd = self._base_cmd(config, model, env)
        cmd.extend(["--session-id", session_id])
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
        return [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message=tool_name,
                metadata={"target": _resolve_tool_target(event.get("args"))},
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
        if input_tokens or output_tokens:
            events.append(
                StreamEvent(
                    raw_line=line,
                    event_type="step",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    metadata={"cost_usd": None},
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
                        # Local inference has no per-token dollar price.
                        "cost_usd": 0.0,
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
        return protocol_int(usage.get("input"), "usage.input"), protocol_int(
            usage.get("output"), "usage.output"
        )

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
        # Classify original protocol fields before sanitizing retained identities.
        category = self._classify(exit_code, stop_reason, error_message, config)
        return ResultClassification(
            category=category,
            session_id=self._session_id,
            stop_reason=reported_identity(stop_reason, "stop_reason"),
        )

    @staticmethod
    def _parse_final_message(last_output: str | None) -> tuple[str, str]:
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
        if stop_reason in _FAILURE_STOP_REASONS:
            return cls._classify_error_message(error_message)
        if exit_code == 0:
            if stop_reason in _SUCCESS_STOP_REASONS:
                return ""
            return "permanent"
        if exit_code not in config.retry.retriable_exit_codes:
            return "permanent"
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
        pass  # message_end events already carry token usage; cost is ≈0 (local).
