"""`kew drive`: one supervised process ticks every active epic (#2615).

The drive replaces the per-epic shell loops and their pause/stop files. Each
epic tick is a fresh ``kew epic _tick`` child on the kew that ``--kew-binary``
resolves to when the tick starts (#2458), and the review stage runs detached
from it (#2459), so no tick waits on a review or reads a half-moved checkout.

The drive's lock and its operator controls live in the ledger — see
:mod:`kew.drive` — so ``kew drive pause|resume|stop`` work from any shell while
it runs, and a second ``kew drive`` refuses and names the holder.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import typer

from kew import cli as _c
from kew.cli import epic as _epic
from kew.cli._apps import drive_app
from kew.config import DispatchConfig
from kew.drive import (
    DRIVE_RESTARTED_EVENT,
    DRIVE_RUNNING,
    DriveLease,
    DriveReading,
    DriveRefusal,
    DriveStateError,
    HostGate,
    HostReader,
    acquire_drive,
    admission,
    booted_at,
    clear_host_load,
    drive_pauses,
    hand_back_drive,
    hold,
    host_gate,
    live_holder,
    pause,
    read_drive,
    read_load,
    record_exit,
    refresh_drive,
    release,
    release_drive,
    request_stop,
    resume,
    settle_restarts,
    stop_for,
)
from kew.drive_agent import (
    AGENT_DISABLED,
    AGENT_MISSING,
    AGENT_NOT_LOADED,
    AGENT_UNCHECKED,
    AGENT_UNREADABLE,
    checked_binary,
    checked_env_file,
    prepare_log,
)
from kew.drive_code import (
    DRIVE_RESTART_EXIT,
    DRIVE_RESUME_ENV,
    CodeMove,
    CodeWatch,
    SchemaRefusal,
    clear_refusal,
    code_identity,
    read_refusal,
    write_refusal,
)
from kew.drive_heartbeat import (
    DRIVE_HEARTBEAT_KEY,
    STAGE_SLEEPING,
    STAGE_STARTING,
    STAGE_STOPPED,
    STAGE_TICKING,
    DriveEpicTick,
    DriveHeartbeat,
)
from kew.drive_host_load import CALM_TICKS, read_host
from kew.drive_inbox import (
    Notifier,
    deliver_attention,
    desktop_notifier,
    raise_attention,
)
from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S
from kew.epic_archive import archived_epic_refs
from kew.graph.driver.attention import (
    AttentionEvent,
    driver_lock_stalled_action,
    driver_lock_stalled_event,
)
from kew.graph.driver.lock import LockHolder, stalled_holder
from kew.ids import item_ref
from kew.progress import _error, _log, _success, _warn
from kew.run_store import CURRENT_SCHEMA_VERSION
from kew.staleness import _read_db_schema_version
from kew.verify_lease import VerifyProbe, verify_running
from kew.waits import AttentionKind

#: Whether a reservation's tracker item is closed (None: unread), and the
#: factory that builds it once the ledger is open.
IssueClosedFn = Callable[[Any], "bool | None"]
IssueClosedFactory = Callable[[Any], IssueClosedFn]

# How often a running drive refreshes its heartbeat, and the longest it sleeps
# between looks at its stop row. The heartbeat runs on its own thread so a long
# tick never lets it go stale.
_HEARTBEAT_EVERY_S = LOCK_HEARTBEAT_STALE_S / 10
_SLEEP_STEP_S = 5
# How many refused ledger opens in a row the heartbeat takes before it gives
# up: the drive then restarts, or reports itself lost (#3197).
_HEARTBEAT_REFUSALS = 3
_RECLAIMED = "its heartbeat went stale and it was reclaimed"

#: One epic tick: the epic and the admission cap it runs under; returns the
#: tick child's exit code.
Tick = Callable[[str, int], int]

DRIVE_LOG = "drive.log"
DRIVE_PID = "drive.pid"
ARCHIVE_REASON = "complete — archived by kew drive"

#: One merge pass over the due epics (``[drive.merge]``, #2618), and what builds
#: it from the drive's open ledger — None when the rule is off.
MergePass = Callable[[Sequence[str]], None]
MergeFactory = Callable[[Any], MergePass | None]

#: One pass over the queued run requests (#2537), and what builds it from the
#: drive's open ledger and lease.
RequestPass = Callable[[], None]
RequestFactory = Callable[[Any, DriveLease], RequestPass]

#: What a drive stopped by a schema refusal needs (#3284).
_REFUSAL_FIX = (
    "upgrade the kew the drive runs, then `kew drive install`, "
    "or run `kew drive` by hand"
)

#: Replace this process (``os.execve``'s shape): the drive's restart on new
#: code (#3197). It returns only by raising.
Exec = Callable[[str, list[str], dict[str, str]], object]


def _ledger(config: DispatchConfig) -> Path:
    return Path(config.logging.dir).resolve() / "dispatch.db"


class _Heartbeat(threading.Thread):
    """Refresh the drive's lock and write its heartbeat (#2617).

    The thread beats every ``_HEARTBEAT_EVERY_S`` on its own ledger connection,
    so a long tick never lets either go stale; the drive's loop also beats on
    every stage change (:meth:`note`) through its own connection. Both beat
    under one lock and snapshot the state inside it, so the row always ends on
    the newest stage, never on an older snapshot written late.

    ``lost`` is set the moment a refresh finds the lock no longer ours — the
    drive was reclaimed from — and the loop stops at its next look. A beat
    after that writes nothing: the heartbeat row is the new holder's. A refresh
    that fails in a way a retry cannot cure sets it too: the heartbeat would
    otherwise go stale unseen, another drive would reclaim the lock, and this
    one would keep ticking. ``cause`` says which happened, and ``refused``
    whether the ledger open refused it — what a ledger a newer kew migrated
    does, which is the restart's to report, not a lost lock (#3197).
    """

    def __init__(self, db: Path, lease: DriveLease) -> None:
        super().__init__(name="kew-drive-heartbeat", daemon=True)
        self._db, self._lease = db, lease
        self.lost = threading.Event()
        self.refused = False
        self.cause = _RECLAIMED
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._stage = STAGE_STARTING
        self._epic: str | None = None
        self._rotation: tuple[str, ...] = ()
        self._ticks: dict[str, DriveEpicTick] = {}

    def snapshot(self) -> DriveHeartbeat:
        holder = self._lease.holder
        return DriveHeartbeat(
            token=holder.token,
            pid=holder.pid,
            host=holder.host,
            at=datetime.now(UTC),
            every_s=float(_HEARTBEAT_EVERY_S),
            stage=self._stage,
            epic=self._epic,
            rotation=self._rotation,
            ticks=dict(self._ticks),
        )

    def beat(self, store: Any) -> None:
        """Refresh the lock and write the heartbeat, unless the lock is lost.
        Once the ledger open refused the thread, the drive's own connection —
        opened before a newer kew migrated the ledger — still beats, so the
        lock stays fresh while the drive settles before its restart (#3197)."""
        with self._lock:
            if self.lost.is_set() and not self.refused:
                return
            if not refresh_drive(store, self._lease):
                self.cause, self.refused = _RECLAIMED, False
                self.lost.set()
                return
            store.set_meta(DRIVE_HEARTBEAT_KEY, self.snapshot().encode())

    def note(
        self,
        store: Any,
        stage: str,
        *,
        epic: str | None = None,
        rotation: tuple[str, ...] | None = None,
        tick: DriveEpicTick | None = None,
    ) -> None:
        """Record what the drive does now and beat at once. ``rotation`` is
        the epics it will tick — paused and stopped ones are not in it, so the
        heartbeat never vouches for an epic the drive has stopped ticking."""
        with self._lock:
            self._stage, self._epic = stage, epic
            if rotation is not None:
                self._rotation = rotation
            if tick is not None and epic is not None:
                self._ticks[epic] = tick
        try:
            self.beat(store)
        except sqlite3.Error as exc:
            _warn(f"kew drive: heartbeat write failed ({exc}); retrying.")
        except Exception as exc:  # the same give-up as the thread's (#2665)
            _warn(f"kew drive: heartbeat refresh failed ({exc!r}); giving up.")
            self._give_up(f"its heartbeat failed: {type(exc).__name__}: {exc}")

    def _give_up(self, cause: str, *, refused: bool = False) -> None:
        """Report the lock lost, and why; under the beat's lock, so a beat
        never reads ``refused`` and ``lost`` half-written."""
        with self._lock:
            self.cause, self.refused = cause, refused
            self.lost.set()

    def run(self) -> None:
        refused = 0
        while not self._done.wait(_HEARTBEAT_EVERY_S):
            try:
                with _c._open_run_store(str(self._db)) as store:
                    self.beat(store)
                refused = 0
            except sqlite3.Error as exc:
                # Not fatal on its own: a heartbeat that keeps failing goes
                # stale, and the refresh after a reclaim reports the loss.
                _warn(f"kew drive: heartbeat refresh failed ({exc}); retrying.")
            except typer.Exit as exc:
                # The open refused the ledger (a newer schema, another
                # ledger): no retry cures that (#3197).
                refused += 1
                if refused >= _HEARTBEAT_REFUSALS:
                    cause = (
                        f"its heartbeat could not open the ledger {refused} "
                        f"times in a row (exit {exc.exit_code})"
                    )
                    _warn(f"kew drive: {cause}; giving up.")
                    self._give_up(cause, refused=True)
                    return
                _warn(
                    f"kew drive: heartbeat could not open the ledger "
                    f"(exit {exc.exit_code}, {refused}/{_HEARTBEAT_REFUSALS})."
                )
            except Exception as exc:  # the thread must never end unseen
                _warn(f"kew drive: heartbeat refresh failed ({exc!r}); giving up.")
                self._give_up(f"its heartbeat failed: {type(exc).__name__}: {exc}")
                return
            if self.lost.is_set():
                return

    def is_lost(self) -> bool:
        """True once the lock is gone, or nothing is left to keep it fresh."""
        return self.lost.is_set() or not self.is_alive()

    def stop(self) -> None:
        self._done.set()
        if self.is_alive():
            self.join()


@dataclass
class _Rotation:
    """What the drive told the operator last, so a round logs only changes."""

    roster: tuple[str, ...] | None = None
    paused: set[str] = field(default_factory=set)
    stopped: set[str] = field(default_factory=set)
    due: tuple[str, ...] = ()
    cap: int | None = None
    verify: str | None = None


def _roster(store: Any, tracker: str) -> tuple[str, ...]:
    """The epics the drive ticks: on this project's tracker and not archived."""
    trackers = store.epic_trackers()
    retired = archived_epic_refs(store)
    return tuple(
        name
        for name in store.list_epic_refs()
        if trackers.get(name) in (None, tracker) and name not in retired
    )


def _stalled_lock(config: DispatchConfig, name: str) -> LockHolder | None:
    """The live holder of epic ``name``'s driver lock that stopped beating its
    heartbeat, or None (#3331)."""
    return stalled_holder(_c._epic_lock_path(config, name))


def _archive_finished(store: Any, name: str, config: DispatchConfig) -> None:
    """Archive an epic that ticked COMPLETE, taking it out of the rotation."""
    from kew.service.verbs import EpicArchiveDenied, set_epic_archived

    try:
        set_epic_archived(
            store,
            epic_ref=name,
            archived=True,
            reason=ARCHIVE_REASON,
            config=config,
            source="drive",
        )
    except EpicArchiveDenied as exc:
        _log(f"epic {name}: COMPLETE ({exc}).")
        return
    except (LookupError, RuntimeError, sqlite3.Error) as exc:
        # Stays in the rotation: the next COMPLETE tick tries again.
        _warn(f"epic {name}: COMPLETE but could not be archived: {exc}.")
        return
    _success(f"epic {name}: COMPLETE — archived; it leaves the drive's rotation.")


def _report_tick(store: Any, name: str, code: int, config: DispatchConfig) -> None:
    if code == 0:
        _archive_finished(store, name, config)
    elif code == _epic._STALL_EXIT_REPAIR:
        _warn(f"epic {name}: STALLED — needs an operator; the drive keeps ticking it.")
    elif code == _epic._TICK_EXIT_LOCKED_OUT:
        _warn(f"epic {name}: another driver holds this epic's lock; skipped.")
    elif code != _epic._TICK_EXIT_CONTINUE:
        _error(f"epic {name}: tick exited {code}; the drive tries it next round.")


@dataclass
class _Drive:
    """One running drive: its ledger, lease and the tick it runs per epic."""

    store: Any
    lease: DriveLease
    heartbeat: _Heartbeat
    config: DispatchConfig
    tick: Tick
    interval: int
    load: Callable[[], float | str] = read_load
    notify: Notifier | None = None
    rotation: _Rotation = field(default_factory=_Rotation)
    merge: MergePass | None = None
    requests: RequestPass | None = None
    host: HostReader = read_host
    verify: VerifyProbe = verify_running
    issue_closed: IssueClosedFn | None = None
    code: CodeWatch | None = None
    #: Why the drive must restart on new code, once it must (#3197).
    restart: CodeMove | None = None

    def _restart_due(self) -> bool:
        """Whether the code under the drive has moved since it started, read
        fresh before every launch (#3197). The first look that finds it moved
        raises the one attention event; after it, nothing new launches."""
        if self.restart is not None:
            return True
        if self.code is None:
            return False
        moved = self.code.moved()
        if moved is None:
            return False
        self.restart = moved
        event = AttentionEvent(
            kind=AttentionKind.HELD,
            epic="kew drive",
            item="restart",
            reason=moved.reason,
            action="nothing new launches; once the running tick child and any "
            "run-request child finish, the drive "
            + (
                "restarts on the new code"
                if moved.reexec
                else "exits until its kew is upgraded"
            ),
        )
        raise_attention(event, notify=self.notify, warn=_warn)
        return True

    def _host_gate(self, *, tick: bool = True) -> HostGate:
        """Read the host (#2813): an episode that begins raises its one
        attention event, and one that ends says admission resumes. Only a
        ``tick`` read counts toward ending one."""
        gate = host_gate(self.store, self.config, self.host, tick=tick)
        if gate.began and gate.hold is not None:
            event = AttentionEvent(
                kind=AttentionKind.HELD,
                epic="kew drive",
                item="admission",
                reason=f"host overloaded: {gate.hold.reason}",
                action=(
                    "running work continues; admission resumes after "
                    f"{CALM_TICKS} reads below the thresholds"
                ),
            )
            raise_attention(event, notify=self.notify, warn=_warn)
        elif gate.resumed:
            _log(f"kew drive: host load below the thresholds for {CALM_TICKS} reads.")
        return gate

    def _admission(self) -> int:
        """The cap the next tick admits under, read fresh before every tick
        so a hold or a load change reaches the very next one (#2616)."""
        gate = self._host_gate()
        now = admission(self.store, self.config, self.load(), gate.hold)
        if now.cap != self.rotation.cap:
            self.rotation.cap = now.cap
            if now.cap == 0:
                _warn(
                    f"kew drive: admitting nothing new — {now.describe()}; "
                    "running work finishes."
                )
            else:
                _log(f"kew drive: admitting up to {now.describe()}.")
        self._verify_gate()
        return now.cap

    def _verify_gate(self) -> None:
        """Say when a local merge verify starts or stops holding its lease
        (#3190). The cap stands: each launch reads the lease at its own spawn,
        admits ``[drive] launches_during_verify`` nodes per verify and defers
        the rest with one event per node."""
        lease = self.verify()
        seen = None if lease is None else lease.token or lease.describe()
        if seen == self.rotation.verify:
            return
        self.rotation.verify = seen
        if lease is None:
            _log("kew drive: the local merge verify ended; deferred launches go.")
            return
        allowed = self.config.drive.launches_during_verify
        _warn(
            f"kew drive: a local merge verify is running ({lease.describe()}); "
            f"admitting {allowed} new launch(es) until it ends — running work "
            "continues."
        )

    def _check_lock(self) -> None:
        """Exit 2 once the drive lock is lost. A heartbeat the ledger open
        refused while the code under the drive moved, or a newer kew migrated
        the ledger, is the restart's to handle (#3197): the caller's next
        restart check returns, and the drive re-execs or exits naming why."""
        if not self.heartbeat.is_lost():
            return
        if self.heartbeat.refused and self._restart_due():
            return
        cause = self.heartbeat.cause
        if not self.heartbeat.lost.is_set():
            cause = "its heartbeat thread died"
        _error(
            f"kew drive: lost the drive lock ({cause}); exiting without another tick."
        )
        _record_exit(self.store, self.lease, 2, f"lost the drive lock ({cause})")
        raise typer.Exit(2)

    def _note(self, seen: set[str], now: set[str], verb: str, undo: str) -> None:
        for name in sorted(now - seen):
            _log(f"epic {name}: {verb} — {undo}.")
        seen.clear()
        seen.update(now)

    def _due(self, roster: tuple[str, ...]) -> list[str] | None:
        """The epics to tick next, or None once the drive is told to stop."""
        stop = stop_for(self.store, self.lease)
        if stop.all:
            return None
        pauses = drive_pauses(self.store)
        paused = {n for n in roster if pauses.covers(n)}
        self._note(self.rotation.paused, paused, "paused", "`kew drive resume`")
        self._note(
            self.rotation.stopped,
            set(stop.epics) & set(roster),
            "stopped for this drive",
            "a new `kew drive` picks it up",
        )
        due = [n for n in roster if n not in paused and n not in stop.epics]
        self.rotation.due = tuple(due)
        return due

    def _tick(self, name: str) -> None:
        """Run one epic's tick, with the heartbeat saying so before and after."""
        started = DriveEpicTick(started_at=datetime.now(UTC).isoformat())
        due = self.rotation.due
        self.heartbeat.note(
            self.store, STAGE_TICKING, epic=name, rotation=due, tick=started
        )
        cap = self._admission()
        if self._restart_due():
            # The code moved while the tick was admitted: it never spawns.
            return
        code = self.tick(name, cap)
        finished = replace(
            started, finished_at=datetime.now(UTC).isoformat(), exit_code=code
        )
        self.heartbeat.note(self.store, STAGE_TICKING, epic=name, tick=finished)
        _report_tick(self.store, name, code, self.config)
        self._requests()

    def round(self) -> bool:
        """Tick every due epic once; False once the drive must stop."""
        roster = _roster(self.store, self.config.tracker.type)
        if roster != self.rotation.roster:
            self.rotation.roster = roster
            listed = ", ".join(roster) if roster else "nothing yet"
            _log(f"kew drive: rotation is {listed}.")
        self._requests()
        self._sweep()
        ticked = False
        for name in roster:
            self._check_lock()
            due = self._due(roster)
            if due is None or self._restart_due():
                return False
            if name in due:
                self._tick(name)
                ticked = True
        self._check_lock()
        if not ticked:
            self._idle_read()
        deliver_attention(
            self.store,
            roster,
            notify=self.notify,
            log=_log,
            warn=_warn,
            live=self._lock_stall,
        )
        due = self._due(roster)
        if due is None or self._restart_due():
            return False
        self._merge(due)
        return stop_for(self.store, self.lease).all is False

    def _lock_stall(self, name: str) -> AttentionEvent | None:
        """The attention event for an epic whose driver lock a stalled live
        process holds (#3331): never stolen, so its tick only locks out."""
        holder = _stalled_lock(self.config, name)
        if holder is None or holder.started is None:
            return None
        return driver_lock_stalled_event(name, pid=holder.pid, started=holder.started)

    def _sweep(self) -> None:
        """Release the reservations, under ANY epic, that hold a slot for work
        that is gone (#3192): only an epic's own tick reconciles its own, so
        one under an epic nobody ticks would otherwise hold its slot forever.
        The sweep reports a reservation it cannot settle and goes on to the
        next; anything else that fails it is reported here and the next round
        sweeps again — the sweep never ends the drive."""
        from kew.graph.driver.reservation_sweep import sweep_reservations

        try:
            swept = sweep_reservations(
                self.store,
                self.config,
                now=datetime.now(UTC),
                issue_closed=self.issue_closed,
            )
        except Exception as exc:  # noqa: BLE001 - the sweep never ends the drive
            _error(
                f"kew drive: reservation sweep failed ({type(exc).__name__}: "
                f"{exc}); retried next round."
            )
            return
        for released in swept:
            _warn(f"{released.describe()}.")

    def _idle_read(self) -> None:
        """Stand in for the tick a round without one never ran (#2813): an
        empty roster or every epic paused still counts one calm read per
        round, so an episode a request probe opened ends by itself."""
        if self._host_gate().resumed:
            self._requests()

    def _merge(self, due: list[str]) -> None:
        """Run the standing merge rule over the due epics, after their ticks.

        A failure is reported and the next round tries again: the rule never
        merges on a fact it could not read, so a skipped pass merges nothing.
        """
        if self.merge is None:
            return
        try:
            self.merge(due)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _error(f"kew drive: merge pass failed ({exc}); retried next round.")

    def _requests(self) -> None:
        """Run what the console queued (#2537). A failure is reported and the
        next pass tries again; every request step is a compare-and-set, so a
        pass that broke halfway never runs a request twice."""
        if self.requests is None:
            return
        if self._restart_due():
            self._request_child_runs()  # collects one that ended; starts none
            return
        try:
            self.requests()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _error(f"kew drive: run-request pass failed ({exc}); retried shortly.")

    def _request_child_runs(self) -> bool:
        """Whether the run-request child this drive started still runs,
        collecting it once it has ended (#3197); a pass that cannot say counts
        it as running, so a restart never execs over it."""
        from kew.drive_requests import RequestRunner

        runner = self.requests
        if not isinstance(runner, RequestRunner):
            return False
        try:
            return runner.busy()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _error(f"kew drive: run-request collect failed ({exc}); retried shortly.")
            return runner.child is not None

    def settle(self) -> bool:
        """Before a restart, launch nothing and wait — a sleep step at a time,
        the lock checked and the heartbeat kept — until the run-request child
        this drive started has been collected (#3197): an exec would leave it
        running under the same lease with nothing to collect it. False when
        a stop arrives meanwhile: the stop wins over the restart."""
        said = False
        while not stop_for(self.store, self.lease).all:
            if not self._request_child_runs():
                return True
            if not said:
                _log("kew drive: its run-request child runs; restarting once it ends.")
                said = True
            self.heartbeat.note(self.store, STAGE_SLEEPING, rotation=self.rotation.due)
            _c._sleep(_SLEEP_STEP_S)
            self._check_lock()
        return False

    def wait(self) -> bool:
        """Sleep one interval in steps; False when a stop arrives meanwhile."""
        remaining = self.interval
        self.heartbeat.note(self.store, STAGE_SLEEPING, rotation=self.rotation.due)
        while remaining > 0:
            step = min(_SLEEP_STEP_S, remaining)
            _c._sleep(step)
            remaining -= step
            self._check_lock()
            if stop_for(self.store, self.lease).all or self._restart_due():
                return False
            self._requests()
        return True


def _write_pid(path: Path) -> None:
    path.write_text(f"{os.getpid()}\n")


def _remove_pid(path: Path) -> None:
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass  # a newer drive wrote it, or it is already gone


def _gate_requests(requests: RequestPass | None, gate: Callable[[], object]) -> None:
    """Have the request runner read the host before each spend it admits
    (#2813): at start, during sleep and with an empty roster no tick has. The
    read is a probe: it opens or extends an episode, never counts a calm read."""
    from kew.drive_requests import RequestRunner

    if isinstance(requests, RequestRunner):
        requests.gate = gate


def _refusal(refused: DriveRefusal) -> str:
    return (
        f"kew drive: another drive already runs for this project — {refused.detail}."
        " `kew drive stop` ends it after its current tick."
    )


def _record_restart(store: Any, lease: DriveLease, config: DispatchConfig) -> None:
    """Record that this drive took over from one that did not stop (#2810):
    the previous pid, and whether it went with a reboot, exited or stalled.
    Every rotation, pause, hold and cap is read back from the ledger as on
    any start, so the event is the only thing a restart adds.

    The takeover wrote each restart it owes into this drive's own lock row
    (#2870), with any a drive killed before recording its own still owed, so
    each is appended here under its reclaimed drive's key — once, however
    many drives get this far — and only then dropped from the row. The write
    is strict: when it fails the lock row goes back as it was and the drive
    exits 1, so the next start (launchd retries) reclaims it again and records
    the restart."""
    from kew.audit import emit, resolve_actor

    for restart in lease.holder.restarts:
        previous_pid = restart.payload.get("previous_pid")
        try:
            emit(
                store,
                DRIVE_RESTARTED_EVENT,
                actor=resolve_actor(),
                repo=config.project.repo or None,
                payload=restart.event_payload(),
                dedup_key=restart.key,
                strict=True,
            )
        except (RuntimeError, sqlite3.Error) as exc:
            handed_back = _hand_back(store, lease)
            _error(
                f"kew drive: could not record the restart after pid {previous_pid} "
                f"({exc}); {handed_back}, so the next start records it."
            )
            raise typer.Exit(1) from None
        _warn(
            f"kew drive: restarted after pid {previous_pid} "
            f"({restart.payload.get('reason')})."
        )
    try:
        settle_restarts(store, lease)
    except sqlite3.Error as exc:
        # Recorded already: a row that still owes them only costs the next
        # drive a deduplicated append.
        _warn(f"kew drive: the recorded restart stays on the lock row ({exc}).")


def _record_exit(store: Any, lease: DriveLease, code: int, reason: str) -> None:
    """Leave why this drive ends where ``kew drive status`` reads it."""
    try:
        record_exit(store, lease, code, reason)
    except sqlite3.Error as exc:
        _warn(f"kew drive: could not record why it exits ({exc}).")


def _restart(
    store: Any, lease: DriveLease, heartbeat: _Heartbeat, moved: CodeMove, execv: Exec
) -> None:
    """Re-exec this drive with the same argv once its work has settled — no
    tick child runs between rounds, and :meth:`_Drive.settle` has collected
    its run-request child — keeping its lease: the new process takes
    it back by token, on the same pid (#3197). When the exec fails, or would
    only reload this same code onto a ledger it refuses, the drive exits
    ``DRIVE_RESTART_EXIT``, which launchd restarts and status names."""
    heartbeat.stop()
    reason = moved.reason
    if moved.refusal is not None:
        # Beside the ledger: the restart launchd makes refuses it at the open.
        write_refusal(heartbeat._db.parent, moved.refusal)
    if not moved.reexec:
        _error(f"kew drive: {reason}; exiting {DRIVE_RESTART_EXIT}.")
        _record_exit(store, lease, DRIVE_RESTART_EXIT, reason)
        raise typer.Exit(DRIVE_RESTART_EXIT)
    _warn(f"kew drive: {reason} — re-executing {sys.executable}.")
    sys.stdout.flush()
    sys.stderr.flush()
    env = {**os.environ, DRIVE_RESUME_ENV: lease.holder.token}
    try:
        execv(sys.executable, list(sys.orig_argv), env)
    except OSError as exc:
        why = f"{reason}; it could not re-exec itself ({exc})"
        _error(f"kew drive: {why}; exiting {DRIVE_RESTART_EXIT} for launchd.")
        _record_exit(store, lease, DRIVE_RESTART_EXIT, why)
        raise typer.Exit(DRIVE_RESTART_EXIT) from None


def _refuse_newer_ledger(db: Path, source: Path | None) -> None:
    """Record, beside the ledger, a start about to refuse a newer ledger
    schema, so the ledger open exits ``DRIVE_RESTART_EXIT`` as before. A start
    that repeats a refusal recorded within ``REFUSAL_REPEAT_S`` on the same
    kew prints it and exits 0 instead, so launchd's ``KeepAlive`` leaves the
    drive down rather than restarting it every ten seconds (#3284). A start
    the ledger does not refuse clears the record."""
    # Read without opening it as a store, whose open refuses a newer schema.
    schema = _read_db_schema_version(db)
    if schema is None or schema <= CURRENT_SCHEMA_VERSION:
        clear_refusal(db.parent)
        return
    refusal = SchemaRefusal(
        f"ledger schema {schema} is newer than this drive's "
        f"{CURRENT_SCHEMA_VERSION}: upgrade the kew the drive runs",
        schema,
        CURRENT_SCHEMA_VERSION,
        code_identity(source),
        datetime.now(UTC),
    )
    earlier = read_refusal(db.parent)
    if earlier is not None and refusal.repeats(earlier):
        _error(
            f"kew drive: {earlier.describe()}; the same refusal stopped this "
            f"drive at {earlier.at.isoformat(timespec='seconds')}, so it exits 0 "
            f"and stays down: {_REFUSAL_FIX}."
        )
        raise typer.Exit(0)
    write_refusal(db.parent, refusal)


def _hand_back(store: Any, lease: DriveLease) -> str:
    try:
        if hand_back_drive(store, lease):
            return "the lock is handed back"
    except sqlite3.Error as exc:
        return f"the lock could not be handed back ({exc}) and goes stale"
    return "another drive took the lock meanwhile"


def _acquire(
    store: Any, config: DispatchConfig, booted: datetime | None, resume: str | None
) -> DriveLease:
    """Take the drive lock — or this process's own, across a re-exec — and
    record any restart the take owes; exit 1 when another drive holds it."""
    lease = acquire_drive(store, booted=booted, resume=resume)
    if isinstance(lease, DriveRefusal):
        _error(_refusal(lease))
        raise typer.Exit(1)
    if lease.reclaimed:
        _warn(f"kew drive: reclaimed the drive lock from {lease.reclaimed}.")
    _record_restart(store, lease, config)
    return lease


def run_drive(
    config: DispatchConfig,
    tick: Tick,
    interval: int,
    load: Callable[[], float | str] = read_load,
    merge: MergeFactory | None = None,
    requests: RequestFactory | None = None,
    *,
    notify: Notifier | None = None,
    host: HostReader = read_host,
    booted: Callable[[], datetime | None] = booted_at,
    issue_closed: IssueClosedFactory | None = None,
    source: Path | None = None,
    execv: Exec = os.execve,
) -> None:
    """Hold the project's drive lock and tick its epics until told to stop.

    After each round, attention events new since the ledger's cursor go to
    ``notify`` (:mod:`kew.drive_inbox`); without one they reach the log only.
    A drive that reclaims a dead holder's lock records ``drive_restarted``;
    ``booted`` says when this host last booted, to tell a reboot apart.

    ``source`` is the kew package whose identity the drive watches (default:
    its own); when it moves, or the ledger's schema passes this drive's, the
    drive launches nothing new and restarts itself through ``execv`` (#3197).
    """
    db = _ledger(config)
    resume = os.environ.pop(DRIVE_RESUME_ENV, None)
    _refuse_newer_ledger(db, source)
    with _c._open_run_store(str(db)) as store:
        code = CodeWatch(store.schema_version, CURRENT_SCHEMA_VERSION, source)
        lease = _acquire(store, config, booted(), resume)
        heartbeat = _Heartbeat(db, lease)
        pid_file = db.parent / DRIVE_PID
        try:
            heartbeat.note(store, STAGE_STARTING)
            heartbeat.start()
            _write_pid(pid_file)
            _log(f"kew drive: driving as {lease.holder.describe()}.")
            drive = _Drive(
                store, lease, heartbeat, config, tick, interval, load, notify
            )
            drive.host = host
            drive.code = code
            drive.issue_closed = None if issue_closed is None else issue_closed(store)
            drive.merge = None if merge is None else merge(store)
            drive.requests = None if requests is None else requests(store, lease)
            _gate_requests(drive.requests, partial(drive._host_gate, tick=False))
            while drive.round() and drive.wait():
                pass
            # A stop the operator asked for wins over a restart.
            if drive.restart is not None and drive.settle():
                _restart(store, lease, heartbeat, drive.restart, execv)
        finally:
            heartbeat.stop()
            heartbeat.note(store, STAGE_STOPPED)
            if not heartbeat.lost.is_set():
                # A drive stolen from leaves the episode to its thief.
                clear_host_load(store)
            if not release_drive(store, lease):
                _warn("kew drive: the drive lock was no longer ours to release.")
            _remove_pid(pid_file)
    _success("kew drive: stopped after the current tick.")


def _kew_binary(kew_binary: Path | None) -> Path:
    from kew.graph.driver.dispatch import resolve_kew_console_script

    if kew_binary is not None:
        return kew_binary
    found = resolve_kew_console_script()
    if found is None:
        _error("kew drive: no `kew` executable found; pass --kew-binary.")
        raise typer.Exit(2)
    return Path(found)


def _pinned_tick(
    pin: Any, binary: Path, interval: int, max_parallel: int | None, prefix: str
) -> Tick:
    def tick(name: str, admit_cap: int) -> int:
        args = _epic._pinned_tick_args(
            name,
            pin=pin,
            interval=interval,
            max_parallel=max_parallel,
            takeover=False,
            epic_prefix=prefix,
            admit_cap=admit_cap,
        )
        return _epic._run_pinned_tick(binary, args)

    return tick


def _merge_factory(config: DispatchConfig, repo: str, prefix: str) -> MergeFactory:
    """Build the drive's merge pass once the ledger is open: the standing rule
    when ``[drive.merge]`` is on, then one pass of the merge queue (#2814)."""

    def build(store: Any) -> MergePass | None:
        from kew.config import project_root
        from kew.policy import repo_root
        from kew.project_merge_queue import QueueRunner

        base = config.branches.base or "main"
        # The project's repository, never the cwd's: the tick's merge verifier
        # and `kew epic merge` work there too (#3108).
        root = repo_root(project_root())
        rule = _standing_rule(config, store, repo, prefix, root, base)
        queue = QueueRunner(
            config, store, repo=repo, repo_root=root, base=base, say=_log
        )

        def merge_pass(epics: Sequence[str]) -> None:
            if rule is not None:
                rule(epics)
            queue.run_once()
            queue.run_reports()

        return merge_pass

    return build


def _standing_rule(
    config: DispatchConfig, store: Any, repo: str, prefix: str, root: Path, base: str
) -> MergePass | None:
    """The ``[drive.merge]`` executor, or None when the rule is off."""
    from kew.drive_merge import DriveMerger

    if not config.drive.merge.enabled:
        return None

    def reader() -> Any:
        return _epic._epic_driver_sources(
            config, repo=repo, epic_prefix=prefix, store=store
        )[1]

    _log(f"kew drive: standing merge rule on, signer {config.drive.merge.signer}.")
    return DriveMerger(
        config,
        store,
        repo=repo,
        repo_root=root,
        reader=reader,
        provider=_c.provider_from_config(config),
        base=base,
        say=_log,
    )


def _request_factory(config: DispatchConfig, binary: Path) -> RequestFactory:
    """Build the drive's run-request executor once the ledger is open."""

    def build(store: Any, lease: DriveLease) -> RequestPass:
        from kew.drive_requests import RequestRunner
        from kew.subprocess_utils import pin_current_executable, spawn_pinned_request

        def spawn(argv: list[str], log: Path, actor: str, env: dict[str, str]) -> Any:
            # Resolved and pinned per request, like a tick: a repointed kew
            # reaches the next request, never one already starting.
            pinned = pin_current_executable(Path(os.path.realpath(binary)))
            return spawn_pinned_request(pinned, argv, log, actor, extra_env=env)

        return RequestRunner(
            store, lease, config, _ledger(config).parent, spawn=spawn, say=_log
        )

    return build


def _issue_closed_factory(
    config: DispatchConfig, repo: str, prefix: str
) -> IssueClosedFactory:
    """Read whether a reservation's tracker item is closed, for the sweep.

    The facts reader is built on the first read, so a drive whose sweep never
    asks builds none. Anything that keeps the item unread answers None, and
    an unread item is never released on.
    """

    def build(store: Any) -> IssueClosedFn:
        readers: list[Any] = []

        def closed(reservation: Any) -> bool | None:
            item = str(reservation.item_id)
            if config.tracker.type == "github" and not item.isdigit():
                return None
            try:
                if not readers:
                    _, facts, _ = _epic._epic_driver_sources(
                        config, repo=repo, epic_prefix=prefix, store=store
                    )
                    readers.append(facts)
                key: int | str = int(item) if item.isdigit() else item
                return bool(readers[0].issue_facts(key).state == "CLOSED")
            except Exception as exc:  # noqa: BLE001 - an unread item stays held
                _warn(f"kew drive: could not read {item}'s tracker state ({exc}).")
                return None

        return closed

    return build


def _drive_args(
    binary: Path, interval: int, max_parallel: int | None, epic_prefix: str
) -> list[str]:
    """The options a detached or launchd-started drive runs under."""
    args = ["--interval", str(interval), "--kew-binary", str(binary)]
    args += ["--epic-prefix", epic_prefix]
    if max_parallel is not None:
        args += ["--max-parallel", str(max_parallel)]
    return args


def _detach(
    config: DispatchConfig,
    binary: Path,
    interval: int,
    max_parallel: int | None,
    epic_prefix: str,
) -> None:
    """Start the drive in its own session and return once it is spawned."""
    from kew.subprocess_utils import pin_current_executable, spawn_pinned_drive

    db = _ledger(config)
    with _c._open_run_store(str(db)) as store:
        holder = live_holder(store)
    if holder is not None:
        _error(_refusal(DriveRefusal(holder, f"held by {holder.describe()}")))
        raise typer.Exit(1)
    resolved = Path(os.path.realpath(binary))
    args = ["drive", *_drive_args(binary, interval, max_parallel, epic_prefix)]
    log = db.parent / DRIVE_LOG
    try:
        pid = spawn_pinned_drive(pin_current_executable(resolved), args, log)
    except (OSError, ValueError) as exc:
        _error(f"kew drive --detach: could not start {resolved}: {exc}.")
        raise typer.Exit(2) from exc
    _success(
        f"kew drive: detached as pid {pid}; log {log}, pid file "
        f"{db.parent / DRIVE_PID}. `kew drive stop` ends it."
    )


@drive_app.callback()
def drive(
    ctx: typer.Context,
    interval: int = typer.Option(
        60, "--interval", help="Seconds between rounds over the active epics."
    ),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    kew_binary: Annotated[
        Path | None,
        typer.Option(
            "--kew-binary",
            help="The kew each tick runs on, resolved (through any symlink) at "
            "tick start. Defaults to the kew running this command.",
        ),
    ] = None,
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Run in the background; pid and log land beside the ledger.",
    ),
    epic_prefix: str = "epic:",
) -> None:
    """Tick every active, non-archived epic of the project, one round per
    ``--interval``, until `kew drive stop`. One drive runs per project."""
    if ctx.invoked_subcommand is not None:
        return
    config, repo, _, _ = _c._epic_driver_config(max_parallel)
    log_dir = _ledger(config).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    binary = _kew_binary(kew_binary)
    if detach:
        _detach(config, binary, interval, max_parallel, epic_prefix)
        return
    pin = _epic._DriverPin(_epic._driver_identity(config), config)
    tick = _pinned_tick(pin, binary, interval, max_parallel, epic_prefix)
    try:
        run_drive(
            config,
            tick,
            interval,
            merge=_merge_factory(config, repo, epic_prefix),
            requests=_request_factory(config, binary),
            notify=desktop_notifier(),
            issue_closed=_issue_closed_factory(config, repo, epic_prefix),
        )
    except (sqlite3.Error, DriveStateError) as exc:
        _error(f"kew drive: ledger {_ledger(config)}: {exc}.")
        raise typer.Exit(2) from None


