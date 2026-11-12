"""Managed-ledger architecture and argument wiring for report production opens."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import kew.report as report
import kew.report_export as report_export
from kew.config import DispatchConfig
from kew.ledger_identity import LedgerIdentityError


class _FakeStore:
    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def list_since(self, since: datetime) -> list[object]:
        return []

    def list_audit_events(self, **kwargs: object) -> list[object]:
        return []

    def list_interactive_usage_since(self, since: datetime) -> list[object]:
        return []


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    return config


def test_report_opens_use_exact_canonical_managed_identity_arguments(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    db_path = tmp_path / "logs" / "dispatch.db"
    db_path.parent.mkdir()
    db_path.touch()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(report, "project_root", lambda: project)
    monkeypatch.setattr(report, "canonical_project_root", lambda root: root.resolve())
    monkeypatch.setattr(
        report,
        "open_managed_store",
        lambda **kwargs: calls.append(kwargs) or _FakeStore(),
    )
    monkeypatch.setattr(report, "sync_outcomes", lambda store, config: 0)

    config = _config(tmp_path)
    config.ingest.cap_usd = 1.0
    report._load_window(config, datetime(2026, 7, 1, tzinfo=UTC))
    report.ingest_cap_alert(config, days=7, now=datetime(2026, 7, 8, tzinfo=UTC))

    assert calls == [
        {
            "project_root": project.resolve(),
            "db_path": db_path.resolve(),
            "anchors_path": project.resolve() / "audit-anchors.jsonl",
            "read_only": False,
            "check_same_thread": True,
        },
        {
            "project_root": project.resolve(),
            "db_path": db_path.resolve(),
            "anchors_path": project.resolve() / "audit-anchors.jsonl",
            "read_only": False,
            "check_same_thread": True,
        },
    ]


def test_report_export_uses_report_managed_open_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config = _config(tmp_path)
    db_path = Path(config.logging.dir) / "dispatch.db"
    db_path.parent.mkdir()
    db_path.touch()
    seen: list[Path] = []
    refusal = LedgerIdentityError("WRONG-LEDGER")

    def refuse(path: Path) -> _FakeStore:
        seen.append(path)
        raise refusal

    monkeypatch.setattr(report_export, "_open_report_store", refuse)

    with pytest.raises(LedgerIdentityError) as raised:
        report_export.build_evidence_pack(
            config=config,
            since=datetime(2026, 7, 1, tzinfo=UTC),
            now=datetime(2026, 7, 8, tzinfo=UTC),
        )

    assert raised.value is refusal
    assert seen == [db_path.resolve()]


@pytest.mark.parametrize("module", [report, report_export])
def test_report_modules_have_no_raw_production_store_opens(module: object) -> None:
    """Reports have no legitimate raw opens; fixtures seed stores in tests."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    raw_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqliteRunStore"
    ]
    assert raw_calls == []
