"""Publication policy for contained workers with provider credentials.

This policy reads immutable profiles, issuer lifecycle evidence, and the
host's output-scan evidence. A contained run that held a reusable provider
credential (``env:`` auth path), and a runner-native run, publish only a head
the host scanned clean immediately before the push (#1969, #1980).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

from kew.audit import SIDECAR_EVIDENCE_EVENT
from kew.credential_reconciliation import reconcile_pending_key, revocation_state
from kew.publication import PublicationError
from kew.publication_canonical import PublicationHeld
from kew.publication_scan import (
    CODEX_AUTH_SOURCE,
    OutputScan,
    OutputScanError,
    OutputScanTarget,
    output_scan_values,
    record_already_published,
    record_output_scan,
    recorded_scan_verdict,
    scan_output,
)
from kew.readiness import RealizedRouteProfile, SidecarTopologyEvidence
from kew.run_store_outcomes import MARKER_ERROR_CATEGORIES
from kew.switchyard import sanitize_endpoint

if TYPE_CHECKING:
    from kew.run_store import RunStore

CREDENTIAL_PUBLICATION_REFUSAL = "credential_publication_refused"
_MAX_LINEAGE_DEPTH = 32
_MAX_PREDECESSOR_DEPTH = 32
_MAX_BRANCH_RUNS = 32
#: The producer guard reads at most ``_MAX_BRANCH_RUNS`` runs on a branch, a hard
#: cap because it bounds a security check (#3193). From this many on, the
#: verdict comment warns that the branch is nearing it.
BRANCH_RUNS_CAP = _MAX_BRANCH_RUNS
BRANCH_RUNS_WARNING = 24
_MAX_REVIEW_EVIDENCE = 32
_MAX_SIDECAR_EVIDENCE = 64
_MAX_ISSUED_KEY_EVIDENCE = 256
_EMPTY_REVIEW_DIGEST = hashlib.sha256(b"").hexdigest()
_REVIEW_VERDICT_EVENT = "adversarial_review_verdict"
_PUBLICATION_INTENT_EVENT = "publication_intent"
HEAD_ATTESTATION_EVENT = "publication_head_attested"
#: The evidence shapes that may claim a remote head for a run. The host
#: publisher claims what it pushed itself; a named human claims a head an
#: operator published by hand after the owner approved it (#2124). Each entry
#: is ``(event, actor_type, actor_id)``; ``None`` accepts any actor id.
_HEAD_CLAIM_SOURCES: tuple[tuple[str, str, str | None], ...] = (
    (_PUBLICATION_INTENT_EVENT, "policy", "host-publication"),
    (HEAD_ATTESTATION_EVENT, "human", None),
)
_MAX_HEAD_INTENT_EVIDENCE = 32
_ISSUED_AUTH_PATH_RE = re.compile(r"issued:openrouter:([0-9a-f]{64})")
_NON_PUBLICATION_RUN_KINDS = frozenset(
    {"adversarial_review", "planning", "readiness_probe", "triage"}
)

_MESSAGES = {
    "issued_key_evidence_missing": (
        "issued OpenRouter key lacks matching immutable attempt issuance evidence"
    ),
    "issued_key_unrevoked": (
        "issued OpenRouter key awaits recorded successful revocation"
    ),
    "credential_in_output": (
        "published output contains a held credential value or a known key shape; "
        "nothing was pushed and the checkout stays local"
    ),
    "credential_already_published": (
        "a commit the agent already pushed to the remote branch contains a held "
        "credential value or a known key shape; rotate that credential and remove "
        "the commit from the branch (kew does not rewrite remote history)"
    ),
    "credential_scan_missing": (
        "output from a reusable provider credential or a native runner has no "
        "clean host scan of this exact head and is not releasable"
    ),
    "credential_scan_unavailable": (
        "the host could not scan the output for credentials and it is not releasable"
    ),
    "credential_scan_oversize": (
        "the output exceeds the credential scan's size bounds and is not releasable"
    ),
    "credential_scan_timeout": (
        "the credential scan did not finish in time and the output is not releasable"
    ),
    "contained_profile_missing": (
        "contained output lacks complete immutable attempt profiles and is not "
        "releasable"
    ),
    "contained_auth_evidence_missing": (
        "contained output lacks immutable authentication evidence and is not releasable"
    ),
    "contained_profile_stale": (
        "contained output has inconsistent immutable attempt evidence and is not "
        "releasable"
    ),
    "publication_lineage_ambiguous": (
        "publication ancestry is ambiguous and protected output is not releasable"
    ),
    "publication_lineage_cycle": (
        "publication ancestry contains a cycle and protected output is not releasable"
    ),
    "publication_lineage_too_deep": (
        "publication ancestry exceeds the bounded policy walk and is not releasable"
    ),
    "publication_evidence_unavailable": (
        "publication evidence is unavailable and protected output is not releasable"
    ),
    "publication_ancestor_evidence_missing": (
        "publication ancestor evidence is missing and protected output is not "
        "releasable"
    ),
    "sidecar_admission_evidence_missing": (
        "sidecar admission evidence is missing and protected output is not releasable"
    ),
    "sidecar_terminal_evidence_missing": (
        "sidecar terminal evidence is missing and protected output is not releasable"
    ),
    "sidecar_identity_stale": (
        "sidecar isolation evidence does not match its attempt and is not releasable"
    ),
    "sidecar_packet_evidence_missing": (
        "sidecar packet evidence is incomplete and protected output is not releasable"
    ),
    "sidecar_filter_evidence_missing": (
        "sidecar filter evidence is incomplete and protected output is not releasable"
    ),
    "sidecar_cleanup_evidence_missing": (
        "sidecar cleanup evidence is incomplete and protected output is not releasable"
    ),
}


class CredentialPublicationRefused(PublicationError):
    """A protected publication lineage is not releasable."""

    category = CREDENTIAL_PUBLICATION_REFUSAL

    def __init__(self, reason: str, *, diagnosis: str = "") -> None:
        self.reason = reason
        self.diagnosis = diagnosis
        message = _MESSAGES.get(reason, "protected output is not releasable")
        super().__init__("commit", f"{message}: {diagnosis}" if diagnosis else message)


#: The diagnosis that sets the branch-run cap apart from every other bounded
#: walk refused as ``publication_lineage_too_deep`` (#3193): only this one is
#: the branch's own run count, which no later round on the branch clears.
_BRANCH_RUN_CAP_DIAGNOSIS = (
    f"the branch reached the {BRANCH_RUNS_CAP} runs the producer guard reads"
)


def branch_run_cap_refusal() -> CredentialPublicationRefused:
    """The refusal of a branch at the producer guard's run cap (#3193)."""
    return CredentialPublicationRefused(
        "publication_lineage_too_deep", diagnosis=_BRANCH_RUN_CAP_DIAGNOSIS
    )


def is_branch_run_cap(detail: str | None) -> bool:
    """Whether a refusal's text is the branch-run cap, which never clears itself.

    Other bounded walks (evidence, lineage, predecessor depth) share the reason
    and its message but not this diagnosis, so they are not mistaken for it.
    """
    return bool(detail) and str(branch_run_cap_refusal()) in str(detail)


def _refuse(reason: str, *, diagnosis: str = "") -> NoReturn:
    raise CredentialPublicationRefused(reason, diagnosis=diagnosis)


class IssuedKeyPublicationHeld(PublicationHeld):
    """Retry publication after the issuer records successful revocation."""

    reason = "issued_key_unrevoked"

    def __init__(
        self, issuer_reason: str, *, run_id: str = "", key_hash: str = ""
    ) -> None:
        self.run_id = run_id
        self.key_hash = key_hash
        self.issuer_reason = issuer_reason
        self.flags = (self.reason, issuer_reason)
        Exception.__init__(self, f"{_MESSAGES[self.reason]}: {issuer_reason}")


def _issued_key_events(store: RunStore, run_id: str) -> list[Any]:
    try:
        events = store.list_audit_events(
            event="sandbox_resolved",
            run_id=run_id,
            limit=_MAX_ISSUED_KEY_EVIDENCE + 1,
        )
    except Exception:
        _refuse("publication_evidence_unavailable")
    if len(events) > _MAX_ISSUED_KEY_EVIDENCE:
        _refuse("publication_lineage_too_deep")
    return events


