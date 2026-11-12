"""One job at a time per host, enforced by a lock kew owns.

The cloud enforces nothing here: two jobs shipped to the same instance would
clone over each other's checkout and read each other's tree. So the mutual
exclusion is kew's, held as a non-blocking ``flock`` on a lock file named for
the host — the same idiom as the review checkout claim (``review_checkout.py``)
and the per-epic driver lock. The holder's pid is written into the file once the
lock is held, so a refusal can name who holds it.

The lock is advisory and per-host, not per-provider-object: a second kew
process addressing the same instance contends for the same inode.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kew.hosts.errors import HostBusy

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def lock_path(directory: Path, key: str) -> Path:
    """The lock file for host *key* under *directory*."""
    return directory / f"{_UNSAFE.sub('-', key).strip('-') or 'host'}.lock"


def _holder(path: Path) -> str:
    """Describe the current holder, or say plainly that it could not be read."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"holder unreadable ({exc})"
    pid = record.get("pid") if isinstance(record, dict) else None
    return f"held by pid {pid}" if pid else "holder records no pid"


@contextmanager
def host_lock(directory: Path, key: str) -> Iterator[Path]:
    """Hold host *key* exclusively for the block, or raise :class:`HostBusy`.

    Yields the lock file path. The file is never unlinked: unlink-then-create
    is how two holders end up flocking two different inodes (#974 finding 3).
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = lock_path(directory, key)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise HostBusy(
                f"host {key} is already running a job ({_holder(path)}): "
                "one job at a time per host"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"host": key, "pid": os.getpid()}))
        handle.flush()
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def host_lock_held(directory: Path, key: str) -> bool:
    """Whether some job holds host *key*'s lock right now, without taking it.

    For a caller choosing BETWEEN hosts, not running on one: a placement asks
    this so an instance another kew job holds — a sharded verify shard, a
    remediation, a dispatch in another process — reads as taken before the
    run is admitted, instead of being picked and refused by :func:`host_lock`
    afterwards (#2526 review).

    The probe is a SHARED non-blocking lock, released at once, so two
    placements probing together never refuse each other; it conflicts only
    with an exclusive holder, which is exactly the question. A lock file that
    does not exist has never been held, and the probe creates nothing. The
    answer is a snapshot: a holder that arrives after it is still refused by
    :func:`host_lock` itself, which stays the guarantee.
    """
    path = lock_path(directory, key)
    # Checked rather than caught: :func:`host_lock` never unlinks the file, so
    # one that exists now still exists when it is opened below.
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False
