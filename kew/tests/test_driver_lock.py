"""Tests for the per-epic single-driver advisory lock (#861/F2)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kew.graph.driver.lock import (
    HEARTBEAT_STALE_SECONDS,
    LockHolder,
    lock_holder,
    refresh_lock_heartbeat,
    single_driver_lock,
)
from kew.process_groups import process_start_token


def _dead_pid() -> int:
    """A pid guaranteed dead: spawn a trivial child, reap it, reuse its pid."""
    proc = subprocess.Popen([sys.executable, "-c", ""])  # noqa: S603
    proc.wait()
    return proc.pid


def test_second_acquire_is_denied(tmp_path: Path) -> None:
    """A second acquire while the first context is open is denied, no block."""
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock) as got_outer:
        assert got_outer is True
        with single_driver_lock(lock) as got_inner:
            assert got_inner is False


def test_lock_released_after_context(tmp_path: Path) -> None:
    """After the first context exits, the lock is re-acquirable."""
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock) as a:
        assert a is True
    with single_driver_lock(lock) as b:
        assert b is True


def test_lock_holder_none_when_free(tmp_path: Path) -> None:
    """`lock_holder` reports None for a never-acquired (and a released) lock."""
    lock = tmp_path / "epic.lock"
    assert lock_holder(lock) is None
    with single_driver_lock(lock, holder="dag-1"):
        pass
    # Released: the file persists but nobody holds it -> None.
    assert lock_holder(lock) is None


def test_lock_holder_names_epic_pid_and_held_since(tmp_path: Path) -> None:
    """While held, `lock_holder` reports the epic, this process's pid, and a
    held-since timestamp (the lock-transparency contract of #861)."""
    import os

    lock = tmp_path / "epic.lock"
    before = datetime.now(UTC)
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.epic == "dag-1"
        assert holder.pid == os.getpid()
        assert holder.held_since is not None
        assert holder.held_since >= before
        assert holder.is_alive() is True


def test_lock_holder_unknown_when_held_without_tag(tmp_path: Path) -> None:
    """A holder-less acquire is still reported as held (epic 'unknown')."""
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock) as got:
        assert got is True
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.epic == "unknown"


def test_denied_acquire_preserves_holder_tag(tmp_path: Path) -> None:
    """A denied second acquire must NOT wipe the holder's recorded identity."""
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1"):
        with single_driver_lock(lock, holder="dag-2") as got_inner:
            assert got_inner is False
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.epic == "dag-1"


def test_legacy_bare_string_holder_is_tolerated(tmp_path: Path) -> None:
    """A pre-F2 lock file whose content is a bare epic name still decodes."""
    lock = tmp_path / "epic.lock"
    lock.write_text("dag-legacy")
    with open(lock, "a+") as holdfd:
        fcntl.flock(holdfd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-legacy"
            assert holder.pid == 0
            assert holder.is_alive() is True  # unknown pid fails toward "held"
        finally:
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)


def test_stale_holder_is_detected(tmp_path: Path) -> None:
    """A recorded holder whose pid is dead reads as not-alive (stale)."""
    holder = LockHolder(epic="dag-1", pid=_dead_pid(), held_since=datetime.now(UTC))
    assert holder.is_alive() is False


