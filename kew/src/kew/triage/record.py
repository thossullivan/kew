"""Record an item's extracted plan for the dispatch that launches it (#2290).

One host-side seam, called where kew already holds the issue body it fetched
for the dispatch prompt: extract the criteria, write them to the ledger keyed on
the body's digest, and attest the write on the audit chain. No model, no extra
network call.

Two things this deliberately does NOT do:

- It never decides anything. A recorded plan gates no dispatch and applies no
  label; it is a record, and acceptance of a plan stays a human act
  (``approved_by``).
- It never swallows a failure silently. A ledger or chain write that fails
  leaves a warning naming the item and the reason, because "no plan on record"
  and "we could not record the plan" are different states and a reader has to
  be able to tell them apart.

The attestation is therefore re-attempted, not fire-and-forget: ``audit.emit``
is fail-soft by contract, and the row is already committed by the time it runs,
so a dropped append would otherwise leave that row off the chain forever — the
next tick reads the same body, writes nothing, and would attest nothing either.
Every event carries a dedup key naming its ``item_plans`` row, so each later
observation retries the append until it lands and can never double-record it
(the ``emit_continuation_evidence`` contract).

The retry covers the item's WHOLE recorded history, not just the row the
current call wrote and the one it displaced. A row whose append failed and was
then superseded by a later body is never the subject of another write, so
anything narrower would leave exactly that row — and the supersession it
caused — off the chain permanently, which is not what the contract above says.

Each event binds the item and the ledger row it describes through its
``payload`` (``item_id``, ``item_plan_id``), the way every additive fact binds:
only ``issue_num`` enters the ``entry_hash`` preimage, and a tracker riding the
``issue_num = 0`` sentinel would otherwise leave the item-to-plan binding
outside the chain, where re-pointing a row at another item is not detectable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kew import audit
from kew.triage.plan import body_digest, canonical_plan_json, extract_plan, plan_sha

if TYPE_CHECKING:
    from kew.run_store import ItemPlanRow, ItemPlanWrite, RunStore

_logger = logging.getLogger(__name__)

PLAN_RECORDED_EVENT = "plan_recorded"
"""A new plan row became the item's current plan."""

PLAN_SUPERSEDED_EVENT = "plan_superseded"
"""An edited body displaced the plan a previous body produced."""

_ACTOR = audit.Actor("policy", "triage-plan")


def record_extracted_plan(
    store: RunStore | None,
    *,
    item_id: str,
    body: str | None,
    run_id: str | None = None,
    issue_num: int | None = None,
    repo: str | None = None,
) -> ItemPlanWrite | None:
    """Record the criteria *body* states for *item_id*, and attest the write.

    Returns the ledger's :class:`~kew.run_store.ItemPlanWrite` — including the
    unchanged-body case, where ``recorded`` is False and no NEW row was written
    — or None when there is no store to write to or the write failed.

    *run_id* is the run doing the recording. It reaches the ledger row, and the
    chain events take their run id from the ROW rather than from this argument,
    so a retried attestation describes the run that recorded the plan however
    many later runs observed the same body.
    """
    if store is None:
        return None
    plan = extract_plan(body)
    try:
        write = store.record_item_plan(
            item_id,
            body_digest=body_digest(body),
            source="extracted",
            plan_sha=plan_sha(plan),
            plan_json=canonical_plan_json(plan),
            criteria_count=len(plan.acceptance_criteria),
            run_id=run_id,
        )
    except Exception:
        # Never fail a dispatch over its plan record, but never pretend the
        # item simply has no criteria either: a failure must be loud.
        _logger.warning(
            "Could not record the acceptance criteria for %s; the ledger has no"
            " plan for this body",
            item_id,
            exc_info=True,
        )
        return None
    _attest(store, write, issue_num=issue_num, repo=repo)
    return write


