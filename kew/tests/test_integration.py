"""Integration test for the full dispatch dry-run cycle."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from kew.cli import app

runner = CliRunner()


class TestDryRunIntegration:
    def test_full_cycle_with_config(self, tmp_path: Path):
        """Full dry-run cycle: config -> select -> prompt -> output."""
        config_content = b"""
[project]
name = "test-project"
repo = "owner/test-repo"

[commands]
ci = "pytest"
format = "black ."
verify = "pytest"

[agent]
timeout_minutes = 30
"""
        config_file = tmp_path / "kew.toml"
        config_file.write_bytes(config_content)

        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli.select_next_issue") as mock_select,
            # The preview reads the duplicate-dispatch guard the real run reads,
            # so it needs a free branch name to promise a dispatch (#2058).
            patch("kew.cli.remote_branch_exists", return_value=False),
        ):
            from kew.config import load_config as real_load

            mock_config.return_value = real_load(config_path=config_file)
            mock_select.return_value = {
                "number": 42,
                "title": "Add dark mode",
                "body": "## Summary\nAdd dark mode toggle",
                "labels": [
                    {"name": "ready"},
                    {"name": "p1-high"},
                    {"name": "medium"},
                ],
            }

            result = runner.invoke(app, ["run", "--dry-run"])
            assert result.exit_code == 0
