"""The ``publication_job`` surface of ``SqliteRunStore`` (I08).

Every write here is a check-then-act on state another host may be touching, so
every one of them runs inside ``BEGIN IMMEDIATE``: the read that decides whether
a move is legal and the write that makes it are one serialized step. Two hosts
racing to create the first job for a lineage, or to advance the same job, both
resolve to exactly one winner and one loud refusal.

Three rules give the table its meaning:

* **A job may not outrun its receipt.** Creation reads the sealed I07 receipt
  for the run and attempt and refuses an intent that names a different receipt,
  base or tree. Publication intent that contradicts sealed evidence is not
  intent, it is a second story.
* **Transitions are compare-and-set.** :meth:`mark_publication_state` takes the
  state the caller believes the job is in. A worker resuming after a crash
  re-reads the row first; a worker holding a stale belief is refused rather
  than allowed to clobber a newer fact.
* **Reading legacy rows writes nothing.** ``awaiting_publication`` runs are
  decoded into views on the way out (see
  :func:`kew.publication_job.decode_legacy_publication`); no row is backfilled
  on their behalf, because no legacy run has a receipt to back one.

The one gated method is :meth:`next_publication_job` — the selector that would
hand a job to a worker. It refuses until E7 installs publication capacity, so
persisting this lifecycle cannot cause a publication.

:class:`HumanReviewStoreMixin` at the foot of the file adds the I13 surface:
the human-review policy in force, and the risk, effective policy and review
slot of one job — written onto the SAME row, never a second one.

:class:`ReviewCapacityStoreMixin` beneath it adds I14's capacity transaction.
Its rule is the one the other three do not cover: occupancy spans a boundary
this database does not own, so the remote read happens OUTSIDE the transaction
and leaves a typed observation behind, and the transaction refuses to count
from an observation that is unavailable or stale. "We could not ask" never
resolves to "nobody is waiting".
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from kew.human_review import (
    REVERSION_EXPIRED,
    REVIEW_CONFIRMED,
    REVIEW_NOT_REQUIRED,
    REVIEW_PENDING,
    REVIEW_RELEASED,
    REVIEW_REQUIRED,
    REVIEW_STATE_FOR_OUTCOME,
    ArtifactRisk,
    ReviewCapacityError,
    ReviewPolicy,
    ReviewPolicyError,
    RiskConfirmation,
    RiskDecision,
    resolve_policy,
)
from kew.publication_job import (
    AMBIGUOUS,
    SLOT_HELD,
    SLOT_RELEASED,
    SUPERSEDED,
    TERMINAL_STATES,
    LegacyPublicationView,
    PublicationIntent,
    PublicationJob,
    PublicationJobConflictError,
    PublicationJobStateError,
    PublicationJobValueError,
    RemoteObservation,
    RemoteReviewObservation,
    count_review_occupancy,
    decode_legacy_publication,
    require_countable_observation,
    require_publication_jobs_active,
    resolve_observation,
    validate_transition,
)

if TYPE_CHECKING:
    from kew.artifact_receipt import ArtifactReceipt

_T = TypeVar("_T")

_JOB_COLS = (
    "job_id",
    "lineage_key",
    "source_kind",
    "source_id",
    "run_id",
    "attempt_number",
    "branch",
    "base_sha",
    "tree_sha",
    "receipt_digest",
    "message",
    "state",
    "resume_state",
    "head_sha",
    "remote_state",
    "remote_ref",
    "attempt_count",
    "last_step",
    "last_attempt_at",
    "detail",
    "predecessor_job_id",
    "superseded_by",
    "created_at",
    "updated_at",
)

# Every SQL string below is built from these column names and from the
# lifecycle constants — never from caller input, whose values are all bound
# parameters. bandit flags the concatenation sites, so the suppressions live
# here rather than being scattered over the call sites.
_JOB_SELECT = f"SELECT {', '.join(_JOB_COLS)} FROM publication_job"  # nosec B608
_JOB_INSERT = (
    f"INSERT INTO publication_job ({', '.join(_JOB_COLS)})"  # nosec B608
    f" VALUES ({', '.join('?' * len(_JOB_COLS))})"
)
_BY_JOB_ID = _JOB_SELECT + " WHERE job_id = ?"  # nosec B608
_BY_ATTEMPT = _JOB_SELECT + " WHERE run_id = ? AND attempt_number = ?"  # nosec B608
_BY_HEAD = _JOB_SELECT + " WHERE branch = ? AND head_sha = ?"  # nosec B608
_LIVE = " state NOT IN ({})".format(
    ", ".join(f"'{s}'" for s in sorted(TERMINAL_STATES))
)
_ACTIVE_FOR_LINEAGE = _JOB_SELECT + f" WHERE lineage_key = ? AND{_LIVE}"  # nosec B608
_NEXT_ACTIVE = _JOB_SELECT + f" WHERE{_LIVE} ORDER BY id ASC LIMIT 1"  # nosec B608
_UPDATE_STATE = (
    "UPDATE publication_job SET state = ?, resume_state = ?, detail = ?,"
    " head_sha = ?, remote_state = ?, remote_ref = ?, updated_at = ?"
    " WHERE job_id = ? AND state = ?"
)
_UPDATE_ATTEMPT = (
    "UPDATE publication_job SET attempt_count = attempt_count + 1,"
    " last_step = ?, last_attempt_at = ?, updated_at = ? WHERE job_id = ?"
)
# Superseding also hands back any review slot the predecessor held. A
# successor SHA means nobody is going to publish the predecessor's tree, so a
# hold left on it would occupy capacity forever — and the release has to be in
# the SAME statement, or a crash between them leaks the slot.
_SUPERSEDE = (
    f"UPDATE publication_job SET state = '{SUPERSEDED}', resume_state = NULL,"  # nosec B608
    f" review_state = CASE WHEN review_state = '{REVIEW_PENDING}'"  # nosec B608
    f" THEN '{REVIEW_RELEASED}' ELSE review_state END,"  # nosec B608
    f" released_at = CASE WHEN review_state = '{REVIEW_PENDING}'"  # nosec B608
    " THEN ? ELSE released_at END,"
    " superseded_by = ?, updated_at = ? WHERE job_id = ? AND state = ?"
)
_LEGACY_SELECT = (
    "SELECT run_id, attempt_number, branch, item_id, pr_url, error_category"
    " FROM runs WHERE status = 'awaiting_publication' ORDER BY started_at ASC"
)


def _iso(value: datetime | str) -> str:
    return value if isinstance(value, str) else value.isoformat()


def _row_to_job(row: sqlite3.Row) -> PublicationJob:
    return PublicationJob(**{col: row[col] for col in _JOB_COLS})


def _refuse(condition: bool, message: str) -> None:
    if not condition:
        raise PublicationJobValueError(message)


def _same_or_conflict(
    existing: PublicationJob, intent: PublicationIntent
) -> PublicationJob:
    """A retry of the SAME intent converges; a different one is a conflict.

    The job id is derived from the lineage, run and attempt, so a caller can
    reach an existing row while naming a different tree, base, receipt or
    message. Returning the old row would quietly publish something other than
    what was asked for, so the disagreement is raised instead.
    """
    differs = next(
        (
            field
            for field in ("base_sha", "tree_sha", "receipt_digest", "message")
            if getattr(existing, field) != getattr(intent, field)
        ),
        None,
    )
    if differs is not None:
        raise PublicationJobConflictError(
            f"publication job {existing.job_id} already exists with a different"
            f" {differs}"
        )
    return existing


class PublicationJobStoreMixin:
    """The ``publication_job`` surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore.
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def get_artifact_receipt(
            self, *, run_id: str, attempt_number: int
        ) -> ArtifactReceipt | None: ...

    # ---- creation ---------------------------------------------------------

    def open_publication_job(
        self, *, intent: PublicationIntent, now: datetime | str
    ) -> PublicationJob:
        """Persist one publication intent, refusing a second live one.

        Idempotent for an identical intent: the job id is derived from the
        lineage, run and attempt, so a creation retried after a crash returns
        the row that already exists instead of forking the lineage.
        """
        intent.validate()
        return self._with_busy_retry(
            "open_publication_job",
            lambda: self._open(intent, stamp=_iso(now), successor=False),
        )

    def open_successor_publication_job(
        self, *, intent: PublicationIntent, now: datetime | str
    ) -> PublicationJob:
        """Take over a lineage from its predecessor, in one transaction.

        A successor SHA for the same branch means the predecessor's tree is no
        longer what the host would publish. Superseding it and creating the
        successor cannot be two steps: a crash between them would leave either
        two live intents or none.
        """
        intent.validate()
        return self._with_busy_retry(
            "open_successor_publication_job",
            lambda: self._open(intent, stamp=_iso(now), successor=True),
        )

    def _open(
        self, intent: PublicationIntent, *, stamp: str, successor: bool
    ) -> PublicationJob:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                job = self._open_in_transaction(
                    intent, stamp=stamp, successor=successor
                )
                self._conn.commit()
                return job
            except BaseException:
                self._conn.rollback()
                raise

    def _open_in_transaction(
        self, intent: PublicationIntent, *, stamp: str, successor: bool
    ) -> PublicationJob:
        existing = self._read_job(intent.job_id)
        if existing is not None:
            return _same_or_conflict(existing, intent)
        self._check_receipt(intent)
        predecessor = self._settle_predecessor(intent, stamp=stamp, successor=successor)
        return self._insert_job(intent, stamp=stamp, predecessor=predecessor)

    def _check_receipt(self, intent: PublicationIntent) -> None:
        """The sealed I07 receipt is the only evidence a job may act on."""
        receipt = self.get_artifact_receipt(
            run_id=intent.run_id, attempt_number=intent.attempt_number
        )
        if receipt is None:
            raise PublicationJobValueError(
                f"run {intent.run_id} attempt {intent.attempt_number} sealed no"
                " artifact receipt to publish"
            )
        _refuse(
            receipt.receipt_digest == intent.receipt_digest,
            "the intent names a different receipt than the one that was sealed",
        )
        _refuse(
            receipt.tree_sha == intent.tree_sha,
            "the intent disagrees with the sealed tree",
        )
        _refuse(
            receipt.base_sha == intent.base_sha,
            "the intent disagrees with the sealed base",
        )
        # The lineage_key the unique-active index and supersession key on is built
        # from the intent's OWN source (source_kind:source_id:branch), so the
        # source must match the sealed receipt or a job could copy a genuine
        # receipt's digest/tree/base while binding an arbitrary, foreign lineage.
        # receipt_digest bakes source into its preimage, so a mismatch here means
        # the persisted lineage and the sealed evidence disagree.
        _refuse(
            receipt.source_kind == intent.source_kind,
            "the intent claims a different source kind than the sealed receipt",
        )
        _refuse(
            receipt.source_id == intent.source_id,
            "the intent claims a different source than the sealed receipt",
        )

    def _settle_predecessor(
        self, intent: PublicationIntent, *, stamp: str, successor: bool
    ) -> str | None:
        live = self._read_active(intent.lineage_key)
        if live is None:
            return None
        if not successor:
            raise PublicationJobConflictError(
                f"lineage {intent.lineage_key} already has an active job"
                f" {live.job_id} in state {live.state}"
            )
        self._execute(
            _SUPERSEDE, (stamp, intent.job_id, stamp, live.job_id, live.state)
        )
        return live.job_id

    def _insert_job(
        self, intent: PublicationIntent, *, stamp: str, predecessor: str | None
    ) -> PublicationJob:
        job = PublicationJob(
            job_id=intent.job_id,
            lineage_key=intent.lineage_key,
            source_kind=intent.source_kind,
            source_id=intent.source_id,
            run_id=intent.run_id,
            attempt_number=intent.attempt_number,
            branch=intent.branch,
            base_sha=intent.base_sha,
            tree_sha=intent.tree_sha,
            receipt_digest=intent.receipt_digest,
            message=intent.message,
            state="pending",
            resume_state=None,
            head_sha=None,
            remote_state=None,
            remote_ref=None,
            attempt_count=0,
            last_step=None,
            last_attempt_at=None,
            detail=None,
            predecessor_job_id=predecessor,
            superseded_by=None,
            created_at=stamp,
            updated_at=stamp,
        )
        self._execute(_JOB_INSERT, tuple(getattr(job, col) for col in _JOB_COLS))
        return job

    # ---- lifecycle --------------------------------------------------------

    def record_publication_attempt(
        self, *, job_id: str, step: str, now: datetime | str
    ) -> PublicationJob:
        """Record "about to do *step*" BEFORE the effect it describes.

        A crash between this write and the effect leaves a row that says an
        attempt was made and nothing that claims it succeeded — which is
        exactly what a restarted host needs to know before it looks at the
        remote.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "record_publication_attempt",
            lambda: self._in_transaction(
                lambda: self._write_attempt(job_id, step=step, stamp=stamp)
            ),
        )

    def _write_attempt(self, job_id: str, *, step: str, stamp: str) -> PublicationJob:
        job = self._require_live(job_id)
        self._execute(_UPDATE_ATTEMPT, (step, stamp, stamp, job_id))
        return replace(
            job,
            attempt_count=job.attempt_count + 1,
            last_step=step,
            last_attempt_at=stamp,
            updated_at=stamp,
        )

    def mark_publication_state(
        self,
        *,
        job_id: str,
        state: str,
        expected_state: str,
        detail: str | None = None,
        head_sha: str | None = None,
        now: datetime | str,
    ) -> PublicationJob:
        """Move one job forward, or to an exit, from the state you believe it is in.

        *expected_state* is mandatory and is compared inside the transaction: a
        writer holding a stale belief is refused rather than allowed to
        overwrite whatever another host just proved.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "mark_publication_state",
            lambda: self._in_transaction(
                lambda: self._write_transition(
                    job_id,
                    state=state,
                    expected_state=expected_state,
                    detail=detail,
                    head_sha=head_sha,
                    stamp=stamp,
                )
            ),
        )

    def _write_transition(
        self,
        job_id: str,
        *,
        state: str,
        expected_state: str | None,
        detail: str | None,
        head_sha: str | None,
        stamp: str,
    ) -> PublicationJob:
        job = self._require_job(job_id)
        if expected_state is not None and job.state != expected_state:
            raise PublicationJobStateError(
                f"publication job {job_id} expected to be {expected_state}"
                f" but is {job.state}"
            )
        validate_transition(
            current=job.state, resume_state=job.resume_state, target=state
        )
        return self._write_row(
            job,
            state=state,
            detail=detail,
            head_sha=head_sha or job.head_sha,
            remote_state=job.remote_state,
            remote_ref=job.remote_ref,
            stamp=stamp,
        )

    def observe_publication_remote(
        self, *, job_id: str, observation: RemoteObservation, now: datetime | str
    ) -> PublicationJob:
        """Record what the forge showed, and move the job to what that proves.

        The observation is the only thing that resolves an ambiguous job: an
        unreadable remote keeps it ambiguous, an absent one returns it to the
        state it interrupted, and a branch or pull request advances it.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "observe_publication_remote",
            lambda: self._in_transaction(
                lambda: self._write_observation(job_id, observation, stamp=stamp)
            ),
        )

    def _write_observation(
        self, job_id: str, observation: RemoteObservation, *, stamp: str
    ) -> PublicationJob:
        job = self._require_live(job_id)
        target = resolve_observation(
            state=job.state, resume_state=job.resume_state, observation=observation
        )
        if target != job.state:
            validate_transition(
                current=job.state, resume_state=job.resume_state, target=target
            )
        return self._write_row(
            job,
            state=target,
            detail=observation.detail or None,
            head_sha=job.head_sha,
            remote_state=observation.kind,
            remote_ref=observation.ref or job.remote_ref,
            stamp=stamp,
        )

    def _write_row(
        self,
        job: PublicationJob,
        *,
        state: str,
        detail: str | None,
        head_sha: str | None,
        remote_state: str | None,
        remote_ref: str | None,
        stamp: str,
    ) -> PublicationJob:
        """The one UPDATE every transition goes through, CAS'd on the old state."""
        resume = (job.resume_state or job.state) if state == AMBIGUOUS else None
        cursor = self._execute(  # nosemgrep
            _UPDATE_STATE,
            (
                state,
                resume,
                detail,
                head_sha,
                remote_state,
                remote_ref,
                stamp,
                job.job_id,
                job.state,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - the txn holds the write lock
            raise PublicationJobStateError(
                f"publication job {job.job_id} changed underneath this write"
            )
        return replace(
            job,
            state=state,
            resume_state=resume,
            detail=detail,
            head_sha=head_sha,
            remote_state=remote_state,
            remote_ref=remote_ref,
            updated_at=stamp,
        )

    def _in_transaction(self, fn: Callable[[], _T]) -> _T:
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = fn()
                self._conn.commit()
                return result
            except BaseException:
                self._conn.rollback()
                raise

    # ---- reads ------------------------------------------------------------

    def _read_job(self, job_id: str) -> PublicationJob | None:
        row = self._conn.execute(_BY_JOB_ID, (job_id,)).fetchone()  # nosemgrep
        return None if row is None else _row_to_job(row)

    def _read_active(self, lineage_key: str) -> PublicationJob | None:
        row = self._conn.execute(  # nosemgrep
            _ACTIVE_FOR_LINEAGE, (lineage_key,)
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def _require_job(self, job_id: str) -> PublicationJob:
        job = self._read_job(job_id)
        if job is None:
            raise PublicationJobStateError(f"no publication job {job_id}")
        return job

    def _require_live(self, job_id: str) -> PublicationJob:
        job = self._require_job(job_id)
        if job.state in TERMINAL_STATES:
            raise PublicationJobStateError(
                f"publication job {job_id} is {job.state} and is settled"
            )
        return job

    def get_publication_job(self, job_id: str) -> PublicationJob | None:
        """One job by id, or None when nothing was ever persisted for it."""
        return self._read_job(job_id)

    def get_normalized_job(self, job_id: str) -> PublicationJob | None:
        """One normalized publication record by id."""
        return self._read_job(job_id)

    def publication_job_for_attempt(
        self, *, run_id: str, attempt_number: int
    ) -> PublicationJob | None:
        """The job one run attempt produced, or None if it produced none."""
        row = self._conn.execute(  # nosemgrep
            _BY_ATTEMPT, (run_id, attempt_number)
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def publication_jobs_for_head(
        self, *, branch: str, head_sha: str, limit: int = 2
    ) -> list[PublicationJob]:
        """Return bounded jobs that attest one exact remote branch head."""
        if limit < 1 or limit > 32:
            raise ValueError("publication head lookup limit must be between 1 and 32")
        rows = self._conn.execute(  # nosemgrep
            _BY_HEAD + " ORDER BY id DESC LIMIT ?", (branch, head_sha, limit)
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def normalized_jobs_for_head(
        self, *, branch: str, head_sha: str, limit: int = 2
    ) -> list[PublicationJob]:
        """Return bounded normalized records for one exact remote head."""
        return self.publication_jobs_for_head(
            branch=branch, head_sha=head_sha, limit=limit
        )

    def active_publication_job(self, lineage_key: str) -> PublicationJob | None:
        """The one live job for a lineage, or None once it has settled."""
        return self._read_active(lineage_key)

    def list_publication_jobs(
        self, *, lineage_key: str | None = None
    ) -> list[PublicationJob]:
        """Every job in creation order, optionally narrowed to one lineage."""
        where = " WHERE lineage_key = ?" if lineage_key is not None else ""
        params: tuple[Any, ...] = () if lineage_key is None else (lineage_key,)
        sql = _JOB_SELECT + where + " ORDER BY id ASC"
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [_row_to_job(row) for row in rows]

    def legacy_publication_backlog(self) -> list[LegacyPublicationView]:
        """Legacy ``awaiting_publication`` runs, decoded and never written.

        These views carry no receipt, no tree and no job id, because no legacy
        run produced any. They exist so a reader can see the outstanding
        publications honestly while I14 owns the writer cutover.
        """
        rows = self._conn.execute(_LEGACY_SELECT).fetchall()  # nosemgrep
        views = [
            decode_legacy_publication(
                status="awaiting_publication",
                run_id=row["run_id"],
                attempt_number=row["attempt_number"],
                branch=row["branch"],
                item_id=row["item_id"],
                pr_url=row["pr_url"],
                error_category=row["error_category"] or "",
            )
            for row in rows
        ]
        return [view for view in views if view is not None]

    # ---- the gate ---------------------------------------------------------

    def next_publication_job(self) -> PublicationJob | None:
        """The oldest job a worker could act on — closed until E7.

        This is the single door between persisted intent and a real commit,
        push or pull request. It refuses loudly rather than returning None
        while the gate is shut, so a future caller cannot mistake "inactive"
        for "no work to do".
        """
        require_publication_jobs_active("select a publication job for effect")
        row = self._conn.execute(_NEXT_ACTIVE).fetchone()  # nosemgrep
        return None if row is None else _row_to_job(row)


@dataclass(frozen=True)
class PublicationReview:
    """The review facts on one publication job row (I13).

    A projection, not a second job: every field here lives on the
    ``publication_job`` row the worker will publish, so the tree a person
    confirmed and the tree that gets pushed cannot drift apart.
    """

    job_id: str
    delivery_repo: str
    review_state: str
    risk_added_lines: int | None
    risk_deleted_lines: int | None
    risk_paths: int | None
    risk_digest: str | None
    risk_reasons: tuple[str, ...]
    policy_hash: str | None
    policy_generation: int | None
    policy_json: str | None
    reserved_at: str | None
    reserved_by: str | None
    released_at: str | None
    confirmed_at: str | None
    confirmed_by: str | None

    @property
    def occupies_slot(self) -> bool:
        """True only while a person is actually holding this publication."""
        return self.review_state == REVIEW_PENDING


_REVIEW_COLS = (
    "job_id",
    "delivery_repo",
    "review_state",
    "risk_added_lines",
    "risk_deleted_lines",
    "risk_paths",
    "risk_digest",
    "risk_reasons",
    "policy_hash",
    "policy_generation",
    "policy_json",
    "reserved_at",
    "reserved_by",
    "released_at",
    "confirmed_at",
    "confirmed_by",
)
_REVIEW_SELECT = (
    f"SELECT {', '.join(_REVIEW_COLS)} FROM publication_job"  # nosec B608
    " WHERE job_id = ?"
)
_RECORD_REVIEW = (
    "UPDATE publication_job SET delivery_repo = ?, review_state = ?,"
    " risk_added_lines = ?, risk_deleted_lines = ?, risk_paths = ?,"
    " risk_digest = ?, risk_reasons = ?, policy_hash = ?,"
    " policy_generation = ?, policy_json = ?, updated_at = ?"
    " WHERE job_id = ? AND state = ?"
)
_RESERVE_REVIEW = (
    f"UPDATE publication_job SET review_state = '{REVIEW_PENDING}',"  # nosec B608
    " reserved_at = ?, reserved_by = ?, released_at = NULL, updated_at = ?"
    " WHERE job_id = ? AND review_state = ?"
)
_CONFIRM_REVIEW = (
    "UPDATE publication_job SET review_state = ?, confirmed_at = ?,"
    " confirmed_by = ?, updated_at = ? WHERE job_id = ? AND review_state = ?"
)
# Occupancy is ALWAYS filtered by delivery_repo. There is deliberately no
# statement in this module that counts holds without one: a global count would
# let a busy repository's backlog stop an unrelated repository's work.
#
# It is deliberately NOT filtered by publication state (I14). A hold is a
# review fact, and a job that reached ``reconciled`` is precisely one whose
# pull request a person is now looking at — dropping it here would let a
# successful publication hand its own slot back the moment it succeeded, and
# would make this count disagree with the one the capacity transaction takes.
_OCCUPANCY = (
    "SELECT COUNT(*) AS holds FROM publication_job"  # nosec B608
    f" WHERE delivery_repo = ? AND review_state = '{REVIEW_PENDING}'"  # nosec B608
)
_POLICY_COLS = (
    "delivery_repo",
    "generation",
    "max_pending_reviews",
    "max_diff_lines",
    "max_paths",
    "approvers",
    "expires_at",
    "set_at",
    "set_by",
    "reason",
    "audit_event_id",
)
_POLICY_SELECT = (
    f"SELECT {', '.join(_POLICY_COLS)} FROM human_review_policy"  # nosec B608
    " WHERE delivery_repo = ?"
)
_POLICY_UPSERT = (
    f"INSERT INTO human_review_policy ({', '.join(_POLICY_COLS)})"  # nosec B608
    f" VALUES ({', '.join('?' * len(_POLICY_COLS))})"
    " ON CONFLICT(delivery_repo) DO UPDATE SET"
    " generation = excluded.generation,"
    " max_pending_reviews = excluded.max_pending_reviews,"
    " max_diff_lines = excluded.max_diff_lines, max_paths = excluded.max_paths,"
    " approvers = excluded.approvers, expires_at = excluded.expires_at,"
    " set_at = excluded.set_at, set_by = excluded.set_by,"
    " reason = excluded.reason, audit_event_id = excluded.audit_event_id"
)
_POLICY_DELETE = "DELETE FROM human_review_policy WHERE delivery_repo = ?"

#: The reversion is attributed to the policy itself, not to whoever happened to
#: be looking when the clock passed the expiry — nobody made a decision here.
_REVERSION_ACTOR = ("policy", "human-review-expiry")
_REVERSION_EVENT = "human_review_policy_reverted"


def _row_to_review(row: sqlite3.Row) -> PublicationReview:
    fields: dict[str, Any] = {col: row[col] for col in _REVIEW_COLS}
    raw = fields.pop("risk_reasons")
    return PublicationReview(
        risk_reasons=tuple(json.loads(raw)) if raw else (), **fields
    )


def _row_to_policy(row: sqlite3.Row) -> ReviewPolicy:
    return ReviewPolicy(
        delivery_repo=row["delivery_repo"],
        max_pending_reviews=row["max_pending_reviews"],
        max_diff_lines=row["max_diff_lines"],
        max_paths=row["max_paths"],
        approvers=tuple(json.loads(row["approvers"] or "[]")),
        generation=row["generation"],
        expires_at=row["expires_at"],
    )


def _require_repo(delivery_repo: str) -> str:
    """No capacity question may be asked without naming whose capacity it is."""
    if not delivery_repo:
        raise ReviewPolicyError(
            "review capacity is counted per canonical GitHub delivery"
            " repository; there is no global count"
        )
    return delivery_repo


# ---------------------------------------------------------------------------
# I14: the atomic capacity transaction.
#
# Reserving a review slot is a check-then-act over facts that live in two
# different places — this database, and a GitHub repository nobody here owns.
# The remote read is a network call, so it happens BEFORE the transaction and
# leaves a typed observation behind (:meth:`record_review_observation`). The
# transaction then takes the slot with no network call at all: it demands the
# fresh, successful observation generation the caller just reconciled, resolves
# any expired policy override (appending the system reversion on the way),
# counts occupancy for this job's delivery repository and nobody else's, and
# writes the hold. Two hosts finishing at the same instant serialize on
# ``BEGIN IMMEDIATE``, so the second one counts the first one's hold.
# ---------------------------------------------------------------------------

#: Where one repository's last remote observation lives. Keyed by repository
#: because occupancy is, so one repository's outage can never be read as
#: another's.
_OBSERVATION_KEY = "publication.review_observation:"
_META_SELECT = "SELECT value FROM meta WHERE key = ?"
_META_UPSERT = (
    "INSERT INTO meta (key, value) VALUES (?, ?)"
    " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)
#: The lineages this host is holding for one repository. Lineage, not job id:
#: a successor SHA inherits its predecessor's place in the queue rather than
#: taking a second one.
#:
#: Deliberately unfiltered by publication state, unlike I13's :data:`_OCCUPANCY`.
#: A hold is a *review* fact: the pull request a reconciled job successfully
#: opened is exactly the thing a person is now looking at, so reaching the end
#: of the publication lifecycle must not quietly hand the slot back. It is
#: given back by :meth:`release_publication_capacity`, or by the supersession
#: statement above, and by nothing else.
_HELD_LINEAGES = (
    "SELECT lineage_key FROM publication_job"  # nosec B608
    f" WHERE delivery_repo = ? AND review_state = '{REVIEW_PENDING}'"  # nosec B608
)
_LINEAGE_BY_JOB = (
    "SELECT job_id, lineage_key FROM publication_job WHERE delivery_repo = ?"
)
_RELEASE_REVIEW = (
    "UPDATE publication_job SET review_state = ?, released_at = ?, updated_at = ?"
    f" WHERE job_id = ? AND review_state = '{REVIEW_PENDING}'"  # nosec B608
)

#: The states a publication may take *publication* capacity from. Not
#: ``required``: that hold is I13's, taken so a person can look at the risk,
#: and released by their confirmation. This one is taken immediately before the
#: remote effect and kept for the whole time the pull request is in front of a
#: reviewer.
_CAPACITY_READY = {
    REVIEW_NOT_REQUIRED: REVIEW_NOT_REQUIRED,
    REVIEW_CONFIRMED: REVIEW_CONFIRMED,
}

#: How old a remote observation may be and still be reserved against. Short:
#: the whole point is that the count reflects the forge as it is now, and a
#: caller that finds it stale simply reconciles again.
DEFAULT_OBSERVATION_MAX_AGE_SECONDS = 300.0


@dataclass(frozen=True)
class CapacityReservation:
    """One taken slot, with everything that justified taking it."""

    review: PublicationReview
    policy: ReviewPolicy
    observation: RemoteReviewObservation
    occupancy: int


class ReviewCapacityStoreMixin:
    """Reconciled remote truth, and the transaction that reserves against it."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _in_transaction(self, fn: Callable[[], _T]) -> _T: ...

        def _require_live(self, job_id: str) -> PublicationJob: ...

        def _read_review_or_raise(self, job_id: str) -> PublicationReview: ...

        def _resolve(self, system: ReviewPolicy, now: datetime) -> ReviewPolicy: ...

    # ---- the reconciled observation (written outside any capacity txn) ----

    def review_observation(self, delivery_repo: str) -> RemoteReviewObservation | None:
        """The last thing this host saw the delivery repository holding."""
        row = self._conn.execute(  # nosemgrep
            _META_SELECT, (_OBSERVATION_KEY + _require_repo(delivery_repo),)
        ).fetchone()
        return None if row is None else RemoteReviewObservation.from_json(row["value"])

    def next_review_observation_generation(self, delivery_repo: str) -> int:
        """The generation the next reconciliation should claim."""
        current = self.review_observation(delivery_repo)
        return 0 if current is None else current.generation + 1

    def record_review_observation(
        self, observation: RemoteReviewObservation
    ) -> RemoteReviewObservation:
        """Persist what the forge showed, refusing a non-advancing generation.

        Monotonic so that a slow reconciliation cannot overwrite a newer one
        and hand a reservation an observation older than the one it asked for.
        An *unavailable* observation is persisted like any other: recording
        that the forge could not be read is the fact that stops the next
        reservation, and dropping it would silently leave stale truth standing.
        """
        observation.validate()
        return self._with_busy_retry(
            "record_review_observation",
            lambda: self._in_transaction(
                lambda: self._write_observation_row(observation)
            ),
        )

    def _write_observation_row(
        self, observation: RemoteReviewObservation
    ) -> RemoteReviewObservation:
        current = self.review_observation(observation.delivery_repo)
        if current is not None and observation.generation <= current.generation:
            raise PublicationJobValueError(
                f"observation generation {observation.generation} does not"
                f" supersede {current.generation} for {observation.delivery_repo}"
            )
        self._execute(
            _META_UPSERT,
            (_OBSERVATION_KEY + observation.delivery_repo, observation.as_json()),
        )
        return observation

    # ---- occupancy --------------------------------------------------------

    def review_capacity_occupancy(
        self, *, delivery_repo: str, observation: RemoteReviewObservation
    ) -> int:
        """Local holds unioned with observed remote obligations, by lineage."""
        repo = _require_repo(delivery_repo)
        held = [
            row["lineage_key"]
            for row in self._conn.execute(_HELD_LINEAGES, (repo,))  # nosemgrep
        ]
        lineage_by_job = {
            row["job_id"]: row["lineage_key"]
            for row in self._conn.execute(_LINEAGE_BY_JOB, (repo,))  # nosemgrep
        }
        return count_review_occupancy(
            held_lineages=held,
            obligations=observation.obligations,
            lineage_by_job=lineage_by_job,
        )

    # ---- the transaction --------------------------------------------------

    def reserve_publication_capacity(
        self,
        *,
        job_id: str,
        system_policy: ReviewPolicy,
        expected_generation: int,
        actor: str,
        now: datetime,
        max_observation_age_seconds: float = DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    ) -> CapacityReservation:
        """Take this job's review slot, or refuse without taking anything.

        Makes no network call. *expected_generation* is the generation the
        caller's own reconciliation just persisted, so a reservation cannot ride
        on somebody else's older look at the forge.
        """
        system_policy.validate()
        return self._with_busy_retry(
            "reserve_publication_capacity",
            lambda: self._in_transaction(
                lambda: self._reserve_capacity(
                    job_id,
                    system_policy=system_policy,
                    expected_generation=expected_generation,
                    actor=actor,
                    now=now,
                    max_age=max_observation_age_seconds,
                )
            ),
        )

    def _reserve_capacity(
        self,
        job_id: str,
        *,
        system_policy: ReviewPolicy,
        expected_generation: int,
        actor: str,
        now: datetime,
        max_age: float,
    ) -> CapacityReservation:
        self._require_live(job_id)
        review = self._read_review_or_raise(job_id)
        repo = _require_repo(review.delivery_repo)
        _refuse(
            system_policy.delivery_repo == repo,
            "the system policy governs a different delivery repository than the"
            " job publishes to",
        )
        observation = require_countable_observation(
            self.review_observation(repo),
            delivery_repo=repo,
            expected_generation=expected_generation,
            now=now,
            max_age_seconds=max_age,
        )
        policy = self._resolve(system_policy, now)
        expected_state = _check_capacity_reservable(review, policy)
        occupancy = self.review_capacity_occupancy(
            delivery_repo=repo, observation=observation
        )
        if occupancy >= policy.max_pending_reviews:
            raise ReviewCapacityError(
                f"{repo} is holding {occupancy} of {policy.max_pending_reviews}"
                " reviews; nothing was evicted"
            )
        stamp = _iso(now)
        self._execute(_RESERVE_REVIEW, (stamp, actor, stamp, job_id, expected_state))
        return CapacityReservation(
            review=self._read_review_or_raise(job_id),
            policy=policy,
            observation=observation,
            occupancy=occupancy + 1,
        )

    # ---- the slot's other end ---------------------------------------------

    def release_publication_capacity(
        self, *, job_id: str, disposition: str, now: datetime | str
    ) -> PublicationReview:
        """Hand the slot back, but only for a disposition that frees it.

        ``held`` is not an error and not a no-op worth hiding: it is the answer
        for an open pull request and for a forge nobody could read, and it
        leaves the hold exactly where it was.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "release_publication_capacity",
            lambda: self._in_transaction(
                lambda: self._release_capacity(job_id, disposition, stamp)
            ),
        )

    def _release_capacity(
        self, job_id: str, disposition: str, stamp: str
    ) -> PublicationReview:
        review = self._read_review_or_raise(job_id)
        _refuse(
            disposition in (SLOT_HELD, SLOT_RELEASED),
            f"{disposition!r} is not a review slot disposition",
        )
        if disposition == SLOT_HELD or not review.occupies_slot:
            return review
        self._execute(_RELEASE_REVIEW, (REVIEW_RELEASED, stamp, stamp, job_id))
        return self._read_review_or_raise(job_id)


def _check_capacity_reservable(review: PublicationReview, policy: ReviewPolicy) -> str:
    """Only a job already cleared under THIS policy may take a publication slot.

    Returns the review state the reserving UPDATE compares against, so the
    write is a compare-and-set on the state this check was made against rather
    than on whatever the row happens to say when the UPDATE runs.
    """
    expected = _CAPACITY_READY.get(review.review_state)
    if expected is None:
        raise PublicationJobStateError(
            f"publication job {review.job_id} is {review.review_state} and is not"
            " cleared to reserve publication capacity"
        )
    _refuse(
        review.delivery_repo == policy.delivery_repo,
        "the job's delivery repository is not the one this policy governs",
    )
    _refuse(
        review.policy_hash == policy.policy_hash
        and review.policy_generation == policy.generation,
        "the job was cleared under a different policy generation; measure it"
        " again before reserving",
    )
    return expected


class HumanReviewStoreMixin(ReviewCapacityStoreMixin):
    """Human-review policy and per-job risk, on the existing job row (I13).

    Split from :class:`PublicationJobStoreMixin` only for readability — both
    are composed into ``SqliteRunStore`` and write the same table. Three rules
    hold everything here together:

    * **Capacity belongs to a repository.** Every occupancy read names a
      canonical GitHub delivery repository, and :func:`_require_repo` refuses
      the question when it does not.
    * **Lowering evicts nothing.** A tightened or expired policy is checked at
      reservation time only. Three holds taken under a limit of three stay
      exactly where they are when the limit drops to one; what changes is that
      no fourth — or second — may be taken until occupancy falls below it.
    * **A confirmation is bound to a tree.** :meth:`confirm_publication_review`
      compares the confirmation's base, tree, risk digest and policy
      generation/hash against the row. Remediation produces a successor job
      with a different tree, so the old confirmation cannot follow it.
    """

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _in_transaction(self, fn: Callable[[], _T]) -> _T: ...

        def _require_live(self, job_id: str) -> PublicationJob: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

    # ---- risk and policy on the job row -----------------------------------

    def record_publication_review(
        self,
        *,
        job_id: str,
        policy: ReviewPolicy,
        risk: ArtifactRisk,
        decision: RiskDecision,
        now: datetime | str,
    ) -> PublicationReview:
        """Bind the measured risk and the effective policy to the job.

        Records only — it never takes a slot. Writing what the artifact *is*
        and taking scarce capacity are separate acts, so a host that crashes
        between them leaves a measured job rather than a leaked hold.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "record_publication_review",
            lambda: self._in_transaction(
                lambda: self._write_review(
                    job_id, policy=policy, risk=risk, decision=decision, stamp=stamp
                )
            ),
        )

    def _write_review(
        self,
        job_id: str,
        *,
        policy: ReviewPolicy,
        risk: ArtifactRisk,
        decision: RiskDecision,
        stamp: str,
    ) -> PublicationReview:
        job = self._require_live(job_id)
        _check_review_inputs(job, policy=policy, risk=risk, decision=decision)
        _check_remeasurable(self._read_review_or_raise(job_id))
        self._execute(
            _RECORD_REVIEW,
            (
                policy.delivery_repo,
                REVIEW_STATE_FOR_OUTCOME[decision.outcome],
                risk.added_lines,
                risk.deleted_lines,
                risk.path_count,
                risk.risk_digest,
                json.dumps(list(decision.reasons)),
                policy.policy_hash,
                policy.generation,
                policy.as_json(),
                stamp,
                job_id,
                job.state,
            ),
        )
        return self._read_review_or_raise(job_id)

    def publication_review(self, job_id: str) -> PublicationReview | None:
        """The review facts on one job, or None when no such job exists."""
        row = self._conn.execute(_REVIEW_SELECT, (job_id,)).fetchone()  # nosemgrep
        return None if row is None else _row_to_review(row)

    def _read_review_or_raise(self, job_id: str) -> PublicationReview:
        review = self.publication_review(job_id)
        if review is None:  # pragma: no cover - the txn holds the write lock
            raise PublicationJobStateError(f"no publication job {job_id}")
        return review

    # ---- capacity ---------------------------------------------------------

    def pending_review_count(self, delivery_repo: str) -> int:
        """How many live publications one delivery repository is holding."""
        row = self._conn.execute(  # nosemgrep
            _OCCUPANCY, (_require_repo(delivery_repo),)
        ).fetchone()
        return int(row["holds"])

    def reserve_publication_review(
        self,
        *,
        job_id: str,
        policy: ReviewPolicy,
        actor: str,
        now: datetime | str,
    ) -> PublicationReview:
        """Take one review slot for this job, or refuse without taking any.

        The occupancy read and the write are one ``BEGIN IMMEDIATE`` step, so
        two hosts finishing at the same moment cannot both see the last free
        slot. A refusal raises :class:`ReviewCapacityError` and changes
        nothing — in particular it evicts no existing hold, whatever the limit
        has since been lowered to.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "reserve_publication_review",
            lambda: self._in_transaction(
                lambda: self._reserve(job_id, policy=policy, actor=actor, stamp=stamp)
            ),
        )

    def _reserve(
        self, job_id: str, *, policy: ReviewPolicy, actor: str, stamp: str
    ) -> PublicationReview:
        policy.validate()
        self._require_live(job_id)
        review = self._read_review_or_raise(job_id)
        _check_reservable(review, policy)
        holds = self.pending_review_count(policy.delivery_repo)
        if holds >= policy.max_pending_reviews:
            raise ReviewCapacityError(
                f"{policy.delivery_repo} is holding {holds} of"
                f" {policy.max_pending_reviews} reviews; nothing was evicted"
            )
        self._execute(_RESERVE_REVIEW, (stamp, actor, stamp, job_id, REVIEW_REQUIRED))
        return self._read_review_or_raise(job_id)

    def confirm_publication_review(
        self,
        *,
        job_id: str,
        confirmation: RiskConfirmation,
        now: datetime | str,
    ) -> PublicationReview:
        """Release one hold, for the exact tree and policy it was given on.

        Refuses a confirmation whose base, tree, risk digest, policy hash or
        policy generation disagrees with the row. That is what makes a
        successor SHA start over: the new job carries a different tree, so
        yesterday's approval simply does not match it.
        """
        stamp = _iso(now)
        return self._with_busy_retry(
            "confirm_publication_review",
            lambda: self._in_transaction(
                lambda: self._confirm(job_id, confirmation=confirmation, stamp=stamp)
            ),
        )

    def _confirm(
        self, job_id: str, *, confirmation: RiskConfirmation, stamp: str
    ) -> PublicationReview:
        job = self._require_live(job_id)
        review = self._read_review_or_raise(job_id)
        _check_confirmation(job, review, confirmation)
        self._execute(
            _CONFIRM_REVIEW,
            (
                REVIEW_CONFIRMED,
                confirmation.confirmed_at or stamp,
                confirmation.actor,
                stamp,
                job_id,
                REVIEW_PENDING,
            ),
        )
        return self._read_review_or_raise(job_id)

    # ---- the durable policy override --------------------------------------

    def set_human_review_policy(
        self,
        *,
        policy: ReviewPolicy,
        actor: str,
        reason: str = "",
        audit_event_id: int | None = None,
        now: datetime | str,
    ) -> ReviewPolicy:
        """Persist one durable override, refusing a non-advancing generation."""
        policy.validate()
        stamp = _iso(now)
        return self._with_busy_retry(
            "set_human_review_policy",
            lambda: self._in_transaction(
                lambda: self._write_policy(
                    policy,
                    actor=actor,
                    reason=reason,
                    audit_event_id=audit_event_id,
                    stamp=stamp,
                )
            ),
        )

    def _write_policy(
        self,
        policy: ReviewPolicy,
        *,
        actor: str,
        reason: str,
        audit_event_id: int | None,
        stamp: str,
    ) -> ReviewPolicy:
        current = self.get_human_review_policy(policy.delivery_repo)
        if current is not None and policy.generation <= current.generation:
            raise ReviewPolicyError(
                f"generation {policy.generation} does not supersede"
                f" {current.generation} for {policy.delivery_repo}"
            )
        self._execute(
            _POLICY_UPSERT,
            (
                policy.delivery_repo,
                policy.generation,
                policy.max_pending_reviews,
                policy.max_diff_lines,
                policy.max_paths,
                json.dumps(list(policy.approvers)),
                policy.expires_at,
                stamp,
                actor,
                reason,
                audit_event_id,
            ),
        )
        return policy

    def get_human_review_policy(self, delivery_repo: str) -> ReviewPolicy | None:
        """The durable override for one delivery repository, if any is set."""
        row = self._conn.execute(  # nosemgrep
            _POLICY_SELECT, (_require_repo(delivery_repo),)
        ).fetchone()
        return None if row is None else _row_to_policy(row)

    def effective_human_review_policy(
        self, *, system: ReviewPolicy, now: datetime
    ) -> ReviewPolicy:
        """The policy in force, reverting an expired override on the way.

        The reversion is a chained audit event appended BEFORE the override
        row is cleared, in the same transaction — attest, then act. Reading
        the effective policy is therefore the moment an expiry becomes a
        durable fact, rather than something each host decides privately.
        """
        system.validate()
        return self._with_busy_retry(
            "effective_human_review_policy",
            lambda: self._in_transaction(lambda: self._resolve(system, now)),
        )

    def _resolve(self, system: ReviewPolicy, now: datetime) -> ReviewPolicy:
        override = self.get_human_review_policy(system.delivery_repo)
        resolution = resolve_policy(system=system, override=override, now=now)
        if not resolution.reverted or override is None:
            return resolution.policy
        self._attest_reversion(override, resolution.policy, now=now)
        self._execute(_POLICY_DELETE, (system.delivery_repo,))
        return resolution.policy

    def _attest_reversion(
        self, override: ReviewPolicy, reverted: ReviewPolicy, *, now: datetime
    ) -> None:
        payload = {
            "delivery_repo": reverted.delivery_repo,
            "reason": REVERSION_EXPIRED,
            "expires_at": override.expires_at,
            "from": {**override.facts, "generation": override.generation},
            "to": {**reverted.facts, "generation": reverted.generation},
        }
        actor_type, actor_id = _REVERSION_ACTOR
        self._chained_insert_in_transaction(
            (
                now.isoformat(),
                _REVERSION_EVENT,
                None,
                None,
                reverted.delivery_repo,
                actor_type,
                actor_id,
                None,
                reverted.policy_hash,
                json.dumps(payload),
                None,
            )
        )


def _check_review_inputs(
    job: PublicationJob,
    *,
    policy: ReviewPolicy,
    risk: ArtifactRisk,
    decision: RiskDecision,
) -> None:
    """The risk, the decision and the job must all name the same tree."""
    policy.validate()
    _refuse(
        risk.base_sha == job.base_sha and risk.tree_sha == job.tree_sha,
        "the risk was measured on a different base/tree than the job publishes",
    )
    _refuse(
        decision.risk_digest == risk.risk_digest,
        "the decision was made on a different measurement than the one given",
    )
    _refuse(
        decision.policy_hash == policy.policy_hash
        and decision.policy_generation == policy.generation
        and decision.delivery_repo == policy.delivery_repo,
        "the decision was made under a different policy than the one given",
    )
    _refuse(
        decision.outcome in REVIEW_STATE_FOR_OUTCOME,
        f"{decision.outcome!r} is not a review decision",
    )


#: Re-measuring is only honest before anyone acts on the measurement. Once a
#: slot is held, confirmed or the artifact refused, overwriting the risk would
#: leave a confirmation attached to numbers nobody approved.
_REMEASURABLE = frozenset({REVIEW_NOT_REQUIRED, REVIEW_REQUIRED})


def _check_remeasurable(review: PublicationReview) -> None:
    """A job already acted on may not have its measurement rewritten."""
    if review.review_state not in _REMEASURABLE:
        raise PublicationJobStateError(
            f"publication job {review.job_id} is {review.review_state};"
            " re-measuring it would rewrite the risk someone already acted on"
        )


def _check_reservable(review: PublicationReview, policy: ReviewPolicy) -> None:
    """Only a job already measured under THIS policy may take a slot."""
    if review.review_state != REVIEW_REQUIRED:
        raise PublicationJobStateError(
            f"publication job {review.job_id} is {review.review_state} and has"
            " no review to reserve"
        )
    _refuse(
        review.delivery_repo == policy.delivery_repo,
        "the job's delivery repository is not the one this policy governs",
    )
    _refuse(
        review.policy_hash == policy.policy_hash
        and review.policy_generation == policy.generation,
        "the job was measured under a different policy generation; measure it"
        " again before reserving",
    )


def _check_confirmation(
    job: PublicationJob, review: PublicationReview, confirmation: RiskConfirmation
) -> None:
    """A confirmation covers one tree under one policy edit, or nothing."""
    if review.review_state != REVIEW_PENDING:
        raise PublicationJobStateError(
            f"publication job {job.job_id} is {review.review_state} and is not"
            " being held for review"
        )
    _refuse(
        confirmation.job_id == job.job_id,
        "the confirmation names a different publication job",
    )
    _refuse(
        confirmation.base_sha == job.base_sha and confirmation.tree_sha == job.tree_sha,
        "the confirmation was given for a different tree; remediation that"
        " changes the SHA needs its own confirmation",
    )
    _refuse(
        confirmation.risk_digest == review.risk_digest,
        "the confirmation was given for a different measurement",
    )
    _refuse(
        confirmation.delivery_repo == review.delivery_repo
        and confirmation.policy_hash == review.policy_hash
        and confirmation.policy_generation == review.policy_generation,
        "the policy changed after this hold was taken; the confirmation no"
        " longer covers it",
    )
