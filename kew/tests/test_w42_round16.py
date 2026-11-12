"""Round 16: required orphan comment operations and status process failures."""

import json
import subprocess
from contextlib import nullcontext
from functools import partial
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

import kew.cli as cli
from kew.config import DispatchConfig
from kew.outcomes import (
    _ORPHAN_COMMENT_MARKER,
    _orphan_comment_exists,
    _QueryFailure,
    reconcile_orphan_prs,
)
from kew.run_store import SqliteRunStore
from kew.selector import GhQueryError
from kew.subprocess_utils import run_with_backoff

PRIVATE = "private remote detail: token=secret\x1b[31m"
STATUS_FAILURE = (
    "GitHub query failed — cannot determine queue status "
    "(this is NOT an empty queue): query_unavailable."
)
PROCESS_ERRORS = [OSError, subprocess.SubprocessError, GhQueryError, ValueError]


def _reply(stdout="", *, code=0):
    return subprocess.CompletedProcess([], code, stdout, PRIVATE if code else "")


def _config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.branches.prefix = "agent/"
    config.logging.dir = str(tmp_path / "logs")
    return config


def _orphan_remote(states, *, error=None):
    posted = []

    def remote(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return _reply(
                json.dumps(
                    [
                        {"number": n, "url": f"u/{n}", "headRefName": f"agent/{n}"}
                        for n in range(1, len(states) + 1)
                    ]
                )
            )
        number = int(cmd[3])
        state = states[number - 1]
        operation = cmd[2]
        if (operation, state) in (
            ("view", "read_failure"),
            ("comment", "write_failure"),
        ):
            if error is not None:
                raise error(PRIVATE)
            return _reply(code=1)
        if operation == "view":
            return _reply(_ORPHAN_COMMENT_MARKER if state == "already_warned" else "")
        assert operation == "comment"
        posted.append(number)
        return _reply()

    return remote, posted


def _sync(tmp_path, monkeypatch, states, *, error=None):
    config = _config(tmp_path, monkeypatch)
    remote, posted = _orphan_remote(states, error=error)
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch("kew.outcomes.run", side_effect=remote),
    ):
        result = CliRunner().invoke(cli.app, ["report", "sync"])
        saved = [row.pr_number for row in store.list_orphan_prs()]
    return result, posted, saved


@pytest.mark.parametrize("error", [None, *PROCESS_ERRORS])
@pytest.mark.parametrize("phase", ["read", "write"])
def test_round16_sync_comment_failure_exits_two(tmp_path, monkeypatch, phase, error):
    result, posted, saved = _sync(
        tmp_path, monkeypatch, [f"{phase}_failure"], error=error
    )
    assert result.exit_code == 2, result.output
    assert "synced 0, no PR 0, query failures 1" in result.output
    assert f"comment-{phase} failures 1" in result.output
    category = "query_unavailable" if error else "github_query_failed"
    assert category in result.output
    assert "private remote detail" not in result.output
    assert "Traceback" not in result.output
    assert posted == []
    assert saved == [1]


@pytest.mark.parametrize("phase", ["read", "write"])
@pytest.mark.parametrize("failed_first", [False, True])
def test_round16_sync_retains_partial_progress(
    tmp_path, monkeypatch, phase, failed_first
):
    states = (
        [f"{phase}_failure", "posted"]
        if failed_first
        else ["posted", f"{phase}_failure"]
    )
    result, posted, saved = _sync(tmp_path, monkeypatch, states)
    assert result.exit_code == 2, result.output
    assert "query failures 1" in result.output
    assert "warnings posted 1" in result.output
    assert posted == ([2] if failed_first else [1])
    assert saved == [1, 2]


@pytest.mark.parametrize("error", [None, *PROCESS_ERRORS])
def test_round16_unknown_comment_is_not_warned(error):
    with patch(
        "kew.outcomes.run",
        return_value=_reply(code=1),
        side_effect=error(PRIVATE) if error else None,
    ):
        result = _orphan_comment_exists(1, "o/r")
    assert result is not True
    assert isinstance(result, _QueryFailure)


