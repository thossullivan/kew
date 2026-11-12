import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.ledger_identity import open_managed_store, registry_paths
from kew.run_store import RunRecord, SqliteRunStore


def _seed_db(path: Path) -> None:
    with SqliteRunStore(str(path)) as store:
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=1,
                branch="agent/issue-1",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )


def _seed_identity_db(project: Path) -> tuple[Path, str]:
    db = project / "logs" / "dispatch.db"
    with open_managed_store(
        project_root=project,
        db_path=db,
        anchors_path=project / "audit-anchors.jsonl",
    ) as store:
        identity = store._conn.execute(
            "SELECT ledger_id FROM ledger_identity WHERE id = 1"
        ).fetchone()
        assert identity is not None
        ledger_id = str(identity["ledger_id"])
    return db, ledger_id


def test_create_backup_produces_archive_and_manifest(tmp_path):
    from kew.backup import create_backup

    db = tmp_path / "logs" / "dispatch.db"
    db.parent.mkdir(parents=True)
    _seed_db(db)
    cfg = tmp_path / "kew.toml"
    cfg.write_text("[budget]\nper_issue_usd = 5.0\n")
    out_dir = tmp_path / "backups"

    manifest = create_backup(db, cfg, out_dir, include_logs=False)

    archives = list(out_dir.glob("kew-backup-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tar:
        names = set(tar.getnames())
    assert {"manifest.json", "dispatch.db", "kew.toml"} <= names
    assert manifest.schema_version >= 14
    assert manifest.runs == 1
    assert manifest.config_included is True
    assert manifest.chain_ok is True


def test_backup_does_not_mutate_source(tmp_path):
    import sqlite3

    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    conn_before = sqlite3.connect(str(db))
    before = conn_before.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    conn_before.close()
    create_backup(db, None, tmp_path / "out", include_logs=False)
    conn_after = sqlite3.connect(str(db))
    after = conn_after.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()[0]
    conn_after.close()
    assert before == after  # backup is read-only on the source


def test_backup_no_config_marks_manifest(tmp_path):
    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    manifest = create_backup(db, None, tmp_path / "out", include_logs=False)
    with tarfile.open(next((tmp_path / "out").glob("*.tar.gz"))) as tar:
        names = set(tar.getnames())
    assert "kew.toml" not in names
    assert manifest.config_included is False


def test_backup_records_broken_chain_but_still_archives(tmp_path):
    import sqlite3

    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    # Tamper a chained audit row so verify_chain reports not-ok.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO audit_events (timestamp, event, prev_hash) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'x', 'deadbeef')"
    )
    conn.commit()
    conn.close()
    manifest = create_backup(db, None, tmp_path / "out", include_logs=False)
    assert manifest.chain_ok is False
    assert list((tmp_path / "out").glob("*.tar.gz"))  # still produced an archive


def _backup_roundtrip_archive(tmp_path) -> Path:
    from kew.backup import create_backup

    db = tmp_path / "src" / "dispatch.db"
    db.parent.mkdir(parents=True)
    _seed_db(db)
    create_backup(db, None, tmp_path / "out", include_logs=False)
    return next((tmp_path / "out").glob("*.tar.gz"))


def test_restore_roundtrip(tmp_path):
    from kew.backup import restore_backup

    archive = _backup_roundtrip_archive(tmp_path)
    target = tmp_path / "restored"
    target.mkdir()
    result = restore_backup(archive, target, target, force=False, verify=True)
    assert (target / "dispatch.db").exists()
    assert result.manifest.runs == 1
    # restored db verifies and carries the seeded run
    with SqliteRunStore(str(target / "dispatch.db")) as store:
        assert store.get("r1") is not None


def test_a_backup_from_before_the_chain_format_step_verifies_and_restores(tmp_path):
    """A schema-76 ledger has no chain_format column; its backup still verifies
    end to end and restores with the chain check on."""
    import sqlite3

    from kew.audit_chain import GENESIS_PREV, ChainRow, entry_hash
    from kew.backup import create_backup, restore_backup

    db = tmp_path / "src" / "dispatch.db"
    db.parent.mkdir(parents=True)
    _seed_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("DROP TRIGGER audit_chain_format_no_downgrade")
        conn.execute("ALTER TABLE audit_events DROP COLUMN chain_format")
        conn.execute("UPDATE schema_version SET version = 76 WHERE id = 1")
        conn.execute("DELETE FROM audit_events")
        row = ChainRow(
            id=1,
            timestamp="2026-10-01T00:00:00+00:00",
            event="epic_merge",
            run_id=None,
            issue_num=None,
            repo=None,
            actor_type="human",
            actor_id="tom",
            endpoint=None,
            policy_hash=None,
            payload=None,
            prev_hash=GENESIS_PREV,
        )
        conn.execute(
            "INSERT INTO audit_events (id, timestamp, event, actor_type, actor_id,"
            " prev_hash, item_id) VALUES (1, ?, ?, ?, ?, ?, 'SFD-1')",
            (row.timestamp, row.event, row.actor_type, row.actor_id, row.prev_hash),
        )
        conn.commit()
    finally:
        conn.close()

    manifest = create_backup(db, None, tmp_path / "out", include_logs=False)
    assert manifest.schema_version == 76
    assert manifest.chain_ok
    assert (manifest.chain_head_hash, manifest.chain_count) == (entry_hash(row), 1)
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    target = tmp_path / "restored"
    target.mkdir()
    result = restore_backup(archive, target, target, force=False, verify=True)
    assert result.manifest.chain_head_hash == entry_hash(row)


def test_restore_refuses_newer_schema(tmp_path):
    import json

    from kew.backup import restore_backup
    from kew.run_store import CURRENT_SCHEMA_VERSION, SchemaTooNewError

    archive = _backup_roundtrip_archive(tmp_path)
    # rewrite the manifest inside a fresh archive with a too-new schema_version
    work = tmp_path / "w"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    m = json.loads((work / "manifest.json").read_text())
    m["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    (work / "manifest.json").write_text(json.dumps(m))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for f in work.iterdir():
            tar.add(f, arcname=f.name)
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(SchemaTooNewError):
        restore_backup(bad, target, target, verify=False)


def test_restore_refuses_newer_db_schema_leaves_target_untouched(tmp_path):
    import sqlite3

    from kew.backup import restore_backup
    from kew.run_store import CURRENT_SCHEMA_VERSION, SchemaTooNewError

    archive = _backup_roundtrip_archive(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    # Doctor the extracted db ITSELF (manifest left honest), so only the
    # actual-db schema guard — not the manifest check — can catch it.
    conn = sqlite3.connect(str(work / "dispatch.db"))
    conn.execute(
        "UPDATE schema_version SET version = ? WHERE id = 1",
        (CURRENT_SCHEMA_VERSION + 1,),
    )
    conn.commit()
    conn.close()
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for f in work.iterdir():
            tar.add(f, arcname=f.name)
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(SchemaTooNewError):
        restore_backup(bad, target, target, verify=False)
    assert not (target / "dispatch.db").exists()  # rejected before any placement


def test_restore_missing_archive_raises_backup_error(tmp_path):
    from kew.backup import BackupError, restore_backup

    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(BackupError):
        restore_backup(tmp_path / "does-not-exist.tar.gz", target, target)


def test_restore_refuses_clobber_without_force(tmp_path):
    from kew.backup import BackupError, restore_backup

    archive = _backup_roundtrip_archive(tmp_path)
    target = tmp_path / "t"
    target.mkdir()
    (target / "dispatch.db").write_bytes(b"existing")
    with pytest.raises(BackupError):
        restore_backup(archive, target, target, force=False)


def test_restore_force_moves_existing_aside(tmp_path):
    from kew.backup import restore_backup

    archive = _backup_roundtrip_archive(tmp_path)
    target = tmp_path / "t"
    target.mkdir()
    # a real, idle existing db (so the idle probe passes)
    _seed_db(target / "dispatch.db")
    result = restore_backup(archive, target, target, force=True, verify=True)
    assert result.moved_old_to is not None
    assert result.moved_old_to.exists()
    assert (target / "dispatch.db").exists()


def test_restore_rejects_archive_without_manifest(tmp_path):
    import tarfile

    from kew.backup import BackupError, restore_backup

    bad = tmp_path / "nomanifest.tar.gz"
    payload = tmp_path / "p.txt"
    payload.write_text("x")
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")  # only a traversal member
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(BackupError):
        restore_backup(bad, target, target)


def test_restore_ignores_traversal_member_no_escape_write(tmp_path, monkeypatch):
    import tarfile
    import tempfile

    from kew.backup import restore_backup

    # Force restore's internal TemporaryDirectory under a known parent so we can
    # check whether a '../escape.txt' member would escape it.
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))

    # A VALID archive (manifest + dispatch.db) that ALSO carries a traversal member.
    good = _backup_roundtrip_archive(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    with tarfile.open(good) as tar:
        tar.extractall(work, filter="data")
    evil = tmp_path / "payload.txt"
    evil.write_text("pwned")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        for f in work.iterdir():
            tar.add(f, arcname=f.name)
        tar.add(evil, arcname="../escape.txt")

    target = tmp_path / "t"
    target.mkdir()
    restore_backup(bad, target, target, force=False, verify=True)
    assert not (systmp / "escape.txt").exists()  # traversal member was dropped
    assert (target / "dispatch.db").exists()  # the legitimate restore still ran


def test_restore_refuses_when_db_locked(tmp_path):
    import sqlite3

    from kew.backup import BackupError, restore_backup

    archive = _backup_roundtrip_archive(tmp_path)
    target = tmp_path / "t"
    target.mkdir()
    # seed a real, idle db so the idle probe has something to lock-probe against
    _seed_db(target / "dispatch.db")
    # hold a write lock — _assert_idle's BEGIN IMMEDIATE will fail
    holding_conn = sqlite3.connect(str(target / "dispatch.db"))
    holding_conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(BackupError):
            restore_backup(archive, target, target, force=True)
    finally:
        holding_conn.close()


def test_restore_bad_archive_raises_backup_error(tmp_path):
    from kew.backup import BackupError, restore_backup

    garbage = tmp_path / "garbage.tar.gz"
    garbage.write_bytes(b"this is not a gzip archive")
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(BackupError, match="not a readable kew backup archive"):
        restore_backup(garbage, target, target)


def _seed_interactive(path):
    from kew.run_store import InteractiveUsage, SqliteRunStore

    with SqliteRunStore(str(path)) as store:
        store.record_interactive_usage(
            InteractiveUsage(
                runner="claude-code",
                model="claude-opus-4-8",
                observed_at="2026-06-26T12:00:00+00:00",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.4,
                session_id="s",
                ingest_source="file:x",
                dedup_key="k1",
            )
        )


def test_manifest_counts_interactive_usage(tmp_path):
    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    _seed_interactive(db)
    manifest = create_backup(db, None, tmp_path / "out", include_logs=False)
    assert manifest.interactive_usage == 1
    assert manifest.schema_version >= 15


def test_manifest_handles_db_without_interactive_table(tmp_path):
    # A pre-v15 db lacks interactive_usage; backup uses a bare, non-migrating
    # connection, so the manifest count must fall back to 0 (not crash).
    import sqlite3

    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE interactive_usage")
    conn.commit()
    conn.close()
    manifest = create_backup(db, None, tmp_path / "out", include_logs=False)
    assert manifest.interactive_usage == 0


def test_restore_roundtrips_interactive_rows(tmp_path):
    from datetime import UTC, datetime

    from kew.backup import create_backup, restore_backup
    from kew.run_store import SqliteRunStore

    db = tmp_path / "src" / "dispatch.db"
    db.parent.mkdir(parents=True)
    _seed_db(db)
    _seed_interactive(db)
    create_backup(db, None, tmp_path / "out", include_logs=False)
    archive = next((tmp_path / "out").glob("*.tar.gz"))

    target = tmp_path / "restored"
    target.mkdir()
    restore_backup(archive, target, target, force=False, verify=True)
    with SqliteRunStore(str(target / "dispatch.db")) as store:
        rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        # off-chain: restoring interactive rows leaves the audit chain empty
        assert store.list_audit_events(limit=None) == []


def test_restore_refuses_broken_chain_unless_no_verify(tmp_path):
    import sqlite3

    from kew.backup import BackupError, create_backup, restore_backup

    db = tmp_path / "src" / "dispatch.db"
    db.parent.mkdir(parents=True)
    _seed_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO audit_events (timestamp, event, prev_hash) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'x', 'deadbeef')"
    )
    conn.commit()
    conn.close()
    create_backup(db, None, tmp_path / "out", include_logs=False)
    archive = next((tmp_path / "out").glob("*.tar.gz"))

    t1 = tmp_path / "t1"
    t1.mkdir()
    with pytest.raises(BackupError):
        restore_backup(archive, t1, t1, verify=True)

    t2 = tmp_path / "t2"
    t2.mkdir()
    restore_backup(archive, t2, t2, verify=False)
    assert (t2 / "dispatch.db").exists()


