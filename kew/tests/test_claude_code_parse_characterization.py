"""Golden/snapshot characterization tests for ClaudeCodeRunner._parse_assistant_event.

Pins the exact parsed output for representative stream-JSON event payloads.
Source function is NOT modified — these tests form a regression net only.

The plain-string message shape (truncation, the 200-char boundary) and the
dict-with-string-content shape are pinned at this same layer by
``tests/test_claude_code_runner.py::TestClaudeCodeActivityEvents``; the
near-copies that re-pinned them here were removed in #2343, and the remaining
per-shape truncation copies collapsed into one parametrized test.
"""

from __future__ import annotations

import json

import pytest

from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.protocol import StreamEvent

_LONG_TEXT = "x" * 250


def _parse(event_dict: dict) -> tuple[str, list[StreamEvent]]:
    """Serialize event_dict to a JSON line, call _parse_assistant_event, return both."""
    line = json.dumps(event_dict)
    runner = ClaudeCodeRunner()
    events = runner._parse_assistant_event(line, event_dict)
    return line, events


class TestStringMessage:
    """message is a plain string (non-dict, non-list)."""

    def test_simple_text(self):
        msg = "I'll fix the bug in config.py"
        line, events = _parse({"type": "assistant", "message": msg})
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message=msg,
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]

    def test_empty_string_produces_no_events(self):
        _, events = _parse({"type": "assistant", "message": ""})
        assert events == []

    def test_whitespace_only_string_is_not_stripped(self):
        # String path does NOT strip — whitespace-only strings produce activity events.
        line, events = _parse({"type": "assistant", "message": "   \n  "})
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="   \n  ",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]

    def test_numeric_message_coerced_to_str(self):
        # raw_msg is not a dict but also not a str — str() is called on it
        line, events = _parse({"type": "assistant", "message": 42})
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="42",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]


