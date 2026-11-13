"""Host process identity checks shared by dispatch, fleet, and reconciliation."""

from __future__ import annotations

import ctypes
import functools
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

from kew.credential_environment import child_environment


def run(
    cmd: list[str], *, check: bool = False, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    """Run only ps without importing the runner-dependent subprocess utilities."""
    if not cmd or cmd[0] != "ps":
        raise ValueError("Process identity probes can only execute ps")
    # ps lives at a fixed system path; a PATH lookup is only a convenience, and
    # a cleared or restricted PATH must not turn every admission into a refusal.
    path = shutil.which("ps") or next(
        (p for p in ("/bin/ps", "/usr/bin/ps") if os.path.exists(p)), None
    )
    if path is None:
        raise FileNotFoundError("ps is unavailable")
    return subprocess.run(  # nosec B603 — fixed read-only ps executable, never an agent command
        [path, *cmd[1:]],
        check=check,
        timeout=timeout,
        capture_output=True,
        text=True,
        env={**child_environment(), "LC_ALL": "C"},
        start_new_session=True,
    )


_MAX_PID = 2**31 - 1  # Signed pid_t limit on supported Linux and macOS hosts.
_MAX_EVENT_BYTES = 16_384
_MAX_EVENT_DEPTH = 64


def _check_event_depth(line: str) -> None:
    """Bound container nesting without counting brackets inside JSON strings."""
    depth = 0
    quoted = escaped = False
    for char in line:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_EVENT_DEPTH:
                raise ValueError("event nesting exceeds 64 levels")
        elif char in "]}":
            depth -= 1


def read_event_evidence(event_path: Path) -> Iterator[dict[str, object] | None]:
    """Yield bounded events; None records unavailable evidence for one line."""
    if not event_path.exists():
        return
    try:
        with event_path.open("rb") as events:
            while raw := events.readline(_MAX_EVENT_BYTES + 1):
                try:
                    if len(raw) > _MAX_EVENT_BYTES:
                        while not raw.endswith(b"\n"):
                            raw = events.readline(_MAX_EVENT_BYTES + 1)
                            if not raw:
                                break
                        raise ValueError("event line exceeds 16384 bytes")
                    line = raw.decode("utf-8")
                    _check_event_depth(line)
                    event = json.loads(line)
                except (ValueError, RecursionError, MemoryError) as exc:
                    logging.getLogger(__name__).warning(
                        "Skipping malformed event evidence in %s: %s", event_path, exc
                    )
                    yield None
                    continue
                if isinstance(event, dict):
                    yield event
    except (OSError, MemoryError) as exc:
        logging.getLogger(__name__).warning(
            "Cannot read event evidence %s: %s", event_path, exc
        )
        yield None


def owner_birth_at_admission(pid: int) -> str | None:
    """Try three bounded probes before recording an unavailable owner identity."""
    for _ in range(3):
        birth = process_start_time(pid)
        if birth is not None:
            return birth
    logging.getLogger(__name__).warning("owner-identity-unavailable for %s", pid)
    return None


def valid_pid(pid: object) -> bool:
    """Reject non-pids before calling host APIs that convert to signed pid_t."""
    return type(pid) is int and 1 < pid <= _MAX_PID


def attempt_from_events(log_file: Path | None) -> list[int]:
    """Read every bounded spawn claim; callers must authenticate live targets."""
    return list(attempt_identities_from_events(log_file) or {})


def attempt_identities_from_events(
    log_file: Path | None,
) -> dict[int, tuple[str | None, int | None]] | None:
    """Recover birth and owner claims without granting signal authority."""
    if log_file is None:
        return {}
    event_path = log_file.with_suffix(".events.jsonl")
    pids: dict[int, tuple[str | None, int | None]] = {}
    unavailable = False
    for event in read_event_evidence(event_path):
        if event is None:
            unavailable = True
            continue
        claim = _spawn_identity_claim(event, event_path)
        if claim is not None:
            pid, identity = claim
            pids[pid] = identity
    if unavailable:
        return None
    return pids


def _spawn_identity_claim(
    event: dict[str, object],
    event_path: Path,
) -> tuple[int, tuple[str | None, int | None]] | None:
    """Parse bounded claims; record malformed evidence without granting authority."""
    if event.get("type") != "pid":
        return None
    candidate = event.get("pid")
    if not isinstance(candidate, int) or not valid_pid(candidate):
        logging.getLogger(__name__).warning(
            "Skipping invalid spawn pid in %s: %r", event_path, candidate
        )
        return None
    birth, owner = event.get("start_token"), event.get("owner_pid")
    return candidate, (
        birth if isinstance(birth, str) else None,
        owner if isinstance(owner, int) and valid_pid(owner) else None,
    )


def process_start_time(pid: int) -> str | None:
    """Read a stable leader birth identity; elapsed time cannot identify a birth."""
    if not valid_pid(pid):
        return None
    output = ps_output("-o", "lstart=", "-p", str(pid))
    birth = " ".join((output or "").split())
    if re.fullmatch(
        r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2} \d{4}", birth
    ):
        return birth
    return None


