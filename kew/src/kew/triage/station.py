"""The triage station: signals, a classifier, a policy, a ledger row (#2293).

This is the seam that joins the four halves of the station, and the ONLY place
a triage decision is written. In shadow mode it writes to the ledger and the
audit chain, and nowhere else:

* it reads the item's title, body and labels from its caller;
* :mod:`kew.triage.signals` adds the host-computed facts and the duplicate
  candidates;
* :mod:`kew.triage.classify` asks a classifier, fencing every untrusted string;
* :mod:`kew.triage.policy` turns the answer into an outcome;
* the row lands in ``triage_decisions`` and the fact lands on the chain.

**Nothing here writes to a tracker.** No label, no comment, no ``ready``, no
close — not even when the outcome is ``propose_dispatch`` and not even when the
body asks for it. That is the station's deny-by-default contract, and
``tests/test_triage_deny_by_default.py`` holds the whole path to it by patching
every writer in :mod:`kew.labels` and the comment writers and asserting zero
calls. The only thing that acts on a proposal is a person running
``kew triage decide`` (:mod:`kew.cli.triage`), which is also the only caller of
:func:`resolve`.

Fail closed, loudly: an unreachable classifier, invalid JSON after one repair
retry, and an unwired backend all become an ``unavailable`` row with a reason —
a needs-you row for a person, never a dispatch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kew import audit
from kew.triage.classify import classify
from kew.triage.policy import decide
from kew.triage.signals import collect_signals

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore, TriageDecisionRow, TriageDecisionWrite
    from kew.triage.classify import Classification
    from kew.triage.policy import Outcome
    from kew.triage.signals import IssueSignals

_logger = logging.getLogger(__name__)

TRIAGE_DECIDED_EVENT = "triage_decided"
"""A new triage proposal became the item's current one."""

TRIAGE_RESOLVED_EVENT = "triage_resolved"
"""A PERSON decided what happens to a proposal."""

_POLICY_ACTOR = audit.Actor("policy", "triage-station")


@dataclass(frozen=True)
class TriageResult:
    """One pass of the station over one item.

    ``row`` is the ledger row that is now current, or None when the row could
    not be written — which is reported, never swallowed, because "triage
    proposed nothing" and "we could not record what triage proposed" are
    different states.
    """

    outcome: Outcome
    signals: IssueSignals
    classification: Classification
    row: TriageDecisionRow | None = None
    recorded: bool = False


@dataclass(frozen=True)
class EpicMembership:
    """Whether an item is a node of some epic, and which one.

    ``is_node`` is None when the epics could not be read — a third state on
    purpose. "This item is not an epic node" and "we could not tell" are
    different answers, and a caller skipping items on the strength of the
    first must not act on the second as if it were (CONTRIBUTING.md, "No
    silent clean defaults").
    """

    is_node: bool | None
    epic_ref: str = ""


def epic_membership(store: RunStore | None, item_id: str) -> EpicMembership:
    """Whether *item_id* appears as a node in any journaled epic snapshot.

    ``[triage] skip_epic_nodes`` is what this answers: an epic already planned
    its nodes, so re-triaging one proposes nothing a person does not have. The
    read is the ledger's own epic journal — never a file an agent can write.
    """
    if store is None:
        return EpicMembership(is_node=None)
    try:
        for epic_ref in store.list_epic_refs():
            tick = store.latest_epic_tick(epic_ref)
            if tick is None:
                continue
            if item_id in _snapshot_node_ids(tick.snapshot):
                return EpicMembership(is_node=True, epic_ref=epic_ref)
    except Exception:
        _logger.warning(
            "Could not read the epic journal to tell whether %s is an epic node",
            item_id,
            exc_info=True,
        )
        return EpicMembership(is_node=None)
    return EpicMembership(is_node=False)


def _snapshot_node_ids(snapshot: dict[str, object] | None) -> set[str]:
    """The node ids one epic snapshot names, as strings."""
    nodes = (snapshot or {}).get("nodes") or []
    if not isinstance(nodes, list):
        return set()
    return {
        str(node.get("id"))
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    }


def triage_item(
    store: RunStore | None,
    *,
    item_id: str,
    title: str,
    body: str | None,
    config: DispatchConfig,
    issue_num: int | None = None,
    labels: tuple[str, ...] = (),
    run_id: str | None = None,
    source_tracker: str | None = None,
    repo: str | None = None,
    search: bool = True,
) -> TriageResult:
    """Classify *item_id* and record the proposal. Applies nothing to a tracker."""
    signals = collect_signals(
        item_id=item_id,
        title=title,
        body=body,
        config=config,
        issue_num=issue_num or 0,
        labels=labels,
        search=search,
    )
    classification = classify(
        item_id=item_id, title=title, body=body, signals=signals, config=config
    )
    outcome = decide(classification.decision, signals, config)
    write = _record(
        store,
        item_id=item_id,
        signals=signals,
        classification=classification,
        outcome=outcome,
        issue_num=issue_num,
        run_id=run_id,
        source_tracker=source_tracker,
    )
    if write is None:
        return TriageResult(
            outcome=outcome, signals=signals, classification=classification
        )
    if write.recorded:
        _emit_decided(store, write, repo=repo)
    return TriageResult(
        outcome=outcome,
        signals=signals,
        classification=classification,
        row=write.current,
        recorded=write.recorded,
    )


