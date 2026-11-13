"""DAG validation: cycles, dangling deps, orphans, topological levels.

Pure functions over a Graph. Output ordering is deterministic (sorted / canonical
cycle rotation) so reports and tests are stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kew.graph.domain import Graph


@dataclass
class ValidationReport:
    cycles: list[list[str]] = field(default_factory=list)
    dangling: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    self_edges: list[str] = field(default_factory=list)
    levels: list[list[str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.cycles and not self.dangling and not self.self_edges


def _canonical_cycle(cycle: list[str]) -> list[str]:
    """Rotate a cycle so it starts at its lexicographically smallest node."""
    if not cycle:
        return cycle
    i = min(range(len(cycle)), key=lambda k: cycle[k])
    return cycle[i:] + cycle[:i]


def _find_cycles(graph: Graph) -> list[list[str]]:
    ids = set(graph.nodes)
    # adjacency among present nodes only (dangling edges handled separately)
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in graph.edges:
        if e.src in ids and e.dst in ids:
            adj[e.src].append(e.dst)
    for nid in adj:
        adj[nid].sort()

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in ids}
    stack: list[str] = []
    found: list[list[str]] = []
    seen_keys: set[tuple[str, ...]] = set()

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in adj[u]:
            if color[v] == GRAY:  # back-edge → cycle from v..u
                idx = stack.index(v)
                cyc = _canonical_cycle(stack[idx:])
                key = tuple(cyc)
                if key not in seen_keys:
                    seen_keys.add(key)
                    found.append(cyc)
            elif color[v] == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for nid in sorted(ids):
        if color[nid] == WHITE:
            dfs(nid)
    return found


def _internal_adjacency(graph: Graph) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Adjacency + in-degree over edges whose endpoints are both present nodes."""
    ids = set(graph.nodes)
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    indeg: dict[str, int] = {nid: 0 for nid in ids}
    for e in graph.edges:
        if e.src in ids and e.dst in ids:
            adj[e.src].append(e.dst)
            indeg[e.dst] += 1
    return adj, indeg


def _next_frontier(
    frontier: list[str], adj: dict[str, list[str]], indeg: dict[str, int]
) -> list[str]:
    """Advance Kahn's algorithm one level: decrement in-degrees of successors and
    return the nodes that become sources, sorted for determinism."""
    nxt: list[str] = []
    for u in frontier:
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                nxt.append(v)
    return sorted(nxt)


def _topo_levels(graph: Graph) -> list[list[str]]:
    adj, indeg = _internal_adjacency(graph)
    frontier = sorted(nid for nid in graph.nodes if indeg[nid] == 0)
    levels: list[list[str]] = []
    while frontier:
        levels.append(frontier)
        frontier = _next_frontier(frontier, adj, indeg)
    return levels


def validate(graph: Graph) -> ValidationReport:
    ids = set(graph.nodes)
    dangling = {e.src for e in graph.edges if e.src not in ids}
    dangling |= {e.dst for e in graph.edges if e.dst not in ids}
    connected = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    report = ValidationReport(
        self_edges=sorted(set(graph.self_edges)),
        dangling=sorted(dangling),
        orphans=sorted(nid for nid in ids if nid not in connected),
        cycles=_find_cycles(graph),
    )
    if not report.cycles:
        report.levels = _topo_levels(graph)
    return report
