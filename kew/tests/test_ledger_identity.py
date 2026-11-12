"""Schema-v36 ledger identity binding tests."""

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kew.audit import audit_app
from kew.audit_chain import GENESIS_PREV, Anchor, entry_hash, format_anchor
from kew.ledger_identity import (
    LedgerIdentityError,
    RegistryRecord,
    atomic_write_registry,
    ledger_never_created,
    lost_ledger_reason,
    open_managed_store,
    registry_paths,
)
from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore


def _seed_v35(db_path: Path, project_root: Path) -> tuple[int, str]:
    """Create a realistic v35 database with one already-chained event."""
    with SqliteRunStore(str(db_path), project_root=project_root) as store:
        store._conn.execute("DELETE FROM audit_events")
        store._conn.execute("DROP TABLE ledger_identity")
        store._conn.commit()
        event_id = store.append_audit_event(
            timestamp="2026-07-01T00:00:00+00:00",
            event="existing_event",
            payload={"kept": True},
        )
        store._conn.execute("UPDATE schema_version SET version = 35 WHERE id = 1")
        store._conn.commit()
        row = store.read_chain_rows()[0]
    assert event_id is not None
    return event_id, entry_hash(row)


def test_v36_migration_binds_identity_without_rewriting_prior_chain(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    project_root = tmp_path / "project"
    project_root.mkdir()
    existing_id, existing_hash = _seed_v35(db_path, project_root)

    with SqliteRunStore(str(db_path), project_root=project_root) as store:
        identity = store._conn.execute(
            "SELECT ledger_id, project_root, created_at, binding_event_id "
            "FROM ledger_identity WHERE id = 1"
        ).fetchone()
        rows = store.read_chain_rows()
        version = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]

    assert identity is not None
    assert str(uuid.UUID(identity["ledger_id"])) == identity["ledger_id"]
    assert identity["project_root"] == str(project_root.resolve())
    assert identity["binding_event_id"] == rows[-1].id
    assert version == CURRENT_SCHEMA_VERSION
    assert len(rows) == 2
    assert rows[0].id == existing_id
    assert entry_hash(rows[0]) == existing_hash
    assert rows[1].event == "ledger_identity_bound"
    assert rows[1].prev_hash == existing_hash
    assert rows[1].payload == json.dumps(
        {
            "format_version": 1,
            "ledger_id": identity["ledger_id"],
            "project_root": str(project_root.resolve()),
        },
        separators=(",", ":"),
    )


def test_fresh_database_first_event_binds_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path), project_root=tmp_path) as store:
        rows = store.read_chain_rows()
        identity = store._conn.execute(
            "SELECT ledger_id, project_root, binding_event_id "
            "FROM ledger_identity WHERE id = 1"
        ).fetchone()

    assert len(rows) == 1
    assert rows[0].event == "ledger_identity_bound"
    assert rows[0].prev_hash == GENESIS_PREV
    assert identity is not None
    assert identity["project_root"] == str(tmp_path.resolve())
    assert identity["binding_event_id"] == rows[0].id
    assert json.loads(rows[0].payload)["ledger_id"] == identity["ledger_id"]


def test_project_aware_open_binds_database_at_current_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path)) as unbound:
        assert (
            unbound._conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        assert (
            unbound._conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'ledger_identity'"
            ).fetchone()
            is None
        )

    with SqliteRunStore(str(db_path), project_root=tmp_path) as bound:
        identity = bound._conn.execute(
            "SELECT project_root, binding_event_id FROM ledger_identity WHERE id = 1"
        ).fetchone()
        rows = bound.read_chain_rows()

    assert identity is not None
    assert identity["project_root"] == str(tmp_path.resolve())
    assert identity["binding_event_id"] == rows[-1].id
    assert rows[-1].event == "ledger_identity_bound"