def _write_stale_locked(lock: Path):  # noqa: ANN202 - returns the holding fd
    """Create a lock file recording a DEAD holder and hold flock on it, to
    simulate an orphaned lock (fd survives its process's death)."""
    lock.write_text(
        json.dumps(
            {
                "epic": "dag-1",
                "pid": _dead_pid(),
                "held_since": "2026-07-20T00:00:00+00:00",
            }
        )
    )
    holdfd = open(lock, "a+")  # noqa: SIM115 — returned so the caller holds it
    fcntl.flock(holdfd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holdfd


def test_stale_dead_pid_lock_auto_stolen_without_takeover(tmp_path: Path) -> None:
    """A dead-PID stale lock is stolen AUTOMATICALLY, no takeover needed (#970).

    This is the core #970 fix: a TaskStop'd driver's orphaned lock must not
    demote every later plain `kew epic run` to a silent observer forever.
    """
    import os

    lock = tmp_path / "epic.lock"
    holdfd = _write_stale_locked(lock)
    try:
        with single_driver_lock(lock, holder="dag-2") as got:
            assert got is True  # auto-stolen, not refused
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-2"
            assert holder.pid == os.getpid()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


def test_a_legacy_record_without_a_start_time_is_stolen_on_a_stale_heartbeat(
    tmp_path: Path,
) -> None:
    """Pins the legacy rule for a record an older kew wrote, with no start time
    (#970): a live pid with a stale heartbeat is auto-stolen, no takeover. A
    record that names the holder's start time is never stolen from that live
    process (#2227); see the start-time tests below."""
    lock = tmp_path / "epic.lock"
    holdfd, _ = _write_stale_heartbeat_locked(lock)
    try:
        with single_driver_lock(lock, holder="dag-new") as got:
            assert got is True  # frozen driver's stale heartbeat → auto-steal
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


def test_takeover_reclaims_stale_lock(tmp_path: Path) -> None:
    """`takeover=True` breaks a stale (dead-pid) lock and acquires fresh."""
    import os

    lock = tmp_path / "epic.lock"
    holdfd = _write_stale_locked(lock)
    try:
        with single_driver_lock(lock, holder="dag-2", takeover=True) as got:
            assert got is True
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-2"
            assert holder.pid == os.getpid()
    finally:
        fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


def test_takeover_does_not_stomp_a_live_holder(tmp_path: Path) -> None:
    """A live holder is never taken over, even with takeover set."""
    lock = tmp_path / "epic.lock"
    with (
        single_driver_lock(lock, holder="dag-1"),
        single_driver_lock(lock, holder="dag-2", takeover=True) as got,
    ):
        assert got is False


# ---------------------------------------------------------------------------
# Guard (a): dead-PID lock → new driver acquires and logs the steal (#970)
# ---------------------------------------------------------------------------


def test_dead_pid_takeover_logs_steal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard (a): stealing a dead-pid lock emits a WARNING naming the steal."""
    lock = tmp_path / "epic.lock"
    holdfd = _write_stale_locked(lock)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="kew.graph.driver.lock"),
            single_driver_lock(lock, holder="dag-2", takeover=True) as got,
        ):
            assert got is True
        steal_records = [r for r in caplog.records if "steal" in r.message.lower()]
        assert steal_records, "expected a steal WARNING from the lock module"
        # The log message must name the previous holder (dag-1)
        assert any("dag-1" in r.message for r in steal_records)
    finally:
        fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


def _write_stale_heartbeat_locked(lock: Path) -> tuple:  # noqa: ANN202
    """Create a lock with a LIVE PID but a STALE heartbeat, held via flock.

    Simulates a frozen driver: the process is alive but hasn't ticked recently.
    Returns (holdfd, pid) so callers can clean up.
    """
    import os

    pid = os.getpid()  # live pid — this process
    stale_ts = (
        datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_SECONDS + 60)
    ).isoformat()
    lock.write_text(
        json.dumps(
            {
                "epic": "dag-frozen",
                "pid": pid,
                "held_since": stale_ts,
                "heartbeat": stale_ts,
            }
        )
    )
    holdfd = open(lock, "a+")  # noqa: SIM115 — returned so caller holds it
    fcntl.flock(holdfd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holdfd, pid


def test_stale_heartbeat_takeover_logs_steal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard (a) variant: a stale heartbeat (even with alive PID) triggers steal."""
    lock = tmp_path / "epic.lock"
    holdfd, _ = _write_stale_heartbeat_locked(lock)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="kew.graph.driver.lock"),
            single_driver_lock(lock, holder="dag-new", takeover=True) as got,
        ):
            assert got is True
        steal_records = [r for r in caplog.records if "steal" in r.message.lower()]
        assert steal_records, "expected a steal WARNING from the lock module"
        assert any("dag-frozen" in r.message for r in steal_records)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


