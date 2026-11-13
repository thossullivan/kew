"""Backup of the kew durable record — the one-file dispatch.db + config (#267).

The snapshot is taken with SQLite's online-backup API over a bare, NON-migrating
connection, so backing up never forward-migrates or write-locks the live DB.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from kew import __version__, work_dirs
from kew.audit_chain import ChainRow, verify_chain
from kew.ledger_identity import (
    LedgerIdentityError,
    RegistryRecord,
    _read_anchors,
    _validate_rows_anchors,
    advisory_registry_lock,
    canonical_project_root,
    registry_paths,
)
from kew.run_store import (
    _CHAIN_SELECT_ALL,
    CURRENT_SCHEMA_VERSION,
    SchemaTooNewError,
    SqliteRunStore,
    _row_to_chain_row,
)


class BackupError(RuntimeError):
    """A backup or restore operation could not be completed safely."""


class RestorePrerequisiteError(BackupError):
    """Restore cannot start because its input or target is unavailable."""


@dataclass
class BackupManifest:
    kew_version: str
    schema_version: int
    created_at: str
    chain_head_hash: str | None
    chain_count: int
    chain_ok: bool
    runs: int
    audit_events: int
    loops: int
    policy_snapshots: int
    config_included: bool
    included_logs: bool
    interactive_usage: int = 0  # legacy trailing default; keep its position stable
    ledger_id: str | None = None
    project_root: str | None = None
    db_path: str | None = None


def _bare_conn(db_path: Path) -> sqlite3.Connection:
    """A plain read connection — NEVER SqliteRunStore, which would _migrate."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _build_manifest(
    db_path: Path,
    config_path: Path | None,
    include_logs: bool,
    *,
    source_db_path: Path,
) -> BackupManifest:
    conn = _bare_conn(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        schema_version = int(row["version"]) if row else 0
        chain = verify_chain(
            [_row_to_chain_row(r) for r in conn.execute(_CHAIN_SELECT_ALL).fetchall()]
        )
        runs = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        audit_events = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events"
        ).fetchone()["n"]
        loops = conn.execute("SELECT COUNT(*) AS n FROM loops").fetchone()["n"]
        snapshots = conn.execute(
            "SELECT COUNT(*) AS n FROM policy_snapshots"
        ).fetchone()["n"]
        has_interactive = conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type = 'table' AND name = 'interactive_usage'"
        ).fetchone()
        interactive_usage = (
            conn.execute("SELECT COUNT(*) AS n FROM interactive_usage").fetchone()["n"]
            if has_interactive
            else 0  # pre-v15 snapshot: table absent (bare, non-migrating conn)
        )
        identity = _read_db_identity(conn)
    finally:
        conn.close()
    config_included = config_path is not None and Path(config_path).exists()
    return BackupManifest(
        kew_version=__version__,
        schema_version=schema_version,
        created_at=datetime.now(UTC).isoformat(),
        chain_head_hash=chain.head_hash,
        chain_count=chain.count,
        chain_ok=chain.ok,
        runs=runs,
        audit_events=audit_events,
        loops=loops,
        policy_snapshots=snapshots,
        config_included=config_included,
        included_logs=include_logs,
        interactive_usage=interactive_usage,
        ledger_id=None if identity is None else identity[0],
        project_root=None if identity is None else str(identity[1]),
        db_path=None if identity is None else str(source_db_path.resolve()),
    )


