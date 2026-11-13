"""GitHub-native adapter: sub-issue membership + `blocked_by` dependency edges.

The convention adapter keys membership on the `epic:<name>` label and edges on
the `<!-- kew:depends -->` HTML comment — both prose an operator can rewrite
from outside kew, and both invisible to GitHub's own UI. GitHub now ships the
same primitives Linear always had (sub-issues and issue dependencies), so this
adapter reads those instead: one GraphQL query per epic per tick returns
membership, state, labels, `blockedBy` and the closing PR references for every
node, replacing the per-node `gh issue view` fan-out.

Neither key is mandatory. A repository whose API does not expose the fields
(GHES) fails the capability probe and gets `GitHubConventionSource`; an epic
whose nodes have no marked parent issue falls back to the label query. During
the one-release write overlap, edge reads union `blockedBy` relations with body
markers. A partial native write therefore cannot lose a blocker.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from kew.dependencies import (
    depends_marker_count,
    epic_container_name,
    parse_depends_ids,
)
from kew.graph.adapters.github_convention import (
    GitHubConventionSource,
    status_from_issue,
)
from kew.graph.domain import Edge, Node, NodeStatus
from kew.progress import _log
from kew.subprocess_utils import run_with_backoff as _default_run

_logger = logging.getLogger(__name__)

# GitHub caps a parent at 100 sub-issues and an issue at 50 `blocked_by`
# relations; both queries ask for the cap so a full epic is one page.
_SUB_ISSUE_PAGE = 100
_BLOCKED_BY_PAGE = 50
_LABEL_PAGE = 100
# A bound on the label query's pagination: the convention adapter reads up to
# 1000 labelled issues, so match it rather than silently reading fewer.
_MAX_PAGES = 10

_NODE_FIELDS = f"""
      number
      title
      url
      state
      stateReason
      body
      repository {{ nameWithOwner }}
      labels(first: 50) {{ nodes {{ name }} }}
      blockedBy(first: {_BLOCKED_BY_PAGE}) {{
        nodes {{ number repository {{ nameWithOwner }} }}
      }}
      closedByPullRequestsReferences(first: 5, includeClosedPrs: true) {{
        nodes {{ number url state }}
      }}
"""

_SUB_ISSUES_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  rateLimit {{ cost }}
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      subIssues(first: {_SUB_ISSUE_PAGE}, after: $after) {{
        nodes {{{_NODE_FIELDS}}}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
  }}
}}
"""

_LABEL_QUERY = f"""
query($owner: String!, $name: String!, $label: String!, $after: String) {{
  rateLimit {{ cost }}
  repository(owner: $owner, name: $name) {{
    issues(first: {_LABEL_PAGE}, after: $after, labels: [$label],
           states: [OPEN, CLOSED]) {{
      nodes {{{_NODE_FIELDS}
        parent {{
          number
          body
          repository {{ nameWithOwner }}
        }}
        subIssues(first: 1) {{ totalCount }}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

_PROBE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      blockedBy(first: 1) { totalCount }
      subIssues(first: 1) { totalCount }
    }
  }
}
"""


@dataclass(frozen=True)
class LinkageProbe:
    """Whether this API exposes GitHub-native linkage, and why we think so."""

    available: bool
    reason: str

    def describe(self) -> str:
        key = (
            "sub-issues + blocked_by relations"
            if self.available
            else "epic label + kew:depends marker"
        )
        return f"Linkage: {key} — {self.reason}"


_probe_cache: dict[str, LinkageProbe] = {}
_FIELD_MISSING_RE = re.compile(
    r"Field '(blockedBy|subIssues)' doesn't exist on type 'Issue'"
)
# GitHub's real undefined-field error (verified live) carries no `type`; it has
# extensions.code == "undefinedField" plus extensions.fieldName. Match that
# shape or the exact message, nothing looser.
_LINKAGE_FIELDS = {"blockedBy", "subIssues"}


def reset_linkage_probe_cache() -> None:
    """Drop the per-process probe cache (tests; a long-lived driver re-probes
    only by restarting, which is the "once per driver run" contract)."""
    _probe_cache.clear()


def probe_native_linkage(
    repo: str, issue_number: int, *, run: Any | None = None
) -> LinkageProbe:
    """Probe native linkage on one issue, once per repository and process.

    Only a GraphQL error that names a linkage field selects the convention
    adapter. Transport, authentication, rate-limit, and other failures raise.
    """
    cached = _probe_cache.get(repo)
    if cached is not None:
        return cached
    probe = _probe(repo, issue_number, run=_default_run if run is None else run)
    _probe_cache[repo] = probe
    _logger.info("github linkage probe (%s): %s", repo, probe.reason)
    return probe