# ---------------------------------------------------------------------------
# Guard (b): fresh lock → observer mode with holder identity in the log (#970)
# ---------------------------------------------------------------------------


def test_fresh_lock_observer_logs_holder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Guard (b): when a fresh lock is held, observer mode logs the holder identity."""
    lock = tmp_path / "epic.lock"
    with (
        single_driver_lock(lock, holder="dag-live"),
        caplog.at_level(logging.WARNING, logger="kew.graph.driver.lock"),
        single_driver_lock(lock, holder="dag-observer") as got,
    ):
        assert got is False
    # The observer-mode log must name the lock holder (dag-live)
    assert any(
        "dag-live" in r.message
        for r in caplog.records
        if r.name == "kew.graph.driver.lock"
    ), "observer-mode log must name the live holder"


# ---------------------------------------------------------------------------
# Guard (c): heartbeat refresh on tick (#970)
# ---------------------------------------------------------------------------


def _backdate_heartbeat(lock: Path, *, epic: str, seconds: int) -> datetime:
    """Rewrite the on-disk heartbeat to ``seconds`` ago, keeping this process's
    pid so ownership checks still pass. Returns the backdated timestamp."""
    import os

    existing = lock_holder(lock)
    assert existing is not None and existing.held_since is not None
    old_hb = datetime.now(UTC) - timedelta(seconds=seconds)
    with open(lock, "r+") as f:
        f.write(
            json.dumps(
                {
                    "epic": epic,
                    "pid": os.getpid(),
                    "held_since": existing.held_since.isoformat(),
                    "heartbeat": old_hb.isoformat(),
                }
            )
        )
        f.truncate()
    return old_hb


def test_refresh_lock_heartbeat_updates_timestamp(tmp_path: Path) -> None:
    """Guard (c): refresh writes a STRICTLY newer heartbeat, preserving held_since.

    Backdates the on-disk heartbeat first so the assertion is not vacuous — a
    no-op refresh would fail (the previous test accepted `>=`, which a no-op
    satisfies on a fast machine).
    """
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        initial = lock_holder(lock)
        assert initial is not None
        assert initial.held_since is not None

        _backdate_heartbeat(lock, epic="dag-1", seconds=120)
        backdated = lock_holder(lock)
        assert backdated is not None and backdated.heartbeat is not None

        refreshed_ok = refresh_lock_heartbeat(lock, "dag-1")
        assert refreshed_ok is True

        refreshed = lock_holder(lock)
        assert refreshed is not None and refreshed.heartbeat is not None
        # STRICTLY newer than the backdated value — a no-op cannot pass this.
        assert refreshed.heartbeat > backdated.heartbeat
        # held_since must be preserved (not bumped by a heartbeat refresh).
        assert refreshed.held_since == initial.held_since


def test_refresh_does_not_clobber_a_foreign_holder(tmp_path: Path) -> None:
    """A driver that lost its lock to a stealer must NOT overwrite the thief's
    record (which would resurrect the stolen lock and reset the staleness clock).

    Ownership is by pid: a record whose pid is not ours is refused, untouched.
    """
    lock = tmp_path / "epic.lock"
    thief_pid = _dead_pid()  # any pid guaranteed != os.getpid()
    lock.write_text(
        json.dumps(
            {
                "epic": "thief",
                "pid": thief_pid,
                "held_since": datetime.now(UTC).isoformat(),
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        )
    )
    before = lock.read_text()
    refreshed_ok = refresh_lock_heartbeat(lock, "victim")
    assert refreshed_ok is False
    assert lock.read_text() == before  # thief's record untouched


def test_second_acquirer_after_steal_becomes_observer(tmp_path: Path) -> None:
    """Once a stale lock is auto-stolen, the thief is a live+fresh holder, so a
    further acquirer observes it — the steal is not itself re-stealable (guards
    against a concurrent double-steal breaking the just-installed lock, #970)."""
    lock = tmp_path / "epic.lock"
    holdfd = _write_stale_locked(lock)
    try:
        with single_driver_lock(lock, holder="A") as got_a:
            assert got_a is True  # A auto-stole the dead-pid lock
            with single_driver_lock(lock, holder="B", takeover=True) as got_b:
                assert got_b is False  # A is now live+fresh; B observes
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


def test_takeover_reclaims_indeterminate_legacy_lock(tmp_path: Path) -> None:
    """A legacy record with no heartbeat and a live-looking pid is NOT auto-stolen
    (staleness unprovable) but IS reclaimable under explicit --takeover (#970/F7).
    """
    import os

    lock = tmp_path / "epic.lock"
    # Live pid (ours), no heartbeat field — indeterminate staleness.
    lock.write_text(json.dumps({"epic": "legacy", "pid": os.getpid()}))
    holdfd = open(lock, "a+")  # noqa: SIM115
    fcntl.flock(holdfd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # Without takeover: fail-toward-held, observer.
        with single_driver_lock(lock, holder="fresh") as got_plain:
            assert got_plain is False
        # With takeover: operator reclaims the indeterminate lock.
        with single_driver_lock(lock, holder="fresh", takeover=True) as got_over:
            assert got_over is True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(holdfd.fileno(), fcntl.LOCK_UN)
        holdfd.close()


# --- Per-tick config reload (#1694) -------------------------------------------
#
# The reload lives beside the lock because it governs the same thing: what a
# LIVE driver is allowed to change about itself between ticks. Retargeting the
# tracker, repo, DB or lock is a relaunch; everything else is a reload.


def _pinned(tmp_path: Path, **overrides: object) -> object:
    """A driver config plus the `_DriverPin` a driver would start with."""
    import kew.cli as cli
    from kew.cli.epic import _driver_identity, _DriverPin

    cfg = cli.DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    cfg.project.repo = "o/r"
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(cfg, section), field, value)
    return cfg, _DriverPin(_driver_identity(cfg), cfg)


def test_reload_identity_change_names_the_field_and_both_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Stop and relaunch" with no field named leaves the operator diffing
    kew.toml by hand; the exit says WHAT moved and from what to what."""
    import typer

    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    _cfg, pin = _pinned(tmp_path)
    retargeted, _ = _pinned(tmp_path, **{"project.repo": "o/other"})
    monkeypatch.setattr(cli, "load_config", lambda **_kw: retargeted)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)

    with pytest.raises(typer.Exit) as exc:
        _reload_driver_controls(object(), pin=pin, max_parallel_override=None)

    assert exc.value.exit_code == 2
    err = capsys.readouterr().err
    assert "project.repo: o/r -> o/other" in err


def test_reload_identity_change_names_every_field_that_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    _cfg, pin = _pinned(tmp_path)
    retargeted, _ = _pinned(
        tmp_path, **{"project.repo": "o/other", "tracker.type": "linear"}
    )
    monkeypatch.setattr(cli, "load_config", lambda **_kw: retargeted)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)

    with pytest.raises(typer.Exit):
        _reload_driver_controls(object(), pin=pin, max_parallel_override=None)

    err = capsys.readouterr().err
    assert "tracker.type: github -> linear" in err
    assert "project.repo: o/r -> o/other" in err


def test_reload_that_cannot_load_keeps_the_last_good_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A half-saved kew.toml is an operator mid-edit, not a retarget. Killing a
    healthy driver over an editor autosave loses every in-flight node."""
    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    cfg, pin = _pinned(tmp_path, **{"epic.max_parallel": 4})

    def _boom(**_kw: object) -> object:
        raise ValueError("kew.toml: Expected '=' after a key in a key/value pair")

    monkeypatch.setattr(cli, "load_config", _boom)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)

    reloaded, max_parallel, _approvers = _reload_driver_controls(
        object(), pin=pin, max_parallel_override=None
    )

    assert reloaded is cfg
    assert max_parallel == 4
    err = capsys.readouterr().err
    assert "config reload failed" in err
    assert "Expected '=' after a key" in err
    assert "keeping the last-good config" in err


