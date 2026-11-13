"""Shared TUI action handlers — used by both CockpitScreen and ExpandedScreen."""

from __future__ import annotations

from functools import partial
from typing import Any, cast

from rich.markup import escape

from kew.ids import item_ref
from kew.models import GhIssue
from kew.tui.selection import (
    WorkItemRef,
    get_selected_epic_hold,
    get_selected_issue,
    get_selected_orphan,
    get_selected_pr,
    get_selected_run,
    get_selected_work_ref,
)
from kew.tui.widgets.confirm_dialog import ConfirmDialog

# Action handlers accept ``app: Any`` rather than ``KewApp`` because
# ``Screen.app`` returns ``App[Any]`` in Textual's type stubs.  The
# functions access KewApp-specific attributes (state, config, _store)
# duck-typed at runtime.


# --------------------------------------------------------------------------- #
# Canonical work actions (I23).
#
# A terminal row is not a mandate. Every critical mutation below is decided by
# ``kew.service.work`` — the same projection the row is rendered from — and
# applied by ``kew.service.verbs.perform_work_action`` against the exact
# revision the operator was looking at. The TUI therefore cannot offer a button
# the projection does not stand behind, cannot act on facts that have since
# moved, and cannot turn preserved-but-unpublished work into a second attempt.
# --------------------------------------------------------------------------- #

#: A reason-requiring decision needs words, and the TUI has no free-text field.
#: It records what it actually knows — where the decision was taken — instead
#: of inventing a justification nobody typed. *Who* took it already rides on
#: the audit event as its actor.
TUI_DECISION_REASON = "decided from the kew TUI; no written reason was captured"


def _canonical_view(app: Any, item_id: str) -> Any:
    """The canonical work view for one item, or None when there is no opinion.

    "No opinion" and "no" are different answers. A surface without state, an
    item the projection did not read, and a view that carries no offers list
    all return None here — which lets the legacy paths keep working rather
    than fabricating a refusal out of an unread fact.
    """
    state = getattr(app, "state", None)
    view = state.work_view(item_id) if state is not None else None
    return view if isinstance(getattr(view, "actions", None), list) else None


def _offered(view: Any, action: str) -> Any:
    """The offer for ``action`` on this view, or None when it is not offered."""
    return next((offer for offer in view.actions if offer.action == action), None)


def _refusal(view: Any, action: str) -> str:
    """Why this key did nothing, in the projection's own words."""
    safest = view.safest_action
    return (
        f"{view.identity.item_ref}: {action} is not offered — "
        f"{view.exception.state}; the safe next step is "
        f"{safest.action} ({safest.reason})"
    )


def refuses_work_action(app: Any, item_id: str, action: str) -> bool:
    """True when the projection read this item and does not offer ``action``.

    This is the whole of "no blind retry": a run whose implementation is
    preserved but unpublished offers ``finalize_publication`` and nothing
    else, so dispatch and retry cannot be aimed at it from any pane.
    """
    view = _canonical_view(app, item_id)
    if view is None or _offered(view, action) is not None:
        return False
    app.notify(_refusal(view, action), severity="warning", timeout=6)
    return True


def _chosen_offer(view: Any, action: str, counterpart: bool) -> Any:
    """The named offer, or the derived safest one / its authorized counterpart.

    ``safest`` is set by the projection on exactly one offer; anything else it
    lists is the counterpart decision the same person may make with the same
    facts in front of them (refusing a zero-diff outcome rather than
    confirming it).
    """
    if action:
        return _offered(view, action)
    matching = [
        offer for offer in view.actions if bool(offer.safest) is not counterpart
    ]
    return matching[0] if matching else None


def handle_work_action(
    app: Any,
    *,
    action: str = "",
    counterpart: bool = False,
    panel_id: str = "holds-queue-panel",
) -> None:
    """Perform ONE centrally derived safe action against the highlighted item.

    With no ``action`` this runs whatever the projection says is safest — or,
    with ``counterpart``, the authorized decision beside it. Either way the
    item id is the opaque canonical one and the revision is the one on screen.
    """
    ref = get_selected_work_ref(app.screen, panel_id)
    if ref is None:
        return
    view = _canonical_view(app, ref.item_id)
    if view is None:
        app.notify(
            f"{ref.display}: the canonical work view was not read — "
            "refresh before acting",
            severity="warning",
            timeout=6,
        )
        return
    offer = _chosen_offer(view, action, counterpart)
    if offer is None:
        app.notify(
            _refusal(view, action or view.safest_action.action),
            severity="warning",
            timeout=6,
        )
        return
    _confirm_work_action(app, ref, view, offer)


