"""Guard the intentionally narrow raw production SQLite open boundaries."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import kew.ci_fix as ci_fix
from kew.config import DispatchConfig
from kew.ledger_identity import LedgerIdentityError

_SRC_ROOT = Path(__file__).parents[1] / "src" / "kew"

# These calls deliberately bypass the managed writable-ledger boundary. Each is
# non-mutating, backup/restore mechanics, legacy migration, or test-only memory.
_ALLOWED_RAW_OPENS = {
    ("backup.py", "_bare_conn", "sqlite3.connect"),
    ("backup.py", "_snapshot_db", "sqlite3.connect"),
    ("backup.py", "_assert_idle", "sqlite3.connect"),
    ("backup.py", "restore_backup", "SqliteRunStore"),
    ("cli/doctor.py", "_check_degraded_review_lane", "SqliteRunStore"),
    # Read-only, like its two siblings here: `kew doctor` reports on a ledger,
    # it never migrates one and never refuses over identity. The
    # route-live-confirm subcommand WRITES spend and therefore goes through
    # open_managed_store instead (#1516).
    ("cli/doctor.py", "_check_route_live", "SqliteRunStore"),
    ("cli/doctor.py", "_check_pinned_reservations", "SqliteRunStore"),
    # Read-only too: the runner-quota check reads the lane holds a quota
    # rejection left in meta, and must never migrate the ledger (#2643).
    ("cli/doctor.py", "_read_runner_quota_holds", "SqliteRunStore"),
    # Read-only for the same reason, and opened for a read the containment
    # check cannot do without: the OpenShell egress gate reads its
    # certification off a ledger, so a storeless resolution reported every
    # certified boundary as uncertified (#2389 review).
    ("cli/doctor.py", "_open_certification_ledger", "SqliteRunStore"),
    ("cli/serve.py", "_serve_check_db_schema", "SqliteRunStore"),
    # docs/operations.md: file: URI with mode=ro, snapshot isolation, and
    # never the writer's connection. This reader cannot migrate the ledger.
    ("run_store_committed.py", "_committed_connection", "sqlite3.connect"),
    # Same mode=ro URI as the committed reader: read_snapshot() holds its own
    # read-only connection so its BEGIN and ROLLBACK never touch the writer
    # another thread may be inside (#1958). It cannot migrate the ledger.
    ("run_store_committed.py", "_open_snapshot_connection", "sqlite3.connect"),
    ("staleness.py", "_read_db_schema_version", "sqlite3.connect"),
    ("tui/app.py", "_open_store", "SqliteRunStore"),
}


def _raw_open_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name) and node.func.id == "SqliteRunStore":
        return "SqliteRunStore"
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
    ):
        return "sqlite3.connect"
    return None


def _raw_open_owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    owner: ast.AST | None = node
    while owner is not None and not isinstance(
        owner, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        owner = parents.get(owner)
    assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
    return owner.name


def _raw_production_opens() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(_SRC_ROOT).as_posix()
        if relative in {"ledger_identity.py", "run_store.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _raw_open_name(node)
            if call is None:
                continue
            found.add((relative, _raw_open_owner(node, parents), call))
    return found


@pytest.mark.policy
def test_raw_production_store_opens_match_documented_allowlist() -> None:
    assert _raw_production_opens() == _ALLOWED_RAW_OPENS


class _FakeStore:
    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs" / ".." / "ledger")
    return config


def test_ci_fix_fallback_uses_exact_managed_open_tuple(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, object] = {}

    def managed_open(**kwargs: object) -> _FakeStore:
        captured.update(kwargs)
        return _FakeStore()

    project = tmp_path / "project"
    project.mkdir()
    # TMPDIR can live inside a linked worktree. Give this project its own root.
    (project / ".git").mkdir()
    monkeypatch.setattr(ci_fix, "project_root", lambda: project / ".")
    monkeypatch.setattr(ci_fix, "open_managed_store", managed_open)
    monkeypatch.setattr(ci_fix, "emit", lambda *_args, **_kwargs: None)

    ci_fix._emit_ci_fix_event(
        _config(tmp_path),
        action_run_id="123",
        pr_number=7,
        issue_number=42,
        attempt=1,
        exit_code=0,
    )

    root = project.resolve()
    assert captured == {
        "project_root": root,
        "db_path": (tmp_path / "ledger" / "dispatch.db").resolve(),
        "anchors_path": root / "audit-anchors.jsonl",
        "read_only": False,
        "check_same_thread": True,
    }


def test_ci_fix_identity_refusal_fails_closed(tmp_path: Path, monkeypatch: Any) -> None:
    def refuse(**_kwargs: object) -> _FakeStore:
        raise LedgerIdentityError("WRONG-LEDGER")

    monkeypatch.setattr(ci_fix, "project_root", lambda: tmp_path)
    monkeypatch.setattr(ci_fix, "open_managed_store", refuse)

    with pytest.raises(LedgerIdentityError, match="WRONG-LEDGER"):
        ci_fix._emit_ci_fix_event(
            _config(tmp_path),
            action_run_id="123",
            pr_number=7,
            issue_number=42,
            attempt=1,
            exit_code=0,
        )
