"""Persist per-run PR outcomes (merged/closed, reviews, diff stats) — #261.

A fail-soft sync: resolves each eligible run's PR from its branch via one
``gh pr view`` and writes the outcome columns. Query failures remain explicit.
The serve sweep reads every branch in one batched GraphQL request instead, and
backs off from PRs that have not changed (#2750, ``PRReadings``).

I14 adds the review-obligation reconciliation at the foot of the file. It is
the one remote read the publication-capacity transaction depends on, and it
lives here rather than in the store because it must never happen while the
SQLite write lock is held.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kew.config import project_root
from kew.github_diagnostics import is_missing_branch_pr
from kew.human_review import ReviewPolicyError
from kew.labels import apply_no_change_outcome, observe_no_change_item
from kew.no_change import (
    ITEM_CLOSED,
    OBSERVED_ALREADY_SYNCHRONIZED,
    OBSERVED_APPLIED,
    OBSERVED_UNAVAILABLE,
    OBSERVED_WRITE_UNCONFIRMED,
    PENDING_CONFIRMATION,
    SYNC_ACKED,
    SYNC_ACTION_ACK,
    SYNC_ACTION_HOLD,
    SYNC_ACTION_MUTATE,
    SYNC_INTENT,
    ItemObservation,
    NoChangeError,
    NoChangeJudgment,
    OutcomeSyncJob,
    RepositoryAuthority,
    SourceRevalidation,
    SyncPlan,
    no_change_comment_body,
    no_change_marker,
    no_change_rejection_body,
    plan_outcome_sync,
    stale_source_plan,
)
from kew.publication_job import (
    REVIEW_REMOTE_AVAILABLE,
    RemoteReviewObservation,
    ReviewObligation,
    unavailable_review_observation,
)
from kew.run_store import PR_SUBJECT_RUN_KINDS
from kew.selector import GhQueryError
from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord, RunStore

_logger = logging.getLogger(__name__)

# Stable marker so the orphan comment is posted at most once per PR (#648).
_ORPHAN_COMMENT_MARKER = "opened by a kew run that was killed before verification"
_ORPHAN_COMMENT_BODY = (
    "⚠️ This PR was "
    + _ORPHAN_COMMENT_MARKER
    + " — review with care or close. kew's ledger has no successful run for its "
    "branch, so it may be half-finished. (Automated note from `kew report sync`; kew "
    "never auto-closes PRs.)"
)


@dataclass(frozen=True)
class _Outcome:
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: str | None
    review_cycles: int
    diff_additions: int | None
    diff_deletions: int | None
    diff_files: int | None


@dataclass(frozen=True)
class _AbsentPR:
    """GitHub confirmed that the branch has no pull request."""


@dataclass(frozen=True)
class _QueryFailure:
    """A failed query, with no untrusted remote diagnostic."""

    category: str


@dataclass(frozen=True)
class SyncResult:
    """Persisted run counts and failed queries, deduplicated by branch.

    ``graphql_calls`` is what the sync spent, not what it found, so it takes no
    part in comparing two results (#2750).
    """

    synced: int = 0
    no_pr: int = 0
    query_failures: int = 0
    graphql_calls: int = field(default=0, compare=False)


_PRQueryResult = _Outcome | _AbsentPR | _QueryFailure


def _fetch_pr_outcome(branch: str, repo: str) -> _PRQueryResult:
    """Distinguish a resolved outcome, confirmed absence, and query failure."""
    try:
        result = run(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--repo",
                repo,
                "--json",
                "url,state,mergedAt,additions,deletions,changedFiles,reviews",
            ],
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _QueryFailure("query_unavailable")
    if result.returncode != 0:
        # Match gh's exact branch-specific absence diagnostic. Authentication,
        # rate limits, repository 404s, and other errors remain failures.
        if is_missing_branch_pr(result.returncode, result.stderr, branch):
            return _AbsentPR()
        return _QueryFailure("github_query_failed")
    return _parse_pr_outcome(result.stdout)


def _parse_pr_outcome(raw: str) -> _Outcome | _QueryFailure:
    """Reject malformed JSON and incomplete outcome responses."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _QueryFailure("invalid_response")
    if not _valid_pr_outcome(data):
        return _QueryFailure("invalid_response")
    return _Outcome(
        pr_url=data["url"],
        pr_state=data["state"],
        pr_merged_at=data["mergedAt"],
        review_cycles=len(data["reviews"]),
        diff_additions=data["additions"],
        diff_deletions=data["deletions"],
        diff_files=data["changedFiles"],
    )


def _valid_pr_outcome(data: Any) -> bool:
    """Require the fields requested from gh before writing any outcome."""
    if not isinstance(data, dict):
        return False
    scalar_fields_valid = (
        isinstance(data.get("url"), str)
        and bool(data["url"])
        and data.get("state") in ("OPEN", "CLOSED", "MERGED")
        and "mergedAt" in data
    )
    return scalar_fields_valid and _valid_pr_details(data)


def _valid_pr_details(data: dict[str, Any]) -> bool:
    """Validate nullable timestamps, reviews, and nonnegative diff counts."""
    if data["mergedAt"] is not None and not isinstance(data["mergedAt"], str):
        return False
    if not isinstance(data.get("reviews"), list):
        return False
    return all(
        type(data.get(key)) is int and data[key] >= 0
        for key in ("additions", "deletions", "changedFiles")
    )


@dataclass(frozen=True)
class OrphanPR:
    """An open agent PR with no successful run behind it (#648)."""

    number: int
    url: str
    branch: str
    reason: str  # "failed-run" | "no-record"


def _list_open_agent_prs(
    prefix: str, repo: str
) -> list[dict[str, Any]] | _QueryFailure:
    """Return open agent PRs or an explicit query failure."""
    try:
        result = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,url,headRefName",
            ],
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return _QueryFailure("query_unavailable")
    if result.returncode != 0:
        return _QueryFailure("github_query_failed")
    try:
        prs = json.loads(result.stdout)
    except (TypeError, ValueError):
        return _QueryFailure("invalid_response")
    if not isinstance(prs, list) or not all(_valid_open_pr(pr) for pr in prs):
        return _QueryFailure("invalid_response")
    return [pr for pr in prs if pr["headRefName"].startswith(prefix)]


def _valid_open_pr(pr: Any) -> bool:
    """Require the fields used to detect and persist orphan PRs."""
    return (
        isinstance(pr, dict)
        and type(pr.get("number")) is int
        and pr["number"] > 0
        and isinstance(pr.get("url"), str)
        and isinstance(pr.get("headRefName"), str)
    )


def _latest_implementation_status_by_branch(store: RunStore) -> dict[str, str] | None:
    """The newest implementation run's status per branch, or None when unreadable.

    ``list_all()`` is ordered by started_at ascending, so the last write per
    branch is that branch's latest run. Reviews and their remediations work on
    the PR (``PR_SUBJECT_RUN_KINDS``); they do not stand behind it, so their
    status never speaks for the branch.
    """
    latest_by_branch: dict[str, str] = {}
    try:
        for record in store.list_all():
            if record.run_kind in PR_SUBJECT_RUN_KINDS:
                continue
            latest_by_branch[record.branch] = record.status
    except Exception as exc:  # noqa: BLE001 — detection must never raise
        _logger.debug("orphan detection could not read runs: %s", exc)
        return None
    return latest_by_branch


