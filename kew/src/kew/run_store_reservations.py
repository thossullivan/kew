"""Epic-reservation store operations, extracted from ``run_store`` (Task 2).

This module owns the epic-DAG node reservation surface of ``SqliteRunStore``:
the atomic reserve idioms (``reserve_epic_node_item`` and the ``BEGIN
IMMEDIATE`` cap-admission ``reserve_epic_node_item_under_cap``), the run_id-keyed
release (``release_epic_node_item`` — #939/#972/#982), the GitHub int-shims, and
the reservation listing. It is a pure seam extraction: the concurrency semantics
(the ``BEGIN IMMEDIATE`` fold of count-and-claim, the generation-conditional
DELETE) are UNCHANGED.

``SqliteRunStore`` mixes :class:`EpicReservationStoreMixin` in. The shared
row/id/timestamp helpers stay in ``run_store`` (they are used by the epic-hold
and epic-retry surfaces too); they are reached here as attributes of the
``kew.run_store`` module at call time, which also breaks the import cycle
(``run_store`` imports this mixin at module load).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import kew.run_store

if TYPE_CHECKING:
    from kew.run_store import (
        ContinuationClaimResult,
        EpicReservation,
        ReserveResult,
    )
    from kew.run_store_liveness import DeadPending

_T = TypeVar("_T")


def _require_recorded_cancellation(
    inserted: int | None, outcome: str | None, run_id: str | None
) -> None:
    """A dropped cancellation write must roll back its claim release."""
    if outcome == "claim_undone" and inserted is None:
        raise RuntimeError(
            f"continuation claim {run_id!r} cancellation was not durably recorded"
        )


class EpicReservationStoreMixin:
    """The epic-node reservation surface of ``SqliteRunStore``.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the in-process transaction lock, the busy-retry / execute
    wrappers, and the live-run counter declared below.
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

        def _count_live_runs(
            self,
            exclude_item: str | None,
            *,
            dead_pending: list[DeadPending] | None = None,
        ) -> int: ...

        def _prepare_admission(self) -> list[DeadPending]: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def get(self, run_id: str) -> kew.run_store.RunRecord | None: ...

        def _continuation_worktree_key(
            self,
            epic_ref: str,
            item_id: str,
            original_run_id: str,
            branch: str,
            worktree: str | Path,
        ) -> str: ...

        def _bind_continuation_checkout(
            self,
            record: kew.run_store.ContinuationRecord,
            generation: str,
            *,
            inserted: bool,
        ) -> None: ...

        def _continuation_run_launched(self, run_id: str) -> bool: ...

        def continuation_is_recoverable(
            self, record: kew.run_store.ContinuationRecord
        ) -> bool: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def _mark_epic_retry_item_in_transaction(
            self, epic_ref: str, item_id: str, retried_at: datetime
        ) -> None: ...

    def _attest_continuation_transition(
        self,
        *,
        event: str,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree_identity: str,
        generation: int,
        continuation_run_id: str | None,
        reason: str,
        actor_id: str | None = None,
        repo: str | None = None,
        outcome: str | None = None,
    ) -> None:
        """Append transition evidence inside the caller's state transaction."""
        from kew.audit_identity import (
            continuation_evidence,
            continuation_evidence_identity,
        )

        abandoning = event == "continuation_abandoned"
        payload = continuation_evidence(
            phase="abandon" if abandoning else "release",
            epic_ref=epic_ref,
            item_id=item_id,
            original_run_id=original_run_id,
            continuation_run_id=continuation_run_id,
            branch=branch,
            worktree_identity=worktree_identity,
            generation=generation,
            state="terminal" if abandoning else "awaiting_continuation",
            outcome=outcome or ("abandoned" if abandoning else "released"),
        )
        payload["reason"] = reason
        if outcome == "claim_undone":
            payload["restored_generation"] = generation - 1
        payload["node"] = payload["item_id"]
        payload["evidence_identity"] = continuation_evidence_identity(event, payload)
        inserted = self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                event,
                continuation_run_id or original_run_id,
                kew.run_store._epic_db_issue_num(item_id),
                repo,
                "human" if abandoning else "policy",
                actor_id or "continuation_release",
                None,
                None,
                json.dumps(payload),
                item_id,
            )
        )
        _require_recorded_cancellation(inserted, outcome, continuation_run_id)

    def _attest_continuation_claim(
        self,
        record: kew.run_store.ContinuationRecord,
        run_id: str,
        evidence: dict[str, Any],
        *,
        actor_id: str,
        repo: str | None,
    ) -> None:
        """Validate the caller's family payload and append before claim commit."""
        from kew.audit_identity import (
            continuation_evidence,
            continuation_evidence_identity,
        )

        expected = continuation_evidence(
            phase="claim",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            continuation_run_id=run_id,
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            generation=record.generation + 1,
            state="continuing",
            outcome="continuing",
        )
        payload = dict(evidence)
        if payload != expected:
            raise ValueError(
                "continuation claim evidence does not match the claimed generation"
            )
        identity = continuation_evidence_identity("continuation_claimed", payload)
        payload["node"] = payload["item_id"]
        payload["evidence_identity"] = identity
        inserted = self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                "continuation_claimed",
                run_id,
                kew.run_store._epic_db_issue_num(record.item_id),
                repo,
                "policy",
                actor_id,
                None,
                None,
                json.dumps(payload),
                record.item_id,
            ),
            dedup_key=identity,
        )
        if inserted is None:
            raise RuntimeError("continuation claim evidence was not durably recorded")

    def reserve_epic_node_item(self, epic_ref: str, item_id: str, run_id: str) -> bool:
        """Atomically reserve a DAG node keyed on the durable ``item_id`` (L3).

        Uses a plain ``INSERT`` and catches the ``IntegrityError`` raised by the
        UNIQUE ``item_id`` index on a second caller — deliberately NOT a
        check-then-insert (that races). A GitHub id stores its numeric
        ``issue_num``; a non-numeric id stores ``issue_num=NULL`` (an auto-rowid,
        distinct per row) so multiple Linear items never collide on a sentinel.
        The epic driver calls this instead of ``try_claim_issue``.
        """
        issue_num = kew.run_store._epic_db_issue_num(item_id)

        def _do_reserve() -> bool:
            try:
                self._execute(
                    "INSERT INTO epic_reservations"
                    " (issue_num, epic_ref, run_id, reserved_at, item_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        issue_num,
                        epic_ref,
                        run_id,
                        kew.run_store._dt_to_iso(datetime.now(UTC)),
                        item_id,
                    ),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            self._conn.commit()
            return True

        return self._with_busy_retry("reserve_epic_node_item", _do_reserve)

    def append_audit_event_reserving_item(
        self,
        epic_ref: str,
        item_id: str,
        run_id: str,
        *,
        event: str,
        actor_type: str,
        actor_id: str,
        repo: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """:meth:`reserve_epic_node_item` and an audit event, in one commit.

        The event is written under *run_id* and *item_id*. No reader ever sees
        the reservation without the event that names its holder, and an event
        that cannot be written leaves no reservation behind (#3208). ``False``
        when another generation already holds *item_id*: nothing is written.
        """
        return self._with_busy_retry(
            "append_audit_event_reserving_item",
            lambda: self._reserve_with_event(
                epic_ref,
                item_id,
                run_id,
                event=event,
                actor_type=actor_type,
                actor_id=actor_id,
                repo=repo,
                payload=payload,
            ),
        )

    def _reserve_with_event(
        self,
        epic_ref: str,
        item_id: str,
        run_id: str,
        *,
        event: str,
        actor_type: str,
        actor_id: str,
        repo: str | None,
        payload: dict[str, Any],
    ) -> bool:
        from kew.run_store_audit import audit_values

        now = datetime.now(UTC)
        values = audit_values(
            timestamp=now.isoformat(),
            event=event,
            run_id=run_id,
            item_id=item_id,
            repo=repo,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
        )
        with self._txn_lock:
            try:
                # The write lock is held from here to the commit, so the
                # holder read below is the one the insert would collide with.
                self._conn.execute("BEGIN IMMEDIATE")
                held = self._execute(
                    "SELECT 1 FROM epic_reservations WHERE item_id = ?", (item_id,)
                ).fetchone()
                if held is None:
                    self._execute(
                        "INSERT INTO epic_reservations"
                        " (issue_num, epic_ref, run_id, reserved_at, item_id)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            kew.run_store._epic_db_issue_num(item_id),
                            epic_ref,
                            run_id,
                            kew.run_store._dt_to_iso(now),
                            item_id,
                        ),
                    )
                    self._chained_insert_in_transaction(values)
                # Inside the try: a commit that fails (disk full, I/O) must
                # roll back too, never leave the write transaction open.
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return held is None

    def register_continuation_and_release_reservation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        reservation_run_id: str,
        *,
        checkout_generation: str,
        precommit_check: Callable[[], None] | None = None,
    ) -> kew.run_store.ContinuationRecord | None:
        """Register one exact continuation and release one exact reservation.

        The continuation insert is idempotent, but the reservation delete is
        conditional on the caller's ``reservation_run_id`` generation. If
        that generation is gone, the whole transaction rolls back, including
        a would-be continuation insert, so a stale caller cannot leave a
        recovery marker behind after losing the node reservation.
        """
        now = kew.run_store._dt_to_iso(datetime.now(UTC))
        assert now is not None

        def _do_register() -> kew.run_store.ContinuationRecord | None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    worktree_identity = self._continuation_worktree_key(
                        epic_ref, item_id, original_run_id, branch, worktree
                    )
                    inserted = self._execute(
                        "INSERT INTO epic_continuations"
                        " (epic_ref, item_id, original_run_id, branch,"
                        "  worktree_identity, state, generation,"
                        "  continuation_run_id, created_at, updated_at,"
                        "  claimed_at, terminal_at)"
                        " VALUES (?, ?, ?, ?, ?, 'awaiting_continuation',"
                        " 0, NULL, ?, ?, NULL, NULL)"
                        " ON CONFLICT(epic_ref, item_id, original_run_id, branch,"
                        " worktree_identity) DO NOTHING",
                        (
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                            now,
                            now,
                        ),
                    )
                    row = self._execute(
                        "SELECT epic_ref, item_id, original_run_id, branch,"
                        " worktree_identity, state, generation,"
                        " continuation_run_id, created_at, updated_at,"
                        " claimed_at, terminal_at FROM epic_continuations"
                        " WHERE epic_ref = ? AND item_id = ?"
                        " AND original_run_id = ? AND branch = ?"
                        " AND worktree_identity = ?",
                        (
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                        ),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError(
                            "continuation registration was not persisted"
                        )
                    self._bind_continuation_checkout(
                        kew.run_store._row_to_continuation(row),
                        checkout_generation,
                        inserted=inserted.rowcount == 1,
                    )
                    released = self._execute(
                        "DELETE FROM epic_reservations"
                        " WHERE epic_ref = ? AND item_id = ? AND run_id = ?",
                        (epic_ref, item_id, reservation_run_id),
                    )
                    if released.rowcount != 1:
                        kew.run_store._invoke_precommit_check(precommit_check)
                        kew.run_store._rollback_for_outcome(self._conn)
                        return None
                    kew.run_store._invoke_precommit_check(precommit_check)
                    self._conn.commit()
                    return kew.run_store._row_to_continuation(row)
                except BaseException as exc:
                    kew.run_store._rollback_after_failure(self._conn, exc)
                    raise

        try:
            return self._with_busy_retry(
                "register_continuation_and_release_reservation", _do_register
            )
        except kew.run_store._PrecommitCheckError as exc:
            kew.run_store._raise_precommit_error(exc)

    def reserve_epic_node_item_under_cap(
        self, epic_ref: str, item_id: str, run_id: str, cap: int
    ) -> ReserveResult:
        """Atomically reserve a node only if the fleet is below ``cap`` (N10).

        The delegation admit path read ``live_run_count()`` and THEN reserved
        (``delegate/run.py``), so two concurrent surfaces (watcher + driver, or
        two watchers) could each read the same sub-cap count and each reserve,
        pushing the fleet past ``[parallel] max_workers``. This folds the count
        and the slot claim into one ``BEGIN IMMEDIATE`` transaction — the exact
        idiom :meth:`try_admit_run` uses for the budget cap — so a second
        contender cannot slip between the check and the insert.

        Returns ``"reserved"`` on success, ``"fleet_full"`` when the cap is
        already met (nothing inserted), or ``"lost_claim"`` when the item is
        already reserved (the UNIQUE ``item_id`` index rejects the insert — the
        same clean double-dispatch no-op :meth:`reserve_epic_node_item` gives).
        A ``cap`` of 0 or less disables the cap check (always reserves unless the
        item is already taken). Owns its rollback so no partial write survives.
        """
        issue_num = kew.run_store._epic_db_issue_num(item_id)
        dead_pending = self._prepare_admission()

        def _do_reserve() -> ReserveResult:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if (
                        cap > 0
                        and self._count_live_runs(None, dead_pending=dead_pending)
                        >= cap
                    ):
                        self._conn.rollback()
                        return "fleet_full"
                    try:
                        self._execute(
                            "INSERT INTO epic_reservations"
                            " (issue_num, epic_ref, run_id, reserved_at, item_id)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (
                                issue_num,
                                epic_ref,
                                run_id,
                                kew.run_store._dt_to_iso(datetime.now(UTC)),
                                item_id,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        self._conn.rollback()
                        return "lost_claim"
                    self._conn.commit()
                    return "reserved"
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("reserve_epic_node_item_under_cap", _do_reserve)

    def claim_continuation_under_cap(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        continuation_run_id: str,
        cap: int,
        *,
        claim_evidence: dict[str, Any],
        actor_id: str = "continuation_claim",
        repo: str | None = None,
    ) -> ContinuationClaimResult:
        """Claim preserved work and its worker slot in one transaction.

        The continuation row changes from ``awaiting_continuation`` to
        ``continuing`` only in the same ``BEGIN IMMEDIATE`` transaction that
        inserts the reservation and immutable claim event. Insertion failure
        rolls back all three writes. The caller supplies the family evidence.
        The generation predicate rejects a stale observed state.
        """
        return self._with_busy_retry(
            "claim_continuation_under_cap",
            partial(
                self._claim_continuation_transaction,
                epic_ref,
                item_id,
                original_run_id,
                branch,
                worktree,
                continuation_run_id,
                cap,
                claim_evidence=claim_evidence,
                actor_id=actor_id,
                repo=repo,
            ),
        )

    def _claim_continuation_transaction(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        continuation_run_id: str,
        cap: int,
        *,
        claim_evidence: dict[str, Any],
        actor_id: str = "continuation_claim",
        repo: str | None = None,
    ) -> ContinuationClaimResult:
        """Commit the reservation, generation, owner, and immutable claim together."""
        issue_num = kew.run_store._epic_db_issue_num(item_id)
        dead_pending = self._prepare_admission()
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                worktree_identity = self._continuation_worktree_key(
                    epic_ref, item_id, original_run_id, branch, worktree
                )
                row = self._execute(
                    "SELECT * FROM epic_continuations"
                    " WHERE epic_ref = ? AND item_id = ?"
                    " AND original_run_id = ? AND branch = ?"
                    " AND worktree_identity = ?",
                    (
                        epic_ref,
                        item_id,
                        original_run_id,
                        branch,
                        worktree_identity,
                    ),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return "not_found"
                if row["state"] == "continuing":
                    self._conn.rollback()
                    return "already_claimed"
                record = kew.run_store._row_to_continuation(row)
                if not self.continuation_is_recoverable(record):
                    self._conn.rollback()
                    return "terminal"

                if (
                    cap > 0
                    and self._count_live_runs(None, dead_pending=dead_pending) >= cap
                ):
                    self._conn.rollback()
                    return "fleet_full"

                try:
                    self._execute(
                        "INSERT INTO epic_reservations"
                        " (issue_num, epic_ref, run_id, reserved_at, item_id)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            issue_num,
                            epic_ref,
                            continuation_run_id,
                            kew.run_store._dt_to_iso(datetime.now(UTC)),
                            item_id,
                        ),
                    )
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    return "lost_claim"

                now = kew.run_store._dt_to_iso(datetime.now(UTC))
                updated = self._execute(
                    "UPDATE epic_continuations SET"
                    " state = 'continuing',"
                    " generation = generation + 1,"
                    " continuation_run_id = ?,"
                    " updated_at = ?,"
                    " claimed_at = ?,"
                    " terminal_at = NULL"
                    " WHERE epic_ref = ? AND item_id = ?"
                    " AND original_run_id = ? AND branch = ?"
                    " AND worktree_identity = ?"
                    " AND state = ?"
                    " AND generation = ?",
                    (
                        continuation_run_id,
                        now,
                        now,
                        epic_ref,
                        item_id,
                        original_run_id,
                        branch,
                        worktree_identity,
                        row["state"],
                        int(row["generation"]),
                    ),
                )
                if updated.rowcount != 1:
                    self._conn.rollback()
                    return "lost_claim"
                self._attest_continuation_claim(
                    record,
                    continuation_run_id,
                    claim_evidence,
                    actor_id=actor_id,
                    repo=repo,
                )
                self._conn.commit()
                return "claimed"
            except BaseException:
                self._conn.rollback()
                raise

    def mark_continuation_awaiting(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str,
        *,
        reason: str = "claim_released",
        cancel_unlaunched: bool = False,
        precommit_check: Callable[[], None] | None = None,
    ) -> bool:
        """Return an owned continuation to awaiting state, conditionally.

        This is the recovery handoff used after positively identifying a dead
        owner.  The reservation is released only when the same generation is
        moved back to ``awaiting_continuation``. Dispatch failures may cancel
        a claim with no run row or execution evidence. The generation rollback,
        reservation release, and cancellation evidence share this transaction.
        """

        def _do_mark() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    worktree_identity = self._continuation_worktree_key(
                        epic_ref, item_id, original_run_id, branch, worktree
                    )
                    undo_claim = (
                        cancel_unlaunched
                        and self.get(continuation_run_id) is None
                        and not self._continuation_run_launched(continuation_run_id)
                    )
                    now = kew.run_store._dt_to_iso(datetime.now(UTC))
                    updated = self._execute(
                        "UPDATE epic_continuations SET"
                        " state = 'awaiting_continuation',"
                        " continuation_run_id = NULL,"
                        " generation = generation - ?,"
                        " updated_at = ?, claimed_at = NULL, terminal_at = NULL"
                        " WHERE epic_ref = ? AND item_id = ?"
                        " AND original_run_id = ? AND branch = ?"
                        " AND worktree_identity = ?"
                        " AND state = 'continuing' AND generation = ?"
                        " AND continuation_run_id = ?",
                        (
                            int(undo_claim),
                            now,
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                            generation,
                            continuation_run_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        kew.run_store._invoke_precommit_check(precommit_check)
                        kew.run_store._rollback_for_outcome(self._conn)
                        return False
                    self._execute(
                        "DELETE FROM epic_reservations"
                        " WHERE epic_ref = ? AND item_id = ? AND run_id = ?",
                        (epic_ref, item_id, continuation_run_id),
                    )
                    self._attest_continuation_transition(
                        event="continuation_released",
                        epic_ref=epic_ref,
                        item_id=item_id,
                        original_run_id=original_run_id,
                        branch=branch,
                        worktree_identity=worktree_identity,
                        generation=generation,
                        continuation_run_id=continuation_run_id,
                        reason=(
                            f"dispatch_prelaunch_failure: {reason}"
                            if undo_claim
                            else reason
                        ),
                        outcome="claim_undone" if undo_claim else None,
                    )
                    kew.run_store._invoke_precommit_check(precommit_check)
                    self._conn.commit()
                    return True
                except BaseException as exc:
                    kew.run_store._rollback_after_failure(self._conn, exc)
                    raise

        try:
            return self._with_busy_retry("mark_continuation_awaiting", _do_mark)
        except kew.run_store._PrecommitCheckError as exc:
            kew.run_store._raise_precommit_error(exc)

    def release_unevidenced_claim(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str,
    ) -> bool:
        """Undo one claim whose evidence was never recorded, conditionally.

        Unlike :meth:`mark_continuation_awaiting` this restores the previous
        generation: no run and no evidence exist for the claim, so the next
        claim must take the same generation number and record it, keeping the
        evidence history contiguous (a released generation without evidence
        would refuse every later launch). The predicate names the exact owner
        and generation, so a newer claim is never touched.
        """

        def _do_release() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    worktree_identity = self._continuation_worktree_key(
                        epic_ref, item_id, original_run_id, branch, worktree
                    )
                    now = kew.run_store._dt_to_iso(datetime.now(UTC))
                    updated = self._execute(
                        "UPDATE epic_continuations SET"
                        " state = 'awaiting_continuation',"
                        " continuation_run_id = NULL,"
                        " generation = generation - 1,"
                        " updated_at = ?, claimed_at = NULL, terminal_at = NULL"
                        " WHERE epic_ref = ? AND item_id = ?"
                        " AND original_run_id = ? AND branch = ?"
                        " AND worktree_identity = ?"
                        " AND state = 'continuing' AND generation = ?"
                        " AND continuation_run_id = ?",
                        (
                            now,
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                            generation,
                            continuation_run_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        kew.run_store._rollback_for_outcome(self._conn)
                        return False
                    self._execute(
                        "DELETE FROM epic_reservations"
                        " WHERE epic_ref = ? AND item_id = ? AND run_id = ?",
                        (epic_ref, item_id, continuation_run_id),
                    )
                    self._attest_continuation_transition(
                        event="continuation_released",
                        epic_ref=epic_ref,
                        item_id=item_id,
                        original_run_id=original_run_id,
                        branch=branch,
                        worktree_identity=worktree_identity,
                        generation=generation,
                        continuation_run_id=continuation_run_id,
                        reason="claim_evidence_not_recorded",
                        outcome="claim_undone",
                    )
                    self._conn.commit()
                    return True
                except BaseException as exc:
                    kew.run_store._rollback_after_failure(self._conn, exc)
                    raise

        return self._with_busy_retry("release_unevidenced_claim", _do_release)

    def finalize_continuation(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree: str | Path,
        generation: int,
        continuation_run_id: str | None,
        *,
        abandon_reason: str | None = None,
        actor_id: str | None = None,
        repo: str | None = None,
    ) -> bool:
        """Finalize exactly one owned continuation generation.

        A stale writer gets ``False`` and cannot release a newer owner's
        reservation because both the terminal update and generation-matched
        delete are enclosed in this transaction.
        """
        abandoning = abandon_reason is not None
        if abandon_reason is not None and (not abandon_reason.strip() or not actor_id):
            raise ValueError("abandon requires a non-empty reason and operator actor")

        def _do_finalize() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    worktree_identity = self._continuation_worktree_key(
                        epic_ref, item_id, original_run_id, branch, worktree
                    )
                    expected_state = self._finalization_state(
                        epic_ref,
                        item_id,
                        original_run_id,
                        branch,
                        worktree_identity,
                        abandoning=abandoning,
                    )
                    retried_at = datetime.now(UTC)
                    now = kew.run_store._dt_to_iso(retried_at)
                    updated = self._execute(
                        "UPDATE epic_continuations SET"
                        " state = 'terminal', updated_at = ?, terminal_at = ?"
                        " WHERE epic_ref = ? AND item_id = ?"
                        " AND original_run_id = ? AND branch = ?"
                        " AND worktree_identity = ?"
                        " AND state = ? AND generation = ?"
                        " AND continuation_run_id IS ?",
                        (
                            now,
                            now,
                            epic_ref,
                            item_id,
                            original_run_id,
                            branch,
                            worktree_identity,
                            expected_state,
                            generation,
                            continuation_run_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        self._conn.rollback()
                        return False
                    self._execute(
                        "DELETE FROM epic_reservations"
                        " WHERE epic_ref = ? AND item_id = ? AND run_id = ?",
                        (epic_ref, item_id, continuation_run_id or original_run_id),
                    )
                    if abandon_reason is not None:
                        self._attest_continuation_transition(
                            event="continuation_abandoned",
                            epic_ref=epic_ref,
                            item_id=item_id,
                            original_run_id=original_run_id,
                            branch=branch,
                            worktree_identity=worktree_identity,
                            generation=generation,
                            continuation_run_id=continuation_run_id,
                            reason=abandon_reason,
                            actor_id=actor_id,
                            repo=repo,
                        )
                        self._mark_epic_retry_item_in_transaction(
                            epic_ref, item_id, retried_at
                        )
                    self._conn.commit()
                    return True
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("finalize_continuation", _do_finalize)

    def _finalization_state(
        self,
        epic_ref: str,
        item_id: str,
        original_run_id: str,
        branch: str,
        worktree_identity: str,
        *,
        abandoning: bool,
    ) -> str | None:
        """Recheck terminal recovery inside the finalization transaction."""
        if not abandoning:
            return "continuing"
        row = self._execute(
            "SELECT * FROM epic_continuations WHERE epic_ref = ? AND item_id = ?"
            " AND original_run_id = ? AND branch = ? AND worktree_identity = ?",
            (epic_ref, item_id, original_run_id, branch, worktree_identity),
        ).fetchone()
        if row is None:
            return None
        record = kew.run_store._row_to_continuation(row)
        return record.state if self.continuation_is_recoverable(record) else None

    def reserve_epic_node(self, epic_ref: str, issue_num: int, run_id: str) -> bool:
        """Int-shim over :meth:`reserve_epic_node_item` for GitHub callers."""
        return self.reserve_epic_node_item(epic_ref, str(issue_num), run_id)

    def release_epic_node_item(self, item_id: str, run_id: str | None = None) -> None:
        """Delete the reservation for ``item_id``.

        With ``run_id`` the DELETE is generation-conditional
        (``WHERE item_id = ? AND run_id = ?``): a reconciler that verified a
        specific reservation generation as terminal can release only THAT
        generation and never a live re-claimed reservation another actor wrote
        between the reconciler's check and its act (check-then-act TOCTOU —
        #972 review Finding 2). Owners must pass their acquired ``run_id``.
        Omitting it retains the legacy unconditional administrative delete.
        """

        def _do_release() -> None:
            if run_id is None:
                self._execute(
                    "DELETE FROM epic_reservations WHERE item_id = ?", (item_id,)
                )
            else:
                deleted = self._execute(
                    "DELETE FROM epic_reservations WHERE item_id = ? AND run_id = ?",
                    (item_id, run_id),
                )
                if deleted.rowcount == 0:
                    kew.run_store._logger.debug(
                        "Reservation release skipped: item %s is not owned by %s",
                        item_id,
                        run_id,
                    )
            self._conn.commit()

        self._with_busy_retry("release_epic_node_item", _do_release)

    def release_epic_node_item_unless(
        self, item_id: str, run_id: str, *, keep: Callable[[], bool]
    ) -> bool:
        """Release one reservation generation unless *keep* says it is owned;
        True when this call deleted it.

        *keep* runs INSIDE the ``BEGIN IMMEDIATE`` transaction that deletes,
        so a decision taken from an earlier read (a slow tracker lookup) is
        re-tested where no other writer can commit. A dispatch child binds to
        its reservation inside its own admission transaction
        (``_bind_dispatch_reservation``), so the child either committed its
        run row before *keep* reads, or finds the reservation gone and is
        refused — never a pending run whose reservation was deleted under it
        (#3192). *keep* may read the store; it must not write.
        """

        def _do_release() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if keep():
                        kew.run_store._rollback_for_outcome(self._conn)
                        return False
                    deleted = self._execute(
                        "DELETE FROM epic_reservations"
                        " WHERE item_id = ? AND run_id = ?",
                        (item_id, run_id),
                    )
                    self._conn.commit()
                    return deleted.rowcount == 1
                except BaseException as exc:
                    kew.run_store._rollback_after_failure(self._conn, exc)
                    raise

        return self._with_busy_retry("release_epic_node_item_unless", _do_release)

    def release_epic_node(self, issue_num: int, *, run_id: str) -> None:
        """Int-shim over :meth:`release_epic_node_item` for GitHub callers."""
        self.release_epic_node_item(str(issue_num), run_id=run_id)

    def item_reservation(self, item_id: str) -> EpicReservation | None:
        """The one reservation holding *item_id* under any epic, if any.

        ``item_id`` is unique across ``epic_reservations``, so this is the row
        a second reserve of the item would collide with.
        """
        row = self._execute(
            "SELECT issue_num, epic_ref, run_id, reserved_at, item_id"
            " FROM epic_reservations WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return None if row is None else kew.run_store._row_to_epic_reservation(row)

    def list_epic_reservations(self, epic_ref: str) -> list[EpicReservation]:
        rows = self._execute(
            "SELECT issue_num, epic_ref, run_id, reserved_at, item_id"
            " FROM epic_reservations WHERE epic_ref = ? ORDER BY item_id",
            (epic_ref,),
        ).fetchall()
        return [kew.run_store._row_to_epic_reservation(row) for row in rows]
