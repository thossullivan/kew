"""Exercise the runner lock with real processes and isolated files, without Docker."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from time import sleep

import pytest

# Bind sleep before conftest patches selector.time.sleep on the shared time module.
SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/docker_daemon_lock.py"
#: The default hold bound (``_acquire_args``).
_HOLD_SECONDS = 30
#: How long a wait gives the holder to reach a state its rules put about a
#: second away, and each poll starts a Python process. Half the hold bound, so
#: a holder that only lets go when that bound runs out still fails the wait.
_PROMPT_SECONDS = _HOLD_SECONDS / 2


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _acquire_args(
    lock: Path, marker: Path, *, timeout: float = 5, hold: float = _HOLD_SECONDS
) -> list[str]:
    return [
        "acquire",
        "--lock",
        str(lock),
        "--marker",
        str(marker),
        "--timeout-seconds",
        str(timeout),
        "--hold-max-seconds",
        str(hold),
    ]


def _tracked_process(argv: list[str], *, cwd: Path | None = None) -> subprocess.Popen:
    child = subprocess.Popen(argv, cwd=cwd)
    listing = Path(os.environ["FAKE_PROCESS_LIST"])
    processes = json.loads(listing.read_text())
    processes.append([child.pid, " ".join(argv)])
    listing.write_text(json.dumps(processes))
    return child


@pytest.fixture(autouse=True)
def fake_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use shell processes and controlled state, without contacting Docker."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    workspace = tmp_path / "job workspace"
    workspace.mkdir()
    # The test controller is outside each job's isolated workspace.
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    containers = tmp_path / "containers"
    clients = tmp_path / "clients"
    containers.write_text("quiet\n")
    clients.write_text("quiet\n")
    docker = binaries / "docker"
    docker.write_text(
        '#!/bin/sh\n[ "$*" = "ps -q --filter name=kew-" ] || exit 2\n'
        'read -r state < "$FAKE_CONTAINERS"\n'
        'case "$state" in\n'
        '  busy) printf "container-id\\n";;\n'
        "  failed) exit 1;;\n"
        # exec keeps the hanging probe in one process for timeout cleanup.
        '  hung) exec "$FAKE_PYTHON" -c "import time; time.sleep(60)";;\n'
        "esac\n"
    )
    docker.chmod(0o755)
    pgrep = binaries / "pgrep"
    pgrep.write_text(
        '#!/bin/sh\n[ "$1" = "-f" ] || exit 2\n'
        '[ "$2" = "docker (build|buildx|image|pull|save|load)" ] || exit 2\n'
        'read -r state < "$FAKE_CLIENTS"\n'
        'case "$state" in\n'
        "  busy) exit 0;;\n"
        "  quiet) exit 1;;\n"
        "  *) exit 2;;\n"
        "esac\n"
    )
    pgrep.chmod(0o755)
    # Some test sandboxes forbid ps. Limit its view to this test's real child
    # processes. The native lsof probe still reads their actual cwd, and the
    # lock, signals, process exits, and waits remain real kernel operations.
    processes = tmp_path / "processes.json"
    processes.write_text(json.dumps([[os.getpid(), "pytest controller"]]))
    ps = binaries / "ps"
    ps.write_text(
        f"#!{sys.executable}\n"
        """import json
import os
from pathlib import Path
from time import sleep
import sys

def alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True

root = Path(os.environ["FAKE_PROCESS_LIST"]).parent
holders = {}
for marker in root.rglob("*.marker"):
    try:
        pid = json.loads(marker.read_text()).get("pid")
        if type(pid) is int and alive(pid):
            holders[pid] = "python docker_daemon_lock.py _hold"
    except (OSError, ValueError):
        pass
args = sys.argv[1:]
if args[:2] == ["-o", "ppid="]:
    print(os.environ["FAKE_RUNNER_PID"])
elif args[:2] == ["-o", "command="]:
    print(holders.get(int(args[3]), ""))
elif args == ["-axo", "pid,command", "-ww"]:
    print("  PID COMMAND")
    for pid, argv in [*json.loads(Path(os.environ["FAKE_PROCESS_LIST"]).read_text()),
                      *holders.items()]:
        if alive(pid):
            print(f"{pid} {argv}")
