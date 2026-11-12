"""Per-epic single-driver advisory lock with holder transparency (#861/F2, #970).

The lock is keyed per epic by its *path* (see ``cli._epic_lock_path``), so two
epics can tick concurrently. The holder metadata recorded in the lock file is
a small JSON record — ``epic``, ``pid``, ``held_since``, and ``heartbeat`` —
so ``kew epic status`` and tick refusals can name exactly who holds an epic's
driver lock and since when. A holder whose ``pid`` is no longer alive, or whose
``heartbeat`` is older than ``HEARTBEAT_STALE_SECONDS``, is *stale*.

The record also carries ``started``, the holder process's kernel start token
(#2227, ``kew.process_groups.process_start_token``: ``proc_pidinfo`` on Darwin,
``/proc`` on Linux — microsecond or tick resolution, independent of locale and
time zone, no subprocess). A holder whose pid is alive with that same token is
the very process that took the lock, so it is never stolen however old its
heartbeat — a hung but live driver is released by killing it, after which its
dead pid is stolen. Such a holder whose heartbeat went stale is *stalled*
(:func:`stalled_holder`): the tick that finds it reports the epic STALLED and
the drive raises an attention event naming the pid to kill (#3331), so a
stopped driver is never a silent forever-observer. Any other live holder — a
different or unreadable token, or a record without ``started`` (an older kew)
— keeps the heartbeat rule: a token only ever protects a holder, it never
licenses a steal, so a live holder with a fresh heartbeat is never stolen.

A *stale* lock is stolen **automatically** by any acquiring driver (logging the
steal): the #970 symptom is a TaskStop'd driver leaving its lock behind so every
later driver silently observes a dead epic forever. Automatic steal is safe
precisely because staleness is only ever a dead PID or an expired heartbeat — a
live, freshly-ticking driver is never stolen from. ``--takeover`` is a narrower,
operator-only escape hatch for *indeterminate* locks (legacy/pre-heartbeat
records whose staleness cannot be proven, e.g. a recycled PID); it still never
stomps a live holder with a fresh heartbeat. Entering observer mode always logs
the current holder identity and heartbeat age (no more silent observers).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S
from kew.process_groups import process_start_token

_logger = logging.getLogger(__name__)

# A heartbeat older than this marks the holder stale even if its PID is alive
# (a frozen/hung or suspended driver) — but only for a record without a start
# time; a holder proven to be the same live process is never stolen (#2227).
# While a driver holds the lock a background refresher beats every
# ``HEARTBEAT_REFRESH_SECONDS`` (#2650), so a tick that legitimately
# dispatches for 5-15 minutes stays fresh; only a holder whose process stops
# beating (suspend/resume, SIGSTOP) goes stale. A thief's
# steal and the victim's own ownership-checked heartbeat refresh
# (``refresh_lock_heartbeat``, which refuses to clobber a foreign record) bound
# the damage of such a steal to a single overlapping tick rather than corrupting
# the lock's identity or staleness clock.
HEARTBEAT_STALE_SECONDS: int = LOCK_HEARTBEAT_STALE_S

# How often a held lock's heartbeat is refreshed by its background refresher.
# Must stay well under ``HEARTBEAT_STALE_SECONDS`` (5x margin at the defaults).
HEARTBEAT_REFRESH_SECONDS: float = 60.0

# Serializes heartbeat writes within this process: the refresher thread and a
# tick's own end-of-tick refresh both rewrite the lock file in place, and two
# interleaved write-then-truncate passes could leave a torn record.
_refresh_mutex = threading.Lock()


@dataclass(frozen=True)
class LockHolder:
    """Who holds an epic's driver lock: the epic key, the holding process pid,
    when the lock was acquired (``held_since``), and the last heartbeat
    timestamp (``heartbeat``) refreshed each tick."""

    epic: str
    pid: int
    held_since: datetime | None
    heartbeat: datetime | None = field(default=None)
    #: The holder's kernel start token, or None in an older kew's record.
    started: str | None = field(default=None)

    def is_alive(self) -> bool:
        """True iff the recorded pid is a live process on this host.

        A missing/legacy pid (``<= 0``) reads as alive — fail toward "still
        held" so an unknown holder is never mistaken for stale and reclaimed.
        """
        if self.pid <= 0:
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The pid exists but is owned by another user — still alive.
            return True
        return True

    def is_heartbeat_stale(
        self, *, now: datetime, stale_seconds: int = HEARTBEAT_STALE_SECONDS
    ) -> bool:
        """True iff the heartbeat is older than ``stale_seconds``.

        Fails toward not-stale when the heartbeat is missing or naive (no
        timezone info) — an unknown heartbeat is never treated as evidence of
        a dead driver.
        """
        if self.heartbeat is None or self.heartbeat.tzinfo is None:
            return False
        return (now - self.heartbeat).total_seconds() > stale_seconds

    def describe(self) -> str:
        """One-line ``epic=… pid=… held-since=… heartbeat=…`` for status lines."""
        since = self.held_since.isoformat() if self.held_since else "unknown"
        hb = self.heartbeat.isoformat() if self.heartbeat else "unknown"
        return f"epic={self.epic} pid={self.pid} held-since={since} heartbeat={hb}"

    def is_same_live_process(self) -> bool | None:
        """Whether the recorded pid is still the process that took the lock.

        None when the record names no start time (an older kew) or the current
        start time cannot be read — no evidence either way.
        """
        if self.started is None or self.pid <= 0:
            return None
        current = process_start_token(self.pid)
        if current is None:
            return None
        return current == self.started

    def is_stalled(self, *, now: datetime) -> bool:
        """True iff this is the live process that took the lock and its
        heartbeat went stale (#3331): stopped (SIGSTOP, Ctrl-Z), suspended or
        hung. It is never stolen, so only killing it releases the epic."""
        return (
            self.is_heartbeat_stale(now=now, stale_seconds=HEARTBEAT_STALE_SECONDS)
            and self.is_alive()
            and self.is_same_live_process() is True
        )


def stalled_holder(
    lock_path: Path, *, now: datetime | None = None
) -> LockHolder | None:
    """The holder of ``lock_path`` when it is stalled (see
    :meth:`LockHolder.is_stalled`), else None — a free lock, a dead holder
    (the next acquirer steals it) or a live, beating one."""
    holder = lock_holder(lock_path)
    if holder is None or not holder.is_stalled(now=now or datetime.now(UTC)):
        return None
    return holder


_own_start: dict[int, str] = {}


def _own_start_token() -> str | None:
    """This process's kernel start token, read once per pid (a fork re-reads
    it). A read that fails is not remembered: the next record tries again."""
    pid = os.getpid()
    if pid not in _own_start:
        token = process_start_token(pid)
        if token is None:
            return None
        _own_start[pid] = token
    return _own_start[pid]


def _encode_holder(
    epic: str | None, *, now: datetime, held_since: datetime | None = None
) -> str:
    """Encode holder metadata. ``held_since`` defaults to ``now`` at first
    acquisition; pass the original value when refreshing the heartbeat so
    the acquisition timestamp is preserved across ticks."""
    record: dict[str, object] = {
        "epic": epic if epic is not None else "unknown",
        "pid": os.getpid(),
        "held_since": (held_since or now).isoformat(),
        "heartbeat": now.isoformat(),
    }
    started = _own_start_token()
    if started is not None:
        record["started"] = started
    return json.dumps(record)


def _decode_started(value: object) -> str | None:
    """A kernel start token; anything else reads as no start time, which keeps
    the record on the heartbeat rule."""
    if isinstance(value, str) and value:
        return value
    return None


#: Above any pid a kernel hands out (``pid_t`` is 32-bit signed): a larger
#: value cannot name a process, and ``os.kill`` overflows on it.
_PID_LIMIT = 2**31


def _decode_pid(value: object) -> int:
    """A recorded pid, or 0 ("unknown", which reads as held) for anything that
    cannot name a process: a non-int, a bool, zero, negative or out of range."""
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return value if 0 < value < _PID_LIMIT else 0


def _decode_holder(raw: str) -> LockHolder | None:
    """Parse a lock file's recorded holder. Tolerates the pre-F2 bare-string
    holder tag (the whole content was the epic name) and any malformed content
    by degrading to a pid-less holder rather than crashing a status read."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return LockHolder(epic=raw, pid=0, held_since=None)
    if not isinstance(data, dict):
        return LockHolder(epic=raw, pid=0, held_since=None)

    def _parse_dt(key: str) -> datetime | None:
        val = data.get(key)
        if not isinstance(val, str):
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    return LockHolder(
        epic=str(data.get("epic", "unknown")),
        pid=_decode_pid(data.get("pid")),
        held_since=_parse_dt("held_since"),
        heartbeat=_parse_dt("heartbeat"),
        started=_decode_started(data.get("started")),
    )


