"""Normalized artifact rows: metadata and a digest, never the bytes (I06).

``run_artifact`` is the durable index over artifacts whose *content* lives
outside SQLite — today the exact compiled prompt held by
:mod:`kew.sensitive_prompt`. The table exists so that the ledger, the CLI and
every later consumer can talk about an artifact (what kind, which digest, how
many bytes, is it still readable) without the bytes ever entering the database,
a backup, an export, or a certificate.

Three rules hold the boundary:

* **No content column.** The schema has nowhere to put bytes, and
  :meth:`ArtifactStoreMixin.record_artifact` takes no content parameter. A
  reference that is not a ``sha256:`` digest is refused outright, so a caller
  cannot smuggle the prompt in through the digest field.
* **A recorded artifact is immutable.** Re-recording an identical row is an
  idempotent no-op; recording anything that DIFFERS for the same
  ``(run_id, attempt_number, kind)`` — the digest, the byte length, or the
  locator — raises :class:`ArtifactConflictError`. The locator matters as much
  as the digest: an artifact whose bytes stay put while its address is quietly
  repointed is exactly the substitution an immutable index exists to refuse
  (I07).
* **Purge is a state, not a delete.** Purging keeps the row and flips
  ``storage`` to ``unavailable`` with a reason, so a reader distinguishes
  "retention removed it" from "there was never one".

Retention is set separately from creation: an artifact only starts aging once
its run reaches a terminal state, at which point
:meth:`ArtifactStoreMixin.set_artifact_retention` stamps
``terminal_at`` and ``expires_at`` (terminal plus
:data:`kew.sensitive_prompt.RETENTION_DAYS`). An artifact with no terminal
stamp never expires — an in-flight run's prompt is not garbage.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from kew.sensitive_prompt import (
    RETENTION_DAYS,
    STORAGE_LOCAL,
    STORAGE_UNAVAILABLE,
    is_digest,
    retention_deadline,
)

_T = TypeVar("_T")

_ARTIFACT_COLS = (
    "run_id",
    "attempt_number",
    "kind",
    "storage",
    "digest",
    "byte_length",
    "locator",
    "created_at",
    "terminal_at",
    "expires_at",
    "purged_at",
    "purge_reason",
)

_ARTIFACT_SELECT = f"SELECT {', '.join(_ARTIFACT_COLS)} FROM run_artifact"  # nosec B608

_KEY_CLAUSE = " WHERE run_id = ? AND attempt_number = ? AND kind = ?"

# Built once from module constants (column names and a fixed WHERE clause),
# never from caller input — every value below is a bound parameter. bandit
# reports the construction site, so the suppressions live here.
_UPDATE_RETENTION = "UPDATE run_artifact SET terminal_at = ?, expires_at = ?"
_SET_RETENTION_SQL = _UPDATE_RETENTION + _KEY_CLAUSE  # nosec B608
_UPDATE_PURGED = "UPDATE run_artifact SET storage = ?, purged_at = ?, purge_reason = ?"
_MARK_PURGED_SQL = _UPDATE_PURGED + _KEY_CLAUSE  # nosec B608


class ArtifactStoreError(RuntimeError):
    """Base class for artifact-row refusals."""


class ArtifactValueError(ArtifactStoreError):
    """A field was not the kind of value this table is allowed to hold."""


class ArtifactConflictError(ArtifactStoreError):
    """A different artifact is already recorded for this attempt and kind."""


@dataclass(frozen=True)
class StoredArtifact:
    """One ``run_artifact`` row — metadata about bytes stored elsewhere."""

    run_id: str
    attempt_number: int
    kind: str
    storage: str
    digest: str
    byte_length: int
    locator: str | None
    created_at: str
    terminal_at: str | None = None
    expires_at: str | None = None
    purged_at: str | None = None
    purge_reason: str | None = None

    @property
    def available(self) -> bool:
        """True while the referenced bytes are still expected on disk."""
        return self.storage == STORAGE_LOCAL and self.purged_at is None


def _row_to_artifact(row: sqlite3.Row) -> StoredArtifact:
    return StoredArtifact(**{col: row[col] for col in _ARTIFACT_COLS})


def _iso(value: datetime | str) -> str:
    return value if isinstance(value, str) else value.isoformat()


class ArtifactStoreMixin:
    """The ``run_artifact`` surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore.
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def record_artifact(
        self,
        *,
        run_id: str,
        attempt_number: int,
        kind: str,
        digest: str,
        byte_length: int,
        locator: str | None,
        created_at: datetime | str,
        storage: str = STORAGE_LOCAL,
    ) -> StoredArtifact:
        """Record metadata for one artifact. Idempotent for identical facts.

        There is no content parameter and no content column: the bytes stay
        wherever *storage* says they are.
        """
        if not is_digest(digest):
            raise ArtifactValueError(
                "artifact reference must be a sha256:<hex> digest, not content"
            )
        if byte_length < 0:
            raise ArtifactValueError("artifact byte_length cannot be negative")
        candidate = StoredArtifact(
            run_id=run_id,
            attempt_number=attempt_number,
            kind=kind,
            storage=storage,
            digest=digest,
            byte_length=byte_length,
            locator=locator,
            created_at=_iso(created_at),
        )
        return self._with_busy_retry(
            "record_artifact", lambda: self._insert_artifact(candidate)
        )

    def _insert_artifact(self, candidate: StoredArtifact) -> StoredArtifact:
        existing = self.get_artifact(
            run_id=candidate.run_id,
            attempt_number=candidate.attempt_number,
            kind=candidate.kind,
        )
        if existing is not None:
            differs = next(
                (
                    field
                    for field in ("digest", "byte_length", "locator")
                    if getattr(existing, field) != getattr(candidate, field)
                ),
                None,
            )
            if differs is not None:
                raise ArtifactConflictError(
                    f"artifact {candidate.kind} for {candidate.run_id}"
                    f" attempt {candidate.attempt_number} is already recorded"
                    f" with a different {differs}"
                )
            return existing
        self._execute(
            "INSERT INTO run_artifact (run_id, attempt_number, kind, storage,"
            " digest, byte_length, locator, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.run_id,
                candidate.attempt_number,
                candidate.kind,
                candidate.storage,
                candidate.digest,
                candidate.byte_length,
                candidate.locator,
                candidate.created_at,
            ),
        )
        self._conn.commit()
        return candidate

    def get_artifact(
        self, *, run_id: str, attempt_number: int, kind: str
    ) -> StoredArtifact | None:
        """One artifact row, or None when this attempt recorded none."""
        row = self._conn.execute(  # nosemgrep
            _ARTIFACT_SELECT + _KEY_CLAUSE, (run_id, attempt_number, kind)
        ).fetchone()
        return None if row is None else _row_to_artifact(row)

    def list_artifacts(
        self, *, run_id: str | None = None, kind: str | None = None
    ) -> list[StoredArtifact]:
        """Artifact rows in creation order, optionally narrowed."""
        clauses: list[str] = []
        params: list[Any] = []
        for clause, value in (("run_id = ?", run_id), ("kind = ?", kind)):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = _ARTIFACT_SELECT + where + " ORDER BY id ASC"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [_row_to_artifact(row) for row in rows]

    def set_artifact_retention(
        self,
        *,
        run_id: str,
        attempt_number: int,
        kind: str,
        terminal_at: datetime,
        days: int = RETENTION_DAYS,
    ) -> bool:
        """Start the retention clock at *terminal_at* plus *days*."""
        expires_at = retention_deadline(terminal_at, days=days)

        def _apply() -> bool:
            cursor = self._execute(  # nosemgrep
                _SET_RETENTION_SQL,
                (
                    _iso(terminal_at),
                    _iso(expires_at),
                    run_id,
                    attempt_number,
                    kind,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("set_artifact_retention", _apply)

    def list_expired_artifacts(
        self, *, now: datetime, kind: str | None = None
    ) -> list[StoredArtifact]:
        """Artifacts past their retention deadline and not yet purged."""
        clauses = ["expires_at IS NOT NULL", "expires_at <= ?", "purged_at IS NULL"]
        params: list[Any] = [_iso(now)]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        sql = _ARTIFACT_SELECT + f" WHERE {' AND '.join(clauses)} ORDER BY id ASC"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [_row_to_artifact(row) for row in rows]

    def mark_artifact_purged(
        self,
        *,
        run_id: str,
        attempt_number: int,
        kind: str,
        purged_at: datetime,
        reason: str,
    ) -> bool:
        """Flip an artifact to unavailable, keeping its metadata and digest."""

        def _apply() -> bool:
            cursor = self._execute(  # nosemgrep
                _MARK_PURGED_SQL,
                (
                    STORAGE_UNAVAILABLE,
                    _iso(purged_at),
                    reason,
                    run_id,
                    attempt_number,
                    kind,
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

        return self._with_busy_retry("mark_artifact_purged", _apply)
