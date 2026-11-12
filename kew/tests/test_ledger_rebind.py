"""Explicit, confirmation-bound ledger registry recovery."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kew.audit import audit_app
from kew.audit_chain import Anchor, anchor_hash, entry_hash, format_anchor
from kew.ledger_identity import (
    LedgerIdentityError,
    RegistryRecord,
    ledger_rebind,
    registry_paths,
)
from kew.run_store import SqliteRunStore


def _identity_ledger(db_path: Path, project_root: Path) -> str:
    with SqliteRunStore(str(db_path), project_root=project_root) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None
    return ledger_id


def _legacy_ledger(db_path: Path, project_root: Path) -> Anchor:
    with SqliteRunStore(str(db_path), project_root=project_root) as store:
        store._conn.execute("DELETE FROM audit_events")
        store._conn.execute("DROP TABLE ledger_identity")
        store._conn.execute("UPDATE schema_version SET version = 35 WHERE id = 1")
        store._conn.commit()
        store.append_audit_event(
            timestamp="2026-07-01T00:00:00+00:00",
            event="legacy_event",
            payload={"kept": True},
        )
        row = store.read_chain_rows()[-1]
    return Anchor(
        id=row.id,
        entry_hash=entry_hash(row),
        count=1,
        timestamp="2026-07-01T01:00:00+00:00",
    )


def test_identity_recovery_records_compact_decision_without_second_binding(
    tmp_path: Path,
) -> None:
    target = tmp_path / "moved" / "dispatch.db"
    target.parent.mkdir()
    ledger_id = _identity_ledger(target, tmp_path)

    result = ledger_rebind(
        project_root=tmp_path,
        target=target,
        anchors_path=tmp_path / "anchors.jsonl",
        expected_ledger_id=ledger_id,
        reason="recover moved canonical ledger",
    )

    assert result.ledger_id == ledger_id
    record = RegistryRecord.from_json(
        registry_paths(tmp_path).registry.read_text(encoding="utf-8")
    )
    assert record.db_path == target.resolve()
    with SqliteRunStore(str(target), project_root=tmp_path) as store:
        rows = store.read_chain_rows()
    assert [row.event for row in rows].count("ledger_identity_bound") == 1
    rebound = [row for row in rows if row.event == "ledger_registry_rebound"]
    assert len(rebound) == 1
    assert rebound[0].payload == json.dumps(
        {
            "old_path": None,
            "new_path": str(target.resolve()),
            "ledger_id": ledger_id,
            "expected_anchor_hash": None,
            "reason": "recover moved canonical ledger",
        },
        separators=(",", ":"),
    )


def test_identity_recovery_fails_closed_before_writes_on_mismatch(
    tmp_path: Path,
) -> None:
    target = tmp_path / "dispatch.db"
    _identity_ledger(target, tmp_path)

    with pytest.raises(LedgerIdentityError, match="expected ledger"):
        ledger_rebind(
            project_root=tmp_path,
            target=target,
            anchors_path=tmp_path / "anchors.jsonl",
            expected_ledger_id=str(uuid.uuid4()),
            reason="recover",
        )

    assert not registry_paths(tmp_path).registry.exists()
    with SqliteRunStore(str(target), project_root=tmp_path) as store:
        assert not [
            row
            for row in store.read_chain_rows()
            if row.event == "ledger_registry_rebound"
        ]


def test_legacy_adoption_requires_latest_anchor_hash_then_migrates(
    tmp_path: Path,
) -> None:
    target = tmp_path / "legacy.db"
    anchor = _legacy_ledger(target, tmp_path)
    anchors_path = tmp_path / "anchors.jsonl"
    anchors_path.write_text(format_anchor(anchor) + "\n", encoding="utf-8")

    result = ledger_rebind(
        project_root=tmp_path,
        target=target,
        anchors_path=anchors_path,
        expected_anchor_hash=anchor_hash(anchor),
        reason="adopt verified legacy ledger",
    )

    with SqliteRunStore(str(target), project_root=tmp_path) as store:
        events = [row.event for row in store.read_chain_rows()]
        assert store.read_ledger_id() == result.ledger_id
    assert events == [
        "legacy_event",
        "ledger_registry_rebound",
        "ledger_identity_bound",
    ]


def test_legacy_decision_is_durable_before_identity_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "legacy.db"
    anchor = _legacy_ledger(target, tmp_path)
    anchors_path = tmp_path / "anchors.jsonl"
    anchors_path.write_text(format_anchor(anchor) + "\n", encoding="utf-8")

    def fail_after_checking_decision(
        self: SqliteRunStore, cursor: sqlite3.Cursor
    ) -> None:
        event = cursor.execute(
            "SELECT event FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert event == "ledger_registry_rebound"
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(SqliteRunStore, "_apply_v36", fail_after_checking_decision)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        ledger_rebind(
            project_root=tmp_path,
            target=target,
            anchors_path=anchors_path,
            expected_anchor_hash=anchor_hash(anchor),
            reason="adopt only after recording",
        )

    connection = sqlite3.connect(target)
    try:
        version = connection.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
        events = [
            row[0]
            for row in connection.execute(
                "SELECT event FROM audit_events ORDER BY id"
            ).fetchall()
        ]
    finally:
        connection.close()
    assert version == 35
    assert events == ["legacy_event", "ledger_registry_rebound"]
    assert not registry_paths(tmp_path).registry.exists()


def test_rebind_rejects_both_confirmation_modes(tmp_path: Path) -> None:
    target = tmp_path / "dispatch.db"
    ledger_id = _identity_ledger(target, tmp_path)

    with pytest.raises(LedgerIdentityError, match="exactly one"):
        ledger_rebind(
            project_root=tmp_path,
            target=target,
            anchors_path=tmp_path / "anchors.jsonl",
            expected_ledger_id=ledger_id,
            expected_anchor_hash="a" * 64,
            reason="ambiguous",
        )


def test_identity_recovery_rejects_anchor_from_another_chain(tmp_path: Path) -> None:
    target = tmp_path / "dispatch.db"
    ledger_id = _identity_ledger(target, tmp_path)
    foreign = Anchor(
        id=1,
        entry_hash="f" * 64,
        count=1,
        timestamp="2026-07-01T00:00:00+00:00",
    )
    anchors_path = tmp_path / "anchors.jsonl"
    anchors_path.write_text(format_anchor(foreign) + "\n", encoding="utf-8")

    with pytest.raises(LedgerIdentityError, match="database/anchor"):
        ledger_rebind(
            project_root=tmp_path,
            target=target,
            anchors_path=anchors_path,
            expected_ledger_id=ledger_id,
            reason="must not splice histories",
        )

    assert not registry_paths(tmp_path).registry.exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "Missing option"),
        (["--yes"], "Missing option"),
        (
            ["--yes", "--reason", "recover", "--expected-ledger-id", "not-a-uuid"],
            "canonical full UUID",
        ),
    ],
)
def test_cli_rebind_requires_target_confirmation_yes_and_reason(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    result = CliRunner().invoke(
        audit_app,
        ["ledger-rebind", "--target", str(tmp_path / "dispatch.db"), *arguments],
    )

    assert result.exit_code != 0
    assert message in result.output


def test_cli_rebind_succeeds_only_with_explicit_identity_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "dispatch.db"
    ledger_id = _identity_ledger(target, tmp_path)
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))

    result = CliRunner().invoke(
        audit_app,
        [
            "ledger-rebind",
            "--target",
            str(target),
            "--expected-ledger-id",
            ledger_id,
            "--reason",
            "recover canonical path",
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert ledger_id in result.output
