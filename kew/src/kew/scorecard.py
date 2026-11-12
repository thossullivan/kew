"""Pure model-scorecard aggregation + rendering.

Store-free by contract: this module must NOT import kew.run_store (or anything
that does), so kew.service (and thus kew.serve) can import it without violating
the import-linter "serve reads runs only through service" contract. Records are
duck-typed (the same RunRecord attributes report.py reads); we alias the record
type to Any rather than importing RunRecord.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kew.pr_states import MERGED, pr_states_by_url

# A run record, duck-typed. Mirrors kew.run_store.RunRecord's attributes without
# importing it (which would pull SqliteRunStore into the import graph).
_Record = Any


@dataclass
class ComplexityRow:
    """One (model, complexity) bucket of scorecard metrics."""

    complexity: str
    runs: int = 0
    opened: int = 0
    merged: int = 0
    review_total: int = 0
    review_n: int = 0
    spend: float = 0.0
    # Planned/actual cost pair (#1092): summed token×bound planned cost and the
    # matching actual spend, over the ``planned_n`` runs that recorded a bound.
    # Rows predating the column carry NULL and are excluded from all three, so
    # the ratio stays apples-to-apples — never a $0 planned stand-in.
    planned_spend: float = 0.0
    paired_spend: float = 0.0
    planned_n: int = 0


@dataclass
class ModelRow:
    """One model's scorecard aggregate, with a complexity drill-down."""

    model: str
    runs: int = 0
    opened: int = 0
    merged: int = 0
    review_total: int = 0
    review_n: int = 0
    spend: float = 0.0
    planned_spend: float = 0.0
    paired_spend: float = 0.0
    planned_n: int = 0
    by_complexity: dict[str, ComplexityRow] = field(default_factory=dict)


@dataclass
class ModelScorecard:
    rows: list[ModelRow]


# Run kinds that carry a PR's URL without having opened that PR (#1131): the
# governed reviewer, remediation flows, CI fixes, and interactive review runs
# all attach to a PR someone else authored. The acceptance author (#2726) runs
# before the implementer and writes only the node's acceptance tests, so it is
# not the PR's author either. Duplicated as literals because this
# module is store-free by contract; tests/test_scorecard.py guards the drift.
_NON_AUTHORING_RUN_KINDS = frozenset(
    {
        "review",
        "adversarial_review",
        "review_remediation",
        "review_stage",
        "ci_fix",
        "resolve",
        "acceptance_author",
    }
)

# Run kinds that are not a model's work at all and never enter the scorecard
# (I05A): a readiness probe is one bounded capability check kew paid for, with
# no work item, no PR and no outcome. Counting it would let a fleet-wide
# $0.02 call move a model's run count, spend and $/merged-PR. Its money stays
# visible in the report's total and probe sub-bucket instead. Duplicated as a
# literal for the same store-free reason as above; the drift guard lives in
# tests/test_scorecard.py.
_EXCLUDED_RUN_KINDS = frozenset({"readiness_probe"})


def _representative_run(runs: list[_Record]) -> _Record:
    """The latest-started_at run among those carrying a shared pr_url.

    Sorts the started_at datetime (tz-aware), not list order. Used only as the
    fallback when no authoring run exists for the PR.
    """
    return max(runs, key=lambda r: r.started_at)


def _authoring_run(runs: list[_Record]) -> _Record:
    """The run that wrote the PR: the earliest non-review run (#1131), and the
    earliest one that succeeded when any did.

    Reviews start after the dispatch they review and carry the same pr_url, so
    latest-started_at attribution credited the outcome to the reviewing model.
    A failed attempt on the branch (a dispatch that died in two seconds) can
    carry the pr_url too; the run that succeeded is the one that wrote the PR,
    so it takes the credit. A PR whose authoring runs all failed (an "orphan
    failure" merged out-of-band, #611) is credited to the earliest of them.
    Falls back to the representative run when a PR has review runs only — an
    out-of-band PR reviewed by kew (#611) is still worth counting somewhere.
    ``run_kind`` and ``status`` are read defensively: records are duck-typed,
    and the store's run_kind column defaults to 'dispatch'.
    """
    authored = [
        r
        for r in runs
        if (getattr(r, "run_kind", None) or "dispatch") not in _NON_AUTHORING_RUN_KINDS
    ]
    if not authored:
        return _representative_run(runs)
    succeeded = [r for r in authored if getattr(r, "status", None) == "success"]
    return min(succeeded or authored, key=lambda r: r.started_at)


def _attribute_prs(records: Sequence[_Record]) -> list[_Record]:
    """One authoring run per distinct truthy pr_url.

    The authoring run decides only WHO gets the credit (model, complexity,
    review_cycles). Whether the PR merged is read across all of its runs by
    :func:`kew.pr_states.pr_states_by_url`, because the authoring run's own
    ``pr_state`` can be a snapshot the outcome sync no longer refreshes.
    """
    by_pr: dict[str, list[_Record]] = {}
    for r in records:
        if r.pr_url:
            by_pr.setdefault(r.pr_url, []).append(r)
    return [_authoring_run(group) for group in by_pr.values()]


def _add_pr(
    model_row: ModelRow, cx_row: ComplexityRow, rep: _Record, *, merged: bool
) -> None:
    """Fold one attributed PR's outcome into its author's model + complexity rows.

    ``review_cycles`` rides along with opened/merged deliberately: it counts the
    PR's GitHub review submissions (bots included), a fact about the authored PR
    rather than about whichever model kew ran as reviewer (#1131).
    """
    model_row.opened += 1
    cx_row.opened += 1
    if merged:
        model_row.merged += 1
        cx_row.merged += 1
    if rep.review_cycles is not None:
        model_row.review_total += rep.review_cycles
        model_row.review_n += 1
        cx_row.review_total += rep.review_cycles
        cx_row.review_n += 1