class _ProcBsdInfo(ctypes.Structure):
    """Darwin's public proc_bsdinfo layout, including the returned PID."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _darwin_start_token(pid: int) -> str | None:
    """Read PROC_PIDTBSDINFO from the fixed system library, without shelling out."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    probe = libproc.proc_pidinfo
    probe.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    probe.restype = ctypes.c_int
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    if probe(pid, 3, 0, ctypes.byref(info), size) != size or info.pbi_pid != pid:
        return None
    if not info.pbi_start_tvsec or info.pbi_start_tvusec >= 1_000_000:
        return None
    return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"


def _linux_start_token(pid: int) -> str | None:
    """Combine boot time with stat field 22; comm can contain spaces and ')'."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    prefix, _, suffix = stat.rpartition(")")
    if prefix.partition(" (")[0] != str(pid):
        return None
    ticks = suffix.split()[19]  # suffix starts at field 3 (state).
    boot = next(
        line.split()[1]
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime ")
    )
    return f"{boot}+{ticks}" if boot.isdigit() and ticks.isdigit() else None


def process_start_token(pid: int) -> str | None:
    """Read kernel birth identity; unsupported or denied probes are unknown."""
    if not valid_pid(pid):
        return None
    try:
        if sys.platform == "darwin":
            return _darwin_start_token(pid)
        if sys.platform.startswith("linux"):
            return _linux_start_token(pid)
    except (OSError, AttributeError, ValueError, IndexError, StopIteration) as exc:
        logging.getLogger(__name__).warning(
            "Cannot read the kernel start identity of process %s: %s", pid, exc
        )
        return None
    return None


def start_token_matches(pid: int, token: str | None) -> bool:
    """Second-resolution births never authorize signals."""
    return token is not None and process_start_token(pid) == token


#: Linux's boot id: a fresh uuid every boot, readable by any user.
LINUX_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
#: Linux's durable OS installation identity, in the order systemd and D-Bus
#: read it. It stays fixed across reboots.
LINUX_MACHINE_IDS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
MACHINE_ID_PREFIX = "machine-id:"
#: How long macOS ``gethostuuid`` may wait for its answer.
_GETHOSTUUID_WAIT_SECONDS = 5


class _Timespec(ctypes.Structure):
    """``struct timespec``, the wait bound ``gethostuuid`` requires."""

    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def read_raw_boot_id(linux_path: Path = LINUX_BOOT_ID) -> str:
    """This machine's raw boot id; :class:`OSError` when it will not say."""
    if linux_path.exists():
        return linux_path.read_text(encoding="ascii")
    sysctlbyname = getattr(ctypes.CDLL(None), "sysctlbyname", None)
    if sysctlbyname is None:
        raise OSError("neither /proc boot_id nor sysctlbyname is available")
    size = ctypes.c_size_t(64)
    buffer = ctypes.create_string_buffer(size.value)
    if sysctlbyname(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0):
        raise OSError("sysctlbyname(kern.bootsessionuuid) failed")
    return buffer.value.decode("ascii", "replace")


def normalized_boot_id(raw: str) -> str | None:
    """*raw* as 32 lowercase hex digits, or ``None`` for anything else."""
    digits = raw.strip().replace("-", "").lower()
    return digits if re.fullmatch(r"[0-9a-f]{32}", digits) else None


def normalized_machine_id(raw: str) -> str | None:
    """*raw* as a machine id, or ``None``; the all-zero sentinel is no id.

    An unset image can carry 32 zeros, and two such hosts on one ledger would
    read each other's boots as their own earlier ones.
    """
    digits = normalized_boot_id(raw)
    return None if digits is None or digits == "0" * 32 else digits


