"""The exact compiled prompt is a protected local artifact, never a store row.

Task 22 (RED) / Task 23 (GREEN) of I06. These tests pin the whole lifecycle:
owner-only no-follow creation, an exact-byte digest, terminal-plus-30-day
retention, explicit purge, and the absence of the bytes from every managed
surface (SQLite, audit payloads, exports, backups).
"""

from __future__ import annotations

import os
import stat
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    RETENTION_DAYS,
    PromptDigestMismatch,
    PromptRetentionError,
    PromptUnavailable,
    default_root,
    prompt_digest,
    purge_expired_prompts,
    purge_prompt,
    read_prompt,
    retain_prompt,
    retention_deadline,
)

_TEXT = "You are dispatched on issue #7.\nSecret spec: rotate the token 'hunter2'.\n"
_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _retain(root: Path, text: str = _TEXT):
    return retain_prompt(root, run_id="run-1", attempt_number=1, text=text, now=_NOW)


# ---------------------------------------------------------------------------
# Exact bytes and their digest
# ---------------------------------------------------------------------------


def test_the_exact_bytes_survive_the_round_trip(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    assert (
        read_prompt(root, run_id="run-1", attempt_number=1, expect_digest=ref.digest)
        == _TEXT
    )


def test_the_digest_covers_the_exact_bytes_not_a_normalized_form() -> None:
    assert prompt_digest("a\r\nb") != prompt_digest("a\nb")
    assert prompt_digest("a\n") != prompt_digest("a\n ")
    assert prompt_digest("").startswith("sha256:")
    assert len(prompt_digest("x")) == len("sha256:") + 64


def test_the_recorded_length_is_the_utf8_byte_count(tmp_path: Path) -> None:
    ref = _retain(default_root(tmp_path), text="é")
    assert ref.byte_length == 2


def test_the_artifact_facts_carry_a_digest_and_never_the_bytes(
    tmp_path: Path,
) -> None:
    ref = _retain(default_root(tmp_path))
    facts = ref.artifact_facts()
    assert facts["digest"] == ref.digest
    assert facts["artifact_kind"] == PROMPT_ARTIFACT_KIND
    assert "hunter2" not in repr(facts)
    assert all("hunter2" not in str(value) for value in facts.values())


def test_a_retained_reference_never_reprs_the_bytes(tmp_path: Path) -> None:
    ref = _retain(default_root(tmp_path))
    assert "hunter2" not in repr(ref)


# ---------------------------------------------------------------------------
# Owner-only, no-follow creation
# ---------------------------------------------------------------------------


def test_the_artifact_and_its_root_are_owner_only(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    path = root / ref.locator
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_creation_refuses_to_follow_a_symlink_planted_at_the_target(
    tmp_path: Path,
) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    purge_prompt(root, run_id="run-1", attempt_number=1)
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    os.symlink(victim, root / ref.locator)
    with pytest.raises(PromptRetentionError):
        _retain(root)
    assert victim.read_text() == "original"


def test_creation_refuses_a_root_that_is_a_symlink(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "linked-root"
    os.symlink(elsewhere, link)
    with pytest.raises(PromptRetentionError):
        _retain(link)


def test_reading_refuses_to_follow_a_swapped_symlink(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    path = root / ref.locator
    path.unlink()
    victim = tmp_path / "victim.txt"
    victim.write_text(_TEXT)
    os.symlink(victim, path)
    with pytest.raises(PromptRetentionError):
        read_prompt(root, run_id="run-1", attempt_number=1, expect_digest=ref.digest)


def test_an_identical_retention_is_idempotent_and_a_different_one_is_refused(
    tmp_path: Path,
) -> None:
    root = default_root(tmp_path)
    first = _retain(root)
    again = _retain(root)
    assert again.digest == first.digest
    with pytest.raises(PromptRetentionError):
        _retain(root, text=_TEXT + "tampered")


def test_a_tampered_artifact_fails_the_digest_check(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    (root / ref.locator).write_text("something else entirely")
    with pytest.raises(PromptDigestMismatch):
        read_prompt(root, run_id="run-1", attempt_number=1, expect_digest=ref.digest)


def test_the_locator_is_an_opaque_key_not_a_run_derived_path(
    tmp_path: Path,
) -> None:
    ref = retain_prompt(
        default_root(tmp_path),
        run_id="../../escape",
        attempt_number=1,
        text=_TEXT,
        now=_NOW,
    )
    assert "/" not in ref.locator
    assert "escape" not in ref.locator


# ---------------------------------------------------------------------------
# Retention, purge, unavailability
# ---------------------------------------------------------------------------


def test_retention_runs_thirty_days_past_terminal() -> None:
    terminal = datetime(2026, 8, 18, tzinfo=UTC)
    assert retention_deadline(terminal) == terminal + timedelta(days=RETENTION_DAYS)


def test_an_explicit_purge_removes_the_bytes(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    assert purge_prompt(root, run_id="run-1", attempt_number=1) is True
    assert not (root / ref.locator).exists()
    assert purge_prompt(root, run_id="run-1", attempt_number=1) is False


def test_a_purged_prompt_reads_as_unavailable_not_as_empty(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    ref = _retain(root)
    purge_prompt(root, run_id="run-1", attempt_number=1)
    with pytest.raises(PromptUnavailable):
        read_prompt(root, run_id="run-1", attempt_number=1, expect_digest=ref.digest)


class _FakeRetentionStore:
    """The metadata side of retention: rows in, purge acknowledgement out."""

    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.acknowledged: list[tuple[str, int, str]] = []

    def list_expired_artifacts(self, *, now, kind=None):  # noqa: ANN001
        return list(self.rows)

    def mark_artifact_purged(self, *, run_id, attempt_number, kind, purged_at, reason):  # noqa: ANN001
        self.acknowledged.append((run_id, attempt_number, reason))
        return True


class _Row:
    def __init__(self, run_id: str, attempt_number: int) -> None:
        self.run_id = run_id
        self.attempt_number = attempt_number
        self.kind = PROMPT_ARTIFACT_KIND


def test_expired_prompts_are_purged_and_the_metadata_acknowledges_it(
    tmp_path: Path,
) -> None:
    root = default_root(tmp_path)
    _retain(root)
    store = _FakeRetentionStore([_Row("run-1", 1)])
    purged = purge_expired_prompts(
        store, root, now=_NOW + timedelta(days=RETENTION_DAYS + 1)
    )
    assert purged == ["run-1"]
    assert store.acknowledged == [("run-1", 1, "retention")]
    assert list(root.iterdir()) == []


def test_a_prompt_already_gone_from_disk_is_still_acknowledged(
    tmp_path: Path,
) -> None:
    root = default_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    store = _FakeRetentionStore([_Row("run-9", 1)])
    assert purge_expired_prompts(store, root, now=_NOW) == ["run-9"]
    assert store.acknowledged == [("run-9", 1, "retention")]


# ---------------------------------------------------------------------------
# Managed backup / export exclusion
# ---------------------------------------------------------------------------


def test_the_protected_root_never_reaches_a_managed_backup(tmp_path: Path) -> None:
    from kew.backup import create_backup
    from kew.run_store import SqliteRunStore

    project = tmp_path / "project"
    project.mkdir()
    db_path = project / "dispatch.db"
    with SqliteRunStore(str(db_path)):
        pass
    root = default_root(project)
    _retain(root)
    out = tmp_path / "out"
    out.mkdir()
    manifest = create_backup(db_path, None, out, include_logs=True)
    assert manifest is not None
    archives = list(out.glob("*.tar.gz"))
    assert archives
    with tarfile.open(archives[0], "r:gz") as tar:
        names = tar.getnames()
        assert not any("prompt" in name for name in names), names
        blob = b""
        for member in tar.getmembers():
            handle = tar.extractfile(member) if member.isfile() else None
            if handle is not None:
                blob += handle.read()
    assert b"hunter2" not in blob


def test_the_default_root_is_a_private_subtree_of_the_project(tmp_path: Path) -> None:
    root = default_root(tmp_path)
    assert root.parts[-3:] == (".kew", "private", "prompts")
