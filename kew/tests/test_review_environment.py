"""The host installs the review checkout's locked dependencies before review."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kew import review_environment
from kew.config import DispatchConfig
from kew.review_dispatch import _REVIEW_RUNTIME_GUIDANCE
from kew.review_environment import Transcript
from kew.sandbox import TEST_WORKER_ENV_NAMES, build_run_argv

IMAGE = "kew-agent:test"
SYNC = [
    "sync",
    "--locked",
    "--all-groups",
    "--no-managed-python",
    "--no-python-downloads",
    "--link-mode",
    "copy",
    "--project",
    "/work/kew",
]
CHECK = [
    "run",
    "--offline",
    "--no-sync",
    "--no-python-downloads",
    "--project",
    "/work/kew",
    "python",
    "-I",
    "-S",
    "-c",
    "import sys; print('kew-interpreter ' + sys.executable, flush=True); "
    "import site; site.main(); import pytest, kew",
]
PS = ["docker", "ps", "-a", "--filter", "name=kew-s-env", "--format", "{{.Names}}"]
INTERPRETER = "/work/kew/.venv/bin/python"


def _checkout(tmp_path: Path, *, lock: bool = True) -> Path:
    checkout = tmp_path / "review-checkout"
    (checkout / "kew").mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/x\n")
    (checkout / ".kew-prompt.txt").write_text("trusted prompt\n")
    (checkout / "CLAUDE.md").write_text("masked\n")
    (checkout / "CLAUDE.local.md").write_text("masked\n")
    (checkout / "AGENTS.md").write_text("masked\n")
    (checkout / ".mcp.json").write_text("{}\n")
    (checkout / ".claude").mkdir()
    (checkout / "kew" / "pyproject.toml").write_text("[project]\nname='kew'\n")
    if lock:
        (checkout / "kew" / "uv.lock").write_text("version = 1\n")
    return checkout


def _config() -> DispatchConfig:
    config = DispatchConfig()
    config.sandbox.runtime = "docker"
    return config


def _mount_of(argv: list[str]) -> Path:
    source = next(a for a in argv if a.endswith(":/work"))
    return Path(source[: -len(":/work")])


class _Runs:
    """A fake for _run_bounded: records calls, simulates the install per label."""

    def __init__(
        self,
        *,
        sync_exit: int = 0,
        check_exit: int = 0,
        make_venv: bool = True,
        first: str = INTERPRETER,
        tail: tuple[str, ...] = (INTERPRETER,),
    ):
        self.calls: list[dict[str, Any]] = []
        self.sync_exit = sync_exit
        self.check_exit = check_exit
        self.make_venv = make_venv
        self.first = first
        self.tail = list(tail)

    def __call__(
        self,
        argv,
        mount,
        timeout_seconds,
        *,
        label,
        launch=None,
        proxy_environment="none",
    ):
        self.calls.append(
            {
                "argv": list(argv),
                "mount": Path(mount),
                "timeout": timeout_seconds,
                "label": label,
            }
        )
        if label == "uv sync":
            if self.make_venv:
                (Path(mount) / "kew" / ".venv").mkdir()
                (Path(mount) / "kew" / ".venv" / "pyvenv.cfg").write_text(
                    "home = /usr\n"
                )
            return Transcript(
                self.sync_exit, "Resolved 78 packages", ["Resolved 78 packages"]
            )
        return Transcript(self.check_exit, self.first, self.tail)


def _provision(checkout: Path, slug: str = "sfd-1") -> bool:
    return review_environment.provision_review_environment(
        _config(), image=IMAGE, runner="claude-code", checkout_dir=checkout, slug=slug
    )


def _no_container(*_a, **_k):
    pytest.fail("no container must run")


def test_install_runs_in_a_disposable_copy_and_only_the_venv_moves_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    runs = _Runs()
    monkeypatch.setattr(review_environment, "_run_bounded", runs)

    assert _provision(checkout) is True
    assert [c["label"] for c in runs.calls] == ["uv sync", "environment check"]
    sync, check = runs.calls
    # The trusted checkout is never mounted: both containers see the copy.
    for call in (sync, check):
        mount = _mount_of(call["argv"])
        assert mount == call["mount"]
        assert mount != checkout and checkout not in mount.parents
        assert mount.parent.name.startswith("kew-review-env-sfd-1-")
        assert f"{checkout.absolute()}:/work" not in call["argv"]
    assert sync["timeout"] == 900.0
    assert check["timeout"] == 120.0
    # Exact commands after the image, spelled out: locked, no interpreter
    # download, copied wheels; then the isolated interpreter that prints its
    # own path before any PR code runs, then site and the imports.
    assert sync["argv"][sync["argv"].index(IMAGE) + 1 :] == SYNC
    assert check["argv"][check["argv"].index(IMAGE) + 1 :] == CHECK
    for call, suffix in ((sync, "-env"), (check, "-env-check")):
        argv = call["argv"]
        assert argv[0] == "docker"
        assert argv[argv.index("--entrypoint") + 1] == "/usr/local/bin/uv"
        assert argv[argv.index("--network") + 1] == "kew-allowlist"
        assert argv[argv.index("--name") + 1] == f"kew-sfd-1{suffix}"
        env_args = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        # The marker, per-attempt acknowledgement, environment pin, and the ten
        # empty proxy assignments.
        nonce_arg = next(a for a in env_args if a.startswith("KEW_AGENT_START_NONCE="))
        assert re.fullmatch(r"KEW_AGENT_START_NONCE=[0-9a-f]{32}", nonce_arg)
        # The test-pool sizes match the container's CPU cap (#2887).
        cpus = argv[argv.index("--cpus") + 1]
        assert re.fullmatch(r"[1-9][0-9]*", cpus), cpus
        pool_args = [a for a in env_args if a.split("=", 1)[0] in TEST_WORKER_ENV_NAMES]
        assert sorted(pool_args) == sorted(
            f"{name}={cpus}" for name in TEST_WORKER_ENV_NAMES
        ), pool_args
        env_args = [a for a in env_args if a not in pool_args]
        assert sorted(env_args) == [
            "AGENT_ASSIGNED_WORKTREE=/work",
            "ALL_PROXY=",
            "FTP_PROXY=",
            "HTTPS_PROXY=",
            "HTTP_PROXY=",
            nonce_arg,
            "NO_PROXY=",
            "UV_PROJECT_ENVIRONMENT=/work/kew/.venv",
            "all_proxy=",
            "ftp_proxy=",
            "http_proxy=",
            "https_proxy=",
            "no_proxy=",
        ], env_args
    # Only the venv came back; the trusted inputs are untouched; the copy is gone.
    assert (checkout / "kew" / ".venv" / "pyvenv.cfg").is_file()
    assert (checkout / ".kew-prompt.txt").read_text() == "trusted prompt\n"
    assert not list(tmp_path.glob("kew-review-env-*"))


def test_the_copy_excludes_the_git_store_review_inputs_and_masked_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    seen: dict[str, set[str]] = {}

    def snapshot(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        seen[label] = {p.name for p in Path(mount).iterdir()}
        (Path(mount) / "kew" / ".venv").mkdir(exist_ok=True)
        return Transcript(0, INTERPRETER, [INTERPRETER])

    monkeypatch.setattr(review_environment, "_run_bounded", snapshot)
    assert _provision(checkout, "s")
    assert seen["uv sync"] == {"kew"}, seen


def test_a_missing_lockfile_runs_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path, lock=False)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", _no_container)
    assert _provision(checkout, "s") is False
    assert any("no lockfile" in w for w in warnings), warnings


@pytest.mark.parametrize("shape", ["project-symlink", "checkout-symlink"])
def test_a_symlinked_checkout_or_project_stops_the_review_and_touches_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Nothing is removed through a symlinked ancestor: the external
    environment behind the link survives, and the review fails closed."""
    checkout = _checkout(tmp_path)
    external = tmp_path / "external"
    (external / ".venv").mkdir(parents=True)
    (external / ".venv" / "pyvenv.cfg").write_text("external\n")
    (external / "uv.lock").write_text("version = 1\n")
    if shape == "project-symlink":
        (checkout / "kew").rename(tmp_path / "kew-moved")
        os.symlink(external, checkout / "kew")
    else:
        link = tmp_path / "link"
        os.symlink(checkout, link)
        checkout = link
    monkeypatch.setattr(review_environment, "_run_bounded", _no_container)

    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="cannot be made truthful"
    ):
        _provision(checkout, "s")
    assert (external / ".venv" / "pyvenv.cfg").read_text() == "external\n"