def _linux_machine_id() -> str:
    """The first valid OS machine id; :class:`OSError` when none is.

    kew mints no stand-in: a host without its own durable id cannot prove
    that a run belongs to an earlier boot of it, so it proves nothing.
    """
    for path in LINUX_MACHINE_IDS:
        try:
            candidate = normalized_machine_id(path.read_text(encoding="ascii"))
        except (OSError, UnicodeError):
            continue
        if candidate is not None:
            return candidate
    raise OSError("no valid machine id in " + ", ".join(map(str, LINUX_MACHINE_IDS)))


def read_raw_machine_id() -> str:
    """This machine's durable OS identity, or :class:`OSError` if unknown."""
    if sys.platform.startswith("linux"):
        return _linux_machine_id()
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        gethostuuid = getattr(libc, "gethostuuid", None)
        if gethostuuid is None:
            raise OSError("gethostuuid is unavailable")
        gethostuuid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gethostuuid.restype = ctypes.c_int
        machine_id = (ctypes.c_ubyte * 16)()
        # The wait must be a real timespec: a NULL pointer fails with EFAULT,
        # and zero fields would wait forever.
        wait = _Timespec(_GETHOSTUUID_WAIT_SECONDS, 0)
        if gethostuuid(machine_id, ctypes.byref(wait)):
            raise OSError(ctypes.get_errno(), "gethostuuid failed")
        return bytes(machine_id).hex()
    raise OSError(f"{sys.platform} has no supported machine identity")


@functools.cache
def _this_boot() -> str | None:
    """This machine's boot id, read once: a process never outlives its boot."""
    try:
        return normalized_boot_id(read_raw_boot_id())
    except (OSError, UnicodeError) as exc:
        logging.getLogger(__name__).warning(
            "cannot read this machine's boot id (%s): no run can be proved "
            "to have died with an earlier boot",
            exc,
        )
        return None


@functools.cache
def _this_machine() -> str | None:
    """Read this OS installation's durable identity once per process."""
    try:
        return normalized_machine_id(read_raw_machine_id())
    except (OSError, UnicodeError) as exc:
        logging.getLogger(__name__).warning(
            "cannot read this machine's durable id (%s): no run can be proved "
            "to belong to this machine",
            exc,
        )
        return None


def machine_boot() -> str | None:
    """This machine and its current boot, as ``<machine id>/<boot id>`` (#2812).

    A pid names a process only within one boot, so a run's owner identity
    records the boot too: a run recorded under an earlier boot of this machine
    had an owner that died with that boot. The machine comes first because a
    boot id alone cannot tell an earlier boot of this machine from a second
    machine writing the same store. ``None`` when the boot id is unreadable.
    """
    machine = _this_machine()
    boot = _this_boot()
    if machine is None or boot is None:
        return None
    return f"{MACHINE_ID_PREFIX}{machine}/{boot}"


def from_earlier_boot(recorded: str | None) -> bool:
    """Whether *recorded* (a :func:`machine_boot`) is an earlier boot of this
    machine — whose every process is gone, and whose pids may since name
    strangers. A run with no recorded boot, another machine's boot, or a
    machine that cannot read its own boot proves nothing."""
    current = machine_boot()
    if not recorded or current is None:
        return False
    machine, separator, boot = recorded.rpartition("/")
    own_machine, _, own_boot = current.rpartition("/")
    durable_machine = machine.removeprefix(MACHINE_ID_PREFIX)
    return bool(
        separator
        and machine.startswith(MACHINE_ID_PREFIX)
        and normalized_machine_id(durable_machine) == durable_machine
        and normalized_boot_id(boot) == boot
        and machine == own_machine
        and boot != own_boot
    )


def from_another_machine(recorded: str | None) -> bool:
    """Whether *recorded* (a :func:`machine_boot`) names a machine other than
    this one, whose pids this machine's process table cannot speak for.

    Only a well-formed ``machine-id:`` record proves another machine; a run
    with no recorded boot, a legacy record, or a machine that cannot read its
    own identity proves nothing, and is probed as before."""
    current = machine_boot()
    if not recorded or current is None:
        return False
    machine, separator, boot = recorded.rpartition("/")
    own_machine, _, _ = current.rpartition("/")
    durable_machine = machine.removeprefix(MACHINE_ID_PREFIX)
    return bool(
        separator
        and machine.startswith(MACHINE_ID_PREFIX)
        and normalized_machine_id(durable_machine) == durable_machine
        and normalized_boot_id(boot) == boot
        and machine != own_machine
    )


