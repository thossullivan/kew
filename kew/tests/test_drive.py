"""`kew drive`: one supervised process ticks every active epic (#2615).

The acceptance: two epics advance in one drive; pausing one leaves the other
ticking; a second drive refuses; stop exits after the current tick; and a crash
leaves no lock that blocks the next start. Every test runs on a real ledger;
only the per-epic tick (a pinned ``kew epic _tick`` child in production) is
replaced.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import kew.cli as cli
import kew.cli.drive as drive_cli
from kew.cli import app
from kew.config import DispatchConfig, DriveLoadConfig, DriveLoadStep, ProjectConfig
from kew.drive import (
    DRIVE_ENDED_EXITED,
    DRIVE_ENDED_REBOOT,
    DRIVE_ENDED_STALLED,
    DRIVE_HOLDER_KEY,
    DRIVE_RESTARTED_EVENT,
    DriveHolder,
    DriveLease,
    DriveRefusal,
    DriveStateError,
    acquire_drive,
    current_holder,
    drive_hold,
    drive_pauses,
    hold,
    pause,
    refresh_drive,
    release,
    release_drive,
    request_stop,
    resume,
    stop_for,
)
from kew.epic_archive import EPIC_ARCHIVED_EVENT, is_archived
from kew.run_store import SqliteRunStore

runner = CliRunner()

CONTINUE = 10  # a PROGRESSING/AWAITING_MERGES tick child (cli.epic)
COMPLETE = 0


def _config(tmp_path: Path, refs: tuple[str, ...] = ("epic:a", "epic:b")) -> Any:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        for ref in refs:
            s.register_epic(ref)
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    # A fleet cap wider than the drive's, so `[drive]` is what bounds a tick.
    cfg.parallel.max_workers = 8
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    return _config(tmp_path)


@pytest.fixture
def ledger(cfg: Any) -> Iterator[Any]:
    """A second connection to the drive's ledger — the operator's shell."""
    with SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as s:
        yield s


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(cli, "_sleep", slept.append)
    return slept


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _ticker(
    plan: Callable[[str, int], int | None] | None = None,
) -> tuple[list[str], Callable[[str, int], int]]:
    """A tick that records each epic it ran; ``plan(name, n)`` may act
    mid-tick (pause, stop) and pick the exit code."""
    ticks: list[str] = []

    def tick(name: str, _admit_cap: int) -> int:
        ticks.append(name)
        code = plan(name, len(ticks)) if plan else None
        return CONTINUE if code is None else code

    return ticks, tick


# --------------------------------------------------------------------------- #
# The lock in the ledger
# --------------------------------------------------------------------------- #


def test_a_second_drive_is_refused_and_names_the_holder(ledger: Any) -> None:
    first = acquire_drive(ledger)
    assert isinstance(first, DriveLease)

    second = acquire_drive(ledger)

    assert isinstance(second, DriveRefusal)
    assert f"pid {os.getpid()}" in second.detail
    assert current_holder(ledger) == first.holder


def test_a_crashed_drive_with_a_dead_pid_does_not_block_the_next(ledger: Any) -> None:
    crashed = acquire_drive(ledger, pid=_dead_pid())
    assert isinstance(crashed, DriveLease)

    next_drive = acquire_drive(ledger)

    assert isinstance(next_drive, DriveLease)
    assert next_drive.reclaimed is not None and "is gone" in next_drive.reclaimed
    # The crashed drive, were it to wake, finds the lock gone.
    assert refresh_drive(ledger, crashed) is False
    assert release_drive(ledger, crashed) is False
    assert current_holder(ledger) == next_drive.holder


def test_a_live_pid_with_a_stale_heartbeat_is_reclaimed(ledger: Any) -> None:
    """A recycled or hung pid: the heartbeat alone proves it dead."""
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    hung = acquire_drive(ledger, now=long_ago)
    assert isinstance(hung, DriveLease)

    next_drive = acquire_drive(ledger)

    assert isinstance(next_drive, DriveLease)
    assert next_drive.reclaimed is not None and "heartbeat" in next_drive.reclaimed


def test_a_fresh_heartbeat_keeps_a_live_drive(ledger: Any) -> None:
    lease = acquire_drive(ledger, now=datetime.now(UTC) - timedelta(hours=1))
    assert isinstance(lease, DriveLease)
    assert refresh_drive(ledger, lease) is True

    assert isinstance(acquire_drive(ledger), DriveRefusal)


def test_release_frees_the_lock(ledger: Any) -> None:
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)
    assert release_drive(ledger, lease) is True

    assert current_holder(ledger) is None
    assert isinstance(acquire_drive(ledger), DriveLease)


def test_an_unreadable_holder_row_is_reclaimed_and_said(ledger: Any) -> None:
    ledger.set_meta(DRIVE_HOLDER_KEY, "{not json")

    lease = acquire_drive(ledger)

    assert isinstance(lease, DriveLease)
    assert lease.reclaimed is not None
    assert lease.reclaimed.startswith("an unreadable holder record")


def test_a_holder_on_another_host_is_judged_by_heartbeat_alone() -> None:
    now = datetime.now(UTC)
    other = DriveHolder(
        pid=_dead_pid(), host="elsewhere", token="t", started_at=now, heartbeat=now
    )
    assert other.stale_reason(now=now) is None


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #


def test_pause_and_resume_one_epic_and_every_epic(ledger: Any) -> None:
    pause(ledger, "epic:a")
    assert drive_pauses(ledger).covers("epic:a")
    assert not drive_pauses(ledger).covers("epic:b")
    pause(ledger, None)
    assert drive_pauses(ledger).covers("epic:b")
    resume(ledger, "epic:a")
    assert drive_pauses(ledger).covers("epic:a")  # still paused by "every"
    resume(ledger, None)
    assert not drive_pauses(ledger).covers("epic:a")


def test_an_unreadable_pause_row_is_an_error_not_a_resume(ledger: Any) -> None:
    ledger.set_meta("drive:paused", "{not json")
    with pytest.raises(DriveStateError):
        drive_pauses(ledger)


def test_a_stop_left_for_an_old_drive_never_stops_the_next(ledger: Any) -> None:
    old = acquire_drive(ledger)
    assert isinstance(old, DriveLease)
    request_stop(ledger, old.holder, None)
    assert stop_for(ledger, old).all
    release_drive(ledger, old)

    new = acquire_drive(ledger)
    assert isinstance(new, DriveLease)

    assert stop_for(ledger, new).all is False


# --------------------------------------------------------------------------- #
# The drive loop
# --------------------------------------------------------------------------- #


def _stop_after(n: int, ledger: Any, epic: str | None = None) -> Callable[..., Any]:
    """A plan that asks the running drive to stop during the n-th tick."""

    def plan(_name: str, count: int) -> None:
        if count == n:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, epic)

    return plan


def test_two_epics_advance_in_one_drive(cfg: Any, ledger: Any) -> None:
    ticks, tick = _ticker(_stop_after(4, ledger))

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:a", "epic:b", "epic:a", "epic:b"]
    assert current_holder(ledger) is None


def test_stop_exits_after_the_current_tick(cfg: Any, ledger: Any) -> None:
    """Stop lands while epic:a ticks: that tick finishes, epic:b is never
    started, and the lock is released."""
    ticks, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:a"]
    assert current_holder(ledger) is None
    assert not (Path(cfg.logging.dir) / drive_cli.DRIVE_PID).exists()


def test_a_stop_during_the_wait_ends_the_drive_without_another_round(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks, tick = _ticker()

    def sleep(_s: float) -> None:
        holder = current_holder(ledger)
        assert holder is not None
        request_stop(ledger, holder, None)

    monkeypatch.setattr(cli, "_sleep", sleep)

    drive_cli.run_drive(cfg, tick, interval=600)

    assert ticks == ["epic:a", "epic:b"]


def test_pausing_one_epic_leaves_the_other_ticking(cfg: Any, ledger: Any) -> None:
    def plan(name: str, count: int) -> None:
        if count == 1:
            pause(ledger, "epic:a")
        _stop_after(5, ledger)(name, count)

    ticks, tick = _ticker(plan)

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:a", "epic:b", "epic:b", "epic:b", "epic:b"]


def test_stop_for_one_epic_takes_it_out_of_this_drive_only(
    cfg: Any, ledger: Any
) -> None:
    def plan(name: str, count: int) -> None:
        if count == 1:
            _stop_after(1, ledger, "epic:a")(name, count)
        _stop_after(3, ledger)(name, count)

    ticks, tick = _ticker(plan)

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:a", "epic:b", "epic:b"]
    assert not drive_pauses(ledger).covers("epic:a")


def test_a_finished_epic_is_archived_and_leaves_the_rotation(
    cfg: Any, ledger: Any
) -> None:
    def plan(name: str, count: int) -> int | None:
        _stop_after(4, ledger)(name, count)
        return COMPLETE if name == "epic:a" else None

    ticks, tick = _ticker(plan)

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:a", "epic:b", "epic:b", "epic:b"]
    assert is_archived(ledger, "epic:a")
    [event] = ledger.list_audit_events(limit=None, event=EPIC_ARCHIVED_EVENT)
    assert event.payload["source"] == "drive"


def test_an_archived_epic_is_never_ticked(cfg: Any, ledger: Any) -> None:
    from kew.service.verbs import set_epic_archived

    set_epic_archived(ledger, epic_ref="epic:a", archived=True, reason="", config=cfg)
    ticks, tick = _ticker(_stop_after(2, ledger))

    drive_cli.run_drive(cfg, tick, interval=1)

    assert ticks == ["epic:b", "epic:b"]


def test_a_drive_that_loses_its_lock_stops_without_another_tick(
    cfg: Any, ledger: Any
) -> None:
    """Another drive reclaimed the row mid-tick (this one's heartbeat had gone
    stale): the heartbeat thread sees the loss and no further tick starts."""
    import threading

    import typer

    def plan(_name: str, count: int) -> None:
        if count == 1:
            now = datetime.now(UTC)
            thief = DriveHolder(
                pid=os.getpid(), host="h", token="thief", started_at=now, heartbeat=now
            )
            ledger.set_meta(DRIVE_HOLDER_KEY, thief.encode())
            # Several heartbeat periods (conftest makes time.sleep a no-op).
            threading.Event().wait(0.5)

    ticks, tick = _ticker(plan)
    with (
        patch.object(drive_cli, "_HEARTBEAT_EVERY_S", 0.01),
        pytest.raises(typer.Exit) as exc,
    ):
        drive_cli.run_drive(cfg, tick, interval=1)

    assert exc.value.exit_code == 2
    assert ticks == ["epic:a"]
    # The thief's row is left alone.
    holder = current_holder(ledger)
    assert holder is not None and holder.token == "thief"


def test_a_heartbeat_that_raises_unexpectedly_stops_the_drive(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh raising something other than a ledger error ends the drive
    with the lost-lock message instead of leaving a dead heartbeat behind a
    drive that keeps ticking."""
    import threading

    import typer

    def refresh(_store: Any, _lease: Any) -> bool:
        raise RuntimeError("boom")

    errors: list[str] = []
    monkeypatch.setattr(drive_cli, "refresh_drive", refresh)
    monkeypatch.setattr(drive_cli, "_error", errors.append)
    ticks, tick = _ticker(lambda _n, _c: threading.Event().wait(0.5) and None)
    with (
        patch.object(drive_cli, "_HEARTBEAT_EVERY_S", 0.01),
        pytest.raises(typer.Exit) as exc,
    ):
        drive_cli.run_drive(cfg, tick, interval=1)

    assert exc.value.exit_code == 2
    # The drive writes its first heartbeat before any tick (#2617), so a
    # refresh that raises stops it before epic:a ever ticks.
    assert ticks == []
    [message] = errors
    assert "lost the drive lock" in message
    assert "RuntimeError: boom" in message
    # The row was still ours, so the drive released it on the way out.
    assert current_holder(ledger) is None


