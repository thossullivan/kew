from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.validate import validate


def _n(nid: str) -> Node:
    return Node(id=nid, title=f"t{nid}", status=NodeStatus.TODO, epic_ref="foo")


def test_clean_chain_is_ok_with_levels():
    g = Graph([_n("1"), _n("2"), _n("3")], [Edge("1", "2"), Edge("2", "3")])
    r = validate(g)
    assert r.ok
    assert r.levels == [["1"], ["2"], ["3"]]


def test_diamond_levels():
    g = Graph(
        [_n("1"), _n("2"), _n("3"), _n("4")],
        [Edge("1", "2"), Edge("1", "3"), Edge("2", "4"), Edge("3", "4")],
    )
    r = validate(g)
    assert r.ok
    assert r.levels[0] == ["1"]
    assert r.levels[-1] == ["4"]
    assert sorted(r.levels[1]) == ["2", "3"]


def test_cycle_detected_and_canonicalized():
    g = Graph(
        [_n("1"), _n("2"), _n("3")], [Edge("1", "2"), Edge("2", "3"), Edge("3", "1")]
    )
    r = validate(g)
    assert not r.ok
    assert r.cycles, "expected at least one cycle"
    # canonical: rotated to start at the lexicographically smallest node id
    assert r.cycles[0][0] == "1"
    assert set(r.cycles[0]) == {"1", "2", "3"}


def test_dangling_dependency():
    g = Graph([_n("1")], [Edge("999", "1")])  # dep 999 is not a node
    r = validate(g)
    assert r.dangling == ["999"]
    assert not r.ok


def test_orphan_node():
    g = Graph([_n("1"), _n("2"), _n("3")], [Edge("1", "2")])
    r = validate(g)
    assert r.orphans == ["3"]
    assert r.ok  # orphans are info, not an error


def test_self_edge_reported():
    g = Graph([_n("1")], [Edge("1", "1")])
    r = validate(g)
    assert r.self_edges == ["1"]
    assert not r.ok


def test_self_loop_does_not_suppress_levels_for_acyclic_chain():
    # A self-loop on an unrelated node must NOT be counted as a real cycle, so
    # topo levels are still computed for the acyclic chain 1 -> 2 -> 3.
    g = Graph(
        [_n("1"), _n("2"), _n("3"), _n("4")],
        [Edge("1", "2"), Edge("2", "3"), Edge("4", "4")],
    )
    r = validate(g)
    assert r.cycles == []  # self-loop is tracked via self_edges, not cycles
    assert r.self_edges == ["4"]
    assert r.levels  # populated, not skipped
    assert r.levels[0] == ["1", "4"]  # node 4 has no in-edges -> level 0
    assert r.levels[-1] == ["3"]