else:
    sys.exit(2)
"""
    )
    ps.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_CONTAINERS", str(containers))
    monkeypatch.setenv("FAKE_CLIENTS", str(clients))
    monkeypatch.setenv("FAKE_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_PROCESS_LIST", str(processes))
    monkeypatch.setenv("FAKE_RUNNER_PID", str(os.getpid()))
    return containers, clients, os.environ.copy()


@pytest.fixture
def worker() -> Iterator[subprocess.Popen]:
    child = _tracked_process([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def _wait_phase(marker: Path, phase: str) -> None:
    deadline = time.monotonic() + _PROMPT_SECONDS
    while time.monotonic() < deadline:
        if marker.exists() and json.loads(marker.read_text()).get("phase") == phase:
            return
        sleep(0.02)
    pytest.fail(f"marker did not enter {phase}")


def _wait_free(lock: Path) -> None:
    deadline = time.monotonic() + _PROMPT_SECONDS
    while time.monotonic() < deadline:
        if _run("status", "--lock", str(lock)).stdout.strip() == "free":
            return
        sleep(0.02)
    pytest.fail(f"holder did not free the lock within {_PROMPT_SECONDS:.0f} seconds")


def _assert_phases(marker: Path, *phases: str) -> None:
    assert json.loads(marker.read_text())["phase"] == phases[-1]
    lines = marker.with_name(marker.name + ".log").read_text().splitlines()
    assert [line.split("phase=", 1)[1] for line in lines if "phase=" in line] == list(
        phases
    )


@pytest.fixture
def lock_files(tmp_path: Path) -> Iterator[tuple[Path, Path, Path]]:
    yield (
        tmp_path / "daemon.lock",
        tmp_path / "first.marker",
        tmp_path / "second.marker",
    )
    for marker in tmp_path.glob("*.marker"):
        _run("release", "--marker", str(marker))


@pytest.mark.parametrize("survivor", ["container", "client"])
def test_worker_crash_drains_surviving_work(
    lock_files, fake_daemon, worker, survivor: str
) -> None:
    lock, first, second = lock_files
    containers, clients, env = fake_daemon
    state = containers if survivor == "container" else clients
    state.write_text("busy\n")
    acquired = _run(
        *_acquire_args(lock, first),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "1",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    _assert_phases(first, "holding")
    worker.kill()
    worker.wait(timeout=5)
    _wait_phase(first, "draining")
    assert _run(*_acquire_args(lock, second, timeout=0.3)).returncode == 2
    state.write_text("quiet\n")
    quiet_started = time.monotonic()
    assert _run(*_acquire_args(lock, second, timeout=0.3)).returncode == 2
    _wait_free(lock)
    assert time.monotonic() - quiet_started >= 1
    _assert_phases(first, "holding", "draining", "released-on-quiet")
    assert _run(*_acquire_args(lock, second)).returncode == 0
    assert lock.read_text() == ""


def test_worker_crash_defaults_keep_lock(lock_files, fake_daemon, worker) -> None:
    """Catch immediate release using flags that existed before orphan draining."""
    lock, first, second = lock_files
    containers, _, env = fake_daemon
    containers.write_text("busy\n")
    acquired = _run(
        *_acquire_args(lock, first), "--watch-pid", str(worker.pid), env=env
    )
    assert acquired.returncode == 0, acquired.stderr
    worker.kill()
    worker.wait(timeout=5)
    assert _run(*_acquire_args(lock, second, timeout=1)).returncode == 2


@pytest.mark.parametrize("unavailable", ["missing", "failed"])
def test_worker_crash_uses_grace_without_docker(
    lock_files, fake_daemon, worker, tmp_path: Path, unavailable: str
) -> None:
    lock, first, second = lock_files
    containers, _, env = fake_daemon
    if unavailable == "missing":
        empty = tmp_path / "empty-path"
        empty.mkdir()
        # Remove Docker, while retaining the independent process probes.
        for name in ("ps", "lsof"):
            executable = shutil.which(name)
            assert executable is not None
            (empty / name).symlink_to(executable)
        env["PATH"] = str(empty)
    else:
        containers.write_text("failed\n")
    acquired = _run(
        *_acquire_args(lock, first),
        "--watch-pid",
        str(worker.pid),
        "--orphan-grace-seconds",
        "1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    worker.kill()
    worker.wait(timeout=5)
    died = time.monotonic()
    _wait_phase(first, "draining")
    assert _run(*_acquire_args(lock, second, timeout=0.3)).returncode == 2
    _wait_free(lock)
    assert time.monotonic() - died >= 1
    _assert_phases(first, "holding", "draining", "released-on-grace")
    assert _run(*_acquire_args(lock, second)).returncode == 0


@pytest.mark.parametrize("probe", ["busy", "hung", "pgrep-failed"])
def test_hold_max_bounds_orphan_drain(
    lock_files, fake_daemon, worker, probe: str
) -> None:
    lock, first, second = lock_files
    containers, clients, env = fake_daemon
    if probe == "pgrep-failed":
        clients.write_text("failed\n")
    else:
        containers.write_text(probe + "\n")
    acquired = _run(
        *_acquire_args(lock, first, hold=2),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    worker.kill()
    worker.wait(timeout=5)
    _wait_phase(first, "draining")
    assert _run(*_acquire_args(lock, second, timeout=0.3)).returncode == 2
    _wait_free(lock)
    _assert_phases(first, "holding", "draining", "released-on-bound")
    assert _run(*_acquire_args(lock, second)).returncode == 0


def test_acquire_times_out_then_succeeds_after_release(lock_files) -> None:
    lock, first, second = lock_files
    acquired = _run(*_acquire_args(lock, first))
    assert acquired.returncode == 0, acquired.stderr
    record = json.loads(first.read_text())
    assert acquired.stdout.startswith(f"acquired pid={record['pid']} waited=")
    assert datetime.fromisoformat(record["acquired_at"]).utcoffset() is not None
    assert os.getsid(record["pid"]) == record["pid"]
    status = _run("status", "--lock", str(lock))
    assert status.returncode == 0 and status.stdout.strip() == "held"

    timed_out = _run(*_acquire_args(lock, second, timeout=0.2))
    assert timed_out.returncode == 2
    assert timed_out.stderr.strip() == "docker daemon lock: timed out after 0.2s"
    assert not second.exists()
    assert _run("release", "--marker", str(first)).returncode == 0
    assert not first.exists()

    acquired = _run(*_acquire_args(lock, second))
    assert acquired.returncode == 0, acquired.stderr
    assert _run("release", "--marker", str(second)).returncode == 0
    assert not second.exists()
    status = _run("status", "--lock", str(lock))
    assert status.returncode == 0 and status.stdout.strip() == "free"


def test_holder_expires_and_frees_lock(lock_files) -> None:
    lock, first, second = lock_files
    acquired = _run(*_acquire_args(lock, first, hold=1))
    assert acquired.returncode == 0, acquired.stderr
    deadline = time.monotonic() + _PROMPT_SECONDS
    while time.monotonic() < deadline:
        status = _run("status", "--lock", str(lock))
        if status.returncode == 0 and status.stdout.strip() == "free":
            break
        sleep(0.05)
    else:
        pytest.fail("holder did not free the lock after its one-second bound")
    assert _run(*_acquire_args(lock, second)).returncode == 0


@pytest.mark.parametrize("stale", [False, True], ids=["missing", "stale-pid"])
def test_release_is_idempotent(lock_files, stale: bool) -> None:
    _, marker, _ = lock_files
    if stale:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        marker.write_text(json.dumps({"pid": child.pid}))
    released = _run("release", "--marker", str(marker))
    assert released.returncode == 0, released.stderr
    assert not marker.exists()
    assert _run("release", "--marker", str(marker)).returncode == 0
    marker.write_text(json.dumps({"pid": 2**100}))
    assert _run("release", "--marker", str(marker)).returncode == 0
    assert not marker.exists()


def test_killed_holder_frees_lock(lock_files) -> None:
    lock, first, second = lock_files
    acquired = _run(*_acquire_args(lock, first))
    assert acquired.returncode == 0, acquired.stderr
    os.kill(json.loads(first.read_text())["pid"], signal.SIGKILL)
    acquired = _run(*_acquire_args(lock, second))
    assert acquired.returncode == 0, acquired.stderr


def test_multiple_waiters_all_acquire(lock_files, tmp_path: Path) -> None:
    lock, first, second = lock_files
    acquired = _run(*_acquire_args(lock, first))
    assert acquired.returncode == 0, acquired.stderr
    markers = [second, tmp_path / "third.marker", tmp_path / "fourth.marker"]
    waiters = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT), *_acquire_args(lock, marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for marker in markers
    ]
    try:
        sleep(0.2)
        assert all(waiter.poll() is None for waiter in waiters)
        assert not any(marker.exists() for marker in markers)
        assert _run("release", "--marker", str(first)).returncode == 0
        pending = set(range(len(waiters)))
        deadline = time.monotonic() + 8
        while pending and time.monotonic() < deadline:
            ready = [index for index in pending if markers[index].exists()]
            assert len(ready) <= 1, "multiple holders acquired the same lock"
            for index in ready:
                stdout, stderr = waiters[index].communicate(timeout=3)
                assert waiters[index].returncode == 0, stderr
                assert stdout.startswith("acquired pid=")
                assert _run("release", "--marker", str(markers[index])).returncode == 0
                pending.remove(index)
            sleep(0.02)
        assert not pending, "a waiting acquire was lost or cancelled"
    finally:
        for waiter in waiters:
            if waiter.poll() is None:
                waiter.terminate()
            waiter.communicate(timeout=5)


def test_holder_frees_lock_when_the_watched_worker_dies(lock_files) -> None:
    """An idle abandoned job drains before the hold bound."""
    _assert_watched_worker_death_frees_lock(lock_files)


def test_lsof_host_warnings_do_not_block_the_drain(lock_files, tmp_path) -> None:
    """Docker's nsfs netns mount makes Linux lsof warn on stderr; not an error."""
    real = shutil.which("lsof")
    if real is None:
        pytest.skip("lsof is not installed")
    warning = "lsof: WARNING: can't stat() nsfs file system /run/docker/netns/default"
    wrapper = tmp_path / "bin" / "lsof"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" -w "*) ;; *) echo {shlex.quote(warning)} >&2;; esac\n'
        f'exec {shlex.quote(real)} "$@"\n'
    )
    wrapper.chmod(0o755)
    _assert_watched_worker_death_frees_lock(lock_files)


