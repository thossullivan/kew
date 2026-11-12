"""Tests for review detail rendering and CI status."""

from kew.tui.palette import ATTESTED, JUDGMENT
from kew.tui.review_detail import compute_verdict
from kew.tui.widgets.reviews_panel import format_review_row_compact


class TestCIStatusIndicator:
    def test_all_checks_pass(self):
        pr = {
            "number": 89,
            "title": "Add logging",
            "author": {"login": "bot"},
            "createdAt": "2026-03-24T10:00:00Z",
            "comments": [],
            "statusCheckRollup": [
                {"name": "ruff", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
        }
        result = format_review_row_compact(pr)
        assert "\u2713CI" in result  # ✓CI

    def test_some_checks_fail(self):
        pr = {
            "number": 87,
            "title": "Refactor",
            "author": {"login": "bot"},
            "createdAt": "2026-03-24T10:00:00Z",
            "comments": [],
            "statusCheckRollup": [
                {"name": "ruff", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "pytest", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
        }
        result = format_review_row_compact(pr)
        assert "\u2717CI" in result  # ✗CI

    def test_checks_pending(self):
        pr = {
            "number": 85,
            "title": "Add cache",
            "author": {"login": "bot"},
            "createdAt": "2026-03-24T10:00:00Z",
            "comments": [],
            "statusCheckRollup": [
                {"name": "ruff", "status": "IN_PROGRESS", "conclusion": ""},
            ],
        }
        result = format_review_row_compact(pr)
        assert "\u00b7\u00b7" in result  # pending indicator

    def test_no_check_data(self):
        pr = {
            "number": 83,
            "title": "Fix edge case",
            "author": {"login": "bot"},
            "createdAt": "2026-03-24T10:00:00Z",
            "comments": [],
        }
        result = format_review_row_compact(pr)
        assert "\u00b7\u00b7" in result  # pending indicator


class TestComputeVerdict:
    def test_ready_to_merge(self):
        pr_data = {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviewDecision": "APPROVED",
            "mergeable": "MERGEABLE",
        }
        verdict = compute_verdict(pr_data)
        assert verdict["status"] == "ready"
        assert "Ready to merge" in verdict["title"]
        assert verdict["color"] == ATTESTED

    def test_ci_failing(self):
        pr_data = {
            "statusCheckRollup": [
                {"name": "ruff", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "pytest", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
            "reviewDecision": "APPROVED",
            "mergeable": "MERGEABLE",
        }
        verdict = compute_verdict(pr_data)
        assert verdict["status"] == "ci_failing"
        assert "CI failing" in verdict["title"]
        assert verdict["color"] == JUDGMENT

    def test_changes_requested(self):
        pr_data = {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeable": "MERGEABLE",
            "reviews": [
                {"author": {"login": "reviewer1"}, "state": "CHANGES_REQUESTED"},
            ],
        }
        verdict = compute_verdict(pr_data)
        assert verdict["status"] == "changes_requested"
        assert "Changes requested" in verdict["title"]
        assert "@reviewer1" in verdict["subtitle"]

    def test_awaiting_review(self):
        pr_data = {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviewDecision": "",
            "mergeable": "MERGEABLE",
            "reviewRequests": [{"login": "reviewer1"}],
        }
        verdict = compute_verdict(pr_data)
        assert verdict["status"] == "awaiting_review"
        assert "Awaiting review" in verdict["title"]
        assert "@reviewer1" in verdict["subtitle"]

    def test_ci_failing_trumps_changes_requested(self):
        pr_data = {
            "statusCheckRollup": [
                {"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
            "reviewDecision": "CHANGES_REQUESTED",
            "mergeable": "MERGEABLE",
        }
        verdict = compute_verdict(pr_data)
        assert verdict["status"] == "ci_failing"
