"""A stopped merge queue is a durable needs-you entry until it resumes (#2883).

When a train stops part-way or a merge lands a tree nobody verified, the queue
holds itself on ``merge_queue_suspects`` and says so once, as an audit event
and a notification. The needs-you set reads the ledger row rather than the
event, so the entry stays as long as the queue is stopped and leaves when a
pass verifies main green and resumes it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore
from kew.service.dashboard import assemble
from kew.service.needs_you import MERGE_QUEUE_RUN_COMMAND, needs_you_set

REPO = "acme/widgets"


def _config(repo: str = REPO) -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = repo
    return config


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _queue_rows(store: Any, now: datetime | None = None, repo: str = REPO) -> list[Any]:
    found = needs_you_set(store, _config(repo), now=now)
    assert "merge_queue" not in found.unavailable
    return [e for e in found.entries if e.type == "merge_queue_suspect"]


def test_a_suspect_queue_is_in_the_set_until_it_is_resumed(store: Any) -> None:
    assert _queue_rows(store) == []

    store.suspect_merge_queue_main(REPO, "a train stopped after #7 merged")
    later = datetime.now(UTC) + timedelta(hours=3)
    (row,) = _queue_rows(store, now=later)
    assert row.consequence == "epic"
    assert row.command == MERGE_QUEUE_RUN_COMMAND
    assert REPO in row.reason
    assert "a train stopped after #7 merged" in row.reason
    assert "kew merge queue list" in row.reason
    assert "3h" in row.reason
    assert datetime.fromisoformat(row.age) <= later - timedelta(hours=3)
    assert row.sentence

    # A verify that found main red keeps the queue stopped, and says which main.
    store.suspect_merge_queue_main(REPO, "ignored", red_on="ab" * 20)
    (row,) = _queue_rows(store)
    assert "abababababab verified red" in row.reason
    assert "a train stopped after #7 merged" in row.reason

    # A pass verifying main green is what resumes the queue.
    store.clear_merge_queue_suspect(REPO)
    assert _queue_rows(store) == []


def test_the_dashboard_counts_the_stopped_queue(store: Any) -> None:
    store.suspect_merge_queue_main(REPO, "#9 landed unverified", red_on="cd" * 20)
    rows = assemble(store, _config()).rows
    assert [r.type for r in rows].count("merge_queue_suspect") == 1


def test_the_resume_command_is_runnable_as_printed() -> None:
    group: Any = get_command(app)
    for word in MERGE_QUEUE_RUN_COMMAND.split()[1:]:
        group = group.commands[word]
    assert group.name == "run"


def test_merge_queue_list_shows_the_stop(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        opened.suspect_merge_queue_main(REPO, "#9 landed unverified")
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{tmp_path}"\n'
    )

    result = CliRunner().invoke(
        app, ["--project", str(project), "merge", "queue", "list"]
    )

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "#9 landed unverified" in result.output
    assert MERGE_QUEUE_RUN_COMMAND in result.output


def test_a_project_serves_only_its_own_stopped_queue(store: Any) -> None:
    """`kew merge queue run` acts on the project it is run in, so a row for
    another repo sharing the ledger would print a command that cannot reach it.
    Each project serves its own row, where the command works as printed."""
    store.suspect_merge_queue_main(REPO, "#9 landed unverified")
    store.suspect_merge_queue_main("acme/gadgets", "#3 landed unverified")

    assert [row.title for row in _queue_rows(store)] == [
        f"merge queue stopped — {REPO}"
    ]
    (gadgets,) = _queue_rows(store, repo="acme/gadgets")
    assert gadgets.title == "merge queue stopped — acme/gadgets"
    assert "#3 landed unverified" in gadgets.reason


def test_with_no_repo_every_stop_is_served_and_the_source_is_incomplete(
    store: Any,
) -> None:
    """A console that cannot tell which repo is its own fails closed: it shows
    every stop, each naming the checkout to resume from, and names the source
    unavailable so no count reads as complete."""
    assert "merge_queue" not in needs_you_set(store, _config("")).unavailable

    store.suspect_merge_queue_main(REPO, "#9 landed unverified")
    store.suspect_merge_queue_main("acme/gadgets", "#3 landed unverified")
    found = needs_you_set(store, _config(""))

    assert "merge_queue" in found.unavailable
    rows = [e for e in found.entries if e.type == "merge_queue_suspect"]
    assert sorted(row.title for row in rows) == [
        "merge queue stopped — acme/gadgets",
        f"merge queue stopped — {REPO}",
    ]
    for row in rows:
        repo = row.title.removeprefix("merge queue stopped — ")
        assert f"`kew merge queue list` in {repo}'s checkout" in row.reason


def test_serve_pins_the_detected_repo_so_its_stop_stays_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kew merge queue` stores a stop under the repo it detects; `kew serve`
    over the same blank `project.repo` must find it, not an empty set."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        opened.suspect_merge_queue_main(REPO, "#9 landed unverified")
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(f'[logging]\ndir = "{tmp_path}"\n')
    served: dict[str, Any] = {}

    def gh(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{REPO}\n")
        return subprocess.CompletedProcess(argv, 1, stdout="")

    def create_app(*_a: Any, **kw: Any) -> MagicMock:
        served["config"] = kw["config"]
        return MagicMock()

    monkeypatch.chdir(project)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.run", side_effect=gh),
        patch("kew.subprocess_utils.run", side_effect=gh),
        patch("kew.serve.app.create_app", side_effect=create_app),
        patch("uvicorn.run"),
    ):
        result = CliRunner().invoke(app, ["--project", str(project), "serve"])

    assert result.exit_code == 0, result.output
    assert served["config"].project.repo == REPO
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        found = needs_you_set(opened, served["config"])
    assert "merge_queue" not in found.unavailable
    assert [e.title for e in found.entries if e.type == "merge_queue_suspect"] == [
        f"merge queue stopped — {REPO}"
    ]


def test_a_landed_mismatch_is_never_said_as_verified_red(store: Any) -> None:
    """Only a failed verify of main is "verified red"; a merge that landed a
    tree nobody verified says so, with the mismatch."""
    store.suspect_merge_queue_main(
        REPO,
        "#9 landed unverified",
        mismatch_on="cd" * 20,
        failure="PR #9 landed with an unverified tree",
    )
    (row,) = _queue_rows(store)
    assert "verified red" not in row.reason
    assert (
        f"main {'cd' * 6} landed a tree nobody verified "
        "(PR #9 landed with an unverified tree)"
    ) in row.reason

    # main moved and a verify of it failed: now it is red, and says why.
    store.suspect_merge_queue_main(
        REPO, "ignored", red_on="ef" * 20, failure="test_api failed"
    )
    (row,) = _queue_rows(store)
    assert f"main {'ef' * 6} verified red (test_api failed)" in row.reason
    assert "nobody verified" not in row.reason
    assert "#9 landed unverified" in row.reason

    # A bare suspect (a pass dying mid-merge) keeps what was found.
    store.suspect_merge_queue_main(REPO, "a pass stopped while merging #10")
    (row,) = _queue_rows(store)
    assert f"main {'ef' * 6} verified red (test_api failed)" in row.reason


def test_merge_queue_list_names_only_this_projects_stop(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        opened.suspect_merge_queue_main("acme/gadgets", "#3 landed unverified")
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "{tmp_path}"\n'
    )

    result = CliRunner().invoke(
        app, ["--project", str(project), "merge", "queue", "list"]
    )

    assert result.exit_code == 0, result.output
    assert "stopped" not in result.output
    assert "#3 landed unverified" not in result.output


def _scenario() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / "frontend/fixtures/scenarios/dashboard-merge-queue-stopped.json"
    )
    served: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return served


def test_the_browser_check_draws_the_row_the_dashboard_serves(store: Any) -> None:
    """The ui-check's stopped-queue row is the one ``assemble`` serves.

    Field for field, all but ``age``, which is the ledger's own clock — so a
    backend that stopped offering the resume command fails here, not only in
    a browser.
    """
    store.suspect_merge_queue_main(
        REPO,
        "#9 landed unverified",
        mismatch_on="cd" * 20,
        failure="PR #9 landed with an unverified tree",
    )
    (suspect,) = store.list_merge_queue_suspects()
    rows = assemble(store, _config(), now=suspect.since + timedelta(hours=3)).rows
    (row,) = [r for r in rows if r.type == "merge_queue_suspect"]
    served = row.model_dump(mode="json")
    expected = _scenario()
    served.pop("age")
    expected.pop("age")
    assert served == expected