def _confirm_work_action(app: Any, ref: WorkItemRef, view: Any, offer: Any) -> None:
    """Ask, then hand the decision to the service verb — nothing in between."""
    reason = TUI_DECISION_REASON if offer.requires_reason else ""
    prompt = f"{ref.display}: {offer.intent}?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(
            _do_work_action(
                app,
                item_id=ref.item_id,
                action=offer.action,
                expected_revision=view.view_revision,
                reason=reason,
            )
        )

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


def _work_result_message(result: Any) -> str:
    """What the response says happened, and what it says to do next."""
    lead = result.detail or result.action
    if result.idempotent:
        lead = f"{lead} — already applied; the effect was not repeated"
    return f"{lead} · next: {result.view.safest_action.action}"


async def _resync_work(app: Any) -> None:
    """Re-read canonical truth, then rebuild the pane from it.

    Runs after a refusal as well as after a success: an action that failed
    because the world moved must not leave the operator deciding against the
    facts that just lost. The error notification stays on screen either way.
    """
    import asyncio

    state = getattr(app, "state", None)
    if state is not None:
        await asyncio.to_thread(state.refresh_work)
    refresh = getattr(app.screen, "refresh_content", None)
    if callable(refresh):
        refresh()


async def _do_work_action(
    app: Any,
    *,
    item_id: str,
    action: str,
    expected_revision: str,
    reason: str = "",
) -> None:
    """Apply one guarded work action and adopt ONLY what came back.

    The notification is read out of the authoritative post-write response, so
    the UI never reports a state it assigned itself. A denial, a moved
    revision or a failed effect surfaces as an error and is never retried on
    the operator's behalf.
    """
    import asyncio

    from kew.service.verbs import perform_work_action

    try:
        result = await asyncio.to_thread(
            perform_work_action,
            app._store,
            item_id=item_id,
            action=action,
            expected_revision=expected_revision,
            reason=reason,
            config=app.config,
            source="tui",
        )
    except Exception as exc:
        app.notify(f"{action} failed: {exc}", severity="error", timeout=6)
        await _resync_work(app)
        return
    app.notify(_work_result_message(result), timeout=4)
    await _resync_work(app)


def _refuses_dispatch(app: Any, item_id: str) -> bool:
    """Whether the projection refuses a (re)dispatch of this item.

    ``dispatch`` is the canonical name for "start another attempt", so both
    the queue keys and the retry key ask this one question. Preserved work,
    a stalled source write and a declined artifact all answer no.
    """
    return refuses_work_action(app, item_id, "dispatch")


def _find_issue(app: Any, issue_num: int) -> GhIssue | None:
    """Look up issue dict from state by number."""
    if app.state is None:
        return None
    for issue in app.state.queue_ready:
        if issue.get("number") == issue_num:
            return cast(GhIssue, issue)
    for issue in app.state.queue_backlog:
        if issue.get("number") == issue_num:
            return cast(GhIssue, issue)
    for issue in app.state.queue_failed:
        if issue.get("number") == issue_num:
            return cast(GhIssue, issue)
    return None


def handle_dispatch(app: Any) -> None:
    """Dispatch the selected queue issue."""
    from rich.markup import escape

    issue_num = get_selected_issue(app.screen)
    if issue_num is None:
        return
    issue = _find_issue(app, issue_num)
    if issue is None:
        return
    if _refuses_dispatch(app, WorkItemRef.from_issue(issue).item_id):
        return
    title = issue.get("title", "")

    # Pre-check budget if store is available
    store = getattr(app, "_store", None)
    if store is not None:
        from kew.budget import BudgetExceeded, check_budget

        try:
            check_budget(store, app.config.budget, issue_num)
        except BudgetExceeded as exc:
            from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

            def on_budget_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                app.run_worker(_do_dispatch(app, issue, force_budget=True))

            app.push_screen(
                BudgetExceededDialog(
                    title="Budget Exceeded",
                    issue_label=f"#{issue_num}: {escape(title)}",
                    status=exc.status,
                    confirm_key="d",
                ),
                callback=on_budget_confirm,
            )
            return

    # Budget OK or no store — show normal confirmation
    prompt = f"Dispatch #{issue_num} — {title}?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_dispatch(app, issue))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


