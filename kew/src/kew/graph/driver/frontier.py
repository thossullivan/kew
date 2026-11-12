"""Pure ready-frontier predicate for the Epic DAG driver.

Extracted from the Phase 1 inline logic in ``kew.cli.epic_status`` so the
driver (Phase 2) can reuse identical semantics without duplicating them.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from kew.graph.domain import Graph, NodeStatus
from kew.ids import item_id_sort_key, item_ref

#: Per-dependency id -> the dependent ids an acknowledged no-change dependency
#: effect has explicitly satisfied (I12). Empty for every ordinary epic.
DependencyEffects = Mapping[str, Collection[str]]

#: The rank an unlabelled node gets: the same as ``p2-medium`` (#2575).
DEFAULT_PRIORITY = 2

# ``p0-critical`` … ``p3-low`` (``kew.graph.plan.Priority``), and the bare
# ``p1`` / ``P1`` spelling. Anchored so ``python``/``perf`` never read as one.
_PRIORITY_LABEL = re.compile(r"p(\d)(?:-|$)", re.IGNORECASE)


def priority_of(labels: Iterable[str]) -> int:
    """The node's priority rank from its labels: 0 is most urgent.

    The most urgent label wins when several are present; a node with none
    ranks as :data:`DEFAULT_PRIORITY` — filing it unlabelled is not a reason
    to run it behind a ``p3-low``.
    """
    ranks = [
        int(m.group(1)) for m in map(_PRIORITY_LABEL.match, labels) if m is not None
    ]
    return min(ranks, default=DEFAULT_PRIORITY)


@dataclass(frozen=True)
class FrontierRank:
    """Why a ready node sits where it does in the frontier (#2575)."""

    node_id: str
    priority: int
    blast: int

    def describe(self) -> str:
        return f"{item_ref(self.node_id)} p{self.priority}, blast {self.blast}"


def frontier_rank(
    graph: Graph, node_id: str, priorities: Mapping[str, int]
) -> FrontierRank:
    """The rank *node_id* is ordered by: its priority and its blast radius."""
    return FrontierRank(
        node_id=node_id,
        priority=priorities.get(node_id, DEFAULT_PRIORITY),
        blast=len(graph.transitive_dependents(node_id)),
    )


def order_frontier(
    graph: Graph, ready: Iterable[str], priorities: Mapping[str, int]
) -> list[str]:
    """Order ready ids: priority first, then blast radius, then the id.

    p0 before p3; at equal priority the node more downstream work waits on
    goes first; at equal both, the lower issue number — so the order is
    stable from tick to tick.
    """

    def key(nid: str) -> tuple[int, int, tuple[int, int | str]]:
        rank = frontier_rank(graph, nid, priorities)
        return (rank.priority, -rank.blast, item_id_sort_key(nid))

    return sorted(ready, key=key)


def _is_satisfied(
    graph: Graph,
    status: dict[str, NodeStatus],
    dependency_effects: DependencyEffects,
    *,
    dependency: str,
    dependent: str,
) -> bool:
    """Whether *dependency* satisfies *dependent*'s edge, and by what right.

    Two routes, and only two. The ordinary one is a raw provider DONE. The
    other is an acknowledged no-change dependency effect naming this exact
    dependent (I12): a zero-diff outcome writes nothing to the provider, so it
    can never earn a DONE — it advances the one edge a person authorized it to
    advance, one dependent at a time, and nothing else.
    """
    if dependency not in graph.nodes:
        return False
    if status.get(dependency) is NodeStatus.DONE:
        return True
    return dependent in dependency_effects.get(dependency, ())


def compute_frontier(
    graph: Graph,
    *,
    status: dict[str, NodeStatus],
    excluded: set[str],
    dependency_effects: DependencyEffects | None = None,
    priorities: Mapping[str, int] | None = None,
) -> list[str]:
    """Return the ids of nodes ready to dispatch, in dispatch order.

    A node is in the frontier iff:
    - its status in ``status`` is raw provider ``NodeStatus.TODO``, and
    - every dependency ``d in graph.dependencies_of(nid)`` is a known node
      (``d in graph.nodes``) that is either DONE or has acknowledged a
      no-change dependency effect naming this node, and
    - its id is not in ``excluded``.

    Fails closed: a dangling/missing dependency keeps the node OUT of the
    frontier rather than being silently treated as satisfied — an effect that
    names an unknown dependency does not rescue it either.

    Args:
        graph: The dependency graph.
        status: Per-node id -> NodeStatus view to check readiness against.
        excluded: Node ids to drop from the frontier regardless of readiness.
        dependency_effects: Per-dependency acknowledged no-change effects
            (I12), read from the durable judgment — never from provider state.
        priorities: Per-node priority rank (see :func:`priority_of`); a node
            absent from it ranks as :data:`DEFAULT_PRIORITY`.

    Returns:
        Ready node ids ordered by :func:`order_frontier`.
    """
    effects: DependencyEffects = dependency_effects or {}
    ready = (
        nid
        for nid in graph.nodes
        if nid not in excluded
        and status.get(nid) is NodeStatus.TODO
        and all(
            _is_satisfied(graph, status, effects, dependency=d, dependent=nid)
            for d in graph.dependencies_of(nid)
        )
    )
    return order_frontier(graph, ready, priorities or {})


def apply_review_backpressure(
    frontier: Sequence[str],
    *,
    saturated: bool,
    fresh_work: Collection[str],
) -> tuple[list[str], list[str]]:
    """Split a ready frontier into what may launch and what review pressure holds.

    The brake is deliberately one-sided. While the delivery repository is
    saturated (I15/E5 occupancy at or above the effective cap, or remote truth
    that may not be counted from), the ids in *fresh_work* — nodes whose
    dispatch would START new independent work — are withheld; everything else
    in the frontier is admitted untouched, so completion, recovery and
    remediation still move while people are the constraint.

    Below capacity this is the identity split: the WHOLE frontier is admitted
    in its ordinary fairness order. There is no one-launch throttle and no
    residual penalty — falling below the cap restores the plain frontier
    contract, it does not replace it with a slower one.

    Withholding is not eviction and not a status change: a withheld node stays
    exactly as ready as it was and returns to the frontier the moment pressure
    drops.

    Returns:
        ``(admitted, withheld)``, each in the frontier's own order.
    """
    if not saturated:
        return list(frontier), []
    admitted = [nid for nid in frontier if nid not in fresh_work]
    withheld = [nid for nid in frontier if nid in fresh_work]
    return admitted, withheld
