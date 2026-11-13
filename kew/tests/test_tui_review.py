"""Tests for TUI PR review action handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kew.review_checkout import ReviewCheckout


def _fake_checkout(path: Path) -> ReviewCheckout:
    """A materialized review checkout, as materialize_review_checkout returns."""
    return ReviewCheckout(
        path=path,
        head_sha="a" * 40,
        branch="agent/issue-42",
        detached=False,
        impl_worktree=None,
        conflict=None,
    )


class TestHandleReview:
    def test_no_selection_is_noop(self):
        """handle_review does nothing when no PR is selected in the reviews panel."""
        from kew.tui.actions import handle_review

        app = MagicMock()
        with patch("kew.tui.actions.get_selected_pr", return_value=None):
            handle_review(app)
        app.push_screen.assert_not_called()

    def test_with_selection_pushes_confirm_dialog(self):
        """handle_review pushes a ConfirmDialog when a PR is selected."""
        from kew.tui.actions import handle_review

        app = MagicMock()
        app._store = None
        app.state.review_prs = [{"number": 42, "title": "Fix widget"}]
        with patch("kew.tui.actions.get_selected_pr", return_value=42):
            handle_review(app)
        app.push_screen.assert_called_once()
        dialog = app.push_screen.call_args[0][0]
        assert "42" in dialog.prompt
        assert "Fix widget" in dialog.prompt

    def test_pr_not_in_state_is_noop(self):
        """handle_review does nothing if selected PR number not found in state."""
        from kew.tui.actions import handle_review

        app = MagicMock()
        app.state.review_prs = []
        with patch("kew.tui.actions.get_selected_pr", return_value=99):
            handle_review(app)
        app.push_screen.assert_not_called()

    def test_on_confirm_runs_worker(self):
        """Confirming the dialog runs a background worker."""
        from kew.tui.actions import handle_review

        app = MagicMock()
        app._store = None
        app.state.review_prs = [{"number": 42, "title": "Fix widget"}]
        with patch("kew.tui.actions.get_selected_pr", return_value=42):
            handle_review(app)

        confirm_callback = app.push_screen.call_args[1]["callback"]
        confirm_callback(True)

        app.run_worker.assert_called_once()

    def test_on_deny_does_not_run_worker(self):
        """Denying confirmation does not dispatch."""
        from kew.tui.actions import handle_review

        app = MagicMock()
        app._store = None
        app.state.review_prs = [{"number": 42, "title": "Fix widget"}]
        with patch("kew.tui.actions.get_selected_pr", return_value=42):
            handle_review(app)

        confirm_callback = app.push_screen.call_args[1]["callback"]
        confirm_callback(False)

        app.run_worker.assert_not_called()


class TestDoReview:
    @pytest.mark.asyncio
    async def test_calls_dispatch_review(self):
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=0),
        ) as mock_dr:
            await _do_review(app, 42)

        mock_dr.assert_called_once()
        call_kwargs = mock_dr.call_args[1]
        assert call_kwargs["pr_number"] == 42
        assert call_kwargs["config"] is app.config
        # store is now passed through so run_tracked_agent handles tracking
        assert call_kwargs["store"] is app._store
        assert app.notify.call_args.args[0] == "Review PR #42 finished"

    @pytest.mark.asyncio
    async def test_creates_run_record(self):
        """RunRecord lifecycle is now delegated to run_tracked_agent via store."""
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=0),
        ):
            await _do_review(app, 42)

        # _do_review itself no longer saves RunRecords directly;
        # run_tracked_agent (called inside dispatch_review) handles that.
        # Verify dispatch_review was called with the store.
        # (No direct store.save calls from _do_review)
        app._store.save.assert_not_called()
        assert app.notify.call_args.args[0] == "Review PR #42 finished"

    @pytest.mark.asyncio
    async def test_updates_record_on_success(self):
        """On success, _do_review notifies user; run_tracked_agent updates store."""
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=0),
        ):
            await _do_review(app, 42)

        app.notify.assert_called_once()
        msg = app.notify.call_args[0][0]
        assert "42" in msg
        # Confirm no "error" severity on success
        assert app.notify.call_args[1].get("severity") != "error"
        assert app.notify.call_args.args[0] == "Review PR #42 finished"

    @pytest.mark.asyncio
    async def test_updates_record_on_failure(self):
        """On failure, _do_review notifies with error severity.

        Store lifecycle is handled by run_tracked_agent.
        """
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=1),
        ):
            await _do_review(app, 42)

        app.notify.assert_called_once()
        assert app.notify.call_args[1].get("severity") == "error"
        assert app.notify.call_args.args[0] == "Review PR #42 failed (exit 1)"

    @pytest.mark.asyncio
    async def test_notifies_on_success(self):
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=0),
        ):
            await _do_review(app, 42)

        app.notify.assert_called_once()
        msg = app.notify.call_args[0][0]
        assert "42" in msg
        assert app.notify.call_args.args[0] == "Review PR #42 finished"

    @pytest.mark.asyncio
    async def test_notifies_on_exception(self):
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = MagicMock()

        with patch(
            "kew.review.dispatch_review",
            side_effect=RuntimeError("boom"),
        ):
            await _do_review(app, 42)

        app.notify.assert_called_once()
        assert app.notify.call_args[1]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_skips_store_when_none(self):
        from kew.tui.actions import _do_review

        app = MagicMock()
        app.config = MagicMock()
        app._store = None

        with patch(
            "kew.review.dispatch_review",
            return_value=SimpleNamespace(kind="executed", exit_code=0),
        ):
            await _do_review(app, 42)

        # Should not crash, and should notify success
        app.notify.assert_called_once()
        assert app.notify.call_args.args[0] == "Review PR #42 finished"


class TestDispatchReviewEventParams:
    """dispatch_review passes store to run_tracked_agent for tracking."""

    def test_passes_store_to_run_tracked_agent(self):
        from pathlib import Path

        from kew.config import InferenceTransportConfig
        from kew.review import dispatch_review

        config = MagicMock()
        config.inference_transport = InferenceTransportConfig()
        config.project.repo = "owner/repo"
        config.review.include_resolved = False
        config.review.auto_reply = False
        config.branches.base = "main"
        config.publication.publish = "canonical"
        config.commands.ci = ""
        config.commands.format = ""
        # Real numbers, not a bare mock: the budget watchdog COMPARES this, so
        # a mock raises TypeError. Same family as #1464 — a mock reaching code
        # that treats it as a value is not inert.
        config.agent.max_budget_usd = 25.0
        config.review.max_remediation_usd = 40.0
        store = MagicMock()
        store.find_run_by_branch.return_value = None
        store.publication_jobs_for_head.return_value = []
        store.normalized_jobs_for_head.return_value = []
        store.list_runs_by_branch_after.return_value = ([], None)

        with (
            patch("kew.review._get_pr_metadata") as mock_meta,
            patch("kew.review.fetch_review_comments") as mock_fetch,
            patch("kew.review.materialize_review_checkout") as mock_checkout,
            patch("kew.review.resolve_runner") as mock_resolve,
            patch("kew.review.run_tracked_agent") as mock_tracked,
            patch("kew.review.discard_checkout"),
            patch("kew.review.detect_base_branch", return_value="main"),
            patch("kew.review._check_github_connectivity", return_value=True),
            patch("kew.review.check_budget"),
        ):
            from kew.tracking import DispatchResult

            mock_meta.return_value = {
                "headRefName": "fix/branch",
                "baseRefName": "main",
                "body": "Closes #99",
                "title": "Fix thing",
            }
            mock_fetch.return_value = [MagicMock(path="f.py", body="fix this")]
            mock_checkout.return_value = _fake_checkout(Path("/tmp/wt"))
            runner = MagicMock()
            mock_resolve.return_value = MagicMock(runner=runner, model="opus")
            mock_tracked.return_value = DispatchResult(
                exit_code=0, issue_num=99, log_file=Path("/tmp/log")
            )

            dispatch_review(
                pr_number=10,
                config=config,
                store=store,
            )

        mock_tracked.assert_called_once()
        call_kwargs = mock_tracked.call_args[1]
        assert call_kwargs["store"] is store


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["review", "resolve"])
@pytest.mark.parametrize("kind,raw_code", [("not_run", 1), ("executed", 127)])
async def test_remediation_notification_keeps_execution_state(command, kind, raw_code):
    from kew.tui import actions

    app = MagicMock()
    with patch(
        f"kew.{command}.dispatch_{command}",
        return_value=SimpleNamespace(kind=kind, exit_code=raw_code),
    ):
        await getattr(actions, f"_do_{command}")(app, 42)
    expected = "could not run" if kind == "not_run" else "failed (exit 127)"
    assert app.notify.call_args.args[0] == f"{command.title()} PR #42 {expected}"
    assert app.notify.call_args.kwargs["severity"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["review", "resolve"])
async def test_round_four_transport_refusal_reaches_cli_and_tui(
    command, tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    from kew.cli import app as cli_app
    from kew.config import DispatchConfig
    from kew.inference_transport import TransportAdmissionError
    from kew.review import ReviewComment
    from kew.tui import actions
    from kew.worktree import ConflictInfo

    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    metadata = {
        "headRefName": "agent/42",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "mergeable": "CONFLICTING",
        "title": "fix",
    }
    prefix = f"kew.{command}"
    monkeypatch.setattr(f"{prefix}._get_pr_metadata", lambda *args, **kwargs: metadata)
    monkeypatch.setattr(f"{prefix}._check_github_connectivity", lambda config: True)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure", lambda *a, **k: None
    )
    monkeypatch.setattr(
        f"{prefix}.resolve_remediation_containment",
        lambda *a, **k: SimpleNamespace(
            contained=False, posture="uncontained_flow", image=None, reason="test"
        ),
    )
    monkeypatch.setattr(f"{prefix}.attest_flow_posture", lambda *a, **k: None)
    monkeypatch.setattr(
        f"{prefix}.ensure_publication_credentials", lambda *a, **k: None
    )
    if command == "review":
        comment = ReviewComment(
            id=1,
            author="reviewer",
            body="fix",
            path="f.py",
            line=1,
            state="CHANGES_REQUESTED",
            in_reply_to=None,
            resolved=False,
        )
        monkeypatch.setattr(
            "kew.review.fetch_review_comments", lambda *a, **k: [comment]
        )
        monkeypatch.setattr(
            "kew.review.materialize_review_checkout",
            lambda *a, **k: _fake_checkout(tmp_path),
        )
        monkeypatch.setattr("kew.review.record_review_outcome", lambda *a, **k: None)
    else:
        monkeypatch.setattr(
            "kew.resolve.checkout_existing_worktree",
            lambda *a, **k: (tmp_path, ConflictInfo("merge", ["f.py"], "main")),
        )
        # Main also validates the pinned head before transport admission.
        # Match test_resolve's seam: this fake checkout has no Git repository.
        monkeypatch.setattr("kew.resolve.require_pinned_pr_head", lambda *a: None)
    launch = MagicMock(
        side_effect=TransportAdmissionError("transport refused before retry")
    )
    monkeypatch.setattr(f"{prefix}.run_tracked_agent", launch)
    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: config)
    monkeypatch.setattr("kew.cli._preflight", lambda config: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda config: None)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda config: None)
    store = MagicMock()
    store.__enter__.return_value = None
    monkeypatch.setattr("kew.cli._open_run_store", lambda *a, **k: store)
    result = CliRunner().invoke(cli_app, [command, "42"])
    assert result.exit_code == 2, result.output
    assert "transport refused before retry" in result.output
    assert launch.call_count == 1
    app = MagicMock()
    app.config = config
    app._store = None
    await getattr(actions, f"_do_{command}")(app, 42)
    assert app.notify.call_args.args[0] == f"{command.title()} PR #42 could not run"
    assert launch.call_count == 2


@pytest.mark.parametrize("verb", ["review", "resolve"])
@pytest.mark.asyncio
async def test_round_ten_tui_zero_exit_refusal(verb):
    from kew.command_outcome import CommandOutcome
    from kew.tui import actions

    app = MagicMock()
    with patch(
        f"kew.{verb}.dispatch_{verb}", return_value=CommandOutcome("not_run", 0)
    ):
        await getattr(actions, f"_do_{verb}")(app, 42)
    notices = [str(call) for call in app.notify.call_args_list]
    assert any("could not run" in message for message in notices)
    assert not any("finished" in message for message in notices)


@pytest.mark.parametrize("verb", ["review", "resolve"])
@pytest.mark.asyncio
async def test_round_twelve_tui_retains_retry_refusal_reason(verb):
    from types import SimpleNamespace

    from kew.tui import actions

    reason = "ERROR: retry wrapper exited without its startup acknowledgement"
    outcome = SimpleNamespace(kind="executed", exit_code=1, reason=reason)
    app = MagicMock()
    with patch(f"kew.{verb}.dispatch_{verb}", return_value=outcome):
        await getattr(actions, f"_do_{verb}")(app, 42)
    notice = app.notify.call_args
    assert reason in notice.args[0]
    assert "failed" in notice.args[0]
    assert "finished" not in notice.args[0]
    assert notice.kwargs["severity"] == "error"
