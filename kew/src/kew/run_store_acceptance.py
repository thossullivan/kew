"""Acceptance contracts: the ledger's record of a node's acceptance tests (#2726).

A node's acceptance contract is its read-only acceptance test file, written by
an ``acceptance_author`` run before the implementer starts. The file lives on
the branch, which an agent can write; this mixin is where the host records
which file it was — path, sha256, the run that authored it and the commit that
carries it — so a later reader can tell the contract the implementer was handed
from whatever the file says now.

One ``acceptance_contracts`` row per (epic, item, version). A node has at most
one current version, the highest; an amendment adds a version and every earlier
one stays listed. Re-recording the current contract unchanged mints nothing: a
host write retried after a crash is not an amendment.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypeVar

_T = TypeVar("_T")

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# A full commit id: 40 hex digits in a SHA-1 repository, 64 in a SHA-256 one.
# An abbreviated id is refused — it names a commit only until it collides.
_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class AcceptanceContractRow:
    """One recorded ``acceptance_contracts`` row: one version of a node's contract."""

    id: int
    epic_ref: str
    item_id: str
    version: int
    """1 for the first contract; each amendment adds one. The highest is current."""
    path: str
    """Repository-relative path of the acceptance test file."""
    sha256: str
    """sha256 of the file's bytes as authored."""
    author_run_id: str
    """The ``acceptance_author`` run that wrote the file."""
    commit: str
    """The commit that carries the file as authored."""
    created_at: datetime
    plan_sha: str | None = None
    """The digest of the plan the file was authored for (#2774); None on a row
    recorded before plans were bound, which no current plan matches."""
    test_names: tuple[str, ...] = ()
    """The ``test_ac<k>`` functions that plan required, in plan order."""


def _row_to_acceptance_contract(row: sqlite3.Row) -> AcceptanceContractRow:
    return AcceptanceContractRow(
        id=int(row["id"]),
        epic_ref=row["epic_ref"],
        item_id=row["item_id"],
        version=int(row["version"]),
        path=row["path"],
        sha256=row["sha256"],
        author_run_id=row["author_run_id"],
        commit=row["commit_sha"],
        created_at=datetime.fromisoformat(row["created_at"]),
        plan_sha=row["plan_sha"],
        test_names=tuple(json.loads(row["test_names"] or "[]")),
    )


def _validate_contract(
    *, path: str, sha256: str, author_run_id: str, commit: str
) -> None:
    """Refuse a contract the ledger could not later prove anything with."""
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or ".." in parts or "\\" in path:
        raise ValueError(f"path must be repository-relative, not {path!r}")
    if not _SHA256_RE.fullmatch(sha256):
        raise ValueError(f"sha256 must be 64 lowercase hex digits, not {sha256!r}")
    if not author_run_id:
        raise ValueError("author_run_id must name the authoring run")
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"commit must be a full lowercase commit id, not {commit!r}")


class AcceptanceContractStoreMixin:
    """The acceptance-contract persistence surface of ``SqliteRunStore``.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the in-process transaction lock, and the busy-retry / execute
    wrappers declared below.
    """

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def record_acceptance_contract(
        self,
        epic_ref: str,
        item_id: str,
        *,
        path: str,
        sha256: str,
        author_run_id: str,
        commit: str,
        plan_sha: str | None = None,
        test_names: tuple[str, ...] = (),
    ) -> AcceptanceContractRow:
        """Record a version of the (*epic_ref*, *item_id*) node's contract.

        Returns the row that is now current. The read of the current version
        and the insert of the next one share one ``BEGIN IMMEDIATE``
        transaction, so two writers cannot both mint the same version (the
        ``UNIQUE`` key would refuse the second anyway). *plan_sha* and
        *test_names* bind the row to the plan it was authored for (#2774).
        """
        _validate_contract(
            path=path, sha256=sha256, author_run_id=author_run_id, commit=commit
        )
        if plan_sha is not None and not _SHA256_RE.fullmatch(plan_sha):
            raise ValueError(
                f"plan_sha must be 64 lowercase hex digits, not {plan_sha!r}"
            )
        created_at = datetime.now(UTC).isoformat()

        def _do() -> AcceptanceContractRow:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._record_acceptance_contract_locked(
                        epic_ref,
                        item_id,
                        (path, sha256, author_run_id, commit),
                        (plan_sha, tuple(test_names)),
                        created_at,
                    )
                    self._conn.commit()
                    return row
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("record_acceptance_contract", _do)

    def _record_acceptance_contract_locked(
        self,
        epic_ref: str,
        item_id: str,
        fields: tuple[str, str, str, str],
        binding: tuple[str | None, tuple[str, ...]],
        created_at: str,
    ) -> AcceptanceContractRow:
        """Insert the next version unless *fields* already are the current one."""
        current = self._current_acceptance_contract_row(self._conn, epic_ref, item_id)
        if current is not None and (fields, binding) == (
            (current.path, current.sha256, current.author_run_id, current.commit),
            (current.plan_sha, current.test_names),
        ):
            return current
        plan_sha, test_names = binding
        version = 1 if current is None else current.version + 1
        cursor = self._conn.execute(
            "INSERT INTO acceptance_contracts"
            " (epic_ref, item_id, version, path, sha256, author_run_id,"
            "  commit_sha, created_at, plan_sha, test_names)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                epic_ref,
                item_id,
                version,
                *fields,
                created_at,
                plan_sha,
                json.dumps(list(test_names)),
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM acceptance_contracts WHERE id = ?",
            (int(cursor.lastrowid or 0),),
        ).fetchone()
        return _row_to_acceptance_contract(row)

    @staticmethod
    def _current_acceptance_contract_row(
        conn: sqlite3.Connection, epic_ref: str, item_id: str
    ) -> AcceptanceContractRow | None:
        row = conn.execute(
            "SELECT * FROM acceptance_contracts WHERE epic_ref = ? AND item_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (epic_ref, item_id),
        ).fetchone()
        return None if row is None else _row_to_acceptance_contract(row)

    def current_acceptance_contract(
        self, epic_ref: str, item_id: str
    ) -> AcceptanceContractRow | None:
        """The node's current contract, or None when none was ever recorded."""
        return self._current_acceptance_contract_row(self._conn, epic_ref, item_id)

    def current_acceptance_contracts_for_item(
        self, item_id: str
    ) -> list[AcceptanceContractRow]:
        """The current contract of every epic that recorded one for *item_id*.

        Publication and the loop guard know the item they act for, not always
        the epic. Every epic's current version is returned, so a reader that
        holds on any mismatch fails closed rather than picking one.
        """
        rows = self._execute(
            "SELECT * FROM acceptance_contracts AS a WHERE a.item_id = ?"
            " AND a.version = (SELECT MAX(b.version) FROM acceptance_contracts AS b"
            "  WHERE b.epic_ref = a.epic_ref AND b.item_id = a.item_id)"
            " ORDER BY a.epic_ref",
            (item_id,),
        ).fetchall()
        return [_row_to_acceptance_contract(row) for row in rows]

    def list_acceptance_contracts(
        self, epic_ref: str, item_id: str
    ) -> list[AcceptanceContractRow]:
        """Every version of the node's contract, oldest first."""
        rows = self._execute(
            "SELECT * FROM acceptance_contracts WHERE epic_ref = ? AND item_id = ?"
            " ORDER BY version",
            (epic_ref, item_id),
        ).fetchall()
        return [_row_to_acceptance_contract(row) for row in rows]
