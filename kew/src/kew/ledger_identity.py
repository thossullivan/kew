"""Canonical project identity and machine-local ledger registry primitives."""

from __future__ import annotations

import configparser
import fcntl
import getpass
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.audit_chain import (
    GENESIS_PREV,
    Anchor,
    ChainRow,
    anchor_hash,
    classify_anchors,
    entry_hash,
    parse_anchors,
    verify_anchor_chain,
    verify_chain,
)
from kew.run_store_audit import (
    _CHAIN_SELECT_ALL,
    _row_to_chain_row,
    chained_insert_statement,
)

if TYPE_CHECKING:
    from kew.run_store import SqliteRunStore

_REGISTRY_KEYS = frozenset({"format_version", "ledger_id", "project_root", "db_path"})
_REGISTRY_FORMAT_VERSION = 1


class LedgerIdentityError(RuntimeError):
    """Ledger identity state cannot be interpreted safely."""


class RegistryLockUnavailable(LedgerIdentityError):
    """Another process holds the requested registry lock."""


@dataclass(frozen=True)
class RegistryPaths:
    """Fixed registry files shared by every checkout of one project."""

    registry: Path
    lock: Path


@dataclass(frozen=True)
class RegistryRecord:
    """Validated, frozen on-disk ledger registry record."""

    format_version: int
    ledger_id: str
    project_root: Path
    db_path: Path

    def __post_init__(self) -> None:
        if self.format_version != _REGISTRY_FORMAT_VERSION:
            raise LedgerIdentityError("unsupported registry format version")
        try:
            parsed_id = uuid.UUID(self.ledger_id)
        except (ValueError, AttributeError) as exc:
            raise LedgerIdentityError("registry ledger_id is not a UUID") from exc
        if str(parsed_id) != self.ledger_id:
            raise LedgerIdentityError("registry ledger_id is not canonical")
        _require_canonical_absolute(self.project_root, "project_root")
        _require_canonical_absolute(self.db_path, "db_path")

    @classmethod
    def from_json(cls, encoded: str) -> RegistryRecord:
        """Parse a registry while rejecting missing, extra, and invalid fields."""
        try:
            payload: Any = json.loads(encoded)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LedgerIdentityError("registry is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != _REGISTRY_KEYS:
            raise LedgerIdentityError("registry keys do not match the frozen schema")
        if (
            type(payload["format_version"]) is not int
            or not isinstance(payload["ledger_id"], str)
            or not isinstance(payload["project_root"], str)
            or not isinstance(payload["db_path"], str)
        ):
            raise LedgerIdentityError("registry fields have invalid types")
        return cls(
            format_version=payload["format_version"],
            ledger_id=payload["ledger_id"],
            project_root=Path(payload["project_root"]),
            db_path=Path(payload["db_path"]),
        )

    def to_json(self) -> str:
        """Serialize only the four frozen registry keys."""
        return json.dumps(
            {
                "format_version": self.format_version,
                "ledger_id": self.ledger_id,
                "project_root": str(self.project_root),
                "db_path": str(self.db_path),
            },
            separators=(",", ":"),
        )


def _require_canonical_absolute(path: Path, field: str) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise LedgerIdentityError(f"registry {field} is not canonical and absolute")


def _find_git_entry(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".git"
        if candidate.is_dir() or candidate.is_file():
            return candidate
    return None


def _read_gitdir_file(git_file: Path) -> Path:
    try:
        prefix, value = git_file.read_text().strip().split(":", 1)
    except (OSError, ValueError) as exc:
        raise LedgerIdentityError("worktree .git file is malformed") from exc
    if prefix.strip().lower() != "gitdir" or not value.strip():
        raise LedgerIdentityError("worktree .git file is malformed")
    path = Path(value.strip())
    if not path.is_absolute():
        path = git_file.parent / path
    resolved = path.resolve()
    if not resolved.is_dir():
        raise LedgerIdentityError("worktree git directory does not exist")
    return resolved


def _common_dir(git_entry: Path) -> Path:
    git_dir = (
        git_entry.resolve() if git_entry.is_dir() else _read_gitdir_file(git_entry)
    )
    marker = git_dir / "commondir"
    if not marker.is_file():
        return git_dir
    try:
        common = Path(marker.read_text().strip())
    except OSError as exc:
        raise LedgerIdentityError("git common directory cannot be read") from exc
    if not str(common):
        raise LedgerIdentityError("git common directory is empty")
    if not common.is_absolute():
        common = git_dir / common
    resolved = common.resolve()
    if not resolved.is_dir():
        raise LedgerIdentityError("git common directory does not exist")
    return resolved


def _configured_worktree(common_dir: Path) -> Path:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with (common_dir / "config").open() as config_file:
            parser.read_file(config_file)
        configured = parser.get("core", "worktree", fallback=None)
    except (OSError, configparser.Error) as exc:
        raise LedgerIdentityError(
            "nonstandard git common directory has no usable working tree"
        ) from exc
    if not configured:
        raise LedgerIdentityError(
            "nonstandard git common directory has no configured working tree"
        )
    worktree = Path(configured)
    if not worktree.is_absolute():
        worktree = common_dir / worktree
    resolved = worktree.resolve()
    if not resolved.is_dir():
        raise LedgerIdentityError(
            "nonstandard git common directory working tree does not exist"
        )
    return resolved


def _project_layout(project_root: Path) -> tuple[Path, Path | None]:
    requested = project_root.resolve()
    if not requested.is_dir():
        raise LedgerIdentityError("project root does not exist")
    git_entry = _find_git_entry(requested)
    if git_entry is None:
        return requested, None
    common_dir = _common_dir(git_entry)
    if common_dir.name == ".git":
        return common_dir.parent.resolve(), common_dir
    return _configured_worktree(common_dir), common_dir


def canonical_project_root(project_root: str | Path) -> Path:
    """Return the durable root shared by a primary checkout and its worktrees."""
    root, _ = _project_layout(Path(project_root))
    return root


def registry_paths(project_root: str | Path) -> RegistryPaths:
    """Return the fixed registry and adjacent advisory-lock locations."""
    root, common_dir = _project_layout(Path(project_root))
    state_dir = common_dir / "kew" if common_dir is not None else root / ".kew"
    return RegistryPaths(
        registry=state_dir / "ledger.json",
        lock=state_dir / "ledger.lock",
    )


def ledger_never_created(
    project_root: str | Path, db_path: str | Path, anchors_path: str | Path
) -> bool:
    """True only when the database, the registry and the anchor history are
    all absent: a project no ledger was ever created for (#3081).

    Any one of them surviving means a ledger existed, so a read must go
    through the managed open and its lost-ledger refusal, never read the
    project as new. An unreadable anchors file counts as surviving history.
    """
    if Path(db_path).exists() or registry_paths(project_root).registry.exists():
        return False
    return not _anchor_history_survives(Path(anchors_path))


def lost_ledger_reason(
    project_root: str | Path, db_path: str | Path, anchors_path: str | Path
) -> str | None:
    """The REGISTERED-LEDGER-MISSING reason for a ledger whose database is
    gone while its own registry or its anchor history survives, else None.

    None when anything exists at *db_path*, when nothing survives (a project
    no ledger was ever created for), and when the registry records another
    project or database path: the managed open's own WRONG-LEDGER refusal
    names that state, and a caller must leave it to the open.
    """
    db = Path(db_path).resolve()
    if db.exists():
        return None
    root = canonical_project_root(project_root)
    paths = registry_paths(root)
    registry = _read_registry(paths.registry)
    if registry is not None:
        if registry.project_root != root or registry.db_path != db:
            return None
        return (
            f"REGISTERED-LEDGER-MISSING: no ledger at {db}, but its registry"
            f" survives at {paths.registry}; restore the ledger before deciding"
        )
    anchors = Path(anchors_path)
    if not _anchor_history_survives(anchors):
        return None
    return (
        f"REGISTERED-LEDGER-MISSING: no ledger at {db}, but the audit anchors"
        f" file {anchors} is not empty or cannot be read; restore the ledger"
        " before deciding"
    )


def _anchor_history_survives(anchors: Path) -> bool:
    """True when the anchors file holds history, or cannot be read to prove
    it holds none: an unreadable file is never taken for an empty one."""
    if not anchors.exists():
        return False
    try:
        return bool(anchors.read_text().strip())
    except (OSError, UnicodeError):
        return True


@contextmanager
def advisory_registry_lock(lock_path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold an exclusive POSIX advisory lock for the context lifetime."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(lock_file.fileno(), operation)
        except BlockingIOError as exc:
            raise RegistryLockUnavailable(
                f"ledger registry lock is held: {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_registry(target: Path, record: RegistryRecord) -> None:
    """Durably replace a registry using file fsync, rename, and directory fsync."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as registry_file:
            registry_file.write(record.to_json())
            registry_file.write("\n")
            registry_file.flush()
            os.fsync(registry_file.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class _DatabaseIdentity:
    ledger_id: str
    project_root: Path


@dataclass(frozen=True)
class LedgerRebindResult:
    """The identity and path selected by an explicit recovery decision."""

    ledger_id: str
    db_path: Path


def _read_anchors(anchors_path: Path) -> list[Any]:
    if not anchors_path.exists():
        return []
    try:
        anchors = parse_anchors(anchors_path.read_text())
    except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
        raise LedgerIdentityError("INVALID-ANCHOR-HISTORY") from exc
    if not verify_anchor_chain(anchors).ok:
        raise LedgerIdentityError("INVALID-ANCHOR-HISTORY")
    return anchors


def _read_registry(registry_path: Path) -> RegistryRecord | None:
    if not registry_path.exists():
        return None
    try:
        return RegistryRecord.from_json(registry_path.read_text())
    except OSError as exc:
        raise LedgerIdentityError("ledger registry cannot be read") from exc


def _read_database_identity(db_path: Path) -> _DatabaseIdentity | None:
    if not db_path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'ledger_identity'"
            ).fetchone()
            if table is None:
                return None
            rows = connection.execute(
                "SELECT ledger_id, project_root FROM ledger_identity"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LedgerIdentityError("ledger database cannot be inspected") from exc
    if len(rows) != 1:
        raise LedgerIdentityError("WRONG-LEDGER: invalid identity singleton")
    ledger_id = rows[0]["ledger_id"]
    project_root = Path(rows[0]["project_root"])
    try:
        parsed_id = uuid.UUID(ledger_id)
    except (ValueError, AttributeError) as exc:
        raise LedgerIdentityError("WRONG-LEDGER: invalid database UUID") from exc
    if str(parsed_id) != ledger_id:
        raise LedgerIdentityError("WRONG-LEDGER: noncanonical database UUID")
    try:
        _require_canonical_absolute(project_root, "project_root")
    except LedgerIdentityError as exc:
        raise LedgerIdentityError(
            "WRONG-LEDGER: invalid database project root"
        ) from exc
    return _DatabaseIdentity(ledger_id=ledger_id, project_root=project_root)


def _validate_confirmation(
    expected_ledger_id: str | None, expected_anchor_hash: str | None
) -> None:
    if (expected_ledger_id is None) == (expected_anchor_hash is None):
        raise LedgerIdentityError("exactly one full confirmation is required")
    if expected_ledger_id is not None:
        try:
            parsed = uuid.UUID(expected_ledger_id)
        except (ValueError, AttributeError) as exc:
            raise LedgerIdentityError(
                "expected ledger ID must be a canonical full UUID"
            ) from exc
        if str(parsed) != expected_ledger_id:
            raise LedgerIdentityError(
                "expected ledger ID must be a canonical full UUID"
            )
    if expected_anchor_hash is not None and (
        len(expected_anchor_hash) != 64
        or any(
            character not in "0123456789abcdef" for character in expected_anchor_hash
        )
    ):
        raise LedgerIdentityError(
            "expected anchor hash must be a full lowercase SHA-256 hash"
        )


def _validate_identity_binding(
    store: SqliteRunStore, identity: _DatabaseIdentity, project_root: Path
) -> None:
    if identity.project_root != project_root:
        raise LedgerIdentityError("WRONG-LEDGER: database project mismatch")
    rows = store._conn.execute(
        "SELECT binding_event_id FROM ledger_identity WHERE id = 1"
    ).fetchall()
    bindings = [
        row for row in store.read_chain_rows() if row.event == "ledger_identity_bound"
    ]
    if (
        len(rows) != 1
        or len(bindings) != 1
        or rows[0]["binding_event_id"] != bindings[0].id
    ):
        raise LedgerIdentityError("WRONG-LEDGER: invalid identity binding event")
    expected_payload = json.dumps(
        {
            "format_version": 1,
            "ledger_id": identity.ledger_id,
            "project_root": str(project_root),
        },
        separators=(",", ":"),
    )
    if bindings[0].payload != expected_payload:
        raise LedgerIdentityError("WRONG-LEDGER: invalid identity binding payload")


def _validate_database_anchors(
    store: SqliteRunStore, anchors: list[Any], ledger_id: str | None
) -> None:
    _validate_rows_anchors(store.read_chain_rows(), anchors, ledger_id)


def _validate_rows_anchors(
    rows: list[ChainRow], anchors: list[Any], ledger_id: str | None
) -> None:
    if not verify_chain(rows).ok:
        raise LedgerIdentityError("WRONG-LEDGER: audit chain is not intact")
    if not anchors:
        return
    report = classify_anchors(rows, anchors, staleness_window=0, ledger_id=ledger_id)
    if (
        report.wrong_ledger
        or report.mismatched
        or report.absent
        or report.lagging
        or report.count_shrink
        or report.matched != len(anchors)
    ):
        raise LedgerIdentityError("WRONG-LEDGER: database/anchor mismatch")


def _legacy_decision_values(
    *, target: Path, old_path: Path | None, expected_anchor_hash: str, reason: str
) -> tuple[Any, ...]:
    payload = json.dumps(
        {
            "old_path": None if old_path is None else str(old_path),
            "new_path": str(target),
            "ledger_id": None,
            "expected_anchor_hash": expected_anchor_hash,
            "reason": reason,
        },
        separators=(",", ":"),
    )
    return (
        datetime.now(UTC).isoformat(),
        "ledger_registry_rebound",
        None,
        None,
        None,
        "human",
        getpass.getuser() or "unknown",
        None,
        None,
        payload,
    )


def _validate_and_record_legacy_decision(
    *,
    target: Path,
    anchors: list[Any],
    old_path: Path | None,
    expected_anchor_hash: str,
    reason: str,
) -> None:
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        raw_rows = connection.execute(_CHAIN_SELECT_ALL).fetchall()
        rows = [_row_to_chain_row(row) for row in raw_rows]
        _validate_rows_anchors(rows, anchors, None)
        prev_hash = entry_hash(rows[-1]) if rows else GENESIS_PREV
        values = _legacy_decision_values(
            target=target,
            old_path=old_path,
            expected_anchor_hash=expected_anchor_hash,
            reason=reason,
        )
        sql, params = chained_insert_statement(
            connection, (*values, None), prev_hash=prev_hash
        )
        connection.execute(sql, params)  # nosemgrep
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _append_rebind_decision(
    store: SqliteRunStore,
    *,
    old_path: Path | None,
    new_path: Path,
    ledger_id: str | None,
    expected_anchor_hash: str | None,
    reason: str,
) -> None:
    payload = json.dumps(
        {
            "old_path": None if old_path is None else str(old_path),
            "new_path": str(new_path),
            "ledger_id": ledger_id,
            "expected_anchor_hash": expected_anchor_hash,
            "reason": reason,
        },
        separators=(",", ":"),
    )
    timestamp = datetime.now(UTC).isoformat()
    with store._txn_lock:
        try:
            store._conn.execute("BEGIN IMMEDIATE")
            store._chained_insert_in_transaction(
                (
                    timestamp,
                    "ledger_registry_rebound",
                    None,
                    None,
                    None,
                    "human",
                    getpass.getuser() or "unknown",
                    None,
                    None,
                    payload,
                    None,
                )
            )
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise


def _recover_identity_ledger(
    *,
    target: Path,
    project_root: Path,
    anchors: list[Any],
    identity: _DatabaseIdentity | None,
    expected_ledger_id: str,
    old_path: Path | None,
    reason: str,
) -> str:
    from kew.run_store import SqliteRunStore

    if identity is None or identity.ledger_id != expected_ledger_id:
        raise LedgerIdentityError("WRONG-LEDGER: expected ledger ID mismatch")
    with SqliteRunStore(str(target), project_root=project_root) as store:
        _validate_identity_binding(store, identity, project_root)
        _validate_database_anchors(store, anchors, identity.ledger_id)
        _append_rebind_decision(
            store,
            old_path=old_path,
            new_path=target,
            ledger_id=identity.ledger_id,
            expected_anchor_hash=None,
            reason=reason,
        )
    return identity.ledger_id


def _adopt_legacy_ledger(
    *,
    target: Path,
    project_root: Path,
    anchors: list[Any],
    identity: _DatabaseIdentity | None,
    expected_anchor_hash: str,
    old_path: Path | None,
    reason: str,
) -> str:
    from kew.run_store import SqliteRunStore

    if identity is not None:
        raise LedgerIdentityError(
            "WRONG-LEDGER: anchor confirmation is only for a legacy ledger"
        )
    if not anchors or any(anchor.version == 2 for anchor in anchors):
        raise LedgerIdentityError("legacy adoption requires legacy anchors")
    if anchor_hash(anchors[-1]) != expected_anchor_hash:
        raise LedgerIdentityError("WRONG-LEDGER: expected anchor hash mismatch")
    _validate_and_record_legacy_decision(
        target=target,
        anchors=anchors,
        old_path=old_path,
        expected_anchor_hash=expected_anchor_hash,
        reason=reason,
    )
    with SqliteRunStore(str(target), project_root=project_root) as migrated:
        migrated_id = migrated.read_ledger_id()
    if migrated_id is None:
        raise LedgerIdentityError("legacy identity migration failed")
    return migrated_id


def ledger_rebind(
    *,
    project_root: str | Path,
    target: str | Path,
    anchors_path: str | Path,
    expected_ledger_id: str | None = None,
    expected_anchor_hash: str | None = None,
    reason: str,
) -> LedgerRebindResult:
    """Explicitly recover an identity ledger or adopt one verified legacy ledger."""
    _validate_confirmation(expected_ledger_id, expected_anchor_hash)
    if not reason.strip():
        raise LedgerIdentityError("reason must be nonempty")
    normalized_root = canonical_project_root(project_root)
    normalized_target = Path(target).resolve()
    paths = registry_paths(normalized_root)
    with advisory_registry_lock(paths.lock):
        registry = _read_registry(paths.registry)
        anchors = _read_anchors(Path(anchors_path))
        identity = _read_database_identity(normalized_target)
        if not normalized_target.is_file():
            raise LedgerIdentityError("target ledger database does not exist")
        old_path = None if registry is None else registry.db_path

        if expected_ledger_id is not None:
            selected_id = _recover_identity_ledger(
                target=normalized_target,
                project_root=normalized_root,
                anchors=anchors,
                identity=identity,
                expected_ledger_id=expected_ledger_id,
                old_path=old_path,
                reason=reason.strip(),
            )
        else:
            assert expected_anchor_hash is not None
            selected_id = _adopt_legacy_ledger(
                target=normalized_target,
                project_root=normalized_root,
                anchors=anchors,
                identity=identity,
                expected_anchor_hash=expected_anchor_hash,
                old_path=old_path,
                reason=reason.strip(),
            )

        atomic_write_registry(
            paths.registry,
            RegistryRecord(
                format_version=_REGISTRY_FORMAT_VERSION,
                ledger_id=selected_id,
                project_root=normalized_root,
                db_path=normalized_target,
            ),
        )
        return LedgerRebindResult(ledger_id=selected_id, db_path=normalized_target)


def _registered_open_checks(
    *,
    registry: RegistryRecord,
    project_root: Path,
    db_path: Path,
    identity: _DatabaseIdentity | None,
) -> None:
    if registry.project_root != project_root:
        raise LedgerIdentityError("WRONG-LEDGER: registry project mismatch")
    if registry.db_path != db_path:
        detail = (
            f"; registered path exists at {registry.db_path}"
            if registry.db_path.exists()
            else ""
        )
        raise LedgerIdentityError(f"WRONG-LEDGER: configured path mismatch{detail}")
    if not db_path.exists():
        raise LedgerIdentityError(f"REGISTERED-LEDGER-MISSING: {registry.db_path}")
    if (
        identity is None
        or identity.ledger_id != registry.ledger_id
        or identity.project_root != registry.project_root
    ):
        raise LedgerIdentityError("WRONG-LEDGER: registry/database mismatch")


def _require_registry_for_anchors(
    registry: RegistryRecord | None, anchors: list[Anchor]
) -> None:
    if registry is not None or not anchors:
        return
    ledger_ids = {
        anchor.ledger_id
        for anchor in anchors
        if anchor.version == 2 and anchor.ledger_id is not None
    }
    detail = f": {next(iter(ledger_ids))}" if len(ledger_ids) == 1 else ""
    raise LedgerIdentityError(f"REGISTERED-LEDGER-MISSING{detail}")


def _open_registered_store(
    *,
    registry: RegistryRecord,
    anchors: list[Anchor],
    project_root: Path,
    db_path: Path,
    identity: _DatabaseIdentity | None,
    read_only: bool,
    check_same_thread: bool,
) -> SqliteRunStore:
    from kew.run_store import SqliteRunStore

    anchor_ids = {
        anchor.ledger_id
        for anchor in anchors
        if anchor.version == 2 and anchor.ledger_id is not None
    }
    if anchor_ids and anchor_ids != {registry.ledger_id}:
        raise LedgerIdentityError("WRONG-LEDGER: anchor identity mismatch")
    _registered_open_checks(
        registry=registry,
        project_root=project_root,
        db_path=db_path,
        identity=identity,
    )
    return SqliteRunStore(
        str(db_path),
        project_root=project_root,
        read_only=read_only,
        check_same_thread=check_same_thread,
    )


def _create_and_register_store(
    *,
    paths: RegistryPaths,
    project_root: Path,
    db_path: Path,
    check_same_thread: bool,
) -> SqliteRunStore:
    from kew.run_store import SqliteRunStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteRunStore(
        str(db_path),
        project_root=project_root,
        check_same_thread=check_same_thread,
    )
    try:
        bound = _read_database_identity(db_path)
        if bound is None or bound.project_root != project_root:
            raise LedgerIdentityError("WRONG-LEDGER: identity binding failed")
        atomic_write_registry(
            paths.registry,
            RegistryRecord(
                format_version=_REGISTRY_FORMAT_VERSION,
                ledger_id=bound.ledger_id,
                project_root=project_root,
                db_path=db_path,
            ),
        )
    except Exception:
        store.close()
        raise
    return store


def open_managed_store(
    *,
    project_root: str | Path,
    db_path: str | Path,
    anchors_path: str | Path,
    read_only: bool = False,
    check_same_thread: bool = True,
    require_registered: bool = False,
) -> SqliteRunStore:
    """Open the one registered ledger after lock-scoped disk classification.

    The registry, anchors, and database identity are deliberately read only
    after the shared registry lock is held.  The lock remains held through any
    schema migration and the durable registry rename.
    """
    normalized_root = canonical_project_root(project_root)
    normalized_db = Path(db_path).resolve()
    paths = registry_paths(normalized_root)
    with advisory_registry_lock(paths.lock):
        anchors = _read_anchors(Path(anchors_path))
        registry = _read_registry(paths.registry)
        _require_registry_for_anchors(registry, anchors)
        if require_registered and registry is None:
            raise LedgerIdentityError("REGISTERED-LEDGER-MISSING: operator proof open")

        identity = _read_database_identity(normalized_db)
        if registry is not None:
            return _open_registered_store(
                registry=registry,
                anchors=anchors,
                project_root=normalized_root,
                db_path=normalized_db,
                identity=identity,
                read_only=read_only,
                check_same_thread=check_same_thread,
            )

        if identity is not None:
            raise LedgerIdentityError("REGISTRY-MISSING-FOR-IDENTITY")
        if read_only:
            raise LedgerIdentityError("REGISTERED-LEDGER-MISSING: read-only open")

        return _create_and_register_store(
            paths=paths,
            project_root=normalized_root,
            db_path=normalized_db,
            check_same_thread=check_same_thread,
        )
