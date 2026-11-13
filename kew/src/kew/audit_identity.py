"""Stable identities for idempotent audit evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

CONTINUATION_IDENTITY_FIELDS = (
    "recovery",
    "recovery_phase",
    "epic_ref",
    "item_id",
    "original_run_id",
    "generation",
    "branch",
    "worktree_identity",
    "continuation_run_id",
)


def continuation_evidence_identity(event: str, evidence: dict[str, Any]) -> str | None:
    """Return the stable v1 identity for continuation audit evidence."""
    if evidence.get("recovery") != "preserved_work_continuation":
        return None
    preimage = json.dumps(
        [event, *(evidence.get(field) for field in CONTINUATION_IDENTITY_FIELDS)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return f"continuation:v1:sha256:{digest}"


_RECOVERY_TOKEN_RE = re.compile(r"[^A-Za-z0-9._:/-]+")
_RECOVERY_TOKEN_LIMIT = 256


def _sanitize_recovery_token(value: str | None) -> str | None:
    """Return a bounded, log-safe identity token for recovery evidence."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _RECOVERY_TOKEN_RE.sub("_", text)[:_RECOVERY_TOKEN_LIMIT]


def _sanitized_worktree_identity(value: str | Path | None) -> str | None:
    """Hash a canonical worktree path before it enters the audit payload.

    Worktree paths contain local usernames and directory layout, neither of
    which is useful to an audit consumer.  The digest remains stable for the
    same checkout and lets recovery events be joined without copying the path
    into the chained ledger.
    """
    if value is None:
        return None
    try:
        canonical = str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        canonical = str(value)
    if not canonical:
        return None
    digest = hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def continuation_evidence(
    *,
    phase: str,
    epic_ref: str,
    item_id: str,
    original_run_id: str | None,
    continuation_run_id: str | None,
    branch: str,
    worktree_identity: str | Path | None,
    generation: int | None = None,
    state: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Build the secret-free lineage payload for preserved-work recovery.

    Recovery evidence is additive payload data, so it remains covered by the
    existing audit hash chain without changing the chain preimage schema.  All
    caller-controlled identifiers are bounded/sanitized and the local
    worktree path is represented only by a stable digest.
    """
    safe_generation = (
        int(generation) if isinstance(generation, int) and generation >= 0 else None
    )
    return {
        "recovery": "preserved_work_continuation",
        "recovery_phase": _sanitize_recovery_token(phase),
        "epic_ref": _sanitize_recovery_token(epic_ref),
        "item_id": _sanitize_recovery_token(item_id),
        "original_run_id": _sanitize_recovery_token(original_run_id),
        "continuation_run_id": _sanitize_recovery_token(continuation_run_id),
        "branch": _sanitize_recovery_token(branch),
        "worktree_identity": _sanitized_worktree_identity(worktree_identity),
        "generation": safe_generation,
        "state": _sanitize_recovery_token(state),
        "outcome": _sanitize_recovery_token(outcome),
    }