def _install_agent(store: Any, spec: Any) -> Any:
    """Write and load ``spec``, replacing only the install the ledger records
    (an unreadable record is rewritten, not trusted), once a reinstall a kill
    left in flight is settled (#2870). A restore that is still unsettled
    stops the install, so its journal and saved plist stay for `status` to
    retry (#2955)."""
    from kew.drive_agent import (
        AgentRefused,
        pending_switch,
        recorded_agent,
        recover_switch,
        write_and_load,
    )

    recovered = recover_switch(spec.project)
    if recovered is not None:
        _warn(f"kew drive install: {recovered}.")
    if pending_switch(spec.label) is not None:
        raise AgentRefused(
            "the previous agent is not yet restored, so nothing was changed; "
            "`kew drive status` retries the restore"
        )
    try:
        recorded = recorded_agent(store)
    except DriveStateError as exc:
        _warn(f"kew drive install: replacing {exc}.")
        recorded = None
    return write_and_load(spec, store, recorded)


@drive_app.command("install")
def drive_install(
    interval: int = typer.Option(
        60, "--interval", help="Seconds between rounds over the active epics."
    ),
    max_parallel: int | None = typer.Option(None, "--max-parallel"),
    kew_binary: Annotated[
        Path | None,
        typer.Option("--kew-binary", help="The kew the agent runs the drive on."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="A shell env file the agent sources before the drive starts "
            "(where your credentials already live). Only its path is written.",
        ),
    ] = None,
    epic_prefix: str = "epic:",
) -> None:
    """Install the drive as a launchd agent (macOS): it starts at login and
    restarts when it exits abnormally. `kew drive stop` still ends it."""
    from kew.config import config_environment, project_root
    from kew.drive_agent import (
        AgentRefused,
        AgentSpec,
        agent_lock,
        launchd_available,
    )

    if not launchd_available():
        _error("kew drive install: launchd agents need macOS with launchctl.")
        raise typer.Exit(2)
    config = _c.load_config()
    db = _ledger(config)
    # The agent this loads starts a drive on the kew it names: a refusal the
    # last one recorded must not keep it down (#3284).
    clear_refusal(db.parent)
    try:
        sourced = None if env_file is None else checked_env_file(env_file)
        binary = checked_binary(_kew_binary(kew_binary))
        prepare_log(db.parent / DRIVE_LOG)
    except (AgentRefused, OSError) as exc:
        _error(f"kew drive install: {exc}.")
        raise typer.Exit(2) from None
    spec = AgentSpec(
        project=project_root(),
        binary=binary,
        drive_args=tuple(_drive_args(binary, interval, max_parallel, epic_prefix)),
        log=db.parent / DRIVE_LOG,
        env_file=sourced,
        environment=config_environment(os.environ, Path.cwd()),
    )
    try:
        with _c._open_run_store(str(db)) as store, agent_lock(spec.label, create=True):
            agent = _install_agent(store, spec)
    except (AgentRefused, OSError, RuntimeError, sqlite3.Error) as exc:
        _error(f"kew drive install: {exc}.")
        raise typer.Exit(2) from None
    _success(
        f"kew drive: installed {agent.label} at {agent.plist}; it starts the "
        "drive at login and restarts it if it exits abnormally."
    )


