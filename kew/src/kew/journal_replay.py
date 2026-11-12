"""Replay the host evidence sequence from the audit journal (read side).

The evidence WRITE surface lives in :mod:`kew.evidence`; this module is the
READ/projection side. It verifies the hash chain and replays the append-only
``evidence.*`` audit rows in ``audit_events.id`` order into records, then
projects telemetry availability and attempted changes over them.

It is a separate module so the service/read projection layer can consume replay
WITHOUT importing the evidence writer — the boundary enforced by
``test_the_work_projection_never_imports_the_evidence_module``. This module may
import the shared evidence vocabulary; nothing here writes the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kew.audit_chain import ChainResult, verify_chain
from kew.evidence import (
    EVIDENCE_EVENT_PREFIX,
    EVIDENCE_SCHEMA_VERSION,
    TELEMETRY_KINDS,
    EvidenceKind,
    Producer,
    ProducerKind,
)


@dataclass(frozen=True)
class EvidenceRecord:
    """One replayed evidence row, in host-sequence position ``audit_id``."""

    audit_id: int
    timestamp: str
    kind: str
    schema_version: int
    producer: Producer
    attested_by_host: bool
    facts: Mapping[str, Any]
    run_id: str | None = None
    item_id: str | None = None
    issue_num: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", dict(self.facts))

    def __hash__(self) -> int:  # frozen + dict field: hash on identity keys
        return hash((self.audit_id, self.kind, self.timestamp))


@dataclass(frozen=True)
class EvidenceGap:
    """A ledger row that claims to be evidence but cannot be replayed as such."""

    audit_id: int
    event: str
    reason: str


@dataclass(frozen=True)
class ReplayResult:
    """The replayed host sequence, plus everything that could not be read."""

    chain: ChainResult
    records: tuple[EvidenceRecord, ...]
    gaps: tuple[EvidenceGap, ...]

    @property
    def ok(self) -> bool:
        """True only when the chain verified AND every row replayed."""
        return self.chain.ok and not self.gaps


def replay_host_sequence(
    store: object,
    *,
    run_id: str | None = None,
    item_id: str | None = None,
    chain: ChainResult | None = None,
) -> ReplayResult:
    """Verify the chain, then replay evidence in ``audit_events.id`` order.

    ``run_id`` is pushed into the query, not applied to the result:
    ``audit_events.run_id`` is indexed (``idx_audit_events_run_id``), so one
    run's evidence is a seek rather than a read of every evidence row ever
    appended followed by a filter in Python (C3/#2296).

    ``chain`` lets a caller that already holds a verdict for this ledger hand
    it in instead of paying another O(N) hash walk — the memoized verdict in
    :func:`kew.service.audit.chain_verdict`. Passing a verdict from a
    DIFFERENT store would report that store's chain here, so only pass one
    read from ``store``.
    """
    if chain is None:
        chain = verify_chain(list(store.read_chain_rows()))  # type: ignore[attr-defined]
    events = store.list_audit_events(  # type: ignore[attr-defined]
        limit=None,
        event_prefix=EVIDENCE_EVENT_PREFIX,
        item_id=item_id,
        run_id=run_id,
    )
    records: list[EvidenceRecord] = []
    gaps: list[EvidenceGap] = []
    for event in sorted(events, key=lambda ev: ev.id):
        parsed = _replay_one(event)
        if isinstance(parsed, EvidenceGap):
            gaps.append(parsed)
        else:
            records.append(parsed)
    return ReplayResult(chain=chain, records=tuple(records), gaps=tuple(gaps))


def _replay_one(event: Any) -> EvidenceRecord | EvidenceGap:
    """Turn one audit row into a record, or name why it could not be."""
    payload = event.payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return EvidenceGap(event.id, event.event, "unreadable_payload")
    if not isinstance(payload, dict):
        return EvidenceGap(event.id, event.event, "unreadable_payload")
    if payload.get("evidence_schema") != EVIDENCE_SCHEMA_VERSION:
        return EvidenceGap(event.id, event.event, "unsupported_schema")
    producer = _replay_producer(payload)
    if producer is None:
        return EvidenceGap(event.id, event.event, "missing_provenance")
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return EvidenceGap(event.id, event.event, "unreadable_facts")
    return EvidenceRecord(
        audit_id=event.id,
        timestamp=event.timestamp,
        kind=str(payload.get("evidence_kind") or event.event),
        schema_version=EVIDENCE_SCHEMA_VERSION,
        producer=producer,
        attested_by_host=bool(payload.get("attested_by_host")),
        facts=facts,
        run_id=event.run_id,
        item_id=event.item_id,
        issue_num=event.issue_num,
    )


def _replay_producer(payload: Mapping[str, Any]) -> Producer | None:
    """Rebuild provenance, or None when the row never carried any."""
    kind = payload.get("producer_kind")
    name = payload.get("producer_name")
    version = payload.get("producer_version")
    if not (isinstance(kind, str) and isinstance(name, str)):
        return None
    if kind not in {str(member) for member in ProducerKind}:
        return None
    return Producer(
        kind=ProducerKind(kind),
        name=name,
        version=version if isinstance(version, str) else "",
    )


def telemetry_status(
    result: ReplayResult, *, kinds: tuple[EvidenceKind, ...] = TELEMETRY_KINDS
) -> dict[str, str]:
    """Say ``present`` or ``unavailable`` for each telemetry kind.

    Best-effort telemetry can genuinely be missing. Reporting that absence
    explicitly is the point: a consumer that saw no usage rows must never
    conclude the run used nothing.
    """
    seen = {record.kind for record in result.records}
    return {
        kind.value: "present" if kind.value in seen else "unavailable" for kind in kinds
    }


def attempted_changes(result: ReplayResult) -> tuple[tuple[str, str], ...]:
    """Every attempted change as ``(subject, disposition)``, in ledger order."""
    return tuple(
        (str(record.facts.get("subject")), str(record.facts.get("disposition")))
        for record in result.records
        if record.kind == EvidenceKind.ATTEMPTED_CHANGE.value
    )
