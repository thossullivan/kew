"""Tests for the OpenCode runner implementation."""

import json

import pytest

from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.inference_transport import (
    RunnerTransportCapability,
    TransportResolutionError,
    resolve_inference_transport,
)
from kew.runners.opencode import OpenCodeRunner


class TestOpenCodeRunnerName:
    def test_name(self):
        assert OpenCodeRunner().name == "opencode"


class TestOpenCodeRunnerBuildCommand:
    def test_default_command_shape(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        cmd = runner.build_command(prompt="fix the bug", config=config, env={})
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--format" in cmd
        fmt_idx = cmd.index("--format")
        assert cmd[fmt_idx + 1] == "json"
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "google/gemini-2.5-pro"
        assert "fix the bug" in cmd

    def test_prompt_is_positional_last(self):
        """Prompt is a positional arg, not a flag value."""
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "openai/gpt-5.1-codex"
        cmd = runner.build_command(prompt="my prompt", config=config, env={})
        assert cmd[-1] == "my prompt"

    def test_agent_flag_only_when_set(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        cmd_default = runner.build_command(prompt="x", config=config, env={})
        assert "--agent" not in cmd_default

        config.agent.opencode.agent = "build"
        cmd_set = runner.build_command(prompt="x", config=config, env={})
        assert "--agent" in cmd_set
        a_idx = cmd_set.index("--agent")
        assert cmd_set[a_idx + 1] == "build"

    def test_extra_args_appended(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        config.agent.opencode.extra_args = ["--share", "--title", "ci-run"]
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert "--share" in cmd
        assert "--title" in cmd
        assert "ci-run" in cmd

    def test_model_param_overrides_config(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        cmd = runner.build_command(
            prompt="x", config=config, env={}, model="openai/gpt-5.1-codex"
        )
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "openai/gpt-5.1-codex"

    def test_env_not_mutated(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        env: dict[str, str] = {}
        runner.build_command(prompt="x", config=config, env=env)
        assert env == {}


class TestOpenCodeRunnerSwitchyard:
    def test_switchyard_capability_is_explicitly_unsupported(self):
        runner = OpenCodeRunner()

        assert runner.transport_capability == RunnerTransportCapability()

    def test_switchyard_is_refused_before_command_construction(self):
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
        runner = OpenCodeRunner()

        with pytest.raises(
            TransportResolutionError,
            match="opencode has no proven Switchyard incoming API capability",
        ):
            resolve_inference_transport(config, runner, "openai/gpt-5.1-codex")

    def test_direct_mode_keeps_command_and_environment_unchanged(self):
        config = DispatchConfig()
        env = {"OPENROUTER_API_KEY": "inherited"}
        runner = OpenCodeRunner()

        resolved = resolve_inference_transport(
            config, runner, "openrouter/qwen/qwen3-coder"
        )
        command = runner.build_command(
            prompt="fix the bug",
            config=config,
            env=env,
            model=resolved.model,
        )

        assert command == [
            "opencode",
            "run",
            "--format",
            "json",
            "-m",
            "openrouter/qwen/qwen3-coder",
            "fix the bug",
        ]
        assert env == {"OPENROUTER_API_KEY": "inherited"}


def _ndjson(payload: dict) -> str:
    """Serialize a dict as a single NDJSON line."""
    return json.dumps(payload)


class TestOpenCodeRunnerParseText:
    def test_text_event_emits_activity(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "text",
                "timestamp": 1700000000000,
                "sessionID": "ses_abc",
                "part": {"id": "p1", "type": "text", "text": "Reading config.py"},
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "activity"
        assert events[0].message == "Reading config.py"

    def test_long_text_truncated_to_200(self):
        runner = OpenCodeRunner()
        long_text = "x" * 500
        line = _ndjson(
            {
                "type": "text",
                "timestamp": 1700000000000,
                "sessionID": "ses_abc",
                "part": {"id": "p1", "type": "text", "text": long_text},
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        # 200 chars + ellipsis suffix
        assert events[0].message.endswith("...")
        assert len(events[0].message) <= 203

    def test_empty_text_returns_empty(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "text",
                "sessionID": "ses_abc",
                "part": {"id": "p1", "type": "text", "text": ""},
            }
        )
        assert runner.parse_line(line) == []


class TestOpenCodeRunnerParseToolUse:
    def test_tool_use_with_file_path_target(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "id": "p2",
                    "callID": "call_1",
                    "tool": "edit",
                    "state": {
                        "status": "completed",
                        "input": {"file_path": "src/config.py"},
                    },
                },
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 1
        assert events[0].event_type == "tool_use"
        assert events[0].message == "edit"
        assert events[0].metadata == {"target": "src/config.py"}

    def test_tool_use_with_path_target(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "tool": "read",
                    "state": {"status": "completed", "input": {"path": "README.md"}},
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].metadata == {"target": "README.md"}

    def test_tool_use_with_command_target(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "ls -la"}},
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].metadata == {"target": "ls -la"}

    def test_tool_use_no_recognized_target(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "tool": "websearch",
                    "state": {"status": "completed", "input": {"query": "x"}},
                },
            }
        )
        events = runner.parse_line(line)
        assert events[0].event_type == "tool_use"
        assert events[0].message == "websearch"
        assert events[0].metadata == {"target": ""}

    def test_tool_use_with_running_status_emits_nothing(self):
        """Defense-in-depth: only completed tool_use events become stream events."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "tool": "edit",
                    "state": {
                        "status": "running",
                        "input": {"file_path": "src/config.py"},
                    },
                },
            }
        )
        assert runner.parse_line(line) == []

    def test_tool_use_with_missing_tool_name_emits_nothing(self):
        """A tool_use event without a tool name carries no useful information."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "tool_use",
                "sessionID": "ses_abc",
                "part": {
                    "state": {
                        "status": "completed",
                        "input": {"file_path": "src/config.py"},
                    },
                },
            }
        )
        assert runner.parse_line(line) == []


class TestOpenCodeRunnerParseStepFinish:
    def test_step_finish_tool_calls_emits_usage_and_step(self):
        """Intermediate step_finish (more steps coming) emits usage and paired step."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {
                    "id": "p3",
                    "type": "step-finish",
                    "reason": "tool-calls",
                    "cost": 0.002,
                    "tokens": {
                        "input": 100,
                        "output": 50,
                        "reasoning": 10,
                        "cache": {"read": 200, "write": 30},
                    },
                },
            }
        )
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "usage" in kinds
        assert "step" in kinds
        usage = next(e for e in events if e.event_type == "usage")
        # tokens.input is uncached input; the cache legs stay separate (#1564).
        assert usage.input_tokens == 100
        # output + reasoning = 50 + 10
        assert usage.output_tokens == 60
        assert usage.cache_read_tokens == 200
        assert usage.cache_creation_tokens == 30

    def test_step_finish_stop_emits_usage_step_and_result(self):
        """Terminal step_finish (reason=stop) emits usage, paired step, and result."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_xyz",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "cost": 0.005,
                    "tokens": {
                        "input": 200,
                        "output": 80,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
            }
        )
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "usage" in kinds
        assert "step" in kinds
        assert "result" in kinds
        usage = next(e for e in events if e.event_type == "usage")
        assert usage.input_tokens == 200
        assert usage.output_tokens == 80
        result = next(e for e in events if e.event_type == "result")
        assert result.metadata == {
            "session_id": "ses_xyz",
            "stop_reason": "stop",
            "cost_usd": 0.005,
        }

    def test_step_finish_emits_paired_step_event(self):
        """opencode emits a 'step' paired with 'usage' so the loop accrues
        step_count/spent_usd (parity with claude-code)."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {"tokens": {"input": 100, "output": 20, "cache": {}}},
            }
        )
        events = runner.parse_line(line)
        kinds = [e.event_type for e in events]
        assert "usage" in kinds
        assert "step" in kinds
        usage = next(e for e in events if e.event_type == "usage")
        step = next(e for e in events if e.event_type == "step")
        assert step.input_tokens == usage.input_tokens
        assert step.output_tokens == usage.output_tokens
        assert step.cache_read_tokens == usage.cache_read_tokens
        assert step.cache_creation_tokens == usage.cache_creation_tokens

    def test_step_finish_splits_cache_legs_out_of_input(self):
        """Cache reads/writes land in their own fields, not folded into input.

        Folding them in reported every opencode run as 0% cached while the raw
        stream showed ~91-96% hit rates (#1564). The split must also stay
        reconcilable with the pre-fix totals, so the three input-side legs sum
        to what ``input_tokens`` alone used to carry.
        """
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {
                    "type": "step-finish",
                    "reason": "tool-calls",
                    # A real payload: total == input + output + reasoning + both
                    # cache legs, which is why tokens.input is uncached-only.
                    "tokens": {
                        "total": 21567,
                        "input": 3,
                        "output": 164,
                        "reasoning": 50,
                        "cache": {"write": 13306, "read": 8044},
                    },
                },
            }
        )
        usage = next(e for e in runner.parse_line(line) if e.event_type == "usage")
        assert usage.input_tokens == 3
        assert usage.cache_read_tokens == 8044
        assert usage.cache_creation_tokens == 13306
        assert usage.output_tokens == 214
        # Reconcilable with the folded figure historical rows were written with.
        assert (
            usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
            == 3 + 8044 + 13306
        )

    def test_step_finish_without_cache_key_reports_no_cache(self):
        """A part with no 'cache' key behaves as before: cache fields zero."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {"tokens": {"input": 100, "output": 20}},
            }
        )
        usage = next(e for e in runner.parse_line(line) if e.event_type == "usage")
        assert usage.input_tokens == 100
        assert usage.output_tokens == 20
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0

    def test_step_finish_emits_step_for_cache_only_usage(self):
        """A turn served entirely from cache still pairs a step with its usage.

        Before the split, cache reads inflated ``input_tokens``, so this gate
        passed by accident; gating on uncached input alone would silently drop
        the step (and its native cost) for a fully-cached turn.
        """
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {
                    "cost": 0.0009,
                    "tokens": {"input": 0, "output": 0, "cache": {"read": 4096}},
                },
            }
        )
        events = runner.parse_line(line)
        step = next(e for e in events if e.event_type == "step")
        assert step.cache_read_tokens == 4096
        assert step.metadata["cost_usd"] == 0.0009

    def test_step_carries_native_per_step_cost(self):
        """The paired 'step' event carries opencode's native per-step cost so
        the orchestrator sums billed truth, not the token estimate (#1082)."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {
                    "reason": "tool-calls",
                    "cost": 0.0037,
                    "tokens": {"input": 100, "output": 20, "cache": {}},
                },
            }
        )
        events = runner.parse_line(line)
        step = next(e for e in events if e.event_type == "step")
        assert step.metadata["cost_usd"] == 0.0037

    def test_step_cost_is_none_when_runner_reports_no_cost(self):
        """Absent 'cost' → None (not 0.0) so the orchestrator can fall back to
        a token estimate instead of recording a spurious $0 (#1082)."""
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {"tokens": {"input": 100, "output": 20, "cache": {}}},
            }
        )
        events = runner.parse_line(line)
        step = next(e for e in events if e.event_type == "step")
        assert step.metadata["cost_usd"] is None

    def test_step_finish_missing_tokens_defaults_to_zero(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_abc",
                "part": {"type": "step-finish", "reason": "stop"},
            }
        )
        events = runner.parse_line(line)
        assert len(events) == 2
        assert events[0].input_tokens == 0
        assert events[0].output_tokens == 0
        assert events[1].metadata["cost_usd"] == 0.0