def test_identity_backup_manifest_records_registered_binding(tmp_path):
    from kew.backup import create_backup

    project = tmp_path / "project"
    project.mkdir()
    db, ledger_id = _seed_identity_db(project)

    manifest = create_backup(db, None, tmp_path / "out")

    assert manifest.ledger_id == ledger_id
    assert manifest.project_root == str(project.resolve())
    assert manifest.db_path == str(db.resolve())


def test_same_registered_identity_restore_auto_proceeds(tmp_path):
    from kew.backup import create_backup, restore_backup

    project = tmp_path / "project"
    project.mkdir()
    db, ledger_id = _seed_identity_db(project)
    create_backup(db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))

    result = restore_backup(
        archive,
        db.parent,
        project,
        force=True,
        project_root=project,
        anchors_path=project / "audit-anchors.jsonl",
    )

    assert result.manifest.ledger_id == ledger_id
    assert result.moved_old_to is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("ledger_id", "not-a-uuid"),
        ("project_root", "relative/project"),
        ("db_path", "relative/dispatch.db"),
    ],
)
def test_restore_rejects_malformed_identity_manifest_before_mutation(
    tmp_path, field, value
):
    from kew.backup import BackupError, create_backup, restore_backup

    project = tmp_path / "project"
    project.mkdir()
    db, _ = _seed_identity_db(project)
    create_backup(db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    work = tmp_path / "work"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    payload = json.loads((work / "manifest.json").read_text())
    payload[field] = value
    (work / "manifest.json").write_text(json.dumps(payload))
    malformed = tmp_path / "malformed.tar.gz"
    with tarfile.open(malformed, "w:gz") as tar:
        for member in work.iterdir():
            tar.add(member, arcname=member.name)
    before = db.read_bytes()

    with pytest.raises(BackupError, match="ledger-rebind"):
        restore_backup(
            malformed,
            db.parent,
            project,
            force=True,
            project_root=project,
            anchors_path=project / "audit-anchors.jsonl",
        )
    assert db.read_bytes() == before


def test_restore_rejects_different_registered_ledger_before_mutation(tmp_path):
    from kew.backup import BackupError, create_backup, restore_backup

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_db, _ = _seed_identity_db(source)
    target_db, _ = _seed_identity_db(target)
    create_backup(source_db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    before = target_db.read_bytes()

    with pytest.raises(BackupError, match="ledger-rebind"):
        restore_backup(
            archive,
            target_db.parent,
            target,
            force=True,
            project_root=target,
            anchors_path=target / "audit-anchors.jsonl",
        )
    assert target_db.read_bytes() == before


def test_identity_restore_rejects_missing_registry_before_mutation(tmp_path):
    from kew.backup import BackupError, create_backup, restore_backup

    project = tmp_path / "project"
    project.mkdir()
    db, _ = _seed_identity_db(project)
    create_backup(db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    registry_paths(project).registry.unlink()
    before = db.read_bytes()

    with pytest.raises(BackupError, match="ledger-rebind"):
        restore_backup(
            archive,
            db.parent,
            project,
            force=True,
            project_root=project,
            anchors_path=project / "audit-anchors.jsonl",
        )
    assert db.read_bytes() == before


def test_legacy_backup_cannot_replace_registered_identity_ledger(tmp_path):
    from kew.backup import BackupError, create_backup, restore_backup

    legacy_db = tmp_path / "legacy" / "dispatch.db"
    legacy_db.parent.mkdir()
    _seed_db(legacy_db)
    create_backup(legacy_db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    project = tmp_path / "project"
    project.mkdir()
    target_db, _ = _seed_identity_db(project)
    before = target_db.read_bytes()

    with pytest.raises(BackupError, match="ledger-rebind"):
        restore_backup(
            archive,
            target_db.parent,
            project,
            force=True,
            project_root=project,
            anchors_path=project / "audit-anchors.jsonl",
        )
    assert target_db.read_bytes() == before


def test_restore_validates_identity_binding_even_without_chain_verify(tmp_path):
    import sqlite3

    from kew.backup import BackupError, create_backup, restore_backup

    project = tmp_path / "project"
    project.mkdir()
    db, _ = _seed_identity_db(project)
    create_backup(db, None, tmp_path / "out")
    archive = next((tmp_path / "out").glob("*.tar.gz"))
    work = tmp_path / "work"
    work.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    conn = sqlite3.connect(work / "dispatch.db")
    conn.execute("UPDATE ledger_identity SET binding_event_id = NULL WHERE id = 1")
    conn.commit()
    conn.close()
    malformed = tmp_path / "malformed-binding.tar.gz"
    with tarfile.open(malformed, "w:gz") as tar:
        for member in work.iterdir():
            tar.add(member, arcname=member.name)
    before = db.read_bytes()

    with pytest.raises(BackupError, match="ledger-rebind"):
        restore_backup(
            malformed,
            db.parent,
            project,
            force=True,
            verify=False,
            project_root=project,
            anchors_path=project / "audit-anchors.jsonl",
        )
    assert db.read_bytes() == before


# ---------------------------------------------------------------------------
# I06T: a backup carries the sanctioned evidence set, never raw agent output
# ---------------------------------------------------------------------------


def _seed_log_dir(db_dir):
    """Everything a live log directory holds after a few runs."""
    (db_dir / "dispatch-1-20260101.log").write_text(
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"sk-ant-api03-NOTAREALKEY"}]}}\n',
        encoding="utf-8",
    )
    (db_dir / "dispatch-1-20260101.events.jsonl").write_text(
        '{"ts":1,"type":"activity","msg":"/Users/operator/secrets/id_rsa"}\n',
        encoding="utf-8",
    )
    (db_dir / "sandbox-issue-7.log").write_text(
        "raw contained output\n", encoding="utf-8"
    )
    (db_dir / "dispatch-1-20260101.evidence.json").write_text(
        '{"version":1,"steps":3}\n', encoding="utf-8"
    )
    (db_dir / "sandbox-issue-7.evidence.json").write_text(
        '{"version":1,"steps":9}\n', encoding="utf-8"
    )
    prompts = db_dir / ".kew" / "private" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "abc123.prompt").write_text(
        "the exact compiled prompt", encoding="utf-8"
    )


