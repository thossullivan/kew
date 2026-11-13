"""Processes a test leaves behind in the worker's own process group (#2563).

Admission (:func:`kew.process_groups.exclusive_owner_pid`) makes the pytest
worker the leader of its own group and then refuses to run while any process in
that group is not its descendant. A test whose child outlives it — a ``sleep``
orphaned when a ceiling killed only the ``sh`` above it — is exactly such a
peer, so every later admission on that worker refused, and which tests failed
depended on which worker the leaking test happened to land on.

The guard in ``tests/conftest.py`` reads the group before and after each test
and fails the test that left something in it, naming the process. Reading is
off ``/proc`` where there is one, because a whole-table ``ps`` per test is
seconds a run on a loaded host; ``ps`` is the fallback elsewhere.

Every function it calls is bound here, at import, before any test runs: the
guard reads the group around tests that replace ``os.getpgrp``, ``os.kill``,
``/proc`` reads, ``subprocess.run``, ``subprocess.Popen`` or the clock with
fakes, and must neither consume their fakes nor be fooled by them.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time

_PROC = "/proc"
_getpgrp = os.getpgrp
_getpid = os.getpid
_kill = os.kill
_waitpid = os.waitpid
_listdir = os.listdir
_isdir = os.path.isdir
_open = open
_Popen = subprocess.Popen
# Resolved now: a test may empty ``PATH``, and the guard reads after it.
_PS = shutil.which("ps") or "ps"
_monotonic = time.monotonic
_sleep = time.sleep


def _proc_members(pgid: int) -> dict[int, str]:
    members: dict[int, str] = {}
    for name in _listdir(_PROC):
        if not name.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError, IndexError):
            with _open(f"{_PROC}/{name}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            # ``comm`` is parenthesised and may hold spaces; the fields after
            # its closing paren are fixed: state, ppid, pgrp, ...
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            state, _ppid, pgrp = stat[stat.rindex(")") + 2 :].split()[:3]
            if int(pgrp) == pgid and state != "Z":
                members[int(name)] = comm
    return members


def _ps_members(pgid: int) -> dict[int, str]:
    # ``ps -ax`` lists itself: run in the caller's group, every read would name
    # its own probe as a member that was not there before. ``Popen`` itself,
    # not ``run``: ``run`` looks ``Popen`` up when called, so a test's wrapper
    # of it would see (and might keep) the guard's probe.
    with _Popen(  # noqa: S603 - fixed argv, test support only
        [_PS, "-axo", "pid=,pgid=,stat=,comm="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as probe:
        try:
            listing, _ = probe.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            probe.kill()
            raise
    if probe.returncode:
        raise subprocess.CalledProcessError(probe.returncode, probe.args)
    members: dict[int, str] = {}
    for line in listing.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and int(fields[1]) == pgid and "Z" not in fields[2]:
            members[int(fields[0])] = fields[3]
    return members


def group_members(pgid: int | None = None) -> dict[int, str]:
    """Live (non-zombie) members of *pgid* — this process's group by default."""
    pgid = _getpgrp() if pgid is None else pgid
    if _isdir(_PROC):
        return _proc_members(pgid)
    return _ps_members(pgid)


def _interpreter_helpers() -> set[int]:
    """multiprocessing's own helpers: one per interpreter, alive until it exits.

    The first ``spawn`` or ``forkserver`` start launches a resource tracker (and
    a forkserver) as this process's child and keeps it for the rest of the
    session by design. It is a descendant, which admission allows, and it was
    not the test's to end.
    """
    helpers: set[int] = set()
    tracker = sys.modules.get("multiprocessing.resource_tracker")
    if tracker is not None:
        helpers.add(getattr(tracker._resource_tracker, "_pid", None) or 0)
    forkserver = sys.modules.get("multiprocessing.forkserver")
    if forkserver is not None:
        helpers.add(getattr(forkserver._forkserver, "_forkserver_pid", None) or 0)
    return helpers


def left_behind(
    before: dict[int, str], *, settle_seconds: float = 2.0
) -> dict[int, str]:
    """Members of this process's group that were not in *before*, and stay.

    Polled for a moment rather than read once: a process the test has just
    killed is gone from the table only once its parent, or init, reaps it.
    """
    deadline = _monotonic() + settle_seconds
    while True:
        ignored = {_getpid(), *_interpreter_helpers()}
        extra = {
            pid: comm
            for pid, comm in group_members().items()
            if pid not in before and pid not in ignored
        }
        if not extra or _monotonic() >= deadline:
            return extra
        _sleep(0.05)


def reap(pids: dict[int, str]) -> None:
    """``SIGKILL`` each of *pids*, so the next test on this worker starts clean."""
    for pid in pids:
        with contextlib.suppress(OSError):
            _kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):  # not ours to wait on: init reaps it
            _waitpid(pid, 0)


def describe(pids: dict[int, str]) -> str:
    return ", ".join(f"{pid} {comm}" for pid, comm in sorted(pids.items()))
