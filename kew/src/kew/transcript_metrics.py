"""Derive sanitized run evidence from a dispatch stream — and keep nothing else.

Kew reads a provider's stream-json to learn two things a receipt needs: the
*measurement substrate* the context-memory epic depends on — **steps-to-first-
edit** and **tokens-to-first-edit** — and the agent's own bounded **Dispatch
Report**. Both used to be recovered by *replaying the raw transcript from disk*
after the run, which meant the raw transcript had to be kept: private
reasoning, quoted source, whatever credential or absolute path the provider
echoed back, all of it durable, all of it later re-ingestible as free text.

This module is the other half of that trade (I06T). Everything is derived
**while streaming** by :class:`StreamDerivation`, one line at a time, and each
raw line is discarded as soon as it has been folded in. What persists is:

* a **sanitized operational log** — one structural line per event
  (``step 3 tokens=155 tools=Edit``), never a byte of provider prose, so an
  operator can still watch a run without the transcript existing;
* a **derived evidence sidecar** beside that log
  (:func:`evidence_path_for`), owner-only, holding exactly the closed fact set
  :class:`DerivedRunEvidence` declares — first-edit metrics, per-tool counts,
  terminal event type, runner token/cost totals, and the bounded report.

Consumers read the sidecar. Nothing reads the log back: a run whose sidecar is
missing — every run recorded before this landed — reports **unavailable**
(``None``), never a number guessed from a legacy raw transcript. That is the
point. A historical log is history; it is not evidence, and re-parsing it would
reintroduce exactly the free-text ingest this module exists to end.

A "step" here is one usage-bearing assistant message — the same model-response
unit the runner counts as a ``step`` StreamEvent (``runners/claude_code.py``),
so the two never diverge. The token sum is the running total of
``input + output + cache_creation + cache_read`` tokens across every step up to
*and including* the step whose message contains the first ``Edit``/``Write``
``tool_use``. OpenCode error events retain only their bounded error name and
valid HTTP status. Provider messages, response bodies, and headers are dropped.

Zero-edit runs (a run that opened no edit — e.g. a lemonade/30B completion-rate
probe) stay **NULL**, never ``0``: ``FirstEditMetrics.none()`` carries two
``None`` fields so the persisted columns distinguish "measured, edited at step
N" from "measured, never edited".
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from kew.credential_surface import redact_credentials
from kew.runner_fields import (
    REDACTED_IDENTITY,
    is_fixed_identity,
    needs_human_reason,
    redact_authorization_values,
    reported_identity,
    reported_model_identity,
    runner_text,
    service_tier_token,
)
from kew.runner_quota import (
    QUOTA_EVENT_TYPE,
    QUOTA_REJECTION_KEY,
    QUOTA_WARNING_EVENT_TYPE,
    QUOTA_WARNING_KEY,
    QuotaRejection,
    QuotaWarning,
)
from kew.tracking import MAX_RESOURCE_CAP_OBSERVATIONS

_logger = logging.getLogger(__name__)

#: The tool names that count as a first *edit*. ``Edit`` and ``Write`` are the
#: two file-mutating Claude Code tools; ``MultiEdit`` (a batched ``Edit``) is
#: folded in for forward-compat with runners that emit it.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})

#: The usage keys that mark an assistant message as a real model-response step
#: (mirrors the ``any(usage.get(k) is not None ...)`` gate in claude_code.py).
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

DISPATCH_REPORT_MARKER = "## Dispatch Report"
"""The marker that makes a terminal message the agent's own Dispatch Report."""

MAX_REPORT_CHARS = 20_000
"""Hard bound on the retained report. A runner cannot make it a transcript."""

MAX_TOOL_NAMES = 64
"""Hard bound on distinct tool names counted — a closed set, not a stream."""

MAX_CONTAINER_EVIDENCE_CHARS = 500
"""Hard bound on each host-authored container diagnostic or reason."""

EVIDENCE_SUFFIX = ".evidence.json"
"""Sidecar suffix, keyed off the run's log path (never inside the log)."""

DERIVED_EVIDENCE_VERSION = 2
"""Current sidecar contract version."""

_SUPPORTED_DERIVED_EVIDENCE_VERSIONS = frozenset({1, DERIVED_EVIDENCE_VERSION})
"""Known sidecars. Version 1 reads with no terminal-error facts."""

