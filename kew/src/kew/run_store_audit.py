"""Audit-chain store operations, extracted from ``run_store`` (Task 1).

This module owns the append/read surface of the tamper-evident audit chain:
the chained writer (``append_audit_event`` → ``_chained_insert``) and the raw
chain readers (``read_chain_rows``, ``read_chain_head_key``). It is a pure seam
extraction — the ``entry_hash``/preimage semantics live in ``audit_chain.py``;
this module only decides which chain format a new row is written in.

``SqliteRunStore`` mixes :class:`AuditChainStoreMixin` in, and ``run_store``
re-exports the module-level helpers so no caller import path changes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from kew.audit_chain import (
    CHAIN_FORMAT_ITEM_KEYS,
    CHAIN_FORMAT_LEGACY,
    GENESIS_PREV,
    ChainRow,
    entry_hash,
)

_T = TypeVar("_T")


def _normalize_event_ts(timestamp: str) -> str:
    """Canonical UTC ISO-8601 so TEXT-based timestamp comparisons hold.

    Naive timestamps are treated as UTC; unparseable strings are stored
    as given (the read side already tolerates arbitrary text).
    """
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def audit_values(
    *,
    timestamp: str,
    event: str,
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    endpoint: str | None = None,
    policy_hash: str | None = None,
    prev_hash: str | None = None,  # accepted for Protocol/back-compat; ignored
    payload: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """The row :meth:`AuditChainStoreMixin._chained_insert_in_transaction`
    takes, from :meth:`AuditChainStoreMixin.append_audit_event`'s fields."""
    return (
        _normalize_event_ts(timestamp),
        event,
        run_id,
        issue_num,
        repo,
        actor_type,
        actor_id,
        endpoint,
        policy_hash,
        json.dumps(payload) if payload is not None else None,
        item_id,
    )


# Chain readers select every column: a ledger (or backup) from before v24,
# v39 or v77 lacks ``item_id``, ``dedup_key`` or ``chain_format``, and
# _row_to_chain_row reads each of those only when the row carries it.
_CHAIN_SELECT_ALL = "SELECT * FROM audit_events ORDER BY id ASC"
_CHAIN_SELECT_TAIL = "SELECT * FROM audit_events ORDER BY id DESC LIMIT 1"

# The columns every chained insert writes, in ``audit_values`` order with
# ``prev_hash`` before ``payload``. Fixed names, never user input.
_INSERT_COLS = (
    "timestamp",
    "event",
    "run_id",
    "issue_num",
    "repo",
    "actor_type",
    "actor_id",
    "endpoint",
    "policy_hash",
    "prev_hash",
    "payload",
    "item_id",
)


