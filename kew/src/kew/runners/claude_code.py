"""Claude Code runner implementation."""

from __future__ import annotations

import json as _json
from typing import Any

from kew.config import DispatchConfig
from kew.inference_transport import RunnerTransportCapability
from kew.progress import display_event, parse_stream_event
from kew.runner_fields import (
    protocol_float,
    protocol_int,
    reported_identity,
    reported_model_identity,
    runner_text,
)
from kew.runner_quota import (
    QUOTA_EVENT_TYPE,
    QUOTA_REJECTION_KEY,
    QUOTA_WARNING_EVENT_TYPE,
    QUOTA_WARNING_KEY,
    QuotaRejection,
    rejection_from_rate_limit_event,
    rejection_from_result_event,
    warning_from_rate_limit_event,
)
from kew.runners.protocol import (
    RUN_TOTALS_KEY,
    ContainmentPosture,
    LoopStyle,
    PublicationStyle,
    ResultClassification,
    RunTotals,
    StreamEvent,
)
from kew.tracking import DispatchResult

_TRANSIENT_ERROR_TYPES = {"api_error", "overloaded_error", "rate_limit_error"}

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


def _coerce_int(value: object) -> int:
    """Best-effort int from an untrusted JSON field — non-numeric counts as zero."""
    return protocol_int(value, "usage.tokens") if isinstance(value, (int, float)) else 0


def _model_usage_totals(model_usage: object) -> RunTotals | None:
    """Sum a result event's per-model ``modelUsage`` block, or None if absent.

    Cost and tokens sum across models (a sonnet worker plus a haiku sub-agent is
    one run); the busiest model — highest ``costUSD`` — names it, matching the
    single ``resolved_model`` a host run records.
    """
    if not isinstance(model_usage, dict):
        return None
    totals = RunTotals()
    cost = 0.0
    best_cost = -1.0
    seen = False
    for name, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        seen = True
        raw = usage.get("costUSD")
        model_cost = (
            protocol_float(raw, "modelUsage.costUSD")
            if isinstance(raw, (int, float))
            else 0.0
        )
        cost += model_cost
        totals.input_tokens += _coerce_int(usage.get("inputTokens"))
        totals.output_tokens += _coerce_int(usage.get("outputTokens"))
        totals.cache_creation_tokens += _coerce_int(
            usage.get("cacheCreationInputTokens")
        )
        totals.cache_read_tokens += _coerce_int(usage.get("cacheReadInputTokens"))
        if isinstance(name, str) and model_cost > best_cost:
            best_cost = model_cost
            totals.model = reported_model_identity(name)
            totals.model_display = runner_text(name)
    if not seen:
        return None
    totals.cost_usd = cost or None
    return totals


def _fill_usage_fallback(totals: RunTotals, usage: object) -> None:
    """Populate *totals* from the result event's top-level ``usage`` block.

    Only reached when ``modelUsage`` supplied no input/output tokens, so a bare
    result event with no per-model breakdown still records what it carries.
    """
    if not isinstance(usage, dict):
        return
    totals.input_tokens = _coerce_int(usage.get("input_tokens"))
    totals.output_tokens = _coerce_int(usage.get("output_tokens"))
    totals.cache_creation_tokens = _coerce_int(usage.get("cache_creation_input_tokens"))
    totals.cache_read_tokens = _coerce_int(usage.get("cache_read_input_tokens"))


def _result_run_totals(event: dict[str, Any]) -> RunTotals:
    """The whole-run totals a claude ``result`` event restates (#761/#1147).

    ``modelUsage`` is authoritative; ``total_cost_usd``/``usage`` are the
    fallbacks for a bare result event. Always returned, never None — a claude
    result event IS the run summary, so an empty one means "this run reported
    nothing", which is a different claim from "this runner sends no summary"
    (see :class:`RunTotals`).
    """
    totals = _model_usage_totals(event.get("modelUsage")) or RunTotals()
    if totals.cost_usd is None:
        raw = event.get("total_cost_usd")
        if isinstance(raw, (int, float)):
            totals.cost_usd = protocol_float(raw, "total_cost_usd")
    if not (totals.input_tokens or totals.output_tokens):
        _fill_usage_fallback(totals, event.get("usage"))
    return totals


