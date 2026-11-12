"""ReconcilingSource: the W2 (#894) reconcile short-circuit over a
DependencySource.

Once Linear webhooks are delivering (rows in ``tracker_events``), a driver tick
/ ``kew epic status`` skips the GraphQL fetch when nothing changed, falling back
to a full fetch only on a fresh event or a generous reconcile interval. The
cold path (no events ever) fetches every time — byte-for-byte today's polling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kew.graph.adapters.linear_native import (
    ReconcilingSource,
    _graph_from_json,
    _graph_to_json,
)
from kew.graph.build import build_graph
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.run_store import SqliteRunStore, TrackerEvent


@pytest.fixture
def store(tmp_path: Path) -> SqliteRunStore:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


class _FakeSource:
    """A DependencySource that counts fetches; edge 'EXT' resolves to an
    external boundary node so build_graph exercises status_of too."""

    def __init__(self) -> None:
        self.node_calls = 0
        self.status_calls = 0

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        self.node_calls += 1
        return [
            Node(id="A", title="a", status=NodeStatus.TODO, epic_ref=epic_ref),
            Node(id="B", title="b", status=NodeStatus.DONE, epic_ref=epic_ref),
        ]

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        return [Edge(src="A", dst="B"), Edge(src="EXT", dst="A")]

    def status_of(self, node_id: str) -> NodeStatus | None:
        self.status_calls += 1
        return NodeStatus.DONE if node_id == "EXT" else None


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _record_event(
    store: SqliteRunStore, delivery_id: str, source: str = "linear"
) -> None:
    store.record_tracker_event(
        TrackerEvent(
            delivery_id=delivery_id,
            source=source,
            event_type="Issue",
            action="update",
            entity_id="x",
            webhook_timestamp=None,
            payload="{}",
        )
    )


# --- serialization round-trip -------------------------------------------------


def test_graph_json_round_trips_nodes_edges_and_external() -> None:
    graph = Graph(
        [
            Node(id="A", title="a", status=NodeStatus.TODO, epic_ref="E", url="u"),
            Node(
                id="EXT",
                title="(external)",
                status=NodeStatus.DONE,
                epic_ref="E",
                external=True,
            ),
        ],
        [Edge(src="EXT", dst="A")],
    )
    restored = _graph_from_json(_graph_to_json(graph))
    assert set(restored.nodes) == {"A", "EXT"}
    assert restored.nodes["EXT"].external is True
    assert restored.nodes["EXT"].status is NodeStatus.DONE
    assert restored.nodes["A"].url == "u"
    assert [(e.src, e.dst) for e in restored.edges] == [("EXT", "A")]


# --- cold path (no webhooks) --------------------------------------------------


def test_cold_path_fetches_every_call(store: SqliteRunStore) -> None:
    """No tracker_events ever recorded -> every build fetches, unchanged."""
    inner = _FakeSource()
    src = ReconcilingSource(inner, store=store, reconcile_interval_seconds=600)

    g1 = build_graph(src, "E")
    g2 = build_graph(src, "E")

    assert inner.node_calls == 2  # no short-circuit on the cold path
    assert set(g1.nodes) == set(g2.nodes) == {"A", "B", "EXT"}


# --- warm path: event-driven --------------------------------------------------


def test_warm_path_reuses_cache_when_nothing_changed(store: SqliteRunStore) -> None:
    _record_event(store, "d0")  # a webhook has delivered -> warm
    clock = _Clock(datetime.now(UTC))
    inner = _FakeSource()
    src = ReconcilingSource(
        inner, store=store, reconcile_interval_seconds=600, clock=clock
    )

    g1 = build_graph(src, "E")
    calls_after_first = inner.node_calls
    g2 = build_graph(src, "E")  # no new event, within window -> reuse

    assert inner.node_calls == calls_after_first  # inner NOT re-fetched
    assert set(g2.nodes) == {"A", "B", "EXT"}
    assert g2.nodes["EXT"].external is True
    assert [(e.src, e.dst) for e in g1.edges] == [(e.src, e.dst) for e in g2.edges]


def test_warm_path_refetches_after_new_event(store: SqliteRunStore) -> None:
    _record_event(store, "d0")
    clock = _Clock(datetime.now(UTC))
    inner = _FakeSource()
    src = ReconcilingSource(
        inner, store=store, reconcile_interval_seconds=600, clock=clock
    )

    build_graph(src, "E")
    calls = inner.node_calls
    _record_event(store, "d1")  # fresh delivery after the cached fetch
    build_graph(src, "E")

    assert inner.node_calls == calls + 1  # event forced a re-fetch


# --- warm path: reconcile interval heals a missed delivery --------------------


def test_reconcile_interval_forces_refetch(store: SqliteRunStore) -> None:
    _record_event(store, "d0")
    clock = _Clock(datetime.now(UTC))
    inner = _FakeSource()
    src = ReconcilingSource(
        inner, store=store, reconcile_interval_seconds=600, clock=clock
    )

    build_graph(src, "E")
    calls = inner.node_calls
    clock.t = clock.t + timedelta(seconds=601)  # window elapsed, no new event
    build_graph(src, "E")

    assert inner.node_calls == calls + 1


def test_zero_interval_disables_short_circuit(store: SqliteRunStore) -> None:
    _record_event(store, "d0")
    clock = _Clock(datetime.now(UTC))
    inner = _FakeSource()
    src = ReconcilingSource(
        inner, store=store, reconcile_interval_seconds=0, clock=clock
    )

    build_graph(src, "E")
    build_graph(src, "E")

    assert inner.node_calls == 2  # always fetch when disabled


# --- status_of / list_edges are served from the cached graph ------------------


def test_status_of_served_from_cache_without_inner_call(store: SqliteRunStore) -> None:
    _record_event(store, "d0")
    clock = _Clock(datetime.now(UTC))
    inner = _FakeSource()
    src = ReconcilingSource(
        inner, store=store, reconcile_interval_seconds=600, clock=clock
    )

    build_graph(src, "E")  # first fetch populates the in-memory graph
    calls = inner.node_calls
    build_graph(src, "E")  # reuse; outer build_graph asks status_of("EXT")

    # The reused snapshot answers status_of from memory: no second fetch, and
    # the external boundary node keeps its resolved status.
    assert inner.node_calls == calls
    assert src.status_of("EXT") is NodeStatus.DONE
    assert src.status_of("missing") is None


def test_list_edges_before_list_epic_nodes_raises(store: SqliteRunStore) -> None:
    src = ReconcilingSource(_FakeSource(), store=store, reconcile_interval_seconds=600)
    with pytest.raises(RuntimeError, match="no resolved graph"):
        src.list_edges([])


# --- run_store accessors (W2 read side) ---------------------------------------


def test_has_tracker_events_since_scopes_by_source_and_time(
    store: SqliteRunStore,
) -> None:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert store.has_tracker_events_since(epoch) is False  # cold
    _record_event(store, "d0", source="linear")
    assert store.has_tracker_events_since(epoch) is True
    assert store.has_tracker_events_since(epoch, source="linear") is True
    assert store.has_tracker_events_since(epoch, source="github") is False
    future = datetime.now(UTC) + timedelta(days=1)
    assert store.has_tracker_events_since(future, source="linear") is False


def test_epic_graph_cache_upserts(store: SqliteRunStore) -> None:
    assert store.get_epic_graph_cache("E") is None
    t0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    store.put_epic_graph_cache("E", "linear", t0, '{"nodes": [], "edges": []}')
    got = store.get_epic_graph_cache("E")
    assert got is not None
    assert got[0] == t0
    assert got[1] == '{"nodes": [], "edges": []}'
    t1 = t0 + timedelta(minutes=5)
    store.put_epic_graph_cache("E", "linear", t1, '{"nodes": [1], "edges": []}')
    got2 = store.get_epic_graph_cache("E")
    assert got2 == (t1, '{"nodes": [1], "edges": []}')  # replaced, not duplicated