def _archive_names(out_dir):
    with tarfile.open(next(out_dir.glob("*.tar.gz"))) as tar:
        return set(tar.getnames()), {
            name: (
                tar.extractfile(name).read() if tar.getmember(name).isfile() else b""
            )
            for name in tar.getnames()
        }


def test_backup_excludes_raw_transcripts_and_event_files(tmp_path):
    """The whole point of --include-logs used to be the transcripts. A backup
    outlives the machine it was taken on, so shipping raw provider output in
    one ships every byte a provider echoed back to wherever the archive goes."""
    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    _seed_log_dir(tmp_path)
    out_dir = tmp_path / "backups"

    create_backup(db, None, out_dir, include_logs=True)

    names, contents = _archive_names(out_dir)
    assert "logs/dispatch-1-20260101.log" not in names
    assert "logs/dispatch-1-20260101.events.jsonl" not in names
    assert "logs/sandbox-issue-7.log" not in names
    blob = b"".join(contents.values())
    assert b"sk-ant-api03-NOTAREALKEY" not in blob
    assert b"/Users/operator/secrets/id_rsa" not in blob


def test_backup_stages_the_derived_evidence_sidecars(tmp_path):
    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    _seed_log_dir(tmp_path)
    out_dir = tmp_path / "backups"

    manifest = create_backup(db, None, out_dir, include_logs=True)

    names, _ = _archive_names(out_dir)
    assert "logs/dispatch-1-20260101.evidence.json" in names
    assert "logs/sandbox-issue-7.evidence.json" in names
    assert manifest.included_logs is True