def _stamp(fd: TextIO, holder: str | None) -> None:
    """Record the current process's holder metadata into the (flock-held) file.

    Safe to truncate: this only runs AFTER the exclusive flock is acquired, so
    no losing acquirer can clobber the winner's tag.
    """
    fd.seek(0)
    fd.truncate()
    fd.write(_encode_holder(holder, now=datetime.now(UTC)))
    fd.flush()


def _reclaim_reason(holder: LockHolder, *, now: datetime, takeover: bool) -> str | None:
    """Return why ``holder``'s lock may be reclaimed, or None to leave it held.

    A *stale* holder is always reclaimable (steal is automatic — see #970):
    its PID is dead, or its heartbeat exceeds ``HEARTBEAT_STALE_SECONDS``.
    Under an explicit ``takeover`` an *indeterminate* holder is also reclaimable
    — a legacy/pre-heartbeat record with no heartbeat, whose staleness cannot be
    proven (e.g. a recycled PID reading alive). A live holder with a *fresh*
    heartbeat is never reclaimable, takeover or not — never stomp a running
    driver. Fails toward "held" for unknown/live state.

    A live pid with the recorded start token (#2227) is never reclaimable,
    stale heartbeat and ``takeover`` notwithstanding. A token that differs or
    cannot be read proves nothing on its own — a recycled pid, or a misread —
    so it falls through to the heartbeat rule, like a record without one: a
    live holder with a fresh heartbeat is still never stolen.
    """
    if not holder.is_alive():
        return "dead pid"
    if holder.is_same_live_process():
        return None
    if holder.is_heartbeat_stale(now=now, stale_seconds=HEARTBEAT_STALE_SECONDS):
        return f"stale heartbeat (>{HEARTBEAT_STALE_SECONDS}s)"
    if takeover and holder.heartbeat is None:
        return "no heartbeat (manual --takeover)"
    return None


