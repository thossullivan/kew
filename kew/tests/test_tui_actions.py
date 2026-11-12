from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from kew.service.fleet import RunStopResult
from kew.tui.actions import handle_dispatch, handle_kill, handle_retry


def _close_worker_coroutines(app) -> None:
    """Close coroutines handed to a mocked ``run_worker``.

    A ``MagicMock`` never awaits what it is given, so a test that only checks
    *that* a worker was started leaks the coroutine and pollutes an unrelated
    test with "was never awaited". Closing it here keeps the suite clean
    enough that a real un-awaited coroutine is visible as a failure.
    """
    for call in app.run_worker.call_args_list:
        for arg in call[0]:
            close = getattr(arg, "close", None)
            if close is not None and hasattr(arg, "cr_await"):
                close()


def test_handle_dispatch_no_selection_is_noop():
    """handle_dispatch does nothing when no queue item is selected."""
    app = MagicMock()
    app.screen = MagicMock()
    with patch("kew.tui.actions.get_selected_issue", return_value=None):
        handle_dispatch(app)
    app.push_screen.assert_not_called()


def test_handle_dispatch_pushes_confirm_dialog():
    """handle_dispatch pushes a ConfirmDialog when a queue item is selected."""
    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app.state.queue_ready = [{"number": 42, "title": "Fix bug"}]
    app.state.queue_backlog = []
    app.state.queue_failed = []
    app._store = None
    with patch("kew.tui.actions.get_selected_issue", return_value=42):
        handle_dispatch(app)
    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert "42" in dialog.prompt
    assert "Fix bug" in dialog.prompt


def test_handle_kill_no_selection_is_noop():
    app = MagicMock()
    app.screen = MagicMock()
    with patch("kew.tui.actions.get_selected_run", return_value=None):
        handle_kill(app)
    app.push_screen.assert_not_called()


def test_handle_retry_skips_successful_run():
    """Retry is a no-op for successful runs."""
    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    run = MagicMock(run_id="r1", issue_num=42, status="success")
    app.state.recent_runs = [run]
    with patch("kew.tui.actions.get_selected_run", return_value="r1"):
        handle_retry(app)
    app.push_screen.assert_not_called()


def test_handle_retry_pushes_confirm_for_failed():
    """Retry pushes ConfirmDialog for failed runs."""
    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app._store = None
    run = MagicMock(run_id="r1", issue_num=42, status="failure", title="Fix it")
    app.state.recent_runs = [run]
    with patch("kew.tui.actions.get_selected_run", return_value="r1"):
        handle_retry(app)
    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert "42" in dialog.prompt


def test_handle_review_no_selection_is_noop():
    from kew.tui.actions import handle_review

    app = MagicMock()
    with patch("kew.tui.actions.get_selected_pr", return_value=None):
        handle_review(app)
    app.push_screen.assert_not_called()


def test_handle_review_with_selection_shows_confirm():
    from kew.tui.actions import handle_review

    app = MagicMock()
    app._store = None
    app.state.review_prs = [
        {"number": 87, "title": "Fix widget"},
    ]
    with patch("kew.tui.actions.get_selected_pr", return_value=87):
        handle_review(app)
    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert "87" in dialog.prompt
    assert "Fix widget" in dialog.prompt


def test_handle_quit_pushes_confirm_dialog():
    """handle_quit pushes a ConfirmDialog asking to confirm exit."""
    from kew.tui.actions import handle_quit

    app = MagicMock()
    handle_quit(app)
    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    from kew.tui.widgets.confirm_dialog import ConfirmDialog

    assert isinstance(dialog, ConfirmDialog)
    assert "quit" in dialog.prompt.lower() or "exit" in dialog.prompt.lower()


def test_handle_kill_process_already_exited():
    """Report the service's exited outcome without an error notification."""
    app = MagicMock()
    app.run_worker.side_effect = asyncio.run
    app.screen = MagicMock()
    app.state = MagicMock()
    run = MagicMock(run_id="r1", issue_num=42, title="Fix bug")
    app.state.pending_runs = [run]

    with patch("kew.tui.actions.get_selected_run", return_value="r1"):
        handle_kill(app)

    # Extract and call the on_confirm callback
    callback = (
        app.push_screen.call_args[1].get("callback") or app.push_screen.call_args[0][1]
    )

    with patch(
        "kew.service.fleet.stop_run",
        return_value=RunStopResult(
            "exited", "Attempt already exited; nothing signalled"
        ),
    ):
        callback(True)

    # Should notify without severity="error" — process is already gone, not a failure
    app.notify.assert_called_once()
    assert app.notify.call_args.kwargs.get("severity") != "error"


