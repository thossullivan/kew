"""Committed posture reads without sharing the writer's transaction (#1958)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, RunRecord


class RunReadCursor(Protocol):
    """Read-only record and posture surface, independent of the store backend."""

    consistent_snapshot: bool
    work_store: Any

    def get(self, run_id: str) -> RunRecord | None: ...
    def list_audit_events(
        self, *, event: str, run_id: str, limit: int
    ) -> list[AuditEvent]: ...

    @property
    def evidence_available(self) -> bool: ...


class _SnapshotWorkReader:
    """Bind existing work reads to the connection owned by the outer scope."""

    def __init__(self, store: Any, connection: sqlite3.Connection) -> None:
        # Reuse the store's SQL decoders without opening or migrating a database.
        # Only the private copy receives this connection; the writer is untouched.
        self._store = copy(store)
        # The copy must read the bound connection, not the original's snapshot.
        self._store._read_snapshot_local = threading.local()
        self._store._conn = connection
        self._store._read_only = True

    def read_snapshot(self) -> nullcontext[None]:
        """Join the committed scope; memory degradation must not start BEGIN."""
        return nullcontext()

    def __getattr__(self, name: str) -> Any:
        if name not in self._store._committed_work_reads:
            raise AttributeError(name)
        return getattr(self._store, name)


class _FileReadCursor:
    consistent_snapshot = True
    evidence_available = True

    def __init__(self, cursor: sqlite3.Cursor, store: Any) -> None:
        self._cursor = cursor
        self.work_store = _SnapshotWorkReader(store, cursor.connection)

    def get(self, run_id: str) -> RunRecord | None:
        from kew.run_store import _row_to_record

        row = self._cursor.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_audit_events(
        self, *, event: str, run_id: str, limit: int
    ) -> list[AuditEvent]:
        from kew.run_store import _row_to_audit_event

        rows = self._cursor.execute(
            "SELECT * FROM audit_events WHERE event = ? AND run_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (event, run_id, limit),
        ).fetchall()
        return [_row_to_audit_event(row) for row in rows]


class _MemoryReadCursor:
    """Test-only fallback. An observed open transaction invalidates all evidence."""

    consistent_snapshot = False

    def __init__(self, store: Any) -> None:
        self._store = store
        self._tainted = False
        self.work_store = _SnapshotWorkReader(store, store._conn)

    @property
    def evidence_available(self) -> bool:
        self._tainted |= self._store._conn.in_transaction
        return not self._tainted

    def get(self, run_id: str) -> RunRecord | None:
        _ = self.evidence_available
        record = cast("RunRecord | None", self._store.get(run_id))
        _ = self.evidence_available
        return record

    def list_audit_events(self, **filters: Any) -> list[AuditEvent]:
        changes = self._store._conn.total_changes
        if not self.evidence_available:
            return []
        events = self._store.list_audit_events(**filters)
        # total_changes also counts rolled-back inserts. Refuse a page if a
        # writer inserted and rolled back between the transaction-state checks.
        stable = self._store._conn.total_changes == changes
        return events if self.evidence_available and stable else []


class CommittedReadStoreMixin:
    """Own lazy read connections, with no writer connection transaction or lock.

    Cross-thread stores share one reader under a separate read lock.
    Thread-affine stores keep one reader per thread with SQLite's default guard.
    Each thread must close its reader with ``close_committed_reader`` before exit.
    ``close`` releases the current thread's reader, or the shared reader.
    Memory databases cannot reopen their data. Their test-only fallback keeps
    the service's three-pass verification and suppresses uncommitted evidence.
    """

    _db_path: str

    @property
    def _conn(self) -> sqlite3.Connection:
        """The writer connection, or this thread's open ``read_snapshot``.

        A snapshot holds its own read-only connection, so its BEGIN and
        ROLLBACK never touch the writer another thread may be inside (#1958).
        """
        state = self.__dict__
        snapshot = getattr(state.get("_read_snapshot_local"), "connection", None)
        connection = snapshot or state.get("_writer_conn")
        if connection is None:
            raise AttributeError("_conn")
        return cast("sqlite3.Connection", connection)

    @_conn.setter
    def _conn(self, connection: sqlite3.Connection) -> None:
        self.__dict__["_writer_conn"] = connection

    def _register_sql_functions(self, connection: sqlite3.Connection) -> None:
        """Install the store's SQL functions on *connection*."""

    def _open_snapshot_connection(self) -> sqlite3.Connection:
        """A private read-only connection for one thread's snapshot block.

        Used in every journal mode. Without WAL its SHARED lock makes another
        thread's commit wait (``busy_timeout``, then the busy retry) until the
        block ends: a stall, never a lost save (#1958). Any thread may close
        the connection, so a generator finalized elsewhere does not leak it.
        """
        connection = sqlite3.connect(self._read_uri, uri=True, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            self._register_sql_functions(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _close_writer(self) -> None:
        """Close the writer connection, even from inside a ``read_snapshot``."""
        self.__dict__["_writer_conn"].close()

    def _init_committed_reader(self, check_same_thread: bool) -> None:
        self._read_check_same_thread = check_same_thread
        self._read_local = threading.local()
        self._read_conn: sqlite3.Connection | None = None
        self._read_lock = threading.RLock()
        self._read_uri = Path(self._db_path).resolve().as_uri() + "?mode=ro"

    def _committed_connection(self) -> sqlite3.Connection:
        connection = (
            getattr(self._read_local, "connection", None)
            if self._read_check_same_thread
            else self._read_conn
        )
        if connection is None:
            connection = sqlite3.connect(
                self._read_uri,
                uri=True,
                check_same_thread=self._read_check_same_thread,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if self._read_check_same_thread:
                self._read_local.connection = connection
            else:
                self._read_conn = connection
        return connection

    @contextmanager
    def read_committed(self) -> Iterator[RunReadCursor]:
        if self._db_path == ":memory:":
            yield _MemoryReadCursor(self)
            return
        with self._read_lock:
            connection = self._committed_connection()
            # Nested reads share the outer snapshot, never end its transaction.
            owned = not connection.in_transaction
            if owned:
                connection.execute("BEGIN")
            cursor = connection.cursor()
            try:
                yield _FileReadCursor(cursor, self)
            finally:
                cursor.close()
                if owned:
                    connection.rollback()

    def close_committed_reader(self) -> None:
        """Release this thread's reader, or the cross-thread shared reader."""
        with self._read_lock:
            connection = (
                getattr(self._read_local, "connection", None)
                if self._read_check_same_thread
                else self._read_conn
            )
            if connection is not None:
                connection.close()
            self._read_local.connection = None
            self._read_conn = None