def _open_agent_prs(
    prefix: str, repo: str, listed: list[dict[str, Any]] | _QueryFailure | None
) -> list[dict[str, Any]] | _QueryFailure:
    """The open agent PRs from a listing already taken, else from a fresh one."""
    if listed is None:
        return _list_open_agent_prs(prefix, repo)
    if isinstance(listed, _QueryFailure):
        return listed
    return [pr for pr in listed if pr["headRefName"].startswith(prefix)]


def detect_orphan_prs(
    store: RunStore,
    config: DispatchConfig,
    *,
    open_prs: list[dict[str, Any]] | _QueryFailure | None = None,
) -> list[OrphanPR] | _QueryFailure:
    """Diff open agent PRs against run records; retain query availability.

    An open PR whose head branch carries the configured prefix is an ORPHAN when
    the latest run for that branch is non-success, or when no run record exists
    at all (a killed run that pushed a PR before kew tracked it). Never mutates
    anything. A failed read returns a static failure category. ``open_prs`` is
    a listing the serve sweep's batched request already took (#2750); without
    it the open PRs are listed here.
    """
    repo = config.project.repo
    if not repo:
        return _QueryFailure("repository_unavailable")
    prs = _open_agent_prs(config.branches.prefix, repo, open_prs)
    if isinstance(prs, _QueryFailure):
        return prs
    if not prs:
        return []
    latest_by_branch = _latest_implementation_status_by_branch(store)
    if latest_by_branch is None:
        return _QueryFailure("ledger_unavailable")
    orphans: list[OrphanPR] = []
    for pr in prs:
        branch = str(pr.get("headRefName", ""))
        status = latest_by_branch.get(branch)
        if status is None:
            reason = "no-record"
        elif status != "success":
            reason = "failed-run"
        else:
            continue  # a successful run stands behind this PR — not an orphan
        raw_number = pr.get("number")
        if raw_number is None:
            continue
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        orphans.append(
            OrphanPR(
                number=number,
                url=str(pr.get("url", "")),
                branch=branch,
                reason=reason,
            )
        )
    return orphans


@dataclass(frozen=True)
class OrphanCommentFailure:
    """A failed comment operation with a static diagnostic category."""

    pr_number: int
    category: str


@dataclass(frozen=True)
class OrphanReconciliationResult:
    """Detected orphans and confirmed comment progress, including partial failures."""

    orphans: tuple[OrphanPR, ...] = ()
    warnings_posted: tuple[int, ...] = ()
    already_warned: tuple[int, ...] = ()
    comment_read_failures: tuple[OrphanCommentFailure, ...] = ()
    comment_write_failures: tuple[OrphanCommentFailure, ...] = ()

    @property
    def query_failures(self) -> int:
        """Count required remote operations that did not complete."""
        return len(self.comment_read_failures) + len(self.comment_write_failures)


def _orphan_comment_exists(number: int, repo: str) -> bool | _QueryFailure:
    """Distinguish an existing note, an absent note, and unavailable evidence."""
    try:
        result = run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "comments",
                "--jq",
                ".comments[].body",
            ],
            check=False,
        )
        if result.returncode != 0:
            return _QueryFailure("github_query_failed")
    except (OSError, subprocess.SubprocessError, GhQueryError, ValueError):
        return _QueryFailure("query_unavailable")
    if not isinstance(result.stdout, str):
        return _QueryFailure("invalid_response")
    return _ORPHAN_COMMENT_MARKER in result.stdout


def _comment_on_orphan(number: int, repo: str) -> _QueryFailure | None:
    """Post an orphan note after the caller confirms its absence."""
    try:
        result = run(
            [
                "gh",
                "pr",
                "comment",
                str(number),
                "--repo",
                repo,
                "--body",
                _ORPHAN_COMMENT_BODY,
            ],
            check=False,
        )
    except (OSError, subprocess.SubprocessError, GhQueryError, ValueError):
        return _QueryFailure("query_unavailable")
    if result.returncode != 0:
        return _QueryFailure("github_query_failed")
    return None


def _reconcile_orphan_comments(
    orphans: list[OrphanPR], repo: str
) -> OrphanReconciliationResult:
    """Keep each confirmed result while continuing past individual failures."""
    posted: list[int] = []
    already_warned: list[int] = []
    read_failures: list[OrphanCommentFailure] = []
    write_failures: list[OrphanCommentFailure] = []
    for orphan in orphans:
        exists = _orphan_comment_exists(orphan.number, repo)
        if isinstance(exists, _QueryFailure):
            read_failures.append(OrphanCommentFailure(orphan.number, exists.category))
        elif exists:
            already_warned.append(orphan.number)
        else:
            failure = _comment_on_orphan(orphan.number, repo)
            if failure is None:
                posted.append(orphan.number)
            else:
                write_failures.append(
                    OrphanCommentFailure(orphan.number, failure.category)
                )
    return OrphanReconciliationResult(
        orphans=tuple(orphans),
        warnings_posted=tuple(posted),
        already_warned=tuple(already_warned),
        comment_read_failures=tuple(read_failures),
        comment_write_failures=tuple(write_failures),
    )


def reconcile_orphan_prs(
    store: RunStore,
    config: DispatchConfig,
    *,
    open_prs: list[dict[str, Any]] | _QueryFailure | None = None,
) -> OrphanReconciliationResult | _QueryFailure:
    """Detect orphaned PRs and retain confirmed progress from idempotent warnings.

    Failed detection preserves persisted orphans. Failed comment operations
    remain explicit in the partial result and never count as confirmed warnings.
    ``open_prs`` is passed through to ``detect_orphan_prs``.
    """
    repo = config.project.repo
    orphans = detect_orphan_prs(store, config, open_prs=open_prs)
    if isinstance(orphans, _QueryFailure):
        return orphans
    result = _reconcile_orphan_comments(orphans, repo)
    # Persist the current orphan set: upsert the detected orphans and clear
    # rows no longer orphaned (PR closed, or branch gained a successful run).
    # Fail-soft — a store hiccup must not sink the sync.
    try:
        store.sync_orphan_prs([(o.number, o.url, o.branch, o.reason) for o in orphans])
    except Exception as exc:  # noqa: BLE001 — reconcile must never raise
        _logger.debug("orphan persistence skipped: %s", exc)
    return result


def _absent_outcome(run_rec: RunRecord) -> _Outcome | None:
    """The outcome to record for a branch whose PR is not visible.

    ``None`` leaves the recorded outcome alone: a run's PR state only advances,
    which ``set_outcome``'s own guard documents, so an absence now must not
    erase the url, review cycles and diff counters an OPEN outcome already
    carries (governed review round 6, 2026-09-22).
    """
    if run_rec.pr_state not in (None, "ABSENT"):
        return None
    return _Outcome(None, "ABSENT", None, 0, None, None, None)


