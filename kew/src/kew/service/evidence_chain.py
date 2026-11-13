"""Item evidence chain (FUI-12) — the guarantees this item's route promises.

The DWork evidence ledger and the DEpic Evidence column read the same ordered
checklist: four guarantees, in the order the work passes through them.

* **Sandboxed** (``sandbox_resolved``) — the containment posture the host
  resolved for the dispatch;
* **Canonical publication** (``publication_posture``) — how the artifact was
  pushed;
* **Scan clean** (``publication_output_scan``) — the host's credential scan of
  the pushed objects;
* **Verdict** (``adversarial_review_verdict``) — the governed review's answer.

Three rules this module holds to.

**It reads; it never attests.** No row here is written, and nothing in this
module is a new attested fact: the four events are already on the hash chain,
written by the host code that performed each step. This is a projection over
them (the audit-chain skill's forward rule is therefore trivially satisfied —
there is no new payload key, column or preimage slot).

**A green check means attested, not good.** ``state`` answers "is this fact on
the host's chain, recorded by the host, and does the chain verify?" — not "was
the answer the one you wanted". A ``refused`` containment posture is
``attested`` with ``refused`` in its summary, because that IS what the host
attested. Reading the content is the swimlane's job (FUI-11), and collapsing
the two questions is how a green check ends up meaning two different things.

**Never a green check over an unverified chain.** ``attested`` requires BOTH
that the row is the host's own attestation (an ``agent`` actor records a
self-report, not a host fact) AND that the chain verifies by the same verdict
``kew audit verify`` and ``/api/audit/status`` use. Anything else recorded is
``unverified``. A fact that is not recorded at all is ``pending`` when its
stage has not passed and ``missing`` when it has — "we looked and it has not
happened yet" is a different world from "the stage is over and the promised
evidence is not there".

The four-state vocabulary has no "does not apply", so a stage that will never
record its fact (an item with no artifact, say) reads ``pending`` with the
stage's own word in the summary. It never reads ``missing``, which would
accuse the host of dropping a promise it never made.

**A superseded fact is not this route's evidence.** ``kew epic retry`` grants a
fresh review allowance, and the rounds the swimlane counts reset with it, so
the Verdict row is scoped to the current review cycle: a prior cycle's attested
verdict reads ``pending`` here, in the review stage's own words. Reading it as
this route's answer is how the ledger ends up saying "4 / 4 attested · ready"
beside a pipeline that says review has not started. The other three guarantees
are NOT cycle-scoped, because the marker's own scope is the review allowance
(and the failure supersession ``overlay_status`` derives from it): the posture,
the publication and the scan of the artifact that is still on the branch are
not undone by a retry.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.publication_scan import OUTPUT_SCAN_EVENT
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.service.audit import audit_status
from kew.service.models import EvidenceChainRowDTO, ItemEvidenceDTO
from kew.service.pipeline import item_pipeline

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from kew.config import DispatchConfig
    from kew.service.models import ItemPipelineDTO, PipelineStageDTO

_logger = logging.getLogger(__name__)

#: The containment posture event (``kew.sandbox.emit_resolved_posture``).
SANDBOX_EVENT = "sandbox_resolved"
#: The publication posture event (``kew.publication.resolve_publication_mode``).
PUBLICATION_POSTURE_EVENT = "publication_posture"

#: The row states, in the order they degrade.
ATTESTED = "attested"
UNVERIFIED = "unverified"
PENDING = "pending"
MISSING = "missing"

#: Named entries in :attr:`ItemEvidenceDTO.unavailable`.
UNREADABLE_AUDIT = "evidence:audit"
UNREADABLE_CHAIN = "evidence:chain"
UNREADABLE_CYCLE = "evidence:review_cycle"

#: Sentences for :attr:`ItemEvidenceDTO.reason` — why the rows read as they do.
NO_RUN_REASON = "no run has been dispatched for this item"
TAMPERED_REASON = "the audit chain did not verify"
UNREADABLE_CHAIN_REASON = "the audit chain could not be verified"
UNATTRIBUTED_REASON = "a recorded fact is not the host's own attestation"
UNREADABLE_FACT_REASON = "a recorded fact could not be read"

#: The unavailable names that mean a FACT could not be read, as opposed to
#: a fact that was read and cannot be vouched for — see :func:`_reason`.
_UNREADABLE_FACTS = frozenset({UNREADABLE_AUDIT, UNREADABLE_CYCLE})

#: Actors whose rows are the host's own attestation. ``agent`` is excluded on
#: purpose: an agent-recorded row is a self-report about its own work, and an
#: unattributed row (no actor at all) vouches for nobody.
_HOST_ACTOR_TYPES = frozenset({"policy", "human"})

#: Pipeline stage states that mean the stage is over, so a fact it should have
#: recorded is ``missing`` rather than still ahead of us. ``unknown`` is absent
#: deliberately: a stage nobody could read has not been shown to have passed.
#: ``not_ready`` is a review round that ended against the change.
_PASSED = frozenset({"done", "needs_you", "failed", "not_ready"})

#: Newest rows examined per event. A guarantee's row is the newest one that
#: actually carries its fact, because ``sandbox_resolved`` also carries the
#: credential issuer's lifecycle rows for the same run.
_ROW_WINDOW = 32


def _sandbox_summary(payload: Mapping[str, Any]) -> str:
    """The resolved containment posture: ``sandboxed``, ``refused``, …"""
    return str(payload.get("posture") or "")


def _publication_summary(payload: Mapping[str, Any]) -> str:
    """``canonical-publication`` -> ``canonical`` (the artboard's word)."""
    return str(payload.get("posture") or "").removesuffix("-publication")


def _scan_summary(payload: Mapping[str, Any]) -> str:
    """The verdict and the head it covers: ``clean · 3670e43f``."""
    head = str(payload.get("head_sha") or "")[:8]
    return _joined(str(payload.get("verdict") or ""), head)


def _verdict_summary(payload: Mapping[str, Any]) -> str:
    """``NEEDS_CHANGES`` -> ``needs changes``."""
    return str(payload.get("verdict") or "").lower().replace("_", " ")


@dataclass(frozen=True)
class _Guarantee:
    """One row of the checklist: what it promises and where to read it."""

    label: str
    event: str
    #: The pipeline stage whose passing makes an unrecorded fact ``missing``.
    stage: str
    #: The payload key that makes a row of this event THIS guarantee. A row
    #: without it is a different fact under the same event name (the issuer
    #: lifecycle rows under ``sandbox_resolved``) or one nobody could parse.
    fact: str
    summarize: Callable[[Mapping[str, Any]], str]
    #: Is this guarantee's row superseded by a fresh review cycle? Only the
    #: verdict is: see the module docstring for why the other three are not.
    cycle_scoped: bool = False


#: The checklist, in the order both artboards number it.
GUARANTEES: tuple[_Guarantee, ...] = (
    _Guarantee("Sandboxed", SANDBOX_EVENT, "implement", "posture", _sandbox_summary),
    _Guarantee(
        "Canonical publication",
        PUBLICATION_POSTURE_EVENT,
        "publish",
        "posture",
        _publication_summary,
    ),
    _Guarantee("Scan clean", OUTPUT_SCAN_EVENT, "publish", "verdict", _scan_summary),
    _Guarantee(
        "Verdict",
        REVIEW_VERDICT_EVENT,
        "review",
        "verdict",
        _verdict_summary,
        cycle_scoped=True,
    ),
)

#: The denominator of the ``attested_count / total`` summary.
TOTAL = len(GUARANTEES)


def _joined(*parts: str) -> str:
    """The ledger's second line: the non-empty parts, dot-separated."""
    return " · ".join(part for part in parts if part)


def _reader(store: Any, name: str) -> Any:
    """A store method if this store has one, else None.

    The degradation rule ``kew.service.work`` and ``kew.service.pipeline`` use:
    a store predating a table (or a double that never had it) must read as
    "could not tell", which this module then NAMES.
    """
    reader = getattr(store, name, None)
    return reader if callable(reader) else None


def _payload(row: Any) -> Mapping[str, Any]:
    """An audit row's payload, or ``{}`` when it has none.

    ``list_audit_events`` coerces an unparseable payload to ``None``: a
    recorded row nobody can read, never an empty one.
    """
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _attested_by_host(row: Any) -> bool:
    """Is this row the host's own attestation?

    A structured evidence row already answers this inside its payload, where a
    runner-produced fact relayed onto the chain carries ``False``; that answer
    wins. Every other row is judged by its actor: the host writes as ``policy``
    or as the operating human, never as ``agent``.

    (The name of the module that builds those payloads is deliberately not
    written here: ``tests/test_evidence.py`` scans this package's source text
    for it, because the work projection may not import it.)
    """
    claimed = _payload(row).get("attested_by_host")
    if isinstance(claimed, bool):
        return claimed
    return str(getattr(row, "actor_type", "") or "") in _HOST_ACTOR_TYPES


def _instant(value: str) -> datetime | None:
    """An ISO-8601 instant, or ``None`` when it is not one this can order.

    A stamp with no offset is not orderable against the boundary, and reading
    one as UTC would be a guess — the guess that ends in a green check.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _logger.warning("evidence timestamp %r is not ISO-8601", value)
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class _Cycle:
    """Where this item's current review cycle starts — the store's own bound.

    ``start is None`` with :attr:`readable` means the node has never been
    retried, so its whole recorded history is the current cycle.
    """

    start: datetime | None = None
    #: Could the boundary be read at all? ``False`` means no row of a
    #: cycle-scoped event can be placed, so none of them can be vouched for.
    readable: bool = True

    def holds(self, row: Any) -> bool:
        """Was *row* recorded in the current cycle?

        A row this projection cannot date is excluded: putting an undatable
        row inside the current cycle is the error that ends in a green check.
        """
        if self.start is None:
            return True
        ts = _instant(str(getattr(row, "timestamp", "") or ""))
        return ts is not None and ts >= self.start


def _cycle(store: Any, item_id: str, unavailable: list[str]) -> _Cycle:
    """The current review cycle's lower bound, read from the store's marker.

    ``review_cycle_start`` is the same ``epic_retries`` marker
    ``review_rounds_in_cycle`` scopes to, so the ledger and the swimlane cannot
    disagree about which cycle we are in. A store with no such reader has no
    marker to read either — there is nothing for a row to be superseded by —
    while a marker that answered and could not be READ is named, never assumed
    away.
    """
    reader = _reader(store, "review_cycle_start")
    if reader is None:
        return _Cycle()
    try:
        marker = reader(item_id)
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean cycle
        _logger.warning("review cycle unreadable for item %s", item_id, exc_info=True)
        unavailable.append(UNREADABLE_CYCLE)
        return _Cycle(readable=False)
    if marker is None:
        return _Cycle()
    start = _instant(str(marker))
    if start is None:
        _logger.warning("review cycle marker unreadable for item %s", item_id)
        unavailable.append(UNREADABLE_CYCLE)
        return _Cycle(readable=False)
    return _Cycle(start=start)


@dataclass(frozen=True)
class _Fact:
    """What the ledger found for one guarantee.

    ``row`` is the newest row carrying the guarantee's fact. ``recorded`` says
    a row of that event exists on the item even when none could be read as this
    guarantee — the difference between "nothing happened" and "something is
    there and we cannot vouch for it".
    """

    row: Any | None = None
    recorded: bool = False


def _run_ids(runs: Sequence[Any]) -> tuple[str, ...]:
    """Every run id on this item, newest first — the pre-item_id join keys.

    WHICH run holds a given fact differs per event: the host records the
    publication scan under the run that pushed, the driver records the review
    verdict under the reviewer's own run. A legacy row is keyed on ``run_id``
    alone, so trying only the newest row's id would report a recorded
    guarantee as ``missing`` — an accusation, from a join that was too narrow.
    """
    return tuple(
        dict.fromkeys(
            run_id for rec in runs if (run_id := str(getattr(rec, "run_id", "") or ""))
        )
    )


def _item_runs(store: Any, item_id: str) -> tuple[Any, ...]:
    """This item's runs, newest attempt first (``kew.service.work``'s order)."""
    reader = _reader(store, "list_by_item")
    if reader is None:
        return ()
    return tuple(
        sorted(
            reader(item_id),
            key=lambda rec: (
                str(getattr(rec, "started_at", "") or ""),
                int(getattr(rec, "attempt_number", 1) or 1),
            ),
            reverse=True,
        )
    )


def _event_rows(
    store: Any,
    event: str,
    *,
    item_id: str,
    run_ids: Sequence[str],
    unavailable: list[str],
) -> list[Any] | None:
    """The newest rows of *event* for this item, or ``None`` when unreadable.

    Joined by item id, then by the item's runs — the same walk
    ``kew.service.pipeline`` uses, and for the same reason: a pre-item_id row
    carries its key on ``run_id`` alone.
    """
    reader = _reader(store, "list_audit_events")
    if reader is None:
        unavailable.append(UNREADABLE_AUDIT)
        return None
    try:
        rows = list(reader(limit=_ROW_WINDOW, event=event, item_id=item_id))
        for run_id in run_ids:
            if rows:
                break
            rows = list(reader(limit=_ROW_WINDOW, event=event, run_id=run_id))
    except Exception:  # noqa: BLE001 — named as unavailable, never a clean []
        _logger.warning(
            "audit event %s unreadable for item %s", event, item_id, exc_info=True
        )
        unavailable.append(UNREADABLE_AUDIT)
        return None
    return rows


def _in_cycle(
    rows: list[Any], guarantee: _Guarantee, cycle: _Cycle
) -> list[Any] | None:
    """*rows* narrowed to the current review cycle, or ``None`` when unknown.

    A guarantee that is not cycle-scoped keeps every row. ``None`` says the
    boundary could not be read, so WHICH cycle these rows belong to is not a
    question this projection can answer — the caller then vouches for none.
    """
    if not guarantee.cycle_scoped:
        return rows
    if not cycle.readable:
        return None
    return [row for row in rows if cycle.holds(row)]


def _fact(
    store: Any,
    guarantee: _Guarantee,
    *,
    item_id: str,
    run_ids: Sequence[str],
    cycle: _Cycle,
    unavailable: list[str],
) -> _Fact:
    """The newest row that carries this guarantee's fact, and what was there.

    Only rows of the current review cycle are candidates for a cycle-scoped
    guarantee: a superseded verdict is not this route's evidence, and it is not
    unreadable either — the row reads from its stage, like any fact that has
    not been recorded yet.
    """
    rows = _event_rows(
        store,
        guarantee.event,
        item_id=item_id,
        run_ids=run_ids,
        unavailable=unavailable,
    )
    if rows is None:
        # The table did not answer. Not "nothing was recorded": treated as a
        # recorded-but-unreadable fact so the row can never read as clean.
        return _Fact(recorded=True)
    scoped = _in_cycle(rows, guarantee, cycle)
    if scoped is None:
        # The cycle did not answer: a recorded row can be neither vouched for
        # nor called absent, so it reads as the unreadable fact it now is.
        return _Fact(recorded=bool(rows))
    for row in scoped:
        if guarantee.fact in _payload(row):
            return _Fact(row=row)
    if scoped:
        _logger.warning(
            "no %s row for item %s carries %r", guarantee.event, item_id, guarantee.fact
        )
        unavailable.append(UNREADABLE_AUDIT)
    return _Fact(recorded=bool(scoped))


def _chain_verdict(store: Any, repo_dir: Path | None, unavailable: list[str]) -> str:
    """``""`` when the chain verifies, else the reason it does not.

    The verdict is ``kew.service.audit.audit_status`` — exactly what
    ``/api/audit/status`` shows and what ``kew audit verify`` decides, anchors
    included. Two surfaces cannot disagree about whether the ledger is intact,
    and this projection owns no second hash implementation. Its TTL cache is
    inherited with it: ``attested`` means "on a chain that verified at most
    ``_STATUS_CACHE_TTL_S`` ago", not instantaneously.

    Called OUTSIDE the read snapshot, and after it, for two reasons.
    ``read_chain_head_key`` opens its own ``BEGIN`` under the store's
    non-reentrant transaction lock — the same lock ``read_snapshot`` holds — so
    calling it inside the block deadlocks. And verifying *after* the rows are
    pinned is the safe order: every row the snapshot saw existed before the
    verification, so a break involving one of them cannot land after the
    verdict and still read as ``attested``.
    """
    try:
        status = audit_status(store, repo_dir=repo_dir)
    except Exception:  # noqa: BLE001 — unverifiable is never "verified"
        _logger.warning("the audit chain could not be verified", exc_info=True)
        unavailable.append(UNREADABLE_CHAIN)
        return UNREADABLE_CHAIN_REASON
    return "" if status.verified else TAMPERED_REASON


def _stages(pipeline: ItemPipelineDTO) -> dict[str, PipelineStageDTO]:
    """The item's stages by name — how far each one got (FUI-11)."""
    return {stage.stage: stage for lane in pipeline.lanes for stage in lane.stages}


