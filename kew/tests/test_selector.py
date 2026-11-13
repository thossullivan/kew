"""Tests for issue selection."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig, LabelConfig
from kew.selector import (
    empty_queue_message,
    get_complexity,
    list_queue_issues,
    list_ready_issues,
    select_next_issue,
)


class TestGetComplexity:
    def test_simple(self):
        labels = [{"name": "simple"}, {"name": "ready"}]
        assert get_complexity(labels) == "simple"

    def test_complex(self):
        labels = [{"name": "complex"}, {"name": "p0-critical"}]
        assert get_complexity(labels) == "complex"

    def test_defaults_to_medium(self):
        labels = [{"name": "ready"}]
        assert get_complexity(labels) == "medium"

    def test_empty_labels(self):
        assert get_complexity([]) == "medium"

    def test_skips_non_dict(self):
        labels: list[Any] = [{"name": "simple"}, "bogus", 42, None]
        assert get_complexity(labels) == "simple"


class TestSelectNextIssue:
    @patch("kew.selector.run_with_backoff")
    def test_returns_none_when_queue_empty(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        config = DispatchConfig()
        result = select_next_issue(config=config)
        assert result is None

    @patch("kew.selector.run_with_backoff")
    def test_empty_body_with_rc_0_is_a_failed_read_not_an_empty_queue(
        self, mock_run: MagicMock
    ):
        # gh exiting 0 with no body is a broken gh, not "nothing ready":
        # strict selection must raise rather than dispatch nothing (#1436).
        from kew.selector import GhQueryError

        mock_run.return_value = MagicMock(returncode=0, stdout="")
        config = DispatchConfig()
        with pytest.raises(GhQueryError, match="no output"):
            select_next_issue(config=config)

    @patch("kew.selector.run_with_backoff")
    def test_force_issue(self, mock_run: MagicMock):
        issue_data = {"number": 42, "title": "Test", "body": "Body", "labels": []}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue_data))
        config = DispatchConfig()
        result = select_next_issue(config=config, force_issue=42)
        assert result is not None
        assert result["number"] == 42

    @patch("kew.selector.run_with_backoff")
    def test_selects_by_priority_order(self, mock_run: MagicMock):
        """Should try p0-critical first, then p1-high, etc."""
        issue_data = [{"number": 1, "title": "Hi-pri", "body": "", "labels": []}]

        def side_effect(cmd: list[str], **kwargs: Any) -> MagicMock:
            # Return empty for p0, hit on p1
            if "p1-high" in cmd:
                return MagicMock(returncode=0, stdout=json.dumps(issue_data))
            return MagicMock(returncode=0, stdout="[]")

        mock_run.side_effect = side_effect
        config = DispatchConfig()
        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 1


class TestSelectNextIssueMalformedJSON:
    """select_next_issue handles malformed JSON from gh CLI gracefully."""

    @patch("kew.selector.run_with_backoff")
    def test_force_issue_malformed_json_returns_none(self, mock_run: MagicMock):
        """Malformed JSON from gh issue view in force_issue path returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="not valid json{{{")
        config = DispatchConfig()
        result = select_next_issue(config=config, force_issue=42)
        assert result is None

    @patch("kew.selector.run_with_backoff")
    def test_query_ready_issue_malformed_json_is_a_failed_read(
        self, mock_run: MagicMock
    ):
        """Malformed JSON from gh issue list under strict selection raises."""
        from kew.selector import GhQueryError

        mock_run.return_value = MagicMock(returncode=0, stdout="garbage output")
        config = DispatchConfig()
        with pytest.raises(GhQueryError, match="invalid JSON"):
            select_next_issue(config=config)

    @patch("kew.selector.run_with_backoff")
    def test_force_issue_non_object_json_returns_none(self, mock_run: MagicMock):
        """Valid JSON that is not a dict (e.g. list) returns None."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        config = DispatchConfig()
        result = select_next_issue(config=config, force_issue=42)
        assert result is None


class TestSelectExcludesIssues:
    """select_next_issue can exclude specific issue numbers."""

    @patch("kew.selector.run_with_backoff")
    def test_exclude_filters_in_python(self, mock_run: MagicMock):
        """Excluding issues filters results in Python, not via --search."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        # gh returns a batch that includes the excluded issue
        batch = json.dumps(
            [
                {"number": 42, "title": "excluded", "labels": [], "body": ""},
                {"number": 43, "title": "keep", "labels": [], "body": ""},
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=batch)

        result = select_next_issue(config=config, exclude={42})
        assert result is not None
        assert result["number"] == 43

        # Verify NO --search flag is used (exclude is done in Python)
        call_args = mock_run.call_args_list
        found_search = any("--search" in str(c) for c in call_args)
        assert not found_search, (
            f"Should not use --search for exclusion (breaks with --json), "
            f"got: {call_args}"
        )

    @patch("kew.selector.run_with_backoff")
    def test_exclude_all_returns_none(self, mock_run: MagicMock):
        """If all returned issues are excluded, returns None."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = json.dumps(
            [
                {"number": 42, "title": "excluded", "labels": [], "body": ""},
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=batch)

        result = select_next_issue(config=config, exclude={42})
        assert result is None

    @patch("kew.selector.run_with_backoff")
    def test_empty_exclude_set_no_filter(self, mock_run: MagicMock):
        """Empty exclude set returns first result."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = json.dumps(
            [
                {"number": 42, "title": "issue", "labels": [], "body": ""},
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=batch)

        result = select_next_issue(config=config, exclude=set())
        assert result is not None

    @patch("kew.selector.run_with_backoff")
    def test_none_exclude_no_filter(self, mock_run: MagicMock):
        """None exclude (default) returns first result."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = json.dumps(
            [
                {"number": 42, "title": "issue", "labels": [], "body": ""},
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=batch)

        result = select_next_issue(config=config)
        assert result is not None


class TestBlockedLabelFiltering:
    """Issues with both 'ready' and 'blocked' labels are skipped by the selector."""

    @patch("kew.selector.run_with_backoff")
    def test_blocked_issue_skipped(self, mock_run: MagicMock):
        """An issue with 'blocked' label is skipped even if it has 'ready'."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        ready_batch = [
            {
                "number": 42,
                "title": "blocked issue",
                "labels": [{"name": "ready"}, {"name": "blocked"}],
                "body": "",
            },
            {
                "number": 43,
                "title": "unblocked issue",
                "labels": [{"name": "ready"}],
                "body": "",
            },
        ]

        def side_effect(cmd: list[str], **kwargs: Any) -> MagicMock:
            assert "--label" in cmd, "expected --label in gh command"
            assert "ready" in cmd, "expected 'ready' label filter"
            return MagicMock(returncode=0, stdout=json.dumps(ready_batch))

        mock_run.side_effect = side_effect

        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 43

    @patch("kew.selector.run_with_backoff")
    def test_all_blocked_returns_none(self, mock_run: MagicMock):
        """If all ready issues are also blocked, returns None."""
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        blocked_only = [
            {
                "number": 42,
                "title": "blocked",
                "labels": [{"name": "ready"}, {"name": "blocked"}],
                "body": "",
            },
        ]

        def side_effect(cmd: list[str], **kwargs: Any) -> MagicMock:
            assert "--label" in cmd
            # Verify batch size is >= 10 (the selector should fetch a
            # batch even without exclude, so blocked filtering has headroom)
            limit_idx = cmd.index("--limit") + 1 if "--limit" in cmd else -1
            if limit_idx > 0:
                assert int(cmd[limit_idx]) >= 10
            return MagicMock(returncode=0, stdout=json.dumps(blocked_only))

        mock_run.side_effect = side_effect

        result = select_next_issue(config=config)
        assert result is None

    @patch("kew.selector.run_with_backoff")
    def test_custom_blocked_label_name(self, mock_run: MagicMock):
        """Respects custom blocked label name from config."""
        config = DispatchConfig(labels=LabelConfig(blocked="on-hold"))
        config.project.repo = "owner/repo"

        ready_batch = [
            {
                "number": 42,
                "title": "on hold",
                "labels": [{"name": "ready"}, {"name": "on-hold"}],
                "body": "",
            },
            {
                "number": 43,
                "title": "good to go",
                "labels": [{"name": "ready"}],
                "body": "",
            },
        ]

        def side_effect(cmd: list[str], **kwargs: Any) -> MagicMock:
            assert "--label" in cmd
            return MagicMock(returncode=0, stdout=json.dumps(ready_batch))

        mock_run.side_effect = side_effect

        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 43


