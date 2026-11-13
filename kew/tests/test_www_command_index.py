"""The kew.dev command index is generated from the live command tree (#3340).

``scripts/www_command_index.py`` projects ``kew schema`` into
``www/src/lib/data/command-index.json``; these tests hold the committed file to
its output and show that a new command fails the check with the fix named.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kew.cli import app

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "www_command_index.py"
_spec = importlib.util.spec_from_file_location("www_command_index", SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
sys.modules["www_command_index"] = gen
_spec.loader.exec_module(gen)

NEW_COMMAND = "brand-new-command"


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A temporary root holding a copy of the committed index."""
    target = tmp_path / gen.WWW_INDEX
    target.parent.mkdir(parents=True)
    target.write_bytes((gen.ROOT / gen.WWW_INDEX).read_bytes())
    return tmp_path


@pytest.fixture
def added_command() -> Iterator[None]:
    """Register one more visible command on the shipped app, then remove it."""
    before = list(app.registered_commands)

    @app.command(NEW_COMMAND)
    def _brand_new(target: str) -> None:
        """A command the committed index has never seen."""

    try:
        yield
    finally:
        app.registered_commands[:] = before


@pytest.mark.policy
def test_committed_www_command_index_equals_the_live_command_tree() -> None:
    committed = (gen.ROOT / gen.WWW_INDEX).read_text(encoding="utf-8")
    assert committed == gen.render_index(gen.live_schema()), (
        f"{gen.WWW_INDEX} differs from the command tree; run `{gen.REGENERATE_COMMAND}`"
    )
    gen.check()
    assert gen.main(["--check"]) == 0

    commands = [entry["command"] for entry in json.loads(committed)["commands"]]
    assert {"kew init", "kew drive"} <= set(commands)


def test_a_new_command_fails_the_check_with_the_regenerate_command(
    repo_copy: Path, added_command: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(gen.DriftError) as excinfo:
        gen.check(repo_copy)
    message = str(excinfo.value)
    assert gen.REGENERATE_COMMAND in message
    assert str(gen.WWW_INDEX) in message

    assert gen.main(["--check"]) == 1
    assert gen.REGENERATE_COMMAND in capsys.readouterr().err

    assert gen.write(repo_copy) is True
    index = json.loads((repo_copy / gen.WWW_INDEX).read_text(encoding="utf-8"))
    entry = next(e for e in index["commands"] if e["path"] == [NEW_COMMAND])
    assert entry["arguments"] == [
        {"name": "TARGET", "required": True, "multiple": False}
    ]
    gen.check(repo_copy)
    assert gen.write(repo_copy) is False


def test_the_index_carries_no_defaults_or_docs_urls() -> None:
    index = json.loads((gen.ROOT / gen.WWW_INDEX).read_text(encoding="utf-8"))
    for entry in index["commands"]:
        assert set(entry) == {"command", "path", "help", "arguments", "options"}
        for option in entry["options"]:
            assert {"flags", "is_flag", "required"} <= set(option)
            assert set(option) <= {"flags", "is_flag", "required", "choices"}
        for argument in entry["arguments"]:
            assert {"name", "required", "multiple"} <= set(argument)
            assert set(argument) <= {"name", "required", "multiple", "choices"}


def test_the_index_keeps_what_a_command_line_needs_to_run() -> None:
    """Required options, choices and variadic arguments survive the projection.

    The www test checks every console line on the page against the index, so a
    line missing ``--head`` or naming a value outside a choice list must be
    visible there, and a variadic argument must accept more than one word.
    """
    index = json.loads((gen.ROOT / gen.WWW_INDEX).read_text(encoding="utf-8"))
    by_command = {entry["command"]: entry for entry in index["commands"]}

    merge = by_command["kew epic merge"]
    head = next(o for o in merge["options"] if "--head" in o["flags"])
    assert head["required"] is True

    names = by_command["kew skills install"]["arguments"]
    assert names == [{"name": "NAMES", "required": False, "multiple": True}]

    schema = {
        "commands": [
            {
                "command": "kew pick",
                "path": ["pick"],
                "help": "Pick one.",
                "arguments": [
                    {
                        "name": "COLOUR",
                        "type": "choice",
                        "choices": ["red", "blue"],
                        "required": True,
                        "default": None,
                        "multiple": False,
                    }
                ],
                "options": [
                    {
                        "name": "--mode",
                        "flags": ["--mode"],
                        "type": "choice",
                        "choices": ["fast", "slow"],
                        "required": True,
                        "default": "/Users/someone/fast",
                        "is_flag": False,
                        "multiple": False,
                    }
                ],
            }
        ]
    }
    (entry,) = json.loads(gen.render_index(schema))["commands"]
    assert entry["arguments"] == [
        {
            "name": "COLOUR",
            "required": True,
            "multiple": False,
            "choices": ["red", "blue"],
        }
    ]
    assert entry["options"] == [
        {
            "flags": ["--mode"],
            "is_flag": False,
            "required": True,
            "choices": ["fast", "slow"],
        }
    ]
