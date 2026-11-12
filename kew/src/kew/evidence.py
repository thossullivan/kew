"""The audit ledger is the one host evidence sequence (I06).

Kew already owns a tamper-evident, hash-chained, monotonically-numbered append
log: ``audit_events``. This module is the strict writer/reader over that log
for *evidence* — the receipt facts, attempted-change checkpoints and artifact
references a customer receipt is later assembled from. It creates no second
table, no second sequence and no second chain, and it adds no new slot to the
``entry_hash`` preimage: every fact it records travels inside the already-hashed
``payload`` dict.

What "strict" buys, versus :func:`kew.audit.emit`:

* **Required facts fail closed.** :func:`record_evidence` validates the fact
  set *before* it writes and raises :class:`EvidenceWriteError` when the append
  did not happen. A receipt that silently lost a fact is a receipt that lies;
  the dispatch that cannot record it must fail instead.
* **Optional telemetry stays best-effort.** :func:`emit_evidence` keeps the
  legacy swallow-and-log behaviour, and refuses to be pointed at a required
  kind — telemetry may be missing, receipts may not.
* **Every row names its producer.** Host-derived and runner-derived evidence
  are distinguishable forever (:class:`ProducerKind`,
  :func:`import_runner_evidence`); an imported fact is never replayed as a host
  attestation.
* **The fact set is closed.** Each kind declares exactly which keys it accepts,
  so raw prompt bytes, transcripts and private reasoning have nowhere to land
  even by accident, and ``digest`` must actually be a digest.

Ordering is ``audit_events.id`` — the insertion sequence — never the wall
clock. Clocks disagree across containers, hosts and restarts; the row id does
not. :func:`replay_host_sequence` walks that order, verifies the chain, and
reports what it could NOT read as explicit gaps rather than dropping it.

One thing this module deliberately does not do: resolve what to do next. No
evidence row may carry a ``safest_action``, and nothing here imports
``kew.service``. The work projection stays the single resolver; evidence is
what it reads, not a competing answer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from kew.audit import Actor
from kew.sensitive_prompt import is_digest

_logger = logging.getLogger(__name__)

EVIDENCE_SCHEMA_VERSION = 1
"""Payload contract version. A newer row replays as a gap, never a guess."""

EVIDENCE_EVENT_PREFIX = "evidence."

#: The receipt fact a contained launch records for the commit its container
#: starts from, read on the host before the checkout is agent-writable.
TRUSTED_BASE_FACT = "trusted_base_sha"


class EvidenceKind(StrEnum):
    """The evidence events this module may append to the ledger."""

    ATTEMPTED_CHANGE = "evidence.attempted_change"
    RECEIPT_FACT = "evidence.receipt_fact"
    ARTIFACT = "evidence.artifact"
    RUNNER_PROGRESS = "evidence.runner_progress"
    RUNNER_USAGE = "evidence.runner_usage"


class ProducerKind(StrEnum):
    """Who derived a fact — the host itself, or a runner it imported from."""

    HOST = "host"
    RUNNER = "runner"


class AttemptedChange(StrEnum):
    """What became of one attempted change.

    These stay distinguishable forever: a change kew *filtered* before it
    reached the workspace is not one the agent *reverted*, which is not one
    publication *dropped*, which is not one policy *refused*, which is not one
    kew *preserved* for review — and none of them is an attempt whose outcome
    is simply *unavailable*.
    """

    FILTERED = "filtered"
    REVERTED = "reverted"
    DROPPED = "dropped"
    REFUSED = "refused"
    PRESERVED = "preserved"
    UNAVAILABLE = "unavailable"


class EvidenceError(RuntimeError):
    """Base class for evidence refusals."""


class EvidenceFactError(EvidenceError):
    """The offered fact set is not one this kind is allowed to record."""


class EvidenceWriteError(EvidenceError):
    """A required fact could not be appended to the ledger."""


RESERVED_FACT_KEYS = frozenset(
    {
        "prompt",
        "prompt_text",
        "compiled_prompt",
        "transcript",
        "raw",
        "raw_output",
        "reasoning",
        "safest_action",
    }
)
"""Keys no evidence row may ever carry, whatever the kind.

