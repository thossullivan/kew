"""Recorded item plans: the ledger's copy of what an item asks for (#2290).

kew's adversarial reviewer is told to judge a diff against "the acceptance
criteria it claims to satisfy", but nothing ever recorded those criteria. This
mixin is where they live: one ``item_plans`` row per (item, body digest), with
the plan's canonical JSON, its sha, and how it was obtained.

The digest is load-bearing. A dispatch tick re-reads the same issue body over
and over, and re-recording it each time would turn the history into noise and
make "the plan changed" unreadable. So the current row is matched on
``body_digest``: the same body records nothing new, and a DIFFERENT body
supersedes the row the old body produced — ``superseded_at`` stamped, the row
kept. The history is append-only; a reverted body records a fresh row rather
than reviving the one it matches, because what matters is when the host read it.

The ledger is the record (epic rule): every row here is written by the host
from a body it fetched itself, never read back from a file an agent can write.
Acceptance of a plan is a separate, human act and lands in ``approved_by``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

import kew.run_store

if TYPE_CHECKING:
    from kew.run_store import ItemPlanRow, ItemPlanWrite

_T = TypeVar("_T")

PLAN_SOURCES = frozenset({"extracted", "model", "human"})
"""How a recorded plan was obtained.

``extracted`` — read out of the body's own acceptance section by
:func:`kew.triage.plan.extract_criteria`. ``model`` — drafted by a classifier.
``human`` — written or edited by a person. Anything else is refused rather than
stored: a reader that cannot tell a drafted plan from an extracted one cannot
tell whether a person ever saw it.
"""


class ItemPlanStoreMixin:
    """The recorded-plan persistence surface of ``SqliteRunStore``.

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

    def record_item_plan(
        self,
        item_id: str,
        *,
        body_digest: str,
        source: str,
        plan_sha: str,
        plan_json: str,
        criteria_count: int,
        run_id: str | None = None,
        approved_by: str | None = None,
    ) -> ItemPlanWrite:
        """Record *item_id*'s plan for the body *body_digest* identifies.

        Returns an :class:`~kew.run_store.ItemPlanWrite` naming the row that is
        now current, whether this call wrote it, and the row it superseded. A
        caller emits ``plan_recorded`` / ``plan_superseded`` from exactly that,
        so an unchanged body emits nothing.

        The read of the current row and the supersede+insert happen inside one
        ``BEGIN IMMEDIATE`` transaction: two dispatch ticks observing the same
        edited body concurrently would otherwise both see "no current row for
        this digest" and both insert (check-then-act on shared state — the
        ``concurrent-state-change`` skill).
        """
        if source not in PLAN_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(PLAN_SOURCES)}, not {source!r}"
            )
        if not isinstance(criteria_count, int) or criteria_count < 0:
            raise ValueError(f"criteria_count must be a count, not {criteria_count!r}")
        recorded_at = datetime.now(UTC).astimezone(UTC).isoformat()

        def _do() -> ItemPlanWrite:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    write = self._record_item_plan_locked(
                        item_id,
                        body_digest=body_digest,
                        source=source,
                        plan_sha=plan_sha,
                        plan_json=plan_json,
                        criteria_count=criteria_count,
                        run_id=run_id,
                        approved_by=approved_by,
                        recorded_at=recorded_at,
                    )
                    self._conn.commit()
                    return write
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("record_item_plan", _do)

    def _record_item_plan_locked(
        self,
        item_id: str,
        *,
        body_digest: str,
        source: str,
        plan_sha: str,
        plan_json: str,
        criteria_count: int,
        run_id: str | None,
        approved_by: str | None,
        recorded_at: str,
    ) -> ItemPlanWrite:
        """The supersede-then-insert body of :meth:`record_item_plan`.

        Runs inside the caller's open transaction.
        """
        current_row = self._current_item_plan_row(self._conn, item_id)
        if current_row is not None and current_row.body_digest == body_digest:
            return kew.run_store.ItemPlanWrite(
                current=current_row, recorded=False, superseded=None
            )
        if current_row is not None:
            self._conn.execute(
                "UPDATE item_plans SET superseded_at = ?"
                " WHERE item_id = ? AND superseded_at IS NULL",
                (recorded_at, item_id),
            )
        cursor = self._conn.execute(
            "INSERT INTO item_plans"
            " (item_id, run_id, body_digest, source, plan_sha, plan_json,"
            "  criteria_count, approved_by, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                run_id,
                body_digest,
                source,
                plan_sha,
                plan_json,
                criteria_count,
                approved_by,
                recorded_at,
            ),
        )
        inserted = self._item_plan_by_id(self._conn, int(cursor.lastrowid or 0))
        superseded = (
            None
            if current_row is None
            else self._item_plan_by_id(self._conn, current_row.id)
        )
        return kew.run_store.ItemPlanWrite(
            current=inserted, recorded=True, superseded=superseded
        )

    @staticmethod
    def _current_item_plan_row(
        conn: sqlite3.Connection, item_id: str
    ) -> ItemPlanRow | None:
        """The newest un-superseded row for *item_id*, read over *conn*.

        Takes the connection explicitly so the transactional writer and the
        public reader below share one query instead of two that could drift.
        """
        row = conn.execute(
            "SELECT * FROM item_plans WHERE item_id = ?"
            " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return None if row is None else kew.run_store._row_to_item_plan(row)

    @staticmethod
    def _item_plan_by_id(conn: sqlite3.Connection, row_id: int) -> ItemPlanRow:
        row = conn.execute(
            "SELECT * FROM item_plans WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"item plan row {row_id} vanished mid-transaction")
        return kew.run_store._row_to_item_plan(row)

    def current_item_plan(self, item_id: str) -> ItemPlanRow | None:
        """*item_id*'s plan as it stands, or None when none was ever recorded."""
        return self._current_item_plan_row(self._conn, item_id)

    def list_item_plans(self, item_id: str) -> list[ItemPlanRow]:
        """*item_id*'s full plan history, oldest first, superseded rows kept."""
        rows = self._execute(
            "SELECT * FROM item_plans WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall()
        return [kew.run_store._row_to_item_plan(row) for row in rows]
