"""Label state machine for dispatch lifecycle.

All label names come from the DispatchConfig, making the system
adaptable to different team conventions.
"""

import dataclasses
import enum
import json
import re
import subprocess
import time
from typing import Any

from kew.config import DispatchConfig
from kew.no_change import ItemObservation, item_observation
from kew.progress import _log
from kew.subprocess_utils import is_rate_limited, run

_CI_FIX_RE = re.compile(r"^ci-fix-attempt-(\d+)$")


class SetupLabelsError(Exception):
    """Raised when label setup fails due to repo access issues."""


@dataclasses.dataclass
class SetupLabelsResult:
    created: int = 0
    updated: int = 0
    failed: int = 0
    # Which labels landed in each count, for `kew setup --json` (#2504).
    names: dict[str, list[str]] = dataclasses.field(
        default_factory=lambda: {"created": [], "updated": [], "failed": []}
    )


class ClaimResult(enum.Enum):
    """Outcome of :func:`try_claim_issue`."""

    CLAIMED = "claimed"
    NOT_READY = "not_ready"
    ALREADY_CLAIMED = "already_claimed"
    FETCH_FAILED = "fetch_failed"
    RACE_DETECTED = "race_detected"

    def __bool__(self) -> bool:
        return self is ClaimResult.CLAIMED


def classify_admission(
    label_names: set[str], config: DispatchConfig, *, approve: bool = False
) -> ClaimResult:
    """Classify dispatch admission from already-fetched labels. Writes nothing.

    The pure half of :func:`try_claim_issue`'s pre-claim check, so ``--dry-run``
    can report the very refusal the real claim would make instead of promising a
    dispatch that never happens (#2058).

    Check in-progress BEFORE ready: a claimed issue is [in-progress,
    agent-working] with ``ready`` removed (see :func:`mark_in_progress`), so
    testing ``ready`` first would mislabel an in-flight issue as NOT_READY and
    advise re-adding the label / --force — inviting a double-dispatch.

    *approve* mirrors ``kew dispatch --approve``, which releases an approval hold
    back to ``ready`` before claiming; a held issue is admissible only then.
    """
    if (
        config.labels.in_progress in label_names
        or config.labels.agent_working in label_names
    ):
        return ClaimResult.ALREADY_CLAIMED
    if config.labels.ready in label_names:
        return ClaimResult.CLAIMED
    if approve and config.approval.label in label_names:
        return ClaimResult.CLAIMED
    return ClaimResult.NOT_READY


def _fetch_issue_labels(issue_num: int, config: DispatchConfig) -> set[str] | None:
    """Fetch the current labels on an issue. Returns None on any failure."""
    cmd = [
        "gh",
        "issue",
        "view",
        str(issue_num),
        "--repo",
        config.project.repo,
        "--json",
        "labels",
    ]
    # Inline retry instead of run_with_backoff() to preserve test mock surface:
    # tests patch kew.labels.run, but run_with_backoff() calls subprocess_utils.run
    # internally, bypassing that mock.
    result = run(cmd, check=False)
    for attempt in range(2):
        if not is_rate_limited(result):
            break
        time.sleep(60 * (2**attempt))
        result = run(cmd, check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        lbl.get("name", "") for lbl in data.get("labels", []) if isinstance(lbl, dict)
    }


