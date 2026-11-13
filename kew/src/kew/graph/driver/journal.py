"""Snapshot builder + journal writer (#454 Phase A, spec Decision 4)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from kew.audit import Actor, emit
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import TickResult

if TYPE_CHECKING:
    from kew.graph.domain import Graph, Node
    from kew.graph.driver.facts import NodeFacts
    from kew.run_store import RunStore

SNAPSHOT_SCHEMA = 1
ACTIONS_SCHEMA = 1

_STATE_FIELDS = ("signal", "counts", "nodes", "edges", "frontier")
_DRIVER_ACTOR = Actor("policy", "epic-driver")
_logger = logging.getLogger(__name__)


def apply_action_statuses(
    statuses: dict[str, DriverStatus],
    *,
    dispatched: tuple[str, ...],
    held: tuple[str, ...],
    merged: tuple[str, ...],
    closed: tuple[str, ...],
) -> dict[str, DriverStatus]:
    """Overlay one tick's own actions onto the pre-action overlay statuses so
    the summary/snapshot never contradicts what the tick DID. `merged`/`closed`
    win last (a node can't be both dispatched and merged in one tick, but the
    order keeps the overlay total). `closed` is the out-of-band-merged set: kew
    didn't merge it, but the PR merged and the item closed, so it reads DONE too
    (SFD-139)."""
    patched = dict(statuses)
    for nid in dispatched:
        patched[nid] = DriverStatus.IN_PROGRESS
    for nid in held:
        patched[nid] = DriverStatus.HELD
    for nid in (*merged, *closed):
        patched[nid] = DriverStatus.DONE
    return patched


def patch_statuses(
    statuses: dict[str, DriverStatus], result: TickResult
) -> dict[str, DriverStatus]:
    """`apply_action_statuses` keyed off a `TickResult`'s action sets — so the
    journal snapshot never contradicts the `actions` column."""
    return apply_action_statuses(
        statuses,
        dispatched=result.dispatched,
        held=result.held,
        merged=result.merged,
        closed=result.closed,
    )


#: ``mergeStateStatus``/``headRefOid`` values that say nothing. GitHub answers
#: ``UNKNOWN`` for a mergeability it has not computed yet, and it recomputes on
#: its own schedule — so a node flips CLEAN → UNKNOWN → CLEAN with no change on
#: this host. An unreadable value is therefore never WRITTEN, which is also how
#: every reader already spells "nobody could look".
#:
#: Omitting it is not enough on its own: an absent key is a different snapshot
#: from a present one, so the flip would still move the hash the journal dedups
#: on, append a tick row per flip and leave an operator reading churn as
#: movement — and the state's ``merge_state_since`` would restart on the way
#: back. The merge state is therefore CARRIED FORWARD across a tick that could
#: not read one AND read the same head (:func:`_carried_merge_state`), with
#: ``merge_state_at`` saying when it was last actually read so no reader has to
#: assume it was this tick.
_UNREADABLE_PR_FACTS = frozenset({"", "UNKNOWN"})

#: The merge-state keys, as one unit: a carried-forward state must keep its
#: first-seen stamp and its reading instant, or the dedup it exists for fails on
#: the stamp instead of the state.
_MERGE_STATE_KEYS = ("merge_state", "merge_state_since", "merge_state_at")

#: Node keys that say WHEN something was observed, not what the state is. They
#: are left out of :func:`snapshot_hash`: ``merge_state_at`` is stamped on every
#: tick GitHub answers, so hashing it made every quiet tick a "change" and the
#: CHANGE column count ticks (#2879). The freshness still reaches readers: every
#: tick writes its read times to separate mutable state, never into a journal
#: row (``RunStore.append_epic_tick``).
_OBSERVATION_KEYS = frozenset({"merge_state_at"})


def _said(value: str | None) -> str | None:
    """*value* when it is a fact, None when it is GitHub's "I did not look"."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.upper() in _UNREADABLE_PR_FACTS else text