class TestListReviewPrs:
    @patch("kew.selector.run_with_backoff")
    def test_returns_prs_sorted_by_age(self, mock_run: MagicMock):
        from kew.selector import list_review_prs

        prs = [
            {
                "number": 87,
                "title": "New PR",
                "author": {"login": "alice"},
                "createdAt": "2026-03-23T10:00:00Z",
                "comments": [],
            },
            {
                "number": 82,
                "title": "Old PR",
                "author": {"login": "bob"},
                "createdAt": "2026-03-21T10:00:00Z",
                "comments": [{"id": 1}, {"id": 2}],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(prs))
        config = DispatchConfig()
        result = list_review_prs(config=config)

        assert len(result) == 2
        # Oldest first
        assert result[0]["number"] == 82
        assert result[1]["number"] == 87

        # Verify the gh CLI was called with expected arguments and timeout
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "gh" in cmd
        assert "pr" in cmd
        assert "list" in cmd
        assert "--label" in cmd
        assert "pr-pending-review" in cmd
        assert call_args[1].get("timeout") == 30

    @patch("kew.selector.run_with_backoff")
    def test_returns_empty_on_no_prs(self, mock_run: MagicMock):
        from kew.selector import list_review_prs

        mock_run.return_value = MagicMock(returncode=0, stdout="[]")
        config = DispatchConfig()
        result = list_review_prs(config=config)
        assert result == []

    @patch("kew.selector.run_with_backoff")
    def test_returns_empty_on_timeout(self, mock_run: MagicMock):
        import subprocess

        from kew.selector import list_review_prs

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        config = DispatchConfig()
        result = list_review_prs(config=config)
        assert result == []


class TestGhQueryFailureIsLoud:
    """A gh nonzero exit on the selection path must be loud, not empty-queue.

    An overnight ``kew run --drain`` used to exit "queue empty" the instant its
    token expired; the selection path now raises ``GhQueryError`` instead of
    degrading to an empty list (#SFD-151, I10).
    """

    @patch("kew.selector.run_with_backoff")
    def test_run_gh_json_strict_raises_on_nonzero(self, mock_run: MagicMock) -> None:
        from kew.selector import GhQueryError, _run_gh_json

        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="gh: token expired"
        )
        with pytest.raises(GhQueryError):
            _run_gh_json(["gh", "issue", "list"], strict=True)

    @patch("kew.selector.run_with_backoff")
    def test_run_gh_json_lenient_returns_empty_on_nonzero(
        self, mock_run: MagicMock
    ) -> None:
        """Non-strict callers (TUI panels) keep degrading to an empty list."""
        from kew.selector import _run_gh_json

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        assert _run_gh_json(["gh", "issue", "list"]) == []

    @patch("kew.selector.run_with_backoff")
    def test_select_next_issue_raises_on_gh_failure(self, mock_run: MagicMock) -> None:
        from kew.selector import GhQueryError

        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="HTTP 401: Bad credentials"
        )
        config = DispatchConfig()
        with pytest.raises(GhQueryError):
            select_next_issue(config=config)

    @patch("kew.selector.run_with_backoff")
    def test_empty_stdout_zero_exit_is_still_empty_queue(
        self, mock_run: MagicMock
    ) -> None:
        """A clean zero-exit empty result is a genuine empty queue, not an error."""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        config = DispatchConfig()
        assert select_next_issue(config=config, sleep=lambda _: None) is None