def _steal_lock_path(lock_path: Path) -> Path:
    """Sidecar meta-lock serializing steals of ``lock_path``.

    Persistent (never unlinked) so it survives the unlink-and-recreate of the
    main lock during a steal. Held EXCLUSIVE around the whole read-decide-
    unlink-reacquire sequence so two acquirers can't both break the same lock
    (the check-then-unlink TOCTOU: without it, both read the same stale holder,
    both unlink, and both flock distinct fresh inodes → two live drivers)."""
    return lock_path.with_name(lock_path.name + ".steal")


def _try_flock(lock_path: Path) -> TextIO | None:
    """Non-blocking ``LOCK_EX`` attempt. Returns the open fd on success (held for
    the caller's context lifetime), else None with the fd closed."""
    fd = open(lock_path, "a+")  # noqa: SIM115 — held for the context lifetime
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


def _steal(lock_path: Path, *, takeover: bool) -> TextIO | None:
    """Break a reclaimable lock under the serialized steal meta-lock and
    re-acquire it on a fresh inode. Returns the open fd, or None if — after
    serialization — the lock is no longer reclaimable (a concurrent steal
    already installed a fresh holder, or the holder released it cleanly)."""
    steal_fd = open(_steal_lock_path(lock_path), "a+")  # noqa: SIM115
    try:
        # Blocking: steals are rare and the critical section is short. Only a
        # would-be stealer ever contends here, never the common acquire path.
        fcntl.flock(steal_fd.fileno(), fcntl.LOCK_EX)
        # Re-evaluate under the meta-lock: the holder may have released, or a
        # concurrent stealer may already own a fresh lock.
        fd = _try_flock(lock_path)
        if fd is not None:
            return fd
        holder = lock_holder(lock_path)
        if holder is None:
            return None
        reason = _reclaim_reason(holder, now=datetime.now(UTC), takeover=takeover)
        if reason is None:
            return None
        _logger.warning(
            "epic-driver.lock: stealing stale lock from %s (reason: %s)",
            holder.describe(),
            reason,
        )
        lock_path.unlink(missing_ok=True)
        return _try_flock(lock_path)
    finally:
        fcntl.flock(steal_fd.fileno(), fcntl.LOCK_UN)
        steal_fd.close()