def _assert_watched_worker_death_frees_lock(lock_files) -> None:
    lock, first, second = lock_files
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        acquired = _run(
            *_acquire_args(lock, first, hold=60),
            "--watch-pid",
            str(worker.pid),
            "--orphan-quiet-seconds",
            "1",
            "--orphan-poll-seconds",
            "0.1",
        )
        assert acquired.returncode == 0, acquired.stderr
        assert json.loads(first.read_text())["watch_pid"] == worker.pid
        worker.kill()
        worker.wait(timeout=5)
        deadline = time.monotonic() + _PROMPT_SECONDS
        while time.monotonic() < deadline:
            status = _run("status", "--lock", str(lock))
            if status.stdout.strip() == "free":
                break
            sleep(0.05)
        else:
            pytest.fail("holder kept the lock after its watched worker died")
        _assert_phases(first, "holding", "draining", "released-on-quiet")
        assert _run(*_acquire_args(lock, second)).returncode == 0
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def test_acquire_without_a_watch_target_still_holds(lock_files) -> None:
    """``--watch-pid 0`` disables watching; the hold bound alone applies."""
    lock, first, _ = lock_files
    acquired = _run(*_acquire_args(lock, first, hold=30), "--watch-pid", "0")
    assert acquired.returncode == 0, acquired.stderr
    assert json.loads(first.read_text())["watch_pid"] is None
    status = _run("status", "--lock", str(lock))
    assert status.stdout.strip() == "held"


