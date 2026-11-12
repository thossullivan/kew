"""`kew setup` writes a kew block into the target repo's AGENTS.md (#2507)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.kew_commands import named_invocations, unknown_invocations
from typer.testing import CliRunner

from kew.agents_md import BEGIN_MARKER, BLOCK, END_MARKER
from kew.cli import app
from kew.config import DispatchConfig

runner = CliRunner()


def _gh(cmd: list[str], **_kwargs: object) -> MagicMock:
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"name": "real-repo"}' if cmd[1:3] == ["repo", "view"] else ""
    fake.stderr = ""
    return fake


@contextmanager
def _setup_env(tmp_path: Path) -> Iterator[None]:
    cfg = DispatchConfig()
    cfg.project.repo = "owner/real-repo"
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.branches.worktree_base = str(tmp_path / "wt")
    old = os.getcwd()
    with (
        patch("kew.cli._detect_repo"),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.labels.run", side_effect=_gh),
    ):
        os.chdir(tmp_path)
        try:
            yield
        finally:
            os.chdir(old)


def _setup(tmp_path: Path, *args: str):
    with _setup_env(tmp_path):
        return runner.invoke(app, ["setup", *args])


def test_setup_creates_agents_md_with_only_the_kew_block(tmp_path: Path) -> None:
    result = _setup(tmp_path)

    assert result.exit_code == 0, result.output
    text = (tmp_path / "AGENTS.md").read_bytes().decode()
    assert text == f"{BLOCK}\n"
    assert text.startswith(BEGIN_MARKER)
    assert text.rstrip("\n").endswith(END_MARKER)


def test_second_setup_preserves_user_text_and_replaces_only_the_block(
    tmp_path: Path,
) -> None:
    before = "# Project rules\r\n\r\nUse tabs.  \r\n"
    after = "\n## Mine\nkeep  me\n\n"
    stale = f"{BEGIN_MARKER}\nold kew guidance\n{END_MARKER}"
    (tmp_path / "AGENTS.md").write_bytes(f"{before}{stale}{after}".encode())

    assert _setup(tmp_path).exit_code == 0
    first = (tmp_path / "AGENTS.md").read_bytes()
    assert first == f"{before}{BLOCK}{after}".encode()

    assert _setup(tmp_path).exit_code == 0
    assert (tmp_path / "AGENTS.md").read_bytes() == first


def test_setup_preserves_user_text_when_appending_the_block(tmp_path: Path) -> None:
    user = b"# Agent notes\n\nno trailing newline"
    (tmp_path / "AGENTS.md").write_bytes(user)

    assert _setup(tmp_path).exit_code == 0
    text = (tmp_path / "AGENTS.md").read_bytes()
    assert text.startswith(user)
    assert text[len(user) :] == f"\n\n{BLOCK}\n".encode()

    assert _setup(tmp_path).exit_code == 0
    assert (tmp_path / "AGENTS.md").read_bytes() == text


@pytest.mark.parametrize(
    "markers",
    [
        f"{BEGIN_MARKER}\nno end\n",
        f"{END_MARKER}\n{BEGIN_MARKER}\n",
        f"{BEGIN_MARKER}\n{END_MARKER}\n{BEGIN_MARKER}\n{END_MARKER}\n",
    ],
)
def test_setup_leaves_unpairable_markers_untouched(
    tmp_path: Path, markers: str
) -> None:
    original = f"mine\n{markers}tail\n".encode()
    (tmp_path / "AGENTS.md").write_bytes(original)

    result = _setup(tmp_path)

    assert result.exit_code == 1
    assert (tmp_path / "AGENTS.md").read_bytes() == original


def test_setup_json_reports_the_agents_md_action(tmp_path: Path) -> None:
    first = json.loads(_setup(tmp_path, "--json").stdout)
    second = json.loads(_setup(tmp_path, "--json").stdout)

    path = str((tmp_path / "AGENTS.md").resolve())
    assert first["agents_md"] == {"path": path, "action": "created"}
    assert second["agents_md"] == {"path": path, "action": "unchanged"}
    assert first["ok"] is second["ok"] is True


def test_agents_block_points_at_the_skill_and_three_first_commands() -> None:
    assert "operate-kew" in BLOCK
    first = [tokens[0] for tokens in named_invocations(BLOCK)][:3]
    assert first == ["doctor", "init", "schema"]


def test_agents_block_skill_commands_exist() -> None:
    assert named_invocations(BLOCK)
    assert unknown_invocations(BLOCK) == []
