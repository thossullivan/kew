"""The drive restarts on new code at a quiet point (#3197).

A running drive records its code identity at start and reads it again before
every launch. When the code under it moves — or a newer kew migrates the
ledger past this drive's schema — it launches nothing new, raises one
attention event, and re-execs itself once its tick has finished; when the exec
fails it exits with a code launchd restarts and ``kew drive status`` names.

Every test runs on a real ledger and a real source tree; only the per-epic
tick (a pinned ``kew epic _tick`` child), the run-request pass (the hook a
test moves the code from between ticks) and the exec (the process boundary)
are replaced. The drive runs at interval 0, so it never sleeps.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

import kew.cli.drive as drive_cli
from kew import run_requests as rr
from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import (
    DRIVE_EXIT_KEY,
    DRIVE_HOLDER_KEY,
    DriveHolder,
    DriveLease,
    DriveRefusal,
    acquire_drive,
    current_holder,
    read_drive,
    request_stop,
)
from kew.drive_code import (
    DRIVE_REFUSAL,
    DRIVE_RESTART_EXIT,
    DRIVE_RESUME_ENV,
    REFUSAL_REPEAT_S,
    SchemaRefusal,
    code_identity,
    write_refusal,
)
from kew.drive_inbox import Notice
from kew.drive_requests import RequestRunner
from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

runner = CliRunner()

CONTINUE = 10  # a PROGRESSING/AWAITING_MERGES tick child (cli.epic)
CHANGED = "kew changed under the drive: restarting"
_BACKSTOP = 12


class _Replaced(Exception):
    """The fake exec's stand-in for a process image that was replaced."""


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
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
def ledger(cfg: Any) -> Iterator[Any]:
    """A second connection to the drive's ledger — the operator's shell."""
    with SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as s:
        yield s


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """An installed kew package the drive watches: no checkout around it."""
    pkg = tmp_path / "site" / "kew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VERSION = 1\n")
    return pkg