def test_a_checkout_over_the_quota_or_the_free_space_floor_is_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    (checkout / "kew" / "big.bin").write_bytes(b"x" * 64)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", _no_container)
    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_BYTES", 32)
    assert _provision(checkout, "s") is False
    assert any("exceeds the sandbox quota" in w for w in warnings), warnings

    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_BYTES", 4 * 1024**3)
    # Directories (and directory links) are copied entries too.
    for i in range(4):
        (checkout / "kew" / f"pkg{i}").mkdir()
    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_ENTRIES", 6)
    assert _provision(checkout, "s") is False
    assert sum("exceeds the sandbox quota" in w for w in warnings) == 2, warnings
    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_ENTRIES", 500_000)

    monkeypatch.setattr(
        review_environment.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(
            total=1, used=0, free=review_environment.HOST_FREE_SPACE_FLOOR_BYTES
        ),
    )
    assert _provision(checkout, "s") is False
    assert any("free-space floor" in w for w in warnings), warnings
    assert not list(tmp_path.glob("kew-review-env-*"))


def test_the_copy_budget_counts_allocated_size_not_length() -> None:
    st = SimpleNamespace(st_size=1, st_blocks=8)
    assert review_environment._allocated_size(st) == 4096
    assert (
        review_environment._allocated_size(SimpleNamespace(st_size=9000, st_blocks=8))
        == 9000
    )
    assert review_environment._allocated_size(SimpleNamespace(st_size=5)) == 5


def test_the_copy_budget_charges_directories_and_links_and_hands_them_to_the_copier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory and link allocations enter the byte total and are handed to
    the copier as already charged: a tree of many empty directories is not
    free, and the file bytes written on top of them are bounded together."""
    checkout = _checkout(tmp_path)
    for i in range(3):
        (checkout / "kew" / f"d{i}").mkdir()
    os.symlink(".", checkout / "kew" / "self")
    monkeypatch.setattr(review_environment, "_allocated_size", lambda _st: 4096)
    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_BYTES", 4096 * 3)
    refusal, structure = review_environment._copy_budget_refusal(checkout)
    assert refusal is not None and "exceeds the sandbox quota" in refusal

    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_BYTES", 4 * 1024**3)
    refusal, structure = review_environment._copy_budget_refusal(checkout)
    assert refusal is None
    # kew, .claude, d0..d2, self: six directory entries at 4096 each (.git is
    # never copied, so it is not charged).
    assert structure == 4096 * 6


def test_the_copier_stops_inside_a_file_that_passes_the_quota(tmp_path: Path) -> None:
    """Bytes are charged before they are written: a file that grew past the
    quota is cut off at the quota, not copied and then refused."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * (3 * 1024 * 1024))
    dst = tmp_path / "dst.bin"
    copier = review_environment._BoundedCopier(int(1.5 * 1024 * 1024))
    with pytest.raises(review_environment._CopyQuotaExceeded):
        copier(src, dst)
    # The actual budget: the overhead plus whole chunks under the quota.
    assert dst.stat().st_size <= int(1.5 * 1024 * 1024) - 4096
    assert dst.stat().st_size < src.stat().st_size