def handle_kill(app: Any) -> None:
    """Kill the selected running worker."""
    run_id = get_selected_run(app.screen, "workers-panel")
    if run_id is None:
        return
    run = _find_run(app, run_id, "pending_runs")
    if run is None:
        return
    title = getattr(run, "title", "") or f"Issue #{run.issue_num}"
    prompt = f"Kill #{run.issue_num} — {title}?"
    app.push_screen(ConfirmDialog(prompt), callback=partial(_confirm_kill, app, run_id))


def _confirm_kill(app: Any, run_id: str, confirmed: bool) -> None:
    if confirmed:
        app.run_worker(_do_kill(app, run_id))


async def _do_kill(app: Any, run_id: str) -> None:
    """Keep process waits off the UI thread and report the service outcome."""
    import asyncio

    from kew.service.fleet import stop_run

    try:
        result = await asyncio.to_thread(stop_run, app._store, run_id, app.config)
    except Exception as exc:
        app.notify(f"Kill failed: {exc}", severity="error", timeout=3)
        return
    app.notify(
        result.message,
        severity="information" if result.outcome == "signalled" else "warning",
        timeout=3,
    )


def _run_item_id(run: Any) -> str:
    """The canonical item id behind a run row — opaque ids included."""
    from kew.tui.state import work_item_id

    return work_item_id(run)


def _find_run(app: Any, run_id: str, run_list: str) -> Any:
    """Look up RunRecord from state by run_id."""
    if app.state is None:
        return None
    runs = getattr(app.state, run_list, [])
    return next((r for r in runs if r.run_id == run_id), None)


def handle_retry(app: Any) -> None:
    """Retry the selected failed run."""
    from rich.markup import escape

    # Check workers panel first (now contains completed runs interleaved)
    run_id = get_selected_run(app.screen, "workers-panel")
    if run_id is None or app.state is None:
        return
    run = _find_run(app, run_id, "recent_runs")
    if run is None or run.status == "success":
        return
    if _refuses_dispatch(app, _run_item_id(run)):
        return
    title = getattr(run, "title", "") or f"Issue #{run.issue_num}"
    issue_num = run.issue_num

    # Pre-check budget if store is available
    store = getattr(app, "_store", None)
    if store is not None:
        from kew.budget import BudgetExceeded, check_budget

        try:
            check_budget(store, app.config.budget, issue_num)
        except BudgetExceeded as exc:
            from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

            def on_budget_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                if getattr(run, "branch", "").startswith("review-pr-"):
                    app.run_worker(_do_review(app, issue_num, force_budget=True))
                else:
                    app.run_worker(_do_retry(app, issue_num, force_budget=True))

            app.push_screen(
                BudgetExceededDialog(
                    title="Budget Exceeded",
                    issue_label=f"#{issue_num}: {escape(title)}",
                    status=exc.status,
                    confirm_key="r",
                ),
                callback=on_budget_confirm,
            )
            return

    prompt = f"Retry #{issue_num} — {title}?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        if getattr(run, "branch", "").startswith("review-pr-"):
            app.run_worker(_do_review(app, issue_num))
        else:
            app.run_worker(_do_retry(app, issue_num))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_retry(app: Any, issue_num: int, *, force_budget: bool = False) -> None:
    """Fetch issue and re-dispatch in background thread."""
    import asyncio
    import json

    from kew.dispatch_core import execute_dispatch
    from kew.subprocess_utils import run as sp_run

    try:
        repo = app.config.project.repo
        result = await asyncio.to_thread(
            sp_run,
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                repo,
                "--json",
                "number,title,body,labels",
            ],
            capture=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            app.notify(
                f"gh issue view failed: {result.stderr}", severity="error", timeout=3
            )
            return
        issue = json.loads(result.stdout)
        await asyncio.to_thread(
            execute_dispatch,
            issue=issue,
            config=app.config,
            store=app._store,
            force_budget=force_budget,
        )
    except Exception as exc:
        app.notify(f"Retry failed: {exc}", severity="error", timeout=3)


