"""The DependencySource port — the seam a provider (GitHub now, Linear later)
implements. The graph core depends only on this, never on gh/Linear directly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kew.graph.domain import Edge, Node, NodeStatus


@runtime_checkable
class DependencySource(Protocol):
    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        """All nodes belonging to the epic identified by ``epic_ref``."""
        ...

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        """Dependency edges among (and out of) ``nodes``. May source edges from
        a marker convention or native provider relations."""
        ...

    def status_of(self, node_id: str) -> NodeStatus | None:
        """Status of any id (need not be an epic node). ``None`` if the id does
        not exist — used to resolve out-of-epic dependencies."""
        ...
