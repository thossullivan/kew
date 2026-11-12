"""Parent-owned issuance and bounded reconciliation of detached run credentials."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from kew import credential_issuer
from kew.audit import Actor, emit
from kew.credential_evidence import record_issuance_evidence
from kew.credential_handoff import DriverCredentialRoute
from kew.credential_intents import settle_issued_intent
from kew.credential_prelaunch import PRELAUNCH_PROTOCOL, record_child_spawned
from kew.credential_reconciliation import reconcile_run_keys
from kew.graph.driver.credential_route import resolve_driver_credential_route
from kew.ids import branch_slug
from kew.run_credentials import (
    _revoke,
    mint_run_credential,
    retry_schedule_seconds,
    run_timeout_seconds,
)
from kew.run_store import RunRecord
from kew.sandbox import WALL_CLOCK_LIMIT_SECONDS

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.credential_issuer import BoundedRunKey
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)
KEY_SCAN_LIMIT = 100
# Only this process's prepared keys belong to its exit cleanup. No PID probing.
_prepared_keys: WeakKeyDictionary[
    RunStore, dict[tuple[int, str, str], BoundedRunKey]
] = WeakKeyDictionary()


def _forget_prepared(store: RunStore, run_id: str, key_hash: str) -> None:
    _prepared_keys.get(store, {}).pop((os.getpid(), run_id, key_hash), None)


def driver_key_spawned(store: RunStore, run_id: str, key: BoundedRunKey) -> None:
    """Transfer ownership before a failed marker update could trigger cleanup."""
    _forget_prepared(store, run_id, key.key_hash)
    record_child_spawned(store, run_id, key.key_hash)


def abandon_driver_key(store: RunStore, run_id: str, key: BoundedRunKey) -> None:
    """Tombstone an unspawned key before DELETE and retain failed cleanup."""
    _revoke(store, run_id, key)
    _forget_prepared(store, run_id, key.key_hash)


def _close_prepared_driver_keys(store: RunStore) -> None:
    for (pid, run_id, _), key in list(_prepared_keys.get(store, {}).items()):
        if pid == os.getpid():
            run = store.get(run_id)
            if run is not None and run.status == "pending":
                _forget_prepared(store, run_id, key.key_hash)
                continue
            abandon_driver_key(store, run_id, key)


class NoProvisioningKey(credential_issuer.IssuerUnavailable):
    reason = "no provisioning key"


def driver_key_lifetime_seconds(config: DispatchConfig) -> float:
    """Cover whichever bounded schedule the detached child enforces.

    A host child retries under the configured schedule. A contained child
    enforces the four-hour wall-clock backstop from container creation and
    may spend up to one attempt's timeout preparing that container, so a key
    sized to a shorter schedule was refused by the child it was minted for
    (governed review round 2, 2026-09-21). With containment switched off the
    schedule alone bounds the child.
    """
    timeout_seconds = run_timeout_seconds(config, None)
    schedule = retry_schedule_seconds(config.retry, timeout_seconds)
    if config.sandbox.enabled is False:
        return schedule
    return max(schedule, WALL_CLOCK_LIMIT_SECONDS + timeout_seconds)


def issue_driver_key(
    store: RunStore,
    config: DispatchConfig,
    run_id: str,
    item_id: str,
    *,
    route: dict[str, object] | None = None,
    route_digest: str | None = None,
) -> BoundedRunKey:
    """Persist verifiable issuance before any child can receive the secret."""
    if not credential_issuer.has_provisioning_key():
        raise NoProvisioningKey()
    key = mint_run_credential(
        run_id=run_id,
        config=config,
        store=store,
        timeout_seconds=driver_key_lifetime_seconds(config),
    )
    _prepared_keys.setdefault(store, {})[(os.getpid(), run_id, key.key_hash)] = (
        credential_issuer.BoundedRunKey(
            "", key.key_hash, key.limit_usd, key.expires_at, name=key.name
        )
    )
    try:
        # A failed evidence write abandons the minted key (re-audit F9).
        record_issuance_evidence(
            store,
            run_id,
            {
                "phase": "issuance",
                "prelaunch_protocol": PRELAUNCH_PROTOCOL,
                "attempt_number": 1,
                "issued_by": "driver",
                "credential_route": route,
                "credential_route_digest": route_digest,
                "credential_issuer": key.identity,
                "secret_sha256": hashlib.sha256(key.secret.encode()).hexdigest(),
            },
            item_id=item_id,
        )
        settle_issued_intent(store, run_id)
    except BaseException:
        abandon_driver_key(store, run_id, key)
        raise
    return key


def prepare_driver_key(
    store: RunStore, config: DispatchConfig, run_id: str, item_id: str
) -> tuple[DriverCredentialRoute, BoundedRunKey | None]:
    """Resolve the route and persist its issuance, exclusion, or refusal."""
    route: dict[str, object] | None = None
    try:
        decision = resolve_driver_credential_route(config, item_id)
        route = decision.evidence()
        if decision.bounded_key:
            return decision, issue_driver_key(
                store,
                config,
                run_id,
                item_id,
                route=route,
                route_digest=decision.digest,
            )
        emit(
            store,
            "sandbox_resolved",
            actor=Actor("policy", "credential-issuer"),
            run_id=run_id,
            item_id=item_id,
            payload={
                "phase": "issuance",
                "credential_route": route,
                "credential_route_digest": decision.digest,
                "credential_issuer": None,
                "credential_issuer_reason": "unsupported route",
            },
        )
        return decision, None
    except Exception as exc:
        reason = (
            exc.reason
            if isinstance(exc, credential_issuer.IssuerError)
            else "issuer unavailable"
            if route is not None
            else "route resolution failed"
        )
        record_driver_refusal(store, config, run_id, item_id, reason, route=route)
        if isinstance(exc, credential_issuer.IssuerError):
            raise
        raise credential_issuer.IssuerUnavailable() from None


def record_driver_refusal(
    store: RunStore,
    config: DispatchConfig,
    run_id: str,
    item_id: str,
    reason: str,
    *,
    route: dict[str, object] | None = None,
) -> None:
    """Record a pre-launch refusal without retaining exception diagnostics."""
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id) if item_id.isdecimal() else 0,
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            status="refused",
            exit_code=1,
            started_at=now,
            finished_at=now,
            error_category="credential_issuer_refused",
        )
    )
    emit(
        store,
        "dispatch_completed",
        actor=Actor("policy", "credential-issuer"),
        run_id=run_id,
        item_id=item_id,
        payload={
            "error_category": "credential_issuer_refused",
            "reason": reason,
            "credential_route": route,
        },
    )


def reconcile_driver_keys(
    store: RunStore,
    *,
    now: datetime | None = None,
    seen: set[str] | None = None,
    exiting: bool = False,
) -> None:
    """Reconcile both issuance sources through the shared bounded retry path."""
    if exiting:
        _close_prepared_driver_keys(store)
    reconcile_run_keys(store, now=now, seen=seen, limit=KEY_SCAN_LIMIT)


@contextmanager
def driver_key_cleanup(store: RunStore) -> Iterator[set[str]]:
    """Reconcile on driver exit, including exceptional exits; expiry bounds failure."""
    seen: set[str] = set()
    try:
        yield seen
    finally:
        try:
            reconcile_driver_keys(store, seen=seen, exiting=True)
        except Exception:
            _logger.warning("driver credential reconciliation unavailable")
