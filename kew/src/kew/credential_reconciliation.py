"""Shared, durable revocation retries for foreground and detached run keys."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kew.credential_intents import reconcile_issuance_intents
from kew.credential_issuer import BoundedRunKey, InvalidProvisioningCredential
from kew.credential_prelaunch import absent_run_is_terminal

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, RunStore

KEY_SCAN_LIMIT = 100
REVOCATION_BACKOFF_BASE = 30
REVOCATION_BACKOFF_MAX = 300
REVOCATION_RETRY_COUNT_MAX = 32
_KEY_CURSOR = "run_credential_issuance_cursor"
_logger = logging.getLogger(__name__)


def _state_key(run_id: str, key_hash: str) -> str:
    return f"run_key_revocation:{run_id}:{key_hash}"


def revocation_state(store: RunStore, run_id: str, key_hash: str) -> dict[str, Any]:
    """Read the public retry projection from the durable meta row."""
    raw = store.get_meta(_state_key(run_id, key_hash))
    return json.loads(raw) if raw else {}


def revocation_retry_schedule(previous_count: int) -> tuple[int, int]:
    """Share the capped recovery schedule across durable obligations."""
    count = min(previous_count + 1, REVOCATION_RETRY_COUNT_MAX)
    delay = min(
        REVOCATION_BACKOFF_BASE * 2 ** min(count - 1, 4), REVOCATION_BACKOFF_MAX
    )
    return count, delay


def begin_revocation(
    store: RunStore, run_id: str, key: BoundedRunKey, now: datetime
) -> dict[str, Any] | None:
    """Persist ambiguity before DELETE; a crash leaves a retryable marker."""
    row_key = _state_key(run_id, key.key_hash)
    previous = store.get_meta(row_key)
    state = json.loads(previous) if previous else {}
    if state and (
        not state["pending"] or datetime.fromisoformat(state["next_attempt_at"]) > now
    ):
        return None
    count, delay = revocation_retry_schedule(state.get("retry_count", 0))
    state = {
        **state,
        "run_id": run_id,
        "credential_issuer": key.identity,
        "pending": True,
        "reason": state.get("reason", "revocation outcome unknown"),
        "retry_count": count,
        "next_attempt_at": (now + timedelta(seconds=delay)).isoformat(),
    }
    claimed = store.compare_set_meta(
        row_key, previous, json.dumps(state, sort_keys=True)
    )
    return state if claimed else None


def finish_revocation(
    store: RunStore, state: dict[str, Any], *, revoked: bool, reason: str | None
) -> None:
    previous = json.dumps(state, sort_keys=True)
    if revoked and not _already_revoked(
        store, state["run_id"], state["credential_issuer"]["key_hash"]
    ):
        revoked, reason = False, "revocation evidence unavailable"
    state.update(pending=not revoked, reason=reason)
    warn = reason == InvalidProvisioningCredential.reason and not state.get("warned")
    if warn:
        state["warned"] = True
    saved = store.compare_set_meta(
        _state_key(state["run_id"], state["credential_issuer"]["key_hash"]),
        previous,
        json.dumps(state, sort_keys=True),
    )
    if warn and saved:
        _logger.warning(
            "revocation pending: run=%s outcome=%s", state["run_id"], reason
        )


def _key_from_identity(identity: dict[str, Any]) -> BoundedRunKey:
    return BoundedRunKey(
        "",
        identity["key_hash"],
        identity["limit_usd"],
        identity["expires_at"],
        name=identity.get("name"),
    )


def reconcile_pending_key(
    store: RunStore, run_id: str, key_hash: str, *, now: datetime | None = None
) -> None:
    """Reconcile one obligation for a completed or reaped run, including ancestors."""
    from kew.run_credentials import _revoke

    now = now or datetime.now(UTC)
    events = store.list_audit_events(
        event="sandbox_resolved", run_id=run_id, limit=None
    )
    for event in events:
        identity = _lifecycle_identity(event)
        if identity is None or identity["key_hash"] != key_hash:
            continue
        target = _revocation_target(store, event, now, set(), pending_only=False)
        if target is not None:
            _revoke(store, *target, now=now)
            return


def _lifecycle_identity(event: AuditEvent) -> dict[str, Any] | None:
    """Accept only complete identities recorded by the host credential policy."""
    payload = event.payload or {}
    identity = payload.get("credential_issuer")
    if not isinstance(identity, dict):
        return None
    if (
        (event.event, event.actor_type, event.actor_id)
        != ("sandbox_resolved", "policy", "credential-issuer")
        or payload.get("phase") not in {"issuance", "revocation_pending", "revocation"}
        or identity.get("issuer") != "openrouter"
        or not {"key_hash", "limit_usd", "expires_at"} <= identity.keys()
    ):
        return None
    return identity


def _unmarked_identity(event: AuditEvent) -> dict[str, Any] | None:
    """Issuance itself and pre-marker failed revocation both establish an obligation."""
    identity = _lifecycle_identity(event)
    phase = (event.payload or {}).get("phase")
    if identity is not None and (
        phase == "issuance"
        or (phase == "revocation" and identity.get("revoked") is False)
    ):
        return identity
    return None


def _run_has_ended(
    store: RunStore, event: AuditEvent, identity: dict[str, Any], now: datetime
) -> bool:
    """Admission supersedes pre-launch protection; missing rows need evidence."""
    if event.run_id is None:
        return False
    run = store.get(event.run_id)
    if run is not None:
        return run.status != "pending"
    return absent_run_is_terminal(store, event, identity, now)


def _already_revoked(store: RunStore, run_id: str, key_hash: str) -> bool:
    events = store.list_audit_events(
        event="sandbox_resolved", run_id=run_id, limit=None
    )
    return any(
        event.actor_type == "policy"
        and event.actor_id == "credential-issuer"
        and (event.payload or {}).get("phase") == "revocation"
        and (event.payload or {}).get("credential_issuer", {}).get("key_hash")
        == key_hash
        and (event.payload or {}).get("credential_issuer", {}).get("revoked") is True
        for event in events
    )


def _revocation_due(
    store: RunStore,
    event: AuditEvent,
    identity: dict[str, Any],
    now: datetime,
    *,
    pending_only: bool,
) -> bool:
    if event.run_id is None or not _run_has_ended(store, event, identity, now):
        return False
    state = revocation_state(store, event.run_id, identity["key_hash"])
    if state:
        return (
            state["pending"] and datetime.fromisoformat(state["next_attempt_at"]) <= now
        )
    if pending_only and (event.payload or {}).get("phase") == "issuance":
        return False
    return _unmarked_identity(event) is not None


def _revocation_target(
    store: RunStore,
    event: AuditEvent,
    now: datetime,
    seen: set[str],
    *,
    pending_only: bool,
) -> tuple[str, BoundedRunKey] | None:
    identity = _lifecycle_identity(event)
    if identity is None or event.run_id is None:
        return None
    key_hash = identity["key_hash"]
    if key_hash in seen or not _revocation_due(
        store, event, identity, now, pending_only=pending_only
    ):
        return None
    if _already_revoked(store, event.run_id, key_hash):
        _settle_recorded_revocation(store, event.run_id, key_hash)
        return None
    return event.run_id, _key_from_identity(identity)


def _settle_recorded_revocation(store: RunStore, run_id: str, key_hash: str) -> None:
    """Repair a crash between confirmed audit evidence and projection settlement."""
    state = revocation_state(store, run_id, key_hash)
    if state.get("pending"):
        finish_revocation(store, state, revoked=True, reason=None)


def reconcile_run_keys(
    store: RunStore,
    *,
    now: datetime | None = None,
    seen: set[str] | None = None,
    limit: int = KEY_SCAN_LIMIT,
    pending_only: bool = False,
) -> None:
    """Seek one indexed page across both sources; retry each hash once per pass."""
    from kew.credential_orphans import sweep_issuer_orphans
    from kew.run_credentials import _revoke

    now = now or datetime.now(UTC)
    seen = set() if seen is None else seen
    reconcile_issuance_intents(store, now=now, seen=seen, limit=limit)
    cursor = int(store.get_meta(_KEY_CURSOR) or 0)
    events = store.list_run_key_issuances(after_id=cursor, limit=limit)
    for event in events:
        target = _revocation_target(store, event, now, seen, pending_only=pending_only)
        if target is None:
            continue
        run_id, key = target
        seen.add(key.key_hash)
        _revoke(store, run_id, key, now=now)
    cursor = events[-1].id if len(events) == limit else 0
    store.set_meta(_KEY_CURSOR, str(cursor))
    sweep_issuer_orphans(store, now=now, seen=seen)
