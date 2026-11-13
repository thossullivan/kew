"""The weekly outcome scorecard, composed for the web surface (C3/#2301).

Four bounded SQL reads (``kew.run_store_outcomes``) plus one local
``git log origin/<base>`` read (``kew.merged_main``, against the configured
base branch) folded into one row per UTC week, oldest first. Nothing here
touches GitHub, and nothing here reads a run row the queries did not already
aggregate.

The store is injected and duck-typed on the four outcome reads only; this
module never imports ``run_store``, so the import-linter "serve reads runs
only through service" contract holds.

Percentiles are linearly interpolated between the two closest ranks, which
makes ``p50`` the ordinary median (the mean of the middle two on an even
sample) and ``p90`` the standard inclusive-rank reading. Stated here because
the acceptance test hand-computes a week against it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.merged_main import DEFAULT_REF, MergedMain, read_merged_main, week_start
from kew.service.models import (
    OutcomeCostDTO,
    OutcomeQuantilesDTO,
    OutcomeReportDTO,
    OutcomeRoundsDTO,
    OutcomeWeekDTO,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kew.config import DispatchConfig

#: Default window. Eight weeks is two months of delivery — long enough for a
#: trend, short enough that every point is a week the operator remembers.
DEFAULT_WEEKS = 8

#: Largest window the endpoint serves.
MAX_WEEKS = 52

#: The sample size below which a week's distribution is not worth reading. Two
#: items can put a median anywhere; the console greys such a point rather than
#: dropping it, because "we shipped three things that week" is itself the
#: finding.
MIN_SAMPLE = 3


def quantile(values: list[float], q: float) -> float | None:
    """The *q*-quantile of *values* by linear interpolation, or None if empty.

    ``q`` is a fraction (0.5 for the median). Values need not be sorted.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _weeks_in_window(*, weeks: int, now: datetime) -> list[str]:
    """The Mondays of the window, oldest first, ending with *now*'s week."""
    current = datetime.fromisoformat(week_start(now)).replace(tzinfo=UTC)
    return [week_start(current - timedelta(weeks=n)) for n in range(weeks - 1, -1, -1)]


def _money(total: float, count: int) -> float | None:
    """A mean that refuses to divide by no merges."""
    return total / count if count else None


def _base_ref(config: DispatchConfig | None) -> str:
    """The remote ref the "merged on main" denominator is read from.

    ``branches.base`` resolved the way ``kew.loops.delegate`` resolves it for
    its merge-base read, so a clone whose base branch is ``master`` gets its
    own denominator instead of a chart that is permanently unavailable. With
    the base unset the default ref stands.
    """
    base = (config.branches.base if config else "") or ""
    return f"origin/{base}" if base else DEFAULT_REF


def outcome_report(
    store: Any,
    *,
    weeks: int = DEFAULT_WEEKS,
    now: datetime | None = None,
    repo_dir: Path | str | None = None,
    config: DispatchConfig | None = None,
    merged_main: MergedMain | None = None,
) -> OutcomeReportDTO:
    """One row per week of the window, oldest first.

    *config* supplies the base branch the denominator is read from; without it
    the read falls back to :data:`kew.merged_main.DEFAULT_REF`.

    *merged_main* is injectable so a test (and a future serve-sweep cache) can
    supply the git read instead of forking one; left None it is read from
    *repo_dir* through the TTL cache in :mod:`kew.merged_main`.
    """
    if weeks < 1 or weeks > MAX_WEEKS:
        raise ValueError(f"outcome window must be between 1 and {MAX_WEEKS} weeks")
    now = now or datetime.now(UTC)
    window = _weeks_in_window(weeks=weeks, now=now)
    since = datetime.fromisoformat(window[0]).replace(tzinfo=UTC)

    if merged_main is None:
        merged_main = read_merged_main(repo_dir, since=since, ref=_base_ref(config))

    merged: defaultdict[str, list[Any]] = defaultdict(list)
    for pr in store.merged_prs_by_week(since=since):
        merged[pr.week_start].append(pr)
    ready: defaultdict[str, list[Any]] = defaultdict(list)
    for item in store.ready_items_by_week(since=since):
        ready[item.week_start].append(item)
    spend = {row.week_start: row for row in store.spend_by_week(since=since)}
    done = store.nodes_done_by_week(since=since)

    this_week = window[-1]
    rows = [
        _week_row(
            week,
            merged=merged.get(week, []),
            ready=ready.get(week, []),
            spend=spend.get(week),
            nodes_done=done.get(week, 0),
            merged_main=merged_main,
            partial=week == this_week,
        )
        for week in window
    ]
    return OutcomeReportDTO(
        weeks=rows,
        min_sample=MIN_SAMPLE,
        merged_main_head=merged_main.head,
        merged_main_unavailable=merged_main.unavailable,
    )


def _ready_hours(ready: list[Any]) -> OutcomeQuantilesDTO:
    """Dispatch -> READY in hours, as the three readings a reader acts on."""
    hours = [item.hours_to_ready for item in ready]
    return OutcomeQuantilesDTO(
        p50=quantile(hours, 0.5),
        p90=quantile(hours, 0.9),
        max=max(hours) if hours else None,
    )


def _ready_rounds(ready: list[Any]) -> OutcomeRoundsDTO:
    """Review rounds to READY: the middle and the worst case."""
    rounds = [float(item.rounds) for item in ready]
    return OutcomeRoundsDTO(
        p50=quantile(rounds, 0.5),
        max=int(max(rounds)) if rounds else None,
    )


def _cost_per_merged(merged: list[Any]) -> OutcomeCostDTO:
    """Attributed spend per merged PR — the mean and the typical PR."""
    costs = [pr.cost_usd for pr in merged]
    return OutcomeCostDTO(
        mean=_money(sum(costs), len(costs)),
        p50=quantile(costs, 0.5),
    )


def _merged_main_count(merged_main: MergedMain, week: str) -> int | None:
    """The git denominator for *week*, or None when the clone was unreadable.

    None and not 0: an unreadable clone is not "nothing merged", and a zero
    denominator would draw kew's share of the week as the whole of it.
    """
    if merged_main.unavailable:
        return None
    return merged_main.by_week.get(week, 0)


def _week_row(
    week: str,
    *,
    merged: list[Any],
    ready: list[Any],
    spend: Any,
    nodes_done: int,
    merged_main: MergedMain,
    partial: bool,
) -> OutcomeWeekDTO:
    """One week, from the four aggregates that cover it."""
    observed = [pr.observed_at for pr in merged if pr.observed_at]
    return OutcomeWeekDTO(
        week_start=week,
        merged_main=_merged_main_count(merged_main, week),
        merged_kew_touched=len(merged),
        merged_kew_authored=sum(1 for pr in merged if pr.authored),
        ready_items=len(ready),
        ready_hours=_ready_hours(ready),
        rounds_to_ready=_ready_rounds(ready),
        cost_per_merged=_cost_per_merged(merged),
        spend_total=spend.spend_total if spend else 0.0,
        spend_unattributed=spend.spend_unattributed if spend else 0.0,
        nodes_done=nodes_done,
        pr_sync_observed_at=max(observed) if observed else None,
        partial=partial,
    )