The first seven are content that belongs in a protected artifact (or nowhere).
``safest_action`` is refused for a different reason: the work projection is the
one resolver, and an evidence row that answered it would become a competing
one."""


@dataclass(frozen=True)
class _Spec:
    """What one evidence kind requires, accepts, and how it fails."""

    required_facts: frozenset[str]
    optional_facts: frozenset[str]
    required: bool


_COMMON_OPTIONAL = frozenset({"attempt_number", "item_id", "detail"})

_SPECS: dict[EvidenceKind, _Spec] = {
    EvidenceKind.ATTEMPTED_CHANGE: _Spec(
        required_facts=frozenset({"subject", "disposition"}),
        optional_facts=_COMMON_OPTIONAL | {"reason", "digest"},
        required=True,
    ),
    EvidenceKind.RECEIPT_FACT: _Spec(
        required_facts=frozenset({"fact", "value"}),
        optional_facts=_COMMON_OPTIONAL | {"unit", "source"},
        required=True,
    ),
    EvidenceKind.ARTIFACT: _Spec(
        required_facts=frozenset({"artifact_kind", "digest", "byte_length", "storage"}),
        optional_facts=_COMMON_OPTIONAL | {"locator", "expires_at"},
        required=True,
    ),
    EvidenceKind.RUNNER_PROGRESS: _Spec(
        required_facts=frozenset({"phase"}),
        optional_facts=_COMMON_OPTIONAL,
        required=False,
    ),
    EvidenceKind.RUNNER_USAGE: _Spec(
        required_facts=frozenset({"metric", "value"}),
        optional_facts=_COMMON_OPTIONAL | {"unit"},
        required=False,
    ),
}

TELEMETRY_KINDS = tuple(kind for kind, spec in _SPECS.items() if not spec.required)
"""The kinds whose absence must be reported rather than read as zero."""


@dataclass(frozen=True)
class Producer:
    """The provenance of a fact: what derived it, at which version."""

    kind: ProducerKind
    name: str
    version: str

    def as_facts(self) -> dict[str, str]:
        return {
            "producer_kind": str(self.kind),
            "producer_name": self.name,
            "producer_version": self.version,
        }


class _AppendOnly(Protocol):
    """The one store capability this module needs."""

    def append_audit_event(self, **kwargs: Any) -> int | None: ...


def _validate_facts(kind: EvidenceKind, facts: Mapping[str, Any]) -> dict[str, Any]:
    """Return the accepted fact dict, or raise :class:`EvidenceFactError`."""
    spec = _SPECS[kind]
    reserved = RESERVED_FACT_KEYS & set(facts)
    if reserved:
        raise EvidenceFactError(f"{kind.value} may never carry {sorted(reserved)}")
    unknown = set(facts) - spec.required_facts - spec.optional_facts
    if unknown:
        raise EvidenceFactError(f"{kind.value} does not accept {sorted(unknown)}")
    missing = [name for name in sorted(spec.required_facts) if _is_blank(facts, name)]
    if missing:
        raise EvidenceFactError(f"{kind.value} requires {missing}")
    normalized = {key: _normalize(value) for key, value in facts.items()}
    _validate_kind_values(kind, normalized)
    return normalized


def _is_blank(facts: Mapping[str, Any], name: str) -> bool:
    value = facts.get(name)
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize(value: Any) -> Any:
    """Enums travel as their string value; everything else travels as-is."""
    return str(value) if isinstance(value, StrEnum) else value


def _validate_kind_values(kind: EvidenceKind, facts: Mapping[str, Any]) -> None:
    """Per-kind value checks that a closed key set cannot express."""
    if kind is EvidenceKind.ATTEMPTED_CHANGE:
        disposition = facts["disposition"]
        if disposition not in {str(member) for member in AttemptedChange}:
            raise EvidenceFactError(f"unknown attempted-change: {disposition!r}")
    if "digest" in facts and not is_digest(facts["digest"]):
        raise EvidenceFactError("digest facts must be a sha256:<hex> reference")


def _build_payload(
    kind: EvidenceKind, producer: Producer, facts: dict[str, Any]
) -> dict[str, Any]:
    return {
        "evidence_schema": EVIDENCE_SCHEMA_VERSION,
        "evidence_kind": kind.value,
        **producer.as_facts(),
        "attested_by_host": producer.kind is ProducerKind.HOST,
        "facts": facts,
    }


def record_evidence(
    store: object,
    kind: EvidenceKind,
    *,
    actor: Actor,
    producer: Producer,
    facts: Mapping[str, Any],
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
    endpoint: str | None = None,
    dedup_key: str | None = None,
    now: datetime | None = None,
) -> int:
    """Append one evidence row to the audit ledger, or raise.

    Validation happens before the write, so a rejected fact set leaves the
    ledger untouched. Returns the ``audit_events.id`` of the new row — the
    row's position in the one host sequence.
    """
    payload = _build_payload(kind, producer, _validate_facts(kind, facts))
    append = getattr(store, "append_audit_event", None)
    if not callable(append):
        raise EvidenceWriteError(
            f"store cannot record {kind.value}: it has no audit append surface"
        )
    kwargs: dict[str, Any] = dict(
        timestamp=(now or datetime.now(UTC)).isoformat(),
        event=kind.value,
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        actor_type=actor.type,
        actor_id=actor.id,
        endpoint=endpoint,
        payload=payload,
    )
    if dedup_key is not None:
        once = getattr(store, "append_audit_event_once", None)
        if callable(once):
            # Route the dedup write through _append too, so a raw store failure
            # becomes an EvidenceWriteError here as well: otherwise emit_evidence
            # ("never raises") leaks it and record_evidence raises the wrong type.
            return _require_id(
                kind, _append(kind, once, {"dedup_key": dedup_key, **kwargs})
            )
    return _require_id(kind, _append(kind, append, kwargs))


def _append(kind: EvidenceKind, append: Any, kwargs: dict[str, Any]) -> int | None:
    try:
        result = append(**kwargs)
    except Exception as exc:
        raise EvidenceWriteError(f"could not record {kind.value}: {exc}") from exc
    return None if result is None else int(result)


def _require_id(kind: EvidenceKind, event_id: int | None) -> int:
    if event_id is None:
        raise EvidenceWriteError(f"{kind.value} was not appended to the ledger")
    return int(event_id)


def emit_evidence(
    store: object,
    kind: EvidenceKind,
    *,
    actor: Actor,
    producer: Producer,
    facts: Mapping[str, Any],
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
    endpoint: str | None = None,
    dedup_key: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Best-effort append for optional telemetry. Never raises on write.

    Refuses a *required* kind outright: fail-closed facts must go through
    :func:`record_evidence`, and quietly downgrading one here would be exactly
    the silent receipt gap this module exists to prevent.
    """
    if _SPECS[kind].required:
        raise EvidenceWriteError(
            f"{kind.value} is required evidence — use record_evidence"
        )
    try:
        return record_evidence(
            store,
            kind,
            actor=actor,
            producer=producer,
            facts=facts,
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=repo,
            endpoint=endpoint,
            dedup_key=dedup_key,
            now=now,
        )
    except EvidenceWriteError as exc:
        _logger.warning("Optional evidence %s dropped: %s", kind.value, exc)
        return None


def import_runner_evidence(
    store: object,
    kind: EvidenceKind,
    *,
    actor: Actor,
    producer: Producer,
    facts: Mapping[str, Any],
    **kwargs: Any,
) -> int | None:
    """Record evidence derived from a runner, marked as such forever.

    The producer is forced to :attr:`ProducerKind.RUNNER` regardless of what
    the caller passed, so an importer cannot launder a runner claim into a host
    attestation. Required kinds still fail closed; optional kinds stay
    best-effort.
    """
    runner = Producer(
        kind=ProducerKind.RUNNER, name=producer.name, version=producer.version
    )
    writer = record_evidence if _SPECS[kind].required else emit_evidence
    return writer(store, kind, actor=actor, producer=runner, facts=facts, **kwargs)