def _quota_events(line: str, rejection: QuotaRejection | None) -> list[StreamEvent]:
    """The provider refusing this run for quota, as a typed event (#2643).

    Two shapes say it: a ``rate_limit_event`` whose ``rate_limit_info.status``
    is ``rejected`` (carrying the limit and its reset), and the CLI's
    out-of-credits error result. Either reaches the stream fold as one
    ``quota_exhausted`` event, so dispatch can tell a refused run from a failed
    one without re-reading claude-shaped JSON itself.
    """
    if rejection is None:
        return []
    return [
        StreamEvent(
            raw_line=line,
            event_type=QUOTA_EVENT_TYPE,
            metadata={QUOTA_REJECTION_KEY: rejection.with_runner("claude-code")},
        )
    ]


def _rate_limit_events(line: str, event: dict[str, Any]) -> list[StreamEvent]:
    """A ``rate_limit_event`` as a typed refusal or warning event, or nothing.

    ``rejected`` is the refusal above; ``allowed_warning`` is the window filling
    up (#3329) and becomes one ``quota_warning`` event, so the stream folds can
    record it before the refusal stalls the fleet. ``allowed`` is routine.
    """
    warning = warning_from_rate_limit_event(event)
    if warning is None:
        return _quota_events(line, rejection_from_rate_limit_event(event))
    return [
        StreamEvent(
            raw_line=line,
            event_type=QUOTA_WARNING_EVENT_TYPE,
            metadata={QUOTA_WARNING_KEY: warning.with_runner("claude-code")},
        )
    ]


