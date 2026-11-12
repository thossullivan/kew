"""Tests for label state transitions."""

import json
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig, LabelConfig
from kew.labels import (
    ClaimResult,
    SetupLabelsError,
    get_ci_fix_attempt,
    mark_completed,
    mark_completed_no_changes,
    mark_failed,
    mark_in_progress,
    mark_no_change_candidate,
    mark_ready,
    mark_ready_from_failed,
    setup_labels,
    try_claim_issue,
    unmark_ready,
    update_pr_labels,
)


def _make_config(
    ready="ready",
    in_progress="in-progress",
    agent_working="agent-working",
    repo="owner/repo",
):
    """Build a minimal DispatchConfig mock for label tests."""
    config = MagicMock()
    config.labels.ready = ready
    config.labels.in_progress = in_progress
    config.labels.agent_working = agent_working
    config.project.repo = repo
    return config


def _gh_label_response(label_names: list[str]):
    """Build a mock CompletedProcess mimicking `gh issue view --json labels`."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = json.dumps({"labels": [{"name": n} for n in label_names]})
    return result


class TestMarkInProgress:
    @patch("kew.labels.run")
    def test_adds_and_removes_correct_labels(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_in_progress(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "in-progress" in cmd
        assert "agent-working" in cmd
        assert "--remove-label" in cmd
        assert "ready" in cmd


class TestMarkCompleted:
    @patch("kew.labels.run")
    def test_adds_pr_pending_review(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed(42, config)
        cmd = mock_run.call_args[0][0]
        assert "pr-pending-review" in cmd

    @patch("kew.labels.run")
    def test_removes_ready_label(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd
        assert "ready" in cmd


class TestMarkFailed:
    @patch("kew.labels.run")
    def test_adds_agent_failed_and_comments(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_failed(42, config, "timeout after 120m")
        # Should make 2 calls: label update + comment
        assert mock_run.call_count == 2

    @patch("kew.labels.run")
    def test_removes_ready_label(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_failed(42, config, "timeout")
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "ready" in label_cmd


class TestMarkCompletedNoChanges:
    @patch("kew.labels.run")
    def test_removes_ready_label(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed_no_changes(42, config, "Done.")
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "ready" in label_cmd

    @patch("kew.labels.run")
    def test_adds_completed_no_changes_label(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed_no_changes(42, config, "All checks passed, no changes needed.")
        # First call: label update, second call: comment, third call: close issue
        assert mock_run.call_count == 3
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "agent-completed-no-changes" in label_cmd
        assert "--add-label" in label_cmd

    @patch("kew.labels.run")
    def test_removes_in_progress_labels(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed_no_changes(42, config, "Done.")
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "--remove-label" in label_cmd
        assert "in-progress" in label_cmd
        assert "agent-working" in label_cmd

    @patch("kew.labels.run")
    def test_posts_comment(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed_no_changes(42, config, "Verification passed.")
        comment_cmd = mock_run.call_args_list[1][0][0]
        assert comment_cmd[1] == "issue"
        assert comment_cmd[2] == "comment"
        assert "Verification passed." in comment_cmd[-1]

    @patch("kew.labels.run")
    def test_closes_issue(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_completed_no_changes(42, config, "Done.")
        close_cmd = mock_run.call_args_list[2][0][0]
        assert close_cmd[1] == "issue"
        assert close_cmd[2] == "close"

    @patch("kew.labels.run")
    def test_uses_custom_label_name(self, mock_run: MagicMock):
        config = DispatchConfig(labels=LabelConfig(completed_no_changes="done-no-code"))
        mark_completed_no_changes(42, config, "Done.")
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "done-no-code" in label_cmd


class TestMarkNoChangeCandidate:
    """The claim release a zero-diff completion owes the item (#1611).

    ``mark_completed_no_changes`` decides the outcome — it comments and closes.
    This one only hands the item back: the candidate it labels is still waiting
    on a human confirm/reject, so the issue must stay open and uncommented.
    """

    @patch("kew.labels.run")
    def test_clears_the_dispatch_claim_and_labels_the_candidate(
        self, mock_run: MagicMock
    ):
        config = DispatchConfig()
        mark_no_change_candidate(42, config)
        cmd = mock_run.call_args_list[0][0][0]
        removed = {cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--remove-label"}
        added = {cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-label"}
        assert removed == {"in-progress", "agent-working", "ready"}
        assert added == {"agent-completed-no-changes"}

    @patch("kew.labels.run")
    def test_neither_comments_nor_closes(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_no_change_candidate(42, config)
        assert mock_run.call_count == 1
        assert mock_run.call_args_list[0][0][0][2] == "edit"

    @patch("kew.labels.run")
    def test_uses_custom_label_name(self, mock_run: MagicMock):
        config = DispatchConfig(labels=LabelConfig(completed_no_changes="done-no-code"))
        mark_no_change_candidate(42, config)
        assert "done-no-code" in mock_run.call_args_list[0][0][0]


class TestMarkFailedExtraLabels:
    @patch("kew.labels.run")
    def test_extra_labels_added_on_failure(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_failed(42, config, "budget exceeded", extra_labels=["budget-exceeded"])
        # First call is label update
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "budget-exceeded" in label_cmd
        assert "agent-failed" in label_cmd

    @patch("kew.labels.run")
    def test_no_extra_labels_by_default(self, mock_run: MagicMock):
        config = DispatchConfig()
        mark_failed(42, config, "exit code 1")
        label_cmd = mock_run.call_args_list[0][0][0]
        assert "agent-failed" in label_cmd
        assert "budget-exceeded" not in label_cmd


class TestCustomLabels:
    @patch("kew.labels.run")
    def test_uses_custom_label_names(self, mock_run: MagicMock):
        config = DispatchConfig(
            labels=LabelConfig(
                ready="queued",
                in_progress="working",
                agent_working="bot-active",
            )
        )
        mark_in_progress(42, config)
        cmd = mock_run.call_args[0][0]
        assert "working" in cmd
        assert "bot-active" in cmd
        assert "queued" in cmd


class TestSetupLabelsNew:
    @patch("kew.labels.run")
    def test_creates_new_labels(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        config = DispatchConfig()
        setup_labels(config)
        # Collect all label create calls — label name is at index 3:
        # ["gh", "label", "create", <name>, "--color", ...]
        label_names = [
            call.args[0][3]
            for call in mock_run.call_args_list
            if len(call.args[0]) > 3
            and call.args[0][1] == "label"
            and call.args[0][2] == "create"
        ]
        assert "agent-needs-help" in label_names
        assert "budget-exceeded" in label_names
        assert "large-pr" in label_names
        assert "ready-to-merge" in label_names
        assert "agent-completed-no-changes" in label_names

    @patch("kew.labels.run")
    def test_new_labels_use_configured_names(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        config = DispatchConfig(labels=LabelConfig(agent_needs_help="needs-human"))
        setup_labels(config)
        label_names = [
            call.args[0][3]
            for call in mock_run.call_args_list
            if len(call.args[0]) > 3
            and call.args[0][1] == "label"
            and call.args[0][2] == "create"
        ]
        assert "needs-human" in label_names
        assert "agent-needs-help" not in label_names


class TestUpdatePrLabels:
    @patch("kew.labels.run")
    def test_constructs_gh_pr_edit_command(self, mock_run: MagicMock):
        config = DispatchConfig()
        update_pr_labels(7, config, add=["ci-fix-attempt-1"], remove=["ci-passing"])
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["gh", "pr", "edit"]
        assert "7" in cmd
        mock_run.assert_called_once_with(cmd, check=False, capture=True)

    @patch("kew.labels.run")
    def test_add_only(self, mock_run: MagicMock):
        config = DispatchConfig()
        update_pr_labels(10, config, add=["ci-fix-attempt-2"])
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "ci-fix-attempt-2" in cmd
        assert "--remove-label" not in cmd

    @patch("kew.labels.run")
    def test_remove_only(self, mock_run: MagicMock):
        config = DispatchConfig()
        update_pr_labels(10, config, remove=["stale"])
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd
        assert "stale" in cmd
        assert "--add-label" not in cmd

    @patch("kew.labels.run")
    def test_uses_repo_from_config(self, mock_run: MagicMock):
        config = DispatchConfig()
        config.project.repo = "org/myrepo"
        update_pr_labels(5, config, add=["x"])
        cmd = mock_run.call_args[0][0]
        assert "--repo" in cmd
        repo_idx = cmd.index("--repo")
        assert cmd[repo_idx + 1] == "org/myrepo"

    @patch("kew.labels.run")
    def test_returns_true_on_success(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0)
        config = DispatchConfig()
        assert update_pr_labels(5, config, add=["x"]) is True

    @patch("kew.labels.run")
    def test_returns_false_on_failure(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1)
        config = DispatchConfig()
        assert update_pr_labels(5, config, add=["x"]) is False


class TestTryClaimIssue:
    """try_claim_issue guards against duplicate dispatch."""

    @patch("kew.labels.run")
    def test_claims_issue_when_ready(self, mock_run: MagicMock):
        config = DispatchConfig()
        view_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "ready"}, {"name": "bug"}]}),
        )
        edit_result = MagicMock(returncode=0)
        verify_result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"labels": [{"name": "in-progress"}, {"name": "agent-working"}]}
            ),
        )
        mock_run.side_effect = [view_result, edit_result, verify_result]

        assert try_claim_issue(42, config) is ClaimResult.CLAIMED
        assert mock_run.call_count == 3

    @patch("kew.labels.run")
    def test_skips_when_ready_label_missing(self, mock_run: MagicMock):
        config = DispatchConfig()
        view_result = MagicMock(
            returncode=0,
            # genuinely not-ready: a non-lifecycle label, no ready, no in-progress
            stdout=json.dumps({"labels": [{"name": "enhancement"}]}),
        )
        mock_run.return_value = view_result

        assert try_claim_issue(42, config) is ClaimResult.NOT_READY
        assert mock_run.call_count == 1

    @patch("kew.labels.run")
    def test_skips_when_already_in_progress(self, mock_run: MagicMock):
        config = DispatchConfig()
        view_result = MagicMock(
            returncode=0,
            # realistic claimed state: in-progress/agent-working, `ready` removed
            stdout=json.dumps(
                {"labels": [{"name": "in-progress"}, {"name": "agent-working"}]}
            ),
        )
        mock_run.return_value = view_result

        assert try_claim_issue(42, config) is ClaimResult.ALREADY_CLAIMED
        assert mock_run.call_count == 1

    @patch("kew.labels.mark_in_progress", side_effect=RuntimeError("gh edit failed"))
    @patch("kew.labels.run")
    def test_fetch_failed_when_mark_in_progress_raises(
        self, mock_run: MagicMock, _mark: MagicMock
    ):
        config = DispatchConfig()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "ready"}]}),
        )
        # a failed claim-label edit must NOT propagate — try_claim_issue stays
        # a pure enum guard and returns FETCH_FAILED
        assert try_claim_issue(42, config) is ClaimResult.FETCH_FAILED

    @patch("kew.labels.run")
    def test_skips_when_agent_working(self, mock_run: MagicMock):
        config = DispatchConfig()
        view_result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"labels": [{"name": "ready"}, {"name": "agent-working"}]}
            ),
        )
        mock_run.return_value = view_result

        assert try_claim_issue(42, config) is ClaimResult.ALREADY_CLAIMED
        assert mock_run.call_count == 1

    @patch("kew.labels.run")
    def test_skips_on_gh_failure(self, mock_run: MagicMock):
        config = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        assert try_claim_issue(42, config) is ClaimResult.FETCH_FAILED

    @patch("kew.labels.run")
    def test_malformed_json_returns_false(self, mock_run: MagicMock):
        """Malformed JSON from gh returns FETCH_FAILED instead of crashing."""
        config = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=0, stdout="not valid json{{{")

        assert try_claim_issue(42, config) is ClaimResult.FETCH_FAILED

    @patch("kew.labels.run")
    def test_non_dict_json_returns_false(self, mock_run: MagicMock):
        """Valid JSON that is not a dict (e.g. list) returns FETCH_FAILED."""
        config = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")

        assert try_claim_issue(42, config) is ClaimResult.FETCH_FAILED

    @patch("kew.labels.run")
    def test_uses_custom_label_names(self, mock_run: MagicMock):
        config = DispatchConfig(
            labels=LabelConfig(
                ready="queued",
                in_progress="working",
                agent_working="bot-active",
            )
        )
        view_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"labels": [{"name": "queued"}]}),
        )
        edit_result = MagicMock(returncode=0)
        verify_result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"labels": [{"name": "working"}, {"name": "bot-active"}]}
            ),
        )
        mock_run.side_effect = [view_result, edit_result, verify_result]

        assert try_claim_issue(42, config) is ClaimResult.CLAIMED


class TestMarkReady:
    """Tests for mark_ready() label addition."""

    @patch("kew.labels.run")
    def test_adds_ready_label(self, mock_run: MagicMock) -> None:
        """mark_ready() calls gh issue edit with --add-label ready."""
        config = DispatchConfig()
        mark_ready(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "ready" in cmd
        assert "42" in cmd

    @patch("kew.labels.run")
    def test_adds_custom_ready_label(self, mock_run: MagicMock) -> None:
        """mark_ready() uses the configured label name."""
        config = DispatchConfig(labels=LabelConfig(ready="queued"))
        mark_ready(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "queued" in cmd

    @patch("kew.labels.run")
    def test_raises_on_failure(self, mock_run: MagicMock) -> None:
        """mark_ready() raises RuntimeError when gh fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        config = DispatchConfig()
        with pytest.raises(RuntimeError, match="gh issue edit failed"):
            mark_ready(42, config)


class TestUnmarkReady:
    """Tests for unmark_ready() label removal."""

    @patch("kew.labels.run")
    def test_removes_ready_label(self, mock_run: MagicMock) -> None:
        """unmark_ready() calls gh issue edit with --remove-label ready."""
        config = DispatchConfig()
        unmark_ready(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd
        assert "ready" in cmd
        assert "42" in cmd

    @patch("kew.labels.run")
    def test_removes_custom_ready_label(self, mock_run: MagicMock) -> None:
        """unmark_ready() uses the configured label name."""
        config = DispatchConfig(labels=LabelConfig(ready="queued"))
        unmark_ready(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--remove-label" in cmd
        assert "queued" in cmd


class TestGetCiFixAttempt:
    def test_no_labels_returns_zero(self):
        assert get_ci_fix_attempt([]) == 0

    def test_no_matching_labels_returns_zero(self):
        assert get_ci_fix_attempt(["bug", "enhancement", "ready"]) == 0

    def test_single_attempt_label(self):
        assert get_ci_fix_attempt(["ci-fix-attempt-1"]) == 1

    def test_returns_highest_attempt(self):
        labels = ["ci-fix-attempt-1", "ci-fix-attempt-3", "ci-fix-attempt-2"]
        assert get_ci_fix_attempt(labels) == 3

    def test_ignores_malformed_labels(self):
        labels = [
            "ci-fix-attempt-",
            "ci-fix-attempt-abc",
            "ci-fix-attempt-2",
            "ci-fix-attempt",
            "ci-fix-attempt-0",
        ]
        assert get_ci_fix_attempt(labels) == 2

    def test_mixed_valid_and_other_labels(self):
        labels = ["bug", "ci-fix-attempt-5", "ready", "ci-fix-attempt-2"]
        assert get_ci_fix_attempt(labels) == 5


class TestMarkReadyFromFailed:
    """Tests for mark_ready_from_failed() label transition."""

    @patch("kew.labels.run")
    def test_adds_ready_removes_failed_and_lifecycle_labels(
        self, mock_run: MagicMock
    ) -> None:
        """mark_ready_from_failed() adds ready, removes failed labels."""
        config = DispatchConfig()
        mark_ready_from_failed(42, config)
        cmd = mock_run.call_args[0][0]
        assert "--add-label" in cmd
        assert "ready" in cmd
        assert "--remove-label" in cmd
        assert "agent-failed" in cmd
        assert "budget-exceeded" in cmd
        assert "agent-needs-help" in cmd

    @patch("kew.labels.run")
    def test_uses_custom_label_names(self, mock_run: MagicMock) -> None:
        """mark_ready_from_failed() uses configured label names."""
        config = DispatchConfig(
            labels=LabelConfig(ready="queued", agent_failed="bot-failed")
        )
        mark_ready_from_failed(42, config)
        cmd = mock_run.call_args[0][0]
        assert "queued" in cmd
        assert "bot-failed" in cmd

    @patch("kew.labels.run")
    def test_raises_on_failure(self, mock_run: MagicMock) -> None:
        """mark_ready_from_failed() raises RuntimeError when gh fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        config = DispatchConfig()
        with pytest.raises(RuntimeError, match="gh issue edit failed"):
            mark_ready_from_failed(42, config)


class TestTryClaimIssueVerification:
    """Post-claim verification re-fetches labels to detect races."""

    @patch("kew.labels.run")
    @patch("kew.labels._update_labels")
    def test_successful_claim_with_verification(self, mock_update, mock_run):
        """Happy path: ready on first fetch, in-progress on verification fetch."""
        config = _make_config()
        mock_run.side_effect = [
            _gh_label_response(["ready"]),
            _gh_label_response(["in-progress", "agent-working"]),
        ]
        assert try_claim_issue(1, config) is ClaimResult.CLAIMED
        # Must have called run() twice: pre-claim + post-claim verification
        assert mock_run.call_count == 2

    @patch("kew.labels.run")
    @patch("kew.labels._update_labels")
    def test_backs_off_when_another_process_claimed(self, mock_update, mock_run):
        """Race lost: 'ready' still present on verification (our mark didn't stick)."""
        config = _make_config()
        mock_run.side_effect = [
            _gh_label_response(["ready"]),
            _gh_label_response(["ready"]),
        ]
        assert try_claim_issue(1, config) is ClaimResult.RACE_DETECTED

    @patch("kew.labels.run")
    @patch("kew.labels._update_labels")
    def test_backs_off_when_verification_fetch_fails(self, mock_update, mock_run):
        """If the verification gh call fails, back off (can't confirm claim)."""
        config = _make_config()
        failed = MagicMock()
        failed.returncode = 1
        mock_run.side_effect = [
            _gh_label_response(["ready"]),
            failed,
        ]
        assert try_claim_issue(1, config) is ClaimResult.FETCH_FAILED


class TestLabelTransitionsRetryOnRateLimit:
    """Label transitions retry on GitHub rate limit errors."""

    @patch("kew.labels.time")
    @patch("kew.labels.run")
    def test_update_labels_retries_on_rate_limit(
        self, mock_run: MagicMock, mock_time: MagicMock
    ):
        config = DispatchConfig()
        rate_limited = MagicMock(
            returncode=1, stderr="API rate limit exceeded", stdout=""
        )
        success = MagicMock(returncode=0)
        mock_run.side_effect = [rate_limited, success]

        mark_in_progress(42, config)

        assert mock_run.call_count == 2
        mock_time.sleep.assert_called_once()

    @patch("kew.labels.time")
    @patch("kew.labels.run")
    def test_fetch_issue_labels_retries_on_rate_limit(
        self, mock_run: MagicMock, mock_time: MagicMock
    ):
        config = DispatchConfig()
        rate_limited = MagicMock(returncode=1, stderr="rate limit exceeded", stdout="")
        success = MagicMock(returncode=0, stdout='{"labels": [{"name": "ready"}]}')
        mock_run.side_effect = [rate_limited, success]

        from kew.labels import _fetch_issue_labels

        result = _fetch_issue_labels(42, config)

        assert result == {"ready"}
        assert mock_run.call_count == 2
        mock_time.sleep.assert_called_once()


class TestResetIssueToReady:
    """reset_issue_to_ready: killed/in-progress -> ready, idempotently."""

    def _label_fetch_result(self, names: list[str]) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps({"labels": [{"name": n} for n in names]})
        result.stderr = ""
        return result

    @patch("kew.labels.run")
    def test_removes_in_progress_and_adds_ready(self, mock_run):
        from kew.labels import reset_issue_to_ready

        config = DispatchConfig()
        config.project.repo = "owner/repo"
        edit_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [
            self._label_fetch_result(["in-progress", "agent-working"]),
            edit_result,
        ]

        reset_issue_to_ready(42, config)

        edit_cmd = mock_run.call_args_list[1].args[0]
        assert "--add-label" in edit_cmd
        assert "ready" in edit_cmd
        assert "in-progress" in edit_cmd
        assert "agent-working" in edit_cmd

    @patch("kew.labels.run")
    def test_idempotent_when_already_ready(self, mock_run):
        from kew.labels import reset_issue_to_ready

        config = DispatchConfig()
        config.project.repo = "owner/repo"
        mock_run.side_effect = [self._label_fetch_result(["ready"])]

        reset_issue_to_ready(42, config)

        assert mock_run.call_count == 1

    @patch("kew.labels.run")
    def test_only_removes_labels_actually_present(self, mock_run):
        from kew.labels import reset_issue_to_ready

        config = DispatchConfig()
        config.project.repo = "owner/repo"
        edit_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [
            self._label_fetch_result(["in-progress"]),  # no agent-working
            edit_result,
        ]

        reset_issue_to_ready(42, config)

        edit_cmd = mock_run.call_args_list[1].args[0]
        assert "in-progress" in edit_cmd
        assert "agent-working" not in edit_cmd

    @patch("kew.labels.run")
    def test_raises_when_label_fetch_fails(self, mock_run):
        from kew.labels import reset_issue_to_ready

        config = DispatchConfig()
        config.project.repo = "owner/repo"
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

        with pytest.raises(RuntimeError):
            reset_issue_to_ready(42, config)


class TestMarkNeedsApproval:
    """Approval hold: in-progress -> needs-approval."""

    @patch("kew.labels.run")
    def test_swaps_lifecycle_labels_for_approval_label(self, mock_run: MagicMock):
        from kew.labels import mark_needs_approval

        config = DispatchConfig()
        mark_needs_approval(42, config)
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["gh", "issue", "edit"]
        add_idx = cmd.index("--add-label")
        assert cmd[add_idx + 1] == "needs-approval"
        assert "--remove-label" in cmd
        assert "in-progress" in cmd
        assert "agent-working" in cmd
        assert "ready" in cmd

    @patch("kew.labels.run")
    def test_uses_configured_label(self, mock_run: MagicMock):
        from kew.labels import mark_needs_approval

        config = DispatchConfig()
        config.approval.label = "hold-for-human"
        mark_needs_approval(42, config)
        cmd = mock_run.call_args[0][0]
        assert "hold-for-human" in cmd
        assert "needs-approval" not in cmd


class TestMarkReadyFromApproval:
    """Interactive decline: in-progress -> ready."""

    @patch("kew.labels.run")
    def test_restores_ready_and_removes_in_progress(self, mock_run: MagicMock):
        from kew.labels import mark_ready_from_approval

        config = DispatchConfig()
        mark_ready_from_approval(42, config)
        cmd = mock_run.call_args[0][0]
        add_idx = cmd.index("--add-label")
        assert cmd[add_idx + 1] == "ready"
        assert "in-progress" in cmd
        assert "agent-working" in cmd
        assert "needs-approval" not in cmd


class TestReleaseApprovalHold:
    """--approve release: needs-approval -> ready."""

    @patch("kew.labels.run")
    def test_swaps_approval_label_for_ready(self, mock_run: MagicMock):
        from kew.labels import release_approval_hold

        config = DispatchConfig()
        release_approval_hold(42, config)
        cmd = mock_run.call_args[0][0]
        add_idx = cmd.index("--add-label")
        assert cmd[add_idx + 1] == "ready"
        remove_idx = cmd.index("--remove-label")
        assert cmd[remove_idx + 1] == "needs-approval"


class TestSetupLabelsApproval:
    @patch("kew.labels.run")
    def test_setup_creates_needs_approval_label(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0)
        config = DispatchConfig()
        setup_labels(config)
        label_names = [
            call.args[0][3]
            for call in mock_run.call_args_list
            if len(call.args[0]) > 3
            and call.args[0][1] == "label"
            and call.args[0][2] == "create"
        ]
        assert "needs-approval" in label_names


class TestSetupLabelsIncompleteRun:
    def test_setup_labels_registers_incomplete_run(self, monkeypatch):
        from kew import labels as labels_mod
        from kew.config import DispatchConfig

        created: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            # gh label create <name> ... — capture the label name argument
            if "label" in cmd and "create" in cmd:
                created.append(cmd[cmd.index("create") + 1])
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(labels_mod, "run", fake_run)
        labels_mod.setup_labels(DispatchConfig())
        assert "incomplete-run" in created


class TestSetupLabelsRepoFailure:
    """setup_labels must raise SetupLabelsError when the repo is inaccessible."""

    @patch("kew.labels.run")
    def test_raises_on_repo_404(self, mock_run: MagicMock):
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "HTTP 404: Not Found"
        fake.stdout = ""
        mock_run.return_value = fake
        config = DispatchConfig()
        config.project.repo = "owner/name"
        with pytest.raises(SetupLabelsError, match="owner/name"):
            setup_labels(config)

    @patch("kew.labels.run")
    def test_raises_on_auth_failure(self, mock_run: MagicMock):
        fake = MagicMock()
        fake.returncode = 4
        fake.stderr = "authentication required"
        fake.stdout = ""
        mock_run.return_value = fake
        config = DispatchConfig()
        config.project.repo = "owner/private-repo"
        with pytest.raises(SetupLabelsError):
            setup_labels(config)

    @patch("kew.labels.run")
    def test_returns_counts_on_success(self, mock_run: MagicMock):
        """All labels created fresh → created count > 0, failed = 0."""

        def side_effect(cmd, **kwargs):
            fake = MagicMock()
            if cmd[1] == "repo" and cmd[2] == "view":
                fake.returncode = 0
                fake.stdout = '{"name": "repo"}'
                fake.stderr = ""
            else:
                fake.returncode = 0
                fake.stderr = ""
                fake.stdout = ""
            return fake

        mock_run.side_effect = side_effect
        config = DispatchConfig()
        result = setup_labels(config)
        assert result.created > 0
        assert result.failed == 0

    @patch("kew.labels.run")
    def test_counts_updated_when_label_exists(self, mock_run: MagicMock):
        """Label already exists → edit succeeds → counted as updated, not failed."""

        def side_effect(cmd, **kwargs):
            fake = MagicMock()
            if cmd[1] == "repo" and cmd[2] == "view":
                fake.returncode = 0
                fake.stdout = '{"name": "repo"}'
                fake.stderr = ""
            elif cmd[1] == "label" and cmd[2] == "create":
                fake.returncode = 1
                fake.stderr = "already exists"
                fake.stdout = ""
            elif cmd[1] == "label" and cmd[2] == "edit":
                fake.returncode = 0
                fake.stderr = ""
                fake.stdout = ""
            else:
                fake.returncode = 0
                fake.stderr = ""
                fake.stdout = ""
            return fake

        mock_run.side_effect = side_effect
        config = DispatchConfig()
        result = setup_labels(config)
        assert result.updated > 0
        assert result.created == 0
        assert result.failed == 0


class TestHeldIssueNotClaimed:
    """A held issue has no 'ready' label, so claiming refuses it —
    the scheduler queue query never even returns it (selects by ready)."""

    @patch("kew.labels.run")
    def test_try_claim_refuses_needs_approval_issue(self, mock_run: MagicMock):
        config = DispatchConfig()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"labels": [{"name": "needs-approval"}, {"name": "medium"}]}
            ),
        )
        assert try_claim_issue(42, config) is ClaimResult.NOT_READY


class TestMarkNeedsReviewIncomplete:
    """Rescue incomplete-but-coherent runs: max-steps transitions."""

    def test_mark_needs_review_incomplete_labels_and_stays_open(self, monkeypatch):
        from kew import labels as labels_mod
        from kew.config import DispatchConfig

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            from types import SimpleNamespace

            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(labels_mod, "run", fake_run)
        labels_mod.mark_needs_review_incomplete(42, DispatchConfig())

        flat = [tok for cmd in calls for tok in cmd]
        # adds needs-help + incomplete-run
        assert "agent-needs-help" in flat
        assert "incomplete-run" in flat
        # never marks it reviewable-as-done or closes it
        assert "pr-pending-review" not in flat
        assert not any("close" in cmd for cmd in calls)

        # removes the working labels (in-progress, agent-working, ready) via
        # --remove-label, so the issue doesn't linger in the active queue
        removed = {
            cmd[i + 1]
            for cmd in calls
            for i, tok in enumerate(cmd)
            if tok == "--remove-label"
        }
        assert removed == {"in-progress", "agent-working", "ready"}