def test_reload_empty_repo_detection_keeps_the_last_good_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed explicit repo detection cannot retarget the live driver."""
    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    cfg, pin = _pinned(tmp_path)
    reloaded = cli.DispatchConfig.model_validate({"project": {"repo": ""}})
    monkeypatch.setattr(cli, "load_config", lambda **_kw: reloaded)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)

    current, _max_parallel, _approvers = _reload_driver_controls(
        object(), pin=pin, max_parallel_override=None
    )

    assert current is cfg
    assert current.project.repo == "o/r"
    err = capsys.readouterr().err
    assert err.count("config reload failed") == 1
    assert "project.repo auto-detection returned no repository" in err
    assert "keeping the last-good config" in err


def test_reload_omitted_repo_reuses_the_startup_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An omitted repo cannot rerun GitHub detection during reload."""
    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    _cfg, pin = _pinned(tmp_path)
    reloaded = cli.DispatchConfig()
    reloaded.logging.dir = str(tmp_path)
    detections: list[object] = []
    monkeypatch.setattr(cli, "load_config", lambda **_kw: reloaded)
    monkeypatch.setattr(cli, "_detect_repo", detections.append)

    current, _max_parallel, _approvers = _reload_driver_controls(
        object(), pin=pin, max_parallel_override=None
    )

    assert current is reloaded
    assert current.project.repo == "o/r"
    assert detections == []