def test_v12_backfill_rolls_back_with_later_migration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path)) as seed:
        seed._conn.execute(
            "INSERT INTO audit_events "
            "(timestamp, event, actor_type, actor_id, prev_hash, payload) "
            "VALUES (?, ?, ?, ?, NULL, NULL)",
            ("2026-07-01T00:00:00+00:00", "legacy", "human", "tom"),
        )
        seed._conn.execute("UPDATE schema_version SET version = 11 WHERE id = 1")
        seed._conn.commit()

    def fail_v13(self: SqliteRunStore, cursor: sqlite3.Cursor) -> None:
        raise RuntimeError("injected later migration failure")

    monkeypatch.setattr(SqliteRunStore, "_apply_v13", fail_v13)
    with pytest.raises(RuntimeError, match="injected later migration failure"):
        SqliteRunStore(str(db_path))

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        prev_hash = conn.execute(
            "SELECT prev_hash FROM audit_events WHERE event = 'legacy'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert version == 11
    assert prev_hash is None


def test_v36_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path), project_root=tmp_path):
        pass
    with SqliteRunStore(str(db_path), project_root=tmp_path) as reopened:
        identities = reopened._conn.execute(
            "SELECT COUNT(*) FROM ledger_identity"
        ).fetchone()[0]
        bindings = reopened._conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event = 'ledger_identity_bound'"
        ).fetchone()[0]

    assert identities == 1
    assert bindings == 1


def test_v36_failure_rolls_back_identity_event_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dispatch.db"
    _seed_v35(db_path, tmp_path)

    def fail_insert(self: SqliteRunStore, values: tuple[object, ...]) -> int | None:
        raise RuntimeError("injected binding failure")

    monkeypatch.setattr(SqliteRunStore, "_chained_insert_in_transaction", fail_insert)
    with pytest.raises(RuntimeError, match="injected binding failure"):
        SqliteRunStore(str(db_path), project_root=tmp_path)

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ledger_identity'"
        ).fetchone()
        bindings = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event = 'ledger_identity_bound'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == 35
    assert table is None
    assert bindings == 0


def test_required_registered_open_never_creates_a_ledger(tmp_path: Path) -> None:
    import inspect

    assert "require_registered" in inspect.signature(open_managed_store).parameters
    project = tmp_path / "project"
    project.mkdir()
    database = project / "dispatch.db"
    with pytest.raises(LedgerIdentityError, match="REGISTERED-LEDGER-MISSING"):
        open_managed_store(
            project_root=project,
            db_path=database,
            anchors_path=project / "audit-anchors.jsonl",
            require_registered=True,
        )
    assert not database.exists()
    assert not registry_paths(project).registry.exists()


def test_required_registered_open_retains_writable_existing_ledger(
    tmp_path: Path,
) -> None:
    import inspect

    assert "require_registered" in inspect.signature(open_managed_store).parameters
    database = tmp_path / "dispatch.db"
    kwargs = dict(
        project_root=tmp_path,
        db_path=database,
        anchors_path=tmp_path / "audit-anchors.jsonl",
    )
    with open_managed_store(**kwargs) as store:
        original = store.read_ledger_id()
    with open_managed_store(**kwargs, require_registered=True) as store:
        assert store.read_ledger_id() == original
        assert store.append_audit_event(
            timestamp="2026-09-11T00:00:00+00:00", event="operator-proof"
        )


def test_managed_open_creates_identity_and_registry(tmp_path: Path) -> None:
    db_path = tmp_path / "logs" / "dispatch.db"
    anchors_path = tmp_path / "anchors.jsonl"

    with open_managed_store(
        project_root=tmp_path, db_path=db_path, anchors_path=anchors_path
    ) as store:
        identity = store._conn.execute(
            "SELECT ledger_id, project_root FROM ledger_identity WHERE id = 1"
        ).fetchone()

    record = RegistryRecord.from_json(registry_paths(tmp_path).registry.read_text())
    assert record.ledger_id == identity["ledger_id"]
    assert record.project_root == tmp_path.resolve()
    assert record.db_path == db_path.resolve()