def birth_matches(pid: int, birth: str | None) -> bool:
    """Check the secondary birth field for spawn registration, never signalling."""
    return birth is not None and process_start_time(pid) == birth


def attempt_groups(attempt_pgid: int | None, log_file: Path | None) -> list[int]:
    """A registered attempt is authoritative; recover all claims only if absent."""
    if attempt_pgid is not None:
        return [attempt_pgid]
    return attempt_from_events(log_file)


def host_stop_is_dead(
    pgid: int | None,
    owner_pid: int | None,
    attempt_pgid: int | None,
    log_file: Path | None,
    owner_start_time: str | None = None,
    owner_ppid: int | None = None,
    owner_command: str | None = None,
    owner_start_token: str | None = None,
    *,
    snapshot: OwnerSnapshot | None = None,
) -> bool:
    """Require exclusive owner identity and positively empty attempt groups."""
    if (
        pgid is None
        or owner_alive(
            pgid,
            owner_pid,
            owner_start_time,
            owner_ppid,
            owner_command,
            owner_start_token,
            snapshot=snapshot,
        )
        is not False
    ):
        return False
    return attempts_are_dead(attempt_pgid, log_file)


def attempts_are_dead(attempt_pgid: int | None, log_file: Path | None) -> bool:
    """Require positive death evidence for every registered or recovered attempt."""
    attempts = attempt_groups(attempt_pgid, log_file)
    if not attempts:
        return attempt_identities_from_events(log_file) == {}
    return all(group_alive(pid) is False for pid in attempts)


def exclusive_owner_pid() -> int:
    """Create an exclusive dispatch group before admitting any run."""
    pid = os.getpid()
    try:
        if os.getpgrp() != pid:
            os.setpgid(0, 0)
        if os.getpgrp() != pid:
            raise OSError("dispatch still belongs to an inherited group")
    except OSError as exc:
        raise RuntimeError(
            f"Cannot establish an exclusive process group for dispatch: {exc}; "
            "refusing admission"
        ) from exc
    refusal = _exclusive_group_refusal(pid)
    if refusal:
        raise RuntimeError(
            "Cannot prove exclusive ownership of the shared foreground group; "
            "run kew without a pipeline, or under setsid; refusing admission: "
            f"{refusal}"
        )
    return pid


def _exclusive_group_refusal(pid: int) -> str:
    """Allow descendants; retry unavailable snapshots at one-second intervals.

    The per-attempt budget is generous on purpose: ``ps -axo`` walks the whole
    process table and takes seconds on a loaded host, and a refused admission
    under load would block every dispatch (observed at 0.5 s in the gates).
    """
    for attempt in range(3):
        started = time.monotonic()
        table = _membership_snapshot(timeout=5.0)
        if table is not None and pid in table and table[pid].group == pid:
            return _foreign_peer(pid, table)
        if attempt < 2:
            time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))
    return "scan unavailable"


def _foreign_peer(pid: int, table: dict[int, _ProcessMembership]) -> str:
    parents = {member: entry.parent for member, entry in table.items()}
    for member, entry in table.items():
        if entry.group != pid or entry.state.startswith("Z") or member == pid:
            continue
        if not _descends_from(member, pid, parents):
            return f"peer {member} {entry.command}"
    return ""


@dataclass(frozen=True)
class _ProcessMembership:
    parent: int
    group: int
    state: str
    command: str


def _membership_snapshot(
    *, timeout: float = 10
) -> dict[int, _ProcessMembership] | None:
    """Read all parent links, including parents outside the dispatch group."""
    output = ps_output("-axo", "pid=,ppid=,pgid=,stat=,comm=", timeout=timeout)
    if output is None or not output.strip():
        return None
    table = {}
    for line in output.splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5 or not all(part.isdigit() for part in parts[:3]):
            return None
        table[int(parts[0])] = _ProcessMembership(
            int(parts[1]), int(parts[2]), parts[3], Path(parts[4]).name
        )
    return table


def group_absent(pgid: int) -> bool:
    """Prove complete absence for the age-gated backstop, without granting identity."""
    if not valid_pid(pgid):
        return False
    table = _membership_snapshot()
    return table is not None and all(entry.group != pgid for entry in table.values())


@dataclass(frozen=True)
class OwnerSnapshot:
    """A complete owner table, or explicit scan failure."""

    table: dict[int, ProcessIdentity] | None


def owner_snapshot() -> OwnerSnapshot:
    return OwnerSnapshot(_identity_snapshot())