@drive_app.command("uninstall")
def drive_uninstall() -> None:
    """Remove the launchd agent `kew drive install` wrote; a drive it started
    is stopped by launchd."""
    from kew.config import project_root
    from kew.drive_agent import (
        AgentRefused,
        agent_label,
        agent_lock,
        known_agent,
        launchd_available,
        record_agent,
        recover_switch,
        unload_and_remove,
    )

    def act(store: Any) -> str:
        try:
            # The lock is taken even when the LaunchAgents directory has gone,
            # so the bootout runs under it and an orphaned one keeps it (#2930).
            with agent_lock(agent_label(project_root()), create=launchd_available()):
                # A reinstall a kill left in flight is settled first, and an
                # install killed before it recorded is found at its plist
                # (#2870).
                recover_switch(project_root())
                agent, recorded = known_agent(store, project_root())
                if agent is None:
                    return "kew drive: no agent is installed for this project."
                unload_and_remove(agent, project_root(), recorded=recorded)
                record_agent(store, None)
        except (AgentRefused, OSError, RuntimeError) as exc:
            _error(f"kew drive uninstall: {exc}.")
            raise typer.Exit(2) from None
        return f"kew drive: uninstalled {agent.label}; removed {agent.plist}."

    _control("uninstall", act)


EpicArg = Annotated[str | None, typer.Argument(help="One epic; omit for every epic.")]


