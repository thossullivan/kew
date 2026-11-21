"""Report the kew drive's state to herdr over OSC 7501.

Run it in a herdr pane of its own, from ``kew/``:

    uv run python scripts/herdr_bridge.py

Not inside tmux: herdr cannot read a status report through tmux.

Each poll reads ``kew drive status --json`` and then, per epic in the
heartbeat's ``ticks`` whose last tick FINISHED, a snapshot ``kew epic watch
--json``. A finished tick has journaled, so the snapshot reads the journal: no
forge call, and it never silences a ``--follow`` watch. While an epic's tick
runs — and for every epic while a restarted drive has not ticked it yet — the
bridge keeps what that epic last said; an epic with no finished tick yet is
not read at all, because a snapshot with no journal falls back to a live read
that calls the forge and can write the ledger. One case still reaches that
fallback: a NEW epic whose first tick finished without journaling (exit 11,
locked out, or a crash's exit 1) is read live on each poll until a tick of it
journals.

The pane's state is the first that holds:

* ``error``   — kew could not be read, the drive's heartbeat stalled, the drive
  stopped with a non-zero exit or a refusal, or an epic's tick could not run.
* ``blocked`` — an epic has an attention event other than ``epic_complete``
  (the vocabulary is additive, so a kind this script does not know blocks
  too), its last tick stalled for an operator (tick exit 1), or a stalled live
  process holds its driver lock.
* ``idle``    — no drive runs (it stopped cleanly), it has no epic, or every
  epic it ticked is complete.
* ``working`` — the drive runs and nothing it can see waits on a person.

What the bridge cannot see: ``kew drive status --json`` names the rotation and
the epics this drive PROCESS ticked, not the drive's roster (#3574). So the
bridge remembers every epic it has read until it exits. An epic paused or
stopped before the bridge first saw it finish a tick is invisible to the pane
(``kew epic status`` and the drive's own notifications still show it). An
epic that leaves the roster while held or failing, or a paused epic whose last
read by the bridge failed (a timeout, a watch exit 2) when the drive then
restarts, keeps the pane blocked or in error until the bridge restarts. For
the same reason the bridge never reports herdr's ``done``: it cannot prove the
roster is complete.

The OSC 7501 report carries only fixed words. Text from kew (an epic name, a
kind, an item, a state) never enters an escape sequence, and the readable line
printed beside the report has every control character removed, so journal data
cannot drive the terminal.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: The states this bridge reports; herdr's ``done`` is left out on purpose.
STATES = ("idle", "working", "blocked", "error")
COMPLETE = "epic_complete"
APP = "kew"
KEW_TIMEOUT_S = 120
DETAIL_MAX = 300
MIN_INTERVAL_S = 5.0
#: `kew epic advance` exits: 0 done, 10 continue, 11 locked out (routine);
#: 1 stalled, an operator must repair it. Anything else: the tick did not run.
TICK_ROUTINE = frozenset({0, 10, 11})
TICK_STALLED = 1
#: The heartbeat stage a drive process writes before its first tick.
STAGE_STARTING = "starting"

#: Runs the kew CLI with these arguments; answers its exit code and stdout.
Kew = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class Reading:
    state: str
    detail: str


@dataclass(frozen=True)
class EpicReading:
    """What one epic's last finished tick said: blocking lines, if any, and
    whether its only event is ``epic_complete``."""

    waiting: tuple[str, ...] = ()
    complete: bool = False
    #: Why the epic could not be read; kept, so a failing epic stays an error
    #: while its next tick runs instead of flickering back to working.
    error: str = ""


class Unreadable(Exception):
    """kew answered, but not with something the bridge can stand behind."""


def kew_cli(executable: str) -> Kew:
    def call(args: Sequence[str]) -> tuple[int, str]:
        try:
            proc = subprocess.run(  # a fixed argv, never a shell
                [executable, *args],
                capture_output=True,
                text=True,
                timeout=KEW_TIMEOUT_S,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 2, ""
        return proc.returncode, proc.stdout

    return call


@dataclass
class Bridge:
    kew: Kew
    #: Each epic's last finished-tick reading, kept while its next tick runs.
    known: dict[str, EpicReading] = field(default_factory=dict)

    def poll(self) -> Reading:
        """One reading; anything kew did not promise reads as ``error``."""
        try:
            return self._read()
        except Unreadable as exc:
            return Reading("error", str(exc))
        except Exception as exc:  # noqa: BLE001 - a crash must not freeze the pane
            return Reading("error", f"bridge could not read kew: {exc!r}")

    def _read(self) -> Reading:
        code, out = self.kew(["drive", "status", "--json"])
        drive = _document(out)
        if drive is None:
            raise Unreadable(f"kew drive status exited {code} with no JSON")
        drive_state = drive.get("state")
        if drive_state == "not running":
            return _stopped(drive)
        if drive_state != "running":
            raise Unreadable(f"drive {drive_state}")
        stalled = [_text(s.get("epic")) for s in _dicts(drive.get("stalled_locks"))]
        heartbeat = drive.get("heartbeat")
        if heartbeat is not None and not isinstance(heartbeat, dict):
            raise TypeError("heartbeat is not an object")
        beat = heartbeat or {}
        rotation = _names(beat.get("rotation", []))
        ticks = _ticks(beat.get("ticks", {}))
        # A drive (re)starting has no heartbeat yet, or one at the starting
        # stage with no rotation and no ticks: its new process has ticked
        # nothing. What the epics last said still stands until they tick.
        starting = heartbeat is None or beat.get("stage") == STAGE_STARTING
        if starting and not (rotation or ticks or stalled or self.known):
            return Reading("working", "the drive is starting")
        return self._summarize(rotation, ticks, stalled)

    def _summarize(
        self, rotation: list[str], ticks: dict[str, dict[str, Any]], stalled: list[str]
    ) -> Reading:
        readings = {epic: self._epic(epic, tick) for epic, tick in ticks.items()}
        # An epic this process has not ticked — every epic right after a
        # restart, and a paused one from then on — keeps what it last said.
        for epic, reading in self.known.items():
            readings.setdefault(epic, reading)
        if not (rotation or readings or stalled):
            return Reading("idle", "the drive runs no epic")
        errors = [r.error for r in readings.values() if r.error]
        if errors:
            return Reading("error", "; ".join(errors))
        waiting = [f"{epic}: driver lock stalled" for epic in stalled]
        waiting += [line for r in readings.values() for line in r.waiting]
        if waiting:
            return Reading("blocked", "; ".join(waiting))
        unread = [e for e in rotation if e not in readings]
        complete = sum(r.complete for r in readings.values())
        if readings and complete == len(readings) and not unread:
            return Reading("idle", f"the {complete} epic(s) it ticked are complete")
        total = len(readings) + len(unread)
        return Reading("working", f"{total} epic(s), nothing waits")

    def _epic(self, epic: str, tick: dict[str, Any]) -> EpicReading:
        if tick.get("finished_at") is None:
            return self.known.get(epic, EpicReading())
        try:
            reading = self._read_epic(epic, tick.get("exit_code"))
        except Unreadable as exc:
            reading = EpicReading(error=str(exc))
        self.known[epic] = reading
        return reading

    def _read_epic(self, epic: str, exit_code: Any) -> EpicReading:
        if exit_code != TICK_STALLED and exit_code not in TICK_ROUTINE:
            raise Unreadable(f"epic {epic}: its last tick exited {exit_code}")
        code, out = self.kew(["epic", "watch", "--json", "--", epic])
        events = _events(out) if code in (0, 1) else None
        if events is None or (code == 1 and not events):
            raise Unreadable(f"epic {epic}: kew epic watch exited {code}")
        waiting = [
            f"{epic}: {e.get('kind')} {e.get('item', '')}".rstrip()
            for e in events
            if e.get("kind") != COMPLETE
        ]
        if exit_code == TICK_STALLED:
            waiting.append(f"{epic}: stalled, needs an operator")
        return EpicReading(tuple(waiting), complete=bool(events) and not waiting)


def _stopped(drive: dict[str, Any]) -> Reading:
    exit_ = drive.get("exit")
    if drive.get("refusal") is not None:
        return Reading("error", "the drive was refused; run `kew drive status`")
    if isinstance(exit_, dict) and exit_.get("code") not in (None, 0):
        return Reading("error", f"the drive exited {exit_.get('code')}")
    return Reading("idle", "no drive runs")


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("expected a list of epic names")
    return [_text(v) for v in value]


def _ticks(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise TypeError("ticks is not an object")
    for name, tick in value.items():
        _text(name)
        if not isinstance(tick, dict):
            raise TypeError("a tick is not an object")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected a non-empty string")
    return value


def _dicts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise TypeError("expected a list of objects")
    return value


def _document(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _events(text: str) -> list[dict[str, Any]] | None:
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        event = _document(line)
        if event is None:
            return None
        events.append(event)
    return events


def is_control(char: str) -> bool:
    """C0/C1 controls, format characters (bidi overrides) and the rest of
    Unicode category C — nothing that should reach a terminal from data."""
    return unicodedata.category(char).startswith("C")


def printable(text: str) -> str:
    return "".join(c for c in text if not is_control(c))[:DETAIL_MAX]


def osc_report(state: str) -> str:
    if state not in STATES:
        raise ValueError(f"not a state this bridge reports: {state!r}")
    return f"\x1b]7501;state={state}:app={APP}\x1b\\"


def run(kew: Kew, *, interval: float, once: bool) -> None:
    """Report each change of state once; Ctrl-C leaves the pane ``idle``."""
    bridge = Bridge(kew)
    last: Reading | None = None
    try:
        while True:
            reading = bridge.poll()
            if last is None or reading.state != last.state:
                sys.stdout.write(osc_report(reading.state))
            if reading != last:
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"{stamp} {reading.state:<7} {printable(reading.detail)}")
            sys.stdout.flush()
            last = reading
            if once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write(osc_report("idle"))
        sys.stdout.flush()


def _interval(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < MIN_INTERVAL_S:
        raise argparse.ArgumentTypeError(f"must be at least {MIN_INTERVAL_S:g}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kew", default="kew", help="The kew executable.")
    parser.add_argument(
        "--interval", type=_interval, default=30.0, help="Seconds between polls."
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit.")
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        # A terminal that cannot encode a character must not end the bridge.
        sys.stdout.reconfigure(errors="replace")
    run(kew_cli(args.kew), interval=args.interval, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
