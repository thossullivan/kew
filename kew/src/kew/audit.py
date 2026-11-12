"""Audit envelope: the single chokepoint for governance event writes (#246).

Every audit event carries the envelope — who (actor), which repo, which
configured endpoint — with policy_hash (#250) and prev_hash (#249) slots
reserved. Storage is the audit_events table in dispatch.db; this module
talks only through the store's API (no SQLite-specific SQL lives here).

Writes are best-effort by default (the completeness caveat in #249 applies).
Required facts can request strict writes, which raise when persistence fails.

This module also hosts the ``kew audit`` read CLI (``tail`` and ``export``
sub-commands) for inspecting the stored audit trail.
"""

from __future__ import annotations

import csv
import getpass
import hashlib
import io
import json
import logging
import os
import re
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import typer

from kew.audit_chain import (
    GENESIS_PREV,
    Anchor,
    AnchorChainResult,
    AnchorReport,
    ChainResult,
    ChainRow,
    anchor_hash,
    classify_anchors,
    format_anchor,
    parse_anchors,
    verify_anchor_chain,
    verify_chain,
)
from kew.audit_identity import (  # noqa: F401 — re-exported for callers
    CONTINUATION_IDENTITY_FIELDS,
    _sanitize_recovery_token,
    _sanitized_worktree_identity,
    continuation_evidence,
    continuation_evidence_identity,
)
from kew.config import DispatchConfig, load_config, project_root
from kew.egress_certification import RuntimeEgressCertification
from kew.ids import item_ref
from kew.ledger_identity import (
    LedgerIdentityError,
    canonical_project_root,
    open_managed_store,
)
from kew.readiness import (
    SidecarPacketCertification,
    SidecarPacketEvidence,
    SidecarTopologyEvidence,
    sidecar_packet_policy_sha256,
)
from kew.run_store import AuditEvent, SqliteRunStore
from kew.switchyard import sanitize_endpoint

DEFAULT_ANCHORS_PATH = "audit-anchors.jsonl"
SIDECAR_EVIDENCE_EVENT = "sidecar_isolation_evidence"
SIDECAR_PACKET_CERTIFICATION_EVENT = "sidecar_packet_certification"
#: SC-0's successor for the runtime-neutral boundary proof (``openshell`` and
#: any later runtime). Its own event name, not SC-0's: the payload binds a
#: different tuple, and a reader that matched both would compare fields one of
#: them does not carry.
RUNTIME_EGRESS_CERTIFICATION_EVENT = "runtime_egress_certification"

if TYPE_CHECKING:
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

ActorType = Literal["human", "policy", "agent"]

#: Module-level audit context, set once at preflight (repo + policy binding).
_audit_repo: str | None = None
_audit_policy_hash: str | None = None
_audit_policy_snapshot: str | None = None


@dataclass(frozen=True)
class Actor:
    """Who performed an audited action."""

    type: ActorType
    id: str


class SidecarEvidencePersistenceError(RuntimeError):
    """Required sidecar evidence did not reach the durable audit chain."""


class SidecarPacketEvidenceUnavailable(RuntimeError):
    """No durable successful packet matrix matches the requested images."""


class EgressCertificationUnavailable(RuntimeError):
    """No durable passing egress proof matches the requested boundary tuple."""


def egress_certification_actor(runtime: str) -> Actor:
    """The one policy identity allowed to sign ``runtime``'s egress proof.

    Matching requires this exact actor, so an event appended by anything else —
    a test fixture, an operator's hand-written row, another gate — cannot
    authorize a dispatch. The identity is derived from the runtime rather than
    enumerated, so a fourth boundary cannot accidentally reuse a third's.
    """
    if not runtime or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", runtime):
        raise ValueError(f"invalid runtime for an egress certification: {runtime!r}")
    return Actor("policy", f"{runtime}-egress-gate")


def sidecar_evidence(evidence: SidecarTopologyEvidence) -> dict[str, object]:
    """Build the validated secret-free payload for one sidecar evidence row."""
    if not isinstance(evidence, SidecarTopologyEvidence):
        raise ValueError("sidecar evidence must be validated before persistence")
    return evidence.to_dict()


def append_sidecar_evidence(
    store: RunStore,
    evidence: SidecarTopologyEvidence,
    *,
    actor: Actor,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
) -> int:
    """Strictly append one required sidecar fact through the chained writer."""
    append_once = getattr(store, "append_audit_event_once", None)
    if not callable(append_once):
        raise SidecarEvidencePersistenceError("sidecar evidence storage is unavailable")
    payload = sidecar_evidence(evidence)
    digest = hashlib.sha256(evidence.to_json().encode()).hexdigest()
    try:
        event_id = append_once(
            dedup_key=(
                f"{SIDECAR_EVIDENCE_EVENT}:{evidence.run_id}:"
                f"{evidence.attempt_number}:{evidence.stage}:{digest}"
            ),
            timestamp=datetime.now(UTC).isoformat(),
            event=SIDECAR_EVIDENCE_EVENT,
            run_id=evidence.run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=repo if repo is not None else _audit_repo,
            actor_type=actor.type,
            actor_id=actor.id,
            policy_hash=_audit_policy_hash,
            payload=payload,
        )
    except Exception as exc:
        raise SidecarEvidencePersistenceError(
            "sidecar evidence could not be persisted"
        ) from exc
    if type(event_id) is not int or event_id < 1:
        raise SidecarEvidencePersistenceError(
            "sidecar evidence persistence was not confirmed"
        )
    return event_id


def append_sidecar_packet_certification(
    store: RunStore,
    certification: SidecarPacketCertification,
    *,
    actor: Actor,
    repo: str | None = None,
) -> int:
    """Strictly append one operator-observed SC-0 packet certification."""
    if not isinstance(certification, SidecarPacketCertification):
        raise ValueError("sidecar packet certification must be validated")
    if certification.packet.verdict != "passed":
        raise ValueError("sidecar packet certification did not pass")
    append_once = getattr(store, "append_audit_event_once", None)
    if not callable(append_once):
        raise SidecarEvidencePersistenceError(
            "sidecar packet certification storage is unavailable"
        )
    digest = hashlib.sha256(certification.to_json().encode()).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    try:
        event_id = append_once(
            dedup_key=f"{SIDECAR_PACKET_CERTIFICATION_EVENT}:{digest}:{timestamp}",
            timestamp=timestamp,
            event=SIDECAR_PACKET_CERTIFICATION_EVENT,
            repo=repo if repo is not None else _audit_repo,
            actor_type=actor.type,
            actor_id=actor.id,
            policy_hash=_audit_policy_hash,
            payload=certification.to_dict(),
        )
    except Exception as exc:
        raise SidecarEvidencePersistenceError(
            "sidecar packet certification could not be persisted"
        ) from exc
    if type(event_id) is not int or event_id < 1:
        raise SidecarEvidencePersistenceError(
            "sidecar packet certification persistence was not confirmed"
        )
    return event_id


