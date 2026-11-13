"""`kew schema` prints the live command tree as JSON (#2506)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.core import TyperCommand
from typer.main import get_command
from typer.testing import CliRunner

from kew.cli import app
from kew.cli.schema import command_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
# Commands documented on a page other than docs/cli/<top-level name>.md.
PAGE_EXCEPTIONS = {"tui": "docs/tui/index.md", "deny": "docs/cli/approve.md"}

runner = CliRunner()


def _schema() -> dict[str, Any]:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _visible_leaves(command: Any, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Visible leaf paths, walked independently of the code under test."""
    children = getattr(command, "commands", None)
    if children is None:
        return {path}
    leaves: set[tuple[str, ...]] = set()
    for name, child in children.items():
        if not child.hidden:
            leaves |= _visible_leaves(child, (*path, name))
    return leaves


def _assert_complete(root: Any, schema: dict[str, Any]) -> None:
    listed = {tuple(entry["path"]) for entry in schema["commands"]}
    missing = _visible_leaves(root) - listed
    assert not missing, f"visible commands missing from kew schema: {sorted(missing)}"
    for entry in schema["commands"]:
        top = entry["path"][0]
        page = REPO_ROOT / PAGE_EXCEPTIONS.get(top, f"docs/cli/{top}.md")
        assert page.is_file(), f"{entry['command']} has no docs page ({page})"
        assert entry["command"] in page.read_text(), (
            f"{page.name} never names {entry['command']}"
        )


@pytest.mark.policy
def test_schema_is_complete_and_every_command_has_a_docs_page() -> None:
    _assert_complete(get_command(app), _schema())


def test_complete_check_goes_red_on_an_omitted_command() -> None:
    root = get_command(app)
    schema = _schema()
    schema["commands"] = [
        entry for entry in schema["commands"] if entry["path"] != ["epic", "status"]
    ]
    with pytest.raises(AssertionError, match="epic', 'status"):
        _assert_complete(root, schema)


def test_complete_check_goes_red_on_an_undocumented_command() -> None:
    # get_command builds a fresh Click tree per call, so this edit stays local.
    root: Any = get_command(app)
    root.commands["control-only"] = TyperCommand("control-only", help="Control.")
    with pytest.raises(AssertionError, match="kew control-only has no docs page"):
        _assert_complete(root, command_schema(root))


def test_hidden_commands_and_options_are_excluded() -> None:
    commands = _schema()["commands"]
    paths = {tuple(entry["path"]) for entry in commands}
    assert ("epic", "_tick") not in paths
    assert ("epic", "_review-node") not in paths
    assert not any(name.startswith("_") for path in paths for name in path)
    flags = {flag for e in commands for o in e["options"] for flag in o["flags"]}
    assert "--help" not in flags


def test_invocable_groups_are_listed_alongside_their_subcommands() -> None:
    paths = {tuple(entry["path"]) for entry in _schema()["commands"]}
    assert {("serve",), ("serve", "url"), ("doctor",)} <= paths
    assert ("epic",) not in paths


def _entry(schema: dict[str, Any], *path: str) -> dict[str, Any]:
    [entry] = [e for e in schema["commands"] if e["path"] == list(path)]
    return entry


def _option(entry: dict[str, Any], name: str) -> dict[str, Any]:
    [option] = [o for o in entry["options"] if name in o["flags"]]
    return option


def test_shape_of_every_entry() -> None:
    schema = _schema()
    assert schema["schema_version"] == 1
    for entry in schema["commands"]:
        assert set(entry) == {
            "path",
            "command",
            "help",
            "arguments",
            "options",
            "json",
            "exit_codes",
        }
        assert entry["command"] == " ".join(["kew", *entry["path"]])
        assert entry["help"], entry["command"]
        assert isinstance(entry["json"], bool)
        assert entry["json"] == any("--json" in o["flags"] for o in entry["options"])
        for option in entry["options"]:
            assert {"name", "type", "default", "required"} <= set(option)
            assert isinstance(option["type"], str)
            assert isinstance(option["required"], bool)
        for argument in entry["arguments"]:
            assert {"name", "type", "default", "required"} <= set(argument)


def test_shape_carries_concrete_types_defaults_and_json_support() -> None:
    schema = _schema()
    status = _entry(schema, "status")
    assert status["json"] is True
    assert _option(status, "--json")["type"] == "boolean"
    assert _option(status, "--json")["default"] is False

    init = _entry(schema, "init")
    assert init["json"] is False
    budget = _option(init, "--budget-per-issue")
    assert (budget["type"], budget["default"], budget["required"]) == (
        "float range",
        None,
        False,
    )
    assert _option(init, "--yes")["flags"] == ["--yes", "-y"]

    approve = _entry(schema, "approve")
    assert approve["arguments"] == [
        {
            "name": "ISSUE",
            "type": "integer",
            "required": True,
            "default": None,
            "multiple": False,
        }
    ]


def test_shape_names_an_exit_code_family_documented_in_the_scheme() -> None:
    text = (REPO_ROOT / "docs/cli/exit-codes.md").read_text()
    families = set(re.findall(r"^\| `([a-z-]+)` \|", text, re.MULTILINE))
    for entry in _schema()["commands"]:
        exit_codes = entry["exit_codes"]
        assert exit_codes["family"] == entry["path"][0]
        assert exit_codes["family"] in families, entry["command"]
        assert exit_codes["reference"].endswith("/cli/exit-codes/")
