"""The project's merge queue: one FIFO of READY heads, kept in the ledger (#2814).

Every operator session on the host shares the one ``dispatch.db``, so a queue
kept here is the one queue: two sessions enqueuing alternately are served in
the order they enqueued, and a reboot loses nothing — the runner simply reads
the queue again. Order is the row id, which SQLite only ever increases.

An entry is ``queued`` until the runner settles it: ``merged``, ``failed`` (its
verification failed, or it does not merge cleanly), ``dropped`` (its head moved
while queued, its PR closed, or main moved under its verification a second
time) or ``removed`` by an operator. Just before its merge call a pass claims
it as ``merging`` and stores the verified base and tree. GitHub's merge answer
makes it ``landed`` until tree validation and report queueing finish. A later
runner resumes either state after a crash. A tree mismatch becomes the terminal
``landed_mismatch`` state, which an operator may remove. A terminal queue
verdict carries an unpredictable comment token until GitHub confirms its
idempotent delivery. A PR has at most one active entry.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

_T = TypeVar("_T")

QUEUED = "queued"
MERGING = "merging"
LANDED = "landed"
LANDED_MISMATCH = "landed_mismatch"
MERGED = "merged"
FAILED = "failed"
DROPPED = "dropped"
REMOVED = "removed"
SETTLED_STATES = frozenset({MERGED, LANDED_MISMATCH, FAILED, DROPPED, REMOVED})
#: The entries still in line: queued, being merged, or awaiting landed validation.
ACTIVE_STATES = (QUEUED, MERGING, LANDED)


@dataclass(frozen=True)
class MergeQueueEntry:
    """One ``merge_queue`` row."""

    id: int
    repo: str
    pr: int
    head: str
    session: str
    state: str
    reason: str | None
    #: How many times main moving under its verification sent it back.
    requeues: int
    enqueued_at: datetime
    updated_at: datetime
    #: The pass that claimed it to merge it; None unless ``merging``.
    owner: str | None
    #: Held-out failures the reviewed PR body declared when this entry was added.
    behavior_changes: tuple[str, ...]
    #: The main and candidate tree bound before the merge call.
    expected_base: str | None
    expected_tree: str | None
    #: The commit GitHub reported after accepting the merge.
    landed_sha: str | None
    #: Unpredictable identity for one idempotent verdict comment.
    report_token: str | None = None
    #: When GitHub confirmed the terminal verdict comment.
    reported_at: datetime | None = None
    #: GitHub may have accepted the PUT whose response was lost.
    merge_unknown: bool = False


#: Why a queue is stopped (``merge_queue_suspects.kind``, v73, #2883).
#: Main moved without a verify reading it: a train stopped part-way, or a pass
#: died while merging. The next pass verifies main first.
SUSPECT_UNVERIFIED = "unverified"
#: A merge landed a tree other than the one verified for it. Nothing verified
#: main red; the queue waits on that landed commit until main moves.
SUSPECT_LANDED_MISMATCH = "landed_mismatch"
#: A verify of main failed; the queue waits on that main until it moves.
SUSPECT_VERIFIED_RED = "verified_red"


@dataclass(frozen=True)
class MergeQueueSuspect:
    """One ``merge_queue_suspects`` row: a queue stopped on an unverified main."""

    repo: str
    #: The first cause of the stop; it stands until the queue resumes.
    reason: str
    #: One of the ``SUSPECT_*`` kinds; None on a row written before v73.
    kind: str | None
    #: The main the queue waits to move from; None while main is unverified.
    blocked_on: str | None
    #: The latest failure behind *blocked_on*: the failed verify of main, or
    #: the landed-tree mismatch. None while main is unverified.
    failure: str | None
    since: datetime

    def state(self) -> str:
        """What is known about main, said without claiming more than that."""
        if self.blocked_on is None:
            return "main not yet verified"
        sha = self.blocked_on[:12]
        if self.kind == SUSPECT_VERIFIED_RED:
            said = f"main {sha} verified red"
        elif self.kind == SUSPECT_LANDED_MISMATCH:
            said = f"main {sha} landed a tree nobody verified"
        else:
            said = f"main {sha} not verified green"
        return f"{said} ({self.failure})" if self.failure else said


def _row_to_entry(row: sqlite3.Row) -> MergeQueueEntry:
    return MergeQueueEntry(
        id=int(row["id"]),
        repo=row["repo"],
        pr=int(row["pr"]),
        head=row["head"],
        session=row["session"],
        state=row["state"],
        reason=row["reason"],
        requeues=int(row["requeues"]),
        enqueued_at=datetime.fromisoformat(row["enqueued_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        owner=row["owner"],
        behavior_changes=tuple(json.loads(row["behavior_changes"])),
        expected_base=row["expected_base"],
        expected_tree=row["expected_tree"],
        landed_sha=row["landed_sha"],
        merge_unknown=bool(row["merge_unknown"]),
        report_token=row["report_token"],
        reported_at=(
            datetime.fromisoformat(row["reported_at"])
            if row["reported_at"] is not None
            else None
        ),
    )


class MergeQueueStoreMixin:
    """The merge-queue persistence surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock
        _db_path: str

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def _merge_queue_txn(self, operation: str, fn: Callable[[], _T]) -> _T:
        def _do() -> _T:
            with self._txn_lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    result = fn()
                    self._conn.commit()
                    return result
                except BaseException:
                    self._conn.rollback()
                    raise

        return self._with_busy_retry(operation, _do)

    def enqueue_merge(
        self,
        repo: str,
        pr: int,
        head: str,
        *,
        session: str,
        behavior_changes: tuple[str, ...],
    ) -> tuple[MergeQueueEntry, bool]:
        """Queue *pr* at *head*; ``(entry, created)``.

        A PR already queued at the same head is that entry, unchanged. Queued at
        another head, it is refused: the operator removes it first, so a head
        nobody re-checked never takes an earlier head's place in the line.
        """
        now = datetime.now(UTC).isoformat()

        def _add() -> tuple[MergeQueueEntry, bool]:
            row = self._conn.execute(
                "SELECT * FROM merge_queue WHERE repo = ? AND pr = ?"
                " AND state IN (?, ?, ?)",
                (repo, pr, *ACTIVE_STATES),
            ).fetchone()
            if row is not None:
                if row["head"] != head:
                    raise ValueError(
                        f"PR #{pr} is already queued at {row['head'][:12]}; "
                        "remove it before queueing another head"
                    )
                return _row_to_entry(row), False
            cursor = self._conn.execute(
                "INSERT INTO merge_queue (repo, pr, head, session, state,"
                " behavior_changes, requeues, enqueued_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    repo,
                    pr,
                    head,
                    session,
                    QUEUED,
                    json.dumps(behavior_changes),
                    now,
                    now,
                ),
            )
            new = self._conn.execute(
                "SELECT * FROM merge_queue WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _row_to_entry(new), True

        return self._merge_queue_txn("enqueue_merge", _add)

    def list_merge_queue(
        self, repo: str, *, include_settled: bool = False
    ) -> list[MergeQueueEntry]:
        """The queue (queued and claimed) in FIFO order; settled too when asked."""
        sql = "SELECT * FROM merge_queue WHERE repo = ?"
        params: tuple[object, ...] = (repo,)
        if not include_settled:
            sql += " AND state IN (?, ?, ?)"
            params = (repo, *ACTIVE_STATES)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def settle_merge_entry(
        self,
        entry_id: int,
        state: str,
        reason: str | None,
        *,
        report_pending: bool = False,
    ) -> bool:
        """Move an entry still in line to *state*; False when it is not.

        ``removed`` takes a ``queued`` or terminal ``landed_mismatch`` entry.
        An operator's remove must not race an active merge call.
        A queue verdict commits its pending comment in the same transaction.
        """
        if state not in SETTLED_STATES:
            raise ValueError(f"not a settled merge-queue state: {state!r}")
        now = datetime.now(UTC).isoformat()
        reported_at = None if report_pending else now
        report_token = secrets.token_hex(16) if report_pending else None

        def _settle() -> bool:
            if state == REMOVED:
                cursor = self._conn.execute(
                    "UPDATE merge_queue SET state = ?, reason = ?, owner = NULL,"
                    " report_token = ?, reported_at = ?, updated_at = ?"
                    " WHERE id = ? AND state IN (?, ?)",
                    (
                        state,
                        reason,
                        report_token,
                        reported_at,
                        now,
                        entry_id,
                        QUEUED,
                        LANDED_MISMATCH,
                    ),
                )
                return cursor.rowcount == 1
            cursor = self._conn.execute(
                "UPDATE merge_queue SET state = ?, reason = ?, owner = NULL,"
                " report_token = ?, reported_at = ?, updated_at = ?"
                " WHERE id = ? AND state IN (?, ?, ?)",
                (
                    state,
                    reason,
                    report_token,
                    reported_at,
                    now,
                    entry_id,
                    *ACTIVE_STATES,
                ),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("settle_merge_entry", _settle)

    def list_pending_merge_reports(self, repo: str) -> list[MergeQueueEntry]:
        """Terminal queue verdicts whose PR comment is not confirmed."""
        rows = self._conn.execute(
            "SELECT * FROM merge_queue WHERE repo = ? AND report_token IS NOT NULL"
            " AND reported_at IS NULL AND state IN (?, ?, ?, ?, ?) ORDER BY id",
            (repo, *sorted(SETTLED_STATES)),
        ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def mark_merge_reported(self, entry_id: int) -> bool:
        """Confirm one terminal verdict's PR comment, once."""
        now = datetime.now(UTC).isoformat()

        def _mark() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET reported_at = ?, updated_at = ?"
                " WHERE id = ? AND reported_at IS NULL",
                (now, now, entry_id),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("mark_merge_reported", _mark)

    def requeue_merge_entry(
        self, entry_id: int, reason: str, *, count_main_move: bool
    ) -> bool:
        """Requeue an entry in place and optionally count a main movement."""
        now = datetime.now(UTC).isoformat()
        increment = 1 if count_main_move else 0

        def _requeue() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET requeues = requeues + ?, reason = ?,"
                " updated_at = ? WHERE id = ? AND state = ?",
                (increment, reason, now, entry_id, QUEUED),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("requeue_merge_entry", _requeue)

    def claim_merge_entry(
        self,
        entry_id: int,
        owner: str,
        *,
        expected_base: str,
        expected_tree: str,
    ) -> bool:
        """Claim a queued entry for *owner*'s merge call; False when not queued."""
        now = datetime.now(UTC).isoformat()

        def _claim() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET state = ?, owner = ?, expected_base = ?,"
                " expected_tree = ?, landed_sha = NULL, merge_unknown = 0,"
                " updated_at = ?"
                " WHERE id = ? AND state = ?",
                (
                    MERGING,
                    owner,
                    expected_base,
                    expected_tree,
                    now,
                    entry_id,
                    QUEUED,
                ),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("claim_merge_entry", _claim)

    def release_merge_entry(self, entry_id: int) -> bool:
        """Put a claimed entry back in line, in its place; False when unclaimed."""
        now = datetime.now(UTC).isoformat()

        def _release() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET state = ?, owner = NULL,"
                " expected_base = NULL, expected_tree = NULL, landed_sha = NULL,"
                " merge_unknown = 0, updated_at = ?"
                " WHERE id = ? AND state = ?",
                (QUEUED, now, entry_id, MERGING),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("release_merge_entry", _release)

    def record_unknown_merge(self, entry_id: int, reason: str) -> bool:
        """Keep a claim whose merge request has no authoritative outcome."""
        now = datetime.now(UTC).isoformat()

        def _record() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET merge_unknown = 1, reason = ?, updated_at = ?"
                " WHERE id = ? AND state = ? AND expected_base IS NOT NULL"
                " AND expected_tree IS NOT NULL",
                (reason, now, entry_id, MERGING),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("record_unknown_merge", _record)

    def record_landed_merge(self, entry_id: int, landed_sha: str, reason: str) -> bool:
        """Persist GitHub's merge answer before validating its landed tree."""
        now = datetime.now(UTC).isoformat()

        def _record() -> bool:
            cursor = self._conn.execute(
                "UPDATE merge_queue SET state = ?, reason = ?, owner = NULL,"
                " landed_sha = ?, merge_unknown = 0, updated_at = ?"
                " WHERE id = ? AND state = ? AND expected_base IS NOT NULL"
                " AND expected_tree IS NOT NULL",
                (LANDED, reason, landed_sha, now, entry_id, MERGING),
            )
            return cursor.rowcount == 1

        return self._merge_queue_txn("record_landed_merge", _record)

    def suspect_merge_queue_main(
        self,
        repo: str,
        reason: str,
        *,
        red_on: str | None = None,
        mismatch_on: str | None = None,
        failure: str | None = None,
    ) -> None:
        """Hold *repo*'s main as unverified: the next pass verifies it first.

        The first reason stands. *red_on* records the main a verify found red,
        *mismatch_on* the commit that landed a tree nobody verified; either
        way the queue waits for main to move instead of verifying it again,
        and *failure* says why, latest first. A bare suspect never erases them.
        """
        if red_on is not None and mismatch_on is not None:
            raise ValueError("a suspect main is red or mismatched, not both")
        blocked_on = red_on or mismatch_on
        if red_on is not None:
            kind = SUSPECT_VERIFIED_RED
        elif mismatch_on is not None:
            kind = SUSPECT_LANDED_MISMATCH
        else:
            kind = SUSPECT_UNVERIFIED
        now = datetime.now(UTC).isoformat()

        def _suspect() -> None:
            self._conn.execute(
                "INSERT INTO merge_queue_suspects"
                " (repo, reason, red_on, since, kind, failure)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(repo) DO UPDATE SET"
                " red_on = COALESCE(excluded.red_on, red_on),"
                " kind = CASE WHEN excluded.red_on IS NULL THEN kind"
                " ELSE excluded.kind END,"
                " failure = CASE WHEN excluded.red_on IS NULL THEN failure"
                " ELSE excluded.failure END",
                (repo, reason, blocked_on, now, kind, failure),
            )

        self._merge_queue_txn("suspect_merge_queue_main", _suspect)

    def merge_queue_suspect(self, repo: str) -> tuple[str, str | None] | None:
        """Why *repo*'s main is unverified and the main it waits on, or None."""
        row = self._conn.execute(
            "SELECT reason, red_on FROM merge_queue_suspects WHERE repo = ?", (repo,)
        ).fetchone()
        return None if row is None else (str(row["reason"]), row["red_on"])

    def list_merge_queue_suspects(self) -> list[MergeQueueSuspect]:
        """Every repo whose queue is stopped on an unverified main, oldest first."""
        rows = self._conn.execute(
            "SELECT repo, reason, red_on, since, kind, failure"
            " FROM merge_queue_suspects ORDER BY since, repo"
        ).fetchall()
        return [
            MergeQueueSuspect(
                repo=str(row["repo"]),
                reason=str(row["reason"]),
                kind=row["kind"],
                blocked_on=row["red_on"],
                failure=row["failure"],
                since=datetime.fromisoformat(str(row["since"])),
            )
            for row in rows
        ]

    def clear_merge_queue_suspect(self, repo: str) -> None:
        """*repo*'s main verified green again."""

        def _clear() -> None:
            self._conn.execute(
                "DELETE FROM merge_queue_suspects WHERE repo = ?", (repo,)
            )

        self._merge_queue_txn("clear_merge_queue_suspect", _clear)

    def merge_queue_ledger_lock_path(self) -> Path | None:
        """The queue's lock beside this ledger file; None for an in-memory one.

        Derived from the ledger, not from config: two configs that share one
        ``dispatch.db`` share its queue, so they must share its lock too.
        """
        if self._db_path in ("", ":memory:") or self._db_path.startswith("file:"):
            return None
        ledger = Path(self._db_path).resolve()
        return ledger.with_name(f"{ledger.name}.merge-queue.lock")