def _control(verb: str, act: Callable[[Any], str]) -> None:
    """Open the project ledger and apply one operator control to it."""
    config = _c.load_config()
    db = _ledger(config)
    if not db.is_file():
        _error(f"kew drive {verb}: no ledger at {db}")
        raise typer.Exit(2)
    try:
        with _c._open_run_store(str(db)) as store:
            message = act(store)
    except (sqlite3.Error, DriveStateError) as exc:
        _error(f"kew drive {verb}: ledger {db}: {exc}.")
        raise typer.Exit(2) from None
    _success(message)


def _known(store: Any, verb: str, epic: str | None) -> None:
    if epic is not None and epic not in set(store.list_epic_refs()):
        _error(
            f"kew drive {verb}: unknown epic {epic!r}: no tick for it in this ledger"
        )
        raise typer.Exit(2)


@drive_app.command("pause")
def drive_pause(epic: EpicArg = None) -> None:
    """Stop ticking EPIC (or every epic) until `kew drive resume`; a running
    tick finishes. The pause is kept in the ledger and outlives the drive."""

    def act(store: Any) -> str:
        _known(store, "pause", epic)
        pause(store, epic)
        return f"kew drive: paused {epic or 'every epic'}."

    _control("pause", act)


@drive_app.command("resume")
def drive_resume(epic: EpicArg = None) -> None:
    """Tick EPIC again — or, with no EPIC, lift every pause."""

    def act(store: Any) -> str:
        _known(store, "resume", epic)
        pauses = resume(store, epic)
        if pauses.all:
            return f"kew drive: resumed {epic}; every epic is still paused."
        return f"kew drive: resumed {epic or 'every epic'}."

    _control("resume", act)


