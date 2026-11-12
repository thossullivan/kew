#!/usr/bin/env python3
"""Serialize Docker jobs on one host with a detached, bounded flock holder.

Requires Python 3.9+ on macOS or Linux (the runner's system python3). The
kernel orders blocking waiters; strict FIFO ordering is not guaranteed. Never
unlink the shared lock file. The lock lives at a fixed path under /tmp so every
runner service on the host, whatever its TMPDIR, contends on the same file;
KEW_CI_DOCKER_LOCK overrides it for tests. RUNNER_TEMP holds each job's marker.

Worker death or SIGUSR1 starts draining because step processes can survive.
Poll every five seconds until kew- containers, Docker build/image clients,
and processes in the recorded workspace stay absent for 60 seconds.
Missing or failing Docker uses a 900-second grace period from drain start.
Grace also requires no workspace processes. Process-probe errors cannot
establish quiet. Flags override these times.
The hold-max deadline bounds every phase, including probes. Atomic marker
updates record holding, draining, and released-on-quiet/grace/bound phases.
Terminal markers remain until successful release or reuse. A neighboring .log
records phase transitions and changes in surviving-producer counts.
Successful jobs release immediately. Other statuses request draining and wait
up to 900 seconds; a wait timeout leaves the holder and marker intact.
The holder runs without RUNNER_TRACKING_ID so the runner's job-completion
cleanup cannot kill it while it drains. acquire and a draining release publish
their pid chains in a directory next to the shared lock; the holder skips this
script's own processes and every published chain whose leader the listing
shows as the named verb of this script, so a following job's acquire on the
same checkout is not a surviving producer.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

_LOCK_FILENAME = "kew-ci-docker-daemon.lock"
_HOLD_VERB = "_hold"
_RELEASE_VERB = "release"
_ACQUIRE_VERB = "acquire"
_COORDINATION_VERBS = (_ACQUIRE_VERB, _RELEASE_VERB, "status", _HOLD_VERB)
# Actions kills every process that still carries the job's tracking id when
# the job completes (#1950 round 5). The detached holder must outlive that.
_RUNNER_TRACKING_VARIABLE = "RUNNER_TRACKING_ID"


def _holder_environment() -> dict[str, str]:
    """The holder's environment without the runner's orphan-tracking id."""
    return {
        name: value
        for name, value in os.environ.items()
        if name != _RUNNER_TRACKING_VARIABLE
    }


def _default_lock_path() -> Path:
    override = os.environ.get("KEW_CI_DOCKER_LOCK")
    return Path(override) if override else Path("/tmp") / _LOCK_FILENAME  # noqa: S108


def _default_marker_path() -> Path:
    return Path(os.environ.get("RUNNER_TEMP") or "/tmp") / "kew-ci-docker-daemon.marker"  # noqa: S108


def _is_holder(pid: int) -> bool:
    """True when *pid* is still one of this script's holder processes.

    A marker can outlive its holder after bounded release or a crash. The pid
    may then belong to an unrelated process, which release must never signal.
    """
    try:
        listing = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = listing.stdout.strip()
    return Path(__file__).name in command and f" {_HOLD_VERB} " in f" {command} "


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("seconds must be finite and positive")
    return seconds


def _marker_record(marker: Path) -> dict[str, object]:
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _marker_pid(marker: Path) -> int | None:
    pid = _marker_record(marker).get("pid")
    return pid if type(pid) is int and pid > 1 else None


def _remove_marker(marker: Path, pid: int | None) -> None:
    if _marker_pid(marker) == pid:
        with contextlib.suppress(OSError):
            marker.unlink(missing_ok=True)


def _stop(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


_PROBE_TIMEOUT_SECONDS = 10.0
_ANCESTOR_LIMIT = 32


def _coordination_dir(lock: Path) -> Path:
    return lock.with_name(lock.name + ".coordination")


def _marker_lock(marker: Path) -> Path:
    """The lock a holder recorded in its marker; the default path otherwise."""
    value = _marker_record(marker).get("lock")
    return Path(value) if isinstance(value, str) and value else _default_lock_path()


def _parent_pid(pid: int) -> int | None:
    try:
        listing = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        value = int(listing.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return value if value > 1 else None


def _pid_chain(pid: int) -> list[int]:
    """This process and its ancestors up to the runner (bounded, loop-safe)."""
    chain = [pid]
    while len(chain) < _ANCESTOR_LIMIT:
        parent = _parent_pid(chain[-1])
        if parent is None or parent in chain:
            break
        chain.append(parent)
    return chain


def _publish_coordination(lock: Path, verb: str) -> Path:
    """Record this process's pid chain as lock coordination, not workload.

    acquire and release run inside the job workspace, so their cwd (and their
    step shell's) would otherwise count as surviving producers: a failed
    job's release for its whole wait (#1950 round 5), a following job's
    acquire on the same persistent checkout until the hold bound (round 6).
    """
    directory = _coordination_dir(lock)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{os.getpid()}.json"
    record = {"verb": verb, "pids": _pid_chain(os.getpid())}
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(record) + "\n")
    temporary.replace(path)
    return path


CoordinationRecords = list[tuple[str, list[int]]]


def _coordination_records(lock: Path) -> CoordinationRecords:
    """Published chains whose leader is alive; stale files are removed."""
    records: CoordinationRecords = []
    try:
        paths = sorted(_coordination_dir(lock).glob("*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        verb = record.get("verb") if isinstance(record, dict) else None
        pids = record.get("pids") if isinstance(record, dict) else None
        if verb not in _COORDINATION_VERBS or not isinstance(pids, list):
            continue
        chain = [pid for pid in pids if type(pid) is int and pid > 1]
        if not chain or not _alive(chain[0]):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        records.append((verb, chain))
    return records


def _script_verb(command: str) -> str | None:
    """The verb when *command* is an invocation of this script, else None.

    The interpreter, the script argument and the verb are checked as argv
    tokens: a workload whose argv merely mentions the script name is not
    coordination (#1950 round 7).
    """
    tokens = command.split()
    # macOS reports the framework interpreter as .../Python.app/Contents/MacOS/Python.
    if len(tokens) < 3 or not Path(tokens[0]).name.lower().startswith("python"):
        return None
    if Path(tokens[1]).name != Path(__file__).name:
        return None
    return tokens[2] if tokens[2] in _COORDINATION_VERBS else None


def _verified_exclusions(
    rows: list[tuple[int, str]], records: CoordinationRecords
) -> set[int]:
    """Pids that are lock coordination, never workload.

    Every invocation of this script (any verb, checked as argv tokens) is
    coordination. A published chain counts only when the listing shows its
    leader invoking this script with the recorded verb; otherwise the file
    is ignored.
    """
    commands = dict(rows)
    skipped = {pid for pid, command in rows if _script_verb(command) is not None}
    for verb, chain in records:
        if _script_verb(commands.get(chain[0], "")) == verb:
            skipped.update(chain)
    return skipped


def _surviving_producers(
    workspace: Path, deadline: float, records: CoordinationRecords | None = None
) -> int | None:
    """Find workspace argv or cwd matches, excluding this holder and a verified
    release coordination chain.

    Scan argv once, then inspect remaining candidates' cwd in one lsof call.
    This avoids recursive workspace traversal and one subprocess per pid.
    Unknown probe results cannot establish that producers have finished.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid,command", "-ww"],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(remaining, _PROBE_TIMEOUT_SECONDS),
        )
        if listing.returncode != 0:
            return None
        lines = listing.stdout.splitlines()
        if not lines or lines[0].split() != ["PID", "COMMAND"]:
            return None
        # Keep the lexical path too: macOS may resolve /tmp to /private/tmp.
        paths = {str(workspace), str(workspace.resolve())}
        patterns = [
            re.compile(r"(?:^|[\s='\"])" + re.escape(path) + r"(?:/|$|[\s'\"])")
            for path in paths
        ]
        rows: list[tuple[int, str]] = []
        for line in lines[1:]:
            fields = line.strip().split(None, 1)
            if len(fields) != 2:
                return None
            rows.append((int(fields[0]), fields[1]))
        skipped = {os.getpid(), *_verified_exclusions(rows, records or [])}
        producers: set[int] = set()
        candidates: list[str] = []
        for pid, command in rows:
            if pid in skipped:
                continue
            if any(pattern.search(command) for pattern in patterns):
                producers.add(pid)
            else:
                candidates.append(str(pid))
        if not candidates:
            return len(producers)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # -w: host warnings (Linux prints one for Docker's nsfs netns mount once
        # any container has run) are not probe errors; real errors still exit >1.
        directories = subprocess.run(
            ["lsof", "-a", "-w", "-d", "cwd", "-p", ",".join(candidates), "-Fpn"],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(remaining, _PROBE_TIMEOUT_SECONDS),
        )
        # Exit 1 with no diagnostics also means all candidates have exited.
        if directories.returncode not in (0, 1) or directories.stderr.strip():
            return None
        pid = 0
        for line in directories.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid:
                cwd = Path(line[1:])
                if not cwd.is_absolute():
                    return None
                if cwd.resolve().is_relative_to(workspace.resolve()):
                    producers.add(pid)
        return len(producers)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _daemon_quiet(deadline: float, poll_seconds: float) -> bool | None:
    """Return None when Docker is unavailable; other probe errors are busy.

    The probe timeout is independent of the poll interval: a short interval
    must not turn a slow-but-healthy ``docker ps`` into "unavailable".
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    probe_timeout = max(poll_seconds, _PROBE_TIMEOUT_SECONDS)
    try:
        containers = subprocess.run(
            ["docker", "ps", "-q", "--filter", "name=kew-"],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(remaining, probe_timeout),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if containers.returncode != 0:
        return None
    if containers.stdout.strip():
        return False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    try:
        clients = subprocess.run(
            ["pgrep", "-f", "docker (build|buildx|image|pull|save|load)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=min(remaining, probe_timeout),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # pgrep returns 1 for no matches; errors cannot establish quiet.
    return clients.returncode == 1


def _drain(
    deadline: float,
    died_at: float,
    quiet_seconds: float,
    grace_seconds: float,
    poll_seconds: float,
    workspace: Path,
    report_producers: Callable[[int | None], None],
    excluded_pids: Callable[[], CoordinationRecords] = list,
) -> str:
    quiet_since: float | None = None
    grace_deadline = died_at + grace_seconds
    fallback = False
    while (now := time.monotonic()) < deadline:
        polled_at = now
        producers = _surviving_producers(workspace, deadline, excluded_pids())
        report_producers(producers)
        quiet = None if fallback else _daemon_quiet(deadline, poll_seconds)
        now = time.monotonic()
        if now >= deadline:
            break
        if quiet is None:
            # Docker failure uses a fixed grace, but producers still block it.
            fallback = True
            if producers == 0 and now >= grace_deadline:
                return "released-on-grace"
        if quiet and producers == 0:
            if quiet_since is None:
                quiet_since = now
            if now - quiet_since >= quiet_seconds:
                return "released-on-quiet"
        else:
            quiet_since = None
        next_poll = polled_at + poll_seconds
        if fallback and now < grace_deadline:
            next_poll = min(next_poll, grace_deadline)
        time.sleep(max(0, min(deadline, next_poll) - time.monotonic()))
    return "released-on-bound"


def _hold(
    lock: Path,
    marker: Path,
    hold_max_seconds: float,
    watch_pid: int | None,
    orphan_quiet_seconds: float,
    orphan_grace_seconds: float,
    orphan_poll_seconds: float,
    workspace: Path,
) -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    drain_requested_at: float | None = None

    def request_drain(signum: int, frame: FrameType | None) -> None:
        nonlocal drain_requested_at
        if drain_requested_at is None:
            drain_requested_at = time.monotonic()

    signal.signal(signal.SIGUSR1, request_drain)
    pid = os.getpid()
    temporary = marker.with_name(f".{marker.name}.{pid}.tmp")
    log = marker.with_name(marker.name + ".log")
    record: dict[str, object] = {
        "pid": pid,
        "watch_pid": watch_pid,
        "workspace": str(workspace),
        "lock": str(lock),
    }
    released = False

    def write_record() -> None:
        temporary.write_text(json.dumps(record) + "\n")
        temporary.replace(marker)

    def log_event(event: str, *, reset: bool = False) -> None:
        with log.open("w" if reset else "a") as events:
            timestamp = datetime.now(timezone.utc).isoformat()  # noqa: UP017
            events.write(f"{timestamp} pid={pid} {event}\n")

    def transition(phase: str) -> None:
        record["phase"] = phase
        write_record()
        log_event(f"phase={phase}", reset=phase == "holding")

    def report_producers(count: int | None) -> None:
        if (
            "surviving_producers" not in record
            or record["surviving_producers"] != count
        ):
            record["surviving_producers"] = count
            write_record()
            log_event(
                f"surviving-producers={count if count is not None else 'unknown'}"
            )

    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as handle:
            # A blocking kernel wait retains every contender across workflows.
            fcntl.flock(handle, fcntl.LOCK_EX)
            deadline = time.monotonic() + hold_max_seconds
            marker.parent.mkdir(parents=True, exist_ok=True)
            # timezone.utc works with the runner's system Python 3.9.
            record["acquired_at"] = datetime.now(timezone.utc).isoformat()  # noqa: UP017
            transition("holding")
            phase = "released-on-bound"
            while (remaining := deadline - time.monotonic()) > 0:
                if drain_requested_at is not None or (
                    watch_pid is not None and not _alive(watch_pid)
                ):
                    died_at = drain_requested_at or time.monotonic()
                    transition("draining")
                    phase = _drain(
                        deadline,
                        died_at,
                        orphan_quiet_seconds,
                        orphan_grace_seconds,
                        orphan_poll_seconds,
                        workspace,
                        report_producers,
                        lambda: _coordination_records(lock),
                    )
                    break
                time.sleep(min(remaining, 0.5))
            transition(phase)
            released = True
    finally:
        # Closing the file also releases the lock after SIGTERM or an error.
        if not released:
            _remove_marker(marker, pid)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
    return 0


def _grandparent_pid() -> int | None:
    """The pid two levels up: under GitHub Actions that is the runner worker
    that owns the job (acquire runs inside the step's shell). It outlives the
    acquire step and dies with the job, so the holder can watch it."""
    try:
        listing = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        value = int(listing.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return value if value > 1 else None


def acquire(
    lock: Path,
    marker: Path,
    timeout_seconds: float,
    hold_max_seconds: float,
    watch_pid: int | None,
    orphan_quiet_seconds: float,
    orphan_grace_seconds: float,
    orphan_poll_seconds: float,
) -> int:
    started = time.monotonic()
    acquired = False
    holder_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_hold",
        "--lock",
        str(lock.resolve()),
        "--marker",
        str(marker.resolve()),
        "--hold-max-seconds",
        str(hold_max_seconds),
        "--orphan-quiet-seconds",
        str(orphan_quiet_seconds),
        "--orphan-grace-seconds",
        str(orphan_grace_seconds),
        "--orphan-poll-seconds",
        str(orphan_poll_seconds),
        "--workspace",
        str(Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd()).absolute()),
    ]
    if watch_pid is not None:
        holder_argv += ["--watch-pid", str(watch_pid)]
    coordination = _publish_coordination(lock, _ACQUIRE_VERB)
    holder = subprocess.Popen(
        holder_argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_holder_environment(),
    )
    try:
        while (remaining := timeout_seconds - (time.monotonic() - started)) > 0:
            if _marker_pid(marker) == holder.pid and holder.poll() is None:
                print(
                    f"acquired pid={holder.pid} waited={time.monotonic() - started:.3f}"
                )
                acquired = True
                return 0
            if holder.poll() is not None:
                print(
                    "docker daemon lock: holder exited before acquisition",
                    file=sys.stderr,
                )
                return 1
            time.sleep(min(remaining, 0.05))
        print(
            f"docker daemon lock: timed out after {timeout_seconds:g}s",
            file=sys.stderr,
        )
        return 2
    finally:
        with contextlib.suppress(OSError):
            coordination.unlink()
        if not acquired:
            # Reap this specific child before returning, including cancellation.
            if holder.poll() is None:
                holder.kill()
            holder.wait()
            _remove_marker(marker, holder.pid)


def release(
    marker: Path, job_status: str = "success", release_wait_seconds: float = 900
) -> int:
    pid = _marker_pid(marker)
    draining = job_status != "success"
    coordination: Path | None = None
    try:
        if pid is not None and _is_holder(pid):
            if draining:
                coordination = _publish_coordination(
                    _marker_lock(marker), _RELEASE_VERB
                )
            os.kill(pid, signal.SIGUSR1 if draining else signal.SIGTERM)
            deadline = time.monotonic() + (release_wait_seconds if draining else 1)
            while time.monotonic() < deadline:
                if draining:
                    record = _marker_record(marker)
                    if record.get("pid") != pid or str(record.get("phase")).startswith(
                        "released-"
                    ):
                        break
                os.kill(pid, 0)
                time.sleep(0.05)
    except (OSError, OverflowError):
        pass
    finally:
        if coordination is not None:
            with contextlib.suppress(OSError):
                coordination.unlink()
        if not draining:
            _remove_marker(marker, pid)
    return 0


def status(lock: Path) -> int:
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("held")
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            print("free")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("acquire", "release", "status", "_hold"):
        command = verbs.add_parser(verb)
        if verb != "release":
            command.add_argument("--lock", type=Path, default=_default_lock_path())
        if verb != "status":
            command.add_argument("--marker", type=Path, default=_default_marker_path())
        if verb in ("acquire", "_hold"):
            command.add_argument(
                "--hold-max-seconds", type=_positive_seconds, required=True
            )
            command.add_argument(
                "--orphan-quiet-seconds", type=_positive_seconds, default=60
            )
            command.add_argument(
                "--orphan-grace-seconds", type=_positive_seconds, default=900
            )
            command.add_argument(
                "--orphan-poll-seconds", type=_positive_seconds, default=5
            )
        if verb == "acquire":
            command.add_argument(
                "--timeout-seconds", type=_positive_seconds, required=True
            )
        if verb == "_hold":
            command.add_argument("--workspace", type=Path, required=True)
        if verb == "release":
            command.add_argument("--job-status", default="success")
            command.add_argument(
                "--release-wait-seconds", type=_positive_seconds, default=900
            )
        if verb in ("acquire", "_hold"):
            # acquire: the process the holder watches (default: the runner
            # worker two levels up; 0 disables watching). _hold: internal.
            command.add_argument("--watch-pid", type=int, default=None)
    args = parser.parse_args()
    if args.verb == "release":
        return release(args.marker, args.job_status, args.release_wait_seconds)
    if args.verb == "status":
        return status(args.lock)
    if args.verb == "_hold":
        return _hold(
            args.lock,
            args.marker,
            args.hold_max_seconds,
            args.watch_pid,
            args.orphan_quiet_seconds,
            args.orphan_grace_seconds,
            args.orphan_poll_seconds,
            args.workspace,
        )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    watch_pid = _grandparent_pid() if args.watch_pid is None else args.watch_pid
    if watch_pid is not None and watch_pid <= 1:
        watch_pid = None
    return acquire(
        args.lock,
        args.marker,
        args.timeout_seconds,
        args.hold_max_seconds,
        watch_pid,
        args.orphan_quiet_seconds,
        args.orphan_grace_seconds,
        args.orphan_poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
