"""Generate the kew.dev command index from kew's live command tree.

``www/src/lib/data/command-index.json`` is the list the site's command index
and console lines are checked against (#3340). It is read off the same Click
tree ``kew schema`` prints (``kew.cli.schema.command_schema``), so its count and
contents cannot drift from what ships.

The file is a projection of that schema, not a copy: each command keeps its
path, help line, argument names with whether each is required or takes
several words, option flags with whether each is required, and any choice
list: enough for the www test to tell whether a console line on the page runs
as written. Defaults are left out, because the schema turns path defaults into
strings, so a raw copy would differ between machines and could put an
operator's local path on the public page.

The file is never edited by hand. ``tests/test_www_command_index.py`` compares
it to this script's output and names the command below when they differ.

    uv run python scripts/www_command_index.py           rewrite the file
    uv run python scripts/www_command_index.py --check   read-only, exit 1 on drift

Run from ``kew/``. Rendering is deterministic: the same command tree always
produces the same bytes, and there is no build-time date stamp.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from typer.main import get_command

REGENERATE_COMMAND = "cd kew && uv run python scripts/www_command_index.py"

ROOT = Path(__file__).resolve().parents[2]
WWW_INDEX = Path("www/src/lib/data/command-index.json")
INDEX_SCHEMA = "kew/command-index@1"


class DriftError(RuntimeError):
    """The committed index is missing or differs from the live command tree."""


def _choices(param: dict[str, Any]) -> dict[str, Any]:
    """A param's choice list, only when it has one."""
    return {"choices": param["choices"]} if "choices" in param else {}


def _argument(arg: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": arg["name"],
        "required": arg["required"],
        "multiple": arg["multiple"],
        **_choices(arg),
    }


def _option(option: dict[str, Any]) -> dict[str, Any]:
    return {
        "flags": option["flags"],
        "is_flag": option["is_flag"],
        "required": option["required"],
        **_choices(option),
    }


def _project(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": entry["command"],
        "path": entry["path"],
        "help": entry["help"],
        "arguments": [_argument(arg) for arg in entry["arguments"]],
        "options": [_option(option) for option in entry["options"]],
    }


def render_index(schema: dict[str, Any]) -> str:
    """The committed JSON text for a ``command_schema`` result."""
    index = {
        "schema": INDEX_SCHEMA,
        "commands": [_project(entry) for entry in schema["commands"]],
    }
    return json.dumps(index, indent=2, ensure_ascii=False) + "\n"


def live_schema() -> dict[str, Any]:
    """The schema ``kew schema`` prints, built from the shipped CLI."""
    from kew.cli import app
    from kew.cli.schema import command_schema

    return command_schema(get_command(app))


def check(root: Path = ROOT, schema: dict[str, Any] | None = None) -> None:
    """Raise :class:`DriftError` naming the regenerate command on any drift."""
    text = render_index(schema if schema is not None else live_schema())
    target = root / WWW_INDEX
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        raise DriftError(
            f"{WWW_INDEX} differs from kew's command tree; run"
            f" `{REGENERATE_COMMAND}` and commit the result"
        )


def write(root: Path = ROOT, schema: dict[str, Any] | None = None) -> bool:
    """Rewrite the index if it is stale; return whether it was written."""
    text = render_index(schema if schema is not None else live_schema())
    target = root / WWW_INDEX
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return False
    target.write_text(text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 instead of rewriting on drift"
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            check()
            print("www command index matches the command tree")
        elif write():
            print(f"wrote {WWW_INDEX}")
        else:
            print("www command index already matches the command tree")
    except DriftError as exc:
        print(f"www command index: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