def _row_to_chain_row(row: sqlite3.Row) -> ChainRow:
    """Build a ChainRow from RAW column values (payload stays verbatim TEXT).

    A row from before the chain-format step has no ``chain_format`` column
    (or a NULL in it) and reads as the legacy format.
    """
    keys = row.keys()
    chain_format = row["chain_format"] if "chain_format" in keys else None
    return ChainRow(
        id=row["id"],
        timestamp=row["timestamp"],
        event=row["event"],
        run_id=row["run_id"],
        issue_num=row["issue_num"],
        repo=row["repo"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        endpoint=row["endpoint"],
        policy_hash=row["policy_hash"],
        payload=row["payload"],
        prev_hash=row["prev_hash"],
        item_id=row["item_id"] if "item_id" in keys else None,
        dedup_key=row["dedup_key"] if "dedup_key" in keys else None,
        chain_format=CHAIN_FORMAT_LEGACY if chain_format is None else chain_format,
    )


def _writer_chain_format(conn: sqlite3.Connection) -> int:
    """The format a new row is written in.

    Item-keys once the v77 column exists. A migration step before v77 that
    writes an event (v36 binds the ledger identity) writes the legacy format,
    which is what the chain held then.
    """
    columns = {col[1] for col in conn.execute("PRAGMA table_info(audit_events)")}
    if "chain_format" in columns:
        return CHAIN_FORMAT_ITEM_KEYS
    return CHAIN_FORMAT_LEGACY


def chained_insert_statement(
    conn: sqlite3.Connection,
    values: tuple[Any, ...],
    *,
    prev_hash: str,
    dedup_key: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """The INSERT for one chained row and its parameters.

    ``values`` is the ``audit_values`` tuple. Every writer of ``audit_events``
    builds its insert here so each writes the same chain format.
    """
    columns = list(_INSERT_COLS)
    params: list[Any] = [*values[:9], prev_hash, values[9], values[10]]
    if dedup_key is not None:
        columns.append("dedup_key")
        params.append(dedup_key)
    chain_format = _writer_chain_format(conn)
    if chain_format != CHAIN_FORMAT_LEGACY:
        columns.append("chain_format")
        params.append(chain_format)
    sql = (
        f"INSERT INTO audit_events ({', '.join(columns)})"  # nosec B608
        f" VALUES ({', '.join('?' * len(columns))})"
    )
    return sql, tuple(params)


class AuditChainStoreMixin:
    """The append/read audit-chain surface of ``SqliteRunStore``.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the in-process transaction lock, and the busy-retry / execute
    wrappers declared below.
    """

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore — declared here so the mixin
        # body type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def append_audit_event(
        self,
        *,
        timestamp: str,
        event: str,
        run_id: str | None = None,
        issue_num: int | None = None,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        endpoint: str | None = None,
        policy_hash: str | None = None,
        prev_hash: str | None = None,  # accepted for Protocol/back-compat; ignored
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """Append one chained audit row; return the new row id.

        prev_hash is recomputed from the current tail under BEGIN IMMEDIATE,
        so the caller-supplied value is ignored. ``item_id`` (v24) enters the
        entry_hash preimage of every row written from v77 on (#2029); rows
        written before that step leave it out.
        """
        values = audit_values(
            timestamp=timestamp,
            event=event,
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=repo,
            actor_type=actor_type,
            actor_id=actor_id,
            endpoint=endpoint,
            policy_hash=policy_hash,
            payload=payload,
        )
        return self._with_busy_retry(
            "append_audit_event", lambda: self._chained_insert(values)
        )

    def append_audit_event_once(
        self,
        *,
        dedup_key: str,
        timestamp: str,
        event: str,
        run_id: str | None = None,
        issue_num: int | None = None,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        endpoint: str | None = None,
        policy_hash: str | None = None,
        prev_hash: str | None = None,  # accepted for Protocol parity; ignored
        payload: dict[str, Any] | None = None,
    ) -> int | None:
        """Atomically append an audit identity once, returning its row id."""
        values = (
            _normalize_event_ts(timestamp),
            event,
            run_id,
            issue_num,
            repo,
            actor_type,
            actor_id,
            endpoint,
            policy_hash,
            json.dumps(payload) if payload is not None else None,
            item_id,
        )
        return self._with_busy_retry(
            "append_audit_event_once",
            lambda: self._chained_insert_once(values, dedup_key),
        )

    def probe_audit_write(self) -> None:
        """Prove an audit row can be written now, and leave none behind.

        One insert into the audit table under ``BEGIN IMMEDIATE``, rolled
        back: a store that refuses it — read-only, locked past its retries,
        out of space — raises here, before a caller that cannot go on
        unrecorded does anything it would have to record (#2874). The row is
        never committed, so it is no event and joins no chain.

        Its INSERT comes from ``chained_insert_statement``, linked to the
        chain tail, so it carries the writer's chain format: a legacy-format
        probe is refused by the v77 downgrade trigger once the ledger holds a
        format-2 row (#3332).
        """
        values = (
            _normalize_event_ts(datetime.now(UTC).isoformat()),
            "",
            *([None] * 9),
        )

        def probe() -> None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    tail = self._read_chain_tail()
                    prev = entry_hash(tail) if tail is not None else GENESIS_PREV
                    sql, params = chained_insert_statement(
                        self._conn, values, prev_hash=prev
                    )
                    self._execute(sql, params)  # nosemgrep
                finally:
                    self._conn.rollback()

        self._with_busy_retry("probe_audit_write", probe)

    def _chained_insert_once(
        self, values: tuple[Any, ...], dedup_key: str
    ) -> int | None:
        """Check identity and append onto the chain in one write transaction."""
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT id FROM audit_events WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if existing is not None:
                    self._conn.commit()
                    return int(existing["id"])
                event_id = self._chained_insert_in_transaction(
                    values, dedup_key=dedup_key
                )
                self._conn.commit()
                return event_id
            except BaseException:
                self._conn.rollback()
                raise

    def _chained_insert(self, values: tuple[Any, ...]) -> int | None:
        """BEGIN IMMEDIATE → chain onto raw tail → INSERT → COMMIT.

        Returns the new row id (lastrowid).

        Owns its rollback: _with_busy_retry only rolls back in its "locked"
        branch, so any other exception here must not leave the write
        transaction open (that would wedge every other writer under WAL).
        """
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                event_id = self._chained_insert_in_transaction(values)
                self._conn.commit()
                return event_id
            except BaseException:
                self._conn.rollback()
                raise

    def _chained_insert_in_transaction(
        self, values: tuple[Any, ...], *, dedup_key: str | None = None
    ) -> int | None:
        """Chain and insert using the caller's already-open write transaction."""

        tail = self._read_chain_tail()
        prev = entry_hash(tail) if tail is not None else GENESIS_PREV
        sql, params = chained_insert_statement(
            self._conn, values, prev_hash=prev, dedup_key=dedup_key
        )
        cursor = self._execute(sql, params)  # nosemgrep
        return cursor.lastrowid

    def read_chain_rows(self) -> list[ChainRow]:
        """All audit rows ascending, as raw ChainRows (for verify/anchor)."""
        rows = self._conn.execute(_CHAIN_SELECT_ALL).fetchall()  # nosemgrep
        return [_row_to_chain_row(r) for r in rows]

    def read_ledger_id(self) -> str | None:
        """Return the v36 singleton identity, or None for a legacy ledger."""
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'ledger_identity'"
        ).fetchone()
        if table is None:
            return None
        row = self._conn.execute(
            "SELECT ledger_id FROM ledger_identity WHERE id = 1"
        ).fetchone()
        return None if row is None else str(row["ledger_id"])

    def read_chain_head_key(self) -> tuple[int, str] | None:
        """Cheap cache key: (row_count, entry_hash_of_tail), or None if empty.

        COUNT and tail are read under one transaction so the pair is a
        consistent snapshot. This only matters if the table can *shrink*
        concurrently (backup/restore); under append-only growth a torn read
        yields a stale key and at worst one harmless extra verify.
        """
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN")
                count = self._conn.execute(  # nosemgrep
                    "SELECT COUNT(*) FROM audit_events"
                ).fetchone()[0]
                tail = self._read_chain_tail() if count else None
            finally:
                self._conn.commit()
        if count == 0 or tail is None:
            return None
        return (count, entry_hash(tail))

    def _read_chain_tail(self) -> ChainRow | None:
        row = self._conn.execute(_CHAIN_SELECT_TAIL).fetchone()  # nosemgrep
        return _row_to_chain_row(row) if row is not None else None
