"""The project drive's ledger state: who holds it, and what it was told (#2615).

`kew drive` is one supervised process that ticks every active epic of a
project. Exactly one may run per ledger, so its lock lives IN the ledger — a
``meta`` row — rather than in a file beside it: every reader that can open the
ledger can name the holder, and a refusal names it too.

The holder record carries a ``pid``, a ``host`` and a ``heartbeat``. A holder
is *stale* — and the next ``kew drive`` reclaims it, saying why — when its pid
is gone on this host or its heartbeat is older than
``LOCK_HEARTBEAT_STALE_S``. A crashed drive therefore never blocks the next
start; a live, heartbeating one is never stolen from. Every write to the row is
a compare-and-set against the exact value last read (``compare_set_meta``), so
a refresh by a drive that was stolen from detects the loss instead of
clobbering the thief.

Three more rows carry the operator's controls:

* ``drive:paused`` — epics (or every epic) the drive must not tick. It is
  persisted, so a pause outlives the drive that saw it; ``resume`` lifts it.
* ``drive:stop`` — a stop request addressed to ONE drive by its token, for the
  whole drive or for named epics. Addressing it by token means a request left
  behind by a drive that already exited can never stop the next one.
* ``drive:hold`` — an operator hold on admission (#2616): every epic keeps
  ticking, so running work finishes and merges, but no tick admits new work.
  Like a pause it is persisted; ``release`` lifts it.

A fourth, ``drive:host-load``, is the drive's own: the host-load episode it
holds admission for (#2813, :mod:`kew.drive_host_load`), which it lifts by
itself once the host is calm.

Every mutation of a control row is a compare-and-set loop, so two operators
pausing two epics at once both land.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from kew.drive_heartbeat import (
    DRIVE_GONE,
    DRIVE_HOLDER_KEY,
    DriveHeartbeat,
    last_drive_heartbeat,
)
from kew.drive_host_load import (
    CALM_TICKS,
    DRIVE_HOST_LOAD_KEY,
    HostLoadHold,
    HostReading,
    host_load_hold_from,
    overload,
)
from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S
from kew.graph.driver.lock import LockHolder
from kew.progress import _warn

if TYPE_CHECKING:
    from kew.config import DispatchConfig

DRIVE_PAUSED_KEY = "drive:paused"
DRIVE_STOP_KEY = "drive:stop"
DRIVE_HOLD_KEY = "drive:hold"
DRIVE_EXIT_KEY = "drive:exit"

# A drive that starts over a reclaimed holder records ``drive_restarted``
# (#2810), naming the previous pid and which of these ended it.
DRIVE_RESTARTED_EVENT = "drive_restarted"
DRIVE_ENDED_REBOOT = "gone after reboot"
DRIVE_ENDED_EXITED = "exited"
DRIVE_ENDED_STALLED = "stalled"

# A compare-and-set that loses this many races in a row is reported, never
# retried forever: something is rewriting the row faster than we can read it.
_CAS_ATTEMPTS = 8


class DriveStore(Protocol):
    def get_meta(self, key: str) -> str | None: ...
    def compare_set_meta(self, key: str, expected: str | None, value: str) -> bool: ...


class DriveStateError(RuntimeError):
    """A drive control row could not be read or written as asked."""


def _now() -> datetime:
    return datetime.now(UTC)


def booted_at() -> datetime | None:
    """When this host last booted, or None where it cannot be read.

    Linux counts from ``CLOCK_BOOTTIME``; on macOS ``CLOCK_MONOTONIC`` is
    continuous time since boot, sleep included. Neither is a subprocess.
    """
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None and sys.platform == "darwin":
        clock = time.CLOCK_MONOTONIC
    if clock is None:
        return None
    return _now() - timedelta(seconds=time.clock_gettime(clock))


def _pid_alive(pid: int) -> bool:
    """The epic driver lock's own pid probe, so the two locks never disagree
    about whether a process lives."""
    return LockHolder(epic="drive", pid=pid, held_since=None).is_alive()


# The ``drive_restarted`` payload, in the order the console's excerpt shows it:
# previous_pid and reason first.
_RESTART_FIELDS = (
    "previous_pid",
    "reason",
    "pid",
    "host",
    "previous_host",
    "previous_started_at",
    "previous_heartbeat",
    "detail",
)


@dataclass(frozen=True)
class PendingRestart:
    """A takeover whose ``drive_restarted`` event is not yet known recorded
    (#2870). It rides in the taker's own lock row from the compare-and-set that
    took the lock until the event is appended, so a taker killed in between
    leaves it for the next drive to record. ``previous_token`` names the
    reclaimed drive, so however many drives record it, the ledger holds it
    once."""

    previous_token: str
    payload: dict[str, Any] = field(hash=False)

    @property
    def key(self) -> str:
        return f"{DRIVE_RESTARTED_EVENT}:{self.previous_token}"

    def event_payload(self) -> dict[str, Any]:
        return {k: self.payload[k] for k in _RESTART_FIELDS if k in self.payload}

    def encode(self) -> dict[str, Any]:
        return {"previous_token": self.previous_token, "payload": self.payload}

    @classmethod
    def parse(cls, data: Any) -> PendingRestart:
        if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
            raise TypeError(f"a pending restart is not a record: {data!r}")
        return cls(str(data["previous_token"]), dict(data["payload"]))


@dataclass(frozen=True)
class DriveHolder:
    """The drive that holds this ledger's lock."""

    pid: int
    host: str
    token: str
    started_at: datetime
    heartbeat: datetime
    # Takeovers this holder has not yet recorded, oldest first.
    restarts: tuple[PendingRestart, ...] = ()

    def encode(self) -> str:
        data: dict[str, Any] = {
            "pid": self.pid,
            "host": self.host,
            "token": self.token,
            "started_at": self.started_at.isoformat(),
            "heartbeat": self.heartbeat.isoformat(),
        }
        if self.restarts:
            data["restarts"] = [r.encode() for r in self.restarts]
        return json.dumps(data, sort_keys=True)

    @classmethod
    def parse(cls, raw: str) -> DriveHolder | str:
        """The holder ``raw`` records, or why it records none."""
        try:
            data = json.loads(raw)
            return cls(
                pid=int(data["pid"]),
                host=str(data["host"]),
                token=str(data["token"]),
                started_at=datetime.fromisoformat(data["started_at"]),
                heartbeat=datetime.fromisoformat(data["heartbeat"]),
                restarts=tuple(
                    PendingRestart.parse(r) for r in data.get("restarts", ())
                ),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            return f"an unreadable holder record ({type(exc).__name__}: {exc})"

    def restarted_by(
        self, taker: DriveHolder, detail: str, *, booted_at: datetime | None
    ) -> PendingRestart:
        """The restart ``taker`` records for reclaiming this holder."""
        return PendingRestart(
            self.token,
            {
                "previous_pid": self.pid,
                "reason": self.ended(booted_at=booted_at),
                "pid": taker.pid,
                "host": taker.host,
                "previous_host": self.host,
                "previous_started_at": self.started_at.isoformat(),
                "previous_heartbeat": self.heartbeat.isoformat(),
                "detail": detail,
            },
        )

    def stale_reason(
        self,
        *,
        now: datetime,
        stale_seconds: float = LOCK_HEARTBEAT_STALE_S,
        alive: Callable[[int], bool] = _pid_alive,
    ) -> str | None:
        """Why this holder is provably dead, or None while it may be live.

        A pid is only probed on its own host; elsewhere the heartbeat alone
        decides, so a drive on another machine sharing the ledger is never
        reclaimed for a pid this host happens not to have.
        """
        if self.host == socket.gethostname() and not alive(self.pid):
            return f"pid {self.pid} is gone"
        age = (now - self.heartbeat).total_seconds()
        if age > stale_seconds:
            return f"heartbeat {age:.0f}s old (stale after {stale_seconds:.0f}s)"
        return None

    def ended(
        self,
        *,
        booted_at: datetime | None,
        alive: Callable[[int], bool] = _pid_alive,
    ) -> str:
        """How a reclaimed holder ended: ``gone after reboot`` when this host
        booted after its last heartbeat (its pid may since have been reused),
        ``exited`` when its pid is gone without a reboot, else ``stalled`` —
        alive but silent, or on another host where only its heartbeat speaks.
        """
        if self.host == socket.gethostname():
            if booted_at is not None and self.heartbeat < booted_at:
                return DRIVE_ENDED_REBOOT
            if not alive(self.pid):
                return DRIVE_ENDED_EXITED
        return DRIVE_ENDED_STALLED

    def describe(self, *, now: datetime | None = None) -> str:
        age = ((now or _now()) - self.heartbeat).total_seconds()
        return (
            f"pid {self.pid} on {self.host}, running since "
            f"{self.started_at.isoformat(timespec='seconds')}, "
            f"heartbeat {age:.0f}s ago"
        )


@dataclass
class DriveLease:
    """This process's hold on the drive lock; ``raw`` is the row as written."""

    holder: DriveHolder
    raw: str
    reclaimed: str | None = None
    previous: DriveHolder | None = None
    # The lock row as the previous holder left it, to hand back.
    previous_raw: str | None = None


@dataclass(frozen=True)
class DriveRefusal:
    """Another live drive holds the lock (or the row could not be won)."""

    holder: DriveHolder | None
    detail: str


def _read_holder(store: DriveStore) -> tuple[str | None, DriveHolder | str | None]:
    """The raw row and what it records: a holder, why it is unreadable, or
    None when no drive holds it."""
    raw = store.get_meta(DRIVE_HOLDER_KEY)
    if not raw:
        return raw, None
    return raw, DriveHolder.parse(raw)


def current_holder(store: DriveStore) -> DriveHolder | None:
    """The recorded holder, live or stale; None when no drive holds the row
    or the row is unreadable (the next ``kew drive`` reclaims it, saying so)."""
    holder = _read_holder(store)[1]
    return holder if isinstance(holder, DriveHolder) else None


def live_holder(
    store: DriveStore, *, now: datetime | None = None
) -> DriveHolder | None:
    """The holder while it is not provably dead."""
    holder = current_holder(store)
    if holder is None or holder.stale_reason(now=now or _now()) is not None:
        return None
    return holder


def acquire_drive(
    store: DriveStore,
    *,
    pid: int | None = None,
    now: datetime | None = None,
    booted: datetime | None = None,
    resume: str | None = None,
) -> DriveLease | DriveRefusal:
    """Take the drive lock, reclaiming a provably-dead holder.

    A reclaim writes the restart it owes into the new lock row in the same
    compare-and-set (#2870), with any its dead holder still owed; ``booted``
    is when this host last booted, to tell a reboot apart. ``resume`` is the
    token of the lease this very process held before it re-exec'd itself on
    new code (#3197): that lease is taken back as it stands, token and all.
    """
    at = now or _now()
    base = DriveHolder(
        pid=os.getpid() if pid is None else pid,
        host=socket.gethostname(),
        token=secrets.token_hex(8),
        started_at=at,
        heartbeat=at,
    )
    holder: DriveHolder | str | None = None
    for _ in range(_CAS_ATTEMPTS):
        raw, holder = _read_holder(store)
        previous = holder if isinstance(holder, DriveHolder) else None
        taken = _take(holder, base, at, booted, resume)
        if isinstance(taken, DriveRefusal):
            return taken
        mine, reclaimed = taken
        raw_mine = mine.encode()
        if store.compare_set_meta(DRIVE_HOLDER_KEY, raw, raw_mine):
            return DriveLease(mine, raw_mine, reclaimed, previous, raw)
    last = holder if isinstance(holder, DriveHolder) else None
    return DriveRefusal(last, "the lock row kept changing while it was read")


def _take(
    holder: DriveHolder | str | None,
    base: DriveHolder,
    at: datetime,
    booted: datetime | None,
    resume: str | None,
) -> tuple[DriveHolder, str | None] | DriveRefusal:
    """The holder to write over ``holder`` and what it reclaimed, if anything:
    ``base`` over a free or unreadable row, this process's own lease back
    across a re-exec, else a reclaim of a provably-dead holder."""
    if not isinstance(holder, DriveHolder):
        return base, holder
    if _resumes(holder, base, resume):
        return replace(holder, heartbeat=at), None
    return _reclaim(holder, base, at, booted)


def _reclaim(
    previous: DriveHolder, base: DriveHolder, at: datetime, booted: datetime | None
) -> tuple[DriveHolder, str | None] | DriveRefusal:
    """The holder that reclaims ``previous`` and why it may, owing its
    restart; a refusal while ``previous`` may still be live."""
    stale = previous.stale_reason(now=at)
    if stale is None:
        return DriveRefusal(previous, f"held by {previous.describe(now=at)}")
    reclaimed = f"{previous.describe(now=at)}: {stale}"
    owed = previous.restarted_by(base, reclaimed, booted_at=booted)
    return replace(base, restarts=(*previous.restarts, owed)), reclaimed


def _resumes(previous: DriveHolder, base: DriveHolder, resume: str | None) -> bool:
    """Whether ``previous`` is this process's own lease from before its
    re-exec: the token it was handed, on this pid and host."""
    return (
        resume is not None
        and previous.token == resume
        and previous.pid == base.pid
        and previous.host == base.host
    )


def refresh_drive(
    store: DriveStore, lease: DriveLease, *, now: datetime | None = None
) -> bool:
    """Advance the lease's heartbeat; False when the lock is no longer ours."""
    holder = replace(lease.holder, heartbeat=now or _now())
    raw = holder.encode()
    if not store.compare_set_meta(DRIVE_HOLDER_KEY, lease.raw, raw):
        return False
    lease.holder, lease.raw = holder, raw
    return True


def settle_restarts(store: DriveStore, lease: DriveLease) -> bool:
    """Drop the restarts the lease's row owes once they are recorded; False
    when the lock is no longer ours (the thief records them again, once)."""
    if not lease.holder.restarts:
        return True
    holder = replace(lease.holder, restarts=())
    raw = holder.encode()
    if not store.compare_set_meta(DRIVE_HOLDER_KEY, lease.raw, raw):
        return False
    lease.holder, lease.raw = holder, raw
    return True


def release_drive(store: DriveStore, lease: DriveLease) -> bool:
    """Free the lock if it is still ours; False when someone else holds it."""
    return store.compare_set_meta(DRIVE_HOLDER_KEY, lease.raw, "")


def hand_back_drive(store: DriveStore, lease: DriveLease) -> bool:
    """Put back the lock row this lease reclaimed, if it is still ours, so the
    next drive reclaims it again; False when someone else holds it."""
    return store.compare_set_meta(DRIVE_HOLDER_KEY, lease.raw, lease.previous_raw or "")


def _update(store: DriveStore, key: str, change: Callable[[Any], Any]) -> Any:
    """Compare-and-set ``key``'s JSON value through ``change``."""
    for _ in range(_CAS_ATTEMPTS):
        raw = store.get_meta(key)
        current = _decode(key, raw)
        updated = change(current)
        if store.compare_set_meta(key, raw, json.dumps(updated, sort_keys=True)):
            return updated
    raise DriveStateError(f"{key} kept changing while it was updated")


def _decode(key: str, raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        # Never read an unreadable control row as "nothing paused": that would
        # silently resume every epic an operator paused.
        raise DriveStateError(f"{key} holds an unreadable value: {exc}") from exc


@dataclass(frozen=True)
class DrivePauses:
    """What the drive must not tick: everything, or the named epics."""

    all: bool = False
    epics: frozenset[str] = frozenset()

    def covers(self, epic: str) -> bool:
        return self.all or epic in self.epics


def _pauses_from(value: Any) -> DrivePauses:
    if value is None:
        return DrivePauses()
    if not isinstance(value, dict):
        raise DriveStateError(f"{DRIVE_PAUSED_KEY} is not an object: {value!r}")
    return DrivePauses(
        all=bool(value.get("all")),
        epics=frozenset(str(e) for e in value.get("epics") or ()),
    )


def _pauses_to(pauses: DrivePauses) -> dict[str, Any]:
    return {"all": pauses.all, "epics": sorted(pauses.epics)}


def drive_pauses(store: DriveStore) -> DrivePauses:
    return _pauses_from(_decode(DRIVE_PAUSED_KEY, store.get_meta(DRIVE_PAUSED_KEY)))


def pause(store: DriveStore, epic: str | None) -> DrivePauses:
    """Pause one epic, or (``None``) every epic."""

    def change(value: Any) -> dict[str, Any]:
        cur = _pauses_from(value)
        if epic is None:
            return _pauses_to(replace(cur, all=True))
        return _pauses_to(replace(cur, epics=cur.epics | {epic}))

    return _pauses_from(_update(store, DRIVE_PAUSED_KEY, change))


def resume(store: DriveStore, epic: str | None) -> DrivePauses:
    """Resume one epic, or (``None``) lift every pause."""

    def change(value: Any) -> dict[str, Any]:
        cur = _pauses_from(value)
        if epic is None:
            return _pauses_to(DrivePauses())
        return _pauses_to(replace(cur, epics=cur.epics - {epic}))

    return _pauses_from(_update(store, DRIVE_PAUSED_KEY, change))


@dataclass(frozen=True)
class DriveStop:
    """A stop addressed to the drive holding ``token``."""

    token: str
    all: bool = False
    epics: frozenset[str] = frozenset()


def _stop_from(value: Any) -> DriveStop | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("token"), str):
        raise DriveStateError(f"{DRIVE_STOP_KEY} is not a stop request: {value!r}")
    return DriveStop(
        token=value["token"],
        all=bool(value.get("all")),
        epics=frozenset(str(e) for e in value.get("epics") or ()),
    )


def request_stop(store: DriveStore, holder: DriveHolder, epic: str | None) -> DriveStop:
    """Ask ``holder``'s drive to stop (``None``) or to stop ticking ``epic``.

    A request left for an older drive is replaced, never merged: it addresses
    a drive that no longer runs.
    """

    def change(value: Any) -> dict[str, Any]:
        cur = _stop_from(value)
        if cur is None or cur.token != holder.token:
            cur = DriveStop(token=holder.token)
        epics = cur.epics if epic is None else cur.epics | {epic}
        return {
            "token": holder.token,
            "all": cur.all or epic is None,
            "epics": sorted(epics),
        }

    stop = _stop_from(_update(store, DRIVE_STOP_KEY, change))
    assert stop is not None
    return stop


def stop_for(store: DriveStore, lease: DriveLease) -> DriveStop:
    """The stop addressed to this drive; an empty one when none is."""
    stop = _stop_from(_decode(DRIVE_STOP_KEY, store.get_meta(DRIVE_STOP_KEY)))
    if stop is None or stop.token != lease.holder.token:
        return DriveStop(token=lease.holder.token)
    return stop


DRIVE_RUNNING = "running"
DRIVE_STALLED = "gone"
DRIVE_NOT_RUNNING = "not running"


@dataclass(frozen=True)
class DriveReading:
    """What ``kew drive status`` reports: the holder, its heartbeat, the word.

    ``state`` is ``running`` while a drive holds the lock and beats inside its
    stall line; ``gone`` when one holds it but has stalled (its heartbeat is
    older than ``DRIVE_STALL_INTERVALS`` of its write interval) or is provably
    dead; ``not running`` when no drive holds the lock. ``heartbeat`` is the
    last one written — the holder's own, or the last drive's when none holds it.
    """

    state: str
    holder: DriveHolder | None
    heartbeat: DriveHeartbeat | None
    reason: str | None = None
    #: Why the drive that wrote ``heartbeat`` ended, when it said (#3197).
    ended: DriveExit | None = None


@dataclass(frozen=True)
class DriveExit:
    """Why a drive ended on its own: the exit code it chose and the cause, so
    ``kew drive status`` names it instead of a stale heartbeat (#3197)."""

    token: str
    pid: int
    code: int
    reason: str
    at: datetime

    def encode(self) -> str:
        data = {
            "token": self.token,
            "pid": self.pid,
            "code": self.code,
            "reason": self.reason,
            "at": self.at.isoformat(),
        }
        return json.dumps(data, sort_keys=True)

    @classmethod
    def parse(cls, raw: str | None) -> DriveExit | None:
        """The recorded exit, or None when none is recorded; an unreadable
        record is warned about and reads as none."""
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return cls(
                token=str(data["token"]),
                pid=int(data["pid"]),
                code=int(data["code"]),
                reason=str(data["reason"]),
                at=datetime.fromisoformat(data["at"]),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _warn(f"kew drive: the recorded drive exit is unreadable ({exc!r}).")
            return None


def record_exit(store: Any, lease: DriveLease, code: int, reason: str) -> None:
    """Record why this drive ends with ``code``, for ``kew drive status``."""
    holder = lease.holder
    ended = DriveExit(holder.token, holder.pid, code, reason, _now())
    store.set_meta(DRIVE_EXIT_KEY, ended.encode())


def _ended(store: DriveStore, beat: DriveHeartbeat | None) -> DriveExit | None:
    """The exit the drive that wrote ``beat`` recorded, if it recorded one."""
    ended = DriveExit.parse(store.get_meta(DRIVE_EXIT_KEY))
    if ended is None or beat is None or ended.token != beat.token:
        return None
    return ended


def read_drive(store: DriveStore, *, now: datetime | None = None) -> DriveReading:
    at = now or _now()
    holder = current_holder(store)
    beat = last_drive_heartbeat(store)
    if holder is None:
        return DriveReading(DRIVE_NOT_RUNNING, None, beat, ended=_ended(store, beat))
    if beat is None or beat.token != holder.token:
        # A drive that never wrote a heartbeat (one predating it): its lock's
        # own heartbeat is the only evidence.
        stale = holder.stale_reason(now=at)
        return DriveReading(
            DRIVE_RUNNING if stale is None else DRIVE_STALLED, holder, None, stale
        )
    reason = holder.stale_reason(now=at)
    if reason is None and beat.status(at) == DRIVE_GONE:
        reason = (
            f"heartbeat {beat.age_s(at):.0f}s old "
            f"(stalled after {beat.stall_after_s:.0f}s)"
        )
    if reason is None:
        return DriveReading(DRIVE_RUNNING, holder, beat)
    return DriveReading(DRIVE_STALLED, holder, beat, reason, _ended(store, beat))


# --------------------------------------------------------------------------- #
# Admission (#2616)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DriveHold:
    """An operator hold: the drive admits nothing new while it stands."""

    since: datetime


def _hold_from(value: Any) -> DriveHold | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("since"), str):
        raise DriveStateError(f"{DRIVE_HOLD_KEY} is not a hold: {value!r}")
    try:
        return DriveHold(since=datetime.fromisoformat(value["since"]))
    except ValueError as exc:
        raise DriveStateError(
            f"{DRIVE_HOLD_KEY} holds an unreadable time: {exc}"
        ) from exc


def drive_hold(store: DriveStore) -> DriveHold | None:
    """The standing hold; an unreadable row is an error, never "not held"."""
    return _hold_from(_decode(DRIVE_HOLD_KEY, store.get_meta(DRIVE_HOLD_KEY)))


def hold(store: DriveStore, *, now: datetime | None = None) -> DriveHold:
    """Hold admission; a hold already standing keeps the time it started."""
    at = (now or _now()).isoformat()

    def change(value: Any) -> dict[str, Any]:
        cur = _hold_from(value)
        return {"since": cur.since.isoformat() if cur else at}

    held = _hold_from(_update(store, DRIVE_HOLD_KEY, change))
    assert held is not None
    return held


def release(store: DriveStore) -> DriveHold | None:
    """Lift the hold; returns the hold that stood, if one did."""
    lifted: list[DriveHold | None] = [None]

    def change(value: Any) -> dict[str, Any]:
        lifted[0] = _hold_from(value)
        return {}

    _update(store, DRIVE_HOLD_KEY, change)
    return lifted[0]


def read_load() -> float | str:
    """This machine's one-minute load average, or why it cannot be read."""
    try:
        return os.getloadavg()[0]
    except OSError as exc:
        return f"load average unreadable ({exc})"


class AdmissionStore(DriveStore, Protocol):
    def live_run_count(self) -> int: ...


@dataclass(frozen=True)
class Admission:
    """What one tick may admit: up to ``cap`` runs in flight on every host.

    ``cap`` is 0 while held (or while a load threshold says 0): the tick
    admits nothing new and its running work finishes. ``live`` is the ledger's
    in-flight count when this was read — dispatches, reviews and remediations
    alike, local or placed on a remote host — for the drive's log line; the
    tick re-counts it at its own atomic admission.
    """

    cap: int
    live: int
    reason: str

    @property
    def room(self) -> int:
        return max(0, self.cap - self.live)

    def describe(self) -> str:
        return f"cap {self.cap} ({self.reason}), {self.live} in flight"


def load_cap(config: DispatchConfig, load: float | str) -> tuple[int, str]:
    """``[drive] max_parallel`` lowered by the fleet cap and by every
    ``[drive.load]`` threshold the load is above, and why.

    The drive never raises the fleet cap (#2398): every run the tick starts
    re-checks admission against it in its own process, so a drive cap above it
    would only reserve work that is then refused. An unreadable load takes the
    lowest threshold's cap: a gate that cannot see the load never reads it as
    idle.
    """
    cap = config.drive.max_parallel
    reason = f"[drive] max_parallel {cap}"
    fleet = config.fleet_concurrency()
    if fleet < cap:
        cap, reason = fleet, f"fleet cap {fleet}"
    steps = config.drive.load.thresholds
    if isinstance(load, str):
        lowest = min((s.max_parallel for s in steps), default=cap)
        if lowest < cap:
            return lowest, f"{load}: lowest load threshold"
        return cap, reason
    for step in steps:
        if load > step.above and step.max_parallel < cap:
            cap = step.max_parallel
            reason = f"load {load:.1f} above {step.above:g}"
    return cap, reason


def admission(
    store: AdmissionStore,
    config: DispatchConfig,
    load: float | str,
    host: HostLoadHold | None = None,
) -> Admission:
    """The cap the drive's next tick admits under, read fresh; ``host`` is
    the standing host-load episode, which admits nothing (#2813)."""
    live = store.live_run_count()
    held = drive_hold(store)
    if held is not None:
        since = held.since.isoformat(timespec="seconds")
        return Admission(0, live, f"held since {since}; `kew drive release`")
    if host is not None:
        return Admission(0, live, host.describe())
    cap, reason = load_cap(config, load)
    return Admission(cap, live, reason)


#: Reads the host; ``swap`` says whether to read its swap too.
HostReader = Callable[..., HostReading | str]


@dataclass(frozen=True)
class HostGate:
    """The host-load episode after one read, and whether this read began or
    ended it — each seen by exactly one drive, the one whose write won."""

    hold: HostLoadHold | None
    began: bool = False
    resumed: bool = False


def clear_host_load(store: DriveStore) -> None:
    """End any host-load episode: a stopping drive's hold is not the next
    one's, which reads the host afresh before its first tick."""
    for _ in range(_CAS_ATTEMPTS):
        raw = store.get_meta(DRIVE_HOST_LOAD_KEY)
        if not raw or store.compare_set_meta(DRIVE_HOST_LOAD_KEY, raw, "{}"):
            return
    raise DriveStateError(f"{DRIVE_HOST_LOAD_KEY} kept changing while it was cleared")


def _episode_from(value: Any) -> HostLoadHold | None:
    try:
        return host_load_hold_from(value)
    except ValueError as exc:
        # Never read an unreadable episode as a calm host.
        raise DriveStateError(str(exc)) from exc


def host_load_episode(store: DriveStore) -> HostLoadHold | None:
    """The standing host-load episode; an unreadable row is an error, never
    "no episode"."""
    raw = store.get_meta(DRIVE_HOST_LOAD_KEY)
    return _episode_from(_decode(DRIVE_HOST_LOAD_KEY, raw))


def host_gate(
    store: DriveStore,
    config: DispatchConfig,
    read: HostReader,
    *,
    tick: bool = True,
    now: datetime | None = None,
) -> HostGate:
    """Read the host and move the ``drive:host-load`` episode (#2813).

    Above a threshold the episode stands (keeping the time it began) and its
    calm count restarts; at or below both, each read counts one calm read, and
    the ``CALM_TICKS``-th ends it. With neither threshold set the host is not
    read, and an episode left by an earlier config is cleared.

    Only the drive's tick cadence counts calm reads. A probe (``tick=False``,
    the request runner's read before a spend) may open or extend an episode,
    but a calm probe leaves it as it stands: request polling every few seconds
    must never stand in for the ticks that end one.
    """
    drive = config.drive
    if not drive.host_gated:
        clear_host_load(store)
        return HostGate(None)
    raw = store.get_meta(DRIVE_HOST_LOAD_KEY)
    over = overload(
        read(swap=drive.max_swap_used_pct is not None),
        max_load_per_cpu=drive.max_load_per_cpu,
        max_swap_used_pct=drive.max_swap_used_pct,
    )
    if over is None:
        standing = _episode_from(_decode(DRIVE_HOST_LOAD_KEY, raw))
        if standing is None or not tick:
            return HostGate(standing)
    at = now or _now()
    before: list[HostLoadHold | None] = [None]

    def change(value: Any) -> dict[str, Any]:
        cur = before[0] = _episode_from(value)
        if over is not None:
            return HostLoadHold(cur.since if cur else at, over).encode()
        if cur is None or cur.calm + 1 >= CALM_TICKS:
            return {}
        return replace(cur, calm=cur.calm + 1).encode()

    after = _episode_from(_update(store, DRIVE_HOST_LOAD_KEY, change))
    return HostGate(
        after,
        began=before[0] is None and after is not None,
        resumed=before[0] is not None and after is None,
    )