def test_the_copier_starts_from_what_the_walk_already_charged(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 80)
    copier = review_environment._BoundedCopier(4096 + 100, charged=60)
    with pytest.raises(review_environment._CopyQuotaExceeded):
        copier(src, tmp_path / "dst.bin")
    assert review_environment._BoundedCopier(4096 + 100, charged=10)(
        src, tmp_path / "dst2.bin"
    ) == str(tmp_path / "dst2.bin")
    assert (tmp_path / "dst2.bin").read_bytes() == b"x" * 80


def test_the_copier_stops_at_the_free_space_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        review_environment.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=1, used=0, free=0),
    )
    with pytest.raises(review_environment._CopyQuotaExceeded, match="free-space floor"):
        review_environment._BoundedCopier(10**9)(src, tmp_path / "dst.bin")


def test_the_copy_stops_when_it_passes_the_quota_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-copy walk is an estimate; the copier is the enforcement."""
    checkout = _checkout(tmp_path)
    (checkout / "kew" / "grew.bin").write_bytes(b"x" * 100_000)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(
        review_environment, "_copy_budget_refusal", lambda _c: (None, 0)
    )
    monkeypatch.setattr(review_environment, "CHECKOUT_QUOTA_BYTES", 50_000)
    monkeypatch.setattr(review_environment, "_run_bounded", _no_container)
    assert _provision(checkout, "s") is False
    assert any("exceeded the sandbox quota" in w for w in warnings), warnings
    assert not list(tmp_path.glob("kew-review-env-*"))


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (_Runs(sync_exit=1), "uv sync exited 1"),
        (_Runs(make_venv=False), "kew/.venv is absent"),
        (_Runs(check_exit=2), "environment check exited 2"),
        (
            _Runs(first="", tail=["/usr/bin/python3", INTERPRETER]),
            "not an interpreter under /work/kew/.venv",
        ),
        (
            _Runs(first="/work/kew/.venv/../../other/bin/python"),
            "not an interpreter under /work/kew/.venv",
        ),
    ],
    ids=["sync-fails", "no-venv", "check-fails", "no-tagged-line", "dotdot-path"],
)
def test_provisioning_failures_are_reported_and_leave_no_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: _Runs, expected: str
) -> None:
    checkout = _checkout(tmp_path)
    # The PR tracked environments of its own; none survives a failure.
    for planted in (checkout / "kew" / ".venv", checkout / ".venv"):
        (planted / "bin").mkdir(parents=True)
        (planted / "bin" / "pytest").write_text("#!/bin/sh\necho planted\n")
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", runs)

    assert _provision(checkout, "s") is False
    assert any(expected in w for w in warnings), warnings
    assert not (checkout / "kew" / ".venv").exists(), (
        "no kew/.venv after a failure: absence is what the reviewer reads as failed"
    )
    assert not (checkout / ".venv").exists()
    assert not list(tmp_path.glob("kew-review-env-*"))


def test_the_interpreter_binding_reads_the_first_line_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The isolated interpreter prints its path before any PR code runs;
    whatever the PR prints afterwards changes nothing."""
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(
        review_environment,
        "_run_bounded",
        _Runs(
            first=INTERPRETER, tail=[INTERPRETER, "planted output", "/usr/bin/python3"]
        ),
    )
    assert _provision(checkout, "s") is True


def test_success_keeps_only_the_promoted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / "kew" / ".venv").mkdir()
    (checkout / "kew" / ".venv" / "planted").write_text("planted\n")
    monkeypatch.setattr(review_environment, "_run_bounded", _Runs())
    assert _provision(checkout, "s") is True
    assert not (checkout / ".venv").exists()
    assert (checkout / "kew" / ".venv" / "pyvenv.cfg").is_file()
    assert not (checkout / "kew" / ".venv" / "planted").exists()


@pytest.mark.parametrize(
    "planted", ["directory", "symlink", "fifo", "mount", "mount-root"]
)
def test_a_planted_environment_that_cannot_be_removed_stops_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    checkout = _checkout(tmp_path)
    target = checkout / "kew" / ".venv"
    if planted == "directory":
        target.mkdir()
        real_rmtree = review_environment.shutil.rmtree

        def locked(path, *args, **kwargs):
            if Path(path) == target:
                raise PermissionError("locked")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(review_environment.shutil, "rmtree", locked)
    elif planted == "mount":
        (target / "cache").mkdir(parents=True)
        (target / "cache" / "victim").write_text("host content\n")
        monkeypatch.setattr(
            review_environment.os.path, "ismount", lambda p: Path(p) == target / "cache"
        )
    elif planted == "mount-root":
        target.mkdir()
        (target / "victim").write_text("host content\n")
        monkeypatch.setattr(
            review_environment.os.path, "ismount", lambda p: Path(p) == target
        )
    else:
        if planted == "symlink":
            os.symlink(tmp_path, target)
        else:
            os.mkfifo(target)
        monkeypatch.setattr(
            review_environment.Path,
            "unlink",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("locked")),
        )
    monkeypatch.setattr(review_environment, "_run_bounded", _Runs(sync_exit=1))
    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="cannot be made truthful"
    ):
        _provision(checkout, "s")
    if planted == "mount":
        assert (target / "cache" / "victim").read_text() == "host content\n"
    if planted == "mount-root":
        assert (target / "victim").read_text() == "host content\n"


def test_a_fifo_planted_as_the_environment_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    os.mkfifo(checkout / "kew" / ".venv")
    monkeypatch.setattr(review_environment, "_warn", lambda _m: None)
    monkeypatch.setattr(review_environment, "_run_bounded", _Runs(sync_exit=1))
    assert _provision(checkout, "s") is False
    assert not (checkout / "kew" / ".venv").exists()


