"""The base-conflict hold of an epic the standing merge rule excludes (#2862).

The drive's merge pass never reaches an excluded epic, so whether its READY PR
still merges into the base is read here: a conflict at the reviewed head gets
the merge pass's own ``ci_pending:dirty`` hold, and the review stage's merge-main
step (the resolve) runs on it exactly as for any other epic.

Both transitions are single store operations scoped to the item and to this
one reason (#2869). Review actions run detached, so a resolver a previous tick
started can write ``base_integration_conflict`` or ``base_integration_exhausted``
at any moment; a read followed by a separate write or delete would replace or
erase that permanent hold, losing its conflict detail and its retry bound.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

from kew.config import DriveMergeConfig
from kew.epic_holds import ci_pending_hold_reason, is_another_writers_hold
from kew.graph.driver.base_integration import DIRTY_MERGE_STATE, base_integration_due
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.run_store import RunStore

_VERDICT_READY = "READY"

#: The one reason these transitions write and release.
CONFLICT_HOLD_REASON = ci_pending_hold_reason(DIRTY_MERGE_STATE)


def _epic_key(name: str) -> str:
    """An epic name as the rule compares it: trimmed, case-folded, with no
    ``epic:`` label prefix, so a near-miss spelling never fails open."""
    return name.strip().casefold().removeprefix("epic:").strip()


def excluded_epics(rule: DriveMergeConfig) -> list[str]:
    """The rule's excluded epics, normalized and sorted (what is signed)."""
    return sorted({_epic_key(name) for name in rule.exclude_epics})


def excluded_by_rule(epic: str, rule: DriveMergeConfig) -> bool:
    """Whether the standing merge rule leaves every PR of *epic* to a person."""
    return _epic_key(epic) in excluded_epics(rule)


def hold_conflict(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """Hold *item_id* ``ci_pending:dirty`` unless another writer holds it;
    True when the node now carries the conflict hold."""
    return store.hold_epic_node_unless(
        epic_ref, item_id, CONFLICT_HOLD_REASON, keep=is_another_writers_hold
    )


def release_conflict(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """Release the conflict hold, and only it; True when one was released."""
    released = store.remove_epic_hold(
        epic_ref, item_id, only_reason_prefixes=(CONFLICT_HOLD_REASON,)
    )
    return released is not None


def hold_excluded_conflicts(
    statuses: MutableMapping[str, DriverStatus],
    node_facts: Mapping[str, NodeFacts],
    *,
    epic_ref: str,
    store: RunStore,
) -> list[str]:
    """Hold, inside the tick and before its review stage, every node of an
    excluded epic whose READY head stopped merging; the ids held.

    Read on the tick's own facts, so the hold lands before the tick journals
    (the console no longer reads ``awaiting merge`` for a round) and the
    review stage sees the node HELD on the merge pass's own reason. Only an
    IN_REVIEW node whose latest review round is READY at the head the conflict
    was read on is classified — the drive's own read holds the same set.

    A node whose hold write was refused is HELD too: the refusal means another
    writer's honored hold already stands — a detached resolver's
    ``base_integration_*``, written after this tick's overlays were built — and
    leaving the stale IN_REVIEW would admit a second resolve past its bound.
    """
    held = []
    for item_id, status in statuses.items():
        facts = node_facts.get(item_id)
        if status is not DriverStatus.IN_REVIEW or facts is None:
            continue
        if not (base_integration_due(facts) and _ready_at(store, item_id, facts)):
            continue
        if hold_conflict(store, epic_ref, item_id):
            held.append(item_id)
        statuses[item_id] = DriverStatus.HELD
    return held


def _ready_at(store: RunStore, item_id: str, facts: NodeFacts) -> bool:
    """Whether the node's latest review round is READY at *facts*' head."""
    latest = store.latest_review_round(item_id)
    return (
        latest is not None
        and latest.verdict == _VERDICT_READY
        and latest.head_sha.lower() == (facts.head_oid or "").lower()
    )
