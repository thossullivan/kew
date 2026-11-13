"""``local`` — run the job on this machine. The default, and needs no cloud.

The bundle is cloned into a fresh directory under kew's work root
(:func:`kew.work_dirs.work_root`) rather than run in place, so a local job sees
exactly what a remote one sees: a fresh checkout built from the bundle, and
nothing else of the operator's working tree.

The job runs in its own session, and its ``TMPDIR`` is ``<workspace>/tmp``. A
job that outruns its ceiling has its whole process group stopped — TERM, then
KILL after :data:`KILL_GRACE_SECONDS` — and kew waits for the group to empty
before the workspace is removed, so nothing the job started keeps writing into
a checkout that is being deleted, and every tree the job made under its
``TMPDIR`` (the held-out repository, a mutation tree) goes with the workspace.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from kew import process_groups, subprocess_utils, work_dirs
from kew.hosts.environment import job_environment
from kew.hosts.errors import HostError, HostNotProvisioned, HostTimeout
from kew.hosts.provider import HostProvider, Job

_CHECKOUT_NAME = "checkout"
_TMP_NAME = "tmp"

#: Seconds a timed-out job's group has between TERM and KILL.
KILL_GRACE_SECONDS = 5.0

#: Seconds kew waits for a KILLed group to leave the process table.
GROUP_EXIT_SECONDS = 30.0

_GROUP_POLL_SECONDS = 0.05


class LocalProvider(HostProvider):
    """A host that is this machine."""

    name = "local"

    #: This machine is the operator machine: whatever private build input it
    #: holds, the job can use without anything leaving it.
    builds_private_images = True

    def __init__(self, *, lock_dir: Path, root: Path | None = None) -> None:
        super().__init__(lock_dir=lock_dir)
        self._root = None if root is None else Path(root)
        self._workspace: Path | None = None
        self._group_outlived_kill = False

    @property
    def lock_key(self) -> str:
        """This machine is one host, however many providers address it."""
        return "local"

    @property
    def checkout(self) -> Path | None:
        """Where the shipped bundle was cloned, or ``None`` before ``ship()``."""
        return None if self._workspace is None else self._workspace / _CHECKOUT_NAME

    def _ensure_up(self) -> None:
        """A local host is already running; what it needs is ``git`` and a shell."""
        for tool in ("git", "sh"):
            if subprocess_utils.ensure_tool(tool) is None:
                raise HostError(
                    f"the local host cannot run a job without {tool} on PATH"
                )

    def _ship(self, bundle_path: Path) -> None:
        """Clone the bundle into a fresh directory under the work root."""
        bundle = Path(bundle_path)
        if not bundle.is_file():
            raise HostError(f"git bundle not found: {bundle}")
        self._release()
        if self._root is None:
            workspace = work_dirs.make_work_dir("kew-host-local-")
        else:
            self._root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="kew-host-local-", dir=self._root))
        try:
            subprocess_utils.run(
                ["git", "clone", "--quiet", str(bundle), _CHECKOUT_NAME],
                cwd=workspace,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise HostError(f"cannot clone {bundle} on the local host: {exc}") from exc
        self._workspace = workspace

    def _execute(self, job: Job) -> tuple[int, str]:
        """Run the job's command through ``sh -c`` in the cloned checkout."""
        checkout = self.checkout
        if checkout is None:
            raise HostNotProvisioned(
                f"local host has no checkout: ship() a bundle before running "
                f"{job.name!r}"
            )
        scratch = checkout.parent / _TMP_NAME
        scratch.mkdir(exist_ok=True)
        environment = job_environment(job.environment)
        environment["TMPDIR"] = str(scratch)
        try:
            proc = subprocess_utils.popen(
                ["sh", "-c", job.command],
                cwd=checkout,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise HostError(
                f"cannot start {job.name!r} on the local host: {exc}"
            ) from exc
        try:
            output, _ = proc.communicate(timeout=job.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._stop(proc)
            raise HostTimeout(
                f"{job.name!r} exceeded its {job.timeout_seconds}s ceiling on the "
                "local host, so it has no exit code"
            ) from exc
        except BaseException:
            self._stop(proc)
            raise
        return proc.returncode, output or ""

    def _stop(self, proc: subprocess.Popen[str]) -> None:
        """Stop the job's group; a group that will not die keeps the workspace."""
        try:
            stop_group(proc)
        except HostError:
            self._group_outlived_kill = True
            raise

    def _release(self) -> None:
        """Remove the workspace. A local host is never stopped.

        A workspace whose job's group outlived SIGKILL is left in place, not
        deleted under a live process; the work-root sweep removes it later.
        """
        if self._workspace is not None:
            if not self._group_outlived_kill:
                shutil.rmtree(self._workspace, ignore_errors=True)
            self._workspace = None
            self._group_outlived_kill = False


def stop_group(proc: subprocess.Popen[str]) -> None:
    """Stop *proc*'s whole session: TERM, KILL after a grace, then wait it out.

    The job is its own group leader (``start_new_session``), so its pid is the
    group id, and a background child the shell left behind (``sleep 300 &``)
    is a member. Only the group's emptiness ends the wait — not the leader's
    exit, which is the first thing to happen — so the caller's ``_release``
    never deletes a checkout something is still running in.
    """
    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=KILL_GRACE_SECONDS)
    _signal_group(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=GROUP_EXIT_SECONDS)
    deadline = time.monotonic() + GROUP_EXIT_SECONDS
    while process_groups.group_alive(pgid) is not False:
        if time.monotonic() >= deadline:
            raise HostError(
                f"the local job's process group {pgid} outlived SIGKILL for "
                f"{GROUP_EXIT_SECONDS:.0f}s, so its workspace was kept"
            )
        _signal_group(pgid, signal.SIGKILL)
        time.sleep(_GROUP_POLL_SECONDS)


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