def _probe(repo: str, issue_number: int, *, run: Any) -> LinkageProbe:
    owner, _, name = repo.partition("/")
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={_PROBE_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    result = run(cmd, check=False)
    try:
        payload = json.loads(result.stdout or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"GitHub linkage probe failed for {repo} issue #{issue_number}: "
            f"rc={result.returncode}, stderr={result.stderr!r}, invalid JSON ({exc})"
        ) from exc
    return _probe_verdict(
        payload,
        repo=repo,
        issue_number=issue_number,
        returncode=result.returncode,
        stderr=result.stderr,
    )


def _probe_verdict(
    payload: dict[str, Any],
    *,
    repo: str,
    issue_number: int,
    returncode: int,
    stderr: str,
) -> LinkageProbe:
    errors = payload.get("errors") or []
    if errors:
        return _probe_error_verdict(errors, repo=repo, issue_number=issue_number)
    if returncode != 0:
        raise RuntimeError(
            f"GitHub linkage probe failed for {repo} issue #{issue_number}: "
            f"rc={returncode}, stderr={stderr!r}"
        )
    issue = ((payload.get("data") or {}).get("repository") or {}).get("issue")
    if issue is None:
        raise RuntimeError(
            f"GitHub linkage probe returned no issue #{issue_number} for {repo}"
        )
    return LinkageProbe(True, "this repository exposes blockedBy and subIssues")


def _probe_error_verdict(
    errors: list[Any], *, repo: str, issue_number: int
) -> LinkageProbe:
    messages = " | ".join(
        _graphql_error_text(error) for error in errors if isinstance(error, dict)
    )
    details = " ".join(
        detail
        for error in errors
        if (detail := _missing_field_error(error)) is not None
    )
    if details:
        named = [field for field in ("blockedBy", "subIssues") if field in details]
        missing = " or ".join(named) if named else "native linkage fields"
        return LinkageProbe(
            False,
            "this repository does not expose " + missing,
        )
    raise RuntimeError(
        f"GitHub linkage probe returned GraphQL errors for {repo} "
        f"issue #{issue_number}: {messages}"
    )


def _query_cost(data: dict[str, Any]) -> int:
    """A query's own ``rateLimit.cost``; GitHub's 1-point floor if absent."""
    rate_limit = data.get("rateLimit")
    cost = rate_limit.get("cost") if isinstance(rate_limit, dict) else None
    return cost if isinstance(cost, int) else 1


def _graphql_error_text(error: dict[str, Any]) -> str:
    """Render one GraphQL error with its provider classification."""
    message = str(error.get("message", error))
    error_type = error.get("type")
    return f"{error_type}: {message}" if error_type else message


def _missing_field_error(error: Any) -> str | None:
    """Return a structured missing-field error, else None."""
    if not isinstance(error, dict):
        return None
    extensions = error.get("extensions") or {}
    if (
        isinstance(extensions, dict)
        and extensions.get("code") == "undefinedField"
        and extensions.get("fieldName") in _LINKAGE_FIELDS
    ):
        return str(error)
    if _FIELD_MISSING_RE.search(str(error.get("message") or "")):
        return str(error)
    return None


def _connection(container: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The `nodes` of one GraphQL connection field, dict entries only."""
    nodes = (container.get(key) or {}).get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _snapshot(issue: dict[str, Any]) -> dict[str, Any]:
    """Flatten one GraphQL issue into the shape `gh issue view --json` returns.

    Keeping the `labels` / `closedByPullRequestsReferences` shape identical is
    what lets the same payload serve both the graph adapter and the driver's
    facts reader without a second read.
    """
    repository = (issue.get("repository") or {}).get("nameWithOwner")
    blockers = [
        {
            "number": int(blocker["number"]),
            "repository": (blocker.get("repository") or {}).get("nameWithOwner"),
        }
        for blocker in _connection(issue, "blockedBy")
    ]
    return {
        "number": int(issue["number"]),
        "title": issue.get("title") or "",
        "url": issue.get("url"),
        "state": issue.get("state") or "",
        "stateReason": issue.get("stateReason"),
        "body": issue.get("body") or "",
        "repository": repository,
        "labels": _connection(issue, "labels"),
        "blockedBy": blockers,
        "closedByPullRequestsReferences": _connection(
            issue, "closedByPullRequestsReferences"
        ),
    }


def _named_parent_candidate(
    issue: dict[str, Any], *, epic_name: str, label: str, repo: str
) -> int | None:
    """Return this issue's verified local, marked parent candidate."""
    parent = issue.get("parent")
    if not isinstance(parent, dict) or parent.get("number") is None:
        return None
    number = int(parent["number"])
    parent_repo = (parent.get("repository") or {}).get("nameWithOwner")
    if parent_repo and parent_repo.casefold() != repo.casefold():
        raise RuntimeError(
            f"Epic ref {label!r}: labelled issue #{issue['number']} has foreign "
            f"parent {parent_repo}#{number}. A foreign parent cannot be this "
            "epic's container."
        )
    if not parent_repo or epic_container_name(parent.get("body")) != epic_name:
        return None
    return number


def _hand_built_containers(issues: list[dict[str, Any]], *, epic_name: str) -> set[int]:
    """Labelled top-level issues with children that may serve as containers.

    The label on such an issue is an explicit opt-in for an epic whose
    container was built by hand. One marked for a DIFFERENT epic is that
    epic's container, not this one's: accepting it would let one epic hijack
    another's membership.
    """
    return {
        int(i["number"])
        for i in issues
        if i.get("parent") is None
        and (i.get("subIssues") or {}).get("totalCount", 0)
        and epic_container_name(i.get("body") or "") in (None, epic_name)
    }


def _unique_parent(
    issues: list[dict[str, Any]], *, epic_name: str, label: str, repo: str
) -> int | None:
    """The epic's parent issue for the labelled issues, or None.

    A parent named by a labelled issue qualifies only when it is local and its
    body carries this epic's container marker. A labelled issue that is itself
    no one's sub-issue and has sub-issues of its own also qualifies. This second
    form supports a hand-built container whose label is an explicit opt-in.

    Two candidates are ambiguous and raise, exactly as Linear's two-projects
    case does; zero means "this epic is not modelled with sub-issues" and the
    caller uses the label query.
    """
    named = {
        candidate
        for issue in issues
        if (
            candidate := _named_parent_candidate(
                issue, epic_name=epic_name, label=label, repo=repo
            )
        )
        is not None
    }
    labelled_containers = _hand_built_containers(issues, epic_name=epic_name)
    candidates = sorted(named | labelled_containers)
    if len(candidates) > 1:
        refs = ", ".join(f"#{n}" for n in sorted(candidates))
        raise RuntimeError(
            f"Ambiguous epic ref: {len(candidates)} issues carrying {label!r} "
            f"are top-level parents with sub-issues ({refs}). Pass the parent "
            "issue number as the epic ref, or remove the label from all but one."
        )
    return candidates[0] if candidates else None


class GitHubNativeSource:
    """Membership from sub-issues, edges from `blockedBy`; marker as fallback."""

    def __init__(
        self,
        repo: str,
        epic_prefix: str = "epic:",
        *,
        run: Any | None = None,
        probe_on_read: bool = False,
    ) -> None:
        self.repo = repo
        self.epic_prefix = epic_prefix
        self.run = _default_run if run is None else run
        # The label query and out-of-epic `status_of` stay exactly as the
        # convention adapter does them — this adapter changes the keys, not
        # the semantics of a status.
        self._fallback = GitHubConventionSource(
            repo=repo,
            epic_prefix=epic_prefix,
            run_fn=self.run if probe_on_read else None,
        )
        self._probe_on_read = probe_on_read
        self._using_fallback = False
        self.linkage_probe: LinkageProbe | None = None
        self.linkage_reported = False
        # number -> the flattened issue payload, published for the facts reader
        # (`prime_issue_snapshots`) so a tick reads each issue once, not twice.
        self.issue_snapshots: dict[int, dict[str, Any]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        # Node ids whose body carries more than one kew:depends comment.
        self.multi_depends: list[str] = []
        #: GraphQL points the last epic read spent, from each query's own
        #: ``rateLimit.cost`` — the tick prints it (#2582).
        self.graphql_points = 0
        # Which key this epic's membership came from; printed by `epic status`.
        self.membership_key: str = ""

    # -- membership ----------------------------------------------------

    def epic_parent(self, epic_ref: str) -> int | None:
        """The epic's parent issue number, or None when the epic has none."""
        if epic_ref.isdigit():
            return int(epic_ref)
        label = f"{self.epic_prefix}{epic_ref}"
        return _unique_parent(
            self._fetch_labelled(label),
            epic_name=epic_ref,
            label=label,
            repo=self.repo,
        )

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        """Read one epic through native relations or the explicit fallback."""
        self._reset_read_state()
        fallback_nodes = self._probe_fallback_nodes(epic_ref)
        if fallback_nodes is not None:
            return fallback_nodes
        raw = self._membership_for(epic_ref)
        self._report_linkage()
        return self._nodes_from_snapshots(raw, epic_ref)

    def _is_local(self, name_with_owner: str | None) -> bool:
        """GitHub repository names are case-insensitive; a hand-typed
        project.repo must not turn every node into a cross-repo one."""
        if name_with_owner is None:
            return True
        return name_with_owner.casefold() == self.repo.casefold()

    def _reset_read_state(self) -> None:
        self._snapshots = {}
        self.issue_snapshots = {}
        self.graphql_points = 0
        self.linkage_reported = False
        self._using_fallback = False

    def _probe_fallback_nodes(self, epic_ref: str) -> list[Node] | None:
        if not self._probe_on_read:
            return None
        preloaded: list[Node] | None = None
        self.linkage_probe = _probe_cache.get(self.repo)
        if self.linkage_probe is None:
            first_issue, preloaded = self._probe_seed(epic_ref)
            if first_issue is None:
                self._report_linkage()
                return []
            self.linkage_probe = probe_native_linkage(
                self.repo, first_issue, run=self.run
            )
        if self.linkage_probe.available:
            return None
        self._select_fallback(epic_ref)
        self._report_linkage()
        return (
            preloaded
            if preloaded is not None
            else self._fallback.list_epic_nodes(epic_ref)
        )

    def _probe_seed(self, epic_ref: str) -> tuple[int | None, list[Node] | None]:
        if epic_ref.isdigit():
            return int(epic_ref), None
        preloaded = self._fallback.list_epic_nodes(epic_ref)
        if preloaded:
            return int(preloaded[0].id), preloaded
        self._select_fallback(epic_ref)
        return None, preloaded

    def _select_fallback(self, epic_ref: str) -> None:
        self._using_fallback = True
        self.membership_key = f"label {self.epic_prefix}{epic_ref}"

    def _membership_for(self, epic_ref: str) -> list[dict[str, Any]]:
        parent, labelled = self._resolve_epic(epic_ref)
        if parent is None:
            self.membership_key = f"label {self.epic_prefix}{epic_ref}"
            return [
                issue
                for issue in labelled
                if epic_container_name(issue.get("body") or "") is None
            ]
        self.membership_key = f"sub-issues of #{parent}"
        return self._local_sub_issues(self._fetch_sub_issues(parent), parent)

    def _local_sub_issues(
        self, issues: list[dict[str, Any]], parent: int
    ) -> list[dict[str, Any]]:
        local = [
            issue
            for issue in issues
            if self._is_local((issue.get("repository") or {}).get("nameWithOwner"))
        ]
        foreign_count = len(issues) - len(local)
        if foreign_count:
            _log(f"skipped {foreign_count} cross-repo sub-issues of #{parent}")
        # A container nested under another parent is still a container, never a
        # node. Every read path filters markers, not just the label fallback.
        members = [
            issue
            for issue in local
            if epic_container_name(issue.get("body") or "") is None
        ]
        container_count = len(local) - len(members)
        if container_count:
            _log(f"skipped {container_count} nested epic containers of #{parent}")
        return members

    def _nodes_from_snapshots(
        self, raw: list[dict[str, Any]], epic_ref: str
    ) -> list[Node]:
        nodes: list[Node] = []
        for issue in raw:
            snap = _snapshot(issue)
            nid = str(snap["number"])
            self._snapshots[nid] = snap
            self.issue_snapshots[snap["number"]] = snap
            nodes.append(
                Node(
                    id=nid,
                    title=snap["title"],
                    status=status_from_issue(snap),
                    epic_ref=epic_ref,
                    url=snap.get("url"),
                )
            )
        return nodes

    @property
    def linkage_description(self) -> str:
        suffix = ""
        if self.linkage_probe is not None and not self.linkage_probe.available:
            suffix = f" (native fields unavailable: {self.linkage_probe.reason})"
        return f"Linkage: {self.membership_key}{suffix}"

    def _report_linkage(self) -> None:
        _log(self.linkage_description)
        self.linkage_reported = True

    def _resolve_epic(self, epic_ref: str) -> tuple[int | None, list[dict[str, Any]]]:
        if epic_ref.isdigit():
            return int(epic_ref), []
        label = f"{self.epic_prefix}{epic_ref}"
        labelled = self._fetch_labelled(label)
        return (
            _unique_parent(labelled, epic_name=epic_ref, label=label, repo=self.repo),
            labelled,
        )

    # -- edges ---------------------------------------------------------

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        """Union `blockedBy` relations with markers during the overlap release.

        An edge whose source is outside the epic is kept, not dropped: the
        graph resolves it through `status_of` and `compute_frontier` fails
        closed on one it cannot resolve. Silently dropping it would turn an
        unreadable dependency into a dispatchable node.

        The union is the fail-closed choice while both keys are written. It
        preserves marker blockers after a partial native write. The marker
        half is removed when the one-release overlap ends.
        """
        if self._using_fallback:
            fallback_edges = self._fallback.list_edges(nodes)
            self.multi_depends = list(self._fallback.multi_depends)
            return fallback_edges
        edges: list[Edge] = []
        seen: set[tuple[str, str]] = set()
        self.multi_depends = []
        for n in nodes:
            snap = self._snapshots.get(n.id)
            if snap is None:
                raise RuntimeError(
                    f"list_edges: no cached snapshot for node {n.id!r}; call "
                    "list_epic_nodes for this epic before list_edges."
                )
            deps = [
                (
                    str(blocker["number"])
                    if self._is_local(blocker.get("repository"))
                    else f"{blocker['repository']}#{blocker['number']}"
                )
                for blocker in snap["blockedBy"]
            ]
            deps.extend(parse_depends_ids(snap.get("body")))
            if depends_marker_count(snap.get("body")) > 1:
                self.multi_depends.append(n.id)
            for dep in deps:
                if (dep, n.id) in seen:
                    continue
                seen.add((dep, n.id))
                edges.append(Edge(src=dep, dst=n.id))
        return edges

    def status_of(self, node_id: str) -> NodeStatus | None:
        if "/" in node_id and "#" in node_id:
            return None
        return self._fallback.status_of(node_id)

    # -- transport -----------------------------------------------------

    def _fetch_labelled(self, label: str) -> list[dict[str, Any]]:
        return self._paged(_LABEL_QUERY, ("repository", "issues"), label=label)

    def _fetch_sub_issues(self, parent: int) -> list[dict[str, Any]]:
        return self._paged(
            _SUB_ISSUES_QUERY, ("repository", "issue", "subIssues"), number=parent
        )

    def _paged(
        self, query: str, path: tuple[str, ...], **variables: Any
    ) -> list[dict[str, Any]]:
        owner, _, name = self.repo.partition("/")
        out: list[dict[str, Any]] = []
        after: str | None = None
        for _page in range(_MAX_PAGES):
            data = self._graphql(
                query, owner=owner, name=name, after=after, **variables
            )
            conn: Any = data
            for key in path:
                conn = (conn or {}).get(key)
            if conn is None:
                raise RuntimeError(
                    f"gh api graphql returned no {'.'.join(path)} for {self.repo} "
                    f"(variables: {variables})"
                )
            out.extend(
                node for node in conn.get("nodes") or [] if isinstance(node, dict)
            )
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")
        raise RuntimeError(
            f"gh api graphql: {'.'.join(path)} for {self.repo} exceeded "
            f"{_MAX_PAGES} pages; refusing to read a truncated epic."
        )

    def _graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            flag = "-F" if isinstance(value, int) else "-f"
            cmd += [flag, f"{key}={value}"]
        result = self.run(cmd, check=False)
        # A gh failure is an error, never an empty epic — same contract as the
        # convention adapter's `gh issue list`.
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError(
                f"gh api graphql failed (rc={result.returncode}): "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
        try:
            payload = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"Failed to parse gh api graphql output: {exc}; "
                f"stdout={result.stdout!r}"
            ) from exc
        if payload.get("errors"):
            raise RuntimeError(f"gh api graphql returned errors: {payload['errors']}")
        data: dict[str, Any] = payload.get("data") or {}
        self.graphql_points += _query_cost(data)
        return data
