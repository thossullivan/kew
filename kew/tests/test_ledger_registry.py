"""Focused ledger registry identity primitives."""

from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from pathlib import Path

import pytest

from kew.ledger_identity import (
    LedgerIdentityError,
    RegistryLockUnavailable,
    RegistryRecord,
    advisory_registry_lock,
    atomic_write_registry,
    canonical_project_root,
    registry_paths,
)


def _standard_repo(root: Path) -> Path:
    common_dir = root / ".git"
    common_dir.mkdir(parents=True)
    return common_dir


def _linked_worktree(common_dir: Path, checkout: Path) -> None:
    git_dir = common_dir / "worktrees" / checkout.name
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {git_dir}\n")


def test_standard_repo_and_linked_worktrees_share_canonical_root(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    common_dir = _standard_repo(primary)
    fresh = tmp_path / "fresh"
    stale = tmp_path / "stale"
    _linked_worktree(common_dir, fresh)
    _linked_worktree(common_dir, stale)
    (common_dir / "worktrees" / stale.name / "gitdir").write_text(
        "/missing/stale/.git\n"
    )

    expected = primary.resolve()
    assert canonical_project_root(primary) == expected
    assert canonical_project_root(fresh) == expected
    assert canonical_project_root(stale) == expected
    assert registry_paths(fresh).registry == common_dir / "kew" / "ledger.json"
    assert registry_paths(stale).lock == common_dir / "kew" / "ledger.lock"


def test_nonstandard_common_dir_uses_core_worktree(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    common_dir = tmp_path / "git-metadata"
    common_dir.mkdir()
    (common_dir / "config").write_text("[core]\n    worktree = ../project\n")
    (root / ".git").write_text(f"gitdir: {common_dir}\n")

    assert canonical_project_root(root) == root.resolve()
    assert registry_paths(root).registry == common_dir / "kew" / "ledger.json"


@pytest.mark.parametrize(
    "config",
    ["[core]\n", "[core]\n    bare = true\n", "[core]\n    worktree = ../missing\n"],
)
def test_nonstandard_common_dir_without_worktree_fails_closed(
    tmp_path: Path, config: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    common_dir = tmp_path / "metadata"
    common_dir.mkdir()
    (common_dir / "config").write_text(config)
    (checkout / ".git").write_text(f"gitdir: {common_dir}\n")

    with pytest.raises(LedgerIdentityError, match="working tree"):
        canonical_project_root(checkout)


def test_non_git_project_uses_local_kew_directory(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    nested = root / "nested"
    nested.mkdir(parents=True)

    assert canonical_project_root(root) == root.resolve()
    paths = registry_paths(root)
    assert paths.registry == root.resolve() / ".kew" / "ledger.json"
    assert paths.lock == root.resolve() / ".kew" / "ledger.lock"


def test_registry_record_round_trip_has_exact_frozen_keys(tmp_path: Path) -> None:
    ledger_id = str(uuid.uuid4())
    record = RegistryRecord(
        format_version=1,
        ledger_id=ledger_id,
        project_root=tmp_path.resolve(),
        db_path=(tmp_path / "dispatch.db").resolve(),
    )

    encoded = record.to_json()
    assert set(json.loads(encoded)) == {
        "format_version",
        "ledger_id",
        "project_root",
        "db_path",
    }
    assert RegistryRecord.from_json(encoded) == record


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"format_version":2,"ledger_id":"00000000-0000-0000-0000-000000000000",'
        '"project_root":"/tmp","db_path":"/tmp/db"}',
        '{"format_version":1,"ledger_id":"bad","project_root":"/tmp",'
        '"db_path":"/tmp/db"}',
        '{"format_version":1,"ledger_id":"00000000-0000-0000-0000-000000000000",'
        '"project_root":"relative","db_path":"/tmp/db"}',
        '{"format_version":1,"ledger_id":"00000000-0000-0000-0000-000000000000",'
        '"project_root":"/tmp","db_path":"/tmp/db","extra":true}',
        "not-json",
    ],
)
def test_registry_record_rejects_malformed_or_noncanonical_data(payload: str) -> None:
    with pytest.raises(LedgerIdentityError):
        RegistryRecord.from_json(payload)


def _try_lock(lock_path: str, queue: multiprocessing.Queue[bool]) -> None:
    try:
        with advisory_registry_lock(Path(lock_path), blocking=False):
            queue.put(True)
    except RegistryLockUnavailable:
        queue.put(False)


def test_registry_lock_excludes_same_path_but_not_different_path(
    tmp_path: Path,
) -> None:
    held = tmp_path / "held.lock"
    other = tmp_path / "other.lock"
    ctx = multiprocessing.get_context("spawn")

    with advisory_registry_lock(held):
        for path, expected in ((held, False), (other, True)):
            queue = ctx.Queue()
            process = ctx.Process(target=_try_lock, args=(str(path), queue))
            process.start()
            process.join(timeout=5)
            assert process.exitcode == 0
            assert queue.get(timeout=1) is expected


def test_atomic_registry_write_fsyncs_file_then_renames_and_fsyncs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state" / "ledger.json"
    record = RegistryRecord(
        format_version=1,
        ledger_id=str(uuid.uuid4()),
        project_root=tmp_path.resolve(),
        db_path=(tmp_path / "dispatch.db").resolve(),
    )
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "replace", tracked_replace)

    atomic_write_registry(target, record)

    assert RegistryRecord.from_json(target.read_text()) == record
    assert events == ["fsync", "replace", "fsync"]
    assert list(target.parent.glob(".ledger.json.*.tmp")) == []