def _recordable_outcome(
    queried: _PRQueryResult, run_rec: RunRecord
) -> tuple[_Outcome | None, str]:
    """What this query means for one run: the outcome to write, and what it counts as.

    A failed query changes nothing and counts as nothing. An absence counts as
    an absence whether or not it is recorded, because the sweep did observe it.
    """
    if isinstance(queried, _QueryFailure):
        return None, ""
    if isinstance(queried, _AbsentPR):
        return _absent_outcome(run_rec), "absent"
    return queried, "updated"


#: How long a confirmed absence keeps being re-checked. A PR can still appear
#: for a recent run (host publication opens it after the run), so a fresh
#: ABSENT is polled again; a months-old one never gets a PR. Without this cut
#: every sweep re-asked GitHub about every ABSENT branch since March — 112 of
#: them, once a minute per `kew serve` — and the console alone drained the
#: account's GraphQL quota within minutes of each hourly reset (#2264).
ABSENT_RECHECK_WINDOW = timedelta(days=7)


def _worth_querying(run_rec: RunRecord, now: datetime) -> bool:
    """Whether a sweep should ask GitHub about this run's branch again."""
    if run_rec.pr_state != "ABSENT":
        return True
    started = run_rec.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started <= ABSENT_RECHECK_WINDOW


def _runs_to_sync(
    store: RunStore, run_id: str | None, now: datetime | None
) -> list[RunRecord]:
    """The syncable runs this call queries: the one named, or every run whose
    absence is still worth re-checking (see ``_worth_querying``)."""
    runs = store.list_syncable_runs()
    if run_id is not None:
        return [r for r in runs if r.run_id == run_id]
    at = now or datetime.now(UTC)
    return [r for r in runs if _worth_querying(r, at)]


def sync_outcomes(
    store: RunStore,
    config: DispatchConfig,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
    readings: PRReadings | None = None,
) -> SyncResult:
    """Backfill outcomes and report successful, absent, and failed queries.

    Eligible = successful runs with a non-terminal pr_state. When ``run_id`` is
    given, only that run is synced (the post-dispatch hot path → one gh call);
    otherwise all eligible runs are swept (``kew report sync`` / ``kew report``). gh
    calls are deduped by branch. Query failures leave existing outcomes intact.
    Confirmed absence is persisted and checked again on later sweeps only while
    the run is younger than ``ABSENT_RECHECK_WINDOW``; an explicit ``run_id``
    is always queried.

    ``readings`` is the serve sweep's memory across cycles (#2750): with it, a
    sweep reads every due branch in one batched GraphQL request and skips the
    branches ``PRReadings`` says have not changed. The ``run_id`` path ignores
    it and reads at once.
    """
    repo = config.project.repo
    if not repo:
        return SyncResult()
    runs = _runs_to_sync(store, run_id, now)
    if readings is not None and run_id is None:
        cache, calls = _read_batched(runs, repo, readings, now or datetime.now(UTC))
    else:
        cache, calls = _read_each(runs, repo)
    counts: Counter[str] = Counter()
    for run_rec in runs:
        if run_rec.branch not in cache:
            continue  # backed off: nothing new was read for this branch
        o, counted = _recordable_outcome(cache[run_rec.branch], run_rec)
        counts[counted] += 1
        if o is None:
            continue
        store.set_outcome(
            run_rec.run_id,
            pr_url=o.pr_url,
            pr_state=o.pr_state,
            pr_merged_at=o.pr_merged_at,
            review_cycles=o.review_cycles,
            diff_additions=o.diff_additions,
            diff_deletions=o.diff_deletions,
            diff_files=o.diff_files,
        )
        _reexport_run_report(store, config, run_rec.run_id)
    failures = sum(isinstance(result, _QueryFailure) for result in cache.values())
    return SyncResult(counts["updated"], counts["absent"], failures, calls)


# ---------------------------------------------------------------------------
# #2750: the serve sweep's batched, backed-off read.
#
# `kew serve` sweeps once a minute whether or not anyone is looking. One
# `gh pr view` per open PR per cycle came to about 15 GraphQL calls a minute on
# its own. The sweep now makes ONE request per cycle (per `_GRAPHQL_BATCH`
# branches): an aliased `pullRequests(headRefName:)` read for each branch that
# is due, plus the repository's most recently updated PRs. A branch read in the
# last `READING_TTL` is not read again unless that list shows its PR moved —
# and the list carries the full fields, so a merge or close still settles in
# the cycle that first sees it.
# ---------------------------------------------------------------------------

#: How long an unchanged PR's reading stands before the sweep reads it again.
READING_TTL = timedelta(minutes=5)
#: Branches per GraphQL request.
_GRAPHQL_BATCH = 50
#: How many recently updated PRs each sweep request lists to see what moved.
_RECENT_PRS = 50

_PR_SELECTION = (
    "url state mergedAt updatedAt additions deletions changedFiles"
    " isCrossRepository reviews { totalCount }"
)


@dataclass(frozen=True)
class _SeenPR:
    """One PR as the batched query returned it."""

    branch: str
    outcome: _Outcome
    updated_at: str


@dataclass(frozen=True)
class _Reading:
    """What the sweep last read for a branch, and when."""

    url: str | None
    updated_at: str | None
    read_at: datetime
    run_ids: frozenset[str]


class PRReadings:
    """The serve sweep's memory of each branch's PR, across cycles (#2750).

    One instance lives as long as the serve process. A branch is due — read
    again — when it has no reading, when its reading is ``READING_TTL`` old,
    when a run this reading did not cover has appeared on it (a new dispatch,
    from any process), or when ``touch`` was called for it.
    """

    def __init__(self) -> None:
        self._by_branch: dict[str, _Reading] = {}
        self._recent_top: str | None = None
        #: The open PRs this cycle's request listed for the orphan pass, so
        #: that pass makes no request of its own; None when no request ran.
        self.open_prs: list[dict[str, Any]] | _QueryFailure | None = None

    def touch(self, branch: str) -> None:
        """Read *branch* in full on the next sweep (a dispatch or merge here)."""
        self._by_branch.pop(branch, None)

    def due(self, branch: str, run_ids: frozenset[str], now: datetime) -> bool:
        reading = self._by_branch.get(branch)
        if reading is None or self._recent_top is None:
            return True
        return now - reading.read_at >= READING_TTL or not run_ids <= reading.run_ids

    def record(
        self,
        branch: str,
        seen: _SeenPR | _AbsentPR,
        run_ids: frozenset[str],
        now: datetime,
    ) -> None:
        if isinstance(seen, _AbsentPR):
            self._by_branch[branch] = _Reading(None, None, now, run_ids)
            return
        self._by_branch[branch] = _Reading(
            seen.outcome.pr_url, seen.updated_at, now, run_ids
        )

    def moved(self, branch: str, recent: list[_SeenPR]) -> _SeenPR | None:
        """The PR to adopt for a resting *branch* from the recently-updated list.

        None when the list shows nothing new for it. A different PR on the
        branch than the one read is not guessed at: the branch is touched, so
        the next sweep reads it in full.
        """
        reading = self._by_branch.get(branch)
        candidates = [seen for seen in recent if seen.branch == branch]
        if reading is None or not candidates:
            return None
        if reading.url is None:
            return _prefer_open(candidates)
        if any(seen.outcome.pr_url != reading.url for seen in candidates):
            self.touch(branch)
            return None
        seen = candidates[0]
        return None if seen.updated_at == reading.updated_at else seen

    def note_recent(self, page: list[str]) -> None:
        """Advance the recently-updated watermark from *page* — the
        ``updatedAt`` of every PR the list returned, forks included, since
        they fill the page too; forget every reading when the list may have
        skipped a PR (a full page, all newer than the last)."""
        if not page:
            return
        top = max(page)
        floor = min(page)
        full = len(page) >= _RECENT_PRS
        if self._recent_top is None or (full and floor > self._recent_top):
            self._by_branch.clear()
        self._recent_top = top