def _stage_summary(stage: PipelineStageDTO | None) -> str:
    """An unrecorded guarantee is worded from its stage, in the swimlane's own
    words, so the ledger and the swimlane cannot tell different stories."""
    return "" if stage is None else _joined(stage.label, stage.detail)


def _row(
    n: int,
    guarantee: _Guarantee,
    fact: _Fact,
    stages: Mapping[str, PipelineStageDTO],
    *,
    verified: bool,
) -> EvidenceChainRowDTO:
    """One checklist row: what was promised, what is there, and how good it is."""
    if fact.row is not None:
        host = _attested_by_host(fact.row)
        return _checklist_row(
            n,
            guarantee,
            summary=guarantee.summarize(_payload(fact.row)),
            ts=str(getattr(fact.row, "timestamp", "") or ""),
            state=ATTESTED if verified and host else UNVERIFIED,
        )
    if fact.recorded:
        # Something IS there and nobody could read it. Never a clean absence.
        return _checklist_row(n, guarantee, state=UNVERIFIED)
    stage = stages.get(guarantee.stage)
    return _checklist_row(
        n,
        guarantee,
        summary=_stage_summary(stage),
        state=MISSING if _passed(stage) else PENDING,
    )


def _passed(stage: PipelineStageDTO | None) -> bool:
    """Whether a stage is over, so a fact it should have recorded is missing.

    A published artifact nobody scanned is drawn ``pending`` (it is not a
    success), but it IS published (``passed``): its scan is missing, not still
    ahead.
    """
    return stage is not None and (stage.state in _PASSED or stage.passed)