def _acquire(lock_path: Path, *, takeover: bool) -> TextIO | None:
    """Try to flock ``lock_path`` non-blocking. Returns the open fd on success,
    else None. When the lock is held by a *stale* holder (dead pid OR heartbeat
    older than ``HEARTBEAT_STALE_SECONDS``) the stale lock is broken and
    re-acquired **automatically** — no ``takeover`` required (#970). ``takeover``
    additionally reclaims an *indeterminate* lock (a legacy record with no
    heartbeat). A live holder with a fresh heartbeat is never taken over. Steals
    are serialized and logged at WARNING."""
    fd = _try_flock(lock_path)
    if fd is not None:
        return fd
    # Held elsewhere. Optimistically pre-check reclaimability so ordinary
    # observers never touch the steal meta-lock; the authoritative re-check
    # happens inside ``_steal`` under serialization.
    holder = lock_holder(lock_path)
    if holder is None:
        return None
    if _reclaim_reason(holder, now=datetime.now(UTC), takeover=takeover) is None:
        return None
    return _steal(lock_path, takeover=takeover)


def _log_observer(lock_path: Path) -> None:
    """Log the current holder identity and heartbeat freshness when entering
    observer mode. Replaces the previous silent observer behaviour."""
    holder = lock_holder(lock_path)
    if holder is None:
        _logger.warning("epic-driver.lock: yielding observer mode (holder unknown)")
        return
    now = datetime.now(UTC)
    if holder.heartbeat and holder.heartbeat.tzinfo:
        age_s = (now - holder.heartbeat).total_seconds()
        age_str = f"{age_s:.0f}s ago"
    else:
        age_str = "unknown"
    _logger.warning(
        "epic-driver.lock: yielding observer mode — holder %s, heartbeat %s",
        holder.describe(),
        age_str,
    )
    if holder.is_stalled(now=now):
        _logger.warning(
            "epic-driver.lock: holder pid %s is alive and is the process that "
            "took the lock, so its stale heartbeat does not release it; if that "
            "driver is hung, kill pid %s and the next start steals the lock",
            holder.pid,
            holder.pid,
        )


def _start_heartbeat_refresher(
    lock_path: Path, holder: str | None
) -> tuple[threading.Thread, threading.Event]:
    """Beat ``lock_path``'s heartbeat every ``HEARTBEAT_REFRESH_SECONDS`` from a
    daemon thread until the returned event is set (#2650).

    Without it the heartbeat moved only at acquisition and tick end, so any tick
    that dispatched for longer than ``HEARTBEAT_STALE_SECONDS`` read as stale and
    an operator command (``retry``, ``hold``, a second ``advance``) stole the lock
    from a live driver. The refresh is ownership-checked; once it reports the
    lock is no longer ours the refresher stops rather than warn every interval.
    """
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(HEARTBEAT_REFRESH_SECONDS):
            if not refresh_lock_heartbeat(lock_path, holder):
                return

    thread = threading.Thread(
        target=_run, name=f"epic-driver-heartbeat:{holder}", daemon=True
    )
    thread.start()
    return thread, stop


