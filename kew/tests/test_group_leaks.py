"""The guard that names a process a test leaves in the worker's group (#2563)."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest
from support import group_leaks


def _orphan(*, own_session: bool) -> int:
    """A ``sleep`` whose parent ``sh`` has already exited; returns its pid."""
    shell = subprocess.run(
        ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
        start_new_session=own_session,
    )
    return int(shell.stdout.strip())


def test_an_orphan_left_in_the_group_is_named() -> None:
    before = group_leaks.group_members()
    pid = _orphan(own_session=False)
    try:
        leaked = group_leaks.left_behind(before, settle_seconds=0.2)
        assert leaked.get(pid) == "sleep"
        assert f"{pid} sleep" in group_leaks.describe(leaked)
    finally:
        group_leaks.reap({pid: "sleep"})
    assert group_leaks.left_behind(before) == {}


def test_an_orphan_in_its_own_session_is_not_this_groups() -> None:
    before = group_leaks.group_members()
    pid = _orphan(own_session=True)
    try:
        assert os.getpgid(pid) != os.getpgrp()
        assert group_leaks.left_behind(before, settle_seconds=0.2) == {}
    finally:
        group_leaks.reap({pid: "sleep"})


def test_the_members_read_includes_this_process() -> None:
    assert group_leaks.group_members()[os.getpid()]


def test_a_test_that_starts_nothing_leaves_nothing() -> None:
    before = group_leaks.group_members()
    assert group_leaks.left_behind(before, settle_seconds=0.2) == {}


def test_the_ps_read_does_not_list_its_own_probe() -> None:
    """The reader used where there is no ``/proc`` (macOS) lists no ``ps`` it ran.

    ``ps -ax`` lists itself, so a probe started in this group read as a member
    that was not there before, and every test on the Mac failed naming a
    ``ps``. Every member it returns must still exist once the read is over.
    """
    gone = []
    for pid in group_leaks._ps_members(os.getpgrp()):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            gone.append(pid)
        except PermissionError:
            pass
    assert gone == []


def test_the_ps_read_works_after_a_test_empties_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that makes a tool absent by emptying ``PATH`` still gets read."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert group_leaks._ps_members(os.getpgrp())[os.getpid()]


def test_multiprocessings_own_tracker_is_not_a_leak() -> None:
    """The resource tracker ``spawn`` starts lives for the session by design."""
    before = group_leaks.group_members()
    worker = multiprocessing.get_context("spawn").Process(target=os.getpid)
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == 0
    assert group_leaks.left_behind(before, settle_seconds=0.2) == {}
