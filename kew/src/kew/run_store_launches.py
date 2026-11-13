"""Durable launch receipts and atomic cancellation finalization."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

import kew.run_store

#: An admission writes its run row inside a bounded transaction, so a
#: cancellation request that still has no row after this long never will.
CANCELLATION_REPLAY_LIMIT_SECONDS = 3600.0

if TYPE_CHECKING:
    from kew.cancellation_journal import CancellationJournal, StartupState

_logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class CiFixAttemptsExhausted(RuntimeError):
    """The PR has no unoccupied repair slot."""


def create_launch_schema(cursor: sqlite3.Cursor) -> None:
    """Install v56 receipts and settle them within every terminal run write."""
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS run_launches ("
        "run_id TEXT PRIMARY KEY REFERENCES runs(run_id),"
        "repo TEXT, pr_number INTEGER, ordinal INTEGER,"
        "state TEXT NOT NULL DEFAULT 'pending'"
        " CHECK(state IN ('pending', 'started', 'refused', 'unknown')),"
        "launched_at TEXT)"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_launches_pr_ordinal"
        " ON run_launches(repo, pr_number, ordinal)"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS settle_run_launch"
        " AFTER UPDATE OF status ON runs WHEN NEW.status != 'pending' BEGIN"
        " UPDATE run_launches SET"
        " state = CASE WHEN state != 'pending' THEN state"
        " WHEN NEW.status = 'refused' THEN 'refused' ELSE 'unknown' END"
        " WHERE run_id = NEW.run_id; END"
    )
    cursor.execute(
        "CREATE TRIGGER IF NOT EXISTS release_refused_launch_slot"
        " AFTER UPDATE OF state ON run_launches WHEN NEW.state = 'refused' BEGIN"
        " UPDATE run_launches SET ordinal = NULL, launched_at = NULL"
        " WHERE run_id = NEW.run_id; END"
    )


class RunLaunchStoreMixin:
    """Keep cleanup ownership separate from accounted repair slots."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: Any
        _cancellation_journal: CancellationJournal

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def register_ci_fix_attempt(
        self, run_id: str, pr_number: int, *, max_attempts: int | None = None
    ) -> None:
        """Register standalone history under a write lock; admission uses allocation."""
        with self._txn_lock:
            self._with_busy_retry(
                "register_ci_fix_attempt",
                partial(
                    self._register_ci_fix_transaction, run_id, pr_number, max_attempts
                ),
            )

    def allocate_ci_fix_attempt(
        self, run_id: str, pr_number: int, *, max_attempts: int
    ) -> None:
        """Join the caller's BEGIN IMMEDIATE transaction without committing it."""
        if not self._conn.in_transaction:
            raise RuntimeError("CI fix allocation requires an admission transaction")
        self._register_ci_fix_attempt(run_id, pr_number, max_attempts)

    def _register_ci_fix_transaction(
        self, run_id: str, pr_number: int, max_attempts: int | None
    ) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._register_ci_fix_attempt(run_id, pr_number, max_attempts)
            self._conn.commit()
        except BaseException as exc:
            kew.run_store._rollback_after_failure(self._conn, exc)
            raise

    def _register_ci_fix_attempt(
        self, run_id: str, pr_number: int, max_attempts: int | None
    ) -> None:
        if self._execute(
            "SELECT 1 FROM run_launches WHERE run_id = ?", (run_id,)
        ).fetchone():
            return
        row = self._execute(
            "SELECT repo FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None or not row[0]:
            raise ValueError("CI fix slot requires an admitted repository")
        repo = row[0]
        occupied = self._execute(
            "SELECT run_id FROM run_launches WHERE repo = ? AND pr_number = ?"
            " AND state != 'refused' ORDER BY ordinal IS NULL, ordinal, run_id",
            (repo, pr_number),
        ).fetchall()
        if max_attempts is not None and len(occupied) >= max_attempts:
            raise CiFixAttemptsExhausted("CI fix retry limit reached")
        # Refusal can free an earlier slot. Compact under the same write lock,
        # so count + 1 remains unique even when a later repair already started.
        # Recovered starts without an ordinal follow existing numbered slots.
        for ordinal, receipt in enumerate(occupied, 1):
            self._execute(
                "UPDATE run_launches SET ordinal = ? WHERE run_id = ?",
                (ordinal, receipt[0]),
            )
        self._execute(
            "INSERT INTO run_launches (run_id, repo, pr_number, ordinal)"
            " VALUES (?, ?, ?, ?)",
            (run_id, repo, pr_number, len(occupied) + 1),
        )

    def record_launch(self, run_id: str) -> None:
        """Record startup evidence; internal retries remain one repair."""
        self._with_busy_retry("record_launch", partial(self._record_launch, run_id))

    def _record_launch(self, run_id: str) -> None:
        with self._txn_lock, self._conn:
            self._execute(
                "INSERT INTO run_launches (run_id, repo, state, launched_at)"
                " SELECT run_id, repo, 'started', ? FROM runs"
                " WHERE run_id = ?"
                " ON CONFLICT(run_id) DO UPDATE SET state = 'started',"
                " launched_at = COALESCE(run_launches.launched_at,"
                " excluded.launched_at)",
                (kew.run_store._dt_to_iso(datetime.now(UTC)), run_id),
            )

    def refuse_launch(self, run_id: str) -> None:
        """Refuse unsettled startup without erasing an earlier execution."""
        self._with_busy_retry("refuse_launch", partial(self._refuse_launch, run_id))

    def _refuse_launch(self, run_id: str) -> None:
        with self._txn_lock, self._conn:
            self._execute(
                "UPDATE run_launches SET state = 'refused'"
                " WHERE run_id = ? AND state != 'started'",
                (run_id,),
            )

    def count_launched_ci_fix_attempts(self, repo: str, pr_number: int) -> int:
        """Count consumed repairs; pending receipts only occupy admission slots."""
        row = self._execute(
            "SELECT COUNT(*) FROM run_launches"
            " WHERE repo = ? AND pr_number = ? AND state IN ('started', 'unknown')",
            (repo, pr_number),
        ).fetchone()
        return int(row[0])

    def count_refused_ci_fix_attempts(self, repo: str, pr_number: int) -> int:
        """Count repairs whose container refused startup.

        A refusal releases its launch slot, so it never spends the repair
        budget. A container that refuses deterministically would therefore
        repair forever, because the escalation counts launches only (governed
        review round 6, 2026-09-22). Its own bound ends that.
        """
        row = self._execute(
            "SELECT COUNT(*) FROM run_launches"
            " WHERE repo = ? AND pr_number = ? AND state = 'refused'",
            (repo, pr_number),
        ).fetchone()
        return int(row[0])

    def finalize_cancelled_run(
        self, run_id: str, *, startup_state: StartupState
    ) -> bool:
        """Settle evidence and release the reservation with pending terminalization."""
        return self._with_busy_retry(
            "finalize_cancelled_run",
            partial(self._finalize_cancelled_run, run_id, startup_state),
        )

    def defer_cancelled_run(self, run_id: str, *, startup_state: StartupState) -> None:
        """Persist recovery even when SQLite cannot accept another write."""
        self._cancellation_journal.record(run_id, startup_state=startup_state)

    def reap_cancelled_runs(self) -> None:
        """Retry acknowledged cancellation without inferring process liveness."""
        for run_id, startup_state in self._cancellation_journal.pending():
            try:
                if self.finalize_cancelled_run(run_id, startup_state=startup_state):
                    self._cancellation_journal.remove(run_id)
                elif self._cancellation_replay_expired(run_id):
                    # This replay changed nothing. A marker recorded before its
                    # run row commits settles on a later pass (round 25), so a
                    # young one is kept. Past the bound nothing can arrive that
                    # would settle it -- the row never committed, or another
                    # actor terminalized it and the receipt already reflects
                    # this state -- and replaying it forever cost every
                    # admission and tick another immediate transaction and left
                    # its file behind (governed review rounds 2 and 3,
                    # 2026-09-21).
                    self._cancellation_journal.remove(run_id)
            except Exception:
                _logger.exception(
                    "Cancellation recovery failed for run %s; reservation run_id=%s",
                    run_id,
                    run_id,
                )

    def _cancellation_replay_expired(self, run_id: str) -> bool:
        """Whether a replay that changed nothing has run out of time to settle."""
        return (
            self._cancellation_journal.age_seconds(run_id)
            >= CANCELLATION_REPLAY_LIMIT_SECONDS
        )

    def _finalize_cancelled_run(self, run_id: str, startup_state: StartupState) -> bool:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                receipt_changed = self._write_cancelled_receipt(run_id, startup_state)
                changed = self._write_cancelled_run(
                    run_id, startup_state in ("started", "unknown")
                )
                self._execute(
                    "DELETE FROM epic_reservations WHERE run_id = ?", (run_id,)
                )
                self._conn.commit()
                return changed or receipt_changed
            except BaseException as exc:
                kew.run_store._rollback_after_failure(self._conn, exc)
                raise

    def _write_cancelled_receipt(
        self, run_id: str, startup_state: StartupState
    ) -> bool:
        """Apply late settled evidence while preserving cumulative execution."""
        state = "refused" if startup_state == "pending" else startup_state
        launched_at = (
            kew.run_store._dt_to_iso(datetime.now(UTC)) if state == "started" else None
        )
        cursor = self._execute(
            "INSERT INTO run_launches (run_id, repo, state, launched_at)"
            " SELECT run_id, repo, ?, ? FROM runs"
            " WHERE run_id = ?"
            " ON CONFLICT(run_id) DO UPDATE SET state = excluded.state,"
            " launched_at = COALESCE(run_launches.launched_at, excluded.launched_at)"
            " WHERE run_launches.state != 'started'"
            " AND run_launches.state != excluded.state",
            (state, launched_at, run_id),
        )
        return cursor.rowcount == 1

    def _write_cancelled_run(self, run_id: str, launched: bool) -> bool:
        finished = kew.run_store._dt_to_iso(datetime.now(UTC))
        cursor = self._execute(
            "UPDATE runs SET status = ?, exit_code = ?, error_category = ?,"
            " finished_at = ?, duration_seconds = MAX(0,"
            " (julianday(?) - julianday(started_at)) * 86400), pgid = NULL"
            " WHERE run_id = ? AND status = 'pending'",
            (
                "killed" if launched else "refused",
                130 if launched else 1,
                "execution_interrupted" if launched else "admission_hook_interrupted",
                finished,
                finished,
                run_id,
            ),
        )
        return cursor.rowcount == 1
