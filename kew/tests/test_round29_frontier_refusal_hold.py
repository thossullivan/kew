"""Round 29 contract regression, PR #1957.

A credential-issuer refusal raised in the driver process reported a StuckNode
and stopped there. The node stayed READY, so the next tick reserved it again,
refused it again, wrote another terminal refused run row, and chained another
audit event, without bound. The same refusal raised inside the detached child
ends the node on a terminal row, so the driver stops offering it. A parent-side
refusal now holds the node, so both sides of that boundary stop together.

#2343 collapsed the two cases into the parameters of one test; every
assertion survives.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kew import credential_issuer
from kew.config import AgentConfig, DispatchConfig
from kew.graph.domain import Graph, Node, NodeStatus
from kew.graph.driver import tick as tick_mod
from kew.graph.driver.facts import ReviewPressure
from kew.graph.driver.overlay import DriverStatus
from kew.run_store import SqliteRunStore


@pytest.fixture
def store():
    with SqliteRunStore(":memory:") as opened:
        yield opened


def _graph() -> Graph:
    return Graph(
        [Node(id="42", title="refused node", status=NodeStatus.TODO, epic_ref="alpha")],
        [],
    )


def _dispatch(store, refusal):
    graph = _graph()
    statuses = {"42": DriverStatus.TODO}
    config = DispatchConfig(agent=AgentConfig(model="openrouter/test"))
    with (
        patch.object(tick_mod, "reserve_and_dispatch", side_effect=refusal),
        patch.object(tick_mod, "remote_branch_exists", return_value=False),
    ):
        return tick_mod._dispatch_frontier(
            graph,
            graph,
            {"42": NodeStatus.TODO},
            statuses,
            provider="linear",
            reserved_item_ids=set(),
            already_stuck_ids=set(),
            name="alpha",
            store=store,
            dispatch_sink=MagicMock(),
            config=config,
            max_parallel=4,
            fleet_cap=4,
            branch_prefix="kew/",
            pressure=ReviewPressure(delivery_repo="owner/repo"),
            dependency_effects={},
            liveness=None,
        ), statuses


@pytest.mark.parametrize(
    ("refusal", "reason_fragments"),
    [
        pytest.param(
            credential_issuer.IssuerRefused(),
            ("credential_issuer_refused", credential_issuer.IssuerRefused.reason),
            id="credential-issuer",
        ),
        pytest.param(
            tick_mod._PreexistingWorktreeRefusal("dirty worktree already on disk"),
            ("preexisting_worktree_refused", "dirty worktree already on disk"),
            id="preexisting-worktree",
        ),
    ],
)
def test_a_parent_side_refusal_holds_the_node_with_its_own_reason(
    store, refusal, reason_fragments
):
    """Round 29: every parent-side refusal holds the node and names why."""
    outcome, statuses = _dispatch(store, refusal)

    assert [s.node_id for s in outcome.stuck] == ["42"], "the refusal is still reported"
    assert statuses["42"] is DriverStatus.HELD, "and the node stops being offered"

    holds = {h.item_id: h.reason for h in store.list_epic_holds("alpha")}
    assert "42" in holds, "the hold is durable, not only this tick's status"
    for fragment in reason_fragments:
        assert fragment in holds["42"]
