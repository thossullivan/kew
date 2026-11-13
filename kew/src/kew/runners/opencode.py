"""OpenCode runner implementation.

OpenCode is a multi-provider terminal coding agent. This runner shells out to
`opencode run --format json` to dispatch jobs to any provider OpenCode
supports (Anthropic, OpenAI, Google, Ollama, OpenRouter, 70+ others).

Provider credentials are managed by OpenCode itself via `opencode auth login
<provider>` — kew does not handle API keys for OpenCode-routed providers.
Missing credentials surface as permanent failures with OpenCode's own error
message.

Known v1 gaps (deferred):
- No native `--max-budget-usd` flag; outer dispatch budget gates apply.
- No `--attach <url>` server mode; per-job spawn only.
"""

from __future__ import annotations

import json as _json
from typing import Any, NamedTuple

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.runner_fields import (
    protocol_float,
    protocol_int,
    reported_identity,
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

_TRANSIENT_ERROR_NAMES = frozenset(
    {"RateLimitError", "OverloadedError", "APIError", "NetworkError"}
)

_RESUME_PROMPT = (
    "The previous run was interrupted by a transient API error. "
    "Please continue where you left off. Check what progress was made "
    "(commits, file changes) and resume the task."
)


def _resolve_tool_target(tool_input: object) -> str:
    """Extract a target path/command from a tool_use input dict."""
    if not isinstance(tool_input, dict):
        return ""
    return runner_text(
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("command", "")
        or ""
    )


class _StepTokens(NamedTuple):
    """The four-way token split carried by one step-finish part."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def any_tokens(self) -> bool:
        """True when the step reported usage of any kind."""
        return bool(
            self.input_tokens
            or self.output_tokens
            or self.cache_read_tokens
            or self.cache_creation_tokens
        )


class OpenCodeRunner:
    """Runner for the OpenCode CLI (sst/opencode)."""

    @property
    def transport_capability(self) -> RunnerTransportCapability:
        """Refuse Switchyard until OpenCode's isolation contract is proven.

        The pinned compatibility audit did not prove a run-owned provider
        configuration with deterministic selection and cleanup. An empty
        capability makes that refusal explicit without changing direct mode.
        """
        return RunnerTransportCapability()

    @property
    def name(self) -> str:
        return "opencode"

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
    def required_credentials(self) -> tuple[str, ...]:
        """OpenRouter only — the namespace kew routes to opencode (#1146).

        Deliberately NARROW. OpenCode itself speaks to 70+ providers, but on the
        host it authenticates through its own ``opencode auth login`` credential
        store, which is host-only and never mounted into a container. The one
        provider a *contained* opencode dispatch reaches is OpenRouter (kew's
        ``openrouter/`` model namespace resolves here), so that is the only key
        the allowlist forwards. Adding ``OPENAI_API_KEY`` /
        ``GEMINI_API_KEY`` / … here would hand every contained opencode run
        keys no kew-routed dispatch needs — a wider exfiltration surface for no
        capability. Add one only alongside a dispatch path that requires it.
        """
        return ("OPENROUTER_API_KEY",)

    def _agent_flags(self, config: DispatchConfig, model: str) -> list[str]:
        """Everything after the binary: subcommand, output format, routing."""
        args = ["run", "--format", "json"]
        if model:
            args.extend(["-m", model])
        if config.agent.opencode.agent:
            args.extend(["--agent", config.agent.opencode.agent])
        args.extend(config.agent.opencode.extra_args)
        return args

    def _build_base_cmd(self, config: DispatchConfig, model: str = "") -> list[str]:
        """Common flags shared by run and resume commands."""
        return ["opencode", *self._agent_flags(config, model or config.agent.model)]

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """Sandbox ``"$@"`` args — see :meth:`AgentRunner.container_args`.

        ``build_command`` minus the ``opencode`` binary and the positional
        prompt (which the image feeds from the mounted file on stdin). The
        ``run`` subcommand stays: it names the invocation, and the entrypoint
        contract forwards ``"$@"`` unmodified.
        """
        return self._agent_flags(config, model)

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        cmd = self._build_base_cmd(config, model)
        cmd.append(prompt)
        return cmd

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

        if event_type == "text":
            return self._parse_text_event(line, event)

        if event_type == "tool_use":
            return self._parse_tool_use_event(line, event)

        if event_type == "step_finish":
            return self._parse_step_finish_event(line, event)

        return []

    @staticmethod
    def _truncate(text: str, limit: int = 200) -> str:
        text = runner_text(text)
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    def _parse_text_event(self, line: str, event: dict[str, Any]) -> list[StreamEvent]:
        part = event.get("part", {})
        if not isinstance(part, dict):
            return []
        text = runner_text(part.get("text", "")).strip()
        if not text:
            return []
        return [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message=self._truncate(text),
            )
        ]

    def _parse_tool_use_event(
        self, line: str, event: dict[str, Any]
    ) -> list[StreamEvent]:
        part = event.get("part", {})
        if not isinstance(part, dict):
            return []
        raw_tool = part.get("tool")
        if not raw_tool:
            return []
        tool_name = reported_identity(runner_text(raw_tool), "tool_name")
        state = part.get("state", {}) if isinstance(part.get("state"), dict) else {}
        if state.get("status") != "completed":
            return []
        target = _resolve_tool_target(state.get("input", {}))
        return [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message=tool_name,
                metadata={"target": target},
            )
        ]

    @staticmethod
    def _dict_field(obj: dict[str, Any], key: str) -> dict[str, Any]:
        """Return obj[key] when it is a dict, else an empty dict."""
        value = obj.get(key)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _native_step_cost(part: dict[str, Any]) -> float | None:
        """The runner's native per-step cost from a step-finish part.

        Returns ``None`` (not ``0.0``) when the runner reports no ``cost`` so
        the orchestrator can tell "no native figure" from "genuinely free" —
        summing these per-step figures is the provider-billed truth (#1082),
        vs. the paired ``usage`` event's token-bound estimate.
        """
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            return protocol_float(cost, "cost")
        return None

    @classmethod
    def _extract_token_counts(cls, part: dict[str, Any]) -> _StepTokens:
        """Return the token split carried by a step-finish part.

        opencode reports *uncached* input in ``tokens.input`` and cache traffic
        separately under ``tokens.cache`` — ``tokens.total`` is the sum of
        input, output, reasoning, and both cache legs. Keep the legs apart
        rather than folding them into ``input_tokens``: summing them reports
        every opencode run as 0% cached, which is what #1564 was. The codex
        runner splits the same way.
        """
        tokens = cls._dict_field(part, "tokens")
        cache = cls._dict_field(tokens, "cache")
        return _StepTokens(
            input_tokens=protocol_int(tokens.get("input"), "tokens.input"),
            output_tokens=sum(
                protocol_int(tokens.get(key), f"tokens.{key}")
                for key in ("output", "reasoning")
            ),
            cache_read_tokens=protocol_int(cache.get("read"), "tokens.cache.read"),
            cache_creation_tokens=protocol_int(
                cache.get("write"), "tokens.cache.write"
            ),
        )

    @staticmethod
    def _build_result_event(
        line: str, event: dict[str, Any], part: dict[str, Any]
    ) -> StreamEvent | None:
        """Build the terminal result event when the step reason is 'stop'."""
        reason = str(part.get("reason", ""))
        if reason != "stop":
            return None
        return StreamEvent(
            raw_line=line,
            event_type="result",
            metadata={
                "session_id": reported_identity(
                    runner_text(event.get("sessionID", "")), "session_id"
                ),
                "stop_reason": reason,
                "cost_usd": protocol_float(part.get("cost"), "cost"),
            },
        )

    def _parse_step_finish_event(
        self, line: str, event: dict[str, Any]
    ) -> list[StreamEvent]:
        part = event.get("part", {})
        if not isinstance(part, dict):
            return []
        counts = self._extract_token_counts(part)

        events: list[StreamEvent] = [
            StreamEvent(
                raw_line=line,
                event_type="usage",
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                cache_read_tokens=counts.cache_read_tokens,
                cache_creation_tokens=counts.cache_creation_tokens,
            )
        ]
        # Paired step per usage-bearing turn → step_count/spent_usd (parity with
        # claude-code). cost_usd carries opencode's native per-step figure when
        # present (#1082); the orchestrator sums these into the run's spend and
        # falls back to a token estimate only when it is None.
        if counts.any_tokens:
            events.append(
                StreamEvent(
                    raw_line=line,
                    event_type="step",
                    input_tokens=counts.input_tokens,
                    output_tokens=counts.output_tokens,
                    cache_read_tokens=counts.cache_read_tokens,
                    cache_creation_tokens=counts.cache_creation_tokens,
                    metadata={"cost_usd": self._native_step_cost(part)},
                )
            )

        result_event = self._build_result_event(line, event, part)
        if result_event is not None:
            events.append(result_event)

        return events

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification:
        last_event = self._parse_last_event(last_output)
        # Classify original protocol fields before sanitizing retained identities.
        category = self._classify_error(exit_code, last_event, config)
        session_id = self._extract_session_id(last_event)
        stop_reason = self._extract_stop_reason(last_event)

        return ResultClassification(
            category=category,
            session_id=session_id,
            stop_reason=reported_identity(stop_reason, "stop_reason"),
        )

    @staticmethod
    def _parse_last_event(last_output: str | None) -> dict[str, Any] | None:
        if not last_output:
            return None
        line = last_output.strip()
        if not line:
            return None
        try:
            data = _json.loads(line)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _extract_session_id(event: dict[str, Any] | None) -> str | None:
        if not event:
            return None
        sid = event.get("sessionID")
        return (
            reported_identity(sid, "session_id")
            if isinstance(sid, str) and sid
            else None
        )

    @staticmethod
    def _extract_stop_reason(event: dict[str, Any] | None) -> str:
        if not event:
            return ""
        if event.get("type") != "step_finish":
            return ""
        part = event.get("part", {})
        if not isinstance(part, dict):
            return ""
        return str(part.get("reason", ""))

    @staticmethod
    def _classify_error(
        exit_code: int,
        last_event: dict[str, Any] | None,
        config: DispatchConfig,
    ) -> str:
        if exit_code == 0:
            return ""
        if exit_code == 124:
            return "transient"
        if exit_code not in config.retry.retriable_exit_codes:
            return "permanent"
        # Retriable exit code: look for a transient error event in last_output
        if not last_event or last_event.get("type") != "error":
            return "transient"
        error = last_event.get("error", {})
        if not isinstance(error, dict):
            return "transient"
        error_name = str(error.get("name", ""))
        if error_name in _TRANSIENT_ERROR_NAMES:
            return "transient"
        return "permanent"

    def supports_resume(self) -> bool:
        return True

    def build_resume_command(
        self,
        *,
        session_id: str,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
    ) -> list[str]:
        cmd = self._build_base_cmd(config, model)
        cmd.extend(["--session", session_id])
        cmd.append(_RESUME_PROMPT)
        return cmd

    def enrich_result(
        self,
        result: DispatchResult,
        config: DispatchConfig,
    ) -> None:
        pass  # No-op: step_finish events already carry cost/tokens