def test_handle_kill_service_error_still_reports():
    """Report failures from the shared stop service."""
    app = MagicMock()
    app.run_worker.side_effect = asyncio.run
    app.screen = MagicMock()
    app.state = MagicMock()
    run = MagicMock(run_id="r1", issue_num=42, title="Fix bug")
    app.state.pending_runs = [run]

    with patch("kew.tui.actions.get_selected_run", return_value="r1"):
        handle_kill(app)

    callback = (
        app.push_screen.call_args[1].get("callback") or app.push_screen.call_args[0][1]
    )

    with patch(
        "kew.service.fleet.stop_run",
        side_effect=PermissionError("Operation not permitted"),
    ):
        callback(True)

    app.notify.assert_called_once()
    _, kwargs = app.notify.call_args
    assert kwargs.get("severity") == "error"


def test_cockpit_tab_focus_order():
    """Tab cycles panels in order: queue → workers → reviews → analytics."""
    from kew.tui.screens.cockpit import CockpitScreen

    expected = ("queue-panel", "workers-panel", "reviews-panel", "analytics-panel")
    assert expected == CockpitScreen._FOCUS_ORDER


def test_cockpit_panel_bindings_order():
    """Number key bindings: 1=queue, 2=workers, 3=reviews, 4=analytics."""
    from kew.tui.screens.cockpit import CockpitScreen

    binding_map = {b[0]: b[1] for b in CockpitScreen.BINDINGS}
    assert binding_map["1"] == "switch_queue"
    assert binding_map["2"] == "switch_workers"
    assert binding_map["3"] == "switch_reviews"
    assert binding_map["4"] == "switch_analytics"


def test_handle_dispatch_budget_exceeded_shows_budget_dialog():
    """When budget is exceeded, handle_dispatch shows BudgetExceededDialog."""
    from kew.budget import BudgetExceeded, BudgetStatus
    from kew.tui.actions import handle_dispatch
    from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app.state.queue_ready = [{"number": 42, "title": "Fix bug"}]
    app.state.queue_backlog = []
    app.state.queue_failed = []
    app._store = MagicMock()

    status = BudgetStatus(
        issue_spent_usd=5.0,
        issue_limit_usd=15.0,
        daily_spent_usd=51.0,
        daily_limit_usd=50.0,
        weekly_spent_usd=80.0,
        weekly_limit_usd=200.0,
    )

    with (
        patch("kew.tui.actions.get_selected_issue", return_value=42),
        patch(
            "kew.budget.check_budget",
            side_effect=BudgetExceeded("daily", 50.0, 51.0, status),
        ),
    ):
        handle_dispatch(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, BudgetExceededDialog)

    # Verify callback dispatches with force_budget=True
    callback = (
        app.push_screen.call_args[1].get("callback") or app.push_screen.call_args[0][1]
    )
    callback(True)
    app.run_worker.assert_called_once()
    _close_worker_coroutines(app)


def test_handle_dispatch_within_budget_shows_confirm_dialog():
    """When budget is OK, handle_dispatch shows normal ConfirmDialog."""
    from kew.budget import BudgetStatus
    from kew.tui.actions import handle_dispatch
    from kew.tui.widgets.confirm_dialog import ConfirmDialog

    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app.state.queue_ready = [{"number": 42, "title": "Fix bug"}]
    app.state.queue_backlog = []
    app.state.queue_failed = []
    app._store = MagicMock()

    status = BudgetStatus(
        issue_spent_usd=5.0,
        issue_limit_usd=15.0,
        daily_spent_usd=20.0,
        daily_limit_usd=50.0,
        weekly_spent_usd=80.0,
        weekly_limit_usd=200.0,
    )

    with (
        patch("kew.tui.actions.get_selected_issue", return_value=42),
        patch("kew.budget.check_budget", return_value=status),
    ):
        handle_dispatch(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, ConfirmDialog)


