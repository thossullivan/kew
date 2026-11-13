"""Review comment model, fetching, grouping, and dispatch orchestration.

Fetches PR review comments from GitHub's three review-related endpoints,
normalizes them into ReviewComment dataclasses, groups by file path,
and orchestrates the full review dispatch cycle.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jinja2 import Environment, FileSystemLoader

from kew import _TEMPLATES_DIR
from kew.behavior_declarations import (
    DeclarationRequest,
    carry_declared_lines,
    propose_lines,
    requested_lines,
)
from kew.budget import (
    WINDOW_BUDGET_REFUSAL,
    BudgetExceeded,
    check_budget,
    check_review_budget,
    check_window_budget,
    window_budget_hold_reason,
)
from kew.command_cleanup import finish_command_cleanup
from kew.command_outcome import CommandOutcome
from kew.config import DispatchConfig
from kew.dispatch_core import (
    AGENT_REPORT_BANNER_PREFIX,
    RUN_REPORT_MARKER_PREFIX,
    WORKER_CAP_REACHED_MESSAGE,
    GitHubAuthError,
    WorkerCapReached,
    _check_github_connectivity,
    _get_checkout_changed_files,
    budget_watchdog,
    revoke_ci_approval,
    run_tracked_agent,
)
from kew.dispatch_core import (
    resolve_issue_number as _resolve_issue_number,
)
from kew.epic_holds import deferred_publication_key
from kew.inference_transport import TransportAdmissionError, resolve_inference_transport
from kew.merge_claim import wait_out_merge_claim
from kew.pr_text import proposed_lines
from kew.progress import _error, _log, _success, _warn
from kew.prompt import (
    UNTRUSTED_PREAMBLE,
    brief_memory_cap,
    fence_slot,
    fence_vocabulary_filter,
    guard_untrusted,
    make_sentinel,
    policy_test_command,
)
from kew.prompt_addendum import (
    PROMPT_CONFIGURATION_REFUSAL,
    ProfileAddendum,
    PromptConfigurationError,
    profile_version_id,
)
from kew.prompt_refusals import record_prompt_refusal
from kew.publication import (
    PUBLICATION_CONFIGURATION_REFUSAL,
    PublicationError,
    TransientPushFailure,
    select_publication_mode,
)
from kew.publication_canonical import (
    CanonicalNoChange,
    CanonicalPublication,
    PublicationHeld,
)
from kew.publication_credentials import (
    _MAX_BRANCH_RUNS,
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    branch_run_cap_refusal,
    ensure_publication_credentials,
    publication_branch_runs,
    publication_producer_for_head,
)
from kew.remediation_containment import (
    record_remediation_refusal,
    resolve_remediation_containment,
)
from kew.review_checkout import (
    ReviewCheckout,
    ReviewCheckoutError,
    discard_review_checkout,
    materialize_review_checkout,
)
from kew.review_contract import Finding, is_host_location
from kew.review_occupancy import reserve_standalone_review, review_occupant
from kew.review_outcome import (
    STAGE_PRE_LAUNCH,
    STAGE_PUBLICATION,
    STAGE_RUNNER,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REFUSED,
    ReviewOutcome,
    record_review_outcome,
)
from kew.review_targets import (
    TargetGroup,
    module_test_targets,
    remediation_test_targets,
)
from kew.run_store import PR_SUBJECT_RUN_KINDS, REVIEW_REMEDIATION_RUN_KIND
from kew.runners import resolve_runner
from kew.sandbox import (
    POSTURE_SANDBOXED,
    SandboxRuntimeUnavailable,
    attest_flow_posture,
)
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import run as subprocess_run
from kew.subprocess_utils import run_with_backoff
from kew.tracking import LaunchState, command_outcome, startup_refused
from kew.transcript_metrics import agent_report_for
from kew.worktree import (
    ConflictInfo,
    PublicationHeadMovedError,
    detect_base_branch,
    discard_checkout,
    publication_remote_sha,
    publish_existing_pr_branch,
    republish_agent_head,
)

if TYPE_CHECKING:
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.remediation_containment import RemediationContainment
    from kew.run_store import RunStore
    from kew.runners.registry import ResolvedRoute
    from kew.tracking import DispatchResult

# Pseudo-epic under which governed-review reservations park (N2/#977).
# Mirrors REVIEW_EPIC_REF in graph/driver/tick.py — kept local to avoid a
# cross-layer import (same pattern as _DELEGATION_EPIC_REF in tick.py).
_REVIEW_EPIC_REF = "review"


@dataclass
class ReviewComment:
    """A normalized review comment from any GitHub endpoint."""

    id: int
    author: str
    body: str
    path: str | None
    line: int | None
    state: str
    in_reply_to: int | None
    resolved: bool


@dataclass
class _RenderComment:
    """A comment row prepared for prompt rendering (body already fenced/capped).

    Kept separate from :class:`ReviewComment` so the caller's objects are never
    mutated and the source type (dataclass, mock, ...) is irrelevant.
    """

    path: str | None
    author: str
    line: int | None
    body: str


def _run_gh_api(endpoint: str, repo: str) -> list[dict[str, Any]]:
    """Call gh api repos/{repo}/{endpoint} --paginate --slurp.

    Returns parsed JSON list.
    """
    result = run_with_backoff(
        ["gh", "api", f"repos/{repo}/{endpoint}", "--paginate", "--slurp"],
        check=True,
    )
    data: Any = json.loads(result.stdout)
    # --slurp wraps paginated results in an outer array.
    # For endpoints returning arrays, we get [[...], [...]] — flatten one level.
    if isinstance(data, list):
        flat: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, list):
                flat.extend(item)
            elif isinstance(item, dict):
                flat.append(item)
        return flat
    return [data]


def _fetch_resolved_threads(pr_number: int, repo: str) -> dict[int, bool]:
    """Fetch review thread resolution state via GraphQL.

    Returns dict mapping comment database ID to resolved status.
    Uses gh api graphql to query pullRequest.reviewThreads.
    """
    owner, name = repo.split("/", 1)
    query = """
    query($owner: String!, $name: String!, $pr: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              comments(first: 100) {
                nodes {
                  databaseId
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        result = run_with_backoff(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"pr={pr_number}",
            ],
            check=True,
        )
        data = json.loads(result.stdout)
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        resolved_map: dict[int, bool] = {}
        for thread in threads:
            is_resolved = thread.get("isResolved", False)
            comments = thread.get("comments", {}).get("nodes", [])
            for comment in comments:
                db_id = comment.get("databaseId")
                if db_id is not None:
                    resolved_map[db_id] = is_resolved
        return resolved_map
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as exc:
        _warn(f"Failed to fetch review thread resolution state: {exc}")
        return {}


def fetch_review_comments(
    pr_number: int,
    config: DispatchConfig,
    include_resolved: bool,
    author_filter: str | None,
) -> list[ReviewComment]:
    """Fetch and normalize comments from all three GitHub review endpoints.

    Queries:
    - pulls/{pr}/reviews — top-level review bodies (skips empty body)
    - pulls/{pr}/comments — inline file-level comments
    - issues/{pr}/comments — general conversation comments

    Args:
        pr_number: The PR number to fetch comments for.
        config: Dispatch configuration (for repo name).
        include_resolved: Whether to include resolved comments.
        author_filter: If set, only include comments from this author.

    Returns:
        List of normalized ReviewComment objects.
    """
    repo = config.project.repo
    comments: list[ReviewComment] = []

    comments.extend(_review_bodies(pr_number, repo))
    comments.extend(_inline_review_comments(pr_number, repo))
    comments.extend(_conversation_comments(pr_number, repo))

    # Filter by author if specified
    if author_filter is not None:
        comments = [c for c in comments if c.author == author_filter]

    # Filter resolved comments if not included
    if not include_resolved:
        comments = [c for c in comments if not c.resolved]

    return comments


def _review_bodies(pr_number: int, repo: str) -> list[ReviewComment]:
    comments: list[ReviewComment] = []
    # 1. Top-level review bodies
    reviews = _run_gh_api(f"pulls/{pr_number}/reviews", repo)
    for item in reviews:
        body = item.get("body")
        if not body:
            continue
        user = item.get("user")
        author = user.get("login", "") if isinstance(user, dict) else ""
        comments.append(
            ReviewComment(
                id=item["id"],
                author=author,
                body=body,
                path=None,
                line=None,
                state=item.get("state", ""),
                in_reply_to=None,
                resolved=False,
            )
        )

    return comments


def _inline_review_comments(pr_number: int, repo: str) -> list[ReviewComment]:
    comments: list[ReviewComment] = []
    # Fetch thread resolution state
    resolved_map = _fetch_resolved_threads(pr_number, repo)

    # 2. Inline file-level comments
    inline = _run_gh_api(f"pulls/{pr_number}/comments", repo)
    for item in inline:
        user = item.get("user")
        author = user.get("login", "") if isinstance(user, dict) else ""
        comment_id = item["id"]
        in_reply_to = item.get("in_reply_to_id")
        # Resolved if this comment or its parent thread is resolved
        is_resolved = resolved_map.get(comment_id, False)
        if not is_resolved and in_reply_to is not None:
            is_resolved = resolved_map.get(in_reply_to, False)
        comments.append(
            ReviewComment(
                id=comment_id,
                author=author,
                body=item.get("body", ""),
                path=item.get("path"),
                line=item.get("line"),
                state="",
                in_reply_to=in_reply_to,
                resolved=is_resolved,
            )
        )

    return comments


def _conversation_comments(pr_number: int, repo: str) -> list[ReviewComment]:
    """General conversation (issue) comments, without kew's posted Dispatch
    Reports: those quote an agent's own text, which is not review to act on.

    A report kew posted opens with its banner and carries its run marker; a
    comment that only quotes the marker is someone's review and stays.
    """
    comments: list[ReviewComment] = []
    issue_comments = _run_gh_api(f"issues/{pr_number}/comments", repo)
    for item in issue_comments:
        body = item.get("body") or ""
        if body.startswith(AGENT_REPORT_BANNER_PREFIX) and (
            RUN_REPORT_MARKER_PREFIX in body
        ):
            continue
        user = item.get("user")
        author = user.get("login", "") if isinstance(user, dict) else ""
        comments.append(
            ReviewComment(
                id=item["id"],
                author=author,
                body=item.get("body", ""),
                path=None,
                line=None,
                state="",
                in_reply_to=None,
                resolved=False,
            )
        )

    return comments


def group_comments_by_file(
    comments: list[ReviewComment],
) -> dict[str | None, list[ReviewComment]]:
    """Group comments by file path. General comments grouped under None key.

    Args:
        comments: List of ReviewComment objects.

    Returns:
        Dict mapping file path (or None for general) to comments.
    """
    grouped: dict[str | None, list[ReviewComment]] = {}
    for comment in comments:
        grouped.setdefault(comment.path, []).append(comment)
    return grouped


def governed_test_targets(worktree_dir: Path, base_branch: str) -> list[TargetGroup]:
    """Test modules named after the source modules this branch changed (#2200).

    Read from the checkout's own diff rather than through the publication
    boundary: the answer only decides which test paths the brief names, so a
    checkout that lied about its diff could at worst ask for extra tests. It
    can never remove one — the set the agent must run is a floor, not a cap.

    Args:
        worktree_dir: The review checkout.
        base_branch: The branch the PR merges into.

    Returns:
        Test paths grouped by the directory they are relative to — the package
        the test command is configured for, not the repository root. Empty when
        the diff cannot be read.
    """
    return module_test_targets(
        _get_checkout_changed_files(worktree_dir, base_branch),
        root=worktree_dir,
    )


def round_test_targets(
    worktree_dir: Path,
    base_branch: str,
    *,
    comments: Sequence[ReviewComment],
    conflict_info: ConflictInfo | None = None,
) -> tuple[list[TargetGroup], bool]:
    """The tests one remediation round names: its findings' files, else the PR.

    Scoped to the files the round's findings (or review comments) name plus the
    round's own delta — the files a base merge left conflicted — and expanded
    through the import-graph selector (#3201). A round whose findings name no
    file in the checkout keeps the whole-diff rule of
    :func:`governed_test_targets`. The flag says which rule answered.
    """
    scoped = remediation_test_targets(
        [comment.path for comment in comments if comment.path],
        conflict_info.conflicted_files if conflict_info is not None else (),
        root=worktree_dir,
        base_branch=base_branch,
    )
    if scoped is None:
        return governed_test_targets(worktree_dir, base_branch), False
    return scoped, True


def _conflict_slots(
    branch_name: str, conflict_info: ConflictInfo | None
) -> dict[str, Any] | None:
    """The merge-conflict section's values, each fenced as data (#3348).

    The branch names and conflicted paths are read off the PR and the merge,
    so a path or ref can spell the fence vocabulary like any other slot.
    """
    if conflict_info is None:
        return None
    return {
        "branch": fence_slot(branch_name, inline=True),
        "base": fence_slot(conflict_info.base_branch, inline=True),
        "paths": [
            fence_slot(path, inline=True) for path in conflict_info.conflicted_files
        ],
    }


def build_review_prompt(
    *,
    pr_number: int,
    issue_number: int,
    branch_name: str,
    comments: list[ReviewComment],
    config: DispatchConfig,
    base_branch: str = "main",
    conflict_info: ConflictInfo | None = None,
    host_publication: bool = False,
    governed: bool = False,
    sandboxed: bool = False,
    targeted_tests: Sequence[TargetGroup] = (),
    targets_scoped: bool = False,
    failing_criterion_ids: Sequence[str] = (),
    declaration: DeclarationRequest | None = None,
    proposal: DeclarationRequest | None = None,
    profile_addendum: ProfileAddendum | None = None,
) -> str:
    """Build the review prompt from Jinja2 template and context.

    Args:
        pr_number: The pull request number.
        issue_number: The related issue number.
        branch_name: The working branch name.
        comments: List of review comments to address.
        config: Dispatch configuration.
        base_branch: The base branch for the PR.
        conflict_info: If set, prepend conflict resolution instructions.
        host_publication: The host commits and pushes; tell the agent not to.
        governed: A governed remediation round (#2109): the agent runs only the
            tests covering its changes plus each finding's regression test, not
            the full suite, which runs before any merge.
        sandboxed: The round runs inside a contained runtime under its memory
            cap (#2984). The brief, governed or standalone, then names only
            targeted tests and static gates and leaves the full suite, semgrep,
            the docs checks and the repo-wide policy gates to the merge verify;
            a host round keeps the policy gates, which it has the memory for.
        targeted_tests: Test modules the host computed from the diff (#2200),
            grouped by the directory they must be run from. The brief names
            them as required; the agent may add to the set, never remove from
            it. Only rendered in a governed or sandboxed round.
        targets_scoped: *targeted_tests* cover the files the round's findings
            name (#3201), not every module the PR changes; the brief says so.
        failing_criterion_ids: Recorded acceptance criteria the reviewer judged
            failed (#2291). Listed FIRST in the brief, above the findings: a
            remediation that fixes every finding and still misses what the item
            was for has not addressed the round.
        declaration: The round asks only for **Behavior changes** lines
            (#2872). The brief names each test to declare and the reviewer's
            reasons, and says no code change is wanted.
        proposal: The round's findings ask only for **Behavior changes** lines
            the host cannot carry itself (#3493). The brief names each test and
            makes one proposed line per test the remediation's whole job.
        profile_addendum: The approved ``review_remediation`` addendum (#3023),
            rendered once under its heading before the safety and publication
            sections; None renders the base bytes.

    Returns:
        Rendered prompt string.
    """
    # Fence attacker-reachable comment bodies with a per-render sentinel and cap
    # each body (H7). guard_untrusted rejects a body that already contains the
    # sentinel, so the fence cannot be spoofed. Render rows are plain objects so
    # the caller's comment type is never mutated.
    sentinel = make_sentinel()
    grouped: dict[str | None, list[_RenderComment]] = {}
    for c in comments:
        row = _RenderComment(
            path=c.path,
            author=c.author,
            line=c.line,
            body=guard_untrusted(c.body, sentinel),
        )
        grouped.setdefault(row.path, []).append(row)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        finalize=fence_vocabulary_filter(sentinel),
    )
    template = env.get_template("review.md.j2")
    return template.render(
        pr_number=pr_number,
        issue_number=issue_number,
        branch_name=branch_name,
        base_branch=base_branch,
        repo=config.project.repo,
        comments_by_file=grouped,
        sentinel=sentinel,
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        auto_reply=config.review.auto_reply,
        commands_ci=config.commands.ci,
        commands_format=config.commands.format,
        commands_test_targeted=config.commands.test_targeted,
        commands_test_policy_mark=config.commands.test_policy_mark,
        commands_test_policy_command=policy_test_command(
            test_targeted=config.commands.test_targeted,
            policy_mark=config.commands.test_policy_mark,
        ),
        commands_static=config.commands.static,
        commands_ui_check=config.commands.ui_check,
        # The cap kew actually enforces, never a hard-coded one (#3201); a host
        # round names none and reads no [sandbox] setting.
        memory_cap=brief_memory_cap(config, sandboxed=sandboxed),
        host_publication=host_publication,
        governed=governed,
        sandboxed=sandboxed,
        targeted_tests=list(targeted_tests),
        targets_scoped=targets_scoped,
        failing_criteria=list(failing_criterion_ids),
        declaration_ids=list(declaration.node_ids)
        if declaration and not declaration.held_out
        else [],
        declaration_reasons=[guard_untrusted(r, sentinel) for r in declaration.reasons]
        if declaration and not declaration.held_out
        else [],
        held_out_ids=list(declaration.node_ids)
        if declaration and declaration.held_out
        else [],
        held_out_section=[guard_untrusted(r, sentinel) for r in declaration.section]
        if declaration and declaration.held_out
        else [],
        proposal_ids=list(proposal.node_ids) if proposal else [],
        proposal_reasons=[guard_untrusted(r, sentinel) for r in proposal.reasons]
        if proposal
        else [],
        profile_addendum=profile_addendum,
        conflict=_conflict_slots(branch_name, conflict_info),
    )


def _get_pr_metadata(pr_number: int, repo: str) -> dict[str, Any] | None:
    """Fetch PR metadata via gh pr view.

    Args:
        pr_number: The PR number.
        repo: The owner/repo string.

    Returns:
        Parsed dict with number, headRefName, headRefOid, baseRefName, body,
        title — or None on failure. ``headRefOid`` is the head the review binds
        to: the checkout refuses a mismatch and publication refuses a moved head
        (#1290).
    """
    try:
        result = subprocess_run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "number,headRefName,headRefOid,baseRefName,body,title",
            ],
            check=True,
        )
        return json.loads(result.stdout)  # type: ignore[no-any-return]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def _worktree_head_sha(worktree_dir: Path) -> str:
    """Return the commit the review checkout ended on, when it is known."""
    try:
        result = subprocess_run(
            [
                "git",
                "-C",
                str(worktree_dir),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_tree(worktree_dir: Path, sha: str) -> str:
    """The tree commit *sha* holds, read in *worktree_dir*; ``""`` if unknown.

    Only for the checkout publication modes, which already read the head from
    the checkout itself; canonical publication proves its tree without
    checkout-rooted Git. An unknown tree proves nothing: the driver then judges
    the run on its commit ids (#3276), so the failure is said, not swallowed.
    """
    if not sha:
        return ""
    try:
        result = subprocess_run(
            [
                "git",
                "-C",
                str(worktree_dir),
                "rev-parse",
                "--verify",
                f"{sha}^{{tree}}",
            ],
            check=False,
        )
    except (OSError, ValueError) as exc:
        _warn(f"could not read the published tree of {sha[:12]}: {exc}")
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _canonical_review_publication(
    *,
    worktree_dir: Path,
    branch_name: str,
    expected_head_sha: str,
    pr_number: int,
    repo: str | None,
    host_publishes: bool,
    base_branch: str | None = None,
    integrated_base_sha: str = "",
    store: RunStore | None = None,
    run_id: str | None = None,
    producer_run_id: str | None = None,
    before_push: Callable[[], object] | None = None,
    item_id: str | None = None,
    record_tree: Callable[[str], object] | None = None,
) -> tuple[str, bool]:
    """Return a remote-proven review SHA without checkout-rooted Git.

    *item_id* is the governed remediation's node. A review handed none
    publishes for the branch's owning item, so the publication reads the
    acceptance contract recorded under that item — never under the PR number,
    which no contract is keyed by (#2729).

    *record_tree* is handed the tree of the returned SHA whenever the
    publication's own evidence proves it (#3276); a SHA read back from the
    remote alone proves no tree, and nothing is recorded.
    """
    keep_tree = record_tree or (lambda _tree: None)
    owner = item_id if item_id is not None else _branch_owner_item(store, branch_name)
    if not host_publishes:
        observed = publication_remote_sha(worktree_dir, branch_name, repo=repo)
        if not observed:
            raise PublicationError("push", "the review branch head is unavailable")
        if observed != expected_head_sha:
            # The agent's own commit graph is never adopted. The host proves the
            # pushed head carries exactly the checkout's tree and republishes
            # that tree as one host commit on the reviewed head, under a lease
            # bound to the agent's head (review round 4).
            republished = republish_agent_head(
                worktree_dir,
                branch_name,
                expected_sha=expected_head_sha,
                observed_sha=observed,
                commit_message=f"fix: address review feedback on PR #{pr_number}",
                repo=repo,
                store=store,
                run_id=run_id,
                producer_run_id=producer_run_id,
                item_id=owner,
            )
            keep_tree(republished.tree_sha)
            return republished.commit_sha, False

    publication_context: dict[str, Any] = {}
    if store is not None:
        publication_context = {
            "store": store,
            "run_id": run_id,
            "item_id": owner or str(pr_number),
            "producer_run_id": producer_run_id,
        }
    outcome = publish_existing_pr_branch(
        worktree_dir,
        branch_name,
        commit_message=f"fix: address review feedback on PR #{pr_number}",
        expected_remote_sha=expected_head_sha,
        repo=repo,
        publish="canonical",
        # A remediation that merged its base publishes the base's own edits too
        # (the checkout was cut from a head that predates them), so the host
        # needs the base branch to tell those apart from this run's work before
        # it stops a protected path for a person (#2362).
        merged_base_branch=base_branch,
        # The commit kew's own checkout integration merged, so the host can
        # tell the base content IT brought from base content the agent copied
        # in instead of merging (#2457).
        integrated_base_sha=integrated_base_sha or None,
        before_push=before_push,
        **publication_context,
    )
    if isinstance(outcome, CanonicalPublication):
        keep_tree(outcome.tree_sha)
        return outcome.commit_sha, False
    if isinstance(outcome, CanonicalNoChange):
        keep_tree(outcome.tree_sha)
        return outcome.base_sha, False
    if outcome:
        observed = publication_remote_sha(worktree_dir, branch_name, repo=repo)
        if observed:
            return observed, False
    raise PublicationError("push", "canonical review returned no remote evidence")


#: What a remediation whose publication the host DEFERRED is classified as —
#: distinct from ``publication_failed``, which is a publication that broke. The
#: work is intact in a preserved checkout and a person has to look at it
#: (#2362), so the driver reads this classification rather than inferring the
#: deferral from a failure string.
REVIEW_PUBLICATION_HELD = "publication_held"


def deferred_publication_flags(exc: BaseException) -> tuple[str, ...] | None:
    """The review flags one exception carries, or ``None`` if it carries none.

    The cause chain is walked because the canonical publication path re-raises
    a deferral as ``PublicationError("push", held)`` for its callers' sake
    (:func:`kew.worktree._publish_existing_pr_branch_canonical`), and this is
    the one caller that has to tell "a person must look at this" apart from "the
    push broke". Reading ``str(exc)`` for the words instead is what left all
    three of #2352's deferred runs recorded as successes.
    """
    for cause in _publication_causes(exc):
        if isinstance(cause, PublicationHeld):
            return tuple(cause.flags)
    return None


def _publication_causes(exc: BaseException) -> Iterator[BaseException]:
    """One exception and every cause behind it, each visited once."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def exhausted_transient_push(exc: BaseException) -> TransientPushFailure | None:
    """The server-side push failure one exception carries, if it carries one.

    Same cause-chain read as :func:`deferred_publication_flags`, for the other
    publication outcome that leaves the work intact: the remote failed the push
    on its own side and kept failing it (#2455). The commit was built and the
    checkout is preserved, so the remediation is held for another push rather
    than failed — a failed one is what sent the epic loop off to redo a finished
    97-step fix from scratch.
    """
    for cause in _publication_causes(exc):
        if isinstance(cause, TransientPushFailure):
            return cause
    return None


def _split_location(location: str) -> tuple[str | None, int | None]:
    """Split a finding ``file:line`` location into ``(path, line)``.

    A location without a parseable trailing line number keeps the whole string
    as the path and a ``None`` line (the grouping/render tolerates both), and
    so does one kew wrote itself (a merge verify's base, a send-back's head),
    whose tail is never a line.
    """
    if is_host_location(location):
        return location, None
    path, _, tail = location.rpartition(":")
    if path and tail.isdigit():
        return path, int(tail)
    return location, None


def failing_criteria(findings: list[Finding] | None) -> list[str]:
    """The recorded criterion ids the round's findings fail, first-seen order.

    Read off the findings themselves (#2291): the parser already refused any
    ``fail`` result without an evidenced finding naming its criterion, so the
    kept findings carry the whole failing set and the brief needs no second
    source for it.
    """
    seen: list[str] = []
    for finding in findings or []:
        if finding.criterion and finding.criterion not in seen:
            seen.append(finding.criterion)
    return seen


def _findings_as_comments(findings: list[Finding]) -> list[ReviewComment]:
    """Render evidenced sidecar findings (#1030) as review comments.

    The governed remediation leg feeds the HOST-STORED sidecar findings — never
    the deletable PR comments (F10) — into the same ``build_review_prompt`` the
    standalone ``kew review`` path uses. Each finding becomes one comment keyed to
    its ``file:line`` so the existing per-file grouping and untrusted-fencing
    apply unchanged; the reviewer's severity + summary + executed evidence go in
    the body so the remediator addresses the exact evidenced defect.
    """
    comments: list[ReviewComment] = []
    for idx, finding in enumerate(findings):
        path, line = _split_location(finding.location)
        criterion = f"[{finding.criterion}] " if finding.criterion else ""
        body = (
            f"[{finding.severity}] {criterion}{finding.summary}\n"
            f"(reviewer evidence: `{finding.evidence_command}` "
            f"→ {finding.evidence_output_digest})"
        )
        comments.append(
            ReviewComment(
                id=idx,
                author="adversarial-reviewer",
                body=body,
                path=path,
                line=line,
                state="",
                in_reply_to=None,
                resolved=False,
            )
        )
    return comments


def _branch_owner_item(store: RunStore | None, branch: str) -> str | None:
    """Return the tracker item the PR's head branch belongs to, or None.

    A hand-started ``kew review <PR>`` is handed no item (#2446). Without one
    the ownership check below sees the branch's implementation runs as foreign
    owners and the remediation row names no branch, so its publication records
    no intent and the next round is refused until an operator attests the head
    by hand — onto whichever run the heuristic names, not the one that pushed.
    This resolves whose branch it is for that check alone. The row keeps its own
    ``item_id``: on a standalone review that field is the key the fleet counts
    the run by, so writing the branch's item into it would hand this review the
    worker slot of whatever else is live on that item.

    The ledger's PR linkage is the only answer read, and it works on any
    tracker: the branch's retained runs name one tracker item. Two items on one
    branch resolve to no owner, exactly as the ownership guard treats them, and
    so does a branch whose runs name no item — that branch is already branch
    scoped, and adopting an item for it would make the next round's ownership
    read ambiguous instead of binding. A branch the ledger retains no run for
    needs no owner either: the ownership check it feeds already names such a
    branch whatever item it is handed, so naming one here buys nothing (a
    branch-prefix fallback was carried here and removed for that reason).

    A run on the PR opened from the branch (a ``ci-fix``, an older review row)
    records the PR number as its item, which made an ``agent/issue-N`` branch
    read as owned by N and its PR, so the review's claim landed in a scope the
    operator could not attest under (#3193). Such a run is set aside here: it
    served the owner's PR and says nothing about who the owner is.
    """
    if store is None:
        return None  # no ledger to own anything, and no row to write
    from kew.publication_credentials import publication_run_item, serves_its_own_pr

    try:
        rows = publication_branch_runs(store, branch=branch)
    except Exception as exc:  # noqa: BLE001 — refused below, never swallowed
        # Resolving no owner is not "unowned": the ownership check reads the
        # same evidence next and refuses the remediation before launch, naming
        # the unreadable read. Say why this resolution gave up first.
        _warn(
            f"Review of {branch!r}: cannot read the branch's runs to resolve "
            f"its owning item ({exc}); the ownership check refuses next."
        )
        return None
    owners = {
        owner
        for row in rows
        if (owner := publication_run_item(row)) and not serves_its_own_pr(row)
    }
    return owners.pop() if len(owners) == 1 else None


def _remediation_row_branch(
    store: RunStore | None, item_id: str | None, branch: str
) -> str:
    """Return the PR branch when the remediation preserves its ownership.

    Item-scoped remediation must match the branch's retained tracker item;
    :func:`_owned_remediation_row_branch` resolves one for a review handed none.
    A review of a branch with no tracker item at all uses branch scope.
    Their PR-number accounting keys do not identify tracker items.
    Conflicting items retain an empty branch and the legacy producer checks.
    Unreadable ownership or evidence overflow refuses remediation before launch.
    The duplicate-PR guard skips these PR-subject run kinds.
    """
    if store is None:
        return ""
    from kew.publication_credentials import publication_run_item, serves_its_own_pr

    try:
        rows = publication_branch_runs(store, branch=branch)
    except Exception as exc:  # noqa: BLE001 — refused below, never swallowed
        # Unreadable ownership evidence is not "no other owner": the producer
        # guard refuses unreadable evidence the same way, and a head published
        # without its lineage recorded could never be bound afterwards.
        _warn(
            f"Review of {branch!r}: cannot read the branch's runs ({exc}); "
            "refusing the remediation before launch."
        )
        raise CredentialPublicationRefused("publication_evidence_unavailable") from exc
    if len(rows) > _MAX_BRANCH_RUNS:
        # The branch already exceeds the guard's evidence window (a row landed
        # after the producer lookup that admitted this review); the guard
        # refuses every later lookup on it, so no head published now could be
        # reviewed, whichever item the row would carry.
        _warn(
            f"Review of {branch!r}: the branch carries {len(rows)} runs, past the "
            f"producer guard's evidence window ({_MAX_BRANCH_RUNS}); no head "
            "published now could be bound for the next round, so the "
            "remediation is refused before launch."
        )
        raise branch_run_cap_refusal()
    others = {
        owner
        for row in rows
        if (owner := publication_run_item(row))
        and owner != item_id
        and not serves_its_own_pr(row)
    }
    if others:
        _warn(
            f"Review of {branch!r}: item {item_id!r} is not the branch's owner "
            f"({', '.join(sorted(others))}); the remediation row names no "
            "branch, so the next round falls back to the newest-run heuristic."
        )
        return ""
    if len(rows) >= _MAX_BRANCH_RUNS:
        # Only a row that would name the branch counts against the guard's
        # evidence window (an empty-branch row above adds nothing). The guard
        # counts at most _MAX_BRANCH_RUNS potential producers and refuses a deeper
        # history outright: this row would be the one too many, and a row kept
        # off the branch could not bind its head either (protected history
        # then refuses the next round as ambiguous). Paid work whose head can
        # never be reviewed is refused before launch instead.
        _warn(
            f"Review of {branch!r}: the branch already carries {len(rows)} runs, "
            f"the producer guard's evidence window ({_MAX_BRANCH_RUNS}); a "
            "remediation head could not be bound for the next round, so the "
            "remediation is refused before launch."
        )
        raise branch_run_cap_refusal()
    return branch


def _owned_remediation_row_branch(
    store: RunStore | None, item_id: str | None, branch: str
) -> str:
    """Return the branch the remediation row records, resolving its owner first.

    A governed remediation is handed its item by the epic loop. A hand-started
    ``kew review <PR>`` is handed none, so the PR's owning item is resolved from
    the head branch (#2446) for the ownership check: the row then names the
    branch the way a governed remediation's does, its publication records an
    item-scoped intent under the owner, and the next round binds the head to the
    run that pushed it without a manual ``kew publication attest``.

    The resolved owner reaches the ownership check only. The row's ``item_id``
    stays the one the caller was handed — a standalone review's is its fleet
    concurrency key, never a tracker item — so adopting a branch owner can never
    draw another live run's worker slot (``parallel.max_workers``).
    """
    owner = item_id if item_id is not None else _branch_owner_item(store, branch)
    return _remediation_row_branch(store, owner, branch)


def dispatch_review(
    *,
    pr_number: int,
    config: DispatchConfig,
    author_filter: str | None = None,
    dry_run: bool = False,
    store: RunStore | None = None,
    emit_events: bool = False,
    log_file_path: Path | None = None,
    force_budget: bool = False,
    item_id: str | None = None,
    findings: list[Finding] | None = None,
    on_outcome: Callable[[ReviewOutcome], None] | None = None,
    reservation_held: bool = False,
    declaration: DeclarationRequest | None = None,
    proposal: DeclarationRequest | None = None,
) -> CommandOutcome:
    """Orchestrate the full review dispatch cycle.

    1. Get PR metadata and extract branch name / issue number.
    2. Fetch review comments.
    3. If no comments, report success (nothing to do).
    4. If dry_run, print grouped comments and report success.
    5. Build review prompt, checkout worktree, execute claude via run_tracked_agent.
    6. On success: clean up the worktree and report success.
    7. On failure: preserve worktree, return exit_code.

    Args:
        pr_number: The PR number to review.
        config: Dispatch configuration.
        author_filter: If set, only include comments from this author.
        dry_run: If True, show comments without dispatching.
        store: Optional run store for tracking run lifecycle.
        emit_events: Deprecated — run_tracked_agent handles event emission internally.
        log_file_path: Deprecated — run_tracked_agent manages log file paths internally.
        force_budget: If True, skip budget pre-check before dispatching.
        item_id: When set, activates governed mode (F3/F5 — Task 9): the run
            carries the node's item_id, uses run_kind ``review_remediation``,
            reserves under the review pseudo-epic, and draws budget from the
            review pool (max_review_usd) rather than the per-issue cap.
            Standalone ``kew review <PR>`` callers leave this None.
        findings: When set (the governed NEEDS_CHANGES remediation leg, #1030),
            the review input is these host-stored sidecar findings rather than
            the PR's GitHub comments — the source of truth is the attested
            sidecar, never a deletable comment (F10). The GitHub-comment fetch is
            skipped entirely; an empty list is a no-op (nothing to remediate).
        on_outcome: Receives the terminal result once, after the audit attempt.
            The driver holds admission refusals even if cleanup fails.
        reservation_held: The governed caller already holds the item's review
            reservation (a detached review-stage child, #2459): the run takes
            none of its own and releases none — the holder does both.
        declaration: Set when the round's only blocking findings are test-change
            answers a **Behavior changes** line settles (#2872): the brief asks
            for those lines in the report and no code change, and the lines the
            report declares are carried into the PR body.
        proposal: Set when the round's findings ask only for **Behavior
            changes** lines but no flagged-test record makes it a declaration
            round (#3493): the brief makes one proposed line per test the whole
            job, and the lines reach the PR body's proposed block alone.

    Returns:
        Execution state and raw exit code; the CLI maps these to public codes.
    """
    review = _ReviewDispatch(
        pr_number=pr_number,
        config=config,
        author_filter=author_filter,
        store=store,
        force_budget=force_budget,
        item_id=item_id,
        findings=findings,
        on_outcome=on_outcome,
        reservation_held=reservation_held,
        declaration=declaration,
        proposal=proposal,
    )
    for stage in (review._metadata, review._comments):
        outcome = stage()
        if outcome is not None:
            return outcome
    if dry_run:
        return review._summarize()
    review._prepare()
    try:
        for stage in (
            review._producer,
            review._budget,
            review._connectivity,
            review._reserve,
            review._route,
            review._occupied,
            review._checkout,
            review._launch,
        ):
            outcome = stage()
            if outcome is not None:
                return review._with_run_id(outcome)
        return review._with_run_id(review._complete())
    finally:
        # A hand-run review gives its reservation back on every way out,
        # an interrupt included; ``_finish`` has already done so on the rest.
        review._release_standalone_reservation()


@dataclass
class _ReviewDispatch:
    """Keep review state together across admission, execution, and publication."""

    pr_number: int
    config: DispatchConfig
    author_filter: str | None
    store: RunStore | None
    force_budget: bool
    item_id: str | None
    findings: list[Finding] | None
    on_outcome: Callable[[ReviewOutcome], None] | None = None
    reservation_held: bool = False
    declaration: DeclarationRequest | None = None
    proposal: DeclarationRequest | None = None
    _outcome_delivered: bool = False
    producer_run_id: str | None = None
    _reservation_run_id: str | None = None
    _reservation_item: str | None = None
    """The item a hand-run review reserved, which is not ``item_id`` (#3208)."""
    meta: dict[str, Any] = field(init=False)
    branch_name: str = field(init=False)
    pr_title: str = field(init=False)
    issue_number: int = field(init=False)
    comments: list[ReviewComment] = field(init=False)
    base_branch: str = field(init=False)
    effective_run_kind: str = field(init=False)
    checkout: ReviewCheckout = field(init=False)
    worktree_dir: Path = field(init=False)
    conflict_info: ConflictInfo | None = field(init=False)
    resolved: ResolvedRoute = field(init=False)
    inference_transport: ResolvedInferenceTransport = field(init=False)
    containment: RemediationContainment = field(init=False)
    publication_mode: Literal["canonical", "checkout"] = field(init=False)
    host_publishes: bool = field(init=False)
    flow_run_id: str = field(init=False)
    result: DispatchResult = field(init=False)
    published_sha: str = field(init=False)
    #: The tree of *published_sha*, as the publication proved it (#3276).
    published_tree: str = field(init=False, default="")
    already_published: bool = field(init=False)
    launch_state: LaunchState = field(default_factory=LaunchState)

    def _with_run_id(self, outcome: CommandOutcome) -> CommandOutcome:
        """Name the run this review minted, for the ``--json`` summary (#2505)."""
        run_id = getattr(self, "flow_run_id", None)
        return replace(outcome, run_id=run_id) if run_id else outcome

    def _metadata(self) -> CommandOutcome | None:
        # 1. Get PR metadata
        meta: dict[str, Any] | None = _get_pr_metadata(
            self.pr_number, self.config.project.repo
        )
        if meta is None:
            _error(f"Could not fetch metadata for PR #{self.pr_number}")
            return CommandOutcome("not_run", 1)
        self.meta = meta

        self.branch_name: str = self.meta.get("headRefName", "")
        if not self.branch_name:
            _error(f"PR #{self.pr_number} has no head branch (headRefName missing)")
            return CommandOutcome("not_run", 1)

        pr_body: str = self.meta.get("body", "") or ""
        self.pr_title: str = self.meta.get("title", "") or f"PR #{self.pr_number}"

        issue_number: int | None = _resolve_issue_number(pr_body, self.branch_name)
        if issue_number is None:
            issue_number = self.pr_number
        self.issue_number = issue_number
        return None

    def _comments(self) -> CommandOutcome | None:
        # 2. Determine the review input: host-stored sidecar findings (governed
        #    remediation, #1030) OR the PR's GitHub comments (standalone path).
        if self.findings is not None:
            self.comments = _findings_as_comments(self.findings)
        else:
            try:
                self.comments = fetch_review_comments(
                    pr_number=self.pr_number,
                    config=self.config,
                    include_resolved=self.config.review.include_resolved,
                    author_filter=self.author_filter,
                )
            except Exception as exc:
                _error(
                    f"Failed to fetch review comments for PR #{self.pr_number}: {exc}"
                )
                return CommandOutcome("not_run", 1)

        # 3. No input → nothing to do
        if not self.comments:
            _log(f"PR #{self.pr_number}: no review comments to address.")
            return CommandOutcome("executed", 0)
        return None

    def _summarize(self) -> CommandOutcome:
        grouped = group_comments_by_file(self.comments)
        _log(
            f"PR #{self.pr_number}: {len(self.comments)} comment(s) across "
            f"{len(grouped)} file(s)"
        )
        for file_path, file_comments in grouped.items():
            label = file_path or "(general)"
            _log(f"  {label}: {len(file_comments)} comment(s)")
            for c in file_comments:
                _log(f"    @{c.author}: {c.body[:80]}")
        return CommandOutcome("executed", 0)

    def _prepare(self) -> None:
        # 5. Determine base branch — prefer PR's actual base ref over config/auto-detect
        self.base_branch = self.meta.get("baseRefName") or detect_base_branch(
            self.config.branches.base
        )

        self._reservation_run_id = None

    def _finish(
        self, outcome: ReviewOutcome, *, checkout: ReviewCheckout | None = None
    ) -> None:
        # Deliver once before cleanup can fail and reenter finalization.
        if self._outcome_delivered:
            return
        self._outcome_delivered = True
        try:
            try:
                record_review_outcome(outcome, store=self.store, config=self.config)
            finally:
                if self.on_outcome is not None:
                    self.on_outcome(outcome)
        finally:
            try:
                if checkout is not None:
                    if outcome.stage == STAGE_PRE_LAUNCH and self._agent_never_ran():
                        # No agent ran in it, in any attempt: nothing to debug,
                        # nothing for the next launch to preserve (#2961).
                        finish_command_cleanup(
                            partial(discard_review_checkout, checkout),
                            command=f"review PR #{self.pr_number}",
                        )
                    finish_command_cleanup(
                        checkout.release, command=f"review PR #{self.pr_number}"
                    )
            finally:
                self._release_reservation()

    def _release_reservation(self) -> None:
        # Release only our generation, never another actor's row.
        if self.store is None or self._reservation_run_id is None:
            return
        try:
            self.store.release_epic_node_item(
                self._reservation_item or self.item_id or "",
                run_id=self._reservation_run_id,
            )
        except Exception as exc:
            _warn(
                f"Failed to release review reservation "
                f"{_REVIEW_EPIC_REF}/{self.item_id} "
                f"(run_id={self._reservation_run_id}): {exc}"
            )
            raise

    def _release_standalone_reservation(self) -> None:
        if self._reservation_item is not None:
            self._release_reservation()

    def _failed(
        self,
        stage: str,
        classification: str,
        detail: str,
        *,
        exit_code: int = 1,
        head_sha: str = "",
    ) -> ReviewOutcome:
        return ReviewOutcome(
            pr_number=self.pr_number,
            status=STATUS_FAILED,
            stage=stage,
            classification=classification,
            detail=detail,
            branch=self.branch_name,
            head_sha=head_sha,
            item_id=self.item_id,
            exit_code=exit_code,
        )

    def _occupied(self) -> CommandOutcome | None:
        # Refuse a hand-started review of a PR the drive already holds (#3047),
        # before a worktree exists: the second launch would otherwise fail late
        # on the container name and report that instead. It is the last
        # admission step, right before the checkout, so the window between
        # this check and the launch it guards is as short as it gets without a
        # checkout to undo. The check is followed by the lock: the item's
        # review reservation, the one the drive takes before it reviews or
        # remediates (#3208), so the drive sees this review and either side
        # refuses the other whatever their containers are named.
        if self.item_id is not None:
            return None  # a governed launch is the holder's own
        owner = _branch_owner_item(self.store, self.branch_name)
        occupant = review_occupant(
            self.pr_number,
            items=(owner, str(self.issue_number)),
            config=self.config,
            store=self.store,
        )
        if occupant is None:
            occupant = self._reserve_standalone(owner or str(self.issue_number))
        if occupant is None:
            return None
        message = f"Review of PR #{self.pr_number} refused: {occupant}"
        _error(message)
        self._finish(self._failed(STAGE_PRE_LAUNCH, "review_in_flight", occupant))
        return CommandOutcome("not_run", 1, message)

    def _reserve_standalone(self, item: str) -> str | None:
        """Reserve *item* for this hand-run review, or name who holds it."""
        if self.store is None:
            return None
        run_id = str(uuid.uuid4())
        holder = reserve_standalone_review(
            self.store,
            item,
            run_id,
            pr_number=self.pr_number,
            repo=self.config.project.repo or None,
        )
        if holder is None:
            self._reservation_item = item
            self._reservation_run_id = run_id
            # The run row is keyed to the reservation, so the drive's
            # reconciler resolves it and can prove its owner dead.
            self.flow_run_id = run_id
        return holder

    def _producer(self) -> CommandOutcome | None:
        if self.store is not None:
            try:
                self.producer_run_id = publication_producer_for_head(
                    self.store,
                    branch=self.branch_name,
                    head_sha=self.meta.get("headRefOid", "") or "",
                )
            except CredentialPublicationRefused as exc:
                _error(
                    f"Review of PR #{self.pr_number} was refused before launch: {exc}"
                )
                self._finish(
                    self._failed(
                        STAGE_PRE_LAUNCH,
                        CREDENTIAL_PUBLICATION_REFUSAL,
                        str(exc),
                        head_sha=self.meta.get("headRefOid", "") or "",
                    )
                )
                return CommandOutcome("not_run", 1)
            self._producer_issue_number()
        return None

    def _producer_issue_number(self) -> None:
        assert self.store is not None
        producer = (
            self.store.get(self.producer_run_id) if self.producer_run_id else None
        )
        if producer is not None and producer.run_kind in PR_SUBJECT_RUN_KINDS:
            producer = None  # a remediation's issue_num is the PR it reviewed
        stored_issue_number = None if producer is None else producer.issue_num
        if isinstance(stored_issue_number, int) and stored_issue_number > 0:
            self.issue_number = stored_issue_number

    def _budget(self) -> CommandOutcome | None:
        # 6. Pre-flight checks (before worktree checkout to avoid orphaned worktrees)
        #
        # Governed mode (item_id set + store available): review-pool budget and
        # REVIEW_EPIC_REF reservation.  Standalone mode: per-issue impl cap only.
        if self.item_id is not None and self.store is not None:
            # Governed remediation: draws from the remediation pool
            # (max_remediation_usd), NOT the adversarial-reviewer pool
            # (max_review_usd) and NOT the per-issue implementation cap.
            # The two review pools have very different cost profiles: one reviewer
            # pass vs. a full fix cycle (#993).
            if not self.force_budget:
                try:
                    check_review_budget(
                        self.store,
                        self.item_id,
                        max_review_usd=self.config.review.max_remediation_usd,
                    )
                except BudgetExceeded as exc:
                    _error(f"Review skipped (remediation pool exhausted): {exc}")
                    self._finish(
                        self._failed(STAGE_PRE_LAUNCH, "budget_exhausted", str(exc))
                    )
                    return CommandOutcome("not_run", 1)
                # ...and the daily and weekly caps, counting this launch at its
                # ceiling (#3205). Checked again where the row is admitted.
                try:
                    check_window_budget(
                        self.store,
                        self.config.budget,
                        reservation_usd=self.config.review.max_remediation_usd,
                        count_admitting_run=True,
                    )
                except BudgetExceeded as exc:
                    return self._window_refused(exc)
        elif self.store is not None and not self.force_budget:
            try:
                check_budget(self.store, self.config.budget, self.pr_number)
            except BudgetExceeded as exc:
                _error(f"Review skipped: {exc}")
                self._finish(
                    self._failed(STAGE_PRE_LAUNCH, "budget_exhausted", str(exc))
                )
                return CommandOutcome("not_run", 1)
        return None

    def _window_refused(
        self, exc: BudgetExceeded, *, checkout: ReviewCheckout | None = None
    ) -> CommandOutcome:
        """A daily or weekly cap refused this run before launch (#3205)."""
        reason = window_budget_hold_reason(exc)
        _error(f"Review of PR #{self.pr_number} skipped: {reason}")
        self._finish(
            self._failed(STAGE_PRE_LAUNCH, WINDOW_BUDGET_REFUSAL, reason),
            checkout=checkout,
        )
        return CommandOutcome("not_run", 1)

    def _connectivity(self) -> CommandOutcome | None:
        try:
            reachable = _check_github_connectivity(self.config)
        except GitHubAuthError as exc:
            _error(f"Review skipped: {exc}")
            self._finish(self._failed(STAGE_PRE_LAUNCH, "github_auth", str(exc)))
            return CommandOutcome("not_run", 1)
        if not reachable:
            _error("Review skipped: GitHub is unreachable")
            self._finish(
                self._failed(
                    STAGE_PRE_LAUNCH, "github_unreachable", "GitHub is unreachable"
                )
            )
            return CommandOutcome("not_run", 1)
        return None

    def _reserve(self) -> CommandOutcome | None:
        # Governed mode: reserve a slot under the review pseudo-epic BEFORE
        # launching the run so the reconciler can find it (F3/#978).
        # Reuse its ID for the run and any pre-launch refusal.
        #
        # Honor the reserve result (F1/#987): item_id is UNIQUE across the whole
        # epic_reservations table, so a False return means another actor already
        # holds it — an unreleased implementation reservation, a concurrent driver
        # tick, or a stale prior-round generation.  Running anyway would let the
        # terminal release DELETE that foreign reservation (the #975-class
        # false-release this epic exists to prevent).  Refuse instead; the
        # reconciler clears a genuinely stale reservation and a later tick retries.
        if (
            self.item_id is not None
            and self.store is not None
            and not self.reservation_held
        ):
            self._reservation_run_id = str(uuid.uuid4())
            if not self.store.reserve_epic_node_item(
                _REVIEW_EPIC_REF, self.item_id, self._reservation_run_id
            ):
                _error(
                    f"Review skipped for PR #{self.pr_number}: item "
                    f"{self.item_id} is already "
                    f"reserved (concurrent dispatch or unreleased reservation)"
                )
                # The generation-conditional release below is a no-op here: the row
                # belongs to whoever won the reservation, not to us.
                self._finish(
                    self._failed(
                        STAGE_PRE_LAUNCH,
                        "reservation_conflict",
                        f"item {self.item_id} is already reserved",
                    )
                )
                return CommandOutcome("not_run", 1)

        self.effective_run_kind = (
            REVIEW_REMEDIATION_RUN_KIND if self.item_id is not None else "review"
        )
        return None

    def _route(self) -> CommandOutcome | None:
        # Resolve admission before checkout claims, fetch, or host preflight.
        self.flow_run_id = self._reservation_run_id or str(uuid.uuid4())
        try:
            self.resolved = resolve_runner(self.config)
            # The container supplies the runner for a sandboxed route.
            self.inference_transport = resolve_inference_transport(
                self.config, self.resolved.runner, self.resolved.model
            )
            self.containment = resolve_remediation_containment(
                self.config,
                runner=self.resolved.runner.name,
                # The same ledger this flow attests its posture onto is the one
                # the OpenShell egress gate reads its proof from (#2389 review).
                store=self.store,
                inference_transport=self.inference_transport,
            )
        except SandboxRuntimeUnavailable as exc:
            return self._containment_refused(exc)
        except Exception as exc:
            # Nothing has launched: a route failure is a pre-launch refusal.
            _error(f"Review dispatch for PR #{self.pr_number} failed: {exc}")
            self._finish(self._failed(STAGE_PRE_LAUNCH, "launch_refused", str(exc)))
            return CommandOutcome("not_run", 1)
        return None

    def _containment_refused(self, exc: SandboxRuntimeUnavailable) -> CommandOutcome:
        try:
            record_remediation_refusal(
                self.store,
                self.config,
                exc,
                runner=self.resolved.runner.name,
                run_id=self.flow_run_id,
                run_kind=self.effective_run_kind,
                issue_num=self.pr_number,
                item_id=self.item_id if self.item_id is not None else "",
                branch=self.branch_name,
            )
        except Exception as audit_exc:
            _error(
                f"Failed to record containment refusal {self.flow_run_id}: {audit_exc}"
            )
        finally:
            self._finish(
                self._failed(
                    STAGE_PRE_LAUNCH,
                    "containment_refused",
                    str(exc),
                    exit_code=2,
                    head_sha=self.meta.get("headRefOid", "") or "",
                )
            )
        _error(f"Review of PR #{self.pr_number} was refused before launch: {exc}")
        # Raw exit 2, the code the refused run row and the outcome carry.
        return CommandOutcome("not_run", 2, str(exc))

    def _checkout(self) -> CommandOutcome | None:
        # 7. Materialize a review checkout that can neither collide with nor share
        #    an implementation worktree (#1290).  A branch already checked out
        #    elsewhere yields a DETACHED review checkout at the recorded PR head
        #    instead of a second `git worktree add -b` (which exits 255).
        try:
            _log(
                f"Checking out worktree for PR #{self.pr_number} (branch: "
                f"{self.branch_name})..."
            )
            self.checkout = materialize_review_checkout(
                pr_number=self.pr_number,
                branch_name=self.branch_name,
                config=self.config,
                base_branch=self.base_branch,
                expected_head_sha=self.meta.get("headRefOid") or None,
                store=self.store,
            )
        except ReviewCheckoutError as exc:
            _error(f"Review pre-launch failed for PR #{self.pr_number}: {exc}")
            self._finish(self._failed(STAGE_PRE_LAUNCH, exc.classification, str(exc)))
            return CommandOutcome("not_run", 1)
        except Exception as exc:  # noqa: BLE001 — the reservation must not leak
            _error(f"Review pre-launch failed for PR #{self.pr_number}: {exc}")
            self._finish(self._failed(STAGE_PRE_LAUNCH, "checkout_unusable", str(exc)))
            return CommandOutcome("not_run", 1)

        self.worktree_dir = self.checkout.path
        self.conflict_info = self.checkout.conflict
        return None

    def _prepare_launch(self) -> CommandOutcome | None:
        from kew.runner_preflight import runner_preflight_failure

        failure = (
            runner_preflight_failure(self.config, runner_name=self.resolved.runner.name)
            if self.containment.posture != POSTURE_SANDBOXED
            else None
        )
        if failure is not None:
            _error(failure)
            self._finish(
                self._failed(
                    STAGE_PRE_LAUNCH,
                    "runner_preflight",
                    failure,
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome("not_run", 1)
        _warn_review_lifecycle(self.config, self.resolved.model)
        self.publication_mode = select_publication_mode(
            self.config.publication.publish,
            dangerously_skip_containment=(
                self.config.sandbox.dangerously_skip_containment
            ),
            store=self.store,
            # A held reservation (#2459) is not this run's to name; its own
            # run id carries the governed posture instead, as it did when the
            # run reserved under that same id.
            run_id=(
                self.flow_run_id if self.reservation_held else self._reservation_run_id
            ),
            issue_num=self.pr_number,
            item_id=self.item_id or str(self.pr_number),
            branch=self.branch_name,
            repo=self.config.project.repo or None,
        )
        # ONE predicate for "the host publishes this", used by both the prompt
        # and the publication decision. They were computed separately and
        # disagreed in the detached case (#1485): the prompt told an agent-style
        # runner to `git push origin <branch>` while the host also intended to
        # publish. A detached review clone inherits a LOCAL branch of that name
        # from the operator's checkout (git clone creates one for the source's
        # HEAD, and `detached` is true precisely when a worktree holds the
        # branch), pointing at the operator's commit rather than the PR head.
        # The agent following that instruction pushed the operator's unpushed
        # commits to the PR and left its own remediation behind on detached
        # HEAD. Keeping the two in one name is the fix; they cannot drift again.
        #
        # A CONTAINED run is the same shape for a different reason (#1673): the
        # container deliberately holds no GitHub token, so an agent told to push
        # from inside one cannot, and its commits died with the clone that
        # `discard_checkout` deletes on the success path. The host has the
        # credential and the same bind-mounted tree the container wrote to, so
        # it publishes through the same canonical broker as dispatch, before
        # cleanup.
        # Under canonical publication the host publishes for every runner,
        # so the prompt says not to push and the child env carries no
        # publication credential (review round 6).
        self.host_publishes = (
            self.resolved.runner.publication_style == "host"
            or self.checkout.detached
            or self.containment.contained
            or self.publication_mode == "canonical"
        )
        attest_flow_posture(
            self.store,
            flow="review remediation",
            runner=self.resolved.runner.name,
            config=self.config.sandbox,
            posture=self.containment.posture,
            image=self.containment.image,
            reason=self.containment.reason,
            run_id=self.flow_run_id,
            checkout_dir=self.worktree_dir,
            repo=self.config.project.repo or None,
            issue_num=self.pr_number,
            item_id=self.item_id,
            inference_transport=self.inference_transport,
        )
        return None

    def _launch_agent(self) -> None:
        # Only kew's own container runs under the memory cap (#2984). A native
        # runner isolates itself on the host and keeps the host's memory.
        capped = self.containment.posture == POSTURE_SANDBOXED
        # Off the host store, never the checkout under review (#3023).
        addendum = launch_addendum(self.store, "review_remediation")
        targets, targets_scoped = (
            round_test_targets(
                self.worktree_dir,
                self.base_branch,
                comments=self.comments,
                conflict_info=self.conflict_info,
            )
            if self.item_id is not None or capped
            else ([], False)
        )
        prompt = build_review_prompt(
            pr_number=self.pr_number,
            issue_number=self.issue_number,
            branch_name=self.branch_name,
            comments=self.comments,
            config=self.config,
            base_branch=self.base_branch,
            conflict_info=self.conflict_info,
            host_publication=self.host_publishes,
            governed=self.item_id is not None,
            sandboxed=capped,
            failing_criterion_ids=failing_criteria(self.findings),
            declaration=self.declaration,
            proposal=self.proposal,
            profile_addendum=addendum,
            targeted_tests=targets,
            targets_scoped=targets_scoped,
        )

        # Surface the effective cap so an operator sees it before spending.
        # Governed remediations draw from max_remediation_usd; standalone
        # reviews have no review-pool cap (they use the per-issue impl cap).
        if self.item_id is not None:
            _log(
                f"Dispatching review for PR #{self.pr_number} "
                f"(remediation pool cap: "
                f"${self.config.review.max_remediation_usd:.2f})..."
            )
        else:
            _log(f"Dispatching review for PR #{self.pr_number}...")
        # In governed mode the runtime kill cap is max_remediation_usd, not the
        # global agent.max_budget_usd — a single remediation can legitimately
        # cost more than a normal implementation run (#993).
        remediation_budget_override = (
            self.config.review.max_remediation_usd if self.item_id is not None else None
        )
        # That cap reaches claude-code as an argv flag on the host, and NOT
        # inside a container — the container command omits it. Without a
        # kew-side watchdog, containing this flow would delete the cap (#1462).
        effective_cap_usd = (
            remediation_budget_override
            if remediation_budget_override is not None
            else self.config.agent.max_budget_usd
        )
        if self.store is not None and self.producer_run_id:
            ensure_publication_credentials(
                self.store,
                run_id=self.producer_run_id,
                producer_run_id=self.producer_run_id,
            )
        # Resolved BEFORE the row is written: a hand-started review names the
        # branch it was handed no item for, so its publication records an
        # item-scoped intent under the branch's owner (#2446).
        row_branch = _owned_remediation_row_branch(
            self.store, self.item_id, self.branch_name
        )
        # The reservation a hand-run review took is the slot this run starts
        # under: the fleet cap must not count it as a second worker.
        from kew.graph.driver.review_detach import holding_reservation

        with holding_reservation(
            self._reservation_run_id if self._reservation_item else None
        ):
            self.result = self._run_agent(
                prompt,
                row_branch,
                remediation_budget_override=remediation_budget_override,
                effective_cap_usd=effective_cap_usd,
                addendum=addendum,
            )

    def _run_agent(
        self,
        prompt: str,
        row_branch: str,
        *,
        remediation_budget_override: float | None,
        effective_cap_usd: float,
        addendum: Any,
    ) -> DispatchResult:
        return run_tracked_agent(
            launch_state=self.launch_state,
            prompt=prompt,
            worktree_dir=self.worktree_dir,
            config=self.config,
            issue_num=self.pr_number,
            store=self.store,
            title=self.pr_title,
            branch=row_branch,
            model=self.resolved.model,
            runner=self.resolved.runner,
            complexity="medium",  # reviews lack issue labels; default to medium
            run_kind=self.effective_run_kind,
            run_id=self.flow_run_id,
            item_id=self.item_id,
            source_tracker=self.config.tracker.type,
            worker_cap=self.config.fleet_concurrency(),
            cap_exclude_issue=self.pr_number,
            budget_override_usd=remediation_budget_override,
            contained_image=self.containment.image,
            inference_transport=self.inference_transport,
            sandbox_posture=self.containment.posture,
            prompt_profile_version_id=profile_version_id(addendum),
            validate_continuation=False,
            force_budget=self.force_budget,
            budget_check=budget_watchdog(
                effective_cap_usd,
                runner_name=self.resolved.runner.name,
                contained=self.containment.contained,
            ),
        )

    def _launch(self) -> CommandOutcome | None:
        # 8. Execute via run_tracked_agent
        try:
            failure = self._prepare_launch()
            if failure is not None:
                return failure
            self._launch_agent()
            return None
        except WorkerCapReached:
            _error(WORKER_CAP_REACHED_MESSAGE)
            self._finish(
                self._failed(
                    STAGE_PRE_LAUNCH,
                    "worker_cap_reached",
                    WORKER_CAP_REACHED_MESSAGE,
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome("not_run", 1)
        except BudgetExceeded as exc:
            return self._window_refused(exc, checkout=self.checkout)
        except CredentialPublicationRefused as exc:
            _error(f"Review of PR #{self.pr_number} failed: {exc}")
            self._warn_preserved(self._failure_stage())
            self._finish(
                self._failed(
                    self._failure_stage(),
                    CREDENTIAL_PUBLICATION_REFUSAL,
                    str(exc),
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome(self.launch_state.kind, 1)
        except (PublicationError, TransportAdmissionError) as exc:
            _error(f"Review of PR #{self.pr_number} failed: {exc}")
            self._warn_preserved(self._failure_stage())
            self._finish(
                self._failed(
                    self._failure_stage(),
                    PUBLICATION_CONFIGURATION_REFUSAL,
                    str(exc),
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome(self.launch_state.kind, 1)
        except PromptConfigurationError as exc:
            return self._prompt_refused(exc)
        except Exception as exc:
            _error(f"Review dispatch for PR #{self.pr_number} failed: {exc}")
            self._finish(
                self._failed(
                    self._failure_stage(),
                    "dispatch_error"
                    if self.launch_state.kind == "executed"
                    else "launch_refused",
                    str(exc),
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome(self.launch_state.kind, 1)

    def _prompt_refused(self, exc: PromptConfigurationError) -> CommandOutcome:
        """Refuse the remediation the operator's profile stopped (#3023).

        Raised while the prompt is built, so no runner started. Only the
        operator can fix it, so the outcome carries its own category: the
        driver holds the node on it instead of retrying every tick.
        """
        _error(f"Review of PR #{self.pr_number} refused: {exc}")
        try:
            record_prompt_refusal(
                self.store,
                self.config,
                run_id=self.flow_run_id,
                run_kind=self.effective_run_kind,
                issue_num=self.pr_number,
                branch=self.branch_name,
                item_id=self.item_id,
                runner=self.resolved.runner.name,
            )
        finally:
            self._finish(
                replace(
                    self._failed(
                        STAGE_PRE_LAUNCH,
                        PROMPT_CONFIGURATION_REFUSAL,
                        str(exc),
                        exit_code=2,
                        head_sha=self.checkout.head_sha,
                    ),
                    status=STATUS_REFUSED,
                ),
                checkout=self.checkout,
            )
        return CommandOutcome(
            "not_run", 2, str(exc), category=PROMPT_CONFIGURATION_REFUSAL
        )

    def _warn_preserved(self, stage: str) -> None:
        """Say the checkout is kept, unless no agent ran in it and it goes."""
        if stage != STAGE_PRE_LAUNCH or not self._agent_never_ran():
            _warn(f"Worktree preserved for debugging: {self.worktree_dir}")

    def _agent_never_ran(self) -> bool:
        """No attempt of this review launched an agent (#3186).

        Launch evidence is cumulative across attempts, and the final attempt's
        refusal names the stage: a second attempt refused at startup does not
        undo the first attempt's edits, so that checkout is kept. Only a review
        whose every attempt never launched has a checkout nothing ran in.
        """
        result: DispatchResult | None = getattr(self, "result", None)
        return self.launch_state.kind == "not_run" and (
            result is None or result.launched is False
        )

    def _failure_stage(self) -> str:
        return (
            STAGE_RUNNER if self.launch_state.kind == "executed" else STAGE_PRE_LAUNCH
        )

    def _complete(self) -> CommandOutcome:
        if startup_refused(self.result):
            return self._startup_refused()
        if self.result.error_category == "needs_human":
            return self._needs_human_refused()
        if self.result.exit_code == 0:
            return self._publish_and_complete()
        _error(
            f"Review dispatch for PR #{self.pr_number} failed (exit "
            f"{self.result.exit_code}): {self.result.stop_reason}"
        )
        stage = STAGE_RUNNER if self.result.launched is not False else STAGE_PRE_LAUNCH
        self._warn_preserved(stage)
        self._finish(
            self._failed(
                stage,
                "runner_failed"
                if self.result.launched is not False
                else self.result.error_category or "launch_refused",
                self.result.stop_reason or f"runner exited {self.result.exit_code}",
                exit_code=self.result.exit_code,
                head_sha=self.checkout.head_sha,
            ),
            checkout=self.checkout,
        )
        return command_outcome(self.result)

    def _needs_human_refused(self) -> CommandOutcome:
        """Refuse publication the agent stopped for (#2601).

        A remediation that ends ``KEW_NEEDS_HUMAN: <reason>`` — a policy gate it
        could not make pass, say — usually exits 0, so the exit code alone would
        publish the very change the agent refused. The reason names what failed.
        """
        reason = self.result.subtype or "unspecified"
        _error(f"Review dispatch for PR #{self.pr_number} needs a human: {reason}")
        _warn(f"Worktree preserved for debugging: {self.worktree_dir}")
        self._finish(
            self._failed(
                STAGE_RUNNER,
                "needs_human",
                reason,
                exit_code=self.result.exit_code or 1,
                head_sha=self.checkout.head_sha,
            ),
            checkout=self.checkout,
        )
        return CommandOutcome("executed", self.result.exit_code or 1, reason)

    def _startup_refused(self) -> CommandOutcome:
        reason = self.result.stop_reason or "Container refused agent startup"
        _error(reason)
        self._warn_preserved(STAGE_PRE_LAUNCH)
        self._finish(
            self._failed(
                STAGE_PRE_LAUNCH,
                self.result.error_category,
                reason,
                exit_code=self.result.exit_code,
                head_sha=self.checkout.head_sha,
            ),
            checkout=self.checkout,
        )
        return command_outcome(self.result)

    def _require_publication(self) -> None:
        if self.store is not None and (self.result.run_id or self.producer_run_id):
            ensure_publication_credentials(
                self.store,
                run_id=self.result.run_id or self.producer_run_id or "",
                producer_run_id=self.producer_run_id,
            )

    def _publication_context(self) -> dict[str, Any]:
        publication_context: dict[str, Any] = {}
        if self.store is not None:
            publication_context = {
                "store": self.store,
                "run_id": self.result.run_id or None,
                "producer_run_id": self.producer_run_id,
            }
        return publication_context

    def _publish(self) -> None:
        # A merge verify that claimed this head after the run launched would
        # see it move under it (#3198): the finished work waits here, intact,
        # and publishes once the claim ends.
        wait_out_merge_claim(
            self.store,
            self.pr_number,
            self.checkout.head_sha,
            flow="review remediation",
        )
        self._require_publication()
        # A new remediation head needs fresh human CI approval (#2033).
        before_push = partial(revoke_ci_approval, self.branch_name, self.config)
        publication_context = self._publication_context()
        if self.publication_mode == "canonical":
            trees: list[str] = []
            self.published_sha, self.already_published = _canonical_review_publication(
                record_tree=trees.append,
                worktree_dir=self.worktree_dir,
                branch_name=self.branch_name,
                expected_head_sha=self.checkout.head_sha,
                pr_number=self.pr_number,
                repo=self.config.project.repo or None,
                host_publishes=self.host_publishes,
                base_branch=self.base_branch,
                integrated_base_sha=self.checkout.integrated_base_sha,
                before_push=before_push,
                item_id=self.item_id,
                **publication_context,
            )
            self.published_tree = trees[-1] if trees else ""
        elif self.host_publishes:
            if self.store is not None:
                publication_context["item_id"] = self.item_id or str(self.pr_number)
            published = publish_existing_pr_branch(
                self.worktree_dir,
                self.branch_name,
                commit_message=f"fix: address review feedback on PR #{self.pr_number}",
                expected_remote_sha=self.checkout.head_sha,
                repo=self.config.project.repo or None,
                publish=self.publication_mode,
                dangerously_skip_containment=(
                    self.config.sandbox.dangerously_skip_containment
                ),
                before_push=before_push,
                **publication_context,
            )
            self.published_sha = (
                _worktree_head_sha(self.worktree_dir) or self.published_sha
            )
            self.already_published = (
                not published and self.published_sha != self.checkout.head_sha
            )
            self.published_tree = _commit_tree(self.worktree_dir, self.published_sha)
        else:
            self.published_sha = (
                _worktree_head_sha(self.worktree_dir) or self.checkout.head_sha
            )
            self.published_tree = _commit_tree(self.worktree_dir, self.published_sha)

    def _publish_and_complete(self) -> CommandOutcome:
        self.published_sha = self.checkout.head_sha
        self.published_tree = ""
        self.already_published = False
        # A detached review checkout must not be pushed by the runner (the local
        # branch it would push is the operator's, not the PR head), so the host
        # publishes it regardless of the runner's publication style — bound to
        # the head the review was cut at. Same predicate the prompt was built
        # from, so the agent was told not to push.
        try:
            self._publish()
        except CredentialPublicationRefused as exc:
            if self.store is not None and self.result.run_id:
                self.store.transition_run_status(
                    self.result.run_id,
                    expected_status="success",
                    new_status="refused",
                    error_category=CREDENTIAL_PUBLICATION_REFUSAL,
                )
            _error(f"Review of PR #{self.pr_number} was not published: {exc}")
            _warn(f"Worktree preserved for debugging: {self.worktree_dir}")
            self._finish(
                self._failed(
                    STAGE_PUBLICATION,
                    CREDENTIAL_PUBLICATION_REFUSAL,
                    str(exc),
                    head_sha=self.checkout.head_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome("executed", 1)
        except PublicationHeadMovedError as exc:
            detail = f"branch moved ({exc.expected_sha[:12]} -> {exc.actual_sha[:12]})"
            _error(
                f"Review of PR #{self.pr_number} completed but was not published "
                f"because the branch moved: {detail}"
            )
            _warn(f"Worktree preserved for debugging: {self.worktree_dir}")
            self._finish(
                self._failed(
                    STAGE_PUBLICATION,
                    "publication_head_moved",
                    detail,
                    head_sha=self.published_sha,
                ),
                checkout=self.checkout,
            )
            return CommandOutcome("executed", 1)
        except Exception as exc:
            return self._publication_failed(exc)
        self._carry_declarations()
        return self._complete_success()

    def _carry_declarations(self) -> None:
        """Copy the report's requested **Behavior changes** lines to the PR body,
        and its other lines to the body's proposed block.

        The reviewer reads the host report, but the merge's held-out gate reads
        the PR body (#2872). A line no round asked for is only a proposal: it
        goes under **Proposed behavior changes**, where the next reviewer rules
        on it (#3475). Fail-soft: a line not carried leaves the round
        standing, which the driver holds for a person.
        """
        repo = self.config.project.repo
        try:
            report = agent_report_for(self.result.log_file)
            proposed = proposed_lines(report)
            if proposed and repo:
                propose_lines(repo, self.pr_number, proposed)
            if self.declaration is None:
                return
            lines = requested_lines(self.declaration, report)
            if lines and repo:
                carry_declared_lines(repo, self.pr_number, lines)
        except Exception as exc:  # noqa: BLE001 — a completed run stays completed
            _warn(f"Behavior changes lines not carried to PR #{self.pr_number}: {exc}")

    def _publication_failed(self, exc: Exception) -> CommandOutcome:
        """Record a publication that did not land — deferred, or broken.

        The run row is the thing that matters here. ``run_tracked_agent``
        finalizes it as ``success`` when the AGENT exits cleanly, which happens
        before publication runs, and only the credential refusal above ever
        moved it afterwards — so all three of #2352's deferred remediation runs
        read ``status=success`` while the branch head never moved. The driver's
        re-dispatch guard reads that status, so it is corrected FIRST, before
        the best-effort outcome record and the cleanup that follow.

        Two outcomes are deferrals rather than breakages: a publication the
        filter held for a person (#2362), and one the REMOTE failed on its own
        side after every retry (#2455). Both leave the commit built and the
        checkout preserved, so both record ``awaiting_publication`` with the
        evidence the driver holds the node from — never a fresh remediation that
        redoes work which is already on disk.
        """
        flags = deferred_publication_flags(exc)
        transient = None if flags is not None else exhausted_transient_push(exc)
        deferred = flags is not None or transient is not None
        category = REVIEW_PUBLICATION_HELD if deferred else "publication_failed"
        if deferred:
            self._record_deferred_publication()
        self._demote_published_run(category, deferred=deferred)
        self._report_publication_outcome(exc, flags=flags, transient=transient)
        _warn(f"Worktree preserved for debugging: {self.worktree_dir}")
        self._finish(
            self._failed(
                STAGE_PUBLICATION,
                category,
                str(exc),
                head_sha=self.checkout.head_sha,
            ),
            checkout=self.checkout,
        )
        return CommandOutcome("executed", 1)

    def _report_publication_outcome(
        self,
        exc: Exception,
        *,
        flags: tuple[str, ...] | None,
        transient: TransientPushFailure | None,
    ) -> None:
        """Name which of the three publication outcomes this run had."""
        if flags is not None:
            _error(
                f"Publication for PR #{self.pr_number} is held for human review: "
                + "; ".join(flags)
            )
        elif transient is not None:
            _error(
                f"Publication for PR #{self.pr_number} is held: the remote failed "
                f"the push on its own side {transient.attempts} times, so the "
                f"finished work is preserved for another push, not redone: "
                f"{transient.detail}"
            )
        else:
            _error(f"Host publication for PR #{self.pr_number} failed: {exc}")

    def _demote_published_run(self, category: str, *, deferred: bool) -> None:
        """Move this run off ``success``: it published nothing.

        ``awaiting_publication`` for a deferral — the work is intact and a
        person can still publish it — and ``failure`` for a publication that
        broke. The transition is conditional on ``success``, so a row some
        other writer already terminalized is left exactly as it is.
        """
        if self.store is None or not self.result.run_id:
            return
        try:
            self.store.transition_run_status(
                self.result.run_id,
                expected_status="success",
                new_status="awaiting_publication" if deferred else "failure",
                error_category=category,
            )
        except Exception as exc:  # noqa: BLE001 — the outcome record still runs
            _warn(f"could not record the publication outcome on the run row: {exc}")

    def _record_deferred_publication(self) -> None:
        """Leave the driver the two facts a deferral's hold is written from.

        The head this remediation was cut at, so a later tick can tell a LIVE
        deferral from a row whose head has since moved on, and the preserved
        checkout, so the hold names what a person has to look at. The run row
        itself carries neither.
        """
        if self.store is None or not self.result.run_id:
            return
        try:
            self.store.set_meta(
                deferred_publication_key(self.result.run_id),
                json.dumps(
                    {
                        "head": self.checkout.head_sha,
                        "checkout": str(self.worktree_dir),
                        "pr": self.pr_number,
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001 — the status is the hard part
            _warn(f"could not record the deferred publication evidence: {exc}")

    def _complete_success(self) -> CommandOutcome:
        if self.already_published:
            _success(
                f"Review dispatch for PR #{self.pr_number} completed; "
                f"publication was already done at {self.published_sha[:12]}."
            )
        else:
            _success(
                f"Review dispatch for PR #{self.pr_number} completed successfully."
            )
        # A CLONE, not a linked worktree (3b/3c), and it sits on the PR branch
        # by name. Worktree-shaped cleanup deleted the OPERATOR's local copy of
        # that branch — unpushed commits included — while `git worktree remove`
        # silently failed and left the clone on disk (#1487). Discarding the
        # checkout is the whole job here: the branch belongs to the PR, not to
        # kew, so nothing should delete it.
        discard_checkout(self.worktree_dir)
        self._finish(
            ReviewOutcome(
                pr_number=self.pr_number,
                status=STATUS_COMPLETED,
                stage=STAGE_PUBLICATION if self.already_published else STAGE_RUNNER,
                classification="already_published"
                if self.already_published
                else "completed",
                branch=self.branch_name,
                head_sha=self.published_sha,
                item_id=self.item_id,
                cut_head_sha=self.checkout.head_sha,
                # Against the tree kew's base integration handed the agent, the
                # published tree tells an agent's change from kew's own merge
                # of the base (#3276).
                integrated_tree=self.checkout.integrated_tree,
                published_tree=self.published_tree,
            ),
            checkout=self.checkout,
        )
        return CommandOutcome("executed", 0)


def _warn_review_lifecycle(config: DispatchConfig, model: str) -> None:
    from kew.model_eol_snapshot import check_lifecycle

    lifecycle = getattr(config, "model_eol", None)
    entries = getattr(lifecycle, "snapshot_models", {}) if lifecycle else {}
    for warning in check_lifecycle([model], entries):
        _warn(f"Model lifecycle: {warning}")
