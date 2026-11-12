"""Move a project's stored rows from an old repository slug to a new one (#3527).

``kew rename-repo`` runs :meth:`RepoRenameStoreMixin.rename_repo` once, after
GitHub renamed the repository and ``project.repo`` names the new slug. One
``BEGIN IMMEDIATE`` transaction re-reads every refusal, appends one
``repo_renamed`` audit event, and rewrites :data:`SLUG_COLUMNS` and the pull
request links in :data:`PR_URL_COLUMNS`; any failure rolls all of it back.

It never touches :data:`UNTOUCHED_TABLES`: ``audit_events`` rows hash their
``repo`` (rewriting one breaks ``kew audit verify`` and the published anchors),
``epic_ticks`` is the drive's journal, ``artifact_receipt`` is sealed by
trigger, and ``outcome_sync_jobs`` keys its idempotency on the target it
recorded. Audit readers count the old slug's rows through
:func:`kew.repo_rename.repo_match` instead.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from kew.repo_rename import REPO_RENAMED_EVENT
from kew.run_store_audit import _normalize_event_ts
from kew.run_store_merge_claims import _row_to_claim
from kew.run_store_merge_queue import ACTIVE_STATES

_T = TypeVar("_T")

#: Every column that holds a bare repository slug, as ``(table, column)``.
SLUG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("runs", "repo"),
    ("run_launches", "repo"),
    ("run_admission_identity", "delivery_repo"),
    ("loops", "repo"),
    ("merge_queue", "repo"),
    ("merge_queue_suspects", "repo"),
    ("publication_job", "delivery_repo"),
    ("human_review_policy", "delivery_repo"),
)
#: Every column that holds a pull request link on the repository.
PR_URL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("runs", "pr_url"),
    ("orphan_prs", "url"),
)
#: What the rename never rewrites.
UNTOUCHED_TABLES = (
    "audit_events",
    "epic_ticks",
    "artifact_receipt",
    "outcome_sync_jobs",
)

#: Tables whose key is the slug alone: both slugs present would collide.
_SLUG_KEYED = (
    ("human_review_policy", "delivery_repo"),
    ("merge_queue_suspects", "repo"),
)

#: A stop-label reset not yet delivered: its outbox row is open, or the audit
#: request was never imported into the outbox and nothing completed it. Its
#: delivery reads the repository from the request's hashed payload, which the
#: rename cannot move.
_PENDING_LABEL_RESETS = (
    "SELECT COUNT(*) FROM audit_events a"
    " WHERE a.event = 'stop_label_reset_requested'"
    " AND NOT EXISTS (SELECT 1 FROM stop_label_outbox o"
    " WHERE o.event_id = a.id AND o.completed = 1)"
    " AND NOT EXISTS (SELECT 1 FROM audit_events done"
    " WHERE done.event = 'stop_label_reset_completed' AND ("
    " json_extract(done.payload, '$.request_id') ="
    " COALESCE(json_extract(a.payload, '$.request_id'), 'legacy:' || a.id)"
    " OR json_extract(done.payload, '$.request_id') = a.id))"
)


def pr_prefix(host: str, repo: str) -> str:
    """The link prefix of *repo*'s pull requests on the forge at *host*."""
    return f"https://{host}/{repo}/pull/"


def _key(table: str, column: str) -> str:
    return f"{table}.{column}"


