from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.frontier import (
    DEFAULT_PRIORITY,
    apply_review_backpressure,
    compute_frontier,
    frontier_rank,
    priority_of,
)


def _n(nid: str, status: NodeStatus = NodeStatus.TODO) -> Node:
    return Node(id=nid, title=nid, status=status, epic_ref="e")


def test_frontier_requires_all_deps_done() -> None:
    g = Graph(
        [_n("1", NodeStatus.DONE), _n("2"), _n("3")],
        [Edge("1", "2"), Edge("2", "3")],
    )
    status = {"1": NodeStatus.DONE, "2": NodeStatus.TODO, "3": NodeStatus.TODO}
    # 2's dep (1) is DONE -> 2 is ready; 3's dep (2) is not DONE -> excluded.
    assert compute_frontier(g, status=status, excluded=set()) == ["2"]


def test_frontier_fails_closed_on_dangling_dependency() -> None:
    g = Graph([_n("2"), _n("3")], [Edge("99", "2"), Edge("1", "3")])
    status = {"2": NodeStatus.TODO, "3": NodeStatus.TODO}
    # Both "2" (depends on missing "99") and "3" (depends on missing "1") stay OUT.
    assert compute_frontier(g, status=status, excluded=set()) == []


def test_frontier_excludes_ids_in_excluded_set() -> None:
    g = Graph(
        [_n("1", NodeStatus.DONE), _n("2"), _n("3")],
        [Edge("1", "2"), Edge("1", "3")],
    )
    status = {"1": NodeStatus.DONE, "2": NodeStatus.TODO, "3": NodeStatus.TODO}
    assert compute_frontier(g, status=status, excluded={"2"}) == ["3"]


def test_frontier_only_considers_raw_todo_status() -> None:
    # A node whose current status is not TODO (e.g. already DONE or IN_PROGRESS)
    # is never part of the frontier, even if all its deps are satisfied.
    g = Graph(
        [
            _n("1", NodeStatus.DONE),
            _n("2", NodeStatus.DONE),
            _n("3", NodeStatus.IN_PROGRESS),
        ],
        [Edge("1", "2"), Edge("1", "3")],
    )
    status = {
        "1": NodeStatus.DONE,
        "2": NodeStatus.DONE,
        "3": NodeStatus.IN_PROGRESS,
    }
    assert compute_frontier(g, status=status, excluded=set()) == []


def test_frontier_returns_sorted_ids() -> None:
    g = Graph(
        [_n("1", NodeStatus.DONE), _n("z"), _n("a")],
        [Edge("1", "z"), Edge("1", "a")],
    )
    status = {"1": NodeStatus.DONE, "z": NodeStatus.TODO, "a": NodeStatus.TODO}
    assert compute_frontier(g, status=status, excluded=set()) == ["a", "z"]


def test_queued_dependency_does_not_satisfy_frontier() -> None:
    g = Graph(
        [_n("1", NodeStatus.DONE), _n("2", NodeStatus.QUEUED), _n("3")],
        [Edge("2", "3")],
    )
    status = {"1": NodeStatus.DONE, "2": NodeStatus.QUEUED, "3": NodeStatus.TODO}

    assert compute_frontier(g, status=status, excluded=set()) == []


# ---------------------------------------------------------------------------
# I15: the human-review brake on the ready frontier.
# ---------------------------------------------------------------------------


def test_below_capacity_the_whole_frontier_is_admitted_in_order() -> None:
    # No throttle, no reordering, no residual penalty: below the cap the
    # ordinary frontier contract is the contract.
    admitted, withheld = apply_review_backpressure(
        ["1", "2", "3"], saturated=False, fresh_work={"1", "2", "3"}
    )
    assert admitted == ["1", "2", "3"]
    assert withheld == []


def test_saturation_withholds_only_fresh_independent_work() -> None:
    admitted, withheld = apply_review_backpressure(
        ["1", "2", "3", "4"], saturated=True, fresh_work={"2", "4"}
    )
    assert admitted == ["1", "3"]
    assert withheld == ["2", "4"]


def test_saturation_with_nothing_fresh_admits_everything() -> None:
    # Completion, recovery and remediation still move while people are the
    # constraint — saturation is not a stop-the-world.
    admitted, withheld = apply_review_backpressure(
        ["1", "2"], saturated=True, fresh_work=set()
    )
    assert admitted == ["1", "2"]
    assert withheld == []


def test_falling_below_capacity_restores_the_same_frontier_it_withheld() -> None:
    frontier = ["1", "2", "3"]
    fresh = {"1", "3"}

    _, withheld = apply_review_backpressure(frontier, saturated=True, fresh_work=fresh)
    restored, _ = apply_review_backpressure(frontier, saturated=False, fresh_work=fresh)

    assert withheld == ["1", "3"]
    # Nothing was consumed, evicted or demoted by being withheld.
    assert restored == frontier


# ---------------------------------------------------------------------------
# I12: an authorized no-change advances a dependency through ONE acknowledged
# dependency effect — never through a provider status shortcut.
# ---------------------------------------------------------------------------


