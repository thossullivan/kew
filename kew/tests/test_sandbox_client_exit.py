"""ContainerHandle: the runtime client's exit can be bounded and forced."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from kew.sandbox import ContainerHandle


def _sleeper() -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "import time; time.sleep(30)"], text=True
    )


def _stub(proc: subprocess.Popen[str]) -> SimpleNamespace:
    return SimpleNamespace(
        _proc=proc,
        _name="kew-s-env",
        _cache_container_id=lambda: None,
        observe_workload_start=lambda: False,
        cleanup_identity=lambda: None,
        # Round 14: wait() settles the startup observation before returning.
        finish_startup_observation=lambda: None,
        # #2053: it settles the separated stderr drain first, so the startup
        # facts it reads are complete.
        _settle_stderr_drain=lambda: None,
    )


def test_wait_with_a_timeout_escapes_instead_of_blocking() -> None:
    proc = _sleeper()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            ContainerHandle.wait(_stub(proc), timeout=0.2)
    finally:
        proc.kill()
        proc.wait()


def test_wait_without_a_timeout_returns_the_exit_code() -> None:
    proc = subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "raise SystemExit(3)"], text=True
    )
    assert ContainerHandle.wait(_stub(proc)) == 3


def test_settle_client_kills_a_live_client_and_reports_its_exit() -> None:
    proc = _sleeper()
    assert proc.poll() is None
    assert ContainerHandle.settle_client(_stub(proc), 5.0) is True
    assert proc.poll() is not None


def test_settle_client_is_a_no_op_for_an_exited_client() -> None:
    proc = subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "pass"], text=True
    )
    proc.wait()
    assert ContainerHandle.settle_client(_stub(proc), 5.0) is True


def test_settle_client_reports_a_client_that_survives_the_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kew.progress

    warnings: list[str] = []
    monkeypatch.setattr(kew.progress, "_warn", warnings.append)

    class Immortal:
        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(["docker"], timeout or 0)

    stub = SimpleNamespace(_proc=Immortal(), _name="kew-s-env")
    assert ContainerHandle.settle_client(stub, 0.01) is False
    assert any("did not exit" in w for w in warnings), warnings
