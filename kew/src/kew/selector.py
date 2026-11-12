"""Issue selection from GitHub via priority queue.

Queries GitHub issues using the gh CLI, selecting the highest-priority
issue with the 'ready' label.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from kew.config import DispatchConfig, RouteEntry
from kew.dependencies import check_dependencies_resolved, parse_depends
from kew.labels import _update_labels
from kew.models import GhIssue, GhLabel, GhPullRequest
from kew.subprocess_utils import run_with_backoff

if TYPE_CHECKING:
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

_PRIORITY_PATTERN = re.compile(r"p?(\d)", re.IGNORECASE)


class GhQueryError(RuntimeError):
    """A `gh` selection query exited nonzero — the queue state is unknown.

    Raised instead of returning an empty list so a genuine gh failure (an
    expired token, a network outage) is never mistaken for a drained queue.
    An overnight ``kew run --drain`` used to exit "queue empty" the moment the
    token lapsed; this makes that failure loud and gives it a distinct exit
    code (see ``GH_QUERY_FAILURE_EXIT_CODE`` in ``cli``).
    """


def _priority_sort_key(issue: GhIssue) -> int:
    """Return a numeric sort key from priority labels (lower = higher priority).

    Recognises labels like 'p0-critical', 'p1-high', 'P0', 'P2', etc.
    Issues with no priority label sort last (value 9).
    """
    labels = issue.get("labels", [])
    min_priority = 9
    for lbl in labels:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        m = _PRIORITY_PATTERN.match(name)
        if m:
            min_priority = min(min_priority, int(m.group(1)))
    return min_priority


def _run_gh_json(
    cmd: list[str], *, timeout: int = 30, strict: bool = False
) -> list[dict[str, Any]]:
    """Run a gh CLI command and return parsed JSON list, or [] on any error.

    When *strict* is True, a nonzero gh exit (after transient-retry backoff has
    been exhausted), a timeout, or an empty / non-JSON / non-list body raises
    :class:`GhQueryError` rather than degrading to an empty list — so the
    dispatch selection path can tell "queue is empty" apart from "gh failed".
    Non-strict callers (TUI panels) keep the lenient behaviour.
    """
    try:
        result = run_with_backoff(cmd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if strict:
            raise GhQueryError(f"gh query timed out: {' '.join(cmd)}") from exc
        return []
    if strict and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GhQueryError(
            f"gh exited {result.returncode}: {' '.join(cmd)}"
            + (f" — {stderr}" if stderr else "")
        )
    return _parse_gh_list((result.stdout or "").strip(), cmd, strict=strict)


def _parse_gh_list(
    output: str, cmd: list[str], *, strict: bool
) -> list[dict[str, Any]]:
    """Parse a gh JSON-list body; under *strict* a bad body is a failed read.

    A gh that exits 0 with an empty or non-JSON body is a broken gh, not an
    empty queue: strict callers must not select nothing on it (#1436).
    """
    problem: str | None = None
    parsed: Any = None
    if not output:
        problem = "gh returned no output"
    else:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError) as exc:
            problem = f"gh returned invalid JSON ({exc})"
        else:
            if not isinstance(parsed, list):
                problem = f"gh returned {type(parsed).__name__}, not a list"
    if problem is None:
        return list(parsed)
    if strict:
        raise GhQueryError(f"{problem}: {' '.join(cmd)}")
    return []


def _gh_issue_list(
    label: str,
    repo: str,
    extra_args: list[str],
    *,
    limit: int = 100,
    strict: bool = False,
) -> list[GhIssue]:
    """Fetch open issues from GitHub, optionally filtered by label.

    *strict* propagates to :func:`_run_gh_json`: the dispatch selection path
    passes ``strict=True`` so a gh failure surfaces loudly instead of looking
    like an empty queue.
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
    ]
    if label:
        cmd.extend(["--label", label])
    cmd.extend(extra_args)
    return cast(list[GhIssue], _run_gh_json(cmd, strict=strict))


