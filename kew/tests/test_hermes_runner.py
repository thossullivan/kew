"""Tests for the Hermes runner implementation.

Hermes is an OpenRouter-only headless coding agent. This runner shells out to
`hermes -z "<prompt>" --usage-file <path> --provider openrouter --model <id>`,
which prints the final response text to stdout and drops a single JSON usage
file on completion. That usage file — not the exit code — is the authoritative
success/failure and cost signal.

Contract reference: `docs/spikes/2026-07-25-hermes-headless-feasibility.md@4a387c0b`.
"""

import json
from pathlib import Path

import pytest

from kew.config import DispatchConfig
from kew.runners.hermes import HermesRunner


def _config(tmp_path: Path, model: str = "openrouter/deepseek/deepseek-v4-flash"):
    config = DispatchConfig()
    config.agent.model = model
    config.logging.dir = str(tmp_path)
    return config


# Actual observed usage-file schema (live-tested 2026-07-25).
def _usage_payload(
    *,
    completed: bool = True,
    failed: bool = False,
    cost: float | None = 0.00030107924,
    input_tokens: int = 161,
    output_tokens: int = 14,
    cache_read_tokens: int = 15104,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 11,
    session_id: str = "20260725_153412_d9da19",
    model: str = "deepseek/deepseek-v4-flash",
    provider: str = "openrouter",
    **extra,
) -> dict:
    payload = {
        "estimated_cost_usd": cost,
        "cost_status": "estimated",
        "cost_source": "provider_models_api",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": input_tokens
        + output_tokens
        + cache_read_tokens
        + cache_write_tokens,
        "api_calls": 1,
        "model": model,
        "provider": provider,
        "session_id": session_id,
        "completed": completed,
        "failed": failed,
        "service_tier": None,
    }
    payload.update(extra)
    return payload


def _write_usage(runner: HermesRunner, payload: dict) -> None:
    """Write a usage file at the path the runner's last command allocated."""
    assert runner._usage_file is not None, "call build_command first"
    runner._usage_file.write_text(json.dumps(payload), encoding="utf-8")


class TestHermesRunnerName:
    def test_name(self):
        assert HermesRunner().name == "hermes"

    def test_loop_style_is_native(self):
        from kew.runners.protocol import loop_style_to_drive_mode

        runner = HermesRunner()
        assert runner.loop_style == "native"
        assert loop_style_to_drive_mode(runner.loop_style) == "delegated"


class TestHermesRunnerBuildCommand:
    def test_command_shape_and_prefix_stripping(self, tmp_path):
        runner = HermesRunner()
        cmd = runner.build_command(
            prompt="fix the bug", config=_config(tmp_path), env={}
        )
        assert cmd[0] == "hermes"
        assert "-z" in cmd
        assert cmd[cmd.index("-z") + 1] == "fix the bug"
        assert "--provider" in cmd
        assert cmd[cmd.index("--provider") + 1] == "openrouter"
        # openrouter/ prefix stripped for --model.
        assert cmd[cmd.index("--model") + 1] == "deepseek/deepseek-v4-flash"

    def test_usage_file_flag_is_run_scoped_under_log_dir(self, tmp_path):
        runner = HermesRunner()
        cmd = runner.build_command(prompt="x", config=_config(tmp_path), env={})
        assert "--usage-file" in cmd
        usage_path = Path(cmd[cmd.index("--usage-file") + 1])
        # Lives under the log dir, never cwd-relative inside the worktree.
        assert usage_path.parent == tmp_path.resolve()
        assert usage_path.is_absolute()

    def test_model_param_overrides_config(self, tmp_path):
        runner = HermesRunner()
        cmd = runner.build_command(
            prompt="x",
            config=_config(tmp_path),
            env={},
            model="openrouter/qwen/qwen3-30b",
        )
        assert cmd[cmd.index("--model") + 1] == "qwen/qwen3-30b"

    def test_non_openrouter_model_fails_closed(self, tmp_path):
        runner = HermesRunner()
        config = _config(tmp_path, model="anthropic/claude-sonnet-4-6")
        with pytest.raises(ValueError, match="OpenRouter"):
            runner.build_command(prompt="x", config=config, env={})

    def test_empty_model_fails_closed(self, tmp_path):
        runner = HermesRunner()
        config = _config(tmp_path, model="")
        with pytest.raises(ValueError, match="OpenRouter"):
            runner.build_command(prompt="x", config=config, env={})

    def test_bare_prefix_fails_closed(self, tmp_path):
        runner = HermesRunner()
        config = _config(tmp_path, model="openrouter/")
        with pytest.raises(ValueError, match="no model id"):
            runner.build_command(prompt="x", config=config, env={})

    def test_extra_args_appended(self, tmp_path):
        runner = HermesRunner()
        config = _config(tmp_path)
        config.agent.hermes.extra_args = ["--thinking", "high"]
        cmd = runner.build_command(prompt="x", config=config, env={})
        assert cmd[cmd.index("--thinking") + 1] == "high"

    def test_env_not_mutated(self, tmp_path):
        runner = HermesRunner()
        env: dict[str, str] = {}
        runner.build_command(prompt="x", config=_config(tmp_path), env=env)
        assert env == {}