def _update_labels(
    issue_num: int,
    config: DispatchConfig,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Add/remove labels on a GitHub issue.

    Raises ``RuntimeError`` if the ``gh`` command exits with a non-zero
    status so callers (e.g. ``mark_ready``) can propagate the error.

    No-op for a non-GitHub tracker: a Linear-tracked project has no GitHub issue
    to relabel — item state is owned by the tracker (claim on dispatch,
    ``tracker.close()`` on the merge pass), so every ``mark_*`` transition that
    funnels here would otherwise shell ``gh issue edit`` against the ``0``
    sentinel and raise. Gating this single funnel keeps the Linear dispatch path
    (L3) from crashing without touching each call site. PR labelling
    (``update_pr_labels``) does NOT route through here — PRs live on GitHub for
    both trackers and stay labelled.
    """
    if config.tracker.type != "github":
        return
    _gh_update_labels(issue_num, config, add=add, remove=remove)


def _gh_update_labels(
    issue_num: int,
    config: DispatchConfig,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Shell ``gh issue edit`` to add/remove labels; raise on non-zero exit."""
    cmd = ["gh", "issue", "edit", str(issue_num), "--repo", config.project.repo]
    for label in add or []:
        cmd.extend(["--add-label", label])
    for label in remove or []:
        cmd.extend(["--remove-label", label])
    # Inline retry instead of run_with_backoff() to preserve test mock surface:
    # tests patch kew.labels.run, but run_with_backoff() calls subprocess_utils.run
    # internally, bypassing that mock.
    result = run(cmd, check=False, capture=True)
    for attempt in range(2):
        if not is_rate_limited(result):
            break
        time.sleep(60 * (2**attempt))
        result = run(cmd, check=False, capture=True)
    rc = getattr(result, "returncode", 0)
    if isinstance(rc, int) and rc != 0:
        stderr = (getattr(result, "stderr", None) or "").strip()
        raise RuntimeError(f"gh issue edit failed (exit {rc}): {stderr}")


def mark_ready(issue_num: int, config: DispatchConfig) -> None:
    """Add the ready label to an issue."""
    _update_labels(issue_num, config, add=[config.labels.ready])


def unmark_ready(issue_num: int, config: DispatchConfig) -> None:
    """Remove the ready label from an issue."""
    _update_labels(issue_num, config, remove=[config.labels.ready])


def clear_failed_label(issue_num: int, config: DispatchConfig) -> None:
    """Remove the agent-failed label from an issue (the epic-retry effector).

    Non-fatal like the original inline ``kew epic retry``: uses ``check=False``
    so a gh miss (label already absent, transient error) does not abort the
    re-admission — the reservation release + retry marker still record it.
    """
    run(
        [
            "gh",
            "issue",
            "edit",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--remove-label",
            config.labels.agent_failed,
        ],
        check=False,
    )


def clear_pending_review_label(issue_num: int, config: DispatchConfig) -> None:
    """Remove the pr-pending-review label from an issue (the epic-unhold effector).

    Best-effort like the driver's own post-terminal strip (see
    ``kew.graph.driver.merge._strip_pending_review_label``): uses ``check=False``
    so a gh miss (label already absent, transient error) does not abort the
    operator's ``kew epic unhold`` — the hold row was already cleared in the store.
    """
    run(
        [
            "gh",
            "issue",
            "edit",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--remove-label",
            config.labels.pr_pending_review,
        ],
        check=False,
    )


def mark_ready_from_failed(
    issue_num: int,
    config: DispatchConfig,
    *,
    current_labels: list[str] | None = None,
) -> None:
    """Transition: agent-failed -> ready (for retry).

    Also removes co-occurring lifecycle labels (budget-exceeded,
    agent-needs-help) so the issue isn't excluded from the ready
    queue by the lifecycle filter on the next poll.

    When *current_labels* is provided, only labels actually present on
    the issue are included in the remove list (``gh issue edit
    --remove-label`` fails for labels that don't exist on the issue).
    """
    remove = [
        config.labels.agent_failed,
        config.labels.budget_exceeded,
        config.labels.budget_stopped,
        config.labels.agent_needs_help,
    ]
    if current_labels is not None:
        present = set(current_labels)
        remove = [lbl for lbl in remove if lbl in present]
    _update_labels(
        issue_num,
        config,
        add=[config.labels.ready],
        remove=remove,
    )


def mark_needs_approval(issue_num: int, config: DispatchConfig) -> None:
    """Transition: in-progress -> needs-approval (approval gate hold).

    The dispatch was already claimed (ready was removed by try_claim_issue),
    so this removes the in-flight labels and parks the issue under the
    approval label. Without 'ready' the queue query naturally skips it.
    """
    _update_labels(
        issue_num,
        config,
        add=[config.approval.label],
        remove=[
            config.labels.in_progress,
            config.labels.agent_working,
            config.labels.ready,
        ],
    )


def mark_ready_from_approval(issue_num: int, config: DispatchConfig) -> None:
    """Transition: in-progress -> ready (interactive approval declined)."""
    _update_labels(
        issue_num,
        config,
        add=[config.labels.ready],
        remove=[config.labels.in_progress, config.labels.agent_working],
    )


def release_approval_hold(issue_num: int, config: DispatchConfig) -> None:
    """Transition: needs-approval -> ready (explicit --approve release)."""
    _update_labels(
        issue_num,
        config,
        add=[config.labels.ready],
        remove=[config.approval.label],
    )


def rollback_claim(issue_num: int, config: DispatchConfig) -> None:
    """Transition: in-progress -> ready (undo a claim that found no worker).

    The mirror image of ``try_claim_issue``: it restores the ``ready`` label
    and clears in-progress/agent-working so the queue re-selects the issue.
    Used when ``pool.submit`` fails after the claim, to avoid stranding the
    issue in-progress until ``kew cleanup``. Idempotent via ``_update_labels``.
    """
    _update_labels(
        issue_num,
        config,
        add=[config.labels.ready],
        remove=[config.labels.in_progress, config.labels.agent_working],
    )


def try_claim_issue(issue_num: int, config: DispatchConfig) -> ClaimResult:
    """Attempt to claim an issue for dispatch, guarding against duplicates.

    Uses a two-phase check:
    1. Pre-claim: re-fetches labels to verify the issue is still 'ready'.
    2. Post-claim: after marking in-progress, re-fetches labels to verify
       the claim stuck (guards against TOCTOU races).

    Returns a :class:`ClaimResult` — truthy only for ``CLAIMED``.
    Callers can inspect the variant to distinguish "not ready" from
    "already claimed by another dispatch".

    Note: if post-claim verification fails, the issue may be left in an
    in-progress state without an active worker. The caller is responsible
    for handling this (e.g., stale-pending cleanup).
    """
    label_names = _fetch_issue_labels(issue_num, config)
    if label_names is None:
        return ClaimResult.FETCH_FAILED

    # One classifier for the pre-claim check, shared with the --dry-run preview
    # so the two can never disagree about why a dispatch is refused (#2058).
    admission = classify_admission(label_names, config)
    if admission is ClaimResult.ALREADY_CLAIMED:
        _log(
            f"Skipping #{issue_num}: already in progress (another dispatch claimed it)"
        )
        return admission
    if admission is ClaimResult.NOT_READY:
        _log(f"Skipping #{issue_num}: '{config.labels.ready}' label not present")
        return admission

    # mark_in_progress -> _update_labels raises RuntimeError on a failed
    # `gh issue edit`; keep try_claim_issue a pure enum-returning guard so
    # callers never get an exception where they expect a ClaimResult.
    try:
        mark_in_progress(issue_num, config)
    except RuntimeError as exc:
        _log(f"#{issue_num}: failed to apply claim labels ({exc}); backing off")
        return ClaimResult.FETCH_FAILED

    # Post-claim verification: re-fetch labels to detect TOCTOU races.
    verified_labels = _fetch_issue_labels(issue_num, config)
    if verified_labels is None:
        _log(f"#{issue_num}: post-claim verification fetch failed, backing off")
        return ClaimResult.FETCH_FAILED

    if config.labels.ready in verified_labels:
        _log(
            f"#{issue_num}: post-claim verification detected race — "
            f"'{config.labels.ready}' still present, another process won the claim"
        )
        return ClaimResult.RACE_DETECTED

    if config.labels.in_progress not in verified_labels:
        _log(
            f"#{issue_num}: post-claim verification failed — "
            f"'{config.labels.in_progress}' not present after mark_in_progress"
        )
        return ClaimResult.RACE_DETECTED

    return ClaimResult.CLAIMED


def mark_in_progress(issue_num: int, config: DispatchConfig) -> None:
    """Transition: ready -> in-progress + agent-working."""
    _update_labels(
        issue_num,
        config,
        add=[config.labels.in_progress, config.labels.agent_working],
        remove=[config.labels.ready],
    )


def mark_completed(issue_num: int, config: DispatchConfig) -> None:
    """Transition: in-progress -> pr-pending-review."""
    _update_labels(
        issue_num,
        config,
        add=[config.labels.pr_pending_review],
        remove=[
            config.labels.in_progress,
            config.labels.agent_working,
            config.labels.ready,
        ],
    )


def mark_no_change_candidate(issue_num: int, config: DispatchConfig) -> None:
    """Transition: in-progress -> agent-completed-no-changes. Decides nothing.

    The claim release a zero-diff completion owes the item, and only that: no
    comment, no close, no unblock. Whether the absence of a diff was the right
    answer is the human decision the no-change ledger records (confirm/reject),
    and the item stays open until it is made.

    Without it a correct run kept its ``agent-working`` claim forever, so a
    finished dispatch read as in-flight to ``kew dispatch`` (a redispatch
    refused as "already in progress") and as an orphan to ``kew cleanup``
    (#1611).
    """
    _update_labels(
        issue_num,
        config,
        add=[config.labels.completed_no_changes],
        remove=[
            config.labels.in_progress,
            config.labels.agent_working,
            config.labels.ready,
        ],
    )


def mark_completed_no_changes(
    issue_num: int,
    config: DispatchConfig,
    summary: str,
) -> None:
    """Transition: in-progress -> agent-completed-no-changes, comment, and close.

    The deciding variant: use ``mark_no_change_candidate`` when the outcome is
    still a candidate awaiting a human.
    """
    mark_no_change_candidate(issue_num, config)
    body = (
        f"**Agent completed — no code changes required.**\n\n"
        f"{summary}\n\n"
        f"Closing automatically."
    )
    run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--body",
            body,
        ],
        check=False,
        capture=True,
    )
    run(
        [
            "gh",
            "issue",
            "close",
            str(issue_num),
            "--repo",
            config.project.repo,
        ],
        check=False,
        capture=True,
    )