def owner_alive(
    pgid: int,
    owner_pid: int | None,
    owner_start_time: str | None = None,
    owner_ppid: int | None = None,
    owner_command: str | None = None,
    owner_start_token: str | None = None,
    owner_boot_id: str | None = None,
    *,
    snapshot: OwnerSnapshot | None = None,
) -> bool | None:
    """Probe only a recorded exclusive owner; missing identity stays unknown.

    An owner recorded under an earlier boot of this machine is dead whatever
    its pid names now (#2812). One recorded on another machine is unknown:
    this machine's process table says nothing about that machine's pids.
    """
    if not valid_pid(pgid) or owner_pid != pgid:
        return None
    if from_earlier_boot(owner_boot_id):
        return False
    if from_another_machine(owner_boot_id):
        return None
    if owner_start_time is None and owner_start_token is None:
        return None
    table = (snapshot if snapshot is not None else owner_snapshot()).table
    if table is None:
        return None
    entry = table.get(pgid)
    if entry is None:
        return False
    return _owner_entry_alive(
        entry, pgid, owner_start_time, owner_start_token, owner_command
    )


def _owner_entry_alive(
    entry: ProcessIdentity,
    pgid: int,
    birth: str | None,
    token: str | None,
    command: str | None,
) -> bool | None:
    if entry.group != pgid or entry.state.startswith("Z"):
        return False
    birth_alive = _owner_birth_alive(pgid, birth, token, entry.birth)
    if birth_alive is not True:
        return birth_alive
    # PPID is reparenting evidence, never death evidence. Command drift alone
    # also cannot prove death (exec preserves birth); retain uncertain rows.
    if command is not None and entry.command != command:
        return None
    return True


def _owner_birth_alive(
    pid: int, birth: str | None, token: str | None, live_birth: str
) -> bool | None:
    live_token = process_start_token(pid)
    if live_token is not None:
        return None if token is None else live_token == token
    if token is not None:
        return None
    return None if birth is None else live_birth == birth


def ps_output(*args: str, timeout: float = 10) -> str | None:
    """Read ps through the executable allowlist; failures remain unknown."""
    try:
        result = run(["ps", *args], check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logging.getLogger(__name__).warning("Cannot inspect process identity: %s", exc)
        return None
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class GroupMember:
    """One non-zombie process in a recorded group."""

    pid: int
    command: str


def group_members(pgid: int) -> list[GroupMember] | None:
    """Scan surviving members even when the original group leader exited."""
    output = ps_output("-axo", "pid=,pgid=,stat=,comm=")
    if output is None or not output.strip():
        return None
    members = []
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        if int(parts[1]) == pgid and not parts[2].startswith("Z"):
            members.append(GroupMember(int(parts[0]), Path(parts[3]).name))
    return members


def group_member_births(pgid: int) -> dict[int, str] | None:
    """Read group membership and births together to avoid separate-probe PID reuse."""
    output = ps_output("-axo", "pid=,pgid=,stat=,lstart=")
    if output is None or not output.strip():
        return None
    identities = {}
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        if int(parts[1]) == pgid and not parts[2].startswith("Z"):
            birth = " ".join(parts[3].split())
            if not re.fullmatch(
                r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2} \d{4}", birth
            ):
                return None
            identities[int(parts[0])] = birth
    return identities


@dataclass(frozen=True)
class ProcessIdentity:
    """Parentage and birth observed together in one host snapshot."""

    parent: int
    group: int
    state: str
    birth: str
    command: str
    start_token: str | None = None

    def token(self) -> str:
        """Retain birth, parent, and command without changing the JSON value shape."""
        return json.dumps(
            [self.birth, self.parent, self.command, self.group, self.start_token]
        )


def process_identity(pid: int) -> ProcessIdentity | None:
    before = process_start_token(pid)
    table = _identity_snapshot()
    entry = table.get(pid) if table is not None else None
    after = process_start_token(pid)
    if entry is None or before != after:
        return None
    return replace(entry, start_token=after)


class OwnerIdentityFields(TypedDict, total=False):
    owner_start_time: str
    owner_start_token: str | None
    owner_ppid: int
    owner_command: str
    owner_boot_id: str | None


