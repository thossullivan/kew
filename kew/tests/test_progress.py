"""Tests for stream-json event parsing."""

import json
from io import StringIO
from unittest.mock import patch

from kew.progress import (
    display_event,
    parse_stream_event,
    summarize_tool_input,
)


class TestSummarizeToolInput:
    def test_string_input(self):
        assert summarize_tool_input("hello world") == "hello world"

    def test_dict_with_command(self):
        assert summarize_tool_input({"command": "git status"}) == "git status"

    def test_dict_with_pattern(self):
        assert summarize_tool_input({"pattern": "*.py"}) == "*.py"

    def test_truncates_long_string(self):
        long = "x" * 200
        result = summarize_tool_input(long)
        assert len(result) <= 80


class TestParseStreamEvent:
    def test_tool_use_event(self):
        event = {"type": "tool_use", "tool": "Bash", "input": {"command": "ls"}}
        parsed = parse_stream_event(json.dumps(event))
        assert parsed is not None
        assert parsed["type"] == "tool_use"

    def test_empty_line_returns_none(self):
        assert parse_stream_event("") is None
        assert parse_stream_event("   ") is None

    def test_malformed_json_returns_none(self):
        assert parse_stream_event("not json at all") is None

    def test_result_event(self):
        event = {"type": "result", "result": "Task completed successfully"}
        parsed = parse_stream_event(json.dumps(event))
        assert parsed is not None
        assert parsed["type"] == "result"

    def test_usage_in_system_event(self):
        event = {
            "type": "system",
            "subtype": "task_notification",
            "status": "completed",
            "summary": "Done",
            "usage": {"total_tokens": 5000, "tool_uses": 10},
        }
        parsed = parse_stream_event(json.dumps(event))
        assert parsed is not None


class TestDisplayEvent:
    """display_event surfaces tool_use and text from nested assistant content."""

    def _capture(self, event: dict) -> str:
        """Run display_event and capture stderr output."""
        buf = StringIO()
        with patch("sys.stderr", buf):
            display_event(event)
        return buf.getvalue()

    def test_assistant_tool_use_in_content_is_displayed(self):
        """Tool calls nested inside assistant.message.content should be shown."""
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "src/foo.py"},
                    }
                ]
            },
        }
        output = self._capture(event)
        assert "Read" in output
        assert "src/foo.py" in output

    def test_assistant_text_content_is_displayed(self):
        """Text blocks in assistant.message.content should be shown."""
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Now let me implement the fix."}]
            },
        }
        output = self._capture(event)
        assert "Now let me implement the fix." in output

    def test_assistant_thinking_blocks_are_suppressed(self):
        """Thinking blocks should not appear in terminal output."""
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me reason about this privately...",
                    }
                ]
            },
        }
        output = self._capture(event)
        assert "Let me reason about this privately" not in output

    def test_assistant_mixed_content_shows_tool_and_text(self):
        """Both text and tool_use blocks should appear; thinking should not."""
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private thought"},
                    {"type": "text", "text": "Running the tests now."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                ]
            },
        }
        output = self._capture(event)
        assert "private thought" not in output
        assert "Running the tests now." in output
        assert "Bash" in output
        assert "pytest" in output

    def test_assistant_with_string_message_still_works(self):
        """Old-style assistant events with string message field should display."""
        event = {"type": "assistant", "message": "Task complete"}
        output = self._capture(event)
        assert "Task complete" in output

    def test_assistant_empty_content_produces_no_output(self):
        """Empty content list should produce no output."""
        event = {"type": "assistant", "message": {"content": []}}
        output = self._capture(event)
        assert output == ""

    def test_assistant_text_non_string_does_not_crash(self):
        """Non-string text field should be coerced, not raise AttributeError."""
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": 42}]},
        }
        output = self._capture(event)
        assert "42" in output

    def test_assistant_text_is_truncated_at_120_chars(self):
        """Long text messages should be truncated to exactly 120 chars."""
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * 300}]},
        }
        output = self._capture(event)
        assert "x" * 120 in output
        assert "x" * 121 not in output


def test_emit_reenters_from_a_signal_handler_without_deadlock(capsys):
    """#2110 round 3: a SIGINT handler that prints while the main thread is
    inside _emit re-enters the echo lock on the same thread. A non-reentrant
    lock would hang here forever."""
    import threading

    from kew import progress

    done = threading.Event()

    def reenter() -> None:
        with progress._echo_lock:  # the interrupted _emit holds the lock
            progress._emit("from the handler")
        done.set()

    worker = threading.Thread(target=reenter, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert done.is_set(), "_emit deadlocked when re-entered on the same thread"
    assert "from the handler" in capsys.readouterr().err