def _read_db_identity(conn: sqlite3.Connection) -> tuple[str, Path] | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ledger_identity'"
    ).fetchone()
    if table is None:
        return None
    rows = conn.execute(
        "SELECT ledger_id, project_root, binding_event_id FROM ledger_identity"
    ).fetchall()
    if len(rows) != 1:
        raise BackupError("ledger identity is malformed; use explicit ledger-rebind")
    ledger_id = rows[0]["ledger_id"]
    project_root = Path(rows[0]["project_root"])
    try:
        parsed = uuid.UUID(ledger_id)
    except (ValueError, AttributeError) as exc:
        raise BackupError(
            "ledger identity is malformed; use explicit ledger-rebind"
        ) from exc
    if (
        str(parsed) != ledger_id
        or not project_root.is_absolute()
        or project_root.resolve() != project_root
    ):
        raise BackupError("ledger identity is malformed; use explicit ledger-rebind")
    bindings = conn.execute(
        "SELECT id, payload FROM audit_events"
        " WHERE event = 'ledger_identity_bound' ORDER BY id"
    ).fetchall()
    expected_payload = json.dumps(
        {
            "format_version": 1,
            "ledger_id": ledger_id,
            "project_root": str(project_root),
        },
        separators=(",", ":"),
    )
    if (
        len(bindings) != 1
        or rows[0]["binding_event_id"] != bindings[0]["id"]
        or bindings[0]["payload"] != expected_payload
    ):
        raise BackupError(
            "ledger identity binding is malformed; use explicit ledger-rebind"
        )
    return ledger_id, project_root


def _snapshot_db(src_path: Path, dest_path: Path) -> None:
    src = _bare_conn(src_path)
    try:
        dest: sqlite3.Connection | None = None
        try:
            dest = sqlite3.connect(str(dest_path))
            src.backup(dest)
        finally:
            if dest is not None:
                dest.close()
    finally:
        src.close()


#: What ``--include-logs`` stages: the SANCTIONED evidence set only (I06T).
#:
#: Raw dispatch transcripts (``dispatch-*.log``, ``sandbox-*.log``) and typed
#: event files (``*.events.jsonl``) are deliberately absent. A backup is a copy
#: that outlives the machine it was taken on — it gets mailed to support,
#: dropped in object storage, restored onto a laptop — so anything a provider
#: echoed into a transcript would outlive the run by exactly as long. Post-I06T
#: those transcripts hold nothing a restore needs: every fact a receipt is
#: assembled from lives in ``dispatch.db`` and in the derived-evidence sidecars
#: staged here. Pre-I06T logs are history, and history is not restored.
#:
#: The protected prompt bytes under ``.kew/private/prompts`` are excluded by
#: construction — they DO live under the log directory, but the staged
#: patterns above are non-recursive top-level globs that never descend into
#: that subtree, and ``*.prompt`` is on the exclusion list below as a second
#: guard (:mod:`kew.sensitive_prompt`).
STAGED_LOG_PATTERNS = ("dispatch-*.evidence.json", "sandbox-*.evidence.json")

#: Patterns that must NEVER be staged, whatever a future pattern list says.
#: Belt and braces: a glob widened by accident is a transcript leak, so the
#: copy loop re-checks each candidate against this set before it copies.
EXCLUDED_LOG_PATTERNS = ("*.log", "*.events.jsonl", "*.prompt")


def _is_excluded_from_backup(path: Path) -> bool:
    """True when *path* is a raw transcript/event/prompt file, never staged."""
    return any(path.match(pattern) for pattern in EXCLUDED_LOG_PATTERNS)


def _stage_logs(log_dir: Path, dest: Path) -> None:
    """Stage the sanctioned evidence set from *log_dir* — never raw output."""
    dest.mkdir(parents=True, exist_ok=True)
    for pattern in STAGED_LOG_PATTERNS:
        for f in log_dir.glob(pattern):
            if _is_excluded_from_backup(f):
                continue
            shutil.copy2(f, dest / f.name)


def _resolve_out_path(out_path: Path, created_at: str) -> Path:
    out_path = Path(out_path)
    if out_path.name.endswith(".tar.gz"):
        return out_path
    stamp = created_at.replace(":", "").replace("-", "").split(".")[0]
    return out_path / f"kew-backup-{stamp}.tar.gz"


def _archive(staging: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".{final.name}.tmp-{os.getpid()}"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for member in sorted(staging.iterdir()):
                tar.add(member, arcname=member.name)
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)