def test_a_project_directory_swapped_for_a_symlink_in_the_copy_is_not_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR's code ran in the copy: if it replaced kew/ with a symlink to
    another environment, the host must not follow it when promoting."""
    checkout = _checkout(tmp_path)
    victim = tmp_path / "victim"
    (victim / ".venv").mkdir(parents=True)
    (victim / ".venv" / "pyvenv.cfg").write_text("host content\n")
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)

    def runs(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        project = Path(mount) / "kew"
        if label == "uv sync":
            (project / ".venv").mkdir()
            return Transcript(0, "", [])
        # the check container's code swaps the project for a symlink
        project.rename(Path(mount) / "built")
        os.symlink(victim, project)
        return Transcript(0, INTERPRETER, [INTERPRETER])

    monkeypatch.setattr(review_environment, "_run_bounded", runs)
    assert _provision(checkout, "s") is False
    assert any("is a symlink after the install" in w for w in warnings), warnings
    assert (victim / ".venv" / "pyvenv.cfg").read_text() == "host content\n"
    assert not (checkout / "kew" / ".venv").exists()
    assert not list(tmp_path.glob("kew-review-env-*"))


def test_an_unconfirmed_container_stops_the_review_and_retains_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container that is not confirmed gone may still hold the copy: the
    copy is retained and reported, never removed under a live writer."""
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)

    def stuck(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        launch.uncertain = (
            True  # what the real bounded run sets before the client starts
        )
        raise review_environment.ProvisioningUnconfirmed(
            "container 'kew-s-env' still present"
        )

    monkeypatch.setattr(review_environment, "_run_bounded", stuck)
    with pytest.raises(review_environment.ProvisioningUnconfirmed):
        _provision(checkout, "s")
    retained = list(tmp_path.glob("kew-review-env-*"))
    assert len(retained) == 1, "the copy is retained while a container may hold it"
    assert any("retained at" in w and "not confirmed gone" in w for w in warnings), (
        warnings
    )
    review_environment.shutil.rmtree(retained[0])


def test_other_failures_are_reported_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)

    def broken(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        raise OSError("docker socket gone")

    monkeypatch.setattr(review_environment, "_run_bounded", broken)
    assert _provision(checkout, "s") is False
    assert any("docker socket gone" in w for w in warnings), warnings


def test_the_disposable_copy_is_removed_even_when_the_build_locked_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    checkout = _checkout(tmp_path)

    def locks(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        junk = Path(mount) / "junk"
        junk.mkdir()
        (junk / "payload").write_text("x")
        junk.chmod(stat.S_IRUSR | stat.S_IXUSR)
        return Transcript(1, "", [])

    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", locks)
    assert _provision(checkout, "s") is False
    assert not list(tmp_path.glob("kew-review-env-*")), (
        "permissions are restored on the way down"
    )
    assert not any("retained" in w for w in warnings), warnings


def test_a_retained_disposable_copy_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", _Runs(sync_exit=1))
    real_rmtree = review_environment.shutil.rmtree

    def stuck(path, *args, **kwargs):
        if Path(path).name.startswith("kew-review-env-"):
            raise PermissionError("busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(review_environment.shutil, "rmtree", stuck)
    assert _provision(checkout, "s") is False
    assert any("retained at" in w for w in warnings), warnings
    for leftover in tmp_path.glob("kew-review-env-*"):
        real_rmtree(leftover)


class _Daemon:
    """A fake subprocess_utils.run for the absence probe and removal."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, check, timeout):
        self.calls.append(list(cmd))
        answer = self.answers.pop(0) if self.answers else (0, "")
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(returncode=answer[0], stdout=answer[1], stderr="")


def _bounded_argv(tmp_path: Path) -> tuple[list[str], Path]:
    mount = tmp_path / "copy"
    mount.mkdir(exist_ok=True)
    return (
        build_run_argv(
            runtime="docker",
            image=IMAGE,
            checkout_dir=mount,
            slug="s-env",
            network="allowlist",
            runner="claude-code",
            entrypoint="uv",
            command=SYNC,
            credential_environment={},
        ),
        mount,
    )


def _fake_handle(
    monkeypatch: pytest.MonkeyPatch,
    created: dict[str, Any],
    *,
    lines=("Resolved 78 packages\n",),
    raise_in_iter: BaseException | None = None,
    wait_raises: BaseException | None = None,
    wait_returns: int = 0,
    container_id: str | None = "abc123",
    settled: bool = True,
    settle_raises: BaseException | None = None,
    order: list[str] | None = None,
):
    class FakeHandle:
        def __init__(self, proc, name, runtime, **kwargs):
            created.update(name=name, runtime=runtime, **kwargs)
            self.container_id = None

        def __iter__(self):
            if raise_in_iter is not None:
                raise raise_in_iter
            yield from lines

        def wait(self, timeout=None):
            created["wait_timeout"] = timeout
            if wait_raises is not None:
                raise wait_raises
            self.container_id = container_id
            return wait_returns

        def settle_client(self, timeout):
            created["settle_timeout"] = timeout
            if order is not None:
                order.append("settle")
            if settle_raises is not None:
                raise settle_raises
            return settled

    monkeypatch.setattr(review_environment, "ContainerHandle", FakeHandle)

    def popen(argv):
        return object()

    monkeypatch.setattr(review_environment.subprocess_utils, "popen", popen)
    return popen


def test_run_bounded_uses_the_sandbox_handle_with_the_copy_quota_wall_clock_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    created: dict[str, Any] = {}
    popen = _fake_handle(
        monkeypatch,
        created,
        lines=(
            "docker: CPU cap rejected, retrying\n",
            "kew-interpreter /work/kew/.venv/bin/python \n",
            "kew-interpreter /usr/bin/python3\n",
            "Resolved 78 packages\n",
        ),
    )
    daemon = _Daemon([(0, "")])
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)

    # The first TAGGED line binds: a runtime diagnostic before it (a CPU-cap
    # retry's first attempt) and any tagged line the PR prints after it are
    # ignored.
    assert review_environment._run_bounded(
        argv, mount, 123.0, label="uv sync"
    ) == Transcript(
        0,
        "/work/kew/.venv/bin/python",
        [
            "docker: CPU cap rejected, retrying",
            "kew-interpreter /work/kew/.venv/bin/python ",
            "kew-interpreter /usr/bin/python3",
            "Resolved 78 packages",
        ],
    )
    assert created["name"] == "kew-s-env"
    assert created["runtime"] == "docker"
    assert created["checkout_dir"] == mount
    assert created["wall_clock_seconds"] == 123.0
    assert created["cidfile"] is not None and created["launch_argv"] == argv
    assert created["cpu_retry_launcher"] is popen
    assert created["wait_timeout"] == 30.0 and created["settle_timeout"] == 30.0
    assert daemon.calls == [PS]


def test_an_aborted_launch_is_unconfirmed_even_when_nothing_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A streaming failure ends the launch before the client saw the container
    end; a creation still in flight can complete after the probe, so the
    client is settled, the daemon is asked, and the run stays unconfirmed."""
    argv, mount = _bounded_argv(tmp_path)
    order: list[str] = []
    _fake_handle(monkeypatch, {}, raise_in_iter=OSError("stream lost"), order=order)

    class Daemon(_Daemon):
        def __call__(self, cmd, *, check, timeout):
            order.append("probe")
            return super().__call__(cmd, check=check, timeout=timeout)

    monkeypatch.setattr(review_environment.subprocess_utils, "run", Daemon([(0, "")]))
    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="aborted"
    ) as info:
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")
    assert order == ["settle", "probe"]
    assert isinstance(info.value.__cause__, OSError)

    # ... and a container still present after the failure is removed first.
    _fake_handle(monkeypatch, {}, raise_in_iter=OSError("stream lost"))
    daemon = _Daemon([(0, "kew-s-env\n"), (0, ""), (0, "kew-s-env\n")])
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="confirmed gone"
    ):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"wait_returns": -9}, "by signal"),
        ({"container_id": None}, "without a creation record"),
        ({"wait_returns": 125, "container_id": None}, "without a creation record"),
    ],
    ids=["client-killed", "no-creation-record", "launch-refused-without-record"],
)
def test_a_client_that_did_not_see_the_launch_through_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs, reason
) -> None:
    """EOF then a signal exit, or an exit without the container id file the
    client writes on creation, does not prove the daemon finished with the
    request; the reviewer must not launch beside it."""
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(monkeypatch, {}, lines=(), **kwargs)
    daemon = _Daemon([(0, "")])
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match=reason):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")
    assert daemon.calls == [PS], "the client is settled and the daemon asked first"


