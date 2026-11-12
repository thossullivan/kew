"""A real process that holds the local verify lease (#3190).

The helper takes the lease through the production
:func:`kew.verify_lease.hold_verify_lease`, prints ``held`` once the lock and
its record are in place, and keeps both until its stdin closes. Waiting is on
that line, never a sleep; the lease is never mocked.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_HOLDER = """
import sys
from kew.verify_lease import hold_verify_lease

with hold_verify_lease(sys.argv[1]):
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    sys.stdin.read()
"""


class LeaseHolder:
    """A running helper process that holds the lease until released."""

    def __init__(self, ref: str) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - the test's own interpreter
            [sys.executable, "-c", _HOLDER, ref],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        if line != "held\n":
            self.process.kill()
            self.process.wait()
            raise RuntimeError(f"the lease helper did not take the lease: {line!r}")

    def release(self) -> None:
        """Let the helper leave its block and exit cleanly."""
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
        self.process.wait()

    def kill(self) -> None:
        """End the helper with SIGKILL: no ``finally`` runs."""
        self.process.kill()
        self.process.wait()


_PROBE = """
import fcntl, os, sys

descriptor = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.read()
"""


@contextmanager
def probe_in_progress(lease: Path) -> Iterator[None]:
    """Another process caught inside a ``verify_running`` probe: it holds the
    SHARED lock a probe takes on *lease* until the block ends."""
    process = subprocess.Popen(  # noqa: S603 - the test's own interpreter
        [sys.executable, "-c", _PROBE, str(lease)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        line = process.stdout.readline()
        if line != "held\n":
            raise RuntimeError(f"the probe helper took no shared lock: {line!r}")
        yield
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()


@contextmanager
def lease_held(ref: str = "refs/pull/7/merge") -> Iterator[LeaseHolder]:
    holder = LeaseHolder(ref)
    try:
        yield holder
    finally:
        if holder.process.poll() is None:
            holder.kill()
        if holder.process.stdin is not None and not holder.process.stdin.closed:
            holder.process.stdin.close()
        if holder.process.stdout is not None:
            holder.process.stdout.close()
