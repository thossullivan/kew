"""Outcome-linked spend report for `kew report`.

Aggregates window run records from the SQLite run store, resolves PR
merge state in a single batched GraphQL call, and renders a shareable
markdown report whose headline metric is cost per merged PR.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kew.notifier import NotificationEvent

from kew.audit import DEFAULT_ANCHORS_PATH
from kew.config import BudgetConfig, DispatchConfig, project_root
from kew.ids import item_id_sort_key, item_ref_or_num
from kew.ledger_identity import canonical_project_root, open_managed_store
from kew.outcomes import SyncResult, sync_outcomes
from kew.pr_states import pr_states_by_url
from kew.pricing import (
    build_pricing_overrides,
    estimate_cost,
)
from kew.run_store import (
    READINESS_PROBE_RUN_KIND,
    REVIEW_RUN_KINDS,
    InteractiveUsage,
    RunRecord,
    RunStore,
    SqliteRunStore,
)
from kew.scorecard import (
    ComplexityRow,  # noqa: F401
    ModelRow,  # noqa: F401
    ModelScorecard,  # noqa: F401
    _escape_cell,
    compute_model_scorecard,
    render_model_scorecard,
)
from kew.subprocess_utils import run


def _parse_pr_states_payload(stdout: str) -> dict[str, str] | None:
    """Parse the GraphQL response into pr_url -> state, or None when malformed."""
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    states: dict[str, str] = {}
    for node in data.values():
        if isinstance(node, dict) and node.get("url") and node.get("state"):
            states[str(node["url"])] = str(node["state"])
    return states


def resolve_pr_states(pr_urls: Sequence[str]) -> dict[str, str] | None:
    """Resolve merge state for a batch of PR URLs in one gh invocation.

    Retired from the report path (outcomes now come from persisted pr_state via
    set_outcome/sync_outcomes); retained here because its unit tests document the
    format and it may be useful for ad-hoc callers.

    Builds a single GraphQL query using aliased ``resource(url:)`` fields,
    so the call count is constant regardless of PR count (spec: <=2).

    Returns:
        Mapping of pr_url -> state ("OPEN" / "CLOSED" / "MERGED").
        Empty dict when there is nothing to resolve (no gh call made).
        None when GitHub is unreachable or gh is unavailable — callers
        render a degraded Outcomes section instead of failing.
    """
    if not pr_urls:
        return {}
    parts = [
        f"pr{i}: resource(url: {json.dumps(url)}) "
        "{ ... on PullRequest { url state } }"
        for i, url in enumerate(pr_urls)
    ]
    query = "query { " + " ".join(parts) + " }"
    try:
        result = run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_pr_states_payload(result.stdout)


@dataclass
class IssueCost:
    """Aggregated cost for one issue inside the reporting window."""

    issue_num: int
    title: str
    cost_usd: float
    status: str
    pr_url: str | None
    # Durable work-item id (A3); ``None`` for legacy rows predating the column.
    item_id: str | None = None


@dataclass
class ApprovalHold:
    """An issue currently held by the approval gate (from the audit log)."""

    issue_num: int
    estimate_usd: float | None
    basis: str


def open_approval_holds(
    store: RunStore, *, issue_nums: Sequence[int] | None = None
) -> list[ApprovalHold]:
    """Issues whose latest approval event is `approval_hold` (still open).

    Reads the audit log newest-first; the first `approval_*` event seen per
    issue is its latest state. A later grant/confirm/decline means resolved.
    Offline — no GitHub call.

    ``issue_nums`` narrows the audit read to those issues. It is a scope, not a
    second rule — inside it the latest-event rule above is unchanged — and it is
    pushed into SQL rather than applied after, because filtering after the read
    still parses every approval payload on the host. A caller on a poll budget
    (the epic stream's needs-you join, every 1s) asks for its own nodes (#2129).
    """
    seen: set[int] = set()
    holds: list[ApprovalHold] = []
    for ev in store.list_audit_events(
        limit=None, event_prefix="approval_", issue_nums=issue_nums
    ):
        if ev.issue_num is None:
            continue
        if ev.issue_num in seen:
            continue
        seen.add(ev.issue_num)
        if ev.event == "approval_hold":
            payload = ev.payload or {}
            holds.append(
                ApprovalHold(
                    issue_num=ev.issue_num,
                    estimate_usd=payload.get("estimate_usd"),
                    basis=str(payload.get("basis", "")),
                )
            )
    return sorted(holds, key=lambda h: h.issue_num)


@dataclass
class _Window:
    """Everything read from the run store for one report window."""

    records: list[RunRecord]
    approval_holds: list[ApprovalHold]
    interactive: list[InteractiveUsage]
    sync: SyncResult = field(default_factory=SyncResult)


def _load_window(config: DispatchConfig, since: datetime) -> _Window:
    """Open the store once, sync outcomes (best-effort), read the window.

    A missing dispatch.db is an empty window (the report renders zero activity
    rather than erroring).
    """
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        return _Window(records=[], approval_holds=[], interactive=[])
    with _open_report_store(db_path) as store:
        sync = sync_outcomes(store, config)
        return _Window(
            records=store.list_since(since),
            approval_holds=open_approval_holds(store),
            interactive=store.list_interactive_usage_since(since),
            sync=sync,
        )


def _open_report_store(db_path: Path) -> SqliteRunStore:
    """Open a writable report store through the canonical ledger boundary."""
    root = canonical_project_root(project_root())
    return open_managed_store(
        project_root=root,
        db_path=db_path.resolve(),
        anchors_path=root / DEFAULT_ANCHORS_PATH,
        read_only=False,
        check_same_thread=True,
    )


_PricingOverrides = dict[str, tuple[float, float]] | None


@dataclass
class GroupStats:
    """Per-model or per-runner aggregation over completed window runs.

    Window-scoped equivalent of run_store.analytics()'s by_model/by_runner
    shape (runs, success rate, avg cost). analytics() itself aggregates the
    whole store and cannot be reused for a windowed report.
    """

    runs: int = 0
    success: int = 0
    total_cost: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.runs if self.runs else 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost / self.runs if self.runs else 0.0


@dataclass
class InteractiveGroupStats:
    """Per-model interactive aggregation: tokens + CC-reported cost."""

    total_tokens: int = 0
    total_cost: float = 0.0


@dataclass
class CacheGroupStats:
    """Per (runner, model, provider) cache token aggregation."""

    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    uncached_input_tokens: int = 0
    runs: int = 0
    cold_runs: int = 0

    @property
    def hit_ratio(self) -> float:
        total = (
            self.cache_read_tokens
            + self.cache_creation_tokens
            + self.uncached_input_tokens
        )
        return self.cache_read_tokens / total if total else 0.0


@dataclass
class CacheStats:
    """Aggregate cache economics for the report window."""

    by_group: dict[str, CacheGroupStats]
    saved_usd: float
    all_cold_usd: float

    @property
    def savings_pct(self) -> float:
        return (self.saved_usd / self.all_cold_usd * 100) if self.all_cold_usd else 0.0


@dataclass
class ReportStats:
    """Everything the renderer needs, precomputed."""

    total_spend: float
    review_spend: float
    run_count: int
    success_count: int
    failure_count: int
    killed_count: int
    budget_exceeded_count: int
    budget_stopped_count: int
    pending_count: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    prs_opened: int
    prs_merged: int | None
    cost_per_merged_pr: float | None
    median_cost: float | None
    top_issues: list[IssueCost]
    by_model: dict[str, GroupStats]
    by_runner: dict[str, GroupStats]
    by_reported_model: dict[str, GroupStats]
    held: list[IssueCost]
    approval_holds: list[ApprovalHold]
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    by_interactive_source: dict[str, dict[str, InteractiveGroupStats]] = field(
        default_factory=dict
    )
    interactive_total_usd: float = 0.0
    cache: CacheStats | None = None
    # Planned half of the cost pair (#1092): summed token×pricing-bound spend
    # over the ``planned_run_count`` runs that recorded a bound, plus those
    # runs' actual spend. Rows predating the column are excluded from all three
    # so the comparison stays apples-to-apples.
    planned_spend: float = 0.0
    paired_spend: float = 0.0
    planned_run_count: int = 0
    # Readiness-probe spend (I05A): real money, inside ``total_spend``, broken
    # out here so it is visible rather than hidden. Every OTHER field on this
    # dataclass is computed over the customer cohort with probes removed — see
    # ``compute_stats``.
    probe_spend: float = 0.0


def _distinct_pr_urls(records: Sequence[RunRecord]) -> list[str]:
    """Distinct pr_urls from window runs; runs without a PR are excluded."""
    seen: set[str] = set()
    for record in records:
        if record.pr_url:
            seen.add(record.pr_url)
    return sorted(seen)


def _status_counts(records: Sequence[RunRecord]) -> dict[str, int]:
    """Count records per report status bucket.

    "killed" counts two cases: runs whose status is literally "killed"
    (operator-terminated), and failure records whose error_category is
    "stale_pending" (crashed/killed dispatches marked by
    cleanup_stale_pending); "failure" excludes both. Other statuses are
    counted only in the run total.
    """
    counts = dict.fromkeys(
        (
            "success",
            "failure",
            "killed",
            "budget_exceeded",
            "budget_stopped",
            "pending",
        ),
        0,
    )
    for r in records:
        if r.status == "killed" or (
            r.status == "failure" and r.error_category == "stale_pending"
        ):
            counts["killed"] += 1
        elif r.status == "budget_stop":
            # Graceful budget stop (#859) folds into the budget_stopped family
            # for reporting — same terminal outcome, WIP committed.
            counts["budget_stopped"] += 1
        elif r.status in (
            "success",
            "failure",
            "budget_exceeded",
            "budget_stopped",
            "pending",
        ):
            counts[r.status] += 1
    return counts


def _merge_outcomes(
    pr_urls: Sequence[str],
    pr_states: dict[str, str] | None,
    total_spend: float,
) -> tuple[int | None, float | None]:
    """Return (prs_merged, cost_per_merged_pr); None/None when gh was down."""
    if pr_states is None:
        return None, None
    prs_merged = sum(1 for url in pr_urls if pr_states.get(url) == "MERGED")
    cost_per_merged_pr = total_spend / prs_merged if prs_merged else None
    return prs_merged, cost_per_merged_pr


def _group_breakdowns(
    records: Sequence[RunRecord],
) -> tuple[dict[str, GroupStats], dict[str, GroupStats], dict[str, GroupStats]]:
    """Aggregate completed (non-pending) runs into by-model/runner/reported tables."""
    by_model: dict[str, GroupStats] = {}
    by_runner: dict[str, GroupStats] = {}
    by_reported_model: dict[str, GroupStats] = {}
    for r in records:
        if r.status == "pending":
            continue
        targets = [
            (r.model or "unknown", by_model),
            (r.runner_name or "unknown", by_runner),
        ]
        if r.reported_model:
            targets.append((r.reported_model, by_reported_model))
        for key, table in targets:
            group = table.setdefault(key, GroupStats())
            group.runs += 1
            group.success += int(r.status == "success")
            group.total_cost += r.estimated_cost_usd
    return by_model, by_runner, by_reported_model


def _record_item_key(r: RunRecord) -> str:
    """Aggregation key: durable ``item_id``, or legacy ``issue_num`` (pre-A3)."""
    return r.item_id if r.item_id is not None else str(r.issue_num)


def _issue_sort_key(issue: IssueCost) -> tuple[int, int | str]:
    """Numeric-aware ordering for an aggregated issue row (GitHub ints first)."""
    return item_id_sort_key(issue.item_id or str(issue.issue_num))


def _aggregate_issue_costs(
    records: Sequence[RunRecord],
    *,
    status_filter: str | set[str] | None = None,
    latest_status_wins: bool = False,
) -> dict[str, IssueCost]:
    """Accumulate per-issue cost over window runs (ordered by started_at).

    With *status_filter* only matching records contribute (accepts a single
    status string or a set of statuses). With *latest_status_wins* an
    issue's status follows its most recent run. Keyed on the durable
    ``item_id`` (falling back to the legacy ``issue_num`` for pre-A3 rows) so
    non-numeric Linear items — which all share the ``0`` issue_num sentinel —
    don't collapse into a single row.
    """
    issues: dict[str, IssueCost] = {}
    for r in records:  # list_since() orders by started_at ascending
        if status_filter is not None:
            if isinstance(status_filter, set):
                if r.status not in status_filter:
                    continue
            elif r.status != status_filter:
                continue
        key = _record_item_key(r)
        entry = issues.get(key)
        if entry is None:
            issues[key] = IssueCost(
                issue_num=r.issue_num,
                title=r.title,
                cost_usd=r.estimated_cost_usd,
                status=r.status,
                pr_url=r.pr_url,
                item_id=r.item_id,
            )
            continue
        entry.cost_usd += r.estimated_cost_usd
        entry.title = r.title or entry.title
        if latest_status_wins:
            entry.status = r.status  # latest run wins
        entry.pr_url = r.pr_url or entry.pr_url
    return issues


def _qs_bucket(qs: str | None) -> str:
    if not qs or qs == "main":
        return "main"
    return qs


def _interactive_breakdown(
    rows: Sequence[InteractiveUsage],
) -> tuple[dict[str, dict[str, InteractiveGroupStats]], float]:
    """Aggregate interactive-usage rows by query_source bucket, then model."""
    by_source: dict[str, dict[str, InteractiveGroupStats]] = {}
    total = 0.0
    for r in rows:
        bucket = _qs_bucket(getattr(r, "query_source", None))
        by_model = by_source.setdefault(bucket, {})
        group = by_model.setdefault(r.model or "unknown", InteractiveGroupStats())
        group.total_tokens += (
            r.input_tokens
            + r.output_tokens
            + r.cache_creation_tokens
            + r.cache_read_tokens
        )
        group.total_cost += r.cost_usd
        total += r.cost_usd
    return by_source, total


def _cache_group_key(r: RunRecord) -> str:
    provider = getattr(r, "provider", None) or "unknown"
    return f"{r.runner_name or 'unknown'}/{r.model or 'unknown'}/{provider}"


def _accumulate_cache_record(
    r: RunRecord,
    g: CacheGroupStats,
    *,
    overrides: _PricingOverrides = None,
) -> tuple[float, float]:
    """Fold one record into its group; return (cold_cost, saved)."""
    g.runs += 1
    g.cache_read_tokens += r.cache_read_tokens
    g.cache_creation_tokens += r.cache_creation_tokens
    g.uncached_input_tokens += r.input_tokens
    if r.cache_read_tokens == 0 and r.cache_creation_tokens == 0:
        g.cold_runs += 1
    if not r.model:
        return 0.0, 0.0
    cold_input = r.input_tokens + r.cache_read_tokens + r.cache_creation_tokens
    cold_cost = estimate_cost(
        r.model,
        input_tokens=cold_input,
        output_tokens=r.output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        overrides=overrides,
    )
    return cold_cost, cold_cost - r.estimated_cost_usd


def _compute_cache_stats(
    records: Sequence[RunRecord],
    *,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> CacheStats | None:
    """Aggregate cache economics from run records that have cache token data."""
    has_cache_data = any(
        r.cache_creation_tokens > 0 or r.cache_read_tokens > 0 for r in records
    )
    if not has_cache_data:
        return None

    groups: dict[str, CacheGroupStats] = {}
    total_saved = 0.0
    total_all_cold = 0.0

    for r in records:
        if r.status == "pending":
            continue
        g = groups.setdefault(_cache_group_key(r), CacheGroupStats())
        cold_cost, saved = _accumulate_cache_record(r, g, overrides=overrides)
        total_all_cold += cold_cost
        total_saved += saved

    return CacheStats(
        by_group=groups,
        saved_usd=max(0.0, total_saved),
        all_cold_usd=total_all_cold,
    )


def _review_spend(records: Sequence[RunRecord]) -> float:
    """Governed-review spend — a run_kind-filtered sub-bucket of total_spend
    (#445 precedent): heavyweight reviewer/remediation runs stay visible
    without inflating the per-issue implementation economics."""
    return sum(r.estimated_cost_usd for r in records if r.run_kind in REVIEW_RUN_KINDS)


def _probe_spend(records: Sequence[RunRecord]) -> float:
    """Readiness-probe spend — the other run_kind-filtered sub-bucket of
    ``total_spend`` (I05A). A probe is one bounded provider call kew made to
    verify an exact execution boundary: real money with no work item, no PR and
    no outcome, so it must be visible in the financial totals and absent from
    every cohort that describes customer work."""
    return sum(
        r.estimated_cost_usd for r in records if r.run_kind == READINESS_PROBE_RUN_KIND
    )


def _work_cohort(records: Sequence[RunRecord]) -> list[RunRecord]:
    """The customer-work cohort: every run except readiness probes.

    Excluding the kind here — once, at the top of ``compute_stats`` — is what
    keeps a probe from improving or degrading run counts, success and failure
    counts, per-model/runner success rates, the cost median, per-issue
    economics and the held list. The alternative (copying probe cost into a
    second total) would create exactly the second truth this slice exists to
    avoid."""
    return [r for r in records if r.run_kind != READINESS_PROBE_RUN_KIND]


def _planned_pair(records: Sequence[RunRecord]) -> tuple[float, float, int]:
    """(planned bound, matching actual, run count) over the runs that recorded
    a planned bound (#1092). Runs predating the column carry NULL and are
    excluded from all three, so the ratio compares like with like."""
    paired = [r for r in records if r.planned_cost_usd is not None]
    return (
        sum(r.planned_cost_usd or 0.0 for r in paired),
        sum(r.estimated_cost_usd for r in paired),
        len(paired),
    )


def compute_stats(
    records: Sequence[RunRecord],
    pr_states: dict[str, str] | None,
    approval_holds: Sequence[ApprovalHold] = (),
    interactive: Sequence[InteractiveUsage] = (),
    *,
    pricing_overrides: dict[str, tuple[float, float]] | None = None,
) -> ReportStats:
    """Aggregate window run records into report statistics.

    See _status_counts for the killed/failure split. pr_states=None means
    GitHub was unreachable: outcome fields become None and the renderer
    degrades the Outcomes section.

    Two populations, deliberately: ``total_spend`` and the token totals cover
    EVERY run in the window, because money and tokens kew actually spent must
    never go missing. Everything else is computed over ``_work_cohort`` — the
    window minus readiness probes (I05A) — because a $0.02 capability check is
    not a run anyone asked for, not a success anyone earned, and not an issue
    anyone filed. ``probe_spend`` names the difference so the two reconcile.
    """
    total_spend = sum(r.estimated_cost_usd for r in records)
    probe_spend = _probe_spend(records)
    cohort = _work_cohort(records)
    review_spend = _review_spend(cohort)
    planned_spend, paired_spend, planned_run_count = _planned_pair(cohort)
    counts = _status_counts(cohort)

    pr_urls = _distinct_pr_urls(cohort)
    prs_merged, cost_per_merged_pr = _merge_outcomes(pr_urls, pr_states, total_spend)

    costs = [r.estimated_cost_usd for r in cohort]
    median_cost = statistics.median(costs) if costs else None

    by_model, by_runner, by_reported_model = _group_breakdowns(cohort)

    issues = _aggregate_issue_costs(cohort, latest_status_wins=True)
    top_issues = sorted(
        issues.values(), key=lambda i: (-i.cost_usd, _issue_sort_key(i))
    )[:5]

    held_by_issue = _aggregate_issue_costs(
        cohort, status_filter={"budget_exceeded", "budget_stopped", "budget_stop"}
    )
    held = sorted(held_by_issue.values(), key=_issue_sort_key)

    by_interactive_source, interactive_total_usd = _interactive_breakdown(interactive)

    cache = _compute_cache_stats(cohort, overrides=pricing_overrides)

    return ReportStats(
        total_spend=total_spend,
        review_spend=review_spend,
        run_count=len(cohort),
        success_count=counts["success"],
        failure_count=counts["failure"],
        killed_count=counts["killed"],
        budget_exceeded_count=counts["budget_exceeded"],
        budget_stopped_count=counts["budget_stopped"],
        pending_count=counts["pending"],
        total_tokens=sum(r.total_tokens for r in records),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        prs_opened=len(pr_urls),
        prs_merged=prs_merged,
        cost_per_merged_pr=cost_per_merged_pr,
        median_cost=median_cost,
        top_issues=top_issues,
        by_model=by_model,
        by_runner=by_runner,
        by_reported_model=by_reported_model,
        held=held,
        approval_holds=list(approval_holds),
        cache_creation_tokens=sum(r.cache_creation_tokens for r in records),
        cache_read_tokens=sum(r.cache_read_tokens for r in records),
        by_interactive_source=by_interactive_source,
        interactive_total_usd=interactive_total_usd,
        cache=cache,
        planned_spend=planned_spend,
        paired_spend=paired_spend,
        planned_run_count=planned_run_count,
        probe_spend=probe_spend,
    )


_FOOTER = (
    "*Cost per merged PR = total spend in window ÷ distinct PRs from window"
    " runs that are merged at report time. Spend on failed and killed runs"
    " is included — that is the honest unit economics.*"
)


def _render_group_table(label: str, groups: dict[str, GroupStats]) -> list[str]:
    if not groups:
        return ["No runs in window."]
    lines = [
        f"| {label} | Runs | Success rate | Avg cost |",
        "| --- | --- | --- | --- |",
    ]
    for name in sorted(groups):
        group = groups[name]
        lines.append(
            f"| {_escape_cell(name)} | {group.runs}"
            f" | {group.success_rate * 100:.1f}% | ${group.avg_cost:.2f} |"
        )
    return lines


def _planned_bound_lines(stats: ReportStats) -> list[str]:
    """The planned-bound half of the cost pair (#1092) — the third honest pair
    alongside list-vs-subscription. Total spend is the *recorded actual*
    (runner-native when reported); this names what dispatch planned against and
    how far off the bound ran. Omitted entirely when no run in the window
    recorded a bound, rather than printing a fabricated $0.00."""
    if stats.planned_run_count == 0 or stats.planned_spend <= 0:
        return []
    ratio = stats.paired_spend / stats.planned_spend
    return [
        f"- **Planned bound:** ${stats.planned_spend:.2f} across"
        f" {stats.planned_run_count} run(s) vs ${stats.paired_spend:.2f} actual"
        f" ({ratio:.2f}x) — planned = tokens × pricing bound,"
        " actual = runner-native spend where reported"
    ]


def _render_totals(stats: ReportStats, budget: BudgetConfig, days: int) -> list[str]:
    """Render the Totals section body (spend, runs, tokens, budget caps)."""
    lines = [f"- **Total spend:** ${stats.total_spend:.2f}"]
    lines += _planned_bound_lines(stats)
    if stats.review_spend > 0:
        lines.append(
            f"- **Governed-review spend:** ${stats.review_spend:.2f}"
            " (adversarial review + remediation, separate pool)"
        )
    if stats.probe_spend > 0:
        lines.append(
            f"- **Readiness-probe spend:** ${stats.probe_spend:.2f}"
            " (bounded capability checks — inside total spend, outside every"
            " run, outcome and model cohort below)"
        )
    runs_line = (
        f"- **Runs:** {stats.run_count} (success {stats.success_count}"
        f" / failure {stats.failure_count} / killed {stats.killed_count}"
        f" / budget_exceeded {stats.budget_exceeded_count}"
        f" / budget_stopped {stats.budget_stopped_count}"
    )
    if stats.pending_count:
        runs_line += f" / pending {stats.pending_count}"
    lines.append(runs_line + ")")
    lines.append(
        f"- **Tokens:** {stats.total_tokens:,}"
        f" ({stats.input_tokens:,} in / {stats.output_tokens:,} out)"
    )
    if budget.daily_usd > 0:
        avg_daily = stats.total_spend / days
        pct = avg_daily / budget.daily_usd * 100
        lines.append(
            f"- **Daily cap:** avg daily spend ${avg_daily:.2f}"
            f" of ${budget.daily_usd:.2f} ({pct:.1f}%)"
        )
    if budget.weekly_usd > 0:
        prorated = budget.weekly_usd * days / 7
        pct = stats.total_spend / prorated * 100
        lines.append(
            f"- **Weekly cap:** window spend ${stats.total_spend:.2f}"
            f" of ${prorated:.2f} pro-rated ({pct:.1f}%)"
        )
    return lines


def _render_outcomes(stats: ReportStats, pr_states: dict[str, str] | None) -> list[str]:
    """Render the Outcomes section body; degraded when GitHub was unreachable."""
    if pr_states is None:
        return ["unavailable (GitHub unreachable)"]
    cost_per_pr = (
        "n/a"
        if stats.cost_per_merged_pr is None
        else f"${stats.cost_per_merged_pr:.2f}"
    )
    median = "n/a" if stats.median_cost is None else f"${stats.median_cost:.2f}"
    return [
        f"- **PRs opened:** {stats.prs_opened}",
        f"- **PRs merged:** {stats.prs_merged}",
        f"- **Cost per merged PR:** {cost_per_pr}",
        f"- **Median cost per run:** {median}",
    ]


def _render_top_issues(top_issues: list[IssueCost]) -> list[str]:
    """Render the most-expensive-issues table body."""
    if not top_issues:
        return ["No runs in window."]
    lines = [
        "| Issue | Title | Cost | Status | PR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for issue in top_issues:
        ref = item_ref_or_num(issue.item_id, issue.issue_num)
        lines.append(
            f"| {ref} | {_escape_cell(issue.title)}"
            f" | ${issue.cost_usd:.2f} | {issue.status}"
            f" | {issue.pr_url or '—'} |"
        )
    return lines


def _render_held(
    held: list[IssueCost], approval_holds: list[ApprovalHold]
) -> list[str]:
    """Render the Held / blocked section body (budget-exceeded + approval holds)."""
    if not held and not approval_holds:
        return ["None."]
    lines = [
        f"- {item_ref_or_num(issue.item_id, issue.issue_num)} {issue.title}"
        f" — budget exceeded (${issue.cost_usd:.2f})"
        for issue in held
    ]
    for h in approval_holds:
        est = f"${h.estimate_usd:.2f}" if h.estimate_usd is not None else "$?"
        lines.append(f"- #{h.issue_num} — needs approval (~{est} estimated)")
    return lines


def _render_cache_section(cache: CacheStats) -> list[str]:
    """Render the Cache economics section as a markdown table + summary."""
    lines = [
        "| Runner / Model / Provider | Read tokens | Write tokens"
        " | Uncached | Hit ratio | Runs (cold) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(cache.by_group):
        g = cache.by_group[key]
        lines.append(
            f"| {key} | {g.cache_read_tokens:,} | {g.cache_creation_tokens:,}"
            f" | {g.uncached_input_tokens:,} | {g.hit_ratio * 100:.1f}%"
            f" | {g.runs} ({g.cold_runs}) |"
        )
    lines.append("")
    if cache.all_cold_usd > 0:
        lines.append(
            f"Cache saved **${cache.saved_usd:.2f}** vs all-cold baseline"
            f" (${cache.all_cold_usd:.2f}) — **{cache.savings_pct:.1f}%** savings"
        )
    return lines


def _render_interactive_table(groups: dict[str, InteractiveGroupStats]) -> list[str]:
    """Render the Interactive (Claude Code) section as a markdown table."""
    lines = ["| Model | Tokens | Cost |", "| --- | --- | --- |"]
    for name in sorted(groups):
        g = groups[name]
        lines.append(
            f"| {_escape_cell(name)} | {g.total_tokens:,} | ${g.total_cost:.2f} |"
        )
    return lines


def _switchyard_records(records: Sequence[RunRecord]) -> list[RunRecord]:
    """Return completed adaptive Switchyard rows with route/evidence intact."""
    return [
        record
        for record in records
        if (
            record.status != "pending"
            and record.transport_mode == "switchyard"
            and record.transport_route != "switchyard/passthrough"
        )
    ]


def _classifier_overhead_cell(record: RunRecord) -> str:
    """Render classifier overhead, or an em dash when none was evidenced.

    An unidentified classifier still shows its tokens and cost, so overhead
    that was measured never reads as overhead that did not happen.
    """
    if not (
        record.classifier_overhead_model
        or record.classifier_overhead_input_tokens
        or record.classifier_overhead_output_tokens
        or record.classifier_overhead_cost_usd
    ):
        return "—"
    return (
        f"{record.classifier_overhead_model or 'unknown'}: "
        f"{record.classifier_overhead_input_tokens:,} in / "
        f"{record.classifier_overhead_output_tokens:,} out "
        f"(${record.classifier_overhead_cost_usd:.4f})"
    )


def _switchyard_row(record: RunRecord) -> str:
    """Render one adaptive-Switchyard accounting row."""
    return (
        "| "
        + " | ".join(
            (
                _escape_cell(record.transport_route or record.model or "—"),
                _escape_cell(record.reported_model or "—"),
                _escape_cell(record.resolved_model_source or "—"),
                _escape_cell(record.selected_tier or "—"),
                _escape_cell(record.requested_service_tier or "—"),
                _escape_cell(record.reported_service_tier or "—"),
                _escape_cell(_classifier_overhead_cell(record)),
                f"${record.estimated_cost_usd:.4f}",
                _escape_cell((record.accounting_status or "unreconciled").upper()),
            )
        )
        + "|"
    )


def _render_switchyard_accounting(records: Sequence[RunRecord]) -> list[str]:
    """Render route, served identity, tiers, and classifier overhead separately."""
    rows = _switchyard_records(records)
    if not rows:
        return []
    return [
        "| Admitted route | Served model | Identity source | Switchyard tier "
        "| Requested service tier | Reported service tier | Classifier overhead "
        "| Governed cost | Accounting status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        *(_switchyard_row(record) for record in rows),
    ]


def _render_switchyard_section(records: Sequence[RunRecord]) -> list[str]:
    """Return the Switchyard accounting section, or nothing when it is empty."""
    rows = _render_switchyard_accounting(records)
    if not rows:
        return []
    adaptive = _switchyard_records(records)
    warning = (
        [
            "",
            "> **UNRECONCILED:** one or more adaptive runs lack complete "
            "classifier or served-model evidence; do not treat $0 as free usage.",
        ]
        if any(record.accounting_status != "reconciled" for record in adaptive)
        else []
    )
    return ["", "## Switchyard accounting", *warning, "", *rows]


def render_report(
    records: Sequence[RunRecord],
    *,
    repo: str,
    days: int,
    now: datetime,
    budget: BudgetConfig,
    pr_states: dict[str, str] | None,
    approval_holds: Sequence[ApprovalHold] = (),
    interactive: Sequence[InteractiveUsage] = (),
    pricing_overrides: dict[str, tuple[float, float]] | None = None,
) -> str:
    """Render the full markdown spend report. Pure — no I/O."""
    stats = compute_stats(
        records,
        pr_states,
        approval_holds,
        interactive,
        pricing_overrides=pricing_overrides,
    )
    since = now - timedelta(days=days)

    lines: list[str] = [
        f"# kew spend report — {repo}",
        "",
        f"- **Period:** {since:%Y-%m-%d} → {now:%Y-%m-%d} ({days} days)",
        f"- **Generated:** {now:%Y-%m-%d %H:%M} UTC",
        "",
        "## Totals",
        "",
    ]
    lines += _render_totals(stats, budget, days)
    lines += ["", "## Outcomes", ""]
    lines += _render_outcomes(stats, pr_states)
    lines += ["", "## Top 5 most expensive issues", ""]
    lines += _render_top_issues(stats.top_issues)
    lines += ["", "## By model", ""]
    lines += _render_group_table("Model", stats.by_model)
    if stats.by_reported_model:
        lines += ["", "## Reported model", ""]
        lines += _render_group_table("Reported model", stats.by_reported_model)
    lines += _render_switchyard_section(records)
    lines += ["", "## By runner", ""]
    lines += _render_group_table("Runner", stats.by_runner)
    if stats.cache is not None:
        lines += ["", "## Cache economics", ""]
        lines += _render_cache_section(stats.cache)
    if stats.by_interactive_source:
        lines += ["", "## Interactive (Claude Code)", ""]
        source_order = ["main", "subagent", "auxiliary"]
        sources = sorted(
            stats.by_interactive_source.keys(),
            key=lambda s: (
                source_order.index(s) if s in source_order else len(source_order)
            ),
        )
        for source in sources:
            models = stats.by_interactive_source[source]
            if len(stats.by_interactive_source) > 1 or source != "main":
                lines += [f"### {source.capitalize()}", ""]
            lines += _render_interactive_table(models)
            lines += [""]
        combined = stats.interactive_total_usd + stats.total_spend
        lines += [
            f"Interactive total: ${stats.interactive_total_usd:.2f} · "
            f"Dispatched total: ${stats.total_spend:.2f} · "
            f"Combined: ${combined:.2f}",
        ]
    if stats.cache is not None and stats.cache.saved_usd > 0:
        lines += [
            "",
            f"*Cache saved you ${stats.cache.saved_usd:.2f}"
            f" ({stats.cache.savings_pct:.1f}% of input cost)*",
        ]
    lines += ["", "## Held / blocked", ""]
    lines += _render_held(stats.held, stats.approval_holds)
    lines += ["", "---", "", _FOOTER, ""]
    return "\n".join(lines)


_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/\d+$")


def _repo_from_pr_urls(pr_urls: Sequence[str]) -> str:
    """Best-effort owner/name extraction from a PR URL. No gh calls."""
    for url in pr_urls:
        match = _PR_URL_RE.match(url)
        if match:
            return match.group(1)
    return ""


@dataclass(frozen=True)
class ReportResult:
    """Rendered output and the availability of its outcome queries."""

    content: str
    sync: SyncResult = field(default_factory=SyncResult)


def outcome_sync_warning(query_failures: int) -> str:
    """Return a portable availability warning for report recipients."""
    if query_failures <= 0:
        return ""
    return (
        f"Outcome data is stale: {query_failures} GitHub queries failed; "
        "PR states may be out of date"
    )


def _with_outcome_warning(content: str, sync: SyncResult) -> str:
    warning = outcome_sync_warning(sync.query_failures)
    return f"# {warning}\n\n{content}" if warning else content


def build_report(
    *, config: DispatchConfig, days: int, now: datetime | None = None
) -> str:
    """Return markdown for library callers that only consume report content."""
    return build_report_result(config=config, days=days, now=now).content


def build_report_result(
    *,
    config: DispatchConfig,
    days: int,
    now: datetime | None = None,
) -> ReportResult:
    """Load window runs, read persisted PR outcomes, and render the report.

    Outcomes are synced from GitHub into the store before reading, so the
    report reflects the latest available state. A missing dispatch.db is an
    empty store: the report renders with zero activity rather than erroring
    (spec edge case).
    """
    if now is None:
        now = datetime.now(UTC)
    since = now - timedelta(days=days)

    window = _load_window(config, since)
    records = window.records
    pr_urls = _distinct_pr_urls(records)
    # pr_states is always a dict (never None): persisted reads cannot produce None.
    # An empty dict means "no PRs with a stored state yet" → renders "PRs merged: 0".
    # None is reserved for live GitHub unreachability, which this path never triggers.
    # One rule for every reader (kew.pr_states): merged if any run says so.
    pr_states = pr_states_by_url(records)
    repo = config.project.repo or _repo_from_pr_urls(pr_urls) or "(unknown repo)"
    pricing_overrides = build_pricing_overrides(config.pricing.overrides)

    content = render_report(
        records,
        repo=repo,
        days=days,
        now=now,
        budget=config.budget,
        pr_states=pr_states,
        approval_holds=window.approval_holds,
        interactive=window.interactive,
        pricing_overrides=pricing_overrides,
    )
    return ReportResult(_with_outcome_warning(content, window.sync), window.sync)


def build_model_scorecard(
    *, config: DispatchConfig, days: int, now: datetime | None = None
) -> str:
    """Return markdown for library callers that only consume scorecard content."""
    return build_model_scorecard_result(config=config, days=days, now=now).content


def build_model_scorecard_result(
    *, config: DispatchConfig, days: int, now: datetime | None = None
) -> ReportResult:
    """Load window runs (+ synced outcomes) and render the model scorecard."""
    if now is None:
        now = datetime.now(UTC)
    since = now - timedelta(days=days)
    window = _load_window(config, since)
    repo = (
        config.project.repo
        or _repo_from_pr_urls(_distinct_pr_urls(window.records))
        or "(unknown repo)"
    )
    content = render_model_scorecard(
        compute_model_scorecard(window.records), repo=repo, days=days, now=now
    )
    return ReportResult(_with_outcome_warning(content, window.sync), window.sync)


def ingest_cap_alert(
    config: DispatchConfig, *, days: int, now: datetime | None = None
) -> NotificationEvent | None:
    """Build a cap-breach NotificationEvent, or None. Alert, not enforcement."""
    from kew.notifier import NotificationEvent

    cap = config.ingest.cap_usd
    if cap <= 0:
        return None
    if now is None:
        now = datetime.now(UTC)
    since = now - timedelta(days=days)
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        return None
    combined = config.ingest.combine_with_dispatch
    with _open_report_store(db_path) as store:
        spend = sum(r.cost_usd for r in store.list_interactive_usage_since(since))
        if combined:
            spend += sum(r.estimated_cost_usd for r in store.list_since(since))
    if spend < cap:
        return None
    # Label the spend honestly: it includes dispatched cost only when combined.
    label = (
        "Combined interactive + dispatched spend"
        if combined
        else "Interactive usage spend"
    )
    return NotificationEvent(
        event_type="ingest_cap",
        severity="warning",
        message=(
            f"{label} ${spend:.2f} reached the cap ${cap:.2f} "
            f"over {days}d. This is an alert — kew does not enforce interactive "
            f"limits."
        ),
    )
