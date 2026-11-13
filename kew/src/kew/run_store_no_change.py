"""The no-change surface of ``SqliteRunStore`` (I10A, Tasks 43-44).

Three tables, one meaning. ``no_change_judgment`` is the projection of one
zero-diff exit; ``outcome_sync_jobs`` is what the host means to write to each
authority once that judgment is authorized; ``no_change_dependency_effect`` is
what each dependent is owed, one row per dependent, in order.

Every write is a check-then-act on state another host may be touching, so every
one of them runs inside ``BEGIN IMMEDIATE``: the read that decides whether a
move is legal and the write that makes it are one serialized step.

Four rules give the tables their meaning:

* **Nothing here causes an effect.** These are intents and acknowledgements.
  I10B creates the candidates, I11 performs the synchronization and I12
  reconciles the dependents; a row written here is a promise the host has not
  kept yet, which is exactly what makes a crash recoverable.
* **Evidence is written before the state it justifies.** Each transition
  appends one strict I06 receipt fact FIRST (dedup-keyed on the row and the
  target state, so a replay converges on one ledger row) and only then makes
  the compare-and-set write, recording the event id on the row. A transition
  whose evidence cannot be recorded does not happen. The audit append takes
  the same write lock, so it cannot be nested inside the state transaction —
  ordering it first is what keeps "state without evidence" impossible.
* **Effect is generation-bound.** A sync job and a dependency effect carry the
  judgment generation they were created under. A worker holding a job from a
  superseded generation is refused, so a replay cannot acknowledge a remote
  write that belongs to a decision somebody has since replaced.
* **Dependents are exposed one at a time.** :meth:`next_dependency_effect`
  returns the lowest unsettled ordinal and nothing else, and
  :meth:`begin_dependency_effect` refuses a row already ``applying``. A host
  that died after the first dependent comes back to the SAME row, in a state
  that forces it to observe rather than apply again — and the second dependent
  is not visible until the first is acknowledged.

Legacy rows are read, never written: :meth:`legacy_no_change_views` decodes
successful runs that opened no pull request as :data:`kew.no_change.LEGACY_UNKNOWN`.
No legacy run recorded a trusted base, a sealed work package or a named check,
so a candidate backfilled on its behalf would attest to evidence that never
existed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from kew.no_change import (
    ATTESTED,
    AUTHORIZED,
    CANDIDATE,
    COMPLETED,
    DEFAULT_NO_CHANGE_POLICY,
    EFFECT_ACKED,
    EFFECT_APPLYING,
    EFFECT_INTENT,
    EFFECT_PENDING,
    EFFECT_STATE_FACT,
    EFFECT_TERMINAL_STATES,
    EVIDENCE_ACTOR_ID,
    EVIDENCE_ACTOR_TYPE,
    JUDGMENT_STATE_FACT,
    NO_CHANGE_POLICIES,
    PENDING_CONFIRMATION,
    RECEIPT_FACT_EVENT,
    REJECTED,
    SYNC_ACKED,
    SYNC_INTENT,
    SYNC_OBSERVED,
    SYNC_PENDING,
    SYNC_STATE_FACT,
    SYNC_TERMINAL_STATES,
    TERMINAL_STATES,
    AlreadySatisfiedContract,
    DependencyEffect,
    LegacyNoChangeView,
    NoChangeConflictError,
    NoChangeError,
    NoChangeEvidence,
    NoChangeIntent,
    NoChangeJudgment,
    NoChangeStateError,
    NoChangeValueError,
    OutcomeSyncJob,
    build_receipt_fact_payload,
    decode_legacy_no_change,
    dependency_effect_id,
    outcome_sync_job_id,
    producer_version,
    resolve_authorization,
    sync_idempotency_key,
    validate_dependency_effect_transition,
    validate_outcome_sync_transition,
    validate_transition,
)

_T = TypeVar("_T")

_logger = logging.getLogger(__name__)

_JUDGMENT_COLS = (
    "judgment_id",
    "lineage_key",
    "generation",
    "source_kind",
    "source_id",
    "item_id",
    "run_id",
    "attempt_number",
    "base_sha",
    "state",
    "policy",
    "evidence_digest",
    "evidence_json",
    "decision_reason",
    "decided_by",
    "decided_at",
    "contract_digest",
    "audit_event_id",
    "created_at",
    "updated_at",
)

_SYNC_COLS = (
    "sync_job_id",
    "judgment_id",
    "generation",
    "target_kind",
    "target_ref",
    "idempotency_key",
    "state",
    "observation_kind",
    "observation_ref",
    "observed_at",
    "acked_at",
    "detail",
    "audit_event_id",
    "created_at",
    "updated_at",
)

_EFFECT_COLS = (
    "effect_id",
    "judgment_id",
    "generation",
    "dependent_id",
    "ordinal",
    "state",
    "applied_at",
    "acked_at",
    "detail",
    "audit_event_id",
    "created_at",
    "updated_at",
)

# Every SQL string below is built from these column names and from the
# lifecycle constants — never from caller input, whose values are all bound
# parameters. bandit flags the concatenation sites, so the suppressions live
# here rather than being scattered over the call sites.
_JUDGMENT_SELECT = f"SELECT {', '.join(_JUDGMENT_COLS)} FROM no_change_judgment"  # nosec B608
_JUDGMENT_INSERT = (
    f"INSERT INTO no_change_judgment ({', '.join(_JUDGMENT_COLS)})"  # nosec B608
    f" VALUES ({', '.join('?' * len(_JUDGMENT_COLS))})"
)
_SYNC_SELECT = f"SELECT {', '.join(_SYNC_COLS)} FROM outcome_sync_jobs"  # nosec B608
_SYNC_INSERT = (
    f"INSERT INTO outcome_sync_jobs ({', '.join(_SYNC_COLS)})"  # nosec B608
    f" VALUES ({', '.join('?' * len(_SYNC_COLS))})"
)
_EFFECT_SELECT = f"SELECT {', '.join(_EFFECT_COLS)} FROM no_change_dependency_effect"  # nosec B608
_EFFECT_INSERT = (
    f"INSERT INTO no_change_dependency_effect ({', '.join(_EFFECT_COLS)})"  # nosec B608
    f" VALUES ({', '.join('?' * len(_EFFECT_COLS))})"
)

_LIVE = " state NOT IN ({})".format(
    ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATES))
)
_BY_JUDGMENT_ID = _JUDGMENT_SELECT + " WHERE judgment_id = ?"  # nosec B608
_BY_RUN_ATTEMPT = (
    _JUDGMENT_SELECT + " WHERE run_id = ? AND attempt_number = ?"  # nosec B608
)
_LIVE_FOR_LINEAGE = _JUDGMENT_SELECT + f" WHERE lineage_key = ? AND{_LIVE}"  # nosec B608
_ALL_JUDGMENTS = _JUDGMENT_SELECT + " ORDER BY id ASC"  # nosec B608
_MAX_GENERATION = (
    "SELECT MAX(generation) AS g FROM no_change_judgment WHERE lineage_key = ?"
)
# COALESCE everywhere a later transition does not carry the field: who
# confirmed, what they confirmed and the contract they confirmed it against
# are facts about the judgment, not about the move that happened to be
# writing when they were learned. Overwriting them with the NULLs of a
# subsequent move would erase the person from the row that authorized an item
# to close.
_UPDATE_JUDGMENT = (
    "UPDATE no_change_judgment SET state = ?, decision_reason = ?,"
    " decided_by = COALESCE(?, decided_by), decided_at = COALESCE(?, decided_at),"
    " evidence_digest = COALESCE(?, evidence_digest),"
    " evidence_json = COALESCE(?, evidence_json),"
    " contract_digest = COALESCE(?, contract_digest), audit_event_id = ?,"
    " updated_at = ? WHERE judgment_id = ? AND state = ?"
)

_REBASE_JUDGMENT = (
    "UPDATE no_change_judgment SET base_sha = ?, updated_at = ?"
    " WHERE judgment_id = ? AND state = ? AND base_sha = ?"
)

_SYNC_BY_ID = _SYNC_SELECT + " WHERE sync_job_id = ?"  # nosec B608
_SYNC_FOR_JUDGMENT = (
    _SYNC_SELECT + " WHERE judgment_id = ? ORDER BY id ASC"  # nosec B608
)
_UPDATE_SYNC = (
    "UPDATE outcome_sync_jobs SET state = ?, observation_kind = ?,"
    " observation_ref = ?, observed_at = ?, acked_at = ?, detail = ?,"
    " audit_event_id = ?, updated_at = ? WHERE sync_job_id = ? AND state = ?"
)
_SYNC_UNSETTLED = (
    "SELECT COUNT(*) AS n FROM outcome_sync_jobs"  # nosec B608
    " WHERE judgment_id = ? AND state NOT IN ({})".format(
        ", ".join(f"'{s}'" for s in sorted(SYNC_TERMINAL_STATES))
    )
)

_EFFECT_BY_ID = _EFFECT_SELECT + " WHERE effect_id = ?"  # nosec B608
_EFFECT_FOR_JUDGMENT = (
    _EFFECT_SELECT + " WHERE judgment_id = ? ORDER BY ordinal ASC"  # nosec B608
)
_EFFECT_UNSETTLED = " AND state NOT IN ({})".format(
    ", ".join(f"'{s}'" for s in sorted(EFFECT_TERMINAL_STATES))
)
_NEXT_EFFECT = (
    _EFFECT_SELECT
    + " WHERE judgment_id = ?"  # nosec B608
    + _EFFECT_UNSETTLED
    + " ORDER BY ordinal ASC LIMIT 1"
)
_COUNT_UNSETTLED_EFFECTS = (
    "SELECT COUNT(*) AS n FROM no_change_dependency_effect"  # nosec B608
    " WHERE judgment_id = ?" + _EFFECT_UNSETTLED
)
_UPDATE_EFFECT = (
    "UPDATE no_change_dependency_effect SET state = ?, applied_at = ?,"
    " acked_at = ?, detail = ?, audit_event_id = ?, updated_at = ?"
    " WHERE effect_id = ? AND state = ?"
)

# Deliberately ``*`` and deliberately unfiltered on the pull request: this
# read has to work against a database that predates every column a no-change
# would care about, and naming one that a given legacy schema lacks would turn
# "what do the old rows mean?" into an OperationalError. The columns are read
# defensively below and the pull-request filter lives in Python.
_LEGACY_SELECT = "SELECT * FROM runs WHERE status = 'success' ORDER BY started_at ASC"


def _append_receipt_fact(
    store: Any,
    *,
    fact: str,
    subject: str,
    judgment: NoChangeJudgment,
    target: str,
    detail: str,
    now: datetime,
) -> int:
    """Append the strict I06 receipt fact one transition rests on, or refuse.

    Written through the store's OWN dedup-keyed audit append rather than
    :func:`kew.evidence.record_evidence`: evidence reaches the audit ledger,
    which reaches the config, which reaches the runners, and the store sits
    below all three in the layered dispatch stack. The payload is built by
    :func:`kew.no_change.build_receipt_fact_payload` to be byte-identical to
    what the evidence writer would have produced, and ``test_no_change.py``
    fails if the two ever drift.

    The dedup key is the row and the target state, so a transition replayed
    after a crash converges on one ledger row. A missing row id is a refusal:
    a transition the ledger did not record must not happen.
    """
    payload = build_receipt_fact_payload(
        fact=fact,
        subject=subject,
        target=target,
        detail=detail,
        attempt_number=judgment.attempt_number,
        producer_version=producer_version(),
    )
    try:
        event_id = store.append_audit_event_once(
            dedup_key=f"no_change:{subject}:{judgment.generation}:{target}",
            timestamp=_iso(now),
            event=RECEIPT_FACT_EVENT,
            run_id=judgment.run_id,
            item_id=judgment.item_id,
            actor_type=EVIDENCE_ACTOR_TYPE,
            actor_id=EVIDENCE_ACTOR_ID,
            payload=payload,
        )
    except Exception as exc:
        raise NoChangeStateError(
            f"{subject} cannot become {target}: its evidence was not recorded ({exc})"
        ) from exc
    if event_id is None:
        raise NoChangeStateError(
            f"{subject} cannot become {target}: its evidence was not appended"
        )
    return int(event_id)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _row_to_judgment(row: sqlite3.Row) -> NoChangeJudgment:
    return NoChangeJudgment(**{col: row[col] for col in _JUDGMENT_COLS})


def _row_to_sync(row: sqlite3.Row) -> OutcomeSyncJob:
    return OutcomeSyncJob(**{col: row[col] for col in _SYNC_COLS})


def _row_to_effect(row: sqlite3.Row) -> DependencyEffect:
    return DependencyEffect(**{col: row[col] for col in _EFFECT_COLS})


def _require_policy(policy: str) -> str:
    if policy not in NO_CHANGE_POLICIES:
        raise NoChangeValueError(f"{policy!r} is not a no-change policy")
    return policy


def _judged_base(judgment: NoChangeJudgment) -> str:
    """The base *judgment*'s run was judged on, as its evidence records it.

    ``""`` while it has no readable evidence: a candidate has not been rejudged.
    """
    try:
        evidence = judgment.evidence
    except NoChangeError as exc:
        _logger.warning(
            "%s evidence is unreadable, so a reopened intent must match its"
            " current base: %s",
            judgment.judgment_id,
            exc,
        )
        return ""
    return evidence.observed_base_sha if evidence is not None else ""


def _same_or_conflict(
    existing: NoChangeJudgment, intent: NoChangeIntent, policy: str
) -> NoChangeJudgment:
    """A retry of the SAME intent converges; a different one is a conflict.

    A rejudged judgment (``kew no-change rejudge``) stands on a newer base than
    the one its run was judged on, which its evidence still records; the run's
    own intent, retried after that, is the same intent and converges too.
    """
    if existing.policy == policy and intent.base_sha == _judged_base(existing):
        return existing
    for field, wanted in (("base_sha", intent.base_sha), ("policy", policy)):
        if getattr(existing, field) != wanted:
            raise NoChangeConflictError(
                f"no-change judgment {existing.judgment_id} already exists with a"
                f" different {field}"
            )
    return existing


class NoChangeStoreMixin:
    """The no-change judgment, sync-job and dependency-effect surface."""

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore.
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    # ---- evidence ---------------------------------------------------------

    def _record_state_evidence(
        self,
        *,
        fact: str,
        subject: str,
        judgment: NoChangeJudgment,
        target: str,
        detail: str,
        now: datetime,
    ) -> int:
        return _append_receipt_fact(
            self,
            fact=fact,
            subject=subject,
            judgment=judgment,
            target=target,
            detail=detail,
            now=now,
        )

    # ---- reads ------------------------------------------------------------

    def get_no_change_judgment(self, judgment_id: str) -> NoChangeJudgment | None:
        """One judgment by id, or None."""
        row = self._execute(_BY_JUDGMENT_ID, (judgment_id,)).fetchone()
        return None if row is None else _row_to_judgment(row)

    def get_no_change_judgment_for_run(
        self, *, run_id: str, attempt_number: int
    ) -> NoChangeJudgment | None:
        """The judgment one attempt produced, or None."""
        row = self._execute(_BY_RUN_ATTEMPT, (run_id, attempt_number)).fetchone()
        return None if row is None else _row_to_judgment(row)

    def live_no_change_judgment(self, lineage_key: str) -> NoChangeJudgment | None:
        """The judgment that currently owns this item's outcome, or None."""
        row = self._execute(_LIVE_FOR_LINEAGE, (lineage_key,)).fetchone()
        return None if row is None else _row_to_judgment(row)

    def list_no_change_judgments(self) -> tuple[NoChangeJudgment, ...]:
        """Every judgment, oldest first."""
        return tuple(_row_to_judgment(r) for r in self._execute(_ALL_JUDGMENTS))

    def list_outcome_sync_jobs(self, judgment_id: str) -> tuple[OutcomeSyncJob, ...]:
        """The sync jobs of one judgment, in the order they were enqueued."""
        rows = self._execute(_SYNC_FOR_JUDGMENT, (judgment_id,))
        return tuple(_row_to_sync(r) for r in rows)

    def list_dependency_effects(self, judgment_id: str) -> tuple[DependencyEffect, ...]:
        """The dependency effects of one judgment, in ordinal order."""
        rows = self._execute(_EFFECT_FOR_JUDGMENT, (judgment_id,))
        return tuple(_row_to_effect(r) for r in rows)

    def next_dependency_effect(self, judgment_id: str) -> DependencyEffect | None:
        """The lowest unsettled dependent, and nothing beyond it.

        One at a time is the invariant: a dependent that is still ``applying``
        keeps its place, so a restarted host resumes the row it interrupted
        rather than moving on and exposing the next dependent early.
        """
        row = self._execute(_NEXT_EFFECT, (judgment_id,)).fetchone()
        return None if row is None else _row_to_effect(row)

    def legacy_no_change_views(self) -> tuple[LegacyNoChangeView, ...]:
        """Pre-I10A zero-change rows, read as ``unknown``. Writes nothing."""
        views = []
        for row in self._execute(_LEGACY_SELECT):
            keys = set(row.keys())

            def col(name: str, present: set[str] = keys, r: sqlite3.Row = row) -> Any:
                return r[name] if name in present else None

            view = decode_legacy_no_change(
                status=row["status"],
                run_id=row["run_id"],
                attempt_number=col("attempt_number") or 1,
                pr_url=col("pr_url"),
                item_id=col("item_id"),
            )
            if view is not None:
                views.append(view)
        return tuple(views)

    # ---- creation ---------------------------------------------------------

    def open_no_change_judgment(
        self,
        *,
        intent: NoChangeIntent,
        policy: str = DEFAULT_NO_CHANGE_POLICY,
        now: datetime,
    ) -> NoChangeJudgment:
        """Persist one zero-diff exit as a candidate, refusing a second live one.

        Idempotent for an identical intent: the judgment id is derived from the
        lineage, run and attempt, so a creation retried after a crash returns
        the row that already exists instead of forking the item's outcome.
        """
        intent.validate()
        _require_policy(policy)
        return self._with_busy_retry(
            "open_no_change_judgment",
            lambda: self._open_judgment(intent, policy=policy, stamp=_iso(now)),
        )

    def _open_judgment(
        self, intent: NoChangeIntent, *, policy: str, stamp: str
    ) -> NoChangeJudgment:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                judgment = self._open_judgment_in_transaction(intent, policy, stamp)
                self._conn.commit()
                return judgment
            except BaseException:
                self._conn.rollback()
                raise

    def _open_judgment_in_transaction(
        self, intent: NoChangeIntent, policy: str, stamp: str
    ) -> NoChangeJudgment:
        existing = self._conn.execute(_BY_JUDGMENT_ID, (intent.judgment_id,)).fetchone()
        if existing is not None:
            return _same_or_conflict(_row_to_judgment(existing), intent, policy)
        live = self._conn.execute(_LIVE_FOR_LINEAGE, (intent.lineage_key,)).fetchone()
        if live is not None:
            raise NoChangeConflictError(
                f"item {intent.lineage_key} already has a live no-change judgment"
                f" {live['judgment_id']} in state {live['state']}"
            )
        row = self._conn.execute(_MAX_GENERATION, (intent.lineage_key,)).fetchone()
        generation = int(row["g"] or 0) + 1
        values = (
            intent.judgment_id,
            intent.lineage_key,
            generation,
            intent.source_kind,
            intent.source_id,
            intent.item_id,
            intent.run_id,
            intent.attempt_number,
            intent.base_sha,
            CANDIDATE,
            policy,
            None,
            None,
            "",
            None,
            None,
            None,
            None,
            stamp,
            stamp,
        )
        self._conn.execute(_JUDGMENT_INSERT, values)
        created = self._conn.execute(_BY_JUDGMENT_ID, (intent.judgment_id,)).fetchone()
        return _row_to_judgment(created)

    # ---- judgment transitions ---------------------------------------------

    def _judgment_or_refuse(self, judgment_id: str) -> NoChangeJudgment:
        judgment = self.get_no_change_judgment(judgment_id)
        if judgment is None:
            raise NoChangeValueError(f"no no-change judgment {judgment_id!r}")
        return judgment

    def _move_judgment(
        self,
        judgment: NoChangeJudgment,
        *,
        target: str,
        reason: str,
        now: datetime,
        decided_by: str | None = None,
        evidence: NoChangeEvidence | None = None,
        contract_digest: str | None = None,
    ) -> NoChangeJudgment:
        """Record the evidence, then compare-and-set the row onto *target*."""
        validate_transition(current=judgment.state, target=target)
        event_id = self._record_state_evidence(
            fact=JUDGMENT_STATE_FACT,
            subject=judgment.judgment_id,
            judgment=judgment,
            target=target,
            detail=reason,
            now=now,
        )
        stamp = _iso(now)
        params = (
            target,
            reason,
            decided_by,
            stamp if decided_by else None,
            None if evidence is None else evidence.digest,
            None if evidence is None else evidence.to_json(),
            contract_digest,
            event_id,
            stamp,
            judgment.judgment_id,
            judgment.state,
        )
        return self._cas(
            "move_no_change_judgment",
            _UPDATE_JUDGMENT,
            params,
            reader=lambda: self._judgment_or_refuse(judgment.judgment_id),
            what=f"no-change judgment {judgment.judgment_id}",
            expected=judgment.state,
        )

    def _cas(
        self,
        operation: str,
        sql: str,
        params: tuple[Any, ...],
        *,
        reader: Callable[[], _T],
        what: str,
        expected: str,
    ) -> _T:
        """One compare-and-set write, refusing a row somebody else moved."""

        def run() -> _T:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    changed = self._conn.execute(sql, params).rowcount
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise
            if not changed:
                raise NoChangeStateError(
                    f"{what} was not in {expected} when the move was applied"
                )
            return reader()

        return self._with_busy_retry(operation, run)

    def attest_no_change(
        self, *, judgment_id: str, evidence: NoChangeEvidence, now: datetime
    ) -> NoChangeJudgment:
        """Bind host evidence to a candidate, or name every reason it cannot."""
        judgment = self._judgment_or_refuse(judgment_id)
        gaps = evidence.gaps()
        if gaps:
            raise NoChangeValueError(
                f"{judgment_id} cannot be attested: " + "; ".join(gaps)
            )
        return self._move_judgment(
            judgment,
            target=ATTESTED,
            reason="host evidence attests that there was nothing to change",
            evidence=evidence,
            now=now,
        )

    def decide_no_change(
        self,
        *,
        judgment_id: str,
        contract: AlreadySatisfiedContract | None = None,
        now: datetime,
    ) -> NoChangeJudgment:
        """Resolve the row's policy against its attested evidence.

        The outcome is ``authorized`` only where the policy authorizes with no
        person involved. Everything else lands in ``pending_confirmation``
        carrying the reason, including ``never_accept`` — which lands there and
        stays, because :meth:`confirm_no_change` refuses it too.
        """
        judgment = self._judgment_or_refuse(judgment_id)
        if judgment.state != ATTESTED:
            raise NoChangeStateError(
                f"{judgment_id} is {judgment.state}, so there is no attested"
                " evidence to decide on"
            )
        evidence = judgment.evidence
        if evidence is None:  # pragma: no cover - attested implies evidence
            raise NoChangeStateError(f"{judgment_id} carries no attested evidence")
        decision = resolve_authorization(
            policy=judgment.policy,
            evidence=evidence,
            item_id=judgment.item_id,
            contract=contract,
        )
        return self._move_judgment(
            judgment,
            target=decision.state,
            reason=decision.reason,
            contract_digest=(
                contract.digest
                if contract is not None and decision.state == AUTHORIZED
                else None
            ),
            now=now,
        )

    def confirm_no_change(
        self, *, judgment_id: str, actor_id: str, now: datetime
    ) -> NoChangeJudgment:
        """A person authorizes a pending no-change outcome.

        Refused where the policy or the evidence makes a human confirmation
        inadmissible: ``never_accept`` is never satisfied, and evidence with
        gaps is not something a person may confirm past.
        """
        judgment = self._judgment_or_refuse(judgment_id)
        evidence = judgment.evidence
        if judgment.state != PENDING_CONFIRMATION or evidence is None:
            raise NoChangeStateError(
                f"{judgment_id} is {judgment.state}, so there is nothing to confirm"
            )
        decision = resolve_authorization(
            policy=judgment.policy, evidence=evidence, item_id=judgment.item_id
        )
        if not decision.human_confirmable:
            raise NoChangeStateError(
                f"{judgment_id} cannot be confirmed by a person: {decision.reason}"
            )
        return self._move_judgment(
            judgment,
            target=AUTHORIZED,
            reason=f"confirmed by {actor_id}",
            decided_by=actor_id,
            now=now,
        )

    def rebase_no_change_judgment(
        self, *, judgment_id: str, expected_base_sha: str, base_sha: str, now: datetime
    ) -> NoChangeJudgment:
        """Move a judgment still waiting on a person onto a new base (#2828).

        The state does not move: a person rejudged the base, and confirming
        the outcome is still their separate decision. Compare-and-set on the
        base it was read with, so two rejudges cannot both claim one move.
        """
        judgment = self._judgment_or_refuse(judgment_id)
        if judgment.state != PENDING_CONFIRMATION:
            raise NoChangeStateError(
                f"{judgment_id} is {judgment.state}, so there is nothing to rejudge"
            )
        return self._cas(
            "rebase_no_change_judgment",
            _REBASE_JUDGMENT,
            (base_sha, _iso(now), judgment_id, PENDING_CONFIRMATION, expected_base_sha),
            reader=lambda: self._judgment_or_refuse(judgment_id),
            what=f"no-change judgment {judgment_id} on base {expected_base_sha}",
            expected=PENDING_CONFIRMATION,
        )

    def reject_no_change(
        self, *, judgment_id: str, actor_id: str, reason: str, now: datetime
    ) -> NoChangeJudgment:
        """Settle a judgment as refused. Reachable from every live state."""
        judgment = self._judgment_or_refuse(judgment_id)
        return self._move_judgment(
            judgment,
            target=REJECTED,
            reason=f"rejected by {actor_id}: {reason}",
            decided_by=actor_id,
            now=now,
        )

    # ---- outcome synchronization ------------------------------------------

    def enqueue_outcome_sync(
        self,
        *,
        judgment_id: str,
        targets: Sequence[tuple[str, str]],
        now: datetime,
    ) -> tuple[OutcomeSyncJob, ...]:
        """Record one generation-bound sync intent per authority. No writes.

        Idempotent for the same target set; a different set is a conflict
        rather than a second intent, because two intent sets for one authorized
        outcome is how one authority gets told and another silently does not.
        """
        judgment = self._judgment_or_refuse(judgment_id)
        if judgment.state not in (AUTHORIZED, SYNC_PENDING):
            raise NoChangeStateError(
                f"{judgment_id} is {judgment.state}, so no outcome may be synchronized"
            )
        if not targets:
            raise NoChangeValueError("an outcome sync needs at least one target")
        existing = self.list_outcome_sync_jobs(judgment_id)
        if existing:
            # The intents are already durable. The judgment may still be
            # `authorized` — a host that died between the two writes comes back
            # here — so the state move below runs either way rather than
            # returning early and leaving the judgment stranded.
            _refuse_different_targets(existing, targets, judgment_id)
        else:
            self._insert_sync_jobs(judgment, targets, _iso(now))
        if judgment.state == AUTHORIZED:
            self._move_judgment(
                judgment,
                target=SYNC_PENDING,
                reason=f"{len(targets)} outcome sync job(s) recorded",
                now=now,
            )
        return self.list_outcome_sync_jobs(judgment_id)

    def _insert_sync_jobs(
        self,
        judgment: NoChangeJudgment,
        targets: Sequence[tuple[str, str]],
        stamp: str,
    ) -> None:
        rows = [
            (
                outcome_sync_job_id(
                    judgment.judgment_id, judgment.generation, kind, ref
                ),
                judgment.judgment_id,
                judgment.generation,
                kind,
                ref,
                sync_idempotency_key(
                    judgment.judgment_id, judgment.generation, kind, ref
                ),
                SYNC_INTENT,
                None,
                None,
                None,
                None,
                "",
                None,
                stamp,
                stamp,
            )
            for kind, ref in targets
        ]
        self._insert_all("enqueue_outcome_sync", _SYNC_INSERT, rows)

    def _insert_all(
        self, operation: str, sql: str, rows: Sequence[tuple[Any, ...]]
    ) -> None:
        """All the intents, or none of them."""

        def run() -> None:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.executemany(sql, rows)
                    self._conn.commit()
                except BaseException:
                    self._conn.rollback()
                    raise

        self._with_busy_retry(operation, run)

    def _sync_or_refuse(
        self, sync_job_id: str
    ) -> tuple[OutcomeSyncJob, NoChangeJudgment]:
        """The job and its judgment, refusing a superseded generation."""
        row = self._execute(_SYNC_BY_ID, (sync_job_id,)).fetchone()
        if row is None:
            raise NoChangeValueError(f"no outcome sync job {sync_job_id!r}")
        job = _row_to_sync(row)
        judgment = self._judgment_or_refuse(job.judgment_id)
        current = self._execute(_MAX_GENERATION, (judgment.lineage_key,)).fetchone()
        if (
            job.generation != int(current["g"] or 0)
            or judgment.state in TERMINAL_STATES
        ):
            raise NoChangeStateError(
                f"{sync_job_id} belongs to generation {job.generation} of"
                f" {judgment.lineage_key}, which is no longer the live decision"
            )
        return job, judgment

    def _move_sync(
        self,
        job: OutcomeSyncJob,
        judgment: NoChangeJudgment,
        *,
        target: str,
        reason: str,
        now: datetime,
        observation: tuple[str | None, str | None] | None = None,
    ) -> OutcomeSyncJob:
        validate_outcome_sync_transition(current=job.state, target=target)
        event_id = self._record_state_evidence(
            fact=SYNC_STATE_FACT,
            subject=job.sync_job_id,
            judgment=judgment,
            target=target,
            detail=reason,
            now=now,
        )
        stamp = _iso(now)
        kind, ref = observation or (job.observation_kind, job.observation_ref)
        params = (
            target,
            kind,
            ref,
            stamp if target == SYNC_OBSERVED else job.observed_at,
            stamp if target == SYNC_ACKED else job.acked_at,
            reason,
            event_id,
            stamp,
            job.sync_job_id,
            job.state,
        )
        return self._cas(
            "move_outcome_sync",
            _UPDATE_SYNC,
            params,
            reader=lambda: _row_to_sync(
                self._execute(_SYNC_BY_ID, (job.sync_job_id,)).fetchone()
            ),
            what=f"outcome sync job {job.sync_job_id}",
            expected=job.state,
        )

    def observe_outcome_sync(
        self,
        *,
        sync_job_id: str,
        observation_kind: str,
        observation_ref: str | None = None,
        now: datetime,
    ) -> OutcomeSyncJob:
        """Record what the authority actually showed. Precedes every ack."""
        job, judgment = self._sync_or_refuse(sync_job_id)
        return self._move_sync(
            job,
            judgment,
            target=SYNC_OBSERVED,
            reason=f"observed {observation_kind}",
            observation=(observation_kind, observation_ref),
            now=now,
        )

    def ack_outcome_sync(
        self, *, sync_job_id: str, now: datetime, detail: str = ""
    ) -> OutcomeSyncJob:
        """Acknowledge an observed synchronization, and settle it durably."""
        job, judgment = self._sync_or_refuse(sync_job_id)
        if job.state == SYNC_ACKED:
            # A host that died between the ack and the judgment advance comes
            # back here: the settle is already durable, so re-run only the
            # (idempotent) advance rather than raising on an acked->acked
            # transition and stranding the judgment in `sync_pending`.
            # Symmetric with enqueue_outcome_sync's resume. A judgment that
            # already advanced past `sync_pending` is refused earlier by
            # _sync_or_refuse (terminal) or no-ops in _advance_after_sync.
            acked = job
        else:
            acked = self._move_sync(
                job,
                judgment,
                target=SYNC_ACKED,
                reason=detail
                or f"{job.target_kind} acknowledged {job.observation_kind}",
                now=now,
            )
        self._advance_after_sync(judgment, now)
        return acked

    def _advance_after_sync(self, judgment: NoChangeJudgment, now: datetime) -> None:
        """When the last sync settles, the judgment owes only its dependents."""
        if self._count(_SYNC_UNSETTLED, judgment.judgment_id):
            return
        current = self._judgment_or_refuse(judgment.judgment_id)
        if current.state != SYNC_PENDING:
            return
        outstanding = self._count(_COUNT_UNSETTLED_EFFECTS, judgment.judgment_id)
        self._move_judgment(
            current,
            target=EFFECT_PENDING if outstanding else COMPLETED,
            reason=(
                f"every authority acknowledged; {outstanding} dependent(s) outstanding"
                if outstanding
                else "every authority acknowledged and no dependent is owed an effect"
            ),
            now=now,
        )

    def _count(self, sql: str, judgment_id: str) -> int:
        return int(self._execute(sql, (judgment_id,)).fetchone()["n"])

    # ---- per-dependent effects --------------------------------------------

    def enqueue_dependency_effects(
        self, *, judgment_id: str, dependents: Sequence[str], now: datetime
    ) -> tuple[DependencyEffect, ...]:
        """Record one durable intent per dependent, in a stable order.

        Written before any dependent is touched and before synchronization
        finishes, so the list a restarted host resumes from is the list the
        decision was made against — not one recomputed from a graph that has
        since moved.
        """
        judgment = self._judgment_or_refuse(judgment_id)
        if judgment.state not in (AUTHORIZED, SYNC_PENDING, EFFECT_PENDING):
            raise NoChangeStateError(
                f"{judgment_id} is {judgment.state}, so no dependent is owed an effect"
            )
        existing = self.list_dependency_effects(judgment_id)
        if existing:
            return _converge_dependents(existing, dependents, judgment_id)
        stamp = _iso(now)
        rows = [
            (
                dependency_effect_id(judgment_id, judgment.generation, dependent),
                judgment_id,
                judgment.generation,
                dependent,
                ordinal,
                EFFECT_INTENT,
                None,
                None,
                "",
                None,
                stamp,
                stamp,
            )
            for ordinal, dependent in enumerate(dependents)
        ]
        self._insert_all("enqueue_dependency_effects", _EFFECT_INSERT, rows)
        return self.list_dependency_effects(judgment_id)

    def _effect_or_refuse(
        self, effect_id: str
    ) -> tuple[DependencyEffect, NoChangeJudgment]:
        row = self._execute(_EFFECT_BY_ID, (effect_id,)).fetchone()
        if row is None:
            raise NoChangeValueError(f"no dependency effect {effect_id!r}")
        effect = _row_to_effect(row)
        return effect, self._judgment_or_refuse(effect.judgment_id)

    def _move_effect(
        self,
        effect: DependencyEffect,
        judgment: NoChangeJudgment,
        *,
        target: str,
        reason: str,
        now: datetime,
    ) -> DependencyEffect:
        validate_dependency_effect_transition(current=effect.state, target=target)
        event_id = self._record_state_evidence(
            fact=EFFECT_STATE_FACT,
            subject=effect.effect_id,
            judgment=judgment,
            target=target,
            detail=reason,
            now=now,
        )
        stamp = _iso(now)
        params = (
            target,
            stamp if target == EFFECT_APPLYING else effect.applied_at,
            stamp if target == EFFECT_ACKED else effect.acked_at,
            reason,
            event_id,
            stamp,
            effect.effect_id,
            effect.state,
        )
        return self._cas(
            "move_dependency_effect",
            _UPDATE_EFFECT,
            params,
            reader=lambda: _row_to_effect(
                self._execute(_EFFECT_BY_ID, (effect.effect_id,)).fetchone()
            ),
            what=f"dependency effect {effect.effect_id}",
            expected=effect.state,
        )

    def begin_dependency_effect(
        self, *, effect_id: str, now: datetime
    ) -> DependencyEffect:
        """Claim one dependent's effect. Refused for a row already applying.

        The refusal is the crash-safety property: a host that died mid-apply
        left ``applying`` behind, and the only safe next move is to observe
        what happened and acknowledge it — never to apply a second time.
        """
        effect, judgment = self._effect_or_refuse(effect_id)
        return self._move_effect(
            effect,
            judgment,
            target=EFFECT_APPLYING,
            reason=f"applying the no-change effect to {effect.dependent_id}",
            now=now,
        )

    def ack_dependency_effect(
        self, *, effect_id: str, now: datetime, detail: str = ""
    ) -> DependencyEffect:
        """Acknowledge one dependent, and expose the next one only then."""
        effect, judgment = self._effect_or_refuse(effect_id)
        if effect.state == EFFECT_ACKED:
            # Resume a host that died between the ack and the completion move:
            # the ack is already durable, so re-run only the (idempotent)
            # completion rather than raising on an acked->acked transition and
            # stranding the judgment in `effect_pending`. Symmetric with the
            # outcome-sync ack; a completed judgment no-ops in
            # _complete_after_effects.
            acked = effect
        else:
            acked = self._move_effect(
                effect,
                judgment,
                target=EFFECT_ACKED,
                reason=detail or f"{effect.dependent_id} acknowledged the no-change",
                now=now,
            )
        self._complete_after_effects(judgment, now)
        return acked

    def _complete_after_effects(
        self, judgment: NoChangeJudgment, now: datetime
    ) -> None:
        if self._count(_COUNT_UNSETTLED_EFFECTS, judgment.judgment_id):
            return
        current = self._judgment_or_refuse(judgment.judgment_id)
        if current.state != EFFECT_PENDING:
            return
        self._move_judgment(
            current,
            target=COMPLETED,
            reason="every dependent acknowledged the no-change outcome",
            now=now,
        )


def _refuse_different_targets(
    existing: tuple[OutcomeSyncJob, ...],
    targets: Sequence[tuple[str, str]],
    judgment_id: str,
) -> None:
    """A re-enqueue must name the intents that are already recorded."""
    if [(j.target_kind, j.target_ref) for j in existing] != [
        (kind, ref) for kind, ref in targets
    ]:
        raise NoChangeConflictError(
            f"{judgment_id} already has outcome sync jobs for a different target set"
        )


def _converge_dependents(
    existing: tuple[DependencyEffect, ...],
    dependents: Sequence[str],
    judgment_id: str,
) -> tuple[DependencyEffect, ...]:
    if [e.dependent_id for e in existing] != list(dependents):
        raise NoChangeConflictError(
            f"{judgment_id} already has dependency effects for a different"
            " dependent set"
        )
    return existing