class HostIdentityUnavailable(RuntimeError):
    """Host execution lacks the kernel identity required for later termination."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"{reason}: {sys.platform} cannot provide verified process identity; "
            "host execution requires readable kernel process identity; not launched"
        )


def identity_at_admission(
    pid: int, *, require_token: bool = False
) -> OwnerIdentityFields:
    """Capture all signal identity fields from the same host snapshot."""
    boot = machine_boot()
    for _ in range(3):
        entry = process_identity(pid)
        if (
            entry is not None
            and entry.group == pid
            and not entry.state.startswith("Z")
            and (not require_token or entry.start_token is not None)
        ):
            return {
                "owner_start_time": entry.birth,
                "owner_start_token": entry.start_token,
                "owner_ppid": entry.parent,
                "owner_command": entry.command,
                "owner_boot_id": boot,
            }
    logging.getLogger(__name__).warning("owner-identity-unavailable for %s", pid)
    if require_token:
        raise HostIdentityUnavailable("owner-identity-unavailable")
    return {"owner_boot_id": boot}


class ProcessIdentityFields(OwnerIdentityFields, total=False):
    owner_pgid: int


def identity_of_process(pid: int) -> ProcessIdentityFields:
    """The identity of process *pid* itself, whether or not it leads its group.

    Its kernel birth token, read without ``ps`` (:func:`process_start_token`),
    is what :func:`process_alive` later tells this process from a reused pid
    by. A ``ps`` snapshot adds the start time, parent and command when it
    answers; when it does not, the token alone still identifies the process.
    The group id is added only when *pid* leads its group, as the one target a
    group signal could name; it never decides liveness.
    """
    fields: ProcessIdentityFields = {"owner_boot_id": machine_boot()}
    token = process_start_token(pid)
    if token is not None:
        fields["owner_start_token"] = token
    entry = _live_snapshot_entry(pid, token)
    if entry is not None:
        fields["owner_start_time"] = entry.birth
        fields["owner_ppid"] = entry.parent
        fields["owner_command"] = entry.command
    if _leads_group(pid):
        fields["owner_pgid"] = pid
    if token is None and entry is None:
        logging.getLogger(__name__).warning("process-identity-unavailable for %s", pid)
    return fields


def _live_snapshot_entry(pid: int, token: str | None) -> ProcessIdentity | None:
    """*pid*'s ``ps`` entry, read under the same kernel birth, or ``None``."""
    for _ in range(3):
        entry = process_identity(pid)
        if (
            entry is not None
            and not entry.state.startswith("Z")
            and entry.start_token == token
        ):
            return entry
    return None


def _leads_group(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "cannot read the process group of %s (%s): no group target recorded",
            pid,
            exc,
        )
        return False


def pid_exists(pid: int) -> bool:
    """Whether *pid* names a process now, by signal 0; nothing is delivered.

    A process another user owns exists too (``PermissionError``). Only a pid
    no process holds reads ``False``; that, not any later reading of it, is
    proof the recorded process is gone.
    """
    if not valid_pid(pid):
        return False
    gone = False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        gone = True
    except PermissionError:
        pass  # another user's process: it exists
    return not gone


def process_alive(
    pid: int,
    start_time: str | None,
    start_token: str | None,
    command: str | None = None,
    boot_id: str | None = None,
) -> bool | None:
    """Whether the process :func:`identity_of_process` recorded still runs.

    Read by its own birth, never its group: ``False`` once it is gone, a
    zombie, or its pid names a later process or an earlier boot; ``None``
    when its identity was not recorded, it ran on another machine, or nothing
    can be read. A recorded kernel token is read without ``ps``; a ``ps``
    that then fails cannot turn the token's alive answer into ``None``.
    """
    if not valid_pid(pid):
        return None
    if from_earlier_boot(boot_id):
        return False
    if from_another_machine(boot_id):
        return None
    if start_token is not None:
        return _alive_by_token(pid, start_token, command)
    if start_time is None:
        return None
    return _recorded_process_alive(pid, start_time, None, command)


def _alive_by_token(pid: int, token: str, command: str | None) -> bool | None:
    """The kernel's birth token decides; ``ps`` only adds zombie and exec."""
    live = process_start_token(pid)
    if live is None:
        return None if pid_exists(pid) else False
    if live != token:
        return False  # the pid names a later process
    table = _identity_snapshot()
    if table is None:
        return True  # ps could not answer; the token already did
    entry = table.get(pid)
    if entry is None:
        # Gone between the two reads, or listed late: the kernel says which.
        return start_token_matches(pid, token)
    if entry.state.startswith("Z"):
        return False
    if command is not None and entry.command != command:
        return None  # exec keeps the birth; a changed command proves nothing
    return True


