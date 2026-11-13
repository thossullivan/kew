"""Plan 2 Task 1: loop_id + per-run budget override thread through dispatch."""

from kew.config import DispatchConfig
from kew.runners.claude_code import ClaudeCodeRunner


def test_budget_override_reaches_claude_command():
    runner = ClaudeCodeRunner()
    config = DispatchConfig()
    config.agent.max_budget_usd = 10.0  # global default
    env: dict[str, str] = {}
    cmd = runner.build_command(
        prompt="hi", config=config, env=env, model="", budget_override_usd=3.5
    )
    assert "--max-budget-usd" in cmd
    # The per-run override wins over the global config field.
    idx = cmd.index("--max-budget-usd")
    assert cmd[idx + 1] == "3.5"


def test_budget_override_none_falls_back_to_config():
    runner = ClaudeCodeRunner()
    config = DispatchConfig()
    config.agent.max_budget_usd = 7.0
    env: dict[str, str] = {}
    cmd = runner.build_command(prompt="hi", config=config, env=env, model="")
    idx = cmd.index("--max-budget-usd")
    assert cmd[idx + 1] == "7.0"


def test_run_tracked_agent_accepts_loop_id_and_budget_override():
    import inspect

    from kew.dispatch_core import run_tracked_agent

    sig = inspect.signature(run_tracked_agent)
    assert "loop_id" in sig.parameters
    assert "budget_override_usd" in sig.parameters
    assert "timeout_override_minutes" in sig.parameters
    assert sig.parameters["loop_id"].default is None
    assert sig.parameters["budget_override_usd"].default is None
    assert sig.parameters["timeout_override_minutes"].default is None


def test_dispatch_result_has_run_id_field():
    import dataclasses

    from kew.tracking import DispatchResult

    fields = {f.name for f in dataclasses.fields(DispatchResult)}
    assert "run_id" in fields
    # Default is empty string (no run id on synthetic/never-ran results).
    assert (
        DispatchResult(
            exit_code=0, issue_num=1, log_file=__import__("pathlib").Path(".")
        ).run_id
        == ""
    )
