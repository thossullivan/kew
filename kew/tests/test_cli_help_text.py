"""Every rendered ``--help`` reads as product text: no tracker issue numbers,
no internal class or module names, no swallowed ``[config]`` sections."""

from __future__ import annotations

import re
from functools import cache

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from kew.cli import app

_WIDE = {"COLUMNS": "240", "TERMINAL_WIDTH": "240"}
_ISSUE_NUMBER = re.compile(r"#\d+")
_INTERNAL_SYMBOLS = ("DriverStatus", "kew.graph.")


def _command_paths(
    command: click.Command, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    paths = [prefix]
    if isinstance(command, click.Group):
        for name, sub in sorted(command.commands.items()):
            paths.extend(_command_paths(sub, (*prefix, name)))
    return paths


@cache
def _rendered_help() -> dict[tuple[str, ...], str]:
    runner = CliRunner()
    rendered: dict[tuple[str, ...], str] = {}
    for path in _command_paths(get_command(app)):
        result = runner.invoke(app, [*path, "--help"], env=_WIDE)
        assert result.exit_code == 0, (path, result.output)
        rendered[path] = result.output
    return rendered


def _help(*path: str) -> str:
    return " ".join(_rendered_help()[path].split())


def test_the_walk_reaches_hidden_and_nested_commands() -> None:
    """The help walk is only a check if it covers the whole tree."""
    rendered = _rendered_help()
    assert ("epic", "retry") in rendered
    assert ("sandbox", "smoke") in rendered
    hidden = [
        path
        for path in _command_paths(get_command(app))
        if path and _lookup(path).hidden
    ]
    assert hidden, "expected at least one hidden command in the walk"
    assert all(path in rendered for path in hidden)


def _lookup(path: tuple[str, ...]) -> click.Command:
    command: click.Command = get_command(app)
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


def test_no_rendered_help_names_a_tracker_issue_number() -> None:
    offenders = {
        " ".join(("kew", *path)): sorted(set(_ISSUE_NUMBER.findall(text)))
        for path, text in _rendered_help().items()
        if _ISSUE_NUMBER.search(text)
    }
    assert not offenders, offenders


def test_no_rendered_help_names_an_internal_symbol() -> None:
    offenders = {
        " ".join(("kew", *path)): [s for s in _INTERNAL_SYMBOLS if s in text]
        for path, text in _rendered_help().items()
        if any(s in text for s in _INTERNAL_SYMBOLS)
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    ("path", "section"),
    (
        (("sandbox", "smoke"), "[sandbox] images.<runner>"),
        (("serve",), "[flight_recorder]"),
    ),
)
def test_bracketed_config_sections_survive_in_rendered_help(
    path: tuple[str, ...], section: str
) -> None:
    assert section in _help(*path)


def test_serve_port_help_shows_no_zero_default() -> None:
    text = _help("serve")
    port = text[text.index("--port") :]
    port = port[: port.index("--", 2)]
    assert "[default: 0]" not in port
    assert "[flight_recorder].port" in port


def test_epic_retry_offers_no_epic_prefix_it_would_ignore() -> None:
    assert "--epic-prefix" not in _help("epic", "retry")