def append_runtime_egress_certification(
    store: RunStore,
    certification: RuntimeEgressCertification,
    *,
    actor: Actor,
    repo: str | None = None,
) -> int:
    """Strictly append one passing runtime egress certification.

    Refuses anything that is not a passing proof. A failed or unavailable
    matrix is still worth an operator's attention, but it is not evidence and
    must never sit on the chain where a matcher could read it as a pass — the
    prober reports the failure to the operator instead.
    """
    if not isinstance(certification, RuntimeEgressCertification):
        raise ValueError("runtime egress certification must be validated")
    if certification.verdict != "passed":
        raise ValueError("runtime egress certification did not pass")
    if actor != egress_certification_actor(certification.runtime):
        raise ValueError("runtime egress certification actor is invalid")
    append_once = getattr(store, "append_audit_event_once", None)
    if not callable(append_once):
        raise SidecarEvidencePersistenceError(
            "runtime egress certification storage is unavailable"
        )
    digest = hashlib.sha256(certification.to_json().encode()).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    try:
        event_id = append_once(
            dedup_key=(f"{RUNTIME_EGRESS_CERTIFICATION_EVENT}:{digest}:{timestamp}"),
            timestamp=timestamp,
            event=RUNTIME_EGRESS_CERTIFICATION_EVENT,
            repo=repo if repo is not None else _audit_repo,
            actor_type=actor.type,
            actor_id=actor.id,
            policy_hash=_audit_policy_hash,
            payload=certification.to_dict(),
        )
    except Exception as exc:
        raise SidecarEvidencePersistenceError(
            "runtime egress certification could not be persisted"
        ) from exc
    if type(event_id) is not int or event_id < 1:
        raise SidecarEvidencePersistenceError(
            "runtime egress certification persistence was not confirmed"
        )
    return event_id


#: How many evidence events one lookup reads per page. The evidence window is
#: read in PAGES rather than truncated at the first one: ``kew sandbox certify``
#: is a shipped command, so valid proofs accumulate from ordinary use, and a
#: truncating lookup refused every OpenShell dispatch once this many landed
#: inside the same 24-hour window — with "run certify again" as the printed
#: remedy, which appended one more (#2389 review).
_EVIDENCE_PAGE = 65

#: Hard ceiling on one lookup's total read. Still fail-closed, but out of reach
#: of certifying: a host that re-certifies before every dispatch does not write
#: this many proofs in one evidence window, so hitting it means something other
#: than the certify command is filling the ledger, and admission refuses rather
#: than reading on.
_EVIDENCE_SCAN_CAP = 10_000


def _evidence_window_events(
    store: RunStore, *, event: str, since: datetime
) -> tuple[list[AuditEvent], bool]:
    """Every event of one kind in the window, newest first, read in pages.

    Returns the events and whether the scan cap stopped it short. Overflow is
    reported rather than raised because each caller refuses in its OWN currency
    and the cause has to survive into the operator's message.
    """
    events: list[AuditEvent] = []
    after_id: int | None = None
    while True:
        page = store.list_audit_events(
            limit=_EVIDENCE_PAGE, event=event, since=since, after_id=after_id
        )
        events.extend(page)
        if len(page) < _EVIDENCE_PAGE:
            return events, False
        if len(events) >= _EVIDENCE_SCAN_CAP:
            return events, True
        after_id = page[-1].id


def runtime_egress_certification_mark(store: RunStore) -> int:
    """The newest runtime egress certification's event id, or ``0`` for none.

    The cheap half of :func:`matching_runtime_egress_certification`: one
    indexed row, no image inspection and no gateway call. It answers only
    "has anything been certified since I last looked?", which is the whole
    question a caller holding an UNCERTIFIED refusal has — a refusal can be
    overturned only by a new certification, and ``kew sandbox certify`` is the
    one thing that appends one.

    Nothing may admit on this: a mark says a proof was written, never that it
    matches this boundary or passed. A caller that sees it move must re-derive
    through the full lookup (``kew.service.dispatch_plan.RouteResolver``).
    Raises whatever the ledger raises — an unreadable ledger is not "unchanged".
    """
    events = store.list_audit_events(limit=1, event=RUNTIME_EGRESS_CERTIFICATION_EVENT)
    return int(events[0].id) if events else 0


def matching_runtime_egress_certification(
    store: RunStore,
    *,
    runtime: str,
    image_id: str,
    policy_sha256: str,
    gateway_version: str,
    gateway_identity: str,
    supervisor_digest: str,
    now: datetime | None = None,
) -> RuntimeEgressCertification:
    """Require fresh, unambiguous, passing proof for this exact boundary.

    Every refusal path raises :class:`EgressCertificationUnavailable`. There is
    no "probably fine" return: a store that cannot be read, a bounded lookup
    that filled up, a timestamp that will not parse and two certifications that
    disagree are all unavailable evidence, and admission treats unavailable
    evidence exactly like absent evidence.

    The window is read in pages (:func:`_evidence_window_events`), so the
    conflict check still sees every proof in it however many the operator has
    appended — truncating the read would have hidden an older FAILING proof for
    the same boundary behind the newest passing ones, which is the one
    direction this lookup must not fail in.
    """
    from kew.run_store_readiness import PROBE_EVIDENCE_TTL

    observed_now = now or datetime.now(UTC)
    expected = egress_certification_actor(runtime)
    try:
        events, overflowed = _evidence_window_events(
            store,
            event=RUNTIME_EGRESS_CERTIFICATION_EVENT,
            since=observed_now - PROBE_EVIDENCE_TTL,
        )
    except Exception as exc:
        raise EgressCertificationUnavailable(
            "runtime egress certification is unavailable"
        ) from exc
    if overflowed:
        raise EgressCertificationUnavailable(
            "runtime egress certification exceeds the bounded lookup"
        )
    binding = (
        image_id,
        policy_sha256,
        gateway_version,
        gateway_identity,
        supervisor_digest,
    )
    matching = [
        proof
        for event in events
        if (
            proof := _matching_egress_certification(
                event,
                runtime=runtime,
                binding=binding,
                expected=expected,
                now=observed_now,
            )
        )
        is not None
    ]
    if not matching:
        raise EgressCertificationUnavailable(
            f"no {runtime} egress certification matches this boundary, or it expired"
        )
    if len({proof.to_json() for proof in matching}) != 1:
        raise EgressCertificationUnavailable("runtime egress certification conflicts")
    if matching[0].verdict != "passed":
        raise EgressCertificationUnavailable(
            "runtime egress certification did not pass"
        )
    return matching[0]


def _matching_egress_certification(
    event: AuditEvent,
    *,
    runtime: str,
    binding: tuple[str, str, str, str, str],
    expected: Actor,
    now: datetime,
) -> RuntimeEgressCertification | None:
    payload = event.payload
    if not isinstance(payload, dict):
        raise EgressCertificationUnavailable(
            "runtime egress certification is malformed"
        )
    observed = (
        payload.get("image_id"),
        payload.get("policy_sha256"),
        payload.get("gateway_version"),
        payload.get("gateway_identity"),
        payload.get("supervisor_digest"),
    )
    if payload.get("runtime") != runtime or observed != binding:
        return None
    if (event.event, event.actor_type, event.actor_id) != (
        RUNTIME_EGRESS_CERTIFICATION_EVENT,
        expected.type,
        expected.id,
    ):
        raise EgressCertificationUnavailable(
            "runtime egress certification owner is invalid"
        )
    try:
        proof = RuntimeEgressCertification.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise EgressCertificationUnavailable(
            "runtime egress certification is malformed"
        ) from exc
    return proof if _egress_certification_is_fresh(event.timestamp, now) else None