@pytest.mark.parametrize("contents", ["not-json\n", '{"id":1}\n'])
def test_managed_open_rejects_malformed_anchors_before_creating_db(
    tmp_path: Path, contents: str
) -> None:
    db_path = tmp_path / "dispatch.db"
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(contents)

    with pytest.raises(LedgerIdentityError, match="INVALID-ANCHOR-HISTORY"):
        open_managed_store(project_root=tmp_path, db_path=db_path, anchors_path=anchors)

    assert not db_path.exists()
    assert not registry_paths(tmp_path).registry.exists()


def test_managed_open_rejects_nonempty_anchors_without_registry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    db_path.write_bytes(b"not a sqlite database")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        '{"id":1,"entry_hash":"'
        + "a" * 64
        + '","count":1,"timestamp":"t","prev":null}\n'
    )

    with pytest.raises(LedgerIdentityError, match="REGISTERED-LEDGER-MISSING"):
        open_managed_store(
            project_root=tmp_path,
            db_path=db_path,
            anchors_path=anchors,
        )


def test_managed_open_rejects_identity_database_without_registry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path), project_root=tmp_path):
        pass

    with pytest.raises(LedgerIdentityError, match="REGISTRY-MISSING-FOR-IDENTITY"):
        open_managed_store(
            project_root=tmp_path,
            db_path=db_path,
            anchors_path=tmp_path / "missing-anchors.jsonl",
        )


def test_managed_open_migrates_legacy_database_and_registers_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path)):
        pass

    with open_managed_store(
        project_root=tmp_path,
        db_path=db_path,
        anchors_path=tmp_path / "anchors.jsonl",
    ) as store:
        ledger_id = store._conn.execute(
            "SELECT ledger_id FROM ledger_identity WHERE id = 1"
        ).fetchone()[0]

    assert (
        RegistryRecord.from_json(
            registry_paths(tmp_path).registry.read_text()
        ).ledger_id
        == ledger_id
    )


def test_managed_open_rejects_registered_path_and_uuid_mismatches(
    tmp_path: Path,
) -> None:
    registered_db = tmp_path / "registered.db"
    with open_managed_store(
        project_root=tmp_path,
        db_path=registered_db,
        anchors_path=tmp_path / "anchors.jsonl",
    ):
        pass

    with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
        open_managed_store(
            project_root=tmp_path,
            db_path=tmp_path / "other.db",
            anchors_path=tmp_path / "anchors.jsonl",
        )

    record_path = registry_paths(tmp_path).registry
    record = RegistryRecord.from_json(record_path.read_text())
    atomic_write_registry(
        record_path,
        RegistryRecord(
            format_version=1,
            ledger_id=str(uuid.uuid4()),
            project_root=record.project_root,
            db_path=record.db_path,
        ),
    )
    with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
        open_managed_store(
            project_root=tmp_path,
            db_path=registered_db,
            anchors_path=tmp_path / "anchors.jsonl",
        )


