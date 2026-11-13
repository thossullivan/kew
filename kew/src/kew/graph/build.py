"""Assemble a Graph from a DependencySource, resolving out-of-epic deps."""

from __future__ import annotations

from kew.graph.domain import Graph, Node
from kew.graph.port import DependencySource


def build_graph(source: DependencySource, epic_ref: str) -> Graph:
    nodes = source.list_epic_nodes(epic_ref)
    edges = source.list_edges(nodes)
    node_ids = {n.id for n in nodes}
    extra: dict[str, Node] = {}
    for e in edges:
        if e.src in node_ids or e.src in extra:
            continue
        status = source.status_of(e.src)
        if status is not None:
            # A resolvable dependency outside the epic: record it as a satisfied
            # boundary node so it is not mistaken for a dangling (404) reference.
            extra[e.src] = Node(
                id=e.src,
                title="(external)",
                status=status,
                epic_ref=epic_ref,
                external=True,
            )
        # else: genuinely missing -> omit; validate() reports it as dangling.
    graph = Graph(list(nodes) + list(extra.values()), list(edges))
    # Only the body-reading GitHub sources record this; others carry no markers.
    graph.multi_depends = list(getattr(source, "multi_depends", ()))
    return graph