def _egress_certification_is_fresh(timestamp: str, now: datetime) -> bool:
    from kew.run_store_readiness import PROBE_EVIDENCE_TTL

    try:
        observed = datetime.fromisoformat(timestamp)
        if observed.tzinfo is None or observed > now:
            raise ValueError("unusable observation time")
    except (TypeError, ValueError) as exc:
        raise EgressCertificationUnavailable(
            "runtime egress certification time is invalid"
        ) from exc
    return now - observed <= PROBE_EVIDENCE_TTL


def matching_sidecar_packet_evidence(
    store: RunStore,
    *,
    worker_image_id: str,
    proxy_image_id: str,
    now: datetime | None = None,
) -> SidecarPacketEvidence:
    """Require fresh, unambiguous operator proof for exact images and policy."""
    from kew.run_store_readiness import PROBE_EVIDENCE_TTL

    observed_now = now or datetime.now(UTC)
    try:
        certifications, overflowed = _evidence_window_events(
            store,
            event=SIDECAR_PACKET_CERTIFICATION_EVENT,
            since=observed_now - PROBE_EVIDENCE_TTL,
        )
    except Exception as exc:
        raise SidecarPacketEvidenceUnavailable(
            "sidecar packet evidence is unavailable"
        ) from exc
    if overflowed:
        raise SidecarPacketEvidenceUnavailable(
            "sidecar packet evidence exceeds the bounded lookup"
        )
    matching = [
        proof
        for event in certifications
        if (
            proof := _matching_packet_certification(
                event,
                worker_image_id=worker_image_id,
                proxy_image_id=proxy_image_id,
                now=observed_now,
            )
        )
        is not None
    ]
    if not matching:
        raise SidecarPacketEvidenceUnavailable(
            "matching sidecar packet evidence is missing or expired"
        )
    if len({proof.to_json() for proof in matching}) != 1:
        raise SidecarPacketEvidenceUnavailable("sidecar packet evidence conflicts")
    if matching[0].packet.verdict != "passed":
        raise SidecarPacketEvidenceUnavailable("sidecar packet evidence did not pass")
    return matching[0].packet


def _matching_packet_certification(
    event: AuditEvent,
    *,
    worker_image_id: str,
    proxy_image_id: str,
    now: datetime,
) -> SidecarPacketCertification | None:
    payload = event.payload
    if not isinstance(payload, dict):
        raise SidecarPacketEvidenceUnavailable("sidecar packet evidence is malformed")
    if (payload.get("worker_image_id"), payload.get("proxy_image_id")) != (
        worker_image_id,
        proxy_image_id,
    ):
        return None
    if (event.event, event.actor_type, event.actor_id) != (
        SIDECAR_PACKET_CERTIFICATION_EVENT,
        "policy",
        "sidecar-packet-gate",
    ):
        raise SidecarPacketEvidenceUnavailable(
            "sidecar packet evidence owner is invalid"
        )
    try:
        proof = SidecarPacketCertification.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise SidecarPacketEvidenceUnavailable(
            "sidecar packet evidence is malformed"
        ) from exc
    if proof.filter_policy_sha256 != sidecar_packet_policy_sha256():
        return None
    return proof if _packet_certification_is_fresh(event.timestamp, now) else None


def _packet_certification_is_fresh(timestamp: str, now: datetime) -> bool:
    from kew.run_store_readiness import PROBE_EVIDENCE_TTL

    try:
        observed = datetime.fromisoformat(timestamp)
        if observed.tzinfo is None or observed > now:
            raise ValueError("unusable observation time")
    except (TypeError, ValueError) as exc:
        raise SidecarPacketEvidenceUnavailable(
            "sidecar packet evidence time is invalid"
        ) from exc
    return now - observed <= PROBE_EVIDENCE_TTL


