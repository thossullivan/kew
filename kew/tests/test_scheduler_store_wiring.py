"""Managed-ledger architecture and argument wiring for scheduler opens."""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kew import scheduler
from kew.config import DispatchConfig
from kew.ledger_identity import LedgerIdentityError


def test_scheduler_opener_passes_canonical_identity_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "relative" / ".." / "logs")
    project = tmp_path / "project"
    canonical = tmp_path / "canonical-project"
    sentinel = MagicMock()
    managed_open = MagicMock(return_value=sentinel)
    monkeypatch.setattr(scheduler, "project_root", lambda: project)
    monkeypatch.setattr(scheduler, "canonical_project_root", lambda root: canonical)
    monkeypatch.setattr(scheduler, "open_managed_store", managed_open)

    assert scheduler._open_scheduler_store(config) is sentinel
    managed_open.assert_called_once_with(
        project_root=canonical,
        db_path=(tmp_path / "logs" / "dispatch.db").resolve(),
        anchors_path=canonical / "audit-anchors.jsonl",
        read_only=False,
        check_same_thread=True,
    )


def test_scheduler_opener_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DispatchConfig()
    refusal = LedgerIdentityError("WRONG-LEDGER")
    monkeypatch.setattr(scheduler, "open_managed_store", MagicMock(side_effect=refusal))

    with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
        scheduler._open_scheduler_store(config)


def test_scheduler_has_no_raw_production_store_opens() -> None:
    tree = ast.parse(Path(scheduler.__file__).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    raw_opens = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "SqliteRunStore"
    ]
    managed_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_open_scheduler_store"
    ]

    assert raw_opens == []
    assert len(managed_calls) == 7
