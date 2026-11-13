"""A live driver that stopped beating its heartbeat blocks its epic visibly (#3331).

kew never takes an epic's driver lock from the live process that took it
(#2227), so a driver stopped with Ctrl-Z or SIGSTOP holds the epic until a
person kills it. These tests pin that the stall is not silent: the tick it
locks out reads STALLED, the drive raises one attention event naming the pid,
and ``kew drive status`` / ``kew epic status`` show it. The holder is a real
subprocess that takes the lock with kew's own code and is then really stopped.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from typer.testing import CliRunner

import kew.cli.drive as drive_cli
from kew.cli import app, epic_app
from kew.cli.epic import _epic_lock_path
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import current_holder, request_stop
from kew.drive_inbox import Notice
from kew.graph.domain import Graph, Node, NodeStatus
from kew.graph.driver.attention import DRIVER_LOCK_ITEM
from kew.graph.driver.lock import (
    HEARTBEAT_STALE_SECONDS,
    lock_holder,
    single_driver_lock,
    stalled_holder,
)
from kew.graph.driver.result import TerminalSignal, TickSummary
from kew.process_groups import process_start_token
from kew.run_store import SqliteRunStore

driver_env = _fx.driver_env
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

runner = CliRunner()

_LOCKED_OUT = 11  # the `kew epic _tick` child's locked-out exit (cli.epic)
_ROUNDS = 4

# The holder: take the lock through kew's own code (so the record names this
# process's real start token), backdate its heartbeat past the stale line, say
# so, and wait — to be stopped by the test.
_HOLDER = r"""
import json, sys, threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from kew.graph.driver import lock as lock_mod

lock_mod.HEARTBEAT_REFRESH_SECONDS = 3600.0
path = Path(sys.argv[1])
with lock_mod.single_driver_lock(path, holder=sys.argv[2]) as got:
    assert got
    record = json.loads(path.read_text())
    age = timedelta(seconds=int(sys.argv[3]))
    record["heartbeat"] = (datetime.now(UTC) - age).isoformat()
    with open(path, "r+") as f:
        f.write(json.dumps(record))
        f.truncate()
    print("held", flush=True)
    threading.Event().wait(600)
