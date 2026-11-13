"""Recorded triage decisions: the ledger's copy of what triage proposed (#2293).

One ``triage_decisions`` row per (item, body digest): the classifier's identity
(backend, model, prompt sha), its decision, the outcome the policy derived from
it, and — later, when a person runs ``kew triage decide`` — how it was resolved
and by whom.

The digest is load-bearing, exactly as in :mod:`kew.run_store_item_plans`: the
same body re-triaged records nothing new, and an EDITED body supersedes the row
the old body produced (``superseded_at`` stamped, the row kept). History is
append-only, so "what did triage say about this item in September" always has
an answer.

Two rules the schema enforces rather than hopes for:

* **A proposal is not a decision.** ``resolution``, ``resolved_by`` and
  ``resolved_at`` are NULL until a PERSON resolves the row. There is no code
  path in kew that fills them from a model: :meth:`resolve_triage_decision`
  requires an actor, and its only caller is the CLI command a person runs.
* **An unavailable row is still a row.** A classifier that could not be reached
  records ``outcome = "unavailable"`` with a NULL ``type``, so "triage could not
  read this item" is a fact on the ledger instead of an absence that looks like
  "nobody has triaged it yet".
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

import kew.run_store

if TYPE_CHECKING:
    from kew.run_store import TriageDecisionRow, TriageDecisionWrite

_T = TypeVar("_T")

TRIAGE_RESOLUTIONS = frozenset({"dispatch", "ask", "close_duplicate", "decline"})
"""What a person can decide about a proposal.

