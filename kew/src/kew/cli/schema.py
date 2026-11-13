"""`kew schema`: print kew's command tree as JSON (#2506).

An agent discovers commands from this output instead of scraping Rich
``--help`` text. Everything is read off the live Click tree Typer builds, so the
output cannot drift from what ships; hidden commands and options are left out.
"""

from __future__ import annotations

import enum
import inspect
import json
from collections.abc import Iterator
from pathlib import PurePath
from typing import Any

import typer
from typer.main import get_command

from kew import cli as _c
from kew.links import docs_url

SCHEMA_VERSION = 1
EXIT_CODES_PAGE = "cli/exit-codes"


def _json_default(value: Any) -> Any:
    """A JSON value for a parameter default; ``None`` when there is none.

    Click marks "no default" with a ``Sentinel`` enum member, its ``UNSET``, and
    a callable default is computed per invocation: neither has a static value.
    """
    if value is None or callable(value) or type(value).__name__ == "Sentinel":
        return None
    if isinstance(value, enum.Enum):
        return _json_default(value.value)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_json_default(item) for item in value]
    return str(value)


# Click objects are read by attribute, never imported: kew does not depend on
# click directly (see the import-linter contract in pyproject.toml).
def _type(param: Any) -> dict[str, Any]:
    described: dict[str, Any] = {"type": param.type.name}
    choices = getattr(param.type, "choices", None)
    if choices is not None:
        described["choices"] = [str(_json_default(choice)) for choice in choices]
    return described


def _argument(param: Any) -> dict[str, Any]:
    return {
        "name": param.human_readable_name,
        **_type(param),
        "required": param.required,
        "default": _json_default(param.default),
        "multiple": param.nargs != 1,
    }


def _option(param: Any) -> dict[str, Any]:
    return {
        "name": max(param.opts, key=len),
        "flags": [*param.opts, *param.secondary_opts],
        **_type(param),
        "required": param.required,
        "default": _json_default(param.default),
        "is_flag": param.is_flag,
        "multiple": param.multiple,
    }


def _help_line(command: Any) -> str:
    """The first paragraph of a command's help, on one line."""
    text = inspect.cleandoc(command.help or command.short_help or "")
    return " ".join(text.split("\n\n", 1)[0].split())


def _entry(path: tuple[str, ...], command: Any) -> dict[str, Any]:
    params = [param for param in command.params if not getattr(param, "hidden", False)]
    options = [_option(p) for p in params if p.param_type_name == "option"]
    return {
        "path": list(path),
        "command": " ".join(("kew", *path)),
        "help": _help_line(command),
        "arguments": [_argument(p) for p in params if p.param_type_name == "argument"],
        "options": options,
        "json": any("--json" in option["flags"] for option in options),
        "exit_codes": {"family": path[0], "reference": docs_url(EXIT_CODES_PAGE)},
    }


def _runnable(
    command: Any, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every visible command a caller can run: leaves, and groups that run bare."""
    children = getattr(command, "commands", None)
    if path and (children is None or getattr(command, "invoke_without_command", False)):
        yield path, command
    for name, child in sorted((children or {}).items()):
        if not child.hidden:
            yield from _runnable(child, (*path, name))


def command_schema(root: Any) -> dict[str, Any]:
    """The machine-readable command tree under ``root``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "commands": [_entry(path, command) for path, command in _runnable(root)],
    }


def schema() -> None:
    """Print every visible kew command, its options, and exit codes as JSON."""
    typer.echo(json.dumps(command_schema(get_command(_c.app)), indent=2))