def test_a_created_container_that_failed_is_an_ordinary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(monkeypatch, {}, wait_returns=2, container_id="abc123")
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    assert (
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync").exit_code
        == 2
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"settled": False}, "still running"),
        ({"wait_raises": subprocess.TimeoutExpired(["docker"], 30)}, "aborted"),
        ({"settle_raises": PermissionError("no kill")}, "could not be settled"),
    ],
    ids=["client-survives-kill", "client-wait-times-out", "settle-raises"],
)
def test_a_runtime_client_that_cannot_be_settled_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs, reason
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(monkeypatch, {}, **kwargs)
    daemon = _Daemon([(0, "")])
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match=reason):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")


def test_absence_is_an_exact_name_comparison_on_either_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker lists names with a leading slash, Podman without; the filter is
    a substring match on both, so a sibling name is not this container."""
    argv, mount = _bounded_argv(tmp_path)
    for listing in ("kew-s-env-check\n", "/kew-s-env-check\nother\n", ""):
        _fake_handle(monkeypatch, {})
        daemon = _Daemon([(0, listing)])
        monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
        assert (
            review_environment._run_bounded(argv, mount, 5.0, label="uv sync").exit_code
            == 0
        )
        assert daemon.calls == [PS]
    for listing in ("kew-s-env\n", "/kew-s-env\n", "other\n/kew-s-env\n"):
        _fake_handle(monkeypatch, {})
        daemon = _Daemon([(0, listing), (0, ""), (0, "")])
        monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
        assert (
            review_environment._run_bounded(argv, mount, 5.0, label="uv sync").exit_code
            == 0
        )
        # Removed by the id the launch recorded, never the name (#1963).
        assert daemon.calls == [PS, ["docker", "rm", "-f", "abc123"], PS]


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (
            [(0, "kew-s-env\n"), (0, ""), (0, "kew-s-env\n")],
            "could not be confirmed gone",
        ),
        ([(1, "")], "the daemon did not answer"),
        ([subprocess.TimeoutExpired(["docker"], 10)], "the daemon did not answer"),
        (
            [(0, "kew-s-env\n"), subprocess.TimeoutExpired(["docker"], 30)],
            "could not be removed",
        ),
    ],
    ids=["still-present", "daemon-error", "probe-timeout", "removal-timeout"],
)
def test_anything_short_of_positive_absence_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers, reason: str
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(monkeypatch, {})
    daemon = _Daemon(answers)
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match=reason):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")


def test_reviewer_guidance_points_at_the_installed_environment() -> None:
    assert "/work/kew/.venv" in _REVIEW_RUNTIME_GUIDANCE
    assert "UV_PROJECT_ENVIRONMENT=/work/kew/.venv" in _REVIEW_RUNTIME_GUIDANCE
    assert (
        "uv run --offline --no-sync\n--no-python-downloads pytest"
        in _REVIEW_RUNTIME_GUIDANCE
    )
    assert "do not install" in _REVIEW_RUNTIME_GUIDANCE


def test_a_source_that_is_not_a_regular_file_is_refused_without_reading_it(
    tmp_path: Path,
) -> None:
    """A FIFO would block the host before any container timeout applies."""
    fifo = tmp_path / "src.fifo"
    os.mkfifo(fifo)
    with pytest.raises(
        review_environment._CopyQuotaExceeded, match="not a regular file"
    ):
        review_environment._BoundedCopier(10**9)(fifo, tmp_path / "dst.bin")
    assert not (tmp_path / "dst.bin").exists()


def test_the_copier_checks_the_floor_before_every_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * (3 * 1024 * 1024))
    dst = tmp_path / "dst.bin"
    answers = iter([10**12, 10**12, 0])
    monkeypatch.setattr(
        review_environment.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=1, used=0, free=next(answers, 0)),
    )
    with pytest.raises(review_environment._CopyQuotaExceeded, match="free-space floor"):
        review_environment._BoundedCopier(10**9)(src, dst)
    assert dst.stat().st_size < src.stat().st_size, "the copy stopped mid-file"


def test_file_links_are_part_of_the_seeded_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """copytree creates file symlinks itself; their allocation is charged up
    front, not left for a growing file to spend again."""
    checkout = _checkout(tmp_path)
    (checkout / "kew" / "real.txt").write_text("x")
    os.symlink("real.txt", checkout / "kew" / "link.txt")
    monkeypatch.setattr(review_environment, "_allocated_size", lambda _st: 4096)
    refusal, structure = review_environment._copy_budget_refusal(checkout)
    assert refusal is None
    # kew, .claude directories plus the file link.
    assert structure == 4096 * 3


def test_provisioning_hands_the_walked_structure_to_the_copier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    seen: dict[str, int] = {}
    real_copier = review_environment._BoundedCopier

    class Recording(real_copier):
        def __init__(self, limit, *, charged=0):
            seen["charged"] = charged
            super().__init__(limit, charged=charged)

    monkeypatch.setattr(review_environment, "_BoundedCopier", Recording)
    monkeypatch.setattr(
        review_environment, "_copy_budget_refusal", lambda _c: (None, 777)
    )
    monkeypatch.setattr(review_environment, "_run_bounded", _Runs())
    assert _provision(checkout, "s") is True
    assert seen == {"charged": 777}


def test_the_copy_excludes_every_masked_autoload_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusions are the review's own masked set, nested occurrences too."""
    from kew.review_dispatch import _HEAD_AUTOLOAD_DIRS, _HEAD_AUTOLOAD_FILES

    checkout = _checkout(tmp_path)
    nested = checkout / "kew" / "src"
    nested.mkdir()
    for name in _HEAD_AUTOLOAD_FILES:
        (nested / name).write_text("planted\n")
    for name in _HEAD_AUTOLOAD_DIRS:
        (nested / name).mkdir()
    seen: dict[str, set[str]] = {}

    def snapshot(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        seen[label] = {p.name for p in Path(mount).rglob("*")}
        (Path(mount) / "kew" / ".venv").mkdir(exist_ok=True)
        return Transcript(0, INTERPRETER, [INTERPRETER])

    monkeypatch.setattr(review_environment, "_run_bounded", snapshot)
    assert _provision(checkout, "s")
    assert not (
        seen["uv sync"] & (set(_HEAD_AUTOLOAD_FILES) | set(_HEAD_AUTOLOAD_DIRS))
    ), seen


def test_a_cancellation_after_the_launch_retains_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)

    def interrupted(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        launch.uncertain = True  # the real bounded run sets it before the client starts
        raise KeyboardInterrupt

    monkeypatch.setattr(review_environment, "_run_bounded", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _provision(checkout, "s")
    retained = list(tmp_path.glob("kew-review-env-*"))
    assert len(retained) == 1
    assert any("retained at" in w for w in warnings), warnings
    review_environment.shutil.rmtree(retained[0])


def test_a_destination_that_cannot_open_raises_its_own_error_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source descriptor is owned by its file object before the
    destination opens, so the failure surfaces as itself, not as a double
    close (EBADF) of an unrelated descriptor."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 10)
    closed: list[int] = []
    real_close = os.close

    def counting_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(review_environment.os, "close", counting_close)
    with pytest.raises(FileNotFoundError):
        review_environment._BoundedCopier(10**9)(
            src, tmp_path / "missing-dir" / "dst.bin"
        )
    assert closed == [], "the file object, not os.close, released the descriptor"


def test_an_empty_first_tag_is_not_repaired_by_a_later_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(
        monkeypatch,
        {},
        lines=("kew-interpreter \n", "kew-interpreter /work/kew/.venv/bin/python\n"),
    )
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    assert (
        review_environment._run_bounded(argv, mount, 5.0, label="check").interpreter
        == ""
    )


def test_a_confirmation_failure_during_a_cancellation_stays_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation lands after the launch; the probe times out and the
    diagnostic write itself raises. Neither may turn into an ordinary failure
    that would let the provisioner remove the copy."""
    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(monkeypatch, {}, raise_in_iter=KeyboardInterrupt())
    monkeypatch.setattr(
        review_environment.subprocess_utils,
        "run",
        _Daemon([subprocess.TimeoutExpired(["docker"], 10)]),
    )

    def sink_full(_message):
        raise OSError("diagnostic sink full")

    monkeypatch.setattr(review_environment, "_warn", sink_full)
    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="confirmation itself failed"
    ):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")


def test_a_handle_that_cannot_be_built_settles_the_started_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    events: list[str] = []

    class Proc:
        def poll(self):
            return None

        def kill(self):
            events.append("kill")

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            return -9

    monkeypatch.setattr(review_environment.subprocess_utils, "popen", lambda _a: Proc())

    def broken(*_a, **_k):
        raise RuntimeError("handle init failed")

    monkeypatch.setattr(review_environment, "ContainerHandle", broken)
    daemon = _Daemon([(0, "")])
    monkeypatch.setattr(review_environment.subprocess_utils, "run", daemon)
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match="aborted"):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")
    assert events == ["kill", "wait:30.0"]
    assert daemon.calls == [PS]


def test_the_provisioner_retains_the_copy_when_the_confirmation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(tmp_path)
    warnings: list[str] = []

    def stuck(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        launch.uncertain = True
        raise review_environment.ProvisioningUnconfirmed(
            "provisioning container 'kew-s-env' could not be confirmed gone: the "
            "confirmation itself failed (OSError: diagnostic sink full)"
        )

    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    monkeypatch.setattr(review_environment, "_run_bounded", stuck)
    with pytest.raises(review_environment.ProvisioningUnconfirmed):
        _provision(checkout, "s")
    retained = list(tmp_path.glob("kew-review-env-*"))
    assert len(retained) == 1
    review_environment.shutil.rmtree(retained[0])


def test_a_failed_fdopen_closes_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    closed: list[int] = []
    real_close = os.close
    opened: list[int] = []
    real_open = os.open

    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def recording_close(fd):
        closed.append(fd)
        real_close(fd)

    def refusing_fdopen(fd, *args, **kwargs):
        raise PermissionError("audit hook rejected the open")

    monkeypatch.setattr(review_environment.os, "open", recording_open)
    monkeypatch.setattr(review_environment.os, "close", recording_close)
    monkeypatch.setattr(review_environment.io, "FileIO", refusing_fdopen)
    with pytest.raises(PermissionError):
        review_environment._BoundedCopier(10**9)(src, tmp_path / "dst.bin")
    assert closed == opened and len(opened) == 1


def test_a_destination_that_cannot_open_closes_the_source_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    handles: list[Any] = []
    real_fileio = review_environment.io.FileIO

    def tracking_fileio(fd, *args, **kwargs):
        handle = real_fileio(fd, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(review_environment.io, "FileIO", tracking_fileio)
    with pytest.raises(FileNotFoundError):
        review_environment._BoundedCopier(10**9)(
            src, tmp_path / "missing-dir" / "dst.bin"
        )
    assert len(handles) == 1 and handles[0].closed


def test_an_uncertain_launch_fails_closed_whatever_the_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime client started; an allocation failure before the handle
    exists (or any other ordinary exception) must not become a False that
    lets the reviewer launch, and the copy must be retained."""
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)
    started: list[str] = []
    monkeypatch.setattr(
        review_environment.subprocess_utils,
        "popen",
        lambda _a: started.append("client") or object(),
    )

    def out_of_memory(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr(review_environment, "deque", out_of_memory)
    with pytest.raises(review_environment.ProvisioningUnconfirmed):
        _provision(checkout, "s")
    assert started == ["client"]
    retained = list(tmp_path.glob("kew-review-env-*"))
    assert len(retained) == 1 and any("retained at" in w for w in warnings)
    review_environment.shutil.rmtree(retained[0])


def test_a_confirmation_error_whose_text_cannot_be_built_is_still_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unspeakable(OSError):
        def __str__(self):
            raise ValueError("no text")

    argv, mount = _bounded_argv(tmp_path)
    _fake_handle(
        monkeypatch, {}, raise_in_iter=KeyboardInterrupt(), settle_raises=Unspeakable()
    )
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match="Unspeakable"):
        review_environment._run_bounded(argv, mount, 5.0, label="uv sync")


def test_the_launch_state_brackets_the_client_from_start_to_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, mount = _bounded_argv(tmp_path)
    launch = review_environment._LaunchState()
    seen: list[bool] = []
    _fake_handle(monkeypatch, {})
    real_popen = review_environment.subprocess_utils.popen

    def observing_popen(a):
        seen.append(launch.uncertain)
        return real_popen(a)

    monkeypatch.setattr(review_environment.subprocess_utils, "popen", observing_popen)
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    review_environment._run_bounded(argv, mount, 5.0, label="uv sync", launch=launch)
    assert seen == [True] and launch.uncertain is False

    # An aborted launch leaves it set.
    _fake_handle(monkeypatch, {}, raise_in_iter=OSError("stream lost"))
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    with pytest.raises(review_environment.ProvisioningUnconfirmed):
        review_environment._run_bounded(
            argv, mount, 5.0, label="uv sync", launch=launch
        )
    assert launch.uncertain is True


def test_a_late_reader_failure_closes_the_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw file object owns the descriptor before the buffered reader is
    built; a failure building the reader closes it through that object, not
    through a second os.close."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x")
    closed: list[int] = []
    real_close = os.close
    raws: list[Any] = []
    real_fileio = review_environment.io.FileIO

    def tracking_fileio(fd, *args, **kwargs):
        raw = real_fileio(fd, *args, **kwargs)
        raws.append(raw)
        return raw

    def out_of_memory(_raw):
        raise MemoryError

    monkeypatch.setattr(
        review_environment.os, "close", lambda fd: (closed.append(fd), real_close(fd))
    )
    monkeypatch.setattr(review_environment.io, "FileIO", tracking_fileio)
    monkeypatch.setattr(review_environment.io, "BufferedReader", out_of_memory)
    with pytest.raises(MemoryError):
        review_environment._BoundedCopier(10**9)(src, tmp_path / "dst.bin")
    assert closed == [], "no raw os.close after ownership moved to the file object"
    assert len(raws) == 1 and raws[0].closed


