"""Provider-neutral epic dependency-graph domain model.

Pure data + pure functions. No I/O, no provider (GitHub/Linear) knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NodeStatus(StrEnum):
    """Normalized node status.

    Provenance matters: TODO/QUEUED/READY/BLOCKED/IN_REVIEW/DONE are
    PROVIDER-derived
    (from a DependencySource). IN_PROGRESS/FAILED are KEW-RUN-derived (from the
    run store), not a provider fact. In Phase 1 all states are display-only.
    """

    TODO = "todo"
    # Known work that is not admitted to the dispatch frontier.
    QUEUED = "queued"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class Node:
    id: str
    title: str
    status: NodeStatus
    epic_ref: str
    url: str | None = None
    pr_url: str | None = None
    last_run_id: str | None = None
    external: bool = False  # a resolved dependency that is not an epic member


@dataclass(frozen=True)
class Edge:
    src: str  # the dependency
    dst: str  # the dependent; dst depends on src, src must be DONE first


class Graph:
    """A DAG over Nodes and Edges.

    On construction, edges are deduplicated and self-edges (src == dst) are
    dropped; dropped self-references are recorded in ``self_edges`` so
    validation can surface them.
    """

    def __init__(self, nodes: list[Node], edges: list[Edge]) -> None:
        self.nodes: dict[str, Node] = {n.id: n for n in nodes}
        seen: set[tuple[str, str]] = set()
        deduped: list[Edge] = []
        self.self_edges: list[str] = []
        for e in edges:
            if e.src == e.dst:
                self.self_edges.append(e.src)
                continue
            key = (e.src, e.dst)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
        self.edges: list[Edge] = deduped
        # Nodes whose body carries more than one kew:depends comment. The
        # comments are unioned; `kew epic validate` warns so they get merged.
        self.multi_depends: list[str] = []

    def dependencies_of(self, node_id: str) -> list[str]:
        return sorted(e.src for e in self.edges if e.dst == node_id)

    def dependents_of(self, node_id: str) -> list[str]:
        return sorted(e.dst for e in self.edges if e.src == node_id)

    def transitive_dependents(self, node_id: str) -> set[str]:
        out: set[str] = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for d in self.dependents_of(cur):
                if d not in out:
                    out.add(d)
                    stack.append(d)
        return out

    def roots(self) -> list[str]:
        has_dep = {e.dst for e in self.edges}
        return sorted(nid for nid in self.nodes if nid not in has_dep)

    def leaves(self) -> list[str]:
        has_dependent = {e.src for e in self.edges}
        return sorted(nid for nid in self.nodes if nid not in has_dependent)
