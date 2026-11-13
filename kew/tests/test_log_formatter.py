"""Tests for the log viewer event formatter."""

from __future__ import annotations

from conftest import (
    make_activity_event as _activity_event,
)
from conftest import (
    make_finished_event as _finished_event,
)
from conftest import (
    make_started_event as _started_event,
)
from conftest import (
    make_tokens_event as _tokens_event,
)
from conftest import (
    make_tool_use_event as _tool_use_event,
)

from kew.tui.log_formatter import fmt_duration, format_event, format_log_view
from kew.tui.palette import ATTESTED, JUDGMENT, MACHINE, MONEY


class TestFormatLogViewSummaryHeader:
    """Summary header should show runner, model, cost, and duration."""

    def test_summary_includes_runner_and_model(self):
        events = [_started_event(), _finished_event()]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "claude-code" in text
        assert "opus-4" in text

    def test_summary_includes_cost(self):
        events = [_started_event(), _finished_event(cost=1.23)]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "$1.23" in text

    def test_summary_includes_duration(self):
        events = [
            _started_event(ts=1711000000.0),
            _finished_event(ts=1711000060.0),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "1m" in text

    def test_summary_includes_total_tokens(self):
        events = [
            _started_event(),
            _tokens_event(inp=1000, out=200),
            _tokens_event(inp=2000, out=300),
            _finished_event(),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        # Last cumulative: 2000 + 300 = 2,300
        assert "2,300" in text

    def test_summary_uses_last_cumulative_tokens_not_sum(self):
        """With cumulative token events, summary should use the last value, not sum."""
        events = [
            _started_event(),
            _tokens_event(inp=1000, out=200),  # cumulative after turn 1
            _tokens_event(inp=3000, out=500),  # cumulative after turn 2
            _tokens_event(inp=5000, out=800),  # cumulative after turn 3
            _finished_event(),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        # Should show 5,800 (last cumulative: 5000 + 800), NOT 9,000+1,500
        assert "5,800" in text


class TestFormatLogViewEvents:
    """Event lines should be color-coded by type."""

    def test_activity_events_present(self):
        events = [_started_event(), _activity_event("Reading file")]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "Reading file" in text

    def test_tool_use_carries_the_machine_signal(self):
        events = [_started_event(), _tool_use_event("Edit", "foo.py")]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert f"[{MACHINE}]" in text
        assert "Edit" in text
        assert "foo.py" in text

    def test_tokens_event_shows_delta_format(self):
        events = [_started_event(), _tokens_event(inp=500, out=100, cost=0.05)]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "+600" in text
        assert "\u254c\u254c" in text  # delta separator chars

    def test_finished_success_carries_the_attested_signal(self):
        events = [_started_event(), _finished_event(exit_code=0)]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert f"[{ATTESTED}]" in text

    def test_finished_failure_carries_the_judgment_signal(self):
        events = [_started_event(), _finished_event(exit_code=1)]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert f"[{JUDGMENT}]" in text

    def test_error_event_highlighted(self):
        events = [
            _started_event(),
            {"ts": 1711000015.0, "type": "error", "msg": "Something broke"},
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "Something broke" in text
        assert f"[{JUDGMENT}]" in text


class TestFormatLogViewEdgeCases:
    """Edge cases and robustness."""

    def test_empty_events(self):
        lines = format_log_view([])
        assert isinstance(lines, list)
        assert len(lines) == 0

    def test_no_started_event_still_works(self):
        """In-progress runs might not have a started event yet."""
        events = [_activity_event("Working...")]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "Working..." in text

    def test_no_finished_event_shows_in_progress(self):
        """In-progress runs won't have a finished event."""
        events = [_started_event(), _activity_event("Thinking...")]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "claude-code" in text
        assert "in progress" in text

    def test_unknown_event_type_included(self):
        events = [{"ts": 1711000010.0, "type": "custom_thing", "data": "xyz"}]
        lines = format_log_view(events)
        assert len(lines) > 0

    def test_format_log_view_returns_summary_plus_events(self):
        """format_log_view returns summary header + event lines."""
        events = [
            _started_event(),
            _activity_event("Working hard"),
            _tokens_event(inp=500, out=100, cost=0.02),
            _finished_event(exit_code=0, cost=0.10),
        ]
        lines = format_log_view(events)
        # Summary lines + one line per event (4 events)
        assert len(lines) > 4, "Expected summary + event lines"
        # First line should be a dim separator
        assert "[dim]" in lines[0]
        # Event lines should include format_event output
        event_lines = [ln for ln in lines if "Started:" in ln or "Working hard" in ln]
        assert len(event_lines) >= 1


class TestFormatLogViewTokenDeltas:
    def test_tokens_event_shows_delta(self):
        """Token events should show delta from previous token event."""
        events = [
            _started_event(),
            _tool_use_event("Read", "foo.py", ts=1711000010.0),
            _tokens_event(inp=1000, out=200, ts=1711000015.0),
            _tool_use_event("Edit", "bar.py", ts=1711000020.0),
            _tokens_event(inp=3000, out=500, ts=1711000025.0),
            _finished_event(),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        # First delta: 1200 (1000+200)
        assert "+1,200" in text
        # Second delta: 2300 (3000+500 - 1000 - 200)
        assert "+2,300" in text

    def test_first_tokens_event_shows_full_count_as_delta(self):
        """First token event has no previous — delta equals the total."""
        events = [
            _started_event(),
            _tokens_event(inp=500, out=100, ts=1711000015.0),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "+600" in text

    def test_same_cumulative_shows_zero_delta(self):
        """Two identical cumulative token events show +0 delta."""
        events = [
            _started_event(),
            _tokens_event(inp=1000, out=200, ts=1711000015.0),
            _tokens_event(inp=1000, out=200, ts=1711000020.0),
        ]
        lines = format_log_view(events)
        text = "\n".join(lines)
        assert "+0 tokens" in text


class TestFormatEvent:
    """format_event should produce Rich markup for each event type."""

    def test_started_event_contains_started_and_runner_model(self):
        ev = _started_event()
        result = format_event(ev)
        assert "Started:" in result
        assert "claude-code" in result
        assert "opus-4" in result
        assert f"[{ATTESTED}]" in result

    def test_finished_event_success(self):
        ev = _finished_event(exit_code=0, cost=0.12)
        result = format_event(ev)
        assert "Finished" in result
        assert "exit 0" in result
        assert f"[{ATTESTED}]" in result
        assert "$0.12" in result

    def test_finished_event_failure(self):
        ev = _finished_event(exit_code=1, cost=0.50)
        result = format_event(ev)
        assert "Finished" in result
        assert "exit 1" in result
        assert f"[{JUDGMENT}]" in result
        assert "$0.50" in result

    def test_activity_event(self):
        ev = _activity_event("Analyzing codebase")
        result = format_event(ev)
        assert "Analyzing codebase" in result

    def test_tool_use_event(self):
        ev = _tool_use_event(tool="Edit", target="src/app.py")
        result = format_event(ev)
        assert "Tool:" in result
        assert f"[{MACHINE}]" in result
        assert "Edit" in result
        assert "src/app.py" in result

    def test_tokens_event(self):
        ev = _tokens_event(inp=5000, out=1000, cost=0.25)
        result = format_event(ev)
        assert "tokens:" in result
        assert "6,000" in result  # 5000 + 1000
        assert "$0.25" in result
        assert f"[{MONEY}]" in result

    def test_error_event(self):
        ev = {"ts": 1711000015.0, "type": "error", "msg": "Connection timeout"}
        result = format_event(ev)
        assert "Error:" in result
        assert "Connection timeout" in result
        assert f"[{JUDGMENT}]" in result

    def test_unknown_event_type(self):
        ev = {"ts": 1711000010.0, "type": "custom_metric", "value": 42}
        result = format_event(ev)
        assert "custom_metric" in result

    def test_event_with_timestamp_shows_time(self):
        """Events with a timestamp should show a formatted time prefix."""
        ev = _started_event(ts=1711000000.0)
        result = format_event(ev)
        # Should contain HH:MM:SS format
        assert "[dim]" in result  # timestamp is wrapped in dim

    def test_event_without_timestamp(self):
        """Events with ts=0 should not show a time prefix."""
        ev = {"ts": 0, "type": "activity", "msg": "Hello"}
        result = format_event(ev)
        assert "Hello" in result


class TestFormatEventSuppression:
    """Internal event types should be suppressed (return None)."""

    def test_pid_event_returns_none(self):
        ev = {"ts": 1711000015.0, "type": "pid", "pid": 25342}
        assert format_event(ev) is None

    def test_known_event_types_not_suppressed(self):
        """Activity, tool_use, etc. should still return strings."""
        ev = _activity_event("Hello")
        result = format_event(ev)
        assert result is not None
        assert "Hello" in result


class TestFmtDuration:
    """fmt_duration should format seconds into human-readable strings."""

    def test_seconds_only(self):
        assert fmt_duration(45) == "45s"

    def test_zero_seconds(self):
        assert fmt_duration(0) == "0s"

    def test_one_second(self):
        assert fmt_duration(1) == "1s"

    def test_59_seconds(self):
        assert fmt_duration(59) == "59s"

    def test_exact_one_minute(self):
        assert fmt_duration(60) == "1m"

    def test_minutes_and_seconds(self):
        assert fmt_duration(90) == "1m 30s"

    def test_many_minutes(self):
        assert fmt_duration(618) == "10m 18s"

    def test_exact_one_hour(self):
        assert fmt_duration(3600) == "1h"

    def test_hours_and_minutes(self):
        assert fmt_duration(3660) == "1h 1m"

    def test_large_duration(self):
        assert fmt_duration(7200) == "2h"

    def test_hours_minutes_truncates_seconds(self):
        """Seconds should be dropped when duration is >= 1 hour."""
        result = fmt_duration(3661)
        assert result == "1h 1m"  # 1h 1m, seconds ignored