def test_reload_failure_falls_back_to_the_newest_good_config_not_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Last-good means LAST: a mid-run cap change that loaded cleanly is what a
    later broken edit falls back to, not the value the driver launched with."""
    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    _cfg, pin = _pinned(tmp_path, **{"epic.max_parallel": 1})
    raised, _ = _pinned(tmp_path, **{"epic.max_parallel": 7})
    monkeypatch.setattr(cli, "load_config", lambda **_kw: raised)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)
    _reload_driver_controls(object(), pin=pin, max_parallel_override=None)

    def _boom(**_kw: object) -> object:
        raise ValueError("unreadable")

    monkeypatch.setattr(cli, "load_config", _boom)
    _reloaded, max_parallel, _approvers = _reload_driver_controls(
        object(), pin=pin, max_parallel_override=None
    )

    assert max_parallel == 7


def test_reload_that_loads_cleanly_accepts_the_new_mutable_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import kew.cli as cli
    from kew.cli.epic import _reload_driver_controls

    _cfg, pin = _pinned(tmp_path, **{"epic.max_parallel": 1})
    fresh, _ = _pinned(tmp_path, **{"epic.max_parallel": 5})
    fresh.epic.approvers = ["alice"]
    monkeypatch.setattr(cli, "load_config", lambda **_kw: fresh)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)

    reloaded, max_parallel, approvers = _reload_driver_controls(
        object(), pin=pin, max_parallel_override=None
    )

    assert reloaded is fresh
    assert max_parallel == 5
    assert approvers == ["alice"]


def test_reload_real_file_preserves_last_good_and_reports_identity_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise valid, retargeted, and stale edits through the real loader."""
    import typer

    import kew.cli as cli
    from kew.cli.epic import _driver_identity, _DriverPin, _reload_driver_controls
    from kew.run_store import SqliteRunStore

    config_path = tmp_path / "kew.toml"
    first_log_dir = tmp_path / "logs-first"
    second_log_dir = tmp_path / "logs-second"
    initial = (
        '[project]\nname = "scratch"\nrepo = "o/r"\n\n'
        f'[logging]\ndir = "{first_log_dir}"\n'
    )
    config_path.write_text(initial)
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.delenv("KEW_REPO", raising=False)
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)

    startup = cli.load_config()
    pin = _DriverPin(_driver_identity(startup), startup)
    with SqliteRunStore(str(tmp_path / "reload.db")) as store:
        config_path.write_text(initial + "\n[agent]\ntimeout_minutes = 90\n")
        fresh, _max_parallel, _approvers = _reload_driver_controls(
            store, pin=pin, max_parallel_override=None
        )
        assert fresh.agent.timeout_minutes == 90

        config_path.write_text(initial.replace(str(first_log_dir), str(second_log_dir)))
        with pytest.raises(typer.Exit) as exc:
            _reload_driver_controls(store, pin=pin, max_parallel_override=None)

        assert exc.value.exit_code == 2
        err = capsys.readouterr().err
        old_db = first_log_dir / "dispatch.db"
        new_db = second_log_dir / "dispatch.db"
        assert f"logging.dir/dispatch.db: {old_db} -> {new_db}" in err

        config_path.write_text(initial + "\n[quality]\nmax_diff_lines = 999\n")
        fallback, _max_parallel, _approvers = _reload_driver_controls(
            store, pin=pin, max_parallel_override=None
        )

    assert fallback is fresh
    assert fallback.agent.timeout_minutes == 90
    err = capsys.readouterr().err
    assert "config reload failed" in err
    assert "quality" in err
    assert "keeping the last-good config" in err


