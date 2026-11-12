"""Sanitize decoded runner fields before adapters truncate or retain them."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence, Set

# Re-exported for callers that predate the split: the display-text
# coercion needs no part of the runner layer, so it lives in the leaf
# redaction module (governed review round 6 of PR #1941, 2026-09-22).
from kew.credential_surface import (
    _authorization_value as _authorization_value,
)
from kew.credential_surface import (
    _dataclass_values as _dataclass_values,
)
from kew.credential_surface import (
    _redact_items as _redact_items,
)
from kew.credential_surface import (
    _redact_key as _redact_key,
)
from kew.credential_surface import (
    _redact_text_leaves as _redact_text_leaves,
)
from kew.credential_surface import (
    exception_text as exception_text,
)
from kew.credential_surface import (
    runner_text as runner_text,
)

REDACTED_IDENTITY = "<redacted-identity>"

# Classification kinds emitted or consumed by kew's adapters and orchestrator.
# Only exact field-specific matches are internal vocabulary. Unknown strings,
# encodings, prefixes and suffixes still pass through credential containment.
_CLASSIFICATION_KINDS = {
    "error_category": frozenset(
        {
            "transient",
            "permanent",
            "budget_exceeded",
            "watchdog_kill",
            "transport_unavailable",
            "needs_human",
            "circuit_breaker",
            "budget_stop",
            "budget_stopped",
            "killed",
            "exception",
            "quota_exhausted",
            "resource",
        }
    ),
    "subtype": frozenset(
        {
            "success",
            "api_error",
            "error_max_budget_usd",
            "error_max_turns",
            "error_during_execution",
            "proxy_lost",
            "watchdog_budget",
            "cost_anomaly",
            "unspecified",
        }
    ),
    "stop_reason": frozenset(
        {
            "end_turn",
            "tool_use",
            "toolUse",
            "stop",
            "length",
            "error",
            "aborted",
            "completed",
            "failed",
            "incomplete",
        }
    ),
}


def is_fixed_identity(value: str, field: str) -> bool:
    """Validate an exact internal vocabulary entry before checking credentials.

    Structured fields retain these public constants for decisions and records.
    Display text must use ``runner_text``, which has no vocabulary exemption.
    """
    if field == "runner":
        from kew.runners.registry import KNOWN_RUNNER_NAMES

        return value in KNOWN_RUNNER_NAMES
    if field == "model":
        from kew.pricing import catalog_pricing

        return value in catalog_pricing()
    if field == "service_tier":
        from typing import get_args

        from kew.config import CodexConfig

        return value in get_args(CodexConfig.model_fields["service_tier"].annotation)
    if field in ("oom_probe_status", "resource_cap_code"):
        from kew.sandbox import OOMProbeStatus, ResourceCapCode

        kinds = OOMProbeStatus if field == "oom_probe_status" else ResourceCapCode
        return value in kinds
    return value in _CLASSIFICATION_KINDS.get(field, ())


def reported_identity(value: str, field: str) -> str:
    """Keep fixed internal kinds; check all other identities before retention."""
    if is_fixed_identity(value, field):
        return value
    return _redacted_identity(value, field)


def _redacted_identity(value: str, field: str) -> str:
    """Sanitize retained text without an internal-vocabulary exemption."""
    from kew.credential_surface import redact_credentials

    if value == REDACTED_IDENTITY:
        return value
    if redact_credentials(value) != value or " redacted>" in value:
        # Logs the field name and the fixed placeholder only, never the value.
        logging.getLogger(__name__).warning(  # noqa: E501  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            "reported %s conflicts with host credentials; retaining %s",
            field,
            REDACTED_IDENTITY,
        )
        return REDACTED_IDENTITY
    return value


def reported_model_identity(value: object) -> str:
    """Keep reported model text separate from the internal pricing identity.

    Reported text retains the round-seven collision marker even for known
    models. Internal validation and pricing can still use the exact public ID.
    """
    if value == REDACTED_IDENTITY:
        return REDACTED_IDENTITY
    return _redacted_identity(model_identity(value), "model")


def service_tier_token(value: object) -> str:
    """Keep short ASCII tier tokens. Replace other populated values with invalid."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return "invalid"
    if value == REDACTED_IDENTITY:
        return value
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", text):
        return "invalid"
    normalized = reported_identity(value, "service_tier").strip()
    return reported_identity(normalized, "service_tier")


def redact_authorization_values(value: object) -> object:
    """Copy structured payloads before serialization without changing fixed kinds.

    Ordinary string leaves retain surrounding evidence after local replacement.
    """
    from kew.credential_surface import redact_credential_values

    value = _dataclass_values(value)
    if isinstance(value, Mapping):
        return {
            key.decode("ascii", errors="backslashreplace")
            if isinstance(key, bytes)
            else key: redact_authorization_values(_authorization_value(key, item))
            for key, item in value.items()
        }
    if isinstance(value, str):
        return redact_credential_values(value, surface="structured string leaf")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value
    if isinstance(value, (Sequence, Set)):
        return [redact_authorization_values(item) for item in value]
    if isinstance(value, BaseException):
        return exception_text(value)
    return value


def sanitize_exception_args(exc: BaseException) -> None:
    """Protect downstream handlers when propagating a runner exception."""
    exc.args = tuple(_redact_text_leaves(arg) for arg in exc.args)


#: The largest magnitude a protocol count is taken at. Far past any real run's
#: tokens, and small enough that a priced count stays a finite float.
PROTOCOL_INT_LIMIT = 10**15


def protocol_int(value: object, field: str) -> int:
    """Parse a numeric protocol field without echoing a rejected value.

    A count past :data:`PROTOCOL_INT_LIMIT` is clamped to it, never refused:
    the line still counts, as the most it can, so a forged figure too large to
    price trips the cost governor instead of crashing it (#1462)."""
    if value is None:
        return 0
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = int(value)
        if isinstance(value, float) and number != value:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{field} is not an integer") from None
    return max(-PROTOCOL_INT_LIMIT, min(PROTOCOL_INT_LIMIT, number))


def protocol_float(value: object, field: str) -> float:
    """Parse finite protocol costs with a fixed, field-specific diagnostic."""
    if value is None:
        return 0.0
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{field} is not a number") from None


def model_identity(value: object) -> str:
    """Validate a public pricing identity without redaction or truncation."""
    if (
        isinstance(value, str)
        and len(value) <= 256
        and re.fullmatch(r"[A-Za-z0-9_.:/+-]+(?:\[[0-9]+[km]\])?", value)
    ):
        return value
    return ""


def require_public_model(value: str) -> None:
    """Refuse invalid or secret pricing identities before any launch or run record."""
    from kew.credential_surface import redact_credentials

    if not value:
        return
    if not model_identity(value):
        raise ValueError("invalid model identity")
    if redact_credentials(value) != value:
        raise ValueError("model identity conflicts with host credentials")


NEEDS_HUMAN_MARKER = "KEW_NEEDS_HUMAN:"


def needs_human_reason(message: str) -> str | None:
    """The reason an agent message stops for a human, or None when it does not.

    An agent stops itself — a policy gate it could not make pass, say — by
    beginning a message ``KEW_NEEDS_HUMAN: <reason>`` (#2601). The reason is
    sanitized and bounded; "" means the agent gave none.
    """
    text = message.strip()
    if not text.startswith(NEEDS_HUMAN_MARKER):
        return None
    return runner_text(text.removeprefix(NEEDS_HUMAN_MARKER)).strip()[:500]