def _recorded_process_alive(
    pid: int, start_time: str | None, start_token: str | None, command: str | None
) -> bool | None:
    table = _identity_snapshot()
    if table is None:
        return None
    entry = table.get(pid)
    if entry is None or entry.state.startswith("Z"):
        return False
    alive = _owner_birth_alive(pid, start_time, start_token, entry.birth)
    if alive is True and command is not None and entry.command != command:
        return None  # exec keeps the birth; a changed command proves nothing
    return alive


def identity_matches(
    pid: int,
    birth: str | None,
    parent: int | None,
    command: str | None,
    start_token: str | None = None,
) -> bool:
    """Missing evidence never authorizes a signal, including same-second reuse."""
    if birth is None or parent is None or command is None or start_token is None:
        return False
    entry = process_identity(pid)
    return bool(
        entry
        and entry.start_token == start_token
        and entry.group == pid
        and not entry.state.startswith("Z")
        and (entry.birth, entry.parent, entry.command) == (birth, parent, command)
    )


def retained_identity_matches(pid: int, token: str) -> bool:
    entry = process_identity(pid)
    if entry is None or entry.start_token is None or entry.state.startswith("Z"):
        return False
    if entry.token() == token:
        return True
    return _retained_reparenting_matches(entry, token)


def _retained_reparenting_matches(entry: ProcessIdentity, token: str) -> bool:
    """Accept init adoption only after the retained parent positively exited."""
    try:
        fields = json.loads(token)
    except (ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning(
            "attempt_identity_unverifiable: malformed retained identity: %s", exc
        )
        return False
    if not isinstance(fields, list) or len(fields) != 5 or type(fields[1]) is not int:
        return False
    parent = fields[1]
    return (
        replace(entry, parent=parent).token() == token
        and entry.parent in {1, entry.group}
        and process_exited(parent)
    )


def _identity_snapshot() -> dict[int, ProcessIdentity] | None:
    """Read births and ancestry from a single ps invocation."""
    output = ps_output("-axo", "pid=,ppid=,pgid=,stat=,lstart=,comm=")
    if output is None or not output.strip():
        return None
    table = {}
    for line in output.splitlines():
        parts = line.split(None, 9)
        if len(parts) != 10 or not all(p.isdigit() for p in parts[:3]):
            return None
        birth = " ".join(parts[4:9])
        if not re.fullmatch(
            r"[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2} \d{4}", birth
        ):
            return None
        table[int(parts[0])] = ProcessIdentity(
            int(parts[1]), int(parts[2]), parts[3], birth, Path(parts[9]).name
        )
    return table


def group_member_identities(pgid: int) -> dict[int, str] | None:
    """Retain kernel tokens with mutable fields before the first signal."""
    members = group_members(pgid)
    if members is None:
        return None
    identities = {}
    for member in members:
        entry = process_identity(member.pid)
        if entry is None or entry.start_token is None or entry.group != pgid:
            return None
        identities[member.pid] = entry.token()
    return identities


def authenticated_group_members(
    pgid: int, retained: dict[int, str]
) -> tuple[dict[int, str], list[int]] | None:
    """Authorize survivors through retained kernel identities or fresh ancestry."""
    table = _identity_snapshot()
    if table is None:
        return None
    members = {
        pid: entry
        for pid, entry in table.items()
        if entry.group == pgid and not entry.state.startswith("Z")
    }
    authenticated = {}
    for pid, snapshot in members.items():
        entry = process_identity(pid)
        if entry is None or not _identity_agrees(entry, snapshot):
            continue
        if _retained_ancestor(pid, retained, table):
            authenticated[pid] = entry.token()
    return authenticated, sorted(members.keys() - authenticated.keys())


def _identity_agrees(entry: ProcessIdentity, snapshot: ProcessIdentity) -> bool:
    """Match a live token-backed probe to its snapshot, ignoring run state.

    Scheduler state (R/S and its flags) moves between two ps reads of a live
    process; ancestry, group, birth, and command must still agree.
    """
    if entry.start_token is None or entry.state.startswith("Z"):
        return False
    return replace(entry, start_token=None, state=snapshot.state) == snapshot


def _retained_ancestor(
    pid: int, retained: dict[int, str], table: dict[int, ProcessIdentity]
) -> bool:
    """Stop at reuse, changed mutable fields, missing parents, or cycles."""
    seen: set[int] = set()
    while pid not in seen:
        seen.add(pid)
        entry = table.get(pid)
        if entry is None:
            return False
        if pid in retained:
            return retained_identity_matches(pid, retained[pid])
        pid = entry.parent
    return False


def group_alive(pgid: int) -> bool | None:
    """Require an empty group or only zombies before reporting death.

    A refused zero signal is not an answer, only a missing one: macOS answers
    EPERM for a group whose members have all exited and not yet been reaped,
    because a zombie keeps no credentials to check the signal against, where
    Linux answers ESRCH (#2436). Reading that refusal as "unknown" left every
    caller on macOS unable to prove the death of a group it had just stopped —
    the stop burned its whole grace window probing a group that was already
    gone, then refused to fall back to the owner, and the row stayed pending as
    unverifiable. So resolve the refusal through the member scan below, which
    already excludes zombies: an empty group is the same death either errno
    means, and a refusal from a group another user now owns still reads alive
    off its surviving members.
    """
    if not valid_pid(pgid):
        return None
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Not an answer; the member scan below gives one.
    except OSError:
        return None
    members = group_members(pgid)
    return None if members is None else bool(members)


def _kew_entrypoint(command: str) -> bool:
    """Recognize kew as the executed entry point, never an arbitrary argument."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable == "kew":
        return True
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return False
    # Interpreter flags may precede the script or module. Stop at executable
    # code (-c), which can carry a misleading trailing argument named kew.
    args = argv[1:]
    while args and args[0] in {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO"}:
        args = args[1:]
    if args[:2] == ["-m", "kew"]:
        return True
    return bool(args and Path(args[0]).name == "kew")


def owner_signallable(
    pgid: int,
    owner_pid: int | None,
    birth: str | None = None,
    parent: int | None = None,
    command: str | None = None,
    start_token: str | None = None,
) -> bool:
    """Verify the exclusive owner and its kew entry point before signalling."""
    if owner_pid != pgid or not identity_matches(
        pgid, birth, parent, command, start_token
    ):
        return False
    output = ps_output("-ww", "-o", "pgid=,stat=,args=", "-p", str(pgid))
    if output is None:
        return False
    parts = output.strip().split(None, 2)
    return (
        len(parts) == 3
        and parts[0] == str(pgid)
        and not parts[1].startswith("Z")
        and _kew_entrypoint(parts[2])
    )


def recovered_attempt_signallable(
    pgid: int,
    owner_pid: int | None,
    birth: str | None = None,
    parent: int | None = None,
    command: str | None = None,
    start_token: str | None = None,
) -> bool:
    """Authenticate a recovered group through its live, verified kew owner.

    An events file cannot grant signal authority. At least one group member
    must descend from the recorded exclusive owner in the host process table.
    Lost ancestry remains unresolved, including children reparented after death.
    """
    if owner_pid is None or not owner_signallable(
        owner_pid, owner_pid, birth, parent, command, start_token
    ):
        return False
    return attempt_descends_from(pgid, owner_pid)


def attempt_descends_from(pgid: int, owner_pid: int) -> bool:
    """Authenticate the leader's ancestry from a read-only host snapshot."""
    table = _parent_table()
    if table is None:
        return False
    parents = {pid: entry[0] for pid, entry in table.items()}
    leader = table.get(pgid)
    return (
        leader is not None
        and leader[1] == pgid
        and not leader[2].startswith("Z")
        and _descends_from(pgid, owner_pid, parents)
    )


def process_exited(pid: int) -> bool:
    """Prove a process absent or zombie, independently of its surviving group."""
    table = _parent_table()
    return table is not None and (pid not in table or table[pid][2].startswith("Z"))


def _parent_table() -> dict[int, tuple[int, int, str]] | None:
    """Read parent relationships, groups, and states from one process snapshot."""
    output = ps_output("-axo", "pid=,ppid=,pgid=,stat=")
    if output is None:
        return None
    table = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 4 or not all(part.isdigit() for part in parts[:3]):
            return None
        pid, parent, group = map(int, parts[:3])
        table[pid] = (parent, group, parts[3])
    return table


def _descends_from(pid: int, owner_pid: int, parents: dict[int, int]) -> bool:
    """Follow a bounded parent chain without trusting names or event content."""
    seen = set()
    while pid in parents and pid not in seen:
        seen.add(pid)
        pid = parents[pid]
        if pid == owner_pid:
            return True
    return False
