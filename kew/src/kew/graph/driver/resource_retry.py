"""Re-admit a node whose run ran out of a resource, once per head (#3191).

A sandboxed dispatch the OOM killer took (137) or that aborted on a failed
allocation (134) reads as ``resource`` (:mod:`kew.exit_signal`): the same
work under the same caps may fit on a second attempt — a test pool sized by
the run, a burst that will not recur — but not on a third. So the driver
re-admits such a node exactly once for the commit its run started from, and
a second ``resource`` failure from that commit holds the node for a person
(``resource_retries_spent``). The run keeps the signal and the runner's last
output as its reason. Each retry is one more paid run.

Every relaunch clones the current main, so a per-head key alone would buy a
node that always runs out of memory one more run each time main moves. A node
therefore gets at most :data:`RESOURCE_RETRY_CAP` automatic retries in total,
across heads; the next ``resource`` failure holds it the same way.

The re-admission is the operator's own ``kew epic retry`` path, attributed to
the driver: it refuses a held node and preserved work the same way, clears
the same failure labels, and writes the same attested marker.

What the driver decided about a failed run is kept in the store's ``meta``
(:func:`decision_key`), because a drive runs each tick in a fresh process: a
hold it wrote for that run is not re-derived, re-logged — or, once a person
releases it, written again — on later ticks. The hold and that record are
written in one transaction, so neither stands without the other.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial

from kew.audit import Actor
from kew.config import DispatchConfig
from kew.epic_holds import (
    holds_node_out_of_frontier,
    resource_retry_cap_hold_reason,
    resource_retry_head_hold_reason,
)
from kew.evidence import TRUSTED_BASE_FACT, EvidenceKind
from kew.exit_signal import RESOURCE_CATEGORY
from kew.graph.domain import Graph
from kew.graph.driver.overlay import current_attempt_runs
from kew.run_store import EpicRetry, RunRecord, RunStore
from kew.service.verbs import epic_retry

_logger = logging.getLogger(__name__)

#: The ``reason`` on the retry event a ``resource`` re-admission writes.
RESOURCE_RETRY_REASON = RESOURCE_CATEGORY

#: The most automatic ``resource`` retries one node gets, across every head.
RESOURCE_RETRY_CAP = 2

_RETRY_EVENT = "epic_node_retry"
_DRIVER = Actor("policy", "driver_resource_retry")
_DECISION_META_PREFIX = "resource_retry_decision:"


class Outcome(StrEnum):
    """Why the driver did not re-admit a node for one failed run."""

    #: This head's one retry is spent; the node is held for a person.
    HELD_AT_HEAD = "held_at_head"
    #: The node's retries across heads are spent; it is held for a person.
    HELD_AT_CAP = "held_at_cap"
    #: A hold or preserved work refused the retry; that state can change.
    REFUSED = "refused"


#: The outcomes that cannot change for the run they were decided for.
_FINAL = frozenset({Outcome.HELD_AT_HEAD, Outcome.HELD_AT_CAP})


class _Withheld(ValueError):
    """The driver's own retry rule withholds this re-admission: hold instead."""

    def __init__(self, outcome: Outcome, message: str, hold_reason: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.hold_reason = hold_reason


@dataclass(frozen=True)
class _Decision:
    """What the driver last decided for one node's failed run."""

    run_id: str
    head: str
    outcome: str

    def encode(self) -> str:
        return json.dumps(
            {"run_id": self.run_id, "head": self.head, "outcome": self.outcome},
            sort_keys=True,
        )


def decision_key(epic_ref: str, item_id: str) -> str:
    """The ``meta`` key one node's last resource-retry decision lives under."""
    return f"{_DECISION_META_PREFIX}{epic_ref}:{item_id}"


def _read_decision(store: RunStore, epic_ref: str, item_id: str) -> _Decision | None:
    raw = store.get_meta(decision_key(epic_ref, item_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        # An unreadable record decides nothing: the run is decided afresh,
        # which at worst re-derives (and re-logs) a decision once.
        _logger.warning(
            "epic %s: %s resource-retry record unreadable, deciding afresh: %s",
            epic_ref,
            item_id,
            exc,
        )
        return None
    if not isinstance(data, Mapping):
        return None
    fields = (data.get("run_id"), data.get("head"), data.get("outcome"))
    if not all(isinstance(value, str) for value in fields):
        return None
    run_id, head, outcome = (str(value) for value in fields)
    return _Decision(run_id, head, outcome)


def retry_resource_failures(
    graph: Graph,
    *,
    epic_ref: str,
    store: RunStore,
    config: DispatchConfig,
) -> tuple[str, ...]:
    """Re-admit each node whose newest run failed for ``resource``, once a head.

    Returns the re-admitted node ids. Run before the overlays, so a node
    re-admitted here re-enters this very tick's frontier.
    """
    retries = {r.item_id: r for r in store.list_epic_retries(epic_ref)}
    readmitted = []
    for node in graph.nodes.values():
        if node.external:
            continue
        failed = _resource_failure(store, node.id, retries.get(node.id))
        if failed is not None and _readmit(store, config, epic_ref, node.id, failed):
            readmitted.append(node.id)
    return tuple(readmitted)


def _resource_failure(
    store: RunStore, item_id: str, retry: EpicRetry | None
) -> RunRecord | None:
    """The node's newest run, when it failed for ``resource`` and stands.

    A newer run still in flight — a ``kew epic dispatch`` by hand, a relaunch
    some other path started — stands in front of the failure: the node is
    being worked on, so the older failure re-admits nothing.
    """
    superseded = retry.superseded_run_ids if retry is not None else frozenset()
    runs = current_attempt_runs(store.list_by_item(item_id), superseded)
    if not runs:
        return None
    newest = max(runs, key=lambda r: (r.started_at, r.finished_at or r.started_at))
    if not _retryable_resource_failure(newest):
        return None
    if retry is not None and retry.retried_at > newest.started_at:
        return None  # a retry already re-admitted the node past this run
    return newest


def _retryable_resource_failure(run: RunRecord) -> bool:
    """A ``resource`` failure whose one retry is not already spent in-process.

    An uncontained dispatch's own retry loop retries a ``resource`` death once
    (``dispatch_core._resource_retry_spent``), so its second attempt's row is
    the node's second failure, not its first.
    """
    return (
        run.status == "failure"
        and run.error_category == RESOURCE_CATEGORY
        and run.attempt_number == 1
    )


def run_head(store: RunStore, run: RunRecord) -> str:
    """The commit *run* started from, as its launch recorded it; ``""`` if none."""
    for event in store.list_audit_events(
        None, event=EvidenceKind.RECEIPT_FACT.value, run_id=run.run_id
    ):
        facts = (event.payload or {}).get("facts")
        if isinstance(facts, Mapping) and facts.get("fact") == TRUSTED_BASE_FACT:
            value = facts.get("value")
            return value if isinstance(value, str) else ""
    return ""


def driver_resource_retry_heads(
    store: RunStore, epic_ref: str, item_id: str
) -> list[str]:
    """The head of every ``resource`` re-admission the driver made of a node.

    Matched on the payload's ``node``: a retry event carries the durable id
    there, whatever its tracker.
    """
    heads = []
    for event in store.list_audit_events(None, event=_RETRY_EVENT, epic_ref=epic_ref):
        payload = event.payload or {}
        if (
            payload.get("node") == item_id
            and payload.get("reason") == RESOURCE_RETRY_REASON
        ):
            head = payload.get("head")
            heads.append(head if isinstance(head, str) else "")
    return heads


def _withhold(store: RunStore, epic_ref: str, item_id: str, head: str) -> None:
    """Refuse a re-admission the node's cap or its head's key has spent.

    The cap first: once a node's retries are spent, its next ``resource``
    failure holds it, whichever head that failure came from.
    """
    heads = driver_resource_retry_heads(store, epic_ref, item_id)
    if len(heads) >= RESOURCE_RETRY_CAP:
        raise _Withheld(
            Outcome.HELD_AT_CAP,
            f"{item_id}: {len(heads)} automatic resource retries spent",
            resource_retry_cap_hold_reason(RESOURCE_RETRY_CAP),
        )
    if head in heads:
        raise _Withheld(
            Outcome.HELD_AT_HEAD,
            f"{item_id}: resource retry spent at {head or '?'}",
            resource_retry_head_hold_reason(head),
        )


def _hold(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    withheld: _Withheld,
    decision: _Decision,
) -> bool:
    """Hold the node for a person, and record why, in one transaction.

    A crash between two separate writes would leave a hold with no record of
    it, and the release of that hold would then be held again on the next
    tick. False when another honored hold stands, which is kept.
    """
    return store.hold_epic_node_unless(
        epic_ref,
        item_id,
        withheld.hold_reason,
        keep=holds_node_out_of_frontier,
        meta=(decision_key(epic_ref, item_id), decision.encode()),
    )


def _readmit(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    item_id: str,
    failed: RunRecord,
) -> bool:
    """Re-admit *item_id* once for *failed*'s head; False when it does not."""
    prior = _read_decision(store, epic_ref, item_id)
    if prior is not None and prior.run_id != failed.run_id:
        prior = None
    if prior is not None and prior.outcome in _FINAL:
        return False  # decided for this run, and nothing can change it
    head = prior.head if prior is not None else run_head(store, failed)
    try:
        epic_retry(
            store,
            epic_ref=epic_ref,
            item_id=item_id,
            config=config,
            source="driver",
            actor=_DRIVER,
            detail={
                "reason": RESOURCE_RETRY_REASON,
                "head": head,
                "run_id": failed.run_id,
            },
            precheck=partial(_withhold, store, epic_ref, item_id, head),
        )
    except _Withheld as exc:
        decision = _Decision(failed.run_id, head, exc.outcome.value)
        if _hold(store, epic_ref, item_id, exc, decision):
            _say_not_readmitted(epic_ref, item_id, f"{exc} — held for a person")
        else:
            _record(
                store, epic_ref, item_id, failed, head, Outcome.REFUSED, prior, str(exc)
            )
        return False
    except ValueError as exc:
        # A hold or preserved work refuses re-admission exactly as it refuses
        # the operator's retry; the node keeps that state, and its reason.
        _record(
            store, epic_ref, item_id, failed, head, Outcome.REFUSED, prior, str(exc)
        )
        return False
    except RuntimeError as exc:
        # The marker and its event are written; only the forge's failure label
        # edit failed. The tick goes on, and the node reads as the label says
        # until `kew epic retry` clears it.
        _logger.warning(
            "epic %s: %s re-admitted, but its failure label was not cleared: %s",
            epic_ref,
            item_id,
            exc,
        )
        return True
    _logger.warning(
        "epic %s: %s re-admitted — its run %s ran out of a resource (%s)",
        epic_ref,
        item_id,
        failed.run_id,
        failed.error_category,
    )
    return True


def _record(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    failed: RunRecord,
    head: str,
    outcome: Outcome,
    prior: _Decision | None,
    why: str,
) -> None:
    """Keep a refusal for *failed*, and say it once, when it is new."""
    decision = _Decision(failed.run_id, head, outcome.value)
    if decision == prior:
        return
    store.set_meta(decision_key(epic_ref, item_id), decision.encode())
    _say_not_readmitted(epic_ref, item_id, why)


def _say_not_readmitted(epic_ref: str, item_id: str, why: str) -> None:
    _logger.warning("epic %s: %s not re-admitted: %s", epic_ref, item_id, why)
