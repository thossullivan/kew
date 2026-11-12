"""Linear-native adapter: epic = Linear Project, edges = native blocks relations.

Second implementation of the DependencySource port (GitHub convention adapter
is the first). Provider-derived statuses only — DONE/BLOCKED/IN_REVIEW/
TODO/QUEUED —
mirroring the GitHub adapter's contract: READY is graph-derived by callers and
IN_PROGRESS/FAILED are kew-run-derived, never a provider fact. Canceled issues
map to BLOCKED (fails closed: a canceled dependency must never read as
satisfied) until a first-class cancelled state exists in the domain.

All queries pass ``includeArchived: true`` — Linear auto-archives completed
issues, and an archived-but-completed dependency is satisfied, not dangling.

Relations are fetched in a separate aliased by-UUID batch, NOT nested under
the project query: the live API (dogfood 2026-07-01) returns empty
relations/inverseRelations from the project-nested and identifier-shorthand
projections even when the relation exists — only ``issue(id: <UUID>)``
resolves them reliably.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Epics+graph.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from kew.graph.build import build_graph
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.port import DependencySource
from kew.linear.client import LinearApiError, LinearClient

if TYPE_CHECKING:
    from kew.run_store import RunStore

# Oldest received_at any tracker_events row can carry — passed as the "since"
# bound to answer the cold-path question "have webhooks EVER delivered for this
# source?" (see ReconcilingSource._can_reuse).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


_PROJECTS_QUERY = """
query Projects($name: String!) {
  projects(filter: { name: { eq: $name } }, first: 5, includeArchived: true) {
    nodes { id name }
  }
}
"""

# Nested-connection page sizes multiply GraphQL complexity (a single query is
# hard-capped at 10,000 points). Keep the project, label, and hierarchy page
# bounds explicit and small; relations use a separate bounded batch below.
_ISSUES_QUERY = """
query ProjectIssues($projectId: String!, $after: String) {
  project(id: $projectId) {
    issues(first: 50, after: $after, includeArchived: true) {
      nodes {
        id
        identifier
        title
        url
        state { name type }
        labels(first: 50) { nodes { name } }
        children(first: 50, includeArchived: true) {
          nodes { identifier }
          pageInfo { hasNextPage endCursor }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

# Aliased per-issue fragment for the relations batch. GraphQL variables cannot
# parametrize alias fan-out, so validated UUIDs are embedded directly.
_RELATIONS_FRAGMENT = """{
  identifier
  relations(first: 15) { nodes { type relatedIssue { identifier } } }
  inverseRelations(first: 15) { nodes { type issue { identifier } } }
}"""

_RELATIONS_BATCH_SIZE = 20

_UUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,40}$")

_ISSUE_STATUS_QUERY = """
query IssueStatus($id: String!) {
  issue(id: $id) {
    identifier
    state { name type }
  }
}
"""

_MEMBER_QUERY = """
query Members($name: String!) {
  users(filter: { name: { eq: $name } }, first: 5) {
    nodes { id name }
  }
}
"""

# Resolve the delegation trigger label NAME to its UUID once at startup so the
# fast local read can decide human-assignee churn on a labeled issue by
# comparing the webhook payload's label UUIDs — never fetch a live snapshot (N4).
_LABEL_QUERY = """
query IssueLabelByName($name: String!) {
  issueLabels(filter: { name: { eq: $name } }, first: 5) {
    nodes { id name }
  }
}
"""

# Poll floor for the delegation watcher (D3): issues delegated to the kew seat
# either by assignee OR by the configured label. Not archived — an archived
# issue is completed/canceled, never actionable. State/actionability is left to
# recognition (D2); this query is the throttled remote half only.
_DELEGATED_QUERY = """
query Delegated($assigneeId: ID!, $label: String!, $after: String) {
  issues(
    filter: {
      or: [
        { assignee: { id: { eq: $assigneeId } } }
        { labels: { some: { name: { eq: $label } } } }
      ]
    }
    first: 50
    after: $after
    includeArchived: false
  ) {
    nodes { identifier }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_LINEAR_EPIC_LABEL = "kew"
_LINEAR_READY_LABEL = "kew:ready"


def is_linear_epic_admitted(
    state: dict[str, Any],
    labels: set[str] | frozenset[str],
    *,
    project_id: str | None,
) -> bool:
    """Return whether a live Linear item belongs to the epic rail.

    Project membership is only a possible epic population. F1's two explicit
    markers and an unstarted state are required before the item owns the epic
    rail. This is shared by graph admission and delegation recognition.
    """
    return (
        bool(project_id)
        and str(state.get("type", "")) == "unstarted"
        and {_LINEAR_EPIC_LABEL, _LINEAR_READY_LABEL}.issubset(labels)
    )


@dataclass(frozen=True)
class LinearContainer:
    """Bounded diagnostic metadata for a non-dispatchable parent issue."""

    id: str
    title: str
    url: str | None
    child_ids: tuple[str, ...]
    children_truncated: bool


def linear_admission_status(
    state: dict[str, Any], labels: set[str] | frozenset[str]
) -> NodeStatus:
    """Return the provider-neutral admission fact for one Linear issue.

    Project membership alone is only a queue. The issue must be in Linear's
    unstarted/Todo state and carry both the ``kew`` membership marker and the
    human arm marker. Unknown or non-dispatchable workflow states fail closed
    to QUEUED; terminal states return TODO as an irrelevant sentinel because
    their ordinary terminal status takes precedence.
    """
    stype = str(state.get("type", ""))
    if stype in {"completed", "canceled"}:
        return NodeStatus.TODO
    if is_linear_epic_admitted(state, labels, project_id="project"):
        return NodeStatus.TODO
    return NodeStatus.QUEUED


class LinearSource:
    def __init__(
        self, client: LinearClient, in_review_state: str = "In Review"
    ) -> None:
        self._client = client
        self._in_review_state = in_review_state
        # identifier -> raw relation payloads, cached by list_epic_nodes for
        # list_edges (same ordering contract as the GitHub adapter's bodies).
        self._relations: dict[str, dict[str, Any]] = {}
        # Parent/container issues are project metadata, not work-graph members.
        # Keep their bounded hierarchy facts available to callers for diagnostics
        # without putting them in Graph.nodes or the dispatch frontier.
        self.excluded_containers: dict[str, LinearContainer] = {}

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        self.excluded_containers = {}
        projects = self._client.execute(_PROJECTS_QUERY, {"name": epic_ref})[
            "projects"
        ]["nodes"]
        if not projects:
            raise RuntimeError(f"No Linear project named {epic_ref!r}.")
        if len(projects) > 1:
            raise RuntimeError(
                f"Ambiguous epic ref {epic_ref!r}: "
                f"{len(projects)} Linear projects match."
            )
        project_id = projects[0]["id"]

        nodes: list[Node] = []
        uuids: dict[str, str] = {}  # identifier -> uuid
        after: str | None = None
        while True:
            page = self._client.execute(
                _ISSUES_QUERY, {"projectId": project_id, "after": after}
            )["project"]["issues"]
            for it in page["nodes"]:
                identifier = it["identifier"]
                container = self._container_from_issue(it)
                if container is not None:
                    self.excluded_containers[identifier] = container
                    continue
                uuids[identifier] = it["id"]
                labels = self._labels_of(it)
                nodes.append(
                    Node(
                        id=identifier,
                        title=it.get("title", ""),
                        status=self._status_from_state(
                            it.get("state") or {}, labels=labels
                        ),
                        epic_ref=epic_ref,
                        url=it.get("url"),
                    )
                )
            info = page["pageInfo"]
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")

        self._relations = self._fetch_relations(uuids)
        return nodes

    def _fetch_relations(self, uuids: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Batch-fetch relations via aliased issue(id: <UUID>) lookups."""
        out: dict[str, dict[str, Any]] = {}
        items = list(uuids.items())
        for start in range(0, len(items), _RELATIONS_BATCH_SIZE):
            chunk = items[start : start + _RELATIONS_BATCH_SIZE]
            parts = []
            for i, (_ident, uuid) in enumerate(chunk):
                if not _UUID_RE.fullmatch(uuid):
                    raise RuntimeError(f"Unexpected issue id from Linear: {uuid!r}")
                parts.append(f'i{i}: issue(id: "{uuid}") {_RELATIONS_FRAGMENT}')
            query = "query RelationsBatch {\n" + "\n".join(parts) + "\n}"
            # GraphQL (not SQL) via LinearClient; embedded ids are _UUID_RE-
            # validated. Suppresses sqlalchemy-execute-raw-query (pattern FP).
            data = self._client.execute(query)  # nosemgrep
            for i in range(len(chunk)):
                issue = data.get(f"i{i}")
                if issue is None:
                    continue
                out[issue["identifier"]] = {
                    "relations": issue.get("relations", {}).get("nodes", []),
                    "inverse": issue.get("inverseRelations", {}).get("nodes", []),
                }
        return out

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        # The same relation is reported from BOTH endpoints (forward on the
        # blocker, inverse on the blocked), so dedupe by (src, dst).
        edges: list[Edge] = []
        seen: set[tuple[str, str]] = set()
        excluded_ids = set(self.excluded_containers)

        def add(src: str, dst: str) -> None:
            # Hierarchy is organizational metadata. If a native blocks relation
            # happens to point at a filtered container, omitting that relation
            # prevents it becoming a false dangling dependency while preserving
            # every edge whose endpoints are actual work or external issues.
            if src in excluded_ids or dst in excluded_ids:
                return
            if (src, dst) not in seen:
                seen.add((src, dst))
                edges.append(Edge(src=src, dst=dst))

        for n in nodes:
            if n.id not in self._relations:
                raise RuntimeError(
                    f"list_edges: no cached relations for node {n.id!r}; call "
                    "list_epic_nodes for this epic before list_edges."
                )
            rel = self._relations[n.id]
            for r in rel["relations"]:
                # n blocks relatedIssue -> the related issue depends on n.
                if r.get("type") == "blocks" and r.get("relatedIssue"):
                    add(n.id, r["relatedIssue"]["identifier"])
            for r in rel["inverse"]:
                # issue blocks n -> n depends on that issue.
                if r.get("type") == "blocks" and r.get("issue"):
                    add(r["issue"]["identifier"], n.id)
        return edges

    def status_of(self, node_id: str) -> NodeStatus | None:
        try:
            issue = self._client.execute(_ISSUE_STATUS_QUERY, {"id": node_id})["issue"]
        except LinearApiError as exc:
            if exc.not_found:
                return None
            raise  # auth/network failures must not read as "dangling"
        if issue is None:
            return None
        # Out-of-epic dependencies are not admitted through this project's
        # human-arm gate. Their provider status remains state-only.
        return self._status_from_state(issue.get("state") or {}, admission=False)

    def resolve_member_id(self, name: str) -> str:
        """Resolve a Linear member name to its member id (KEW_MEMBER_ID, D2).

        Used once to turn ``DelegationConfig.assignee`` into the exact assignee
        id recognition matches against. Raises on no match / ambiguous match so
        a misconfigured seat name fails loud rather than silently claiming
        nothing (mirrors ``list_epic_nodes``' project-ref resolution).
        """
        users = self._client.execute(_MEMBER_QUERY, {"name": name})["users"]["nodes"]
        if not users:
            raise RuntimeError(f"No Linear member named {name!r}.")
        if len(users) > 1:
            raise RuntimeError(
                f"Ambiguous member name {name!r}: {len(users)} Linear members match."
            )
        return str(users[0]["id"])

    def resolve_label_id(self, name: str) -> str | None:
        """Resolve the delegation trigger label name to its UUID, or None (N4).

        The fast local read compares this id against the webhook payload's label
        UUIDs to decide human-assignee churn on a labeled issue locally (zero
        live fetches, never pinning the cursor). Returns None on a missing or
        ambiguous name so the watcher falls back to the conservative
        assignee-only local filter rather than mis-deciding a real delegation.
        """
        nodes = self._client.execute(_LABEL_QUERY, {"name": name})["issueLabels"][
            "nodes"
        ]
        if len(nodes) != 1:
            return None
        return str(nodes[0]["id"])

    def list_delegated(self, assignee_id: str, label: str) -> list[str]:
        """Poll floor for the delegation watcher (D3): issues currently
        delegated to kew (assignee id OR the configured label).

        Returns canonical Linear identifiers so the dispatch child receives
        the same durable id used by the rest of the Linear execution vertical.
        Assignee/label filtering happens server-side; actionable-state
        filtering is recognition's job (D2).
        """
        ids: list[str] = []
        after: str | None = None
        while True:
            page = self._client.execute(
                _DELEGATED_QUERY,
                {"assigneeId": assignee_id, "label": label, "after": after},
            )["issues"]
            for it in page["nodes"]:
                ids.append(it["identifier"])
            info = page["pageInfo"]
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        return ids

    @staticmethod
    def _labels_of(issue: dict[str, Any]) -> frozenset[str]:
        nodes = (issue.get("labels") or {}).get("nodes") or []
        return frozenset(
            label.get("name", "")
            for label in nodes
            if isinstance(label, dict) and label.get("name")
        )

    @staticmethod
    def _container_from_issue(issue: dict[str, Any]) -> LinearContainer | None:
        """Return explicit bounded hierarchy metadata for a parent issue.

        A non-empty ``children`` connection is the provider fact that an issue
        is an organizer/container. The first page is intentionally bounded: we
        only need to know whether a child exists, and ``hasNextPage`` preserves
        that fact for a parent with more children than the metadata page size.
        Title text and naming conventions are never used for classification.
        """
        children = issue.get("children") or {}
        child_nodes = children.get("nodes") or []
        page_info = children.get("pageInfo") or {}
        if not child_nodes and not page_info.get("hasNextPage"):
            return None
        child_ids = tuple(
            str(child.get("identifier"))
            for child in child_nodes
            if isinstance(child, dict) and child.get("identifier")
        )
        return LinearContainer(
            id=str(issue["identifier"]),
            title=str(issue.get("title", "")),
            url=issue.get("url"),
            child_ids=child_ids,
            children_truncated=bool(page_info.get("hasNextPage")),
        )

    def _status_from_state(
        self,
        state: dict[str, Any],
        *,
        labels: set[str] | frozenset[str] = frozenset(),
        admission: bool = True,
    ) -> NodeStatus:
        stype = str(state.get("type", ""))
        if stype == "completed":
            return NodeStatus.DONE
        if stype == "canceled":
            return NodeStatus.BLOCKED
        if stype == "started" and state.get("name") == self._in_review_state:
            return NodeStatus.IN_REVIEW
        if not admission:
            return NodeStatus.TODO
        return linear_admission_status(state, labels)


def _graph_to_json(graph: Graph) -> str:
    """Serialize a provider Graph for the epic_graph_cache (W2). Only the
    provider-derived fields are stored — the kew-run overlay (IN_PROGRESS/
    FAILED, pr_url, last_run_id) is re-applied downstream from run history, so
    caching it would be both redundant and a staleness hazard."""
    return json.dumps(
        {
            "nodes": [
                {
                    "id": n.id,
                    "title": n.title,
                    "status": n.status.value,
                    "epic_ref": n.epic_ref,
                    "url": n.url,
                    "external": n.external,
                }
                for n in graph.nodes.values()
            ],
            "edges": [[e.src, e.dst] for e in graph.edges],
        },
        sort_keys=True,
    )


def _graph_from_json(raw: str) -> Graph:
    data = json.loads(raw)
    nodes = [
        Node(
            id=n["id"],
            title=n["title"],
            status=NodeStatus(n["status"]),
            epic_ref=n["epic_ref"],
            url=n.get("url"),
            external=n.get("external", False),
        )
        for n in data["nodes"]
    ]
    edges = [Edge(src=src, dst=dst) for src, dst in data["edges"]]
    return Graph(nodes, edges)


class ReconcilingSource(DependencySource):
    """Wraps a DependencySource so the expensive provider fetch runs only when
    ``tracker_events`` show something changed or a reconcile window has elapsed
    (W2/#894). Every ``build_graph`` call routes through ONE fetch decision:

    * **Cold path** — no webhook has ever delivered for this ``source`` (the
      ``tracker_events`` table has no matching row): the cache is never trusted
      and every call fetches, byte-for-byte today's polling behavior.
    * **Warm path** — once at least one delivery has landed: the cached graph is
      replayed unless (a) the reconcile interval has elapsed since the last
      fetch (heals a missed delivery) or (b) a fresh event arrived since. A
      non-positive ``reconcile_interval_seconds`` disables the short-circuit outright.

    On a cache hit the whole cached graph — epic nodes, resolved external
    boundary nodes, and edges — is served from memory, so ``list_edges`` and
    ``status_of`` issue no further calls. The inner source is only ever driven
    through ``build_graph`` (which fetches nodes, edges, and externals), so the
    cache is always a complete, self-consistent snapshot.
    """

    def __init__(
        self,
        inner: DependencySource,
        *,
        store: RunStore,
        reconcile_interval_seconds: int,
        source: str = "linear",
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._inner = inner
        self._store = store
        self._reconcile_interval_seconds = reconcile_interval_seconds
        self._source = source
        self._clock = clock
        self._graph: Graph | None = None

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        self._graph = self._resolve_graph(epic_ref)
        return [n for n in self._graph.nodes.values() if not n.external]

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        if self._graph is None:
            raise RuntimeError(
                "list_edges: no resolved graph; call list_epic_nodes first."
            )
        return list(self._graph.edges)

    def status_of(self, node_id: str) -> NodeStatus | None:
        # After list_epic_nodes the cached graph holds every resolvable node
        # (epic members + external boundary nodes); an id absent from it was
        # unresolvable at fetch time, i.e. None — mirroring build_graph's own
        # omit-when-None contract. So the reused snapshot answers without a call.
        if self._graph is None:
            return self._inner.status_of(node_id)
        node = self._graph.nodes.get(node_id)
        return node.status if node is not None else None

    def _resolve_graph(self, epic_ref: str) -> Graph:
        cached = self._store.get_epic_graph_cache(epic_ref)
        if cached is not None and self._can_reuse(cached[0]):
            return _graph_from_json(cached[1])
        fetched_at = self._clock()
        graph = build_graph(self._inner, epic_ref)
        self._store.put_epic_graph_cache(
            epic_ref, self._source, fetched_at, _graph_to_json(graph)
        )
        return graph

    def _can_reuse(self, fetched_at: datetime) -> bool:
        if self._reconcile_interval_seconds <= 0:
            return False  # short-circuit disabled -> always fetch.
        # Cold path: trust the cache only once a webhook has ever delivered.
        if not self._store.has_tracker_events_since(_EPOCH, source=self._source):
            return False
        # Reconcile: force a full fetch at least every interval to heal a
        # webhook delivery that never arrived.
        if self._clock() - fetched_at >= timedelta(
            seconds=self._reconcile_interval_seconds
        ):
            return False
        # Event-driven: a delivery since the last fetch means state moved.
        return not self._store.has_tracker_events_since(fetched_at, source=self._source)