"""


@contextmanager
def _stopped_holder(lock: Path, epic: str) -> Iterator[int]:
    """A real process holding ``lock`` with a stale heartbeat, SIGSTOPped."""
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(lock),
            epic,
            str(HEARTBEAT_STALE_SECONDS + 60),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        os.kill(proc.pid, signal.SIGSTOP)
        _, status = os.waitpid(proc.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        yield proc.pid
    finally:
        proc.kill()  # SIGKILL ends a stopped process too
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


# --- the lock -----------------------------------------------------------------


def test_a_stopped_holder_is_not_stolen_and_reads_stalled(tmp_path: Path) -> None:
    lock = tmp_path / "epic.lock"
    with _stopped_holder(lock, "dag-hung") as pid:
        with single_driver_lock(lock, holder="dag-new", takeover=True) as got:
            assert got is False
        holder = lock_holder(lock)
        assert holder is not None
        assert (holder.pid, holder.epic) == (pid, "dag-hung")
        assert holder.started == process_start_token(pid)
        stalled = stalled_holder(lock)
        assert stalled is not None
        assert stalled.pid == pid


def test_a_beating_holder_and_an_older_kews_record_are_not_stalled(
    tmp_path: Path,
) -> None:
    """Only the live process that took the lock AND stopped beating is
    stalled: a beating holder is a driver at work, and a stale record without a
    start token is stolen by the next acquirer, so neither asks a person."""
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-live") as got:
        assert got is True
        assert stalled_holder(lock) is None

    legacy = tmp_path / "legacy.lock"
    stale = (
        datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_SECONDS + 60)
    ).isoformat()
    legacy.write_text(
        json.dumps(
            {
                "epic": "dag-old",
                "pid": os.getpid(),
                "held_since": stale,
                "heartbeat": stale,
            }
        )
    )
    with open(legacy, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lock_holder(legacy) is not None
        assert stalled_holder(legacy) is None


# --- the tick -----------------------------------------------------------------


def test_a_tick_locked_out_by_a_stopped_holder_is_stalled(driver_env: Any) -> None:
    with _stopped_holder(driver_env.lock_path, "dag-hung"):
        result = driver_env.advance()
    assert result.locked_out is True
    assert result.signal is TerminalSignal.STALLED


def test_a_tick_locked_out_by_a_beating_driver_is_still_progressing(
    driver_env: Any,
) -> None:
    with single_driver_lock(driver_env.lock_path, holder="dag-live") as got:
        assert got is True
        result = driver_env.advance()
    assert result.locked_out is True
    assert result.signal is TerminalSignal.PROGRESSING


# --- the drive ----------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> DispatchConfig:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        for ref in ("epic:a", "epic:b"):
            s.register_epic(ref)
    config = DispatchConfig(project=ProjectConfig(repo="o/r"))
    config.logging.dir = str(log_dir)
    config.parallel.max_workers = 8
    return config


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """An installed kew package the drive watches, which never moves."""
    pkg = tmp_path / "site" / "kew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VERSION = 1\n")
    return pkg


def _locked_out_ticker(ledger: Any) -> Callable[[str, int], int]:
    """Every tick is locked out, as a tick child whose epic lock is held
    elsewhere exits; the drive is stopped after ``_ROUNDS`` rounds."""
    ticks: list[str] = []

    def tick(name: str, _admit_cap: int) -> int:
        ticks.append(name)
        if len(ticks) == 2 * _ROUNDS:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
        return _LOCKED_OUT

    return tick


def _status(
    cfg: DispatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.setenv("KEW_LOG_DIR", cfg.logging.dir)

    def invoke(*args: str) -> Any:
        return runner.invoke(app, ["drive", "status", *args])

    return invoke


def test_the_drive_raises_one_attention_event_naming_the_stopped_pid(
    cfg: DispatchConfig, source: Path
) -> None:
    lock = _epic_lock_path(cfg, "epic:a")
    notices: list[Notice] = []
    with (
        SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as ledger,
        _stopped_holder(lock, "epic:a") as pid,
    ):
        drive_cli.run_drive(
            cfg,
            _locked_out_ticker(ledger),
            0,
            notify=notices.append,
            source=source,
        )

    stalls = [n for n in notices if n.event.item == DRIVER_LOCK_ITEM]
    assert len(stalls) == 1, [n.event.as_line() for n in notices]
    event = stalls[0].event
    assert event.epic == "epic:a"
    assert stalls[0].owner.value == "operator"
    assert f"pid {pid}" in event.reason
    assert f"kill -9 {pid}" in event.action
    assert event.identity.startswith(f"pid {pid} started ")


def test_drive_status_shows_the_stalled_lock(
    cfg: DispatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = _status(cfg, tmp_path, monkeypatch)
    lock = _epic_lock_path(cfg, "epic:b")
    with _stopped_holder(lock, "epic:b") as pid:
        text = status()
        document = json.loads(status("--json").output)

    assert "epic epic:b: STALLED" in text.output, text.output
    assert f"held by pid {pid}" in text.output
    assert f"kill -9 {pid}" in text.output
    assert [s["epic"] for s in document["stalled_locks"]] == ["epic:b"]
    assert document["stalled_locks"][0]["pid"] == pid
    assert f"kill -9 {pid}" in document["stalled_locks"][0]["action"]


def test_drive_status_lists_no_stall_for_a_beating_driver(
    cfg: DispatchConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = _status(cfg, tmp_path, monkeypatch)
    with single_driver_lock(_epic_lock_path(cfg, "epic:b"), holder="epic:b"):
        document = json.loads(status("--json").output)
    assert document["stalled_locks"] == []


# --- kew epic status ----------------------------------------------------------

_EPIC = "dag-1"


@pytest.fixture
def epic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    """The config ``kew epic status`` loads, set through the environment the
    operator's shell would set."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_REPO", "o/r")
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path)
    return config