def test_audit_verify_refuses_foreign_v2_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v2 anchor from another ledger is refused through the real CLI.

    Stricter sibling of
    test_audit.py::test_verify_wrong_ledger_precedes_count_and_hash: id,
    entry hash and count all match the chain; only ledger_id differs, so
    the refusal can only come from the identity comparison (#1157).
    """
    db_path = tmp_path / "dispatch.db"
    with open_managed_store(
        project_root=tmp_path,
        db_path=db_path,
        anchors_path=tmp_path / "managed-anchors.jsonl",
    ) as store:
        rows = store.read_chain_rows()
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None

    foreign_ledger_id = str(uuid.uuid4())
    anchors_path = tmp_path / "foreign-anchors.jsonl"
    anchors_path.write_text(
        format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=entry_hash(rows[-1]),
                count=len(rows),
                timestamp="2026-08-25T00:00:00+00:00",
                prev=GENESIS_PREV,
                version=2,
                ledger_id=foreign_ledger_id,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))

    result = CliRunner().invoke(
        audit_app, ["verify", "--anchors", str(anchors_path), "--json"]
    )

    assert foreign_ledger_id != ledger_id
    assert result.exit_code == 1
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False
    assert verdict["anchors"]["wrong_ledger"] is True


def test_audit_verify_detects_copied_ledger_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_db = source_root / "dispatch.db"
    with open_managed_store(
        project_root=source_root,
        db_path=source_db,
        anchors_path=source_root / "audit-anchors.jsonl",
    ) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None

    fork_root = tmp_path / "fork"
    fork_root.mkdir()
    fork_db = fork_root / "dispatch.db"
    shutil.copy2(source_db, fork_db)
    atomic_write_registry(
        registry_paths(fork_root).registry,
        RegistryRecord(
            format_version=1,
            ledger_id=ledger_id,
            project_root=fork_root.resolve(),
            db_path=fork_db.resolve(),
        ),
    )
    monkeypatch.setenv("KEW_LOG_DIR", str(fork_root))
    monkeypatch.setenv("KEW_PROJECT", str(fork_root))

    result = CliRunner().invoke(audit_app, ["verify"])

    assert result.exit_code == 2
    assert "Refusing to read the audit ledger" in result.stderr
    assert "WRONG-LEDGER: registry/database mismatch" in result.stderr


def test_audit_verify_detects_fork_by_cwd_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #1157 incident shape: no KEW_PROJECT, operator stands in the fork.

    project_root() falls back to the directory holding the kew.toml found by
    walking up from cwd. A copied ledger under that root, claimed by a
    registry, must be refused exactly as it is when the project is selected
    explicitly -- from the root and from a subdirectory of it.
    """
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_db = source_root / "dispatch.db"
    with open_managed_store(
        project_root=source_root,
        db_path=source_db,
        anchors_path=source_root / "audit-anchors.jsonl",
    ) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None

    fork_root = tmp_path / "fork"
    (fork_root / "sub").mkdir(parents=True)
    fork_db = fork_root / "dispatch.db"
    shutil.copy2(source_db, fork_db)
    (fork_root / "kew.toml").write_text('[logging]\ndir = "."\n', encoding="utf-8")
    atomic_write_registry(
        registry_paths(fork_root).registry,
        RegistryRecord(
            format_version=1,
            ledger_id=ledger_id,
            project_root=fork_root.resolve(),
            db_path=fork_db.resolve(),
        ),
    )
    monkeypatch.delenv("KEW_PROJECT", raising=False)
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)

    for cwd in (fork_root, fork_root / "sub"):
        monkeypatch.chdir(cwd)
        result = CliRunner().invoke(audit_app, ["verify"])
        assert result.exit_code == 2, (cwd, result.stdout, result.stderr)
        assert "Refusing to read the audit ledger" in result.stderr
        assert "WRONG-LEDGER: registry/database mismatch" in result.stderr


def test_ledger_never_created_only_when_database_registry_and_anchors_are_absent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / ".kew" / "runs.db"
    anchors = tmp_path / ".kew" / "anchors"
    assert ledger_never_created(tmp_path, db_path, anchors) is True

    anchors.parent.mkdir(parents=True)
    anchors.write_text("  \n")
    assert ledger_never_created(tmp_path, db_path, anchors) is True

    anchors.write_text("anchor-line\n")
    assert ledger_never_created(tmp_path, db_path, anchors) is False


@pytest.mark.parametrize("unreadable", ["directory", "invalid_utf8"])
def test_ledger_never_created_treats_unreadable_anchors_as_surviving_history(
    tmp_path: Path, unreadable: str
) -> None:
    db_path = tmp_path / ".kew" / "runs.db"
    anchors = tmp_path / ".kew" / "anchors"
    if unreadable == "directory":
        anchors.mkdir(parents=True)
    else:
        anchors.parent.mkdir(parents=True)
        anchors.write_bytes(b"\xff\xfe\xfa")
    assert ledger_never_created(tmp_path, db_path, anchors) is False