_SANITIZED_LOG_BANNER = (
    "# kew sanitized dispatch log: provider output is derived while streaming "
    "and discarded. Structured evidence lives in the .evidence.json sidecar."
)

_SAFE_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)
_MAX_TOKEN_CHARS = 48

_OPENCODE_ERROR_NAMES = frozenset(
    {
        "APIError",
        "ContextOverflowError",
        "MessageAbortedError",
        "MessageOutputLengthError",
        "NetworkError",
        "OverloadedError",
        "ProviderAuthError",
        "RateLimitError",
        "StructuredOutputError",
        "UnknownError",
    }
)
"""Names defined by OpenCode's error contract and kew's host classifier."""


@dataclass(frozen=True)
class FirstEditMetrics:
    """A run's steps/tokens-to-first-edit — both ``None`` for a zero-edit run."""

    steps_to_first_edit: int | None = None
    tokens_to_first_edit: int | None = None

    @classmethod
    def none(cls) -> FirstEditMetrics:
        """The zero-edit sentinel: both metrics NULL (never ``0``)."""
        return cls(None, None)


def _usage_tokens(usage: dict[str, object]) -> int:
    """Total tokens for one usage block (input+output+cache_creation+cache_read)."""
    total = 0
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


def _is_usage_bearing(usage: object) -> bool:
    """True when *usage* is a real model-response step (matches the runner)."""
    return isinstance(usage, dict) and any(
        usage.get(key) is not None for key in _USAGE_KEYS
    )


def _message_has_edit(content: object) -> bool:
    """True when an assistant message's content blocks include an edit tool_use."""
    if not isinstance(content, list):
        return False
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and str(block.get("name", "")) in EDIT_TOOLS
        ):
            return True
    return False


def derive_first_edit_metrics(lines: Iterable[str]) -> FirstEditMetrics:
    """Derive first-edit metrics from stream-json transcript *lines*.

    Streams the JSONL: each usage-bearing ``assistant`` message is one step,
    accumulating its tokens; the first such message that carries an
    ``Edit``/``Write`` ``tool_use`` fixes both metrics and short-circuits.
    Non-JSON lines and non-assistant events are skipped defensively so a
    partial or interleaved log never raises.
    """
    step = 0
    tokens = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or not _is_usage_bearing(message.get("usage")):
            continue
        step += 1
        tokens += _usage_tokens(message["usage"])
        if _message_has_edit(message.get("content")):
            return FirstEditMetrics(step, tokens)
    return FirstEditMetrics.none()


# ---------------------------------------------------------------------------
# Streaming derivation: fold a line in, keep the facts, drop the bytes
# ---------------------------------------------------------------------------


_PROTOCOL_EVENT_TYPES = frozenset(
    {
        "assistant",
        "system",
        "result",
        "tool_use",
        "error",
        "thread.started",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "text",
        "step_finish",
        "session",
        "tool_execution_start",
        "message_end",
    }
)


def _event_type_token(value: object) -> str:
    """Preserve fixed protocol types; sanitize unknown labels as free text."""
    if isinstance(value, str) and value in _PROTOCOL_EVENT_TYPES:
        return value
    return _safe_token(value)


def _safe_token(value: object, field: str = "protocol_identifier") -> str:
    """A bounded identifier-shaped token, or ``""``.

    Applied to every provider-supplied string that reaches the sanitized log or
    the sidecar (unknown labels, tool names, model ids). Anything outside the
    allowlist is dropped rather than escaped: a token is a name, and a name
    that needs escaping is prose wearing a name's clothes.
    """
    if isinstance(value, str):
        if is_fixed_identity(value, field):
            return value
        value = reported_identity(value, field)
        if value == REDACTED_IDENTITY:
            return value
    text = runner_text(value)
    # Reject possible userinfo before punctuation removal can conceal it.
    # This also handles malformed URLs without parsing untrusted authorities.
    if ":" in text and "@" in text:
        return "redacted"
    text = text[:_MAX_TOKEN_CHARS]
    normalized = "".join(ch for ch in text if ch in _SAFE_TOKEN_CHARS)
    # Filtering can join harmless fragments into a complete host credential.
    # Recheck before either the operational log or evidence retains the token.
    return reported_identity(normalized, field)