def _checklist_row(
    n: int,
    guarantee: _Guarantee,
    *,
    state: str,
    summary: str = "",
    ts: str = "",
) -> EvidenceChainRowDTO:
    """One row, with its position and promise filled in from the table."""
    return EvidenceChainRowDTO(
        n=n,
        label=guarantee.label,
        event=guarantee.event,
        summary=summary,
        ts=ts,
        state=state,
    )


def _pending_rows() -> list[EvidenceChainRowDTO]:
    """The checklist for an item nothing has been dispatched for yet."""
    return [
        _checklist_row(n, guarantee, state=PENDING)
        for n, guarantee in enumerate(GUARANTEES, 1)
    ]


def _reason(
    rows: Sequence[EvidenceChainRowDTO],
    chain_reason: str,
    unavailable: Sequence[str],
) -> str:
    """Why the checklist reads as it does — the chain first, then the rows.

    An ``unverified`` row has two causes and they are not interchangeable: the
    row was read and is not the host's own fact, or it could not be read at all.
    Reporting the first for the second would tell the reader to distrust a
    producer when the truth is that nobody could look.
    """
    if chain_reason:
        return chain_reason
    if any(name in _UNREADABLE_FACTS for name in unavailable):
        return UNREADABLE_FACT_REASON
    if any(row.state == UNVERIFIED for row in rows):
        return UNATTRIBUTED_REASON
    return ""