def _issued_key_payload(event: Any, run_id: str, key_hash: str) -> Mapping[str, Any]:
    if (event.event, event.run_id, event.actor_type, event.actor_id) != (
        "sandbox_resolved",
        run_id,
        "policy",
        "credential-issuer",
    ):
        return {}
    payload = event.payload
    if not isinstance(payload, Mapping):
        return {}
    identity = payload.get("credential_issuer")
    if not isinstance(identity, Mapping):
        return {}
    if (identity.get("issuer"), identity.get("key_hash")) != ("openrouter", key_hash):
        return {}
    return payload


def _require_issued_key_revocation(
    store: RunStore,
    payloads: list[Mapping[str, Any]],
    *,
    run_id: str,
    key_hash: str,
) -> None:
    revocations = [
        p["credential_issuer"] for p in payloads if p.get("phase") == "revocation"
    ]
    if any(identity.get("revoked") is True for identity in revocations):
        return
    reason = revocations[0].get("reason") if revocations else None
    pending = revocation_state(store, run_id, key_hash)
    if pending.get("pending"):
        reason = pending.get("reason")
    raise IssuedKeyPublicationHeld(
        reason if isinstance(reason, str) and reason else "revocation evidence missing",
        run_id=run_id,
        key_hash=key_hash,
    )


def _require_issued_key_evidence(
    store: RunStore, profile: Any, *, run_id: str, attempt: int, admission: bool
) -> None:
    match = _ISSUED_AUTH_PATH_RE.fullmatch(profile.auth_path)
    if match is None:
        _refuse("issued_key_evidence_missing")
    payloads = [
        _issued_key_payload(event, run_id, match[1])
        for event in _issued_key_events(store, run_id)
    ]
    if not any(
        payload.get("phase") == "issuance"
        and type(payload.get("attempt_number")) is int
        and payload["attempt_number"] == attempt
        for payload in payloads
    ):
        _refuse("issued_key_evidence_missing")
    if not admission:
        _require_issued_key_revocation(
            store, payloads, run_id=run_id, key_hash=match[1]
        )


@dataclass(frozen=True)
class _PublishedHead:
    """The exact head a push is about to publish, and the run publishing it."""

    run_id: str
    head_sha: str


def _require_output_scan(store: RunStore, head: _PublishedHead | None) -> None:
    """Admit only a head the host scanned clean for this publishing run.

    Without a head there is no output to release yet: a launch admission or a
    pre-push check. Every push primitive calls the gate again with its head
    (:func:`publication_output_guard`), and that call decides.
    """
    if head is None:
        return
    try:
        verdict = recorded_scan_verdict(
            store, run_id=head.run_id, head_sha=head.head_sha
        )
    except Exception:
        _refuse("publication_evidence_unavailable")
    if verdict == "found":
        _refuse("credential_in_output")
    if verdict != "clean":
        _refuse("credential_scan_missing")


def _refuse_found_head(store: RunStore, head: _PublishedHead | None) -> None:
    """A recorded finding for the head refuses, whatever the run's posture.

    Runs before any posture is read: a host, uncontained, ``env:`` or native
    run alike never publishes a head the host scan found a credential in.
    """
    if head is None:
        return
    try:
        verdict = recorded_scan_verdict(
            store, run_id=head.run_id, head_sha=head.head_sha
        )
    except Exception:
        _refuse("publication_evidence_unavailable")
    if verdict == "found":
        _refuse("credential_in_output")


def _require_contained_auth_evidence(
    store: RunStore,
    profile: Any,
    *,
    run_id: str,
    attempt: int,
    admission: bool,
    head: _PublishedHead | None,
) -> None:
    if not profile.auth_path:
        _refuse("contained_auth_evidence_missing")
    if profile.auth_path.startswith("env:"):
        _require_output_scan(store, head)
    if profile.auth_path.startswith("issued:"):
        _require_issued_key_evidence(
            store, profile, run_id=run_id, attempt=attempt, admission=admission
        )


def _profile_is_consistent(profile: Any, run_id: str, attempt: int) -> bool:
    restored = RealizedRouteProfile.restore(profile.profile_json, run_id, attempt)
    return (
        restored.schema_version == 1
        and restored.run_id == run_id
        and restored.attempt_number == attempt
        and restored.fingerprint == profile.fingerprint
        and restored.containment.value == profile.containment
        and restored.auth_path.value == profile.auth_path
    )


def _canonical_profile_is_contained(profile: Any, run_id: str) -> bool:
    restored = RealizedRouteProfile.restore(
        profile.profile_json, run_id, profile.attempt_number
    )
    return restored.containment.value == "contained"


def _read_run_evidence(store: RunStore, run_id: str) -> tuple[Any, list[Any]]:
    try:
        return store.get(run_id), list(store.list_route_profiles(run_id))
    except Exception:
        _refuse("publication_evidence_unavailable")


def _known_contained(record: Any, profiles: list[Any], run_id: str) -> bool:
    container_name = getattr(record, "container_name", None)
    return bool(
        (isinstance(container_name, str) and container_name)
        or any(profile.containment == "contained" for profile in profiles)
        or any(_canonical_profile_is_contained(profile, run_id) for profile in profiles)
    )


def _highest_attempt(
    record: Any, profiles: list[Any], required_attempt: int | None
) -> int:
    return max(
        max(int(getattr(record, "attempt_number", 1) or 1), 1),
        max(required_attempt or 1, 1),
        *(int(profile.attempt_number) for profile in profiles),
    )


def _require_attempt_evidence(
    store: RunStore,
    record: Any,
    profile: Any,
    *,
    run_id: str,
    attempt: int,
    legacy_container_name: str | None,
    referenced: bool,
    admission: bool,
    issued_admission: bool,
    head: _PublishedHead | None,
) -> None:
    restored = RealizedRouteProfile.restore(profile.profile_json, run_id, attempt)
    if referenced and (
        restored.schema_version != 1
        or restored.containment.value
        not in {"host", "contained", "native", "uncontained"}
    ):
        _refuse("publication_ancestor_evidence_missing")
    if not _profile_is_consistent(profile, run_id, attempt):
        _refuse("contained_profile_stale")
    if profile.containment != "contained":
        if legacy_container_name:
            _refuse("contained_profile_stale")
        if profile.containment == "native":
            _require_output_scan(store, head)
        return
    _require_contained_auth_evidence(
        store,
        profile,
        run_id=run_id,
        attempt=attempt,
        admission=issued_admission,
        head=head,
    )
    if _profile_uses_sidecar(restored):
        _require_sidecar_evidence(
            store,
            record,
            restored,
            profile_fingerprint=profile.fingerprint,
            admission=admission,
        )


def _profile_uses_sidecar(profile: RealizedRouteProfile) -> bool:
    transport = profile.route.transport.value
    return isinstance(transport, Mapping) and transport.get("sidecar") is True


def _sidecar_events(store: RunStore, record: Any, run_id: str) -> list[Any]:
    item_id = getattr(record, "item_id", None)
    try:
        events = list(
            store.list_audit_events(
                limit=_MAX_SIDECAR_EVIDENCE + 1,
                event=SIDECAR_EVIDENCE_EVENT,
                item_id=item_id if isinstance(item_id, str) and item_id else None,
            )
        )
    except Exception:
        _refuse("publication_evidence_unavailable")
    if len(events) > _MAX_SIDECAR_EVIDENCE:
        _refuse("publication_lineage_too_deep")
    return [event for event in events if getattr(event, "run_id", None) == run_id]


def _parse_sidecar_event(event: Any) -> SidecarTopologyEvidence:
    if (
        getattr(event, "event", None) != SIDECAR_EVIDENCE_EVENT
        or getattr(event, "actor_type", None) != "policy"
        or getattr(event, "actor_id", None) != "sidecar-supervisor"
        or not isinstance(getattr(event, "payload", None), Mapping)
    ):
        _refuse("sidecar_identity_stale")
    try:
        return SidecarTopologyEvidence.from_dict(event.payload)
    except (TypeError, ValueError):
        _refuse("sidecar_identity_stale")


def _sidecar_matches_profile(
    evidence: SidecarTopologyEvidence,
    profile: RealizedRouteProfile,
    profile_fingerprint: str,
) -> bool:
    transport = profile.route.transport.value
    if not isinstance(transport, Mapping):
        return False
    endpoint = profile.endpoint.value
    expected_endpoint = (
        sanitize_endpoint(endpoint) if isinstance(endpoint, str) else None
    )
    return (
        evidence.run_id == profile.run_id
        and evidence.attempt_number == profile.attempt_number
        and evidence.profile_fingerprint == profile_fingerprint
        and evidence.route == transport.get("route")
        and evidence.config_sha256 == transport.get("config_sha256")
        and evidence.worker_image_id == profile.image_id.value
        and evidence.endpoint == expected_endpoint
    )


