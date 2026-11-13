"""Durable obligations for issuer requests whose response may never be recorded."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from kew import credential_issuer

if TYPE_CHECKING:
    from kew.run_store import RunStore

INTENT_PREFIX = "run_key_intent:"
_CURSOR = "run_key_intent_cursor"
_logger = logging.getLogger(__name__)


def record_issuance_intent(
    store: RunStore, run_id: str, limit: float, expiry: datetime, now: datetime
) -> None:
    """Commit the exact request identity before the issuer can create a key."""
    ledger_id = store.read_ledger_id()
    if ledger_id is None:
        raise credential_issuer.IssuerRefused()
    state = {
        "run_id": run_id,
        "name": credential_issuer.run_key_name(ledger_id, run_id),
        "limit_usd": limit,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "issued_by": os.getpid(),
        "timestamp": now.isoformat(),
        "pending": True,
    }
    if not store.compare_set_meta(
        INTENT_PREFIX + run_id, None, json.dumps(state, sort_keys=True)
    ):
        raise credential_issuer.IssuerRefused()


def settle_issued_intent(store: RunStore | None, run_id: str) -> None:
    """Transfer responsibility after the caller's strict issuance readback."""
    if store is None:
        return
    row_key = INTENT_PREFIX + run_id
    previous = store.get_meta(row_key)
    if previous is None:
        return
    state = json.loads(previous)
    state.update(pending=False, outcome="issuance recorded")
    store.compare_set_meta(row_key, previous, json.dumps(state, sort_keys=True))


def _claim_intent(
    store: RunStore, row_key: str, previous: str, now: datetime
) -> dict[str, Any] | None:
    state: dict[str, Any] = json.loads(previous)
    if not state["pending"]:
        return None
    run = store.get(state["run_id"])
    if run is not None and run.status == "pending":
        return None
    due = state.get("next_attempt_at", state["expires_at"])
    if datetime.fromisoformat(due) > now:
        return None
    from kew.credential_reconciliation import revocation_retry_schedule

    count, delay = revocation_retry_schedule(state.get("retry_count", 0))
    state["retry_count"] = count
    state["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat()
    if store.compare_set_meta(row_key, previous, json.dumps(state, sort_keys=True)):
        return state
    return None


def _recover_named_keys(
    store: RunStore, state: dict[str, Any], now: datetime, seen: set[str]
) -> str | None:
    from kew.credential_reconciliation import _already_revoked
    from kew.run_credentials import _revoke

    ledger_id = store.read_ledger_id()
    if ledger_id is None or state["name"] != credential_issuer.run_key_name(
        ledger_id, state["run_id"]
    ):
        raise credential_issuer.IssuerRefused()
    hashes = credential_issuer.lookup_run_key_hashes(state["name"])
    for key_hash in hashes:
        if key_hash not in seen:
            seen.add(key_hash)
            key = credential_issuer.BoundedRunKey(
                "",
                key_hash,
                state["limit_usd"],
                state["expires_at"],
                name=state["name"],
            )
            _revoke(store, state["run_id"], key, now=now)
        if not _already_revoked(store, state["run_id"], key_hash):
            return None
    if hashes:
        return "confirmed deletion"
    # A local timeout does not cancel issuer work or establish list consistency.
    expiry = datetime.fromisoformat(state["expires_at"])
    return "confirmed absence" if now > expiry else None


def _recover_intent(
    store: RunStore, row_key: str, state: dict[str, Any], now: datetime, seen: set[str]
) -> None:
    previous = json.dumps(state, sort_keys=True)
    reason = "revocation unconfirmed or issuance request may still complete"
    try:
        outcome = _recover_named_keys(store, state, now, seen)
    except credential_issuer.IssuerError as exc:
        outcome, reason = None, exc.reason
    except Exception:
        outcome, reason = None, "issuer unavailable"
    if outcome is not None:
        state.update(pending=False, outcome=outcome, settled_at=now.isoformat())
    else:
        state["reason"] = reason
    warn = outcome is None and not state.get("warned")
    if warn:
        state["warned"] = True
    saved = store.compare_set_meta(row_key, previous, json.dumps(state, sort_keys=True))
    if saved and warn:
        _logger.warning(
            "issuance unresolved: run=%s outcome=%s expires_at=%s; "
            "restore management lookup access and retry reconciliation",
            state["run_id"],
            reason,
            state["expires_at"],
        )


def reconcile_issuance_intents(
    store: RunStore, *, now: datetime, seen: set[str], limit: int
) -> None:
    """Scan one meta page; never replay a POST or infer deletion from expiry."""
    rows = store.list_meta(
        INTENT_PREFIX, after=store.get_meta(_CURSOR) or "", limit=limit
    )
    for row_key, previous in rows:
        state = _claim_intent(store, row_key, previous, now)
        if state is not None:
            _recover_intent(store, row_key, state, now, seen)
    store.set_meta(_CURSOR, rows[-1][0] if len(rows) == limit else "")
