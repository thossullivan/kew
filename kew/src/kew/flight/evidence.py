"""Receipt-backed evidence: what the Flight Recorder is allowed to show (I16).

The recorder used to answer "what happened in this run?" out of two mutable
places — a speculative tree of git snapshots captured from the agent's own
worktree, and a diff computed against whatever the host repository directory
contained when the page was opened. Both could change after the run ended, and
neither was bound to the thing that would actually be published.

This module is the replacement read model. It projects the two durable things
I06 and I07 already produce:

* **The host sequence** — evidence rows on the ``audit_events`` chain, ordered
  by row id rather than by any clock and replayed by :mod:`kew.evidence`.
* **The sealed artifact** — the receipt whose ``through_seq`` names the exact
  prefix of that sequence it covers (:mod:`kew.artifact_receipt`), addressing a
  preserved work package by digest (:mod:`kew.artifact_store`).

Three rules keep the projection honest.

1. **The prefix is the seal.** Every event at or before ``through_seq`` is
   covered by the receipt; everything after it is a later chained fact and is
   labelled :attr:`FlightEvent.sealed` ``False``. The split is recorded on each
   event rather than left to be inferred from its position in a list.
2. **A diff is bound to the artifact or it is refused.** The package is opened
   by the receipt's own ``package_digest`` — which re-verifies every blob and
   re-derives the name — and its base has to be the base the receipt sealed.
   There is no repository path in this module, so there is nothing to point at
   a working tree that has moved on.
3. **Missing is missing.** Absent runner telemetry becomes an explicit
   ``unavailable`` dimension, never a zero and never a value taken from another
   run. The same goes for what this route structurally cannot know: the sealed
   package carries final content only, so line counts have no base to be
   counted against and say so.

Nothing here reads the run table, a worktree, or a git object database. The
inputs are a replayed ledger, a receipt row, and bytes under the host's own
private package root; :mod:`kew.service.runs` is what fetches those and turns
what comes back into DTOs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kew.artifact_receipt import ArtifactReceipt, ReceiptAttestation
from kew.evidence import EvidenceKind, ProducerKind
from kew.journal_replay import ReplayResult
from kew.publication_artifact import Artifact

__all__ = [
    "AVAILABLE",
    "DIM_ARTIFACT_DIFF",
    "DIM_ARTIFACT_SEAL",
    "DIM_DIFF_LINE_COUNTS",
    "DIM_HOST_SEQUENCE",
    "DIM_RUNNER_PROGRESS",
    "DIM_RUNNER_USAGE",
    "ERROR",
    "MAX_FILE_TEXT_BYTES",
    "MAX_TOTAL_TEXT_BYTES",
    "OBSERVED",
    "UNAVAILABLE",
    "UNOBSERVED",
    "ArtifactDiff",
    "ArtifactFile",
    "ChangeKind",
    "EventOrigin",
    "EvidenceAvailability",
    "FlightEvent",
    "FlightGap",
    "FlightTimeline",
    "TelemetryMode",
    "artifact_node_id",
    "build_artifact_diff",
    "build_flight_timeline",
]

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

OBSERVED = "observed"
UNOBSERVED = "unobserved"
ERROR = "error"

DIM_HOST_SEQUENCE = "host_sequence"
DIM_ARTIFACT_SEAL = "artifact_seal"
DIM_ARTIFACT_DIFF = "artifact_diff"
DIM_DIFF_LINE_COUNTS = "diff_line_counts"
DIM_RUNNER_PROGRESS = "runner_progress"
DIM_RUNNER_USAGE = "runner_usage"

#: Per-file and whole-diff ceilings on the text this route will carry. A
#: package may legitimately hold 20,000 entries; a read surface that tried to
#: inline all of them would be a denial of service written by the agent whose
#: work it is displaying. Over the ceiling the bytes are not shown and the
#: entry says why — the file is still listed, with its digest and length.
MAX_FILE_TEXT_BYTES = 256 * 1024
MAX_TOTAL_TEXT_BYTES = 2 * 1024 * 1024

_TEXT_TOO_LARGE = "the entry is larger than this route will inline"
_TEXT_BUDGET_SPENT = "the diff's inline-text budget was already spent"
_TEXT_NOT_UTF8 = "the entry is not UTF-8 text"

_LINE_COUNTS_DETAIL = (
    "the sealed package carries final content only; the base content the"
    " counts would be measured against is not part of this route"
)


class EventOrigin(StrEnum):
    """Which durable surface an event was read off."""

    EVIDENCE = "evidence"
    ATTESTATION = "attestation"


class TelemetryMode(StrEnum):
    """How much the runner actually told the host about this run.

    Kept as three values rather than a boolean because they are three
    different states: a run with usage metrics, a run that reported only that
    it was doing something, and a run that reported nothing. Collapsing the
    last two would let "we never heard from the runner" read as "the runner
    was idle".
    """

    STRUCTURED = "structured"
    ACTIVITY_ONLY = "activity_only"
    MISSING = "missing"


class ChangeKind(StrEnum):
    """What the package says became of one path.

    Deliberately two values, not three: a package carries the final content of
    every path it touched, and nothing that would distinguish a file the agent
    created from one it edited. Reporting ``added`` would be a guess about a
    base this route never reads.
    """

    CHANGED = "changed"
    DELETED = "deleted"


@dataclass(frozen=True)
class EvidenceAvailability:
    """Whether one dimension could be read, and on what basis.

    ``provenance`` separates the two ways a dimension goes missing: nothing was
    ever recorded (``unobserved``) versus something was recorded and could not
    be trusted (``error``). A consumer must be able to tell "the runner sent no
    usage" from "the ledger is broken".
    """

    dimension: str
    availability: str
    provenance: str
    detail: str = ""


@dataclass(frozen=True)
class FlightEvent:
    """One event in the host sequence, at its ledger position."""

    seq: int
    timestamp: str
    kind: str
    origin: str
    producer_kind: str
    producer_name: str
    producer_version: str
    attested_by_host: bool
    sealed: bool
    terminal: bool = False
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlightGap:
    """A ledger row that claims to be evidence and could not be read as one."""

    seq: int
    event: str
    reason: str


@dataclass(frozen=True)
class FlightTimeline:
    """The replayed sequence for one run/attempt, and what it could not say."""

    run_id: str
    attempt_number: int
    item_id: str | None
    issue_num: int | None
    source_tracker: str | None
    source_id: str | None
    sealed: bool
    through_seq: int | None
    chain_head: str | None
    chain_verified: bool
    artifact_node_id: str | None
    runner_telemetry: str
    events: tuple[FlightEvent, ...]
    gaps: tuple[FlightGap, ...]
    availability: tuple[EvidenceAvailability, ...]


@dataclass(frozen=True)
class ArtifactFile:
    """One path the sealed package accounts for."""

    path: str
    change: str
    mode: str = ""
    digest: str = ""
    byte_length: int = 0
    text: str | None = None
    text_availability: str = AVAILABLE
    detail: str = ""


@dataclass(frozen=True)
class ArtifactDiff:
    """The change set of one sealed package, named by what binds it."""

    run_id: str
    attempt_number: int
    node_id: str
    base_sha: str
    tree_sha: str
    package_digest: str
    receipt_digest: str
    verification: str
    files: tuple[ArtifactFile, ...]
    text: str
    availability: tuple[EvidenceAvailability, ...]


def artifact_node_id(receipt: ArtifactReceipt) -> str:
    """The one node a diff may be asked for: the seal's ledger position.

    A receipt covers the sequence *through* ``through_seq``, so that id is
    exactly the point in the timeline the artifact is bound to. Naming the node
    after it means "diff at this node" and "diff of the sealed artifact" cannot
    drift apart — there is no second identifier to keep in step.
    """
    return f"{receipt.run_id}:{receipt.through_seq}"


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


def _evidence_event(record: Any, through_seq: int | None) -> FlightEvent:
    return FlightEvent(
        seq=record.audit_id,
        timestamp=record.timestamp,
        kind=record.kind,
        origin=EventOrigin.EVIDENCE,
        producer_kind=str(record.producer.kind),
        producer_name=record.producer.name,
        producer_version=record.producer.version,
        attested_by_host=record.attested_by_host,
        sealed=through_seq is not None and record.audit_id <= through_seq,
        facts=dict(record.facts),
    )


def _attestation_event(
    attestation: ReceiptAttestation, through_seq: int | None
) -> FlightEvent:
    """An attestation is a host-chained fact ABOUT a receipt.

    It carries no producer block of its own: the only writer of the row is
    ``attest_artifact_receipt`` on the host's own store, so the provenance is
    the host by construction rather than by a claim inside the payload.
    """
    return FlightEvent(
        seq=attestation.seq,
        timestamp=attestation.timestamp,
        kind=attestation.kind or EventOrigin.ATTESTATION.value,
        origin=EventOrigin.ATTESTATION,
        producer_kind=str(ProducerKind.HOST),
        producer_name="kew.artifact_receipt",
        producer_version="",
        attested_by_host=True,
        sealed=through_seq is not None and attestation.seq <= through_seq,
        terminal=attestation.terminal,
        facts=dict(attestation.payload),
    )


def telemetry_mode(events: Sequence[FlightEvent]) -> TelemetryMode:
    """Structured, activity-only, or missing — for these events only."""
    kinds = {event.kind for event in events}
    if EvidenceKind.RUNNER_USAGE.value in kinds:
        return TelemetryMode.STRUCTURED
    if EvidenceKind.RUNNER_PROGRESS.value in kinds:
        return TelemetryMode.ACTIVITY_ONLY
    return TelemetryMode.MISSING


def _telemetry_availability(
    dimension: str, kind: EvidenceKind, events: Sequence[FlightEvent]
) -> EvidenceAvailability:
    present = any(event.kind == kind.value for event in events)
    return EvidenceAvailability(
        dimension=dimension,
        availability=AVAILABLE if present else UNAVAILABLE,
        provenance=OBSERVED if present else UNOBSERVED,
        detail="" if present else f"no {kind.value} row was recorded for this run",
    )


def _sequence_availability(replay: ReplayResult) -> EvidenceAvailability:
    if not replay.chain.ok:
        return EvidenceAvailability(
            dimension=DIM_HOST_SEQUENCE,
            availability=UNAVAILABLE,
            provenance=ERROR,
            detail=f"the audit chain breaks at row {replay.chain.broken_at_id}",
        )
    if replay.gaps:
        return EvidenceAvailability(
            dimension=DIM_HOST_SEQUENCE,
            availability=UNAVAILABLE,
            provenance=ERROR,
            detail=f"{len(replay.gaps)} ledger row(s) could not be replayed",
        )
    return EvidenceAvailability(
        dimension=DIM_HOST_SEQUENCE, availability=AVAILABLE, provenance=OBSERVED
    )


def _seal_availability(
    receipt: ArtifactReceipt | None, detail: str
) -> EvidenceAvailability:
    if receipt is None:
        return EvidenceAvailability(
            dimension=DIM_ARTIFACT_SEAL,
            availability=UNAVAILABLE,
            provenance=UNOBSERVED,
            detail=detail or "no artifact receipt is sealed for this attempt",
        )
    if detail:
        return EvidenceAvailability(
            dimension=DIM_ARTIFACT_SEAL,
            availability=UNAVAILABLE,
            provenance=ERROR,
            detail=detail,
        )
    return EvidenceAvailability(
        dimension=DIM_ARTIFACT_SEAL, availability=AVAILABLE, provenance=OBSERVED
    )


def _diff_availability(
    receipt: ArtifactReceipt | None, sealed: bool, detail: str
) -> EvidenceAvailability:
    if receipt is None or not sealed:
        return EvidenceAvailability(
            dimension=DIM_ARTIFACT_DIFF,
            availability=UNAVAILABLE,
            provenance=UNOBSERVED,
            detail=detail or "there is no sealed artifact to diff",
        )
    if detail:
        return EvidenceAvailability(
            dimension=DIM_ARTIFACT_DIFF,
            availability=UNAVAILABLE,
            provenance=ERROR,
            detail=detail,
        )
    return EvidenceAvailability(
        dimension=DIM_ARTIFACT_DIFF, availability=AVAILABLE, provenance=OBSERVED
    )


def line_counts_availability() -> EvidenceAvailability:
    """Line counts are structurally unknowable from a package alone."""
    return EvidenceAvailability(
        dimension=DIM_DIFF_LINE_COUNTS,
        availability=UNAVAILABLE,
        provenance=UNOBSERVED,
        detail=_LINE_COUNTS_DETAIL,
    )


def _first(values: Sequence[Any]) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


@dataclass(frozen=True)
class _Seal:
    """The receipt-derived half of a timeline, absent when nothing is sealed."""

    through_seq: int | None = None
    chain_head: str | None = None
    node_id: str | None = None
    source_tracker: str | None = None
    source_id: str | None = None


def _seal_of(receipt: ArtifactReceipt | None) -> _Seal:
    if receipt is None:
        return _Seal()
    return _Seal(
        through_seq=receipt.through_seq,
        chain_head=receipt.chain_head,
        node_id=artifact_node_id(receipt),
        source_tracker=receipt.source_kind,
        source_id=receipt.source_id,
    )


def _ordered_events(
    replay: ReplayResult,
    attestations: Sequence[ReceiptAttestation],
    through_seq: int | None,
) -> list[FlightEvent]:
    """One list, in ledger order, of both durable surfaces' events."""
    events = [_evidence_event(record, through_seq) for record in replay.records]
    events.extend(
        _attestation_event(attestation, through_seq) for attestation in attestations
    )
    events.sort(key=lambda event: event.seq)
    return events