def _unique_sidecar_stage(
    evidence: list[SidecarTopologyEvidence], *, stage: str
) -> SidecarTopologyEvidence:
    if not evidence:
        _refuse(f"sidecar_{stage}_evidence_missing")
    if len(evidence) > 1:
        _refuse("sidecar_identity_stale")
    return evidence[0]


def _sidecar_stage_evidence(
    store: RunStore, record: Any, profile: RealizedRouteProfile
) -> dict[str, list[SidecarTopologyEvidence]]:
    relevant = [
        _parse_sidecar_event(event)
        for event in _sidecar_events(store, record, profile.run_id)
    ]
    relevant = [
        evidence
        for evidence in relevant
        if evidence.attempt_number == profile.attempt_number
    ]
    return {
        stage: [evidence for evidence in relevant if evidence.stage == stage]
        for stage in ("admission", "terminal")
    }


def _require_sidecar_evidence(
    store: RunStore,
    record: Any,
    profile: RealizedRouteProfile,
    *,
    profile_fingerprint: str,
    admission: bool,
) -> None:
    by_stage = _sidecar_stage_evidence(store, record, profile)
    admission_evidence = _unique_sidecar_stage(by_stage["admission"], stage="admission")
    if not _sidecar_matches_profile(admission_evidence, profile, profile_fingerprint):
        _refuse("sidecar_identity_stale")
    _require_sidecar_admission_verdicts(admission_evidence)
    if admission:
        return
    terminal_evidence = _unique_sidecar_stage(by_stage["terminal"], stage="terminal")
    if (
        not _sidecar_matches_profile(terminal_evidence, profile, profile_fingerprint)
        or terminal_evidence.binding() != admission_evidence.binding()
    ):
        _refuse("sidecar_identity_stale")
    _require_sidecar_admission_verdicts(terminal_evidence)
    if terminal_evidence.cleanup_verdict != "passed":
        _refuse("sidecar_cleanup_evidence_missing")


def _require_sidecar_admission_verdicts(
    evidence: SidecarTopologyEvidence,
) -> None:
    if evidence.packet.verdict != "passed":
        _refuse("sidecar_packet_evidence_missing")
    if evidence.filter_verdict != "passed":
        _refuse("sidecar_filter_evidence_missing")


def _require_run_evidence(
    store: RunStore,
    run_id: str,
    *,
    required_attempt: int | None = None,
    referenced: bool = False,
    admission_attempt: int | None = None,
    head: _PublishedHead | None = None,
) -> None:
    """Refuse unsafe or incomplete evidence for one run."""
    record, profiles = _read_run_evidence(store, run_id)
    if record is None:
        _require_missing_run_evidence(profiles, referenced=referenced)
        return
    container_name = getattr(record, "container_name", None)
    legacy_container_name = container_name if isinstance(container_name, str) else None
    if referenced and not profiles:
        _refuse("publication_ancestor_evidence_missing")
    if not referenced and not _known_contained(record, profiles, run_id):
        if any(profile.containment == "native" for profile in profiles):
            _require_output_scan(store, head)
        return
    _require_complete_attempt_profiles(
        store,
        record,
        profiles,
        run_id=run_id,
        required_attempt=required_attempt,
        legacy_container_name=legacy_container_name,
        referenced=referenced,
        admission_attempt=admission_attempt,
        head=head,
    )


def _require_issued_keys_revoked(
    store: RunStore, run_id: str, *, admission: bool, publishing: bool
) -> None:
    """Gate on issuance evidence, not containment (re-audit F8, 2026-09-22).

    Direct OpenRouter routes mint bounded keys on host and native launches
    too, so every key a run's profiles or issuer events name needs recorded
    successful revocation before its output is published. A launch admission
    precedes revocation of the run's own shared key. Runs only after the
    lineage walk, so a structural refusal is never masked by a retryable hold.

    A driver-dispatched child publishes while the driver still owns its key:
    the driver records issuance under the child's run id and revokes only
    after the child exits. The publishing run's own driver-issued keys keep
    the containment-scoped treatment they had before this gate (governed
    review of PR #1984, round 1); keys the run minted itself, and every key
    of an ancestor, still need recorded revocation.
    """
    if admission:
        return
    _, profiles = _read_run_evidence(store, run_id)
    events = _issued_key_events(store, run_id)
    hashes = _issued_key_hashes(store, profiles, events, run_id, publishing=publishing)
    for key_hash in sorted(hashes):
        payloads = [_issued_key_payload(event, run_id, key_hash) for event in events]
        _require_issued_key_revocation(
            store, payloads, run_id=run_id, key_hash=key_hash
        )


def _issued_key_hashes(
    store: RunStore,
    profiles: list[Any],
    events: list[Any],
    run_id: str,
    *,
    publishing: bool,
) -> set[str]:
    """Keys named by issued profiles or issuance events, less live driver keys."""
    hashes = {
        match[1]
        for profile in profiles
        if isinstance(getattr(profile, "auth_path", None), str)
        and (match := _ISSUED_AUTH_PATH_RE.fullmatch(profile.auth_path)) is not None
    }
    hashes.update(
        key_hash
        for event in events
        if (key_hash := _issuance_key_hash(event, run_id)) is not None
    )
    if publishing:
        hashes -= {
            key_hash
            for event in events
            if (key_hash := _issuance_key_hash(event, run_id, driver=True)) is not None
            and _driver_key_still_owned(store, events, run_id, key_hash)
        }
    return hashes


def _driver_key_still_owned(
    store: RunStore, events: list[Any], run_id: str, key_hash: str
) -> bool:
    """Whether the driver may still revoke this key after its child exits.

    Only an unexpired key with no revocation outcome yet. Any revocation
    record (refused, pending, or failed) or a passed expiry ends the
    exemption, so the key is gated like a self-minted one (PR #1984 review
    round 2). Expiry is the bound: the driver sizes it to the child's whole
    schedule plus grace, and reconciliation records the outcome after it.
    """
    payloads = [_issued_key_payload(event, run_id, key_hash) for event in events]
    if any(p.get("phase") in {"revocation", "revocation_pending"} for p in payloads):
        return False
    if revocation_state(store, run_id, key_hash):
        return False
    expiries = {
        p["credential_issuer"].get("expires_at")
        for p in payloads
        if p.get("phase") == "issuance"
    }
    return len(expiries) == 1 and _unexpired(expiries.pop())


def _unexpired(expires_at: object) -> bool:
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return expiry.tzinfo is not None and expiry > datetime.now(UTC)
    except ValueError:
        # Fail closed: an unreadable expiry never extends the driver exemption.
        logging.getLogger(__name__).warning(
            "driver key expiry %r is unparseable; publication holds on that key",
            expires_at,
        )
        return False


def _issuance_key_hash(event: Any, run_id: str, *, driver: bool = False) -> str | None:
    """The key hash of one issuer-recorded issuance event for this run.

    With ``driver``, only an issuance the epic driver recorded for its child.
    """
    payload = getattr(event, "payload", None)
    identity = (
        payload.get("credential_issuer") if isinstance(payload, Mapping) else None
    )
    key_hash = identity.get("key_hash") if isinstance(identity, Mapping) else None
    if not isinstance(key_hash, str):
        return None
    issued = _issued_key_payload(event, run_id, key_hash)
    if issued.get("phase") != "issuance":
        return None
    if driver and issued.get("issued_by") != "driver":
        return None
    return key_hash


def _require_missing_run_evidence(profiles: list[Any], *, referenced: bool) -> None:
    if referenced and not profiles:
        _refuse("publication_ancestor_evidence_missing")
    if profiles:
        _refuse("contained_profile_stale")


def _require_complete_attempt_profiles(
    store: RunStore,
    record: Any,
    profiles: list[Any],
    *,
    run_id: str,
    required_attempt: int | None,
    legacy_container_name: str | None,
    referenced: bool = False,
    admission_attempt: int | None = None,
    head: _PublishedHead | None = None,
) -> None:
    highest_attempt = _highest_attempt(record, profiles, required_attempt)
    by_attempt = {profile.attempt_number: profile for profile in profiles}
    if any(attempt not in by_attempt for attempt in range(1, highest_attempt + 1)):
        _refuse("contained_profile_missing")
    # Retries share a live issued key until the run context unwinds.
    # Earlier sidecar attempts still require their own terminal evidence.
    for attempt in range(1, highest_attempt + 1):
        _require_attempt_evidence(
            store,
            record,
            by_attempt[attempt],
            run_id=run_id,
            attempt=attempt,
            legacy_container_name=legacy_container_name,
            referenced=referenced,
            admission=attempt == admission_attempt,
            issued_admission=admission_attempt is not None,
            head=head,
        )