def _attest(
    store: RunStore,
    write: ItemPlanWrite,
    *,
    issue_num: int | None,
    repo: str | None,
) -> None:
    """Append the chain events the item's plan history is still missing.

    Runs on EVERY observation of the body, not only the one that wrote the row:
    the dedup keys make a landed event a no-op and an absent one a retry, which
    is the only way a failed append can still reach the chain (the row itself
    never gets rewritten). It walks the whole history for the same reason —
    a row superseded before its own append landed gets no second write of its
    own, so this walk is its only remaining chance.

    Oldest row first, each supersession after the ``plan_recorded`` of the row
    it ends and before the ``plan_recorded`` of the row that displaced it, so
    the chain reads in the order the plans actually happened.
    """
    if not callable(getattr(store, "append_audit_event_once", None)):
        _attest_this_write(store, write, issue_num=issue_num, repo=repo)
        return
    history = _plan_history(store, write.current)
    displacers: list[ItemPlanRow | None] = [*history[1:], None]
    for row, displacer in zip(history, displacers, strict=True):
        _emit_recorded(store, row, issue_num=issue_num, repo=repo)
        if row.superseded_at is not None and displacer is not None:
            _emit_superseded(store, row, displacer, issue_num=issue_num, repo=repo)


def _attest_this_write(
    store: RunStore,
    write: ItemPlanWrite,
    *,
    issue_num: int | None,
    repo: str | None,
) -> None:
    """Attest just this write, for a store that cannot dedup an append.

    A store predating the atomic dedup append (a legacy fake) has no identity
    to recover against, so re-attesting against it would duplicate the event
    rather than fill a gap. An unchanged body wrote no row, so it attests
    nothing — as before the recovery walk existed.
    """
    if not write.recorded:
        return
    if write.superseded is not None:
        _emit_superseded(
            store, write.superseded, write.current, issue_num=issue_num, repo=repo
        )
    _emit_recorded(store, write.current, issue_num=issue_num, repo=repo)


def _plan_history(store: RunStore, current: ItemPlanRow) -> list[ItemPlanRow]:
    """The item's plan rows up to and including *current*, oldest first.

    Rows are only ever inserted as the item's current plan, so the row after a
    superseded one in id order is exactly the row that displaced it. Rows newer
    than *current* belong to a concurrent write's own attestation, not this
    one's. A history read that fails leaves *current* to be attested alone: the
    walk is a recovery, and losing it must not cost the attestation this call
    was made for.
    """
    try:
        history = store.list_item_plans(current.item_id)
    except Exception:
        _logger.warning(
            "Could not read %s's plan history to re-check its attestation",
            current.item_id,
            exc_info=True,
        )
        return [current]
    rows = [row for row in history if row.id <= current.id]
    return rows or [current]


def _emit_recorded(
    store: RunStore,
    row: ItemPlanRow,
    *,
    issue_num: int | None,
    repo: str | None,
) -> None:
    """Attest that *row* became its item's current plan."""
    audit.emit(
        store,
        PLAN_RECORDED_EVENT,
        actor=_ACTOR,
        run_id=row.run_id,
        issue_num=issue_num,
        item_id=row.item_id,
        repo=repo,
        payload={
            "item_id": row.item_id,
            "item_plan_id": row.id,
            "body_digest": row.body_digest,
            "plan_sha": row.plan_sha,
            "criteria_count": row.criteria_count,
            "source": row.source,
            "criteria_ids": [
                f"AC{index}" for index in range(1, row.criteria_count + 1)
            ],
        },
        dedup_key=f"{PLAN_RECORDED_EVENT}:item_plan:{row.id}",
    )


def _emit_superseded(
    store: RunStore,
    row: ItemPlanRow,
    displacer: ItemPlanRow,
    *,
    issue_num: int | None,
    repo: str | None,
) -> None:
    """Attest that *displacer*'s body displaced the plan *row* holds.

    The run id is the DISPLACER's: this event records the reading of the body
    that ended *row*, so a retried append still names the run that read it,
    not whichever later run noticed the event was missing.
    """
    audit.emit(
        store,
        PLAN_SUPERSEDED_EVENT,
        actor=_ACTOR,
        run_id=displacer.run_id,
        issue_num=issue_num,
        item_id=row.item_id,
        repo=repo,
        payload={
            "item_id": row.item_id,
            "item_plan_id": row.id,
            "body_digest": row.body_digest,
            "plan_sha": row.plan_sha,
            "criteria_count": row.criteria_count,
            "source": row.source,
            "superseded_by_body_digest": displacer.body_digest,
        },
        dedup_key=f"{PLAN_SUPERSEDED_EVENT}:item_plan:{row.id}",
    )