def _record(
    store: RunStore | None,
    *,
    item_id: str,
    signals: IssueSignals,
    classification: Classification,
    outcome: Outcome,
    issue_num: int | None,
    run_id: str | None,
    source_tracker: str | None,
) -> TriageDecisionWrite | None:
    """Write the proposal to the ledger. None (with a warning) when it failed."""
    if store is None:
        _logger.warning(
            "No ledger available — the triage proposal for %s (%s) was not"
            " recorded and no person will be shown it",
            item_id,
            outcome.kind,
        )
        return None
    decision = classification.decision
    try:
        return store.record_triage_decision(
            item_id,
            body_digest=signals.body_digest,
            backend=classification.backend,
            model=classification.model,
            prompt_sha=classification.prompt_sha,
            outcome=outcome.kind,
            decision_json=_decision_json(classification, outcome, signals),
            source_tracker=source_tracker,
            issue_num=issue_num,
            run_id=run_id,
            decision_type=None if decision is None else decision.type,
            priority=None if decision is None else decision.priority,
            complexity=None if decision is None else decision.complexity,
            confidence=None if decision is None else decision.confidence,
            actionable=None if decision is None else decision.actionable,
            needs_info=None if decision is None else decision.needs_info,
            duplicate_of=outcome.duplicate_of,
        )
    except Exception:
        # Never raise over a record, but never pretend the item was not
        # triaged either (CONTRIBUTING.md, "No silent clean defaults").
        _logger.warning(
            "Could not record the triage proposal for %s; the ledger has no"
            " decision for this body",
            item_id,
            exc_info=True,
        )
        return None


def _decision_json(
    classification: Classification, outcome: Outcome, signals: IssueSignals
) -> str:
    """The canonical stored answer: the decision, plus how it was obtained.

    ``unavailable_reason`` and ``signals_unavailable`` ride inside it rather
    than in columns because they are prose and a list — the columns hold the
    facts a query filters on, and an additive fact belongs in the JSON for the
    same reason an audit fact belongs in the payload.

    Sorted keys and tight separators, as in
    :func:`kew.triage.plan.canonical_plan_json`: the same decision always
    serializes the same way.
    """
    decision = classification.decision
    body: dict[str, object] = {
        "decision": None if decision is None else decision.model_dump(mode="json"),
        "outcome": outcome.kind,
        "reason": outcome.reason,
        "questions": list(outcome.questions),
        "repaired": classification.repaired,
        "schema_enforced": classification.schema_enforced,
        "signals_unavailable": list(signals.unavailable),
    }
    if not classification.available:
        body["unavailable_reason"] = classification.unavailable_reason
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def resolve(
    store: RunStore | None,
    row_id: int,
    *,
    resolution: str,
    resolved_by: str,
    repo: str | None = None,
) -> TriageDecisionRow | None:
    """Record that a PERSON decided *row_id*, and attest it.

    The ledger refuses an empty actor, so there is no path from here to a
    resolution with nobody behind it. Returns the row, or None when no such row
    exists.
    """
    if store is None:
        return None
    row = store.resolve_triage_decision(
        row_id, resolution=resolution, resolved_by=resolved_by
    )
    if row is None:
        return None
    _emit_resolved(store, row, repo=repo)
    return row


def _emit_decided(
    store: RunStore | None, write: TriageDecisionWrite, *, repo: str | None
) -> None:
    """Attest the proposal. Every additive fact rides in the payload."""
    row = write.current
    audit.emit(
        store,
        TRIAGE_DECIDED_EVENT,
        actor=_POLICY_ACTOR,
        run_id=row.run_id,
        issue_num=row.issue_num,
        item_id=row.item_id,
        repo=repo,
        payload={
            "item_id": row.item_id,
            "triage_decision_id": row.id,
            "body_digest": row.body_digest,
            "backend": row.backend,
            "model": row.model,
            "prompt_sha": row.prompt_sha,
            "outcome": row.outcome,
            "type": row.type,
            "priority": row.priority,
            "complexity": row.complexity,
            "confidence": row.confidence,
            "actionable": row.actionable,
            "needs_info": row.needs_info,
            "duplicate_of": row.duplicate_of,
            "superseded_triage_decision_id": (
                None if write.superseded is None else write.superseded.id
            ),
        },
        dedup_key=f"{TRIAGE_DECIDED_EVENT}:triage_decision:{row.id}",
    )


def _emit_resolved(
    store: RunStore, row: TriageDecisionRow, *, repo: str | None
) -> None:
    """Attest the person's decision, under their own actor.

    The actor is ``human`` with the resolver's id: a proposal resolved by the
    station would be the one thing this station must never be able to do, and
    the chain says who it was.
    """
    audit.emit(
        store,
        TRIAGE_RESOLVED_EVENT,
        actor=audit.Actor("human", row.resolved_by or "unknown"),
        run_id=row.run_id,
        issue_num=row.issue_num,
        item_id=row.item_id,
        repo=repo,
        payload={
            "item_id": row.item_id,
            "triage_decision_id": row.id,
            "body_digest": row.body_digest,
            "outcome": row.outcome,
            "resolution": row.resolution,
            "resolved_by": row.resolved_by,
            "duplicate_of": row.duplicate_of,
        },
        dedup_key=f"{TRIAGE_RESOLVED_EVENT}:triage_decision:{row.id}",
    )