async def _do_dispatch(app: Any, issue: GhIssue, *, force_budget: bool = False) -> None:
    """Run dispatch in background thread."""
    import asyncio

    from kew.dispatch_core import execute_dispatch

    try:
        await asyncio.to_thread(
            execute_dispatch,
            issue=issue,
            config=app.config,
            store=app._store,
            force_budget=force_budget,
        )
    except Exception as exc:
        app.notify(f"Dispatch failed: {exc}", severity="error", timeout=3)


def _find_review_pr(app: Any, pr_number: int) -> dict[str, Any] | None:
    """Look up PR dict from state by number."""
    if app.state is None:
        return None
    for pr in app.state.review_prs:
        if pr.get("number") == pr_number:
            return dict(pr)
    return None


def handle_review(app: Any) -> None:
    """Dispatch review on selected PR from the reviews panel."""
    from rich.markup import escape

    pr_number = get_selected_pr(app.screen)
    if pr_number is None:
        return
    pr = _find_review_pr(app, pr_number)
    if pr is None:
        return
    title = pr.get("title", "")

    # Pre-check budget if store is available
    store = getattr(app, "_store", None)
    if store is not None:
        from kew.budget import BudgetExceeded, check_budget

        try:
            check_budget(store, app.config.budget, pr_number)
        except BudgetExceeded as exc:
            from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

            def on_budget_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                app.run_worker(_do_review(app, pr_number, force_budget=True))

            app.push_screen(
                BudgetExceededDialog(
                    title="Budget Exceeded",
                    issue_label=f"PR #{pr_number}: {escape(title)}",
                    status=exc.status,
                    confirm_key="r",
                ),
                callback=on_budget_confirm,
            )
            return

    prompt = f"Review PR #{pr_number} — {escape(title)}?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_review(app, pr_number))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_review(app: Any, pr_number: int, *, force_budget: bool = False) -> None:
    """Run review dispatch in background thread, tracking via RunStore."""
    import asyncio

    from kew.review import dispatch_review

    try:
        # Pass store to dispatch_review so run_tracked_agent handles
        # RunRecord lifecycle (pending → success/failure) automatically.
        outcome = await asyncio.to_thread(
            dispatch_review,
            pr_number=pr_number,
            config=app.config,
            store=app._store,
            force_budget=force_budget,
        )
        if outcome.kind == "not_run":
            app.notify(
                f"Review PR #{pr_number} could not run", severity="error", timeout=3
            )
        elif outcome.exit_code == 0:
            app.notify(f"Review PR #{pr_number} finished", timeout=3)
        else:
            reason = getattr(outcome, "reason", "")
            app.notify(
                f"Review PR #{pr_number} failed (exit {outcome.exit_code})"
                + (f": {reason}" if reason else ""),
                severity="error",
                timeout=3,
            )
    except Exception as exc:
        app.notify(f"Review failed: {exc}", severity="error", timeout=3)


def handle_resolve(app: Any) -> None:
    """Dispatch conflict resolution on selected PR from the reviews panel."""
    from rich.markup import escape

    pr_number = get_selected_pr(app.screen)
    if pr_number is None:
        return
    pr = _find_review_pr(app, pr_number)
    if pr is None:
        return
    title = pr.get("title", "")

    # Pre-check budget if store is available
    store = getattr(app, "_store", None)
    if store is not None:
        from kew.budget import BudgetExceeded, check_budget

        try:
            check_budget(store, app.config.budget, pr_number)
        except BudgetExceeded as exc:
            from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

            def on_budget_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                app.run_worker(_do_resolve(app, pr_number, force_budget=True))

            app.push_screen(
                BudgetExceededDialog(
                    title="Budget Exceeded",
                    issue_label=f"PR #{pr_number}: {escape(title)}",
                    status=exc.status,
                    confirm_key="m",
                ),
                callback=on_budget_confirm,
            )
            return

    prompt = f"Resolve merge conflicts on PR #{pr_number} — {escape(title)}?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_resolve(app, pr_number))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_resolve(app: Any, pr_number: int, *, force_budget: bool = False) -> None:
    """Run resolve dispatch in background thread."""
    import asyncio

    from kew.resolve import dispatch_resolve

    try:
        outcome = await asyncio.to_thread(
            dispatch_resolve,
            pr_number=pr_number,
            config=app.config,
            store=app._store,
            force_budget=force_budget,
        )
        if outcome.kind == "not_run":
            app.notify(
                f"Resolve PR #{pr_number} could not run", severity="error", timeout=3
            )
        elif outcome.exit_code == 0:
            app.notify(f"Resolve PR #{pr_number} finished", timeout=3)
        else:
            reason = getattr(outcome, "reason", "")
            app.notify(
                f"Resolve PR #{pr_number} failed (exit {outcome.exit_code})"
                + (f": {reason}" if reason else ""),
                severity="error",
                timeout=3,
            )
    except Exception as exc:
        app.notify(f"Resolve failed: {exc}", severity="error", timeout=3)


