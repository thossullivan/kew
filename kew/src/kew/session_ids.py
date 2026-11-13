"""Validate untrusted session handles before retention or command construction."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from kew.runner_fields import REDACTED_IDENTITY, reported_identity

if TYPE_CHECKING:
    from kew.tracking import DispatchResult

_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
# Claude and Codex use UUID handles. Kew generates UUIDs for pi and lemonade.
# OpenCode uses a ses_ prefix and an ASCII alphanumeric suffix.
# Hermes uses YYYYMMDD_HHMMSS followed by six hexadecimal characters.
_GRAMMARS = {
    "claude-code": _UUID,
    "codex": _UUID,
    "pi": _UUID,
    "lemonade": _UUID,
    "opencode": r"ses_[A-Za-z0-9]{1,128}",
    "hermes": r"[0-9]{8}_[0-9]{6}_[0-9a-fA-F]{6}",
}
# Custom adapters have a bounded opaque handle, without flags or delimiters.
_OPAQUE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


def session_id_rejection(value: object, runner_name: str) -> str:
    """Return a fixed rejection reason. Never include the rejected value."""
    if not isinstance(value, str):
        return "non-string session identifier"
    if reported_identity(value, "session_id") == REDACTED_IDENTITY:
        return "credential-bearing session identifier"
    if re.fullmatch(_GRAMMARS.get(runner_name, _OPAQUE), value) is None:
        return "non-conforming session identifier"
    return ""


def capture_session_id(result: DispatchResult, value: object, runner_name: str) -> None:
    """Keep a valid handle. Any rejection disables resume for this attempt."""
    if value is not None:
        reason = session_id_rejection(value, runner_name)
        if reason == "credential-bearing session identifier":
            result.session_id_warning = f"session_id: {REDACTED_IDENTITY}"
        result.session_id_rejection = result.session_id_rejection or reason
        result.session_id = value if isinstance(value, str) and not reason else None
    if result.session_id_rejection:
        result.session_id = None


def require_session_id(value: str, runner_name: str) -> str:
    """Refuse an invalid resume handle before invoking the adapter."""
    reason = session_id_rejection(value, runner_name)
    if reason:
        raise ValueError(f"Resume refused: {reason}")
    return value