@contextmanager
def single_driver_lock(
    lock_path: Path, holder: str | None = None, *, takeover: bool = False
) -> Iterator[bool]:
    """Non-blocking per-epic advisory lock. Yields True if acquired, False if
    held by a live driver elsewhere.

    Uses ``fcntl.flock`` with ``LOCK_EX | LOCK_NB`` so the call never blocks.
    The lock is held only for the lifetime of the ``with`` block and released
    (fd closed) on exit. When ``holder`` is given, its epic identity — plus the
    process pid and an acquire timestamp — is recorded into the lock file AFTER
    the flock is acquired, so ``lock_holder()`` (and ``kew epic status``) can
    name who holds the driver and since when. While held, a daemon thread
    refreshes the heartbeat every ``HEARTBEAT_REFRESH_SECONDS`` (#2650), so a
    long-running tick is never mistaken for a hung one.

    A *stale* lock (dead pid OR heartbeat older than ``HEARTBEAT_STALE_SECONDS``)
    is reclaimed **automatically** — the acquiring driver steals it and logs the
    steal (#970). ``takeover=True`` additionally reclaims an *indeterminate* lock
    (a legacy record with no heartbeat whose staleness can't be proven). A live
    holder with a fresh heartbeat is refused regardless (never stomp a running
    driver).

    Args:
        lock_path: Path to this epic's lock file.
        holder: Optional epic identity to stamp into the lock file once acquired.
        takeover: Also reclaim an indeterminate (no-heartbeat) lock, not just a
            provably-stale one.

    Yields:
        True if this process acquired the lock, False otherwise.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire(lock_path, takeover=takeover)
    if fd is None:
        _log_observer(lock_path)
        yield False
        return
    refresher: tuple[threading.Thread, threading.Event] | None = None
    try:
        _stamp(fd, holder)
        refresher = _start_heartbeat_refresher(lock_path, holder)
        yield True
    finally:
        if refresher is not None:
            # Stop and join BEFORE unlocking, so no beat lands after release.
            thread, stop = refresher
            stop.set()
            thread.join()
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def lock_holder(lock_path: Path) -> LockHolder | None:
    """Return the ``LockHolder`` currently holding ``lock_path``, or None if free.

    Read-only: attempts a non-blocking flock to decide whether anyone holds the
    lock. If the flock is DENIED the lock is held elsewhere, and the file's
    recorded holder is decoded and returned (a legacy bare tag / missing pid
    degrades gracefully). If the flock succeeds nobody holds it, so it is
    released immediately and None is returned. A missing lock file is also None.
    """
    if not lock_path.exists():
        return None
    try:
        # Undecodable bytes are replaced, not raised: a damaged file reads as
        # a pid-less holder (held, never stolen on it), like any malformed one.
        fd = open(lock_path, errors="replace")  # noqa: SIM115 — closed below
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _decode_holder(fd.read())
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        fd.close()


def refresh_lock_heartbeat(lock_path: Path, epic: str | None = None) -> bool:
    """Refresh the heartbeat timestamp in a lock file **this process owns**.

    Call once per completed tick (inside the ``single_driver_lock`` context)
    so the lock file reflects when the driver last successfully ticked.
    A driver acquiring the lock can then detect a stale heartbeat (past
    ``HEARTBEAT_STALE_SECONDS``) even when the holder PID is still alive
    (frozen / hung driver). The original ``held_since`` is preserved.

    Returns True if the heartbeat was refreshed, False if the lock was **not**
    owned by this process — the on-disk record's pid is not ours because a
    stealer reclaimed the lock while we ran a long tick (e.g. suspend/resume).
    In that case we log loudly and refuse to write: overwriting the thief's
    record would resurrect the stolen lock under our identity and reset the very
    staleness clock the steal relied on, producing two live drivers (#970). The
    ownership check is what makes automatic heartbeat-stale steals safe.

    Writes in place (same inode) — the current process holds the flock on that
    inode via ``single_driver_lock``; ``os.replace`` would swap in a new inode
    and orphan our own flock. Writes the new payload before truncating so a
    concurrent reader never sees an empty file. No-ops (returns False) on any
    ``OSError`` (e.g. the lock file was deleted by a concurrent takeover).
    """
    if not lock_path.exists():
        return False
    now = datetime.now(UTC)
    try:
        with _refresh_mutex:
            with open(lock_path) as rfd:
                existing = _decode_holder(rfd.read())
            if existing is None or existing.pid != os.getpid():
                _logger.warning(
                    "epic-driver.lock: heartbeat refresh skipped — lock now held "
                    "by %s, not this driver (pid=%s); another driver stole it",
                    existing.describe() if existing is not None else "unknown",
                    os.getpid(),
                )
                return False
            payload = _encode_holder(epic, now=now, held_since=existing.held_since)
            with open(lock_path, "r+") as wfd:
                wfd.write(payload)
                wfd.truncate()
                wfd.flush()
            return True
    except OSError:
        return False
