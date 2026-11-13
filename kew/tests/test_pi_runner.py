"""Tests for the Pi runner implementation.

Pi (https://pi.dev, @earendil-works/pi-coding-agent) is a minimal, multi-provider
terminal coding agent. This runner shells out to `pi --mode json -p <prompt>`,
which emits the session as a stream of JSON event lines.

Event schema reference: the installed package's `docs/json.md` plus the
`AgentSessionEvent` / `AssistantMessage` / `Usage` types in
`@earendil-works/pi-ai`.
"""

import json
import uuid

from kew.config import DispatchConfig
from kew.runners.pi import PiRunner


def _ndjson(payload: dict) -> str:
    """Serialize a dict as a single NDJSON line."""
    return json.dumps(payload)


def _assistant_message_end(
    *,
    text: str = "",
    stop_reason: str = "stop",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    cost_total: float = 0.0,
    error_message: str | None = None,
    content: list | None = None,
) -> str:
    """Build a `message_end` event carrying an assistant message."""
    msg: dict = {
        "role": "assistant",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "model": "anthropic/claude-sonnet-4-6",
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
            "cost": {
                "input": 0.0,
                "output": 0.0,
                "cacheRead": 0.0,
                "cacheWrite": 0.0,
                "total": cost_total,
            },
        },
        "stopReason": stop_reason,
        "timestamp": 1700000000000,
    }
    if error_message is not None:
        msg["errorMessage"] = error_message
    return _ndjson({"type": "message_end", "message": msg})


class TestPiRunnerName:
    def test_name(self):
        assert PiRunner().name == "pi"

    def test_loop_style_is_native(self):
        """pi is a first-class native loop runner (resumable, agentic, emits steps)."""
        from kew.runners.protocol import loop_style_to_drive_mode

        runner = PiRunner()
        assert runner.loop_style == "native"
        assert loop_style_to_drive_mode(runner.loop_style) == "delegated"


