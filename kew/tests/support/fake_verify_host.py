"""A remote verify host faked at its process boundary.

What a ``gcp`` instance would do with a job, without a cloud: the host is up,
takes any bundle, releases cleanly, and answers each job from a list of
``(substring, exit code, log)`` entries, the first whose substring the job's
command contains. A job no entry matches answers ``(0, "")``. Every job is kept
(:attr:`FakeVerifyHost.jobs`), with the ceiling kew gave it, so a test asserts
what kew asked the host to run. ``expire`` makes every job outrun its ceiling,
the way a host reports one: :class:`~kew.hosts.HostTimeout`, no exit code.
``slow_start`` makes bringing the host up time out instead, as a ``gcloud``
call that waited too long for the instance does, before any job runs.

Install it with :func:`install`, which makes it the one host
``provider_pool`` hands the held-out check, the way a configured instance is.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from kew import merge_verify
from kew.hosts import HostProvider, HostTimeout, Job


class FakeVerifyHost(HostProvider):
    """One remote verify host."""

    name = "gcp"

    def __init__(
        self,
        lock_dir: Path,
        answers: Sequence[tuple[str, int, str]] = (),
        *,
        expire: bool = False,
        slow_start: bool = False,
    ) -> None:
        super().__init__(lock_dir=lock_dir)
        self.answers = list(answers)
        self.expire = expire
        self.slow_start = slow_start
        self.jobs: list[Job] = []

    @property
    def address(self) -> str:
        return "verify-1"

    @property
    def lock_key(self) -> str:
        return "fake-verify-1"

    def _ensure_up(self) -> None:
        if self.slow_start:
            raise HostTimeout("instance verify-1 did not reach RUNNING within 300s")

    def _ship(self, bundle_path: Path) -> None:
        pass

    def _release(self) -> None:
        pass

    def _execute(self, job: Job) -> tuple[int, str]:
        self.jobs.append(job)
        if self.expire:
            ceiling = job.timeout_seconds
            raise HostTimeout(f"{job.name!r} exceeded its {ceiling}s ceiling")
        for match, code, log in self.answers:
            if match in job.command:
                return code, log
        return 0, ""

    @property
    def commands(self) -> list[str]:
        return [job.command for job in self.jobs]


def install(monkeypatch: pytest.MonkeyPatch, host: FakeVerifyHost) -> FakeVerifyHost:
    """Make *host* the verify host every held-out check runs on."""
    monkeypatch.setattr(merge_verify, "provider_pool", lambda *_a, **_k: [host])
    return host
