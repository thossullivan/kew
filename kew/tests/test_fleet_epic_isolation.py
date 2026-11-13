"""Positive fleet-exclusion guard: epic:* nodes/PRs must never be picked up
by the fleet selector, conflict resolver, or auto-unblock.

Non-negotiable: the guard must be POSITIVE (skip anything carrying an
``epic:`` label) rather than relying on the negative assumption that epic
nodes never carry ``ready`` — reset paths (``reset_issue_to_ready``) re-add
``ready``, so an issue can legitimately carry both labels at once.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig, ResolveConfig


def _epic_issue(number: int, extra_labels: list[str] | None = None) -> dict[str, Any]:
    """Build an issue dict carrying both 'ready' and 'epic:alpha' labels."""
    labels = [{"name": "ready"}, {"name": "epic:alpha"}]
    for name in extra_labels or []:
        labels.append({"name": name})
    return {
        "number": number,
        "title": f"Epic node #{number}",
        "labels": labels,
        "body": "",
        "createdAt": "2024-01-01",
    }


def _plain_issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Plain issue #{number}",
        "labels": [{"name": "ready"}],
        "body": "",
        "createdAt": "2024-01-02",
    }


class TestSelectorExcludesEpicIssues:
    """(1) An issue carrying both 'ready' and 'epic:alpha' is never returned."""

    def test_first_eligible_skips_epic_labelled_issue(self):
        from kew.selector import _first_eligible

        issues = [_epic_issue(1), _plain_issue(2)]
        config = DispatchConfig()

        result = _first_eligible(issues, exclude=None, config=config)

        assert result is not None
        assert result["number"] == 2

    def test_first_eligible_returns_none_when_only_epic_issues(self):
        from kew.selector import _first_eligible

        issues = [_epic_issue(1), _epic_issue(2)]
        config = DispatchConfig()

        result = _first_eligible(issues, exclude=None, config=config)

        assert result is None

    @patch("kew.selector.run_with_backoff")
    def test_select_next_issue_never_returns_epic_node(self, mock_run: MagicMock):
        from kew.selector import select_next_issue

        # The gh CLI happily returns an epic-labelled issue that still
        # carries 'ready' (e.g. after a reset_issue_to_ready call).
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps([_epic_issue(1)])
        )
        config = DispatchConfig()

        result = select_next_issue(config=config)

        assert result is None


class TestListReadyIssuesExcludesEpic:
    """(2) list_ready_issues drops epic-labelled issues."""

    def test_drops_epic_labelled_issue(self, monkeypatch):
        from kew.selector import list_ready_issues

        issues = [_epic_issue(1), _plain_issue(2)]
        monkeypatch.setattr(
            "kew.selector._gh_issue_list",
            lambda label, repo, extra_args, strict=False: issues,
        )

        config = DispatchConfig()
        result = list_ready_issues(config=config)

        assert [i["number"] for i in result] == [2]

    def test_all_epic_yields_empty(self, monkeypatch):
        from kew.selector import list_ready_issues

        issues = [_epic_issue(1), _epic_issue(2)]
        monkeypatch.setattr(
            "kew.selector._gh_issue_list",
            lambda label, repo, extra_args, strict=False: issues,
        )

        config = DispatchConfig()
        result = list_ready_issues(config=config)

        assert result == []


class TestListQueueIssuesExcludesEpic:
    """(3) list_queue_issues drops epic-labelled issues from every partition.

    A ready+epic:alpha issue must not leak into ready/failed/backlog even
    though it carries the 'ready' label — TUI dispatch reads straight from
    the 'ready' partition (TuiState.queue_ready -> handle_dispatch), so any
    leak here is a direct fleet-dispatch-onto-an-epic-node bug.
    """

    def test_epic_ready_issue_excluded_from_all_partitions(self, monkeypatch):
        from kew.selector import list_queue_issues

        issues = [_epic_issue(1), _plain_issue(2)]
        monkeypatch.setattr(
            "kew.selector._gh_issue_list",
            lambda label, repo, extra_args, strict=False: issues,
        )

        config = DispatchConfig()
        ready, failed, backlog = list_queue_issues(config=config)

        assert [i["number"] for i in ready] == [2]
        assert all(i["number"] != 1 for i in failed)
        assert all(i["number"] != 1 for i in backlog)


class TestPollConflictingPRsExcludesEpic:
    """(4) _poll_conflicting_prs skips a CONFLICTING PR labelled 'epic:alpha'."""

    @patch("kew.scheduler.subprocess_run")
    def test_skips_epic_labelled_conflicting_pr(self, mock_run: MagicMock):
        from kew.scheduler import DispatchScheduler

        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)

        epic_pr = {
            "number": 10,
            "headRefName": "agent/issue-10",
            "baseRefName": "main",
            "title": "Epic PR",
            "body": "Closes #10",
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "labels": [{"name": "agent-pr"}, {"name": "epic:alpha"}],
        }
        plain_pr = {
            "number": 20,
            "headRefName": "agent/issue-20",
            "baseRefName": "main",
            "title": "Plain PR",
            "body": "Closes #20",
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "labels": [{"name": "agent-pr"}],
        }
        mock_run.return_value = MagicMock(
            stdout=json.dumps([epic_pr, plain_pr]), returncode=0
        )

        result = scheduler._poll_conflicting_prs()

        assert [pr["number"] for pr in result] == [20]

    @patch("kew.scheduler.subprocess_run")
    def test_all_epic_yields_empty(self, mock_run: MagicMock):
        from kew.scheduler import DispatchScheduler

        config = DispatchConfig(resolve=ResolveConfig(auto=True))
        scheduler = DispatchScheduler(config)

        epic_pr = {
            "number": 10,
            "headRefName": "agent/issue-10",
            "baseRefName": "main",
            "title": "Epic PR",
            "body": "Closes #10",
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "labels": [{"name": "epic:alpha"}],
        }
        mock_run.return_value = MagicMock(stdout=json.dumps([epic_pr]), returncode=0)

        result = scheduler._poll_conflicting_prs()

        assert result == []


class TestAutoUnblockDependentsExcludesEpic:
    """(5) _auto_unblock_dependents does not unblock epic-labelled dependents."""

    _P = "kew.dispatch_core"

    @patch(f"{_P}.unblock_issue")
    @patch(f"{_P}._fetch_issue_labels")
    @patch(f"{_P}.find_blocked_dependents", return_value=[72, 73])
    def test_skips_epic_dependent_but_unblocks_plain_dependent(
        self,
        mock_find: MagicMock,
        mock_fetch_labels: MagicMock,
        mock_unblock: MagicMock,
    ):
        from kew.dispatch_core import _auto_unblock_dependents

        def fake_fetch(dep_num: int, config: DispatchConfig) -> set[str]:
            if dep_num == 72:
                return {"blocked", "epic:alpha"}
            return {"blocked"}

        mock_fetch_labels.side_effect = fake_fetch
        config = DispatchConfig()

        _auto_unblock_dependents(42, config)

        mock_unblock.assert_called_once_with(73, config)

    @patch(f"{_P}.unblock_issue")
    @patch(f"{_P}._fetch_issue_labels", return_value=None)
    @patch(f"{_P}.find_blocked_dependents", return_value=[72])
    def test_fails_open_when_labels_unavailable(
        self,
        mock_find: MagicMock,
        mock_fetch_labels: MagicMock,
        mock_unblock: MagicMock,
    ):
        """Best-effort semantics: if labels can't be fetched, still unblock."""
        from kew.dispatch_core import _auto_unblock_dependents

        config = DispatchConfig()

        _auto_unblock_dependents(42, config)

        mock_unblock.assert_called_once_with(72, config)
