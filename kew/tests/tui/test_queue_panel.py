"""Tests for queue panel formatting."""

from datetime import UTC, datetime, timedelta

from kew.tui.palette import JUDGMENT
from kew.tui.widgets.queue_panel import (
    _relative_age,
    format_backlog_row_compact,
    format_failed_row_compact,
    format_failed_separator,
)


class TestRelativeAge:
    def _iso(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_zero_days(self):
        now = datetime.now(UTC)
        assert _relative_age(self._iso(now)) == "0d"

    def test_hours_ago_still_zero_days(self):
        dt = datetime.now(UTC) - timedelta(hours=5)
        assert _relative_age(dt.isoformat()) == "0d"

    def test_three_days(self):
        dt = datetime.now(UTC) - timedelta(days=3)
        assert _relative_age(self._iso(dt)) == "3d"

    def test_six_days(self):
        dt = datetime.now(UTC) - timedelta(days=6)
        assert _relative_age(self._iso(dt)) == "6d"

    def test_one_week(self):
        dt = datetime.now(UTC) - timedelta(days=7)
        assert _relative_age(self._iso(dt)) == "1w"

    def test_four_weeks(self):
        dt = datetime.now(UTC) - timedelta(days=28)
        assert _relative_age(self._iso(dt)) == "4w"

    def test_two_months(self):
        dt = datetime.now(UTC) - timedelta(days=65)
        assert _relative_age(self._iso(dt)) == "2mo"

    def test_eleven_months(self):
        dt = datetime.now(UTC) - timedelta(days=335)
        assert _relative_age(self._iso(dt)) == "11mo"

    def test_one_year(self):
        dt = datetime.now(UTC) - timedelta(days=366)
        assert _relative_age(self._iso(dt)) == "1y"

    def test_empty_string_returns_empty(self):
        assert _relative_age("") == ""

    def test_none_returns_empty(self):
        assert _relative_age(None) == ""


class TestFormatBacklogRowCompact:
    def test_shows_priority(self):
        issue = {
            "number": 55,
            "title": "Add retry logic",
            "labels": [{"name": "p2-medium"}],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        assert "P2" in result

    def test_priority_is_colored_not_bold(self):
        issue = {
            "number": 55,
            "title": "Add retry logic",
            "labels": [{"name": "p0-critical"}],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        # Should have color but NOT bold
        assert JUDGMENT in result
        assert "bold" not in result

    def test_shows_complexity(self):
        issue = {
            "number": 55,
            "title": "Add retry logic",
            "labels": [{"name": "simple"}],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        assert "simple" in result

    def test_shows_age(self):
        issue = {
            "number": 55,
            "title": "Add retry logic",
            "labels": [],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        # Should contain some age string (exact value depends on current date)
        # At minimum it should have the issue number and not the old ── prefix
        assert "#55" in result
        assert "──" not in result

    def test_no_dash_prefix(self):
        issue = {
            "number": 55,
            "title": "Test",
            "labels": [],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        assert "──" not in result

    def test_truncates_long_title(self):
        issue = {
            "number": 55,
            "title": "A" * 50,
            "labels": [],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        assert "\u2026" in result

    def test_no_priority_shows_dashes(self):
        issue = {
            "number": 55,
            "title": "Test",
            "labels": [],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        result = format_backlog_row_compact(issue)
        assert "--" in result

    def test_missing_created_at(self):
        issue = {
            "number": 55,
            "title": "Test",
            "labels": [],
        }
        result = format_backlog_row_compact(issue)
        assert "#55" in result


class TestFormatFailedRowCompact:
    def test_basic_failed_row(self):
        issue = {
            "number": 42,
            "title": "Fix auth bug",
            "labels": [{"name": "agent-failed"}, {"name": "p1-high"}],
        }
        result = format_failed_row_compact(issue)
        assert "#42" in result
        assert "Fix auth bug" in result
        assert JUDGMENT in result

    def test_shows_budget_exceeded_label(self):
        issue = {
            "number": 10,
            "title": "Refactor",
            "labels": [{"name": "agent-failed"}, {"name": "budget-exceeded"}],
        }
        result = format_failed_row_compact(issue)
        assert "budget-exceeded" in result

    def test_shows_agent_needs_help_label(self):
        issue = {
            "number": 11,
            "title": "Deploy",
            "labels": [{"name": "agent-failed"}, {"name": "agent-needs-help"}],
        }
        result = format_failed_row_compact(issue)
        assert "agent-needs-help" in result

    def test_limits_extra_labels_to_two(self):
        issue = {
            "number": 12,
            "title": "Test",
            "labels": [
                {"name": "agent-failed"},
                {"name": "budget-exceeded"},
                {"name": "agent-needs-help"},
            ],
        }
        result = format_failed_row_compact(issue)
        assert "budget-exceeded" in result
        assert "agent-needs-help" in result

    def test_no_extra_labels(self):
        issue = {
            "number": 13,
            "title": "Simple fail",
            "labels": [{"name": "agent-failed"}],
        }
        result = format_failed_row_compact(issue)
        assert "#13" in result
        assert JUDGMENT in result

    def test_truncates_long_title(self):
        issue = {
            "number": 14,
            "title": "A" * 50,
            "labels": [{"name": "agent-failed"}],
        }
        result = format_failed_row_compact(issue)
        assert "\u2026" in result


class TestFormatFailedSeparator:
    def test_contains_failed_text(self):
        result = format_failed_separator()
        assert "failed" in result.lower()
        assert JUDGMENT in result