def mark_failed(
    issue_num: int,
    config: DispatchConfig,
    reason: str,
    *,
    extra_labels: list[str] | None = None,
) -> None:
    """Transition: in-progress -> agent-failed + comment."""
    add_labels = [config.labels.agent_failed]
    if extra_labels:
        add_labels.extend(extra_labels)
    _update_labels(
        issue_num,
        config,
        add=add_labels,
        remove=[
            config.labels.in_progress,
            config.labels.agent_working,
            config.labels.ready,
        ],
    )
    log_dir = config.logging.dir
    body = (
        f"**Dispatch failed** — {reason}\n\n"
        f"Log: `{log_dir}/`\n"
        f"Re-label as `{config.labels.ready}` to retry."
    )
    run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--body",
            body,
        ],
        check=False,
        capture=True,
    )


def mark_needs_review_incomplete(issue_num: int, config: DispatchConfig) -> None:
    """Transition a rescued (max-steps) run: -> agent-needs-help + incomplete-run.

    The agent ran out of steps but left coherent work that kew brokered into a
    draft PR. The issue is NOT completed and NOT closed — it stays open as a
    human gate. Distinct from ``mark_failed`` (no ``agent-failed``) and from
    ``mark_completed`` (no ``pr-pending-review``).
    """
    _update_labels(
        issue_num,
        config,
        add=[config.labels.agent_needs_help, config.labels.incomplete_run],
        remove=[
            config.labels.in_progress,
            config.labels.agent_working,
            config.labels.ready,
        ],
    )
    body = (
        "**Run reached `max_steps` without finishing.** kew brokered the work "
        "into a **draft PR** so nothing is lost — the agent may not have "
        "completed the task. Review the draft; if the work is good, mark it "
        "ready and merge. To retry from scratch, re-label as "
        f"`{config.labels.ready}`."
    )
    run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--body",
            body,
        ],
        check=False,
        capture=True,
    )