def _merge_state_since(
    prev: dict[str, Any] | None, merge_state: str, tick_ts: str
) -> str:
    """When THIS merge state was first seen, carried forward like ``status_since``.

    A node whose previous snapshot reported the same state keeps that snapshot's
    stamp, so a surface can say "behind since 10:46" instead of inventing a
    duration from whichever tick happened to read it. A missing previous stamp
    falls back to this tick rather than to None: an absent instant would make the
    state look unaged.
    """
    if prev is None or prev.get("merge_state") != merge_state:
        return tick_ts
    return str(prev.get("merge_state_since") or tick_ts)


def _carried_merge_state(
    prev: dict[str, Any] | None, head_oid: str | None
) -> dict[str, Any]:
    """Last snapshot's merge state, VERBATIM, for a tick that could not read one.

    Copied key for key rather than recomposed: the whole point is that a tick
    GitHub answered ``UNKNOWN`` on produces a byte-identical entry, so the
    snapshot hash does not move and no tick row is appended for a flip that
    happened on GitHub's side. ``merge_state_at`` comes along unchanged, which is
    what keeps the carried value honestly aged — a reader bounds it (see
    ``kew.service.work.JOURNALED_MERGE_STATE_MAX_AGE_S``) rather than reading it
    as this tick's own observation.

    The carry is PINNED TO THE HEAD it was read about (*head_oid*, this tick's
    own reading). A ``mergeStateStatus`` is a fact about one commit against its
    base, and a pushed head is the canonical reason GitHub answers ``UNKNOWN``
    next — so carrying across a head change journals the OLD head's reading as a
    fact about the new one: a blocking ``merge_conflict`` on a branch that was
    just rebased clean, or a CLEAN read as "this head merges into its base" for
    a head nobody has ever merged. Nothing is carried unless BOTH heads are
    known and identical; where they are not, the tick simply has no merge state,
    which is what every reader already spells "nobody could look".

    Dropping the carry there costs no dedup either: a tick that read no head
    omits ``head_oid`` (and one that read a new head writes it), so the snapshot
    hash has already moved for that node. The hash stability this carry exists
    for is exactly the stable-head case it still covers.

    A previous snapshot with no merge state carries nothing: an epic whose
    driver has never read one must not gain a fact from being ticked.
    """
    if prev is None or _said(prev.get("merge_state")) is None:
        return {}
    if head_oid is None or _said(prev.get("head_oid")) != head_oid:
        return {}
    return {k: prev[k] for k in _MERGE_STATE_KEYS if prev.get(k)}


def _merge_state_entry(
    facts: NodeFacts | None,
    prev: dict[str, Any] | None,
    tick_ts: str,
    head_oid: str | None,
) -> dict[str, Any]:
    """The merge state, when it was first seen, and when it was last READ."""
    state = _said(getattr(facts, "merge_state_status", None))
    if state is None:
        return _carried_merge_state(prev, head_oid)
    return {
        "merge_state": state,
        "merge_state_since": _merge_state_since(prev, state, tick_ts),
        "merge_state_at": tick_ts,
    }


def _pr_facts_entry(
    facts: NodeFacts | None,
    prev: dict[str, Any] | None,
    tick_ts: str,
) -> dict[str, Any]:
    """The driver's per-tick PR facts, as snapshot keys (#2303).

    ``headRefOid``, ``mergeStateStatus`` and the head's push time are read every
    tick (``graph/driver/facts.py``) and were then thrown away, so no reader
    without a GitHub token could tell a verdict's head from the current one, or
    a conflict from a behind-but-clean branch. Persisting them needs no
    migration — a snapshot is JSON — and an unreadable value is never written as
    ``UNKNOWN``: the merge state keeps its last reading (``_merge_state_entry``)
    and the other two are omitted, so a fact GitHub has not computed cannot
    churn the snapshot hash.

    The carry is pinned to the head: a merge state is a fact about one commit
    against its base, so it is only carried over a tick whose head reads the
    same (``_carried_merge_state``).

    ``merge_state_since`` carries forward exactly as ``status_since`` does: it
    is when THIS merge state was first seen, which is what lets a surface say
    "behind since 10:46" instead of inventing a duration from the tick it
    happened to be read on. ``merge_state_at`` is the other half of that: when
    the state was last READ, which is this tick when GitHub answered and an
    older tick when the value was carried over an ``UNKNOWN``.
    """
    # ``getattr`` rather than a ternary per field: a node with no facts at all
    # (an unlinked issue) answers None to every one of them, which is the same
    # "nobody looked" an unreadable value gets.
    read = {
        "head_oid": _said(getattr(facts, "head_oid", None)),
        "pr_pushed_at": _said(getattr(facts, "pr_pushed_at", None)),
    }
    entry: dict[str, Any] = {k: v for k, v in read.items() if v is not None}
    # The head goes INTO the merge-state entry: a carried state is only a fact
    # about the head it was read on (``_carried_merge_state``).
    entry.update(_merge_state_entry(facts, prev, tick_ts, read["head_oid"]))
    return entry