@dataclass(frozen=True)
class RepoRenamePlan:
    """What a rename of *from_repo* to *to_repo* would do, and what stops it."""

    from_repo: str
    to_repo: str
    #: The forge host the pull request links name (``repo_host``).
    host: str
    #: Rows holding the old slug (or an old PR link), by ``table.column``.
    counts: dict[str, int]
    #: Why ``--apply`` would refuse now; empty when it would run.
    refusals: list[str] = field(default_factory=list)
    #: Whether a ``repo_renamed`` event from *from_repo* to *to_repo* exists.
    renamed: bool = False

    @property
    def rows(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class RepoRenameResult:
    """The committed rename: its audit event and the rows it rewrote."""

    event_id: int
    counts: dict[str, int]


class RepoRenameRefused(RuntimeError):
    """The rename found something that must stop first; nothing was written."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


class RepoRenameStoreMixin:
    """The one-time repository rename of ``SqliteRunStore``."""

    if TYPE_CHECKING:
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def live_run_count(self) -> int: ...

    def plan_repo_rename(
        self, from_repo: str, to_repo: str, *, host: str, now: datetime
    ) -> RepoRenamePlan:
        """Read what a rename would rewrite and what would refuse it.

        *host* is the forge the pull request links name. Reads only, so it
        runs on a read-only store (the dry run).

        A rename already recorded refuses a second one: rows that hold the
        old slug after it were written under that name since, and moving them
        would append a second event the readers must not take as the cutoff.
        """
        counts = self._rename_counts(pr_prefix(host, from_repo), from_repo)
        renamed = self._renamed_before(from_repo, to_repo)
        refusals = [
            *self._rename_blockers(now),
            *self._rename_collisions(from_repo, to_repo),
        ]
        rows = sum(counts.values())
        if renamed and rows:
            refusals.append(
                f"{from_repo} was already renamed to {to_repo}, yet {rows} row(s)"
                f" hold {from_repo} again: something wrote under the old name"
                " after the rename (a process still configured with it, or the"
                " repository now holding that name). kew moves them only once;"
                " find and stop what wrote them, then fix those rows by hand"
            )
        return RepoRenamePlan(from_repo, to_repo, host, counts, refusals, renamed)

    def rename_repo(
        self,
        from_repo: str,
        to_repo: str,
        *,
        host: str,
        actor_id: str,
        now: datetime,
        guard: Callable[[], Sequence[str]] = tuple,
    ) -> RepoRenameResult:
        """Rewrite every :data:`SLUG_COLUMNS` and :data:`PR_URL_COLUMNS` row of
        *from_repo* to *to_repo* and append the ``repo_renamed`` event.

        One ``BEGIN IMMEDIATE`` transaction: the refusals, *guard*'s included
        (the checks that live outside the ledger, such as lock files), are read
        inside it, so nothing they check can start between the check and the
        write. Raises :class:`RepoRenameRefused` with nothing written.
        """
        if not from_repo or not to_repo or from_repo == to_repo:
            raise ValueError("a rename names two different repositories")
        return self._with_busy_retry(
            "rename_repo",
            partial(self._rename_txn, (from_repo, to_repo, host), actor_id, now, guard),
        )

    def _rename_txn(
        self,
        names: tuple[str, str, str],
        actor_id: str,
        now: datetime,
        guard: Callable[[], Sequence[str]],
    ) -> RepoRenameResult:
        from_repo, to_repo, host = names
        with self._txn_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                plan = self.plan_repo_rename(from_repo, to_repo, host=host, now=now)
                refusals = [*plan.refusals, *guard()]
                if plan.renamed and not plan.rows:
                    refusals.append(
                        f"{from_repo} was already renamed to {to_repo};"
                        " nothing holds it to rename again"
                    )
                if refusals:
                    raise RepoRenameRefused(refusals)
                event_id = self._append_rename_event(plan, actor_id, now)
                self._rename_rewrite(plan)
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise RepoRenameRefused([f"a unique key would collide: {exc}"]) from exc
            except BaseException:
                self._conn.rollback()
                raise
        return RepoRenameResult(event_id, plan.counts)

    def _append_rename_event(
        self, plan: RepoRenamePlan, actor_id: str, now: datetime
    ) -> int:
        tail = self._conn.execute("SELECT MAX(id) FROM audit_events").fetchone()[0]
        payload = {
            "from": plan.from_repo,
            "to": plan.to_repo,
            "rewritten": plan.counts,
            "through_audit_id": tail,
        }
        event_id = self._chained_insert_in_transaction(
            (
                _normalize_event_ts(now.isoformat()),
                REPO_RENAMED_EVENT,
                None,
                None,
                plan.to_repo,
                "human",
                actor_id,
                None,
                None,
                json.dumps(payload),
                None,
            )
        )
        if event_id is None:
            raise sqlite3.OperationalError("the repo_renamed event was not written")
        return event_id

    def _rename_rewrite(self, plan: RepoRenamePlan) -> None:
        """Rewrite each column, failing when a count moved under the lock."""
        old, new = plan.from_repo, plan.to_repo
        for table, column in SLUG_COLUMNS:
            cursor = self._conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # nosec B608
                (new, old),
            )
            self._check_rewritten(plan, _key(table, column), cursor.rowcount)
        old_prefix = pr_prefix(plan.host, old)
        new_prefix = pr_prefix(plan.host, new)
        for table, column in PR_URL_COLUMNS:
            cursor = self._conn.execute(
                f"UPDATE {table} SET {column} = ? || substr({column}, ?)"  # nosec B608
                f" WHERE substr({column}, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            self._check_rewritten(plan, _key(table, column), cursor.rowcount)

    @staticmethod
    def _check_rewritten(plan: RepoRenamePlan, key: str, rowcount: int) -> None:
        if rowcount != plan.counts[key]:
            raise sqlite3.OperationalError(
                f"{key}: counted {plan.counts[key]} rows but rewrote {rowcount}"
            )

    def _rename_count(self, sql: str, params: Sequence[Any] = ()) -> int:
        return int(self._conn.execute(sql, tuple(params)).fetchone()[0])

    def _rename_counts(self, prefix: str, from_repo: str) -> dict[str, int]:
        counts = {
            _key(table, column): self._rename_count(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",  # nosec B608
                (from_repo,),
            )
            for table, column in SLUG_COLUMNS
        }
        for table, column in PR_URL_COLUMNS:
            counts[_key(table, column)] = self._rename_count(
                f"SELECT COUNT(*) FROM {table}"  # nosec B608
                f" WHERE substr({column}, 1, ?) = ?",
                (len(prefix), prefix),
            )
        return counts

    def _rename_blockers(self, now: datetime) -> list[str]:
        """Live work in the ledger that would still write or act on a slug."""
        found = {
            "runs in flight": self.live_run_count(),
            "epic reservations held": self._rename_count(
                "SELECT COUNT(*) FROM epic_reservations"
            ),
            "unexpired merge claims": self._live_merge_claims(now),
            "unsettled merge queue entries": self._rename_count(
                "SELECT COUNT(*) FROM merge_queue WHERE state IN (?, ?, ?)",
                ACTIVE_STATES,
            ),
            "pending stop-label resets": self._rename_count(_PENDING_LABEL_RESETS),
        }
        return [f"{count} {what}" for what, count in found.items() if count]

    def _live_merge_claims(self, now: datetime) -> int:
        live = 0
        for row in self._conn.execute("SELECT * FROM merge_claims").fetchall():
            try:
                live += _row_to_claim(row).live(now)
            except (TypeError, ValueError):
                live += 1  # an unreadable claim is never proven lapsed
        return live

    def _rename_collisions(self, from_repo: str, to_repo: str) -> list[str]:
        """Unique keys both slugs hold, which a rewrite would collide on."""
        found = [
            f"{table} holds a row for both {from_repo} and {to_repo}"
            for table, column in _SLUG_KEYED
            if self._rename_count(
                f"SELECT COUNT(DISTINCT {column}) FROM {table}"  # nosec B608
                f" WHERE {column} IN (?, ?)",
                (from_repo, to_repo),
            )
            == 2
        ]
        launches = self._rename_count(
            "SELECT COUNT(*) FROM run_launches a JOIN run_launches b"
            " ON a.pr_number = b.pr_number AND a.ordinal = b.ordinal"
            " WHERE a.repo = ? AND b.repo = ?",
            (from_repo, to_repo),
        )
        if launches:
            found.append(
                f"run_launches holds {launches} CI-fix slot(s) numbered the same"
                f" for one PR under both {from_repo} and {to_repo}"
            )
        return found

    def _renamed_before(self, from_repo: str, to_repo: str) -> bool:
        return bool(
            self._rename_count(
                "SELECT COUNT(*) FROM audit_events WHERE event = ? AND repo = ?"
                " AND json_extract(payload, '$.from') = ?"
                " AND json_extract(payload, '$.to') = ?",
                (REPO_RENAMED_EVENT, to_repo, from_repo, to_repo),
            )
        )
