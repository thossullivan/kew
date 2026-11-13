import json
from unittest.mock import patch

from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.domain import Edge, NodeStatus


def _result(stdout, returncode=0, stderr=""):
    class R:
        pass

    r = R()
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


ISSUES = [
    {
        "number": 1,
        "title": "Root",
        "url": "u1",
        "state": "CLOSED",
        "stateReason": "COMPLETED",
        "labels": [],
        "body": "",
    },
    {
        "number": 2,
        "title": "Blocked one",
        "url": "u2",
        "state": "OPEN",
        "stateReason": None,
        "labels": [{"name": "blocked"}],
        "body": "<!-- kew:depends 1 -->",
    },
    {
        "number": 3,
        "title": "In review",
        "url": "u3",
        "state": "OPEN",
        "stateReason": None,
        "labels": [{"name": "pr-pending-review"}],
        "body": "<!-- kew:depends 2 -->",
    },
]


def test_list_epic_nodes_derives_status():
    with patch(
        "kew.graph.adapters.github_convention.run",
        return_value=_result(json.dumps(ISSUES)),
    ):
        src = GitHubConventionSource(repo="o/r")
        nodes = src.list_epic_nodes("checkout")
    by_id = {n.id: n for n in nodes}
    assert by_id["1"].status is NodeStatus.DONE
    assert by_id["2"].status is NodeStatus.BLOCKED
    assert by_id["3"].status is NodeStatus.IN_REVIEW
    assert by_id["1"].url == "u1"


def test_list_edges_from_markers():
    with patch(
        "kew.graph.adapters.github_convention.run",
        return_value=_result(json.dumps(ISSUES)),
    ):
        src = GitHubConventionSource(repo="o/r")
        nodes = src.list_epic_nodes("checkout")
        edges = src.list_edges(nodes)
    assert Edge("1", "2") in edges
    assert Edge("2", "3") in edges


def test_list_edges_without_prior_list_epic_nodes_fails_loud():
    # list_edges depends on bodies cached by list_epic_nodes. Calling it out of
    # order (or for a stale node) must fail loudly, not silently drop edges.
    import pytest

    from kew.graph.domain import Node

    src = GitHubConventionSource(repo="o/r")
    stale = Node(id="42", title="t", status=NodeStatus.TODO, epic_ref="checkout")
    with pytest.raises(RuntimeError, match="list_edges"):
        src.list_edges([stale])


def test_list_epic_nodes_surfaces_gh_failure():
    # A real gh failure must raise, not masquerade as an empty epic.
    import pytest

    with patch(
        "kew.graph.adapters.github_convention.run",
        return_value=_result("", returncode=1),
    ):
        src = GitHubConventionSource(repo="o/r")
        with pytest.raises(RuntimeError, match="gh issue list failed"):
            src.list_epic_nodes("checkout")


def test_status_of_missing_returns_none():
    with patch(
        "kew.graph.adapters.github_convention.run",
        return_value=_result("", returncode=1),
    ):
        src = GitHubConventionSource(repo="o/r")
        assert src.status_of("999") is None


def test_status_of_closed_is_done():
    payload = json.dumps(
        {"number": 100, "state": "CLOSED", "stateReason": "COMPLETED", "labels": []}
    )
    with patch(
        "kew.graph.adapters.github_convention.run", return_value=_result(payload)
    ):
        src = GitHubConventionSource(repo="o/r")
        assert src.status_of("100") is NodeStatus.DONE


def test_status_of_closed_missing_state_reason_is_done_backward_compat():
    # Older fetches lacked `stateReason` entirely; treat missing as COMPLETED
    # rather than regressing every historical closed issue to BLOCKED.
    payload = json.dumps({"number": 100, "state": "CLOSED", "labels": []})
    with patch(
        "kew.graph.adapters.github_convention.run", return_value=_result(payload)
    ):
        src = GitHubConventionSource(repo="o/r")
        assert src.status_of("100") is NodeStatus.DONE


def test_status_of_closed_not_planned_is_blocked_not_done():
    # An external dependency closed "not planned" must NOT read as DONE --
    # a non-DONE status is required so it can never satisfy a dependent's
    # frontier check (the external-boundary-node NOT_PLANNED bug).
    payload = json.dumps(
        {"number": 100, "state": "CLOSED", "stateReason": "NOT_PLANNED", "labels": []}
    )
    with patch(
        "kew.graph.adapters.github_convention.run", return_value=_result(payload)
    ):
        src = GitHubConventionSource(repo="o/r")
        assert src.status_of("100") is NodeStatus.BLOCKED