def test_handle_dispatch_no_store_skips_budget_check():
    """When app has no store, budget check is skipped and normal ConfirmDialog shown."""
    from kew.tui.actions import handle_dispatch
    from kew.tui.widgets.confirm_dialog import ConfirmDialog

    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app.state.queue_ready = [{"number": 42, "title": "Fix bug"}]
    app.state.queue_backlog = []
    app.state.queue_failed = []
    app._store = None

    with patch("kew.tui.actions.get_selected_issue", return_value=42):
        handle_dispatch(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, ConfirmDialog)


def test_handle_retry_budget_exceeded_shows_budget_dialog():
    """When budget is exceeded, handle_retry shows BudgetExceededDialog."""
    from kew.budget import BudgetExceeded, BudgetStatus
    from kew.tui.actions import handle_retry
    from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app._store = MagicMock()
    run = MagicMock(
        run_id="r1",
        issue_num=42,
        status="failure",
        title="Fix it",
        branch="agent/issue-42",
    )
    app.state.recent_runs = [run]

    status = BudgetStatus(
        issue_spent_usd=5.0,
        issue_limit_usd=15.0,
        daily_spent_usd=51.0,
        daily_limit_usd=50.0,
        weekly_spent_usd=80.0,
        weekly_limit_usd=200.0,
    )

    with (
        patch("kew.tui.actions.get_selected_run", return_value="r1"),
        patch(
            "kew.budget.check_budget",
            side_effect=BudgetExceeded("daily", 50.0, 51.0, status),
        ),
    ):
        handle_retry(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, BudgetExceededDialog)


def test_handle_review_budget_exceeded_shows_budget_dialog():
    """When budget is exceeded, handle_review shows BudgetExceededDialog."""
    from kew.budget import BudgetExceeded, BudgetStatus
    from kew.tui.actions import handle_review
    from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

    app = MagicMock()
    app._store = MagicMock()
    app.state = MagicMock()
    app.state.review_prs = [{"number": 87, "title": "Fix widget"}]

    status = BudgetStatus(
        issue_spent_usd=5.0,
        issue_limit_usd=15.0,
        daily_spent_usd=51.0,
        daily_limit_usd=50.0,
        weekly_spent_usd=80.0,
        weekly_limit_usd=200.0,
    )

    with (
        patch("kew.tui.actions.get_selected_pr", return_value=87),
        patch(
            "kew.budget.check_budget",
            side_effect=BudgetExceeded("daily", 50.0, 51.0, status),
        ),
    ):
        handle_review(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, BudgetExceededDialog)


def test_handle_approve_shows_approval_dialog_and_approves():
    from kew.tui.actions import handle_approve
    from kew.tui.widgets.approval_dialog import ApprovalDialog

    app = MagicMock()
    app.state.queue_held = [{"number": 42, "estimate_usd": 3.1, "basis": "median"}]
    with patch("kew.tui.actions.get_selected_issue", return_value=42):
        handle_approve(app)

    app.push_screen.assert_called_once()
    dialog = app.push_screen.call_args[0][0]
    assert isinstance(dialog, ApprovalDialog)
    callback = app.push_screen.call_args[1]["callback"]

    callback("approve")
    app.run_worker.assert_called_once()
    _close_worker_coroutines(app)
    app.run_worker.reset_mock()
    callback("decline")
    app.run_worker.assert_called_once()
    _close_worker_coroutines(app)
    app.run_worker.reset_mock()
    callback(None)  # cancel
    app.run_worker.assert_not_called()


def test_handle_approve_no_held_selection_is_noop():
    from kew.tui.actions import handle_approve

    app = MagicMock()
    app.state.queue_held = []
    with patch("kew.tui.actions.get_selected_issue", return_value=None):
        handle_approve(app)
    app.push_screen.assert_not_called()


