"""Managed-ledger wiring for approval and audit production store opens."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import kew.approval as approval
import kew.audit as audit
from kew.config import DispatchConfig
from kew.ledger_identity import LedgerIdentityError


class _FakeStore:
    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_approval_uses_exact_canonical_managed_identity_arguments(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs" / ".." / "ledger")
    captured: dict[str, object] = {}

    monkeypatch.setattr("kew.config.project_root", lambda: project)
    monkeypatch.setattr(
        "kew.ledger_identity.open_managed_store",
        lambda **kwargs: captured.update(kwargs) or _FakeStore(),
    )
    monkeypatch.setattr(approval, "record_current_policy", lambda store: None)
    monkeypatch.setattr(
        approval,
        "remote_approve",
        lambda *args, **kwargs: "released",
    )

    assert (
        approval.apply_approval_decision(
            "approve", issue_num=7, reason="", config=config, source="tui"
        )
        == "released"
    )
    assert captured == {
        "project_root": project.resolve(),
        "db_path": (tmp_path / "ledger" / "dispatch.db").resolve(),
        "anchors_path": project.resolve() / "audit-anchors.jsonl",
        "read_only": False,
        "check_same_thread": True,
    }


def test_audit_uses_exact_canonical_managed_identity_arguments(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(audit, "project_root", lambda: project)
    monkeypatch.setattr(
        audit,
        "open_managed_store",
        lambda **kwargs: captured.update(kwargs) or _FakeStore(),
    )

    assert audit._open_audit_store(tmp_path / "logs" / ".." / "dispatch.db")
    assert captured == {
        "project_root": project.resolve(),
        "db_path": (tmp_path / "dispatch.db").resolve(),
        "anchors_path": project.resolve() / "audit-anchors.jsonl",
        "read_only": False,
        "check_same_thread": True,
    }


@pytest.mark.parametrize("surface", ["approval", "audit"])
def test_governance_identity_refusal_fails_closed(
    surface: str, tmp_path: Path, monkeypatch: Any
) -> None:
    refusal = LedgerIdentityError("WRONG-LEDGER")

    def refuse(**kwargs: object) -> _FakeStore:
        raise refusal

    if surface == "audit":
        monkeypatch.setattr(audit, "project_root", lambda: tmp_path)
        monkeypatch.setattr(audit, "open_managed_store", refuse)
        with pytest.raises(LedgerIdentityError) as raised:
            audit._open_audit_store(tmp_path / "dispatch.db")
    else:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path)
        monkeypatch.setattr("kew.config.project_root", lambda: tmp_path)
        monkeypatch.setattr("kew.ledger_identity.open_managed_store", refuse)
        with pytest.raises(LedgerIdentityError) as raised:
            approval.apply_approval_decision(
                "approve", issue_num=7, reason="", config=config, source="tui"
            )
    assert raised.value is refusal


def test_governance_production_modules_do_not_open_sqlite_store_directly() -> None:
    for module in (approval, audit):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        raw_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SqliteRunStore"
        ]
        assert raw_calls == [], f"{module.__name__} bypasses managed ledger opens"