def _hold_for(seconds: float) -> None:
    """Block like a busy tick. Not ``time.sleep``: conftest no-ops it suite-wide
    (it patches ``kew.selector.time.sleep``, which is the shared ``time`` module)."""
    threading.Event().wait(seconds)


def test_live_holder_past_stale_threshold_is_not_stolen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A holder busy past the stale line keeps its lock while its PID lives (#2650).

    The background refresher beats the heartbeat during the hold, so a second
    acquirer (``kew epic retry`` during a long dispatching tick) is refused
    instead of stealing.
    """
    from kew.graph.driver import lock as lock_mod

    monkeypatch.setattr(lock_mod, "HEARTBEAT_STALE_SECONDS", 1)
    monkeypatch.setattr(lock_mod, "HEARTBEAT_REFRESH_SECONDS", 0.1)
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        acquired_at = lock_holder(lock)
        assert acquired_at is not None
        _hold_for(1.6)
        with (
            caplog.at_level(logging.WARNING),
            single_driver_lock(lock, holder="retry-probe:dag-1") as got_inner,
        ):
            assert got_inner is False
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.epic == "dag-1"
        assert holder.held_since == acquired_at.held_since
        assert holder.heartbeat is not None
        assert acquired_at.heartbeat is not None
        assert holder.heartbeat > acquired_at.heartbeat
    assert "stealing stale lock" not in caplog.text


def test_holder_whose_refresher_stopped_is_still_stolen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder that stops beating is still stolen past the line once its pid
    no longer names the process that took the lock (a recycled pid)."""
    import os

    from kew.graph.driver import lock as lock_mod

    monkeypatch.setattr(lock_mod, "HEARTBEAT_STALE_SECONDS", 1)
    # The refresher never fires within the test: the simulated hang.
    monkeypatch.setattr(lock_mod, "HEARTBEAT_REFRESH_SECONDS", 3600.0)
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        _hold_for(1.3)
        _rewrite_started(lock, _ANOTHER_START)
        with single_driver_lock(lock, holder="dag-2") as got_inner:
            assert got_inner is True
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-2"
            assert holder.pid == os.getpid()


def test_refresher_stops_on_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No heartbeat thread outlives the ``with`` block that started it."""
    from kew.graph.driver import lock as lock_mod

    monkeypatch.setattr(lock_mod, "HEARTBEAT_REFRESH_SECONDS", 0.05)
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1"):
        assert any(
            t.name == "epic-driver-heartbeat:dag-1" for t in threading.enumerate()
        )
    assert not any(
        t.name == "epic-driver-heartbeat:dag-1" for t in threading.enumerate()
    )


# --- A live holder is never stolen (#2227) ----------------------------------

# A kernel start token no live process on this host can have.
_ANOTHER_START = "0.000001"


def _rewrite_started(lock: Path, started: str) -> None:
    """Rewrite the on-disk record's start time in place (same inode)."""
    record = json.loads(lock.read_text())
    record["started"] = started
    with open(lock, "r+") as f:
        f.write(json.dumps(record))
        f.truncate()


def _hold_record(lock: Path, record: dict[str, object]):  # noqa: ANN202
    """Write ``record`` and flock it from this process (an orphan-style hold)."""
    lock.write_text(json.dumps(record))
    holdfd = open(lock, "a+")  # noqa: SIM115 — returned so the caller holds it
    fcntl.flock(holdfd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holdfd


def _stale_record(pid: int, **extra: object) -> dict[str, object]:
    stale_ts = (
        datetime.now(UTC) - timedelta(seconds=HEARTBEAT_STALE_SECONDS + 60)
    ).isoformat()
    return {
        "epic": "dag-hung",
        "pid": pid,
        "held_since": stale_ts,
        "heartbeat": stale_ts,
        **extra,
    }


def _live_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )


def test_lock_record_names_the_holders_start_time(tmp_path: Path) -> None:
    """The record a driver writes carries its own process start time."""
    import os

    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        holder = lock_holder(lock)
        assert holder is not None
        assert holder.started is not None
        assert holder.started == process_start_token(os.getpid())
        assert json.loads(lock.read_text())["started"] == holder.started


@pytest.mark.parametrize("takeover", [False, True])
def test_a_live_holder_with_a_stale_heartbeat_is_not_stolen(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, takeover: bool
) -> None:
    """A live pid with its recorded start time keeps the lock whatever its
    heartbeat age, and the refusal says to kill it to release the lock."""
    proc = _live_sleeper()
    try:
        started = process_start_token(proc.pid)
        assert started is not None
        lock = tmp_path / "epic.lock"
        holdfd = _hold_record(lock, _stale_record(proc.pid, started=started))
        try:
            with (
                caplog.at_level(logging.WARNING, logger="kew.graph.driver.lock"),
                single_driver_lock(lock, holder="dag-new", takeover=takeover) as got,
            ):
                assert got is False
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-hung"
            assert holder.pid == proc.pid
            assert "stealing stale lock" not in caplog.text
            assert f"kill pid {proc.pid}" in caplog.text
        finally:
            holdfd.close()
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("takeover", [False, True])
def test_a_recycled_pid_with_another_start_time_is_stolen(
    tmp_path: Path, takeover: bool
) -> None:
    """A live pid whose start time differs from the record is a recycled pid."""
    import os

    proc = _live_sleeper()
    try:
        lock = tmp_path / "epic.lock"
        holdfd = _hold_record(lock, _stale_record(proc.pid, started=_ANOTHER_START))
        try:
            with single_driver_lock(lock, holder="dag-new", takeover=takeover) as got:
                assert got is True
                holder = lock_holder(lock)
                assert holder is not None
                assert holder.epic == "dag-new"
                assert holder.pid == os.getpid()
        finally:
            holdfd.close()
    finally:
        proc.kill()
        proc.wait()


def test_a_dead_holder_with_a_recorded_start_time_is_stolen(tmp_path: Path) -> None:
    """Killing a hung holder releases it: its dead pid is stolen as before."""
    lock = tmp_path / "epic.lock"
    holdfd = _hold_record(lock, _stale_record(_dead_pid(), started=_ANOTHER_START))
    try:
        with single_driver_lock(lock, holder="dag-new") as got:
            assert got is True
    finally:
        holdfd.close()


def test_a_lock_without_a_recorded_start_time_keeps_todays_rule(
    tmp_path: Path,
) -> None:
    """An older kew's record (no start time): a stale heartbeat on a live pid is
    stolen, a fresh one is not."""
    proc = _live_sleeper()
    try:
        stale = tmp_path / "stale.lock"
        holdfd = _hold_record(stale, _stale_record(proc.pid))
        try:
            with single_driver_lock(stale, holder="dag-new") as got:
                assert got is True
        finally:
            holdfd.close()
        fresh = tmp_path / "fresh.lock"
        now = datetime.now(UTC).isoformat()
        holdfd = _hold_record(
            fresh,
            {"epic": "dag-live", "pid": proc.pid, "held_since": now, "heartbeat": now},
        )
        try:
            with single_driver_lock(fresh, holder="dag-new", takeover=True) as got:
                assert got is False
        finally:
            holdfd.close()
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("takeover", [False, True])
def test_a_live_holder_with_a_fresh_heartbeat_is_not_stolen_on_another_start_time(
    tmp_path: Path, takeover: bool
) -> None:
    """A start time that does not match proves nothing on its own: a live
    holder whose heartbeat is fresh keeps the lock, takeover or not."""
    proc = _live_sleeper()
    try:
        lock = tmp_path / "epic.lock"
        now = datetime.now(UTC).isoformat()
        record = {
            "epic": "dag-live",
            "pid": proc.pid,
            "held_since": now,
            "heartbeat": now,
            "started": _ANOTHER_START,
        }
        holdfd = _hold_record(lock, record)
        try:
            with single_driver_lock(lock, holder="dag-new", takeover=takeover) as got:
                assert got is False
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-live"
        finally:
            holdfd.close()
    finally:
        proc.kill()
        proc.wait()


def test_a_holder_recorded_under_another_time_zone_is_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder that recorded its start time under one time zone is still the
    same live process to a contender running under another (launchd vs shell),
    so its stale heartbeat does not release it."""
    import time

    proc = _live_sleeper()
    try:
        monkeypatch.setenv("TZ", "UTC0")
        time.tzset()
        started = process_start_token(proc.pid)
        assert started is not None
        lock = tmp_path / "epic.lock"
        holdfd = _hold_record(lock, _stale_record(proc.pid, started=started))
        try:
            monkeypatch.setenv("TZ", "JST-9")
            time.tzset()
            with single_driver_lock(lock, holder="dag-new") as got:
                assert got is False
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.epic == "dag-hung"
        finally:
            holdfd.close()
    finally:
        monkeypatch.undo()
        time.tzset()
        proc.kill()
        proc.wait()


def test_a_start_token_that_could_not_be_read_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read of this process's own start token is not remembered: the
    next acquisition reads it again and records it."""
    import os

    from kew.graph.driver import lock as lock_mod

    real = process_start_token
    # A fresh process: nothing read yet (earlier tests in this one cached it).
    monkeypatch.setattr(lock_mod, "_own_start", {})
    monkeypatch.setattr(lock_mod, "process_start_token", lambda _pid: None)
    lock = tmp_path / "epic.lock"
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        assert "started" not in json.loads(lock.read_text())
    monkeypatch.setattr(lock_mod, "process_start_token", real)
    with single_driver_lock(lock, holder="dag-1") as got:
        assert got is True
        assert json.loads(lock.read_text())["started"] == real(os.getpid())


def test_a_live_holder_whose_recorded_token_matches_is_refused_on_takeover(
    tmp_path: Path,
) -> None:
    """The recorded kernel start token is what proves the holder is the live
    process that took the lock: it is refused, stale heartbeat or not."""
    proc = _live_sleeper()
    try:
        token = process_start_token(proc.pid)
        assert token is not None
        lock = tmp_path / "epic.lock"
        holdfd = _hold_record(lock, _stale_record(proc.pid, started=token))
        try:
            with single_driver_lock(lock, holder="dag-new", takeover=True) as got:
                assert got is False
            holder = lock_holder(lock)
            assert holder is not None
            assert holder.started == token
            assert holder.is_same_live_process() is True
        finally:
            holdfd.close()
    finally:
        proc.kill()
        proc.wait()
