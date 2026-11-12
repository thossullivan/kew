"""Issuer-list defense against late visibility after local absence settlement."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kew import credential_issuer
from kew.credential_intents import INTENT_PREFIX
from kew.credential_prelaunch import prelaunch_state

if TYPE_CHECKING:
    from kew.run_store import RunStore

ORPHAN_SWEEP_GRACE_SECONDS = 300
_logger = logging.getLogger(__name__)


def _orphan_candidate(
    item: dict[str, object], run_id: str, now: datetime
) -> tuple[str, credential_issuer.BoundedRunKey] | None:
    try:
        created = datetime.fromisoformat(str(item["created_at"]))
        expiry = datetime.fromisoformat(str(item["expires_at"]))
        limit = float(str(item["limit"]))
        old_enough = now - created > timedelta(seconds=ORPHAN_SWEEP_GRACE_SECONDS)
    except (KeyError, ValueError, TypeError, OverflowError):
        _logger.warning("orphan scan: run=%s outcome=invalid issuer metadata", run_id)
        return None
    if not (
        run_id and old_enough and expiry.tzinfo and math.isfinite(limit) and limit > 0
    ):
        return None
    return run_id, credential_issuer.BoundedRunKey(
        "", str(item["hash"]), limit, expiry.isoformat(), name=str(item["name"])
    )


def _locally_owned(store: RunStore, run_id: str, key_hash: str, name: str) -> bool:
    raw = store.get_meta(INTENT_PREFIX + run_id)
    intent = json.loads(raw) if raw else {}
    matching_intent = intent.get("name") == name
    run = store.get(run_id)
    if matching_intent and run is not None and run.status == "pending":
        return True
    if matching_intent and intent["pending"]:
        return True
    if prelaunch_state(store, run_id, key_hash).get("name") == name:
        return True
    return _has_issuance(store, run_id, key_hash, name)


def _has_issuance(store: RunStore, run_id: str, key_hash: str, name: str) -> bool:
    from kew.credential_reconciliation import _lifecycle_identity

    for event in store.list_audit_events(
        event="sandbox_resolved", run_id=run_id, limit=None
    ):
        identity = _lifecycle_identity(event)
        if (
            identity is not None
            and identity["key_hash"] == key_hash
            and identity.get("name") == name
            and (event.payload or {}).get("phase") == "issuance"
        ):
            return True
    return False


def sweep_issuer_orphans(store: RunStore, *, now: datetime, seen: set[str]) -> None:
    """Delete unowned names after grace; persist and confirm every deletion."""
    from kew.credential_reconciliation import _already_revoked
    from kew.run_credentials import _revoke

    try:
        items = credential_issuer.list_run_keys()
    except credential_issuer.IssuerError:
        _logger.log(logging.DEBUG, "orphan scan outcome=issuer unavailable")
        return
    ledger_id = store.read_ledger_id()
    skipped = 0
    for item in items:
        identity = credential_issuer.parse_run_key_name(str(item["name"]))
        if identity is None or identity[0] != ledger_id:
            skipped += 1
            continue
        candidate = _orphan_candidate(item, identity[1], now)
        if candidate is None:
            continue
        run_id, key = candidate
        if key.key_hash in seen or _locally_owned(
            store, run_id, key.key_hash, str(item["name"])
        ):
            continue
        seen.add(key.key_hash)
        if not _already_revoked(store, run_id, key.key_hash):
            _revoke(store, run_id, key, now=now)
    _logger.debug("orphan scan: skipped outside ledger namespace=%d", skipped)
