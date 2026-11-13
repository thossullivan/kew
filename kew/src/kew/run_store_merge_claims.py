"""Merge claims: an operator's merge verify of a PR head, held in the ledger (#3198).

``kew verify --pr`` (and ``kew merge claim`` for a hand flow) claims the
``(pr, head)`` it is verifying for as long as the verify runs. While the claim
is live the drive remediates nothing on that head: a remediation pushed under a
running verify moves the head, and the merge that verify was for then fails on a
head mismatch.

A claim lives :data:`MERGE_CLAIM_TTL` from its last renewal. The verify renews
it every :data:`MERGE_CLAIM_RENEW_EVERY` (:mod:`kew.merge_claim`), so no TTL has
to cover the verify's whole ceiling, and a killed verify frees the head within
one TTL.

Take, renew and release each run in one ``BEGIN IMMEDIATE`` transaction, so
the check and the write are never two steps another process can fall between.
Renew and release name the owner that took the claim: a writer whose claim
lapsed and was taken by someone else finds that out instead of clobbering it.
Every time is passed in, so a test pins the clock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

_T = TypeVar("_T")

#: How long a claim lives past its last take or renewal.
MERGE_CLAIM_TTL = timedelta(minutes=15)
#: How often a running verify renews its claim: three renewals per TTL.
MERGE_CLAIM_RENEW_EVERY = timedelta(minutes=5)


@dataclass(frozen=True)
class MergeClaim:
    """One ``merge_claims`` row."""

    pr: int
    head: str
    owner: str
    taken_at: datetime
    expires_at: datetime

    def live(self, now: datetime) -> bool:
        """Whether the claim still holds at *now*."""
        return now < self.expires_at


class MergeClaimRefused(RuntimeError):
    """A take found a live claim on one of its heads."""

    def __init__(self, claim: MergeClaim) -> None:
        super().__init__(
            f"PR #{claim.pr} at {claim.head[:12]} is already claimed by "
            f"{claim.owner} until {claim.expires_at.isoformat(timespec='seconds')}"
        )
        self.claim = claim


def _row_to_claim(row: sqlite3.Row) -> MergeClaim:
    return MergeClaim(
        pr=int(row["pr"]),
        head=row["head"],
        owner=row["owner"],
        taken_at=datetime.fromisoformat(row["taken_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )


class MergeClaimStoreMixin:
    """The merge-claim persistence surface of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

    def _merge_claim_txn(self, operation: str, fn: Callable[[], _T]) -> _T:
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

    def _claim_rows(self, pr: int, heads: Sequence[str]) -> list[MergeClaim]:
        rows = self._conn.execute(
            "SELECT * FROM merge_claims WHERE pr = ? AND head IN"
            " (SELECT value FROM json_each(?))",
            (pr, _json_list(heads)),
        ).fetchall()
        return [_row_to_claim(row) for row in rows]

    def _drop_lapsed(self, pr: int, now: datetime) -> None:
        """Delete *pr*'s claims that lapsed by *now* (read as datetimes, not
        compared as text, so an offset written differently still reads right)."""
        rows = self._conn.execute(
            "SELECT * FROM merge_claims WHERE pr = ?", (pr,)
        ).fetchall()
        for claim in (_row_to_claim(row) for row in rows):
            if not claim.live(now):
                self._conn.execute(
                    "DELETE FROM merge_claims WHERE pr = ? AND head = ?",
                    (pr, claim.head),
                )

    def take_merge_claim(
        self,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        now: datetime,
        ttl: timedelta = MERGE_CLAIM_TTL,
    ) -> list[MergeClaim]:
        """Claim every one of *heads* of *pr* for *owner*, or none of them.

        A live claim on any head refuses the whole take with
        :class:`MergeClaimRefused`, whoever holds it — the owner included, so
        two verifies sharing an owner name still cannot both hold one head. A
        lapsed claim is replaced, and every other lapsed row of *pr* goes with
        it, so a PR's dead claims never pile up behind the live one.
        """
        if not heads:
            raise ValueError("a merge claim names at least one head")
        taken_at = now.isoformat()
        expires_at = (now + ttl).isoformat()

        def _take() -> list[MergeClaim]:
            for claim in self._claim_rows(pr, heads):
                if claim.live(now):
                    raise MergeClaimRefused(claim)
            self._drop_lapsed(pr, now)
            for head in dict.fromkeys(heads):
                self._conn.execute(
                    "INSERT OR REPLACE INTO merge_claims"
                    " (pr, head, owner, taken_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (pr, head, owner, taken_at, expires_at),
                )
            return self._claim_rows(pr, heads)

        return self._merge_claim_txn("take_merge_claim", _take)

    def renew_merge_claim(
        self,
        pr: int,
        heads: Sequence[str],
        *,
        owner: str,
        now: datetime,
        ttl: timedelta = MERGE_CLAIM_TTL,
    ) -> bool:
        """Extend *owner*'s live claim on *heads* to *now* + *ttl*.

        False, renewing nothing, unless *owner* holds a live claim on every one
        of *heads*: a claim that lapsed may already have let a remediation
        through, so it is never silently revived, and one somebody else took is
        theirs.
        """
        expires_at = (now + ttl).isoformat()

        def _renew() -> bool:
            held = [
                claim
                for claim in self._claim_rows(pr, heads)
                if claim.owner == owner and claim.live(now)
            ]
            if {claim.head for claim in held} != set(heads):
                return False
            self._conn.execute(
                "UPDATE merge_claims SET expires_at = ? WHERE pr = ? AND owner = ?"
                " AND head IN (SELECT value FROM json_each(?))",
                (expires_at, pr, owner, _json_list(heads)),
            )
            return True

        return self._merge_claim_txn("renew_merge_claim", _renew)

    def release_merge_claim(self, pr: int, heads: Sequence[str], *, owner: str) -> int:
        """Drop *owner*'s claims on *heads*; how many rows went.

        Only *owner*'s rows: a claim that lapsed and was taken again is the new
        holder's, and a late release must not free it.
        """

        def _release() -> int:
            cursor = self._conn.execute(
                "DELETE FROM merge_claims WHERE pr = ? AND owner = ?"
                " AND head IN (SELECT value FROM json_each(?))",
                (pr, owner, _json_list(heads)),
            )
            return int(cursor.rowcount)

        return self._merge_claim_txn("release_merge_claim", _release)

    def live_merge_claim(
        self, pr: int, head: str, *, now: datetime
    ) -> MergeClaim | None:
        """The claim holding *pr* at *head* at *now*, or None."""
        for claim in self._claim_rows(pr, (head,)):
            if claim.live(now):
                return claim
        return None

    def list_merge_claims(self) -> list[MergeClaim]:
        """Every claim row, lapsed ones included, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM merge_claims ORDER BY taken_at, pr, head"
        ).fetchall()
        return [_row_to_claim(row) for row in rows]


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values))