def _continuation_origin(store: RunStore, run_id: str) -> str | None:
    try:
        origins = store.continuation_origins(run_id, limit=2)
    except OverflowError:
        _refuse("publication_lineage_too_deep")
    except ValueError:
        _refuse("publication_lineage_ambiguous")
    except Exception:
        _refuse("publication_evidence_unavailable")
    if len(origins) > 1:
        _refuse("publication_lineage_ambiguous")
    if not origins:
        return None
    origin = origins[0]
    if not isinstance(origin, str) or not origin:
        _refuse("publication_evidence_unavailable")
    return origin


def _walk_lineage(
    store: RunStore,
    *,
    seed: str,
    run_id: str,
    attempt_number: int | None,
    evaluated: set[str],
    referenced: bool = False,
    admission_attempt: int | None = None,
    head: _PublishedHead | None = None,
) -> None:
    current = seed
    depth = 0
    path: set[str] = set()
    while current not in evaluated:
        if current in path:
            _refuse("publication_lineage_cycle")
        path.add(current)
        _require_run_evidence(
            store,
            current,
            required_attempt=attempt_number if current == run_id else None,
            referenced=referenced,
            admission_attempt=(
                admission_attempt if current == run_id and not referenced else None
            ),
            head=head,
        )
        origin = _continuation_origin(store, current)
        if origin is None:
            evaluated.update(path)
            return
        if depth >= _MAX_LINEAGE_DEPTH:
            _refuse("publication_lineage_too_deep")
        current = origin
        referenced = True
        depth += 1
    if current in path:
        _refuse("publication_lineage_cycle")
    evaluated.update(path)


def ensure_publication_credentials(
    store: RunStore,
    *,
    run_id: str,
    producer_run_id: str | None = None,
    attempt_number: int | None = None,
    policy: Callable[..., None] | None = None,
    published_head: str | None = None,
) -> None:
    """Recover credential revocations and require confirmed publication evidence.

    This admission check never reconciles dormant publication jobs. A caller
    may supply its policy seam; every retry uses that same policy.
    ``published_head`` is the exact commit a push is about to publish; push
    primitives supply it after the scan of that commit is recorded
    (:func:`publication_output_guard`, the publication worker's push).
    """
    policy = policy or require_publication_credentials
    seen: set[tuple[str, str]] = set()
    while True:
        try:
            policy(
                store,
                run_id=run_id,
                producer_run_id=producer_run_id,
                attempt_number=attempt_number,
                published_head=published_head,
            )
            return
        except IssuedKeyPublicationHeld as exc:
            target = (exc.run_id, exc.key_hash)
            if target in seen or len(seen) >= _MAX_ISSUED_KEY_EVIDENCE:
                raise
            seen.add(target)
            reconcile_pending_key(store, *target)


# Compatibility for callers of the original credential recovery helper.


def require_publication_credentials(
    store: RunStore,
    *,
    run_id: str,
    producer_run_id: str | None = None,
    attempt_number: int | None = None,
    published_head: str | None = None,
) -> None:
    """Require safe evidence for a run and its bounded continuation ancestry.

    ``producer_run_id`` binds remediation of an existing branch to its earlier
    producer. Continuation parents come from durable recovery rows. Missing
    run rows outside a continuation remain legacy host behavior. With
    ``published_head``, a recorded finding for that head refuses first,
    whatever the posture, and every ``env:`` or native attempt in the walk
    needs a clean host scan of that head recorded for ``run_id``.
    """
    _require_publication_evidence(
        store,
        run_id=run_id,
        producer_run_id=producer_run_id,
        attempt_number=attempt_number,
        admission_attempt=None,
        head=(
            _PublishedHead(run_id=run_id, head_sha=published_head)
            if published_head
            else None
        ),
    )


def _require_publication_evidence(
    store: RunStore,
    *,
    run_id: str,
    producer_run_id: str | None,
    attempt_number: int | None,
    admission_attempt: int | None,
    head: _PublishedHead | None = None,
) -> None:
    _refuse_found_head(store, head)
    seeds = tuple(
        seed
        for seed in dict.fromkeys((run_id, producer_run_id))
        if isinstance(seed, str) and seed
    )
    evaluated: set[str] = set()
    for seed in seeds:
        _walk_lineage(
            store,
            seed=seed,
            run_id=run_id,
            attempt_number=attempt_number,
            evaluated=evaluated,
            referenced=bool(producer_run_id and seed == producer_run_id),
            admission_attempt=admission_attempt,
            head=head,
        )
    for candidate in sorted(evaluated):
        _require_issued_keys_revoked(
            store,
            candidate,
            admission=admission_attempt is not None and candidate == run_id,
            publishing=candidate == run_id and producer_run_id != run_id,
        )


def require_publication_launch_credentials(
    store: RunStore, *, run_id: str, run_kind: str, attempt_number: int
) -> None:
    """Refuse a paid producer launch whose output could later be published."""
    if run_kind in _NON_PUBLICATION_RUN_KINDS:
        return
    _require_publication_evidence(
        store,
        run_id=run_id,
        producer_run_id=None,
        attempt_number=attempt_number,
        admission_attempt=attempt_number,
    )


def publication_output_guard(
    store: RunStore | None,
    *,
    run_id: str | None,
    producer_run_id: str | None = None,
) -> Callable[[OutputScanTarget], None]:
    """The check every push primitive runs on its exact commit before pushing.

    Scans the objects the push sends, records the verdict bound to the run,
    attempt and head, refuses a finding, then runs the credential gate with
    that head so an ``env:`` or native run needs the clean record just made.
    Without a store there is no gate to run, but a finding still refuses.

    A target marked ``already_published`` is a head the agent itself pushed
    before the host looked (the republish of an agent-pushed review head): a
    finding there is not "nothing was pushed" but a credential already on the
    remote branch, recorded as ``credential_already_published``. The mark is
    per target, so the host-built commit the same republish scans next, which
    the remote does not hold, refuses as ``credential_in_output``.
    """
    publishing = run_id or producer_run_id

    def guard(target: OutputScanTarget) -> None:
        found_reason = (
            "credential_already_published"
            if target.already_published
            else "credential_in_output"
        )
        try:
            result = scan_output(
                target, _admitted_scan_values(store, publishing, producer_run_id)
            )
        except OutputScanError as exc:
            _refuse(exc.reason)
        recorded = (
            _record_scan(
                store,
                publishing,
                target,
                result,
                already_published=target.already_published,
            )
            if store is not None and publishing
            else None
        )
        if result.verdict != "clean":
            logging.getLogger(__name__).warning(
                "publication refused (%s): output scan found %s",
                found_reason,
                result.describe(),
            )
            _refuse(found_reason, diagnosis=result.describe())
        if recorded is False:
            _refuse("credential_scan_missing")
        if store is not None and publishing:
            ensure_publication_credentials(
                store,
                run_id=publishing,
                producer_run_id=producer_run_id,
                published_head=target.head_sha,
            )

    return guard


def _admitted_scan_values(
    store: RunStore | None, publishing: str | None, producer_run_id: str | None
) -> tuple[tuple[str, str], ...]:
    """The scan's needles, holding every credential the lineage was admitted with.

    An ``env:<NAME>`` profile the gate relies on (contained or native) needs
    NAME's value in the publishing process, and a native profile needs the
    host Codex login's secret fields: without them a clean verdict would not
    prove the credential absent, so the scan is unavailable and records
    nothing (#1969 round 12).
    """
    if store is None or not publishing:
        return output_scan_values()
    seeds = tuple(dict.fromkeys(s for s in (publishing, producer_run_id) if s))
    names, native = _scan_requirements(store, seeds)
    values = output_scan_values(admitted_names=names, codex_login_required=native)
    held = {name for name, _ in values}
    missing = sorted(names - held)
    if native and not any(name.startswith(f"{CODEX_AUTH_SOURCE}:") for name in held):
        missing.append("the host Codex login")
    if missing:
        _refuse(
            "credential_scan_unavailable",
            diagnosis=(
                "the publishing process does not hold "
                f"{', '.join(missing)}, which the run was admitted with"
            ),
        )
    return values