class TestPiRunnerBuildCommand:
    def test_default_command_shape(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.agent.model = "anthropic/claude-sonnet-4-6"
        cmd = runner.build_command(prompt="fix the bug", config=config, env={})
        assert cmd[0] == "pi"
        assert "--mode" in cmd
        assert cmd[cmd.index("--mode") + 1] == "json"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-6"
        # Prompt follows -p as a positional and is the final argument.
        assert "-p" in cmd
        assert cmd[-1] == "fix the bug"

    def test_no_model_flag_when_model_empty(self):
        runner = PiRunner()
        config = DispatchConfig()  # model defaults to ""
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert "--model" not in cmd

    def test_model_param_overrides_config(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.agent.model = "anthropic/claude-sonnet-4-6"
        cmd = runner.build_command(
            prompt="x", config=config, env={}, model="openai/gpt-5.1"
        )
        assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.1"

    def test_session_id_is_passed_and_is_a_uuid(self):
        runner = PiRunner()
        config = DispatchConfig()
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert "--session-id" in cmd
        sid = cmd[cmd.index("--session-id") + 1]
        # Must be a syntactically valid UUID so resume is deterministic.
        uuid.UUID(sid)

    def test_session_id_is_stable_within_an_instance(self):
        """build_command must reuse the same session id it generated, so that
        classify_result can report the id the run actually used."""
        runner = PiRunner()
        config = DispatchConfig()
        cmd1 = runner.build_command(prompt="a", config=config, env={})
        cmd2 = runner.build_command(prompt="b", config=config, env={})
        sid1 = cmd1[cmd1.index("--session-id") + 1]
        sid2 = cmd2[cmd2.index("--session-id") + 1]
        assert sid1 == sid2

    def test_distinct_instances_get_distinct_session_ids(self):
        config = DispatchConfig()
        a = PiRunner().build_command(prompt="x", config=config, env={})
        b = PiRunner().build_command(prompt="x", config=config, env={})
        assert a[a.index("--session-id") + 1] != b[b.index("--session-id") + 1]

    def test_explicit_session_id_used(self):
        runner = PiRunner(session_id="fixed-123")
        config = DispatchConfig()
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert cmd[cmd.index("--session-id") + 1] == "fixed-123"

    def test_extra_args_appended(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.agent.pi.extra_args = ["--thinking", "high"]
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert "--thinking" in cmd
        assert cmd[cmd.index("--thinking") + 1] == "high"

    def test_env_not_mutated(self):
        runner = PiRunner()
        config = DispatchConfig()
        env: dict[str, str] = {}
        runner.build_command(prompt="x", config=config, env=env)
        assert env == {}


class TestPiRunnerParseInit:
    def test_session_header_emits_init_with_session_id(self):
        runner = PiRunner()
        line = _ndjson(
            {
                "type": "session",
                "version": 3,
                "id": "11111111-2222-3333-4444-555555555555",
                "timestamp": "2026-06-17T00:00:00Z",
                "cwd": "/work",
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "init"
        assert events[0].metadata["session_id"] == (
            "11111111-2222-3333-4444-555555555555"
        )


class TestPiRunnerParseAssistant:
    def test_assistant_text_emits_activity_and_usage(self):
        runner = PiRunner()
        line = _assistant_message_end(
            text="Reading config.py",
            stop_reason="toolUse",
            input_tokens=100,
            output_tokens=50,
            cache_read=200,
            cache_write=30,
        )
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "activity" in kinds
        assert "usage" in kinds
        activity = next(e for e in events if e.event_type == "activity")
        assert activity.message == "Reading config.py"
        usage = next(e for e in events if e.event_type == "usage")
        # input billed = input + cacheRead + cacheWrite
        assert usage.input_tokens == 330
        assert usage.output_tokens == 50

    def test_tooluse_turn_emits_no_result(self):
        """A turn that stops to call tools (stopReason=toolUse) is not terminal."""
        runner = PiRunner()
        line = _assistant_message_end(text="", stop_reason="toolUse")
        events = runner.parse_line(line)
        assert all(e.event_type != "result" for e in events)

    def test_empty_text_emits_no_activity(self):
        runner = PiRunner()
        line = _assistant_message_end(text="", stop_reason="toolUse")
        assert all(e.event_type != "activity" for e in events_of(runner, line))

    def test_long_text_truncated(self):
        runner = PiRunner()
        line = _assistant_message_end(text="x" * 500, stop_reason="toolUse")
        activity = next(
            e for e in runner.parse_line(line) if e.event_type == "activity"
        )
        assert activity.message.endswith("...")
        assert len(activity.message) <= 203

    def test_terminal_stop_emits_result_with_session_and_cost(self):
        runner = PiRunner(session_id="sess-xyz")
        line = _assistant_message_end(
            text="All done.",
            stop_reason="stop",
            input_tokens=200,
            output_tokens=80,
            cost_total=0.0123,
        )
        events = runner.parse_line(line)
        result = next(e for e in events if e.event_type == "result")
        assert result.metadata["session_id"] == "sess-xyz"
        assert result.metadata["stop_reason"] == "stop"
        assert result.metadata["cost_usd"] == 0.0123

    def test_text_concatenated_across_multiple_blocks(self):
        runner = PiRunner()
        line = _assistant_message_end(
            stop_reason="stop",
            content=[
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
        )
        activity = next(
            e for e in runner.parse_line(line) if e.event_type == "activity"
        )
        # Thinking blocks are excluded; text blocks are joined.
        assert activity.message == "Hello world"

    def test_assistant_turn_emits_paired_step_event(self):
        """pi emits a 'step' paired with 'usage' so the loop accrues
        step_count/spent_usd (parity with claude-code)."""
        runner = PiRunner()
        line = _assistant_message_end(
            text="Reading config.py",
            stop_reason="toolUse",
            input_tokens=100,
            output_tokens=50,
            cache_read=200,
            cache_write=30,
        )
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "usage" in kinds
        assert "step" in kinds
        usage = next(e for e in events if e.event_type == "usage")
        step = next(e for e in events if e.event_type == "step")
        assert (
            step.input_tokens == usage.input_tokens
        )  # 330 (input + cacheRead + cacheWrite)
        assert step.output_tokens == usage.output_tokens

    def test_step_carries_native_per_message_cost(self):
        """The paired 'step' event carries pi's native per-message cost so the
        orchestrator sums billed truth, not the token estimate (#1082)."""
        runner = PiRunner()
        line = _assistant_message_end(
            text="patching",
            stop_reason="toolUse",
            input_tokens=100,
            output_tokens=50,
            cost_total=0.0041,
        )
        events = runner.parse_line(line)
        step = next(e for e in events if e.event_type == "step")
        assert step.metadata["cost_usd"] == 0.0041

    def test_step_cost_is_none_when_message_has_no_cost(self):
        """A message without a usage.cost block → step cost None (not 0.0) so
        the orchestrator falls back to a token estimate (#1082)."""
        runner = PiRunner()
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "model": "anthropic/claude-sonnet-4-6",
            "usage": {"input": 100, "output": 50, "totalTokens": 150},
            "stopReason": "toolUse",
        }
        line = _ndjson({"type": "message_end", "message": msg})
        events = runner.parse_line(line)
        step = next(e for e in events if e.event_type == "step")
        assert step.metadata["cost_usd"] is None

    def test_zero_token_turn_emits_usage_but_no_step(self):
        """A 0-token assistant turn still emits 'usage' but NOT 'step' — the
        `if input_tokens or output_tokens` gate keeps phantom empty turns out of
        step_count (parity with opencode's zero-token behavior)."""
        runner = PiRunner()
        # All token args default to 0 → the gate must suppress the step.
        line = _assistant_message_end(text="thinking", stop_reason="toolUse")
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "usage" in kinds
        assert "step" not in kinds


class TestPiRunnerParseToolUse:
    def test_tool_execution_start_emits_tool_use(self):
        runner = PiRunner()
        line = _ndjson(
            {
                "type": "tool_execution_start",
                "toolCallId": "tc_1",
                "toolName": "edit",
                "args": {"file_path": "src/config.py"},
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "tool_use"
        assert events[0].message == "edit"
        assert events[0].metadata == {"target": "src/config.py"}

    def test_tool_execution_start_with_command_target(self):
        runner = PiRunner()
        line = _ndjson(
            {
                "type": "tool_execution_start",
                "toolCallId": "tc_2",
                "toolName": "bash",
                "args": {"command": "pytest -q"},
            }
        )
        events = runner.parse_line(line)
        assert events[0].metadata == {"target": "pytest -q"}

    def test_tool_execution_end_emits_nothing(self):
        runner = PiRunner()
        line = _ndjson(
            {
                "type": "tool_execution_end",
                "toolCallId": "tc_1",
                "toolName": "edit",
                "result": {"ok": True},
                "isError": False,
            }
        )
        assert runner.parse_line(line) == []


class TestPiRunnerParseSilent:
    def test_lifecycle_events_emit_nothing(self):
        runner = PiRunner()
        for t in (
            "agent_start",
            "turn_start",
            "turn_end",
            "message_start",
            "message_update",
            "agent_end",
            "queue_update",
        ):
            assert runner.parse_line(_ndjson({"type": t})) == []

    def test_non_assistant_message_end_emits_nothing(self):
        runner = PiRunner()
        line = _ndjson(
            {"type": "message_end", "message": {"role": "user", "content": "hi"}}
        )
        assert runner.parse_line(line) == []

    def test_empty_line_emits_nothing(self):
        runner = PiRunner()
        assert runner.parse_line("") == []
        assert runner.parse_line("   ") == []

    def test_malformed_json_emits_nothing(self):
        runner = PiRunner()
        assert runner.parse_line("not json") == []
        assert runner.parse_line("{oops") == []

    def test_non_dict_json_emits_nothing(self):
        runner = PiRunner()
        assert runner.parse_line("[1,2,3]") == []
        assert runner.parse_line('"a string"') == []


class TestPiRunnerClassifyResult:
    def test_exit_zero_stop_is_success(self):
        runner = PiRunner(session_id="s1")
        config = DispatchConfig()
        last = _assistant_message_end(text="done", stop_reason="stop")
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.category == ""
        assert rc.stop_reason == "stop"
        # session id is the one the run was launched with
        assert rc.session_id == "s1"

    def test_session_id_reported_even_without_last_output(self):
        runner = PiRunner(session_id="s-known")
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=0, last_output=None, config=config)
        assert rc.session_id == "s-known"

    def test_json_mode_exit_zero_but_error_stopreason_is_failure(self):
        """In --mode json pi exits 0 even on a model error; the stopReason in the
        final assistant message is the real signal."""
        runner = PiRunner(session_id="s1")
        config = DispatchConfig()
        last = _assistant_message_end(
            stop_reason="error", error_message="Overloaded: please retry"
        )
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.category == "transient"

    def test_error_stopreason_non_transient_is_permanent(self):
        runner = PiRunner(session_id="s1")
        config = DispatchConfig()
        last = _assistant_message_end(
            stop_reason="error", error_message="invalid request: bad tool schema"
        )
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.category == "permanent"

    def test_aborted_stopreason_with_rate_limit_is_transient(self):
        runner = PiRunner(session_id="s1")
        config = DispatchConfig()
        last = _assistant_message_end(
            stop_reason="aborted", error_message="429 rate limit exceeded"
        )
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.category == "transient"

    def test_exit_zero_without_terminal_message_is_permanent(self):
        """pi prints startup/credential errors as plain text and still exits 0,
        emitting only the session header. A clean exit that never produced a
        terminal assistant turn must not be mistaken for success."""
        runner = PiRunner(session_id="s1")
        config = DispatchConfig()
        header = _ndjson({"type": "session", "version": 3, "id": "s1", "cwd": "/work"})
        # Stream the header (as the orchestrator would) then classify on it.
        runner.parse_line(header)
        rc = runner.classify_result(exit_code=0, last_output=header, config=config)
        assert rc.category == "permanent"

    def test_timeout_exit_is_transient(self):
        runner = PiRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=124, last_output=None, config=config)
        assert rc.category == "transient"

    def test_non_retriable_exit_is_permanent(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=2, last_output=None, config=config)
        assert rc.category == "permanent"

    def test_retriable_exit_no_payload_is_transient(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=1, last_output=None, config=config)
        assert rc.category == "transient"


class TestPiRunnerResume:
    def test_supports_resume(self):
        assert PiRunner().supports_resume() is True

    def test_resume_command_uses_session_flag(self):
        runner = PiRunner()
        config = DispatchConfig()
        config.agent.model = "anthropic/claude-sonnet-4-6"
        cmd = runner.build_resume_command(
            session_id="sess-abc", prompt="ignored", config=config, env={}
        )
        assert cmd[0] == "pi"
        assert "--session" in cmd
        assert cmd[cmd.index("--session") + 1] == "sess-abc"
        assert "--mode" in cmd
        assert cmd[cmd.index("--mode") + 1] == "json"

    def test_resume_uses_resume_prompt_not_caller_prompt(self):
        runner = PiRunner()
        config = DispatchConfig()
        cmd = runner.build_resume_command(
            session_id="sess-abc", prompt="caller prompt", config=config, env={}
        )
        assert "caller prompt" not in cmd
        assert any("continue where you left off" in a.lower() for a in cmd)

    def test_resume_reports_the_resumed_session_id(self):
        """On a resumed run, classify_result should report the session id the
        run was resumed into, so retries keep targeting the same session."""
        runner = PiRunner()
        config = DispatchConfig()
        runner.build_resume_command(
            session_id="prev-sess", prompt="x", config=config, env={}
        )
        last = _assistant_message_end(text="done", stop_reason="stop")
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.session_id == "prev-sess"

    def test_resume_carries_model(self):
        runner = PiRunner()
        config = DispatchConfig()
        cmd = runner.build_resume_command(
            session_id="s", prompt="x", config=config, env={}, model="openai/gpt-5.1"
        )
        assert cmd[cmd.index("--model") + 1] == "openai/gpt-5.1"


def events_of(runner: PiRunner, line: str):
    return runner.parse_line(line)


def test_message_end_usage_event_carries_model():
    runner = PiRunner(session_id="sess-xyz")
    line = _assistant_message_end(text="done", input_tokens=10, output_tokens=5)
    events = runner.parse_line(line)
    usage = next(e for e in events if e.event_type == "usage")
    assert usage.metadata.get("model") == "anthropic/claude-sonnet-4-6"