@pytest.fixture
def status(cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``kew drive status`` over the test's ledger, found through the
    environment the operator's shell would set."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(project))
    monkeypatch.setenv("KEW_LOG_DIR", cfg.logging.dir)

    def invoke(*args: str) -> Any:
        return runner.invoke(app, ["drive", "status", *args])

    return invoke


def _move(source: Path) -> None:
    (source / "__init__.py").write_text("VERSION = 2  # moved\n")


class _Exec:
    """The process boundary: records each exec, and either replaces the
    process (raises :class:`_Replaced`) or fails like a missing interpreter."""

    def __init__(self, ledger: Any, *, fails: bool = False) -> None:
        self.ledger, self.fails = ledger, fails
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []
        self.holder_at_exec: DriveHolder | None = None
        self.order: list[str] = []

    def __call__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        self.calls.append((path, argv, env))
        self.order.append("exec")
        self.holder_at_exec = current_holder(self.ledger)
        if self.fails:
            raise OSError(2, "No such file or directory")
        raise _Replaced


def _ticker(
    ledger: Any, plan: Callable[[str, int], None] | None = None
) -> tuple[list[str], Callable[[str, int], int]]:
    """A tick that records each epic it ran. Past ``_BACKSTOP`` ticks it asks
    the drive to stop, so a drive that misses a restart ends the test by
    failing it rather than ticking forever."""
    ticks: list[str] = []

    def tick(name: str, _admit_cap: int) -> int:
        ticks.append(name)
        if plan is not None:
            plan(name, len(ticks))
        if len(ticks) == _BACKSTOP:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
        return CONTINUE

    return ticks, tick


def _run(
    cfg: Any,
    tick: Any,
    source: Path,
    execv: _Exec,
    between: Callable[[], None] | None = None,
    notices: list[Notice] | None = None,
) -> list[Notice]:
    """Drive at interval 0; ``between`` runs at each run-request pass — at the
    start of every round and after every tick. ``notices`` collects the
    attention the drive raised, even when it ends by raising."""
    notices = [] if notices is None else notices
    requests = None if between is None else (lambda _store, _lease: between)
    drive_cli.run_drive(
        cfg,
        tick,
        0,
        notify=notices.append,
        requests=requests,
        source=source,
        execv=execv,
    )
    return notices


def _after_round_one(ticks: list[str], source: Path) -> Callable[[], None]:
    """Move the code once the first round's two ticks have finished."""

    def between() -> None:
        if len(ticks) == 2:
            _move(source)

    return between


def _restarts(notices: list[Notice]) -> list[str]:
    return [n.event.reason for n in notices if n.event.item == "restart"]


def test_a_source_change_between_ticks_launches_nothing_and_re_execs_once(
    cfg: Any, ledger: Any, source: Path
) -> None:
    execv = _Exec(ledger)
    ticks, tick = _ticker(ledger)

    with pytest.raises(_Replaced):
        _run(cfg, tick, source, execv, _after_round_one(ticks, source))

    # The first round ran on the start-time code; the change landed after
    # it, so no second round launched.
    assert ticks == ["epic:a", "epic:b"]
    assert len(execv.calls) == 1
    path, argv, env = execv.calls[0]
    assert (path, argv) == (sys.executable, list(sys.orig_argv))
    # The lease is still held at the exec, and the new process is handed it.
    assert execv.holder_at_exec is not None
    assert env[DRIVE_RESUME_ENV] == execv.holder_at_exec.token
    assert DRIVE_RESUME_ENV not in os.environ


def test_the_restart_raises_exactly_one_attention_event(
    cfg: Any, ledger: Any, source: Path
) -> None:
    execv = _Exec(ledger)
    ticks, tick = _ticker(ledger)
    notices: list[Notice] = []

    with pytest.raises(_Replaced):
        _run(cfg, tick, source, execv, _after_round_one(ticks, source), notices)

    assert _restarts(notices) == [CHANGED]
    assert len(execv.calls) == 1


def test_a_change_between_admission_and_spawn_defers_that_launch(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """The code moves while epic:b's tick is admitted (the admission's load
    read is the last thing before the spawn): that tick never spawns."""
    execv = _Exec(ledger)
    ticks, tick = _ticker(ledger)
    reads: list[int] = []

    def load() -> float:
        reads.append(1)
        if len(reads) == 2:
            _move(source)
        return 0.0

    notices: list[Notice] = []
    with pytest.raises(_Replaced):
        drive_cli.run_drive(
            cfg, tick, 1, load, notify=notices.append, source=source, execv=execv
        )

    assert ticks == ["epic:a"]
    assert _restarts(notices) == [CHANGED]
    assert len(execv.calls) == 1


def test_a_running_tick_finishes_before_the_re_exec(
    cfg: Any, ledger: Any, source: Path
) -> None:
    execv = _Exec(ledger)

    def plan(name: str, _count: int) -> None:
        execv.order.append(f"start {name}")
        _move(source)  # the checkout moves under a running tick child
        execv.order.append(f"end {name}")

    _, tick = _ticker(ledger, plan)

    notices: list[Notice] = []
    with pytest.raises(_Replaced):
        drive_cli.run_drive(
            cfg, tick, 1, notify=notices.append, source=source, execv=execv
        )

    assert execv.order == ["start epic:a", "end epic:a", "exec"]
    assert _restarts(notices) == [CHANGED]


def test_an_unchanged_tree_across_many_ticks_never_restarts(
    cfg: Any, ledger: Any, source: Path
) -> None:
    execv = _Exec(ledger)
    ticks, tick = _ticker(ledger)
    notices = _run(cfg, tick, source, execv)

    assert len(ticks) == _BACKSTOP
    assert _restarts(notices) == []
    assert execv.calls == []
    assert current_holder(ledger) is None


def test_a_failed_re_exec_exits_with_the_scheme_code_and_status_names_why(
    cfg: Any, ledger: Any, source: Path, status: Any
) -> None:
    """The scheme allows no public exit above 2: a drive that could not
    re-exec exits 2 (non-zero, so launchd restarts it) and the exit it
    records — not the code — tells it apart from a lost lock."""
    execv = _Exec(ledger, fails=True)
    ticks, tick = _ticker(ledger)

    with pytest.raises(typer.Exit) as exc:
        _run(cfg, tick, source, execv, _after_round_one(ticks, source))

    assert exc.value.exit_code == DRIVE_RESTART_EXIT == 2
    assert len(execv.calls) == 1
    assert current_holder(ledger) is None

    result = status()
    assert result.exit_code == 1, result.output
    assert f"exited {DRIVE_RESTART_EXIT}" in result.output
    assert CHANGED in result.output
    assert "could not re-exec itself" in result.output

    document = json.loads(status("--json").output)
    assert document["exit"]["code"] == DRIVE_RESTART_EXIT
    assert CHANGED in document["exit"]["reason"]


def _bump_schema(cfg: Any, version: int) -> None:
    """What a newer kew's migration leaves in the ledger."""
    with sqlite3.connect(Path(cfg.logging.dir) / "dispatch.db") as conn:
        conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (version,))


def test_a_newer_ledger_under_new_code_restarts_the_drive(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """A child migrated the ledger and the drive's own kew moved too: the
    re-exec loads code that may know the new schema."""
    execv = _Exec(ledger)
    newer = CURRENT_SCHEMA_VERSION + 1

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _move(source)
            _bump_schema(cfg, newer)

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with pytest.raises(_Replaced):
        _run(cfg, tick, source, execv, notices=notices)

    assert ticks == ["epic:a"]
    assert _restarts(notices) == [
        f"ledger schema {newer} is newer than this drive's "
        f"{CURRENT_SCHEMA_VERSION}: restarting"
    ]
    assert len(execv.calls) == 1


def test_a_newer_ledger_under_unchanged_code_exits_instead_of_re_execing(
    cfg: Any, ledger: Any, source: Path, status: Any
) -> None:
    """A pinned ``--kew-binary`` child migrated the ledger, but the drive's
    own kew is the same: a re-exec would load this code again and refuse the
    ledger before taking its lease back. The drive exits and says why."""
    execv = _Exec(ledger)
    newer = CURRENT_SCHEMA_VERSION + 1
    why = (
        f"ledger schema {newer} is newer than this drive's "
        f"{CURRENT_SCHEMA_VERSION} and the drive's own kew has not changed: "
        "upgrade the kew the drive runs"
    )

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _bump_schema(cfg, newer)

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with pytest.raises(typer.Exit) as exc:
        _run(cfg, tick, source, execv, notices=notices)

    assert exc.value.exit_code == DRIVE_RESTART_EXIT
    assert ticks == ["epic:a"]
    assert execv.calls == []
    assert _restarts(notices) == [why]
    assert current_holder(ledger) is None
    assert why in (ledger.get_meta(DRIVE_EXIT_KEY) or "")

    _bump_schema(cfg, CURRENT_SCHEMA_VERSION)
    result = status()
    assert result.exit_code == 1, result.output
    assert f"exited {DRIVE_RESTART_EXIT}" in result.output
    assert why in result.output


def _heartbeat_gives_up() -> None:
    """Wait for the heartbeat thread to end: it does once it gives up."""
    [beat] = [t for t in threading.enumerate() if t.name == "kew-drive-heartbeat"]
    beat.join()


def test_a_heartbeat_a_newer_ledger_refuses_ends_the_drive_on_the_schema_exit(
    cfg: Any, ledger: Any, source: Path, status: Any
) -> None:
    """A newer schema makes every heartbeat open refuse. The heartbeat gives
    up after 3 refusals instead of retrying forever, and the drive ends
    through the schema exit — its attention event and the reason status
    names — not as a lost lock."""
    execv = _Exec(ledger)
    newer = CURRENT_SCHEMA_VERSION + 1
    why = (
        f"ledger schema {newer} is newer than this drive's "
        f"{CURRENT_SCHEMA_VERSION} and the drive's own kew has not changed: "
        "upgrade the kew the drive runs"
    )

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _bump_schema(cfg, newer)
            _heartbeat_gives_up()

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with (
        patch.object(drive_cli, "_HEARTBEAT_EVERY_S", 0.001),
        pytest.raises(typer.Exit) as exc,
    ):
        _run(cfg, tick, source, execv, notices=notices)

    assert exc.value.exit_code == DRIVE_RESTART_EXIT
    assert ticks == ["epic:a"]
    assert execv.calls == []
    assert _restarts(notices) == [why]

    _bump_schema(cfg, CURRENT_SCHEMA_VERSION)
    result = status()
    assert result.exit_code == 1, result.output
    assert f"exited {DRIVE_RESTART_EXIT}" in result.output
    assert why in result.output
    assert "lost the drive lock" not in result.output


def test_a_tick_that_outlives_the_refused_heartbeat_still_re_execs_on_new_code(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """A ``--kew-binary`` tick child migrates the ledger and the checkout
    moves; the tick outlives three heartbeats, so the heartbeat has given up
    on the refused ledger before the drive looks again. The drive re-execs
    with the schema's one attention event; it does not exit as lost."""
    execv = _Exec(ledger)
    newer = CURRENT_SCHEMA_VERSION + 1

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _move(source)
            _bump_schema(cfg, newer)
            _heartbeat_gives_up()

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with (
        patch.object(drive_cli, "_HEARTBEAT_EVERY_S", 0.001),
        pytest.raises(_Replaced),
    ):
        _run(cfg, tick, source, execv, notices=notices)

    assert ticks == ["epic:a"]
    assert len(execv.calls) == 1
    assert _restarts(notices) == [
        f"ledger schema {newer} is newer than this drive's "
        f"{CURRENT_SCHEMA_VERSION}: restarting"
    ]
    # The lease the new process resumes is still this drive's.
    assert execv.holder_at_exec is not None
    assert execv.calls[0][2][DRIVE_RESUME_ENV] == execv.holder_at_exec.token
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION)
    assert "lost the drive lock" not in (ledger.get_meta(DRIVE_EXIT_KEY) or "")


def test_a_lock_another_drive_took_exits_lost_even_when_the_code_moved(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """Only a refused ledger open goes through the restart: a heartbeat that
    finds another holder on the lock is a lost lock, code move or not."""
    execv = _Exec(ledger)

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _move(source)
            mine = current_holder(ledger)
            assert mine is not None
            thief = replace(mine, token="another-drive", pid=os.getppid())
            ledger.set_meta(DRIVE_HOLDER_KEY, thief.encode())
            _heartbeat_gives_up()

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with (
        patch.object(drive_cli, "_HEARTBEAT_EVERY_S", 0.001),
        pytest.raises(typer.Exit) as exc,
    ):
        _run(cfg, tick, source, execv, notices=notices)

    assert exc.value.exit_code == 2
    assert ticks == ["epic:a"]
    assert execv.calls == []
    assert "lost the drive lock" in (ledger.get_meta(DRIVE_EXIT_KEY) or "")
    holder = current_holder(ledger)
    assert holder is not None and holder.token == "another-drive"


# --------------------------------------------------------------------------- #
# A run-request child across the restart
# --------------------------------------------------------------------------- #


class _Spawner:
    """The process boundary of the run-request runner: each request starts a
    real child that sleeps until the test ends it."""

    def __init__(self) -> None:
        self.children: list[subprocess.Popen[bytes]] = []

    def __call__(
        self, argv: list[str], log: Path, actor: str, env: dict[str, str]
    ) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.children.append(child)
        return child

    def close(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.fixture
def spawner(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Spawner]:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    spawn = _Spawner()
    try:
        yield spawn
    finally:
        spawn.close()


def _request_states(ledger: Any) -> list[str]:
    return [r.state for r in rr.list_requests(ledger)]


def test_a_restart_waits_for_the_run_request_child_it_started(
    cfg: Any,
    ledger: Any,
    source: Path,
    spawner: _Spawner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console-queued request runs as a child when the code moves: the drive
    launches nothing new, and execs only once that child has been collected —
    its row finished, not left ``running`` under the lease the new process
    resumes."""
    import kew.cli as cli

    rr.queue(ledger, "kew epic unhold epic:a 12", operator="tom", source="web")
    execv = _Exec(ledger)
    at_exec: list[list[str]] = []

    def exec_after(path: str, argv: list[str], env: dict[str, str]) -> None:
        at_exec.append(_request_states(ledger))
        execv(path, argv, env)

    def sleep(_seconds: float) -> None:
        # The clock boundary: the request's child ends during the drive's
        # first wait for it.
        execv.order.append("child ends")
        [child] = spawner.children
        child.kill()
        child.wait()

    monkeypatch.setattr(cli, "_sleep", sleep)

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _move(source)

    ticks, tick = _ticker(ledger, plan)
    notices: list[Notice] = []
    with pytest.raises(_Replaced):
        drive_cli.run_drive(
            cfg,
            tick,
            0,
            notify=notices.append,
            requests=lambda store, lease: RequestRunner(
                store, lease, cfg, Path(cfg.logging.dir), spawn=spawner, say=print
            ),
            source=source,
            execv=exec_after,
        )

    assert ticks == ["epic:a"]
    assert len(spawner.children) == 1
    assert execv.order == ["child ends", "exec"]
    assert at_exec == [[rr.FAILED]]
    assert _restarts(notices) == [CHANGED]


def test_a_re_execd_drive_waits_on_and_collects_the_child_its_earlier_self_started(
    ledger: Any, cfg: Any, spawner: _Spawner
) -> None:
    """The new process resumes the lease under the same token with no child in
    hand. A request that token left running holds the queue while its child
    lives; once the child exits it is collected — no zombie — and its row
    finished with the exit code, and only then does the next request start."""
    rr.queue(ledger, "kew epic unhold epic:a 12", operator="tom", source="web")
    rr.queue(ledger, "kew epic unhold epic:b 13", operator="tom", source="web")
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)
    log_dir = Path(cfg.logging.dir)
    RequestRunner(ledger, lease, cfg, log_dir, spawn=spawner, say=print)()
    [child] = spawner.children

    resumed = acquire_drive(ledger, resume=lease.holder.token)
    assert isinstance(resumed, DriveLease)
    after = RequestRunner(ledger, resumed, cfg, log_dir, spawn=spawner, say=print)

    after()
    assert len(spawner.children) == 1
    assert _request_states(ledger) == [rr.RUNNING, rr.QUEUED]

    # The child exits; wait for that without collecting it, as the exec'd
    # process would find it: a zombie only its parent can collect.
    os.kill(child.pid, signal.SIGKILL)
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
    after()

    first, second = rr.list_requests(ledger)
    assert first.state == rr.FAILED and first.exit_code == -signal.SIGKILL
    assert second.state == rr.RUNNING
    assert len(spawner.children) == 2
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def test_a_request_its_earlier_self_left_running_with_its_child_gone_is_unknown(
    ledger: Any, cfg: Any, spawner: _Spawner
) -> None:
    """A row the drive's own token left running whose child is gone and not
    its to collect is settled ``unknown``, never left ``running``."""
    import socket

    request, _ = rr.queue(
        ledger, "kew epic unhold epic:a 12", operator="tom", source="web"
    )
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)
    gone = subprocess.Popen(["true"])
    gone.wait()
    claimed = rr.claim(
        ledger,
        request,
        drive=lease.holder.token,
        host=socket.gethostname(),
        log="",
    )
    assert claimed is not None
    rr.started(ledger, claimed, gone.pid)

    RequestRunner(ledger, lease, cfg, Path(cfg.logging.dir), spawn=spawner, say=print)()

    [settled] = rr.list_requests(ledger)
    assert settled.state == rr.UNKNOWN
    assert "re-exec'd itself before it finished" in (settled.detail or "")
    assert spawner.children == []


def test_the_drive_reads_a_ledger_a_newer_kew_migrated_on_its_open_store(
    cfg: Any, ledger: Any
) -> None:
    """The schema read goes through the store the drive already holds, so a
    ledger migrated past this kew is read rather than refused."""
    newer = CURRENT_SCHEMA_VERSION + 1
    assert ledger.schema_version() == CURRENT_SCHEMA_VERSION

    _bump_schema(cfg, newer)

    assert ledger.schema_version() == newer


def test_an_unreadable_exit_record_is_warned_about_and_reads_as_none(
    ledger: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger.set_meta(DRIVE_EXIT_KEY, "{not json")

    reading = read_drive(ledger)

    assert reading.ended is None
    assert "the recorded drive exit is unreadable" in capsys.readouterr().err


def test_a_re_execd_drive_takes_its_own_lease_back(ledger: Any) -> None:
    first = acquire_drive(ledger)
    assert isinstance(first, DriveLease)

    assert isinstance(acquire_drive(ledger), DriveRefusal)
    assert isinstance(acquire_drive(ledger, resume="not-mine"), DriveRefusal)
    again = acquire_drive(ledger, resume=first.holder.token)

    assert isinstance(again, DriveLease)
    assert again.holder.token == first.holder.token
    assert again.holder.started_at == first.holder.started_at
    assert again.reclaimed is None and again.holder.restarts == ()
    assert ledger.get_meta(DRIVE_HOLDER_KEY) == again.raw


def test_a_resume_token_from_another_pid_takes_nothing(ledger: Any) -> None:
    first = acquire_drive(ledger, pid=os.getppid())
    assert isinstance(first, DriveLease)

    refused = acquire_drive(ledger, resume=first.holder.token)

    assert isinstance(refused, DriveRefusal)


# --------------------------------------------------------------------------- #
# The code identity
# --------------------------------------------------------------------------- #


def test_an_installed_package_is_identified_by_its_content(source: Path) -> None:
    before = code_identity(source)
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"cache")

    assert code_identity(source) == before
    (source / "new.py").write_text("x = 1\n")
    assert code_identity(source) != before


def _checkout(root: Path, head: str) -> Path:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(f"{head}\n")
    pkg = root / "kew" / "src" / "kew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def test_a_checkout_is_identified_by_its_git_head(tmp_path: Path) -> None:
    pkg = _checkout(tmp_path, "a" * 40)
    before = code_identity(pkg)

    assert before.endswith(f"+git:{'a' * 40}")
    (pkg / "edited.py").write_text("x = 1\n")
    assert code_identity(pkg) == before
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert code_identity(pkg).endswith(f"+git:{'b' * 40}")


def test_a_packed_head_is_read(tmp_path: Path) -> None:
    pkg = _checkout(tmp_path, "a" * 40)
    (tmp_path / ".git" / "refs" / "heads" / "main").unlink()
    (tmp_path / ".git" / "packed-refs").write_text(
        f"# pack-refs with: peeled\n{'c' * 40} refs/heads/main\n"
    )

    assert code_identity(pkg).endswith(f"+git:{'c' * 40}")


def test_an_unreadable_head_is_warned_about_and_the_content_identifies_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pkg = _checkout(tmp_path, "a" * 40)
    head = tmp_path / ".git" / "HEAD"
    head.unlink()
    head.mkdir()  # reading it raises IsADirectoryError

    identity = code_identity(pkg)

    assert "+sha256:" in identity
    assert "cannot read the git head" in capsys.readouterr().err


def test_an_install_inside_a_checkout_follows_the_install_not_the_head(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path, "a" * 40)
    pkg = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "kew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    before = code_identity(pkg)

    assert "+sha256:" in before
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    assert code_identity(pkg) == before


def test_a_stop_requested_alongside_a_restart_stops_the_drive(
    cfg: Any, ledger: Any, source: Path
) -> None:
    execv = _Exec(ledger)

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _move(source)
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)

    ticks, tick = _ticker(ledger, plan)
    _run(cfg, tick, source, execv)

    assert ticks == ["epic:a"]
    assert execv.calls == []
    assert current_holder(ledger) is None


# --- A refusal that will repeat leaves a launchd drive down (#3284) ----------


def _start(cfg: Any, ledger: Any, source: Path) -> int:
    """One drive start, as launchd makes it; returns its exit code."""
    _ticks, tick = _ticker(ledger)
    try:
        _run(cfg, tick, source, _Exec(ledger))
    except typer.Exit as exc:
        return exc.exit_code
    return 0


def _refusal_record(cfg: Any) -> dict[str, Any]:
    return json.loads((Path(cfg.logging.dir) / DRIVE_REFUSAL).read_text())


def test_a_first_schema_refusal_still_exits_2(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """The first start on a newer ledger exits ``DRIVE_RESTART_EXIT`` as before
    and records the refusal beside the ledger, outside the ledger it refused."""
    newer = CURRENT_SCHEMA_VERSION + 1
    _bump_schema(cfg, newer)

    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT == 2

    record = _refusal_record(cfg)
    assert record["ledger_schema"] == newer
    assert record["drive_schema"] == CURRENT_SCHEMA_VERSION
    assert record["identity"] == code_identity(source)


def test_a_second_identical_schema_refusal_exits_so_launchd_leaves_the_drive_down(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """launchd restarts any non-zero exit: the start that repeats the recorded
    refusal on the same kew exits 0, keeping the record for status."""
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION + 1)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT
    first = _refusal_record(cfg)

    assert _start(cfg, ledger, source) == 0
    assert _start(cfg, ledger, source) == 0
    assert _refusal_record(cfg) == first


def test_a_running_drive_refusal_then_its_restart_exits_0(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """The refusal a running drive hits first is the one its launchd restart
    repeats at the ledger open."""
    newer = CURRENT_SCHEMA_VERSION + 1

    def plan(_name: str, count: int) -> None:
        if count == 1:
            _bump_schema(cfg, newer)

    _ticks, tick = _ticker(ledger, plan)
    with pytest.raises(typer.Exit) as exc:
        _run(cfg, tick, source, _Exec(ledger))
    assert exc.value.exit_code == DRIVE_RESTART_EXIT
    assert _refusal_record(cfg)["ledger_schema"] == newer

    assert _start(cfg, ledger, source) == 0


def test_a_new_kew_or_an_old_record_refuses_on_exit_2_again(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """Another code identity is another refusal, and a record past the window
    no longer holds the drive down."""
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION + 1)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT

    _move(source)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT
    assert _refusal_record(cfg)["identity"] == code_identity(source)

    record = _refusal_record(cfg)
    old = datetime.now(UTC) - timedelta(seconds=REFUSAL_REPEAT_S + 60)
    record["at"] = old.isoformat()
    (Path(cfg.logging.dir) / DRIVE_REFUSAL).write_text(json.dumps(record))
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT


def test_a_start_the_ledger_does_not_refuse_clears_the_refusal(
    cfg: Any, ledger: Any, source: Path
) -> None:
    """Once the drive's kew reads the ledger again the record is gone, so a
    later refusal starts over at exit 2."""
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION + 1)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT

    _bump_schema(cfg, CURRENT_SCHEMA_VERSION)
    assert _start(cfg, ledger, source) == 0
    assert not (Path(cfg.logging.dir) / DRIVE_REFUSAL).exists()

    _bump_schema(cfg, CURRENT_SCHEMA_VERSION + 1)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT


def test_drive_status_names_the_refusal_that_stopped_the_drive(
    cfg: Any, ledger: Any, source: Path, status: Any
) -> None:
    """The stopped drive's refusal and its fix reach ``kew drive status``,
    even while the ledger it refused is still too new to open."""
    newer = CURRENT_SCHEMA_VERSION + 1
    _bump_schema(cfg, newer)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT
    assert _start(cfg, ledger, source) == 0
    fix = "upgrade the kew the drive runs, then `kew drive install`"

    result = status()
    assert result.exit_code == 2, result.output
    assert f"ledger schema {newer} is newer than this drive's" in result.output
    assert code_identity(source) in result.output
    assert fix in result.output

    # Still too new to open: --json cannot read the rest, yet says why.
    refused = status("--json")
    assert refused.exit_code == 2, refused.output
    document = json.loads(refused.stdout)
    assert document["state"] == "refused"
    assert document["refusal"]["ledger_schema"] == newer
    assert document["refusal"]["identity"] == code_identity(source)
    assert fix in document["refusal"]["fix"]


def _record_refusal(cfg: Any, drive_schema: int, ledger_schema: int) -> None:
    write_refusal(
        Path(cfg.logging.dir),
        SchemaRefusal(
            f"ledger schema {ledger_schema} is newer than this drive's "
            f"{drive_schema}: upgrade the kew the drive runs",
            ledger_schema,
            drive_schema,
            "kew 0+sha256:stale",
            datetime.now(UTC),
        ),
    )


def test_drive_status_drops_a_refusal_the_fixed_ledger_no_longer_makes(
    cfg: Any, ledger: Any, source: Path, status: Any
) -> None:
    """Once the ledger's schema is back within the refused drive's, the
    recorded refusal no longer applies, drive running or not."""
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION + 1)
    assert _start(cfg, ledger, source) == DRIVE_RESTART_EXIT
    _bump_schema(cfg, CURRENT_SCHEMA_VERSION)

    result = status()
    assert "by a refusal" not in result.output, result.output
    assert json.loads(status("--json").output)["refusal"] is None


def test_drive_status_drops_a_stale_kews_refusal_while_a_drive_runs(
    cfg: Any, ledger: Any, status: Any
) -> None:
    """An older kew (a stale worktree) refused the ledger, but a drive on a
    kew that reads it is running: that refusal stopped nothing."""
    _record_refusal(cfg, CURRENT_SCHEMA_VERSION - 1, CURRENT_SCHEMA_VERSION)
    acquire_drive(ledger)

    result = status()
    assert result.exit_code == 0, result.output
    assert "by a refusal" not in result.output
    assert json.loads(status("--json").output)["refusal"] is None


def test_drive_status_shows_a_standing_refusal_while_no_drive_runs(
    cfg: Any, status: Any
) -> None:
    """The same stale kew's refusal, with no drive running: the ledger still
    refuses that kew, and it is why nothing drives the project."""
    _record_refusal(cfg, CURRENT_SCHEMA_VERSION - 1, CURRENT_SCHEMA_VERSION)

    result = status()
    assert result.exit_code == 1, result.output
    assert "by a refusal" in result.output
    document = json.loads(status("--json").output)
    assert document["refusal"]["drive_schema"] == CURRENT_SCHEMA_VERSION - 1