def create_backup(
    db_path: Path | str,
    config_path: Path | str | None,
    out_path: Path | str,
    *,
    include_logs: bool = False,
) -> BackupManifest:
    """Snapshot dispatch.db (+ config) into a single kew-backup-<ts>.tar.gz."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise BackupError(f"no database at {db_path}")
    cfg = Path(config_path) if config_path is not None else None
    with work_dirs.work_tempdir("kew-backup-") as staging_str:
        staging = Path(staging_str)
        snapshot = staging / "dispatch.db"
        # Snapshot FIRST, then derive the manifest from the staged snapshot, so
        # manifest.json describes exactly the archived db (a write landing between
        # two separate reads of the live db can't desync them).
        _snapshot_db(db_path, snapshot)
        manifest = _build_manifest(snapshot, cfg, include_logs, source_db_path=db_path)
        final = _resolve_out_path(Path(out_path), manifest.created_at)
        (staging / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2))
        if manifest.config_included and cfg is not None:
            shutil.copy2(cfg, staging / "kew.toml")
        if include_logs:
            _stage_logs(db_path.parent, staging / "logs")
        _archive(staging, final)
    return manifest


@dataclass
class RestoreResult:
    manifest: BackupManifest
    moved_old_to: Path | None
    config_moved_to: Path | None


_KNOWN_MEMBERS = {"manifest.json", "dispatch.db", "kew.toml"}


def _safe_extract(archive_path: Path, dest: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            members = [
                m
                for m in tar.getmembers()
                if m.name in _KNOWN_MEMBERS or m.name.startswith("logs/")
            ]
            tar.extractall(dest, members=members, filter="data")
    except OSError as exc:
        raise RestorePrerequisiteError(
            f"not a readable kew backup archive: {exc}"
        ) from exc
    except tarfile.TarError as exc:
        raise BackupError(f"not a readable kew backup archive: {exc}") from exc


def _read_manifest(path: Path) -> BackupManifest:
    if not path.exists():
        raise BackupError("archive has no manifest.json — not a kew backup")
    try:
        manifest = BackupManifest(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BackupError(f"backup manifest is malformed: {exc}") from exc
    _validate_manifest_identity(manifest)
    return manifest


def _validate_manifest_identity(manifest: BackupManifest) -> None:
    fields = (manifest.ledger_id, manifest.project_root, manifest.db_path)
    if all(field is None for field in fields):
        return
    if not all(isinstance(field, str) for field in fields):
        raise _malformed_manifest_identity()
    assert manifest.ledger_id is not None
    assert manifest.project_root is not None
    assert manifest.db_path is not None
    _validate_manifest_uuid(manifest.ledger_id)
    _validate_manifest_path(manifest.project_root)
    _validate_manifest_path(manifest.db_path)


def _malformed_manifest_identity() -> BackupError:
    return BackupError(
        "backup identity metadata is malformed; use explicit ledger-rebind"
    )


def _validate_manifest_uuid(ledger_id: str) -> None:
    try:
        parsed = uuid.UUID(ledger_id)
    except ValueError as exc:
        raise _malformed_manifest_identity() from exc
    if str(parsed) != ledger_id:
        raise _malformed_manifest_identity()


def _validate_manifest_path(value: str) -> None:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise _malformed_manifest_identity()


def _assert_schema_compat(manifest: BackupManifest) -> None:
    if manifest.schema_version > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"backup is schema v{manifest.schema_version}; this kew supports up "
            f"to v{CURRENT_SCHEMA_VERSION}. Upgrade kew to restore."
        )


def _assert_db_schema_compat(db_path: Path) -> None:
    """Guard against the EXTRACTED db's real schema, not just the manifest.

    Runs before any placement, so a crafted archive whose manifest understates
    its db's schema version is still rejected with the target left untouched.
    """
    if not db_path.exists():
        raise BackupError("archive has no dispatch.db — not a kew backup")
    conn = _bare_conn(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    finally:
        conn.close()
    version = int(row["version"]) if row else 0
    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"backup's dispatch.db is schema v{version}; this kew supports up "
            f"to v{CURRENT_SCHEMA_VERSION}. Upgrade kew to restore."
        )


def _verify_against_manifest(db_path: Path, manifest: BackupManifest) -> None:
    if not manifest.chain_ok:
        raise BackupError(
            "backup recorded a broken chain (ok=false); restore with --no-verify."
        )
    conn = _bare_conn(db_path)
    try:
        chain = verify_chain(
            [_row_to_chain_row(r) for r in conn.execute(_CHAIN_SELECT_ALL).fetchall()]
        )
    finally:
        conn.close()
    if (chain.head_hash, chain.count) != (
        manifest.chain_head_hash,
        manifest.chain_count,
    ):
        raise BackupError(
            "restored chain head/count does not match the manifest — "
            "corruption in transit."
        )


def _assert_idle(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=0.5)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        raise RestorePrerequisiteError(
            "another kew process is using the database — stop it first."
        ) from exc
    finally:
        conn.close()


def _restore_refusal(reason: str) -> RestorePrerequisiteError:
    return RestorePrerequisiteError(f"{reason}; run explicit `kew audit ledger-rebind`")


def _read_candidate_identity(
    db_path: Path,
) -> tuple[tuple[str, Path] | None, list[ChainRow]]:
    conn = _bare_conn(db_path)
    try:
        identity = _read_db_identity(conn)
        rows = [
            _row_to_chain_row(row) for row in conn.execute(_CHAIN_SELECT_ALL).fetchall()
        ]
    finally:
        conn.close()
    return identity, rows


def _assert_legacy_restore(
    candidate_identity: tuple[str, Path] | None,
    registry: RegistryRecord | None,
    anchors: list[object],
) -> None:
    if candidate_identity is not None:
        raise _restore_refusal("legacy manifest contains an identity-aware database")
    if registry is not None or anchors:
        raise _restore_refusal("legacy backup cannot replace a registered ledger")


def _assert_registry_matches(
    registry: RegistryRecord,
    *,
    ledger_id: str,
    project_root: Path,
    db_path: Path,
) -> None:
    if registry.ledger_id != ledger_id:
        raise _restore_refusal("backup identity does not match the registered ledger")
    if registry.project_root != project_root:
        raise _restore_refusal("backup identity does not match the registered ledger")
    if registry.db_path != db_path:
        raise _restore_refusal("backup identity does not match the registered ledger")


def _assert_identity_restore(
    *,
    candidate_identity: tuple[str, Path] | None,
    registry: RegistryRecord | None,
    manifest: BackupManifest,
    normalized_root: Path,
    normalized_target: Path,
    anchors: list[object],
    rows: list[ChainRow],
) -> None:
    assert manifest.ledger_id is not None
    assert manifest.project_root is not None
    assert manifest.db_path is not None
    expected = (manifest.ledger_id, Path(manifest.project_root))
    if registry is None:
        raise _restore_refusal("identity-aware backup has no unambiguous registry")
    if candidate_identity != expected or expected[1] != normalized_root:
        raise _restore_refusal("backup identity does not match the registered ledger")
    _assert_registry_matches(
        registry,
        ledger_id=manifest.ledger_id,
        project_root=normalized_root,
        db_path=normalized_target,
    )
    if registry.db_path != Path(manifest.db_path):
        raise _restore_refusal("backup identity does not match the registered ledger")
    _validate_rows_anchors(rows, anchors, manifest.ledger_id)


@contextmanager
def _validated_restore_identity(
    *,
    candidate_db: Path,
    manifest: BackupManifest,
    project_root: Path,
    db_target: Path,
    anchors_path: Path,
) -> Iterator[None]:
    try:
        normalized_root = canonical_project_root(project_root)
        normalized_target = db_target.resolve()
        paths = registry_paths(normalized_root)
        with advisory_registry_lock(paths.lock):
            anchors = _read_anchors(anchors_path)
            registry = (
                RegistryRecord.from_json(paths.registry.read_text())
                if paths.registry.exists()
                else None
            )
            candidate_identity, rows = _read_candidate_identity(candidate_db)
            if manifest.ledger_id is None:
                _assert_legacy_restore(candidate_identity, registry, anchors)
            else:
                _assert_identity_restore(
                    candidate_identity=candidate_identity,
                    registry=registry,
                    manifest=manifest,
                    normalized_root=normalized_root,
                    normalized_target=normalized_target,
                    anchors=anchors,
                    rows=rows,
                )
            yield
    except (LedgerIdentityError, OSError, UnicodeError) as exc:
        raise _restore_refusal(str(exc)) from exc


def _move_aside(db_target: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = db_target.with_name(f"{db_target.name}.bak-{stamp}")
    db_target.replace(dest)
    for suffix in ("-wal", "-shm"):
        side = db_target.with_name(db_target.name + suffix)
        if side.exists():
            side.replace(side.with_name(f"{side.name}.bak-{stamp}"))
    return dest


def _fsync(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _place_db(src_db: Path, db_target: Path) -> Path | None:
    db_target.parent.mkdir(parents=True, exist_ok=True)
    new = db_target.with_name("dispatch.db.new")
    shutil.copy2(src_db, new)
    _fsync(new)
    moved = _move_aside(db_target) if db_target.exists() else None
    new.replace(db_target)
    return moved


def _place_config(src_cfg: Path, config_dir: Path) -> Path | None:
    if not src_cfg.exists():
        return None
    dest = config_dir / "kew.toml"
    moved = None
    if dest.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        moved = dest.with_name(f"kew.toml.bak-{stamp}")
        dest.replace(moved)
    shutil.copy2(src_cfg, dest)
    _fsync(dest)
    return moved


def restore_backup(
    archive_path: Path | str,
    target_dir: Path | str,
    config_dir: Path | str,
    *,
    force: bool = False,
    with_config: bool = False,
    verify: bool = True,
    project_root: Path | str | None = None,
    anchors_path: Path | str | None = None,
) -> RestoreResult:
    """Validate and non-destructively restore a kew backup archive."""
    target_dir = Path(target_dir)
    db_target = target_dir / "dispatch.db"
    with work_dirs.work_tempdir("kew-backup-") as tmp_str:
        tmp = Path(tmp_str)
        _safe_extract(Path(archive_path), tmp)
        manifest = _read_manifest(tmp / "manifest.json")
        _assert_schema_compat(manifest)
        _assert_db_schema_compat(tmp / "dispatch.db")
        if verify:
            _verify_against_manifest(tmp / "dispatch.db", manifest)
        restore_root = (
            Path(project_root) if project_root is not None else Path(config_dir)
        )
        restore_anchors = (
            Path(anchors_path)
            if anchors_path is not None
            else restore_root / "audit-anchors.jsonl"
        )
        with _validated_restore_identity(
            candidate_db=tmp / "dispatch.db",
            manifest=manifest,
            project_root=restore_root,
            db_target=db_target,
            anchors_path=restore_anchors,
        ):
            if db_target.exists():
                if not force:
                    raise RestorePrerequisiteError(
                        "dispatch.db already exists at the target; pass --force "
                        "(the existing db is moved aside first)."
                    )
                _assert_idle(db_target)
            moved = _place_db(tmp / "dispatch.db", db_target)
            config_moved = (
                _place_config(tmp / "kew.toml", Path(config_dir))
                if with_config
                else None
            )
            with SqliteRunStore(str(db_target)):
                pass  # forward-migrate legacy archives without minting identity
    return RestoreResult(
        manifest=manifest, moved_old_to=moved, config_moved_to=config_moved
    )