def test_backup_never_stages_protected_prompt_bytes(tmp_path):
    """The exact compiled prompt is owner-only local bytes (I06). A managed
    copy of them is exactly the surface sensitive_prompt exists to deny."""
    from kew.backup import create_backup

    db = tmp_path / "dispatch.db"
    _seed_db(db)
    _seed_log_dir(tmp_path)
    out_dir = tmp_path / "backups"

    create_backup(db, None, out_dir, include_logs=True)

    names, contents = _archive_names(out_dir)
    assert not any(name.endswith(".prompt") for name in names)
    assert b"the exact compiled prompt" not in b"".join(contents.values())


def test_excluded_patterns_hold_even_if_a_glob_widens(tmp_path):
    """Belt and braces: the copy loop re-checks each candidate, so a staged
    pattern widened by a later edit still cannot carry a transcript out."""
    from kew.backup import _stage_logs

    src = tmp_path / "logs"
    src.mkdir()
    (src / "dispatch-9.log").write_text("raw", encoding="utf-8")
    (src / "dispatch-9.evidence.json").write_text("{}", encoding="utf-8")
    dest = tmp_path / "staged"

    with patch("kew.backup.STAGED_LOG_PATTERNS", ("dispatch-*",)):
        _stage_logs(src, dest)

    staged = {p.name for p in dest.iterdir()}
    assert staged == {"dispatch-9.evidence.json"}
