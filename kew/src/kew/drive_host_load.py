"""The drive's host-load hold: no new admission while this machine is
overloaded (#2813).

``[drive] max_load_per_cpu`` and ``[drive] max_swap_used_pct`` are off when
unset. With either set, the drive reads the host before every tick; above a
threshold it holds admission (running work continues) and records the episode
in the ``drive:host-load`` meta row, so an epic tick told to admit nothing can
say it was the host load, and the episode raises its attention event once.
Admission resumes by itself once both readings stay at or below their
thresholds for ``CALM_TICKS`` consecutive reads.

A reading that cannot be taken is an overload, never a calm host: a gate that
cannot see the load must not read it as idle (the ``[drive.load]`` rule). So is
a malformed one — a load that is not a finite number at or above 0, a CPU count
below 1, a swap share outside 0..100 — and so is a sysctl answer that is not
exactly the ``vm.swapusage`` line.

This module holds the reading and the row's shape only; the compare-and-set
that moves the row lives with the drive's other control rows in
:mod:`kew.drive`.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess  # nosec B404 — only for its exception types; runs go through subprocess_utils
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

DRIVE_HOST_LOAD_KEY = "drive:host-load"

#: Consecutive reads at or below both thresholds that end an episode.
CALM_TICKS = 2

_MEMINFO = Path("/proc/meminfo")
#: macOS ships sysctl here; it is never looked up on PATH, which an agent's
#: environment could have pointed at a stand-in that reports an idle host.
_SYSCTL = Path("/usr/sbin/sysctl")
_SYSCTL_TIMEOUT_S = 5
_MEGABYTES = r"(\d+(?:\.\d+)?)M"
#: The whole of ``sysctl -n vm.swapusage``, so a fragment inside malformed
#: output is never read as a reading.
_SWAPUSAGE = re.compile(
    rf"total = {_MEGABYTES}\s+used = {_MEGABYTES}\s+free = {_MEGABYTES}"
    r"(?:\s+\(encrypted\))?"
)


class MetaReader(Protocol):
    def get_meta(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class HostReading:
    """One look at this machine: its one-minute load average, its CPU count,
    and the share of swap in use — or why the swap could not be read. ``swap``
    is None when it was not asked for."""

    load: float
    cpus: int
    swap: float | str | None = None


def _linux_swap() -> float:
    fields: dict[str, int] = {}
    for line in _MEMINFO.read_text().splitlines():
        name, _, rest = line.partition(":")
        if name in ("SwapTotal", "SwapFree"):
            fields[name] = int(rest.split()[0])
    total = fields["SwapTotal"]
    return 0.0 if total == 0 else 100.0 * (total - fields["SwapFree"]) / total


def _macos_swap() -> float:
    from kew.subprocess_utils import TOOL_PATHS, run

    if not _SYSCTL.is_file():
        raise OSError(f"no {_SYSCTL}")
    TOOL_PATHS["sysctl"] = str(_SYSCTL)
    out = run(["sysctl", "-n", "vm.swapusage"], timeout=_SYSCTL_TIMEOUT_S).stdout
    match = _SWAPUSAGE.fullmatch(out.strip())
    if match is None:
        raise ValueError(f"unrecognised vm.swapusage {out.strip()!r}")
    total, used = float(match[1]), float(match[2])
    return 0.0 if total == 0 else 100.0 * used / total


def read_swap() -> float | str:
    """The share of swap in use, as a percentage, or why it cannot be read."""
    try:
        if sys.platform == "darwin":
            return _macos_swap()
        if _MEMINFO.exists():
            return _linux_swap()
        return f"swap unreadable on {sys.platform}"
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as e:
        return f"swap unreadable ({e})"


def read_host(*, swap: bool) -> HostReading | str:
    """This machine's load and CPU count — and its swap when ``swap`` — or
    why the load cannot be read."""
    try:
        load = os.getloadavg()[0]
    except OSError as exc:
        return f"host load unreadable ({exc})"
    cpus = os.cpu_count()
    if cpus is None:
        return "host CPU count unreadable"
    return HostReading(load, cpus, read_swap() if swap else None)


def _invalid(reading: HostReading, *, swap: bool) -> str | None:
    """Why ``reading`` is not a reading at all, or None when it is one. A
    malformed number is unreadable, never a calm host."""
    if not math.isfinite(reading.load) or reading.load < 0:
        return f"host load unreadable (load average {reading.load!r})"
    if reading.cpus < 1:
        return f"host CPU count unreadable ({reading.cpus!r})"
    share = reading.swap if swap else None
    if isinstance(share, float | int) and not (
        math.isfinite(share) and 0 <= share <= 100
    ):
        return f"swap unreadable ({share!r}% used)"
    return None


def overload(
    reading: HostReading | str,
    *,
    max_load_per_cpu: float | None,
    max_swap_used_pct: float | None,
) -> str | None:
    """What about ``reading`` is above a threshold, or None for a calm host."""
    if isinstance(reading, str):
        return reading
    invalid = _invalid(reading, swap=max_swap_used_pct is not None)
    if invalid is not None:
        return invalid
    over = [
        why
        for why in (
            _load_over(reading, max_load_per_cpu),
            _swap_over(reading, max_swap_used_pct),
        )
        if why is not None
    ]
    return "; ".join(over) or None


def _load_over(reading: HostReading, limit: float | None) -> str | None:
    per_cpu = reading.load / reading.cpus
    if limit is None or per_cpu <= limit:
        return None
    return (
        f"host load {reading.load:.1f} on {reading.cpus} CPU(s) "
        f"({per_cpu:.2f} per CPU, above [drive] max_load_per_cpu {limit:g})"
    )


def _swap_over(reading: HostReading, limit: float | None) -> str | None:
    if limit is None:
        return None
    if isinstance(reading.swap, str) or reading.swap is None:
        return reading.swap or "swap not read"
    if reading.swap <= limit:
        return None
    return f"swap {reading.swap:.0f}% used (above [drive] max_swap_used_pct {limit:g})"


@dataclass(frozen=True)
class HostLoadHold:
    """A standing host-load episode: since when, the last overloaded reading,
    and how many calm reads in a row have followed it."""

    since: datetime
    reason: str
    calm: int = 0

    def encode(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "reason": self.reason,
            "calm": self.calm,
        }

    def describe(self) -> str:
        since = self.since.isoformat(timespec="seconds")
        easing = (
            f"{self.calm} of {CALM_TICKS} calm reads"
            if self.calm
            else f"resumes after {CALM_TICKS} reads below the thresholds"
        )
        return f"{self.reason} (held since {since}; {easing})"


def host_load_hold_from(value: Any) -> HostLoadHold | None:
    """The episode a decoded row records; raises ValueError for a row that is
    not one, including a calm count outside ``0..CALM_TICKS``. An empty object
    is no episode."""
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{DRIVE_HOST_LOAD_KEY} is not an episode: {value!r}")
    calm = value.get("calm", 0)
    if type(calm) is not int or not 0 <= calm <= CALM_TICKS:
        raise ValueError(
            f"{DRIVE_HOST_LOAD_KEY} is unreadable: calm count {calm!r} is not "
            f"within 0..{CALM_TICKS}"
        )
    try:
        return HostLoadHold(
            since=datetime.fromisoformat(value["since"]),
            reason=str(value["reason"]),
            calm=calm,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{DRIVE_HOST_LOAD_KEY} is unreadable: {exc}") from exc


def host_load_hold(store: MetaReader) -> HostLoadHold | None:
    """The standing episode, for a reader that only names it (an epic tick).

    Raises ValueError for an unreadable row; the drive itself never reads one
    as "no episode" (:func:`kew.drive.host_gate`).
    """
    raw = store.get_meta(DRIVE_HOST_LOAD_KEY)
    return host_load_hold_from(json.loads(raw) if raw else None)
