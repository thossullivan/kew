"""Test isolation: no test may leave a checkout in the machine's temp root (#1480).

kew's two checkout primitives — ``review_dispatch.materialize_reviewer_worktree``
and ``dispatch_core._materialize_checkout`` — call ``tempfile.mkdtemp`` with no
``dir=``. Production owns the result and removes it; a test calls the primitive
and hands the result to nobody, so nothing removed it. Six days of CI on the
self-hosted runner left 3,284 ``kew-review-*`` and 333 ``kew-sandbox-*`` clones,
growing by ten per suite run.

The autouse ``_hermetic_temp_root`` fixture in conftest points ``tempfile``'s
root at each test's ``tmp_path``. These tests fail if that fixture is removed or
stops working — the leak is silent otherwise, which is how it ran for 1,613
sessions.
"""

import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest

KEW_DIR = Path(__file__).resolve().parents[1]


def test_bare_mkdtemp_lands_inside_this_test_s_tmp_path(tmp_path: Path):
    """The exact call both checkout primitives make is redirected.

    Asserted at the mechanism rather than against a list of known callers, so a
    primitive added later is covered without anyone remembering to add it here.
    """
    created = Path(tempfile.mkdtemp(prefix="kew-probe-"))
    try:
        assert created.is_relative_to(tmp_path), (
            f"mkdtemp escaped this test's tmp_path: {created}. The autouse "
            "_hermetic_temp_root fixture is not in force, so every checkout a "
            "test materializes now accumulates on the machine forever."
        )
    finally:
        created.rmdir()


def test_the_suite_s_temp_root_is_not_the_machine_s(tmp_path: Path):
    """``gettempdir`` answers the redirected root, which is what mkdtemp reads."""
    assert Path(tempfile.gettempdir()) == tmp_path / "tmp"


def test_pytest_retains_only_the_latest_failed_run() -> None:
    config = tomllib.loads((KEW_DIR / "pyproject.toml").read_text())
    pytest_options = config["tool"]["pytest"]["ini_options"]

    assert pytest_options.get("tmp_path_retention_policy") == "failed"
    assert pytest_options.get("tmp_path_retention_count") == 1


def _synthetic_pytest_command(test_file: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-c",
        str(KEW_DIR / "pyproject.toml"),
        str(test_file),
        "-q",
    ]


def _retained_runs(temp_root: Path) -> list[Path]:
    return sorted(
        path
        for owner_root in temp_root.glob("pytest-of-*")
        for path in owner_root.glob("pytest-[0-9]*")
        if path.is_dir() and not path.is_symlink()
    )


@pytest.mark.serial
def test_success_cleanup_and_concurrent_pytest_roots_are_isolated(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_synthetic_retention.py"
    test_file.write_text(
        """\
import os
import time
from pathlib import Path

def test_retention_probe(tmp_path):
    tmp_path.joinpath("owned.bin").write_bytes(b"owned")
    ready = os.environ.get("READY_FILE")
    release = os.environ.get("RELEASE_FILE")
    if ready:
        Path(ready).write_text(str(tmp_path))
    while release and not Path(release).exists():
        time.sleep(0.01)
    assert os.environ.get("FAIL_PROBE") != "1"
"""
    )
    temp_root = tmp_path / "child-temp"
    temp_root.mkdir()
    base_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temp_root),
    }

    passed = subprocess.run(
        _synthetic_pytest_command(test_file),
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert _retained_runs(temp_root) == []

    processes: list[subprocess.Popen[str]] = []
    ready_files = [tmp_path / "ready-a", tmp_path / "ready-b"]
    release_files = [tmp_path / "release-a", tmp_path / "release-b"]
    try:
        for ready, release in zip(ready_files, release_files, strict=True):
            env = {
                **base_env,
                "READY_FILE": str(ready),
                "RELEASE_FILE": str(release),
            }
            processes.append(
                subprocess.Popen(
                    _synthetic_pytest_command(test_file),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_files):
            assert time.monotonic() < deadline, "concurrent pytest probes did not start"
            time.sleep(0.01)
        active_paths = [Path(path.read_text()) for path in ready_files]
        assert active_paths[0] != active_paths[1]
        assert all(path.exists() for path in active_paths)
        for release in release_files:
            release.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()

    assert _retained_runs(temp_root) == []

    for _ in range(2):
        failed = subprocess.run(
            _synthetic_pytest_command(test_file),
            env={**base_env, "FAIL_PROBE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert failed.returncode == 1, failed.stdout + failed.stderr
    retained = _retained_runs(temp_root)
    assert len(retained) == 1
    assert retained[0].joinpath("test_retention_probe0", "owned.bin").is_file()