class TestHermesRunnerParseLine:
    def test_nonempty_line_emits_single_activity(self):
        runner = HermesRunner()
        events = runner.parse_line("Applied the fix and ran tests.")
        assert len(events) == 1
        assert events[0].event_type == "activity"
        assert events[0].message == "Applied the fix and ran tests."

    def test_empty_line_emits_nothing(self):
        runner = HermesRunner()
        assert runner.parse_line("") == []
        assert runner.parse_line("   ") == []

    def test_long_line_truncated(self):
        runner = HermesRunner()
        events = runner.parse_line("x" * 500)
        assert events[0].message.endswith("...")
        assert len(events[0].message) <= 203


class TestHermesRunnerClassifyResult:
    def test_usage_completed_is_success(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload(completed=True, failed=False))
        rc = runner.classify_result(
            exit_code=0, last_output="done", config=_config(tmp_path)
        )
        assert rc.category == ""
        assert rc.stop_reason == "completed"
        assert rc.session_id == "20260725_153412_d9da19"

    def test_usage_failed_exit_zero_is_permanent(self, tmp_path):
        """hermes exits 0 on a credential failure; failed:true is authoritative."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(
            runner,
            _usage_payload(completed=False, failed=True, cost=None),
        )
        rc = runner.classify_result(
            exit_code=0,
            last_output="No LLM provider configured for openrouter",
            config=_config(tmp_path),
        )
        assert rc.category == "permanent"

    def test_usage_failed_with_rate_limit_is_transient(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(
            runner,
            _usage_payload(
                completed=False, failed=True, cost=None, error="429 rate limit exceeded"
            ),
        )
        rc = runner.classify_result(
            exit_code=0, last_output="", config=_config(tmp_path)
        )
        assert rc.category == "transient"

    def test_usage_failed_marker_in_last_output_is_transient(self, tmp_path):
        """The failure marker may be in stdout, not the usage file."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload(completed=False, failed=True, cost=None))
        rc = runner.classify_result(
            exit_code=0,
            last_output="upstream connection reset by peer",
            config=_config(tmp_path),
        )
        assert rc.category == "transient"

    def test_missing_usage_file_exit_zero_is_permanent(self, tmp_path):
        """A clean exit that never wrote a completed usage file is not success."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        # No usage file written.
        rc = runner.classify_result(
            exit_code=0, last_output="", config=_config(tmp_path)
        )
        assert rc.category == "permanent"

    def test_timeout_exit_is_transient(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        rc = runner.classify_result(
            exit_code=124, last_output=None, config=_config(tmp_path)
        )
        assert rc.category == "transient"

    def test_sigterm_kill_exit_is_transient(self, tmp_path):
        """SIGTERM (143) leaves no usage file → transient, not a crash."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        rc = runner.classify_result(
            exit_code=143, last_output=None, config=_config(tmp_path)
        )
        assert rc.category == "transient"

    def test_retriable_exit_no_usage_is_transient(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        config = _config(tmp_path)
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=1, last_output=None, config=config)
        assert rc.category == "transient"

    def test_non_retriable_exit_no_usage_is_permanent(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        config = _config(tmp_path)
        config.retry.retriable_exit_codes = [1]
        rc = runner.classify_result(exit_code=2, last_output=None, config=config)
        assert rc.category == "permanent"

    def test_completed_overrides_nonzero_exit(self, tmp_path):
        """usage completed:true is authoritative even with a nonzero exit."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload(completed=True, failed=False))
        rc = runner.classify_result(
            exit_code=1, last_output="", config=_config(tmp_path)
        )
        assert rc.category == ""


class TestHermesRunnerEnrichResult:
    def _result(self):
        from kew.tracking import DispatchResult

        return DispatchResult(exit_code=0, issue_num=1, log_file=Path("/dev/null"))

    def test_enrich_from_usage_file_sets_tokens_and_cost(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload())
        result = self._result()
        runner.enrich_result(result, _config(tmp_path))
        assert result.input_tokens == 161
        # output = output_tokens + reasoning_tokens
        assert result.output_tokens == 14 + 11
        assert result.cache_read_tokens == 15104
        assert result.cache_creation_tokens == 0
        assert result.reported_cost_usd == pytest.approx(0.00030107924)
        assert result.resolved_model == "openrouter/deepseek/deepseek-v4-flash"

    def test_enrich_cleans_up_usage_file(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload())
        usage_path = runner._usage_file
        assert usage_path.exists()
        runner.enrich_result(self._result(), _config(tmp_path))
        # Cleaned up like the sandbox prompt file (#725); never lands in a PR.
        assert not usage_path.exists()

    def test_enrich_tolerates_missing_usage_file(self, tmp_path):
        """A killed run leaves no usage file — meter $0 native, do not crash."""
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        result = self._result()
        runner.enrich_result(result, _config(tmp_path))  # must not raise
        assert result.input_tokens == 0
        assert result.reported_cost_usd is None

    def test_enrich_null_cost_leaves_reported_none(self, tmp_path):
        runner = HermesRunner()
        runner.build_command(prompt="x", config=_config(tmp_path), env={})
        _write_usage(runner, _usage_payload(failed=True, completed=False, cost=None))
        result = self._result()
        runner.enrich_result(result, _config(tmp_path))
        assert result.reported_cost_usd is None


class TestHermesRunnerResume:
    def test_supports_resume(self):
        assert HermesRunner().supports_resume() is True

    def test_resume_command_uses_resume_flag_and_new_usage_file(self, tmp_path):
        runner = HermesRunner()
        first = runner.build_command(prompt="x", config=_config(tmp_path), env={})
        first_usage = first[first.index("--usage-file") + 1]
        cmd = runner.build_resume_command(
            session_id="sess-abc",
            prompt="ignored",
            config=_config(tmp_path),
            env={},
        )
        assert cmd[0] == "hermes"
        assert cmd[cmd.index("--resume") + 1] == "sess-abc"
        # A fresh usage file for the resume (never reuse the prior one).
        assert cmd[cmd.index("--usage-file") + 1] != first_usage

    def test_resume_uses_resume_prompt_not_caller_prompt(self, tmp_path):
        runner = HermesRunner()
        cmd = runner.build_resume_command(
            session_id="sess-abc",
            prompt="caller prompt",
            config=_config(tmp_path),
            env={},
        )
        assert "caller prompt" not in cmd
        assert any("continue where you left off" in a.lower() for a in cmd)

    def test_resume_reports_resumed_session_id_when_no_new_usage(self, tmp_path):
        """Resume locks the instance to the prior session; classify reports it
        even if the resumed run leaves no usage file (killed)."""
        runner = HermesRunner()
        runner.build_resume_command(
            session_id="prev-sess", prompt="x", config=_config(tmp_path), env={}
        )
        rc = runner.classify_result(
            exit_code=143, last_output=None, config=_config(tmp_path)
        )
        assert rc.session_id == "prev-sess"

    def test_resume_carries_model_prefix_stripped(self, tmp_path):
        runner = HermesRunner()
        cmd = runner.build_resume_command(
            session_id="s",
            prompt="x",
            config=_config(tmp_path),
            env={},
            model="openrouter/qwen/qwen3-30b",
        )
        assert cmd[cmd.index("--model") + 1] == "qwen/qwen3-30b"