def test_do_approval_notifies_on_result():
    import asyncio

    from kew.tui.actions import _do_approval

    app = MagicMock()
    app.config = MagicMock()
    with patch(
        "kew.approval.apply_approval_decision", return_value="released"
    ) as mock_apply:
        asyncio.run(_do_approval(app, 42, "approve"))
    mock_apply.assert_called_once()
    assert mock_apply.call_args[1]["source"] == "tui"
    assert mock_apply.call_args[0][0] == "approve"
    app.notify.assert_called_once()


def test_do_approval_catches_exceptions():
    import asyncio

    from kew.tui.actions import _do_approval

    app = MagicMock()
    app.config = MagicMock()
    with patch(
        "kew.approval.apply_approval_decision", side_effect=RuntimeError("boom")
    ):
        asyncio.run(_do_approval(app, 42, "approve"))  # must not raise
    app.notify.assert_called_once()
    assert app.notify.call_args[1].get("severity") == "error"


def test_cockpit_action_approve_gates_on_focus_and_held_selection():
    from unittest.mock import MagicMock, PropertyMock, patch

    from textual.widgets import ListView

    from kew.tui.screens.cockpit import CockpitScreen

    screen = CockpitScreen.__new__(CockpitScreen)  # no Textual mount
    mock_app = MagicMock()
    queue_lv = ListView(id="queue-panel")
    other_lv = ListView(id="workers-panel")

    # Focus on a non-queue panel → no-op even if a held item is selected
    with (
        patch.object(
            type(screen), "focused", new_callable=PropertyMock, return_value=other_lv
        ),
        patch("kew.tui.selection.is_selected_held", return_value=True),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_not_called()

    # Queue focus + not held → no-op
    with (
        patch.object(
            type(screen), "focused", new_callable=PropertyMock, return_value=queue_lv
        ),
        patch("kew.tui.selection.is_selected_held", return_value=False),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_not_called()

    # Queue focus + held → handle_approve called with the app
    with (
        patch.object(
            type(screen), "focused", new_callable=PropertyMock, return_value=queue_lv
        ),
        patch.object(
            type(screen), "app", new_callable=PropertyMock, return_value=mock_app
        ),
        patch("kew.tui.selection.is_selected_held", return_value=True),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_called_once_with(mock_app)


def test_expanded_action_approve_gates_on_panel_and_held_selection():
    from unittest.mock import MagicMock, PropertyMock, patch

    from kew.tui.screens.expanded import ExpandedScreen

    screen = ExpandedScreen.__new__(ExpandedScreen)  # no Textual mount
    mock_app = MagicMock()

    # Non-queue panel → no-op even if a held item is selected
    screen.panel = "reviews"
    with (
        patch("kew.tui.selection.is_selected_held", return_value=True),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_not_called()

    # Queue panel + not held → no-op
    screen.panel = "queue"
    with (
        patch("kew.tui.selection.is_selected_held", return_value=False),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_not_called()

    # Queue panel + held → handle_approve called with the app
    with (
        patch.object(
            type(screen), "app", new_callable=PropertyMock, return_value=mock_app
        ),
        patch("kew.tui.selection.is_selected_held", return_value=True),
        patch("kew.tui.actions.handle_approve") as mock_handle,
    ):
        screen.action_approve()
        mock_handle.assert_called_once_with(mock_app)


# --------------------------------------------------------------------------- #
# I23 — critical TUI actions route through the shared service verbs.
#
# The TUI used to mutate the domain directly (dispatch, retry) from whatever a
# row happened to be showing. These tests pin the replacement: one centrally
# derived offer, decided against ONE named revision, applied by
# ``kew.service.verbs.perform_work_action``, and reflected only from what came
# back. Nothing here asserts a state the UI assigned itself.
# --------------------------------------------------------------------------- #


def _offer(
    action: str,
    *,
    safest: bool = True,
    requires_reason: bool = False,
    target_kind: str = "run",
    target_ref: str = "r1",
):
    from types import SimpleNamespace

    return SimpleNamespace(
        action=action,
        intent=f"the {action} intent",
        target_kind=target_kind,
        target_ref=target_ref,
        safest=safest,
        requires_reason=requires_reason,
    )


def _work_view(
    *,
    item_id: str = "SFD-12",
    item_ref: str = "SFD-12",
    exception: str = "awaiting_publication",
    safest: str = "finalize_publication",
    reason: str = "publish that same artifact",
    actions=None,
    revision: str = "wv1:deadbeef",
):
    """A stand-in for one composed :class:`WorkViewDTO`."""
    from types import SimpleNamespace

    return SimpleNamespace(
        identity=SimpleNamespace(
            item_id=item_id, item_ref=item_ref, source_tracker="linear"
        ),
        exception=SimpleNamespace(state=exception, detail="", blocking=False),
        safest_action=SimpleNamespace(action=safest, reason=reason),
        actions=[_offer(safest)] if actions is None else list(actions),
        view_revision=revision,
    )


def _work_app(view, *, ref=None):
    """A TUI app whose highlighted row names ``view``'s item."""
    from kew.tui.selection import WorkItemRef

    app = MagicMock()
    app.screen = MagicMock()
    app.state = MagicMock()
    app.state.work_view.return_value = view
    app.config = MagicMock()
    app._selected_ref = ref or WorkItemRef(
        item_id=getattr(view, "identity", MagicMock()).item_id, tracker="linear"
    )
    return app


def _result(
    *,
    action: str = "finalize_publication",
    applied: bool = True,
    idempotent: bool = False,
    detail: str = "run r1 finalized to success",
    next_action: str = "none",
):
    from types import SimpleNamespace

    return SimpleNamespace(
        action=action,
        applied=applied,
        idempotent=idempotent,
        detail=detail,
        expected_revision="wv1:deadbeef",
        view=_work_view(safest=next_action, exception="none"),
    )


def _confirm_and_run(app, mock_verb=None):
    """Say yes to the pushed dialog and await the worker coroutine."""
    import asyncio

    app.push_screen.assert_called_once()
    app.push_screen.call_args[1]["callback"](True)
    coro = app.run_worker.call_args[0][0]
    if mock_verb is None:
        asyncio.run(coro)
        return None
    with patch("kew.service.verbs.perform_work_action", mock_verb):
        asyncio.run(coro)
    return mock_verb


def _invoke_work_action(app, view, **kwargs):
    """Run ``handle_work_action`` with the row's ref already selected."""
    from kew.tui.actions import handle_work_action

    with patch("kew.tui.actions.get_selected_work_ref", return_value=app._selected_ref):
        handle_work_action(app, **kwargs)


class TestCanonicalWorkActions:
    """Task 85/86 — one derived offer, one revision, one authoritative reply."""

    def test_finalizing_preserved_work_calls_the_verb_not_a_redispatch(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(app, MagicMock(return_value=_result()))

        verb.assert_called_once()
        assert verb.call_args[0][0] is app._store
        assert verb.call_args[1]["action"] == "finalize_publication"
        assert verb.call_args[1]["source"] == "tui"

    def test_the_opaque_item_id_and_its_revision_reach_the_verb_unchanged(self):
        view = _work_view(item_id="SFD-12", revision="wv1:cafe")
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(app, MagicMock(return_value=_result()))

        assert verb.call_args[1]["item_id"] == "SFD-12"
        assert verb.call_args[1]["expected_revision"] == "wv1:cafe"

    def test_no_change_confirm_is_the_safest_offer(self):
        view = _work_view(
            exception="no_change_pending",
            safest="confirm_no_change",
            actions=[
                _offer("confirm_no_change", target_kind="no_change_judgment"),
                _offer(
                    "reject_no_change",
                    safest=False,
                    requires_reason=True,
                    target_kind="no_change_judgment",
                ),
            ],
        )
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(
            app, MagicMock(return_value=_result(action="confirm_no_change"))
        )

        assert verb.call_args[1]["action"] == "confirm_no_change"

    def test_rejecting_a_no_change_is_the_counterpart_and_carries_words(self):
        view = _work_view(
            exception="no_change_pending",
            safest="confirm_no_change",
            actions=[
                _offer("confirm_no_change", target_kind="no_change_judgment"),
                _offer(
                    "reject_no_change",
                    safest=False,
                    requires_reason=True,
                    target_kind="no_change_judgment",
                ),
            ],
        )
        app = _work_app(view)
        _invoke_work_action(app, view, counterpart=True)
        verb = _confirm_and_run(
            app, MagicMock(return_value=_result(action="reject_no_change"))
        )

        assert verb.call_args[1]["action"] == "reject_no_change"
        assert verb.call_args[1]["reason"].strip()

    def test_risk_confirmation_addresses_the_publication_job(self):
        view = _work_view(
            exception="publication_risk_hold",
            safest="confirm_publication_risk",
            actions=[_offer("confirm_publication_risk", target_kind="publication_job")],
        )
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(
            app, MagicMock(return_value=_result(action="confirm_publication_risk"))
        )

        assert verb.call_args[1]["action"] == "confirm_publication_risk"

    def test_a_stalled_source_write_is_retried_as_a_sync_not_a_run(self):
        view = _work_view(
            exception="no_change_sync_failed",
            safest="synchronize_source",
            actions=[_offer("retry_no_change_sync", target_kind="no_change_judgment")],
        )
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(
            app, MagicMock(return_value=_result(action="retry_no_change_sync"))
        )

        assert verb.call_args[1]["action"] == "retry_no_change_sync"

    def test_the_fleet_latch_is_cleared_through_the_same_guarded_path(self):
        view = _work_view(
            exception="fleet_stopped",
            safest="resume_fleet",
            actions=[_offer("resume_fleet", target_kind="fleet", target_ref="fleet")],
        )
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(
            app, MagicMock(return_value=_result(action="resume_fleet"))
        )

        assert verb.call_args[1]["action"] == "resume_fleet"

    def test_an_action_the_projection_does_not_offer_is_refused_here(self):
        view = _work_view()  # offers finalize_publication only
        app = _work_app(view)
        _invoke_work_action(app, view, action="dispatch")

        app.push_screen.assert_not_called()
        message = app.notify.call_args[0][0]
        assert "finalize_publication" in message
        assert app.notify.call_args[1]["severity"] == "warning"

    def test_an_unread_projection_is_never_acted_on(self):
        app = _work_app(None)
        from kew.tui.selection import WorkItemRef

        app._selected_ref = WorkItemRef(item_id="SFD-12", tracker="linear")
        _invoke_work_action(app, None)

        app.push_screen.assert_not_called()
        app.run_worker.assert_not_called()
        assert app.notify.call_args[1]["severity"] == "warning"

    def test_no_selected_work_item_is_a_silent_noop(self):
        from kew.tui.actions import handle_work_action

        app = _work_app(_work_view())
        with patch("kew.tui.actions.get_selected_work_ref", return_value=None):
            handle_work_action(app)
        app.push_screen.assert_not_called()
        app.notify.assert_not_called()

    def test_declining_the_dialog_performs_nothing(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        app.push_screen.call_args[1]["callback"](False)
        app.run_worker.assert_not_called()


class TestWorkActionResponses:
    """Task 85/87 — the UI moves on the reply, and failures stay on screen."""

    def test_a_stale_revision_is_reported_and_never_retried_blindly(self):
        from kew.service.verbs import WorkActionStale

        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        verb = _confirm_and_run(app, MagicMock(side_effect=WorkActionStale("moved")))

        assert verb.call_count == 1  # refused once; not re-attempted
        assert app.notify.call_args[1]["severity"] == "error"
        assert "moved" in app.notify.call_args[0][0]

    def test_a_failed_effect_stays_visible_as_an_error(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        _confirm_and_run(app, MagicMock(side_effect=RuntimeError("forge is down")))

        assert app.notify.call_args[1]["severity"] == "error"
        assert "forge is down" in app.notify.call_args[0][0]

    def test_a_duplicate_action_reports_the_replay_and_repeats_no_effect(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        _confirm_and_run(
            app,
            MagicMock(
                return_value=_result(
                    applied=False,
                    idempotent=True,
                    detail="this exact request already applied",
                )
            ),
        )

        message = app.notify.call_args[0][0]
        assert "already applied" in message
        assert app.notify.call_args[1].get("severity") in (None, "information")

    def test_the_notification_is_read_out_of_the_authoritative_response(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        _confirm_and_run(
            app,
            MagicMock(
                return_value=_result(
                    detail="run r1 finalized to success", next_action="none"
                )
            ),
        )

        message = app.notify.call_args[0][0]
        assert "run r1 finalized to success" in message
        assert "none" in message  # the post-write safest action, not a guess

    def test_the_pane_is_rebuilt_only_after_the_response_arrives(self):
        view = _work_view()
        app = _work_app(view)
        _invoke_work_action(app, view)
        app.state.refresh_work.assert_not_called()
        app.screen.refresh_content.assert_not_called()

        _confirm_and_run(app, MagicMock(return_value=_result()))

        app.state.refresh_work.assert_called_once()
        app.screen.refresh_content.assert_called_once()

    def test_every_work_coroutine_is_awaited_without_a_runtime_warning(self):
        import asyncio
        import warnings

        from kew.tui.actions import _do_work_action

        view = _work_view()
        app = _work_app(view)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            _invoke_work_action(app, view)
            _confirm_and_run(app, MagicMock(return_value=_result()))
            with patch(
                "kew.service.verbs.perform_work_action",
                MagicMock(return_value=_result()),
            ):
                asyncio.run(
                    _do_work_action(
                        app,
                        item_id="SFD-12",
                        action="finalize_publication",
                        expected_revision="wv1:deadbeef",
                    )
                )
        assert app.notify.called


class TestNoBlindRetryOverUsefulWork:
    """Task 87 — dispatch and retry cannot run over work already preserved."""

    def _queue_app(self, view):
        app = _work_app(view)
        app.state.queue_ready = [{"number": 42, "title": "Fix bug", "item_id": "42"}]
        app.state.queue_backlog = []
        app.state.queue_failed = []
        app._store = None
        return app

    def test_awaiting_publication_cannot_be_dispatched_from_the_queue(self):
        from kew.tui.actions import handle_dispatch

        view = _work_view(item_id="42", item_ref="#42")
        app = self._queue_app(view)
        with patch("kew.tui.actions.get_selected_issue", return_value=42):
            handle_dispatch(app)

        app.push_screen.assert_not_called()
        assert "finalize_publication" in app.notify.call_args[0][0]

    def test_awaiting_publication_cannot_be_retried(self):
        from kew.tui.actions import handle_retry

        view = _work_view(item_id="42", item_ref="#42")
        app = _work_app(view)
        app._store = None
        run = MagicMock(
            run_id="r1",
            issue_num=42,
            item_id="42",
            status="awaiting_publication",
            title="Fix it",
            branch="agent/issue-42",
        )
        app.state.recent_runs = [run]
        with patch("kew.tui.actions.get_selected_run", return_value="r1"):
            handle_retry(app)

        app.push_screen.assert_not_called()
        assert "finalize_publication" in app.notify.call_args[0][0]

    def test_a_stalled_source_write_is_not_a_retry_target(self):
        from kew.tui.actions import handle_retry

        view = _work_view(
            item_id="42",
            item_ref="#42",
            exception="no_change_sync_failed",
            safest="synchronize_source",
            actions=[_offer("retry_no_change_sync", target_kind="no_change_judgment")],
        )
        app = _work_app(view)
        app._store = None
        run = MagicMock(
            run_id="r1",
            issue_num=42,
            item_id="42",
            status="failure",
            title="Fix it",
            branch="agent/issue-42",
        )
        app.state.recent_runs = [run]
        with patch("kew.tui.actions.get_selected_run", return_value="r1"):
            handle_retry(app)

        app.push_screen.assert_not_called()
        message = app.notify.call_args[0][0]
        assert "synchronize_source" in message  # the safe key, not another run

    def test_a_retryable_terminal_failure_still_dispatches(self):
        from kew.tui.actions import handle_dispatch

        view = _work_view(
            item_id="42",
            item_ref="#42",
            exception="terminal_failure",
            safest="dispatch",
            actions=[_offer("dispatch", target_kind="item", target_ref="42")],
        )
        app = self._queue_app(view)
        with patch("kew.tui.actions.get_selected_issue", return_value=42):
            handle_dispatch(app)

        app.push_screen.assert_called_once()

    def test_an_unread_projection_does_not_fabricate_a_refusal(self):
        from kew.tui.actions import handle_dispatch

        app = self._queue_app(None)
        with patch("kew.tui.actions.get_selected_issue", return_value=42):
            handle_dispatch(app)

        app.push_screen.assert_called_once()