class ClaudeCodeRunner:
    """Runner for Claude Code CLI."""

    @property
    def name(self) -> str:
        return "claude-code"

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
        """Claude's documented child-local Anthropic Messages overrides."""
        return RunnerTransportCapability(
            incoming_api_formats=("anthropic-messages",),
            endpoint_override=True,
            model_override=True,
        )

    @property
    def required_credentials(self) -> tuple[str, ...]:
        """Anthropic's two container-forwardable auth paths (#1146).

        ``CLAUDE_CODE_OAUTH_TOKEN`` is the subscription path (minted once via
        ``claude setup-token``); ``ANTHROPIC_API_KEY`` is the API-account path.
        CLI login (macOS Keychain / ``~/.claude``) can never reach a container —
        ``~/.claude`` is deliberately not mounted — so one of these env names is
        the ONLY auth path in (#721).
        """
        return ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

    def _agent_flags(self, config: DispatchConfig) -> list[str]:
        """The flags every claude invocation carries, host or contained."""
        flags = []
        if config.agent.claude_code.skip_permissions:
            flags.append("--dangerously-skip-permissions")
        flags.extend(["--verbose", "--output-format", "stream-json"])
        return flags

    def container_args(
        self,
        *,
        config: DispatchConfig,
        model: str = "",
    ) -> list[str]:
        """Sandbox ``"$@"`` args — see :meth:`AgentRunner.container_args`.

        No ``-p``/prompt (the image's entrypoint feeds the mounted prompt file
        to stdin). The run cap rides in the same position as on the host
        (#1462); a caller holding a runtime override hands in a config copy
        carrying it (``dispatch_sandbox.config_with_run_cap``), so
        ``max_remediation_usd`` wins in the container as it does on the host.
        """
        args = self._agent_flags(config)
        if config.agent.max_budget_usd:
            args.extend(["--max-budget-usd", str(config.agent.max_budget_usd)])
        # The ROUTED model, with no config fallback — the caller already
        # resolved the effective model, and re-deriving one here is how routing
        # stops reaching the container (#761).
        if model:
            args.extend(["--model", model])
        return args

    def _build_base_cmd(
        self,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
        budget_override_usd: float | None = None,
    ) -> list[str]:
        """Build common claude CLI flags and set env vars."""
        cmd = ["claude", *self._agent_flags(config)]
        budget = (
            budget_override_usd
            if budget_override_usd is not None
            else config.agent.max_budget_usd
        )
        if budget:
            cmd.extend(["--max-budget-usd", str(budget)])
        effective_model = model or config.agent.model
        if effective_model:
            cmd.extend(["--model", effective_model])
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(
            config.agent.claude_code.autocompact_pct
        )
        if config.inference_transport.mode == "switchyard":
            env["ANTHROPIC_BASE_URL"] = config.inference_transport.switchyard.endpoint
        return cmd

    def build_command(
        self,
        *,
        prompt: str,
        config: DispatchConfig,
        env: dict[str, str],
        model: str = "",
        budget_override_usd: float | None = None,
    ) -> list[str]:
        cmd = self._build_base_cmd(config, env, model, budget_override_usd)
        cmd.extend(["-p", prompt])
        return cmd

    def parse_line(self, line: str) -> list[StreamEvent]:
        event = parse_stream_event(line)
        # A well-formed JSON line need not be an OBJECT: agent stdout is another
        # process's output, and a bare array/scalar would otherwise reach
        # ``display_event``'s ``.get`` and raise out of the caller's read loop.
        if not isinstance(event, dict):
            return []
        display_event(event)
        event_type = event.get("type", "")

        if event_type == "assistant":
            return self._parse_assistant_event(line, event)

        if event_type == "tool_use":
            tool_name = reported_identity(
                runner_text(event.get("tool", "")), "tool_name"
            )
            return [
                StreamEvent(
                    raw_line=line,
                    event_type="tool_use",
                    message=tool_name,
                    metadata={"target": _resolve_tool_target(event.get("input", {}))},
                )
            ]

        if event_type == "system":
            return self._parse_system_event(line, event)

        if event_type == "rate_limit_event":
            return _rate_limit_events(line, event)

        if event_type == "result":
            meta: dict[str, object] = {
                "session_id": reported_identity(
                    runner_text(event["session_id"]), "session_id"
                )
                if event.get("session_id")
                else None,
                "subtype": event.get("subtype", ""),
                "stop_reason": event.get("stop_reason", ""),
                # The run summary, in protocol terms, so consumers that
                # accumulate per-turn events (the sandbox telemetry path) can
                # take the authoritative totals without re-reading claude-shaped
                # JSON fields of their own (#1147). Only a result event carries
                # the run summary, so only a result event is parsed for it.
                RUN_TOTALS_KEY: _result_run_totals(event),
            }
            if "total_cost_usd" in event:
                meta["total_cost_usd"] = event["total_cost_usd"]
            return [
                StreamEvent(
                    raw_line=line,
                    event_type="result",
                    metadata=meta,
                ),
                *_quota_events(line, rejection_from_result_event(event)),
            ]

        return []

    def _parse_assistant_event(
        self, line: str, event: dict[str, Any]
    ) -> list[StreamEvent]:
        raw_msg = event.get("message", "")
        results: list[StreamEvent] = []

        # List of blocks (legacy format) — handle per block type
        if isinstance(raw_msg, list):
            for block in raw_msg:
                if isinstance(block, str):
                    stripped = runner_text(block).strip()
                    if stripped:
                        if len(stripped) > 200:
                            stripped = stripped[:200] + "..."
                        results.append(
                            StreamEvent(
                                raw_line=line,
                                event_type="activity",
                                message=stripped,
                            )
                        )
                elif isinstance(block, dict):
                    block_type = block.get("type", "")
                    if block_type == "tool_use":
                        results.append(
                            StreamEvent(
                                raw_line=line,
                                event_type="tool_use",
                                message=reported_identity(
                                    runner_text(block.get("name", "")), "tool_name"
                                ),
                                metadata={
                                    "target": _resolve_tool_target(
                                        block.get("input", {})
                                    ),
                                },
                            )
                        )
                    elif block_type == "thinking":
                        pass  # suppress thinking blocks
                    else:
                        text = runner_text(
                            block.get("text") or block.get("content") or ""
                        ).strip()
                        if text:
                            if len(text) > 200:
                                text = text[:200] + "..."
                            results.append(
                                StreamEvent(
                                    raw_line=line,
                                    event_type="activity",
                                    message=text,
                                )
                            )
            return results

        if not isinstance(raw_msg, dict):
            # Simple string message — activity only
            msg = runner_text(raw_msg or "")
            if len(msg) > 200:
                msg = msg[:200] + "..."
            if msg:
                results.append(
                    StreamEvent(
                        raw_line=line,
                        event_type="activity",
                        message=msg,
                    )
                )
            return results

        # Dict message — extract content blocks first, then usage
        content = raw_msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                if block_type == "tool_use":
                    results.append(
                        StreamEvent(
                            raw_line=line,
                            event_type="tool_use",
                            message=reported_identity(
                                runner_text(block.get("name", "")), "tool_name"
                            ),
                            metadata={
                                "target": _resolve_tool_target(block.get("input", {})),
                            },
                        )
                    )
                elif block_type == "text":
                    text = runner_text(block.get("text", "")).strip()
                    if text:
                        if len(text) > 200:
                            text = text[:200] + "..."
                        results.append(
                            StreamEvent(
                                raw_line=line,
                                event_type="activity",
                                message=text,
                            )
                        )
                # "thinking" blocks are intentionally skipped
        elif isinstance(content, str) and content.strip():
            msg = runner_text(content).strip()
            if len(msg) > 200:
                msg = msg[:200] + "..."
            results.append(
                StreamEvent(
                    raw_line=line,
                    event_type="activity",
                    message=msg,
                )
            )

        # Usage — always appended last, followed by the step boundary event.
        usage = raw_msg.get("usage", {})
        if isinstance(usage, dict) and any(
            usage.get(k) is not None
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        ):
            # Cache tokens are kept separate so they can be priced at their
            # real (discounted) rates downstream rather than folded into input
            # at the full input rate (#255).
            input_tokens = protocol_int(usage.get("input_tokens"), "usage.input_tokens")
            cache_creation = protocol_int(
                usage.get("cache_creation_input_tokens"),
                "usage.cache_creation_input_tokens",
            )
            cache_read = protocol_int(
                usage.get("cache_read_input_tokens"), "usage.cache_read_input_tokens"
            )
            output_tokens = protocol_int(
                usage.get("output_tokens"), "usage.output_tokens"
            )
            results.append(
                StreamEvent(
                    raw_line=line,
                    event_type="usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                )
            )
            # One step per usage-bearing assistant event — the natural
            # model-response unit and the loop's iteration boundary (spec R13).
            # cost_usd is best-effort; the parser has no pricing context so it
            # is None here and computed downstream by the driver/orchestrator.
            results.append(
                StreamEvent(
                    raw_line=line,
                    event_type="step",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_creation_tokens=cache_creation,
                    cache_read_tokens=cache_read,
                    metadata={"cost_usd": None},
                )
            )

        # If dict message had no content and no usage, try fallback text.
        # Skip when content was a list — all-filtered lists (e.g. thinking-only)
        # must not be stringified and re-emitted as activity events.
        if not results and not isinstance(content, list):
            msg = runner_text(raw_msg.get("content", ""))
            if len(msg) > 200:
                msg = msg[:200] + "..."
            if msg:
                results.append(
                    StreamEvent(
                        raw_line=line,
                        event_type="activity",
                        message=msg,
                    )
                )

        return results

    def _parse_system_event(
        self, line: str, event: dict[str, Any]
    ) -> list[StreamEvent]:
        subtype = event.get("subtype", "")

        # Init event — return model metadata + session_id for early capture
        if subtype == "init":
            return [
                StreamEvent(
                    raw_line=line,
                    event_type="init",
                    metadata={
                        "model": reported_model_identity(event.get("model", "")),
                        "session_id": reported_identity(
                            runner_text(event["session_id"]), "session_id"
                        )
                        if event.get("session_id")
                        else None,
                    },
                )
            ]

        # Autocompaction boundary — the CLI rewrote its context, adding a
        # cache-creation spike. Surface it so the breaker can discount the
        # compaction overhead before judging a cost anomaly (#859).
        if subtype == "compact_boundary":
            metadata = event.get("compact_metadata")
            trigger = metadata.get("trigger", "") if isinstance(metadata, dict) else ""
            return [
                StreamEvent(
                    raw_line=line,
                    event_type="compaction",
                    metadata={"trigger": trigger},
                )
            ]

        # Legacy usage from system events
        usage = event.get("usage", {})
        if isinstance(usage, dict) and any(
            usage.get(k) is not None
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        ):
            return [
                StreamEvent(
                    raw_line=line,
                    event_type="usage",
                    input_tokens=(
                        protocol_int(usage.get("input_tokens"), "usage.input_tokens")
                    ),
                    output_tokens=(
                        protocol_int(usage.get("output_tokens"), "usage.output_tokens")
                    ),
                    cache_creation_tokens=(
                        protocol_int(
                            usage.get("cache_creation_input_tokens"),
                            "usage.cache_creation_input_tokens",
                        )
                    ),
                    cache_read_tokens=(
                        protocol_int(
                            usage.get("cache_read_input_tokens"),
                            "usage.cache_read_input_tokens",
                        )
                    ),
                    tool_uses=(usage.get("tool_uses") or 0),
                )
            ]

        return []

    def classify_result(
        self,
        *,
        exit_code: int,
        last_output: str | None,
        config: DispatchConfig,
    ) -> ResultClassification:
        result_json = self._parse_result_json(last_output)
        # Classify original protocol fields before sanitizing retained identities.
        category = self._classify_error(
            exit_code,
            result_json,
            config.retry.retriable_exit_codes,
            config.agent.max_budget_usd,
        )
        session_id = result_json.get("session_id") if result_json else None
        subtype = result_json.get("subtype", "") if result_json else ""
        stop_reason = result_json.get("stop_reason", "") if result_json else ""
        return ResultClassification(
            category=category,
            session_id=reported_identity(session_id, "session_id")
            if isinstance(session_id, str)
            else None,
            subtype=reported_identity(subtype, "subtype")
            if isinstance(subtype, str)
            else "",
            stop_reason=reported_identity(stop_reason, "stop_reason")
            if isinstance(stop_reason, str)
            else "",
        )

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
        budget_override_usd: float | None = None,
    ) -> list[str]:
        cmd = self._build_base_cmd(config, env, model, budget_override_usd)
        cmd.extend(["--resume", session_id])
        cmd.extend(["-p", _RESUME_PROMPT])
        return cmd

    def enrich_result(
        self,
        result: DispatchResult,
        config: DispatchConfig,
    ) -> None:
        pass  # Stream provides full usage data

    @staticmethod
    def _parse_result_json(last_output: str | None) -> dict[str, Any] | None:
        if last_output is None:
            return None
        line = last_output.strip()
        if not line:
            return None
        try:
            data = _json.loads(line)
        except (ValueError, TypeError):
            return None
        if isinstance(data, dict) and data.get("type") == "result":
            return data
        return None

    @staticmethod
    def _classify_error(
        exit_code: int,
        result_json: dict[str, Any] | None,
        retriable_exit_codes: list[int],
        budget_cap_usd: float | None = None,
    ) -> str:
        # Cost-aware budget guard (#642): a `--max-budget-usd` termination can
        # surface without a budget subtype — a plain nonzero exit, or the
        # SIGTERM-143 shape that otherwise reads as an operator kill. Whenever
        # the result reports cost at/over the cap, classify it as a budget kill
        # regardless of subtype or exit code so the retry ladder never re-runs a
        # deterministic budget kill. 143/137 with no cost signal falls through
        # to "watchdog_kill" below, keeping operator kills resumable.
        if budget_cap_usd:
            cost = result_json.get("total_cost_usd") if result_json else None
            if isinstance(cost, (int, float)) and cost >= budget_cap_usd:
                return "budget_exceeded"
        if exit_code == 0 and result_json is not None:
            subtype = result_json.get("subtype", "")
            if isinstance(subtype, str) and "budget" in subtype:
                return "budget_exceeded"
        if exit_code == 0:
            return ""
        if exit_code == 124:
            return "transient"
        # 128+SIGTERM(15)=143, 128+SIGKILL(9)=137: our own _terminate_group
        # sends these when the budget watchdog trips.  Classify as
        # "watchdog_kill" so the retry loop can auto-resume instead of
        # treating it as a permanent failure (#401).
        if exit_code in (143, 137):
            return "watchdog_kill"
        if exit_code not in retriable_exit_codes:
            return "permanent"
        if result_json is None:
            return "transient"
        result_str = result_json.get("result", "")
        if isinstance(result_str, str):
            for transient_type in _TRANSIENT_ERROR_TYPES:
                if transient_type in result_str:
                    return "transient"
            if "_error" in result_str:
                return "permanent"
        return "transient"
