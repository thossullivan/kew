"""Weekly outcome aggregation, computed in SQLite (C3/#2301).

The `/scorecard` outcome section answers one question — *what did the factory
deliver, week by week* — and every number on it except one comes from this
module. The exception is "merged on main", which is a local ``git log`` read
(:mod:`kew.merged_main`); the ledger cannot know about a PR nobody dispatched.

Four reads, four shapes, one week expression. They are SQL rather than a
Python fold over ``list_all`` for the reason the rest of C3 is: the ledger
grows forever and a report must not read all of it to print eight weeks.

The rules, which the reader may not quietly re-interpret:

* **Weeks are UTC, Monday-first.** ``date(ts, 'weekday 0', '-6 days')`` is the
  Monday of the week containing *ts*: ``weekday 0`` moves to the coming Sunday
  (or stays put on a Sunday), and six days back from that Sunday is its
  Monday. Stored timestamps carry their offset, and SQLite normalizes an
  ISO-8601 value with an offset to UTC before applying a modifier, so a run
  dispatched at 23:30-05:00 lands in the following UTC day's week — the same
  week the audit chain and the budget windows put it in.

* **PRs are counted distinct, never summed off run rows.** ``runs.pr_state`` is
  a snapshot written when the row was written, so three rows for one PR are
  three snapshots of one fact, not three merges. Every PR figure here groups
  by ``pr_url`` first and counts groups.

* **Base-integration markers are not runs.** ``record_base_integration_step``
  writes a $0 ``run_kind='resolve'`` row to bound a merge-main streak. It is a
  marker, not work: counted as a run it inflates the run count of every week a
  branch needed rebasing. Excluded by (run_kind, error_category) — the pair,
  not the kind alone, because a real ``kew resolve`` run is also ``resolve``.

* **A merged PR belongs to the week it merged.** The scan behind each read is
  bounded (:data:`ATTRIBUTION_LOOKBACK_DAYS`) so a report cannot cost the whole
  ledger, but that bound buys cheap *attribution*, never membership: a PR
  dispatched long before it merged is still in its merge week, or the oldest
  weeks of "kew's share of merged" count it in the ``git log`` denominator and
  drop it from the numerator (#2387).

* **Nothing is repaired.** A row with no ``pr_merged_at`` behind a merged
  ``pr_state``, a READY round with no dispatch before it, a non-numeric node
  id: each is tolerated by the query (``COALESCE``, an ``IS NOT NULL`` filter,
  a ``DISTINCT`` over whichever key the row has) and left exactly as written.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: The Monday of the UTC week containing a stored ISO-8601 timestamp column.
#: A literal (no parameters) so it can be interpolated into a SELECT list and
#: a GROUP BY without a bound value appearing twice.
_WEEK_OF = "date({column}, 'weekday 0', '-6 days')"

#: ``error_category`` of the two marker rows ``record_base_integration_step``
#: writes. Restated rather than imported: this module sits at the bottom of the
#: dispatch stack and ``kew.graph.driver.base_integration`` is near the top.
#: ``tests/test_outcome_scorecard.py`` guards the two against drift.
MARKER_ERROR_CATEGORIES = ("base_integration_step", "base_integration_exhausted")

#: Run kinds whose subject is an already-open PR (restating
#: ``run_store.PR_SUBJECT_RUN_KINDS``, which is defined above this module's
#: importer) plus the kinds that are not implementation work at all. A merged
#: PR is "authored by kew" when some run outside this set carries its URL:
#: reviewing, fixing CI on, or rebasing a PR a person opened is touching it,
#: not writing it.
NON_AUTHORING_RUN_KINDS = (
    "review",
    "ci_fix",
    "resolve",
    "adversarial_review",
    "review_remediation",
    "review_stage",
    "readiness_probe",
    "triage",
    "acceptance_author",
)

#: The verdicts that read as merge-ready. ``accepted_verdict`` is the driver's
#: own disposition when the review stop rule took a round as
#: ``READY_WITH_RESIDUALS`` (#2151); reading ``verdict`` alone would date the
#: item's readiness to a round that never happened.
READY_VERDICTS = ("READY", "READY_WITH_RESIDUALS")

#: How far before the window a run may have started and still be attributed to
#: a PR or an item inside it. The scan has to be bounded by *something* —
#: "every run that ever carried a PR URL" is the read C3 exists to remove — and
#: a fortnight is well past the lifetime of the runs of one PR.
#:
#: It bounds *attribution* only. Whether a merged PR is IN a week is decided by
#: the merge evidence on its rows and never by when its runs started: a PR
#: dispatched a month before it merged would otherwise be counted by the git
#: denominator and dropped from the kew numerator, understating the oldest
#: weeks of the headline share chart (#2387). See
#: :meth:`OutcomeStoreMixin.merged_prs_by_week`.
ATTRIBUTION_LOOKBACK_DAYS = 14

#: Largest window ``outcome_*`` reads will serve. A year of weeks is already
#: more than the surface draws; past that a caller is asking for the whole
#: ledger again.
MAX_OUTCOME_WEEKS = 52

#: ``runs.pr_state`` for a PR the sync saw merged. GitHub's ``state`` is
#: upper case and ``kew.outcomes`` stores it verbatim, so the ledger never
#: holds a lower-case spelling of it.
MERGED_PR_STATE = "MERGED"

#: The node status whose arrival is the "issue closed" proxy. The authority is
#: ``kew.graph.domain.NodeStatus.DONE``; this module may not import
#: ``kew.graph`` (and neither may the service layer above it), so the literal
#: is restated and pinned by a test.
NODE_STATUS_DONE = "done"

#: The driver's per-node transition event, as ``journal.record_tick`` emits it.
NODE_STATUS_EVENT = "epic_node_status"


@dataclass(frozen=True)
class MergedPrWeek:
    """One merged PR the ledger knows about, in the week it merged."""

    week_start: str
    """ISO date of the Monday of the merge week, UTC."""
    pr_url: str
    """The group key. Distinct PRs are distinct URLs — never run rows."""
    authored: bool
    """Whether some run outside :data:`NON_AUTHORING_RUN_KINDS` carries it."""
    cost_usd: float
    """Ledger spend on every run carrying this URL within the read window."""
    observed_at: str | None
    """When the newest merged snapshot for this PR was written, or None when no
    row carried a timestamp. This is how fresh the merge count is — the ledger
    learns a PR merged only when something wrote a row saying so."""


@dataclass(frozen=True)
class ReadyItemWeek:
    """One work item that first read merge-ready in the week, and how long it
    took from the dispatch that produced it."""

    week_start: str
    item_id: str
    hours_to_ready: float
    """Dispatch → READY, in hours: the READY round's ``recorded_at`` less the
    first implementation run that started at or before it."""
    rounds: int
    """Review rounds recorded for the item between that dispatch and that
    round, inclusive — the rounds this readiness actually cost."""


@dataclass(frozen=True)
class SpendWeek:
    """Ledger spend in one dispatch week, and the part of it no PR explains."""

    week_start: str
    spend_total: float
    spend_unattributed: float
    """Spend on runs carrying no PR URL. Not waste — a run that failed before
    opening a PR, a probe, a triage pass — but not spend any merge can be
    divided by, which is why a cost-per-merged figure must print it beside."""
    runs: int


def _placeholders(values: Sequence[object]) -> str:
    """``?, ?, ?`` for *values* — the values stay bound, never interpolated."""
    return ", ".join("?" for _ in values)


class OutcomeStoreMixin:
    """The weekly outcome reads of :class:`~kew.run_store.SqliteRunStore`."""

    if TYPE_CHECKING:
        # Provided by the composition root; declared so the mixin body
        # type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection

    def _outcome_bounds(self, since: datetime) -> tuple[str, str]:
        """``(window_start, scan_start)`` as stored ISO text.

        The second is the first the scan may read: earlier than the window by
        :data:`ATTRIBUTION_LOOKBACK_DAYS` so a PR that merged on the Monday of
        the first week still owns the runs that opened it.
        """
        from kew.run_store import _dt_to_iso

        start = _dt_to_iso(since)
        scan = _dt_to_iso(since - timedelta(days=ATTRIBUTION_LOOKBACK_DAYS))
        if start is None or scan is None:  # pragma: no cover - None input only
            raise ValueError("outcome window bounds must be timezone-aware")
        return start, scan

    def merged_prs_by_week(self, *, since: datetime) -> list[MergedPrWeek]:
        """Every PR the ledger saw merged on or after *since*, one row each.

        Grouped by ``pr_url``, so a PR with an implementation run, two review
        runs and a CI fix is one row. The merge week comes from
        ``pr_merged_at`` when any row carried it and from the newest merged
        snapshot's own timestamp when none did — a tolerated bad row, read for
        what it does say rather than rewritten.

        A row is read when it started inside the attribution window
        (:data:`ATTRIBUTION_LOOKBACK_DAYS` before *since*) **or** when it
        carries merge evidence inside the window itself: a ``pr_merged_at``, or
        a finish that dates a merged snapshot carrying no timestamp. That
        second half is what keeps a PR dispatched long before it merged in the
        numerator of its merge week. Deciding membership by the attribution
        scan alone understated the oldest weeks of "kew's share of merged",
        because the ``git log`` denominator counts such a PR regardless
        (#2387).
        """
        start, scan = self._outcome_bounds(since)
        markers = MARKER_ERROR_CATEGORIES
        authoring = NON_AUTHORING_RUN_KINDS
        # ``pr_state`` holds GitHub's own spelling, which is upper case
        # (``MERGED``/``OPEN``/``CLOSED``, plus kew's ``ABSENT``), and SQLite
        # compares TEXT case-sensitively — so the tolerance for a merged
        # snapshot that carries no ``pr_merged_at`` only exists if the literal
        # matches what the writer wrote.
        merged = f"(pr_state = '{MERGED_PR_STATE}' OR pr_merged_at IS NOT NULL)"
        sql = (
            # Every clause below is a literal; the window, the marker
            # categories and the run kinds are all bound parameters.
            "SELECT * FROM ("  # nosec B608
            " SELECT pr_url,"
            f"  {_WEEK_OF.format(column='merged_at')} AS week_start,"
            "   merged_at, authored, cost_usd, observed_at"
            " FROM ("
            "  SELECT pr_url,"
            "   COALESCE(MAX(pr_merged_at),"
            f"    MAX(CASE WHEN {merged} THEN COALESCE(finished_at, started_at) END))"
            "    AS merged_at,"
            f"   MAX(CASE WHEN {merged}"
            "     THEN COALESCE(finished_at, started_at) END) AS observed_at,"
            "   MAX(CASE WHEN COALESCE(run_kind, 'dispatch')"
            f"     NOT IN ({_placeholders(authoring)}) THEN 1 ELSE 0 END) AS authored,"
            "   COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd"
            "  FROM runs"
            "  WHERE pr_url IS NOT NULL AND pr_url != ''"
            # Attribution window OR merge evidence inside the window: three
            # plain column ranges, which the planner reads as a MULTI-INDEX OR
            # of three seeks (idx_runs_started, idx_runs_pr_merged,
            # idx_runs_finished). A COALESCE around any of them, or a column
            # without its index, collapses the whole OR into a ledger scan.
            "   AND (started_at >= ? OR pr_merged_at >= ? OR finished_at >= ?)"
            "   AND NOT (COALESCE(run_kind, 'dispatch') = 'resolve'"
            f"    AND error_category IN ({_placeholders(markers)}))"
            "  GROUP BY pr_url"
            " )"
            ")"
            " WHERE merged_at IS NOT NULL AND week_start >= "
            f"{_WEEK_OF.format(column='?')}"
            " ORDER BY week_start, pr_url"
        )
        rows = self._conn.execute(  # nosemgrep
            sql, (*authoring, scan, start, start, *markers, start)
        ).fetchall()
        return [
            MergedPrWeek(
                week_start=str(row["week_start"]),
                pr_url=str(row["pr_url"]),
                authored=bool(row["authored"]),
                cost_usd=float(row["cost_usd"] or 0.0),
                observed_at=row["observed_at"],
            )
            for row in rows
        ]

    def ready_items_by_week(self, *, since: datetime) -> list[ReadyItemWeek]:
        """Dispatch → READY for every item that first read ready in the window.

        An item whose earliest READY round has no implementation run at or
        before it — a round recorded against work dispatched before the scan
        window, or a ledger that lost the run — is omitted rather than dated
        from nothing.
        """
        start, scan = self._outcome_bounds(since)
        authoring = NON_AUTHORING_RUN_KINDS
        markers = MARKER_ERROR_CATEGORIES
        effective = "COALESCE(NULLIF(accepted_verdict, ''), verdict)"
        sql = (
            # Literal clauses; window, verdicts, kinds and categories are bound.
            "WITH ready AS ("  # nosec B608
            "  SELECT item_id, MIN(recorded_at) AS ready_at"
            "  FROM review_rounds"
            f"  WHERE recorded_at >= ? AND {effective} IN "
            f"({_placeholders(READY_VERDICTS)})"
            "  GROUP BY item_id"
            " ), paired AS ("
            "  SELECT r.item_id, r.ready_at,"
            "   (SELECT MIN(started_at) FROM runs"
            "     WHERE runs.item_id = r.item_id AND runs.started_at >= ?"
            "      AND runs.started_at <= r.ready_at"
            "      AND COALESCE(runs.run_kind, 'dispatch') NOT IN "
            f"({_placeholders(authoring)})"
            "      AND NOT (COALESCE(runs.run_kind, 'dispatch') = 'resolve'"
            f"       AND runs.error_category IN ({_placeholders(markers)}))"
            "   ) AS dispatched_at"
            "  FROM ready r"
            " )"
            f" SELECT {_WEEK_OF.format(column='ready_at')} AS week_start,"
            " item_id,"
            " (julianday(ready_at) - julianday(dispatched_at)) * 24.0 AS hours,"
            " (SELECT COUNT(*) FROM review_rounds rr"
            "   WHERE rr.item_id = paired.item_id"
            "    AND rr.recorded_at >= paired.dispatched_at"
            "    AND rr.recorded_at <= paired.ready_at) AS rounds"
            " FROM paired WHERE dispatched_at IS NOT NULL"
            " ORDER BY week_start, item_id"
        )
        params = (start, *READY_VERDICTS, scan, *authoring, *markers)
        rows = self._conn.execute(sql, params).fetchall()  # nosemgrep
        return [
            ReadyItemWeek(
                week_start=str(row["week_start"]),
                item_id=str(row["item_id"]),
                hours_to_ready=float(row["hours"] or 0.0),
                rounds=int(row["rounds"] or 0),
            )
            for row in rows
        ]

    def spend_by_week(self, *, since: datetime) -> list[SpendWeek]:
        """Ledger spend per dispatch week, with the unattributed part split out.

        Bucketed on ``started_at``: this is what the factory spent that week,
        not what that week's merges cost (which is
        :meth:`merged_prs_by_week`).
        """
        start, _scan = self._outcome_bounds(since)
        markers = MARKER_ERROR_CATEGORIES
        week = _WEEK_OF.format(column="started_at")
        sql = (
            # Literal clauses; the window and the marker categories are bound.
            f"SELECT {week} AS week_start,"  # nosec B608
            " COALESCE(SUM(estimated_cost_usd), 0.0) AS spend_total,"
            " COALESCE(SUM(CASE WHEN pr_url IS NULL OR pr_url = ''"
            "  THEN estimated_cost_usd ELSE 0 END), 0.0) AS spend_unattributed,"
            " COUNT(*) AS runs"
            " FROM runs WHERE started_at >= ?"
            "  AND NOT (COALESCE(run_kind, 'dispatch') = 'resolve'"
            f"   AND error_category IN ({_placeholders(markers)}))"
            " GROUP BY week_start ORDER BY week_start"
        )
        rows = self._conn.execute(sql, (start, *markers)).fetchall()  # nosemgrep
        return [
            SpendWeek(
                week_start=str(row["week_start"]),
                spend_total=float(row["spend_total"] or 0.0),
                spend_unattributed=float(row["spend_unattributed"] or 0.0),
                runs=int(row["runs"] or 0),
            )
            for row in rows
        ]

    def nodes_done_by_week(self, *, since: datetime) -> dict[str, int]:
        """Distinct nodes that reached ``done`` per week — the closed-issue proxy.

        Distinct by node, not by event: a node the driver re-observed as done
        after a reconcile emits the transition twice and closed one issue. The
        key is ``item_id`` when the row has one and the issue number otherwise,
        so a pre-``item_id`` row still counts once.
        """
        start, _scan = self._outcome_bounds(since)
        week = _WEEK_OF.format(column="timestamp")
        sql = (
            # Literal clauses; the event name, the window and the status are
            # bound. The JSON arm is unindexed, which is why the event name and
            # the window — both covered by idx_audit_events_event_ts — lead.
            f"SELECT {week} AS week_start,"  # nosec B608
            " COUNT(DISTINCT COALESCE(NULLIF(item_id, ''), CAST(issue_num AS TEXT)))"
            "  AS nodes_done"
            " FROM audit_events"
            " WHERE event = ? AND timestamp >= ?"
            "  AND json_valid(payload) AND json_extract(payload, '$.to') = ?"
            " GROUP BY week_start ORDER BY week_start"
        )
        rows = self._conn.execute(  # nosemgrep
            sql, (NODE_STATUS_EVENT, start, NODE_STATUS_DONE)
        ).fetchall()
        return {str(row["week_start"]): int(row["nodes_done"] or 0) for row in rows}