def _no_change_graph() -> tuple[Graph, dict[str, NodeStatus]]:
    """Node "1" is a live (non-DONE) no-change; "2" and "3" both depend on it."""
    g = Graph(
        [_n("1", NodeStatus.BLOCKED), _n("2"), _n("3")],
        [Edge("1", "2"), Edge("1", "3")],
    )
    status = {
        "1": NodeStatus.BLOCKED,
        "2": NodeStatus.TODO,
        "3": NodeStatus.TODO,
    }
    return g, status


def test_a_no_change_dependency_without_an_effect_satisfies_nobody() -> None:
    g, status = _no_change_graph()
    assert compute_frontier(g, status=status, excluded=set()) == []
    assert (
        compute_frontier(g, status=status, excluded=set(), dependency_effects={}) == []
    )


def test_an_acked_dependency_effect_admits_only_its_own_dependent() -> None:
    g, status = _no_change_graph()
    frontier = compute_frontier(
        g,
        status=status,
        excluded=set(),
        dependency_effects={"1": frozenset({"2"})},
    )
    assert frontier == ["2"]


def test_a_dependency_effect_for_one_edge_does_not_satisfy_another() -> None:
    """The effect names a dependent, not a status: it travels no further."""
    g = Graph(
        [_n("1", NodeStatus.BLOCKED), _n("2"), _n("3")],
        [Edge("1", "2"), Edge("2", "3")],
    )
    status = {
        "1": NodeStatus.BLOCKED,
        "2": NodeStatus.TODO,
        "3": NodeStatus.TODO,
    }
    frontier = compute_frontier(
        g,
        status=status,
        excluded=set(),
        dependency_effects={"1": frozenset({"2"})},
    )
    assert frontier == ["2"]


def test_a_dependency_effect_naming_an_unknown_dependency_stays_closed() -> None:
    """Fail-closed still wins: a dangling dep is not satisfiable by an effect."""
    g = Graph([_n("2")], [Edge("99", "2")])
    status = {"2": NodeStatus.TODO}
    assert (
        compute_frontier(
            g,
            status=status,
            excluded=set(),
            dependency_effects={"99": frozenset({"2"})},
        )
        == []
    )


# --- Frontier order: priority, then blast radius, then id (#2575) ----------


def _ready(*nids: str, edges: list[Edge] | None = None) -> tuple[Graph, dict]:
    g = Graph([_n(n) for n in nids], edges or [])
    return g, {n: NodeStatus.TODO for n in nids}


def test_a_p1_node_dispatches_before_a_lower_numbered_p2() -> None:
    g, status = _ready("100", "200")
    frontier = compute_frontier(
        g, status=status, excluded=set(), priorities={"100": 2, "200": 1}
    )
    assert frontier == ["200", "100"]


def test_equal_priority_the_larger_blast_radius_goes_first() -> None:
    # 300 unblocks 301 and (through it) 302; 100 unblocks nothing.
    g = Graph(
        [_n("100"), _n("300"), _n("301"), _n("302")],
        [Edge("300", "301"), Edge("301", "302")],
    )
    status = {n: NodeStatus.TODO for n in g.nodes}
    frontier = compute_frontier(
        g, status=status, excluded=set(), priorities={"100": 1, "300": 1}
    )
    assert frontier == ["300", "100"]


def test_equal_priority_and_blast_the_lower_number_goes_first() -> None:
    # Numeric, not lexical: 99 before 100.
    g, status = _ready("100", "99")
    assert compute_frontier(
        g, status=status, excluded=set(), priorities={"100": 1, "99": 1}
    ) == ["99", "100"]


def test_priority_outranks_blast_radius() -> None:
    g = Graph([_n("1"), _n("2"), _n("3")], [Edge("1", "3")])
    status = {n: NodeStatus.TODO for n in g.nodes}
    frontier = compute_frontier(
        g, status=status, excluded=set(), priorities={"1": 2, "2": 0}
    )
    assert frontier == ["2", "1"]


def test_an_unlabelled_node_ranks_as_p2() -> None:
    assert priority_of(()) == DEFAULT_PRIORITY == 2
    assert priority_of({"bug", "python", "perf"}) == 2
    # Ranked between a p1 and a p3, tied with a p2 (so the number decides).
    g, status = _ready("1", "2", "3", "4")
    frontier = compute_frontier(
        g,
        status=status,
        excluded=set(),
        priorities={"1": 3, "2": 2, "4": 1},  # 3 has no priority entry at all
    )
    assert frontier == ["4", "2", "3", "1"]


def test_priority_labels_are_read_from_the_plan_vocabulary() -> None:
    assert priority_of({"p0-critical"}) == 0
    assert priority_of({"p1-high", "enhancement"}) == 1
    assert priority_of({"p3-low"}) == 3
    assert priority_of({"P1"}) == 1
    # Two priority labels: the most urgent wins.
    assert priority_of({"p3-low", "p1-high"}) == 1


def test_frontier_rank_describes_priority_and_blast() -> None:
    g = Graph([_n("2570"), _n("2571")], [Edge("2570", "2571")])
    rank = frontier_rank(g, "2570", {"2570": 1})
    assert (rank.priority, rank.blast) == (1, 1)
    assert rank.describe() == "#2570 p1, blast 1"
