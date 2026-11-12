from unittest.mock import MagicMock

from kew.tui.widgets.approval_dialog import ApprovalDialog


def _dialog():
    d = ApprovalDialog(issue_label="#42: Fix bug", estimate_usd=3.1, basis="median")
    d.dismiss = MagicMock()
    return d


def test_approve_dismisses_with_approve():
    d = _dialog()
    d.action_approve()
    d.dismiss.assert_called_once_with("approve")


def test_decline_dismisses_with_decline():
    d = _dialog()
    d.action_decline()
    d.dismiss.assert_called_once_with("decline")


def test_cancel_dismisses_with_none():
    d = _dialog()
    d.action_cancel()
    d.dismiss.assert_called_once_with(None)


def test_unknown_estimate_renders_without_crashing():
    # estimate_usd None must render as "unknown" — exercise compose(), not just
    # construction, so a broken None branch would be caught.
    from textual.widgets import Static

    d = ApprovalDialog(issue_label="#9", estimate_usd=None, basis="")
    widgets = list(d.compose())
    body = next(
        w
        for w in widgets
        if isinstance(w, Static) and getattr(w, "id", "") == "approval-dialog-body"
    )
    assert "unknown" in str(body.render())
