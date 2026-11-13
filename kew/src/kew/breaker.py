"""Pure decision logic for the automatic circuit breaker (#245).

No I/O: the integration points (orchestrator watchdog, dispatch_core,
scheduler) read live cost / persisted spend and pass values in. ``run_baseline``
is the one function that touches the store, through the narrow
``median_cost_for_bucket`` read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from kew.run_store import RunStore

#: The ``error_category`` of a successful run the cost-anomaly rule flagged on
#: the line that ended it (#1798). Its cost is anomalous, so the bucket median
#: the next threshold is drawn from leaves it out.
FLAGGED_ANOMALY_CATEGORY = "cost_anomaly_flagged"


@dataclass(frozen=True)
class BreakerTrip:
    """A fired breaker rule, ready to log and audit."""

    # cost_anomaly | wall_clock | resource_cap | hard_kill | daily_breach |
    # weekly_breach
    rule: str
    scope: str  # run | fleet
    observed_usd: float | None
    threshold_usd: float | None
    detail: str


@dataclass(frozen=True)
class RunBaseline:
    """Per-(model, complexity, run_kind) cost baseline, computed once at dispatch."""

    median_usd: float | None
    sample_count: int
    anomaly_threshold_usd: float | None  # None => rule dormant
    factor: float = 0.0
    model: str = ""
    complexity: str = ""
    run_kind: str = "dispatch"
    #: Which term produced ``anomaly_threshold_usd`` — ``median_factor``,
    #: ``relative_floor``, ``abs_floor``, or ``dormant`` (#1116). Logged on the
    #: evaluation path so a drill can verify arming without tripping it.
    threshold_source: str = "dormant"


#: Own-kind samples an under-sampled bucket needs before the relative floor may
#: arm it (#1138 review). One observation is not a median — arming off it made
#: the threshold a coin flip on the first run's size, and a thin-but-cheap
#: first sample armed at ``3 x`` it, which is the #992 kill shape. Three keeps
#: the under-sampled window narrow (samples 3..``min_samples``-1 at the
#: defaults) and means the bar rests on a real central tendency.
_MIN_UNDER_SAMPLED_SAMPLES = 3


def _under_sampled_threshold(
    median_usd: float, abs_floor_usd: float, floor_multiple: float
) -> tuple[float | None, str]:
    """Threshold for a bucket too fresh to judge against ``median x factor``.

    Proportionate to the bucket's own median, with the absolute floor demoted
    to a backstop that may only *raise* it for near-zero medians.

    A non-positive relative floor means the bucket-relative term is switched
    off (``anomaly_floor_multiple = 0``, or a zero median). What is left is the
    flat absolute floor as the entire threshold on an under-sampled bucket —
    literally the mode that SIGTERM-killed the #992 remediation — so the rule
    stays dormant there instead of falling back to it (#1116 review).
    """
    relative_floor = median_usd * floor_multiple
    if relative_floor <= 0:
        return None, "dormant"
    if relative_floor >= abs_floor_usd:
        return relative_floor, "relative_floor"
    return abs_floor_usd, "abs_floor"


def anomaly_threshold(
    *,
    median_usd: float | None,
    sample_count: int,
    reference_cost_usd: float | None,
    factor: float,
    min_samples: int,
    abs_floor_usd: float,
    floor_multiple: float,
) -> tuple[float | None, str]:
    """Resolve ``(threshold, winning_term)`` for one bucket (#1116).

    A single absolute floor cannot serve buckets whose medians span three
    orders of magnitude: at $50 it is 69x a cheap OpenRouter route's median
    (decorative), and at $10 it was the *whole* threshold for a fresh bucket
    and killed a legitimately-large remediation (#992). So the floor is made
    bucket-relative, with the absolute value demoted to a noise backstop.

    An established bucket (``min_samples`` samples) is judged against its own
    median x ``factor``. An under-sampled one is judged against its own median
    x ``floor_multiple``, and only arms at all when it has at least
    ``_MIN_UNDER_SAMPLED_SAMPLES`` of its own samples *and* that median has
    cleared a positive ``reference_cost_usd``. Everything thinner than that
    stays dormant, as it was before #1116.

    ``reference_cost_usd`` is an admissibility *gate*, never a threshold input,
    and that is what keeps #992 from coming back. The reference is not evidence
    about this run_kind — it is what the route's *other* kinds cost — so
    letting it set the bar would arm a first-ever ``review_remediation`` on a
    cheap route at 3 x the route's ~$0.72 dispatch cost and kill a legitimate
    $2.50 remediation. Taking ``max(reference, median)`` does not fix that: the
    max is won by the reference, and so is inert, in exactly the case that
    hurts — a bucket whose thin samples are *cheaper* than the route. So a
    bucket not yet observed to cost at least what the rest of the route costs
    stays dormant; once it has cleared that bar, its own median is both the
    evidence and the threshold.

    The gate is only meaningful because the caller computes the reference over
    a population that *excludes* this run_kind. Read over a superset that still
    contained the bucket's own rows it would be free whenever the bucket is the
    route's only kind — median equals reference, ``<`` is false — arming
    buckets that were dormant before this rule existed (#1138 review).
    """
    established = median_usd is not None and sample_count >= min_samples
    if not established:
        if median_usd is None or reference_cost_usd is None:
            return None, "dormant"
        if sample_count < _MIN_UNDER_SAMPLED_SAMPLES:
            return None, "dormant"
        if reference_cost_usd <= 0 or median_usd < reference_cost_usd:
            return None, "dormant"
        return _under_sampled_threshold(median_usd, abs_floor_usd, floor_multiple)
    # Established bucket: median x factor is the primary rule; the floors only
    # raise it. Ties go to the earliest term, keeping median_factor the default
    # explanation when a floor multiple equals the anomaly factor.
    med = median_usd or 0.0
    terms = (
        (med * factor, "median_factor"),
        (med * floor_multiple, "relative_floor"),
        (abs_floor_usd, "abs_floor"),
    )
    return max(terms, key=lambda term: term[0])


def run_baseline(
    store: RunStore,
    model: str,
    complexity: str,
    run_kind: str = "dispatch",
    *,
    factor: float,
    min_samples: int,
    min_anomaly_threshold_usd: float = 0.0,
    floor_multiple: float = 0.0,
    reference_cost_usd: float | Callable[[], float | None] | None = None,
) -> RunBaseline:
    """Build the cost-anomaly baseline for a (model, complexity, run_kind) bucket.

    Keying on ``run_kind`` (I1/#445) buckets review / dispatch / loop / planning
    / delegation runs separately, so a heavyweight review run (~$5.37) is judged
    against review-kind medians instead of cheap first-dispatch ones that
    false-killed it.

    An established bucket (``min_samples`` normally-completed runs) is judged
    against ``median * factor``, raised by whichever floor is higher. An
    under-sampled one is judged against ``median * floor_multiple``, and arms
    only once it has at least ``_MIN_UNDER_SAMPLED_SAMPLES`` samples whose
    median has reached the caller's ``reference_cost_usd`` — which gates arming
    but never sets the threshold, and which the caller must compute over the
    route's *other* run_kinds for that gate to mean anything. A bucket with too
    few samples, or one still cheaper than the rest of its route, stays
    dormant. See ``anomaly_threshold``.

    ``reference_cost_usd`` may be a zero-arg callable, resolved only on the one
    path that can consume it — an under-sampled bucket already past
    ``_MIN_UNDER_SAMPLED_SAMPLES``. Every other path discards it, and it costs
    a second store read.
    """
    med, count = store.median_cost_for_bucket(model, complexity, run_kind)
    reference_cost = reference_cost_usd
    if callable(reference_cost):
        consumed = med is not None and _MIN_UNDER_SAMPLED_SAMPLES <= count < min_samples
        reference_cost = reference_cost() if consumed else None
    threshold, source = anomaly_threshold(
        median_usd=med,
        sample_count=count,
        reference_cost_usd=reference_cost,
        factor=factor,
        min_samples=min_samples,
        abs_floor_usd=min_anomaly_threshold_usd,
        floor_multiple=floor_multiple,
    )
    return RunBaseline(
        median_usd=med,
        sample_count=count,
        anomaly_threshold_usd=threshold,
        factor=factor,
        model=model,
        complexity=complexity,
        run_kind=run_kind,
        threshold_source=source,
    )


#: The most compactions a CONTAINED run's cost is discounted for. There the
#: count comes from the agent's own stream, so a forged marker would otherwise
#: buy back spend without limit and switch the anomaly rule off (#1462). A cap
#: on the count, not a share of the cost, keeps the per-compaction discount for
#: an ordinary run and bounds what any stream can claim at a fixed figure (three
#: allowances), where a share would let a forged run grow with its own spend.
#: The host rule passes no cap and discounts every compaction it counted.
MAX_DISCOUNTED_COMPACTIONS = 3


def compaction_adjusted_cost(
    live_cost_usd: float,
    compaction_count: int,
    compaction_allowance_usd: float,
    *,
    max_compactions: int | None = None,
) -> float:
    """Discount an autocompact allowance from a live run cost (#859).

    Each autocompaction rewrites the whole context, adding a cache-creation
    spike that inflates cumulative cost without reflecting genuine work. We
    subtract ``compaction_count * compaction_allowance_usd`` so the anomaly rule
    compares the run's *real work* cost — not its compaction overhead — against
    the (model, complexity, run_kind) baseline threshold. The allowance is
    keyed to the run-kind baseline by the caller, so an autocompact-inflated run
    is judged like-for-like against buckets that may or may not have compacted.

    Negative inputs are floored to zero (defensive: a bad token/count read must
    never *inflate* the cost), and the adjusted cost never drops below zero.
    *max_compactions*, when given, is the most compactions discounted: the
    contained governor passes :data:`MAX_DISCOUNTED_COMPACTIONS`.
    """
    counted = max(0, compaction_count)
    if max_compactions is not None:
        counted = min(counted, max(0, max_compactions))
    allowance = counted * max(0.0, compaction_allowance_usd)
    return max(0.0, live_cost_usd - allowance)


def baseline_payload(
    baseline: RunBaseline | None,
    *,
    tripped_threshold_usd: float | None = None,
    terminal_observed: bool = False,
) -> dict[str, object]:
    """The baseline facts a ``circuit_breaker`` event records (#1798).

    The threshold, the median it was drawn from, how many runs that median
    covers and which term won, so the ledger says why a cost was anomalous,
    and whether the runner had reported its terminal event (a flagged finished
    run, not a kill). *tripped_threshold_usd* is the bar the trip was judged
    against and wins over the baseline's. Every key is present; a run with no
    computed baseline records nulls.
    """
    threshold = tripped_threshold_usd
    if threshold is None and baseline is not None:
        threshold = baseline.anomaly_threshold_usd
    median = None if baseline is None else baseline.median_usd
    return {
        "threshold_usd": None if threshold is None else round(threshold, 4),
        "median_usd": None if median is None else round(median, 4),
        "sample_count": None if baseline is None else baseline.sample_count,
        "threshold_source": None if baseline is None else baseline.threshold_source,
        "terminal_observed": terminal_observed,
    }


def baseline_basis(baseline: RunBaseline) -> str:
    """Human-readable evidence behind a baseline's threshold.

    Keyed on the winning term, not on the values: only ``median_factor`` was
    actually ``median x factor``. The relative floor used a different multiple
    and the absolute floor ignored the median entirely, so reporting either as
    "median x factor" would misstate the threshold (#1116).
    """
    median = baseline.median_usd
    if median is None:
        head = "no baseline"
    elif baseline.threshold_source == "median_factor":
        head = f"median ${median:.2f} x {baseline.factor:.1f}"
    else:
        term = baseline.threshold_source.replace("_", " ")
        head = f"{term} over median ${median:.2f}"
    return f"{head}, {baseline.sample_count} samples"


def evaluate_run_live(
    live_cost_usd: float,
    baseline: RunBaseline,
    *,
    compaction_count: int = 0,
    compaction_allowance_usd: float = 0.0,
) -> BreakerTrip | None:
    """Cost-anomaly check for one in-flight run. None when dormant or under.

    When ``compaction_count``/``compaction_allowance_usd`` are supplied, the
    live cost is compaction-adjusted first (see ``compaction_adjusted_cost``) so
    autocompact-inflated steps are not mistaken for a genuine cost anomaly.
    """
    threshold = baseline.anomaly_threshold_usd
    if threshold is None:
        return None
    adjusted = compaction_adjusted_cost(
        live_cost_usd, compaction_count, compaction_allowance_usd
    )
    if adjusted <= threshold:
        return None
    compaction_note = (
        f", {compaction_count} compaction(s) discounted"
        if compaction_count > 0 and compaction_allowance_usd > 0
        else ""
    )
    return BreakerTrip(
        rule="cost_anomaly",
        scope="run",
        observed_usd=adjusted,
        threshold_usd=threshold,
        detail=(
            f"run cost ${adjusted:.2f} exceeds ${threshold:.2f} "
            f"({baseline_basis(baseline)}, "
            f"{baseline.model}/{baseline.complexity}, "
            f"via {baseline.threshold_source}{compaction_note})"
        ),
    )


def evaluate_fleet(
    daily_spent_usd: float,
    daily_limit_usd: float,
    weekly_spent_usd: float,
    weekly_limit_usd: float,
) -> BreakerTrip | None:
    """Fleet check on persisted spend. Daily is evaluated before weekly.

    A non-positive limit means the limit is unset, so that rule stays dormant —
    the fleet stop only fires against an explicitly-configured ``[budget]`` limit.
    """
    if daily_limit_usd > 0 and daily_spent_usd > daily_limit_usd:
        return BreakerTrip(
            rule="daily_breach",
            scope="fleet",
            observed_usd=daily_spent_usd,
            threshold_usd=daily_limit_usd,
            detail=f"daily spend ${daily_spent_usd:.2f} exceeds ${daily_limit_usd:.2f}",
        )
    if weekly_limit_usd > 0 and weekly_spent_usd > weekly_limit_usd:
        return BreakerTrip(
            rule="weekly_breach",
            scope="fleet",
            observed_usd=weekly_spent_usd,
            threshold_usd=weekly_limit_usd,
            detail=(
                f"weekly spend ${weekly_spent_usd:.2f} exceeds ${weekly_limit_usd:.2f}"
            ),
        )
    return None
