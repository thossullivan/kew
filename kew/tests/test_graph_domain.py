from kew.graph.domain import Edge, Graph, Node, NodeStatus


def _n(nid: str, status: NodeStatus = NodeStatus.TODO) -> Node:
    return Node(id=nid, title=f"t{nid}", status=status, epic_ref="foo")


def test_dedup_and_self_edges_removed():
    g = Graph(
        [_n("1"), _n("2")],
        [Edge("1", "2"), Edge("1", "2"), Edge("2", "2")],
    )
    assert g.edges == [Edge("1", "2")]
    assert g.self_edges == ["2"]


def test_dependencies_and_dependents():
    g = Graph([_n("1"), _n("2"), _n("3")], [Edge("1", "3"), Edge("2", "3")])
    assert g.dependencies_of("3") == ["1", "2"]
    assert g.dependents_of("1") == ["3"]
    assert g.dependencies_of("1") == []


def test_transitive_dependents_diamond():
    # 1 -> 2 -> 4, 1 -> 3 -> 4
    g = Graph(
        [_n("1"), _n("2"), _n("3"), _n("4")],
        [Edge("1", "2"), Edge("1", "3"), Edge("2", "4"), Edge("3", "4")],
    )
    assert g.transitive_dependents("1") == {"2", "3", "4"}


def test_roots_and_leaves():
    g = Graph([_n("1"), _n("2"), _n("3")], [Edge("1", "2"), Edge("2", "3")])
    assert g.roots() == ["1"]
    assert g.leaves() == ["3"]
