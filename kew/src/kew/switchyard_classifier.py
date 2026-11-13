"""Bounded, sanitized classifier decisions emitted by Switchyard.

Switchyard classifier implementations have used more than one wire format for
their diagnostic record.  This module accepts only the small, stable subset
that is useful for routing calibration.  It deliberately never copies model
errors, prompts, responses, rationale text, or arbitrary labels into Kew
evidence.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ClassifierTier = Literal["weak", "strong"]
ClassifierVerdict = Literal[
    "selected_weak",
    "selected_strong",
    "classifier_unavailable_fallback_strong",
]
ClassifierFormat = Literal[
    "openai_responses",
    "openai_chat",
    "anthropic_messages",
    "unknown",
]
ConfidenceBucket = Literal[
    "unavailable",
    "low",
    "medium",
    "high",
    "very_high",
]
FailClosedReason = Literal[
    "classifier_unavailable",
    "classifier_invalid_verdict",
    "classifier_abstained",
    "classifier_low_confidence",
    "classifier_missing_selection",
]

_MAX_EVIDENCE_LINE_BYTES = 16 * 1024
_FORMAT_ALIASES: dict[str, ClassifierFormat] = {
    "anthropic": "anthropic_messages",
    "anthropic-messages": "anthropic_messages",
    "anthropic_messages": "anthropic_messages",
    "chat": "openai_chat",
    "chat-completions": "openai_chat",
    "chat_completions": "openai_chat",
    "openai-chat": "openai_chat",
    "openai_chat": "openai_chat",
    "openai-responses": "openai_responses",
    "openai_responses": "openai_responses",
    "responses": "openai_responses",
}
_TIER_ALIASES: dict[str, ClassifierTier] = {
    "capable": "strong",
    "capable_target": "strong",
    "classifier_selected_strong": "strong",
    "selected_strong": "strong",
    "strong": "strong",
    "strong_target": "strong",
    "efficient": "weak",
    "efficient_target": "weak",
    "classifier_selected_weak": "weak",
    "selected_weak": "weak",
    "weak": "weak",
    "weak_target": "weak",
}
_CAPABILITY_BOUNDARIES = frozenset(
    {"supported", "uncertain", "unsupported", "unmatched"}
)
_REASON_ALIASES: dict[str, FailClosedReason] = {
    "abstain": "classifier_abstained",
    "classifier_abstained": "classifier_abstained",
    "invalid": "classifier_invalid_verdict",
    "invalid_response": "classifier_invalid_verdict",
    "parse_error": "classifier_invalid_verdict",
    "classifier_invalid_verdict": "classifier_invalid_verdict",
    "low_confidence": "classifier_low_confidence",
    "classifier_low_confidence": "classifier_low_confidence",
    "missing_signals": "classifier_missing_selection",
    "missing_selection": "classifier_missing_selection",
    "classifier_missing_selection": "classifier_missing_selection",
    "error": "classifier_unavailable",
    "fail_open": "classifier_unavailable",
    "unavailable": "classifier_unavailable",
    "classifier_unavailable": "classifier_unavailable",
}


@dataclass(frozen=True)
class ClassifierEvidence:
    """Safe classifier evidence for one served request.

    The fields are intentionally values from finite allowlists or bounded
    numeric signals.  In particular, this type has no field for the
    classifier's reason text or upstream error body.
    """

    verdict: ClassifierVerdict
    selected_tier: ClassifierTier
    confidence: float | None
    confidence_bucket: ConfidenceBucket
    ingress_format: ClassifierFormat
    upstream_format: ClassifierFormat
    fallback_taken: bool
    fail_closed_reason: FailClosedReason | None
    p_solve: float | None
    capability_boundary: str | None

    def to_json(self) -> dict[str, object]:
        """Return the complete sanitized evidence shape."""
        return {
            "capability_boundary": self.capability_boundary,
            "confidence": self.confidence,
            "confidence_bucket": self.confidence_bucket,
            "fail_closed_reason": self.fail_closed_reason,
            "fallback_taken": self.fallback_taken,
            "ingress_format": self.ingress_format,
            "p_solve": self.p_solve,
            "selected_tier": self.selected_tier,
            "upstream_format": self.upstream_format,
            "verdict": self.verdict,
        }


def _format(value: object) -> ClassifierFormat:
    if not isinstance(value, str):
        return "unknown"
    return _FORMAT_ALIASES.get(value.strip().lower(), "unknown")


def _probability(value: object) -> tuple[float | None, bool]:
    """Return ``(value, valid)`` while rejecting bool, NaN, and infinity."""
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None, False
    return number, True


def _confidence_bucket(value: float | None) -> ConfidenceBucket:
    if value is None:
        return "unavailable"
    if value < 0.5:
        return "low"
    if value < 0.75:
        return "medium"
    if value < 0.9:
        return "high"
    return "very_high"


def _tier(value: object) -> ClassifierTier | None:
    if not isinstance(value, str):
        return None
    return _TIER_ALIASES.get(value.strip().lower())


def _record_tier(
    record: Mapping[str, object], selected_tier: object
) -> ClassifierTier | None:
    candidates = (
        selected_tier,
        *(
            record.get(key)
            for key in (
                "selected_tier",
                "tier",
                "policy_tier",
                "recommended_route",
                "verdict",
            )
        ),
    )
    for candidate in candidates:
        resolved = _tier(candidate)
        if resolved is not None:
            return resolved
    return None


def _reason(record: Mapping[str, object]) -> FailClosedReason | None:
    for key in ("fail_closed_reason", "fallback_reason", "source", "decision_source"):
        value = record.get(key)
        if isinstance(value, str):
            resolved = _REASON_ALIASES.get(value.strip().lower())
            if resolved is not None:
                return resolved
    return None


def _fallback(
    *,
    ingress_format: ClassifierFormat,
    upstream_format: ClassifierFormat,
    reason: FailClosedReason,
    confidence: float | None = None,
    p_solve: float | None = None,
    capability_boundary: str | None = None,
) -> ClassifierEvidence:
    return ClassifierEvidence(
        verdict="classifier_unavailable_fallback_strong",
        selected_tier="strong",
        confidence=confidence,
        confidence_bucket=_confidence_bucket(confidence),
        ingress_format=ingress_format,
        upstream_format=upstream_format,
        fallback_taken=True,
        fail_closed_reason=reason,
        p_solve=p_solve,
        capability_boundary=capability_boundary,
    )


@dataclass(frozen=True)
class _RecordFields:
    confidence: float | None
    confidence_valid: bool
    p_solve: float | None
    p_solve_valid: bool
    capability_boundary: str | None
    capability_valid: bool
    abstain: bool | None
    fail_open: bool | None
    booleans_valid: bool
    reason: FailClosedReason | None
    explicit_fallback: bool
    tier: ClassifierTier | None


def _record_mapping(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or not all(
        isinstance(key, str) for key in payload
    ):
        return {}
    record = dict(payload)
    nested = record.get("classifier_signals")
    if isinstance(nested, Mapping) and all(isinstance(key, str) for key in nested):
        merged = {
            key: value for key, value in record.items() if key != "classifier_signals"
        }
        merged.update(nested)
        return merged
    return record


def _capability(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, True
    if isinstance(value, str) and value in _CAPABILITY_BOUNDARIES:
        return value, True
    return None, False


def _boolean(value: object) -> tuple[bool | None, bool]:
    if isinstance(value, bool):
        return value, True
    return None, False


def _explicit_fallback(record: Mapping[str, object]) -> bool:
    verdict = record.get("verdict")
    return record.get("fallback_taken") is True or (
        isinstance(verdict, str)
        and verdict.strip().lower() == "classifier_unavailable_fallback_strong"
    )


def _record_fields(
    record: Mapping[str, object], selected_tier: object
) -> _RecordFields:
    confidence, confidence_valid = _probability(record.get("confidence"))
    p_solve, p_solve_valid = _probability(record.get("p_solve"))
    capability_boundary, capability_valid = _capability(
        record.get("capability_boundary")
    )
    abstain, abstain_valid = _boolean(record.get("abstain", False))
    fail_open, fail_open_valid = _boolean(record.get("fail_open", False))
    if "error" in record:
        fail_open = True
    return _RecordFields(
        confidence=confidence,
        confidence_valid=confidence_valid,
        p_solve=p_solve,
        p_solve_valid=p_solve_valid,
        capability_boundary=capability_boundary,
        capability_valid=capability_valid,
        abstain=abstain,
        fail_open=fail_open,
        booleans_valid=abstain_valid and fail_open_valid,
        reason=_reason(record),
        explicit_fallback=_explicit_fallback(record),
        tier=_record_tier(record, selected_tier),
    )


def _structural_reason(fields: _RecordFields) -> FailClosedReason | None:
    if fields.fail_open is True or fields.explicit_fallback:
        return fields.reason or "classifier_unavailable"
    if (
        not fields.booleans_valid
        or not fields.confidence_valid
        or not fields.p_solve_valid
    ):
        return "classifier_invalid_verdict"
    if not fields.capability_valid:
        return "classifier_invalid_verdict"
    return None


def _decision_reason(fields: _RecordFields) -> FailClosedReason | None:
    if fields.abstain is True:
        return fields.reason or "classifier_abstained"
    if fields.reason == "classifier_low_confidence":
        return fields.reason
    if fields.confidence is None:
        return "classifier_invalid_verdict"
    return None


def _preflight_reason(
    fields: _RecordFields,
) -> tuple[FailClosedReason, bool] | None:
    structural = _structural_reason(fields)
    if structural is not None:
        return structural, False
    decision = _decision_reason(fields)
    if decision is not None:
        return decision, True
    return None


def _fallback_from_fields(
    *,
    fields: _RecordFields,
    ingress_format: ClassifierFormat,
    upstream_format: ClassifierFormat,
    reason: FailClosedReason,
    include_signals: bool,
) -> ClassifierEvidence:
    if not include_signals:
        return _fallback(
            ingress_format=ingress_format,
            upstream_format=upstream_format,
            reason=reason,
        )
    return _fallback(
        ingress_format=ingress_format,
        upstream_format=upstream_format,
        reason=reason,
        confidence=fields.confidence,
        p_solve=fields.p_solve,
        capability_boundary=fields.capability_boundary,
    )


def parse_classifier_evidence(
    payload: object,
    *,
    ingress_format: object = None,
    upstream_format: object = None,
    selected_tier: object = None,
    parse_failed: bool = False,
) -> ClassifierEvidence:
    """Parse one classifier record into sanitized, fail-closed evidence.

    ``payload`` is an untrusted diagnostic boundary.  Malformed, missing, or
    fail-open records produce an explicit strong fallback rather than an
    exception or an inferred successful selection.
    """
    record = _record_mapping(payload)

    ingress = _format(
        ingress_format
        if ingress_format is not None
        else record.get("ingress_format", record.get("request_type"))
    )
    upstream = _format(
        upstream_format
        if upstream_format is not None
        else record.get("upstream_format", record.get("format"))
    )
    if parse_failed or not record:
        return _fallback(
            ingress_format=ingress,
            upstream_format=upstream,
            reason="classifier_unavailable",
        )

    fields = _record_fields(record, selected_tier)
    preflight = _preflight_reason(fields)
    if preflight is not None:
        reason, include_signals = preflight
        return _fallback_from_fields(
            fields=fields,
            ingress_format=ingress,
            upstream_format=upstream,
            reason=reason,
            include_signals=include_signals,
        )
    if fields.tier is None:
        return _fallback(
            ingress_format=ingress,
            upstream_format=upstream,
            reason="classifier_missing_selection",
            confidence=fields.confidence,
            p_solve=fields.p_solve,
            capability_boundary=fields.capability_boundary,
        )
    return ClassifierEvidence(
        verdict=("selected_strong" if fields.tier == "strong" else "selected_weak"),
        selected_tier=fields.tier,
        confidence=fields.confidence,
        confidence_bucket=_confidence_bucket(fields.confidence),
        ingress_format=ingress,
        upstream_format=upstream,
        fallback_taken=False,
        fail_closed_reason=None,
        p_solve=fields.p_solve,
        capability_boundary=fields.capability_boundary,
    )


def parse_classifier_evidence_line(
    line: str,
    *,
    ingress_format: object = None,
    upstream_format: object = None,
    selected_tier: object = None,
) -> ClassifierEvidence | None:
    """Parse a Switchyard ``classifier_signals=`` line, if present."""
    if len(line.encode("utf-8", errors="replace")) > _MAX_EVIDENCE_LINE_BYTES:
        if line.lstrip().startswith(("classifier_signals=", "classifier_evidence=")):
            return parse_classifier_evidence(
                None,
                ingress_format=ingress_format,
                upstream_format=upstream_format,
                selected_tier=selected_tier,
                parse_failed=True,
            )
        return None
    stripped = line.strip()
    prefix = next(
        (
            candidate
            for candidate in ("classifier_signals=", "classifier_evidence=")
            if stripped.startswith(candidate)
        ),
        None,
    )
    if prefix is None:
        return None
    try:
        payload = json.loads(stripped[len(prefix) :])
    except (RecursionError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return parse_classifier_evidence(
            None,
            ingress_format=ingress_format,
            upstream_format=upstream_format,
            selected_tier=selected_tier,
            parse_failed=True,
        )
    return parse_classifier_evidence(
        payload,
        ingress_format=ingress_format,
        upstream_format=upstream_format,
        selected_tier=selected_tier,
    )
