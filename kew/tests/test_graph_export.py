import pytest

from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.export import render


def _g():
    return Graph(
        [
            Node(id="1", title="Root", status=NodeStatus.DONE, epic_ref="foo"),
            Node(id="2", title="Child", status=NodeStatus.TODO, epic_ref="foo"),
        ],
        [Edge("1", "2")],
    )


def test_mermaid_contains_nodes_edges_and_is_deterministic():
    out = render(_g(), "mermaid")
    assert out.startswith("graph TD")
    assert "n1" in out and "n2" in out
    assert "n1 --> n2" in out
    assert render(_g(), "mermaid") == out  # deterministic


def test_dot_format():
    out = render(_g(), "dot")
    assert out.startswith("digraph")
    assert '"1" -> "2"' in out


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        render(_g(), "svg")
