from kew.graph.build import build_graph
from kew.graph.domain import Edge, Node, NodeStatus
from kew.graph.validate import validate


class FakeSource:
    """In-memory DependencySource for testing the build logic."""

    def __init__(self, nodes, edges, statuses):
        self._nodes = nodes
        self._edges = edges
        self._statuses = statuses  # id -> NodeStatus | None

    def list_epic_nodes(self, epic_ref):
        return list(self._nodes)

    def list_edges(self, nodes):
        return list(self._edges)

    def status_of(self, node_id):
        return self._statuses.get(node_id)


def _n(nid, status=NodeStatus.TODO):
    return Node(id=nid, title=f"t{nid}", status=status, epic_ref="foo")


def test_out_of_epic_closed_dep_added_as_satisfied_boundary_node():
    # epic node "1" depends on "100" which is NOT an epic member but is closed/DONE
    src = FakeSource(
        nodes=[_n("1")],
        edges=[Edge("100", "1")],
        statuses={"100": NodeStatus.DONE},
    )
    g = build_graph(src, "foo")
    assert "100" in g.nodes
    assert g.nodes["100"].external is True
    assert g.nodes["100"].status is NodeStatus.DONE
    assert validate(g).dangling == []  # resolved, not dangling


def test_missing_dep_left_dangling():
    src = FakeSource(
        nodes=[_n("1")],
        edges=[Edge("999", "1")],
        statuses={"999": None},  # 404
    )
    g = build_graph(src, "foo")
    assert "999" not in g.nodes
    assert validate(g).dangling == ["999"]


def test_epic_member_dep_not_double_resolved():
    src = FakeSource(
        nodes=[_n("1"), _n("2")],
        edges=[Edge("1", "2")],
        statuses={},  # status_of must NOT be consulted for epic members
    )
    g = build_graph(src, "foo")
    assert set(g.nodes) == {"1", "2"}