def test_ledger_never_created_is_false_while_the_database_exists(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    db_path.write_bytes(b"")

    assert ledger_never_created(tmp_path, db_path, tmp_path / "anchors") is False


def test_ledger_never_created_is_false_while_a_git_common_dir_registry_survives(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    registry = registry_paths(tmp_path).registry
    assert registry.is_relative_to(tmp_path / ".git")
    registry.parent.mkdir(parents=True)
    registry.write_text("{}")

    assert (
        ledger_never_created(tmp_path, tmp_path / "dispatch.db", tmp_path / "anchors")
        is False
    )


def _registered_then_lost(tmp_path: Path) -> Path:
    db_path = tmp_path / "dispatch.db"
    with open_managed_store(
        project_root=tmp_path,
        db_path=db_path,
        anchors_path=tmp_path / "anchors.jsonl",
    ):
        pass
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    return db_path


def test_lost_ledger_reason_names_the_registry_recorded_for_this_path(
    tmp_path: Path,
) -> None:
    db_path = _registered_then_lost(tmp_path)

    reason = lost_ledger_reason(tmp_path, db_path, tmp_path / "anchors.jsonl")

    assert reason is not None
    assert reason.startswith("REGISTERED-LEDGER-MISSING")
    assert str(registry_paths(tmp_path).registry) in reason
    with pytest.raises(LedgerIdentityError, match="REGISTERED-LEDGER-MISSING"):
        open_managed_store(
            project_root=tmp_path,
            db_path=db_path,
            anchors_path=tmp_path / "anchors.jsonl",
        )


def test_lost_ledger_reason_leaves_a_registry_for_another_path_to_the_open(
    tmp_path: Path,
) -> None:
    registered = tmp_path / "registered.db"
    with open_managed_store(
        project_root=tmp_path,
        db_path=registered,
        anchors_path=tmp_path / "anchors.jsonl",
    ):
        pass
    configured = tmp_path / "other" / "dispatch.db"

    assert lost_ledger_reason(tmp_path, configured, tmp_path / "anchors.jsonl") is None
    with pytest.raises(
        LedgerIdentityError, match="WRONG-LEDGER: configured path mismatch"
    ):
        open_managed_store(
            project_root=tmp_path,
            db_path=configured,
            anchors_path=tmp_path / "anchors.jsonl",
        )


@pytest.mark.parametrize("anchors", ["anchor-line\n", b"\xff\xfe"], ids=str)
def test_lost_ledger_reason_without_a_registry_names_the_anchors_file(
    tmp_path: Path, anchors: str | bytes
) -> None:
    anchors_path = tmp_path / "anchors.jsonl"
    if isinstance(anchors, bytes):
        anchors_path.write_bytes(anchors)
    else:
        anchors_path.write_text(anchors)

    reason = lost_ledger_reason(tmp_path, tmp_path / "dispatch.db", anchors_path)

    assert reason is not None
    assert reason.startswith("REGISTERED-LEDGER-MISSING")
    assert f"audit anchors file {anchors_path} is not empty or cannot be read" in (
        reason
    )
    assert "registry" not in reason


def test_lost_ledger_reason_is_none_for_a_directory_at_the_database_path(
    tmp_path: Path,
) -> None:
    db_path = _registered_then_lost(tmp_path)
    db_path.mkdir()

    assert ledger_never_created(tmp_path, db_path, tmp_path / "anchors.jsonl") is False
    assert lost_ledger_reason(tmp_path, db_path, tmp_path / "anchors.jsonl") is None


def test_lost_ledger_reason_is_none_when_nothing_survives(tmp_path: Path) -> None:
    assert (
        lost_ledger_reason(tmp_path, tmp_path / "dispatch.db", tmp_path / "anchors")
        is None
    )