def test_defaults_share_lock_across_job_marker_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "host-temp"
    shared.mkdir()
    # The production default is a fixed /tmp path; the override keeps this
    # test off the host's real CI lock (this machine is a runner).
    monkeypatch.setenv("KEW_CI_DOCKER_LOCK", str(shared / "kew-ci-docker-daemon.lock"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "unrelated-tmpdir"))
    markers = []
    try:
        for name in ("runner-one", "runner-two"):
            job_temp = tmp_path / name
            job_temp.mkdir()
            marker = job_temp / "kew-ci-docker-daemon.marker"
            markers.append(marker)
            monkeypatch.setenv("RUNNER_TEMP", str(job_temp))
            # The uncontended first acquisition gets a generous deadline (it
            # includes interpreter start-up under CI load); only the contended
            # second one must time out quickly.
            timeout = "5" if name == "runner-one" else "0.2"
            result = _run(
                "acquire", "--timeout-seconds", timeout, "--hold-max-seconds", "30"
            )
            if name == "runner-one":
                assert result.returncode == 0, result.stderr
                assert marker.exists()
            else:
                assert result.returncode == 2
                assert "timed out after 0.2s" in result.stderr
                assert not marker.exists()
        assert (shared / "kew-ci-docker-daemon.lock").exists()
    finally:
        for marker in markers:
            _run("release", "--marker", str(marker))


