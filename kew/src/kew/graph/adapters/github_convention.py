"""GitHub convention adapter: epic:<name> label membership + kew:depends edges.

The only provider-aware code in the graph package. Uses the `gh` CLI. Produces
provider-derived NodeStatus (DONE/BLOCKED/IN_REVIEW/TODO); READY is graph-derived
by callers, and IN_PROGRESS/FAILED (kew-run-derived) are deferred to Phase 2.
"""

from __future__ import annotations

import json
from typing import Any

from kew.dependencies import (
    depends_marker_count,
    epic_container_name,
    parse_depends_ids,
)
from kew.graph.domain import Edge, Node, NodeStatus
from kew.subprocess_utils import run


class GitHubConventionSource:
    def __init__(
        self, repo: str, epic_prefix: str = "epic:", *, run_fn: Any | None = None
    ) -> None:
        self.repo = repo
        self.epic_prefix = epic_prefix
        self._run_fn = run_fn
        self._bodies: dict[str, str] = {}
        # Node ids whose body carries more than one kew:depends comment.
        self.multi_depends: list[str] = []

    def _run(self, cmd: list[str], **kwargs: Any) -> Any:
        return (run if self._run_fn is None else self._run_fn)(cmd, **kwargs)

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        label = f"{self.epic_prefix}{epic_ref}"
        result = self._run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repo,
                "--label",
                label,
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                "number,title,url,state,stateReason,labels,body",
            ],
            check=False,
        )
        # Surface a real gh failure (auth, network, rate limit) as an error
        # rather than masquerading it as an empty epic.
        if result.returncode != 0:
            raise RuntimeError(
                f"gh issue list failed (rc={result.returncode}): "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        try:
            issues: list[dict[str, Any]] = json.loads(result.stdout or "[]")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Failed to parse gh output: {exc}; stdout={result.stdout!r}"
            ) from exc
        nodes: list[Node] = []
        self._bodies = {}
        for it in issues:
            body = it.get("body") or ""
            if epic_container_name(body) is not None:
                continue
            nid = str(it["number"])
            self._bodies[nid] = body
            nodes.append(
                Node(
                    id=nid,
                    title=it.get("title", ""),
                    status=self._status_from_issue(it),
                    epic_ref=epic_ref,
                    url=it.get("url"),
                )
            )
        return nodes

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        # Edges come from the issue bodies fetched by list_epic_nodes. This is an
        # explicit ordering contract: list_epic_nodes MUST have been called for
        # these nodes first. Fail loud rather than silently dropping edges if a
        # body is missing (stale/out-of-order call).
        edges: list[Edge] = []
        self.multi_depends = []
        for n in nodes:
            if n.id not in self._bodies:
                raise RuntimeError(
                    f"list_edges: no cached body for node {n.id!r}; call "
                    "list_epic_nodes for this epic before list_edges."
                )
            if depends_marker_count(self._bodies[n.id]) > 1:
                self.multi_depends.append(n.id)
            for dep in parse_depends_ids(self._bodies[n.id]):
                edges.append(Edge(src=dep, dst=n.id))
        return edges

    def status_of(self, node_id: str) -> NodeStatus | None:
        result = self._run(
            [
                "gh",
                "issue",
                "view",
                node_id,
                "--repo",
                self.repo,
                "--json",
                "number,state,stateReason,labels",
            ],
            check=False,
        )
        # A non-zero rc here (notably a 404 for an unknown id) means "not
        # resolvable" -> None, which build_graph treats as a dangling reference.
        if result.returncode != 0:
            return None
        try:
            it = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError):
            return None
        return self._status_from_issue(it)

    @staticmethod
    def _status_from_issue(it: dict[str, Any]) -> NodeStatus:
        return status_from_issue(it)


def status_from_issue(it: dict[str, Any]) -> NodeStatus:
    """Provider-derived NodeStatus for one issue payload.

    Shared with the GitHub-native adapter (``github_native.py``), which changes
    which keys name an epic's members — not what a member's status means.
    """
    if str(it.get("state", "")).upper() == "CLOSED":
        # A CLOSED issue only means DONE if it was closed as completed.
        # A "not planned" (or any other non-completed) close reason must
        # NOT read as DONE -- otherwise an external (out-of-epic)
        # dependency closed "not planned" would wrongly satisfy a
        # dependent's frontier check. Missing `stateReason` (older
        # fetches predating this field) is treated as COMPLETED for
        # backward-compat.
        state_reason = it.get("stateReason")
        if state_reason is None or str(state_reason).upper() == "COMPLETED":
            return NodeStatus.DONE
        return NodeStatus.BLOCKED
    labels = {lbl.get("name") for lbl in it.get("labels", [])}
    if "blocked" in labels:
        return NodeStatus.BLOCKED
    if "pr-pending-review" in labels:
        return NodeStatus.IN_REVIEW
    return NodeStatus.TODO
