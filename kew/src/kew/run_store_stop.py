"""Durable label-reset requests, leases, and admission fencing.

Provider operations must be idempotent. A crash after delivery but before commit
leaves the lease intact. Expiry permits one claimant to retry the same request.
The delivery transaction excludes admissions until the provider operation ends.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from kew import stop_recovery
from kew.stop_recovery import LabelReset

_T = TypeVar("_T")
LABEL_LEASE_SECONDS = 60.0


class StopLabelStoreMixin:
    """Serialize request claims across connections and fence provider effects."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock
        _stop_label_reset: LabelReset | None

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

    def _label_event(
        self, event: str, run: sqlite3.Row, payload: dict[str, Any]
    ) -> int | None:
        return self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                event,
                run["run_id"],
                run["issue_num"],
                run["repo"],
                None,
                None,
                None,
                None,
                json.dumps(payload),
                run["item_id"],
            )
        )

    def _request_stop_label_reset(self, run_id: str, task: dict[str, str]) -> None:
        def request() -> None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    existing = self._conn.execute(
                        "SELECT 1 FROM audit_events WHERE run_id = ?"
                        " AND event = 'stop_label_reset_requested'",
                        (run_id,),
                    ).fetchone()
                    run = self._conn.execute(
                        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    if run is not None and existing is None:
                        self._label_event(
                            "stop_label_reset_requested",
                            run,
                            {**task, "request_id": uuid.uuid4().hex},
                        )
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        self._with_busy_retry("request_stop_label_reset", request)

    def _seed_label_outbox(self) -> None:
        """Import audit requests without rewriting historical chained payloads."""
        self._conn.execute(
            "INSERT OR IGNORE INTO stop_label_outbox(request_id, event_id, completed)"
            " SELECT COALESCE(json_extract(a.payload, '$.request_id'),"
            " 'legacy:' || a.id),"
            " a.id, EXISTS(SELECT 1 FROM audit_events done"
            " WHERE done.event = 'stop_label_reset_completed' AND ("
            " json_extract(done.payload, '$.request_id') ="
            " COALESCE(json_extract(a.payload, '$.request_id'), 'legacy:' || a.id)"
            " OR json_extract(done.payload, '$.request_id') = a.id))"
            " FROM audit_events a WHERE a.event = 'stop_label_reset_requested'"
        )

    def _claim_label_resets(self, run_id: str | None, token: str) -> list[sqlite3.Row]:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # Partial historical test schemas have no outbox.
                if not self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'audit_events'"
                ).fetchone():
                    self._conn.rollback()
                    return []
                self._seed_label_outbox()
                now = time.time()
                self._conn.execute(
                    "UPDATE stop_label_outbox SET lease_token = ?, lease_until = ?"
                    " WHERE completed = 0 AND lease_until <= ? AND event_id IN ("
                    " SELECT a.id FROM audit_events a"
                    " JOIN runs r ON r.run_id = a.run_id"
                    " WHERE r.status != 'pending' AND (? IS NULL OR a.run_id = ?))",
                    (token, now + LABEL_LEASE_SECONDS, now, run_id, run_id),
                )
                tasks = self._conn.execute(
                    "SELECT o.request_id, o.event_id, a.payload, a.timestamp, r.*"
                    " FROM stop_label_outbox o JOIN audit_events a ON a.id = o.event_id"
                    " JOIN runs r ON r.run_id = a.run_id WHERE o.lease_token = ?",
                    (token,),
                ).fetchall()
                self._conn.commit()
                return tasks
            except BaseException:
                self._conn.rollback()
                raise

    def _label_request_superseded(self, task: sqlite3.Row) -> bool:
        """Admission rows and dispatch events both transfer item label ownership.

        The repo match is deliberately asymmetric: two *known* slugs that differ
        are two different items, but an unknown slug on either side matches
        every repo. Rows written before #2097 left ``runs.repo`` NULL whenever
        ``project.repo`` was auto-detected, so a reset requested before the
        upgrade and delivered after it compares NULL against a resolved slug —
        an equality match read that as a different item and stripped the live
        run's labels. Suppressing a stale reset costs one leftover label;
        missing a supersede strips a live run's.
        """
        return (
            self._conn.execute(
                "SELECT 1 FROM runs newer WHERE newer.run_id != ?"
                " AND COALESCE(newer.item_id, CAST(newer.issue_num AS TEXT)) = ?"
                " AND (COALESCE(newer.repo, '') = '' OR COALESCE(?, '') = ''"
                " OR newer.repo = ?) AND ("
                " newer.started_at > ? OR EXISTS(SELECT 1 FROM run_admission_identity i"
                " WHERE i.run_id = newer.run_id AND i.recorded_at > ?)"
                " OR EXISTS(SELECT 1 FROM audit_events a"
                " WHERE a.run_id = newer.run_id AND a.id > ?"
                " AND (a.event = 'dispatch_started' OR a.event LIKE '%admi%')))"
                " LIMIT 1",
                (
                    task["run_id"],
                    task["item_id"] or str(task["issue_num"]),
                    task["repo"],
                    task["repo"],
                    task["timestamp"],
                    task["timestamp"],
                    task["event_id"],
                ),
            ).fetchone()
            is not None
        )

    def _deliver_label_reset(self, task: sqlite3.Row, token: str) -> None:
        """Hold the write fence from lease validation through provider completion."""
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                lease = self._conn.execute(
                    "SELECT 1 FROM stop_label_outbox WHERE request_id = ?"
                    " AND lease_token = ? AND lease_until > ? AND completed = 0",
                    (task["request_id"], token, time.time()),
                ).fetchone()
                if lease is None:
                    self._conn.rollback()
                    return
                superseded = self._label_request_superseded(task)
                if not superseded:
                    reset = self._stop_label_reset or stop_recovery.reset_labels
                    reset(task["issue_num"], json.loads(task["payload"]))
                self._label_event(
                    "stop_label_reset_completed",
                    task,
                    {"request_id": task["request_id"], "superseded": superseded},
                )
                self._conn.execute(
                    "UPDATE stop_label_outbox SET completed = 1, lease_token = NULL"
                    " WHERE request_id = ? AND lease_token = ?",
                    (task["request_id"], token),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _release_label_lease(self, request_id: str, token: str) -> None:
        with self._txn_lock:
            self._conn.execute(
                "UPDATE stop_label_outbox SET lease_until = 0, lease_token = NULL"
                " WHERE request_id = ? AND lease_token = ? AND completed = 0",
                (request_id, token),
            )
            self._conn.commit()

    def retry_stop_label_resets(
        self, run_id: str | None = None, *, reset_labels: LabelReset | None = None
    ) -> None:
        """Deliver terminal requests under exclusive, expiring leases."""
        if reset_labels is not None:
            self._stop_label_reset = reset_labels
        token = uuid.uuid4().hex
        tasks = self._with_busy_retry(
            "claim_stop_label_resets", lambda: self._claim_label_resets(run_id, token)
        )
        for task in tasks:
            try:
                self._with_busy_retry(
                    "deliver_stop_label_reset",
                    partial(self._deliver_label_reset, task, token),
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Label reset remains pending for %s: %s", task["request_id"], exc
                )
                self._release_label_lease(task["request_id"], token)