def get_complexity(labels: list[GhLabel]) -> str:
    """Determine issue complexity from labels. Defaults to 'medium'."""
    label_names = {lbl.get("name", "") for lbl in labels if isinstance(lbl, dict)}
    if "complex" in label_names:
        return "complex"
    if "simple" in label_names:
        return "simple"
    return "medium"


def select_next_issue(
    *,
    config: DispatchConfig,
    force_issue: int | None = None,
    exclude: set[int] | None = None,
    sleep: Callable[[float], None] | None = None,
    retry_delay: float = 5.0,
) -> GhIssue | None:
    """Pick the next issue to work on. Returns issue dict or None.

    When the queue looks empty, re-query exactly once after a short delay
    to absorb GitHub label-write lag: ``gh issue edit --add-label ready``
    followed immediately by a list often returns stale (empty) results
    for a few seconds (CS-2, CS-6). ``sleep`` is injectable for tests;
    it defaults to ``time.sleep`` resolved at call time.
    """
    _sleep = sleep if sleep is not None else time.sleep
    repo = config.project.repo

    if force_issue is not None:
        if exclude and force_issue in exclude:
            return None
        result = run_with_backoff(
            [
                "gh",
                "issue",
                "view",
                str(force_issue),
                "--repo",
                repo,
                "--json",
                "number,title,labels,body",
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        try:
            parsed = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return cast(GhIssue, parsed)

    issue = _full_ready_pass(config=config, exclude=exclude)
    if issue is not None:
        return issue

    # Zero-result re-query: absorb GitHub label-write lag. Re-check exactly
    # once before giving up; ``kew run --drain`` already re-polls beyond this.
    _logger.info(
        "Ready queue empty — re-checking once "
        "(GitHub label writes lag list results by a few seconds)..."
    )
    _sleep(retry_delay)
    return _full_ready_pass(config=config, exclude=exclude)


def _full_ready_pass(
    *, config: DispatchConfig, exclude: set[int] | None
) -> GhIssue | None:
    """Run one full selection pass: priority labels then any-ready fallback."""
    # Search by priority label (highest first)
    for label in config.labels.priorities:
        issue = _query_ready_issue(config=config, extra_label=label, exclude=exclude)
        if issue is not None:
            return issue

    # Fallback: any ready issue
    return _query_ready_issue(config=config, exclude=exclude)


def empty_queue_message(config: DispatchConfig) -> str:
    """Build a teaching message shown when no ready issues are found.

    Lists up to three open issues that lack the ready label, each with the
    exact ``gh issue edit`` command to queue it, so a first-run user is not
    left at a dead end (CS-2, CS-6). Falls back to a generic hint when there
    is nothing to label.
    """
    ready_label = config.labels.ready
    # All open issues (no label filter), then subtract already-ready ones.
    # Best-effort: this hint must never crash dispatch, so a gh failure
    # degrades to the generic message below.
    try:
        all_open = _gh_issue_list(
            "",
            config.project.repo,
            extra_args=["--json", "number,title,labels"],
        )
    except Exception:
        all_open = []
    candidates = [
        issue
        for issue in all_open
        if ready_label
        not in {
            lbl.get("name", "")
            for lbl in issue.get("labels", [])
            if isinstance(lbl, dict)
        }
    ]

    if not candidates:
        return (
            f"No ready issues. No open issues without the '{ready_label}' label "
            "to queue — open an issue on GitHub first."
        )

    lines = [
        f"No ready issues. {len(candidates)} open issue(s) exist — "
        "label one to queue it:"
    ]
    for issue in candidates[:3]:
        num = issue.get("number")
        title = issue.get("title", "")
        lines.append(f"  gh issue edit {num} --add-label {ready_label}  # {title}")
    return "\n".join(lines)


def _query_ready_issue(
    *,
    config: DispatchConfig,
    extra_label: str | None = None,
    exclude: set[int] | None = None,
) -> GhIssue | None:
    """Query for a single ready issue, optionally filtered by extra label.

    Exclusion is done in Python, not via --search, because combining
    --search with --label and --json in the gh CLI produces unreliable
    results (GitHub search API index inconsistency).
    """
    batch_size = max(10, len(exclude) + 1) if exclude else 10
    extra_args = [
        "--json",
        "number,title,labels,body",
    ]
    if extra_label:
        extra_args = ["--label", extra_label] + extra_args
    issues = _gh_issue_list(
        config.labels.ready,
        config.project.repo,
        extra_args=extra_args,
        limit=batch_size,
        strict=True,
    )
    return _first_eligible(issues, exclude=exclude, config=config)


def _is_excluded(issue: GhIssue, exclude: set[int] | None) -> bool:
    """Check whether the issue number appears in the exclude set."""
    if not exclude:
        return False
    num = issue.get("number")
    return isinstance(num, int) and num in exclude


def _has_skip_label(issue: GhIssue, config: DispatchConfig) -> bool:
    """Check for the blocked label (dependency not yet resolved) or the
    overlap-hold label (held due to overlapping in-flight work)."""
    label_names = {
        lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)
    }
    return (
        config.labels.blocked in label_names
        or config.labels.overlap_hold in label_names
    )


def _has_epic_label(labels: list[GhLabel], prefix: str = "epic:") -> bool:
    """Return True if any label name starts with *prefix* (default ``"epic:"``).

    Epic-DAG nodes are owned by the driver and must be POSITIVELY excluded
    from the fleet selector, conflict resolver, and auto-unblock — this is
    checked independently of any other label (e.g. ``ready``) an epic node
    may carry, since reset paths (``reset_issue_to_ready``) re-add ``ready``.
    """
    return any(
        isinstance(lbl, dict) and str(lbl.get("name", "")).startswith(prefix)
        for lbl in labels
    )


def _block_on_unresolved_deps(issue: GhIssue, config: DispatchConfig) -> bool:
    """Auto-block: if dependency comments are unresolved, apply the blocked
    label and return True so the issue is skipped."""
    deps = parse_depends(issue.get("body"))
    if not deps or check_dependencies_resolved(deps, config.project.repo):
        return False
    num = issue.get("number")
    if isinstance(num, int):
        with contextlib.suppress(Exception):
            _update_labels(num, config, add=[config.labels.blocked])
    return True


def _first_eligible(
    issues: list[GhIssue],
    *,
    exclude: set[int] | None,
    config: DispatchConfig,
) -> GhIssue | None:
    """Return the first issue not excluded or blocked, or None.

    Also checks ``<!-- kew:depends -->`` comments. If unresolved
    dependencies exist, the ``blocked`` label is applied and the
    issue is skipped.
    """
    for issue in issues:
        if _is_excluded(issue, exclude):
            continue
        if _has_skip_label(issue, config):
            continue
        if _has_epic_label(issue.get("labels", [])):
            continue
        if _block_on_unresolved_deps(issue, config):
            continue
        return issue
    return None


def list_review_prs(*, config: DispatchConfig) -> list[GhPullRequest]:
    """Return open PRs with the pr-pending-review label, sorted oldest first."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        config.project.repo,
        "--label",
        config.labels.pr_pending_review,
        "--state",
        "open",
        "--limit",
        "50",
        "--json",
        "number,title,author,createdAt,comments,statusCheckRollup",
    ]
    prs = cast(list[GhPullRequest], _run_gh_json(cmd))
    return sorted(prs, key=lambda p: p.get("createdAt", ""))


def _effective_default_runner(config: DispatchConfig) -> str:
    """Resolve the effective default runner name from routing/agent config."""
    if config.routing is not None:
        default_entry = config.routing.default
        if isinstance(default_entry, str):
            return default_entry
        if isinstance(default_entry, RouteEntry) and default_entry.runner:
            return default_entry.runner
    return config.agent.runner or "claude-code"


def predict_cluster_key(config: DispatchConfig, complexity: str) -> str:
    """Predict the (runner, model) routing for *complexity* as a cluster key.

    Issues with the same cluster key share a prefix_key and benefit from
    back-to-back dispatch inside the provider cache TTL.
    """
    if config.routing is not None:
        entry = getattr(config.routing, complexity, None) or config.routing.default
        if isinstance(entry, str):
            runner = entry
            model = config.agent.model
        elif isinstance(entry, RouteEntry):
            runner = entry.runner or _effective_default_runner(config)
            model = entry.model or config.agent.model
        else:
            runner = "claude-code"
            model = config.agent.model
    else:
        runner = config.agent.runner or "claude-code"
        model = config.agent.model
    return f"{runner}|{model}"


def _cluster_adjacent(
    issues: list[GhIssue],
    cluster_keys: dict[int, str],
    max_lookahead: int,
) -> list[GhIssue]:
    """Cluster same-key issues adjacently using greedy lookahead."""
    n = len(issues)
    result: list[GhIssue] = []
    used = [False] * n

    i = 0
    while len(result) < n:
        while i < n and used[i]:
            i += 1
        if i >= n:
            break

        result.append(issues[i])
        used[i] = True
        current_key = cluster_keys.get(issues[i].get("number", 0), "")

        if current_key:
            for j in range(i + 1, min(i + 1 + max_lookahead, n)):
                if used[j]:
                    continue
                if cluster_keys.get(issues[j].get("number", 0), "") == current_key:
                    result.append(issues[j])
                    used[j] = True

        i += 1

    return result


def _reorder_tier(
    tier_issues: list[GhIssue],
    config: DispatchConfig,
    max_lookahead: int,
    prio: int,
) -> tuple[list[GhIssue], dict[str, Any] | None]:
    """Reorder a single priority tier. Returns (issues, decision_or_none)."""
    if len(tier_issues) <= 1:
        return tier_issues, None

    cluster_keys: dict[int, str] = {}
    for issue in tier_issues:
        labels = issue.get("labels", [])
        complexity = get_complexity(labels)
        cluster_keys[issue["number"]] = predict_cluster_key(config, complexity)

    if len(set(cluster_keys.values())) <= 1:
        return tier_issues, None

    reordered = _cluster_adjacent(tier_issues, cluster_keys, max_lookahead)
    original_nums = [i["number"] for i in tier_issues]
    reordered_nums = [i["number"] for i in reordered]

    if original_nums == reordered_nums:
        return reordered, None

    decision = {
        "tier": prio,
        "original_order": original_nums,
        "reordered": reordered_nums,
        "cluster_keys": {str(num): cluster_keys[num] for num in original_nums},
    }
    _logger.info(
        "Cache reorder tier P%d: %s → %s",
        prio,
        original_nums,
        reordered_nums,
    )
    return reordered, decision


def reorder_within_tiers(
    issues: list[GhIssue],
    config: DispatchConfig,
    max_lookahead: int | None = None,
) -> tuple[list[GhIssue], list[dict[str, Any]]]:
    """Within-tier reorder pass: cluster same-prefix_key issues adjacently.

    Returns (reordered_issues, audit_decisions). Each audit decision is a
    dict describing a per-tier reorder that changed the order.
    """
    cache_cfg = config.routing.cache if config.routing else None
    if max_lookahead is None:
        max_lookahead = cache_cfg.max_reorder_lookahead if cache_cfg else 10

    if len(issues) <= 1:
        return list(issues), []

    tiers: dict[int, list[GhIssue]] = {}
    for issue in issues:
        prio = _priority_sort_key(issue)
        tiers.setdefault(prio, []).append(issue)

    result: list[GhIssue] = []
    decisions: list[dict[str, Any]] = []

    for prio in sorted(tiers):
        reordered, decision = _reorder_tier(tiers[prio], config, max_lookahead, prio)
        result.extend(reordered)
        if decision is not None:
            decisions.append(decision)

    return result, decisions


def emit_reorder_audit(
    decisions: list[dict[str, Any]],
    *,
    applied: bool,
    store: RunStore | None = None,
) -> None:
    """Write ordering decisions to the hash-chained audit."""
    if not decisions or store is None:
        return
    try:
        from kew.audit import Actor, emit

        actor = Actor("policy", "cache-reorder")
        for decision in decisions:
            payload = {**decision, "applied": applied}
            emit(store, "cache_reorder", actor=actor, payload=payload)
    except Exception:
        _logger.debug("Failed to emit cache reorder audit", exc_info=True)


def apply_cache_reorder(
    issues: list[GhIssue],
    config: DispatchConfig,
    store: RunStore | None = None,
) -> list[GhIssue]:
    """Apply cache-aware reorder if order_aware=true, else return original."""
    cache_cfg = config.routing.cache if config.routing else None
    if cache_cfg is None:
        return issues

    reordered, decisions = reorder_within_tiers(issues, config)
    applied = cache_cfg.order_aware and bool(decisions)
    emit_reorder_audit(decisions, applied=applied, store=store)

    if cache_cfg.order_aware:
        return reordered
    return issues


def list_ready_issues(
    *,
    config: DispatchConfig,
    store: RunStore | None = None,
    strict: bool = False,
) -> list[GhIssue]:
    """Return all open issues with the ready label, sorted by priority then age.

    Issues are sorted ascending by priority (P0 first) and then by
    ``createdAt`` so that older issues of equal priority rank higher.

    When ``routing.cache.order_aware`` is true, applies a within-tier
    reorder pass that clusters same-prefix_key issues adjacently for
    cache warmth.
    """
    issues = _gh_issue_list(
        config.labels.ready,
        config.project.repo,
        extra_args=["--json", "number,title,labels,createdAt,assignees,body"],
        strict=strict,
    )
    issues = [i for i in issues if not _has_epic_label(i.get("labels", []))]
    sorted_issues = sorted(
        issues,
        key=lambda i: (_priority_sort_key(i), i.get("createdAt", "")),
    )
    return apply_cache_reorder(sorted_issues, config, store=store)


def list_queue_issues(
    *, config: DispatchConfig, store: RunStore | None = None
) -> tuple[list[GhIssue], list[GhIssue], list[GhIssue]]:
    """Return all open issues partitioned into (ready, failed, backlog).

    Fetches all open issues in a single API call. Partitions into:
    - ready: issues with the ``ready`` label
    - failed: issues with the ``agent-failed`` label
    - backlog: issues with no dispatch lifecycle labels

    Issues with lifecycle labels (in-progress, agent-working, etc.)
    are excluded — they appear in Workers/Recent/Reviews panels.
    All three lists are sorted by priority then age.
    """
    labels_cfg = config.labels
    lifecycle_labels = {
        labels_cfg.in_progress,
        labels_cfg.agent_working,
        labels_cfg.agent_pr,
        labels_cfg.pr_pending_review,
        labels_cfg.blocked,
        labels_cfg.overlap_hold,
        labels_cfg.completed_no_changes,
        labels_cfg.agent_needs_help,
        labels_cfg.budget_exceeded,
        labels_cfg.budget_stopped,
        labels_cfg.large_pr,
        labels_cfg.ready_to_merge,
        config.approval.label,
    }

    all_issues = _gh_issue_list(
        label="",
        repo=config.project.repo,
        extra_args=["--json", "number,title,labels,createdAt,assignees,body"],
    )
    # Epic-DAG nodes are driver-owned and must never surface as a
    # fleet-dispatchable queue item, even if a reset path (e.g.
    # reset_issue_to_ready) leaves them carrying the `ready` label.
    all_issues = [i for i in all_issues if not _has_epic_label(i.get("labels", []))]

    ready: list[GhIssue] = []
    failed: list[GhIssue] = []
    backlog: list[GhIssue] = []
    for issue in all_issues:
        label_names = {
            lbl.get("name", "")
            for lbl in issue.get("labels", [])
            if isinstance(lbl, dict)
        }
        if labels_cfg.agent_failed in label_names:
            failed.append(issue)
            continue
        has_lifecycle = bool(label_names & lifecycle_labels)
        if has_lifecycle:
            continue  # visible in Workers/Recent/Reviews panels
        if labels_cfg.ready in label_names:
            ready.append(issue)
        else:
            backlog.append(issue)

    def sort_key(i: GhIssue) -> tuple[int, str]:
        return (_priority_sort_key(i), i.get("createdAt", ""))

    ready.sort(key=sort_key)
    failed.sort(key=sort_key)
    backlog.sort(key=sort_key)
    ready = apply_cache_reorder(ready, config, store=store)
    return ready, failed, backlog
