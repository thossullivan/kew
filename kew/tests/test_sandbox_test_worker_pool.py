"""A sandbox's test-runner pools are sized to its ``--cpus`` cap (#2887).

``--cpus`` is a CFS quota, so ``nproc`` in the container still prints the
host's count and ``pytest -n auto`` started ten ~370 MB workers in a 3-CPU,
5 GiB container. The launch argv therefore tells pytest-xdist and vitest the
cap itself, read off the same argv's ``--cpus`` value.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.config import SandboxConfig
from kew.sandbox import (
    _cpu_cap_retry_argv,
    build_run_argv,
    resource_caps,
)

pytestmark = pytest.mark.usefixtures("real_daemon_cpu_probe")

_POOL_NAMES = (
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
    "VITEST_MAX_WORKERS",
    "VITEST_MAX_THREADS",
    "VITEST_MAX_FORKS",
)


def _daemon(answer: subprocess.CompletedProcess[str] | Exception):
    """Script the runtime's ``info`` reply: the process boundary."""
    if isinstance(answer, Exception):
        return patch("kew.sandbox.subprocess_utils.run", side_effect=answer)
    return patch("kew.sandbox.subprocess_utils.run", return_value=answer)


def _cpus(count: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=f"{count}\n", stderr=""
    )


def _launch_argv(checkout: Path, *, max_cpus: int | None = None) -> list[str]:
    return build_run_argv(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=checkout,
        slug="2887",
        network="allowlist",
        runner="claude-code",
        caps=resource_caps(SandboxConfig(max_cpus=max_cpus)),
    )


def _cpus_value(argv: list[str]) -> str:
    values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--cpus"]
    assert len(values) == 1, values
    return values[0]


def _env(argv: list[str]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for index, arg in enumerate(argv[:-1]):
        if arg == "-e" and "=" in argv[index + 1]:
            name, value = argv[index + 1].split("=", 1)
            seen.setdefault(name, []).append(value)
    return seen


@pytest.mark.parametrize(
    ("daemon_cpus", "max_cpus", "expected"),
    [(10, 3, "3"), (2, None, "2")],
    ids=["10-cpu-daemon-cap-3", "2-cpu-daemon"],
)
def test_every_pool_size_equals_the_cpus_value_in_the_same_argv(
    tmp_path: Path, daemon_cpus: int, max_cpus: int | None, expected: str
) -> None:
    with _daemon(_cpus(daemon_cpus)):
        argv = _launch_argv(tmp_path, max_cpus=max_cpus)

    assert _cpus_value(argv) == expected
    env = _env(argv)
    for name in _POOL_NAMES:
        # Exactly once: docker takes the last occurrence of a repeated -e.
        assert env.get(name) == [expected], name


def test_an_unanswered_daemon_sizes_the_pools_to_the_one_cpu_fallback(
    tmp_path: Path,
) -> None:
    with _daemon(OSError("no daemon")):
        argv = _launch_argv(tmp_path)

    assert _cpus_value(argv) == "1"
    env = _env(argv)
    assert {name: env.get(name) for name in _POOL_NAMES} == {
        name: ["1"] for name in _POOL_NAMES
    }


def test_the_pool_flags_reach_docker_before_the_image(tmp_path: Path) -> None:
    # Anything after the image is the container's command, not a docker flag.
    with _daemon(_cpus(10)):
        argv = _launch_argv(tmp_path, max_cpus=3)

    image_at = argv.index("kew-agent:latest")
    assert argv.index("PYTEST_XDIST_AUTO_NUM_WORKERS=3") < image_at


def test_the_one_cpu_relaunch_resizes_the_pools_with_the_cap(tmp_path: Path) -> None:
    # A daemon that refuses the cap gets one relaunch at one CPU; a pool still
    # sized for the refused cap would be the same OOM on a smaller box.
    with _daemon(_cpus(10)):
        argv = _launch_argv(tmp_path, max_cpus=3)

    with _daemon(_cpus(1)):
        retry = _cpu_cap_retry_argv(argv, 125, Decimal("1"))

    assert retry is not None
    assert _cpus_value(retry) == "1"
    env = _env(retry)
    assert {name: env.get(name) for name in _POOL_NAMES} == {
        name: ["1"] for name in _POOL_NAMES
    }