@drive_app.command("hold")
def drive_hold() -> None:
    """Admit no new work — dispatch, review or remediation — until `kew drive
    release`. Every epic keeps ticking, so running work finishes and merges.
    The hold is kept in the ledger and outlives the drive."""

    def act(store: Any) -> str:
        held = hold(store)
        since = held.since.isoformat(timespec="seconds")
        return (
            f"kew drive: holding admission (since {since}); running work "
            "finishes. `kew drive release` lifts it."
        )

    _control("hold", act)


@drive_app.command("release")
def drive_release() -> None:
    """Lift `kew drive hold`: the next tick admits under the cap again."""

    def act(store: Any) -> str:
        if release(store) is None:
            return "kew drive: no hold was standing; nothing to release."
        return "kew drive: released; the next tick admits under the cap again."

    _control("release", act)


@drive_app.command("stop")
def drive_stop(epic: EpicArg = None) -> None:
    """Stop the running drive after its current tick — or, with EPIC, stop
    ticking that epic for the rest of this drive's run."""

    def act(store: Any) -> str:
        _known(store, "stop", epic)
        holder = live_holder(store)
        if holder is None:
            _error("kew drive stop: no drive is running for this project.")
            raise typer.Exit(1)
        request_stop(store, holder, epic)
        what = f"ticking {epic}" if epic else "after its current tick"
        return f"kew drive: asked {holder.describe()} to stop {what}."

    _control("stop", act)