def _queue_sort_key(issue: GhIssue) -> tuple[int, str]:
    """Sort key matching selector ordering — priority then age."""
    from kew.selector import _priority_sort_key

    return (_priority_sort_key(issue), issue.get("createdAt", ""))


def _sorted_insert(lst: list[GhIssue], issue: GhIssue) -> None:
    """Insert issue into a list maintaining sort order."""
    import bisect

    keys = [_queue_sort_key(i) for i in lst]
    idx = bisect.bisect_right(keys, _queue_sort_key(issue))
    lst.insert(idx, issue)


def _toggle_ready_label(
    issue: dict[str, Any], *, add_ready: bool, ready_label: str
) -> None:
    """Update the issue dict's labels to reflect the ready toggle."""
    labels = issue.get("labels", [])
    if add_ready:
        if not any(
            isinstance(lbl, dict) and lbl.get("name") == ready_label for lbl in labels
        ):
            labels.append({"name": ready_label})
            issue["labels"] = labels
    else:
        issue["labels"] = [
            lbl
            for lbl in labels
            if not (isinstance(lbl, dict) and lbl.get("name") == ready_label)
        ]


def handle_toggle_ready(app: Any) -> None:
    """Toggle the ready label on the selected queue issue (optimistic UI)."""
    from kew.tui.selection import is_selected_failed, is_selected_ready

    issue_num = get_selected_issue(app.screen)
    if issue_num is None or app.state is None:
        return

    currently_ready = is_selected_ready(app.screen)
    currently_failed = is_selected_failed(app.screen)
    ready_label = app.config.labels.ready
    failed_label = app.config.labels.agent_failed

    if currently_ready:
        # Move from ready to backlog (existing logic — unchanged)
        issue = None
        for i, iss in enumerate(app.state.queue_ready):
            if iss.get("number") == issue_num:
                issue = app.state.queue_ready.pop(i)
                break
        if issue is None:
            return
        _toggle_ready_label(issue, add_ready=False, ready_label=ready_label)
        _sorted_insert(app.state.queue_backlog, issue)
        app._detail_issue_num = None
        app._update_widgets()
        app.run_worker(_do_unmark_ready(app, issue_num))
    elif currently_failed:
        # Move from failed to ready
        issue = None
        for i, iss in enumerate(app.state.queue_failed):
            if iss.get("number") == issue_num:
                issue = app.state.queue_failed.pop(i)
                break
        if issue is None:
            return
        # Save original labels for revert, then strip lifecycle labels
        original_labels = list(issue.get("labels", []))
        remove_labels = {
            failed_label,
            app.config.labels.budget_exceeded,
            app.config.labels.budget_stopped,
            app.config.labels.agent_needs_help,
        }
        issue["labels"] = [
            lbl
            for lbl in issue.get("labels", [])
            if not (isinstance(lbl, dict) and lbl.get("name") in remove_labels)
        ]
        _toggle_ready_label(issue, add_ready=True, ready_label=ready_label)
        _sorted_insert(app.state.queue_ready, issue)
        app._detail_issue_num = None
        app._update_widgets()
        app.run_worker(_do_mark_ready_from_failed(app, issue_num, original_labels))
    else:
        # Move from backlog to ready (existing logic — unchanged)
        issue = None
        for i, iss in enumerate(app.state.queue_backlog):
            if iss.get("number") == issue_num:
                issue = app.state.queue_backlog.pop(i)
                break
        if issue is None:
            return
        _toggle_ready_label(issue, add_ready=True, ready_label=ready_label)
        _sorted_insert(app.state.queue_ready, issue)
        app._detail_issue_num = None
        app._update_widgets()
        app.run_worker(_do_mark_ready(app, issue_num))