def _dto(
    item_id: str,
    rows: list[EvidenceChainRowDTO],
    *,
    reason: str,
    verified: bool,
    unavailable: Sequence[str],
    composed_at: datetime,
) -> ItemEvidenceDTO:
    return ItemEvidenceDTO(
        item_id=item_id,
        rows=rows,
        attested_count=sum(1 for row in rows if row.state == ATTESTED),
        total=TOTAL,
        chain_verified=verified,
        reason=reason,
        unavailable=list(dict.fromkeys(unavailable)),
        composed_at=composed_at.isoformat(),
    )


def item_evidence(
    store: Any,
    item_id: str,
    *,
    config: DispatchConfig | None = None,
    repo_dir: Path | None = None,
    now: datetime | None = None,
) -> ItemEvidenceDTO:
    """One item's evidence chain — always four rows, never a 404.

    An item with no runs still has the four guarantees its route promises; they
    are simply all ``pending``, with :attr:`ItemEvidenceDTO.reason` saying why.
    Answering 404 there would make "we have not started" indistinguishable from
    "this id does not exist", and the Epics rail selects nodes that have not
    been dispatched yet.

    ``repo_dir`` is where the anchor file lives, so the chain verdict here is
    the one ``kew audit verify`` reaches. The stages and the rows are read in
    ONE snapshot, so they cannot describe two different moments; the chain
    verdict is taken just after it closes (see :func:`_chain_verdict`).
    """
    moment = now or datetime.now(UTC)
    unavailable: list[str] = []
    with store.read_snapshot():
        pipeline = item_pipeline(store, item_id, config=config, now=moment)
        facts: list[_Fact] = []
        if pipeline is not None:
            unavailable.extend(pipeline.unavailable)
            run_ids = _run_ids(_item_runs(store, item_id))
            cycle = _cycle(store, item_id, unavailable)
            facts = [
                _fact(
                    store,
                    guarantee,
                    item_id=item_id,
                    run_ids=run_ids,
                    cycle=cycle,
                    unavailable=unavailable,
                )
                for guarantee in GUARANTEES
            ]
    chain_reason = _chain_verdict(store, repo_dir, unavailable)
    if pipeline is None:
        return _dto(
            item_id,
            _pending_rows(),
            reason=NO_RUN_REASON,
            verified=not chain_reason,
            unavailable=unavailable,
            composed_at=moment,
        )
    stages = _stages(pipeline)
    rows = [
        _row(n, guarantee, fact, stages, verified=not chain_reason)
        for n, (guarantee, fact) in enumerate(zip(GUARANTEES, facts, strict=True), 1)
    ]
    return _dto(
        item_id,
        rows,
        reason=_reason(rows, chain_reason, unavailable),
        verified=not chain_reason,
        unavailable=unavailable,
        composed_at=moment,
    )
