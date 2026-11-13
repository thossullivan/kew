"""Deterministic host identity for lifecycle tests that simulate agent execution."""

import os
from unittest.mock import Mock

from kew import process_groups, run_store

BIRTH = "Sun Sep 20 10:00:00 2026"


def _birth(pid):
    return BIRTH


def _identity_number(pid):
    return f"test-process-{pid}"


def _identity(pid):
    parent = os.getppid() if pid == os.getpid() else os.getpid()
    return process_groups.ProcessIdentity(
        parent, pid, "S", BIRTH, "python3", _identity_number(pid)
    )


def _owner_alive(pgid, owner_pid, *args, **kwargs):
    return True if pgid == owner_pid == os.getpid() else None


def _descends_from(pgid, owner_pid):
    return pgid != owner_pid and owner_pid == os.getpid()


def install_dispatch_identity(monkeypatch):
    """Supply admission and registration observations without inspecting host ps."""
    monkeypatch.setattr(process_groups, "exclusive_owner_pid", os.getpid)
    monkeypatch.setattr(process_groups, "process_identity", _identity)
    for module in (process_groups, run_store):
        monkeypatch.setattr(module, "process_start_time", _birth)
        monkeypatch.setattr(module, "process_start_token", _identity_number)
        monkeypatch.setattr(module, "owner_alive", _owner_alive)
    monkeypatch.setattr(run_store, "attempt_descends_from", _descends_from)
    monkeypatch.setattr(process_groups, "_identity_snapshot", Mock(return_value={}))