@pytest.mark.parametrize("identity", ["argv", "cwd"])
@pytest.mark.parametrize("daemon", ["quiet", "failed"])
def test_surviving_producer_blocks_quiet_and_grace(
    lock_files, fake_daemon, worker, identity: str, daemon: str
) -> None:
    lock, marker, _ = lock_files
    containers, _, env = fake_daemon
    containers.write_text(daemon + "\n")
    workspace = Path(env["GITHUB_WORKSPACE"])
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    if identity == "argv":
        argv.append(str(workspace / "next-build.py"))
    producer = _tracked_process(argv, cwd=workspace if identity == "cwd" else None)
    sibling = workspace.with_name(workspace.name + "-other")
    sibling.mkdir()
    unrelated = _tracked_process(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            str(sibling / "build.py"),
        ],
        cwd=sibling,
    )
    try:
        acquired = _run(
            *_acquire_args(lock, marker),
            "--watch-pid",
            str(worker.pid),
            "--orphan-quiet-seconds",
            "0.2",
            "--orphan-grace-seconds",
            "0.3",
            "--orphan-poll-seconds",
            "0.1",
            env=env,
        )
        assert acquired.returncode == 0, acquired.stderr
        worker.kill()
        worker.wait(timeout=5)
        _wait_phase(marker, "draining")
        sleep(0.8)
        assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
        assert producer.poll() is None
        record = json.loads(marker.read_text())
        assert record["workspace"] == str(workspace)
        assert record["watch_pid"] == worker.pid
        log = marker.with_suffix(".marker.log")
        deadline = time.monotonic() + _PROMPT_SECONDS
        while "surviving-producers=1" not in log.read_text():
            assert time.monotonic() < deadline, "producer count was not logged"
            sleep(0.02)
        producer.terminate()
        producer.wait(timeout=5)
        _wait_free(lock)
        phase = "released-on-quiet" if daemon == "quiet" else "released-on-grace"
        _assert_phases(marker, "holding", "draining", phase)
        assert "surviving-producers=0" in marker.with_suffix(".marker.log").read_text()
        assert unrelated.poll() is None, "a sibling workspace must not block release"
    finally:
        if producer.poll() is None:
            producer.kill()
        producer.wait(timeout=5)
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_acquire_records_cwd_when_workspace_is_unset(lock_files, fake_daemon) -> None:
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    env.pop("GITHUB_WORKSPACE")
    acquired = _run(*_acquire_args(lock, marker), env=env)
    assert acquired.returncode == 0, acquired.stderr
    assert json.loads(marker.read_text()).get("workspace") == str(Path.cwd())


def test_surviving_producer_is_bounded_by_hold_max(lock_files, fake_daemon, worker):
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    producer = _tracked_process(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            str(Path(env["GITHUB_WORKSPACE"]) / "next-build.py"),
        ]
    )
    try:
        acquired = _run(
            *_acquire_args(lock, marker, hold=2),
            "--watch-pid",
            str(worker.pid),
            "--orphan-quiet-seconds",
            "0.1",
            "--orphan-poll-seconds",
            "0.1",
            env=env,
        )
        assert acquired.returncode == 0, acquired.stderr
        worker.kill()
        worker.wait(timeout=5)
        _wait_free(lock)
        assert json.loads(marker.read_text())["phase"] == "released-on-bound"
        assert producer.poll() is None
    finally:
        producer.kill()
        producer.wait(timeout=5)


