"""The live activity chain for one run.

A run's host-written event stream reduces to a line of checkpoints (init ->
tool calls -> result) — what the agent was *doing*, moment to moment, for the
scrubber to walk.

That is all this model claims. It used to stitch runs into a tree through
ForkLink and hang a git tree off each node so any two steps could be diffed;
both were speculative reconstructions from a worktree the agent still owned.
What a run *produced* is now read from the sealed artifact and the receipt
prefix that covers it (:mod:`kew.flight.evidence`), so nothing here needs to
guess at lineage or hold a handle on a mutable repository.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Map a tool name to a node kind (drives colour/grouping in the UI).
TOOL_KIND: dict[str, str] = {
    "read": "read",
    "edit": "edit",
    "write": "write",
    "bash": "bash",
    "test": "test",
    "grep": "read",
    "ls": "read",
}


@dataclass
class TimelineNode:
    id: str
    parent_id: str | None
    run_id: str
    seq: int
    ts: float
    kind: str
    label: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass
class Timeline:
    root_id: str | None = None
    head_id: str | None = None
    nodes: dict[str, TimelineNode] = field(default_factory=dict)


def _node_id(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"


def _label(event: dict[str, Any]) -> str:
    tool = str(event.get("tool", ""))
    target = str(event.get("target", ""))
    return f"{tool} {target}".strip() if tool else str(event.get("type", ""))


def _node_kind_label(event: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, label) for events that create a checkpoint node, else None."""
    etype = event.get("type")
    if etype == "started":
        return "init", f"start · #{event.get('issue', '')}"
    if etype == "tool_use":
        tool = str(event.get("tool", ""))
        return TOOL_KIND.get(tool, tool or "tool"), _label(event)
    if etype == "finished":
        return "result", "done"
    if etype == "failed":
        return "error", str(event.get("error", "failed"))
    return None


def _build_one_run(run_id: str, events: Iterable[dict[str, Any]]) -> list[TimelineNode]:
    """Reduce one run's event stream to its checkpoint chain.

    Node-producing events become nodes; `tokens` events refresh the running
    totals on the latest node.
    """
    nodes: list[TimelineNode] = []
    cum_in = 0
    cum_out = 0
    cum_cost = 0.0

    for ev in events:
        produced = _node_kind_label(ev)
        if produced is not None:
            kind, label = produced
            nodes.append(
                TimelineNode(
                    id=_node_id(run_id, len(nodes)),
                    parent_id=nodes[-1].id if nodes else None,
                    run_id=run_id,
                    seq=len(nodes),
                    ts=float(ev.get("ts", 0.0)),
                    kind=kind,
                    label=label,
                    tokens_in=cum_in,
                    tokens_out=cum_out,
                    cost_usd=cum_cost,
                )
            )
        elif ev.get("type") == "tokens":
            cum_in = int(ev.get("input", 0))
            cum_out = int(ev.get("output", 0))
            cum_cost = float(ev.get("cost_usd", 0.0))
            if nodes:
                nodes[-1].tokens_in = cum_in
                nodes[-1].tokens_out = cum_out
                nodes[-1].cost_usd = cum_cost
    return nodes


def build_timeline(events_by_run: dict[str, list[dict[str, Any]]]) -> Timeline:
    """Each run's own checkpoint chain, keyed by node id.

    Runs are kept side by side rather than joined: a retry is a separate run
    whose relationship to its predecessor is a fact somebody has to record, not
    one a reader may infer from two event streams sitting in the same dict.
    """
    tl = Timeline()
    for run_id, events in events_by_run.items():
        for node in _build_one_run(run_id, events):
            tl.nodes[node.id] = node

    roots = [n for n in tl.nodes.values() if n.parent_id is None]
    tl.root_id = roots[0].id if roots else None
    tl.head_id = max(tl.nodes.values(), key=lambda n: n.ts).id if tl.nodes else None
    return tl


def linear_path(tl: Timeline, node_id: str | None) -> list[TimelineNode]:
    """Path from root to `node_id` (inclusive), in chronological order."""
    out: list[TimelineNode] = []
    cur = tl.nodes.get(node_id) if node_id else None
    while cur is not None:
        out.append(cur)
        cur = tl.nodes.get(cur.parent_id) if cur.parent_id else None
    out.reverse()
    return out
