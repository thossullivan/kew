"""Stub ``sudo``, ``busctl``, ``systemctl`` and ``flock`` for a job's scope (#2876).

A verify job is moved into a root-owned systemd scope, and a stop kills the
whole scope. Neither the sandbox nor a developer machine gives a test a
cgroup it can create, so these stubs stand in for one:

- ``sudo`` runs its command as this user, with every ``/run/`` path moved
  under the test's own directory — root's ``/run``, where the scope record
  lives. Root owns that directory, so outside ``sudo`` the stand-in and
  everything in it lose the owner's write bit: a direct write fails as it
  would for the login, and the group and other bits are whatever the command
  under ``sudo`` left them.
- ``busctl ... StartTransientUnit <unit> ... PIDs au 1 <pid>`` puts *pid* in
  the scope. Membership is what a cgroup's is: inherited by every descendant,
  ``setsid`` or not. The stand-in is a file of its own the test starts the
  job holding open (:func:`start_in_cgroup`), a descriptor each descendant
  inherits the same way; the scope's members are the processes holding that
  file. The stub tools that start commands of their own (``sudo``, ``flock``)
  pass it on, as a cgroup is passed on; ``docker`` does not, because its
  containers are the daemon's children. A pid holding no such file is a scope
  of that pid alone. Linux and macOS read who holds it in their own way
  (:mod:`support.process_facts`).
- ``systemctl is-active``, ``show --property=LoadState --property=ActiveState``
  and ``kill`` answer for ``*.scope`` units from that membership, and
  ``list-units <pattern>`` lists each matching scope that still has a member.
  A scope with no member left is collected, as a transient scope started with
  ``CollectMode=inactive-or-failed`` is: systemd unloads it, and ``show`` then
  fails with *could not be found*. Any other unit (the power-off timers) is
  answered with success, as the plain stub before this one did.

``KILL_IGNORED`` in the state directory makes ``systemctl kill`` a no-op, for
the scope that survives its kill; ``SHOW_FAILS`` makes ``show`` fail as a
manager that cannot be reached does, for a scope whose end is unknown.
``START_BLOCKED`` holds ``busctl`` before it starts the scope, writing
``START_WAITING`` once it is held, for a scope recorded but not yet started.

``flock LOCKFILE COMMAND...`` takes a real exclusive lock on *LOCKFILE*, as
``flock(1)`` does, and writes ``LOCK_WAITED`` beside the state once it has
had to wait for another holder (#2980).
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from support.process_facts import stub_prelude

#: The directory in the stubs' state that holds one file per cgroup.
CGROUPS = "cgroups"

#: The lowest descriptor a job holds its cgroup's file on: above any a shell
#: script redirects by number, so no ``exec 3>...`` in a job drops it. A job's
#: Python children stay in the cgroup only if started with ``close_fds=False``.
_CGROUP_FD_FLOOR = 64

#: A file in the state directory that makes ``systemctl kill`` kill nothing.
KILL_IGNORED = "kill-ignored"

#: A file in the state directory that makes ``systemctl show`` fail to answer.
SHOW_FAILS = "show-fails"

#: A file in the state directory that holds ``busctl`` before the scope starts.
START_BLOCKED = "start-blocked"

#: Written in the state directory once ``busctl`` is held by ``START_BLOCKED``.
START_WAITING = "start-waiting"

#: Written in the state directory once a ``flock`` has waited for its lock.
LOCK_WAITED = "lock-waited"

_FLOCK = """
import fcntl, subprocess, sys
from pathlib import Path
state = Path(sys.argv[0]).resolve().parent.parent / "systemd"
with open(sys.argv[1], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        (state / "@LOCK_WAITED@").touch()
        fcntl.flock(lock, fcntl.LOCK_EX)
    sys.exit(subprocess.run(sys.argv[2:], close_fds=False).returncode)
"""

_SUDO = """
import subprocess, sys
from pathlib import Path
root = Path(sys.argv[0]).resolve().parent.parent / "systemd" / "run"
args = [arg.replace("/run/", f"{root}/") for arg in sys.argv[1:]]
while args[0].startswith("-"):
    args.pop(0)

def owner_may_write(writable):
    for path in [root, *root.iterdir()]:
        mode = path.stat().st_mode
        path.chmod(mode | 0o200 if writable else mode & ~0o200)

owner_may_write(True)
try:
    code = subprocess.run(args, close_fds=False).returncode
finally:
    owner_may_write(False)
sys.exit(code)
"""

_MEMBERS = """
import os, signal, sys
from pathlib import Path
from support.process_facts import alive, file_identity, holders, open_files
state = Path(sys.argv[0]).resolve().parent.parent / "systemd"
scopes = state / "scopes"
cgroups = state / "@CGROUPS@"

def members(unit):
    record = scopes / unit
    if not record.exists():
        return []
    kind, value = record.read_text().split(" ", 1)
    if kind == "tag":
        return holders(cgroups / value)
    pid = int(value)
    return [pid] if pid != os.getpid() and alive(pid) else []
"""

_BUSCTL = """
import time
argv = sys.argv[1:]
if (state / "@START_BLOCKED@").exists():
    (state / "@START_WAITING@").touch()
    while (state / "@START_BLOCKED@").exists():
        time.sleep(0.02)
unit = argv[argv.index("ssa(sv)a(sa(sv))") + 1]
pid = argv[argv.index("au") + 2]
held = open_files(int(pid))
if held is None:
    sys.exit(1)
known = sorted(cgroups.iterdir()) if cgroups.exists() else []
tag = [group.name for group in known if file_identity(group) in held]
scopes.mkdir(parents=True, exist_ok=True)
(scopes / unit).write_text(f"tag {tag[0]}" if tag else f"pid {pid}")
"""

_SYSTEMCTL = """
import fnmatch
argv = sys.argv[1:]
if argv[0] == "list-units":
    pattern = argv[-1]
    for record in sorted(scopes.glob("*.scope")) if scopes.exists() else []:
        if fnmatch.fnmatch(record.name, pattern) and members(record.name):
            print(f"{record.name} loaded active running Scope")
    sys.exit(0)
unit = argv[-1]
if not unit.endswith(".scope"):
    sys.exit(0)
live = members(unit)
if argv[0] == "is-active":
    sys.exit(0 if live else 3)
if argv[0] == "show":
    if (state / "@SHOW_FAILS@").exists():
        sys.exit("Failed to connect to bus: No such file or directory")
    if not live:
        (scopes / unit).unlink(missing_ok=True)
        sys.exit(f"Unit {unit} could not be found.")
    print("LoadState=loaded\\nActiveState=active")
if argv[0] == "kill" and not (state / "@KILL_IGNORED@").exists():
    for pid in live:
        os.kill(pid, signal.SIGKILL)
"""


def install_fake_systemd(bin_dir: Path) -> Path:
    """Write the four stubs into *bin_dir*; return the stand-in for ``/run``.

    The stubs keep their state in a ``systemd`` directory beside *bin_dir*.
    """
    shebang = f"#!{sys.executable}\n" + stub_prelude()
    members = _MEMBERS.replace("@CGROUPS@", CGROUPS)
    scripts = {
        "sudo": _SUDO,
        "busctl": members
        + _BUSCTL.replace("@START_BLOCKED@", START_BLOCKED).replace(
            "@START_WAITING@", START_WAITING
        ),
        "flock": _FLOCK.replace("@LOCK_WAITED@", LOCK_WAITED),
        "systemctl": members
        + _SYSTEMCTL.replace("@KILL_IGNORED@", KILL_IGNORED).replace(
            "@SHOW_FAILS@", SHOW_FAILS
        ),
    }
    for tool, body in scripts.items():
        stub = bin_dir / tool
        stub.write_text(shebang + body, encoding="utf-8")
        stub.chmod(0o755)
    run = bin_dir.parent / "systemd" / "run"
    run.mkdir(parents=True, exist_ok=True)
    return run


def start_in_cgroup(
    bin_dir: Path, argv: Sequence[str], **popen: Any
) -> subprocess.Popen[Any]:
    """Start *argv* in a fresh stand-in cgroup of the stubs in *bin_dir*.

    The process holds the cgroup's file open on a descriptor no other process
    is handed; *popen* is passed to :class:`subprocess.Popen` as given.
    """
    cgroups = bin_dir.parent / "systemd" / CGROUPS
    cgroups.mkdir(parents=True, exist_ok=True)
    group = cgroups / uuid.uuid4().hex
    group.touch()
    opened = os.open(group, os.O_RDONLY)
    try:
        held = fcntl.fcntl(opened, fcntl.F_DUPFD_CLOEXEC, _CGROUP_FD_FLOOR)
    finally:
        os.close(opened)
    try:
        return subprocess.Popen(argv, pass_fds=(held,), **popen)  # noqa: S603
    finally:
        os.close(held)
