"""Linear facts reader: driver ``NodeFacts`` for Linear-tracked nodes (L2).

The Phase 2 driver needs richer per-node facts than the coarse ``NodeStatus``
the ``LinearSource`` dependency adapter yields — issue state + reason, labels,
and the linked PR's review/merge state — to compute its status overlay and
merge gate. This is the Linear counterpart to :class:`GitHubFactsReader`, with
exactly two provider swaps; everything PR-side is unchanged because PRs still
live on GitHub — only the tracker moved.

* **Issue state + labels** come from Linear's workflow-state ``type`` and the
  issue's Linear labels (not ``gh issue view``). A ``completed`` state reads as
  ``CLOSED``/``COMPLETED`` (overlay DONE); ``canceled`` as ``CLOSED`` with a
  non-``COMPLETED`` reason (overlay CANCELLED, failing closed); everything else
  as ``OPEN``.
* **The linked PR** comes from the newest run for the item on its dispatch
  branch that has a PR URL — for Linear there is no ``Closes #N`` marker
  GitHub can resolve, so the dispatch branch (``branches.prefix`` + the id's
  :func:`branch_slug`) is the join key back to the run that opened the PR.
  Later PR-subject runs on the same branch must not mask that association. The
  PR facts for every active branch are fetched in one ``gh pr list`` batch per
  driver tick and reused by the merge pass. A bounded branch fallback runs only
  after that batch fails or reaches its 100-row limit.
  ``pr_facts`` / ``fresh_pr_facts`` /
  ``required_checks_green`` delegate to :class:`GitHubFactsReader` unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from kew.ci_checks import CheckState
from kew.graph.adapters.linear_native import linear_admission_status
from kew.graph.driver.facts import (
    _LINK_PR_FIELDS,
    GitHubFactsReader,
    NodeFacts,
    RepositoryPermission,
    RunFn,
    reconcile_awaiting_publication,
    resolve_linked_pr,
)
from kew.ids import branch_slug
from kew.linear.client import LinearApiError
from kew.run_store import PR_SUBJECT_RUN_KINDS
from kew.subprocess_utils import run as _default_run

if TYPE_CHECKING:
    from kew.linear.client import LinearClient
    from kew.run_store import RunStore

# Issue state + labels for one Linear issue, keyed by its human identifier
# (``issue(id:)`` accepts the ``SFD-12`` shorthand, same as LinearSource).
_ISSUE_FACTS_QUERY = """
query IssueFacts($id: String!) {
  issue(id: $id) {
    identifier
    state { name type }
    labels(first: 50) { nodes { name } }
  }
}
"""


class LinearFactsReader:
    """Reads richer per-node facts for a Linear-tracked driver node."""

    def __init__(
        self,
        client: LinearClient,
        store: RunStore,
        repo: str,
        *,
        branch_prefix: str = "agent/issue-",
        run: RunFn = _default_run,
        host: str | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self.repo = repo
        self._branch_prefix = branch_prefix
        self.run = run
        # PRs are GitHub-native: reuse the GitHub reader verbatim for the
        # by-number PR fetch and the mergeStateStatus gate the driver consumes.
        self._gh = GitHubFactsReader(repo, run=run, host=host)
        self._prepared = False
        self._batch_succeeded: bool | None = None
        self._batch_row_count = 0
        self._prepared_issues: dict[str, dict[str, Any]] = {}
        self._target_branches: set[str] = set()
        self._pr_by_branch: dict[str, list[dict[str, Any]]] = {}
        self._pr_by_number: dict[int, dict[str, Any]] = {}
        self._branch_fallback_attempted: set[str] = set()
        self._logged_pr_disagreements: set[tuple[str, tuple[int, ...]]] = set()

    def prepare(self, item_ids: list[str]) -> None:
        """Fetch all PR facts for one driver tick in a single GraphQL call.

        Linear issue reads are staged here too, allowing completed/cancelled
        nodes to be excluded before GitHub is queried. A failed or saturated
        batch permits one bounded branch fallback per missing node.
        """
        self._prepared = True
        self._batch_succeeded = None
        self._batch_row_count = 0
        self._prepared_issues.clear()
        self._pr_by_branch.clear()
        self._pr_by_number.clear()
        self._branch_fallback_attempted.clear()
        self._target_branches = self._active_pr_branches(item_ids)
        if not self._target_branches:
            self._batch_succeeded = True
            return

        result = self.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "all",
                "--limit",
                # One GitHub GraphQL page: ``gh`` must not quietly paginate
                # this batch back into the per-node quota pattern we removed.
                "100",
                "--json",
                _LINK_PR_FIELDS,
            ],
            check=False,
        )
        self._ingest_pr_batch(result)

    def _active_pr_branches(self, item_ids: list[str]) -> set[str]:
        """Dispatch branches for every open Linear item."""
        branches: set[str] = set()
        for item_id in item_ids:
            issue = self._fetch_issue(item_id)
            self._prepared_issues[item_id] = issue
            state, _reason = self._issue_state(issue.get("state") or {})
            branch = f"{self._branch_prefix}{branch_slug(item_id)}"
            if state != "CLOSED":
                branches.add(branch)
        return branches

    def _ingest_pr_batch(self, result: Any) -> None:
        """Validate and index one ``gh pr list`` response."""
        if getattr(result, "returncode", 1) != 0:
            self._batch_succeeded = False
            return
        try:
            parsed: Any = json.loads(getattr(result, "stdout", "") or "[]")
        except (json.JSONDecodeError, TypeError):
            self._batch_succeeded = False
            return
        if not isinstance(parsed, list):
            self._batch_succeeded = False
            return
        self._batch_succeeded = True
        self._batch_row_count = len(parsed)
        self._ingest_pr_candidates(parsed)

    def _ingest_pr_candidates(self, parsed: list[Any]) -> None:
        """Index validated PR candidates for target branches."""
        for candidate in parsed:
            if not isinstance(candidate, dict):
                continue
            candidate_branch = candidate.get("headRefName")
            number = candidate.get("number")
            if (
                candidate_branch not in self._target_branches
                or not isinstance(candidate_branch, str)
                or not isinstance(number, int)
            ):
                continue
            self._pr_by_branch.setdefault(candidate_branch, []).append(candidate)
            self._pr_by_number[number] = candidate

    def issue_facts(self, item_id: str) -> NodeFacts:
        """Return ``NodeFacts`` for one Linear item, enriched from its PR.

        ``issue_num`` is the ``0`` non-numeric sentinel (Linear ids are not
        integers; A3 convention) — the item id lives in the branch/run join,
        not this field.
        """
        issue = self._prepared_issues.pop(item_id, None)
        if issue is None:
            issue = self._fetch_issue(item_id)
        state, state_reason = self._issue_state(issue.get("state") or {})
        labels = self._labels_of(issue)
        admission_status = linear_admission_status(issue.get("state") or {}, labels)

        branch = f"{self._branch_prefix}{branch_slug(item_id)}"
        runs = list(self._store.list_by_item(item_id))
        run = self._latest_pr_run(runs, branch)
        if state == "CLOSED" and state_reason == "COMPLETED":
            reconcile_awaiting_publication(
                self._store,
                item_id=item_id,
                repo=self.repo,
                pr_url=None,
                observation="provider_completed",
            )
        (
            pr_number,
            pr_url,
            review_decision,
            merge_state_status,
            head_oid,
            pr_pushed_at,
            pr_state,
            pr_link_source,
            pr_has_closing_ref,
        ) = (
            self._linked_pr_fields(item_id, run, branch, runs=runs)
            if state != "CLOSED"
            else (None, None, None, None, None, None, None, None, None)
        )

        return NodeFacts(
            issue_num=0,
            state=state,
            state_reason=state_reason,
            labels=labels,
            pr_number=pr_number,
            pr_url=pr_url,
            review_decision=review_decision,
            merge_state_status=merge_state_status,
            head_oid=head_oid,
            pr_pushed_at=pr_pushed_at,
            admission_status=admission_status,
            pr_state=pr_state,
            pr_link_source=pr_link_source,
            # Linear cannot observe GitHub closing references: not a fact here.
            pr_has_closing_ref=None,
        )

    def _linked_pr_fields(
        self,
        item_id: str,
        run: Any,
        branch: str,
        *,
        runs: list[Any],
    ) -> tuple[
        int | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        bool,
    ]:
        """PR-derived ``NodeFacts`` fields, failing closed on missing live data."""
        link = resolve_linked_pr(
            item_id,
            branch,
            store=self._store,
            batch=self,
            closing_refs=(),
            runs=runs,
        )
        if link.number is None:
            return (None, None, None, None, None, None, None, None, False)
        pr = link.pr
        url = link.url
        if run is not None:
            self._reconcile_published_run(run, url, state=pr.get("state"))
        return (
            link.number,
            url,
            pr.get("reviewDecision") or None,
            pr.get("mergeStateStatus") or None,
            pr.get("headRefOid") or None,
            GitHubFactsReader._last_commit_date(pr),
            str(pr["state"]),
            link.source,
            link.has_closing_ref,
        )

    def _latest_pr_run(self, runs: list[Any], branch: str) -> Any | None:
        """Newest run that owns or is durably awaiting a PR on the branch."""
        return next(
            (
                candidate
                for candidate in reversed(runs)
                if getattr(candidate, "branch", None) == branch
                and getattr(candidate, "run_kind", "dispatch")
                not in PR_SUBJECT_RUN_KINDS
                and (
                    getattr(candidate, "pr_url", None)
                    or getattr(candidate, "status", None) == "awaiting_publication"
                )
            ),
            None,
        )

    def _reconcile_published_run(
        self, run: Any, pr_url: str | None, *, state: Any = None
    ) -> None:
        """Idempotently settle an awaiting run once its branch has a PR."""
        if getattr(run, "status", None) != "awaiting_publication" or not pr_url:
            return
        normalized = str(state or "OPEN").upper()
        # A closed-unmerged PR is not published work: finalizing here would
        # terminal the continuation and strand the preserved branch. Same gate
        # as `GitHubFactsReader._reconcile_issue_publication`.
        if normalized not in {"OPEN", "MERGED"}:
            return
        reconcile_awaiting_publication(
            self._store,
            item_id=str(getattr(run, "item_id", "")) or "",
            repo=self.repo,
            pr_url=pr_url,
            observation=("pr_merged" if normalized == "MERGED" else "pr_open"),
        )

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        """Delegate to the GitHub reader — the PR is GitHub-native."""
        prepared = self._pr_by_number.get(pr_number)
        if prepared is not None:
            return prepared
        if self._prepared:
            return {}
        return self._gh.pr_facts(pr_number)

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        """Read one PR from GitHub NOW, never from this tick's batch.

        The serial merge queue's post-verification head check (#2148) needs a
        head that may have moved *during* the verification, so this must not be
        answered from ``self._pr_by_number`` — that is the very batch row the
        classification already used. PRs are GitHub-native, so the uncached
        read delegates verbatim.
        """
        return self._gh.fresh_pr_facts(pr_number)

    def pr_number_from_url(self, url: str) -> int | None:
        """Parse a canonical PR URL through the shared GitHub reader."""
        return self._gh.pr_number_from_url(url)

    def _fetch_branch_prs(self, branch: str) -> list[Any]:
        result = self.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "5",
                "--json",
                _LINK_PR_FIELDS,
            ],
            check=False,
        )
        if getattr(result, "returncode", 1) != 0:
            return []
        try:
            parsed: Any = json.loads(getattr(result, "stdout", "") or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    def pr_candidates_for_branch(self, branch: str) -> list[dict[str, Any]]:
        """Return batch candidates or one bounded branch fallback."""
        prepared = self._pr_by_branch.get(branch)
        if prepared:
            return list(prepared)
        batch_missed = self._batch_succeeded is False or self._batch_row_count >= 100
        if not batch_missed:
            return []
        if branch not in self._branch_fallback_attempted:
            self._branch_fallback_attempted.add(branch)
            result = self._fetch_branch_prs(branch)
            for candidate in result:
                if not isinstance(candidate, dict):
                    continue
                candidate.setdefault("headRefName", branch)
            self._ingest_pr_candidates(result)
        return list(self._pr_by_branch.get(branch, []))

    def repository_permission(self, login: str) -> RepositoryPermission | None:
        """Delegate the fresh repository-authority read to GitHub."""
        return self._gh.repository_permission(login)

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        """Delegate to the GitHub reader — merge state is GitHub-native."""
        return self._gh.required_checks_green(base, pr)

    def head_checks(
        self, sha: str, gated_checks: Sequence[str]
    ) -> tuple[CheckState, str]:
        """Delegate to the GitHub reader — check runs are GitHub-native."""
        return self._gh.head_checks(sha, gated_checks)

    def _fetch_issue(self, item_id: str) -> dict[str, Any]:
        """Fetch one issue's state + labels; ``{}`` if it does not resolve.

        A not-found id reads as "no facts" (``{}``), mirroring
        ``LinearSource.status_of`` — but auth/network failures propagate rather
        than masquerading as an empty (dangling-looking) node.
        """
        try:
            issue = self._client.execute(_ISSUE_FACTS_QUERY, {"id": item_id})["issue"]
        except LinearApiError as exc:
            if exc.not_found:
                return {}
            raise
        return issue or {}

    @staticmethod
    def _pr_number_from_url(url: str) -> int | None:
        tail = url.rpartition("/pull/")[2]
        try:
            number = int(tail)
        except ValueError:
            return None
        return number if number > 0 else None

    @staticmethod
    def _issue_state(state: dict[str, Any]) -> tuple[str, str | None]:
        """Map a Linear workflow-state to (issue-state, state-reason).

        Aligns with the overlay's CLOSED handling: ``completed`` -> DONE,
        ``canceled`` -> CANCELLED (any non-``COMPLETED`` reason), else OPEN.
        """
        stype = str(state.get("type", ""))
        if stype == "completed":
            return "CLOSED", "COMPLETED"
        if stype == "canceled":
            return "CLOSED", "CANCELLED"
        # No resolvable state (an unknown id -> empty payload) reads as ""
        # rather than OPEN, mirroring GitHubFactsReader's empty-issue handling.
        if not stype:
            return "", None
        return "OPEN", None

    @staticmethod
    def _labels_of(issue: dict[str, Any]) -> frozenset[str]:
        nodes = (issue.get("labels") or {}).get("nodes") or []
        return frozenset(
            lbl.get("name", "")
            for lbl in nodes
            if isinstance(lbl, dict) and lbl.get("name")
        )