def _ago(stamp: str | None, now: datetime) -> str:
    if stamp is None:
        return "—"
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return f"{(now - at).total_seconds():.0f}s ago"


def _beat_document(beat: DriveHeartbeat, now: datetime) -> dict[str, Any]:
    return {
        "at": beat.at.isoformat(),
        "age_s": round(beat.age_s(now), 1),
        "every_s": beat.every_s,
        "stall_after_s": beat.stall_after_s,
        "stage": beat.stage,
        "epic": beat.epic,
        "rotation": list(beat.rotation),
        "ticks": {k: v.to_json() for k, v in sorted(beat.ticks.items())},
    }


def _settled_switch() -> str | None:
    """Settle a reinstall a kill left in flight, unless an install or
    uninstall is running now: its switch is live, not left (#2870)."""
    from kew.config import project_root
    from kew.drive_agent import (
        AgentBusy,
        agent_label,
        agent_lock,
        pending_switch,
        recover_switch,
    )

    label = agent_label(project_root())
    if pending_switch(label) is None:
        return None
    try:
        with agent_lock(label):
            return recover_switch(project_root())
    except AgentBusy:
        return "a `kew drive install` or uninstall is changing it right now"


def _agent_reading(store: Any) -> dict[str, str] | None:
    """The installed launchd agent and what launchd makes of it, if any —
    recorded in the ledger, or found at its plist when an install was killed
    before it recorded (#2870)."""
    from kew.config import project_root
    from kew.drive_agent import AgentRefused, agent_state, known_agent

    try:
        # A reinstall a kill left in flight is settled before it is read.
        recovered = _settled_switch()
        agent, recorded = known_agent(store, project_root())
        state = None if agent is None else agent_state(agent)
    except (DriveStateError, AgentRefused, OSError, RuntimeError) as exc:
        return {"label": "", "plist": "", "state": AGENT_UNREADABLE, "detail": str(exc)}
    if agent is None or state is None:
        return None
    reading = {
        "label": agent.label,
        "plist": str(agent.plist),
        "state": state,
    }
    if not recorded:
        reading["detail"] = _UNRECORDED
    if recovered is not None:
        reading["recovered"] = recovered
    return reading