class TestListMessage:
    """message is a list (legacy format) — each block processed individually."""

    def test_empty_string_item_skipped(self):
        _, events = _parse({"type": "assistant", "message": ["", "  "]})
        assert events == []

    def test_tool_use_dict_block(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": [
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/config.py",
                            "old_string": "x",
                            "new_string": "y",
                        },
                    }
                ],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Edit",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "src/config.py"},
            )
        ]

    def test_tool_use_with_command_target(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git status"},
                    }
                ],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Bash",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "git status"},
            )
        ]

    def test_tool_use_no_name_uses_empty_string(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": [{"type": "tool_use", "input": {"file_path": "x.py"}}],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "x.py"},
            )
        ]

    def test_thinking_block_suppressed(self):
        _, events = _parse(
            {
                "type": "assistant",
                "message": [{"type": "thinking", "thinking": "let me reason..."}],
            }
        )
        assert events == []

    def test_text_dict_block_uses_text_field(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": [{"type": "text", "text": "Here is the result"}],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="Here is the result",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]

    def test_dict_block_falls_back_to_content_field(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": [{"content": "content field text"}],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="content field text",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]

    def test_dict_block_empty_text_and_content_skipped(self):
        _, events = _parse(
            {"type": "assistant", "message": [{"type": "text", "text": ""}]}
        )
        assert events == []

    def test_mixed_list_string_tool_use_thinking_text(self):
        """Representative full list: string + tool_use + thinking + text block."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": [
                    "Starting task",
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "main.py"},
                    },
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "text", "text": "Task complete"},
                ],
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="Starting task",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Read",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "main.py"},
            ),
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="Task complete",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
        ]

    def test_list_multiple_strings(self):
        line, events = _parse({"type": "assistant", "message": ["first", "second"]})
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="first",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="second",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
        ]


class TestDictMessage:
    """message is a dict — extract content blocks + usage."""

    def test_content_list_text_block(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Working on it..."}]},
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="Working on it...",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            )
        ]

    def test_content_list_tool_use_block(self):
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "out.txt"},
                        }
                    ]
                },
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Write",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "out.txt"},
            )
        ]

    def test_content_list_thinking_only_suppressed_produces_no_events(self):
        # thinking blocks in a content list are suppressed; the list fallback is
        # skipped when content is a list, so no event is emitted.
        _, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "thinking", "thinking": "let me think..."}]
                },
            }
        )
        assert events == []

    def test_usage_all_fields(self):
        """Full token usage dict produces usage + step events."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [],
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 200,
                        "output_tokens": 50,
                    },
                },
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="usage",
                message="",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=500,
                cache_read_tokens=200,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="step",
                message="",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=500,
                cache_read_tokens=200,
                tool_uses=0,
                metadata={"cost_usd": None},
            ),
        ]

    def test_usage_no_cache_fields(self):
        """Usage without cache fields: cache tokens default to 0."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {"input_tokens": 42, "output_tokens": 10},
                },
            }
        )
        usage_ev = next(e for e in events if e.event_type == "usage")
        assert usage_ev == StreamEvent(
            raw_line=line,
            event_type="usage",
            message="",
            input_tokens=42,
            output_tokens=10,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            tool_uses=0,
            metadata={},
        )

    def test_usage_all_null_fields_no_usage_event(self):
        # The guard is `any(usage.get(k) is not None ...)`. When all are None,
        # none are non-None → condition is False → no usage/step events emitted.
        _, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {
                        "input_tokens": None,
                        "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": None,
                        "output_tokens": None,
                    },
                },
            }
        )
        assert events == []

    def test_content_plus_usage_produces_activity_usage_step(self):
        """The real Claude Code format: content blocks + usage in one message."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Here is the fix."}],
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 200,
                        "output_tokens": 50,
                    },
                },
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="Here is the fix.",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="usage",
                message="",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=500,
                cache_read_tokens=200,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="step",
                message="",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=500,
                cache_read_tokens=200,
                tool_uses=0,
                metadata={"cost_usd": None},
            ),
        ]

    def test_empty_dict_no_content_no_usage_returns_empty(self):
        """Dict with neither content nor usage produces no events."""
        _, events = _parse({"type": "assistant", "message": {}})
        assert events == []

    def test_no_message_key_produces_no_events(self):
        """Missing message key → raw_msg is empty string → no events."""
        _, events = _parse({"type": "assistant"})
        assert events == []

    def test_content_list_non_dict_blocks_only_produces_no_events(self):
        # Non-dict items are skipped by the content loop; the list fallback is
        # not triggered when content is a list, so no event is emitted.
        _, events = _parse(
            {
                "type": "assistant",
                "message": {"content": ["this is a string block", 42]},
            }
        )
        assert events == []

    def test_tool_use_path_field_as_target(self):
        """tool_use input with 'path' key uses it as target."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"path": "docs/README.md"},
                        }
                    ]
                },
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Read",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "docs/README.md"},
            )
        ]

    def test_content_list_with_tool_use_and_thinking_and_text(self):
        """Representative multi-block content: tool_use + thinking + text."""
        line, events = _parse(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        },
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "All tests passed."},
                    ],
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                    },
                },
            }
        )
        assert events == [
            StreamEvent(
                raw_line=line,
                event_type="tool_use",
                message="Bash",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"target": "pytest"},
            ),
            StreamEvent(
                raw_line=line,
                event_type="activity",
                message="All tests passed.",
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="usage",
                message="",
                input_tokens=200,
                output_tokens=80,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={},
            ),
            StreamEvent(
                raw_line=line,
                event_type="step",
                message="",
                input_tokens=200,
                output_tokens=80,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                tool_uses=0,
                metadata={"cost_usd": None},
            ),
        ]


@pytest.mark.parametrize(
    "message",
    [
        pytest.param([_LONG_TEXT], id="list-string-item"),
        pytest.param({"content": _LONG_TEXT}, id="dict-content-string"),
        pytest.param(
            {"content": [{"type": "text", "text": _LONG_TEXT}]},
            id="dict-content-text-block",
        ),
    ],
)
def test_every_message_shape_truncates_at_200_with_an_ellipsis(message):
    """One rule at three call sites: over 200 chars is cut to 200 + "...".

    The plain-string shape is pinned at the same layer by
    tests/test_claude_code_runner.py::TestClaudeCodeActivityEvents
    ::test_long_message_truncated_at_200_chars (#2343).
    """
    line, events = _parse({"type": "assistant", "message": message})
    assert events == [
        StreamEvent(
            raw_line=line,
            event_type="activity",
            message="x" * 200 + "...",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            tool_uses=0,
            metadata={},
        )
    ]
