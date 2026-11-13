"""Tests for compact analytics widget formatting."""

from kew.tui.widgets.analytics_panel import format_analytics_compact


class TestFormatAnalyticsCompact:
    def test_basic_stats(self):
        stats = {
            "runs": 5,
            "success_rate": 80.0,
            "avg_cost": 0.34,
            "avg_duration": 372.0,
            "total_spend": 1.70,
        }
        result = format_analytics_compact(stats, daily_budget=25.0)
        assert "5 runs" in result
        assert "80" in result
        assert "$0.34" in result
        assert "6.2m" in result
        assert "$25" in result

    def test_empty_stats(self):
        stats = {
            "runs": 0,
            "success_rate": 0.0,
            "avg_cost": 0.0,
            "avg_duration": 0.0,
            "total_spend": 0.0,
        }
        result = format_analytics_compact(stats, daily_budget=25.0)
        assert "0 runs" in result

    def test_no_budget(self):
        stats = {
            "runs": 3,
            "success_rate": 100.0,
            "avg_cost": 0.20,
            "avg_duration": 300.0,
            "total_spend": 0.60,
        }
        result = format_analytics_compact(stats, daily_budget=None)
        assert "3 runs" in result
        assert "Spent" in result
        assert "Budget" not in result