_UNRECORDED = "found at its plist, not recorded in the ledger"


_AGENT_ADVICE = {
    AGENT_MISSING: "its plist is gone; `kew drive install` writes it again",
    AGENT_NOT_LOADED: "launchd will not start the drive; `kew drive install` loads it",
    AGENT_DISABLED: "launchd will not start the drive; "
    "`launchctl enable gui/$(id -u)/{label}`, then `kew drive install`",
    AGENT_UNCHECKED: "launchctl is not available here to check it",
    AGENT_UNREADABLE: "{detail}; `kew drive install` records it again",
}


def _print_agent(agent: dict[str, str] | None) -> None:
    if agent is None:
        return
    state = agent["state"]
    advice = _AGENT_ADVICE.get(state)
    tail = "" if advice is None else f" — {advice.format_map(agent)}"
    if agent.get("detail") == _UNRECORDED:
        tail += f" ({_UNRECORDED}; `kew drive uninstall` removes it)"
    typer.echo(f"  launchd agent {agent['label'] or '?'}: {state}{tail}.")
    if "recovered" in agent:
        typer.echo(f"  launchd agent: {agent['recovered']}.")


def _age(seconds: float) -> str:
    """A reservation's age the way an operator reads it: ``45s``, ``12m``,
    ``2h``, ``73d``."""
    whole = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if whole >= size:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def _slot_holders(store: Any, now: datetime) -> list[dict[str, Any]]:
    """Every epic reservation in the ledger: each holds a fleet slot until its
    run ends or the sweep releases it (#3192), with the run's owner pid, what
    is known of that owner, and the sweep's verdict on it."""
    from kew.graph.driver.reservation_sweep import slot_holders

    return [
        {
            "epic": h.reservation.epic_ref,
            "item": h.reservation.item_id,
            "run_id": h.reservation.run_id,
            "reserved_at": h.reservation.reserved_at.isoformat(),
            "age_s": round((now - h.reservation.reserved_at).total_seconds(), 1),
            "owner_pid": h.owner_pid,
            "owner": h.owner,
            "verdict": h.verdict,
        }
        for h in slot_holders(store, now=now)
    ]


def _print_slot_holders(holders: list[dict[str, Any]]) -> None:
    if not holders:
        typer.echo("  reservations holding a slot: none")
        return
    typer.echo(f"  reservations holding a slot: {len(holders)}")
    for held in holders:
        pid = held["owner_pid"]
        owner = held["owner"] if pid is None else f"owner pid {pid} {held['owner']}"
        typer.echo(
            f"    {held['epic']}: {item_ref(held['item'])} run {held['run_id']}, "
            f"reserved {_age(held['age_s'])} ago, {owner} — {held['verdict']}"
        )


