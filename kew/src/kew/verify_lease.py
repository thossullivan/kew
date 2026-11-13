"""The local merge verify's lease, and the launches that yield to it (#3190).

Every ``kew verify --host local`` holds an exclusive ``flock`` on
``<work_root>/verify.lease`` for the life of its job, and writes a small record
beside it: pid, start token, ref and ``started_at``. The kernel drops the lock
when the process dies, so a lease never goes stale — a record a killed verify
left behind is never read, because nothing holds the lock it describes. A
second local verify never waits for the first: it is refused at once
(:class:`VerifyLeaseBusy`), naming the verify that holds the lease.

:func:`verify_running` probes the lock with a SHARED non-blocking lock, so two
probes never read each other as a running verify. :func:`verify_deferral` is
the spawn guard every drive launch passes — a review stage, a reviewer, a
remediation, a merge-main step, a dispatch, a continuation, a ci-fix, a
console run request: while a verify holds the lease it admits launches for
``[drive] launches_during_verify`` nodes during that verify, then defers the
rest, recording one ``launch_deferred`` event per node per verify. Running
work is never stopped, and the first launch after the lease is released goes
as it always would.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import work_dirs

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore

logger = logging.getLogger(__name__)

#: The audit event a launch deferred for a running verify records.
LAUNCH_DEFERRED_EVENT = "launch_deferred"

#: The ledger meta row naming the nodes admitted during one verify.
VERIFY_LAUNCHES_KEY = "drive:verify-launches"

_CAS_ATTEMPTS = 20

#: How long taking the lease rides out a conflicting lock before it refuses.
#: A probe holds its shared lock for an instant, so only a verify holds it this
#: long; a second verify is refused, never queued behind the first.
_ACQUIRE_PATIENCE_S = 0.5
_ACQUIRE_PAUSE_S = 0.01


@dataclass(frozen=True)
class VerifyLease:
    """A running local verify, as its record describes it.

    ``token`` is ``None`` when the lock is held but its record cannot be read
    yet (the verify is between taking the lock and writing it).
    """

    pid: int | None
    token: str | None
    ref: str
    started_at: str

    def describe(self) -> str:
        return f"{self.ref}, since {self.started_at}"

    def deferral(self) -> str:
        return f"deferred: verify running ({self.describe()})"


class VerifyLeaseBusy(RuntimeError):
    """Another local verify holds the lease: one local verify at a time."""

    def __init__(self, holder: VerifyLease) -> None:
        pid = "unknown" if holder.pid is None else str(holder.pid)
        super().__init__(
            f"a local merge verify is already running (pid {pid}, "
            f"{holder.describe()}): one local verify at a time, run this one "
            "again once it ends"
        )
        self.holder = holder


#: Reads whether a local verify holds the lease.
VerifyProbe = Callable[[], "VerifyLease | None"]


def _record_path(lease: Path) -> Path:
    return lease.with_name(lease.name + ".json")


def _write_record(lease: Path, record: dict[str, Any]) -> None:
    target = _record_path(lease)
    partial = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(partial, target)


def _read_record(lease: Path) -> VerifyLease:
    try:
        raw = json.loads(_record_path(lease).read_text(encoding="utf-8"))
        return VerifyLease(
            pid=int(raw["pid"]),
            token=str(raw["token"]),
            ref=str(raw["ref"]),
            started_at=str(raw["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return VerifyLease(pid=None, token=None, ref="unknown ref", started_at="?")


def _take_lock(descriptor: int, lease: Path) -> None:
    """Take the lease's exclusive lock, or raise :class:`VerifyLeaseBusy`.

    Never a blocking wait: the drive runs its merge verifies in its own
    process, so a verify queued behind a stuck one would freeze the drive with
    it. A conflict is ridden out for :data:`_ACQUIRE_PATIENCE_S` only, because
    a :func:`verify_running` probe holds a shared lock for an instant and must
    not refuse a verify that starts beside it.
    """
    deadline = time.monotonic() + _ACQUIRE_PATIENCE_S
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise VerifyLeaseBusy(_read_record(lease)) from None
        time.sleep(_ACQUIRE_PAUSE_S)


@contextlib.contextmanager
def hold_verify_lease(ref: str, *, path: Path | None = None) -> Iterator[VerifyLease]:
    """Hold the verify lease for the life of the block.

    A second local verify is refused with :class:`VerifyLeaseBusy` rather than
    queued: the lease is exclusive, and the machine it promises is a quiet one.
    """
    lease = work_dirs.verify_lease_path() if path is None else path
    descriptor = os.open(lease, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _take_lock(descriptor, lease)
        held = VerifyLease(
            pid=os.getpid(),
            token=uuid.uuid4().hex,
            ref=ref,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        _write_record(
            lease,
            {
                "pid": held.pid,
                "token": held.token,
                "ref": held.ref,
                "started_at": held.started_at,
            },
        )
        try:
            yield held
        finally:
            with contextlib.suppress(OSError):
                _record_path(lease).unlink()
    finally:
        os.close(descriptor)


def verify_running(path: Path | None = None) -> VerifyLease | None:
    """The local verify holding the lease, or ``None`` when it is free.

    Reading never creates the work root: no root, no verify. The probe takes a
    SHARED lock, released at once (as :func:`kew.hosts.lock.host_lock_held`
    does), so it conflicts only with the verify's exclusive one: two probes at
    the same instant never read each other as a running verify.
    """
    lease = (
        work_dirs.configured_root() / work_dirs.VERIFY_LEASE if path is None else path
    )
    if not lease.exists():  # no local verify has run here; kew never unlinks it
        return None
    descriptor = os.open(lease, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_record(lease)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return None
    finally:
        os.close(descriptor)


def _admitted(raw: str | None, token: str) -> list[str]:
    """The nodes the meta row says were admitted during the verify *token*."""
    try:
        current = json.loads(raw) if raw else {}
    except ValueError:
        logger.warning(
            "unreadable %s row %r: counting this verify's launches afresh",
            VERIFY_LAUNCHES_KEY,
            raw,
        )
        return []
    if not isinstance(current, dict) or current.get("token") != token:
        return []
    items = current.get("items")
    return [str(item) for item in items] if isinstance(items, list) else []


def _take_launch(store: RunStore, token: str | None, limit: int, item_id: str) -> bool:
    """Admit *item_id* as one of the *limit* nodes the verify *token* lets launch.

    A node admitted once is not charged again during the same verify: a
    detached review child reads the lease again at its own spawn boundary, and
    the tick already spent the node's admission when it started the child.
    """
    if limit <= 0 or token is None:
        return False
    for _ in range(_CAS_ATTEMPTS):
        raw = store.get_meta(VERIFY_LAUNCHES_KEY)
        items = _admitted(raw, token)
        if item_id in items:
            return True
        if len(items) >= limit:
            return False
        value = json.dumps({"token": token, "items": [*items, item_id]})
        if store.compare_set_meta(VERIFY_LAUNCHES_KEY, raw, value):
            return True
    return False


def _record_deferral(
    store: RunStore, lease: VerifyLease, item_id: str, kind: str
) -> None:
    if lease.token is None:  # no record yet to name the episode by
        return
    try:
        store.append_audit_event_once(
            dedup_key=f"{LAUNCH_DEFERRED_EVENT}:{lease.token}:{item_id}",
            timestamp=datetime.now(UTC).isoformat(),
            event=LAUNCH_DEFERRED_EVENT,
            item_id=item_id,
            actor_type="driver",
            payload={
                "reason": lease.deferral(),
                "launch": kind,
                "verify_ref": lease.ref,
                "verify_started_at": lease.started_at,
                "verify_pid": lease.pid,
                "verify_token": lease.token,
            },
        )
    except Exception:  # noqa: BLE001 - the deferral stands without its event
        logger.warning(
            "%s: could not record its verify deferral", item_id, exc_info=True
        )


def verify_deferral(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    kind: str,
    probe: VerifyProbe = verify_running,
) -> str | None:
    """Why *item_id*'s *kind* launch waits for a running verify, or ``None``.

    Called at the spawn boundary, so a verify that started after the tick's
    admission still defers the launches after it.
    """
    lease = probe()
    if lease is None:
        return None
    if _take_launch(store, lease.token, config.drive.launches_during_verify, item_id):
        return None
    _record_deferral(store, lease, item_id, kind)
    logger.info("%s: %s launch %s", item_id, kind, lease.deferral())
    return lease.deferral()


@dataclass(frozen=True)
class StandingDeferral:
    """A launch the running verify deferred: why, and which launch it was."""

    reason: str
    launch: str


def standing_deferrals(
    store: RunStore, probe: VerifyProbe = verify_running
) -> dict[str, StandingDeferral]:
    """Item -> the deferral recorded for it during the verify running now.

    For a read-only view (``kew epic status``) with no live tick to say why a
    node waits: the ``launch_deferred`` events of THIS verify, by its token. A
    verify that has ended defers nothing, so its events stop answering.
    """
    lease = probe()
    if lease is None or lease.token is None:
        return {}
    try:  # this verify's events are none older than its start
        since: datetime | None = datetime.fromisoformat(lease.started_at)
    except ValueError:
        logger.warning(
            "the running verify's start %r is unreadable: reading every "
            "%s event for its token",
            lease.started_at,
            LAUNCH_DEFERRED_EVENT,
        )
        since = None
    found: dict[str, StandingDeferral] = {}
    events = store.list_audit_events(
        limit=None, event=LAUNCH_DEFERRED_EVENT, since=since
    )
    for row in events:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("verify_token") == lease.token and row.item_id:
            found[str(row.item_id)] = StandingDeferral(
                reason=str(payload.get("reason") or lease.deferral()),
                launch=str(payload.get("launch") or ""),
            )
    return found


def verify_warning(probe: VerifyProbe = verify_running) -> str | None:
    """The warning a hand-started launch prints while a verify runs."""
    lease = probe()
    if lease is None:
        return None
    return (
        f"a local merge verify is running ({lease.describe()}); this launch "
        "goes ahead, but it loads the machine that verify runs on"
    )