def _prefer_open(seen: list[_SeenPR]) -> _SeenPR:
    """gh's choice among a branch's PRs: an open one, else the newest."""
    return next((s for s in seen if s.outcome.pr_state == "OPEN"), seen[0])


_BatchAnswer = dict[str, _SeenPR | _AbsentPR | _QueryFailure]


def _read_batched(
    runs: list[RunRecord], repo: str, readings: PRReadings, now: datetime
) -> tuple[dict[str, _PRQueryResult], int]:
    """Read every due branch in batched GraphQL requests; return what each
    branch read resolved to (resting, unmoved branches are absent) and the
    number of requests made."""
    readings.open_prs = None
    ids = _run_ids_by_branch(runs)
    if not ids:
        return {}, 0
    due = [b for b in ids if readings.due(b, ids[b], now)]
    resting = [b for b in ids if b not in due]
    batches = [
        due[i : i + _GRAPHQL_BATCH] for i in range(0, len(due), _GRAPHQL_BATCH)
    ] or [[]]
    answers: _BatchAnswer = {}
    for index, batch in enumerate(batches):
        answers.update(
            _answer_batch(repo, batch, resting if index == 0 else None, readings)
        )
    return _settle(answers, ids, readings, now), len(batches)


def _settle(
    answers: _BatchAnswer,
    ids: dict[str, frozenset[str]],
    readings: PRReadings,
    now: datetime,
) -> dict[str, _PRQueryResult]:
    """Remember every answered branch, and hand the sync its query results."""
    cache: dict[str, _PRQueryResult] = {}
    for branch, answer in answers.items():
        if not isinstance(answer, _QueryFailure):
            readings.record(branch, answer, ids[branch], now)
        cache[branch] = answer.outcome if isinstance(answer, _SeenPR) else answer
    return cache


def _read_each(
    runs: list[RunRecord], repo: str
) -> tuple[dict[str, _PRQueryResult], int]:
    """One ``gh pr view`` per distinct branch, and the number of calls made."""
    cache = {
        b: _fetch_pr_outcome(b, repo) for b in dict.fromkeys(r.branch for r in runs)
    }
    return cache, len(cache)


def _run_ids_by_branch(runs: list[RunRecord]) -> dict[str, frozenset[str]]:
    found: dict[str, set[str]] = {}
    for run_rec in runs:
        found.setdefault(run_rec.branch, set()).add(run_rec.run_id)
    return {branch: frozenset(ids) for branch, ids in found.items()}


def _answer_batch(
    repo: str, batch: list[str], resting: list[str] | None, readings: PRReadings
) -> _BatchAnswer:
    """One request: *batch* read in full, plus — when *resting* is given — the
    open PRs for the orphan pass and the recently updated PRs, from which a
    resting branch's move is adopted."""
    data = _query_batch(repo, batch, recent=resting is not None)
    if isinstance(data, _QueryFailure):
        if resting is not None:
            readings.open_prs = data
        return {branch: data for branch in [*batch, *(resting or [])]}
    answers: _BatchAnswer = {
        branch: _branch_answer(data.get(f"b{i}"), branch)
        for i, branch in enumerate(batch)
    }
    if resting is not None:
        readings.open_prs = _open_answer(data.get("open"))
        answers.update(_resting_answers(data.get("recent"), resting, readings))
    return answers


def _resting_answers(
    connection: Any, resting: list[str], readings: PRReadings
) -> _BatchAnswer:
    """The resting branches whose PR the recently-updated list shows moved."""
    answer = _recent_answer(connection)
    if isinstance(answer, _QueryFailure):
        return {branch: answer for branch in resting}
    recent, page = answer
    answers: _BatchAnswer = {}
    for branch in resting:
        seen = readings.moved(branch, recent)
        if seen is not None:
            answers[branch] = seen
    readings.note_recent(page)
    return answers


_OPEN_FIELD = (
    # What `gh pr list --state open` lists by default: the newest 30.
    "open: pullRequests(states: OPEN, first: 30,"
    " orderBy: {field: CREATED_AT, direction: DESC})"
    " { nodes { number url headRefName } }"
)
_RECENT_FIELD = (
    f"recent: pullRequests(first: {_RECENT_PRS},"
    " orderBy: {field: UPDATED_AT, direction: DESC})"
    f" {{ nodes {{ headRefName {_PR_SELECTION} }} }}"
)


def _branch_field(index: int) -> str:
    # Branch names travel as GraphQL variables, never spliced into the query.
    return (
        # 30, as `gh pr view <branch>` reads, so forks' same-named PRs that
        # are then dropped cannot crowd this repository's own off the page.
        f"b{index}: pullRequests(headRefName: $b{index}, first: 30,"
        " orderBy: {field: CREATED_AT, direction: DESC})"
        f" {{ nodes {{ {_PR_SELECTION} }} }}"
    )


