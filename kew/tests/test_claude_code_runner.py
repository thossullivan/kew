"""Tests for the Claude Code runner implementation."""

import json

import pytest

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.inference_transport import resolve_inference_transport
from kew.runners.claude_code import ClaudeCodeRunner


class TestClaudeCodeRunnerName:
    def test_name(self):
        runner = ClaudeCodeRunner()
        assert runner.name == "claude-code"


class TestClaudeCodeRunnerBuildCommand:
    def test_default_command(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        env: dict[str, str] = {}
        cmd = runner.build_command(prompt="hello", config=config, env=env)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "-p" in cmd
        assert "hello" in cmd

    def test_skip_permissions_false(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.claude_code.skip_permissions = False
        env: dict[str, str] = {}
        cmd = runner.build_command(prompt="hello", config=config, env=env)
        assert "--dangerously-skip-permissions" not in cmd

    def test_includes_budget(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.max_budget_usd = 5.0
        env: dict[str, str] = {}
        cmd = runner.build_command(prompt="hello", config=config, env=env)
        assert "--max-budget-usd" in cmd
        assert "5.0" in cmd

    def test_includes_model(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.model = "claude-sonnet-4-6"
        env: dict[str, str] = {}
        cmd = runner.build_command(prompt="hello", config=config, env=env)
        assert "--model" in cmd
        assert "claude-sonnet-4-6" in cmd

    def test_injects_autocompact_env(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.claude_code.autocompact_pct = 50
        env: dict[str, str] = {}
        runner.build_command(prompt="hello", config=config, env=env)
        assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "50"

    def test_switchyard_uses_anthropic_messages_child_local_override(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig(
            inference_transport=InferenceTransportConfig(
                mode="switchyard",
                switchyard=SwitchyardConfig(
                    endpoint="http://127.0.0.1:4000",
                    route="switchyard/passthrough",
                    expected_version="pinned-version",
                ),
            )
        )
        env = {"EXISTING": "unchanged"}

        resolved = resolve_inference_transport(config, runner, "claude-sonnet-4-6")
        cmd = runner.build_command(
            prompt="hello", config=config, env=env, model=resolved.model
        )

        assert resolved.api_format == "anthropic-messages"
        assert env == {
            "EXISTING": "unchanged",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
        }
        assert cmd[cmd.index("--model") + 1] == "switchyard/passthrough"

    def test_direct_mode_does_not_override_anthropic_endpoint(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        env = {"ANTHROPIC_BASE_URL": "https://operator.example"}

        runner.build_command(prompt="hello", config=config, env=env)

        assert env["ANTHROPIC_BASE_URL"] == "https://operator.example"


class TestClaudeCodeRunnerBuildResumeCommand:
    def test_resume_command(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        env: dict[str, str] = {}
        cmd = runner.build_resume_command(
            session_id="session-abc", prompt="original", config=config, env=env
        )
        assert "--resume" in cmd
        assert "session-abc" in cmd
        assert "-p" in cmd
        # Resume prompt should mention "continue", not the original prompt
        p_index = cmd.index("-p")
        resume_prompt = cmd[p_index + 1]
        assert "continue" in resume_prompt.lower()
        assert "original" not in resume_prompt

    def test_switchyard_resume_keeps_endpoint_and_route(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig(
            inference_transport=InferenceTransportConfig(
                mode="switchyard",
                switchyard=SwitchyardConfig(
                    endpoint="http://127.0.0.1:4000",
                    route="switchyard/passthrough",
                    expected_version="pinned-version",
                ),
            )
        )
        env: dict[str, str] = {}

        cmd = runner.build_resume_command(
            session_id="session-abc",
            prompt="original",
            config=config,
            env=env,
            model="switchyard/passthrough",
        )

        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
        assert cmd[cmd.index("--model") + 1] == "switchyard/passthrough"


class TestClaudeCodeRunnerParseLine:
    def test_system_usage_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "task_notification",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "tool_uses": 3,
                },
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "usage"
        assert event.input_tokens == 100
        assert event.output_tokens == 50
        assert event.tool_uses == 3

    def test_compact_boundary_emits_compaction_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"trigger": "auto", "pre_tokens": 120000},
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "compaction"
        assert events[0].metadata["trigger"] == "auto"

    def test_compact_boundary_without_metadata(self):
        runner = ClaudeCodeRunner()
        line = json.dumps({"type": "system", "subtype": "compact_boundary"})
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "compaction"
        assert events[0].metadata["trigger"] == ""

    def test_result_line(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "result",
                "session_id": "abc-123",
                "subtype": "success",
                "stop_reason": "end_turn",
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "result"
        assert event.metadata.get("session_id") == "abc-123"

    def test_result_line_captures_total_cost_usd(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "result",
                "session_id": "abc-123",
                "subtype": "success",
                "stop_reason": "end_turn",
                "total_cost_usd": 7.13,
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.metadata.get("total_cost_usd") == 7.13

    def test_result_line_without_total_cost_usd(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "result",
                "session_id": "abc-123",
                "subtype": "success",
                "stop_reason": "end_turn",
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.metadata.get("total_cost_usd") is None

    def test_non_json_returns_none(self):
        runner = ClaudeCodeRunner()
        assert runner.parse_line("not json at all") == []

    def test_empty_line_returns_none(self):
        runner = ClaudeCodeRunner()
        assert runner.parse_line("") == []

    def test_init_event_includes_session_id(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "opus-4",
                "session_id": "sess-early",
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "init"
        assert events[0].metadata["session_id"] == "sess-early"
        assert events[0].metadata["model"] == "opus-4"


class TestClaudeCodeRunnerClassifyResult:
    def test_api_error_is_transient(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        last_output = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": (
                    '{"type":"error","error":'
                    '{"type":"api_error",'
                    '"message":"Internal server error"}}'
                ),
            }
        )
        rc = runner.classify_result(exit_code=1, last_output=last_output, config=config)
        assert rc.category == "transient"

    def test_exit_0_is_success(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=0, last_output=None, config=config)
        assert rc.category == ""

    def test_timeout_is_transient(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=124, last_output=None, config=config)
        assert rc.category == "transient"

    def test_budget_exceeded(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        last_output = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "session_id": "abc-123",
            }
        )
        rc = runner.classify_result(exit_code=0, last_output=last_output, config=config)
        assert rc.category == "budget_exceeded"
        assert rc.session_id == "abc-123"

    def test_exit_code_not_in_retriable_is_permanent(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=2, last_output=None, config=config)
        assert rc.category == "permanent"

    def test_exit_143_is_watchdog_kill(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=143, last_output=None, config=config)
        assert rc.category == "watchdog_kill"

    def test_exit_137_is_watchdog_kill(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=137, last_output=None, config=config)
        assert rc.category == "watchdog_kill"

    def test_extracts_session_id(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        last_output = json.dumps(
            {
                "type": "result",
                "session_id": "sess-xyz",
                "subtype": "success",
                "stop_reason": "end_turn",
            }
        )
        rc = runner.classify_result(exit_code=0, last_output=last_output, config=config)
        assert rc.session_id == "sess-xyz"
        assert rc.subtype == "success"
        assert rc.stop_reason == "end_turn"

    # --- #642: cost-aware budget-kill classification -----------------------
    # A --max-budget-usd termination can surface without a budget subtype
    # (plain nonzero exit, or SIGTERM-143). Whenever the runner-reported cost
    # reaches the cap, it must classify as a budget kill regardless of
    # subtype/exit code. Uses run-#5's cap ($0.15) / final cost ($0.53) shape.

    def test_cost_at_cap_no_subtype_is_budget_exceeded(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.max_budget_usd = 0.15
        last_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",  # deliberately NOT a budget subtype
                "total_cost_usd": 0.53,
                "session_id": "sess-kill",
            }
        )
        rc = runner.classify_result(exit_code=1, last_output=last_output, config=config)
        assert rc.category == "budget_exceeded"

    def test_cost_over_cap_exit_143_is_budget_exceeded(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.max_budget_usd = 0.15
        last_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.53,
            }
        )
        rc = runner.classify_result(
            exit_code=143, last_output=last_output, config=config
        )
        assert rc.category == "budget_exceeded"

    def test_exit_143_with_cost_below_cap_stays_watchdog_kill(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.max_budget_usd = 0.15
        last_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.02,  # well under the cap: operator kill
            }
        )
        rc = runner.classify_result(
            exit_code=143, last_output=last_output, config=config
        )
        assert rc.category == "watchdog_kill"

    def test_cost_below_cap_is_not_budget(self):
        runner = ClaudeCodeRunner()
        config = DispatchConfig()
        config.agent.max_budget_usd = 25.0
        last_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 0.53,
            }
        )
        rc = runner.classify_result(exit_code=1, last_output=last_output, config=config)
        assert rc.category != "budget_exceeded"


class TestClaudeCodeRunnerResume:
    def test_supports_resume(self):
        assert ClaudeCodeRunner().supports_resume() is True


class TestClaudeCodeActivityEvents:
    def test_assistant_message_returns_activity_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {"type": "assistant", "message": "I'll fix the bug in config.py"}
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "activity"
        assert "fix the bug" in event.message

    def test_tool_use_returns_tool_use_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "tool_use",
                "tool": "Edit",
                "input": {
                    "file_path": "src/config.py",
                    "old_string": "x",
                    "new_string": "y",
                },
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "tool_use"
        assert event.message == "Edit"
        assert event.metadata.get("target") == "src/config.py"

    def test_tool_use_read(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "tool_use",
                "tool": "Read",
                "input": {"file_path": "src/main.py"},
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "tool_use"
        assert event.message == "Read"
        assert event.metadata.get("target") == "src/main.py"

    def test_usage_event_still_works(self):
        """Existing usage parsing is unchanged."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.event_type == "usage"
        assert event.input_tokens == 80

    def test_unknown_type_returns_none(self):
        runner = ClaudeCodeRunner()
        line = json.dumps({"type": "unknown_type"})
        assert runner.parse_line(line) == []

    def test_long_message_truncated_at_200_chars(self):
        """Messages longer than 200 chars are truncated with '...'."""
        runner = ClaudeCodeRunner()
        long_msg = "x" * 250
        line = json.dumps({"type": "assistant", "message": long_msg})
        events = runner.parse_line(line)
        event = events[0]
        assert len(event.message) == 203  # 200 chars + "..."
        assert event.message.endswith("...")

    def test_exactly_200_char_message_not_truncated(self):
        """Messages exactly 200 chars are not truncated."""
        runner = ClaudeCodeRunner()
        msg = "x" * 200
        line = json.dumps({"type": "assistant", "message": msg})
        events = runner.parse_line(line)
        event = events[0]
        assert len(event.message) == 200
        assert not event.message.endswith("...")

    def test_dict_message_extracts_content(self):
        """When message is a dict, extract the 'content' key."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": "Hello from dict"},
            }
        )
        events = runner.parse_line(line)
        event = events[0]
        assert event.message == "Hello from dict"
        assert event.event_type == "activity"

    def test_mixed_list_blocks_produce_separate_events(self):
        """List content with string and dict blocks produces per-block events."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": [
                    "plain text",
                    {"type": "text", "text": "block text"},
                    {"content": "content text"},
                ],
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 3
        assert events[0].message == "plain text"
        assert events[1].message == "block text"
        assert events[2].message == "content text"
        assert all(e.event_type == "activity" for e in events)

    def test_list_with_tool_use_block(self):
        """List content with tool_use blocks emits tool_use events."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "x.py"},
                    },
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "done"},
                ],
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 2  # tool_use + text, thinking skipped
        assert events[0].event_type == "tool_use"
        assert events[0].message == "Read"
        assert events[1].event_type == "activity"
        assert events[1].message == "done"


