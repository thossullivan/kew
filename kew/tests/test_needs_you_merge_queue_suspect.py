"""A held merge-queue main is a needs-you entry until it clears (#2982).

The queue's ``merge_queue_attention`` is one audit event, so it scrolls off
Monitor's default page after 200 newer events while the suspect row in
``merge_queue_suspects`` still blocks every merge. The needs-you set reads the
suspect table instead, so the header, Monitor and Work show the hold for exactly
as long as it stands.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command

from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.run_store import SqliteRunStore
from kew.service import needs_you
from kew.service.needs_you import MERGE_QUEUE_RUN_COMMAND


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Any]:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _suspects(store: Any, configured: str = "o/r") -> list[Any]:
    config = DispatchConfig(project=ProjectConfig(repo=configured))
    entries = needs_you.needs_you_set(store, config).entries
    return [e for e in entries if e.type == "merge_queue_suspect"]


def test_a_red_main_is_one_entry_naming_the_repo_and_the_fix(store: Any) -> None:
    store.suspect_merge_queue_main(
        "o/r", "#10 failed on it", red_on="a" * 40, failure="test_api failed"
    )

    [entry] = _suspects(store)

    assert entry.repo == "o/r" and entry.consequence == "epic"
    assert "o/r — main aaaaaaaaaaaa verified red (test_api failed)" in entry.reason
    assert "after #10 failed on it" in entry.reason
    assert "repair main" in entry.sentence
    assert entry.command == MERGE_QUEUE_RUN_COMMAND
    assert entry.attention_kind == "merge_queue_suspect"
    assert entry.age


def test_an_unverified_main_says_unverified(store: Any) -> None:
    store.suspect_merge_queue_main("o/r", "#11 landed unverified")

    [entry] = _suspects(store)

    assert "main not yet verified after #11 landed unverified" in entry.reason


def test_one_entry_per_held_repo_until_each_clears(store: Any) -> None:
    """With no repo to place them in, every held main is served."""
    store.suspect_merge_queue_main("o/a", "#1 landed unverified")
    store.suspect_merge_queue_main("o/b", "#2 landed unverified")
    assert sorted(e.repo for e in _suspects(store, configured="")) == ["o/a", "o/b"]

    store.clear_merge_queue_suspect("o/a")

    assert [e.repo for e in _suspects(store, configured="")] == ["o/b"]


def test_only_the_configured_repos_row_is_served(store: Any) -> None:
    """``kew merge queue run`` verifies the configured repository's main, so in
    a shared ledger another repo's row is not served here: run here, its
    command would verify ``o/a`` and leave ``o/b`` held."""
    store.suspect_merge_queue_main("o/a", "#1 landed unverified")
    store.suspect_merge_queue_main("o/b", "#2 landed unverified")

    [entry] = _suspects(store, configured="o/a")

    assert entry.repo == "o/a" and entry.command == MERGE_QUEUE_RUN_COMMAND


def test_with_no_repo_configured_the_row_names_its_checkout(store: Any) -> None:
    store.suspect_merge_queue_main("o/r", "#1 landed unverified")

    [entry] = _suspects(store, configured="")

    assert "`kew merge queue list` in o/r's checkout" in entry.reason


def test_the_command_is_one_the_cli_runs() -> None:
    command = get_command(app)
    for word in MERGE_QUEUE_RUN_COMMAND.split()[1:]:
        command = command.commands[word]  # type: ignore[attr-defined]
    assert command.name == "run"