def _scan_requirements(
    store: RunStore, seeds: tuple[str, ...]
) -> tuple[frozenset[str], bool]:
    """``env:`` names and native posture across the seeds' continuation lineage.

    Only profiles whose output the gate admits on the scan count: contained
    and native attempts. The gate itself validates the evidence afterwards.
    """
    names: set[str] = set()
    native = False
    for seed in seeds:
        for profile in _lineage_profiles(store, seed):
            if profile.containment not in {"contained", "native"}:
                continue
            native = native or profile.containment == "native"
            auth_path = profile.auth_path or ""
            if auth_path.startswith("env:"):
                names.add(auth_path.removeprefix("env:"))
    return frozenset(names), native


def _lineage_profiles(store: RunStore, seed: str) -> list[Any]:
    profiles: list[Any] = []
    seen: set[str] = set()
    current: str | None = seed
    while current is not None and current not in seen:
        if len(seen) > _MAX_LINEAGE_DEPTH:
            _refuse("publication_lineage_too_deep")
        seen.add(current)
        profiles.extend(_read_run_evidence(store, current)[1])
        current = _continuation_origin(store, current)
    return profiles


def _record_scan(
    store: RunStore,
    run_id: str,
    target: OutputScanTarget,
    result: OutputScan,
    *,
    already_published: bool,
) -> bool:
    """Record the scan strictly; ``False`` when it did not persist.

    A finding on commits the agent already pushed also records the distinct
    ``credential_already_published`` fact, so the ledger shows a credential
    that reached the remote before the host scan.
    """
    try:
        record = store.get(run_id)
        attempt = getattr(record, "attempt_number", None)
        attempt_number = attempt if type(attempt) is int else 1
        item_id = publication_run_item(record) if record is not None else None
        record_output_scan(
            store,
            run_id=run_id,
            attempt_number=attempt_number,
            item_id=item_id,
            target=target,
            result=result,
            already_published=already_published,
        )
        if already_published and result.verdict != "clean":
            record_already_published(
                store,
                run_id=run_id,
                attempt_number=attempt_number,
                item_id=item_id,
                target=target,
                result=result,
            )
    except Exception:
        logging.getLogger(__name__).warning(
            "publication output scan for run %s was not recorded", run_id
        )
        return False
    return True


def require_normalized_job_credentials(
    store: RunStore,
    job: Any,
    *,
    referenced: bool = False,
    published_head: str | None = None,
) -> None:
    """Check one normalized job and every persisted predecessor producer."""
    ensure_publication_credentials(
        store,
        run_id=job.run_id,
        producer_run_id=job.run_id if referenced else None,
        attempt_number=job.attempt_number,
        published_head=published_head,
    )
    current = job
    seen = {job.job_id}
    for _depth in range(_MAX_PREDECESSOR_DEPTH):
        predecessor = _publication_predecessor(store, job, current, seen)
        if predecessor is None:
            return
        ensure_publication_credentials(
            store,
            run_id=predecessor.run_id,
            producer_run_id=predecessor.run_id,
            attempt_number=predecessor.attempt_number,
        )
        seen.add(predecessor.job_id)
        current = predecessor
    if current.predecessor_job_id is not None:
        _refuse("publication_lineage_too_deep")


def _publication_predecessor(
    store: RunStore, root: Any, current: Any, seen: set[str]
) -> Any | None:
    predecessor_id = current.predecessor_job_id
    if predecessor_id is None:
        return None
    if predecessor_id in seen:
        _refuse("publication_lineage_cycle")
    predecessor = store.get_normalized_job(predecessor_id)
    if predecessor is None:
        _refuse("publication_evidence_unavailable")
    identity = (
        predecessor.lineage_key,
        predecessor.source_kind,
        predecessor.source_id,
        predecessor.branch,
        predecessor.superseded_by,
    )
    expected = (
        root.lineage_key,
        root.source_kind,
        root.source_id,
        root.branch,
        current.job_id,
    )
    if identity != expected:
        _refuse("publication_lineage_ambiguous")
    return predecessor


def publication_producer_for_head(
    store: RunStore, *, branch: str, head_sha: str
) -> str | None:
    """Bind an effectful remediation to one retained remote-head producer.

    Precedence: a normalized publication job for the head, then the run a
    recorded claim (``publication_intent`` from the host publisher, or an
    operator's ``publication_head_attested``) names for this exact branch and
    head, then the legacy newest-run heuristic. The claim binding is the check
    the legacy path lacked: it ties the remediation to the run that actually
    produced the head instead of to whatever ran on the branch last.
    """
    producer = recorded_head_producer_with_credentials(
        store, branch=branch, head_sha=head_sha
    )
    if producer is not None:
        return producer
    return _legacy_branch_producer(store, branch=branch)


def recorded_head_producer_with_credentials(
    store: RunStore, *, branch: str, head_sha: str
) -> str | None:
    """The recorded producer of this exact head, with its credentials checked.

    :func:`recorded_head_producer` plus the credential checks
    :func:`publication_producer_for_head` applies to the run it finds. A head
    nobody claimed reads None, never the branch's newest run: operator
    attestation binds only through a recorded producer, because the legacy
    guess names a run for every commit on the branch, including commits that
    run never produced (#2635).
    """
    job = _normalized_job_for_head(store, branch=branch, head_sha=head_sha)
    if job is not None:
        require_normalized_job_credentials(store, job, referenced=True)
        return str(job.run_id)
    attested = _attested_head_producer(store, branch=branch, head_sha=head_sha)
    if attested is not None:
        ensure_publication_credentials(store, run_id=attested, producer_run_id=attested)
    return attested


def recorded_head_producer(
    store: RunStore, *, branch: str, head_sha: str
) -> str | None:
    """The run kew's own recorded evidence says published this exact head.

    The first two legs of :func:`publication_producer_for_head` and nothing
    else: a normalized publication job for the head, or one recorded claim
    (the host publisher's ``publication_intent`` or an operator's
    ``publication_head_attested``). There is no newest-run fallback, so a head
    nobody claimed reads None; ambiguous or unreadable evidence refuses with
    :class:`CredentialPublicationRefused` exactly as the full lookup does.
    """
    job = _normalized_job_for_head(store, branch=branch, head_sha=head_sha)
    if job is not None:
        return str(job.run_id)
    return _attested_head_producer(store, branch=branch, head_sha=head_sha)


def _attested_head_producer(
    store: RunStore, *, branch: str, head_sha: str
) -> str | None:
    """The run whose recorded head claim names this exact branch and head.

    Two shapes may claim a head. The host publisher writes
    ``publication_intent`` (policy actor ``host-publication``) immediately
    before the push that creates the head. A named human writes
    ``publication_head_attested`` for a head kew did not push itself — the
    owner approved protected output and an operator published it by hand
    (#2124) — naming the run whose output it is. Exactly one claim of either
    shape may hold a head; two claims refuse as ambiguous. An absent claim
    hands over to the legacy heuristic, so heads published before claims were
    recorded keep their existing protection.

    Item scope requires one tracker item across the branch's runs.
    Branch scope applies only when every run names no tracker item.
    Mixed ownership reads every scope the branch's runs name, under stricter
    rules (:func:`_mixed_branch_head_producer`). Evidence overflow refuses
    binding. The bound run must have a row on the requested branch.
    """
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        return None
    item_id = _single_branch_item(store, branch=branch)
    if item_id is None:
        return _mixed_branch_head_producer(store, branch=branch, head_sha=head_sha)
    claims = _intent_claims(
        store,
        item_id=item_id or branch,
        branch=branch,
        head_sha=head_sha,
        scope="item" if item_id else "branch",
    )
    if not claims:
        return None
    if len(claims) != 1:
        _refuse("publication_lineage_ambiguous")
    run_id = claims.pop()
    if not isinstance(run_id, str) or not run_id:
        _refuse("publication_evidence_unavailable")
    _require_bound_run_on_branch(store, run_id, branch=branch)
    return run_id


def _mixed_branch_head_producer(
    store: RunStore, *, branch: str, head_sha: str
) -> str | None:
    """The one run a recorded claim names for this head on a mixed branch (#3067).

    A branch whose runs name more than one tracker item (the drive's own item,
    and the empty item its merge-main ``resolve`` run recorded) used to skip
    every claim and fall to the newest-run heuristic, which refuses for good
    once any run on the branch is contained. The claim naming this exact head
    is still the evidence of who produced it, so it is read under each item
    scope a branch run names, and under the branch scope.

    No one scope owns such a branch, so the read is stricter than the
    single-scope one: exactly one (run, scope) claim may name the head across
    all of them, and the claiming run needs a row on this branch and route
    profile evidence of its own. A claim under an item no branch run names is
    never read.
    """
    claims = _scoped_head_claims(
        store,
        _mixed_branch_scopes(store, branch=branch),
        branch=branch,
        head_sha=head_sha,
    )
    if not claims:
        return None
    if len(claims) != 1:
        _refuse("publication_lineage_ambiguous")
    ((run_id, _scope_key),) = claims
    if not isinstance(run_id, str) or not run_id:
        _refuse("publication_evidence_unavailable")
    _require_bound_run_on_branch(store, run_id, branch=branch)
    _require_run_evidence(store, run_id, referenced=True)
    return run_id


