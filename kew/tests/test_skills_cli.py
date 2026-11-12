"""CLI tests for the `kew skills` command group."""

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig

runner = CliRunner()


def _in(tmp_path: Path):
    """Run CLI with cwd = tmp repo root (kew skills is cwd-based)."""

    class _Ctx:
        def __enter__(self):
            self._old = os.getcwd()
            os.chdir(tmp_path)

        def __exit__(self, *exc):
            os.chdir(self._old)

    return _Ctx()


def test_skills_list_shows_catalog(tmp_path):
    with _in(tmp_path):
        result = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    for name in (
        "plan-to-issues",
        "security-guard",
        "playbook-skill",
        "verify-parity",
        "handoff",
    ):
        assert name in result.output
    assert "not-installed" in result.output


def test_skills_install_all_then_current(tmp_path):
    with _in(tmp_path):
        result = runner.invoke(app, ["skills", "install", "--all"])
        assert result.exit_code == 0
        assert (tmp_path / ".claude/skills/handoff/SKILL.md").exists()
        assert (tmp_path / ".claude/hooks/security-guard.sh").exists()
        listing = runner.invoke(app, ["skills", "list"])
    assert "not-installed" not in listing.output
    assert "current" in listing.output


def test_skills_install_all_writes_no_internal_playbook(tmp_path):
    """kew's own concurrent-state-change playbook is not a user artifact (#2016)."""
    with _in(tmp_path):
        result = runner.invoke(app, ["skills", "install", "--all"])
        listing = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    assert not (tmp_path / ".claude/skills/concurrent-state-change").exists()
    assert "concurrent-state-change" not in listing.output
    assert (tmp_path / ".claude/kew/adversarial-reviewer.md").exists()


def test_skills_install_unknown_name_errors(tmp_path):
    with _in(tmp_path):
        result = runner.invoke(app, ["skills", "install", "nope"])
    assert result.exit_code == 2
    clean = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "unknown artifact" in clean.lower()


def test_skills_update_refuses_modified_then_force(tmp_path):
    with _in(tmp_path):
        runner.invoke(app, ["skills", "install", "handoff"])
        (tmp_path / ".claude/skills/handoff/SKILL.md").write_text("edited\n")
        refused = runner.invoke(app, ["skills", "update", "handoff"])
        assert refused.exit_code == 1
        assert "locally modified" in refused.output
        forced = runner.invoke(app, ["skills", "update", "handoff", "--force"])
        assert forced.exit_code == 0


def test_skills_diff_shows_drift(tmp_path):
    with _in(tmp_path):
        runner.invoke(app, ["skills", "install", "handoff"])
        (tmp_path / ".claude/skills/handoff/SKILL.md").write_text("edited\n")
        result = runner.invoke(app, ["skills", "diff", "handoff"])
    assert result.exit_code == 1
    assert "edited" in result.output


def test_skills_list_works_without_kew_toml(tmp_path):
    """load_config() falls back to defaults when no kew.toml exists in cwd."""
    with _in(tmp_path):
        assert not (tmp_path / "kew.toml").exists()
        result = runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0
    assert "not-installed" in result.output


class TestSetupWithSkills:
    """`kew setup --with-skills` provisions the full artifact catalog."""

    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_with_skills_installs_catalog(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
        tmp_path: Path,
    ):
        cfg = DispatchConfig()
        cfg.project.repo = "owner/real-repo"
        cfg.logging.dir = str(tmp_path / "logs")
        cfg.branches.worktree_base = str(tmp_path / "wt")
        mock_config.return_value = cfg

        def side_effect(cmd, **kwargs):
            fake = MagicMock()
            if cmd[1] == "repo" and cmd[2] == "view":
                fake.returncode = 0
                fake.stdout = '{"name": "real-repo"}'
                fake.stderr = ""
            elif cmd[1] == "label" and cmd[2] == "create":
                fake.returncode = 0
                fake.stdout = ""
                fake.stderr = ""
            else:
                fake.returncode = 0
                fake.stdout = ""
                fake.stderr = ""
            return fake

        mock_run.side_effect = side_effect

        with _in(tmp_path):
            result = runner.invoke(app, ["setup", "--with-skills"])
        assert result.exit_code == 0
        assert (tmp_path / ".claude/skills/handoff/SKILL.md").exists()
        assert not (tmp_path / ".claude/skills/concurrent-state-change").exists()

    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_without_with_skills_hints_at_install(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
        tmp_path: Path,
    ):
        cfg = DispatchConfig()
        cfg.project.repo = "owner/real-repo"
        cfg.logging.dir = str(tmp_path / "logs")
        cfg.branches.worktree_base = str(tmp_path / "wt")
        mock_config.return_value = cfg

        def side_effect(cmd, **kwargs):
            fake = MagicMock()
            if cmd[1] == "repo" and cmd[2] == "view":
                fake.returncode = 0
                fake.stdout = '{"name": "real-repo"}'
                fake.stderr = ""
            elif cmd[1] == "label" and cmd[2] == "create":
                fake.returncode = 0
                fake.stdout = ""
                fake.stderr = ""
            else:
                fake.returncode = 0
                fake.stdout = ""
                fake.stderr = ""
            return fake

        mock_run.side_effect = side_effect

        with _in(tmp_path):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0
        assert "kew skills install" in result.output
        assert not (tmp_path / ".claude/skills").exists()