def _status_document(
    reading: DriveReading,
    now: datetime,
    agent: dict[str, str] | None = None,
    reservations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    holder = reading.holder
    return {
        "reservations": reservations or [],
        "state": reading.state,
        "reason": reading.reason,
        "agent": agent,
        "holder": None
        if holder is None
        else {
            "pid": holder.pid,
            "host": holder.host,
            "started_at": holder.started_at.isoformat(),
            "heartbeat_at": holder.heartbeat.isoformat(),
        },
        "heartbeat": None
        if reading.heartbeat is None
        else _beat_document(reading.heartbeat, now),
        "exit": None
        if reading.ended is None
        else {
            "code": reading.ended.code,
            "reason": reading.ended.reason,
            "pid": reading.ended.pid,
            "at": reading.ended.at.isoformat(),
        },
    }


def _print_beat(beat: DriveHeartbeat, now: datetime) -> None:
    doing = f"{beat.stage} {beat.epic}" if beat.epic else beat.stage
    typer.echo(
        f"  heartbeat {beat.age_s(now):.0f}s ago (every {beat.every_s:.0f}s, "
        f"stalled after {beat.stall_after_s:.0f}s); stage: {doing}"
    )
    typer.echo(f"  rotation: {', '.join(beat.rotation) or 'nothing'}")
    for name, tick in sorted(beat.ticks.items()):
        if tick.finished_at is None:
            typer.echo(f"  {name}: ticking since {_ago(tick.started_at, now)}")
        else:
            typer.echo(
                f"  {name}: last tick started {_ago(tick.started_at, now)}, "
                f"finished {_ago(tick.finished_at, now)}, exit {tick.exit_code}"
            )


def _print_status(reading: DriveReading, now: datetime) -> None:
    holder = reading.holder
    if holder is None:
        typer.echo("kew drive: not running.")
    else:
        why = f" — {reading.reason}" if reading.reason else ""
        typer.echo(f"kew drive: {reading.state}{why}: {holder.describe(now=now)}.")
    if reading.heartbeat is not None:
        if holder is None:
            typer.echo("  last drive's final heartbeat:")
        _print_beat(reading.heartbeat, now)
    elif holder is not None:
        typer.echo("  no heartbeat written by this drive yet.")
    if reading.ended is not None:
        ended = reading.ended
        typer.echo(
            f"  pid {ended.pid} exited {ended.code} {_ago(ended.at.isoformat(), now)}"
            f": {ended.reason}."
        )


def _stalled_locks(store: Any, config: DispatchConfig) -> list[dict[str, Any]]:
    """Every rotation epic whose driver lock a stalled live process holds
    (#3331): its ticks only lock out until a person kills that process."""
    stalled: list[dict[str, Any]] = []
    for name in _roster(store, config.tracker.type):
        try:
            holder = _stalled_lock(config, name)
        except Exception as exc:  # noqa: BLE001 — any failed read is the same
            # A damaged lock file must not end the status read (#3331).
            _warn(f"epic {name}: driver lock unreadable ({exc}); not listed.")
            continue
        if holder is None:
            continue
        beat = holder.heartbeat
        stalled.append(
            {
                "epic": name,
                "pid": holder.pid,
                "started": holder.started,
                "heartbeat": None if beat is None else beat.isoformat(),
                "action": driver_lock_stalled_action(holder.pid),
            }
        )
    return stalled


def _print_stalled_locks(stalled: list[dict[str, Any]], now: datetime) -> None:
    for lock in stalled:
        beat = lock["heartbeat"]
        last = "" if beat is None else f" (last beat {_ago(beat, now)})"
        typer.echo(
            f"  epic {lock['epic']}: STALLED — its driver lock is held by pid "
            f"{lock['pid']}, alive but no longer beating its heartbeat{last}; "
            f"{lock['action']}."
        )


def _print_refusal(refusal: SchemaRefusal, now: datetime) -> None:
    typer.echo(
        f"kew drive: stopped {_ago(refusal.at.isoformat(), now)} by a refusal: "
        f"{refusal.describe()}."
    )
    typer.echo(f"  fix: {_REFUSAL_FIX}.")


def _refusal_document(refusal: SchemaRefusal) -> dict[str, Any]:
    return {
        "reason": refusal.reason,
        "ledger_schema": refusal.ledger_schema,
        "drive_schema": refusal.drive_schema,
        "identity": refusal.identity,
        "at": refusal.at.isoformat(),
        "fix": _REFUSAL_FIX,
    }


@drive_app.command("status")
def drive_status(
    json_output: bool = typer.Option(
        False, "--json", help="Print one JSON document instead of text."
    ),
) -> None:
    """Print the running drive's heartbeat: its stage, rotation and last tick
    per epic. Exits 1 when no drive runs or its heartbeat has stalled."""
    config = _c.load_config()
    db = _ledger(config)
    if not db.is_file():
        _error(f"kew drive status: no ledger at {db}")
        raise typer.Exit(2)
    now = datetime.now(UTC)
    refusal = _standing_refusal(db)
    if refusal is not None and _refuses_this_kew(db):
        # Before the ledger open, which refuses the same newer schema.
        _report_refused(refusal, now, json_output=json_output)
    try:
        with _c._open_run_store(str(db)) as store:
            reading = read_drive(store, now=now)
            agent = _agent_reading(store)
            holders = _slot_holders(store, now)
            stalled = _stalled_locks(store, config)
    except sqlite3.Error as exc:
        _error(f"kew drive status: ledger {db}: {exc}.")
        raise typer.Exit(2) from None
    if reading.state == DRIVE_RUNNING:
        # A drive runs on this ledger: a refusal some other kew recorded did
        # not stop it.
        refusal = None
    if json_output:
        document = _status_document(reading, now, agent, holders)
        document["refusal"] = None if refusal is None else _refusal_document(refusal)
        document["stalled_locks"] = stalled
        typer.echo(json.dumps(document, indent=2))
    else:
        if refusal is not None:
            _print_refusal(refusal, now)
        _print_status(reading, now)
        _print_stalled_locks(stalled, now)
        _print_slot_holders(holders)
        _print_agent(agent)
    if reading.state != DRIVE_RUNNING:
        raise typer.Exit(1)


def _standing_refusal(db: Path) -> SchemaRefusal | None:
    """The recorded refusal while it still applies: the ledger's schema is
    still newer than the refused drive's. A ledger since put right, or one a
    newer drive's kew can read, leaves the record standing for nothing."""
    refusal = read_refusal(db.parent)
    if refusal is None:
        return None
    schema = _read_db_schema_version(db)
    if schema is None or schema <= refusal.drive_schema:
        return None
    return refusal


def _refuses_this_kew(db: Path) -> bool:
    """Whether this kew's own ledger open will refuse the ledger's schema."""
    schema = _read_db_schema_version(db)
    return schema is not None and schema > CURRENT_SCHEMA_VERSION


def _report_refused(
    refusal: SchemaRefusal, now: datetime, *, json_output: bool
) -> None:
    """Report a refusal the ledger open is about to repeat. The text names it
    and lets the open fail as it does (exit 2); ``--json`` cannot open the
    ledger to read the rest, so it prints a ``refused`` document and exits 2
    without the open (#3331)."""
    if not json_output:
        _print_refusal(refusal, now)
        return
    document: dict[str, Any] = {
        "reservations": [],
        "state": "refused",
        "reason": refusal.reason,
        "agent": None,
        "holder": None,
        "heartbeat": None,
        "exit": None,
        "refusal": _refusal_document(refusal),
        "stalled_locks": [],
    }
    typer.echo(json.dumps(document, indent=2))
    raise typer.Exit(2)