def test_the_provisioner_fails_closed_when_the_bounded_run_leaves_the_launch_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: an ordinary exception that escapes the bounded run
    while the launch state is still uncertain is not an ordinary failure."""
    checkout = _checkout(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(review_environment, "_warn", warnings.append)

    def escapes(
        argv, mount, timeout_seconds, *, label, launch=None, proxy_environment="none"
    ):
        launch.uncertain = True
        raise RuntimeError("escaped the bounded run")

    monkeypatch.setattr(review_environment, "_run_bounded", escapes)
    with pytest.raises(
        review_environment.ProvisioningUnconfirmed, match="left uncertain"
    ):
        _provision(checkout, "s")
    retained = list(tmp_path.glob("kew-review-env-*"))
    assert len(retained) == 1 and any("retained at" in w for w in warnings)
    review_environment.shutil.rmtree(retained[0])


def test_the_launch_stays_uncertain_through_confirmation_and_the_record_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clear must come after both the daemon's confirmation and the
    creation-record check: a clear placed any earlier lets a confirmation
    failure remove the copy beside a live container."""
    argv, mount = _bounded_argv(tmp_path)
    launch = review_environment._LaunchState()
    observed: dict[str, bool] = {}
    created: dict[str, Any] = {}
    _fake_handle(monkeypatch, created)

    class Handle:
        def __init__(self, proc, name, runtime, **kwargs):
            self.container_id = None

        def __iter__(self):
            yield "Resolved 78 packages\n"

        def wait(self, timeout=None):
            self.container_id = "abc123"
            return 0

        def settle_client(self, timeout):
            observed["during settle"] = launch.uncertain
            return True

    monkeypatch.setattr(review_environment, "ContainerHandle", Handle)

    class Daemon(_Daemon):
        def __call__(self, cmd, *, check, timeout):
            observed["during probe"] = launch.uncertain
            return super().__call__(cmd, check=check, timeout=timeout)

    monkeypatch.setattr(review_environment.subprocess_utils, "run", Daemon([(0, "")]))
    real_check = review_environment._require_seen_through

    def observing_check(handle, exit_code, name):
        observed["during record check"] = launch.uncertain
        return real_check(handle, exit_code, name)

    monkeypatch.setattr(review_environment, "_require_seen_through", observing_check)
    review_environment._run_bounded(argv, mount, 5.0, label="uv sync", launch=launch)
    assert observed == {
        "during settle": True,
        "during probe": True,
        "during record check": True,
    }
    assert launch.uncertain is False

    # ... and a failure inside the record check leaves it set.
    launch = review_environment._LaunchState()
    Handle.wait = lambda self, timeout=None: -9  # type: ignore[method-assign]
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    with pytest.raises(review_environment.ProvisioningUnconfirmed, match="by signal"):
        review_environment._run_bounded(
            argv, mount, 5.0, label="uv sync", launch=launch
        )
    assert launch.uncertain is True