def _mixed_branch_scopes(store: RunStore, *, branch: str) -> list[tuple[str, str]]:
    """The ``(audit key, scope)`` pairs a mixed branch's claims may sit under.

    Empty when no owning run is retained on the branch at all: that branch is
    unknown rather than mixed, and nothing binds there.
    """
    items = {
        publication_run_item(record) or ""
        for record in _bounded_branch_runs(store, branch=branch)
        if not abstains_from_branch_ownership(record)
    }
    if len(items) < 2:
        return []
    return [(item, "item") for item in sorted(items) if item] + [(branch, "branch")]


def recorded_head_claims(store: RunStore, *, branch: str, head_sha: str) -> set[Any]:
    """The runs whose recorded claim already names this exact branch and head.

    The run ids of :func:`recorded_head_claim_scopes`, for a reader that asks
    only who holds a head, not under which scope.
    """
    return {
        run_id
        for run_id, _scope_key in recorded_head_claim_scopes(
            store, branch=branch, head_sha=head_sha
        )
    }


def recorded_head_claim_scopes(
    store: RunStore, *, branch: str, head_sha: str
) -> set[tuple[Any, str]]:
    """Each ``(run, scope key)`` claim already naming this exact branch and head.

    The one-claim-per-head rule is enforced where claims are *read*:
    :func:`_attested_head_producer` refuses two claims on one head as
    ambiguous. A writer needs the same fact *before* it appends, because a
    chained claim cannot be withdrawn — a second claim on a head another run
    already claimed turns a working binding into a permanent refusal (#2124).

    A branch whose runs name more than one item is read across every scope its
    head lookup reads (#3193), and that lookup counts (run, scope) pairs: one
    run claiming the head under two scopes is two claims. So the scope key is
    kept, and a writer can tell its own claim under the scope it would write
    from the same run's claim under another.

    This refuses rather than returning an empty set when the branch's claims
    cannot be paged under one scope at all: evidence that cannot be read is not
    evidence of an unclaimed head.
    """
    item_id = _single_branch_item(store, branch=branch)
    if item_id is not None:
        scopes = [(item_id or branch, "item" if item_id else "branch")]
    else:
        scopes = _mixed_branch_scopes(store, branch=branch)
        if not scopes:
            _refuse("publication_lineage_ambiguous")
    return _scoped_head_claims(store, scopes, branch=branch, head_sha=head_sha)


def _scoped_head_claims(
    store: RunStore,
    scopes: list[tuple[str, str]],
    *,
    branch: str,
    head_sha: str,
) -> set[tuple[Any, str]]:
    """The ``(run, scope key)`` claims naming this head under each of *scopes*."""
    return {
        (run_id, scope_key)
        for scope_key, scope in scopes
        for run_id in _intent_claims(
            store, item_id=scope_key, branch=branch, head_sha=head_sha, scope=scope
        )
    }


def _intent_claims(
    store: RunStore, *, item_id: str, branch: str, head_sha: str, scope: str = "item"
) -> set[Any]:
    try:
        events = _scoped_intent_events(
            store, item_id=item_id, branch=branch, scope=scope
        )
        return {
            event.run_id
            for event in events
            if _intent_names_head(
                event, branch=branch, head_sha=head_sha, item_id=item_id, scope=scope
            )
        }
    except CredentialPublicationRefused:
        raise
    except Exception:
        # Unreadable or malformed intent evidence is not "no claim".
        _refuse("publication_evidence_unavailable")


def _scoped_intent_events(
    store: RunStore, *, item_id: str, branch: str, scope: str
) -> list[Any]:
    """Page every claim shape's shared audit key under one evidence bound.

    Host intents and human head attestations are separate events with separate
    actor types, so each is paged on its own. The bound spans both: thirty-two
    claims for one item is already more evidence than a decision may rest on,
    whichever shape wrote them.
    """
    relevant: list[Any] = []
    for event, actor_type, _actor_id in _HEAD_CLAIM_SOURCES:
        _collect_scoped_claims(
            store,
            relevant,
            item_id=item_id,
            branch=branch,
            scope=scope,
            event=event,
            actor_type=actor_type,
        )
    return relevant


def _collect_scoped_claims(
    store: RunStore,
    relevant: list[Any],
    *,
    item_id: str,
    branch: str,
    scope: str,
    event: str,
    actor_type: str,
) -> None:
    """Append one claim shape's events for this scope and branch, in place."""
    after_id = None
    page_size = _MAX_HEAD_INTENT_EVIDENCE + 1
    while True:
        events = store.list_audit_events(
            limit=page_size,
            event=event,
            actor_type=actor_type,
            item_id=item_id,
            after_id=after_id,
        )
        relevant.extend(
            candidate
            for candidate in events
            if _intent_matches_scope(candidate, branch=branch, scope=scope)
        )
        if len(relevant) > _MAX_HEAD_INTENT_EVIDENCE:
            _refuse("publication_lineage_too_deep")
        if len(events) < page_size:
            return
        next_id = events[-1].id
        if not isinstance(next_id, int) or (
            after_id is not None and next_id >= after_id
        ):
            _refuse("publication_evidence_unavailable")
        after_id = next_id


def _require_bound_run_on_branch(store: RunStore, run_id: str, *, branch: str) -> None:
    record, _profiles = _read_run_evidence(store, run_id)
    if record is None:
        _refuse("publication_evidence_unavailable")
    if getattr(record, "branch", None) != branch:
        _refuse("publication_lineage_ambiguous")
    if prelaunch_refusal_without_launch(store, record):
        _refuse("publication_lineage_ambiguous")


def _marked_or_legacy_refusal(record: Any) -> bool:
    if getattr(record, "pre_admission_refusal", False) is True:
        return bool(
            getattr(record, "status", None) in {"refused", "failure", "killed"}
            and isinstance(getattr(record, "exit_code", None), int)
            and record.exit_code >= 0
        )
    # Legacy rows retain the narrow proof used before the durable marker existed.
    return bool(
        getattr(record, "status", None) == "refused"
        and getattr(record, "error_category", None)
        in {"containment_refused", "reservation_replaced"}
        and getattr(record, "exit_code", None) in {1, 2}
    )


def _terminal_prelaunch_refusal(record: Any) -> bool:
    started = getattr(record, "started_at", None)
    finished = getattr(record, "finished_at", None)
    return bool(
        _marked_or_legacy_refusal(record)
        and getattr(record, "attempt_number", None) == 1
        and isinstance(finished, datetime)
        and (started is None or (isinstance(started, datetime) and finished <= started))
    )


def prelaunch_containment_refusal(store: RunStore, record: Any) -> bool:
    """Compatibility name for callers proving a containment refusal launched nothing."""
    return getattr(
        record, "error_category", None
    ) == "containment_refused" and prelaunch_refusal_without_launch(store, record)


def prelaunch_refusal_without_launch(store: RunStore, record: Any) -> bool:
    """Prove that a refusal produced no output and owns no branch.

    A terminal refusal has no launch handles, elapsed work, or retained launch
    profile. Conflicting evidence preserves protection. Unreadable evidence
    refuses publication. Completion events are not required: their writes can
    fail after the pre-launch row becomes durable.
    """
    if not _terminal_prelaunch_refusal(record):
        return False
    if any(
        getattr(record, name, "unknown") is not None
        for name in (
            "pgid",
            "container_name",
            "session_id",
            "log_file",
            "pr_url",
            "steps_to_first_edit",
            "tokens_to_first_edit",
        )
    ):
        return False
    if any(
        getattr(record, name, None) != 0
        for name in (
            "duration_seconds",
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "estimated_cost_usd",
            "classifier_overhead_input_tokens",
            "classifier_overhead_output_tokens",
            "classifier_overhead_cost_usd",
        )
    ):
        return False
    try:
        return _reviewer_retains_no_lineage(
            store, record
        ) and not store.list_audit_events(
            event="dispatch_reservation_bound", run_id=record.run_id, limit=1
        )
    except Exception:
        _refuse("publication_evidence_unavailable")


