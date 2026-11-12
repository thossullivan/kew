"""#2508: one getting-started path built on ``kew init``, plus an agent page.

Every ``kew`` command the two onboarding pages tell a reader to run must be a
real command with real flags, the getting-started path must be one numbered
list, neither page may send the reader to hand-write ``kew.toml`` or create a
network by hand, and the agent page must be in the published nav.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import typer

from kew.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
GETTING_STARTED = REPO_ROOT / "docs/getting-started.md"
AGENTS = REPO_ROOT / "docs/agents.md"
PAGES = (GETTING_STARTED, AGENTS, REPO_ROOT / "README.md")

pytestmark = pytest.mark.policy


def _fenced_commands(text: str) -> list[str]:
    """Every ``kew ...`` line inside a fenced code block, comments stripped."""
    commands = []
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, flags=re.S):
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.split(" #", 1)[0].strip()
            if line.startswith("kew "):
                commands.append(line)
    return commands


def _inline_commands(text: str) -> list[str]:
    return [m for m in re.findall(r"`(kew [^`]+)`", text) if "<" not in m]


def _resolve(command: str) -> tuple[Any, list[str]]:
    """Walk the registered command tree; return the leaf and the leftover args."""
    node: Any = typer.main.get_command(app)
    args = shlex.split(command)[1:]
    while hasattr(node, "commands") and args and not args[0].startswith("-"):
        child = node.commands.get(args[0])
        if child is None:
            break
        node, args = child, args[1:]
    return node, args


def _unknown(command: str) -> list[str]:
    node, args = _resolve(command)
    if hasattr(node, "commands") and args and not args[0].startswith("-"):
        return [f"no such command: {command}"]
    known = {opt for p in node.params for opt in getattr(p, "opts", [])}
    known |= {"--help"}
    flags = {a.split("=", 1)[0] for a in args if a.startswith("-")}
    return [f"{command}: unknown option {f}" for f in sorted(flags - known)]


def _numbered_path(text: str) -> str:
    return text.split("## Install and first dispatch", 1)[1].split("\n## ", 1)[0]


def test_getting_started_is_one_numbered_path_from_install_to_dispatch() -> None:
    path = _numbered_path(GETTING_STARTED.read_text())
    steps = re.findall(r"^(\d+)\. \*\*(.+?)\*\*", path, flags=re.M)
    assert [int(n) for n, _ in steps] == list(range(1, len(steps) + 1))
    assert steps[0][1].startswith("Install")
    assert steps[-1][1].startswith("Dispatch")
    commands = _fenced_commands(path)
    order = ["kew init", "kew doctor", "kew run"]
    firsts = [next(i for i, c in enumerate(commands) if c.startswith(o)) for o in order]
    assert firsts == sorted(firsts), commands
    assert "uv tool install" in path
    # Only one setup path: the old Option A / Option B split is gone.
    assert "Option B" not in GETTING_STARTED.read_text()


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_kew_command_on_the_onboarding_pages_exists(page: Path) -> None:
    text = page.read_text()
    if page.name == "README.md":
        text = text.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
    commands = _fenced_commands(text) + _inline_commands(text)
    assert commands, page
    problems = [p for c in commands for p in _unknown(c)]
    assert not problems, problems


def test_command_check_rejects_a_ghost_command_and_flag() -> None:
    assert _unknown("kew init --yes --runner codex") == []
    assert _unknown("kew sandbox network") == []
    assert _unknown("kew nosuch") == ["no such command: kew nosuch"]
    assert _unknown("kew init --nosuch") == [
        "kew init --nosuch: unknown option --nosuch"
    ]
    assert _unknown("kew sandbox nosuch") == ["no such command: kew sandbox nosuch"]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_onboarding_pages_never_hand_write_config_or_networks(page: Path) -> None:
    text = page.read_text()
    flat = " ".join(text.split()).lower()
    for banned in (
        "cat > kew.toml",
        "<< 'eof'",
        "network create",
        "provision a restricted",
        "edit kew.toml",
        "edit `kew.toml`",
        "never creates or modifies",
    ):
        assert banned not in flat, (page.name, banned)


def test_agent_page_is_in_the_nav_and_linked_from_getting_started() -> None:
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text()
    # Nested under its nav section, so any indent counts; a comment does not.
    assert re.search(r"^ +- Agents: agents\.md$", mkdocs, flags=re.M)
    assert "](agents.md)" in GETTING_STARTED.read_text()


def test_agent_page_covers_the_non_interactive_flow() -> None:
    text = AGENTS.read_text()
    for token in ("kew init --yes", "kew doctor --json", "--json", "## Exit codes"):
        assert token in text, token
    # Every `--json` command the page lists really takes `--json`.
    for command in re.findall(r"`(kew [^`]+ --json)`", text):
        assert _unknown(command) == [], command


def test_getting_started_names_runtimes_as_runtimes_not_installs() -> None:
    text = GETTING_STARTED.read_text()
    for runtime in ("Docker", "OrbStack", "Podman", "OpenShell"):
        assert f"**{runtime}**" in text, runtime
    assert "npx create-kew" not in text
    assert "gh auth login" in text