@pytest.mark.parametrize("padding", ["", "a" * 390])
def test_inherited_proxy_is_redacted_from_provisioning_warning(
    tmp_path, monkeypatch, caplog, padding
):
    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    config = _config()
    config.sandbox.proxy_environment = "inherit"
    mount = tmp_path / "copy"
    mount.mkdir()
    argv = review_environment._argv(config, IMAGE, "claude-code", mount, "s-env", SYNC)
    assert "HTTP_PROXY" in argv
    _fake_handle(
        monkeypatch, {}, lines=(f"failed {secret}{padding}\n",), wait_returns=2
    )
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    transcript = review_environment._run_bounded(argv, mount, 5.0, label="uv sync")
    assert transcript.exit_code == 2
    assert "last output:" in caplog.text
    assert secret not in caplog.text
    assert "proxy.invalid" not in caplog.text
    assert secret not in repr(transcript.tail)


def test_provisioning_exception_description_redacts_inherited_proxy(monkeypatch):
    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    description = review_environment._describe(RuntimeError(f"failed {secret}"))
    assert description == "RuntimeError: failed <HTTP_PROXY redacted>"


# --- #1963/#3315: the provisioning container is removed by its recorded id ---

PROVISION = "kew-42-env"


def _launch_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
    from support.fake_docker import LaunchDocker

    from kew import subprocess_utils

    fake = LaunchDocker(tmp_path / "runtime", **kwargs)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(fake.path))
    return fake


