"""Run-record store operations, extracted from ``run_store`` (Task 4).

This module owns a cohesive slice of the run-record CRUD surface of
``SqliteRunStore``: the upsert writer (``save``), the item-keyed reader
(``list_by_item``), and the cost-baseline aggregate (``median_cost_for_bucket``).
Run saves preserve committed stop intent and registered ownership by default.
Cost reads retain the shared bucket-exclusion rules.

``SqliteRunStore`` mixes :class:`RunRecordStoreMixin` in. The upsert SQL
constant (``_UPSERT_SQL``), the bind-tuple builder (``_run_record_params``,
shared with ``try_admit_run``), and the row builder (``_row_to_record``) stay in
``run_store``; the module-level ones are reached here as attributes of the
``kew.run_store`` module at call time, which also breaks the import cycle
(``run_store`` imports this mixin at module load).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from statistics import median
from typing import TYPE_CHECKING, Any, TypeVar

import kew.run_store
from kew.breaker import FLAGGED_ANOMALY_CATEGORY
from kew.claim_retention import LINEAR_CLAIM_RECOVERY

if TYPE_CHECKING:
    from kew.run_store import EpicReservation, RunRecord
    from kew.stop_recovery import LabelReset

_T = TypeVar("_T")

# The two cost-baseline reads differ *only* in the sense of the run-kind
# predicate, so they are composed from shared literal halves: the "which rows
# count as normal" rules (#668) can then never drift between the bucket median
# and the other-kind reference the breaker gates arming on (#1116).
_COST_ROWS_HEAD = (
    "SELECT estimated_cost_usd FROM runs WHERE model = ? AND complexity = ? "
)
# A run the breaker flagged as anomalous after it finished (#1798) is a plain
# success with an anomalous cost: it is kept out of the baseline it tripped.
_COST_ROWS_TAIL = (
    "AND status IN ('success', 'failure') AND estimated_cost_usd >= 0.01 "
    "AND COALESCE(error_category, '') != ?"
)
_BUCKET_COST_SQL = _COST_ROWS_HEAD + "AND run_kind = ? " + _COST_ROWS_TAIL
_OTHER_KINDS_COST_SQL = _COST_ROWS_HEAD + "AND run_kind != ? " + _COST_ROWS_TAIL

# Appended to ``_UPSERT_SQL``'s DO UPDATE: the conflict update applies only to
# the row the epic driver writes for a child it judged dead
# (``graph.driver.tick._record_dispatch_child_died``).
_ONLY_OVER_DEAD_CHILD = (
    " WHERE runs.status = 'failure' AND runs.error_category = 'dispatch_child_died'"
)

_OWNER_COLUMNS = (
    "pgid",
    "owner_pid",
    "owner_start_time",
    "owner_start_token",
    "attempt_start_token",
    "owner_boot_id",
    "owner_ppid",
    "owner_command",
    "attempt_pgid",
    "attempt_start_time",
    "attempt_owner_verified",
    "container_name",
    "container_id",
    "container_cidfile",
    "container_runtime",
)


def ownership_guard(record: RunRecord | None) -> tuple[str, tuple[Any, ...]]:
    """Bind the exact probed identity, including null fields, to the transition."""
    if record is None:
        return "", ()
    return (
        "".join(f" AND {column} IS ?" for column in _OWNER_COLUMNS),
        tuple(getattr(record, column) for column in _OWNER_COLUMNS),
    )


def _record_orphaned_linear_claim(
    store: RunRecordStoreMixin, run_id: str, timestamp: str
) -> bool:
    """Preserve exact-run claim evidence inside the reap transaction."""
    started = store._conn.execute(
        "SELECT a.* FROM audit_events a JOIN runs r ON r.run_id = a.run_id"
        " AND r.item_id = a.item_id WHERE a.run_id = ?"
        " AND a.event = 'linear_claim_started'"
        " AND NOT EXISTS (SELECT 1 FROM audit_events settled"
        " WHERE settled.run_id = a.run_id AND settled.item_id = a.item_id"
        " AND settled.event IN"
        " ('linear_claim_orphaned', 'linear_claim_retained'))"
        " ORDER BY a.id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if started is None:
        return False
    payload = json.loads(started["payload"] or "{}")
    payload.update(
        run_id=run_id,
        item_id=started["item_id"],
        epic_ref=payload.get("epic_ref"),
        started_event_id=started["id"],
    )
    completed = store._conn.execute(
        "SELECT id, payload FROM audit_events WHERE run_id = ? AND item_id = ?"
        " AND event = 'linear_claim_completed' AND id > ? ORDER BY id DESC LIMIT 1",
        (run_id, started["item_id"], started["id"]),
    ).fetchone()
    launched = store._conn.execute(
        "SELECT 1 FROM audit_events WHERE run_id = ? AND item_id = ?"
        " AND event = 'linear_claim_launched' AND id > ? LIMIT 1",
        (run_id, started["item_id"], started["id"]),
    ).fetchone()
    if completed is not None:
        payload["completed_event_id"] = completed["id"]
        payload["snapshot"] = json.loads(completed["payload"] or "{}").get("snapshot")
    event = "linear_claim_orphaned"
    if launched is not None:
        event = "linear_claim_retained"
        payload.update(
            reason="launched dispatcher died", recovery=LINEAR_CLAIM_RECOVERY
        )
    store._chained_insert_in_transaction(
        (
            timestamp,
            event,
            run_id,
            started["issue_num"],
            started["repo"],
            "policy",
            "driver_reconcile",
            None,
            started["policy_hash"],
            json.dumps(payload),
            started["item_id"],
        )
    )
    # Launched work keeps the existing continuation ownership/release path.
    return launched is None


def _finalize_orphaned_pending(
    store: RunRecordStoreMixin,
    run_id: str,
    error_category: str,
    finished_iso: str,
    duration_seconds: float | None,
    precommit_check: Callable[[], None] | None,
    reservation: EpicReservation | None,
    expected_owner: RunRecord | None,
    retry_reason: str | None,
    retry_repo: str | None,
) -> bool:
    guard, identity = ownership_guard(expected_owner)
    with store._txn_lock:
        try:
            store._conn.execute("BEGIN IMMEDIATE")
            cursor = store._execute(
                "UPDATE runs SET status = 'killed', error_category = ?,"  # nosec B608
                " exit_code = 137, finished_at = ?,"
                " duration_seconds = COALESCE(?, duration_seconds)"
                " WHERE run_id = ? AND status = 'pending'" + guard,
                (error_category, finished_iso, duration_seconds, run_id, *identity),
            )
            if cursor.rowcount != 1:
                kew.run_store._invoke_precommit_check(precommit_check)
                kew.run_store._rollback_for_outcome(store._conn)
                return False
            orphaned_claim = _record_orphaned_linear_claim(store, run_id, finished_iso)
            # Other orphan recovery can still require continuation ownership checks.
            # Its caller releases the reservation after those checks succeed.
            if orphaned_claim and reservation is not None:
                store._execute(
                    "DELETE FROM epic_reservations"
                    " WHERE epic_ref = ? AND item_id = ? AND run_id = ?",
                    (reservation.epic_ref, reservation.item_id, reservation.run_id),
                )
            kew.run_store._invoke_precommit_check(precommit_check)
            # The reap marker commits in the SAME transaction as the terminal
            # row: credential reconciliation reads it to tell a reaped orphan
            # from a run still holding an issued key.
            store._execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (f"orphaned_run_reaped:{run_id}", finished_iso),
            )
            _retry_orphan_in_transaction(
                store,
                reservation,
                run_id=run_id,
                timestamp=finished_iso,
                reason=retry_reason,
                repo=retry_repo,
            )
            store._conn.commit()
            return True
        except BaseException as exc:
            kew.run_store._rollback_after_failure(store._conn, exc)
            raise


def _retry_orphan_in_transaction(
    store: RunRecordStoreMixin,
    reservation: EpicReservation | None,
    *,
    run_id: str,
    timestamp: str,
    reason: str | None,
    repo: str | None,
) -> None:
    """Re-admit and attest the node in the reap's own transaction (#2812).

    A driver that died after this commit must leave both the retry marker and
    its reason. The next tick can then dispatch without inventing an audit row.
    The marker binds *run_id* itself, not the reap's clock (#2969).
    """
    if reservation is None or reason is None:
        return
    store._supersede_run_in_transaction(
        reservation.epic_ref, reservation.item_id, run_id
    )
    payload = {
        "epic_ref": reservation.epic_ref,
        "node": reservation.item_id,
        "source": "driver",
        "reason": reason,
    }
    store._chained_insert_in_transaction(
        (
            timestamp,
            "epic_node_retry",
            run_id,
            reservation.issue_num,
            repo,
            "policy",
            "driver_reconcile",
            None,
            None,
            json.dumps(payload),
            reservation.item_id,
        ),
        dedup_key=(
            f"orphan-retry:{reservation.epic_ref}:{reservation.item_id}:{run_id}"
        ),
    )


class RunRecordStoreMixin:
    """A cohesive slice of the run-record CRUD surface of ``SqliteRunStore``.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the busy-retry / execute wrappers, and the shared
    ``_run_record_params`` bind-tuple builder declared below.
    """

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore — declared here so the mixin
        # body type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection
        _txn_lock: Any

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _run_record_params(self, record: RunRecord) -> tuple[Any, ...]: ...

        def reap_cancelled_runs(self) -> None: ...

        def _supersede_run_in_transaction(
            self, epic_ref: str, item_id: str, run_id: str
        ) -> None: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def retry_stop_label_resets(
            self,
            run_id: str | None = None,
            *,
            reset_labels: LabelReset | None = None,
        ) -> None: ...

    def save(self, record: RunRecord, *, update_ownership: bool = False) -> None:
        """Save results while preserving committed stop intent and ownership."""

        def _do_save() -> None:
            self._execute(kew.run_store._UPSERT_SQL, self._run_record_params(record))
            if update_ownership:
                self._replace_run_ownership(record)
            self._conn.commit()

        self._with_busy_retry("save", _do_save)
        if record.status != "pending":
            self.retry_stop_label_resets(record.run_id)

    def save_over_dead_child(self, record: RunRecord) -> bool:
        """Insert *record*, or replace only the driver's dead-child verdict.

        One conditional statement: a missing row is inserted, a row the driver
        wrote as ``failure`` / ``dispatch_child_died`` for a child it judged
        dead is replaced, and any other row under the run id is left whole.
        True when the row was written.
        """
        sql = kew.run_store._UPSERT_SQL + _ONLY_OVER_DEAD_CHILD

        def _do_save() -> bool:
            cursor = self._execute(sql, self._run_record_params(record))
            self._conn.commit()
            return cursor.rowcount == 1

        written = self._with_busy_retry("save_over_dead_child", _do_save)
        if written and record.status != "pending":
            self.retry_stop_label_resets(record.run_id)
        return written

    def _replace_run_ownership(self, record: RunRecord) -> None:
        """Apply an explicit ownership write in the same transaction as save."""
        self._execute(
            "UPDATE runs SET pgid = ?, owner_pid = ?, owner_start_time = ?,"
            " attempt_pgid = ?, attempt_start_time = ?, attempt_owner_verified = ?,"
            " container_name = ?, container_id = ?, container_cidfile = ?"
            ", owner_ppid = ?, owner_command = ?"
            ", container_runtime = ?, owner_start_token = ?, attempt_start_token = ?"
            ", owner_boot_id = ?"
            " WHERE run_id = ?",
            (
                record.pgid,
                record.owner_pid,
                record.owner_start_time,
                record.attempt_pgid,
                record.attempt_start_time,
                int(record.attempt_owner_verified),
                record.container_name,
                record.container_id,
                record.container_cidfile,
                record.owner_ppid,
                record.owner_command,
                record.container_runtime,
                record.owner_start_token,
                record.attempt_start_token,
                record.owner_boot_id,
                record.run_id,
            ),
        )

    def transition_run_status(
        self,
        run_id: str,
        *,
        expected_status: str,
        new_status: str,
        error_category: str,
        pr_url: str | None = None,
    ) -> bool:
        """Atomically transition a run status when its current state matches.

        The expected status is the persisted state-version for the narrow
        publication state machine.  Only the status, error category, and an
        optional PR URL are touched, so a concurrent writer's other terminal
        fields cannot be overwritten by a stale publication worker.
        """

        def _do_transition() -> bool:
            cursor = self._execute(
                "UPDATE runs SET status = ?, error_category = ?,"
                " pr_url = COALESCE(?, pr_url)"
                " WHERE run_id = ? AND status = ?",
                (new_status, error_category, pr_url, run_id, expected_status),
            )
            self._conn.commit()
            return cursor.rowcount == 1

        changed = self._with_busy_retry("transition_run_status", _do_transition)
        self.retry_stop_label_resets(run_id)
        return changed

    def finish_pending_refusal(
        self,
        run_id: str,
        *,
        status: str,
        error_category: str,
        exit_code: int,
        finished_at: datetime,
    ) -> bool:
        """Finish a live ``pending`` row as refused, in one conditional write.

        A row an operator asked to stop (``pending`` with ``error_category``
        killed, see ``request_run_stop``) is left as it is, so the stop
        reconciler still finishes it as killed. Only the status, category,
        exit code and finish time change. True when the row was finished.
        """

        def _do_finish() -> bool:
            cursor = self._execute(
                "UPDATE runs SET status = ?, error_category = ?, exit_code = ?,"
                " finished_at = ? WHERE run_id = ? AND status = 'pending'"
                " AND COALESCE(error_category, '') != 'killed'",
                (
                    status,
                    error_category,
                    exit_code,
                    kew.run_store._dt_to_iso(finished_at),
                    run_id,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

        changed = self._with_busy_retry("finish_pending_refusal", _do_finish)
        if changed:
            self.retry_stop_label_resets(run_id)
        return changed

    def finalize_orphaned_pending(
        self,
        run_id: str,
        *,
        error_category: str,
        finished_at: datetime,
        duration_seconds: float | None = None,
        precommit_check: Callable[[], None] | None = None,
        reservation: EpicReservation | None = None,
        expected_owner: RunRecord | None = None,
        retry_reason: str | None = None,
        retry_repo: str | None = None,
    ) -> bool:
        """Atomically terminalize a pending row whose owner was proven dead.

        The liveness probe precedes this write. The pending predicate preserves
        concurrent terminal outcomes. ``expected_owner`` also binds the exact
        probed ownership fields, so a replacement owner defeats a stale probe.
        Only orphan-terminal fields are updated, preserving all telemetry the
        owner may already have attached to the row. Unlaunched Linear claims
        receive a recovery marker in this transaction, including completed claims.
        Their reservation is released only if its generation still matches.
        Launched claims retain guidance and use the existing continuation path.

        ``finished_at`` goes through the store's canonical serializer, so the
        stored text is UTC and rejects naive datetimes: cost-window reads
        compare ``finished_at`` lexically, and a preserved non-UTC offset would
        silently drop reaped runs out of their window.

        ``retry_reason`` writes *reservation*'s retry marker and audit event in
        this transaction. A re-admission therefore keeps its reason after a
        driver crash.
        """
        self.reap_cancelled_runs()
        finished_iso = kew.run_store._dt_to_iso(finished_at)
        assert finished_iso is not None
        try:
            finalized = self._with_busy_retry(
                "finalize_orphaned_pending",
                partial(
                    _finalize_orphaned_pending,
                    self,
                    run_id,
                    error_category,
                    finished_iso,
                    duration_seconds,
                    precommit_check,
                    reservation,
                    expected_owner,
                    retry_reason,
                    retry_repo,
                ),
            )
            self.retry_stop_label_resets(run_id)
            return finalized
        except kew.run_store._PrecommitCheckError as exc:
            kew.run_store._raise_precommit_error(exc)

    def request_orphan_cleanup(self, record: RunRecord, runtime: str) -> bool:
        """Persist retryable cleanup before any contained orphan signal."""
        guard, identity = ownership_guard(record)

        def write() -> bool:
            # Guard columns are constants; every value uses a bound parameter.
            cursor = self._execute(
                "UPDATE runs SET error_category = 'killed',"  # nosec B608
                " cleanup_runtime = ?"
                " WHERE run_id = ? AND status = 'pending'"
                " AND (cleanup_runtime IS NULL OR cleanup_runtime = ?)" + guard,
                (runtime, record.run_id, runtime, *identity),
            )
            self._conn.commit()
            return cursor.rowcount == 1

        return self._with_busy_retry("request_orphan_cleanup", write)

    def finalize_awaiting_publication(
        self, run_id: str, *, pr_url: str | None = None
    ) -> bool:
        """Settle one deferred publication and its unclaimed recovery handle.

        The run transition and continuation terminalization share one immediate
        transaction.  A remote-fact reconciler therefore cannot expose a
        successful run while leaving the publication continuation active (or
        vice versa), which would let the continuation overlay hide the newly
        observed provider truth.
        """
        now = datetime.now(UTC).isoformat()

        def _do_finalize() -> bool:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    cursor = self._execute(
                        "UPDATE runs SET status = 'success', error_category = '',"
                        " pr_url = COALESCE(?, pr_url)"
                        " WHERE run_id = ? AND status = 'awaiting_publication'",
                        (pr_url, run_id),
                    )
                    if cursor.rowcount != 1:
                        self._conn.rollback()
                        return False
                    self._execute(
                        "UPDATE epic_continuations SET state = 'terminal',"
                        " updated_at = ?, terminal_at = ?"
                        " WHERE (original_run_id = ? OR (item_id, branch) IN"
                        " (SELECT item_id, branch FROM runs WHERE run_id = ?))"
                        " AND state = 'awaiting_continuation'"
                        " AND continuation_run_id IS NULL",
                        (now, now, run_id, run_id),
                    )
                    self._conn.commit()
                    return True
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry("finalize_awaiting_publication", _do_finalize)

    def record_managed_transport_admission(
        self, run_id: str, *, observed_version: str, config_sha256: str
    ) -> bool:
        """Atomically attach ready evidence to one pending managed run."""

        def _do_record() -> bool:
            cursor = self._execute(
                "UPDATE runs SET transport_observed_version = ?,"
                " transport_config_sha256 = ?, transport_preflight_verdict = 'ready'"
                " WHERE run_id = ? AND status = 'pending'"
                " AND transport_managed = 1"
                " AND transport_preflight_verdict = 'managed_pending'",
                (observed_version, config_sha256, run_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

        return self._with_busy_retry("record_managed_transport_admission", _do_record)

    def list_by_item(self, item_id: str) -> list[RunRecord]:
        """``list_by_issue`` twin keyed on the durable ``item_id`` (A3), the
        currency for non-numeric trackers. Index-backed by ``idx_runs_item_id``.
        """
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE item_id = ? ORDER BY started_at",
            (item_id,),
        ).fetchall()
        return [kew.run_store._row_to_record(row) for row in rows]

    def median_cost_for_bucket(
        self,
        model: str,
        complexity: str,
        run_kind: str = "dispatch",
        *,
        other_kinds: bool = False,
    ) -> tuple[float | None, int]:
        """Median estimated_cost_usd over normally-completed runs in a
        (model, complexity, run_kind) bucket, plus the sample count. Keying on
        ``run_kind`` (I1/#445) keeps heavyweight review runs (~$5.37) out of the
        cheap first-dispatch median that used to false-kill them. Killed /
        pending / budget_exceeded rows are excluded so abnormal terminations
        don't poison the baseline. Degenerate near-zero-cost rows (< $0.01, e.g.
        local lemonade/no-op runs) are also excluded so a bucket of $0.00
        samples can't collapse the median to $0 and make any real spend read as
        anomalous (#668). Empty bucket -> (None, 0).

        ``other_kinds=True`` inverts only the run-kind predicate, giving the
        median over every kind on the route *except* this one. That is the
        breaker's reference for a bucket too fresh to judge itself (#1116).
        The exclusion is the point: a reference read over a superset that still
        contains the bucket's own rows is not independent evidence, and when
        the bucket is the only kind the route has ever run it collapses into
        the bucket median exactly, making any "has this bucket cleared the
        route?" gate a tautology (#1138 review). It shares this query otherwise
        so the two can never disagree about which rows count as normal (#668).
        """
        sql = _OTHER_KINDS_COST_SQL if other_kinds else _BUCKET_COST_SQL
        params = (model, complexity, run_kind, FLAGGED_ANOMALY_CATEGORY)
        rows = self._conn.execute(sql, params).fetchall()
        costs = [r["estimated_cost_usd"] for r in rows]
        if not costs:
            return None, 0
        return float(median(costs)), len(costs)