async def _do_mark_ready(app: Any, issue_num: int) -> None:
    """Add ready label in background thread. Reverts on failure."""
    import asyncio

    from kew.labels import mark_ready

    try:
        await asyncio.to_thread(mark_ready, issue_num, app.config)
    except Exception as exc:
        # Revert: move issue back from ready to backlog
        if app.state is not None:
            for i, iss in enumerate(app.state.queue_ready):
                if iss.get("number") == issue_num:
                    issue = app.state.queue_ready.pop(i)
                    _toggle_ready_label(
                        issue,
                        add_ready=False,
                        ready_label=app.config.labels.ready,
                    )
                    _sorted_insert(app.state.queue_backlog, issue)
                    app._detail_issue_num = None
                    app._update_widgets()
                    break
        app.notify(f"Failed to mark ready: {exc}", severity="error", timeout=3)


async def _do_unmark_ready(app: Any, issue_num: int) -> None:
    """Remove ready label in background thread. Reverts on failure."""
    import asyncio

    from kew.labels import unmark_ready

    try:
        await asyncio.to_thread(unmark_ready, issue_num, app.config)
    except Exception as exc:
        # Revert: move issue back from backlog to ready
        if app.state is not None:
            for i, iss in enumerate(app.state.queue_backlog):
                if iss.get("number") == issue_num:
                    issue = app.state.queue_backlog.pop(i)
                    _toggle_ready_label(
                        issue,
                        add_ready=True,
                        ready_label=app.config.labels.ready,
                    )
                    _sorted_insert(app.state.queue_ready, issue)
                    app._detail_issue_num = None
                    app._update_widgets()
                    break
        app.notify(f"Failed to unmark ready: {exc}", severity="error", timeout=3)


async def _do_mark_ready_from_failed(
    app: Any, issue_num: int, original_labels: list[dict[str, Any]]
) -> None:
    """Add ready + remove agent-failed in background. Reverts on failure."""
    import asyncio

    from kew.labels import mark_ready_from_failed

    current_label_names = [
        lbl.get("name", "") for lbl in original_labels if isinstance(lbl, dict)
    ]
    try:
        await asyncio.to_thread(
            mark_ready_from_failed,
            issue_num,
            app.config,
            current_labels=current_label_names,
        )
    except Exception as exc:
        # Revert: move issue back from ready to failed, restoring original labels
        if app.state is not None:
            for i, iss in enumerate(app.state.queue_ready):
                if iss.get("number") == issue_num:
                    issue = app.state.queue_ready.pop(i)
                    issue["labels"] = original_labels
                    _sorted_insert(app.state.queue_failed, issue)
                    app._detail_issue_num = None
                    app._update_widgets()
                    break
        app.notify(f"Failed to mark ready: {exc}", severity="error", timeout=3)


def _export_output_path() -> str:
    """Return the default export file path."""
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"kew-export-{ts}.json"


def handle_export(app: Any) -> None:
    """Export run history to a JSON file."""
    state = getattr(app, "state", None)
    store = getattr(state, "store", None) if state else None
    if store is None:
        return

    records = store.list_all()
    count = len(records)
    prompt = f"Export {count} run record{'s' if count != 1 else ''} to JSON?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        from kew.run_store import export_runs_json

        content = export_runs_json(records)
        path = _export_output_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            app.notify(f"Export failed: {exc}", severity="error", timeout=3)
            return
        app.notify(f"Exported {count} records to {path}", timeout=3)

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


def handle_purge(app: Any, *, days: int = 90) -> None:
    """Purge run records older than the given number of days."""
    if days <= 0:
        app.notify("Purge days must be greater than 0", severity="error", timeout=3)
        return

    state = getattr(app, "state", None)
    store = getattr(state, "store", None) if state else None
    if store is None:
        return

    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    total = len(store.list_all())
    retained = len(store.list_since(cutoff))
    to_delete = max(total - retained, 0)
    prompt = (
        f"Purge {to_delete} record{'s' if to_delete != 1 else ''} "
        f"older than {days} days?"
    )

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        try:
            deleted = store.purge_older_than(days)
        except Exception as exc:
            app.notify(f"Purge failed: {exc}", severity="error", timeout=3)
            return
        app.notify(f"Purged {deleted} record{'s' if deleted != 1 else ''}", timeout=3)

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


