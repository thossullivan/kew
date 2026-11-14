"""The host contract: one shape, one job at a time, no credentials.

The gcp provider is driven entirely through a fake ``gcloud`` on PATH. No test
here touches a real cloud: the fake records every argv it is given and answers
from a JSON state file the test writes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import functools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support import group_leaks
from support.fake_docker import INSPECT_FAILS as CONTAINER_INSPECT_FAILS
from support.fake_docker import KILL_IGNORED as CONTAINER_KILL_IGNORED
from support.fake_docker import LISTINGS as CONTAINER_LISTINGS
from support.fake_docker import (
    add_container,
    add_created_container,
    add_pending_container,
    install_fake_docker,
)
from support.fake_gcloud import (
    NOLOGIN_REFUSAL,
    fake_gcloud_on_path,
    launchable,
)
from support.fake_gcloud import (
    read_state as _read_state,
)
from support.fake_gcloud import (
    recorded_calls as _calls,
)
from support.fake_gcloud import (
    write_state as _write_state,
)
from support.fake_systemd import (
    KILL_IGNORED,
    LOCK_WAITED,
    SHOW_FAILS,
    START_BLOCKED,
    START_WAITING,
    install_fake_systemd,
    start_in_cgroup,
)
from support.process_facts import alive as _pid_alive
from support.takeover_ledger import MemoryTakeoverLedger
from typer.testing import CliRunner

from kew import process_groups, subprocess_utils
from kew.cli import app
from kew.config import DispatchConfig, GcpHostConfig, HostsConfig
from kew.hosts import (
    HOST_LOCK_DIR_ENV,
    CredentialVariableRefused,
    GcpProvider,
    HostBusy,
    HostConnectionLost,
    HostError,
    HostInterrupted,
    HostJobDeadlineExceeded,
    HostNotProvisioned,
    HostTimeout,
    Job,
    JobResult,
    LocalProvider,
    VariableNameRefused,
    host_lock_dir,
    job_environment,
    resolve_provider,
)
from kew.hosts import gcp as gcp_module
from kew.hosts.diagnostics import FAIL, GCLOUD_LOGIN_FIX, OK, SKIP, check_hosts
from kew.hosts.environment import REMOTE_PATH, remote_path_prefix
from kew.hosts.gcp import (
    _CANCEL_SHUTDOWN,
    _CONTAINER_SETTLE,
    _LEASE_LOCK,
    _SHUTDOWN_SENTINEL,
    BOOTS_FILE,
    DEADLINE_STOP_UNIT,
    EXIT_MARKER,
    IDLE_STOP_UNIT,
    JOB_SESSION_FILE,
    JOB_SURVIVES_EXIT,
    JOB_UNRECORDED_EXIT,
    LEASE_CHANGED_EXIT,
    LEASE_FILE,
    LEASE_HELD_EXIT,
    SCP_BOUNDARY_FLAGS,
    SCP_KEEPALIVE_FLAGS,
    SSH_BOUNDARY_FLAGS,
    SSH_KEEPALIVE_FLAGS,
    boot_id,
    drop_command,
    drop_holder_command,
    hold_command,
    job_session_record,
    lease_lapsed,
    lease_token,
    marker_command,
    parse_exit_marker,
    parse_lease_token,
    release_command,
    remote_session_environment,
)


@pytest.fixture
def fake_gcloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake ``gcloud`` first on PATH; yields its JSON state file."""
    with fake_gcloud_on_path(tmp_path, monkeypatch) as state:
        yield state


def _write_stub(bin_dir: Path, tool: str, body: str) -> None:
    """Write one launchable stub tool into *bin_dir*."""
    stub = bin_dir / tool
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


#: Where an instance's ``setsid`` and ``date`` are, and a developer's are not
#: always the instance's.
_SYSTEM_PATH = "/usr/bin:/bin"
#: How long a test waits for a job it started to reach a step that must come.
#: A late step still passes and a missing one still fails; only how long a
#: missing one takes to fail depends on it.
_STARTED_SECONDS = 60


@functools.cache
def _date_reads_iso_8601() -> bool:
    """Whether this machine's ``date -d`` reads a timestamp as GNU's does."""
    probe = subprocess.run(
        ["date", "-d", "1970-01-01T00:00:01Z", "+%s"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": _SYSTEM_PATH},
    )
    return (probe.returncode, probe.stdout.strip()) == (0, "1")


def _instance_tools_where_missing(bin_dir: Path) -> None:
    """Stand-ins in *bin_dir* for the instance's util-linux ``setsid`` and its
    GNU ``date -d``, where this machine has neither (macOS).

    The ``setsid`` does what util-linux's does: a process-group leader forks
    first, so the child can lead a new session; the command runs in it,
    holding every descriptor it was handed. The ``date`` reads ``-d STAMP
    +%s``, the one form the lease commands use it in, as GNU's does for
    docker's timestamps; anything else goes to this machine's ``date``.
    """
    if shutil.which("setsid", path=_SYSTEM_PATH) is None:
        _write_stub(
            bin_dir,
            "setsid",
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "if os.getpgrp() == os.getpid() and os.fork():\n"
            "    os._exit(0)\n"
            "os.setsid()\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n",
        )
    if not _date_reads_iso_8601():
        _write_stub(
            bin_dir,
            "date",
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "from datetime import datetime\n"
            "argv = sys.argv[1:]\n"
            "if '-d' not in argv:\n"
            "    os.execv('/bin/date', ['date', *argv])\n"
            "at = argv.index('-d')\n"
            "if argv[:at] + argv[at + 2:] != ['+%s']:\n"
            "    sys.exit(f'date: only -d STAMP +%s is stood in for: {argv}')\n"
            "try:\n"
            "    print(int(datetime.fromisoformat(argv[at + 1]).timestamp()))\n"
            "except ValueError:\n"
            "    sys.exit(f'date: invalid date {argv[at + 1]!r}')\n",
        )


@pytest.fixture
def stub_root(tmp_path: Path) -> Iterator[Path]:
    """A directory whose contents can actually be launched.

    Under ``HOME`` when the pytest temporary filesystem is mounted ``noexec``,
    for the same reason the fake ``gcloud`` goes there: the assertion is about
    the command, not about the sandbox's mount options.
    """
    if launchable(tmp_path):
        yield tmp_path
        return
    fallback = tempfile.mkdtemp(prefix="kew-cancel-probe-", dir=Path.home())
    try:
        yield Path(fallback)
    finally:
        # The stand-in for root's /run is left without its write bit.
        for directory in Path(fallback).rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)
        shutil.rmtree(fallback, ignore_errors=True)


def _settings() -> GcpHostConfig:
    return GcpHostConfig(project="proj", zone="us-central1-a", instance="verify-1")


def _gcp(tmp_path: Path) -> GcpProvider:
    return GcpProvider(
        _settings(),
        lock_dir=tmp_path / "locks",
        sleep=lambda _seconds: None,
        poll_seconds=0.0,
    )


def _commands(state: Path) -> list[str]:
    """Every command the shim was asked to run over ssh, in order."""
    return [
        call[call.index("--command") + 1]
        for call in _calls(state)
        if "--command" in call
    ]


def _bundle(tmp_path: Path) -> Path:
    """A real git bundle, built from a one-commit repository."""
    bundle = tmp_path / "repo.bundle"
    if bundle.is_file():
        return bundle
    repo = tmp_path / "origin"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "init", "--quiet", "-b", "main"], cwd=repo, check=True)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", "seed"], cwd=repo, check=True)
    subprocess.run(
        [*git, "bundle", "create", str(bundle), "--all"], cwd=repo, check=True
    )
    return bundle


def _is_local_job(argv: object) -> bool:
    """Whether *argv* is the ``sh -c <command>`` a local job is run as."""
    return (
        isinstance(argv, list | tuple)
        and len(argv) >= 2
        and os.path.basename(str(argv[0])) == "sh"
        and argv[1] == "-c"
    )