def transport_evidence(
    *,
    mode: str,
    endpoint: str | None,
    route: str | None,
    managed: bool,
    expected_version: str | None,
    observed_version: str | None,
    config_sha256: str | None,
    preflight_verdict: str | None,
    binary_sha256: str | None = None,
    credential_env_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the secret-free transport identity payload for an audit event.

    Only the endpoint origin is retained. Callers provide a digest of the
    admitted configuration bytes, never the bytes or credential values.
    """
    return {
        "transport_mode": mode,
        "transport_endpoint": sanitize_endpoint(endpoint) if endpoint else None,
        "transport_route": route,
        "transport_managed": managed,
        "transport_expected_version": expected_version,
        "transport_observed_version": observed_version,
        "transport_config_sha256": config_sha256,
        "transport_preflight_verdict": preflight_verdict,
        "transport_binary_sha256": binary_sha256,
        "transport_credential_env_names": list(credential_env_names),
    }


def _continuation_epic_refs(store: RunStore, epic_ref: str | None) -> list[str]:
    """Return the active epic refs that can own a continuation run."""
    if epic_ref:
        # Generic dispatch events carry the epic when the caller has it. Keep
        # that identity authoritative so an item reused by another epic cannot
        # make an otherwise unique run-less lookup appear ambiguous.
        return [epic_ref]
    refs: list[str] = []
    list_epics = getattr(store, "live_reservation_counts_by_epic", None)
    if callable(list_epics):
        with suppress(Exception):
            refs.extend(epic for epic in list_epics() if isinstance(epic, str))
    return list(dict.fromkeys(refs))


def _continuation_records_for_epic(list_continuations: Any, epic_ref: str) -> list[Any]:
    """Read one epic's continuation records without affecting audit writes."""
    try:
        records = list_continuations(epic_ref)
    except Exception:
        return []
    return list(records) if isinstance(records, list) else []


def _matching_continuation_records(
    list_continuations: Any,
    epic_ref: str,
    *,
    run_id: str | None,
    item_id: str | None,
) -> list[Any]:
    """Return records matching one continuation lookup's identity."""
    matches: list[Any] = []
    for record in _continuation_records_for_epic(list_continuations, epic_ref):
        if item_id is not None and getattr(record, "item_id", None) != item_id:
            continue
        if run_id and getattr(record, "continuation_run_id", None) != run_id:
            continue
        if not run_id and getattr(record, "state", None) != "continuing":
            continue
        matches.append(record)
    return matches


def _continuation_record_for_run(
    store: RunStore | None,
    run_id: str | None,
    item_id: str | None,
    epic_ref: str | None = None,
) -> Any | None:
    """Find the durable continuation for a run or claimed item, best-effort.

    Pre-spawn refusal paths can emit ``dispatch_completed`` before a
    ``RunRecord`` exists, leaving ``run_id`` unset. In that case, resolve only
    a unique ``continuing`` record for the durable item (and optional epic); an
    ambiguous match is left unenriched rather than binding evidence to the
    wrong preserved worktree.
    """
    if store is None or (not run_id and not item_id):
        return None
    list_continuations = getattr(store, "list_continuations", None)
    if not callable(list_continuations):
        return None
    matches = [
        record
        for candidate in _continuation_epic_refs(store, epic_ref)
        for record in _matching_continuation_records(
            list_continuations, candidate, run_id=run_id, item_id=item_id
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _continuation_phase(event: str) -> str | None:
    return {
        "dispatch_started": "start",
        "dispatch_completed": "terminal",
        "publication_deferred": "publication",
    }.get(event)


def _continuation_outcome(phase: str, fields: dict[str, Any]) -> str | None:
    """Convert generic event fields to a bounded recovery outcome."""
    if phase == "terminal":
        exit_code = fields.get("exit_code")
        if exit_code == 0:
            return "success"
        if isinstance(exit_code, int):
            return "failure"
        candidate = fields.get("error_category")
    elif phase == "publication":
        candidate = fields.get("category")
    else:
        candidate = None
    return candidate if isinstance(candidate, str) else None


def _continuation_payload_for_event(
    store: RunStore | None,
    event: str,
    run_id: str | None,
    item_id: str | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive lineage for generic dispatch/publication audit events."""
    phase = _continuation_phase(event)
    if phase is None:
        return None
    epic_ref = payload.get("epic_ref") if isinstance(payload, dict) else None
    record = _continuation_record_for_run(store, run_id, item_id, epic_ref)
    if record is None:
        return None
    outcome = _continuation_outcome(phase, payload or {})
    return continuation_evidence(
        phase=phase,
        epic_ref=str(getattr(record, "epic_ref", "")),
        item_id=str(getattr(record, "item_id", item_id or "")),
        original_run_id=getattr(record, "original_run_id", None),
        continuation_run_id=getattr(record, "continuation_run_id", run_id),
        branch=str(getattr(record, "branch", "")),
        worktree_identity=getattr(record, "worktree_identity", None),
        generation=getattr(record, "generation", None),
        state=getattr(record, "state", None),
        outcome=outcome if isinstance(outcome, str) else None,
    )


def _recovery_event_exists(
    store: RunStore, event: str, item_id: str, evidence: dict[str, Any]
) -> bool:
    list_events = getattr(store, "list_audit_events", None)
    if not callable(list_events):
        return False
    try:
        existing = list_events(limit=None, event=event, item_id=item_id)
    except Exception:
        return False
    return any(
        isinstance(row.payload, dict)
        and all(
            row.payload.get(key) == evidence.get(key)
            for key in CONTINUATION_IDENTITY_FIELDS
        )
        for row in existing
    )


def emit_continuation_evidence(
    store: RunStore | None,
    event: str,
    *,
    actor: Actor,
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
    run_id: str | None = None,
    issue_num: int | None = None,
    repo: str | None = None,
    deduplicate: bool = True,
) -> None:
    """Emit one idempotent recovery-lineage event.

    Driver polling can observe the same durable state repeatedly.  A prior
    matching event suppresses a duplicate, while a failed append is retried on
    the next observation because the best-effort audit contract is preserved.
    """
    evidence = continuation_evidence(
        phase=phase,
        epic_ref=epic_ref,
        item_id=item_id,
        original_run_id=original_run_id,
        continuation_run_id=continuation_run_id,
        branch=branch,
        worktree_identity=worktree_identity,
        generation=generation,
        state=state,
        outcome=outcome,
    )
    evidence_identity = continuation_evidence_identity(event, evidence)
    if evidence_identity is not None:
        evidence["evidence_identity"] = evidence_identity
    append_once = getattr(store, "append_audit_event_once", None)
    if (
        deduplicate
        and store is not None
        and not callable(append_once)
        and _recovery_event_exists(store, event, item_id, evidence)
    ):
        return
    emit(
        store,
        event,
        actor=actor,
        run_id=run_id or continuation_run_id or original_run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload=evidence,
        dedup_key=evidence_identity if deduplicate else None,
    )


def resolve_actor() -> Actor:
    """The acting human: KEW_ACTOR env override, else the OS user.

    Policy/agent actors are constructed explicitly by callers
    (e.g. ``Actor("policy", "budget-breach")`` from the future breaker).
    An empty/whitespace KEW_ACTOR is treated as unset.
    """
    override = os.environ.get("KEW_ACTOR", "").strip()
    if override:
        return Actor("human", override)
    try:
        user = getpass.getuser()
    except (OSError, KeyError, ImportError):
        user = "unknown"
    return Actor("human", user)


def set_audit_context(
    *,
    repo: str | None,
    policy_hash: str | None = None,
    snapshot_json: str | None = None,
) -> None:
    """Record this process's default repo + policy binding for emit().

    Unconditionally overwrites all three globals, so a repo-only call (the
    autouse test reset, pure-read commands) clears any prior policy binding.
    """
    global _audit_repo, _audit_policy_hash, _audit_policy_snapshot
    _audit_repo = repo or None
    _audit_policy_hash = policy_hash or None
    _audit_policy_snapshot = snapshot_json or None


def current_policy_hash() -> str | None:
    """The policy hash bound for this process, or None."""
    return _audit_policy_hash


def current_policy_snapshot() -> str | None:
    """The policy snapshot JSON bound for this process, or None."""
    return _audit_policy_snapshot


def bind_policy_context(config: DispatchConfig) -> None:
    """Compute the policy binding from *config* and bind it for this process.

    Best-effort: any compute failure logs and binds the repo only
    (policy_hash=None) — a dispatch or governance command never fails because
    the policy hash could not be computed.
    """
    repo = config.project.repo or None
    try:
        from kew.policy import compute_policy

        policy_hash_hex, snapshot_json = compute_policy(config)
    except Exception as exc:
        _logger.warning("Policy hash unavailable (binding repo only): %s", exc)
        set_audit_context(repo=repo)
        return
    set_audit_context(
        repo=repo, policy_hash=policy_hash_hex, snapshot_json=snapshot_json
    )


def record_current_policy(store: RunStore | None) -> None:
    """Persist the bound policy snapshot to *store* (best-effort, store-once)."""
    if store is None or _audit_policy_hash is None or _audit_policy_snapshot is None:
        return
    record = getattr(store, "record_policy_snapshot", None)
    if record is None:
        return  # fake/legacy store without the policy API — no-op
    try:
        record(
            _audit_policy_hash,
            _audit_policy_snapshot,
            datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        _logger.warning(
            "Failed to retain policy snapshot %s: %s", _audit_policy_hash, exc
        )


def _sanitize_endpoint_url(value: str) -> str:
    """Strip credentials from URL-shaped endpoint values before persisting.

    Userinfo (``user:pass@``), query strings, and fragments may carry
    secrets; the audit trail only needs scheme, host, and path.
    Non-URL values pass through unchanged.
    """
    if "://" not in value:
        return value
    parts = urlsplit(value)
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def configured_endpoint(runner_name: str, model: str) -> str:
    """The *configured* endpoint for a dispatch — honest, not attested.

    Recorded at the pending-save from the process environment (pure
    stdlib); #251's enrichment will attest the endpoint actually used.
    URL values are sanitized (no userinfo/query/fragment) before return.

    - claude-code: ANTHROPIC_BASE_URL when set, else "anthropic:default".
    - opencode: "<provider>:via-opencode" from the model's provider prefix.
    - pi: "<provider>:via-pi" from the model's provider prefix.
    - hermes: "<provider>:via-hermes" from the model's provider prefix.
    - anything else: "" (callers store ``endpoint or None``).
    """
    if runner_name == "claude-code":
        base_url = _sanitize_endpoint_url(
            os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        )
        return base_url or "anthropic:default"
    if runner_name == "codex":
        return "openai:via-codex"
    prefix = model.split("/", 1)[0] if model else "unknown"
    if runner_name == "opencode":
        return f"{prefix}:via-opencode"
    if runner_name == "pi":
        return f"{prefix}:via-pi"
    if runner_name == "hermes":
        return f"{prefix}:via-hermes"
    return ""


def _append_with_optional_dedup(
    store: RunStore,
    append: Any,
    append_kwargs: dict[str, Any],
    dedup_key: str | None,
    *,
    strict: bool = False,
) -> int | None:
    """Use a store's atomic append when an evidence identity is available."""
    append_once = getattr(store, "append_audit_event_once", None)
    if dedup_key is not None and callable(append_once):
        event_id = append_once(dedup_key=dedup_key, **append_kwargs)
    else:
        event_id = append(**append_kwargs)
    if strict:
        _confirm_audit_write(event_id, append_kwargs["event"])
    return event_id if type(event_id) is int else None


def _require_audit_write(store: RunStore | None, *, strict: bool) -> None:
    """Require storage support before a strict audit write."""
    if strict and (
        store is None or not callable(getattr(store, "append_audit_event", None))
    ):
        raise RuntimeError("Required audit storage is unavailable")


def _confirm_audit_write(event_id: Any, event: str) -> None:
    """Reject writes without a confirmed durable row identifier."""
    if type(event_id) is not int or event_id < 1:
        raise RuntimeError(f"Audit event {event} persistence was not confirmed")


def _audit_write_failure(event: str, exc: Exception, *, strict: bool) -> None:
    """Report optional failures and raise required failures with event context."""
    if strict:
        raise RuntimeError(
            f"Failed to write required audit event {event}: {exc}"
        ) from exc
    _logger.warning("Failed to write audit event %s: %s", event, exc)


def event_fields(
    *,
    actor: Actor,
    item_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One audit row's fields besides its event, as :func:`emit` would append
    them, for a store that chains the row inside its own transaction (#2975)."""
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "item_id": item_id,
        "repo": _audit_repo,
        "actor_type": actor.type,
        "actor_id": actor.id,
        "policy_hash": _audit_policy_hash,
        "payload": payload,
    }


def append_credential_issuance(
    store: RunStore,
    run_id: str,
    payload: dict[str, Any],
    *,
    item_id: str | None,
) -> int | None:
    """Append required host issuance without suppressing storage failures."""
    return store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="sandbox_resolved",
        actor_type="policy",
        actor_id="credential-issuer",
        run_id=run_id,
        item_id=item_id,
        repo=_audit_repo,
        policy_hash=_audit_policy_hash,
        payload=payload,
    )


def emit(
    store: RunStore | None,
    event: str,
    *,
    actor: Actor,
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    endpoint: str | None = None,
    repo: str | None = None,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    strict: bool = False,
) -> int | None:
    """Append one audit event. Return its row id when persistence confirms it.

    Strict writes raise when persistence fails.

    *repo* defaults from set_audit_context(); callers with config in hand
    pass it explicitly. *item_id* is the durable work-item key (``SFD-12``);
    callers pass it where they have it (dispatch/driver/merge). It is the
    UNCHAINED additive column — issue_num alone enters the entry_hash preimage.
    Default writes log failures and never raise. Legacy fakes no-op silently.
    Strict writes require storage support and a confirmed row identifier.
    """
    _require_audit_write(store, strict=strict)
    try:
        if store is None:
            _logger.warning("No store available — audit event %s dropped", event)
            return None
        append = getattr(store, "append_audit_event", None)
        if append is None:
            return None  # fake store predating the audit API — no-op
        event_payload = dict(payload) if payload is not None else None
        recovery_payload = _continuation_payload_for_event(
            store, event, run_id, item_id, payload
        )
        audit_run_id = run_id
        if recovery_payload is not None:
            if event_payload is None:
                event_payload = {}
            event_payload.update(recovery_payload)
            if audit_run_id is None:
                candidate_run_id = recovery_payload.get("continuation_run_id")
                if isinstance(candidate_run_id, str):
                    audit_run_id = candidate_run_id
        append_kwargs = dict(
            timestamp=datetime.now(UTC).isoformat(),
            event=event,
            run_id=audit_run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=repo if repo is not None else _audit_repo,
            actor_type=actor.type,
            actor_id=actor.id,
            policy_hash=_audit_policy_hash,
            endpoint=endpoint,
            payload=event_payload,
        )
        return _append_with_optional_dedup(
            store, append, append_kwargs, dedup_key, strict=strict
        )
    except Exception as exc:
        _audit_write_failure(event, exc, strict=strict)
        return None


def audit_fields(
    *,
    actor: Actor,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``append_audit_event`` keywords :func:`emit` would write, but the
    event's name.

    For a store writer that appends its own event inside its own transaction,
    beside the state it attests.
    """
    return dict(
        timestamp=datetime.now(UTC).isoformat(),
        issue_num=issue_num,
        item_id=item_id,
        repo=repo if repo is not None else _audit_repo,
        actor_type=actor.type,
        actor_id=actor.id,
        policy_hash=_audit_policy_hash,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# CLI sub-app: kew audit tail
# ---------------------------------------------------------------------------

audit_app = typer.Typer(help="Inspect the audit trail (audit_events in dispatch.db)")

_FOLLOW_POLL_SECONDS = 1.0
# poll window for client-side id filtering; a burst larger than the
# window triggers an uncapped refetch in _drain_new, so nothing is lost
_FOLLOW_WINDOW = 200


def _open_store() -> SqliteRunStore:
    """Open the dispatch store read-side, exiting cleanly when absent."""
    config = load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        typer.echo(f"No dispatch database found at {db_path}", err=True)
        raise typer.Exit(2)
    return _open_audit_store(db_path)


def _open_audit_store(db_path: Path, *, read_only: bool = False) -> SqliteRunStore:
    """Open an audit store through the canonical ledger boundary."""
    root = canonical_project_root(project_root())
    return open_managed_store(
        project_root=root,
        db_path=db_path.resolve(),
        anchors_path=root / DEFAULT_ANCHORS_PATH,
        read_only=read_only,
        check_same_thread=True,
    )


def _format_event_line(ev: AuditEvent) -> str:
    """One human-readable line per event."""
    actor = f"{ev.actor_type}:{ev.actor_id}" if ev.actor_type else "-"
    # ids.item_ref semantics: bare (SFD-12) for non-GitHub trackers, #N for
    # GitHub numbers. item_id is null on the classic GitHub path, so those rows
    # fall back to issue_num — keeping byte-identical #N output.
    if ev.item_id:
        addr = item_ref(ev.item_id)
    elif ev.issue_num is not None:
        addr = f"#{ev.issue_num}"
    else:
        addr = "-"
    parts = [ev.timestamp, f"{ev.event:<20}", f"{addr:>6}", actor]
    if ev.run_id:
        parts.append(ev.run_id[:8])
    if ev.endpoint:
        parts.append(ev.endpoint)
    if ev.payload:
        parts.append(json.dumps(ev.payload, sort_keys=True))
    return "  ".join(parts)


def _echo_events(events: list[AuditEvent], *, as_json: bool) -> None:
    for ev in events:
        typer.echo(json.dumps(asdict(ev)) if as_json else _format_event_line(ev))


def _drain_new(
    store: SqliteRunStore,
    *,
    last_id: int,
    event: str | None,
    item_id: str | None = None,
) -> tuple[list[AuditEvent], int]:
    """Chronological events with id > last_id, and the new high-water mark."""
    batch = store.list_audit_events(_FOLLOW_WINDOW, event=event, item_id=item_id)
    if len(batch) == _FOLLOW_WINDOW and batch[-1].id > last_id:
        # A full window whose oldest row is still newer than last_id may
        # hide unseen events behind it — refetch without a cap this poll.
        batch = store.list_audit_events(limit=None, event=event, item_id=item_id)
    fresh = [e for e in reversed(batch) if e.id > last_id]
    if fresh:
        last_id = fresh[-1].id
    return fresh, last_id


def _follow(
    store: SqliteRunStore,
    *,
    last_id: int,
    event: str | None,
    item_id: str | None,
    as_json: bool,
) -> None:
    """Poll the table at 1s until interrupted — no push machinery."""
    try:
        while True:
            time.sleep(_FOLLOW_POLL_SECONDS)
            fresh, last_id = _drain_new(
                store, last_id=last_id, event=event, item_id=item_id
            )
            _echo_events(fresh, as_json=as_json)
    except KeyboardInterrupt:
        pass


_AUDIT_EXPORT_FIELDS = [
    "id",
    "timestamp",
    "event",
    "run_id",
    "issue_num",
    "item_id",
    "repo",
    "actor_type",
    "actor_id",
    "endpoint",
    "policy_hash",
    "prev_hash",
    "payload",
    "dedup_key",
]


def export_audit_json(events: list[AuditEvent]) -> str:
    """Export audit events to a JSON string (payload stays structured)."""
    return json.dumps([asdict(e) for e in events], indent=2)


def export_audit_csv(events: list[AuditEvent]) -> str:
    """Export audit events to CSV (payload JSON-encoded into one column)."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_AUDIT_EXPORT_FIELDS)
    writer.writeheader()
    for ev in events:
        row = asdict(ev)
        if row["payload"] is not None:
            row["payload"] = json.dumps(row["payload"])
        writer.writerow(row)
    return output.getvalue()


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        typer.echo(f"Invalid --since date: {since!r} (expected ISO 8601)", err=True)
        raise typer.Exit(2) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@audit_app.command("export")
def export_cmd(
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="Export format: json or csv")
    ] = "json",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file path (default: stdout)"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only events at/after this ISO 8601 date"),
    ] = None,
) -> None:
    """Export the audit trail to JSON or CSV (chronological)."""
    if fmt not in ("json", "csv"):
        typer.echo("--format must be 'json' or 'csv'", err=True)
        raise typer.Exit(2)
    since_dt = _parse_since(since)
    with _open_store() as store:
        events = list(reversed(store.list_audit_events(limit=None, since=since_dt)))
    content = export_audit_json(events) if fmt == "json" else export_audit_csv(events)
    if output is not None:
        output.write_text(content, encoding="utf-8")
        typer.echo(f"Exported {len(events)} event(s) to {output}")
    else:
        typer.echo(content)


def _resolve_db_path() -> Path:
    config = load_config()
    return Path(config.logging.dir).resolve() / "dispatch.db"


def _is_int(value: Any) -> bool:
    """True for a real int (JSON bools parse to Python bool, a subclass of int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def default_anchors_path() -> Path:
    """``audit-anchors.jsonl`` at the project root, not at the cwd (#1156/#1120).

    The anchors file is a repo-root artifact, so the default has to resolve
    against the same root as the DB. Resolving it against the cwd wrote a stray
    copy from every subdirectory and verified against the wrong one. An
    *explicit* ``--anchors`` path is left alone: a relative path the operator
    typed is relative to where they typed it.
    """
    from kew.config import project_root

    return project_root() / DEFAULT_ANCHORS_PATH


def _load_anchors(anchors: Path | None) -> list[Any] | None:
    """Return parsed anchors, or None when the default file is simply absent.

    An explicitly-passed --anchors path that cannot be read/parsed is a
    cannot-run condition (Exit 2); a missing DEFAULT file is a silent skip.
    """
    explicit = anchors is not None
    resolved: Path = anchors if anchors is not None else default_anchors_path()
    if not resolved.exists():
        if explicit:
            typer.echo(f"Anchors file not found: {resolved}", err=True)
            raise typer.Exit(2)
        return None
    try:
        parsed = parse_anchors(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Unreadable anchors file {resolved}: {exc}", err=True)
        raise typer.Exit(2) from None
    # parse_anchors copies fields verbatim; a non-int id/count would otherwise
    # blow up later in classify_anchors' comparisons. Treat as cannot-run.
    if any(not _is_int(a.id) or not _is_int(a.count) for a in parsed):
        typer.echo(
            f"Unreadable anchors file {resolved}: id and count must be integers",
            err=True,
        )
        raise typer.Exit(2)
    return parsed


@dataclass(frozen=True)
class AnchorAge:
    """How long ago the newest anchor was taken — the *witness's* own freshness.

    ``staleness_window`` measures the live chain against the anchors that
    exist; this measures whether anchors are still being taken at all. A dead
    anchor workflow leaves a chain that verifies perfectly and is witnessed by
    nobody (#1408), so the age is reported rather than inferred from silence.
    """

    newest: datetime
    days: float


def _parse_anchor_timestamp(value: Any) -> datetime | None:
    """Anchor stamps are ISO-8601 UTC. Anything else reads as unknown, never
    as fresh: a stamp we cannot parse is not evidence of a recent anchor."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Historical anchors could be written without an offset; UTC is what the
    # anchor CLI has always recorded, so assume it rather than local time.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def newest_anchor_age(
    anchors: Iterable[Anchor], *, now: datetime | None = None
) -> AnchorAge | None:
    """Age of the newest anchor that carries a readable timestamp.

    ``None`` when no anchor does — the honest answer is "unknown", and an
    unreadable stamp must never mask a readable one, so the newest *timestamp*
    wins rather than the last line in the file.
    """
    stamps = [
        stamp
        for stamp in (_parse_anchor_timestamp(a.timestamp) for a in anchors)
        if stamp is not None
    ]
    if not stamps:
        return None
    newest = max(stamps)
    moment = now if now is not None else datetime.now(UTC)
    return AnchorAge(newest=newest, days=(moment - newest).total_seconds() / 86400.0)


def _verify_exit_code(
    result: ChainResult,
    report: AnchorReport | None,
    anchor_chain: AnchorChainResult | None,
) -> int:
    if not result.ok:
        return 1
    if report is not None and not report.ok:
        return 1
    if anchor_chain is not None and not anchor_chain.ok:
        return 1
    return 0


@audit_app.command("verify")
def verify_cmd(
    anchors: Annotated[
        Path | None,
        typer.Option("--anchors", help="Anchors file to verify the head against"),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a JSON verdict")
    ] = False,
) -> None:
    """Verify audit-log chain integrity (exit 0 ok / 1 tamper / 2 cannot-run)."""
    db_path = _project_db_path(canonical_project_root(project_root()))
    if not db_path.exists():
        typer.echo(f"No dispatch database found at {db_path}", err=True)
        raise typer.Exit(2)
    staleness_window = load_config().audit.staleness_window
    anchor_list = _load_anchors(anchors)
    with _open_registered_readonly(db_path) as store:
        rows = store.read_chain_rows()
        ledger_id = store.read_ledger_id()
    result = verify_chain(rows)
    report: AnchorReport | None = None
    anchor_chain: AnchorChainResult | None = None
    if anchor_list is not None:
        report = classify_anchors(
            rows,
            anchor_list,
            staleness_window=staleness_window,
            ledger_id=ledger_id,
        )
        anchor_chain = verify_anchor_chain(anchor_list)
    age = None if anchor_list is None else newest_anchor_age(anchor_list)
    _emit_verify(result, report, anchor_chain, age, as_json=as_json)
    raise typer.Exit(_verify_exit_code(result, report, anchor_chain))


def _emit_verify_json(
    result: ChainResult,
    report: AnchorReport | None,
    anchor_chain: AnchorChainResult | None,
    age: AnchorAge | None,
) -> None:
    """Machine-readable ``kew audit verify`` output. ``ok`` is the conjunction of
    the event chain and any anchor / anchor-chain checks that ran."""
    ok = (
        result.ok
        and (report is None or report.ok)
        and (anchor_chain is None or anchor_chain.ok)
    )
    out: dict[str, Any] = {
        "ok": ok,
        "count": result.count,
        "broken_at_id": result.broken_at_id,
        "head_hash": result.head_hash,
    }
    if report is not None:
        out["anchors"] = {
            "matched": report.matched,
            "mismatched": report.mismatched,
            "absent": report.absent,
            "lagging": report.lagging,
            "count_shrink": report.count_shrink,
            "wrong_ledger": report.wrong_ledger,
            "identity_unanchored": report.identity_unanchored,
            "newest_timestamp": None if age is None else age.newest.isoformat(),
            "age_days": None if age is None else round(age.days, 4),
        }
    if anchor_chain is not None:
        out["anchor_chain"] = {
            "ok": anchor_chain.ok,
            "broken_at_index": anchor_chain.broken_at_index,
            "checked": anchor_chain.checked,
            "total": anchor_chain.total,
        }
    typer.echo(json.dumps(out))


def _emit_verify_human(
    result: ChainResult,
    report: AnchorReport | None,
    anchor_chain: AnchorChainResult | None,
    age: AnchorAge | None,
) -> None:
    """Human-readable ``kew audit verify`` output: chain verdict, anchor
    coverage, anchor age, and anchor-chain continuity."""
    if result.ok:
        typer.echo(f"Chain intact: {result.count} event(s).")
    else:
        typer.echo(f"TAMPER: chain breaks at id {result.broken_at_id}.")
    if report is not None:
        _emit_anchor_age(age)
        if report.wrong_ledger:
            typer.echo("Anchors: WRONG-LEDGER.")
            return
        if report.identity_unanchored:
            typer.echo("Anchors: IDENTITY-UNANCHORED.")
            return
        shrink = " · COUNT-SHRINK" if report.count_shrink else ""
        typer.echo(
            f"Anchors: {report.matched} matched, "
            f"{len(report.mismatched)} mismatched, {len(report.absent)} absent, "
            f"{len(report.lagging)} ahead (not yet local).{shrink}"
        )
    if anchor_chain is not None:
        _emit_anchor_chain_human(anchor_chain)


def _emit_anchor_age(age: AnchorAge | None) -> None:
    """How old the newest anchor is, so an intact chain cannot pass for a
    *witnessed* one (#1408). The alarm lives in ``kew doctor``; this only
    reports, and never changes the exit code."""
    if age is None:
        typer.echo("Newest anchor: unknown (no readable anchor timestamp).")
        return
    if age.days < 0:
        typer.echo(
            f"Newest anchor: {age.newest.isoformat()} "
            "(timestamp is in the future — check clock skew)."
        )
        return
    typer.echo(f"Newest anchor: {age.newest.isoformat()} ({age.days:.1f} day(s) old).")


def _emit_anchor_chain_human(anchor_chain: AnchorChainResult) -> None:
    """Anchor-chain continuity verdict, plus the link count it rests on.

    "intact (0 link(s) verified)" over several anchors means the re-chain
    defence never actually ran (an all-legacy file) — warn rather than let it
    read like a clean bill of health (#1122).
    """
    if not anchor_chain.ok:
        typer.echo(
            "Anchor chain: RE-CHAINED — continuity breaks at anchor index "
            f"{anchor_chain.broken_at_index}."
        )
        return
    typer.echo(f"Anchor chain: intact ({anchor_chain.checked} link(s) verified).")
    if anchor_chain.checked == 0 and anchor_chain.total > 1:
        typer.echo(
            f"WARNING: {anchor_chain.total} anchors carry no prev links — "
            "the re-chain defence verified nothing."
        )


def _emit_verify(
    result: ChainResult,
    report: AnchorReport | None,
    anchor_chain: AnchorChainResult | None,
    age: AnchorAge | None,
    *,
    as_json: bool,
) -> None:
    if as_json:
        _emit_verify_json(result, report, anchor_chain, age)
    else:
        _emit_verify_human(result, report, anchor_chain, age)


def _verified_anchor_head(rows: list[ChainRow]) -> tuple[ChainResult, str]:
    result = verify_chain(rows)
    if not result.ok:
        # Never anchor a chain that's already broken — that would bless tamper.
        typer.echo(
            f"Refusing to anchor: chain breaks at id {result.broken_at_id}.", err=True
        )
        raise typer.Exit(1)
    if result.head_hash is None:  # pragma: no cover — non-empty rows ⇒ head_hash set
        raise RuntimeError("verify_chain returned no head_hash for a non-empty chain")
    return result, result.head_hash


def _project_db_path(root: Path) -> Path:
    """The database the *explicitly selected project* configures.

    ``load_config`` already anchors a relative ``logging.dir`` from kew.toml (or
    the built-in default) to the project root; an env-supplied relative override
    is anchored here too. Anchoring must never resolve a cwd-relative ledger:
    that is how a scheduled run in an ephemeral checkout mints a second chain.
    """
    log_dir = Path(load_config().logging.dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    return (log_dir / "dispatch.db").resolve()


def _open_registered_readonly(
    db_path: Path, *, refusal: str = "Refusing to read the audit ledger"
) -> SqliteRunStore:
    """Open the registered project ledger read-only or fail closed."""
    try:
        return _open_audit_store(db_path, read_only=True)
    except LedgerIdentityError as exc:
        typer.echo(f"{refusal}: {exc}", err=True)
        raise typer.Exit(2) from None


def _read_registered_chain(db_path: Path) -> tuple[list[ChainRow], str | None]:
    """Read the chain head from the project's *registered* ledger, read-only.

    Anchoring is a read. Opening read-only through the managed boundary means an
    anchor run can neither migrate the schema nor create/register a ledger, so
    an unregistered or mismatched database is a cannot-run condition (exit 2)
    rather than an invitation to mint a fresh chain and anchor that instead.
    """
    with _open_registered_readonly(db_path, refusal="Refusing to anchor") as store:
        return store.read_chain_rows(), store.read_ledger_id()


def _require_matching_anchor_ledger(
    existing: list[Anchor], ledger_id: str | None
) -> None:
    anchored_ledger_id = next(
        (anchor.ledger_id for anchor in existing if anchor.version == 2),
        None,
    )
    if anchored_ledger_id is not None and anchored_ledger_id != ledger_id:
        typer.echo("Refusing to anchor: WRONG-LEDGER.", err=True)
        raise typer.Exit(1)


def _append_anchor(
    anchors: Path, existing: list[Anchor], record_anchor: Anchor
) -> None:
    if _already_anchored(existing, record_anchor):
        typer.echo(format_anchor(existing[-1]))
        typer.echo(f"anchors: {anchors.resolve()}", err=True)
        return
    record = format_anchor(record_anchor)
    with anchors.open("a", encoding="utf-8") as fh:
        fh.write(record + "\n")
    typer.echo(record)
    typer.echo(f"anchors: {anchors.resolve()}", err=True)


@audit_app.command("anchor")
def anchor_cmd(
    anchors: Annotated[
        Path | None,
        typer.Option(
            "--anchors",
            help="Anchors file to append the head to "
            "(default: audit-anchors.jsonl at the project root)",
        ),
    ] = None,
) -> None:
    """Append the current chain head to the anchors file (pure-local; no git)."""
    if anchors is None:
        anchors = default_anchors_path()
    db_path = _project_db_path(canonical_project_root(project_root()))
    if not db_path.exists():
        typer.echo(f"No dispatch database found at {db_path}", err=True)
        raise typer.Exit(2)
    rows, ledger_id = _read_registered_chain(db_path)
    if not rows:
        return  # empty chain — nothing to anchor (exit 0)
    result, head_hash = _verified_anchor_head(rows)
    if not anchors.exists():
        typer.echo(
            f"warning: creating a new anchors file at {anchors.resolve()} "
            f"for a chain with {result.count} event(s)",
            err=True,
        )
    existing = _anchor_history_for_append(anchors)
    _require_matching_anchor_ledger(existing, ledger_id)
    record_anchor = Anchor(
        id=rows[-1].id,
        entry_hash=head_hash,
        count=result.count,
        timestamp=datetime.now(UTC).isoformat(),
        prev=GENESIS_PREV if not existing else anchor_hash(existing[-1]),
        version=2 if ledger_id is not None else None,
        ledger_id=ledger_id,
    )
    _append_anchor(anchors, existing, record_anchor)


@audit_app.command("ledger-rebind")
def ledger_rebind_cmd(
    target: Annotated[
        Path, typer.Option("--target", help="Existing ledger database to register")
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Nonempty operator recovery reason")
    ],
    expected_ledger_id: Annotated[
        str | None,
        typer.Option("--expected-ledger-id", help="Canonical full ledger UUID"),
    ] = None,
    expected_anchor_hash: Annotated[
        str | None,
        typer.Option(
            "--expected-anchor-hash", help="Full latest legacy anchor SHA-256"
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm this registry recovery decision")
    ] = False,
) -> None:
    """Explicitly recover or adopt one verified ledger; never merge chains."""
    if not yes:
        typer.echo("Refusing ledger rebind without --yes.", err=True)
        raise typer.Exit(2)
    from kew.config import project_root
    from kew.ledger_identity import LedgerIdentityError, ledger_rebind

    try:
        result = ledger_rebind(
            project_root=project_root(),
            target=target,
            anchors_path=default_anchors_path(),
            expected_ledger_id=expected_ledger_id,
            expected_anchor_hash=expected_anchor_hash,
            reason=reason,
        )
    except LedgerIdentityError as exc:
        typer.echo(f"Refusing ledger rebind: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Ledger {result.ledger_id} registered at {result.db_path}")


def _anchor_history_for_append(anchors: Path) -> list[Anchor]:
    """Read and validate the history before appending an anchor.

    Refuses to extend a re-chained history, so a tampered anchor file cannot be
    silently blessed. The caller derives ``prev`` from the returned final
    record, which makes the first v2 record bind the legacy chain head.
    """
    if not anchors.exists():
        return []
    try:
        existing = parse_anchors(anchors.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        typer.echo(f"Refusing to anchor: unreadable anchors file: {exc}", err=True)
        raise typer.Exit(2) from None
    if not existing:
        return []
    chain = verify_anchor_chain(existing)
    if not chain.ok:
        typer.echo(
            "Refusing to anchor: anchor history continuity breaks at index "
            f"{chain.broken_at_index}.",
            err=True,
        )
        raise typer.Exit(1)
    return existing


def _already_anchored(existing: list[Anchor], candidate: Anchor) -> bool:
    """A v2 retry at the same ledger head is a no-op."""
    if not existing or candidate.version != 2:
        return False
    latest = existing[-1]
    return (
        latest.version == candidate.version
        and latest.ledger_id == candidate.ledger_id
        and latest.id == candidate.id
        and latest.entry_hash == candidate.entry_hash
        and latest.count == candidate.count
    )


def _policy_show_current(*, as_json: bool) -> None:
    from kew.policy import compute_policy

    config = load_config()
    policy_hash_hex, snapshot_json = compute_policy(config)
    snapshot = json.loads(snapshot_json)
    if as_json:
        typer.echo(json.dumps({"policy_hash": policy_hash_hex, "snapshot": snapshot}))
        return
    typer.echo(f"policy_hash: {policy_hash_hex}")
    typer.echo(json.dumps(snapshot, indent=2, sort_keys=True))


def _policy_resolve(policy_hash_arg: str, *, as_json: bool) -> None:
    db_path = _project_db_path(canonical_project_root(project_root()))
    if not db_path.exists():
        typer.echo(f"No dispatch database found at {db_path}", err=True)
        raise typer.Exit(2)
    with _open_registered_readonly(db_path) as store:
        row = store.get_policy_snapshot(policy_hash_arg)
    if row is None:
        typer.echo(f"policy {policy_hash_arg} not retained", err=True)
        raise typer.Exit(1)
    snapshot_json, first_seen = row
    snapshot = json.loads(snapshot_json)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "policy_hash": policy_hash_arg,
                    "first_seen": first_seen,
                    "snapshot": snapshot,
                }
            )
        )
        return
    typer.echo(f"policy_hash: {policy_hash_arg}")
    typer.echo(f"first_seen: {first_seen}")
    typer.echo(json.dumps(snapshot, indent=2, sort_keys=True))


@audit_app.command("policy")
def policy_cmd(
    policy_hash_arg: Annotated[
        str | None,
        typer.Argument(
            metavar="[HASH]",
            help="Resolve a retained policy hash; omit to show the current policy",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show the current effective policy, or resolve a retained policy hash."""
    if policy_hash_arg is None:
        _policy_show_current(as_json=as_json)
        return
    _policy_resolve(policy_hash_arg, as_json=as_json)


@audit_app.command("tail")
def tail_cmd(
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=0, help="How many recent events to show")
    ] = 20,
    event: Annotated[
        str | None, typer.Option("--event", help="Filter by event type")
    ] = None,
    item: Annotated[
        str | None,
        typer.Option("--item", help="Filter by work-item id (e.g. SFD-12 or 42)"),
    ] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Poll for new events (1s)")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="One JSON object per line")
    ] = False,
) -> None:
    """Show recent audit events (oldest first)."""
    with _open_store() as store:
        events = list(
            reversed(store.list_audit_events(limit, event=event, item_id=item))
        )
        _echo_events(events, as_json=as_json)
        if follow:
            last_id = events[-1].id if events else 0
            _follow(store, last_id=last_id, event=event, item_id=item, as_json=as_json)