def handle_approve(app: Any) -> None:
    """Approve/decline the selected needs-approval held issue."""
    issue_num = get_selected_issue(app.screen)
    if issue_num is None:
        return
    held = next((h for h in app.state.queue_held if h.get("number") == issue_num), None)
    if held is None:
        return

    from kew.tui.widgets.approval_dialog import ApprovalDialog

    def on_decision(decision: str | None) -> None:
        if decision not in ("approve", "decline"):
            return  # cancel
        app.run_worker(_do_approval(app, issue_num, decision))

    app.push_screen(
        ApprovalDialog(
            issue_label=f"#{issue_num}",
            estimate_usd=held.get("estimate_usd"),
            basis=held.get("basis", ""),
        ),
        callback=on_decision,
    )


_APPROVAL_MESSAGES = {
    "released": ("#{n} released to ready", "information"),
    "denied": ("#{n} declined", "information"),
    "not_held": ("#{n} not held — nothing to do", "warning"),
    "gh_error": ("#{n}: couldn't reach GitHub", "error"),
    "unauthorized": (
        "Not authorized to approve — see [approval] approvers",
        "error",
    ),
}


async def _do_approval(app: Any, issue_num: int, decision: str) -> None:
    """Apply an approve/decline decision in a background thread."""
    import asyncio

    from kew.approval import apply_approval_decision

    action = "approve" if decision == "approve" else "deny"
    try:
        result = await asyncio.to_thread(
            apply_approval_decision,
            action,
            issue_num=issue_num,
            reason="",
            config=app.config,
            source="tui",
        )
    except Exception as exc:
        app.notify(f"Approval failed: {exc}", severity="error", timeout=3)
        return
    template, severity = _APPROVAL_MESSAGES.get(
        result, ("#{n}: " + result, "information")
    )
    app.notify(template.format(n=issue_num), severity=severity, timeout=3)


# --- Holds & Queue pane verbs (#685) -----------------------------------------
# Each verb reaches a mutation only through ``kew.service`` (never run_store or
# gh directly) and confirms first with the shared ConfirmDialog pattern.


def _hold_retry_cannot_clear(app: Any, item_id: str, epic_ref: str) -> str | None:
    """The selected row's hold reason when retry would only be refused for it.

    The holds pane is the retry verb's only selection surface, so without this
    the key pushes a confirm dialog and then reports a failure the row already
    knew about: ``epic_retry`` refuses every hold the driver honors (#2154).
    Answer up front instead, naming the command that does release the node.
    Rows held for a reason the overlay ignores keep their retry path.

    Keyed on the durable ``item_id``, never ``issue_num``: a non-numeric
    tracker writes that column as the ``0`` sentinel, so matching on it aliases
    every Linear hold in an epic onto the first one's reason.
    """
    from kew.epic_holds import holds_node_out_of_frontier, safe_hold_reason
    from kew.tui.state import hold_item_id

    holds = getattr(getattr(app, "state", None), "epic_holds", None) or []
    row = next(
        (h for h in holds if h.epic_ref == epic_ref and hold_item_id(h) == item_id),
        None,
    )
    if row is None or not holds_node_out_of_frontier(row.reason):
        return None
    return safe_hold_reason(row.reason)


