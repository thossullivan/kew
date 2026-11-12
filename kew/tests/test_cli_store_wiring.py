"""Architecture and argument wiring for the shared CLI ledger boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import kew.cli as cli
from kew.cli import app
from kew.ledger_identity import LedgerIdentityError
from kew.run_store import RunStore

runner = CliRunner()


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_open_run_store_routes_canonical_identity_paths_to_managed_opener(
    tmp_path: Path, monkeypatch: Any
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    nested = project / "nested"
    nested.mkdir()
    db_path = nested / ".." / "logs" / "dispatch.db"
    captured: dict[str, object] = {}
    store = _FakeStore()

    def fake_open_managed_store(**kwargs: object) -> _FakeStore:
        captured.update(kwargs)
        return store

    monkeypatch.setattr(cli, "project_root", lambda: project)
    monkeypatch.setattr(cli, "open_managed_store", fake_open_managed_store)

    with cli._open_run_store(db_path) as opened:
        assert opened is store

    assert captured == {
        "project_root": project.resolve(),
        "db_path": (project / "logs" / "dispatch.db").resolve(),
        "anchors_path": project.resolve() / "audit-anchors.jsonl",
    }
    assert store.closed


def test_open_run_store_forwards_read_only_mode(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, object] = {}
    store = _FakeStore()

    def fake_open_managed_store(**kwargs: object) -> _FakeStore:
        captured.update(kwargs)
        return store

    monkeypatch.setattr(cli, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "open_managed_store", fake_open_managed_store)

    with cli._open_run_store(tmp_path / "logs" / "dispatch.db", read_only=True):
        pass

    assert captured["read_only"] is True
    assert store.closed


def test_open_run_store_reports_identity_refusal_without_traceback(
    tmp_path: Path, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "open_managed_store",
        lambda **_kwargs: (_ for _ in ()).throw(
            LedgerIdentityError("WRONG-LEDGER: configured path mismatch")
        ),
    )

    with (
        pytest.raises(typer.Exit) as raised,
        cli._open_run_store(tmp_path / "other.db"),
    ):
        pass

    assert raised.value.exit_code == 2
    captured = capsys.readouterr()
    assert "WRONG-LEDGER: configured path mismatch" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.policy
def test_cli_modules_using_shared_store_boundary_do_not_open_sqlite_directly() -> None:
    cli_dir = Path(cli.__file__).parent
    shared_boundary_users: set[Path] = set()

    for path in cli_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "_open_run_store")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_open_run_store"
                )
            )
        ]
        if calls:
            shared_boundary_users.add(path)
            assert not any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "SqliteRunStore"
                    )
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "SqliteRunStore"
                    )
                )
                for node in ast.walk(tree)
            ), f"{path} bypasses the shared managed-store boundary"

    assert {path.name for path in shared_boundary_users} == {
        "__init__.py",
        "config.py",
        "delegate.py",
        "drive.py",
        "epic.py",
        "gc.py",
        "hosts.py",
        "ingest.py",
        "loop.py",
        "merge.py",
        "no_change.py",
        "plan.py",
        "prompts.py",
        "publication.py",
        "quota.py",
        "rename_repo.py",
        "run_request.py",
        "triage.py",
        "verify_health.py",
    }


def _patch_preflight(monkeypatch: Any) -> None:
    monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)


def _cfg_with_log_dir(tmp_path: Path) -> Any:
    from kew.config import DispatchConfig

    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


class TestReviewPassesStore:
    def test_review_opens_store_and_passes_to_dispatch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        _patch_preflight(monkeypatch)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
        monkeypatch.setattr(
            "kew.cli.load_config", lambda *a, **k: _cfg_with_log_dir(tmp_path)
        )
        captured: dict[str, object] = {}

        def fake_dispatch_review(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(kind="executed", exit_code=0)

        monkeypatch.setattr("kew.cli.dispatch_review", fake_dispatch_review)

        result = runner.invoke(app, ["review", "42"])
        assert result.exit_code == 0, result.output
        assert isinstance(captured["store"], RunStore)

    def test_review_dry_run_still_skips_store(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """--dry-run early-returns inside dispatch_review; store is still opened."""
        _patch_preflight(monkeypatch)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
        monkeypatch.setattr(
            "kew.cli.load_config", lambda *a, **k: _cfg_with_log_dir(tmp_path)
        )
        captured: dict[str, object] = {}

        def fake_dispatch_review(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(kind="executed", exit_code=0)

        monkeypatch.setattr("kew.cli.dispatch_review", fake_dispatch_review)

        result = runner.invoke(app, ["review", "42", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert captured["store"] is not None


class TestResolvePassesStore:
    def test_resolve_opens_store_and_passes_to_dispatch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        _patch_preflight(monkeypatch)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
        monkeypatch.setattr(
            "kew.cli.load_config", lambda *a, **k: _cfg_with_log_dir(tmp_path)
        )
        captured: dict[str, object] = {}

        def fake_dispatch_resolve(**kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(kind="executed", exit_code=0)

        monkeypatch.setattr(
            "kew.cli._resolve_module.dispatch_resolve", fake_dispatch_resolve
        )

        result = runner.invoke(app, ["resolve", "42"])
        assert result.exit_code == 0, result.output
        assert isinstance(captured["store"], RunStore)


class TestCiFixPassesStore:
    def test_ci_fix_opens_store_and_passes_to_dispatch(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        _patch_preflight(monkeypatch)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
        monkeypatch.setattr(
            "kew.cli.load_config", lambda *a, **k: _cfg_with_log_dir(tmp_path)
        )
        captured: dict[str, object] = {}

        def fake_dispatch_ci_fix(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr("kew.cli.dispatch_ci_fix", fake_dispatch_ci_fix)

        result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 0, result.output
        assert isinstance(captured["store"], RunStore)