def test_a_dead_heartbeat_thread_is_a_lost_lock(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However the heartbeat thread ended, the loop treats it as a lost lock."""
    import threading

    import typer

    ended = threading.Event()

    def run(_self: Any) -> None:
        ended.wait()

    def plan(_name: str, _count: int) -> None:
        ended.set()
        threading.Event().wait(0.2)

    errors: list[str] = []
    monkeypatch.setattr(drive_cli._Heartbeat, "run", run)
    monkeypatch.setattr(drive_cli, "_error", errors.append)
    ticks, tick = _ticker(plan)
    with pytest.raises(typer.Exit) as exc:
        drive_cli.run_drive(cfg, tick, interval=1)

    assert exc.value.exit_code == 2
    assert ticks == ["epic:a"]
    [message] = errors
    assert "lost the drive lock (its heartbeat thread died)" in message


def test_the_rotation_excludes_archived_epics(cfg: Any, ledger: Any) -> None:
    from kew.service.verbs import set_epic_archived

    assert drive_cli._roster(ledger, cfg.tracker.type) == ("epic:a", "epic:b")
    set_epic_archived(ledger, epic_ref="epic:a", archived=True, reason="", config=cfg)

    assert drive_cli._roster(ledger, cfg.tracker.type) == ("epic:b",)


# --------------------------------------------------------------------------- #
# Admission (#2616)
# --------------------------------------------------------------------------- #


def _admitting(
    plan: Callable[[str, int], int | None] | None = None,
) -> tuple[list[tuple[str, int]], Callable[[str, int], int]]:
    """A tick that records the epic and the cap it was told to admit under."""
    seen: list[tuple[str, int]] = []

    def tick(name: str, admit_cap: int) -> int:
        seen.append((name, admit_cap))
        code = plan(name, len(seen)) if plan else None
        return CONTINUE if code is None else code

    return seen, tick


def test_the_load_gate_lowers_the_cap_each_tick_reads(cfg: Any, ledger: Any) -> None:
    """A fake load source climbs past each threshold; the next tick's cap
    drops as configured, and falls back once the load does."""
    cfg.drive.max_parallel = 4
    cfg.drive.load = DriveLoadConfig(
        thresholds=[
            DriveLoadStep(above=14, max_parallel=3),
            DriveLoadStep(above=25, max_parallel=2),
        ]
    )
    loads = iter([3.0, 14.5, 25.5, 40.0, 1.0])
    seen, tick = _admitting(_stop_after(5, ledger))

    drive_cli.run_drive(cfg, tick, interval=1, load=lambda: next(loads))

    assert [cap for _, cap in seen] == [4, 3, 2, 2, 4]


def test_a_hold_stops_admission_while_every_epic_keeps_ticking(
    cfg: Any, ledger: Any
) -> None:
    """Held, the drive still ticks each epic — so running work is read, merged
    and reaped — but tells every tick to admit nothing; release reopens it."""

    def plan(_name: str, count: int) -> None:
        if count == 1:
            hold(ledger)
        if count == 4:
            release(ledger)
        if count == 6:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)

    seen, tick = _admitting(plan)

    drive_cli.run_drive(cfg, tick, interval=1, load=lambda: 0.0)

    assert seen == [
        ("epic:a", 3),
        ("epic:b", 0),
        ("epic:a", 0),
        ("epic:b", 0),
        ("epic:a", 3),
        ("epic:b", 3),
    ]


def test_cli_hold_and_release_outlive_the_drive(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, cfg)

    result = runner.invoke(app, ["drive", "hold"])
    assert result.exit_code == 0, result.output
    assert "running work finishes" in result.output
    assert drive_hold(ledger) is not None

    result = runner.invoke(app, ["drive", "release"])
    assert result.exit_code == 0, result.output
    assert drive_hold(ledger) is None
    result = runner.invoke(app, ["drive", "release"])
    assert "no hold was standing" in result.output


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #


def _wire(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_kw: cfg)
    monkeypatch.setattr(cli, "_epic_driver_config", lambda _mp: (cfg, "o/r", 3, []))


def test_cli_second_drive_refuses_and_names_the_holder(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, cfg)
    held = acquire_drive(ledger)
    assert isinstance(held, DriveLease)

    result = runner.invoke(app, ["drive", "--kew-binary", str(tmp_path / "kew")])

    assert result.exit_code == 1, result.output
    assert f"pid {os.getpid()}" in result.output
    assert current_holder(ledger) == held.holder


def test_cli_detach_refuses_while_a_drive_runs(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, cfg)
    assert isinstance(acquire_drive(ledger), DriveLease)

    result = runner.invoke(
        app, ["drive", "--detach", "--kew-binary", str(tmp_path / "kew")]
    )

    assert result.exit_code == 1, result.output
    assert "another drive already runs" in result.output


def test_cli_pause_resume_and_unknown_epic(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, cfg)

    assert runner.invoke(app, ["drive", "pause", "epic:a"]).exit_code == 0
    assert drive_pauses(ledger).covers("epic:a")
    assert runner.invoke(app, ["drive", "resume", "epic:a"]).exit_code == 0
    assert not drive_pauses(ledger).covers("epic:a")

    result = runner.invoke(app, ["drive", "pause", "epic:typo"])
    assert result.exit_code == 2
    assert "unknown epic" in result.output


def test_cli_stop_with_no_drive_running_says_so(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, cfg)

    result = runner.invoke(app, ["drive", "stop"])

    assert result.exit_code == 1
    assert "no drive is running" in result.output


def test_cli_stop_addresses_the_running_drive(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, cfg)
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)

    result = runner.invoke(app, ["drive", "stop"])

    assert result.exit_code == 0, result.output
    assert stop_for(ledger, lease).all


def test_cli_drive_ticks_each_epic_on_the_pinned_kew(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each tick is a `kew epic _tick` child of the binary --kew-binary names."""
    _wire(monkeypatch, cfg)
    calls: list[tuple[Path, list[str]]] = []

    def pinned(binary: Path, args: list[str]) -> int:
        calls.append((binary, args))
        if len(calls) == 2:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
        return CONTINUE

    monkeypatch.setattr(cli.epic, "_run_pinned_tick", pinned)
    binary = tmp_path / "kew"

    result = runner.invoke(app, ["drive", "--kew-binary", str(binary)])

    assert result.exit_code == 0, result.output
    assert [(b, a[:3]) for b, a in calls] == [
        (binary, ["epic", "_tick", "epic:a"]),
        (binary, ["epic", "_tick", "epic:b"]),
    ]
    # Each child admits under the drive's cap: `[drive] max_parallel`, 3.
    assert all(a[a.index("--admit-cap") + 1] == "3" for _, a in calls)
    assert "stopped after the current tick" in result.output


def test_a_drive_process_that_dies_holding_the_lock_does_not_block_the_next(
    cfg: Any, ledger: Any
) -> None:
    """A real process takes the lock and exits without releasing it."""
    import sys

    db = Path(cfg.logging.dir) / "dispatch.db"
    code = (
        "import sys, os\n"
        "from kew.run_store import SqliteRunStore\n"
        "from kew.drive import acquire_drive, DriveLease\n"
        f"s = SqliteRunStore({str(db)!r})\n"
        "assert isinstance(acquire_drive(s), DriveLease)\n"
        "os._exit(9)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    assert proc.wait() == 9
    crashed = current_holder(ledger)
    assert crashed is not None and crashed.pid == proc.pid

    lease = acquire_drive(ledger)

    assert isinstance(lease, DriveLease)
    assert lease.reclaimed is not None and f"pid {proc.pid} is gone" in lease.reclaimed


# --------------------------------------------------------------------------- #
# A drive that takes over from one that did not stop (#2810)
# --------------------------------------------------------------------------- #


def _restarts(ledger: Any) -> list[dict[str, Any]]:
    rows = ledger.list_audit_events(None, event=DRIVE_RESTARTED_EVENT)
    return [dict(row.payload) for row in rows]


def test_a_drive_started_after_its_previous_pid_is_gone_records_drive_restarted(
    cfg: Any, ledger: Any
) -> None:
    dead = _dead_pid()
    assert isinstance(acquire_drive(ledger, pid=dead), DriveLease)
    pause(ledger, "epic:b")  # a pause the crashed drive saw survives it
    ticks, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    [restart] = _restarts(ledger)
    assert restart["previous_pid"] == dead
    assert restart["reason"] == DRIVE_ENDED_EXITED
    assert restart["pid"] == os.getpid()
    assert f"pid {dead} is gone" in restart["detail"]
    assert ticks == ["epic:a"]


def test_a_drive_whose_holder_predates_the_last_boot_says_reboot(
    cfg: Any, ledger: Any
) -> None:
    """After a reboot the old pid may belong to anything; the boot time, not
    the pid, says the old drive went with the host."""
    beat = datetime.now(UTC) - timedelta(minutes=10)
    old = acquire_drive(ledger, pid=_dead_pid(), now=beat)
    assert isinstance(old, DriveLease)
    _, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(
        cfg, tick, interval=1, booted=lambda: beat + timedelta(minutes=1)
    )

    [restart] = _restarts(ledger)
    assert restart["previous_pid"] == old.holder.pid
    assert restart["reason"] == DRIVE_ENDED_REBOOT


def test_a_drive_that_reclaims_a_silent_live_pid_says_stalled(
    cfg: Any, ledger: Any
) -> None:
    hung = acquire_drive(ledger, now=datetime.now(UTC) - timedelta(hours=1))
    assert isinstance(hung, DriveLease)
    _, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    [restart] = _restarts(ledger)
    assert restart["reason"] == DRIVE_ENDED_STALLED
    assert restart["previous_pid"] == hung.holder.pid


def test_a_drive_after_a_clean_stop_records_no_restart(cfg: Any, ledger: Any) -> None:
    for _ in range(2):
        _, tick = _ticker(_stop_after(1, ledger))
        drive_cli.run_drive(cfg, tick, interval=1)

    assert _restarts(ledger) == []


def test_cli_detach_starts_the_drive_on_the_pinned_kew_with_its_log(
    cfg: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil
    import tempfile

    from support.fake_gcloud import launchable

    _wire(monkeypatch, cfg)
    root = tmp_path if launchable(tmp_path) else Path(tempfile.mkdtemp(dir=Path.home()))
    try:
        binary = root / "bin" / "kew"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\necho "argv $*"\n')
        binary.chmod(0o755)

        result = runner.invoke(
            app, ["drive", "--detach", "--interval", "30", "--kew-binary", str(binary)]
        )

        assert result.exit_code == 0, result.output
        assert "detached as pid" in result.output
        log = Path(cfg.logging.dir) / drive_cli.DRIVE_LOG
        for _ in range(200):
            if log.exists() and "argv" in log.read_text():
                break
            import threading

            threading.Event().wait(0.05)
        assert log.read_text().strip() == (
            f"argv drive --interval 30 --kew-binary {binary} --epic-prefix epic:"
        )
    finally:
        if root != tmp_path:
            shutil.rmtree(root)


def test_each_round_runs_the_merge_pass_over_the_due_epics(
    cfg: Any, ledger: Any
) -> None:
    """The standing merge rule (#2618) runs after a round's ticks, over the
    epics that round ticked — a paused epic's READY nodes are not merged."""
    pause(ledger, "epic:b")
    ticks, tick = _ticker(_stop_after(2, ledger))
    passes: list[list[str]] = []

    def merge_pass(due: Any) -> None:
        passes.append(list(due))
        if len(passes) == 1:
            raise RuntimeError("forge unreachable")  # reported, retried

    drive_cli.run_drive(cfg, tick, interval=1, merge=lambda _store: merge_pass)

    assert ticks == ["epic:a", "epic:a"]
    assert passes == [["epic:a"]]


def test_a_restart_that_cannot_be_recorded_hands_the_lock_back(
    cfg: Any, ledger: Any
) -> None:
    """A failed audit append leaves the lock row as the dead drive left it and
    exits 1; the next start reclaims it again and records the restart."""
    import click

    dead = _dead_pid()
    assert isinstance(acquire_drive(ledger, pid=dead), DriveLease)
    left = ledger.get_meta(DRIVE_HOLDER_KEY)
    ticks, tick = _ticker(_stop_after(1, ledger))

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    with (
        patch.object(SqliteRunStore, "append_audit_event", refuse),
        patch.object(SqliteRunStore, "append_audit_event_once", refuse),
        pytest.raises(click.exceptions.Exit) as exited,
    ):
        drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    assert exited.value.exit_code == 1
    assert ticks == []
    assert ledger.get_meta(DRIVE_HOLDER_KEY) == left
    assert _restarts(ledger) == []

    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    [restart] = _restarts(ledger)
    assert restart["previous_pid"] == dead
    assert ticks == ["epic:a"]


def _killed_after_takeover(db: Path) -> int:
    """A real drive process that wins the lock and is killed before it
    records the restart it owes; its pid."""
    import sys

    code = (
        "import os\n"
        "from kew.run_store import SqliteRunStore\n"
        "from kew.drive import acquire_drive, DriveLease\n"
        f"s = SqliteRunStore({str(db)!r})\n"
        "assert isinstance(acquire_drive(s), DriveLease)\n"
        "os._exit(9)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    assert proc.wait() == 9
    return proc.pid


def test_a_drive_killed_between_takeover_and_its_restart_record_loses_nothing(
    cfg: Any, ledger: Any
) -> None:
    """The lock row names the killed taker, and the takeover it never
    recorded rides in that row: the next drive records both restarts, each
    once, and the row it ticks under owes none."""
    dead = _dead_pid()
    assert isinstance(acquire_drive(ledger, pid=dead), DriveLease)
    killed = _killed_after_takeover(Path(cfg.logging.dir) / "dispatch.db")
    owed: list[Any] = []

    def plan(name: str, count: int) -> None:
        holder = current_holder(ledger)
        assert holder is not None
        owed.append(holder.restarts)
        request_stop(ledger, holder, None)

    _, tick = _ticker(plan)
    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    restarts = _restarts(ledger)[::-1]  # oldest first
    assert [r["previous_pid"] for r in restarts] == [dead, killed]
    assert [r["pid"] for r in restarts] == [killed, os.getpid()]
    assert all(r["reason"] == DRIVE_ENDED_EXITED for r in restarts)
    assert owed == [()]


def test_a_restart_recorded_by_a_drive_killed_before_settling_is_recorded_once(
    cfg: Any, ledger: Any
) -> None:
    """The taker appended its restart and died before dropping it from its
    row; the next drive appends it again under the same key, which the
    ledger holds once."""
    from kew.audit import Actor, emit

    dead = _dead_pid()
    assert isinstance(acquire_drive(ledger, pid=dead), DriveLease)
    taker = acquire_drive(ledger, pid=_dead_pid())
    assert isinstance(taker, DriveLease)
    [owed] = taker.holder.restarts
    emit(
        ledger,
        DRIVE_RESTARTED_EVENT,
        actor=Actor(type="policy", id="drive"),
        payload=owed.event_payload(),
        dedup_key=owed.key,
        strict=True,
    )
    _, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    restarts = _restarts(ledger)[::-1]  # oldest first
    assert [r["previous_pid"] for r in restarts] == [dead, taker.holder.pid]


def test_the_consoles_restart_summary_keeps_the_previous_pid_and_reason(
    cfg: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 20-character host and microsecond timestamps: the console's capped
    excerpt still names the previous pid and why it ended."""
    import socket

    from kew.service.audit import list_audit

    monkeypatch.setattr(socket, "gethostname", lambda: "studio-mac-mini.lan1")
    dead = _dead_pid()
    assert isinstance(acquire_drive(ledger, pid=dead), DriveLease)
    _, tick = _ticker(_stop_after(1, ledger))

    drive_cli.run_drive(cfg, tick, interval=1, booted=lambda: None)

    [event] = list_audit(ledger, event=DRIVE_RESTARTED_EVENT).events
    assert event.summary is not None and len(event.summary) <= 200
    assert event.summary.startswith(
        f'{{"previous_pid":{dead},"reason":"{DRIVE_ENDED_EXITED}"'
    )