def publication_run_item(record: Any) -> str | None:
    """Return the tracker item, excluding standalone review accounting keys."""
    if getattr(record, "run_kind", None) == "review":
        return None
    item_id = getattr(record, "item_id", None)
    return item_id if isinstance(item_id, str) and item_id else None


def abstains_from_branch_ownership(record: Any) -> bool:
    """Whether *record*'s ``item_id`` can say nothing about who owns its branch.

    A standalone ``review`` row's ``item_id`` is a key the fleet counts it by —
    the reviewed PR's number, or its own run id when there is no number (the
    store backfills one so no row shares another's concurrency slot). It is
    never a tracker item: the governed remediation kind is
    ``review_remediation``. Such a row therefore ABSTAINS from its branch's
    ownership — it is not a run asserting the branch names no tracker work, so
    it must not make an owned branch read as ambiguously owned (#2446).
    """
    return getattr(record, "run_kind", None) == "review"


def serves_its_own_pr(record: Any) -> bool:
    """Whether *record* is a run on the PR opened from its branch (#3193).

    A ``ci-fix``, a merge-main ``resolve`` or an older review row records the
    PR number as its issue and, on older rows, as its item (or no item at all).
    Such a row says nothing about which node owns the branch: it worked on that
    node's PR. Recognised from the row alone — a PR-subject run whose item is
    empty or the PR number it records as its issue.
    """
    from kew.run_store import PR_SUBJECT_RUN_KINDS

    item = publication_run_item(record) or ""
    return getattr(record, "run_kind", None) in PR_SUBJECT_RUN_KINDS and item in {
        "",
        str(getattr(record, "issue_num", "")),
    }


def branch_owner_beside_its_prs(store: RunStore, *, branch: str) -> str | None:
    """The one item owning a branch whose other runs only served its PR (#3193).

    A branch carrying the node's runs and runs on the PR opened from it reads
    as two items to :func:`_single_branch_item`, so the head lookup reads it as
    a mixed branch, under every item its runs name. A writer that has to pick
    one scope for that branch — an operator's attestation, a hand review's
    claim — picks the item left once those PR runs are set aside. ``None`` when
    no single tracker item is left.
    """
    items = {
        publication_run_item(record) or ""
        for record in _bounded_branch_runs(store, branch=branch)
        if not abstains_from_branch_ownership(record) and not serves_its_own_pr(record)
    }
    return items.pop() or None if len(items) == 1 else None


def _single_branch_item(store: RunStore, *, branch: str) -> str | None:
    """Return one item, an empty string for no tracker item, or ambiguous None.

    Rows that abstain (:func:`abstains_from_branch_ownership`) are not read for
    an item at all. A branch of nothing but those names no tracker work, which
    is branch scope; a branch with no retained run at all stays ambiguous.
    """
    records = _bounded_branch_runs(store, branch=branch)
    items = {
        publication_run_item(record) or ""
        for record in records
        if not abstains_from_branch_ownership(record)
    }
    if not items:
        return "" if records else None
    if len(items) != 1:
        return None
    return items.pop()


def _intent_matches_scope(event: Any, *, branch: str, scope: str) -> bool:
    payload = event.payload
    return bool(
        isinstance(payload, dict)
        and payload.get("branch") == branch
        and payload.get("scope", "item") == scope
    )


def _head_claim_source(event: Any) -> tuple[str, str, str | None] | None:
    """The claim shape *event* is, or None when it may not claim a head."""
    for source in _HEAD_CLAIM_SOURCES:
        name, actor_type, actor_id = source
        if (event.event, event.actor_type) == (name, actor_type) and (
            actor_id is None or event.actor_id == actor_id
        ):
            return source
    return None


def _intent_names_head(
    event: Any, *, branch: str, head_sha: str, item_id: str, scope: str
) -> bool:
    payload = event.payload
    return bool(
        _head_claim_source(event) is not None
        and _intent_matches_scope(event, branch=branch, scope=scope)
        and payload.get("commit_sha") == head_sha
        and event.item_id == item_id
    )


def _normalized_job_for_head(
    store: RunStore, *, branch: str, head_sha: str
) -> Any | None:
    try:
        jobs = store.normalized_jobs_for_head(branch=branch, head_sha=head_sha, limit=2)
    except Exception:
        _refuse("publication_evidence_unavailable")
    if len(jobs) > 1:
        _refuse("publication_lineage_ambiguous")
    if not jobs:
        return None
    job = jobs[0]
    if job.branch != branch or job.head_sha != head_sha:
        _refuse("publication_lineage_ambiguous")
    return job


def _legacy_branch_producer(store: RunStore, *, branch: str) -> str | None:
    """Select the newest producer, excluding attested reviews that cannot publish.

    Empty DEGRADED and UNPARSEABLE failures never supply publication ancestry.
    Completed READY and NEEDS_CHANGES reviews use the governed exemption.
    """
    branch_runs = [
        record
        for record in _bounded_branch_runs(store, branch=branch)
        if not _empty_failed_adversarial_review(store, record)
        and not _attested_governed_adversarial_review(store, record)
        and not _base_integration_marker(store, record)
    ]
    if not branch_runs:
        return None
    protected_records = _protected_branch_records(store, branch_runs)
    _require_head_binding_for_protected(store, protected_records)
    run_id = branch_runs[0].run_id
    if not isinstance(run_id, str) or not run_id:
        _refuse("publication_evidence_unavailable")
    return run_id


def _base_integration_marker(store: RunStore, record: Any) -> bool:
    """Whether *record* is the epic driver's merge-main bookkeeping row (#3067).

    ``record_base_integration_step`` writes a ``resolve`` row with a marker
    ``error_category`` to bound its streak. No agent ran under it, so it has no
    route profile and never produced a head. Named as the newest producer, it
    refused every later launch as missing ancestor evidence, for good. A row
    that carries a profile is not bookkeeping and keeps its protection.
    """
    if (
        getattr(record, "run_kind", None) != "resolve"
        or getattr(record, "error_category", None) not in MARKER_ERROR_CATEGORIES
    ):
        return False
    try:
        return not list(store.list_route_profiles(record.run_id))
    except Exception:
        _refuse("publication_evidence_unavailable")


def _terminal_failed_adversarial_review(record: Any) -> bool:
    return bool(
        (
            getattr(record, "run_kind", None),
            getattr(record, "status", None),
            getattr(record, "pr_url", None),
        )
        == ("adversarial_review", "failure", None)
        and type(getattr(record, "exit_code", None)) is int
        and record.exit_code != 0
        and isinstance(getattr(record, "finished_at", None), datetime)
        and all(
            isinstance(value, str) and value
            for value in (
                getattr(record, "run_id", None),
                getattr(record, "item_id", None),
            )
        )
    )


def _empty_failed_adversarial_review(store: RunStore, record: Any) -> bool:
    """Exclude attested empty DEGRADED or UNPARSEABLE failures from producer selection.

    Both need exactly one policy-attested verdict event naming this run with
    the empty digest, and a run that retains no lineage and names no issued
    key. DEGRADED also needs its one matching output-free round. UNPARSEABLE
    is decided by the run's own verdict event alone: the driver records no
    round for it, so any row at its head and round belongs to a later review
    that reused the number, and must not revoke this exemption.
    Missing or conflicting evidence leaves the original protection in place.
    Explicit ancestors, continuations, and normalized jobs never use this rule.
    """
    try:
        if not _terminal_failed_adversarial_review(record):
            return False
        if not _reviewer_retains_no_lineage(store, record):
            return False
        matches = _verdict_events_for_run(store, record.run_id)
        if len(matches) != 1 or not _empty_review_verdict(matches[0], record):
            return False
        if matches[0].payload["verdict"] == "UNPARSEABLE":
            return True
        return _matching_empty_review_round(store, record.item_id, matches[0].payload)
    except Exception:
        _refuse("publication_evidence_unavailable")


def _verdict_events_for_run(store: RunStore, run_id: str) -> list[Any]:
    """Every ``adversarial_review_verdict`` event naming *run_id*, any item.

    The lookup is keyed on the run, not on the record's item: a second
    verdict event for the same run filed under another item id is
    conflicting evidence, and filtering by item would hide it. The caller
    requires exactly one event and then validates its envelope and payload
    item ids against the record. More events than the evidence window holds
    refuses rather than deciding from a truncated view.
    """
    events = store.list_audit_events(
        limit=_MAX_REVIEW_EVIDENCE + 1,
        event=_REVIEW_VERDICT_EVENT,
        run_id=run_id,
    )
    if len(events) > _MAX_REVIEW_EVIDENCE:
        return []
    return [event for event in events if event.run_id == run_id]


