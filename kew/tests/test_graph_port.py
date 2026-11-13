from kew.graph.domain import Edge, Node, NodeStatus
from kew.graph.port import DependencySource


class _Fake:
    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return []

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        return []

    def status_of(self, node_id: str) -> NodeStatus | None:
        return None


def test_fake_satisfies_protocol():
    assert isinstance(_Fake(), DependencySource)