class TestOpenCodeRunnerParseSilent:
    def test_step_start_emits_nothing(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "step_start",
                "sessionID": "ses_abc",
                "part": {
                    "id": "p0",
                    "type": "step-start",
                    "snapshot": "abc123",
                },
            }
        )
        assert runner.parse_line(line) == []

    def test_error_event_emits_nothing(self):
        runner = OpenCodeRunner()
        line = _ndjson(
            {
                "type": "error",
                "sessionID": "ses_abc",
                "error": {"name": "RateLimitError", "data": {"message": "slow down"}},
            }
        )
        assert runner.parse_line(line) == []

    def test_unknown_event_type_emits_nothing(self):
        runner = OpenCodeRunner()
        line = _ndjson({"type": "future_event_type_we_dont_know", "foo": "bar"})
        assert runner.parse_line(line) == []

    def test_empty_line_emits_nothing(self):
        runner = OpenCodeRunner()
        assert runner.parse_line("") == []
        assert runner.parse_line("   ") == []

    def test_malformed_json_emits_nothing(self):
        runner = OpenCodeRunner()
        assert runner.parse_line("not json at all") == []
        assert runner.parse_line("{incomplete") == []

    def test_non_dict_json_emits_nothing(self):
        runner = OpenCodeRunner()
        assert runner.parse_line("[1, 2, 3]") == []
        assert runner.parse_line('"just a string"') == []


