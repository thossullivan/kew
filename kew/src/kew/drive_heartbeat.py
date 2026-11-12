"""The project drive's heartbeat: what `kew drive` is doing right now (#2617).

A running drive writes one ``meta`` row, ``drive:heartbeat``, on its own
thread every ``every_s`` seconds and again whenever its stage changes: the
time, the stage (``ticking`` an epic, ``sleeping`` between rounds), the
epics it will tick (its rotation) and, per epic, when the drive last started
and finished a tick and how that tick exited.

Every surface that reads a driver's health reads it here — ``kew drive
status``, the console's header and ``/health`` (through
:func:`kew.service.epics.liveness_from_state`). The reason is #2534's false
"down": one drive ticks its epics in turn, and each tick hands its review to a
detached stage, so an epic waiting its turn — or whose review is still running
— has no fresh tick stamp and no live lock of its own. It is not gone: the
drive is working. Only the drive's own heartbeat going silent is.

The reading rule:

* a heartbeat counts only while it belongs to the drive that holds the lock
  NOW (same ``token`` as ``drive:holder``) — a stopped drive releases the lock,
  and a reclaimed one is no longer the holder, so neither vouches for anything;
* it is **stalled** once older than ``DRIVE_STALL_INTERVALS`` of its own
  write interval — the drive is gone, and so is every epic in its rotation;
* otherwise every epic in the rotation is driven: ``live`` while its own tick
  stamps are inside their cadence, ``busy`` while it waits its turn.

This module imports nothing from ``kew.graph``, so the service layer (which
the import-linter fences off from it) can read the drive's heartbeat directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger(__name__)

DRIVE_HOLDER_KEY = "drive:holder"
DRIVE_HEARTBEAT_KEY = "drive:heartbeat"

#: A drive whose heartbeat is older than this many of its own write intervals
#: has stalled: three misses absorb a slow write or a busy ledger, and still
#: say "gone" within a couple of minutes rather than at the lock's 5-minute
#: reclaim line.
DRIVE_STALL_INTERVALS = 3.0

STAGE_STARTING = "starting"
STAGE_TICKING = "ticking"
STAGE_SLEEPING = "sleeping"
STAGE_STOPPED = "stopped"

#: The drive's own reading: its heartbeat is inside the stall line, or not.
DRIVE_LIVE = "live"
DRIVE_GONE = "gone"


@dataclass(frozen=True)
class DriveEpicTick:
    """The drive's last tick of one epic."""

    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_json(cls, data: Any) -> DriveEpicTick:
        code = data.get("exit_code")
        return cls(
            started_at=str(data["started_at"]),
            finished_at=None
            if data.get("finished_at") is None
            else str(data["finished_at"]),
            exit_code=None if code is None else int(code),
        )


@dataclass(frozen=True)
class DriveHeartbeat:
    """One heartbeat of the drive holding ``token``."""

    token: str
    pid: int
    host: str
    at: datetime
    every_s: float
    stage: str
    epic: str | None = None
    rotation: tuple[str, ...] = ()
    ticks: dict[str, DriveEpicTick] = field(default_factory=dict)

    def encode(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "pid": self.pid,
                "host": self.host,
                "at": self.at.isoformat(),
                "every_s": self.every_s,
                "stage": self.stage,
                "epic": self.epic,
                "rotation": list(self.rotation),
                "ticks": {k: v.to_json() for k, v in sorted(self.ticks.items())},
            },
            sort_keys=True,
        )

    @classmethod
    def parse(cls, raw: str) -> DriveHeartbeat | None:
        """The heartbeat ``raw`` records, or None when it records none usable.

        A non-positive interval is refused: nothing can be thresholded against
        it, and a reading that cannot say "gone" must not say "driven" either.
        """
        try:
            data = json.loads(raw)
            at = datetime.fromisoformat(data["at"])
            beat = cls(
                token=str(data["token"]),
                pid=int(data["pid"]),
                host=str(data["host"]),
                at=at if at.tzinfo is not None else at.replace(tzinfo=UTC),
                every_s=float(data["every_s"]),
                stage=str(data["stage"]),
                epic=None if data.get("epic") is None else str(data["epic"]),
                rotation=tuple(str(e) for e in data.get("rotation") or ()),
                ticks={
                    str(k): DriveEpicTick.from_json(v)
                    for k, v in (data.get("ticks") or {}).items()
                },
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _logger.warning("drive heartbeat unreadable: %s", exc)
            return None
        return beat if beat.every_s > 0 else None

    @property
    def stall_after_s(self) -> float:
        return DRIVE_STALL_INTERVALS * self.every_s

    def age_s(self, now: datetime) -> float:
        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return (reference - self.at).total_seconds()

    def status(self, now: datetime) -> str:
        """``gone`` once the heartbeat is older than its stall line."""
        return DRIVE_GONE if self.age_s(now) > self.stall_after_s else DRIVE_LIVE

    def drives(self, epic_ref: str) -> bool:
        """Whether this drive ticks ``epic_ref`` — it is in the rotation."""
        return epic_ref in self.rotation


def _get_meta(store: Any, key: str) -> str | None:
    getter = getattr(store, "get_meta", None)
    if not callable(getter):
        return None
    try:
        value = getter(key)
    except Exception as exc:  # noqa: BLE001 — a health read never sinks its page
        _logger.warning("drive heartbeat: meta %r could not be read: %s", key, exc)
        return None
    return value if isinstance(value, str) else None


def last_drive_heartbeat(store: Any) -> DriveHeartbeat | None:
    """The last heartbeat any drive wrote, current holder or not."""
    raw = _get_meta(store, DRIVE_HEARTBEAT_KEY)
    return DriveHeartbeat.parse(raw) if raw else None


def holder_token(store: Any) -> str | None:
    """The token of the drive that holds the lock, or None when none does."""
    raw = _get_meta(store, DRIVE_HOLDER_KEY)
    if not raw:
        return None
    try:
        token = json.loads(raw).get("token")
    except (ValueError, AttributeError) as exc:
        # The next `kew drive` reclaims an unreadable holder row; until then no
        # drive is provably holding the lock, so its heartbeat vouches nothing.
        _logger.warning("drive holder row unreadable: %s", exc)
        return None
    return token if isinstance(token, str) else None


def read_drive_heartbeat(store: Any) -> DriveHeartbeat | None:
    """The heartbeat of the drive holding the lock now, live or stalled.

    None when no drive holds the lock, or when the heartbeat row was written by
    a drive that no longer holds it — a stopped or reclaimed drive vouches for
    nothing.
    """
    beat = last_drive_heartbeat(store)
    if beat is None or beat.token != holder_token(store):
        return None
    return beat