@pytest.fixture(autouse=True)
def _jobs_reaped(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every job a test here starts is in a session of its own, ended in teardown.

    A local job is ``sh -c <command>``, and dash does not exec the last command
    of a ``-c`` string: a ceiling kills the ``sh`` and orphans the ``sleep``
    under it, in the worker's own process group — where every later admission
    on that worker refused it as a foreign peer (#2563). A gcp job's session
    already starts a session of its own; a local one is given one here. Each
    such group is then killed and waited out once the test is done, whatever
    the test did or did not wait for.
    """
    groups: list[subprocess.Popen[Any]] = []

    class _ReapedPopen(subprocess.Popen[Any]):
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            if _is_local_job(args):
                kwargs["start_new_session"] = True
            super().__init__(args, *rest, **kwargs)
            if kwargs.get("start_new_session"):
                groups.append(self)

    monkeypatch.setattr(subprocess, "Popen", _ReapedPopen)
    yield
    deadline = time.monotonic() + 5.0
    for session in groups:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            session.kill()
            session.wait(timeout=5.0)
        while members := group_leaks.group_members(session.pid):
            group_leaks.reap(members)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)


@pytest.fixture(autouse=True)
def _tools_registered() -> None:
    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)


class TestJobResultShape:
    """AC1 — both providers answer with the same JobResult."""

    def test_local_and_gcp_return_the_same_result_shape(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        job = Job(name="verify", command="cat hello.txt")
        local = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        local.ensure_up()
        local.ship(_bundle(tmp_path))
        try:
            local_result = local.run(job)
        finally:
            local.release()

        _write_state(fake_gcloud, job_output="hello\n", job_exit=0)
        remote = _gcp(tmp_path)
        remote_result = remote.run(job)

        assert isinstance(local_result, JobResult)
        assert isinstance(remote_result, JobResult)
        local_fields = [(f.name, f.type) for f in dataclasses.fields(local_result)]
        assert local_fields == [
            (f.name, f.type) for f in dataclasses.fields(remote_result)
        ]
        assert (local_result.exit_code, local_result.ok) == (0, True)
        assert (remote_result.exit_code, remote_result.ok) == (0, True)
        assert "hello" in local_result.log
        assert "hello" in remote_result.log
        assert local_result.seconds >= 0.0 and remote_result.seconds >= 0.0
        assert (local_result.host, remote_result.host) == ("local", "gcp")

    def test_a_failing_job_reports_its_exit_code_on_both_hosts(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        job = Job(name="verify", command="exit 3")
        local = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        local.ensure_up()
        local.ship(_bundle(tmp_path))
        try:
            assert local.run(job).exit_code == 3
        finally:
            local.release()
        _write_state(fake_gcloud, job_exit=3)
        assert _gcp(tmp_path).run(job).exit_code == 3

    def test_an_absent_exit_marker_is_an_error_not_a_success(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, swallow_marker=True)
        with pytest.raises(HostError, match="no exit marker"):
            _gcp(tmp_path).run(Job(name="verify", command="true"))

    def test_parse_exit_marker_strips_the_marker_from_the_log(self) -> None:
        exit_code, log = parse_exit_marker("built fine\nKEW_HOST_EXIT=0\n")
        assert (exit_code, "KEW_HOST_EXIT" in log) == (0, False)
        assert "built fine" in log


class TestGcpLifecycle:
    """AC2 — stopped is started, mid-stop is waited out, a copy retries once."""

    def test_a_stopped_instance_is_started_and_waited_to_running(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, statuses=["TERMINATED"])
        _gcp(tmp_path).ensure_up()
        state = _read_state(fake_gcloud)
        assert state["started"] == 1
        verbs = [call[:3] for call in _calls(fake_gcloud)]
        assert ["compute", "instances", "start"] in verbs

    def test_an_instance_mid_stop_is_waited_out_then_started(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud, statuses=["STOPPING", "STOPPING", "TERMINATED", "RUNNING"]
        )
        _gcp(tmp_path).ensure_up()
        state = _read_state(fake_gcloud)
        assert state["started"] == 1
        describes = [
            call
            for call in _calls(fake_gcloud)
            if call[:3] == ["compute", "instances", "describe"]
        ]
        assert len(describes) >= 3

    def test_a_running_instance_is_not_started(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, statuses=["RUNNING"])
        _gcp(tmp_path).ensure_up()
        assert "started" not in _read_state(fake_gcloud)

    def test_ensure_up_cancels_both_stops_a_host_can_carry(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The idle-stop timer and a `shutdown -h` an older kew may have armed.

        Both are cancelled and the absence of both is proved, because a job
        shipped onto either one dies mid-run for a reason nobody can see.
        """
        _gcp(tmp_path).ensure_up()
        commands = _commands(fake_gcloud)
        assert any("shutdown -c" in command for command in commands)
        assert any("/run/systemd/shutdown/scheduled" in command for command in commands)
        assert any(
            f"systemctl stop {IDLE_STOP_UNIT}.timer" in command for command in commands
        )
        assert any(
            f"is-active {IDLE_STOP_UNIT}.timer" in command for command in commands
        )

    def test_a_pending_shutdown_that_closes_logins_is_waited_out(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2411 — the nologin window is restarted through, not refused.

        A pending `shutdown -h` closes every unprivileged login five minutes
        before it fires, so the ssh that would cancel it is refused by pam.
        No retry gets in: the window ends when the stop does. kew waits the
        stop out, starts the instance again, and the job runs — which is what
        the operator's own scripts did by hand.
        """
        _write_state(
            fake_gcloud,
            statuses=["RUNNING", "STOPPING", "TERMINATED"],
            nologin_until_start=True,
            job_output="verified\n",
        )
        provider = _gcp(tmp_path)
        with provider.claim():
            provider.ensure_up()
            provider.ship(_bundle(tmp_path))
            result = provider.run(Job(name="verify", command="true"))
        assert (result.exit_code, result.log.strip()) == (0, "verified")
        assert _read_state(fake_gcloud)["started"] == 1

    def test_a_host_still_going_down_after_a_restart_is_named_for_that(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """Someone else is stopping it; that is not an unreachable host.

        kew waited the stop out and started the instance, and pam is still
        refusing the login — three refusals, three different things for the
        operator to do, so this one is not folded into "cannot be reached".
        """
        _write_state(
            fake_gcloud,
            statuses=["RUNNING", "STOPPING", "TERMINATED"],
            cancel_exit=255,
            cancel_stderr=NOLOGIN_REFUSAL,
        )
        with pytest.raises(HostError, match="pre-shutdown nologin window") as refused:
            _gcp(tmp_path).ensure_up()
        assert "cannot be reached over ssh" not in str(refused.value)
        assert _read_state(fake_gcloud)["started"] == 1

    def test_the_nologin_refusal_is_handed_back_instead_of_retried(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2411 — pam's refusal is not a cold start's, so it is not retried.

        A connection refused by sshd lifts on the next attempt; a login
        refused by pam lifts only when the stop fires. Retrying the second
        one spends a dozen ssh round-trips learning what the first refusal
        already said, and delays the restart that is the only way through.
        """
        _write_state(
            fake_gcloud,
            statuses=["RUNNING", "STOPPING", "TERMINATED"],
            nologin_until_start=True,
        )
        _gcp(tmp_path).ensure_up()
        disarms = [
            command for command in _commands(fake_gcloud) if "shutdown -c" in command
        ]
        assert (len(disarms), _read_state(fake_gcloud)["started"]) == (2, 1)

    def test_the_wait_for_a_stop_outlasts_the_nologin_window(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2411 — the window is five minutes wide; the wait must beat it.

        `shutdown` writes /run/nologin five minutes before the stop fires, so
        an instance refusing kew's login reads RUNNING for that whole window
        and only then takes the STOPPING -> TERMINATED transition. A budget
        exactly as wide as the window expires one tick before the stop even
        begins, and the instance is never started again.
        """
        slept: list[float] = []
        _write_state(
            fake_gcloud,
            statuses=["RUNNING"] * 70 + ["STOPPING", "TERMINATED"],
            nologin_until_start=True,
        )
        provider = GcpProvider(
            _settings(),
            lock_dir=tmp_path / "locks",
            sleep=slept.append,
            poll_seconds=5.0,
        )
        provider.ensure_up()
        assert sum(slept) > 300.0
        assert _read_state(fake_gcloud)["started"] == 1

    def test_a_stop_that_never_finishes_names_the_stop_and_the_window(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The operator is told what kew was waiting for, not just a status.

        "still RUNNING after N polls" on its own sends them looking at an
        instance that is, as far as they can see, perfectly healthy.
        """
        _write_state(fake_gcloud, statuses=["RUNNING"], nologin_until_start=True)
        with pytest.raises(HostError, match="nologin window") as refused:
            _gcp(tmp_path).ensure_up()
        message = str(refused.value)
        assert "the stop behind that window has not finished" in message
        assert "started" not in _read_state(fake_gcloud)

    def test_a_cold_start_waits_for_sshd_instead_of_refusing_the_host(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """An instance reads RUNNING before it accepts logins (#2411).

        The first contacts after a start are refused by the connection, not
        by the host; reporting that as unreachable refuses a host that is
        merely still booting.
        """
        _write_state(fake_gcloud, statuses=["TERMINATED"], cancel_failures=2)
        _gcp(tmp_path).ensure_up()
        disarms = [
            command for command in _commands(fake_gcloud) if "shutdown -c" in command
        ]
        assert (len(disarms), _read_state(fake_gcloud)["started"]) == (3, 1)

    def test_a_stop_that_will_not_cancel_refuses_the_host(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, cancel_exit=1, cancel_stderr="still armed\n")
        with pytest.raises(HostError, match="still has a stop pending"):
            _gcp(tmp_path).ensure_up()

    def test_an_unreachable_instance_is_not_reported_as_a_shutdown_pending(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """An ssh that never connected proves nothing about the sentinel (#2379).

        ``test`` exits 0 or 1, so a 255 is ssh's own failure. Reading it as
        "the sentinel is present" sent the operator looking for a pending
        stop on an instance that had just booted and had none.

        Waited out first, since a cold start is refused the same way, and
        then named — the readiness wait is a wait, not an endless retry.
        """
        _write_state(
            fake_gcloud, cancel_exit=255, cancel_stderr="ssh: connect refused\n"
        )
        with pytest.raises(HostError, match="cannot be reached over ssh") as refused:
            _gcp(tmp_path).ensure_up()
        assert "is present" not in str(refused.value)
        disarms = [
            command for command in _commands(fake_gcloud) if "shutdown -c" in command
        ]
        assert len(disarms) == 13

    def test_a_copy_that_fails_once_is_retried_once(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, scp_failures=1)
        _gcp(tmp_path).ship(_bundle(tmp_path))
        state = _read_state(fake_gcloud)
        copies = [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "scp"]
        ]
        assert (len(copies), state["copies"]) == (2, 1)

    def test_a_copy_that_fails_twice_is_refused(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, scp_failures=2)
        with pytest.raises(HostError, match="after 2 attempts"):
            _gcp(tmp_path).ship(_bundle(tmp_path))

    def test_the_configured_idle_stop_is_armed_and_closes_no_logins(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2411 — the delay rides a systemd timer, never `shutdown -h +N`.

        `shutdown` writes /run/nologin five minutes before it fires and pam
        refuses every unprivileged login from then on, so the stop armed here
        would lock out the very login the next job needs to cancel it. A
        timer holds the same delay and closes nothing until it fires.
        """
        _gcp(tmp_path).release()
        commands = _commands(fake_gcloud)
        armed = [command for command in commands if "systemd-run" in command]
        assert len(armed) == 1
        assert "--on-active=30m" in armed[0]
        assert f"--unit={IDLE_STOP_UNIT}" in armed[0]
        assert "systemctl poweroff" in armed[0]
        assert not any("shutdown -h" in command for command in commands)

    def test_a_missing_bundle_is_named_before_any_gcloud_call(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        with pytest.raises(HostError, match="git bundle not found"):
            _gcp(tmp_path).ship(tmp_path / "absent.bundle")
        assert _calls(fake_gcloud) == []

    def test_an_unreadable_status_is_an_error(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, describe_exit=1, describe_stderr="no such instance\n")
        with pytest.raises(HostError, match="cannot read the status"):
            _gcp(tmp_path).ensure_up()

    def test_gcloud_absent_names_the_install_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _gcp(tmp_path)
        # Dropping the cached path is not enough: ensure_tool re-resolves from
        # PATH, and a developer machine with the real gcloud installed found it
        # (the operator Mac's merge verify, #2378). An empty PATH is "absent".
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        original = subprocess_utils.TOOL_PATHS.pop("gcloud", None)
        try:
            with pytest.raises(HostError, match="gcloud is not on PATH"):
                provider.status()
        finally:
            if original is not None:
                subprocess_utils.TOOL_PATHS["gcloud"] = original


class TestTheCancelCommandProvesWhatItClaims:
    """The disarming command's answer is read as a word, not as a 0 or 1.

    ``! systemctl is-active --quiet`` is true for a timer that is not armed
    *and* for a systemctl that could not answer at all — a missing binary, an
    unreachable bus — so the second one came back as positive proof that no
    stop was pending, and a job was shipped onto a host about to stop.

    Run against the real command text under ``/bin/sh``, with a PATH of stubs
    and the sentinel redirected into ``tmp_path``: nothing on the machine
    running the test is reached, so the answer is the command's own.
    """

    @staticmethod
    def _probe(root: Path, systemctl: str | None) -> int:
        bin_dir = root / "cancelbin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        _write_stub(bin_dir, "sudo", "#!/bin/sh\nexit 0\n")
        if systemctl is not None:
            _write_stub(bin_dir, "systemctl", systemctl)
        command = _CANCEL_SHUTDOWN.replace(_SHUTDOWN_SENTINEL, str(root / "scheduled"))
        return subprocess.run(
            ["/bin/sh", "-c", command],
            env={"PATH": str(bin_dir)},
            capture_output=True,
            check=False,
        ).returncode

    def test_a_timer_that_answers_inactive_is_proof_the_stop_is_gone(
        self, stub_root: Path
    ) -> None:
        inactive = '#!/bin/sh\ncase "$1" in is-active) echo inactive; exit 3;; esac\n'
        assert self._probe(stub_root, inactive) == 0

    def test_a_missing_systemctl_is_not_read_as_a_disarmed_timer(
        self, stub_root: Path
    ) -> None:
        """Exit 127 and no answer. The negated status read that as "gone"."""
        assert self._probe(stub_root, None) == 1

    def test_a_systemctl_that_cannot_answer_is_not_read_as_a_disarmed_timer(
        self, stub_root: Path
    ) -> None:
        """An unreachable bus: non-zero, nothing on stdout, nothing proved."""
        bus_error = "#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n"
        assert self._probe(stub_root, bus_error) == 1

    def test_an_armed_timer_still_refuses_the_host(self, stub_root: Path) -> None:
        active = '#!/bin/sh\ncase "$1" in is-active) echo active; exit 0;; esac\n'
        assert self._probe(stub_root, active) == 1


class TestCredentialRefusal:
    """AC3 — a credential-shaped variable is refused, by name."""

    @pytest.mark.parametrize(
        "name",
        [
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "LINEAR_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "MY_SECRET_THING",
            "some_token_lowercase",
        ],
    )
    def test_a_credential_shaped_variable_is_refused_by_name(self, name: str) -> None:
        with pytest.raises(CredentialVariableRefused) as caught:
            job_environment({name: "value"})
        assert name in str(caught.value)
        assert caught.value.name == name
        assert "value" not in str(caught.value)

    def test_local_run_refuses_a_credential_even_when_asked(
        self, tmp_path: Path
    ) -> None:
        provider = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        provider.ensure_up()
        provider.ship(_bundle(tmp_path))
        try:
            with pytest.raises(CredentialVariableRefused, match="GH_TOKEN"):
                provider.run(
                    Job(
                        name="verify",
                        command="true",
                        environment={"GH_TOKEN": "ghp_secret"},
                    )
                )
        finally:
            provider.release()

    def test_gcp_run_refuses_a_credential_before_any_ssh(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        with pytest.raises(CredentialVariableRefused, match="ANTHROPIC_API_KEY"):
            _gcp(tmp_path).run(
                Job(
                    name="verify",
                    command="true",
                    environment={"ANTHROPIC_API_KEY": "sk-secret"},
                )
            )
        assert _calls(fake_gcloud) == []

    def test_the_job_environment_is_an_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ambient = {
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "GH_TOKEN": "ghp_secret",
            "KEW_LOG_DIR": "/tmp/logs",
        }
        resolved = job_environment({"CI": "1"}, ambient=ambient)
        assert resolved == {"PATH": "/usr/bin", "HOME": "/home/agent", "CI": "1"}

    def test_a_declared_non_credential_reaches_the_job(self, tmp_path: Path) -> None:
        provider = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        provider.ensure_up()
        provider.ship(_bundle(tmp_path))
        try:
            result = provider.run(
                Job(
                    name="verify",
                    command="printf '%s' \"$KEW_HOST_MARKER\"",
                    environment={"KEW_HOST_MARKER": "visible"},
                )
            )
        finally:
            provider.release()
        assert (result.exit_code, result.log) == (0, "visible")

    def test_an_ambient_credential_does_not_leak_into_a_local_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_secret")
        provider = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        provider.ensure_up()
        provider.ship(_bundle(tmp_path))
        try:
            result = provider.run(
                Job(name="verify", command="printf '[%s]' \"${GH_TOKEN-unset}\"")
            )
        finally:
            provider.release()
        assert result.log == "[unset]"


class TestOneJobAtATime:
    """The lock kew owns: one job per host, whoever is asking."""

    def test_a_second_job_on_the_same_host_is_refused(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / "locks"
        first = LocalProvider(lock_dir=lock_dir, root=tmp_path / "work")
        second = LocalProvider(lock_dir=lock_dir, root=tmp_path / "work2")
        for provider in (first, second):
            provider.ensure_up()
            provider.ship(_bundle(tmp_path))
        entered = threading.Event()
        release = threading.Event()
        refusal: list[BaseException] = []

        def hold() -> None:
            first.run(
                Job(
                    name="holder",
                    command=(
                        f"touch {tmp_path / 'entered'}; "
                        f"while [ ! -e {tmp_path / 'release'} ]; do sleep 0.02; done"
                    ),
                    timeout_seconds=30,
                )
            )

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            while not (tmp_path / "entered").exists():
                entered.wait(0.02)
            with pytest.raises(HostBusy, match="one job at a time per host"):
                second.run(Job(name="contender", command="true"))
        except BaseException as exc:  # pragma: no cover - reported below
            refusal.append(exc)
        finally:
            (tmp_path / "release").touch()
            release.set()
            holder.join(timeout=30)
            first.release()
            second.release()
        assert refusal == []

    def test_a_claim_covers_shipping_as_well_as_running(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / "locks"
        first = LocalProvider(lock_dir=lock_dir, root=tmp_path / "work")
        second = LocalProvider(lock_dir=lock_dir, root=tmp_path / "work2")
        with first.claim():
            with pytest.raises(HostBusy):
                second.ship(_bundle(tmp_path))
            first.ensure_up()
            first.ship(_bundle(tmp_path))
            try:
                assert first.run(Job(name="verify", command="true")).exit_code == 0
            finally:
                first.release()
        second.ensure_up()
        second.ship(_bundle(tmp_path))
        try:
            assert second.run(Job(name="verify", command="true")).exit_code == 0
        finally:
            second.release()

    def test_different_hosts_do_not_contend(self, tmp_path: Path) -> None:
        local = LocalProvider(lock_dir=tmp_path / "locks")
        remote = GcpProvider(_settings(), lock_dir=tmp_path / "locks")
        assert local.lock_key != remote.lock_key


class TestProviderPreconditions:
    def test_running_before_shipping_is_refused(self, tmp_path: Path) -> None:
        provider = LocalProvider(lock_dir=tmp_path / "locks")
        with pytest.raises(HostNotProvisioned, match="ship"):
            provider.run(Job(name="verify", command="true"))

    def test_local_is_the_default_provider(self) -> None:
        provider = resolve_provider(DispatchConfig())
        assert (provider.name, isinstance(provider, LocalProvider)) == ("local", True)

    def test_gcp_is_resolved_when_selected(self) -> None:
        config = DispatchConfig(
            hosts=HostsConfig(provider="gcp", gcp=_settings()),
        )
        assert isinstance(resolve_provider(config), GcpProvider)

    def test_selecting_gcp_without_an_instance_is_refused_at_load(self) -> None:
        with pytest.raises(ValueError, match="requires \\[hosts.gcp\\]"):
            HostsConfig(provider="gcp")

    def test_an_unknown_provider_is_refused_at_load(self) -> None:
        with pytest.raises(ValueError, match='must be "local" or "gcp"'):
            HostsConfig(provider="aws")

    def test_a_zero_idle_stop_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            GcpHostConfig(idle_stop_minutes=0)


class TestDoctorChecks:
    """AC4 — an expired gcloud login is a failed check with its fix command."""

    def _config(self) -> DispatchConfig:
        return DispatchConfig(hosts=HostsConfig(provider="gcp", gcp=_settings()))

    def test_an_expired_login_is_a_failed_check_with_its_fix(
        self, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            auth_exit=1,
            auth_stderr=(
                "ERROR: (gcloud.auth.print-access-token) Reauthentication required.\n"
            ),
        )
        checks = {check.name: check for check in check_hosts(self._config())}
        auth = checks["gcloud-auth"]
        assert (auth.status, auth.fix) == (FAIL, GCLOUD_LOGIN_FIX)
        assert "expired" in auth.detail
        assert checks["gcp-instance"].status == SKIP

    def test_a_healthy_gcp_host_passes_every_check(self, fake_gcloud: Path) -> None:
        _write_state(fake_gcloud, statuses=["RUNNING"])
        checks = check_hosts(self._config())
        assert [check.status for check in checks] == [OK] * 4
        instance = checks[-1]
        assert "verify-1" in instance.detail and "RUNNING" in instance.detail

    def test_local_only_reports_local_and_probes_no_cloud(
        self, fake_gcloud: Path
    ) -> None:
        checks = check_hosts(DispatchConfig())
        assert [(check.name, check.status) for check in checks] == [("local", OK)]
        assert _calls(fake_gcloud) == []

    def test_an_unreachable_instance_names_the_listing_command(
        self, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, describe_exit=1, describe_stderr="was not found\n")
        checks = {check.name: check for check in check_hosts(self._config())}
        instance = checks["gcp-instance"]
        assert instance.status == FAIL
        assert instance.fix == "gcloud compute instances list --project proj"

    def test_a_missing_gcloud_skips_rather_than_passes_downstream(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delitem(subprocess_utils.TOOL_PATHS, "gcloud", raising=False)
        try:
            checks = {check.name: check for check in check_hosts(self._config())}
        finally:
            subprocess_utils.TOOL_PATHS.pop("gcloud", None)
        assert checks["gcloud"].status == FAIL
        assert checks["gcloud-auth"].status == SKIP
        assert checks["gcp-instance"].status == SKIP


class TestTheExitMarkerCannotBeForged:
    """Review #2386: the outcome is read off stdout, where the wrapper puts it."""

    def test_a_marker_on_stderr_does_not_override_the_real_exit_code(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(
            fake_gcloud,
            job_output="build failed\n",
            job_exit=1,
            job_stderr="KEW_HOST_EXIT=0\n",
        )
        result = _gcp(tmp_path).run(Job(name="verify", command="false"))
        assert (result.exit_code, result.ok) == (1, False)

    def test_a_marker_on_stderr_alone_is_no_marker_at_all(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, swallow_marker=True)
        with pytest.raises(HostError, match="no exit marker"):
            _gcp(tmp_path).run(Job(name="verify", command="true"))

    def test_the_log_still_carries_what_the_job_wrote_to_stderr(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_exit=2, job_stderr="compiler said no\n")
        result = _gcp(tmp_path).run(Job(name="verify", command="false"))
        assert (result.exit_code, "compiler said no" in result.log) == (2, True)


def _run_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[str, ...], float | None]]:
    """Record the (argv, timeout) of every gcloud call, then run it for real.

    Every gcloud call is a session of its own process group since #2811, so
    :meth:`GcpProvider.interrupt` can end it; the bound is read off the
    collector every session is read through.
    """
    recorded: list[tuple[tuple[str, ...], float | None]] = []
    original = gcp_module._collect_session

    def spy(
        session: subprocess.Popen[str], *, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        recorded.append((tuple(str(arg) for arg in session.args), timeout))
        return original(session, timeout=timeout)

    monkeypatch.setattr(gcp_module, "_collect_session", spy)
    return recorded


def _ceiling_spy(monkeypatch: pytest.MonkeyPatch) -> list[float | None]:
    """Every ceiling a job's own ssh session was opened under, in order.

    A job's session is opened with ``popen`` so the ceiling can kill the whole
    process group (#2465), which is what the ceiling it was given is read off.
    Every other gcloud call is now read the same way (#2811), so only the
    sessions carrying a job's exit marker are the job's.
    """
    recorded: list[float | None] = []
    original = gcp_module._collect_session

    def spy(
        session: subprocess.Popen[str], *, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if any(EXIT_MARKER in str(arg) for arg in session.args):
            recorded.append(timeout)
        return original(session, timeout=timeout)

    monkeypatch.setattr(gcp_module, "_collect_session", spy)
    return recorded


class TestTheJobOwnsItsCeiling:
    """Review #2386: a gcp job is bounded by Job.timeout_seconds, not by 300s."""

    def test_a_ceiling_above_the_gcloud_timeout_is_honoured(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = _ceiling_spy(monkeypatch)
        job = Job(name="verify", command="true", timeout_seconds=1800.0)
        assert _gcp(tmp_path).run(job).exit_code == 0
        assert recorded == [1800.0]

    def test_an_unbounded_job_is_bounded_by_the_hosts_deadline_not_the_plumbing(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A job with no ceiling is not capped at 300s — and not unbounded either.

        The scar #2386 left stands: the gcloud plumbing's own 300s is not a
        job's ceiling. What #2465 added underneath it is the host's deadline,
        so "no ceiling" is the deadline rather than for ever.
        """
        recorded = _ceiling_spy(monkeypatch)
        assert _gcp(tmp_path).run(Job(name="verify", command="true")).exit_code == 0
        assert recorded == [GcpHostConfig().job_timeout_minutes * 60.0]

    def test_a_ceiling_longer_than_the_deadline_is_not_cut_down_to_it(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #2475: the deadline may not shorten a ceiling that was asked for.

        Cutting it would end a job that was merely slow and then attest the
        result as a lost connection — "NOT a test failure" against a gate that
        never ran. The caller's number wins; what the host owes it is a kill
        that really ends the session, not a smaller ceiling.
        """
        recorded = _ceiling_spy(monkeypatch)
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=0.5
        )
        job = Job(name="verify", command="true", timeout_seconds=99999.0)
        assert provider.run(job).exit_code == 0
        assert recorded == [99999.0]

    def test_the_plumbing_keeps_its_own_bound(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = _run_spy(monkeypatch)
        _gcp(tmp_path).ship(_bundle(tmp_path))
        copies = [timeout for command, timeout in recorded if "scp" in command]
        assert copies == [300.0]

    def test_a_job_that_outruns_its_ceiling_names_the_ceiling(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, job_sleep=5)
        job = Job(name="verify", command="sleep 5", timeout_seconds=0.3)
        with pytest.raises(HostTimeout, match="exceeded its 0.3s ceiling"):
            _gcp(tmp_path).run(job)

    def test_both_hosts_report_an_outrun_ceiling_the_same_way(
        self, tmp_path: Path
    ) -> None:
        provider = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        provider.ensure_up()
        provider.ship(_bundle(tmp_path))
        try:
            with pytest.raises(HostTimeout, match="exceeded its 0.3s ceiling"):
                provider.run(Job(name="verify", command="sleep 5", timeout_seconds=0.3))
        finally:
            provider.release()


def _await_orphan(state: Path, *, within: float = 10.0) -> int:
    """The orphan pid the shim recorded, once it has recorded it.

    The shim forks its holder and writes the pid before it starts sleeping, so
    an assertion made the instant the session opened would race the fork. A torn
    read is skipped rather than failed: the shim writes the file under its own
    lock, which this reader does not take.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError, KeyError, AssertionError):
            pid = _read_state(state)["orphan_pid"]
            assert isinstance(pid, int)
            return pid
        time.sleep(0.05)
    raise AssertionError("the fake gcloud never recorded an orphan pid")


def _reaped(pid: int, *, within: float = 5.0) -> bool:
    """Whether *pid* has gone, giving a ``SIGKILL`` a moment to land.

    The orphan is the shim's child, not this process's, so it is reparented and
    reaped by init: once the signal lands the pid resolves to nothing. Polled
    rather than read once, because "killed" and "gone from the table" are not
    the same instant.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - not ours to signal
            return False
        time.sleep(0.05)
    return False


class TestDeadSessionsEnd:
    """#2465 — a silently dead ssh session no longer hangs a verify for ever.

    The scar: ``kew verify --host gcp`` held a session open on the operator Mac
    for 66 minutes with nothing left of it on the instance — a half-open TCP
    connection, a local ``ssh`` with no keepalive, and a job with no deadline of
    its own, holding the host lock the whole time.
    """

    def test_every_job_session_carries_the_keepalive_flags(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).run(Job(name="verify", command="true"))
        sessions = [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "ssh"]
        ]
        assert sessions
        for call in sessions:
            assert all(flag in call for flag in SSH_KEEPALIVE_FLAGS), call

    def test_the_streamed_session_argv_carries_them_too(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        argv = _gcp(tmp_path).ssh_argv("true")
        assert all(flag in argv for flag in SSH_KEEPALIVE_FLAGS)

    def test_a_copy_carries_them_in_the_spelling_scp_takes(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """``gcloud compute scp`` spells its passthrough ``--scp-flag``.

        Spelling it ``--ssh-flag`` would be refused outright, so every bundle
        ship would fail — a keepalive is worth nothing if it breaks the copy.
        """
        _gcp(tmp_path).ship(_bundle(tmp_path))
        copies = [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "scp"]
        ]
        assert copies
        for call in copies:
            assert all(flag in call for flag in SCP_KEEPALIVE_FLAGS), call
        assert SCP_KEEPALIVE_FLAGS == (
            "--scp-flag=-oServerAliveInterval=15",
            "--scp-flag=-oServerAliveCountMax=4",
        )

    def test_the_keepalives_detect_a_dead_peer_within_about_a_minute(self) -> None:
        """15s probes, four of them: a dead connection ends in about a minute."""
        assert SSH_KEEPALIVE_FLAGS == (
            "--ssh-flag=-oServerAliveInterval=15",
            "--ssh-flag=-oServerAliveCountMax=4",
        )

    def test_a_session_that_never_returns_is_killed_at_the_deadline(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The deadline ends the whole session, and says what it is.

        ``orphan`` is the shim doing what the real ``gcloud compute ssh``
        wrapper does: a child that outlives it holding the session's stdout and,
        on a real host, still running the job. Ending only the pid kew holds
        leaves that child — and the remote job — alive on an instance whose idle
        stop kew is about to arm.
        """
        _write_state(fake_gcloud, job_responses=[{"match": "", "orphan": 120}])
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=0.5
        )
        started = time.monotonic()
        with pytest.raises(HostJobDeadlineExceeded) as raised:
            provider.run(Job(name="verify", command="sleep 600"))
        assert time.monotonic() - started < 60.0
        assert "job_timeout_minutes" in str(raised.value)
        assert "NOT a test failure" in str(raised.value)
        orphan = _read_state(fake_gcloud)["orphan_pid"]
        assert isinstance(orphan, int)
        assert _reaped(orphan), f"the wrapper's ssh child {orphan} outlived the kill"

    def test_the_deadline_is_an_interruption_with_its_own_category(self) -> None:
        assert issubclass(HostJobDeadlineExceeded, HostInterrupted)
        assert HostJobDeadlineExceeded.error_category == "host_job_deadline_exceeded"
        assert HostConnectionLost.error_category == "host_connection_lost"
        assert issubclass(HostInterrupted, HostError)

    def test_the_host_lock_is_released_when_the_deadline_fires(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The hang held the lock every merge verify waits on. It must not."""
        _write_state(fake_gcloud, job_responses=[{"match": "", "orphan": 120}])
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=0.5
        )
        with pytest.raises(HostJobDeadlineExceeded):
            provider.run(Job(name="verify", command="sleep 600"))

        waiting = GcpProvider(_settings(), lock_dir=tmp_path / "locks")
        with waiting.claim():
            pass

    def test_a_session_that_closes_with_no_marker_is_a_lost_connection(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, swallow_marker=True)
        with pytest.raises(HostConnectionLost) as raised:
            _gcp(tmp_path).run(Job(name="verify", command="true"))
        message = str(raised.value)
        assert "connection to the gcp host was lost" in message
        assert "NOT a test failure" in message

    def test_the_deadline_comes_from_config_in_minutes(self, tmp_path: Path) -> None:
        settings = GcpHostConfig(
            project="proj", zone="z", instance="i", job_timeout_minutes=7
        )
        provider = GcpProvider(settings, lock_dir=tmp_path / "locks")
        assert provider._job_ceiling(None) == 7 * 60.0
        assert provider._job_ceiling(99999.0) == 99999.0

    def test_the_shipped_deadline_is_above_the_shipped_verify_ceiling(self) -> None:
        """Review of #2475: the defaults may not contradict each other.

        A deadline below ``[verify] timeout_seconds`` was a shipped
        configuration that cut every verification run on a gcp host in half and
        then attested the result as ``host_job_deadline_exceeded`` — "the
        connection is presumed lost", "NOT a test failure" — for a job that was
        only slow.
        """
        shipped = DispatchConfig()
        assert shipped.verify.timeout_seconds == 7200
        deadline = GcpHostConfig().job_timeout_minutes * 60
        assert deadline > shipped.verify.timeout_seconds

    def test_a_job_at_the_shipped_verify_ceiling_runs_under_that_ceiling(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole path, from the shipped config to the session's ceiling."""
        recorded = _ceiling_spy(monkeypatch)
        config = DispatchConfig()
        provider = GcpProvider(_settings(), lock_dir=tmp_path / "locks")
        ceiling = float(config.verify.timeout_seconds)
        provider.run(Job(name="verify", command="true", timeout_seconds=ceiling))
        assert recorded == [ceiling]

    def test_a_deadline_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="job_timeout_minutes"):
            GcpHostConfig(project="p", zone="z", instance="i", job_timeout_minutes=0)

    def test_the_jobs_own_ceiling_still_wins_when_it_is_the_shorter_one(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """A job that was simply slow is still reported against its own number."""
        _write_state(fake_gcloud, job_sleep=5)
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=30.0
        )
        with pytest.raises(HostTimeout, match="exceeded its 0.3s ceiling") as raised:
            provider.run(Job(name="verify", command="sleep 5", timeout_seconds=0.3))
        assert not isinstance(raised.value, HostInterrupted)

    def test_any_escape_from_the_wait_ends_the_session_too(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #2475: the ceiling was not the only way out of the wait.

        A ``KeyboardInterrupt`` at the terminal — or a ``SIGTERM`` the driver
        turned into an exception, or an error raised above the read — unwound
        past the one guarded path and left the detached ``gcloud``/``ssh`` pair,
        and the job on the instance, running while kew released the host lock
        and armed the idle stop. The session is in a process group of its own,
        so the terminal's own signal does not reach it either.
        """
        _write_state(fake_gcloud, job_responses=[{"match": "", "orphan": 120}])
        original = subprocess.Popen.communicate

        def interrupt_the_job(
            session: subprocess.Popen[str], *args: object, **kwargs: object
        ) -> tuple[str, str]:
            if "KEW_HOST_EXIT" not in " ".join(session.args):
                return original(session, *args, **kwargs)  # type: ignore[arg-type]
            _await_orphan(fake_gcloud)
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_the_job)
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=30.0
        )
        with pytest.raises(KeyboardInterrupt):
            provider.run(Job(name="verify", command="sleep 600"))
        orphan = _await_orphan(fake_gcloud)
        assert _reaped(orphan), f"the wrapper's ssh child {orphan} outlived the escape"

        waiting = GcpProvider(_settings(), lock_dir=tmp_path / "locks")
        with waiting.claim():
            pass

    def test_a_recipe_that_outruns_its_ceiling_ends_the_whole_session(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """Review of #2475: a recipe's ceiling ended one pid, and it runs as root.

        The orphaned ``ssh`` — and the recipe's apt transaction behind it —
        outlived the ceiling on an instance kew was about to release and arm the
        idle stop on, which is a half-provisioned host nothing is watching.
        """
        _write_state(fake_gcloud, job_responses=[{"match": "", "orphan": 120}])
        recipe = tmp_path / "recipe.sh"
        recipe.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        started = time.monotonic()
        with pytest.raises(HostTimeout, match="recipe.sh exceeded its 0.5s ceiling"):
            _gcp(tmp_path).run_recipe(recipe, timeout_seconds=0.5)
        assert time.monotonic() - started < 60.0
        orphan = _await_orphan(fake_gcloud)
        assert _reaped(orphan), f"the recipe's ssh child {orphan} outlived the kill"

    def test_a_recipe_with_no_ceiling_runs_under_the_hosts_deadline(
        self, tmp_path: Path
    ) -> None:
        provider = GcpProvider(
            _settings(), lock_dir=tmp_path / "locks", job_deadline_seconds=42.0
        )
        assert provider._job_ceiling(None) == 42.0

    def test_a_plumbing_calls_ceiling_ends_the_whole_session_too(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2811: every gcloud call now gets the job's group kill.

        This used to pin that ``subprocess.run``'s ceiling, which ``_ssh`` was
        bounded by, came back on time but left the wrapper's ``ssh`` child
        alive. Every gcloud call is now a session :meth:`interrupt` can end,
        so a plumbing call's ceiling ends the whole group as a job's does.
        """
        _write_state(fake_gcloud, job_responses=[{"match": "", "orphan": 30}])
        started = time.monotonic()
        with pytest.raises(HostTimeout, match="did not finish within 0.5s"):
            _gcp(tmp_path)._ssh(marker_command("true"), timeout=0.5)
        elapsed = time.monotonic() - started
        orphan = _await_orphan(fake_gcloud)
        try:
            assert elapsed < 10.0, "the plumbing's ceiling came back late"
            assert _reaped(orphan), f"the wrapper's ssh child {orphan} outlived it"
        finally:
            with contextlib.suppress(OSError):
                os.kill(orphan, signal.SIGKILL)


class TestSuspendedInstances:
    """Review #2386: GCE refuses ``start`` on a suspended instance."""

    def test_a_suspended_instance_is_resumed_not_started(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, statuses=["SUSPENDED"])
        _gcp(tmp_path).ensure_up()
        verbs = [call[:3] for call in _calls(fake_gcloud)]
        assert ["compute", "instances", "resume"] in verbs
        assert ["compute", "instances", "start"] not in verbs
        assert _read_state(fake_gcloud)["resumed"] == 1

    def test_a_suspending_instance_is_waited_out_then_resumed(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, statuses=["SUSPENDING", "SUSPENDED", "RUNNING"])
        _gcp(tmp_path).ensure_up()
        state = _read_state(fake_gcloud)
        assert (state["resumed"], "started" in state) == (1, False)

    def test_a_terminated_instance_is_still_started(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, statuses=["TERMINATED"])
        _gcp(tmp_path).ensure_up()
        state = _read_state(fake_gcloud)
        assert (state["started"], "resumed" in state) == (1, False)


class TestVariableNamesAreRefusedNotEscaped:
    """Review #2386: a name is interpolated into the host's command line."""

    @pytest.mark.parametrize(
        "name",
        [
            "PATH;touch /tmp/kew-host-pwned",
            "A B",
            "$(id)",
            "`id`",
            "1INVALID",
            "",
        ],
    )
    def test_a_name_outside_the_portable_set_is_refused(self, name: str) -> None:
        with pytest.raises(VariableNameRefused) as caught:
            job_environment({name: "value"})
        assert caught.value.name == name

    def test_gcp_refuses_the_name_before_any_ssh(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        with pytest.raises(VariableNameRefused):
            _gcp(tmp_path).run(
                Job(
                    name="verify",
                    command="true",
                    environment={"X;touch /tmp/kew-host-pwned": "1"},
                )
            )
        assert _calls(fake_gcloud) == []

    def test_an_ordinary_name_still_reaches_the_remote_command(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).run(
            Job(name="verify", command="true", environment={"KEW_HOST_MARKER": "ok"})
        )
        commands = [
            call[call.index("--command") + 1]
            for call in _calls(fake_gcloud)
            if "--command" in call
        ]
        assert any("env KEW_HOST_MARKER=ok " in command for command in commands)


class TestTheClaimKnowsWhoHoldsIt:
    """Review #2386: one provider object shared by two threads is two jobs."""

    def test_two_threads_sharing_one_provider_do_not_both_run(
        self, tmp_path: Path
    ) -> None:
        provider = LocalProvider(lock_dir=tmp_path / "locks", root=tmp_path / "work")
        provider.ensure_up()
        provider.ship(_bundle(tmp_path))
        failures: list[BaseException] = []

        def hold() -> None:
            try:
                provider.run(
                    Job(
                        name="holder",
                        command=(
                            f"touch {tmp_path / 'entered'}; "
                            f"while [ ! -e {tmp_path / 'release'} ]; do "
                            "sleep 0.02; done"
                        ),
                        timeout_seconds=30,
                    )
                )
            except BaseException as exc:  # pragma: no cover - reported below
                failures.append(exc)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            deadline = time.monotonic() + 30
            while not (tmp_path / "entered").exists():
                assert time.monotonic() < deadline, "the holder never started"
                time.sleep(0.02)
            with pytest.raises(HostBusy, match="one job at a time per host"):
                provider.run(Job(name="contender", command="true"))
        finally:
            (tmp_path / "release").touch()
            holder.join(timeout=30)
            provider.release()
        assert failures == []


class TestTheWholeLifecycleIsUnderTheClaim:
    """Review #2386: ensure_up and release are writes to someone else's host."""

    def test_release_leaves_a_host_another_holder_claims_alone(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        lock_dir = tmp_path / "locks"
        holder = GcpProvider(_settings(), lock_dir=lock_dir, sleep=lambda _s: None)
        departing = GcpProvider(_settings(), lock_dir=lock_dir, sleep=lambda _s: None)
        with holder.claim():
            departing.release()
        assert not any("systemd-run" in command for command in _commands(fake_gcloud))

    def test_ensure_up_is_refused_while_another_holder_claims(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        lock_dir = tmp_path / "locks"
        holder = GcpProvider(_settings(), lock_dir=lock_dir, sleep=lambda _s: None)
        contender = GcpProvider(_settings(), lock_dir=lock_dir, sleep=lambda _s: None)
        with holder.claim(), pytest.raises(HostBusy):
            contender.ensure_up()
        assert _calls(fake_gcloud) == []

    def test_a_held_claim_still_lets_its_own_holder_release(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        provider = _gcp(tmp_path)
        with provider.claim():
            provider.release()
        assert any("--on-active=30m" in command for command in _commands(fake_gcloud))


class TestARemoteJobGetsAKnownPath:
    """#2433 — a job arrives in a non-login shell, so kew hands it a PATH.

    The host was up, provisioned and reachable; the job died in 1.6 seconds
    with ``sh: 6: uv: not found``, exit 127. ``gcloud compute ssh --command``
    is a non-interactive, non-login shell — no profile runs, so nothing puts
    ``~/.local/bin`` on PATH — and kew deliberately forwards no ambient PATH
    to a host whose directories are not this machine's. The job therefore ran
    with whatever PATH sshd happened to hand it, and found nothing.

    These tests make the fake ``gcloud`` *run* what the provider built, in a
    ``/bin/sh`` with a HOME and no PATH, because the whole question is whether
    that command can reach a tool.
    """

    @pytest.fixture
    def host_home(self, stub_root: Path, fake_gcloud: Path) -> Path:
        """A fake login's home, with the shipped checkout the job cds into."""
        home = stub_root / "host-home"
        (home / ".local" / "bin").mkdir(parents=True)
        (home / "kew-host" / "checkout").mkdir(parents=True)
        run = install_fake_systemd(home / ".local" / "bin")
        # The lease this process holds, which a job checks under the lease
        # lock before it starts (#2980).
        lease = run / LEASE_FILE.removeprefix("/run/")
        lease.write_text(f"{lease_token()} {int(time.time()) + 600}\n")
        _write_state(fake_gcloud, execute_in=str(home))
        return home

    @staticmethod
    def _scope_record(host_home: Path) -> Path:
        """Where the fake ``sudo`` puts root's scope record."""
        return host_home / ".local" / "systemd" / JOB_SESSION_FILE.lstrip("/")

    def test_a_job_finds_a_tool_only_a_login_shell_would_have_found(
        self, tmp_path: Path, fake_gcloud: Path, host_home: Path
    ) -> None:
        _write_stub(host_home / ".local" / "bin", "uv", "#!/bin/sh\necho 'uv 0.5.11'\n")
        result = _gcp(tmp_path).run(Job(name="verify", command="uv --version"))
        assert (result.exit_code, "uv 0.5.11" in result.log) == (0, True)

    def test_a_job_whose_session_cannot_be_recorded_is_refused_unstarted(
        self, tmp_path: Path, fake_gcloud: Path, host_home: Path
    ) -> None:
        """#2811 — a job a takeover could never stop does not run at all."""
        self._scope_record(host_home).mkdir(parents=True)
        with pytest.raises(HostError, match="was not started") as raised:
            _gcp(tmp_path).run(Job(name="verify", command="touch started"))
        assert not isinstance(raised.value, HostInterrupted)
        assert not (host_home / "kew-host" / "checkout" / "started").exists()

    def test_a_job_records_the_scope_it_runs_in_before_it_runs(
        self, tmp_path: Path, fake_gcloud: Path, host_home: Path
    ) -> None:
        """#2876 — the job runs inside the scope its record names, which also
        says when it started."""
        before = int(time.time())
        result = _gcp(tmp_path).run(Job(name="verify", command='echo "$PPID"'))
        token, unit, start = self._scope_record(host_home).read_text().split()
        assert (result.exit_code, token) == (0, lease_token())
        assert before <= int(start) <= time.time()
        scope = host_home / ".local" / "systemd" / "scopes" / f"{unit}.scope"
        assert scope.read_text() == f"pid {result.log.split()[0]}"

    def test_a_job_with_no_inherited_path_still_has_the_recipes_locations(
        self, tmp_path: Path, fake_gcloud: Path, host_home: Path
    ) -> None:
        """The PATH the job sees leads with kew's list, expanded and clean.

        An empty entry is the *current* directory, which for a job is the
        shipped checkout — so an entry kew leaves empty would put the code kew
        just shipped on the search path. Asserted over the entries *kew*
        contributed, not over the whole reported value: a shell handed no PATH
        at all substitutes a default of its own before the prefix ever runs,
        and that default is the shell's to choose — the ``/bin/sh`` on Linux
        answers with the standard utility directories, the one on macOS with a
        different list, and neither is kew's to vet (#2456). Whether kew's own
        join can leave an empty entry is the one case no shell default
        reaches, so it is asserted directly in
        :meth:`test_a_host_whose_path_is_set_but_empty_gets_no_trailing_gap`.
        """
        result = _gcp(tmp_path).run(Job(name="verify", command='printf %s "$PATH"'))
        reported = result.log.split("\n", 1)[0]
        kews_own = REMOTE_PATH.replace("$HOME", str(host_home))

        assert reported.startswith(kews_own)
        entries = kews_own.split(":")
        assert "" not in entries and "." not in entries

    def test_a_path_the_host_already_had_is_kept_after_kews_own(self) -> None:
        """Prepended, not substituted: a host with extra on PATH keeps it."""
        prefix = remote_path_prefix()
        probe = subprocess.run(
            ["/bin/sh", "-c", f'{prefix}printf %s "$PATH"'],
            capture_output=True,
            text=True,
            check=True,
            env={"HOME": "/home/nobody", "PATH": "/opt/extra/bin"},
        )
        expected = REMOTE_PATH.replace("$HOME", "/home/nobody")
        assert probe.stdout == f"{expected}:/opt/extra/bin"

    def test_a_host_whose_path_is_set_but_empty_gets_no_trailing_gap(self) -> None:
        """What ``${PATH:+:$PATH}`` is for, asserted where it is reachable.

        A shell asked for ``$PATH`` while the variable is *unset* answers with
        a default of its own, so the job-level test above can never reach this
        case. Set-but-empty is the one environment in which a bare ``:$PATH``
        would append the current directory, and here it is exported.
        """
        prefix = remote_path_prefix()
        probe = subprocess.run(
            ["/bin/sh", "-c", f'{prefix}printf %s "$PATH"'],
            capture_output=True,
            text=True,
            check=True,
            env={"HOME": "/home/nobody", "PATH": ""},
        )
        assert probe.stdout == REMOTE_PATH.replace("$HOME", "/home/nobody")

    def test_the_path_is_exported_before_the_job_can_run(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """Every remote command, not only the job: the clone needs git too."""
        _gcp(tmp_path).run(Job(name="verify", command="true"))
        commands = _commands(fake_gcloud)
        assert commands
        assert all(command.startswith(remote_path_prefix()) for command in commands)


class TestDoctorFixesAreRunnableCommands:
    """#2499 review: a `fix` is one runnable command or nothing; prose is a hint."""

    def test_a_missing_gcloud_puts_the_install_link_in_the_hint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delitem(subprocess_utils.TOOL_PATHS, "gcloud", raising=False)
        config = DispatchConfig(hosts=HostsConfig(provider="gcp", gcp=_settings()))
        try:
            checks = check_hosts(config)
        finally:
            subprocess_utils.TOOL_PATHS.pop("gcloud", None)
        for check in checks:
            assert "https://" not in check.fix, check
        gcloud = next(check for check in checks if check.name == "gcloud")
        assert gcloud.fix == ""
        assert "cloud.google.com/sdk/docs/install" in gcloud.hint

    def test_a_shell_metacharacter_project_never_reaches_a_fix(
        self, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, describe_exit=1, describe_stderr="was not found\n")
        settings = GcpHostConfig(
            project="safe; echo PWNED", zone="us-central1-a", instance="verify-1"
        )
        config = DispatchConfig(hosts=HostsConfig(provider="gcp", gcp=settings))
        instance = {c.name: c for c in check_hosts(config)}["gcp-instance"]
        assert instance.status == FAIL
        assert instance.fix == ""
        assert "not a GCP project id" in instance.hint

    def test_a_domain_scoped_project_is_quoted_as_one_argument(
        self, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, describe_exit=1, describe_stderr="was not found\n")
        settings = GcpHostConfig(
            project="example.com:my-project", zone="us-central1-a", instance="v-1"
        )
        config = DispatchConfig(hosts=HostsConfig(provider="gcp", gcp=settings))
        instance = {c.name: c for c in check_hosts(config)}["gcp-instance"]
        assert shlex.split(instance.fix) == [
            "gcloud",
            "compute",
            "instances",
            "list",
            "--project",
            "example.com:my-project",
        ]


class TestNoSessionCarriesOperatorCredentials:
    """#2550 finding 1: no ssh session lends the VM the operator's agent or tokens."""

    def test_every_job_session_turns_forwarding_off(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).run(Job(name="verify", command="true"))
        sessions = [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "ssh"]
        ]
        assert sessions
        for call in sessions:
            assert all(flag in call for flag in SSH_BOUNDARY_FLAGS), call

    def test_the_streamed_session_argv_turns_it_off_too(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        argv = _gcp(tmp_path).ssh_argv("true")
        assert all(flag in argv for flag in SSH_BOUNDARY_FLAGS)

    def test_a_copy_turns_it_off_in_the_spelling_scp_takes(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).ship(_bundle(tmp_path))
        copies = [
            call for call in _calls(fake_gcloud) if call[:2] == ["compute", "scp"]
        ]
        assert copies
        for call in copies:
            assert all(flag in call for flag in SCP_BOUNDARY_FLAGS), call

    def test_the_options_are_the_ones_the_boundary_names(self) -> None:
        assert SSH_BOUNDARY_FLAGS == (
            "--ssh-flag=-F/dev/null",
            "--ssh-flag=-oForwardAgent=no",
            "--ssh-flag=-oForwardX11=no",
            "--ssh-flag=-oClearAllForwardings=yes",
            "--ssh-flag=-oSendEnv=-*",
        )

    def test_no_config_file_is_read(self) -> None:
        # #2569 defect 1: ``-oSendEnv=-*`` clears only what precedes it, and a
        # config file's ``SendEnv``/``SetEnv`` come after the command line.
        assert "--ssh-flag=-F/dev/null" in SSH_BOUNDARY_FLAGS
        assert "--scp-flag=-F/dev/null" in SCP_BOUNDARY_FLAGS

    @pytest.mark.skipif(shutil.which("ssh") is None, reason="no ssh client here")
    def test_ssh_resolves_the_boundary_with_nothing_to_send(self) -> None:
        """What ``ssh -G`` says the session will do, not what the argv hopes."""
        options = [flag.split("=", 1)[1] for flag in SSH_BOUNDARY_FLAGS]
        resolved = subprocess.run(  # noqa: S603 — fixed argv, no session opened
            ["ssh", "-G", *options, "kew-boundary.invalid"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.lower()
        lines = resolved.splitlines()
        assert "forwardagent no" in lines
        assert "forwardx11 no" in lines
        assert "clearallforwardings yes" in lines
        assert not [line for line in lines if line.startswith("sendenv ")]
        assert not [line for line in lines if line.startswith("setenv ")]

    def test_the_session_environment_holds_no_agent_and_no_credential(self) -> None:
        env = remote_session_environment(
            {
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
                "SSH_AGENT_PID": "42",
                "GH_TOKEN": "x",
                "GITHUB_TOKEN": "x",
                "GH_ENTERPRISE_TOKEN": "x",
                "LINEAR_API_KEY": "x",
                "ANTHROPIC_API_KEY": "x",
                "CLAUDE_CODE_OAUTH_TOKEN": "x",
                "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
                "CLOUDSDK_CONFIG": "/gcloud",
                # #2569: what the namespace rule alone let through — a name a
                # stock ``SendEnv LC_*`` forwards.
                "LC_AUTH": "x",
                "LC_SESSION_SECRET": "x",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin",
                "HOME": "/home/op",
            }
        )
        assert env == {
            "CLOUDSDK_CONFIG": "/gcloud",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin",
            "HOME": "/home/op",
        }

    def test_gcloud_keeps_its_own_auth_configuration(self) -> None:
        """#2573: gcloud's auth is configuration it reads, never forwarded.

        Dropping it would fail every API call or quietly run gcloud as a
        different principal; no session can send it (``-F`` + ``SendEnv=-*``).
        """
        gcloud_auth = {
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/home/runner/creds.json",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": "x",
            "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/tok",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "sa@p.iam.example",
            "CLOUDSDK_PROXY_PASSWORD": "x",
            "CLOUDSDK_CORE_PROJECT": "p",
        }
        assert remote_session_environment(gcloud_auth) == gcloud_auth

    def test_every_gcloud_call_runs_in_that_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The effect, not the argv: what ``subprocess`` is actually handed."""
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        monkeypatch.setenv("GH_TOKEN", "kew-fixture-forge-credential")
        monkeypatch.setattr(gcp_module, "require_gcloud", lambda: None)
        handed: list[Mapping[str, str]] = []

        def popen(cmd: list[str], **kwargs: Any) -> Any:
            handed.append(kwargs["env"])
            raise OSError("no session in this test")

        # Every gcloud call is a popen'd session since #2811, the plumbing's
        # as well as the job's, so both are refused at the same launch.
        monkeypatch.setattr(gcp_module.subprocess_utils, "popen", popen)
        provider = _gcp(tmp_path)
        with pytest.raises(HostError):
            provider.status()
        with pytest.raises(HostError):
            provider._ssh_bounded("true", timeout=5)

        assert len(handed) == 2
        for env in handed:
            assert "SSH_AUTH_SOCK" not in env
            assert "GH_TOKEN" not in env


class TestTheInstanceHoldsItsOwnLease:
    """#2627 — the exclusion outlives the operator process that took it.

    The operator-side flock dies with a SIGKILLed operator while the job it
    shipped runs on to its deadline. The lease on the instance is what the next
    process reads before it ships over that job's checkout, and the backstop
    behind it is what powers the instance off when no release ever comes.
    """

    def test_ensure_up_takes_the_lease_and_arms_the_backstop_after_the_disarm(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).ensure_up()
        commands = _commands(fake_gcloud)
        cancelled = next(i for i, c in enumerate(commands) if "shutdown -c" in c)
        held = [i for i, c in enumerate(commands) if LEASE_FILE in c]
        assert held and held[0] > cancelled
        assert DEADLINE_STOP_UNIT in commands[held[0]]
        assert f"$((now + {240 * 60}))" in commands[held[0]]

    def test_a_live_lease_of_another_process_refuses_ensure_up(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, lease_held=True)
        with pytest.raises(HostBusy, match="still leased to another kew job"):
            _gcp(tmp_path).ensure_up()

    def test_a_live_lease_refuses_the_ship_before_anything_is_deleted(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """The checkout ``ship`` removes may be the one the job is running in."""
        _write_state(fake_gcloud, lease_held=True)
        with pytest.raises(HostBusy):
            _gcp(tmp_path).ship(_bundle(tmp_path))
        assert not any("rm -rf" in command for command in _commands(fake_gcloud))
        assert "copies" not in _read_state(fake_gcloud)

    def test_a_job_extends_the_lease_past_its_own_ceiling(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).run(Job(name="verify", command="true", timeout_seconds=1200))
        commands = _commands(fake_gcloud)
        job = next(i for i, c in enumerate(commands) if "KEW_HOST_EXIT" in c)
        assert any(
            "$((now + 1500))" in command and LEASE_FILE in command
            for command in commands[:job]
        )

    def test_a_release_over_another_live_lease_arms_nothing_and_raises_nothing(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, lease_held=True)
        _gcp(tmp_path).release()
        assert _read_state(fake_gcloud)["leases"] == 1

    def test_a_release_that_cannot_arm_the_idle_stop_says_so(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _write_state(fake_gcloud, release_exit=1)
        with pytest.raises(HostError, match="may keep running and billing"):
            _gcp(tmp_path).release()

    def test_a_release_after_a_job_demands_that_jobs_session_record(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2811: a job the release cannot find keeps the lease it ran under."""
        provider = _gcp(tmp_path)
        provider.run(Job(name="verify", command="true", timeout_seconds=60))
        provider.release()
        released = [c for c in _commands(fake_gcloud) if IDLE_STOP_UNIT in c]
        assert released[-1].endswith(
            release_command(
                lease_token(),
                idle_minutes=_settings().idle_stop_minutes,
                ran_job=True,
            )
        )

    def test_a_release_whose_job_survives_keeps_the_lease_and_says_so(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
    ) -> None:
        """#2811: the backstop, not this release, ends a job that will not die.

        Raised, so a verify that passed still says its host stayed leased.
        """
        _write_state(fake_gcloud, release_exit=JOB_SURVIVES_EXIT)
        with pytest.raises(HostError, match="not proved stopped") as raised:
            _gcp(tmp_path).release()
        assert "lease holds until its deadline" in str(raised.value)


class TestTheLeaseCommandsDoWhatTheySay:
    """The lease commands, run for real under ``/bin/sh`` with stub tools.

    ``sudo`` runs as this user with root's ``/run`` moved into the test's
    directory, ``systemctl`` and ``busctl`` answer for a stand-in cgroup
    (:mod:`support.fake_systemd`), ``flock`` takes a real lock on its file,
    ``systemd-run`` records its argv, and the lease file is redirected into
    the test's directory: the answer is the commands' own, and nothing on this
    machine is reached.
    """

    @classmethod
    def _run(cls, root: Path, command: str) -> tuple[int, str]:
        """*command*'s exit status, and the argv of every unit it armed."""
        ran = cls._shell(root, command)
        armed = root / "armed.log"
        return ran.returncode, armed.read_text() if armed.exists() else ""

    @staticmethod
    def _moved(root: Path, command: str) -> str:
        """*command* with the lease under *root*. The lease lock beside it
        stays on /run, where a job's scope start takes it too (#2980)."""
        return re.sub(
            re.escape(LEASE_FILE) + r"(?!\.lock)", str(root / "lease"), command
        )

    @staticmethod
    def _shell(
        root: Path,
        command: str,
        *,
        system_path: str = "/usr/bin:/bin",
        docker: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        bin_dir = root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        armed = root / "armed.log"
        install_fake_systemd(bin_dir)
        _instance_tools_where_missing(bin_dir)
        if docker:
            install_fake_docker(bin_dir)
        _write_stub(
            bin_dir,
            "systemd-run",
            f'#!/bin/sh\necho "$*" >> {shlex.quote(str(armed))}\n',
        )
        moved = TestTheLeaseCommandsDoWhatTheySay._moved(root, command)
        return subprocess.run(
            ["/bin/sh", "-c", moved],
            env={"PATH": f"{bin_dir}:{system_path}"},
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _lease(root: Path, token: str, seconds_left: int) -> None:
        expiry = int(time.time()) + seconds_left
        (root / "lease").write_text(f"{token} {expiry}\n", encoding="utf-8")

    def test_a_free_instance_is_leased_and_its_backstop_armed(
        self, stub_root: Path
    ) -> None:
        code, armed = self._run(
            stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
        )
        assert code == 0
        token, expiry = (stub_root / "lease").read_text().split()
        assert token == "me-1-a"
        assert 0 < int(expiry) - time.time() <= 600
        assert f"--on-active={600 + 1800}s --unit={DEADLINE_STOP_UNIT}" in armed

    def test_another_live_lease_refuses_and_is_left_alone(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "other-2-b", 600)
        before = (stub_root / "lease").read_text()
        code, armed = self._run(
            stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
        )
        assert code == LEASE_HELD_EXIT
        assert (stub_root / "lease").read_text() == before
        assert armed == ""

    def test_an_expired_lease_no_scope_record_names_is_kept_and_names_the_hand_drop(
        self, stub_root: Path
    ) -> None:
        """#2876: with no record there is no job start to stop containers
        from, so nothing proves the expired holder's job gone; the operator
        frees the instance with a forced hand drop."""
        self._lease(stub_root, "other-2-b", -5)
        before = (stub_root / "lease").read_text()
        reported = self._shell(
            stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
        )
        assert reported.returncode == LEASE_HELD_EXIT
        assert "expired lease held by other-2-b" in reported.stdout
        assert (stub_root / "lease").read_text() == before
        ran = self._shell(
            stub_root,
            hold_command(
                "me-1-a",
                seconds=600,
                idle_minutes=30,
                lapsed="other-2-b",
                expired=True,
            ),
        )
        assert ran.returncode == LEASE_HELD_EXIT
        assert (stub_root / "lease").read_text() == before
        assert "kew hosts release NAME --holder other-2-b --force" in ran.stdout
        assert not (stub_root / "armed.log").exists()

    def test_a_release_drops_its_own_lease_and_arms_the_idle_stop(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "me-1-a", 600)
        code, armed = self._run(stub_root, release_command("me-1-a", idle_minutes=30))
        assert code == 0
        assert not (stub_root / "lease").exists()
        assert f"--on-active=30m --unit={IDLE_STOP_UNIT}" in armed

    def test_a_release_over_another_live_lease_arms_nothing(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "other-2-b", 600)
        code, armed = self._run(stub_root, release_command("me-1-a", idle_minutes=30))
        assert code == LEASE_HELD_EXIT
        assert (stub_root / "lease").exists()
        assert armed == ""

    def test_a_drop_removes_only_its_own_lease_and_arms_nothing(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "other-2-b", 600)
        code, armed = self._run(stub_root, drop_command("me-1-a"))
        assert (code, armed) == (LEASE_HELD_EXIT, "")
        assert (stub_root / "lease").exists()
        self._lease(stub_root, "me-1-a", 600)
        code, armed = self._run(stub_root, drop_command("me-1-a"))
        assert (code, armed) == (0, "")
        assert not (stub_root / "lease").exists()


def _holder(*, machine: str | None = None, pid: int, boot: str | None = None) -> str:
    """A lease token in this kew's own shape, for this machine by default."""
    me = parse_lease_token(lease_token())
    assert me is not None, "this machine's boot id is unreadable"
    return (
        f"{me.machine if machine is None else machine}-{pid}-"
        f"{me.boot if boot is None else boot}-{'0' * 12}"
    )


def _dead_pid() -> int:
    """A pid that was this machine's a moment ago and is reaped now."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class TestALeaseFromADeadOperatorLapses:
    """#2811 — a holder on this machine that is provably gone does not hold.

    The lease names the operator's machine, pid and boot id. A holder from an
    earlier boot of this machine, or whose pid is not running, is lapsed: its
    orphaned job is stopped and its lease taken in one locked command, before
    anything ships. Any holder kew cannot prove gone still refuses.
    """

    def test_a_holder_from_an_earlier_boot_is_stopped_before_the_ship(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        earlier = uuid.uuid4().hex
        holder = _holder(pid=os.getpid(), boot=earlier)
        (tmp_path / "locks").mkdir()
        (tmp_path / "locks" / BOOTS_FILE).write_text(f"{earlier}\n")
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        provider = _gcp(tmp_path)
        provider.record_takeovers_in(MemoryTakeoverLedger())

        provider.ship(_bundle(tmp_path))

        commands = _commands(fake_gcloud)
        takeover = next(
            i
            for i, c in enumerate(commands)
            if "systemctl kill" in c and f'[ "$tok" = {holder} ]' in c
        )
        deleted = next(i for i, c in enumerate(commands) if "rm -rf" in c)
        assert takeover < deleted
        state = _read_state(fake_gcloud)
        assert state["taken_over"] == 1
        assert state["copies"] == 1
        assert provider.lapsed_holders() == (holder,)

    def test_a_same_named_machines_boot_is_not_an_earlier_boot_of_this_one(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """A second operator machine sharing this hostname keeps its live job."""
        holder = _holder(pid=_dead_pid(), boot=uuid.uuid4().hex)
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        provider = _gcp(tmp_path)
        with pytest.raises(HostBusy, match=f"held by {holder}"):
            provider.ensure_up()
        assert not any(f'[ "$tok" = {holder} ]' in c for c in _commands(fake_gcloud))
        assert "taken_over" not in _read_state(fake_gcloud)
        assert provider.lapsed_holders() == ()

    def test_a_lease_taken_records_this_boot_as_this_machines(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        _gcp(tmp_path).ensure_up()
        recorded = (tmp_path / "locks" / BOOTS_FILE).read_text().split()
        assert recorded == [boot_id()]

    def test_a_holder_whose_pid_is_gone_is_taken_over(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        holder = _holder(pid=_dead_pid())
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        provider = _gcp(tmp_path)
        ledger = MemoryTakeoverLedger()
        provider.record_takeovers_in(ledger)
        provider.ensure_up()
        assert _read_state(fake_gcloud)["taken_over"] == 1
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "settled"]

    def test_a_lease_that_changed_before_the_takeover_is_read_again_not_taken(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2874: the dead holder's lease expired between the hold and the takeover.

        The takeover is settled as refused because the lease changed, never as
        taken, no marker is written, and the next hold takes the free lease.
        """
        holder = _holder(pid=_dead_pid())
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            lease_changed_at_takeover=True,
        )
        provider = _gcp(tmp_path)
        ledger = MemoryTakeoverLedger()
        provider.record_takeovers_in(ledger)
        provider.ensure_up()
        state = _read_state(fake_gcloud)
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "lease_changed"]
        assert "takeover_marker" not in state
        assert "taken_over" not in state
        assert provider.lapsed_holders() == ()

    @pytest.mark.parametrize("code", [1, 255])
    def test_a_takeover_that_fails_after_its_marker_is_settled_as_taken(
        self, tmp_path: Path, fake_gcloud: Path, code: int
    ) -> None:
        """#2874: the lease and marker were written, then the hold failed.

        The instance's marker names the takeover, so it goes on record as
        taken before the hold's error is raised — never left pending.
        """
        holder = _holder(pid=_dead_pid())
        _write_state(
            fake_gcloud, lease_held=True, lease_holder=holder, takeover_exit=code
        )
        provider = _gcp(tmp_path)
        ledger = MemoryTakeoverLedger()
        provider.record_takeovers_in(ledger)
        with pytest.raises(HostError):
            provider.ship(_bundle(tmp_path))
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "settled"]
        assert len({takeover for _, _, takeover in ledger.writes}) == 1
        assert "copies" not in _read_state(fake_gcloud)

    @pytest.mark.parametrize("code", [1, 255])
    def test_a_failed_takeover_the_marker_does_not_name_is_unconfirmed(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
        code: int,
    ) -> None:
        """#2874: a marker read, and fenced, that does not name it is unconfirmed."""
        holder = _holder(pid=_dead_pid())
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            takeover_exit=code,
            takeover_unmarked=True,
        )
        provider = _gcp(tmp_path)
        ledger = MemoryTakeoverLedger()
        provider.record_takeovers_in(ledger)
        with pytest.raises(HostError):
            provider.ship(_bundle(tmp_path))
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "unconfirmed"]
        assert "copies" not in _read_state(fake_gcloud)

    @pytest.mark.parametrize("code", [1, 255])
    def test_a_failed_takeover_whose_fence_failed_stays_pending(
        self, tmp_path: Path, fake_gcloud: Path, code: int
    ) -> None:
        """#2978: no fence written, so nothing is settled on no evidence.

        The marker-and-fence read fails like ssh does; the takeover stays
        pending, and the next hold, which fences it, settles it unconfirmed.
        """
        holder = _holder(pid=_dead_pid())
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            takeover_exit=code,
            takeover_unmarked=True,
            marker_unreadable=True,
        )
        ledger = MemoryTakeoverLedger()
        provider = _gcp(tmp_path)
        provider.record_takeovers_in(ledger)
        with pytest.raises(HostError):
            provider.ship(_bundle(tmp_path))
        assert [kind for kind, _, _ in ledger.writes] == ["pending"]
        (takeover,) = {takeover for _, _, takeover in ledger.writes}
        assert takeover not in _read_state(fake_gcloud).get("fenced", [])

        later = _gcp(tmp_path)
        later.record_takeovers_in(ledger)
        later.ensure_up()
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "unconfirmed"]
        assert takeover in _read_state(fake_gcloud)["fenced"]

    def test_a_late_takeover_after_a_failed_fence_is_settled_as_taken(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2978: the takeover's ssh timed out; its session lands later.

        The read that would fence it fails, so the takeover is left pending
        rather than settled unconfirmed. The delayed session then takes the
        lease and writes the marker, and the next hold — fencing it first —
        settles it as taken: the record never says a lease taken was not.
        """
        holder = _holder(pid=_dead_pid())
        _write_state(
            fake_gcloud,
            lease_held=True,
            lease_holder=holder,
            takeover_dropped=True,
            marker_unreadable=True,
        )
        ledger = MemoryTakeoverLedger()
        provider = _gcp(tmp_path)
        provider.record_takeovers_in(ledger)
        with pytest.raises(HostError):
            provider.ship(_bundle(tmp_path))
        assert [kind for kind, _, _ in ledger.writes] == ["pending"]
        (takeover,) = {takeover for _, _, takeover in ledger.writes}

        (dropped,) = [
            call
            for call in _calls(fake_gcloud)
            if "--command" in call
            and f"{takeover} " in call[call.index("--command") + 1]
            and "> /run/kew-host.lease.takeover" in call[call.index("--command") + 1]
        ]
        # The fixture put the shim first on PATH: this is the late session.
        late = subprocess.run(
            ["gcloud", *dropped], capture_output=True, text=True, check=False
        )
        assert late.returncode == 0, late.stderr
        _write_state(fake_gcloud, marker_unreadable=False)

        later = _gcp(tmp_path)
        later.record_takeovers_in(ledger)
        later.ensure_up()
        assert [kind for kind, _, _ in ledger.writes] == ["pending", "settled"]
        state = _read_state(fake_gcloud)
        assert state["takeover_marker"][0] == takeover
        assert takeover in state["fenced"]

    def test_a_fenced_takeover_refuses_in_a_real_shell_and_another_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2978: the fence a hold writes is the one a late takeover reads.

        The two fragments run in ``/bin/sh`` against a fence file of the
        test's own: the fenced id exits 7 before anything after it runs, an
        id nobody fenced falls through, and so does every id with no file.
        """
        fence = tmp_path / "fenced"
        monkeypatch.setattr(gcp_module, "TAKEOVER_FENCE_FILE", str(fence))
        fenced, other = "a" * 32, "b" * 32

        def takeover(takeover_id: str) -> subprocess.CompletedProcess[str]:
            script = gcp_module._refuse_fenced(takeover_id) + "echo LEASE-WRITTEN"
            return subprocess.run(
                ["/bin/sh", "-c", script], capture_output=True, text=True, check=False
            )

        unfenced = takeover(fenced)
        subprocess.run(
            ["/bin/sh", "-c", gcp_module._fence_takeovers(["c" * 32, fenced])],
            check=True,
        )
        refused, allowed = takeover(fenced), takeover(other)

        assert (unfenced.returncode, unfenced.stdout) == (0, "LEASE-WRITTEN\n")
        assert refused.returncode == gcp_module.TAKEOVER_FENCED_EXIT
        assert "LEASE-WRITTEN" not in refused.stdout
        assert (allowed.returncode, allowed.stdout) == (0, "LEASE-WRITTEN\n")
        assert fence.read_text().split() == ["c" * 32, fenced]

    def test_a_takeover_with_no_ledger_is_refused_before_the_instance_is_asked(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        """#2874: a takeover nobody can read about is not taken, nor shipped over."""
        holder = _holder(pid=_dead_pid())
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        provider = _gcp(tmp_path)
        with pytest.raises(HostError, match="nothing is installed to record"):
            provider.ship(_bundle(tmp_path))
        assert not any("pkill" in c for c in _commands(fake_gcloud))
        state = _read_state(fake_gcloud)
        assert "taken_over" not in state
        assert "copies" not in state

    def test_a_live_pid_of_this_machine_is_still_refused(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        holder = _holder(pid=os.getppid())
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        provider = _gcp(tmp_path)
        with pytest.raises(HostBusy, match=f"held by {holder}"):
            provider.ship(_bundle(tmp_path))
        assert not any(f'[ "$tok" = {holder} ]' in c for c in _commands(fake_gcloud))
        assert "taken_over" not in _read_state(fake_gcloud)
        assert "copies" not in _read_state(fake_gcloud)
        assert provider.lapsed_holders() == ()

    @pytest.mark.parametrize(
        "boot", [None, uuid.uuid4().hex], ids=["same-boot-id", "other-boot-id"]
    )
    def test_another_machines_holder_is_refused_until_its_deadline(
        self, tmp_path: Path, fake_gcloud: Path, boot: str | None
    ) -> None:
        holder = _holder(machine="another-mac", pid=_dead_pid(), boot=boot)
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        with pytest.raises(HostBusy, match="still leased to another kew job"):
            _gcp(tmp_path).ensure_up()
        assert not any(f'[ "$tok" = {holder} ]' in c for c in _commands(fake_gcloud))
        assert "taken_over" not in _read_state(fake_gcloud)

    def test_a_token_that_names_no_boot_is_refused(
        self, tmp_path: Path, fake_gcloud: Path
    ) -> None:
        me = parse_lease_token(lease_token())
        assert me is not None
        holder = f"{me.machine}-{_dead_pid()}-{'0' * 12}"
        _write_state(fake_gcloud, lease_held=True, lease_holder=holder)
        with pytest.raises(HostBusy):
            _gcp(tmp_path).ensure_up()
        assert "taken_over" not in _read_state(fake_gcloud)

    def test_concurrent_first_calls_mint_one_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #2811: shards lease from several threads at once.

        The boot id is read through a FIFO, so every first call blocks in that
        read until the test writes it once: without one minting at a time,
        each waiting reader mints its own token, and a shard's renewal then
        reads as a stranger's.
        """
        fifo = tmp_path / "boot_id"
        os.mkfifo(fifo)
        monkeypatch.setattr(gcp_module, "_LINUX_BOOT_ID", fifo)
        monkeypatch.setattr(gcp_module, "_LEASE_TOKENS", {})
        barrier = threading.Barrier(4)
        tokens: list[str] = []

        def first_call() -> None:
            barrier.wait()
            tokens.append(lease_token())

        threads = [threading.Thread(target=first_call, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        with fifo.open("w", encoding="ascii") as boot:
            boot.write(uuid.uuid4().hex + "\n")
        for thread in threads:
            thread.join(timeout=5)

        assert len(tokens) == 4
        assert len(set(tokens)) == 1

    def test_this_process_names_its_machine_pid_and_boot(self) -> None:
        holder = parse_lease_token(lease_token())
        assert holder is not None
        assert holder.pid == os.getpid()
        assert holder.boot == boot_id()
        assert not lease_lapsed(lease_token(), lock_dir=Path("/nonexistent"))


class TestTheTakeoverCommandStopsTheOrphanedJob:
    """The takeover, run for real under ``/bin/sh``: the job's scope is killed."""

    _run = staticmethod(TestTheLeaseCommandsDoWhatTheySay._run)
    _lease = staticmethod(TestTheLeaseCommandsDoWhatTheySay._lease)

    @staticmethod
    def _record(root: Path) -> Path:
        """Root's scope record, where the fake ``sudo`` puts ``/run``."""
        return root / "systemd" / JOB_SESSION_FILE.lstrip("/")

    @classmethod
    def _orphan(
        cls, root: Path, token: str, then: str = "exec sleep 60"
    ) -> subprocess.Popen[bytes]:
        """A job in a session and a scope of its own, contained the way a job
        is, running *then* once its scope is active."""
        bin_dir = root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_fake_systemd(bin_dir)
        install_fake_docker(bin_dir)
        _instance_tools_where_missing(bin_dir)
        job = start_in_cgroup(
            bin_dir,
            [
                "/bin/sh",
                "-c",
                TestTheLeaseCommandsDoWhatTheySay._moved(
                    root, job_session_record(token) + "touch contained; " + then
                ),
            ],
            cwd=root,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            start_new_session=True,
        )
        deadline = time.monotonic() + _STARTED_SECONDS
        while not (root / "contained").exists():
            assert time.monotonic() < deadline, "the job never contained itself"
            time.sleep(0.02)
        (root / "contained").unlink()
        return job

    @staticmethod
    def _escapee(root: Path) -> int:
        """The pid a job's ``setsid`` child wrote once it was running."""
        written = root / "escaped.pid"
        deadline = time.monotonic() + _STARTED_SECONDS
        while not written.exists() or not written.read_text().strip():
            assert time.monotonic() < deadline, "the escapee never started"
            time.sleep(0.02)
        return int(written.read_text())

    #: A job that detaches a child into a session of its own, then exits 0.
    _ESCAPE = (
        "setsid sh -c 'echo $$ > escaped.pid.tmp && "
        "mv escaped.pid.tmp escaped.pid && exec sleep 60' "
        "</dev/null >/dev/null 2>&1 & "
        "while [ ! -s escaped.pid ]; do sleep 0.02; done; exit 0"
    )

    def test_the_lapsed_holders_job_is_killed_and_its_lease_taken(
        self, stub_root: Path
    ) -> None:
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        token, unit, _ = self._record(stub_root).read_text().split()
        assert (token, unit.startswith("kew-job-")) == (holder, True)

        code, armed = self._run(
            stub_root,
            hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed=holder),
        )

        assert code == 0
        assert job.wait(timeout=5) == -signal.SIGKILL
        assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        assert DEADLINE_STOP_UNIT in armed

    def test_a_takeover_is_marked_on_the_instance_and_read_back_by_the_next_hold(
        self, stub_root: Path
    ) -> None:
        """#2874: the next operator learns which pending takeover went through."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        job.kill()
        job.wait()
        takeover = "c" * 32

        code, _ = self._run(
            stub_root,
            hold_command(
                "me-1-a",
                seconds=600,
                idle_minutes=30,
                lapsed=holder,
                takeover=takeover,
            ),
        )
        assert code == 0
        (stub_root / "lease").unlink()
        listing = self._run_with_output(
            stub_root, hold_command("me-2-d", seconds=600, idle_minutes=30)
        )

        assert f"last takeover {takeover} of {holder} by me-1-a" in listing

    def _run_with_output(self, root: Path, command: str) -> str:
        """*command*'s stdout, with the lease files redirected under *root*."""
        bin_dir = root / "leasebin"
        ran = subprocess.run(
            ["/bin/sh", "-c", command.replace(LEASE_FILE, str(root / "lease"))],
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == 0, ran.stderr
        return ran.stdout

    @pytest.mark.parametrize("seconds_left", [-5, None], ids=["expired", "released"])
    def test_a_lease_that_changed_since_it_was_read_is_not_taken_over(
        self, stub_root: Path, seconds_left: int | None
    ) -> None:
        """#2874: the holder seen lapsed no longer holds the lease under the lock.

        Its job is not stopped, no lease is written and no takeover is marked:
        the answer is "read the lease again", never a takeover.
        """
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        if seconds_left is None:
            (stub_root / "lease").unlink()
        else:
            self._lease(stub_root, holder, seconds_left)
        before = self._lease_text(stub_root)
        try:
            code, armed = self._run(
                stub_root,
                hold_command(
                    "me-1-a",
                    seconds=600,
                    idle_minutes=30,
                    lapsed=holder,
                    takeover="c" * 32,
                ),
            )
            assert (code, armed) == (LEASE_CHANGED_EXIT, "")
            assert job.poll() is None
            assert self._lease_text(stub_root) == before
            assert not (stub_root / "lease.takeover").exists()
        finally:
            job.kill()
            job.wait()

    @staticmethod
    def _lease_text(root: Path) -> str | None:
        lease = root / "lease"
        return lease.read_text() if lease.exists() else None

    def test_a_record_of_another_job_kills_nothing_and_takes_nothing(
        self, stub_root: Path
    ) -> None:
        """No record names the holder, so its job may still be running."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, "someone-else-1-c", 600)
        job = self._orphan(stub_root, "someone-else-1-c")
        self._lease(stub_root, holder, 600)
        try:
            code, armed = self._run(
                stub_root,
                hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed=holder),
            )
            assert (code, armed) == (LEASE_HELD_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == holder
        finally:
            job.kill()
            job.wait()

    def test_takeover_refuses_when_session_record_is_missing(
        self, stub_root: Path
    ) -> None:
        """A job the takeover cannot find is a job it cannot stop (#2811)."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        code, armed = self._run(
            stub_root,
            hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed=holder),
        )
        assert (code, armed) == (LEASE_HELD_EXIT, "")
        assert (stub_root / "lease").read_text().split()[0] == holder

    def test_takeover_refuses_when_the_kill_is_unconfirmed(
        self, stub_root: Path
    ) -> None:
        """A scope still running after the kill keeps its lease (#2811).

        ``systemctl kill`` is a no-op here, so the job survives it the way one
        stuck in an uninterruptible wait would: shipping now would delete its
        checkout.
        """
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        (stub_root / "systemd" / KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root,
                hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed=holder),
            )
            assert (code, armed) == (LEASE_HELD_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == holder
        finally:
            job.kill()
            job.wait()

    def test_a_finished_jobs_holder_is_taken_over(self, stub_root: Path) -> None:
        """The record names the holder and nothing is left in its scope."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        job.kill()
        job.wait()
        code, _ = self._run(
            stub_root,
            hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed=holder),
        )
        assert code == 0
        assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

    def test_a_job_whose_session_cannot_be_recorded_never_starts(
        self, stub_root: Path
    ) -> None:
        """No record, no job: a takeover could never stop it (#2811)."""
        self._lease(stub_root, "me-1-a", 600)
        bin_dir = stub_root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_fake_systemd(bin_dir)
        self._record(stub_root).mkdir()
        ran = subprocess.run(
            [
                "/bin/sh",
                "-c",
                TestTheLeaseCommandsDoWhatTheySay._moved(
                    stub_root,
                    marker_command(job_session_record("me-1-a") + "touch started"),
                ),
            ],
            cwd=stub_root,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == JOB_UNRECORDED_EXIT
        assert EXIT_MARKER not in ran.stdout
        assert "so it was not started" in ran.stderr
        assert not (stub_root / "started").exists()

    def test_a_release_kills_its_own_job_that_outlived_the_ssh_session(
        self, stub_root: Path
    ) -> None:
        """#2811: a SIGTERM ends only the operator's side of the session.

        The job here is the VM-side process left behind: in a session of its
        own, still running after the ssh session that started it has gone. It
        must be dead before the lease that keeps the next ship off its
        checkout is removed.
        """
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a")

        code, armed = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )

        assert code == 0
        assert job.wait(timeout=5) == -signal.SIGKILL
        assert not (stub_root / "lease").exists()
        assert f"--on-active=30m --unit={IDLE_STOP_UNIT}" in armed

    def test_a_release_whose_job_survives_the_kill_keeps_its_lease(
        self, stub_root: Path
    ) -> None:
        """``systemctl kill`` is a no-op here, so the job outlives it (#2811)."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a")
        (stub_root / "systemd" / KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            job.kill()
            job.wait()

    def test_a_job_that_escapes_with_setsid_and_exits_0_is_still_stopped(
        self, stub_root: Path
    ) -> None:
        """#2876: the escapee left the job's session, not the job's scope.

        It must be dead before the lease that keeps the next ship off its
        checkout is removed.
        """
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then=self._ESCAPE)
        assert job.wait(timeout=5) == 0
        escapee = self._escapee(stub_root)
        assert os.getsid(escapee) != job.pid  # the job led its own session

        code, _ = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )

        assert code == 0
        assert not (stub_root / "lease").exists()
        assert not _pid_alive(escapee)

    def test_an_escapee_that_survives_the_kill_blocks_the_release(
        self, stub_root: Path
    ) -> None:
        """#2876: the job exited 0, but its scope is not empty, so the lease
        stays."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then=self._ESCAPE)
        assert job.wait(timeout=5) == 0
        escapee = self._escapee(stub_root)
        (stub_root / "systemd" / KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            assert _pid_alive(escapee)
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            os.kill(escapee, signal.SIGKILL)

    def test_a_job_that_rewrites_the_old_session_record_cannot_redirect_the_stop(
        self, stub_root: Path
    ) -> None:
        """#2876: the job can write its own workspace, so the stop reads
        nothing there; a decoy named by that record is left alone."""
        decoy = subprocess.Popen(
            ["/bin/sh", "-c", "exec sleep 60"], start_new_session=True
        )
        self._lease(stub_root, "me-1-a", 600)
        rewrite = (
            "mkdir -p kew-host && "
            f"echo 'me-1-a {decoy.pid}' > kew-host/job.session && exec sleep 60"
        )
        job = self._orphan(stub_root, "me-1-a", then=rewrite)
        try:
            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert code == 0
            assert job.wait(timeout=5) == -signal.SIGKILL
            assert decoy.poll() is None
        finally:
            decoy.kill()
            decoy.wait()

    def test_a_job_that_rewrites_the_scope_record_through_sudo_is_still_stopped(
        self, stub_root: Path
    ) -> None:
        """#2876: the login has passwordless ``sudo``, so the job can rewrite
        root's record to name another scope; the stop kills every job scope,
        so the lease is dropped only once the job itself is dead. The rewrite
        keeps a start time: one with none leaves no start to stop containers
        from, and keeps the lease (the no-record tests below)."""
        self._lease(stub_root, "me-1-a", 600)
        rewrite = (
            "sudo -n sh -c "
            f'"echo me-1-a kew-job-deadbeef $(date +%s) > {JOB_SESSION_FILE}" '
            "&& exec sleep 60"
        )
        job = self._orphan(stub_root, "me-1-a", then=rewrite)
        try:
            deadline = time.monotonic() + _STARTED_SECONDS
            while "deadbeef" not in self._record(stub_root).read_text():
                assert time.monotonic() < deadline, "the job never rewrote it"
                time.sleep(0.02)

            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )

            assert code == 0
            assert job.wait(timeout=5) == -signal.SIGKILL
            assert not (stub_root / "lease").exists()
        finally:
            if job.poll() is None:
                job.kill()
                job.wait()

    def test_the_scope_record_is_written_only_through_sudo(
        self, stub_root: Path
    ) -> None:
        """#2876: a login whose ``sudo`` refuses gets no record and no job."""
        self._lease(stub_root, "me-1-a", 600)
        bin_dir = stub_root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_fake_systemd(bin_dir)
        _write_stub(bin_dir, "sudo", "#!/bin/sh\nexit 1\n")
        ran = subprocess.run(
            [
                "/bin/sh",
                "-c",
                TestTheLeaseCommandsDoWhatTheySay._moved(
                    stub_root,
                    marker_command(job_session_record("me-1-a") + "touch started"),
                ),
            ],
            cwd=stub_root,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == JOB_UNRECORDED_EXIT
        assert JOB_SESSION_FILE.startswith("/run/")
        assert not self._record(stub_root).exists()
        assert not (stub_root / "started").exists()

    @staticmethod
    def _container_pids(root: Path) -> list[int]:
        """The process of every container the fake ``docker`` ran."""
        records = (root / "docker" / "containers").iterdir()
        return [json.loads(r.read_text())["pid"] for r in records]

    #: A job that leaves a detached container behind, then exits 0.
    _DETACH = "docker run -d busybox sleep 60 >/dev/null && exit 0"

    def test_an_expired_foreign_lease_with_a_live_scope_is_stopped_first(
        self, stub_root: Path
    ) -> None:
        """#2876: a lease past its deadline is not proof its job ended; the
        job is killed before the lease that kept a ship off its checkout is
        overwritten."""
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b")
        self._lease(stub_root, "other-2-b", -5)
        try:
            code, _ = self._run(
                stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
            )
            assert code == LEASE_HELD_EXIT
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "other-2-b"
            code, _ = self._run(
                stub_root,
                hold_command(
                    "me-1-a",
                    seconds=600,
                    idle_minutes=30,
                    lapsed="other-2-b",
                    expired=True,
                ),
            )
            assert code == 0
            assert job.wait(timeout=5) == -signal.SIGKILL
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            if job.poll() is None:
                job.kill()
                job.wait()

    def test_an_expired_lease_whose_scope_survives_the_kill_is_kept(
        self, stub_root: Path
    ) -> None:
        """#2876: the expired holder's job outlives ``systemctl kill``, so its
        lease is not overwritten and nothing is shipped over it."""
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b")
        self._lease(stub_root, "other-2-b", -5)
        (stub_root / "systemd" / KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root,
                hold_command(
                    "me-1-a",
                    seconds=600,
                    idle_minutes=30,
                    lapsed="other-2-b",
                    expired=True,
                ),
            )
            assert (code, armed) == (LEASE_HELD_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "other-2-b"
        finally:
            job.kill()
            job.wait()

    def test_renewing_its_own_expired_lease_leaves_its_job_running(
        self, stub_root: Path
    ) -> None:
        """#2876: the expired-lease stop is for another holder's job only."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a")
        self._lease(stub_root, "me-1-a", -5)
        try:
            code, _ = self._run(
                stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
            )
            assert code == 0
            assert job.poll() is None
        finally:
            job.kill()
            job.wait()

    def test_a_detached_container_blocks_the_release_until_it_is_stopped(
        self, stub_root: Path
    ) -> None:
        """#2876: a ``docker run -d`` container is the daemon's child, not in
        the job's scope; the lease is dropped only once it is dead."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then=self._DETACH)
        assert job.wait(timeout=5) == 0
        (container,) = self._container_pids(stub_root)
        (stub_root / "docker" / CONTAINER_KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            assert _pid_alive(container)
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

            (stub_root / "docker" / CONTAINER_KILL_IGNORED).unlink()
            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert code == 0
            assert not _pid_alive(container)
            assert not (stub_root / "lease").exists()
        finally:
            if _pid_alive(container):
                os.kill(container, signal.SIGKILL)

    def test_an_expired_holders_container_is_stopped_before_its_lease_is_taken(
        self, stub_root: Path
    ) -> None:
        """#2876: the containers count for an expired lease as for a live one."""
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b", then=self._DETACH)
        assert job.wait(timeout=5) == 0
        (container,) = self._container_pids(stub_root)
        self._lease(stub_root, "other-2-b", -5)
        try:
            code, _ = self._run(
                stub_root,
                hold_command(
                    "me-1-a",
                    seconds=600,
                    idle_minutes=30,
                    lapsed="other-2-b",
                    expired=True,
                ),
            )
            assert code == 0
            assert not _pid_alive(container)
        finally:
            if _pid_alive(container):
                os.kill(container, signal.SIGKILL)

    def test_a_container_running_before_the_job_started_is_left_alone(
        self, stub_root: Path
    ) -> None:
        """#2876: only containers started since the job's recorded start are
        the job's to stop."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        _, earlier = add_container(
            stub_root / "docker", datetime.now(UTC) - timedelta(hours=1)
        )
        try:
            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert code == 0
            assert earlier.poll() is None
        finally:
            earlier.kill()
            earlier.wait()

    def test_the_job_cannot_write_the_scope_record_without_sudo(
        self, stub_root: Path
    ) -> None:
        """#2876: the record is root's; the login writes it only through
        ``sudo``, and a stop still finds the job after the direct write fails."""
        record = self._record(stub_root)
        self._lease(stub_root, "me-1-a", 600)
        tamper = (
            f"if printf 'me-1-a kew-job-deadbeef\\n' > {shlex.quote(str(record))}; "
            "then touch tampered; else touch refused; fi; exec sleep 60"
        )
        job = self._orphan(stub_root, "me-1-a", then=tamper)
        try:
            deadline = time.monotonic() + _STARTED_SECONDS
            while not any((stub_root / f).exists() for f in ("tampered", "refused")):
                assert time.monotonic() < deadline, "the job never tried the write"
                time.sleep(0.02)
            assert not (stub_root / "tampered").exists()
            assert record.stat().st_mode & 0o022 == 0
            assert "deadbeef" not in record.read_text()

            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert code == 0
            assert job.wait(timeout=5) == -signal.SIGKILL
        finally:
            if job.poll() is None:
                job.kill()
                job.wait()

    def test_a_collected_scope_is_confirmed_gone_and_releases_the_lease(
        self, stub_root: Path
    ) -> None:
        """#2876: systemd unloads a finished transient scope, so a stop that
        asks after it is told the unit is not found: that is a scope gone."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        _, unit, _ = self._record(stub_root).read_text().split()

        code, _ = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )

        assert code == 0
        assert not (stub_root / "lease").exists()
        assert not (stub_root / "systemd" / "scopes" / f"{unit}.scope").exists()

    def test_a_scope_whose_state_cannot_be_read_keeps_the_lease(
        self, stub_root: Path
    ) -> None:
        """#2876: a ``systemctl show`` that fails for any reason but a unit
        not found proves nothing, so the release keeps the lease."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        (stub_root / "systemd" / SHOW_FAILS).touch()

        code, armed = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )

        assert (code, armed) == (JOB_SURVIVES_EXIT, "")
        assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

    def test_a_running_container_that_cannot_be_inspected_keeps_the_lease(
        self, stub_root: Path
    ) -> None:
        """#2876: a listed container whose start is unknown may be the job's,
        so cleanup is unconfirmed and the lease stays."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then=self._DETACH)
        assert job.wait(timeout=5) == 0
        (container,) = self._container_pids(stub_root)
        (stub_root / "docker" / CONTAINER_INSPECT_FAILS).touch()
        try:
            code, armed = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            assert _pid_alive(container)
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            if _pid_alive(container):
                os.kill(container, signal.SIGKILL)

    def test_a_release_after_a_job_no_record_names_keeps_its_lease(
        self, stub_root: Path
    ) -> None:
        """A job the release cannot find is a job it cannot prove stopped."""
        self._lease(stub_root, "me-1-a", 600)
        code, armed = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )
        assert (code, armed) == (JOB_SURVIVES_EXIT, "")
        assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

    def test_a_release_that_ran_no_job_needs_no_record(self, stub_root: Path) -> None:
        self._lease(stub_root, "me-1-a", 600)
        code, _ = self._run(stub_root, release_command("me-1-a", idle_minutes=30))
        assert code == 0
        assert not (stub_root / "lease").exists()

    def test_a_hand_drop_kills_the_holders_job_with_its_lease(
        self, stub_root: Path
    ) -> None:
        """``kew hosts release`` leaves no job running for the next ship."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)

        code, armed = self._run(stub_root, drop_holder_command(holder))

        assert code == 0
        assert job.wait(timeout=5) == -signal.SIGKILL
        assert not (stub_root / "lease").exists()
        assert armed == ""

    def test_a_hand_drop_of_a_holder_no_record_names_is_refused(
        self, stub_root: Path
    ) -> None:
        """A pre-upgrade holder left no record, so its live job cannot be
        stopped: its lease stays rather than reading as dropped."""
        self._lease(stub_root, "me-9-aaaaaaaaaaaa", 600)
        orphan = subprocess.Popen(
            ["/bin/sh", "-c", "exec sleep 60"], start_new_session=True
        )
        try:
            code, _ = self._run(stub_root, drop_holder_command("me-9-aaaaaaaaaaaa"))
            assert code == JOB_SURVIVES_EXIT
            assert orphan.poll() is None
            lease = (stub_root / "lease").read_text().split()[0]
            assert lease == "me-9-aaaaaaaaaaaa"
        finally:
            orphan.kill()
            orphan.wait()

    def test_a_forced_hand_drop_drops_the_lease_and_stops_nothing(
        self, stub_root: Path
    ) -> None:
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        try:
            code, armed = self._run(stub_root, drop_holder_command(holder, force=True))
            assert (code, armed) == (0, "")
            assert not (stub_root / "lease").exists()
            assert job.poll() is None
        finally:
            job.kill()
            job.wait()

    def test_a_drop_keeps_its_lease_while_its_job_survives_the_kill(
        self, stub_root: Path
    ) -> None:
        """#2876: ``drop_hold`` removes a lease like any release, so the work
        under it is proved stopped first, and one that survives keeps it."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a")
        (stub_root / "systemd" / KILL_IGNORED).touch()
        try:
            code, armed = self._run(stub_root, drop_command("me-1-a"))
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            job.kill()
            job.wait()

    def test_a_drop_stops_a_container_started_under_its_lease_first(
        self, stub_root: Path
    ) -> None:
        """#2876: a placed run's container is started under the lease, not in
        a job scope; the lease's recorded start finds it, and the lease goes
        only once it is stopped."""
        code, _ = self._run(
            stub_root, hold_command("me-1-a", seconds=600, idle_minutes=30)
        )
        assert code == 0
        started = TestTheLeaseCommandsDoWhatTheySay._shell(
            stub_root, "docker run -d busybox sleep 60"
        )
        assert started.returncode == 0
        (container,) = self._container_pids(stub_root)
        (stub_root / "docker" / CONTAINER_KILL_IGNORED).touch()
        try:
            code, _ = self._run(stub_root, drop_command("me-1-a"))
            assert code == JOB_SURVIVES_EXIT
            assert _pid_alive(container)
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

            (stub_root / "docker" / CONTAINER_KILL_IGNORED).unlink()
            code, armed = self._run(stub_root, drop_command("me-1-a"))
            assert (code, IDLE_STOP_UNIT in armed) == (0, False)
            assert not _pid_alive(container)
            assert not (stub_root / "lease").exists()
        finally:
            if _pid_alive(container):
                os.kill(container, signal.SIGKILL)

    def test_a_drop_leaves_an_expired_lease_of_another_holder_and_its_job(
        self, stub_root: Path
    ) -> None:
        """#2876: only a takeover stops another holder's job; a drop that
        found an expired foreign lease used to remove it, job still running."""
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b")
        self._lease(stub_root, "other-2-b", -5)
        try:
            for command in (
                drop_command("me-1-a"),
                release_command("me-1-a", idle_minutes=30),
            ):
                code, armed = self._run(stub_root, command)
                assert (code, armed) == (LEASE_HELD_EXIT, "")
                assert job.poll() is None
                assert (stub_root / "lease").read_text().split()[0] == "other-2-b"
        finally:
            job.kill()
            job.wait()

    def test_a_hand_drop_stops_an_expired_holders_job_before_its_lease(
        self, stub_root: Path
    ) -> None:
        """#2876: past its deadline is not proof the job ended; a hand drop
        without ``--force`` stops it whether or not the lease is live."""
        holder = "me-9-" + "a" * 32 + "-" + "b" * 12
        self._lease(stub_root, holder, 600)
        job = self._orphan(stub_root, holder)
        self._lease(stub_root, holder, -5)
        try:
            code, armed = self._run(stub_root, drop_holder_command(holder))

            assert (code, armed) == (0, "")
            assert job.wait(timeout=5) == -signal.SIGKILL
            assert not (stub_root / "lease").exists()
        finally:
            if job.poll() is None:
                job.kill()
                job.wait()

    def test_a_hand_drop_leaves_an_expired_lease_of_another_holder(
        self, stub_root: Path
    ) -> None:
        """#2876: the holder must match exactly, live lease or expired."""
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b")
        self._lease(stub_root, "other-2-b", -5)
        try:
            for force in (False, True):
                code, _ = self._run(
                    stub_root, drop_holder_command("dead-3-c", force=force)
                )
                assert code == LEASE_HELD_EXIT
                assert job.poll() is None
                assert (stub_root / "lease").read_text().split()[0] == "other-2-b"
        finally:
            job.kill()
            job.wait()

    def test_a_container_still_created_at_the_first_listing_keeps_the_lease(
        self, stub_root: Path
    ) -> None:
        """#2876: a ``docker run`` not yet through has a container that is
        created, not running; it counts as the job's until it is removed."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        created = add_created_container(stub_root / "docker", datetime.now(UTC))
        record = stub_root / "docker" / "containers" / created
        (stub_root / "docker" / CONTAINER_KILL_IGNORED).touch()

        code, armed = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )
        assert (code, armed) == (JOB_SURVIVES_EXIT, "")
        assert record.exists()
        assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

        (stub_root / "docker" / CONTAINER_KILL_IGNORED).unlink()
        code, _ = self._run(
            stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
        )
        assert code == 0
        assert not record.exists()
        assert not (stub_root / "lease").exists()

    def test_a_release_during_a_blocked_scope_start_keeps_the_lease(
        self, stub_root: Path
    ) -> None:
        """#2980: a scope recorded but not yet started is not a scope gone.
        The release waits for the start under the lease lock, then finds the
        job running in it."""
        self._lease(stub_root, "me-1-a", 600)
        state = stub_root / "systemd"
        bin_dir = stub_root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_fake_systemd(bin_dir)
        (state / KILL_IGNORED).touch()
        (state / START_BLOCKED).touch()
        job = start_in_cgroup(
            bin_dir,
            [
                "/bin/sh",
                "-c",
                TestTheLeaseCommandsDoWhatTheySay._moved(
                    stub_root, job_session_record("me-1-a") + "exec sleep 60"
                ),
            ],
            cwd=stub_root,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
            start_new_session=True,
        )
        release: dict[str, tuple[int, str]] = {}
        releaser = threading.Thread(
            target=lambda: release.update(
                ran=self._run(
                    stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
                )
            )
        )
        try:
            deadline = time.monotonic() + _STARTED_SECONDS
            while not (state / START_WAITING).exists():
                assert time.monotonic() < deadline, "the scope start never blocked"
                time.sleep(0.02)
            releaser.start()
            deadline = time.monotonic() + 10
            while releaser.is_alive() and not (state / LOCK_WAITED).exists():
                assert time.monotonic() < deadline, "the release never ran"
                time.sleep(0.02)
            (state / START_BLOCKED).unlink()
            releaser.join(timeout=30)

            assert release["ran"] == (JOB_SURVIVES_EXIT, "")
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            (state / START_BLOCKED).unlink(missing_ok=True)
            if releaser.is_alive():
                releaser.join(timeout=30)
            job.kill()
            job.wait()

    @staticmethod
    def _system_without_docker(root: Path) -> str:
        """A ``PATH`` of this machine's tools with no ``docker`` or ``dockerd``
        on it; ``flock`` and ``sudo`` come from the stubs."""
        system = root / "system-bin"
        system.mkdir(exist_ok=True)
        for directory in ("/usr/bin", "/bin"):
            for tool in Path(directory).iterdir():
                if tool.name not in ("docker", "dockerd", "flock", "sudo"):
                    with contextlib.suppress(FileExistsError):
                        (system / tool.name).symlink_to(tool)
        return str(system)

    def test_a_lease_that_ran_no_job_is_released_with_no_docker_client(
        self, stub_root: Path
    ) -> None:
        """#2980: a hold records only the lease's start, not a job. On an
        instance with no ``docker`` yet — a first provisioning that failed
        before installing it — a release of a lease that started nothing
        still frees it: no job of the holder could have left a container."""
        system = self._system_without_docker(stub_root)
        held = TestTheLeaseCommandsDoWhatTheySay._shell(
            stub_root,
            hold_command("me-1-a", seconds=600, idle_minutes=30),
            system_path=system,
            docker=False,
        )
        assert held.returncode == 0, held.stdout + held.stderr
        ran = TestTheLeaseCommandsDoWhatTheySay._shell(
            stub_root,
            release_command("me-1-a", idle_minutes=30),
            system_path=system,
            docker=False,
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert not (stub_root / "lease").exists()

    def test_a_missing_docker_client_keeps_the_lease_with_no_daemon_in_sight(
        self, stub_root: Path
    ) -> None:
        """#2980: with no ``docker`` to list them, a daemon's containers may
        be the job's. No socket at a standard path and no ``dockerd`` on
        ``PATH`` rule nothing out — a rootless daemon listens elsewhere — so
        the stop is unconfirmed and the lease stays."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        _, container = add_container(stub_root / "docker", datetime.now(UTC))
        (stub_root / "leasebin" / "docker").unlink()
        try:
            ran = TestTheLeaseCommandsDoWhatTheySay._shell(
                stub_root,
                release_command("me-1-a", idle_minutes=30, ran_job=True),
                system_path=self._system_without_docker(stub_root),
                docker=False,
            )
            assert ran.returncode == JOB_SURVIVES_EXIT
            assert "docker client is missing" in ran.stdout
            assert container.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"
        finally:
            container.kill()
            container.wait()

    def test_a_job_whose_lease_a_release_removed_first_never_starts(
        self, stub_root: Path
    ) -> None:
        """#2980: a release that takes the lease lock before the job's scope
        start removes the lease; the job, once it has the lock, finds the
        lease gone and records and starts nothing, so nothing runs unleased.

        The test holds the lock for the release, whose body then runs while
        the job waits on it: the order is fixed, not raced."""
        self._lease(stub_root, "me-1-a", 600)
        state = stub_root / "systemd"
        bin_dir = stub_root / "leasebin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        install_fake_systemd(bin_dir)
        run = state / "run"
        run.chmod(0o700)
        lock = run / _LEASE_LOCK.removeprefix("/run/")
        with lock.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            job = start_in_cgroup(
                bin_dir,
                [
                    "/bin/sh",
                    "-c",
                    TestTheLeaseCommandsDoWhatTheySay._moved(
                        stub_root,
                        marker_command(job_session_record("me-1-a") + "touch started"),
                    ),
                ],
                cwd=stub_root,
                env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + _STARTED_SECONDS
                while not (state / LOCK_WAITED).exists():
                    assert time.monotonic() < deadline, "the job never waited"
                    time.sleep(0.02)
                release = release_command("me-1-a", idle_minutes=30, ran_job=False)
                unlocked = release.replace(f"sudo flock {_LEASE_LOCK} ", "sudo ", 1)
                assert unlocked != release
                code, _ = self._run(stub_root, unlocked)
                assert code == 0
                assert not (stub_root / "lease").exists()
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
            out, err = job.communicate(timeout=30)

        assert job.returncode == JOB_UNRECORDED_EXIT
        assert EXIT_MARKER not in out
        assert "names nobody" in err
        assert not (stub_root / "started").exists()
        assert not self._record(stub_root).exists()

    def test_a_container_listed_only_after_the_first_listing_blocks_release(
        self, stub_root: Path
    ) -> None:
        """#2980: one empty listing can miss a container a ``docker run`` in
        flight is creating; it is caught by the second, a settle later.

        The container is listed only a quarter second after the first
        listing returns, so a second listing straight after the first misses
        it, and the gap between the two is a full settle."""
        self._lease(stub_root, "me-1-a", 600)
        job = self._orphan(stub_root, "me-1-a", then="exit 0")
        assert job.wait(timeout=5) == 0
        _, late = add_pending_container(
            stub_root / "docker", datetime.now(UTC), after=0.25
        )
        (stub_root / "docker" / CONTAINER_KILL_IGNORED).touch()
        try:
            code, armed = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert (code, armed) == (JOB_SURVIVES_EXIT, "")
            listings = [
                json.loads(line)
                for line in (stub_root / "docker" / CONTAINER_LISTINGS)
                .read_text()
                .splitlines()
            ]
            (_, first_ended), (second_began, _) = listings[:2]
            assert second_began - first_ended >= float(_CONTAINER_SETTLE)
            assert late.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "me-1-a"

            (stub_root / "docker" / CONTAINER_KILL_IGNORED).unlink()
            code, _ = self._run(
                stub_root, release_command("me-1-a", idle_minutes=30, ran_job=True)
            )
            assert code == 0
            assert late.wait(timeout=5) == -signal.SIGKILL
            assert not (stub_root / "lease").exists()
        finally:
            if late.poll() is None:
                late.kill()
                late.wait()

    def test_a_hand_drop_of_one_holder_leaves_anothers_lease_and_job(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "other-2-b", 600)
        job = self._orphan(stub_root, "other-2-b")
        try:
            code, _ = self._run(stub_root, drop_holder_command("dead-3-c"))
            assert code == LEASE_HELD_EXIT
            assert job.poll() is None
            assert (stub_root / "lease").read_text().split()[0] == "other-2-b"
        finally:
            job.kill()
            job.wait()

    def test_naming_one_lapsed_holder_does_not_take_anothers_lease(
        self, stub_root: Path
    ) -> None:
        self._lease(stub_root, "other-2-b", 600)
        code, armed = self._run(
            stub_root,
            hold_command("me-1-a", seconds=600, idle_minutes=30, lapsed="dead-3-c"),
        )
        assert (code, armed) == (LEASE_HELD_EXIT, "")
        assert (stub_root / "lease").read_text().split()[0] == "other-2-b"


class _NoSysctlByName:
    """A loaded library without ``sysctlbyname``, as a libc that cannot say
    which boot this is; everything else it answers as loaded."""

    def __init__(self, library: Any) -> None:
        self._library = library

    def __getattr__(self, name: str) -> Any:
        if name == "sysctlbyname":
            raise AttributeError(name)
        return getattr(self._library, name)


class TestHostsReleaseCommand:
    """``kew hosts release <name> --holder <token>`` drops a named lease."""

    @staticmethod
    def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kew.toml naming the gcp instance, so the command loads its own."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "kew.toml").write_text(
            "[logging]\n"
            f"dir = {str(tmp_path / 'logs')!r}\n"
            "[hosts.gcp]\n"
            'project = "proj"\n'
            'zone = "us-central1-a"\n'
            'instance = "verify-1"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("KEW_PROJECT", str(project))
        monkeypatch.chdir(project)
        monkeypatch.setenv(HOST_LOCK_DIR_ENV, str(tmp_path / "locks"))

    def test_it_drops_the_named_lease(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        holder = _holder(machine="another-mac", pid=1)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 0, result.output
        dropped = [c for c in _commands(fake_gcloud) if LEASE_FILE in c]
        assert len(dropped) == 1
        assert dropped[0].endswith(drop_holder_command(holder))

    def test_a_live_holder_on_this_machine_is_refused(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        holder = _holder(pid=os.getppid())
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 2
        assert "live kew process" in result.output
        assert not any(LEASE_FILE in c for c in _commands(fake_gcloud))

    def test_a_live_pid_is_refused_when_this_boot_cannot_be_read(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review of #2811: an unreadable boot proves nothing, so no drop."""
        self._configure(tmp_path, monkeypatch)
        holder = _holder(pid=os.getppid())
        monkeypatch.setattr(gcp_module, "_LINUX_BOOT_ID", tmp_path / "no-boot-id")
        # macOS reads its boot from libc instead (``kern.bootsessionuuid``).
        loaded = process_groups.ctypes.CDLL
        monkeypatch.setattr(
            process_groups.ctypes,
            "CDLL",
            lambda *args, **kwargs: _NoSysctlByName(loaded(*args, **kwargs)),
        )
        assert boot_id() is None
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 2
        assert "live kew process" in result.output
        assert not any(LEASE_FILE in c for c in _commands(fake_gcloud))

    def test_a_live_pid_in_a_token_with_no_boot_is_refused(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2811: a pre-upgrade token has no session record, so its job would
        survive the drop; its live pid is taken at its word."""
        self._configure(tmp_path, monkeypatch)
        me = parse_lease_token(lease_token())
        assert me is not None
        pid = os.getppid()
        holder = f"{me.machine}-{pid}-{'a' * 12}"
        assert parse_lease_token(holder) is None
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 2
        assert f"pid {pid}" in result.output
        assert not any(LEASE_FILE in c for c in _commands(fake_gcloud))

    def test_a_dead_pid_in_a_token_with_no_boot_can_be_dropped(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        me = parse_lease_token(lease_token())
        assert me is not None
        holder = f"{me.machine}-{_dead_pid()}-{'a' * 12}"
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 0, result.output
        assert any(LEASE_FILE in c for c in _commands(fake_gcloud))

    def test_a_same_named_machines_holder_from_another_boot_can_be_dropped(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its pid says nothing about this machine, so it is the operator's call."""
        self._configure(tmp_path, monkeypatch)
        holder = _holder(pid=os.getppid(), boot=uuid.uuid4().hex)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 0, result.output
        assert any(LEASE_FILE in c for c in _commands(fake_gcloud))

    def test_a_holder_whose_job_cannot_be_proved_stopped_is_refused(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #2811: no record, or a job that survives, keeps the lease."""
        self._configure(tmp_path, monkeypatch)
        _write_state(fake_gcloud, release_exit=JOB_SURVIVES_EXIT)
        holder = _holder(machine="another-mac", pid=1)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 2
        assert "--force drops it anyway and stops nothing" in result.output
        assert "Dropped" not in result.output

    def test_force_drops_the_lease_and_says_it_stopped_nothing(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        holder = _holder(machine="another-mac", pid=1)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder, "--force"]
        )
        assert result.exit_code == 0, result.output
        assert "stopped nothing" in " ".join(result.output.split())
        dropped = [c for c in _commands(fake_gcloud) if LEASE_FILE in c]
        assert len(dropped) == 1
        assert dropped[0].endswith(drop_holder_command(holder, force=True))
        assert "systemctl kill" not in dropped[0]

    def test_force_still_refuses_a_live_holder_on_this_machine(
        self, tmp_path: Path, fake_gcloud: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        holder = _holder(pid=os.getppid())
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder, "--force"]
        )
        assert result.exit_code == 2
        assert "live kew process" in result.output
        assert not any(LEASE_FILE in c for c in _commands(fake_gcloud))

    @pytest.mark.parametrize("status", ["SUSPENDED", "SUSPENDING", "STOPPING"])
    def test_an_instance_not_stopped_keeps_its_lease_and_says_so(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
    ) -> None:
        """#2874: a suspend keeps the instance's memory, and its lease with it."""
        self._configure(tmp_path, monkeypatch)
        _write_state(fake_gcloud, statuses=[status])
        holder = _holder(machine="another-mac", pid=1)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 2
        assert status in result.output
        assert "Dropped" not in result.output

    @pytest.mark.parametrize("status", ["TERMINATED", "STOPPED"])
    def test_a_stopped_instance_has_no_lease_left_to_drop(
        self,
        tmp_path: Path,
        fake_gcloud: Path,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
    ) -> None:
        self._configure(tmp_path, monkeypatch)
        _write_state(fake_gcloud, statuses=[status])
        holder = _holder(machine="another-mac", pid=1)
        result = CliRunner().invoke(
            app, ["hosts", "release", "gcp", "--holder", holder]
        )
        assert result.exit_code == 0, result.output
        assert not any(LEASE_FILE in c for c in _commands(fake_gcloud))


class TestTheLockIsKeyedByTheHost:
    """#2627 — two projects addressing one instance flock one inode."""

    def test_two_logging_dirs_lock_the_same_file_for_one_instance(
        self, tmp_path: Path
    ) -> None:
        first = DispatchConfig(
            logging={"dir": str(tmp_path / "a")},
            hosts=HostsConfig(gcp=_settings()),
        )
        second = DispatchConfig(
            logging={"dir": str(tmp_path / "b")},
            hosts=HostsConfig(gcp=_settings()),
        )
        one = resolve_provider(first)
        other = resolve_provider(second)
        with one.claim(), pytest.raises(HostBusy):
            other.ensure_up()

    def test_without_an_override_the_locks_live_under_the_users_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(HOST_LOCK_DIR_ENV, raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert host_lock_dir() == (tmp_path / "state" / "kew" / "hosts").resolve()
