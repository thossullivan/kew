"""Verified dispatch evidence for unit tests with simulated execution."""

import os

import pytest

from kew import process_groups


@pytest.fixture
def admission_owner(monkeypatch):
    """Supply verified evidence for claim unit tests."""
    pid = os.getpid()
    entry = process_groups.ProcessIdentity(
        os.getppid(), pid, "S", "Sun Sep 20 01:00:00 2026", "python3", "admitted-birth"
    )
    monkeypatch.setattr(process_groups, "exclusive_owner_pid", os.getpid)
    monkeypatch.setattr(process_groups, "process_identity", lambda _: entry)
    monkeypatch.setattr(
        process_groups, "process_start_token", lambda _: entry.start_token
    )
    monkeypatch.setattr(
        process_groups,
        "owner_snapshot",
        lambda: process_groups.OwnerSnapshot({pid: entry}),
    )
    return entry
