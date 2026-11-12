"""CI fix prompt builder and orchestration.

Renders the ci-fix.md.j2 template with CI failure context
for autonomous fix attempts.  Provides get_pr_for_run() to
fetch PR metadata for a GitHub Actions run and dispatch_ci_fix()
to orchestrate the full CI failure auto-fix cycle.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from functools import partial
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from kew import _TEMPLATES_DIR, subprocess_utils
from kew.audit import (
    DEFAULT_ANCHORS_PATH,
    Actor,
    configured_endpoint,
    emit,
    resolve_actor,
)
from kew.budget import BudgetExceeded, check_pr_subject_budget
from kew.command_cleanup import finish_command_cleanup
from kew.config import DispatchConfig, project_root
from kew.dispatch_core import (
    WORKER_CAP_REACHED_MESSAGE,
    WorkerCapReached,
    budget_watchdog,
    extract_label_names,
    revoke_ci_approval,
    run_tracked_agent,
)
from kew.dispatch_core import (
    resolve_issue_number as _resolve_issue_number,
)
from kew.github_diagnostics import is_missing_branch_pr
from kew.ids import InvalidItemIdError, branch_slug, validate_item_id
from kew.inference_transport import resolve_inference_transport
from kew.labels import update_pr_labels
from kew.ledger_identity import (
    LedgerIdentityError,
    canonical_project_root,
    open_managed_store,
)
from kew.merge_claim import PublishDeferred, defer_if_claimed
from kew.notifier import NotificationEvent, Notifier
from kew.progress import _error, _log, _success, _warn
from kew.prompt import (
    fence_slot,
    fence_untrusted,
    fence_vocabulary_filter,
    make_sentinel,
)
from kew.prompt_addendum import (
    ProfileAddendum,
    PromptConfigurationError,
    profile_version_id,
)
from kew.prompt_refusals import PromptRefusal, record_prompt_refusal
from kew.publication import PublicationMode, select_publication_mode
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    ensure_publication_credentials,
    publication_producer_for_head,
)
from kew.run_store import CI_FIX_RUN_KIND, RunStore
from kew.run_store_launches import CiFixAttemptsExhausted
from kew.runners import resolve_runner
from kew.runners.registry import ResolvedRoute
from kew.sandbox import SandboxDecision, resolve_flow_containment
from kew.selector import GhQueryError
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import run
from kew.tracking import DispatchResult, LaunchState, startup_refused
from kew.worktree import (
    checkout_existing_worktree,
    cleanup_worktree,
    publish_existing_pr_branch,
    release_checkout_claims,
    require_pinned_pr_head,
)

# Maximum number of log lines to keep when truncating CI output.
_MAX_LOG_LINES = 500
_NON_SUCCESS_CATEGORIES = frozenset(
    {"budget_exceeded", "budget_stopped", "budget_stop", "timeout"}
)


def _update_attempt_labels(
    run_id: str,
    pr_number: int,
    config: DispatchConfig,
    new_label: str,
    old_label: str | None,
    *,
    store: RunStore,
    labels: list[str],
) -> None:
    """Reconcile labels only after the atomic PR slot has committed.

    ``run_id`` arrives from the admission hook: the run whose slot committed."""
    count = store.count_launched_ci_fix_attempts(config.project.repo, pr_number)
    _reconcile_attempt_labels(pr_number, config, labels, count)
    if not update_pr_labels(
        pr_number, config, add=[new_label], remove=[old_label] if old_label else None
    ):
        raise RuntimeError("CI fix attempt label update failed.")


def _reconcile_attempt_labels(
    pr_number: int, config: DispatchConfig, labels: list[str], count: int
) -> None:
    """Remote labels mirror durable starts and never establish consumed retries."""
    expected = [f"ci-fix-attempt-{count}"] if count else []
    observed = [label for label in labels if label.startswith("ci-fix-attempt-")]
    if sorted(observed) == expected:
        return
    _warn(f"PR #{pr_number}: reconciling CI fix labels to {count} durable launches")
    if not update_pr_labels(pr_number, config, add=expected, remove=observed):
        raise RuntimeError("Could not reconcile CI fix attempt labels.")


def _ci_fix_failure_reason(result: Any) -> str:
    """Keep optional runner diagnostics as serializable audit text."""
    reason = getattr(result, "stop_reason", "")
    return reason if isinstance(reason, str) else ""


def _ci_fix_run_succeeded(result: Any) -> bool:
    """True only for a clean agent exit without a terminal stop condition."""
    return (
        result.exit_code == 0
        and not startup_refused(result)
        and getattr(result, "budget_exceeded", False) is not True
        and getattr(result, "hard_stop", "") != "wall_clock"
        and getattr(result, "error_category", "") not in _NON_SUCCESS_CATEGORIES
    )


def _recorded_run_id(store: RunStore | None, run_id: str | None) -> str | None:
    """Return a Kew run ID only when its run row exists."""
    if store is None or run_id is None:
        return None
    try:
        return run_id if store.get(run_id) is not None else None
    except Exception:
        _warn(f"Could not confirm CI fix run {run_id} in the audit store")
        return None


def _emit_ci_fix_event(
    config: DispatchConfig,
    *,
    action_run_id: str,
    pr_number: int,
    issue_number: int,
    attempt: int,
    exit_code: int,
    run_id: str | None = None,
    endpoint: str | None = None,
    store: RunStore | None = None,
    reason: str | None = None,
) -> None:
    """Record ci_fix_completed in audit_events.

    When *store* is provided the caller's handle is reused; otherwise a
    one-shot store is opened (legacy path — degrades to a warning on failure).
    """
    bound_run_id = _recorded_run_id(store, run_id)
    try:
        if store is not None:
            emit(
                store,
                "ci_fix_completed",
                actor=resolve_actor(),
                run_id=bound_run_id,
                issue_num=issue_number,
                endpoint=endpoint,
                repo=config.project.repo or None,
                payload={
                    "action_run_id": action_run_id,
                    "pr_number": pr_number,
                    "attempt": attempt,
                    "exit_code": exit_code,
                    "outcome": "success" if exit_code == 0 else "failure",
                    **({"reason": reason} if reason else {}),
                },
            )
        else:
            db_path = Path(config.logging.dir).resolve() / "dispatch.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            root = canonical_project_root(project_root())
            with open_managed_store(
                project_root=root,
                db_path=db_path.resolve(),
                anchors_path=root / DEFAULT_ANCHORS_PATH,
                read_only=False,
                check_same_thread=True,
            ) as _store:
                emit(
                    _store,
                    "ci_fix_completed",
                    actor=resolve_actor(),
                    run_id=bound_run_id,
                    issue_num=issue_number,
                    endpoint=endpoint,
                    repo=config.project.repo or None,
                    payload={
                        "action_run_id": action_run_id,
                        "pr_number": pr_number,
                        "attempt": attempt,
                        "exit_code": exit_code,
                        "outcome": "success" if exit_code == 0 else "failure",
                        **({"reason": reason} if reason else {}),
                    },
                )
    except LedgerIdentityError:
        raise
    except Exception as exc:
        _warn(f"Failed to record ci_fix_completed for PR #{pr_number}: {exc}")


def extract_failed_logs(
    *,
    run_id: str,
    repo: str,
    max_lines: int = 200,
) -> str:
    """Extract failed CI step logs using gh run view.

    Args:
        run_id: The GitHub Actions run ID.
        repo: The repository in owner/repo format.
        max_lines: Maximum number of lines to return (tail behavior).

    Returns:
        The last max_lines lines of the failed log output,
        or empty string on failure.
    """
    if not isinstance(max_lines, int) or max_lines <= 0:
        raise ValueError("max_lines must be a positive integer")
    result = subprocess_utils.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
        check=False,
    )
    if result.returncode != 0:
        return ""
    lines = result.stdout.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines) + "\n" if lines else ""


def build_ci_fix_prompt(
    *,
    pr_number: int,
    issue_number: int,
    issue_title: str,
    branch_name: str,
    ci_log: str,
    attempt: int,
    max_attempts: int,
    config: DispatchConfig,
    diff_summary: str | None = None,
    host_publication: bool = False,
    profile_addendum: ProfileAddendum | None = None,
) -> str:
    """Build a CI fix prompt from the template.

    Args:
        pr_number: The pull request number.
        issue_number: The originating issue number.
        issue_title: The issue title.
        branch_name: The git branch to push fixes to.
        ci_log: The CI failure log output.
        attempt: Current attempt number.
        max_attempts: Maximum fix attempts allowed.
        config: Dispatch configuration (provides CI/format commands).
        diff_summary: Optional summary of recent changes.
        host_publication: Leave publication to kew instead of the agent.
        profile_addendum: The approved ``ci_fix`` addendum (#3023), rendered
            once under its heading before the safety and publication sections;
            None renders the base bytes.

    Returns:
        Rendered prompt string.

    Raises:
        ValueError: If inputs are out of valid range or empty.
    """
    if pr_number <= 0:
        raise ValueError("pr_number must be > 0")
    if issue_number <= 0:
        raise ValueError("issue_number must be > 0")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if attempt < 1 or attempt > max_attempts:
        raise ValueError(f"attempt must be between 1 and max_attempts ({max_attempts})")
    if not issue_title:
        raise ValueError("issue_title must be non-empty")
    if not branch_name:
        raise ValueError("branch_name must be non-empty")
    if not ci_log:
        raise ValueError("ci_log must be non-empty")

    # Fence the attacker-reachable CI log with a per-render sentinel (H7), and
    # the issue title and diff summary through the slot helper (#3348).
    sentinel = make_sentinel()
    ci_log_fenced = fence_untrusted(ci_log, sentinel)

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        finalize=fence_vocabulary_filter(sentinel),
    )
    template = env.get_template("ci-fix.md.j2")

    return template.render(
        pr_number=pr_number,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_title_fenced=fence_slot(issue_title, inline=True),
        branch_name=branch_name,
        ci_log=ci_log,
        ci_log_fenced=ci_log_fenced,
        attempt=attempt,
        max_attempts=max_attempts,
        commands_ci=config.commands.ci,
        commands_format=config.commands.format,
        diff_summary=diff_summary,
        diff_summary_fenced=fence_slot(diff_summary) if diff_summary else "",
        host_publication=host_publication,
        profile_addendum=profile_addendum,
    )


# ---------------------------------------------------------------------------
# PR metadata fetching
# ---------------------------------------------------------------------------


#: ``dispatch_ci_fix`` result: the PR lookup query failed (not "no PR"). The
#: CLI boundary translates it to the public could-not-run exit (2).
CI_FIX_QUERY_FAILED = 3
CI_FIX_CANNOT_RUN = 4


def _gh_stderr(result: Any) -> str:
    stderr = getattr(result, "stderr", "") or ""
    return stderr.strip() if isinstance(stderr, str) else ""


def get_pr_for_run(*, run_id: str, repo: str) -> dict[str, Any] | None:
    """Fetch PR metadata for a GitHub Actions run.

    1. Gets the head branch from ``gh run view --json headBranch``.
    2. Gets PR details from ``gh pr view <branch> --json ...``.

    Returns:
        Parsed PR dict with keys: number, headRefName, headRefOid, baseRefName,
        body, title, labels.
        Returns ``None`` only when GitHub answered and there is no PR (the run
        has no head branch, or the branch has no pull request).

    Raises:
        GhQueryError: the ``gh`` query itself failed (authentication, network,
        an unreadable reply). A failed query is not an empty answer; the
        caller must not treat it as "nothing to fix".
    """
    # Step 1: get head branch from the run
    result = run(
        ["gh", "run", "view", run_id, "--repo", repo, "--json", "headBranch"],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        raise GhQueryError(
            f"gh run view {run_id} failed (exit {result.returncode}): "
            f"{_gh_stderr(result)}"
        )

    try:
        run_data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GhQueryError(f"gh run view {run_id} returned unreadable JSON") from exc

    if not isinstance(run_data, dict):
        raise GhQueryError(f"gh run view {run_id} returned a non-object reply")

    head_branch = run_data.get("headBranch")
    if not head_branch:
        return None

    # Step 2: get PR details for that branch
    pr_fields = "number,headRefName,headRefOid,baseRefName,body,title,labels"
    result = run(
        [
            "gh",
            "pr",
            "view",
            head_branch,
            "--repo",
            repo,
            "--json",
            pr_fields,
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        # ``gh pr view <branch>`` answers "no pull requests found for branch"
        # when the branch exists without a PR; anything else is a failed query.
        if is_missing_branch_pr(result.returncode, result.stderr, head_branch):
            return None
        raise GhQueryError(
            f"gh pr view {head_branch} failed (exit {result.returncode}): "
            f"{_gh_stderr(result)}"
        )

    try:
        pr_data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise GhQueryError(
            f"gh pr view {head_branch} returned unreadable JSON"
        ) from exc

    if not isinstance(pr_data, dict):
        raise GhQueryError(f"gh pr view {head_branch} returned a non-object reply")

    return pr_data


# ---------------------------------------------------------------------------
# CI log extraction
# ---------------------------------------------------------------------------


def extract_ci_log(*, run_id: str, repo: str) -> str:
    """Extract failed CI log output for a GitHub Actions run.

    Uses ``gh run view --log-failed`` to get only the failing job
    output.  Truncates to *_MAX_LOG_LINES* to keep prompts manageable.

    Returns:
        The log text, or "" on failure.
    """
    result = run(
        ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
        check=False,
        capture=True,
    )
    if result.returncode != 0 or not result.stdout:
        return ""

    lines = result.stdout.splitlines()
    if len(lines) > _MAX_LOG_LINES:
        lines = lines[-_MAX_LOG_LINES:]
        return "(truncated)\n" + "\n".join(lines)
    return result.stdout


def _resolve_ci_fix_work(
    pr_body: str,
    branch_name: str,
    *,
    store: RunStore | None,
    fallback_issue_number: int,
    provider: str,
    branch_prefix: str,
) -> tuple[int, str]:
    """Resolve a numeric shim and provider-valid item ID for one CI fix."""
    source_run = store.find_run_by_branch(branch_name) if store is not None else None
    issue_number = (
        source_run.issue_num
        if source_run is not None and source_run.issue_num > 0
        else _resolve_issue_number(pr_body, branch_name)
    )

    def _source_item() -> str | None:
        source_item_id = getattr(source_run, "item_id", None)
        if isinstance(source_item_id, str) and source_item_id:
            try:
                item_id = validate_item_id(source_item_id, provider)
            except InvalidItemIdError as exc:
                _warn(f"Ignoring invalid CI fix source item: {exc}")
                item_id = None
        else:
            item_id = None
        return item_id

    item_id = _source_item()

    if item_id is None and provider == "github":
        item_id = str(issue_number) if issue_number is not None else ""

    def _registered_branch_item() -> str | None:
        nonlocal item_id
        if item_id is None and branch_name.startswith(branch_prefix):
            candidate = branch_name.removeprefix(branch_prefix).upper()
            try:
                registered_item = validate_item_id(candidate, provider)
            except InvalidItemIdError as exc:
                _warn(f"CI fix branch does not register a valid item: {exc}")
                registered_item = None
            if (
                registered_item is not None
                and f"{branch_prefix}{branch_slug(registered_item)}" == branch_name
            ):
                item_id = registered_item
        return item_id

    item_id = _registered_branch_item()

    if item_id is None:
        raise ValueError(
            f"cannot resolve a {provider} work item for branch {branch_name!r}"
        )
    return issue_number or fallback_issue_number, item_id


def _ci_fix_producer(
    store: RunStore | None, *, branch: str, head_sha: str
) -> tuple[str | None, str | None]:
    """Return the selected parent and its optional strict publication binding."""
    if store is None:
        return None, None
    producer_run_id = publication_producer_for_head(
        store, branch=branch, head_sha=head_sha
    )
    return producer_run_id, _ci_fix_referenced_producer(store, producer_run_id)


def _ci_fix_referenced_producer(
    store: RunStore | None, producer_run_id: str | None
) -> str | None:
    """Revalidate the selected parent, retaining unprofiled legacy validation.

    Head lookup already requires referenced evidence for explicit intents and
    normalized jobs. Only the legacy heuristic can select an unprofiled parent.
    Keep that parent's ID across the agent run and check its ancestry again at
    publication. Bind it as a referenced producer only with complete profiles.
    """
    if store is None or not producer_run_id:
        return None
    ensure_publication_credentials(store, run_id=producer_run_id)
    if not store.list_route_profiles(producer_run_id):
        return None
    ensure_publication_credentials(
        store, run_id=producer_run_id, producer_run_id=producer_run_id
    )
    return producer_run_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def dispatch_ci_fix(
    *,
    run_id: str,
    config: DispatchConfig,
    notifier: Notifier | None = None,
    store: RunStore | None = None,
) -> int:
    """Orchestrate a CI failure auto-fix attempt; the checkout lease ends with
    the attempt on every exit (review round 12 of #1440).

    A refused ``ci_fix`` profile reads as :data:`CI_FIX_CANNOT_RUN` here; the
    epic driver calls :func:`attempt_ci_fix` to tell it apart.
    """
    result = attempt_ci_fix(
        run_id=run_id, config=config, notifier=notifier, store=store
    )
    return CI_FIX_CANNOT_RUN if isinstance(result, PromptRefusal) else result


def attempt_ci_fix(
    *,
    run_id: str,
    config: DispatchConfig,
    notifier: Notifier | None = None,
    store: RunStore | None = None,
) -> int | PromptRefusal:
    """:func:`dispatch_ci_fix`, with a refused ``ci_fix`` profile returned as
    a :class:`~kew.prompt_refusals.PromptRefusal` (#3023)."""
    try:
        return _dispatch_ci_fix(
            run_id=run_id, config=config, notifier=notifier, store=store
        )
    finally:
        finish_command_cleanup(release_checkout_claims, command="ci-fix")


def _dispatch_ci_fix(
    *,
    run_id: str,
    config: DispatchConfig,
    notifier: Notifier | None = None,
    store: RunStore | None = None,
) -> int | PromptRefusal:
    """Orchestrate a CI failure auto-fix attempt.

    1. Fetch PR metadata for the failed run.
    2. Scope guard — skip non-agent PRs.
    3. Retry guard — label ``agent-needs-help`` if max retries exceeded.
    4. Extract CI failure logs.
    5. Determine the numeric shim and durable item ID.
    6. Update attempt label.
    7. Build prompt and dispatch fix.
    8. Clean up worktree on success, preserve on failure.

    Returns:
        0 = fix dispatched successfully,
        1 = fix dispatch failed,
        2 = skipped (no PR, not an agent PR, or max retries),
        CI_FIX_QUERY_FAILED (3) = the PR lookup query failed; GitHub gave no
        answer, so nothing was skipped or fixed (public exit 2 at the CLI).
        CI_FIX_CANNOT_RUN (4) = required logs or preparation were unavailable.
        A :class:`~kew.prompt_refusals.PromptRefusal` = the active ``ci_fix``
        profile refused before any runner (#3023).
    """
    repo = config.project.repo

    # 1. Fetch PR metadata. A failed query is not "no PR": it must never
    # read as a clean skip (adversarial review of #1943, round 2).
    try:
        pr = get_pr_for_run(run_id=run_id, repo=repo)
    except (GhQueryError, OSError, subprocess.SubprocessError, ValueError) as exc:
        _error(f"Could not look up the PR for run {run_id}: {exc}")
        return CI_FIX_QUERY_FAILED
    if pr is None:
        _warn(f"No PR found for run {run_id} — skipping")
        return 2

    pr_number: int = pr["number"]
    branch_name: str = pr["headRefName"]
    pr_body: str = pr.get("body") or ""
    pr_title: str = pr.get("title") or ""
    expected_head_sha: str = pr.get("headRefOid") or ""
    label_names: list[str] = extract_label_names(pr)

    # 2. Scope guard — only fix agent PRs
    if config.labels.agent_pr not in label_names:
        _log(f"PR #{pr_number} missing '{config.labels.agent_pr}' label — skipping")
        return 2

    # 3. Retry guard
    if store is None:
        _error("CI fix requires a run store for durable attempt accounting.")
        return CI_FIX_CANNOT_RUN
    try:
        current_attempt = store.count_launched_ci_fix_attempts(repo, pr_number)
        refused_attempts = store.count_refused_ci_fix_attempts(repo, pr_number)
    except Exception as exc:
        _error(f"Could not establish CI fix attempt count: {exc}")
        return CI_FIX_CANNOT_RUN
    next_attempt = current_attempt + 1
    max_retries = config.triggers.ci_failure_max_retries

    # A refusal releases its launch slot so a transient one never spends the
    # repair budget: at a limit of one, a refusal is still followed by a real
    # repair (round 22). One more refusal than that budget cannot be transient,
    # so a container that refuses every time escalates instead of repairing on
    # every tick forever (governed review round 6, 2026-09-22).
    max_refusals = max_retries + 1
    if current_attempt >= max_retries or refused_attempts >= max_refusals:

        def _exhausted_retries() -> int:
            already_labelled = config.labels.agent_needs_help in label_names
            if not already_labelled:
                _warn(
                    f"PR #{pr_number}: max CI fix retries ({max_retries}) reached — "
                    f"labelling {config.labels.agent_needs_help}"
                )
                update_pr_labels(
                    pr_number,
                    config,
                    add=[config.labels.agent_needs_help],
                )
                # Post summary comment (best-effort)
                comment_body = (
                    f"**CI fix retry limit reached** — {current_attempt}/{max_retries} "
                    f"attempts exhausted for run `{run_id}`.\n\n"
                    "Human intervention needed. "
                    f"Labelled `{config.labels.agent_needs_help}`."
                )
                run(
                    [
                        "gh",
                        "pr",
                        "comment",
                        str(pr_number),
                        "--repo",
                        repo,
                        "--body",
                        comment_body,
                    ],
                    check=False,
                    capture=True,
                )
                if notifier is not None:
                    try:
                        notifier.notify(
                            NotificationEvent(
                                event_type="ci_fix_max_retries",
                                severity="warning",
                                message=(
                                    f"PR #{pr_number}: CI fix retries exhausted "
                                    f"({current_attempt}/{max_retries})"
                                ),
                                pr_number=pr_number,
                                run_id=run_id,
                            )
                        )
                    except Exception as exc:
                        _warn(f"Notifier failed for PR #{pr_number}: {exc}")
            return 2

        return _exhausted_retries()

    def _fix_pr() -> int | PromptRefusal:
        # 4. Extract CI failure logs
        try:
            ci_log = extract_ci_log(run_id=run_id, repo=repo)
        except (OSError, subprocess.SubprocessError) as exc:
            _error(f"Could not extract CI log for run {run_id}: {exc}")
            return CI_FIX_CANNOT_RUN
        if not ci_log:
            _error(f"Could not extract CI log for run {run_id}")
            return CI_FIX_CANNOT_RUN

        # 5. Determine work identity. The store is the canonical PR-to-item join.
        # The registered branch convention recovers a Linear item when needed.
        try:
            issue_number, item_id = _resolve_ci_fix_work(
                pr_body,
                branch_name,
                store=store,
                fallback_issue_number=pr_number,
                provider=config.tracker.type,
                branch_prefix=config.branches.prefix,
            )
        except ValueError as exc:
            _error(f"CI fix refused for branch {branch_name!r}: {exc}")
            return CI_FIX_CANNOT_RUN

        # 5b. Budget admission (#1863). The gate binds the global daily and
        # weekly caps, and agent.max_budget_usd kills the run itself. A cheap
        # pre-check of committed spend runs before any checkout, label, or
        # paid launch; the atomic admit-time gate inside try_admit_run values
        # every pending row at the kill cap, this run's own row included, as
        # ordinary dispatch's gate does. The per-issue implementation cap
        # deliberately does not bind: ci_fix is PR-subject work, which never
        # joins that pool (#1932), and ci_fix is not a review kind, so no
        # review pool applies (check_pr_subject_budget).
        budget_store = store

        def _admit_budget_check() -> None:
            check_pr_subject_budget(
                budget_store,
                config.budget,
                pending_reservation_usd=config.agent.max_budget_usd,
            )

        def _refuse_ci_fix_budget(exc: BudgetExceeded) -> int:
            # Dispatch's refusal category and public exit (2, could not run).
            _error(f"CI fix for PR #{pr_number} refused: {exc}")
            _emit_ci_fix_event(
                config,
                action_run_id=run_id,
                pr_number=pr_number,
                issue_number=issue_number,
                attempt=next_attempt,
                exit_code=1,
                store=store,
                reason="budget_exceeded",
            )
            return CI_FIX_CANNOT_RUN

        try:
            check_pr_subject_budget(budget_store, config.budget)
        except BudgetExceeded as exc:
            return _refuse_ci_fix_budget(exc)

        # 6. Prepare attempt labels. The prompt is built after containment resolves.
        old_label = f"ci-fix-attempt-{current_attempt}" if current_attempt > 0 else None
        new_label = f"ci-fix-attempt-{next_attempt}"

        # 7. Checkout worktree, then update label, then dispatch
        flow_run_id = str(uuid.uuid4())
        try:
            # Off the host store, never the checkout being fixed (#3023), and
            # before the checkout, so a refused profile leaves none behind.
            addendum = launch_addendum(store, "ci_fix")
        except PromptConfigurationError as exc:
            _error(f"CI fix for PR #{pr_number} refused: {exc}")
            record_prompt_refusal(
                store,
                config,
                run_id=flow_run_id,
                run_kind=CI_FIX_RUN_KIND,
                issue_num=issue_number,
                branch=branch_name,
                item_id=item_id or None,
                runner=resolve_runner(config).runner.name,
            )
            return PromptRefusal(str(exc))
        launch_state = LaunchState()
        producer_run_id = None
        try:
            producer_run_id, referenced_producer_run_id = _ci_fix_producer(
                store, branch=branch_name, head_sha=expected_head_sha
            )
            _log(
                f"Checking out worktree for PR #{pr_number} "
                f"(branch: {branch_name}, attempt {next_attempt})..."
            )
            worktree_dir, _conflict_info = checkout_existing_worktree(
                issue_num=issue_number,
                item_id=item_id or None,
                provider=config.tracker.type,
                branch_name=branch_name,
                config=config,
                pinned_head_sha=expected_head_sha,
            )

            resolved = resolve_runner(config)
            inference_transport = resolve_inference_transport(
                config, resolved.runner, resolved.model
            )

            def _prepare_launch() -> tuple[
                ResolvedRoute, SandboxDecision, PublicationMode, bool, str
            ]:
                runner = resolved.runner
                containment = resolve_flow_containment(
                    store,
                    inference_transport=inference_transport,
                    flow="CI fix",
                    runner=runner.name,
                    config=config.sandbox,
                    # This flow's agent runs as a container argv, so the
                    # OpenShell gateway is not a boundary it can launch into.
                    gateway_launch=False,
                    run_id=flow_run_id,
                    checkout_dir=worktree_dir,
                    repo=config.project.repo or None,
                    issue_num=issue_number,
                    item_id=item_id,
                )
                from kew.runner_preflight import runner_preflight_failure

                if not containment.sandboxed:
                    failure = runner_preflight_failure(config, runner_name=runner.name)
                    if failure is not None:
                        raise RuntimeError(failure)
                # The publication mode is chosen BEFORE the prompt: a canonical or
                # host-style run must be told not to push, and must be published by
                # the host whether or not it ran in a container (review round 3).
                publication_mode = select_publication_mode(
                    config.publication.publish,
                    dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
                    store=store,
                    run_id=flow_run_id,
                    issue_num=issue_number,
                    item_id=item_id,
                    branch=branch_name,
                    repo=config.project.repo or None,
                )
                host_publication = (
                    containment.sandboxed
                    or publication_mode == "canonical"
                    or getattr(runner, "publication_style", "agent") == "host"
                )
                if host_publication and not expected_head_sha:
                    raise RuntimeError(
                        "host publication of a CI fix requires the PR head SHA"
                    )
                prompt = build_ci_fix_prompt(
                    pr_number=pr_number,
                    issue_number=issue_number,
                    issue_title=pr_title or f"Issue #{issue_number}",
                    branch_name=branch_name,
                    ci_log=ci_log,
                    attempt=next_attempt,
                    max_attempts=max_retries,
                    config=config,
                    host_publication=host_publication,
                    profile_addendum=addendum,
                )
                return resolved, containment, publication_mode, host_publication, prompt

            resolved, containment, publication_mode, host_publication, prompt = (
                _prepare_launch()
            )
            runner = resolved.runner

            def _run_fix() -> DispatchResult:
                _log(f"Dispatching CI fix for PR #{pr_number}...")
                require_pinned_pr_head(worktree_dir, branch_name, expected_head_sha)
                result = run_tracked_agent(
                    on_admitting=partial(
                        store.allocate_ci_fix_attempt,
                        flow_run_id,
                        pr_number,
                        max_attempts=max_retries,
                    ),
                    on_admitted=partial(
                        _update_attempt_labels,
                        pr_number=pr_number,
                        config=config,
                        new_label=new_label,
                        old_label=old_label,
                        store=store,
                        labels=label_names,
                    ),
                    launch_state=launch_state,
                    runner=runner,
                    prompt=prompt,
                    prompt_profile_version_id=profile_version_id(addendum),
                    worktree_dir=worktree_dir,
                    config=config,
                    issue_num=issue_number,
                    model=resolved.model,
                    store=store,
                    title=pr_title or f"Issue #{issue_number}",
                    branch=branch_name,
                    complexity="medium",
                    run_kind=CI_FIX_RUN_KIND,
                    run_id=flow_run_id,
                    item_id=item_id,
                    source_tracker=config.tracker.type,
                    worker_cap=config.fleet_concurrency(),
                    cap_exclude_issue=issue_number,
                    admit_budget_check=_admit_budget_check,
                    contained_image=containment.image,
                    inference_transport=inference_transport,
                    sandbox_posture=containment.posture,
                    allow_unrecorded=store is None,
                    validate_continuation=False,
                    budget_check=budget_watchdog(
                        config.agent.max_budget_usd,
                        runner_name=runner.name,
                        contained=containment.sandboxed,
                    ),
                )
                return result

            result = _run_fix()

            launch_state.observe(result)
            succeeded = _ci_fix_run_succeeded(result)

            def _publish_fix() -> None:
                if host_publication and succeeded:
                    # Not onto a head a merge verify claimed meanwhile (#3198),
                    # and no waiting for it either: this runs inside the tick.
                    defer_if_claimed(store, pr_number, expected_head_sha, flow="CI fix")
                    if (
                        store is not None
                        and producer_run_id
                        and not referenced_producer_run_id
                    ):
                        ensure_publication_credentials(store, run_id=producer_run_id)
                    publish_existing_pr_branch(
                        worktree_dir,
                        branch_name,
                        # A new CI-fix head needs fresh human approval (#2033).
                        before_push=partial(revoke_ci_approval, branch_name, config),
                        commit_message=f"fix: repair CI for PR #{pr_number}",
                        expected_remote_sha=expected_head_sha,
                        repo=config.project.repo or None,
                        publish=publication_mode,
                        dangerously_skip_containment=(
                            config.sandbox.dangerously_skip_containment
                        ),
                        store=store,
                        run_id=flow_run_id,
                        item_id=item_id,
                        producer_run_id=referenced_producer_run_id,
                    )

            _publish_fix()

        except PublishDeferred as exc:
            # The fix commit stays in the preserved worktree; the drive's next
            # tick skips the node while the claim lives, then fixes it again.
            _warn(
                f"PR #{pr_number}: {exc}; the fix stays in {worktree_dir} and "
                "the drive retries once the claim ends"
            )
            _emit_ci_fix_event(
                config,
                action_run_id=run_id,
                pr_number=pr_number,
                issue_number=issue_number,
                attempt=next_attempt,
                exit_code=1,
                run_id=flow_run_id,
                store=store,
                reason=str(exc),
            )
            return 1
        except CiFixAttemptsExhausted as exc:
            _warn(f"PR #{pr_number}: {exc}")
            return CI_FIX_CANNOT_RUN
        except BudgetExceeded as exc:
            # Lost the atomic admit-time race: no run was admitted.
            return _refuse_ci_fix_budget(exc)
        except WorkerCapReached:
            _error(WORKER_CAP_REACHED_MESSAGE)
            return CI_FIX_CANNOT_RUN
        except Exception as exc:

            def _record_exception(exc: Exception) -> int:
                if store is not None and isinstance(exc, CredentialPublicationRefused):
                    emit(
                        store,
                        CREDENTIAL_PUBLICATION_REFUSAL,
                        actor=Actor("policy", "ci-fix"),
                        run_id=_recorded_run_id(store, flow_run_id) or producer_run_id,
                        issue_num=issue_number,
                        item_id=item_id or None,
                        repo=config.project.repo or None,
                        payload={
                            "reason": exc.reason,
                            "publication_allowed": False,
                            "action_run_id": run_id,
                            "pr_number": pr_number,
                        },
                    )
                _error(f"CI fix dispatch for PR #{pr_number} failed: {exc}")
                _emit_ci_fix_event(
                    config,
                    action_run_id=run_id,
                    pr_number=pr_number,
                    issue_number=issue_number,
                    attempt=next_attempt,
                    exit_code=1,
                    run_id=flow_run_id,
                    store=store,
                    reason=str(exc),
                )
                return 1 if launch_state.kind == "executed" else CI_FIX_CANNOT_RUN

            return _record_exception(exc)

        def _finish_fix() -> int:
            # 9. Cleanup / preserve
            _emit_ci_fix_event(
                config,
                action_run_id=run_id,
                pr_number=pr_number,
                issue_number=issue_number,
                attempt=next_attempt,
                exit_code=0 if succeeded else result.exit_code or 1,
                run_id=flow_run_id,
                endpoint=configured_endpoint(
                    str(getattr(runner, "name", "")), resolved.model or ""
                )
                or None,
                store=store,
                reason=_ci_fix_failure_reason(result) if not succeeded else None,
            )
            if succeeded:
                _success(f"CI fix for PR #{pr_number} completed successfully.")
                cleanup_worktree(worktree_dir, branch_name)
                return 0
            _error(
                f"CI fix for PR #{pr_number} failed (exit {result.exit_code}). "
                f"Worktree preserved: {worktree_dir}. {_ci_fix_failure_reason(result)}"
            )
            return 1 if launch_state.kind == "executed" else CI_FIX_CANNOT_RUN

        return _finish_fix()

    return _fix_pr()