def test_round16_reconciliation_reports_each_operation(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    remote, posted = _orphan_remote(
        ["posted", "read_failure", "write_failure", "already_warned"]
    )
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.outcomes.run", side_effect=remote),
    ):
        result = reconcile_orphan_prs(store, config)
        assert not isinstance(result, (list, _QueryFailure))
        assert [orphan.number for orphan in result.orphans] == [1, 2, 3, 4]
        assert result.warnings_posted == (1,)
        assert result.already_warned == (4,)
        assert [(f.pr_number, f.category) for f in result.comment_read_failures] == [
            (2, "github_query_failed")
        ]
        assert [(f.pr_number, f.category) for f in result.comment_write_failures] == [
            (3, "github_query_failed")
        ]
        assert result.query_failures == 2
        assert len(store.list_orphan_prs()) == 4
    assert posted == [1]


def test_round16_sync_counts_each_failed_operation(tmp_path, monkeypatch):
    result, posted, saved = _sync(
        tmp_path,
        monkeypatch,
        ["posted", "read_failure", "write_failure", "already_warned", "read_failure"],
    )
    assert result.exit_code == 2, result.output
    assert "query failures 3" in result.output
    assert "warnings posted 1, already warned 1" in result.output
    assert "comment-read failures 2, comment-write failures 1" in result.output
    assert posted == [1]
    assert saved == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("stdout", [None, b""])
def test_round16_unknown_comment_response_is_not_warned(stdout):
    with patch("kew.outcomes.run", return_value=_reply(stdout)):
        result = _orphan_comment_exists(1, "o/r")
    assert result == _QueryFailure("invalid_response")


@pytest.mark.parametrize("export", [False, True])
@pytest.mark.parametrize("error", PROCESS_ERRORS)
def test_round16_status_process_failure_is_static(tmp_path, monkeypatch, export, error):
    config = _config(tmp_path, monkeypatch)

    def spawn(cmd, **kwargs):
        if cmd[:3] == ["gh", "auth", "status"]:
            return _reply()
        raise error(PRIVATE)

    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch("kew.cli.run", side_effect=spawn),
        patch("kew.subprocess_utils.run", side_effect=spawn),
    ):
        result = CliRunner().invoke(
            cli.app, ["status", *(["--json"] if export else [])]
        )
    assert result.exit_code == 2, result.output
    assert STATUS_FAILURE in result.stderr
    assert "private remote detail" not in result.output
    assert "Traceback" not in result.output
    assert "Queue status" not in result.output
    if export:
        assert result.stdout == ""


@pytest.mark.parametrize("export", [False, True])
def test_round16_status_retries_transient_failure(tmp_path, monkeypatch, export):
    config = _config(tmp_path, monkeypatch)
    attempts = []
    sleep = Mock()

    def spawn(cmd, **kwargs):
        if cmd[:3] == ["gh", "auth", "status"]:
            return _reply()
        assert cmd[:3] == ["gh", "issue", "list"]
        attempts.append(cmd)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 503")
        return _reply("[]" if export else "0")

    retry = partial(run_with_backoff, _sleep=sleep)
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch("kew.cli.detect_orphan_prs", return_value=[]),
        patch("kew.cli.run", side_effect=spawn),
        patch("kew.subprocess_utils.run", side_effect=spawn),
        patch("kew.cli.run_with_backoff", side_effect=retry, create=True),
        patch("kew.selector.run_with_backoff", side_effect=retry),
    ):
        result = CliRunner().invoke(
            cli.app, ["status", *(["--json"] if export else [])]
        )
    assert result.exit_code == 0, result.output
    assert len(attempts) == (2 if export else 6)
    assert attempts[0] == attempts[1]
    sleep.assert_called_once_with(60.0)
    if export:
        assert json.loads(result.stdout)["queue"]["count"] == 0
    else:
        assert result.output.count(" 0\n") == 5