@pytest.mark.parametrize("probe", ["ps", "lsof"])
def test_process_probe_failure_cannot_establish_quiet(
    lock_files, fake_daemon, worker, probe: str
) -> None:
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    executable = Path(env["PATH"].split(os.pathsep)[0]) / probe
    real_probe = shutil.which(probe)
    assert real_probe is not None
    if Path(real_probe) == executable:
        # Preserve the controlled ps fixture for release's identity check.
        original = executable.with_suffix(".original")
        executable.rename(original)
        real_probe = str(original)
    executable.write_text(
        '#!/bin/sh\n[ "$1" = "-axo" ] || [ "$1" = "-a" ] && exit 2\n'
        f'exec "{real_probe}" "$@"\n'
    )
    executable.chmod(0o755)
    acquired = _run(
        *_acquire_args(lock, marker, hold=2),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.1",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    worker.kill()
    worker.wait(timeout=5)
    _wait_free(lock)
    assert json.loads(marker.read_text())["phase"] == "released-on-bound"
    assert (
        "surviving-producers=unknown" in marker.with_suffix(".marker.log").read_text()
    )


def test_sigusr1_drains_with_a_live_worker(lock_files, fake_daemon, worker) -> None:
    lock, marker, _ = lock_files
    containers, _, env = fake_daemon
    containers.write_text("busy\n")
    acquired = _run(
        *_acquire_args(lock, marker),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.2",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    os.kill(json.loads(marker.read_text())["pid"], signal.SIGUSR1)
    sleep(0.8)
    assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
    _wait_phase(marker, "draining")
    # Repeated requests must not stop draining or reset its deadlines.
    os.kill(json.loads(marker.read_text())["pid"], signal.SIGUSR1)
    containers.write_text("quiet\n")
    _wait_free(lock)
    _assert_phases(marker, "holding", "draining", "released-on-quiet")
    assert worker.poll() is None


@pytest.mark.parametrize("job_status", ["cancelled", "failure"])
def test_unsuccessful_release_waits_for_verified_drain(
    lock_files, fake_daemon, worker, job_status: str
) -> None:
    lock, marker, _ = lock_files
    containers, _, env = fake_daemon
    containers.write_text("busy\n")
    acquired = _run(
        *_acquire_args(lock, marker),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.2",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    releaser = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "release",
            "--marker",
            str(marker),
            "--job-status",
            job_status,
            "--release-wait-seconds",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        sleep(0.8)
        assert releaser.poll() is None, "release must wait while Docker is busy"
        _wait_phase(marker, "draining")
        assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
        containers.write_text("quiet\n")
        _, stderr = releaser.communicate(timeout=5)
        assert releaser.returncode == 0, stderr
        _wait_free(lock)
        _assert_phases(marker, "holding", "draining", "released-on-quiet")
    finally:
        if releaser.poll() is None:
            releaser.kill()
        releaser.communicate(timeout=5)


def test_unsuccessful_release_timeout_preserves_drain(lock_files, fake_daemon) -> None:
    lock, marker, _ = lock_files
    containers, _, env = fake_daemon
    containers.write_text("busy\n")
    acquired = _run(
        *_acquire_args(lock, marker),
        "--watch-pid",
        "0",
        "--orphan-quiet-seconds",
        "0.2",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    released = _run(
        "release",
        "--marker",
        str(marker),
        "--job-status",
        "cancelled",
        "--release-wait-seconds",
        "0.6",
        env=env,
    )
    assert released.returncode == 0, released.stderr
    _wait_phase(marker, "draining")
    assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
    containers.write_text("quiet\n")
    _wait_free(lock)
    _assert_phases(marker, "holding", "draining", "released-on-quiet")


def _environment_names(pid: int) -> set[str] | None:
    """Variable names of a live process, via /proc or the real BSD ps."""
    proc_environ = Path(f"/proc/{pid}/environ")
    if proc_environ.exists():
        return {
            item.split("=", 1)[0]
            for item in proc_environ.read_bytes().decode(errors="replace").split("\0")
            if item
        }
    real_ps = Path("/bin/ps")
    if not real_ps.exists():
        return None
    listing = subprocess.run(
        [str(real_ps), "-Eww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if listing.returncode != 0:
        return None
    return {
        token.split("=", 1)[0]
        for token in listing.stdout.split()
        if "=" in token and token.split("=", 1)[0].isupper()
    }


def test_holder_drops_runner_tracking_id(lock_files, fake_daemon) -> None:
    """Actions kills every process carrying the job's RUNNER_TRACKING_ID when
    the job completes; the detached holder must survive that to keep draining."""
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    env = {**env, "RUNNER_TRACKING_ID": "github_00000000-0000-0000-0000-000000000000"}
    acquired = _run(*_acquire_args(lock, marker), "--watch-pid", "0", env=env)
    assert acquired.returncode == 0, acquired.stderr
    holder = json.loads(marker.read_text())["pid"]
    names = _environment_names(holder)
    if names is None:
        pytest.skip("no readable process environment on this host")
    assert "RUNNER_TRACKING_ID" not in names
    assert "PATH" in names  # only the tracking id is dropped


def test_release_coordination_inside_workspace_is_not_a_producer(
    lock_files, fake_daemon
) -> None:
    """The release step runs inside the workspace: its cwd must not keep the
    drain busy for the whole release wait."""
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    workspace = Path(env["GITHUB_WORKSPACE"])
    acquired = _run(
        *_acquire_args(lock, marker),
        "--watch-pid",
        "0",
        "--orphan-quiet-seconds",
        "0.4",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    releaser = _tracked_process(
        [
            sys.executable,
            str(SCRIPT),
            "release",
            "--marker",
            str(marker),
            "--job-status",
            "failure",
            "--release-wait-seconds",
            "4",
        ],
        cwd=workspace,
    )
    try:
        releaser.wait(timeout=6)
        assert releaser.returncode == 0
        _wait_free(lock)
        record = json.loads(marker.read_text())
        assert record["phase"] == "released-on-quiet", record
        assert record["surviving_producers"] == 0
        coordination = lock.with_name(lock.name + ".coordination")
        assert not list(coordination.glob("*.json"))
    finally:
        if releaser.poll() is None:
            releaser.kill()
        releaser.wait(timeout=5)


def test_unverified_coordination_file_is_ignored(lock_files, fake_daemon) -> None:
    """A published pid list counts only when the listing shows a live release
    process of this script among those pids."""
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    workspace = Path(env["GITHUB_WORKSPACE"])
    acquired = _run(
        *_acquire_args(lock, marker),
        "--watch-pid",
        "0",
        "--orphan-quiet-seconds",
        "0.3",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    holder = json.loads(marker.read_text())["pid"]
    survivor = _tracked_process(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=workspace
    )
    try:
        coordination = lock.with_name(lock.name + ".coordination")
        coordination.mkdir(exist_ok=True)
        (coordination / f"{survivor.pid}.json").write_text(
            json.dumps({"verb": "release", "pids": [survivor.pid]})
        )
        os.kill(holder, signal.SIGUSR1)
        _wait_phase(marker, "draining")
        sleep(1.0)
        assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
        assert json.loads(marker.read_text())["surviving_producers"] == 1
    finally:
        survivor.kill()
        survivor.wait(timeout=5)
    _wait_free(lock)


def test_following_job_acquire_is_not_a_producer(lock_files, fake_daemon, worker):
    """After a worker dies the runner can start the next job in the same
    checkout; its acquire (cwd inside the workspace) waits on this lock and
    must not keep the drain busy until the hold bound (#1950 round 6)."""
    lock, first, second = lock_files
    _, _, env = fake_daemon
    workspace = Path(env["GITHUB_WORKSPACE"])
    # The drain lets go after 0.4 s of quiet; the hold bound is 60 s. Half of
    # it separates the two however slow the machine, and the follower waits
    # longer than the bound so that it never gives up first.
    hold = 60
    acquired = _run(
        *_acquire_args(lock, first, hold=hold),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.4",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    worker.kill()
    worker.wait(timeout=5)
    _wait_phase(first, "draining")
    started = time.monotonic()
    follower = _tracked_process(
        [
            sys.executable,
            str(SCRIPT),
            *_acquire_args(lock, second, timeout=hold + 15, hold=5),
        ],
        cwd=workspace,
    )
    try:
        follower.wait(timeout=hold + 30)
        assert follower.returncode == 0
        waited = time.monotonic() - started
        assert waited < hold / 2, "the drain waited for the hold bound"
        _assert_phases(first, "holding", "draining", "released-on-quiet")
        assert json.loads(second.read_text())["phase"] == "holding"
    finally:
        if follower.poll() is None:
            follower.kill()
        follower.wait(timeout=5)


def test_workload_that_mentions_the_script_is_still_a_producer(
    lock_files, fake_daemon, worker
) -> None:
    """Only an argv of <python> <this script> <verb> is coordination; a live
    workload wrapper whose arguments mention the script keeps the lock held
    (#1950 round 7)."""
    lock, marker, _ = lock_files
    _, _, env = fake_daemon
    workspace = Path(env["GITHUB_WORKSPACE"])
    acquired = _run(
        *_acquire_args(lock, marker, hold=8),
        "--watch-pid",
        str(worker.pid),
        "--orphan-quiet-seconds",
        "0.3",
        "--orphan-poll-seconds",
        "0.1",
        env=env,
    )
    assert acquired.returncode == 0, acquired.stderr
    wrapper = _tracked_process(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "docker_daemon_lock.py",
            "status",
            "--lock",
            str(lock),
        ],
        cwd=workspace,
    )
    try:
        worker.kill()
        worker.wait(timeout=5)
        _wait_phase(marker, "draining")
        sleep(1.0)
        assert _run("status", "--lock", str(lock)).stdout.strip() == "held"
        assert json.loads(marker.read_text())["surviving_producers"] == 1
    finally:
        wrapper.kill()
        wrapper.wait(timeout=5)
    _wait_free(lock)
    _assert_phases(marker, "holding", "draining", "released-on-quiet")


def test_script_verb_recognizes_the_native_interpreter_in_real_ps(
    lock_files, fake_daemon
) -> None:
    """The real ps reports the interpreter by its native name (on macOS the
    framework binary ".../Python.app/Contents/MacOS/Python"); a waiting
    acquire must still be recognised as coordination (#1950 round 8)."""
    real_ps = Path("/bin/ps")
    if not real_ps.exists():
        pytest.skip("no /bin/ps on this host")
    lock, first, second = lock_files
    _, _, env = fake_daemon
    acquired = _run(*_acquire_args(lock, first), "--watch-pid", "0", env=env)
    assert acquired.returncode == 0, acquired.stderr
    waiter = subprocess.Popen(
        [sys.executable, str(SCRIPT), *_acquire_args(lock, second, timeout=8, hold=5)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        sleep(0.5)
        listing = subprocess.run(
            [str(real_ps), "-ww", "-o", "command=", "-p", str(waiter.pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        command = listing.stdout.strip()
        assert command, "the waiting acquire must be visible to the real ps"
        spec = importlib.util.spec_from_file_location(
            "docker_daemon_lock_under_test", SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module._script_verb(command) == "acquire", command
        assert (
            module._script_verb(
                f"{sys.executable} -c 'import time' {SCRIPT.name} status"
            )
            is None
        )
        # The framework interpreter as the real ps prints it on macOS.
        framework = (
            "/Library/Frameworks/Python.framework/Versions/3.13/Resources/"
            "Python.app/Contents/MacOS/Python"
        )
        assert (
            module._script_verb(f"{framework} {SCRIPT} release --marker m") == "release"
        )
        assert module._script_verb(f"{framework} {SCRIPT} _hold --lock l") == "_hold"
    finally:
        if waiter.poll() is None:
            waiter.kill()
        waiter.wait(timeout=5)


def test_successful_release_is_immediate_even_when_docker_is_busy(
    lock_files, fake_daemon
) -> None:
    lock, marker, _ = lock_files
    containers, _, env = fake_daemon
    containers.write_text("busy\n")
    acquired = _run(*_acquire_args(lock, marker), "--watch-pid", "0", env=env)
    assert acquired.returncode == 0, acquired.stderr
    started = time.monotonic()
    released = _run(
        "release", "--marker", str(marker), "--job-status", "success", env=env
    )
    assert released.returncode == 0, released.stderr
    assert time.monotonic() - started < 2
    assert not marker.exists()
    assert _run("status", "--lock", str(lock)).stdout.strip() == "free"
