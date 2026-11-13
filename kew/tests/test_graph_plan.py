"""The epic-plan query prerequisite retains the public cannot-run outcome."""

from unittest.mock import MagicMock

from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig


def test_round_four_epic_plan_initial_query_cannot_run(monkeypatch, tmp_path):
    config = DispatchConfig()
    config.project.repo = "owner/repo"
    plan = tmp_path / "plan.md"
    plan.write_text("Plan")
    source = MagicMock()
    source.list_epic_nodes.side_effect = RuntimeError("initial query unavailable")
    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: config)
    monkeypatch.setattr("kew.cli._preflight", lambda config: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda config: None)
    monkeypatch.setattr(
        "kew.graph.plan_run.make_dependency_source", lambda config: source
    )
    result = CliRunner().invoke(app, ["epic", "plan", "demo", "--from", str(plan)])
    assert result.exit_code == 2, result.output
    assert "initial query unavailable" in result.output