class TestOpenCodeRunnerClassifyResult:
    def test_exit_zero_is_success(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=0, last_output=None, config=config)
        assert rc.category == ""
        assert rc.session_id is None

    def test_exit_zero_extracts_session_id_and_stop_reason(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        last = _ndjson(
            {
                "type": "step_finish",
                "sessionID": "ses_done",
                "part": {"type": "step-finish", "reason": "stop", "cost": 0.01},
            }
        )
        rc = runner.classify_result(exit_code=0, last_output=last, config=config)
        assert rc.category == ""
        assert rc.session_id == "ses_done"
        assert rc.stop_reason == "stop"

    def test_timeout_is_transient(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        rc = runner.classify_result(exit_code=124, last_output=None, config=config)
        assert rc.category == "transient"

    def test_non_retriable_exit_is_permanent(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        # Default retriable_exit_codes is [1] (verify in your config); pick something
        # known not to be retriable.
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=2, last_output=None, config=config)
        assert rc.category == "permanent"

    def test_retriable_exit_with_rate_limit_error_is_transient(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.retry.retriable_exit_codes = [1]
        last = _ndjson(
            {
                "type": "error",
                "sessionID": "ses_x",
                "error": {"name": "RateLimitError", "data": {"message": "slow down"}},
            }
        )
        rc = runner.classify_result(exit_code=1, last_output=last, config=config)
        assert rc.category == "transient"
        assert rc.session_id == "ses_x"

    def test_retriable_exit_with_other_error_is_permanent(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.retry.retriable_exit_codes = [1]
        last = _ndjson(
            {
                "type": "error",
                "sessionID": "ses_x",
                "error": {"name": "ValidationError", "data": {"message": "bad input"}},
            }
        )
        rc = runner.classify_result(exit_code=1, last_output=last, config=config)
        assert rc.category == "permanent"

    def test_retriable_exit_no_error_payload_is_transient(self):
        """Exit was retriable but we couldn't read an error event — assume transient."""
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=1, last_output=None, config=config)
        assert rc.category == "transient"


class TestOpenCodeRunnerResume:
    def test_supports_resume(self):
        assert OpenCodeRunner().supports_resume() is True

    def test_resume_command_includes_session_flag(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        cmd = runner.build_resume_command(
            session_id="ses_abc", prompt="ignored", config=config, env={}
        )
        assert cmd[0] == "opencode"
        assert "run" in cmd
        assert "--session" in cmd
        s_idx = cmd.index("--session")
        assert cmd[s_idx + 1] == "ses_abc"

    def test_resume_command_uses_resume_prompt_not_caller_prompt(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "google/gemini-2.5-pro"
        cmd = runner.build_resume_command(
            session_id="ses_abc", prompt="caller's prompt", config=config, env={}
        )
        assert "caller's prompt" not in cmd
        # Resume prompt mentions continuing where left off
        assert any("continue where you left off" in arg.lower() for arg in cmd)

    def test_resume_command_carries_format_and_model(self):
        runner = OpenCodeRunner()
        config = DispatchConfig()
        config.agent.model = "openai/gpt-5.1-codex"
        cmd = runner.build_resume_command(
            session_id="ses_abc", prompt="x", config=config, env={}
        )
        assert "--format" in cmd
        assert "json" in cmd
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "openai/gpt-5.1-codex"