def handle_retry_node(app: Any) -> None:
    """Re-admit the selected FAILED epic-DAG node (retry-node verb)."""
    sel = get_selected_epic_hold(app.screen)
    if sel is None:
        return
    issue_num, epic_ref = sel
    # The row's canonical id — the hold lookup and the `unhold` command it
    # names both key on it, and it is the ONLY id a Linear row has (its
    # ``issue_num`` is the shared ``0`` sentinel).
    ref = get_selected_work_ref(app.screen)
    item_id = ref.item_id if ref is not None else str(issue_num)
    reason = _hold_retry_cannot_clear(app, item_id, epic_ref)
    if reason is not None:
        # ``notify`` renders Rich markup, and an ``admission_refused`` reason
        # carries a detached child's stderr tail — escape it like every other
        # untrusted string the TUI renders (kew.tui.log_formatter).
        app.notify(
            f"{escape(item_ref(item_id))} is held ({escape(reason)}) — retry "
            f"cannot clear a hold; run "
            f"`kew epic unhold {escape(epic_ref)} {escape(item_id)}`",
            severity="warning",
            timeout=6,
        )
        return
    prompt = f"Retry epic node #{issue_num} ({epic_ref})?"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_retry_node(app, epic_ref, issue_num))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_retry_node(app: Any, epic_ref: str, issue_num: int) -> None:
    """Call the epic-retry service verb in a background thread."""
    import asyncio

    from kew.service.verbs import epic_retry_gated

    try:
        await asyncio.to_thread(
            # The console's gated verb: approver check + bound policy (#2552).
            epic_retry_gated,
            app._store,
            epic_ref=epic_ref,
            # The verb is item-keyed; the TUI hold selector still yields an
            # int issue_num, so pass its stringified id (the TUI-side item-id
            # sweep threads the durable id through separately).
            item_id=str(issue_num),
            config=app.config,
            source="tui",
        )
        app.notify(f"Retried epic node #{issue_num}", timeout=3)
    except Exception as exc:
        # ``EpicRetryHeld`` names the hold's reason, and an
        # ``admission_refused`` one carries a detached child's stderr tail —
        # unescaped, a bracketed tail raises MarkupError while the toast
        # renders and the refusal never reaches the operator at all. Escaped
        # exactly like the pre-check's refusal above.
        app.notify(
            f"Retry node failed: {escape(str(exc))}", severity="error", timeout=3
        )


def handle_queue_issue(app: Any) -> None:
    """Queue the highlighted item — through the derived ``dispatch`` offer.

    The blind counterpart of this used to label whatever number the row was
    carrying, so an item whose work was already preserved could be queued for
    a second attempt. Asking the projection for ``dispatch`` by name means the
    key does nothing (and says why) whenever queueing is not the safe move.
    """
    handle_work_action(app, action="dispatch")


def handle_ack_orphan(app: Any) -> None:
    """Acknowledge the selected orphan PR (ack-orphan verb)."""
    pr_number = get_selected_orphan(app.screen)
    if pr_number is None:
        return
    prompt = f"Acknowledge orphan PR #{pr_number}? (does not close the PR)"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_ack_orphan(app, pr_number))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_ack_orphan(app: Any, pr_number: int) -> None:
    """Call the ack-orphan service verb in a background thread."""
    import asyncio

    from kew.service.verbs import ack_orphan_pr

    try:
        acked = await asyncio.to_thread(
            ack_orphan_pr,
            app._store,
            pr_number=pr_number,
            config=app.config,
            source="tui",
        )
    except Exception as exc:
        app.notify(f"Ack failed: {exc}", severity="error", timeout=3)
        return
    if acked:
        app.notify(f"Acknowledged orphan PR #{pr_number}", timeout=3)
    else:
        app.notify(
            f"PR #{pr_number} not tracked — nothing to ack",
            severity="warning",
            timeout=3,
        )


def handle_resume_fleet(app: Any) -> None:
    """Clear the fleet-stop latch (resume-fleet verb)."""
    prompt = "Resume the fleet? (clears the stop latch)"

    def on_confirm(confirmed: bool) -> None:
        if not confirmed:
            return
        app.run_worker(_do_resume_fleet(app))

    app.push_screen(ConfirmDialog(prompt), callback=on_confirm)


async def _do_resume_fleet(app: Any) -> None:
    """Call the resume-fleet service verb in a background thread."""
    import asyncio

    from kew.service.verbs import resume_fleet

    try:
        resumed = await asyncio.to_thread(
            resume_fleet,
            app._store,
            config=app.config,
        )
    except Exception as exc:
        app.notify(f"Resume failed: {exc}", severity="error", timeout=3)
        return
    if resumed:
        app.notify("Fleet resumed", timeout=3)
    else:
        app.notify("Fleet was not stopped", severity="warning", timeout=3)


def handle_quit(app: Any) -> None:
    """Show confirmation dialog before quitting."""

    def on_confirm(confirmed: bool) -> None:
        if confirmed:
            app.exit()

    app.push_screen(ConfirmDialog("Quit kew?"), callback=on_confirm)