class TestListQueueIssues:
    """Tests for list_queue_issues() partitioning."""

    @patch("kew.selector._run_gh_json")
    def test_partitions_ready_and_backlog(self, mock_gh: MagicMock) -> None:
        """Ready issues go to first list, unlabeled to second."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "Ready issue",
                "labels": [{"name": "ready"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "Backlog issue",
                "labels": [{"name": "bug"}],
                "createdAt": "2026-03-02T00:00:00Z",
            },
            {
                "number": 3,
                "title": "No labels",
                "labels": [],
                "createdAt": "2026-03-03T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in ready] == [1]
        assert failed == []
        assert [i["number"] for i in backlog] == [2, 3]

    @patch("kew.selector._run_gh_json")
    def test_excludes_lifecycle_labeled_issues(self, mock_gh: MagicMock) -> None:
        """Issues with dispatch lifecycle labels are excluded from both lists."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "In progress",
                "labels": [{"name": "in-progress"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "Agent working",
                "labels": [{"name": "agent-working"}],
                "createdAt": "2026-03-02T00:00:00Z",
            },
            {
                "number": 3,
                "title": "Has PR",
                "labels": [{"name": "agent-pr"}],
                "createdAt": "2026-03-03T00:00:00Z",
            },
            {
                "number": 4,
                "title": "Backlog",
                "labels": [],
                "createdAt": "2026-03-04T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)
        assert ready == []
        assert failed == []
        assert [i["number"] for i in backlog] == [4]

    @patch("kew.selector._run_gh_json")
    def test_sorts_by_priority_then_age(self, mock_gh: MagicMock) -> None:
        """Both lists sort by priority (ascending) then createdAt."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "P2",
                "labels": [{"name": "ready"}, {"name": "p2-medium"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "P0",
                "labels": [{"name": "ready"}, {"name": "p0-critical"}],
                "createdAt": "2026-03-02T00:00:00Z",
            },
            {
                "number": 3,
                "title": "P1",
                "labels": [{"name": "ready"}, {"name": "p1-high"}],
                "createdAt": "2026-03-03T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in ready] == [2, 3, 1]

    @patch("kew.selector._run_gh_json")
    def test_partitions_agent_failed_into_failed_list(self, mock_gh: MagicMock) -> None:
        """Issues with agent-failed label go to the failed list (middle tuple)."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "Ready",
                "labels": [{"name": "ready"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "Failed",
                "labels": [{"name": "agent-failed"}],
                "createdAt": "2026-03-02T00:00:00Z",
            },
            {
                "number": 3,
                "title": "Backlog",
                "labels": [],
                "createdAt": "2026-03-03T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in ready] == [1]
        assert [i["number"] for i in failed] == [2]
        assert [i["number"] for i in backlog] == [3]

    @patch("kew.selector._run_gh_json")
    def test_failed_sorted_by_priority_then_age(self, mock_gh: MagicMock) -> None:
        """Failed list sorts by priority then createdAt, same as ready."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "P2 failed",
                "labels": [{"name": "agent-failed"}, {"name": "p2-medium"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "P0 failed",
                "labels": [{"name": "agent-failed"}, {"name": "p0-critical"}],
                "createdAt": "2026-03-02T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        _, failed, _ = list_queue_issues(config=config)
        assert [i["number"] for i in failed] == [2, 1]

    @patch("kew.selector._run_gh_json")
    def test_agent_failed_with_budget_exceeded_goes_to_failed(
        self, mock_gh: MagicMock
    ) -> None:
        """Agent-failed + budget-exceeded goes to failed, not skipped."""
        mock_gh.return_value = [
            {
                "number": 1,
                "title": "Budget fail",
                "labels": [{"name": "agent-failed"}, {"name": "budget-exceeded"}],
                "createdAt": "2026-03-01T00:00:00Z",
            },
        ]
        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in failed] == [1]
        assert ready == []
        assert backlog == []


class TestListReadyIssues:
    def test_returns_sorted_issues(self, monkeypatch):
        """Returns issues sorted by priority then age."""
        issues = [
            {
                "number": 10,
                "title": "Low",
                "labels": [{"name": "kew:ready"}, {"name": "P2"}],
                "createdAt": "2024-01-03",
            },
            {
                "number": 11,
                "title": "High",
                "labels": [{"name": "kew:ready"}, {"name": "P0"}],
                "createdAt": "2024-01-02",
            },
            {
                "number": 12,
                "title": "Mid",
                "labels": [{"name": "kew:ready"}, {"name": "P1"}],
                "createdAt": "2024-01-01",
            },
        ]
        monkeypatch.setattr(
            "kew.selector._gh_issue_list",
            lambda label, repo, extra_args, strict=False: issues,
        )

        config = DispatchConfig()
        result = list_ready_issues(config=config)

        assert [i["number"] for i in result] == [11, 12, 10]

    def test_returns_empty_on_no_issues(self, monkeypatch):
        monkeypatch.setattr(
            "kew.selector._gh_issue_list",
            lambda label, repo, extra_args, strict=False: [],
        )

        config = DispatchConfig()
        result = list_ready_issues(config=config)
        assert result == []

    def test_uses_config_label(self, monkeypatch):
        """Uses the ready label from config.labels.ready."""
        captured = {}

        def fake_gh(label, repo, extra_args, strict=False):
            captured["label"] = label
            return []

        monkeypatch.setattr("kew.selector._gh_issue_list", fake_gh)

        config = DispatchConfig(labels=LabelConfig(ready="custom:ready"))
        list_ready_issues(config=config)
        assert captured["label"] == "custom:ready"


class TestOverlapHoldSkipping:
    @patch("kew.selector.run_with_backoff")
    def test_skips_overlap_hold_issues(self, mock_run: MagicMock):
        """Issues with overlap-hold label are skipped like blocked issues."""
        issues = [
            {
                "number": 1,
                "title": "held",
                "labels": [{"name": "ready"}, {"name": "overlap-hold"}],
                "body": "",
            },
            {"number": 2, "title": "free", "labels": [{"name": "ready"}], "body": ""},
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))
        config = DispatchConfig()
        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 2


class TestDependencyAwareSelection:
    """Issues with unresolved kew:depends are auto-blocked and skipped."""

    @patch("kew.selector._update_labels")
    @patch("kew.selector.check_dependencies_resolved")
    @patch("kew.selector.run_with_backoff")
    def test_unresolved_deps_auto_blocked_and_skipped(
        self,
        mock_run: MagicMock,
        mock_check: MagicMock,
        mock_labels: MagicMock,
    ):
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = [
            {
                "number": 42,
                "title": "Has deps",
                "labels": [{"name": "ready"}],
                "body": "<!-- kew:depends 10 -->",
            },
            {
                "number": 43,
                "title": "No deps",
                "labels": [{"name": "ready"}],
                "body": "No dependencies here",
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(batch))
        mock_check.return_value = False  # deps unresolved

        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 43

        # Verify blocked label was applied to #42
        mock_labels.assert_called_once()
        call_args = mock_labels.call_args
        assert call_args[0][0] == 42
        assert call_args[1]["add"] == ["blocked"]

    @patch("kew.selector.check_dependencies_resolved")
    @patch("kew.selector.run_with_backoff")
    def test_resolved_deps_not_blocked(
        self,
        mock_run: MagicMock,
        mock_check: MagicMock,
    ):
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = [
            {
                "number": 42,
                "title": "Deps resolved",
                "labels": [{"name": "ready"}],
                "body": "<!-- kew:depends 10 -->",
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(batch))
        mock_check.return_value = True  # all deps closed

        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 42

    @patch("kew.selector.check_dependencies_resolved")
    @patch("kew.selector.run_with_backoff")
    def test_no_depends_comment_not_checked(
        self,
        mock_run: MagicMock,
        mock_check: MagicMock,
    ):
        config = DispatchConfig()
        config.project.repo = "owner/repo"

        batch = [
            {
                "number": 42,
                "title": "Normal issue",
                "labels": [{"name": "ready"}],
                "body": "Just a regular issue",
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(batch))

        result = select_next_issue(config=config)
        assert result is not None
        assert result["number"] == 42
        mock_check.assert_not_called()


class TestQueueIssuesExcludeApprovalHolds:
    """Held issues are lifecycle-labelled — not backlog, not ready."""

    @patch("kew.selector._gh_issue_list")
    def test_needs_approval_issue_not_in_backlog_or_ready(self, mock_list):
        from kew.config import DispatchConfig
        from kew.selector import list_queue_issues

        config = DispatchConfig()
        mock_list.return_value = [
            {
                "number": 1,
                "title": "held",
                "labels": [{"name": "needs-approval"}],
                "createdAt": "2026-06-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "fresh",
                "labels": [],
                "createdAt": "2026-06-02T00:00:00Z",
            },
        ]
        ready, failed, backlog = list_queue_issues(config=config)
        assert [i["number"] for i in backlog] == [2]
        assert ready == []
        assert failed == []


class TestSelectNextIssueRequery:
    """Zero-result re-query absorbs GitHub label-write lag (CS-2, CS-6)."""

    @patch("kew.selector._query_ready_issue")
    def test_requery_finds_on_second_pass(self, mock_query: MagicMock) -> None:
        issue = {"number": 7, "title": "Later", "labels": []}
        # First full pass (4 priority labels + 1 fallback) is all empty;
        # second pass hits on the first priority label.
        mock_query.side_effect = [None, None, None, None, None, issue]
        sleep = MagicMock()
        config = DispatchConfig()
        result = select_next_issue(config=config, sleep=sleep)
        assert result == issue
        sleep.assert_called_once()

    @patch("kew.selector._query_ready_issue")
    def test_requery_fires_exactly_once_then_none(self, mock_query: MagicMock) -> None:
        mock_query.return_value = None
        sleep = MagicMock()
        config = DispatchConfig()
        result = select_next_issue(config=config, sleep=sleep)
        assert result is None
        # Exactly one retry — never a busy-loop.
        sleep.assert_called_once()

    @patch("kew.selector._query_ready_issue")
    def test_no_requery_when_first_pass_finds(self, mock_query: MagicMock) -> None:
        issue = {"number": 1, "title": "Now", "labels": []}
        mock_query.side_effect = [issue]
        sleep = MagicMock()
        config = DispatchConfig()
        result = select_next_issue(config=config, sleep=sleep)
        assert result == issue
        sleep.assert_not_called()

    @patch("kew.selector.run_with_backoff")
    def test_force_issue_skips_requery(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        sleep = MagicMock()
        config = DispatchConfig()
        result = select_next_issue(config=config, force_issue=42, sleep=sleep)
        assert result is None
        sleep.assert_not_called()


class TestEmptyQueueMessage:
    """Teaching message shown when no ready issues are found (CS-2, CS-6)."""

    @patch("kew.selector._gh_issue_list")
    def test_lists_unlabeled_candidates_with_command(
        self, mock_list: MagicMock
    ) -> None:
        mock_list.return_value = [
            {"number": 1, "title": "Add word validation", "labels": []},
            {"number": 2, "title": "Fix scoring", "labels": [{"name": "bug"}]},
            {"number": 3, "title": "Ready one", "labels": [{"name": "ready"}]},
        ]
        config = DispatchConfig()
        msg = empty_queue_message(config)
        assert "gh issue edit 1 --add-label ready" in msg
        assert "gh issue edit 2 --add-label ready" in msg
        assert "Add word validation" in msg
        # #3 already carries the ready label → not a labeling candidate.
        assert "gh issue edit 3" not in msg

    @patch("kew.selector._gh_issue_list")
    def test_caps_candidates_at_three(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"number": n, "title": f"T{n}", "labels": []} for n in range(1, 6)
        ]
        config = DispatchConfig()
        msg = empty_queue_message(config)
        assert msg.count("gh issue edit") == 3

    @patch("kew.selector._gh_issue_list")
    def test_no_open_issues(self, mock_list: MagicMock) -> None:
        mock_list.return_value = []
        config = DispatchConfig()
        msg = empty_queue_message(config)
        assert "No ready issues" in msg
        assert "open an issue" in msg.lower()
        assert "gh issue edit" not in msg

    @patch("kew.selector._gh_issue_list")
    def test_all_open_already_ready(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"number": 1, "title": "T", "labels": [{"name": "ready"}]},
        ]
        config = DispatchConfig()
        msg = empty_queue_message(config)
        assert "gh issue edit 1" not in msg
        assert "open an issue" in msg.lower()

    @patch("kew.selector._gh_issue_list")
    def test_respects_custom_ready_label(self, mock_list: MagicMock) -> None:
        mock_list.return_value = [
            {"number": 9, "title": "Nine", "labels": []},
        ]
        config = DispatchConfig(labels=LabelConfig(ready="go"))
        msg = empty_queue_message(config)
        assert "gh issue edit 9 --add-label go" in msg
