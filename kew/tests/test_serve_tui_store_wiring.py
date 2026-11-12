"""Managed-ledger wiring for the serve and TUI production store opens."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import typer

import kew.cli.serve as serve_module
import kew.tui.app as tui_module
from kew.config import DispatchConfig
from kew.ledger_identity import LedgerIdentityError
from kew.run_store import CURRENT_SCHEMA_VERSION


class _FakeStore:
    def close(self) -> None:
        pass


def test_serve_store_factory_uses_managed_identity_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / ".git").mkdir()
    captured: dict[str, object] = {}

    def fake_open_managed_store(**kwargs: object) -> _FakeStore:
        captured.update(kwargs)
        return _FakeStore()

    monkeypatch.setattr(serve_module, "open_managed_store", fake_open_managed_store)
    store = serve_module._open_serve_store(
        project_root=tmp_path / ".",
        db_path=tmp_path / "logs" / ".." / "dispatch.db",
    )

    assert isinstance(store, _FakeStore)
    assert captured == {
        "project_root": tmp_path.resolve(),
        "db_path": (tmp_path / "dispatch.db").resolve(),
        "anchors_path": tmp_path.resolve() / "audit-anchors.jsonl",
        "read_only": False,
        "check_same_thread": True,
    }


def test_tui_store_uses_managed_identity_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    (tmp_path / ".git").mkdir()
    captured: dict[str, object] = {}

    def fake_open_managed_store(**kwargs: object) -> _FakeStore:
        captured.update(kwargs)
        return _FakeStore()

    monkeypatch.setattr(tui_module, "open_managed_store", fake_open_managed_store)
    app = tui_module.KewApp(
        config=DispatchConfig(),
        db_path=str(tmp_path / "logs" / ".." / "dispatch.db"),
        project_root=tmp_path / ".",
    )

    store = app._open_store()

    assert isinstance(store, _FakeStore)
    assert captured == {
        "project_root": tmp_path.resolve(),
        "db_path": (tmp_path / "dispatch.db").resolve(),
        "anchors_path": tmp_path.resolve() / "audit-anchors.jsonl",
        "read_only": False,
        "check_same_thread": False,
    }


def test_serve_schema_probe_is_the_only_documented_raw_production_open() -> None:
    serve_tree = ast.parse(Path(serve_module.__file__).read_text(encoding="utf-8"))
    tui_tree = ast.parse(Path(tui_module.__file__).read_text(encoding="utf-8"))

    serve_raw_calls = [
        node
        for node in ast.walk(serve_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqliteRunStore"
    ]
    assert len(serve_raw_calls) == 1
    assert any(
        keyword.arg == "read_only"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in serve_raw_calls[0].keywords
    )

    tui_raw_calls = [
        node
        for node in ast.walk(tui_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqliteRunStore"
    ]
    assert len(tui_raw_calls) == 1
    assert tui_raw_calls[0].args[0].value == ":memory:"


def test_serve_schema_probe_rejects_future_schema_without_mutating_db(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dispatch.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER)"
        )
        connection.execute(
            "INSERT INTO schema_version VALUES (1, ?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
    before = db_path.read_bytes()

    with pytest.raises(typer.Exit):
        serve_module._serve_check_db_schema(db_path)

    assert db_path.read_bytes() == before
    assert not Path(f"{db_path}-wal").exists()


@pytest.mark.parametrize("surface", ["serve", "tui"])
def test_registry_identity_errors_fail_closed(
    surface: str, tmp_path: Path, monkeypatch: Any
) -> None:
    def refuse(**_kwargs: object) -> _FakeStore:
        raise LedgerIdentityError("WRONG-LEDGER")

    if surface == "serve":
        monkeypatch.setattr(serve_module, "open_managed_store", refuse)
        with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
            serve_module._open_serve_store(
                project_root=tmp_path,
                db_path=tmp_path / "dispatch.db",
            )
    else:
        monkeypatch.setattr(tui_module, "open_managed_store", refuse)
        app = tui_module.KewApp(
            config=DispatchConfig(),
            db_path=str(tmp_path / "dispatch.db"),
            project_root=tmp_path,
        )
        with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
            app._open_store()