def test_a_left_provisioning_container_is_removed_by_the_id_its_launch_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _launch_docker(tmp_path, monkeypatch)
    ours = fake.add(PROVISION, {}, status="exited")

    review_environment._require_container_gone("docker", PROVISION, ours)

    assert fake.calls("rm") == [["rm", "-f", ours]]
    assert fake.containers() == {}


def test_a_container_under_the_name_that_is_not_the_recorded_one_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _launch_docker(tmp_path, monkeypatch)
    theirs = fake.add(PROVISION, {}, status="running")
    recorded = format(7, "064x")

    with pytest.raises(review_environment.ProvisioningUnconfirmed) as raised:
        review_environment._require_container_gone("docker", PROVISION, recorded)

    assert fake.calls("rm") == [["rm", "-f", recorded]]
    assert list(fake.containers()) == [theirs]
    assert not raised.value.timed_out


def test_a_launch_that_recorded_no_id_frees_its_per_item_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client died before the cidfile: the fixed provisioning name is the
    only handle left, and the next provisioning of the item needs it free."""
    fake = _launch_docker(tmp_path, monkeypatch)
    fake.add(PROVISION, {}, status="exited")

    review_environment._require_container_gone("docker", PROVISION, None)

    assert fake.calls("rm") == [["rm", "-f", PROVISION]]
    assert fake.containers() == {}


def test_an_absence_probe_the_daemon_never_answers_is_a_runtime_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loaded daemon that does not answer ``ps`` is retried later, not held
    on the first try (#3246)."""
    fake = _launch_docker(tmp_path, monkeypatch, ps_hangs=True)
    monkeypatch.setattr(review_environment, "_CONTAINER_PROBE_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(review_environment.ProvisioningUnconfirmed) as raised:
        review_environment._require_container_gone("docker", PROVISION, None)

    assert raised.value.timed_out
    assert "did not answer" in str(raised.value)
    assert fake.calls("rm") == []
