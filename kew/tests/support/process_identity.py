"""Deterministic identity observations for tests that own one real child."""

import os
from functools import partial

from kew import process_groups


def _child_snapshot(process, identity):
    return {process.pid: identity} if process.poll() is None else {}


def _child_start(process, identity, pid):
    return (
        identity.start_token if pid == process.pid and process.poll() is None else None
    )


def fixture_child_identity(monkeypatch, process):
    """Control birth observations while real child exit still determines death."""
    identity = process_groups.ProcessIdentity(
        os.getpid(),
        process.pid,
        "S",
        "Tue Sep 15 10:00:00 2026",
        "python",
        f"fixture-birth-{process.pid}",
    )
    monkeypatch.setattr(
        process_groups,
        "_identity_snapshot",
        partial(_child_snapshot, process, identity),
    )
    monkeypatch.setattr(
        process_groups, "process_start_token", partial(_child_start, process, identity)
    )