def build_flight_timeline(
    *,
    run_id: str,
    attempt_number: int,
    replay: ReplayResult,
    receipt: ArtifactReceipt | None,
    attestations: Sequence[ReceiptAttestation] = (),
    seal_detail: str = "",
    diff_available: bool = False,
    diff_detail: str = "",
) -> FlightTimeline:
    """Project one run's ledger prefix and seal into the canonical timeline.

    ``seal_detail`` and ``diff_detail`` are how the caller reports a read it
    attempted and could not complete — a receipt that stopped verifying, a
    package root that is not there. They become ``error``/``unobserved``
    dimensions rather than a silently shorter list of events.
    """
    seal = _seal_of(receipt)
    events = _ordered_events(replay, attestations, seal.through_seq)
    return FlightTimeline(
        run_id=run_id,
        attempt_number=attempt_number,
        item_id=_first([record.item_id for record in replay.records]),
        issue_num=_first([record.issue_num for record in replay.records]),
        source_tracker=seal.source_tracker,
        source_id=seal.source_id,
        sealed=receipt is not None and not seal_detail,
        through_seq=seal.through_seq,
        chain_head=seal.chain_head,
        chain_verified=replay.chain.ok,
        artifact_node_id=seal.node_id,
        runner_telemetry=telemetry_mode(events),
        events=tuple(events),
        gaps=tuple(
            FlightGap(seq=gap.audit_id, event=gap.event, reason=gap.reason)
            for gap in replay.gaps
        ),
        availability=(
            _sequence_availability(replay),
            _seal_availability(receipt, seal_detail),
            _diff_availability(receipt, diff_available, diff_detail),
            _telemetry_availability(
                DIM_RUNNER_PROGRESS, EvidenceKind.RUNNER_PROGRESS, events
            ),
            _telemetry_availability(
                DIM_RUNNER_USAGE, EvidenceKind.RUNNER_USAGE, events
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


class ArtifactUnbound(RuntimeError):
    """The package offered is not the package the receipt sealed."""


def _decode(payload: bytes, budget: int) -> tuple[str | None, str, str]:
    """``(text, availability, detail)`` for one entry's bytes."""
    if len(payload) > MAX_FILE_TEXT_BYTES:
        return None, UNAVAILABLE, _TEXT_TOO_LARGE
    if len(payload) > budget:
        return None, UNAVAILABLE, _TEXT_BUDGET_SPENT
    try:
        return payload.decode("utf-8"), AVAILABLE, ""
    except UnicodeDecodeError:
        return None, UNAVAILABLE, _TEXT_NOT_UTF8


def _changed_files(artifact: Artifact) -> list[ArtifactFile]:
    files: list[ArtifactFile] = []
    budget = MAX_TOTAL_TEXT_BYTES
    for entry in sorted(artifact.entries, key=lambda e: e.path):
        payload = artifact.content(entry)
        text, availability, detail = _decode(payload, budget)
        if text is not None:
            budget -= len(payload)
        files.append(
            ArtifactFile(
                path=entry.path,
                change=ChangeKind.CHANGED,
                mode=entry.mode,
                digest=entry.sha256,
                byte_length=len(payload),
                text=text,
                text_availability=availability,
                detail=detail,
            )
        )
    return files


def _deleted_files(artifact: Artifact) -> list[ArtifactFile]:
    return [
        ArtifactFile(
            path=path,
            change=ChangeKind.DELETED,
            text=None,
            text_availability=UNAVAILABLE,
            detail="the package records the removal, not the removed bytes",
        )
        for path in sorted(artifact.deletions)
    ]


def _render(receipt: ArtifactReceipt, files: Sequence[ArtifactFile]) -> str:
    """A deterministic, honestly-labelled rendering of the change set.

    Not a unified diff: a unified diff shows what the base held, and this route
    does not read a base. Presenting the package's final content between ``---``
    and ``+++`` markers would look like a comparison that never happened, so the
    text says what it is — one line per path, bound to the lineage above it.
    """
    header = [
        f"artifact {receipt.package_digest}",
        f"base {receipt.base_sha}",
        f"tree {receipt.tree_sha}",
        f"verification {receipt.verification}",
        f"sealed through audit sequence {receipt.through_seq}",
        f"# {_LINE_COUNTS_DETAIL}",
    ]
    body = [
        f"{entry.change} {entry.mode or '-'} {entry.digest or '-'}"
        f" {entry.byte_length} {entry.path}"
        for entry in files
    ]
    return "\n".join([*header, *body]) + "\n"


def build_artifact_diff(
    *, receipt: ArtifactReceipt, artifact: Artifact
) -> ArtifactDiff:
    """The sealed package's change set, refused unless it is the sealed one.

    The caller loads the package by ``receipt.package_digest``, which already
    re-hashes every blob and re-derives the name. The base check here closes
    the remaining gap: a package whose manifest names a different base than the
    receipt sealed is not this lineage, whatever its digest says.
    """
    if artifact.base_sha != receipt.base_sha:
        raise ArtifactUnbound(
            f"the package's base {artifact.base_sha!r} is not the sealed base"
            f" {receipt.base_sha!r}"
        )
    files = tuple(_changed_files(artifact) + _deleted_files(artifact))
    return ArtifactDiff(
        run_id=receipt.run_id,
        attempt_number=receipt.attempt_number,
        node_id=artifact_node_id(receipt),
        base_sha=receipt.base_sha,
        tree_sha=receipt.tree_sha,
        package_digest=receipt.package_digest,
        receipt_digest=receipt.receipt_digest,
        verification=receipt.verification,
        files=files,
        text=_render(receipt, files),
        availability=(
            EvidenceAvailability(
                dimension=DIM_ARTIFACT_DIFF,
                availability=AVAILABLE,
                provenance=OBSERVED,
            ),
            line_counts_availability(),
        ),
    )