def _aggregate_rows(
    records: Sequence[_Record], pr_reps: Sequence[_Record]
) -> list[ModelRow]:
    rows: dict[str, ModelRow] = {}

    def buckets(model: str, complexity: str) -> tuple[ModelRow, ComplexityRow]:
        mr = rows.setdefault(model or "unknown", ModelRow(model=model or "unknown"))
        cx = complexity or "unknown"
        cr = mr.by_complexity.setdefault(cx, ComplexityRow(complexity=cx))
        return mr, cr

    for r in records:  # runs + spend: per-run, by each run's own model/complexity
        mr, cr = buckets(r.model, r.complexity)
        mr.runs += 1
        mr.spend += r.estimated_cost_usd
        cr.runs += 1
        cr.spend += r.estimated_cost_usd
        planned = getattr(r, "planned_cost_usd", None)
        if planned is not None:
            for row in (mr, cr):
                row.planned_spend += planned
                row.paired_spend += r.estimated_cost_usd
                row.planned_n += 1
    # PR outcomes: by the authoring run's model/complexity, never the reviewer's
    # (#1131). Spend stays where it was incurred, so a model that mostly reviews
    # carries that spend against the PRs it authored itself — see the footnote.
    states = pr_states_by_url(records)
    for rep in pr_reps:
        mr, cr = buckets(rep.model, rep.complexity)
        _add_pr(mr, cr, rep, merged=states.get(rep.pr_url) == MERGED)
    return sorted(rows.values(), key=lambda mr: mr.model)


def compute_model_scorecard(records: Sequence[_Record]) -> ModelScorecard:
    """Aggregate window runs into a per-model, per-complexity outcome scorecard.

    Operational kinds (:data:`_EXCLUDED_RUN_KINDS`) are dropped before anything
    is counted, so they can neither add rows nor move an existing model's runs,
    spend or merge rate.
    """
    scored = [
        r
        for r in records
        if (getattr(r, "run_kind", None) or "dispatch") not in _EXCLUDED_RUN_KINDS
    ]
    return ModelScorecard(rows=_aggregate_rows(scored, _attribute_prs(scored)))


_MIN_SCORECARD_SAMPLE = 3
_COMPLEXITY_ORDER = {"simple": 0, "medium": 1, "complex": 2}
_SCORECARD_FOOTNOTE = (
    "\\* low sample (<3 PRs) · PRs are credited to the model that authored them,"
    " not the one that reviewed them · Reviews = PR review submissions (incl."
    " bots) · $/merged PR = all of that model's run spend (failed, abandoned and"
    " review runs included) ÷ the PRs it authored that merged"
    " · Planned vs actual = recorded actual ÷ planned bound over the runs"
    " carrying both (1.00x = the token×pricing-bound estimate predicted spend"
    " exactly; below 1.00x = the bound over-records)"
)


def _merge_rate_cell(merged: int, opened: int) -> str:
    if opened == 0:
        return "—"
    star = "*" if opened < _MIN_SCORECARD_SAMPLE else ""
    return f"{round(100 * merged / opened)}%{star} ({merged}/{opened})"


def _reviews_cell(review_total: int, review_n: int) -> str:
    return f"{review_total / review_n:.1f}" if review_n else "—"


def _per_merged_cell(spend: float, merged: int) -> str:
    return f"${spend / merged:.2f}" if merged else "—"


def _planning_ratio_cell(row: _Record) -> str:
    """Planning accuracy: actual ÷ planned bound (#1092/#534). ``1.00x`` means
    the bound predicted spend exactly; ``0.10x`` means it over-recorded 10x.
    ``—`` when no run in the bucket recorded a bound — a missing bound is never
    read as $0 planned, which would render a meaningless infinite ratio."""
    if not row.planned_n or row.planned_spend <= 0:
        return "—"
    return f"{row.paired_spend / row.planned_spend:.2f}x"


def _escape_cell(text: str) -> str:
    """Escape pipes so issue titles cannot break markdown tables."""
    return text.replace("|", "\\|")


def _scorecard_line(label: str, row: _Record) -> str:
    """One table row. ``row`` is a ModelRow or ComplexityRow — duck-typed, they
    carry the same metric fields."""
    return (
        f"| {label} | {row.runs} | {_merge_rate_cell(row.merged, row.opened)}"
        f" | {_reviews_cell(row.review_total, row.review_n)}"
        f" | {_per_merged_cell(row.spend, row.merged)}"
        f" | {_planning_ratio_cell(row)} |"
    )


def render_model_scorecard(
    scorecard: ModelScorecard, *, repo: str, days: int, now: datetime
) -> str:
    """Render the per-model outcome scorecard as markdown. Pure — no I/O."""
    if not scorecard.rows:
        return (
            f"# Model scorecard — {repo}\n\n"
            f"No model activity in the last {days} days.\n"
        )
    since = now - timedelta(days=days)
    lines = [
        f"# Model scorecard — {repo}",
        "",
        f"- **Period:** {since:%Y-%m-%d} → {now:%Y-%m-%d} ({days} days)",
        f"- **Generated:** {now:%Y-%m-%d %H:%M} UTC",
        "",
        "| Model | Runs | Merge rate | Reviews | $/merged PR | Planned vs actual |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for mr in scorecard.rows:
        lines.append(_scorecard_line(_escape_cell(mr.model), mr))
        for cr in sorted(
            mr.by_complexity.values(),
            key=lambda c: _COMPLEXITY_ORDER.get(c.complexity, 99),
        ):
            lines.append(_scorecard_line(f"· {_escape_cell(cr.complexity)}", cr))
    lines += ["", _SCORECARD_FOOTNOTE]
    return "\n".join(lines) + "\n"
