"""Per-run issuer lifecycle, safe evidence, and launch-only environments."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from kew import credential_issuer
from kew.audit import Actor, emit
from kew.budget import effective_budget_view
from kew.credential_evidence import record_issuance_evidence
from kew.credential_handoff import direct_openrouter_route as direct_openrouter_route
from kew.credential_handoff import is_detached_credential_child
from kew.credential_intents import record_issuance_intent, settle_issued_intent
from kew.credential_issuer import INFERENCE_ENV, BoundedRunKey
from kew.credential_prelaunch import (
    PRELAUNCH_PROTOCOL,
    record_prelaunch,
    tombstone_absent_run,
)
from kew.credential_reconciliation import (
    begin_revocation,
    finish_revocation,
    reconcile_run_keys,
)
from kew.credential_surface import issued_secret_redaction
from kew.openshell_broker import run_credential_provider

if TYPE_CHECKING:
    from kew.config import DispatchConfig, RetryConfig
    from kew.readiness import RealizedLaunchBoundary
    from kew.run_store import RunStore

EXPIRY_GRACE_SECONDS = 300
RETRY_JITTER_MAX = 1.25
ISSUED_KEY_HASH_ENV = "KEW_ISSUED_RUN_KEY_HASH"


class CredentialLifetimeRefused(credential_issuer.IssuerRefused):
    reason = "credential lifetime insufficient"


class ProvisioningRequired(credential_issuer.IssuerRefused):
    """A direct OpenRouter route has no issuer, so no bounded credential can exist.

    Host launches strip the ambient credential like contained ones, so a host
    run without provisioning would start with no credential at all; it is
    refused before launch instead (governed review round 1, 2026-09-21).
    """

    reason = "OpenRouter provisioning key required for direct routes"


def retry_backoff_seconds(retry: RetryConfig, attempt: int) -> float:
    """Return the unjittered delay after the zero-based attempt."""
    return retry.backoff_base_seconds * retry.backoff_multiplier**attempt


def retry_schedule_seconds(
    retry: RetryConfig, timeout_seconds: float, *, start_attempt: int = 0
) -> float:
    """Bound every remaining attempt and the maximum jitter between attempts."""
    try:
        backoffs = (
            retry_backoff_seconds(retry, attempt) * RETRY_JITTER_MAX
            for attempt in range(start_attempt, retry.max_attempts - 1)
        )
        duration = (retry.max_attempts - start_attempt) * timeout_seconds
        for backoff in backoffs:
            if not math.isfinite(backoff) or backoff < 0:
                raise CredentialLifetimeRefused()
            duration += backoff
        if not math.isfinite(duration) or timeout_seconds <= 0 or duration <= 0:
            raise CredentialLifetimeRefused()
        return duration
    except (OverflowError, ValueError):
        raise CredentialLifetimeRefused() from None


def _expiry_after(seconds: float) -> datetime:
    """Refuse nonfinite or unrepresentable lifetimes before issuer requests."""
    try:
        if not math.isfinite(seconds) or seconds <= 0:
            raise CredentialLifetimeRefused()
        return datetime.now(UTC) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        raise CredentialLifetimeRefused() from None


def require_credential_lifetime(key: BoundedRunKey | None, seconds: float) -> None:
    """Refuse launch when the key cannot cover the remaining bounded schedule."""
    if key is None:
        return
    try:
        expiry = datetime.fromisoformat(key.expires_at.replace("Z", "+00:00"))
        if expiry <= _expiry_after(seconds):
            raise CredentialLifetimeRefused()
    except (TypeError, ValueError):
        raise CredentialLifetimeRefused() from None


def require_retry_credential_lifetime(
    key: BoundedRunKey | None,
    retry: RetryConfig,
    timeout_seconds: float,
    *,
    start_attempt: int,
) -> None:
    """Check the remaining retry schedule only when issuance bounds this run."""
    if key is not None:
        require_credential_lifetime(
            key,
            retry_schedule_seconds(retry, timeout_seconds, start_attempt=start_attempt),
        )


def issuer_reason(*, supported: bool) -> str:
    if not supported:
        return "unsupported route"
    return (
        "issuance pending"
        if credential_issuer.has_provisioning_key()
        else "no provisioning key"
    )


def launch_environment(
    environment: Mapping[str, str],
    key: BoundedRunKey | None,
    *,
    excluded: bool = False,
    keep: frozenset[str] = frozenset(),
    contained: bool = False,
) -> dict[str, str]:
    """Copy the environment, remove management authority, and replace inference."""
    result = {
        name: value
        for name, value in environment.items()
        if name not in credential_issuer.MANAGEMENT_CREDENTIAL_ENVS
    }
    if excluded:
        from kew.credential_handoff import CLAUDE_CREDENTIAL_NAMES
        from kew.runners.codex import CODEX_CREDENTIAL_ENV_NAMES

        inference_names = (CODEX_CREDENTIAL_ENV_NAMES | CLAUDE_CREDENTIAL_NAMES) - {
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }
        for name in (inference_names - keep) | {
            "LEMONADE_API_KEY",
            ISSUED_KEY_HASH_ENV,
        }:
            result.pop(name, None)
    if contained:
        result.pop(INFERENCE_ENV, None)
    if key is not None:
        result[INFERENCE_ENV] = key.secret
    return result


def openshell_launch_environment(
    environment: Mapping[str, str], runner: str
) -> dict[str, str]:
    """The child environment for an OpenShell launch: names, never values.

    This is :func:`launch_environment`'s counterpart for the runtime where the
    gateway owns every credential, and the two differ in SHAPE, not only in
    which names they name. ``launch_environment`` copies the caller's
    environment and removes authority from the copy, because its child is a
    host process that needs the host's environment. Here the child is a
    CONTAINED AGENT — ``openshell sandbox exec`` forwards this mapping into the
    sandbox — so it is built from :func:`kew.openshell.sandbox_exec_environment`
    instead: an allowlist of the few names the launcher and the CLI need, and
    nothing else.

    That shape is the fix for the leak the denylist was. Stripping management,
    inference and handoff authority plus the GitHub tokens left every OTHER
    host variable in place, so a secret kew does not enumerate — a CI token, a
    cloud key, an IDE session token — reached the agent, while the docker
    boundary beside it forwarded the runner's declared credential names and
    nothing else (PR #2401 review).

    On top of the allowlist goes the one binding the sandbox cannot get any
    other way: each credential variable the runner declares, bound to
    :data:`~kew.openshell_broker.PLACEHOLDER_CREDENTIAL_VALUE`. The names stay
    because the runner CLIs require them — an unset or empty variable makes
    claude and the OpenRouter-backed runners exit before the first model call,
    which is why the 2026-10-03 spike ran them on placeholders — while the
    VALUES are what this function exists to remove, the bounded key for this
    run included, since that one lives in the per-run provider instead.
    """
    from kew.openshell import sandbox_exec_environment
    from kew.openshell_broker import credential_placeholders

    result = sandbox_exec_environment(environment)
    result.update(credential_placeholders(runner))
    return result


def _credential_payload(
    key: BoundedRunKey | None,
    *,
    phase: str,
    reason: str | None,
    revoked: bool | None,
    attempt_number: int,
) -> dict[str, Any]:
    identity = None if key is None else key.identity
    if identity is not None and revoked is not None:
        identity["revoked"] = revoked
        if reason is not None:
            identity["reason"] = reason
    return {
        "prelaunch_protocol": PRELAUNCH_PROTOCOL,
        "phase": phase,
        "attempt_number": attempt_number,
        "credential_issuer": identity,
        "credential_issuer_reason": reason,
        "secret_sha256": (
            hashlib.sha256(key.secret.encode()).hexdigest()
            if key is not None and phase == "issuance"
            else None
        ),
    }


def _record(
    store: RunStore | None,
    run_id: str,
    key: BoundedRunKey | None,
    *,
    phase: str,
    reason: str | None = None,
    revoked: bool | None = None,
    attempt_number: int = 1,
) -> None:
    payload = _credential_payload(
        key, phase=phase, reason=reason, revoked=revoked, attempt_number=attempt_number
    )
    if key is not None and phase == "issuance":
        record_issuance_evidence(store, run_id, payload)
        return
    emit(
        store,
        "sandbox_resolved",
        actor=Actor("policy", "credential-issuer"),
        run_id=run_id,
        dedup_key=(
            f"key-{phase}:{run_id}:{key.key_hash}:{revoked}:{reason}"
            if key is not None and phase in {"revocation", "revocation_pending"}
            else None
        ),
        payload={
            **_previous_resolution(store, run_id),
            **payload,
        },
    )


def _revoke(
    store: RunStore | None,
    run_id: str,
    key: BoundedRunKey,
    *,
    now: datetime | None = None,
) -> None:
    state = None
    if store is not None:
        tombstone_absent_run(store, run_id, key.key_hash, now or datetime.now(UTC))
        state = begin_revocation(store, run_id, key, now or datetime.now(UTC))
        if state is None:
            return
        _record(store, run_id, key, phase="revocation_pending")
    reason = None
    revoked = False
    try:
        outcome = credential_issuer.revoke_run_key(key.key_hash, issued_key=key.secret)
        reason = "already absent" if outcome == "already absent" else None
        revoked = True
    except credential_issuer.IssuerError as exc:
        reason = exc.reason
    except Exception:
        reason = "issuer unavailable"
    _record(store, run_id, key, phase="revocation", reason=reason, revoked=revoked)
    if store is not None and state is not None:
        finish_revocation(store, state, revoked=revoked, reason=reason)


def record_credential_attempt(
    store: RunStore, run_id: str, key: BoundedRunKey | None, attempt_number: int
) -> None:
    """Bind a shared run key to each retry before its paid launch."""
    if key is not None and attempt_number > 1:
        _record(store, run_id, key, phase="issuance", attempt_number=attempt_number)


def _finalize_run_key(store: RunStore | None, run_id: str, key: BoundedRunKey) -> None:
    _revoke(store, run_id, key)
    if store is not None:
        reconcile_run_keys(store, seen={key.key_hash}, pending_only=True)


@contextmanager
def bounded_run_credentials(
    *,
    run_id: str,
    config: DispatchConfig,
    store: RunStore | None,
    model: str,
    credential_names: tuple[str, ...],
    timeout_seconds: float,
    limit_usd: float | None = None,
    direct: bool = True,
    contained: bool = False,
    retry: RetryConfig | None = None,
) -> Iterator[BoundedRunKey | None]:
    """Own one key across retries and revoke it on every unwinding path.

    Use the caller's effective cap, then the preflight runtime cap. If absent,
    use configured daily remaining spend. Expiry covers the full retry schedule
    with maximum jitter, plus five minutes of grace. Container streamers pass
    their single enforced wall-clock limit without a retry schedule.
    Switchyard has a separate upstream owner and retains its explicit No route.
    Direct OpenRouter routes require provisioning or a verified key, host or
    contained; host launches filter ambient OpenRouter authentication too.

    Containment is not a parameter to whether a key EXISTS: that question has
    the same answer on the host and in a container, and each launch site filters
    the ambient credential out of the environment it hands its child (governed
    review round 7, 2026-09-22). ``contained`` governs only WHERE the minted
    secret is put, and matters on one runtime: a contained launch on
    ``[sandbox] runtime = "openshell"`` is handed no credential value at all, so
    the key goes into a per-run gateway provider. An uncontained run on that
    same config executes on the host with the value in its environment, and
    minting a provider for it would attest a gateway boundary it does not have
    — on a host that need not have the ``openshell`` CLI installed to run it.
    """
    supported = direct_openrouter_route(model, credential_names, direct=direct)
    if ISSUED_KEY_HASH_ENV in os.environ:
        key = inherited_run_credential(store, run_id, supported=supported)
        duration = (
            timeout_seconds
            if retry is None
            else retry_schedule_seconds(retry, timeout_seconds)
        )
        require_credential_lifetime(key, duration)
        _record(store, run_id, key, phase="inherited")
        # An inherited key is this run's bounded key too, so it goes into this
        # run's gateway provider exactly as a locally minted one does. Leaving
        # the broker out here created no provider while the launch sites still
        # recorded `broker:openshell:kew-run-<run_id>` as what authenticated
        # the attempt — an auth path naming a provider that does not exist,
        # beside a posture event naming the operator's long-lived one (#2318
        # review). Revocation belongs to the driver that minted the key, so
        # there is no `finally` to nest this inside: the provider is deleted
        # when this child's dispatch ends, before the driver revokes.
        with run_credential_provider(
            config.sandbox, run_id, key.secret, contained=contained
        ):
            yield key
        return
    if supported and is_detached_credential_child():
        raise IssuedCredentialMismatch()
    reason = issuer_reason(supported=supported)
    if supported and reason == "no provisioning key":
        _record(
            store,
            run_id,
            None,
            phase="issuance",
            reason=ProvisioningRequired.reason,
        )
        raise ProvisioningRequired()
    if reason != "issuance pending":
        if supported:
            _record(store, run_id, None, phase="issuance", reason=reason)
        yield None
        return
    key = mint_run_credential(
        run_id=run_id,
        config=config,
        store=store,
        timeout_seconds=timeout_seconds,
        limit_usd=limit_usd,
        retry=retry,
    )
    try:
        # Inside the revoking try: a failed issuance record must not leave the
        # minted key live (re-audit F9, 2026-09-22).
        _record(store, run_id, key, phase="issuance")
        settle_issued_intent(store, run_id)
        # The per-run provider is nested INSIDE the revoking `finally`, which
        # is what orders the two teardowns: the provider holding the key is
        # gone before the key is revoked, never a revoked key left sitting in
        # a live provider for the gateway to inject. For any launch this
        # runtime does not broker — another runtime, or an uncontained run on
        # this one — the broker yields without touching a gateway.
        with (
            issued_secret_redaction(key.secret),
            run_credential_provider(
                config.sandbox, run_id, key.secret, contained=contained
            ),
        ):
            yield key
    finally:
        _finalize_run_key(store, run_id, key)


def mint_run_credential(
    *,
    run_id: str,
    config: DispatchConfig,
    store: RunStore | None,
    timeout_seconds: float,
    limit_usd: float | None = None,
    retry: RetryConfig | None = None,
) -> BoundedRunKey:
    """Apply the shared spend cap and complete retry lifetime before issuance."""
    view = effective_budget_view(config, store, None)
    limit: float | None = view.runtime_cap_usd if limit_usd is None else limit_usd
    if limit is None:
        limit = view.daily_remaining_usd
    if limit is None:
        raise credential_issuer.IssuerRefused()
    duration = (
        timeout_seconds
        if retry is None
        else retry_schedule_seconds(retry, timeout_seconds)
    )
    expiry = _expiry_after(duration + EXPIRY_GRACE_SECONDS)
    if store is None:
        raise credential_issuer.IssuerRefused()
    ledger_id = store.read_ledger_id()
    if ledger_id is None:
        raise credential_issuer.IssuerRefused()
    record_issuance_intent(store, run_id, limit, expiry, datetime.now(UTC))
    key = credential_issuer.mint_bounded_run_key(
        run_id,
        limit,
        expiry,
        ledger_id=ledger_id,
        on_malformed=partial(_revoke, store, run_id),
    )
    try:
        if store is not None:
            record_prelaunch(store, run_id, key, datetime.now(UTC))
        require_credential_lifetime(key, duration)
    except BaseException:
        _revoke(store, run_id, key)
        raise
    return key


class IssuedCredentialMismatch(credential_issuer.IssuerRefused):
    reason = "issued credential mismatch"


def inherited_run_credential(
    store: RunStore | None, run_id: str, *, supported: bool
) -> BoundedRunKey:
    """Verify the held secret against the parent's durable issuance identity."""
    if not supported or store is None:
        raise IssuedCredentialMismatch()
    marker = os.environ[ISSUED_KEY_HASH_ENV]
    payload = _inherited_issuance(store, run_id, marker)
    secret = os.environ.get(INFERENCE_ENV, "")
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
    if not secret or not hmac.compare_digest(
        fingerprint, payload.get("secret_sha256", "")
    ):
        raise IssuedCredentialMismatch()
    identity = payload["credential_issuer"]
    return BoundedRunKey(
        secret,
        marker,
        identity["limit_usd"],
        identity["expires_at"],
        name=identity.get("name"),
    )


def _inherited_issuance(store: RunStore, run_id: str, marker: str) -> dict[str, Any]:
    events = store.list_audit_events(
        event="sandbox_resolved", run_id=run_id, limit=None
    )
    for event in events:
        payload = event.payload or {}
        identity = payload.get("credential_issuer") or {}
        if identity.get("key_hash") != marker:
            continue
        if payload.get("phase") == "revocation":
            raise IssuedCredentialMismatch()
        if payload.get("phase") == "issuance" and payload.get("issued_by") == "driver":
            return payload
    raise IssuedCredentialMismatch()


def credential_identity(key: BoundedRunKey | None) -> dict[str, object] | None:
    return None if key is None else key.identity


def credential_boundary(
    boundary: RealizedLaunchBoundary, key: BoundedRunKey | None
) -> RealizedLaunchBoundary:
    return boundary if key is None else replace(boundary, auth_path=key.auth_path)


def run_timeout_seconds(config: DispatchConfig, override_minutes: int | None) -> int:
    return (override_minutes or config.agent.timeout_minutes) * 60


def _previous_resolution(store: RunStore | None, run_id: str) -> dict[str, object]:
    """Keep the resolved posture visible when appending issuer lifecycle facts."""
    if store is None:
        return {}
    events = store.list_audit_events(event="sandbox_resolved", run_id=run_id, limit=1)
    if events and isinstance(events[0].payload, dict):
        return dict(events[0].payload)
    return {}