def _node_entry(
    node: Node,
    status: str,
    *,
    tick_ts: str,
    prev_nodes: dict[str, dict[str, Any]],
    facts: NodeFacts | None,
    stuck_reason: str | None,
) -> dict[str, Any]:
    if node.external:
        return {"id": str(node.id), "status": status, "external": True}
    prev = prev_nodes.get(str(node.id))
    status_since = (
        prev["status_since"]
        if prev is not None
        and prev.get("status") == status
        and prev.get("status_since")
        else tick_ts
    )
    pr_url = (facts.pr_url if facts is not None else None) or node.pr_url
    return {
        "id": str(node.id),
        "title": str(node.title or ""),
        "status": status,
        "status_since": status_since,
        "url": node.url,
        "pr_url": pr_url,
        "external": False,
        "stuck": stuck_reason,
        **_pr_facts_entry(facts, prev, tick_ts),
    }


def _predecessor_nodes(prev_snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The previous snapshot's nodes, or none when it is not a snapshot (#2949).

    The previous snapshot is the epic's latest stored journal row, read back as
    JSON. A row of the wrong shape — ``nodes`` not a list, or a node that is not
    an object (``nodes: [null]``) — would otherwise raise after the tick's
    driver work, so the journal write and the finish heartbeat never happen and
    every later tick meets the same row. It is read as no predecessor instead:
    the tick journals as though the epic had no earlier row.
    """
    if prev_snapshot is None:
        return []
    nodes = prev_snapshot.get("nodes") if isinstance(prev_snapshot, dict) else None
    if isinstance(nodes, list) and all(isinstance(n, dict) for n in nodes):
        return nodes
    _logger.warning(
        "epic %s: latest journal snapshot is not a snapshot this build can read;"
        " journaling this tick with no predecessor",
        prev_snapshot.get("epic_ref") if isinstance(prev_snapshot, dict) else None,
    )
    return []


def build_snapshot(
    *,
    epic_ref: str,
    graph: Graph,
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    stuck: dict[str, str],
    frontier: list[str],
    signal: str,
    tick_ts: str,
    prev_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a self-contained snapshot JSON capturing node facts, patched
    statuses, and graph topology at this tick. List fields are sorted so the
    output is canonical for hashing."""
    counts: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []
    prev_nodes = {str(n.get("id")): n for n in _predecessor_nodes(prev_snapshot)}

    for nid in sorted(graph.nodes):
        node = graph.nodes[nid]
        ds = statuses.get(nid)
        status = str(ds.value) if ds is not None else str(node.status.value)
        counts[status] = counts.get(status, 0) + 1
        nodes.append(
            _node_entry(
                node,
                status,
                tick_ts=tick_ts,
                prev_nodes=prev_nodes,
                facts=node_facts.get(nid),
                stuck_reason=stuck.get(nid),
            )
        )

    return {
        "schema": SNAPSHOT_SCHEMA,
        "epic_ref": epic_ref,
        "signal": str(signal),
        "counts": counts,
        # Dispatch order, not sorted: the console's "next up" reads it as the
        # order the driver will take (#2575). `snapshot_hash` sorts its copy.
        "frontier": [str(x) for x in frontier],
        "nodes": nodes,
        "edges": sorted([str(e.src), str(e.dst)] for e in graph.edges),
    }


def build_actions(result: TickResult) -> dict[str, Any]:
    """The tick's own action sets, as the journal's ``actions`` column.

    ``closed`` is the out-of-band-merged set (SFD-139) and was the one action
    the journal did not record: :func:`apply_action_statuses` already paints
    those nodes DONE in the snapshot, so a reader saw a node go DONE on a tick
    whose ``actions`` claimed the tick did nothing (#2304). It is a separate key
    rather than folded into ``merged`` because kew did not do the merge —
    somebody else did, and a surface that cannot tell them apart cannot say so.
    """
    return {
        "schema": ACTIONS_SCHEMA,
        "dispatched": sorted(result.dispatched),
        "merged": sorted(result.merged),
        "closed": sorted(result.closed),
        "held": sorted(result.held),
        "stuck": [
            {"id": s.node_id, "reason": s.reason}
            for s in sorted(result.stuck, key=lambda s: s.node_id)
        ],
    }


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    """SHA-256 over a canonical serialization of the state-only fields.
    Sorts list fields itself so a permuted-input snapshot (e.g. from a
    test) hashes identically to the canonical one. A node's observation
    instants (:data:`_OBSERVATION_KEYS`) are not state and are not hashed."""
    state: dict[str, Any] = {}
    for field in _STATE_FIELDS:
        value = snapshot[field]
        if field == "nodes":
            value = sorted(
                (
                    {k: v for k, v in n.items() if k not in _OBSERVATION_KEYS}
                    for n in value
                ),
                key=lambda n: str(n.get("id")),
            )
        elif field == "edges":
            value = sorted(map(list, value))
        elif field == "frontier":
            value = sorted(value)
        state[field] = value
    canonical = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _transitions(
    prev_snapshot: dict[str, Any] | None, snapshot: dict[str, Any]
) -> list[tuple[str, str | None, str]]:
    """Extract (node_id, from_status, to_status) for nodes whose status changed."""
    prev_status = {
        str(n.get("id")): n.get("status") for n in _predecessor_nodes(prev_snapshot)
    }
    out: list[tuple[str, str | None, str]] = []
    for n in snapshot.get("nodes", []):
        nid = str(n.get("id"))
        if prev_status.get(nid) != (new := n.get("status")):
            out.append((nid, prev_status.get(nid), str(new)))
    return out


def _int_or_none(node_id: str) -> int | None:
    """Parse a node id to int when it is numeric (a GitHub issue number)."""
    try:
        return int(node_id)
    except ValueError:
        return None


def record_tick(
    store: RunStore,
    *,
    epic_ref: str,
    snapshot: dict[str, Any],
    actions: dict[str, Any],
    tick_ts: str,
    prev_snapshot: dict[str, Any] | None,
) -> tuple[int, bool]:
    """Dedup-append the snapshot and emit node-transition audit events.
    Append failures PROPAGATE loudly (this raises); audit emission stays
    best-effort via audit.emit. The journal row is the authoritative record;
    the chain corroborates it."""
    tick_id, appended = store.append_epic_tick(
        epic_ref=epic_ref,
        tick_ts=tick_ts,
        snapshot=snapshot,
        actions=actions,
        snapshot_hash=snapshot_hash(snapshot),
        # A latest row hashed by an older build still matches an unchanged
        # tick when its snapshot re-hashes the same (#2879).
        rehash=snapshot_hash,
    )
    if appended:
        for nid, frm, to in _transitions(prev_snapshot, snapshot):
            emit(
                store,
                "epic_node_status",
                actor=_DRIVER_ACTOR,
                issue_num=_int_or_none(nid),
                item_id=nid,
                payload={"epic_ref": epic_ref, "from": frm, "to": to},
            )
    return tick_id, appended