def _epic_status(*args: str) -> Any:
    """``kew epic status`` with the forge reads (graph and facts) answered."""
    graph = Graph(
        [Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=_EPIC)], []
    )
    summary = TickSummary(
        counts={"todo": 1},
        frontier=("1",),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=1,
        signal=TerminalSignal.PROGRESSING,
    )
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
    ):
        return runner.invoke(epic_app, ["status", _EPIC, *args])


def test_epic_status_reads_stalled_and_names_the_pid_to_kill(
    epic_config: DispatchConfig,
) -> None:
    with _stopped_holder(_epic_lock_path(epic_config, _EPIC), _EPIC) as pid:
        text = _epic_status()
        document = json.loads(_epic_status("--json").stdout)

    assert text.exit_code == 0, text.output
    assert "Signal: stalled" in text.output
    assert "progressing" not in text.output.lower()
    assert f"held by pid {pid}" in text.output
    assert f"kill -9 {pid}" in text.output
    assert document["signal"] == "stalled"
    assert document["lock_holder"]["stalled"] is True
    assert f"kill -9 {pid}" in document["lock_holder"]["action"]


def test_epic_status_with_a_beating_driver_keeps_its_signal(
    epic_config: DispatchConfig,
) -> None:
    with single_driver_lock(_epic_lock_path(epic_config, _EPIC), holder=_EPIC):
        document = json.loads(_epic_status("--json").stdout)
    assert document["signal"] == "progressing"
    assert document["lock_holder"]["stalled"] is False
    assert document["lock_holder"]["action"] is None


# --- a damaged lock file ------------------------------------------------------

_DAMAGED = {
    "pid-overflow": (
        b'{"pid": 99999999999999999999, "heartbeat": "2020-01-01T00:00:00+00:00",'
        b' "started": "x"}'
    ),
    "pid-text": b'{"pid": "abc", "heartbeat": "2020-01-01T00:00:00+00:00"}',
    "pid-list": b'{"pid": [1], "heartbeat": "2020-01-01T00:00:00+00:00"}',
    "not-utf8": b'\xff\xfe{"pid": 1}',
}


@contextmanager
def _held_damaged(lock: Path, raw: bytes) -> Iterator[None]:
    """``raw`` in ``lock``, flocked by a live holder (this process)."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(raw)
    with open(lock, "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@pytest.mark.parametrize("raw", _DAMAGED.values(), ids=_DAMAGED.keys())
def test_a_damaged_lock_file_reads_as_no_stall(tmp_path: Path, raw: bytes) -> None:
    lock = tmp_path / "epic.lock"
    with _held_damaged(lock, raw):
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.pid == 0
        assert stalled_holder(lock) is None


@pytest.mark.parametrize("raw", _DAMAGED.values(), ids=_DAMAGED.keys())
def test_a_damaged_lock_file_does_not_stop_the_drive(
    cfg: DispatchConfig, source: Path, raw: bytes
) -> None:
    notices: list[Notice] = []
    with (
        SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as ledger,
        _held_damaged(_epic_lock_path(cfg, "epic:a"), raw),
    ):
        drive_cli.run_drive(
            cfg,
            _locked_out_ticker(ledger),
            0,
            notify=notices.append,
            source=source,
        )
    assert [n for n in notices if n.event.item == DRIVER_LOCK_ITEM] == []


@pytest.mark.parametrize("raw", _DAMAGED.values(), ids=_DAMAGED.keys())
def test_drive_status_reads_past_a_damaged_lock_file(
    cfg: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    status = _status(cfg, tmp_path, monkeypatch)
    with _held_damaged(_epic_lock_path(cfg, "epic:b"), raw):
        text = status()
        document = status("--json")
    assert text.exit_code == 1, text.output  # no drive runs; nothing crashed
    assert document.exit_code == 1, document.output
    assert json.loads(document.stdout)["stalled_locks"] == []
