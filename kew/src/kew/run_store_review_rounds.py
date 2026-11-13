"""Review-round store operations, extracted from ``run_store`` (Task 4).

This module owns the review-round persistence surface of ``SqliteRunStore``:
the cycle-aware recorder (``record_review_round`` and its ``BEGIN IMMEDIATE``
next-round derivation), the cycle counters/readers (``review_round_count``,
``latest_review_round``), the full append-only history (``list_review_rounds``),
and the cycle boundary itself (``review_cycle_start``, over the private
``_review_cycle_start``). It is a pure seam extraction: the cycle-supersession
semantics (rounds recorded at/after the node's latest ``epic_retries`` marker
are the current cycle) are UNCHANGED.

``SqliteRunStore`` mixes :class:`ReviewRoundStoreMixin` in. The module-level
row builder ``_row_to_review_round`` stays in ``run_store`` and is reached here
as an attribute of the ``kew.run_store`` module at call time, which also breaks
the import cycle (``run_store`` imports this mixin at module load).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

import kew.run_store

if TYPE_CHECKING:
    from kew.run_store import ReviewRound

_T = TypeVar("_T")

_READY = "READY"
"""The reviewer verdict that reads as merge-ready on its own."""


class ReviewRoundStoreMixin:
    """The review-round persistence surface of ``SqliteRunStore``.

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

    def _review_cycle_start(self, item_id: str) -> str | None:
        """The current review cycle's lower bound: the node's latest
        ``epic_retries`` marker, or None when it has never been retried.

        A ``kew epic retry`` grants a fresh review allowance (F15), so rounds
        recorded at/after the marker are the current cycle; earlier rounds are
        superseded history. This mirrors the failure supersession
        ``overlay_status`` derives from the same marker.
        """
        row = self._execute(
            "SELECT retried_at FROM epic_retries WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row["retried_at"] if row is not None else None

    def review_cycle_start(self, item_id: str) -> str | None:
        """The current review cycle's lower bound, as the stored ISO stamp.

        The public form of :meth:`_review_cycle_start`, for readers that must
        place a fact recorded elsewhere (an ``adversarial_review_verdict`` row
        on the audit chain, say) in the current cycle rather than in the
        superseded history a retry left behind. ``None`` means the node has
        never been retried, so its whole history is the current cycle.
        """
        return self._review_cycle_start(item_id)

    def record_review_round(
        self,
        epic_ref: str,
        item_id: str,
        *,
        head_sha: str,
        verdict: str,
        sidecar_digest: str | None = None,
        sidecar_path: str | None = None,
        review_mode: str | None = None,
        plan_sha: str | None = None,
        criteria_json: str | None = None,
        prompt_profile_version_id: int | None = None,
    ) -> int:
        """Append one review round and return its 1-based number in the cycle.

        ``sidecar_path`` (#1030) records where this round's collected sidecar is
        stored on the host, so the NEEDS_CHANGES remediation leg — which runs on
        a later tick from persisted state — can load the findings.
        ``review_mode`` (#2111) records ``full`` or ``delta``.
        ``plan_sha`` / ``criteria_json`` (#2290) record which recorded plan the
        reviewer was judged against and the criteria text it received. Both stay
        None until the reviewer is actually handed the criteria: a round that
        was given nothing must read as having been given nothing.
        ``prompt_profile_version_id`` (#3024) names the operator addendum the
        reviewer prompt carried, None when it ran the bare base prompt.

        The next round number is derived and the row inserted inside a single
        ``BEGIN IMMEDIATE`` transaction so a concurrent recorder cannot slip
        between the count and the insert (check-then-act on shared state —
        ``concurrent-state-change`` skill; the reservation UNIQUE index already
        makes two live reviewers on one node impossible, this is defence in
        depth). ``round`` restarts at 1 each cycle (post-retry).
        """
        cycle_start = self._review_cycle_start(item_id)
        recorded_at = kew.run_store._dt_to_iso(datetime.now(UTC))

        def _do() -> int:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if cycle_start is None:
                        row = self._conn.execute(
                            "SELECT COALESCE(MAX(round), 0) + 1 FROM review_rounds"
                            " WHERE item_id = ?",
                            (item_id,),
                        ).fetchone()
                    else:
                        row = self._conn.execute(
                            "SELECT COALESCE(MAX(round), 0) + 1 FROM review_rounds"
                            " WHERE item_id = ? AND recorded_at >= ?",
                            (item_id, cycle_start),
                        ).fetchone()
                    next_round = int(row[0])
                    self._conn.execute(
                        "INSERT INTO review_rounds"
                        " (epic_ref, item_id, round, head_sha, verdict,"
                        "  sidecar_digest, recorded_at, sidecar_path,"
                        "  review_mode, plan_sha, criteria_json,"
                        "  prompt_profile_version_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            epic_ref,
                            item_id,
                            next_round,
                            head_sha,
                            verdict,
                            sidecar_digest,
                            recorded_at,
                            sidecar_path,
                            review_mode,
                            plan_sha,
                            criteria_json,
                            prompt_profile_version_id,
                        ),
                    )
                    self._conn.commit()
                    return next_round
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("record_review_round", _do)

    def accept_review_round(
        self,
        item_id: str,
        *,
        round_num: int,
        head_sha: str,
        sidecar_digest: str | None,
        verdict: str,
    ) -> bool:
        """Record *verdict* as the driver's accepted disposition for one round.

        The review stop rule (#2112) accepts a late low/medium-only
        ``NEEDS_CHANGES`` round as READY-with-residuals. That acceptance used to
        live on the audit chain alone, so a watcher reading recorded rounds saw
        ``NEEDS_CHANGES`` for a node that was already merge-ready (#2151). This
        writes it beside the round WITHOUT touching ``verdict``: the reviewer's
        verdict stays exactly as returned, and ``accepted_verdict`` is the
        driver's separate fact about it.

        Binds to the round, its head SHA and its sidecar digest, within the
        current review cycle — the same binding the chain event carries — so an
        acceptance can never mark a different round, head or cycle. Returns
        False when no such round is on record: the caller must treat that as the
        acceptance NOT being durable rather than assume the row it meant.
        """
        cycle_start = self._review_cycle_start(item_id)

        def _do() -> bool:
            with self._txn_lock:
                try:
                    cursor = self._conn.execute(
                        "UPDATE review_rounds SET accepted_verdict = ?"
                        " WHERE item_id = ? AND round = ? AND head_sha = ?"
                        "  AND sidecar_digest IS ?"
                        "  AND (? IS NULL OR recorded_at >= ?)",
                        (
                            verdict,
                            item_id,
                            round_num,
                            head_sha,
                            sidecar_digest,
                            cycle_start,
                            cycle_start,
                        ),
                    )
                    self._conn.commit()
                    return cursor.rowcount > 0
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("accept_review_round", _do)

    def merge_ready_review_rounds(self, item_id: str) -> list[ReviewRound]:
        """The current cycle's rounds whose recorded disposition reads merge-ready.

        A reviewer ``READY``, or a round the stop rule accepted as
        READY-with-residuals (#2151) — the two dispositions the merge gate treats
        alike. This is the ledger's verdict-level reading, which is what a
        watcher polls; the gate itself adds the head binding and the approval
        ordering before it merges.
        """
        cycle_start = self._review_cycle_start(item_id)
        rows = self._execute(
            "SELECT * FROM review_rounds WHERE item_id = ?"
            " AND (verdict = ? OR accepted_verdict IS NOT NULL)"
            " AND (? IS NULL OR recorded_at >= ?) ORDER BY id",
            (item_id, _READY, cycle_start, cycle_start),
        ).fetchall()
        return [kew.run_store._row_to_review_round(row) for row in rows]

    def review_items_at_head(self, head_sha: str) -> list[str]:
        """The nodes with a review round recorded against *head_sha*, by first round.

        The merge queue's admission reads a PR head's verdict from here: the
        head is the code the reviewer read, whichever node it was recorded for.
        """
        rows = self._execute(
            "SELECT item_id FROM review_rounds WHERE head_sha = ?"
            " GROUP BY item_id ORDER BY MIN(id)",
            (head_sha,),
        ).fetchall()
        return [str(row["item_id"]) for row in rows]

    def review_round_count(self, item_id: str) -> int:
        """Rounds recorded in the current review cycle (post-latest-retry)."""
        cycle_start = self._review_cycle_start(item_id)
        if cycle_start is None:
            row = self._execute(
                "SELECT COUNT(*) FROM review_rounds WHERE item_id = ?", (item_id,)
            ).fetchone()
        else:
            row = self._execute(
                "SELECT COUNT(*) FROM review_rounds"
                " WHERE item_id = ? AND recorded_at >= ?",
                (item_id, cycle_start),
            ).fetchone()
        return int(row[0])

    def latest_review_round(self, item_id: str) -> ReviewRound | None:
        """The newest round in the current cycle, or None (post-retry/never)."""
        cycle_start = self._review_cycle_start(item_id)
        if cycle_start is None:
            row = self._execute(
                "SELECT * FROM review_rounds WHERE item_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        else:
            row = self._execute(
                "SELECT * FROM review_rounds WHERE item_id = ? AND recorded_at >= ?"
                " ORDER BY id DESC LIMIT 1",
                (item_id, cycle_start),
            ).fetchone()
        return kew.run_store._row_to_review_round(row) if row is not None else None

    def review_rounds_in_cycle(self, item_id: str) -> list[ReviewRound]:
        """The current cycle's rounds (post-latest-retry), oldest first."""
        cycle_start = self._review_cycle_start(item_id)
        if cycle_start is None:
            rows = self._execute(
                "SELECT * FROM review_rounds WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM review_rounds WHERE item_id = ? AND recorded_at >= ?"
                " ORDER BY id",
                (item_id, cycle_start),
            ).fetchall()
        return [kew.run_store._row_to_review_round(row) for row in rows]

    def list_review_rounds(self, item_id: str) -> list[ReviewRound]:
        """Full append-only round history for a node, oldest first — cycles
        included, so a retry's reset stays visible in the persisted record."""
        rows = self._execute(
            "SELECT * FROM review_rounds WHERE item_id = ? ORDER BY id",
            (item_id,),
        ).fetchall()
        return [kew.run_store._row_to_review_round(row) for row in rows]

    def list_review_rounds_for_epic(self, epic_ref: str) -> list[ReviewRound]:
        """Every round recorded under *epic_ref*, oldest first, in ONE query.

        The epic timeline plots the verdicts of every node at once (#2270);
        reading them node by node would be one query per lane. Rounds are
        stored with the epic they were recorded under, so this is the whole
        epic's review history — cycles included, same as
        :meth:`list_review_rounds`.
        """
        rows = self._execute(
            "SELECT * FROM review_rounds WHERE epic_ref = ? ORDER BY id",
            (epic_ref,),
        ).fetchall()
        return [kew.run_store._row_to_review_round(row) for row in rows]