class TestClaudeCodeAssistantUsageEvents:
    """Token usage from assistant events (real Claude Code stream-json format)."""

    def test_assistant_message_usage_returns_usage_event(self):
        """Assistant events with message.usage produce activity + usage + step."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Working on it..."}],
                    "usage": {
                        "input_tokens": 100,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 200,
                        "output_tokens": 50,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 3
        assert events[0].event_type == "activity"
        usage_ev = next(e for e in events if e.event_type == "usage")
        # Cache tokens are split out, not folded into input_tokens (#255).
        assert usage_ev.input_tokens == 100
        assert usage_ev.cache_creation_tokens == 500
        assert usage_ev.cache_read_tokens == 200
        assert usage_ev.output_tokens == 50
        # New: a step event closes the model-response unit.
        step_ev = next(e for e in events if e.event_type == "step")
        assert step_ev.input_tokens == 100
        assert step_ev.cache_creation_tokens == 500
        assert step_ev.cache_read_tokens == 200
        assert step_ev.output_tokens == 50

    def test_assistant_message_usage_no_cache_fields(self):
        """Usage without cache fields should still work."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {
                        "input_tokens": 42,
                        "output_tokens": 10,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].event_type == "usage"
        assert events[0].input_tokens == 42
        assert events[0].output_tokens == 10

    def test_assistant_cache_only_tokens_still_emits_usage(self):
        """Cache-only usage (input_tokens=0) should still emit a usage event."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 200,
                        "output_tokens": 0,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].event_type == "usage"
        assert events[0].input_tokens == 0
        assert events[0].cache_creation_tokens == 500
        assert events[0].cache_read_tokens == 200
        assert events[0].output_tokens == 0

    def test_assistant_without_usage_returns_activity(self):
        """Assistant events without usage should still return activity events."""
        runner = ClaudeCodeRunner()
        line = json.dumps({"type": "assistant", "message": "Just some text, no usage"})
        events = runner.parse_line(line)
        assert events[0].event_type == "activity"

    def test_assistant_dict_message_without_usage_returns_activity(self):
        """Dict message without usage key returns activity."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": "Hello from dict"},
            }
        )
        events = runner.parse_line(line)
        assert events[0].event_type == "activity"
        assert events[0].message == "Hello from dict"

    def test_assistant_usage_null_token_fields_normalized_to_zero(self):
        """Explicit null token fields are coalesced to zero, not crash."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {
                        "input_tokens": None,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 200,
                        "output_tokens": None,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].event_type == "usage"
        assert events[0].input_tokens == 0
        assert events[0].cache_creation_tokens == 500
        assert events[0].cache_read_tokens == 200
        assert events[0].output_tokens == 0

    def test_assistant_with_usage_and_tool_use_content_emits_both(self):
        """Assistant event with usage AND tool_use content blocks emits both."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "src/foo.py"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/bar.py"},
                        },
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 4
        assert events[0].event_type == "tool_use"
        assert events[0].message == "Read"
        assert events[0].metadata.get("target") == "src/foo.py"
        assert events[1].event_type == "tool_use"
        assert events[1].message == "Edit"
        assert events[1].metadata.get("target") == "src/bar.py"
        assert events[2].event_type == "usage"
        assert events[2].input_tokens == 100
        assert events[2].output_tokens == 50
        assert events[3].event_type == "step"

    def test_assistant_with_usage_and_text_content_emits_both(self):
        """Assistant event with usage AND text content emits activity + usage."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "I'll fix the bug"}],
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 30,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 3
        assert events[0].event_type == "activity"
        assert "fix the bug" in events[0].message
        assert events[1].event_type == "usage"
        assert events[2].event_type == "step"

    def test_assistant_with_usage_and_thinking_only_emits_usage_only(self):
        """Thinking blocks are suppressed — only usage event emitted."""
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "thinking", "thinking": "Let me consider..."}],
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 40,
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 2
        assert events[0].event_type == "usage"
        assert events[1].event_type == "step"


class TestClaudeCodeInitEvent:
    def test_system_init_returns_init_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-opus-4-6[1m]",
                "cwd": "/some/path",
                "session_id": "abc-123",
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "init"
        assert events[0].metadata.get("model") == "claude-opus-4-6[1m]"

    def test_system_init_without_model(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "cwd": "/some/path",
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "init"
        assert events[0].metadata.get("model") == ""


class TestClaudeCodeModelOverride:
    def _make_config(self, model="claude-sonnet-4"):
        from kew.config import AgentConfig, DispatchConfig

        return DispatchConfig(agent=AgentConfig(model=model))

    def test_model_override_in_build_command(self):
        """When model param is set, it overrides config model."""
        runner = ClaudeCodeRunner()
        cmd = runner.build_command(
            prompt="fix bug",
            config=self._make_config(),
            env={},
            model="claude-opus-4",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-4"

    def test_empty_model_uses_config(self):
        """When model param is empty, falls back to config model."""
        runner = ClaudeCodeRunner()
        cmd = runner.build_command(
            prompt="fix bug",
            config=self._make_config(),
            env={},
            model="",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4"

    def test_model_override_in_resume_command(self):
        """Resume command also respects model override."""
        runner = ClaudeCodeRunner()
        cmd = runner.build_resume_command(
            session_id="sess-123",
            prompt="continue",
            config=self._make_config(),
            env={},
            model="claude-opus-4",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-4"


class TestClaudeCodeStepEvent:
    """A step event closes each model-response unit (one per usage-bearing turn)."""

    def test_no_step_without_usage(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {"type": "assistant", "message": {"content": "no usage here"}}
        )
        events = runner.parse_line(line)
        assert all(e.event_type != "step" for e in events)

    def test_exactly_one_step_per_usage_assistant_event(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        steps = [e for e in runner.parse_line(line) if e.event_type == "step"]
        assert len(steps) == 1
        assert steps[0].input_tokens == 10
        assert steps[0].output_tokens == 5

    def test_step_carries_cost_metadata_key(self):
        runner = ClaudeCodeRunner()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": "",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        step = next(e for e in runner.parse_line(line) if e.event_type == "step")
        # cost is best-effort; the key is always present (None when un-priced here,
        # since the parser has no model/pricing context — cost is computed downstream).
        assert "cost_usd" in step.metadata


def test_model_redaction_preserves_distinct_usage_entries(monkeypatch):
    import json

    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.protocol import RUN_TOTALS_KEY

    password = "s3cret" * 60
    secret = f"http://user:{password}@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    [event] = ClaudeCodeRunner().parse_line(
        json.dumps(
            {
                "type": "result",
                "modelUsage": {
                    secret: {"costUSD": 0.2, "inputTokens": 20},
                    password: {"costUSD": 0.1, "inputTokens": 10},
                },
            }
        )
    )
    totals = event.metadata[RUN_TOTALS_KEY]
    assert "s3cret" not in totals.model
    assert totals.input_tokens == 30
    assert totals.cost_usd == pytest.approx(0.3)