def _query_batch(
    repo: str, batch: list[str], *, recent: bool
) -> dict[str, Any] | _QueryFailure:
    """Run one GraphQL request; return the ``repository`` object or a failure."""
    owner, _, name = repo.partition("/")
    params = "".join(f", $b{i}: String!" for i in range(len(batch)))
    fields = [_branch_field(i) for i in range(len(batch))]
    if recent:
        fields[:0] = [_OPEN_FIELD, _RECENT_FIELD]
    query = (
        f"query($owner: String!, $name: String!{params}) {{"
        f" repository(owner: $owner, name: $name) {{ {' '.join(fields)} }} }}"
    )
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    args += ["-f", f"owner={owner}", "-f", f"name={name}"]
    for i, branch in enumerate(batch):
        args += ["-f", f"b{i}={branch}"]
    try:
        result = run(args, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _QueryFailure("query_unavailable")
    if result.returncode != 0:
        return _QueryFailure("github_query_failed")
    try:
        repository = json.loads(result.stdout)["data"]["repository"]
    except (TypeError, ValueError, KeyError):
        return _QueryFailure("invalid_response")
    if not isinstance(repository, dict):
        return _QueryFailure("invalid_response")
    return repository


def _nodes(connection: Any) -> list[Any] | None:
    if not isinstance(connection, dict) or not isinstance(
        connection.get("nodes"), list
    ):
        return None
    return list(connection["nodes"])


def _same_repo_nodes(connection: Any) -> list[Any] | None:
    """The PR nodes whose head lives in this repository, as ``gh pr view
    <branch>`` sees them: a fork's PR from a same-named branch is not the
    run's PR, so it is dropped here rather than allowed to outrank it."""
    nodes = _nodes(connection)
    if nodes is None:
        return None
    flags = [n.get("isCrossRepository") if isinstance(n, dict) else None for n in nodes]
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    return [node for node, cross in zip(nodes, flags, strict=True) if not cross]


def _seen_pr(node: Any, branch: str) -> _SeenPR | None:
    """Validate one GraphQL PR node the way ``_parse_pr_outcome`` validates gh's."""
    if not isinstance(node, dict) or not isinstance(node.get("updatedAt"), str):
        return None
    reviews = node.get("reviews")
    count = reviews.get("totalCount") if isinstance(reviews, dict) else None
    if type(count) is not int or count < 0:
        return None
    if not _valid_pr_outcome({**node, "reviews": []}):
        return None
    outcome = _Outcome(
        pr_url=node["url"],
        pr_state=node["state"],
        pr_merged_at=node["mergedAt"],
        review_cycles=count,
        diff_additions=node["additions"],
        diff_deletions=node["deletions"],
        diff_files=node["changedFiles"],
    )
    return _SeenPR(branch, outcome, node["updatedAt"])


def _branch_answer(connection: Any, branch: str) -> _SeenPR | _AbsentPR | _QueryFailure:
    nodes = _same_repo_nodes(connection)
    if nodes is None:
        return _QueryFailure("invalid_response")
    if not nodes:
        return _AbsentPR()
    seen = [_seen_pr(node, branch) for node in nodes]
    valid = [s for s in seen if s is not None]
    if len(valid) != len(seen):
        return _QueryFailure("invalid_response")
    return _prefer_open(valid)


def _page_stamps(connection: Any) -> list[str] | None:
    """The ``updatedAt`` of every PR node on a page, or None if one lacks it."""
    nodes = _nodes(connection)
    if nodes is None:
        return None
    stamps = [n.get("updatedAt") if isinstance(n, dict) else None for n in nodes]
    valid = [s for s in stamps if isinstance(s, str)]
    return valid if len(valid) == len(stamps) else None


def _recent_answer(
    connection: Any,
) -> tuple[list[_SeenPR], list[str]] | _QueryFailure:
    """This repository's recently updated PRs, and the ``updatedAt`` of every
    PR on the page — forks' too, which the overflow check must count."""
    stamps = _page_stamps(connection)
    nodes = _same_repo_nodes(connection)
    if stamps is None or nodes is None:
        return _QueryFailure("invalid_response")
    seen = [
        _seen_pr(node, node["headRefName"])
        if isinstance(node, dict) and isinstance(node.get("headRefName"), str)
        else None
        for node in nodes
    ]
    valid = [s for s in seen if s is not None]
    if len(valid) != len(seen):
        return _QueryFailure("invalid_response")
    return valid, stamps


def _open_answer(connection: Any) -> list[dict[str, Any]] | _QueryFailure:
    nodes = _nodes(connection)
    if nodes is None or not all(_valid_open_pr(pr) for pr in nodes):
        return _QueryFailure("invalid_response")
    return nodes


def _reexport_run_report(store: RunStore, config: DispatchConfig, run_id: str) -> None:
    """Re-export the run's decision-intelligence report (#1578) now that its
    outcome has settled — this is where ``outcome: merged`` becomes true.
    An unchanged report is left untouched; best-effort like everything else in sync.
    The enabled gate precedes the store read so the sweep form of
    ``sync_outcomes`` costs the default-off majority nothing."""
    try:
        if not config.run_reports.enabled:
            return
        from kew.run_report_sink import export_run_report

        refreshed = store.get(run_id)
        if refreshed is not None:
            export_run_report(config, store, refreshed)
    except Exception as exc:
        _logger.debug("run-report re-export skipped (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# I14: reconciling the review obligations a delivery repository is carrying.
#
# This is the only part of the capacity transaction that talks to GitHub, and
# it deliberately lives outside it. The SQLite write lock is held for the few
# microseconds it takes to count and reserve; a `gh` call that hangs for thirty
# seconds inside that lock would stall every other host on the repository.
#
# The fail-soft stance of the rest of this module holds here with one change of
# sign: a `gh` failure produces an *unavailable* observation, never an empty
# one. Everywhere else in kew a failed remote read degrades into "nothing to
# do". Here it must degrade into "we do not know", because "no open pull
# requests" is what frees a review slot.
# ---------------------------------------------------------------------------

_PR_FIELDS = "number,url,headRefName,body,state"

#: The marker `publication_marker` writes into every pull request kew opens.
#: A body carrying one attributes the obligation to the job that owns it; a
#: body without one is an obligation this host did not create, which occupies a
#: reviewer exactly as much.
_MARKER_RE = re.compile(r"<!--\s*kew-publication-job:\s*(pj-[0-9a-f]{6,64})\s*-->")


def _obligation_from_pr(pr: dict[str, Any]) -> ReviewObligation | None:
    """One open pull request as an obligation, or None when it names nothing."""
    ref = str(pr.get("url") or pr.get("number") or "")
    if not ref:
        return None
    match = _MARKER_RE.search(str(pr.get("body") or ""))
    return ReviewObligation(
        ref=ref,
        job_id=match.group(1) if match else None,
        detail=str(pr.get("headRefName") or ""),
    )


def _list_open_prs(repo: str) -> list[dict[str, Any]] | None:
    """Every open pull request on *repo*, or None when the forge went unread.

    ``None`` and ``[]`` are different answers and the caller depends on the
    difference: one is an outage, the other is an empty review queue.
    """
    try:
        result = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                _PR_FIELDS,
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(prs, list):
        return None
    return [pr for pr in prs if isinstance(pr, dict)]


def observe_review_obligations(
    config: DispatchConfig, *, generation: int, now: datetime | None = None
) -> RemoteReviewObservation:
    """Read the delivery repository's open pull requests as a typed observation.

    Never raises and never invents an empty queue: an unreadable forge, or a
    project that names no delivery repository, both come back ``unavailable``.
    """
    observed_at = (now or datetime.now(UTC)).isoformat()
    repo = config.project.repo
    if not repo:
        raise ReviewPolicyError(
            "review capacity is counted per canonical GitHub delivery"
            " repository; [project].repo names none"
        )
    prs = _list_open_prs(repo)
    if prs is None:
        _logger.debug("review obligations for %s could not be read", repo)
        return unavailable_review_observation(
            delivery_repo=repo,
            generation=generation,
            observed_at=observed_at,
            detail="gh could not list the delivery repository's pull requests",
        )
    obligations = tuple(
        obligation
        for obligation in (_obligation_from_pr(pr) for pr in prs)
        if obligation is not None
    )
    return RemoteReviewObservation(
        delivery_repo=repo,
        availability=REVIEW_REMOTE_AVAILABLE,
        observed_at=observed_at,
        generation=generation,
        obligations=obligations,
        detail=f"{len(obligations)} open pull request(s)",
    )


def import_review_obligations(
    store: Any, config: DispatchConfig, *, now: datetime | None = None
) -> RemoteReviewObservation:
    """Reconcile the forge and persist the result, holding no write lock across it.

    Returns the persisted observation, whose ``generation`` is what the caller
    passes to the reservation transaction — so a reservation always rides on
    the look at the forge that this caller itself just took.
    """
    generation = store.next_review_observation_generation(config.project.repo)
    observation = observe_review_obligations(config, generation=generation, now=now)
    persisted: RemoteReviewObservation = store.record_review_observation(observation)
    return persisted


# ---------------------------------------------------------------------------
# I11: synchronizing an authorized no-change outcome with the tracker.
#
# One reconciler, two dialects. The judgment, its generation and its durable
# sync intent were written by I10A before anything remote was touched; this is
# the part that touches something remote, and its whole job is to make sure it
# does so at most once and never claims more than it saw.
#
# The sequence per sync job is fixed: LOOK (is the item still open, does it
# already carry this judgment's marker?), REVALIDATE (is the base the judgment
# was made against still the base?), WRITE ONCE, then observe and acknowledge
# what the write produced. Anything unexpected at any step HOLDS — the job
# stays sync-pending, the item stays open, and no dependent is unblocked. A
# hold is recoverable; a false acknowledgement is not.
# ---------------------------------------------------------------------------


class _ItemAdapter(Protocol):
    """The two moves a tracker has to support to carry a no-change outcome."""

    def observe(self, *, marker: str) -> ItemObservation: ...

    def apply(self, *, body: str | None, close: bool) -> bool: ...


@dataclass(frozen=True)
class _GitHubItemAdapter:
    issue_num: int
    config: DispatchConfig

    def observe(self, *, marker: str) -> ItemObservation:
        return observe_no_change_item(self.issue_num, self.config, marker=marker)

    def apply(self, *, body: str | None, close: bool) -> bool:
        return apply_no_change_outcome(
            self.issue_num, self.config, body=body, close=close
        )


@dataclass(frozen=True)
class _LinearItemAdapter:
    item_id: str
    tracker: Any

    def observe(self, *, marker: str) -> ItemObservation:
        seen: ItemObservation = self.tracker.observe_no_change_item(
            self.item_id, marker=marker
        )
        return seen

    def apply(self, *, body: str | None, close: bool) -> bool:
        result: bool = self.tracker.apply_no_change_outcome(
            self.item_id, body=body, close=close
        )
        return result


def _github_adapter(
    config: DispatchConfig, judgment: NoChangeJudgment
) -> _ItemAdapter | None:
    if not config.project.repo:
        return None
    try:
        return _GitHubItemAdapter(int(judgment.source_id), config)
    except (TypeError, ValueError):
        return None


def _linear_adapter(
    config: DispatchConfig, judgment: NoChangeJudgment
) -> _ItemAdapter | None:
    api_key = os.environ.get("LINEAR_API_KEY", "")
    if not api_key or config.tracker.type != "linear":
        return None
    from kew.linear.client import LinearClient
    from kew.linear.tracker import LinearTracker

    return _LinearItemAdapter(
        judgment.source_id, LinearTracker(config, LinearClient(api_key))
    )


def no_change_item_adapter(
    config: DispatchConfig, judgment: NoChangeJudgment
) -> _ItemAdapter | None:
    """The adapter for the authority that owns this item, or None.

    None is an outage, not an error: a project with no delivery repository, a
    Linear key that is not in the environment, or a GitHub source id that is
    not an issue number all leave the outcome sync-pending rather than half
    synchronized somewhere else.
    """
    if judgment.source_kind == "linear":
        return _linear_adapter(config, judgment)
    if judgment.source_kind == "github":
        return _github_adapter(config, judgment)
    return None


@dataclass(frozen=True)
class ForgeIdentity:
    """Who the host is authenticated as on the forge, or why that is unknown.

    ``available`` False is the outage answer, exactly as it is for a
    permission read: a caller that cannot learn the login has learned nothing
    it may substitute the local OS username for.
    """

    login: str
    available: bool = True
    detail: str = ""


def observe_forge_identity() -> ForgeIdentity:
    """The login ``gh`` is authenticated as — the identity the forge grants on.

    The OS username in the audit envelope is not a forge identity. A
    repository's permissions are granted to a LOGIN, and the two names differ
    on plenty of machines (they differ for this repository's own owner, #2463),
    so asking the forge about the OS username answers ``none`` for somebody who
    in fact holds admin. This is the read that turns "the person at this
    terminal" into "the account the host publishes with".
    """
    try:
        result = run(["gh", "api", "user", "--jq", ".login"], check=False)
        if result.returncode != 0:
            raise OSError((result.stderr or "gh api user failed").strip())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ForgeIdentity(login="", available=False, detail=str(exc))
    login = (result.stdout or "").strip()
    if not login:
        return ForgeIdentity(
            login="", available=False, detail="gh named no authenticated login"
        )
    return ForgeIdentity(login=login)


def observe_repository_authority(
    config: DispatchConfig, *, actor: str, now: datetime | None = None
) -> RepositoryAuthority:
    """Read *actor*'s permission on the delivery repository. Fail-soft, timed.

    ``available=False`` is what an unreadable forge produces, and
    :func:`kew.no_change.resolve_no_change_authority` refuses it exactly as it
    refuses ``read``: an unread permission is not write access. The stamp is
    what makes the evidence *fresh* rather than merely present — permission is
    revocable, and a no-change closure is not.
    """
    observed_at = (now or datetime.now(UTC)).isoformat()
    repo = config.project.repo
    if not repo or not actor:
        return RepositoryAuthority(
            delivery_repo=repo or "",
            actor=actor,
            permission="",
            observed_at=observed_at,
            available=False,
            detail="no delivery repository or actor to ask about",
        )
    try:
        result = run(
            [
                "gh",
                "api",
                f"repos/{repo}/collaborators/{actor}/permission",
                "--jq",
                ".permission",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise OSError((result.stderr or "gh api failed").strip())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return RepositoryAuthority(
            delivery_repo=repo,
            actor=actor,
            permission="",
            observed_at=observed_at,
            available=False,
            detail=str(exc),
        )
    return RepositoryAuthority(
        delivery_repo=repo,
        actor=actor,
        permission=(result.stdout or "").strip(),
        observed_at=observed_at,
    )


#: A full commit id, the only thing a base comparison hands to git.
_COMMIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: The verb that moves a stale, still-pending no-change forward.
REJUDGE_COMMAND = "kew no-change rejudge {item}"
#: The verb left for a stale judgment somebody already decided: rejudge admits
#: only a pending one, and a decision cannot be re-made on a new base.
REJECT_COMMAND = 'kew no-change reject {item} --reason "…"'
#: How many paths a refusal names before it counts the rest.
_PATHS_NAMED = 10


def _judgment_item(judgment: NoChangeJudgment) -> str:
    return judgment.item_id or judgment.source_id


def rejudge_command(judgment: NoChangeJudgment) -> str:
    """The command that re-checks *judgment* against the current base."""
    return REJUDGE_COMMAND.format(item=_judgment_item(judgment))


def reject_command(judgment: NoChangeJudgment) -> str:
    """The command that refuses *judgment*, whatever live state it is in."""
    return REJECT_COMMAND.format(item=_judgment_item(judgment))


def _stale_next_step(judgment: NoChangeJudgment) -> str:
    """What a stale refusal tells the operator to run, for this judgment's state."""
    if judgment.state == PENDING_CONFIRMATION:
        return (
            f"run `{rejudge_command(judgment)}` to re-check it against the current base"
        )
    return (
        f"it is already {judgment.state}, so it cannot be rejudged — run"
        f" `{reject_command(judgment)}` to refuse it; the item stays open"
    )


def name_paths(paths: tuple[str, ...]) -> str:
    """*paths* for a message: the first few named, the rest counted."""
    named = ", ".join(paths[:_PATHS_NAMED])
    rest = len(paths) - _PATHS_NAMED
    return f"{named} and {rest} more" if rest > 0 else named


def read_delivery_base(config: DispatchConfig) -> tuple[str, str]:
    """The delivery base's current sha, and ``""`` — or ``""`` and why not."""
    repo = config.project.repo
    base = config.branches.base or "main"
    if not repo:
        return "", "this project names no delivery repository to revalidate against"
    try:
        result = run(
            ["gh", "api", f"repos/{repo}/commits/{base}", "--jq", ".sha"], check=False
        )
        observed = (result.stdout or "").strip() if result.returncode == 0 else ""
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return "", f"{repo}@{base} could not be read: {exc}"
    if not observed:
        return "", f"{repo}@{base} could not be read, so the base is unverified"
    return observed, ""


def _git_in(cwd: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """One read-only git call in *cwd*, or None if git could not be run at all."""
    argv = ["git", "--literal-pathspecs", "-C", str(cwd), *args]
    try:
        return run(argv, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _logger.warning("git %s could not run in %s: %s", args[0], cwd, exc)
        return None


def _repository_root(start: Path) -> Path | None:
    """The top level of the git repository holding *start*, or None."""
    result = _git_in(start, "rev-parse", "--show-toplevel")
    top = (result.stdout or "").strip() if result and result.returncode == 0 else ""
    return Path(top) if top else None


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """One read-only git call at the top level of the project's repository.

    Judged paths are repository-root-relative (``git ls-files``), so a project
    whose ``kew.toml`` sits in a subdirectory must not run ``git diff`` there:
    from a subdirectory the pathspecs name files that do not exist, every diff
    comes back empty, and every path would read as untouched.
    """
    top = _repository_root(project_root())
    return None if top is None else _git_in(top, *args)


def tracked_files(base_sha: str) -> tuple[str, ...] | None:
    """Every file tracked at commit *base_sha*, root-relative, or None.

    What :func:`kew.no_change.judged_paths_from` resolves an item's names
    against: an untracked or ignored file is never in a ``git diff``, so it
    could only ever read as untouched, and is no scope at all. Read from the
    project's own repository — the host-owned one the base comparison reads
    later — never from the agent's checkout, which no host git may resolve.
    None when *base_sha* is not a commit id or that repository lacks it.
    """
    if not _COMMIT_OID.fullmatch(base_sha):
        return None
    listed = _git("ls-tree", "-r", "-z", "--name-only", "--full-tree", base_sha)
    if listed is None or listed.returncode != 0:
        return None
    return tuple(path for path in (listed.stdout or "").split("\0") if path)


@dataclass(frozen=True)
class BaseComparison:
    """What the commits from a judged base to the current one did to its paths.

    ``touched`` is None when that could not be answered as "these, or none",
    and ``unanswered`` then says why.
    """

    touched: tuple[str, ...] | None
    unanswered: str = ""


def _descent_problem(base_sha: str, observed: str) -> str:
    """Why *observed* cannot be read as *base_sha* plus commits, or ``""``."""
    if not (_COMMIT_OID.fullmatch(base_sha) and _COMMIT_OID.fullmatch(observed)):
        return "the bases are not both full commit ids"
    ancestry = _git("merge-base", "--is-ancestor", base_sha, observed)
    if ancestry is not None and ancestry.returncode == 1:
        return f"{observed} does not descend from the judged base {base_sha}"
    if ancestry is None or ancestry.returncode != 0:
        return (
            "the project's repository could not compare them (fetch the base"
            " branch into it, or check that git can read it)"
        )
    return ""


def compare_judged_base(
    base_sha: str, observed: str, paths: tuple[str, ...]
) -> BaseComparison:
    """Which of *paths* the commits from *base_sha* to *observed* touch.

    No *paths* asks about every file: the scope of a judgment that records
    none is the whole tree. Unanswered when the shas are not commit ids, the
    checkout does not hold both, *observed* does not descend from *base_sha*
    (a rewritten base carries no "since" to read), or git could not run.
    """
    undescended = _descent_problem(base_sha, observed)
    if undescended:
        return BaseComparison(None, undescended)
    diff = _git("diff", "--name-only", base_sha, observed, "--", *paths)
    if diff is None or diff.returncode != 0:
        return BaseComparison(None, "git diff could not read the commits since")
    return BaseComparison(
        tuple(line for line in (diff.stdout or "").splitlines() if line.strip())
    )


def judged_paths_of(judgment: NoChangeJudgment) -> tuple[str, ...]:
    """The paths *judgment*'s evidence records, or none if it cannot be read."""
    try:
        evidence = judgment.evidence
    except NoChangeError as exc:
        _logger.warning(
            "%s evidence is unreadable, so it records no judged paths: %s",
            judgment.judgment_id,
            exc,
        )
        return ()
    return evidence.judged_paths if evidence is not None else ()


def _moved_base(
    judgment: NoChangeJudgment, observed: str, where: str
) -> SourceRevalidation:
    """A base that moved: still valid when no commit since touched a judged path."""
    moved = f"{where} moved from {judgment.base_sha} to {observed}"
    paths = judged_paths_of(judgment)
    comparison = (
        compare_judged_base(judgment.base_sha, observed, paths) if paths else None
    )
    if comparison is not None and comparison.touched == ():
        return SourceRevalidation(
            valid=True,
            expected_base_sha=judgment.base_sha,
            observed_base_sha=observed,
            detail=f"{moved}, and no commit since touches a judged path",
        )
    if comparison is None:
        why = " after the no-change was judged, and it records no judged paths"
    elif comparison.touched:
        why = f" and touched {name_paths(comparison.touched)}"
    else:
        why = f", and the commits since could not be read: {comparison.unanswered}"
    return SourceRevalidation(
        valid=False,
        expected_base_sha=judgment.base_sha,
        observed_base_sha=observed,
        detail=f"{moved}{why}, so the outcome is stale — {_stale_next_step(judgment)}",
    )


def revalidate_no_change_source(
    config: DispatchConfig, *, judgment: NoChangeJudgment
) -> SourceRevalidation:
    """Is the base this judgment was made against still the delivery base?

    A no-change is a claim about a tree at a commit. Once the base moves, the
    claim describes a tree nobody is asking about any more — unless the base
    descends from the judged one and no commit since touches a path the
    judgment is about (#2828). Anything else stops being confirmable until the
    work is looked at again, and the refusal names the verb that does that. An
    unreadable base is invalid for the same reason an unread permission is:
    not knowing is not the same as knowing it is fine.
    """
    observed, unread = read_delivery_base(config)
    if not observed:
        return SourceRevalidation(
            valid=False,
            expected_base_sha=judgment.base_sha,
            observed_base_sha="",
            detail=unread,
        )
    where = f"{config.project.repo}@{config.branches.base or 'main'}"
    if observed != judgment.base_sha:
        return _moved_base(judgment, observed, where)
    return SourceRevalidation(
        valid=True,
        expected_base_sha=judgment.base_sha,
        observed_base_sha=observed,
        detail=f"{where} is still {observed}",
    )


@dataclass(frozen=True)
class OutcomeSyncResult:
    """What one pass over one sync job did, and what the job is worth now."""

    sync_job_id: str
    target_kind: str
    target_ref: str
    state: str
    observation_kind: str
    detail: str

    @property
    def acknowledged(self) -> bool:
        return self.state == SYNC_ACKED


def _hold(job: OutcomeSyncJob, plan: SyncPlan) -> OutcomeSyncResult:
    """A pass that changed nothing remote and acknowledged nothing local."""
    _logger.debug("no-change sync %s holds: %s", job.sync_job_id, plan.reason)
    return OutcomeSyncResult(
        sync_job_id=job.sync_job_id,
        target_kind=job.target_kind,
        target_ref=job.target_ref,
        state=job.state,
        observation_kind=plan.observation_kind,
        detail=plan.reason,
    )


def _acknowledge(
    store: Any,
    job: OutcomeSyncJob,
    plan: SyncPlan,
    observation: ItemObservation,
    now: datetime,
) -> OutcomeSyncResult:
    """Record what was seen, then settle the job — observation first, always.

    A job already ``observed`` (a host that died between the two writes) skips
    straight to the acknowledgement rather than observing twice; the store
    refuses a repeated observation, and re-looking is what this pass already
    did.
    """
    if job.state == SYNC_INTENT:
        store.observe_outcome_sync(
            sync_job_id=job.sync_job_id,
            observation_kind=plan.observation_kind,
            observation_ref=observation.ref,
            now=now,
        )
    acked = store.ack_outcome_sync(
        sync_job_id=job.sync_job_id, now=now, detail=plan.reason
    )
    return OutcomeSyncResult(
        sync_job_id=job.sync_job_id,
        target_kind=job.target_kind,
        target_ref=job.target_ref,
        state=acked.state,
        observation_kind=plan.observation_kind,
        detail=plan.reason,
    )


def _write_once(
    adapter: _ItemAdapter,
    judgment: NoChangeJudgment,
    marker: str,
    observation: ItemObservation,
    plan: SyncPlan,
) -> tuple[ItemObservation, SyncPlan] | None:
    """Write the half of the outcome still owed. None when it went unconfirmed.

    ``body=None`` is a resumed pass finishing a closure whose comment already
    landed — the marker is what proves it did, so the note is never posted
    twice.
    """
    body = (
        no_change_comment_body(judgment=judgment, marker=marker)
        if plan.needs_comment
        else None
    )
    if not adapter.apply(body=body, close=True):
        return None
    return (
        ItemObservation(
            available=True,
            state=ITEM_CLOSED,
            marker_found=True,
            ref=observation.ref,
            detail="kew wrote the no-change outcome",
        ),
        SyncPlan(
            SYNC_ACTION_ACK,
            OBSERVED_APPLIED,
            f"kew commented and closed {judgment.source_id} as a no-change",
        ),
    )


def _synchronize_job(
    store: Any,
    config: DispatchConfig,
    judgment: NoChangeJudgment,
    job: OutcomeSyncJob,
    *,
    adapter: _ItemAdapter | None,
    now: datetime,
) -> OutcomeSyncResult:
    """One pass over one sync job: look, revalidate, write once, acknowledge."""
    if adapter is None:
        return _hold(
            job,
            SyncPlan(
                SYNC_ACTION_HOLD,
                OBSERVED_UNAVAILABLE,
                f"no adapter could be built for {job.target_kind}"
                f" {job.target_ref}, so nothing was written",
            ),
        )
    marker = no_change_marker(job.idempotency_key)
    observation = adapter.observe(marker=marker)
    plan = plan_outcome_sync(observation)
    if plan.action == SYNC_ACTION_HOLD:
        return _hold(job, plan)
    if plan.action == SYNC_ACTION_MUTATE:
        revalidation = revalidate_no_change_source(config, judgment=judgment)
        if not revalidation.valid:
            return _hold(job, stale_source_plan(revalidation))
        written = _write_once(adapter, judgment, marker, observation, plan)
        if written is None:
            return _hold(
                job,
                SyncPlan(
                    SYNC_ACTION_HOLD,
                    OBSERVED_WRITE_UNCONFIRMED,
                    f"{job.target_kind} did not confirm the no-change write, so"
                    " the outcome stays sync-pending",
                ),
            )
        observation, plan = written
    return _acknowledge(store, job, plan, observation, now)


def synchronize_no_change(
    store: Any,
    config: DispatchConfig,
    *,
    judgment_id: str,
    now: datetime | None = None,
    adapter: _ItemAdapter | None = None,
) -> tuple[OutcomeSyncResult, ...]:
    """Carry one authorized no-change outcome to every authority it names.

    Idempotent by discovery rather than by memory: each pass looks for the
    judgment's own generation-bound marker on the item first, so a host that
    died after the remote write comes back, finds its own work, and
    acknowledges it instead of writing a second comment and a second close.
    """
    judgment = store.get_no_change_judgment(judgment_id)
    if judgment is None:
        raise ValueError(f"no no-change judgment {judgment_id!r}")
    stamp = now or datetime.now(UTC)
    resolved = (
        adapter if adapter is not None else no_change_item_adapter(config, judgment)
    )
    results = []
    for job in store.list_outcome_sync_jobs(judgment_id):
        if job.settled:
            results.append(
                OutcomeSyncResult(
                    sync_job_id=job.sync_job_id,
                    target_kind=job.target_kind,
                    target_ref=job.target_ref,
                    state=job.state,
                    observation_kind=job.observation_kind
                    or OBSERVED_ALREADY_SYNCHRONIZED,
                    detail=job.detail,
                )
            )
            continue
        results.append(
            _synchronize_job(store, config, judgment, job, adapter=resolved, now=stamp)
        )
    return tuple(results)


def post_no_change_rejection(
    config: DispatchConfig,
    *,
    judgment: NoChangeJudgment,
    reason: str,
    actor: str,
    adapter: _ItemAdapter | None = None,
) -> bool:
    """Leave the disposition note a rejection owes the item. Closes NOTHING.

    Best-effort by design: the rejection is already durable in the ledger, and
    a tracker that cannot be reached must not turn a refusal into an error the
    caller might retry into a second refusal. ``close=False`` is the whole
    point — a rejected no-change leaves the item open and unsatisfied.
    """
    resolved = (
        adapter if adapter is not None else no_change_item_adapter(config, judgment)
    )
    if resolved is None:
        return False
    body = no_change_rejection_body(judgment=judgment, reason=reason, actor=actor)
    try:
        return bool(resolved.apply(body=body, close=False))
    except Exception as exc:  # noqa: BLE001 — a note must never sink a refusal
        _logger.debug("no-change rejection note skipped: %s", exc)
        return False