``dispatch`` — the item becomes work. ``ask`` — the person asked the item's
author. ``close_duplicate`` — the person closed it against the duplicate.
``decline`` — the person said no. Each is recorded with the actor who chose it;
none of them is reachable without one.
"""

_DECISION_COLS = (
    "item_id",
    "source_tracker",
    "issue_num",
    "run_id",
    "body_digest",
    "backend",
    "model",
    "prompt_sha",
    "type",
    "priority",
    "complexity",
    "confidence",
    "actionable",
    "needs_info",
    "duplicate_of",
    "outcome",
    "decision_json",
    "recorded_at",
)


class TriageDecisionStoreMixin:
    """The recorded-triage persistence surface of ``SqliteRunStore``.

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

    def record_triage_decision(
        self,
        item_id: str,
        *,
        body_digest: str,
        backend: str,
        model: str,
        prompt_sha: str,
        outcome: str,
        decision_json: str,
        source_tracker: str | None = None,
        issue_num: int | None = None,
        run_id: str | None = None,
        decision_type: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        confidence: float | None = None,
        actionable: bool | None = None,
        needs_info: bool | None = None,
        duplicate_of: int | None = None,
    ) -> TriageDecisionWrite:
        """Record triage's proposal about the body *body_digest* identifies.

        Returns a :class:`~kew.run_store.TriageDecisionWrite` naming the row
        that is now current, whether this call wrote it, and the row it
        superseded — so the caller emits ``triage_decided`` from exactly that
        and a re-triage of an unchanged body emits nothing.

        The read of the current row and the supersede+insert happen inside one
        ``BEGIN IMMEDIATE`` transaction: two triage runs over the same edited
        body would otherwise both see "no current row for this digest" and both
        insert (check-then-act on shared state — the
        ``concurrent-state-change`` skill).
        """
        if outcome not in kew.run_store.TRIAGE_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {sorted(kew.run_store.TRIAGE_OUTCOMES)},"
                f" not {outcome!r}"
            )
        recorded_at = datetime.now(UTC).astimezone(UTC).isoformat()
        values = (
            item_id,
            source_tracker,
            issue_num,
            run_id,
            body_digest,
            backend,
            model,
            prompt_sha,
            decision_type,
            priority,
            complexity,
            confidence,
            None if actionable is None else int(actionable),
            None if needs_info is None else int(needs_info),
            duplicate_of,
            outcome,
            decision_json,
            recorded_at,
        )

        def _do() -> TriageDecisionWrite:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    write = self._record_triage_decision_locked(
                        item_id,
                        body_digest=body_digest,
                        values=values,
                        recorded_at=recorded_at,
                    )
                    self._conn.commit()
                    return write
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("record_triage_decision", _do)

    def _record_triage_decision_locked(
        self,
        item_id: str,
        *,
        body_digest: str,
        values: tuple[object, ...],
        recorded_at: str,
    ) -> TriageDecisionWrite:
        """The supersede-then-insert body of :meth:`record_triage_decision`.

        Runs inside the caller's open transaction.
        """
        current_row = self._current_triage_row(self._conn, item_id)
        if current_row is not None and current_row.body_digest == body_digest:
            return kew.run_store.TriageDecisionWrite(
                current=current_row, recorded=False, superseded=None
            )
        if current_row is not None:
            self._conn.execute(
                "UPDATE triage_decisions SET superseded_at = ?"
                " WHERE item_id = ? AND superseded_at IS NULL",
                (recorded_at, item_id),
            )
        placeholders = ", ".join("?" for _ in _DECISION_COLS)
        cursor = self._conn.execute(
            f"INSERT INTO triage_decisions ({', '.join(_DECISION_COLS)})"  # nosec B608 — column names are a module constant
            f" VALUES ({placeholders})",
            values,
        )
        inserted = self._triage_row_by_id(self._conn, int(cursor.lastrowid or 0))
        superseded = (
            None
            if current_row is None
            else self._triage_row_by_id(self._conn, current_row.id)
        )
        return kew.run_store.TriageDecisionWrite(
            current=inserted, recorded=True, superseded=superseded
        )

    def resolve_triage_decision(
        self, row_id: int, *, resolution: str, resolved_by: str
    ) -> TriageDecisionRow | None:
        """Record that a PERSON decided *row_id*. None when there is no such row.

        *resolved_by* is required and must be non-empty: a resolution with no
        actor is indistinguishable from the station resolving its own proposal,
        which is the one thing this station must never be able to do.

        Idempotent by refusal rather than by overwrite — an already-resolved row
        comes back unchanged, so a second ``kew triage decide`` cannot rewrite
        who decided or what they chose.
        """
        if resolution not in TRIAGE_RESOLUTIONS:
            raise ValueError(
                f"resolution must be one of {sorted(TRIAGE_RESOLUTIONS)},"
                f" not {resolution!r}"
            )
        if not resolved_by.strip():
            raise ValueError(
                "resolved_by is required: only a person resolves a proposal"
            )
        resolved_at = datetime.now(UTC).astimezone(UTC).isoformat()

        def _do() -> TriageDecisionRow | None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._triage_row_or_none(self._conn, row_id)
                    if row is None:
                        self._conn.commit()
                        return None
                    if row.resolution is None:
                        self._conn.execute(
                            "UPDATE triage_decisions SET resolution = ?,"
                            " resolved_by = ?, resolved_at = ? WHERE id = ?",
                            (resolution, resolved_by, resolved_at, row_id),
                        )
                    updated = self._triage_row_by_id(self._conn, row_id)
                    self._conn.commit()
                    return updated
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("resolve_triage_decision", _do)

    @staticmethod
    def _current_triage_row(
        conn: sqlite3.Connection, item_id: str
    ) -> TriageDecisionRow | None:
        """The newest un-superseded row for *item_id*, read over *conn*.

        Takes the connection explicitly so the transactional writer and the
        public reader below share one query instead of two that could drift.
        """
        row = conn.execute(
            "SELECT * FROM triage_decisions WHERE item_id = ?"
            " AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return None if row is None else kew.run_store._row_to_triage_decision(row)

    @staticmethod
    def _triage_row_or_none(
        conn: sqlite3.Connection, row_id: int
    ) -> TriageDecisionRow | None:
        row = conn.execute(
            "SELECT * FROM triage_decisions WHERE id = ?", (row_id,)
        ).fetchone()
        return None if row is None else kew.run_store._row_to_triage_decision(row)

    @classmethod
    def _triage_row_by_id(
        cls, conn: sqlite3.Connection, row_id: int
    ) -> TriageDecisionRow:
        row = cls._triage_row_or_none(conn, row_id)
        if row is None:
            raise RuntimeError(f"triage decision row {row_id} vanished mid-transaction")
        return row

    def current_triage_decision(self, item_id: str) -> TriageDecisionRow | None:
        """*item_id*'s triage proposal as it stands, or None if never triaged."""
        return self._current_triage_row(self._conn, item_id)

    def list_triage_decisions(self, item_id: str) -> list[TriageDecisionRow]:
        """*item_id*'s full triage history, oldest first, superseded rows kept."""
        rows = self._execute(
            "SELECT * FROM triage_decisions WHERE item_id = ? ORDER BY id", (item_id,)
        ).fetchall()
        return [kew.run_store._row_to_triage_decision(row) for row in rows]

    def list_unresolved_triage_decisions(self) -> list[TriageDecisionRow]:
        """Every current proposal no person has decided yet, oldest first.

        Superseded rows are excluded: a proposal about a body nobody has now is
        not a thing anybody can act on. This is what ``kew triage queue`` prints
        and what the needs-you set counts.
        """
        rows = self._execute(
            "SELECT * FROM triage_decisions WHERE superseded_at IS NULL"
            " AND resolution IS NULL ORDER BY id"
        ).fetchall()
        return [kew.run_store._row_to_triage_decision(row) for row in rows]