def _reviewer_retains_no_lineage(store: RunStore, record: Any) -> bool:
    """A real reviewer records no route profile, continues from nothing, and
    mints no key.

    Any one means the record is not the unprofiled reviewer the exemptions
    describe: a relabeled producer keeps its exposure profile, a continuation
    keeps the ancestry the guard must still walk, and an issued key must stay
    under the issuance-evidence gate, which an exempted run would skip.
    """
    if list(store.list_route_profiles(record.run_id)):
        return False
    if list(store.continuation_origins(record.run_id, limit=2)):
        return False
    # Stricter than the gate's own key set: any issuer record, whatever its
    # phase or actor, keeps the run a candidate.
    return not any(
        isinstance(payload := getattr(event, "payload", None), Mapping)
        and "credential_issuer" in payload
        for event in _issued_key_events(store, record.run_id)
    )


def _empty_review_verdict(event: Any, record: Any) -> bool:
    if (event.event, event.run_id, event.item_id, event.actor_type, event.actor_id) != (
        _REVIEW_VERDICT_EVENT,
        record.run_id,
        record.item_id,
        "policy",
        "epic-driver",
    ):
        return False
    payload = event.payload
    return bool(
        isinstance(payload, dict)
        and (
            payload.get("item_id"),
            payload.get("verdict"),
            payload.get("sidecar_digest"),
        )
        in (
            (record.item_id, "DEGRADED", _EMPTY_REVIEW_DIGEST),
            (record.item_id, "UNPARSEABLE", _EMPTY_REVIEW_DIGEST),
        )
        and type(payload.get("round")) is int
        and payload["round"] > 0
        and isinstance(payload.get("head_sha"), str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", payload["head_sha"])
    )


def _matching_empty_review_round(store: RunStore, item_id: str, payload: Any) -> bool:
    rounds = store.list_review_rounds(item_id)
    if len(rounds) > _MAX_REVIEW_EVIDENCE:
        return False
    matches = [
        row
        for row in rounds
        if (row.item_id, row.head_sha, row.round)
        == (item_id, payload["head_sha"], payload["round"])
    ]
    return (
        len(matches) == 1
        and type(matches[0].round) is int
        and (matches[0].verdict, matches[0].sidecar_digest, matches[0].sidecar_path)
        == ("DEGRADED", _EMPTY_REVIEW_DIGEST, None)
    )


# NEEDS_FULL (#2111) is a reviewer's delta READY recorded as still owing a
# full round: a real reviewer run with a real sidecar, like the other two.
_GOVERNED_REVIEW_VERDICTS = frozenset({"READY", "NEEDS_CHANGES", "NEEDS_FULL"})


def _terminal_completed_adversarial_review(record: Any) -> bool:
    return bool(
        (
            getattr(record, "run_kind", None),
            getattr(record, "status", None),
            getattr(record, "exit_code", None),
            getattr(record, "pr_url", None),
        )
        == ("adversarial_review", "success", 0) + (None,)
        and type(getattr(record, "exit_code", None)) is int
        and isinstance(getattr(record, "finished_at", None), datetime)
        and all(
            isinstance(value, str) and value
            for value in (
                getattr(record, "run_id", None),
                getattr(record, "item_id", None),
            )
        )
    )


def _attested_governed_adversarial_review(store: RunStore, record: Any) -> bool:
    """Exclude an attested completed governed review from producer selection.

    A contained reviewer never publishes a branch head: the review checkout
    is read for a verdict and the run kind is exempt from launch credentials.
    Selecting it as the legacy branch producer refused every remediation
    after a successful contained review, because the reviewer records no
    route profile. The exemption is as narrow as the empty-failure one: the
    run finished successfully without a PR, it retains no route profile, no
    continuation origin, and no issuer record (a real reviewer has none; any
    one means the record is not the unprofiled reviewer this rule describes),
    exactly one policy-attested ``adversarial_review_verdict`` names it with a
    real verdict and a non-empty sidecar digest, and exactly one stored review
    round agrees on head, round, verdict, and digest. Missing, unavailable,
    or conflicting evidence leaves the original protection in place.
    """
    try:
        if not _terminal_completed_adversarial_review(record):
            return False
        if not _reviewer_retains_no_lineage(store, record):
            return False
        matches = _verdict_events_for_run(store, record.run_id)
        if len(matches) != 1 or not _governed_review_verdict(matches[0], record):
            return False
        return _matching_governed_review_round(
            store, record.item_id, matches[0].payload
        )
    except Exception:
        _refuse("publication_evidence_unavailable")


def _governed_review_verdict(event: Any, record: Any) -> bool:
    if (event.event, event.run_id, event.item_id, event.actor_type, event.actor_id) != (
        _REVIEW_VERDICT_EVENT,
        record.run_id,
        record.item_id,
        "policy",
        "epic-driver",
    ):
        return False
    payload = event.payload
    if not isinstance(payload, dict) or payload.get("item_id") != record.item_id:
        return False
    return _governed_verdict_digest(payload) and _review_round_locator(payload)


def _governed_verdict_digest(payload: dict[str, Any]) -> bool:
    if payload.get("verdict") not in _GOVERNED_REVIEW_VERDICTS:
        return False
    digest = payload.get("sidecar_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    return digest != _EMPTY_REVIEW_DIGEST


def _review_round_locator(payload: dict[str, Any]) -> bool:
    round_number = payload.get("round")
    head_sha = payload.get("head_sha")
    if type(round_number) is not int or round_number <= 0:
        return False
    return isinstance(head_sha, str) and bool(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha)
    )


def _matching_governed_review_round(
    store: RunStore, item_id: str, payload: Any
) -> bool:
    rounds = store.list_review_rounds(item_id)
    if len(rounds) > _MAX_REVIEW_EVIDENCE:
        return False
    matches = [
        row
        for row in rounds
        if (row.item_id, row.head_sha, row.round)
        == (item_id, payload["head_sha"], payload["round"])
    ]
    return (
        len(matches) == 1
        and type(matches[0].round) is int
        and (matches[0].verdict, matches[0].sidecar_digest)
        == (payload["verdict"], payload["sidecar_digest"])
        and isinstance(matches[0].sidecar_path, str)
        and bool(matches[0].sidecar_path)
    )


def publication_branch_runs(store: RunStore, *, branch: str) -> list[Any]:
    """Read distinct potential producers using descending branch-history keys.

    Retain one overflow row. Proven pre-launch refusals consume no slots.
    Each page is independent.
    This read never controls the shared connection's transaction.
    """
    try:
        return _publication_branch_runs_pages(store, branch=branch)
    except CredentialPublicationRefused:
        raise
    except Exception as exc:
        raise CredentialPublicationRefused("publication_evidence_unavailable") from exc


def _publication_branch_runs_pages(store: RunStore, *, branch: str) -> list[Any]:
    """Read pages until distinct producers overflow or branch history ends."""
    branch_runs: dict[str, Any] = {}
    page_size = _MAX_BRANCH_RUNS + 1
    cursor: tuple[str, int] | None = None
    while True:
        page, cursor = store.list_runs_by_branch_after(
            branch, limit=page_size, after=cursor
        )
        for record in page:
            if not prelaunch_refusal_without_launch(store, record):
                branch_runs.setdefault(record.run_id, record)
        if len(branch_runs) >= page_size or len(page) < page_size:
            return list(branch_runs.values())[:page_size]


def _bounded_branch_runs(store: RunStore, *, branch: str) -> list[Any]:
    branch_runs = publication_branch_runs(store, branch=branch)
    if len(branch_runs) > _MAX_BRANCH_RUNS:
        raise branch_run_cap_refusal()
    return branch_runs


def _protected_branch_records(store: RunStore, branch_runs: list[Any]) -> list[Any]:
    protected_records: list[Any] = []
    for record in branch_runs:
        try:
            profiles = list(store.list_route_profiles(record.run_id))
            if _known_contained(record, profiles, record.run_id):
                protected_records.append(record)
        except Exception:
            _refuse("publication_evidence_unavailable")
    return protected_records


def _require_head_binding_for_protected(
    store: RunStore, protected_records: list[Any]
) -> None:
    for record in protected_records:
        require_publication_credentials(
            store,
            run_id=record.run_id,
            producer_run_id=record.run_id,
        )
    if protected_records:
        _refuse("publication_lineage_ambiguous")