def _bounded_error_name(value: object) -> str | None:
    """Return one known OpenCode error name, or None."""
    if not isinstance(value, str) or not value or len(value) > _MAX_TOKEN_CHARS:
        return None
    if any(character not in _SAFE_TOKEN_CHARS for character in value):
        return None
    return value if value in _OPENCODE_ERROR_NAMES else None


def _http_status(value: object) -> int | None:
    """Return a valid integer HTTP status, excluding bool, or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _supported_evidence_version(value: object) -> int | None:
    """Return a supported integer sidecar version, excluding bool."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in _SUPPORTED_DERIVED_EVIDENCE_VERSIONS else None


def _terminal_error_facts(event: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """Extract the closed, secret-free subset of an OpenCode error event."""
    if event.get("type") != "error":
        return None, None
    error = event.get("error")
    if not isinstance(error, Mapping):
        return None, None
    data = error.get("data")
    status = _http_status(data.get("statusCode")) if isinstance(data, Mapping) else None
    return _bounded_error_name(error.get("name")), status


def clean_report_body(text: str) -> str:
    """Return the Dispatch Report markdown alone — stripped of any preamble the
    agent wrote before it and of a single wrapping code fence.

    Sandboxed agents sometimes end their run with the report prefixed by prose
    ("Committed. Here is my dispatch report.") and/or wrapped in a ``` fence
    (PR #1020 incident). Posted verbatim, GitHub renders the whole report as a
    literal code block instead of native markdown. Slicing from the marker drops
    the preamble AND any opening wrapper fence; the wrapper's CLOSING ``` then
    dangles at the end. Real code blocks inside the report come in balanced
    pairs, so an ODD count of remaining fence lines means the last one is that
    dangling wrapper closer — drop it, leaving internal fences intact.
    """
    idx = text.find(DISPATCH_REPORT_MARKER)
    if idx == -1:
        return text
    lines = text[idx:].splitlines()
    fences = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
    if len(fences) % 2 == 1:
        del lines[fences[-1]]
    return "\n".join(lines).rstrip()


def report_text_from_event(event: object) -> str | None:
    """Return report-bearing text from one supported terminal stream event.

    Only a *terminal* message may carry a report: claude's ``result`` and
    codex's completed ``agent_message``. Any other event — an assistant turn,
    a tool result, a reasoning block — is telemetry, and telemetry that merely
    quoted the marker must never be mistaken for the agent's own account.
    """
    if not isinstance(event, dict):
        return None
    text: object = None
    if event.get("type") == "result":
        text = event.get("result")
    elif event.get("type") == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
    return text if isinstance(text, str) and DISPATCH_REPORT_MARKER in text else None


@dataclass(frozen=True)
class RunnerTotals:
    """Runner-reported usage totals — a closed numeric set, never free text.

    The sandbox path folds these through the runner's OWN ``parse_line`` while
    the container streams, so a contained run's spend survives the transcript
    it was derived from. ``from_result_event`` distinguishes "the run genuinely
    reported these zeros" from "the stream never reached a terminal result
    event", exactly as ``_SandboxTelemetry`` does.
    """

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reported_model: str | None = None
    reported_service_tier: str | None = None
    from_result_event: bool = False
    has_native_cost: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "cost_usd": float(self.cost_usd),
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "cache_creation_tokens": int(self.cache_creation_tokens),
            "cache_read_tokens": int(self.cache_read_tokens),
            "reported_model": reported_model_identity(self.reported_model) or None,
            "reported_service_tier": service_tier_token(self.reported_service_tier)
            or None,
            "from_result_event": bool(self.from_result_event),
            "has_native_cost": bool(self.has_native_cost),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> RunnerTotals:
        return cls(
            cost_usd=_as_float(raw.get("cost_usd")) or 0.0,
            input_tokens=_as_int(raw.get("input_tokens")),
            output_tokens=_as_int(raw.get("output_tokens")),
            cache_creation_tokens=_as_int(raw.get("cache_creation_tokens")),
            cache_read_tokens=_as_int(raw.get("cache_read_tokens")),
            reported_model=reported_model_identity(raw.get("reported_model")) or None,
            reported_service_tier=service_tier_token(raw.get("reported_service_tier"))
            or None,
            from_result_event=bool(raw.get("from_result_event")),
            has_native_cost=bool(raw.get("has_native_cost")),
        )


def _as_int(value: object) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class DerivedRunEvidence:
    """The complete set of facts a finished dispatch stream may leave behind.

    Closed by construction: there is no field a raw response, a reasoning block
    or a prompt could land in. ``report`` is the one agent-authored string, and
    it is admitted only from a terminal message, only when it carries the
    Dispatch Report marker, and only up to :data:`MAX_REPORT_CHARS`.
    """

    first_edit: FirstEditMetrics = field(default_factory=FirstEditMetrics.none)
    steps: int = 0
    tool_uses: tuple[tuple[str, int], ...] = ()
    terminal_type: str | None = None
    error_name: str | None = None
    http_status: int | None = None
    lines_observed: int = 0
    report: str | None = None
    totals: RunnerTotals = field(default_factory=RunnerTotals)
    oom_probe_status: str | None = None
    oom_probe_diagnostic: str | None = None
    resource_cap_observations: tuple[tuple[str, str], ...] = ()
    resource_cap_observations_truncated: bool = False
    #: Why the agent stopped for a human (#2601), or None when it did not.
    needs_human: str | None = None
    #: The provider refused the runner for quota (#2643), or None.
    quota_exhausted: QuotaRejection | None = None

    @property
    def tool_use_count(self) -> int:
        """How many tool calls the stream reported, across every tool name."""
        return sum(count for _, count in self.tool_uses)

    def as_json(self) -> dict[str, Any]:
        return {
            "version": DERIVED_EVIDENCE_VERSION,
            "steps": int(self.steps),
            "steps_to_first_edit": self.first_edit.steps_to_first_edit,
            "tokens_to_first_edit": self.first_edit.tokens_to_first_edit,
            "tool_uses": {
                _safe_token(name): int(count) for name, count in self.tool_uses
            },
            "terminal_type": _event_type_token(self.terminal_type or "") or None,
            "error_name": _bounded_error_name(self.error_name),
            "http_status": _http_status(self.http_status),
            "lines_observed": int(self.lines_observed),
            "report": _bounded_report(self.report),
            "totals": self.totals.as_json(),
            "oom_probe_status": _safe_token(
                self.oom_probe_status or "", "oom_probe_status"
            )
            or None,
            "oom_probe_diagnostic": _bounded_container_text(self.oom_probe_diagnostic),
            "resource_cap_observations": [
                {
                    "code": _safe_token(code, "resource_cap_code"),
                    "reason": _bounded_container_text(reason),
                }
                for code, reason in self.resource_cap_observations
            ],
            "resource_cap_observations_truncated": (
                self.resource_cap_observations_truncated
            ),
            "needs_human": _bounded_needs_human(self.needs_human),
            "quota_exhausted": (
                self.quota_exhausted.as_json() if self.quota_exhausted else None
            ),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> DerivedRunEvidence | None:
        """Rebuild from a sidecar mapping, or None when it is not this contract.

        A sidecar written by a NEWER kew replays as unavailable rather than as
        a partially-understood fact set — the same rule the evidence ledger
        applies to a newer payload version.
        """
        version = _supported_evidence_version(raw.get("version"))
        if version is None:
            return None
        error_name, http_status = _versioned_terminal_error_facts(raw, version)
        totals = raw.get("totals")
        return cls(
            first_edit=FirstEditMetrics(
                _optional_int(raw.get("steps_to_first_edit")),
                _optional_int(raw.get("tokens_to_first_edit")),
            ),
            steps=_as_int(raw.get("steps")),
            tool_uses=_tool_uses_from_json(raw.get("tool_uses")),
            terminal_type=_event_type_token(raw.get("terminal_type") or "") or None,
            error_name=error_name,
            http_status=http_status,
            lines_observed=_as_int(raw.get("lines_observed")),
            report=_bounded_report(raw.get("report")),
            totals=(
                RunnerTotals.from_json(totals)
                if isinstance(totals, Mapping)
                else RunnerTotals()
            ),
            oom_probe_status=_safe_token(
                raw.get("oom_probe_status") or "", "oom_probe_status"
            )
            or None,
            oom_probe_diagnostic=_bounded_container_text(
                raw.get("oom_probe_diagnostic")
            ),
            resource_cap_observations=_container_observations_from_json(
                raw.get("resource_cap_observations")
            ),
            resource_cap_observations_truncated=(
                raw.get("resource_cap_observations_truncated") is True
            ),
            needs_human=_bounded_needs_human(raw.get("needs_human")),
            quota_exhausted=QuotaRejection.from_json(raw.get("quota_exhausted")),
        )


def _bounded_needs_human(value: object) -> str | None:
    """A stored stop reason, re-sanitized and bounded; None when absent."""
    if not isinstance(value, str):
        return None
    return redact_credentials(runner_text(value)).strip()[:500]


def _versioned_terminal_error_facts(
    raw: Mapping[str, Any], version: int
) -> tuple[str | None, int | None]:
    """Decode terminal-error facts only from their defining schema version."""
    if version < 2:
        return None, None
    return _bounded_error_name(raw.get("error_name")), _http_status(
        raw.get("http_status")
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _tool_uses_from_json(raw: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(raw, Mapping):
        return ()
    counted = [
        (_safe_token(name), _as_int(count))
        for name, count in list(raw.items())[:MAX_TOOL_NAMES]
    ]
    return tuple(sorted((n, c) for n, c in counted if n and c > 0))


def _bounded_container_text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_credentials(value)[:MAX_CONTAINER_EVIDENCE_CHARS]


def _container_observations_from_json(
    raw: object,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    observations: list[tuple[str, str]] = []
    for item in raw[:MAX_RESOURCE_CAP_OBSERVATIONS]:
        if not isinstance(item, Mapping):
            continue
        code = _safe_token(item.get("code") or "", "resource_cap_code")
        reason = _bounded_container_text(item.get("reason"))
        if code and reason:
            observations.append((code, reason))
    return tuple(observations)


def _bounded_report(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if DISPATCH_REPORT_MARKER not in value:
        return None
    return redact_credentials(value)[:MAX_REPORT_CHARS]


class StreamDerivation:
    """Folds provider stream lines into :class:`DerivedRunEvidence`, in order.

    One instance per run (retries included — an attempt that resumes keeps
    accumulating, so the first edit stays the run's first edit). :meth:`observe`
    is the whole contract: hand it a raw line, take back the one *sanitized*
    line the operational log may keep, and drop the raw one. The instance never
    stores a raw line, and no method returns one.
    """

    def __init__(self) -> None:
        self.steps = 0
        self.tokens = 0
        self.lines_observed = 0
        self.terminal_type: str | None = None
        self._error_name: str | None = None
        self._http_status: int | None = None
        self._first_edit = FirstEditMetrics.none()
        self._tools: dict[str, int] = {}
        self._report: str | None = None
        self._totals = RunnerTotals()
        self._oom_probe_status: str | None = None
        self._oom_probe_diagnostic: str | None = None
        self._resource_cap_observations: tuple[tuple[str, str], ...] = ()
        self._resource_cap_observations_truncated = False
        self._needs_human: str | None = None
        self._quota_exhausted: QuotaRejection | None = None
        self._quota_warning: QuotaWarning | None = None

    def observe(self, line: str) -> str | None:
        """Fold one raw stream line in; return the safe progress text, if any.

        Never raises: a container's stdout is another process's untrusted
        output, and a stream that a parser trips on must still be counted.
        """
        self.lines_observed += 1
        event = _parse_event(line)
        if event is None:
            return None
        kind = _event_type_token(event.get("type", ""))
        if kind:
            self.terminal_type = kind
            self._error_name, self._http_status = _terminal_error_facts(event)
        self._capture_report(event)
        if event.get("type") == "assistant":
            return self._observe_assistant(event.get("message"))
        return f"event {kind}" if kind else None

    def record_totals(self, totals: RunnerTotals) -> None:
        """Attach the runner-derived usage totals folded alongside this stream."""
        self._totals = replace(
            totals,
            reported_model=reported_model_identity(totals.reported_model) or None,
            reported_service_tier=service_tier_token(totals.reported_service_tier)
            or None,
        )

    def record_identity(self, metadata: Mapping[str, object]) -> None:
        """Retain safe reported identities independently of accounting keys."""
        from kew.runners.protocol import RUN_TOTALS_KEY, RunTotals

        totals = metadata.get(RUN_TOTALS_KEY)
        model = totals.model if isinstance(totals, RunTotals) else metadata.get("model")
        self.record_totals(
            replace(
                self._totals,
                reported_model=reported_model_identity(model)
                or self._totals.reported_model,
                reported_service_tier=service_tier_token(metadata.get("service_tier"))
                or self._totals.reported_service_tier,
            )
        )

    def record_message(
        self,
        event_type: str,
        message: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Note whether the agent's closing words stop for a human (#2601).

        Fed the runner-parsed events in order, so it reads every runner's
        stream the same way: an agent message that begins ``KEW_NEEDS_HUMAN:``
        after the agent's last tool call is a stop, and a later tool call means
        the agent carried on. Only the bounded, sanitized reason is kept.

        A runner's ``quota_exhausted`` event (#2643) is kept too, the latest
        one winning: it carries the reset time the lane hold is set to. So is
        its ``quota_warning`` (#3329), the latest one winning.
        """
        if event_type == QUOTA_EVENT_TYPE:
            rejection = (metadata or {}).get(QUOTA_REJECTION_KEY)
            if isinstance(rejection, QuotaRejection):
                self._quota_exhausted = rejection
        elif event_type == QUOTA_WARNING_EVENT_TYPE:
            warning = (metadata or {}).get(QUOTA_WARNING_KEY)
            if isinstance(warning, QuotaWarning):
                self._quota_warning = warning
        elif event_type == "tool_use":
            self._needs_human = None
        elif event_type == "activity" and self._needs_human is None:
            self._needs_human = needs_human_reason(message)

    @property
    def needs_human(self) -> str | None:
        """The reason the agent stopped for a human, or None when it did not."""
        return self._needs_human

    @property
    def quota_exhausted(self) -> QuotaRejection | None:
        """The provider's quota refusal this stream reported, or None."""
        return self._quota_exhausted

    @property
    def quota_warning(self) -> QuotaWarning | None:
        """The provider's latest usage-limit warning on this stream, or None."""
        return self._quota_warning

    def record_container_evidence(
        self,
        *,
        oom_probe_status: str | None,
        oom_probe_diagnostic: str | None,
        resource_cap_observations: list[dict[str, str]] | None,
        resource_cap_observations_truncated: bool,
    ) -> None:
        """Attach bounded host observations made after the stream ended."""
        self._oom_probe_status = (
            _safe_token(oom_probe_status or "", "oom_probe_status") or None
        )
        self._oom_probe_diagnostic = _bounded_container_text(oom_probe_diagnostic)
        self._resource_cap_observations = _container_observations_from_json(
            resource_cap_observations
        )
        self._resource_cap_observations_truncated = (
            resource_cap_observations_truncated is True
        )

    def evidence(self) -> DerivedRunEvidence:
        """The facts derived so far. Safe to call mid-stream and at the end."""
        return DerivedRunEvidence(
            first_edit=self._first_edit,
            steps=self.steps,
            tool_uses=tuple(sorted(self._tools.items())),
            terminal_type=self.terminal_type,
            error_name=self._error_name,
            http_status=self._http_status,
            lines_observed=self.lines_observed,
            report=self._report,
            totals=self._totals,
            oom_probe_status=self._oom_probe_status,
            oom_probe_diagnostic=self._oom_probe_diagnostic,
            resource_cap_observations=self._resource_cap_observations,
            resource_cap_observations_truncated=(
                self._resource_cap_observations_truncated
            ),
            needs_human=self._needs_human,
            quota_exhausted=self._quota_exhausted,
        )

    def _observe_assistant(self, message: object) -> str | None:
        if not isinstance(message, dict) or not _is_usage_bearing(message.get("usage")):
            return None
        self.steps += 1
        self.tokens += _usage_tokens(message["usage"])
        tools = _tool_names(message.get("content"))
        self._count_tools(tools)
        if self._first_edit.steps_to_first_edit is None and any(
            name in EDIT_TOOLS for name in tools
        ):
            self._first_edit = FirstEditMetrics(self.steps, self.tokens)
        suffix = f" tools={','.join(tools)}" if tools else ""
        return f"step {self.steps} tokens={self.tokens}{suffix}"

    def _count_tools(self, tools: tuple[str, ...]) -> None:
        for name in tools:
            if name in self._tools or len(self._tools) < MAX_TOOL_NAMES:
                self._tools[name] = self._tools.get(name, 0) + 1

    def _capture_report(self, event: Mapping[str, Any]) -> None:
        text = report_text_from_event(event)
        if text is not None:
            self._report = _bounded_report(clean_report_body(text))


def _parse_event(line: str) -> dict[str, Any] | None:
    """Parse one stream line into a JSON object, or None. Never raises."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    return event if isinstance(event, dict) else None


def _tool_names(content: object) -> tuple[str, ...]:
    """The sanitized ``tool_use`` names in one assistant message's content."""
    if not isinstance(content, list):
        return ()
    names = [
        _safe_token(block.get("name", ""), "tool_name")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    return tuple(name for name in names if name)


# ---------------------------------------------------------------------------
# The sidecar: the only durable product of a stream
# ---------------------------------------------------------------------------


def sanitized_log_banner() -> str:
    """The first line of a sanitized operational log, ending in a newline."""
    return _SANITIZED_LOG_BANNER + "\n"


def evidence_path_for(log_file: Path | str) -> Path:
    """The derived-evidence sidecar path beside *log_file*."""
    path = Path(log_file)
    return path.with_name(path.stem + EVIDENCE_SUFFIX)


def write_derived_evidence(
    log_file: Path | str | None, evidence: DerivedRunEvidence
) -> Path | None:
    """Persist *evidence* beside *log_file*, owner-only. Returns the path.

    Owner-only (``0o600``) and ``O_NOFOLLOW``: the sidecar sits in the host log
    directory, and a planted symlink must not redirect the write. Returns None
    and logs when it could not be written — an unwritable sidecar is a run with
    no evidence, and the next reader must see the gap rather than a stale file.
    """
    if log_file is None:
        return None
    path = evidence_path_for(log_file)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    payload = redact_credentials(
        json.dumps(
            redact_authorization_values(evidence.as_json()),
            indent=2,
            sort_keys=True,
            default=runner_text,
        )
    )
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    except OSError as exc:
        _logger.warning("Derived run evidence not written to %s: %s", path, exc)
        return None
    return path


def write_container_evidence(
    log_file: Path | str | None,
    *,
    oom_probe_status: str | None,
    oom_probe_diagnostic: str | None,
    resource_cap_observations: list[dict[str, str]] | None,
    resource_cap_observations_truncated: bool,
) -> Path | None:
    """Add post-stream container observations to the durable sidecar."""
    evidence = read_derived_evidence(log_file) or DerivedRunEvidence()
    updated = replace(
        evidence,
        oom_probe_status=_safe_token(oom_probe_status or "", "oom_probe_status")
        or None,
        oom_probe_diagnostic=_bounded_container_text(oom_probe_diagnostic),
        resource_cap_observations=_container_observations_from_json(
            resource_cap_observations
        ),
        resource_cap_observations_truncated=(
            resource_cap_observations_truncated is True
        ),
    )
    return write_derived_evidence(log_file, updated)


def read_derived_evidence(log_file: Path | str | None) -> DerivedRunEvidence | None:
    """Read the sidecar beside *log_file*, or None when there is none.

    None means **unavailable**, and every caller must treat it that way: a run
    from before this contract has a raw log and no sidecar, and re-parsing that
    log is exactly what this module ended.
    """
    if log_file is None:
        return None
    try:
        raw = json.loads(evidence_path_for(log_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return DerivedRunEvidence.from_json(raw) if isinstance(raw, Mapping) else None


def first_edit_metrics_for(log_file: Path | str | None) -> FirstEditMetrics:
    """The run's first-edit metrics from its sidecar — NULL when unavailable.

    Replaces the old ``derive_first_edit_metrics_from_file`` raw-log replay.
    Both the "no evidence" and the "measured, never edited" answers are the
    NULL pair, and they are distinguished by :func:`read_derived_evidence`
    returning None — the columns themselves cannot carry that difference and
    never could.
    """
    evidence = read_derived_evidence(log_file)
    return FirstEditMetrics.none() if evidence is None else evidence.first_edit


def agent_report_for(log_file: Path | str | None) -> str | None:
    """The agent's own bounded Dispatch Report, or None when there is none."""
    evidence = read_derived_evidence(log_file)
    return None if evidence is None else evidence.report
