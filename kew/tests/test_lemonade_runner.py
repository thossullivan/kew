"""Tests for the native Lemonade runner (kew's own local-model loop)."""

from __future__ import annotations

import json

from kew.config import DispatchConfig
from kew.runners.lemonade import LemonadeRunner
from kew.runners.registry import resolve_runner


def _config(model: str = "lemonade/Qwen3-Coder-30B") -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.agent.model = model
    cfg.agent.runner = "lemonade"
    return cfg


# -- routing ---------------------------------------------------------------


def test_lemonade_prefix_resolves_to_lemonade_runner() -> None:
    cfg = _config()
    resolved = resolve_runner(cfg)
    assert resolved.runner.name == "lemonade"


def test_explicit_runner_resolves() -> None:
    cfg = DispatchConfig()
    cfg.agent.runner = "lemonade"
    assert resolve_runner(cfg).runner.name == "lemonade"


# -- build_command ---------------------------------------------------------


def test_build_command_shape_and_prefix_stripping() -> None:
    cfg = _config("lemonade/Qwen3-Coder-30B")
    cfg.agent.lemonade.base_url = "http://localhost:13305/v1"
    cfg.agent.lemonade.max_steps = 12
    runner = LemonadeRunner()
    cmd = runner.build_command(prompt="fix #1", config=cfg, env={}, model="")

    assert cmd[0] == "kew-lemonade"
    assert "--base-url" in cmd
    assert "http://localhost:13305/v1" in cmd
    # routing prefix is stripped before the endpoint sees the model name
    assert "Qwen3-Coder-30B" in cmd
    assert "lemonade/Qwen3-Coder-30B" not in cmd
    assert cmd[cmd.index("--max-steps") + 1] == "12"
    assert cmd[-2] == "-p"
    assert cmd[-1] == "fix #1"
    # a session id was generated
    assert "--session-id" in cmd


def test_resume_command_reuses_session_id() -> None:
    cfg = _config()
    runner = LemonadeRunner()
    cmd = runner.build_resume_command(
        session_id="sess-abc", prompt="ignored", config=cfg, env={}, model=""
    )
    assert cmd[cmd.index("--session-id") + 1] == "sess-abc"
    assert runner.supports_resume() is True


# -- parse_line ------------------------------------------------------------


def test_parse_session_header_emits_init() -> None:
    runner = LemonadeRunner()
    events = runner.parse_line(json.dumps({"type": "session", "id": "sess-1"}))
    assert len(events) == 1
    assert events[0].event_type == "init"
    assert events[0].metadata["session_id"] == "sess-1"


def test_parse_tool_start_emits_tool_use_with_target() -> None:
    runner = LemonadeRunner()
    line = json.dumps(
        {
            "type": "tool_execution_start",
            "toolName": "edit_file",
            "args": {"path": "src/kew/foo.py"},
        }
    )
    events = runner.parse_line(line)
    assert len(events) == 1
    assert events[0].event_type == "tool_use"
    assert events[0].message == "edit_file"
    assert events[0].metadata["target"] == "src/kew/foo.py"


def test_parse_message_end_tooluse_emits_usage_and_step_no_result() -> None:
    runner = LemonadeRunner()
    line = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "editing"}],
                "usage": {"input": 100, "output": 20},
                "stopReason": "toolUse",
                "model": "Qwen3-Coder-30B",
            },
        }
    )
    events = runner.parse_line(line)
    kinds = [e.event_type for e in events]
    assert "activity" in kinds
    assert "usage" in kinds
    assert "step" in kinds
    assert "result" not in kinds  # toolUse is not terminal
    usage = next(e for e in events if e.event_type == "usage")
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_parse_message_end_stop_emits_result() -> None:
    runner = LemonadeRunner()
    line = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input": 5, "output": 5},
                "stopReason": "stop",
                "model": "Qwen3-Coder-30B",
            },
        }
    )
    events = runner.parse_line(line)
    result = next(e for e in events if e.event_type == "result")
    assert result.metadata["stop_reason"] == "stop"
    assert result.metadata["cost_usd"] == 0.0  # local inference is ≈free


def test_parse_garbage_line_is_ignored() -> None:
    runner = LemonadeRunner()
    assert runner.parse_line("not json") == []
    assert runner.parse_line("") == []


# -- classify_result -------------------------------------------------------


def test_classify_success() -> None:
    runner = LemonadeRunner()
    last = json.dumps(
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
    )
    got = runner.classify_result(exit_code=0, last_output=last, config=_config())
    assert got.category == ""


def test_classify_connection_error_is_transient() -> None:
    runner = LemonadeRunner()
    last = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "connection refused",
            },
        }
    )
    got = runner.classify_result(exit_code=1, last_output=last, config=_config())
    assert got.category == "transient"


def test_classify_max_steps_aborted_is_permanent() -> None:
    runner = LemonadeRunner()
    last = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "aborted",
                "errorMessage": "reached max_steps=40 without finishing",
            },
        }
    )
    got = runner.classify_result(exit_code=1, last_output=last, config=_config())
    assert got.category == "permanent"


def test_lemonade_models_are_free_and_silent() -> None:
    """Local inference must resolve to $0 with no 'unknown model' warning."""
    import warnings

    from kew.pricing import estimate_cost, is_free_model

    assert is_free_model("lemonade/qwen2.5-coder") is True
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        cost = estimate_cost(
            "lemonade/qwen2.5-coder", input_tokens=5000, output_tokens=2000
        )
    assert cost == 0.0


def test_build_command_api_key_via_env_not_argv() -> None:
    """The API key is forwarded through the environment, never in argv."""
    cfg = _config()
    cfg.agent.lemonade.api_key = "secret-token"
    cfg.agent.lemonade.extra_args = ["--foo", "bar"]
    runner = LemonadeRunner()
    env: dict[str, str] = {}
    cmd = runner.build_command(prompt="p", config=cfg, env=env, model="")
    assert env["LEMONADE_API_KEY"] == "secret-token"
    assert "--api-key" not in cmd
    assert "secret-token" not in cmd
    # extra_args pass through verbatim
    assert "--foo" in cmd and "bar" in cmd


def test_agent_resolve_in_worktree_rejects_escape(tmp_path) -> None:
    """The shared path guard keeps file tools inside the worktree."""
    from kew.runners.lemonade_agent import _resolve_in_worktree

    root = tmp_path.resolve()
    assert _resolve_in_worktree(root, "sub/f.py") is not None
    assert _resolve_in_worktree(root, "../escape.py") is None
    assert _resolve_in_worktree(root, "/etc/passwd") is None


def test_run_loop_surfaces_length_finish_reason(monkeypatch, tmp_path, capsys) -> None:
    """A truncated ('length') terminal answer is reported, not masked as 'stop'."""
    from kew.runners import lemonade_agent

    body = {
        "choices": [
            {
                "message": {"content": "done", "tool_calls": []},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        "model": "m",
    }
    monkeypatch.setattr(lemonade_agent, "_post_json", lambda *a, **k: body)
    rc = lemonade_agent.run_loop(
        prompt="p",
        base_url="http://x/v1",
        api_key="",
        model="m",
        session_id="s",
        max_steps=5,
        timeout=1.0,
        root=tmp_path.resolve(),
    )
    assert rc == 0
    ends = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and json.loads(line).get("type") == "message_end"
    ]
    assert ends and ends[-1]["message"]["stopReason"] == "length"