def _setup_labels_preflight(repo: str) -> None:
    """Translate startup failures before any label check or repair runs."""
    from kew.selector import GhQueryError

    try:
        preflight = run(
            ["gh", "repo", "view", repo, "--json", "name"],
            check=False,
            capture=True,
        )
    except (OSError, subprocess.SubprocessError, GhQueryError, ValueError) as exc:
        raise SetupLabelsError("Setup label preflight unavailable.") from exc
    if preflight.returncode != 0:
        stderr = (getattr(preflight, "stderr", None) or "").strip()
        # The redactor drops the rest of a line after a credential-like key
        # ("gh auth status:", "Bad credentials (https:"). Our advice ends its
        # own line with no colon, and gh's text starts the next line, so a key
        # in either can never swallow the other (F23).
        raise SetupLabelsError(
            f"Cannot access repo {repo}. "
            "Check project.repo in kew.toml and run gh auth status.\n"
            f"gh said: {stderr}"
        )


def setup_labels(config: DispatchConfig) -> SetupLabelsResult:
    """Create all dispatch labels on the GitHub repo. Idempotent.

    Raises :class:`SetupLabelsError` when the repository preflight cannot run.
    """
    repo = config.project.repo
    labels = config.labels
    _setup_labels_preflight(repo)

    label_defs: list[tuple[str, str, str]] = [
        (labels.ready, "0E8A16", "Available for dispatch pickup"),
        (labels.in_progress, "FBCA04", "Being worked on by agent"),
        (labels.agent_working, "FBCA04", "Actively being implemented"),
        (labels.agent_failed, "D93F0B", "Agent could not complete"),
        (labels.agent_pr, "1D76DB", "PR created by agent"),
        (
            labels.ci_approved,
            "0E8A16",
            "Human approved self-hosted CI for this agent PR head",
        ),
        (labels.pr_pending_review, "BFD4F2", "PR waiting for review"),
        (labels.completed_no_changes, "0E8A16", "Agent completed with no code changes"),
        (labels.blocked, "B60205", "Has unresolved dependencies"),
        (labels.agent_needs_help, "D93F0B", "Agent needs human help"),
        (labels.incomplete_run, "FBCA04", "Incomplete run draft — review carefully"),
        (labels.budget_exceeded, "B60205", "Budget limit exceeded"),
        (
            labels.budget_stopped,
            "D93F0B",
            "Stopped — sustained over per-run budget cap",
        ),
        (labels.large_pr, "FBCA04", "PR exceeds size threshold"),
        (labels.ready_to_merge, "0E8A16", "Ready for merge"),
        (labels.overlap_hold, "FBCA04", "Held due to overlapping in-flight work"),
        (
            config.approval.label,
            "FBCA04",
            "Held — estimated cost at or above approval threshold",
        ),
    ]

    priority_colors = ["D93F0B", "E99695", "FBCA04", "C2E0C6"]
    for i, label in enumerate(labels.priorities):
        color = priority_colors[i] if i < len(priority_colors) else "FBCA04"
        label_defs.append((label, color, f"Priority level {i}"))

    complexity_colors = {"simple": "C2E0C6", "medium": "FBCA04", "complex": "E99695"}
    for name, level in labels.complexity.items():
        color = complexity_colors.get(name, "FBCA04")
        label_defs.append((name, color, f"{level.tokens}, {level.scope}"))

    counts = SetupLabelsResult()

    for name, color, description in label_defs:
        result = run(
            [
                "gh",
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                description,
                "--repo",
                repo,
            ],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            counts.created += 1
            counts.names["created"].append(name)
        else:
            edit_result = run(
                [
                    "gh",
                    "label",
                    "edit",
                    name,
                    "--color",
                    color,
                    "--description",
                    description,
                    "--repo",
                    repo,
                ],
                check=False,
                capture=True,
            )
            if edit_result.returncode == 0:
                counts.updated += 1
                counts.names["updated"].append(name)
            else:
                counts.failed += 1
                counts.names["failed"].append(name)

    return counts


def update_pr_labels(
    pr_num: int,
    config: DispatchConfig,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> bool:
    """Add/remove labels on a GitHub pull request. Returns True on success."""
    cmd = ["gh", "pr", "edit", str(pr_num), "--repo", config.project.repo]
    for label in add or []:
        cmd.extend(["--add-label", label])
    for label in remove or []:
        cmd.extend(["--remove-label", label])
    result = run(cmd, check=False, capture=True)
    return result.returncode == 0


def get_ci_fix_attempt(labels: list[str]) -> int:
    """Return the highest ci-fix-attempt-N from *labels*, or 0 if none."""
    best = 0
    for label in labels:
        m = _CI_FIX_RE.match(label)
        if m:
            best = max(best, int(m.group(1)))
    return best


def fetch_agent_working_issues(config: DispatchConfig) -> list[dict[str, Any]]:
    """Return all open issues that carry the agent-working label.

    Each entry is a dict with keys ``number`` (int), ``title`` (str),
    and ``labels`` (list[dict] with a ``name`` key).
    """
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            config.project.repo,
            "--label",
            config.labels.agent_working,
            "--state",
            "open",
            "--json",
            "number,title,labels",
            "--limit",
            "1000",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh issue list failed (rc={result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"Failed to parse gh output: {exc}; stdout={result.stdout!r}"
        ) from exc
    if not isinstance(data, list):
        raise RuntimeError(
            f"Expected list from gh output, got {type(data).__name__}: "
            f"{result.stdout!r}"
        )
    return [
        item
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("number"), int)
        and isinstance(item.get("title"), str)
    ]


def unblock_issue(issue_num: int, config: DispatchConfig) -> None:
    """Remove the ``blocked`` label from an issue whose dependencies are resolved."""
    _update_labels(issue_num, config, remove=[config.labels.blocked])


def reset_orphaned_issue(
    issue_num: int,
    config: DispatchConfig,
    *,
    target: str,
    current_labels: list[str],
) -> None:
    """Reset an orphaned issue's labels to *target* (``'ready'`` or ``'agent-failed'``).

    Removes in-progress and agent-working labels (only those present on the
    issue) and adds the target label.
    """
    remove = [config.labels.in_progress, config.labels.agent_working]

    if target == "ready":
        remove.extend(
            [
                config.labels.agent_failed,
                config.labels.budget_exceeded,
                config.labels.budget_stopped,
                config.labels.agent_needs_help,
            ]
        )
    else:
        remove.append(config.labels.ready)

    remove = [lbl for lbl in remove if lbl in current_labels]

    if target == "ready":
        _update_labels(issue_num, config, add=[config.labels.ready], remove=remove)
    else:
        _update_labels(
            issue_num, config, add=[config.labels.agent_failed], remove=remove
        )


def reset_issue_to_ready(issue_num: int, config: DispatchConfig) -> None:
    """Reset a killed/in-progress issue back to ready. Idempotent.

    Used by the fleet kill switch (`kew stop`) and by workers whose run
    was killed. Fetches current labels first so the remove list only
    contains labels actually present (``gh issue edit --remove-label``
    fails for absent labels), making double resets harmless.

    Raises RuntimeError when the current labels cannot be fetched.
    """
    current = _fetch_issue_labels(issue_num, config)
    if current is None:
        raise RuntimeError(f"Could not fetch labels for #{issue_num}")
    remove = [
        lbl
        for lbl in (config.labels.in_progress, config.labels.agent_working)
        if lbl in current
    ]
    if config.labels.ready in current and not remove:
        return  # already reset — nothing to do
    _update_labels(issue_num, config, add=[config.labels.ready], remove=remove)


# --- no-change outcome adapter (I11) -----------------------------------------
#
# Two functions, and deliberately no third. A no-change synchronization LOOKS
# (is this item still open, and does it already carry this judgment's marker?)
# and then WRITES ONCE (one comment carrying the marker, then the close). The
# reconciler in ``kew.outcomes`` owns every decision between the two; this is
# only the GitHub dialect of them.


def observe_no_change_item(
    issue_num: int, config: DispatchConfig, *, marker: str
) -> ItemObservation:
    """One look at a GitHub issue: open or closed, and marked or not.

    An unreadable issue comes back ``available=False`` — never as an open
    unmarked one — because "we could not ask" must not become "so write".
    """
    result = run(
        [
            "gh",
            "issue",
            "view",
            str(issue_num),
            "--repo",
            config.project.repo,
            "--json",
            "state,url,comments",
        ],
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        return ItemObservation(
            available=False,
            detail=(getattr(result, "stderr", None) or "gh issue view failed").strip(),
        )
    return _issue_observation(result.stdout or "", marker)


def _issue_observation(payload: str, marker: str) -> ItemObservation:
    """One ``gh issue view`` payload as an observation, or as unreadable."""
    try:
        data = json.loads(payload)
    except ValueError as exc:
        return ItemObservation(available=False, detail=f"unreadable gh payload: {exc}")
    if not isinstance(data, dict):
        return ItemObservation(available=False, detail="unreadable gh payload")
    state = str(data.get("state") or "").upper()
    return item_observation(
        closed=state != "OPEN",
        bodies=[str(c.get("body") or "") for c in _comment_nodes(data)],
        marker=marker,
        ref=str(data.get("url") or ""),
        detail=f"gh reports {state or 'no'} state",
    )


def _comment_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (data.get("comments") or []) if isinstance(c, dict)]


def apply_no_change_outcome(
    issue_num: int, config: DispatchConfig, *, body: str | None, close: bool
) -> bool:
    """Comment (and optionally close). True ONLY if every write succeeded.

    The comment carries the marker and is written first, so a crash between
    the two leaves the item discoverable as half-written rather than closed
    with nothing on it explaining why. ``body=None`` is a resumed pass
    finishing that closure — the note is already there. A non-zero ``gh``
    exit — the shape a warning-only reply takes — returns False, and False
    leaves the sync job unacknowledged.
    """
    repo = config.project.repo
    if body is not None:
        commented = run(
            ["gh", "issue", "comment", str(issue_num), "--repo", repo, "--body", body],
            check=False,
            capture=True,
        )
        if getattr(commented, "returncode", 1) != 0:
            return False
    if not close:
        return True
    closed = run(
        ["gh", "issue", "close", str(issue_num), "--repo", repo],
        check=False,
        capture=True,
    )
    return bool(getattr(closed, "returncode", 1) == 0)
