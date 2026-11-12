"""Tests for the circuit-breaker decision module (#245)."""

import logging
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kew import dispatch_core
from kew.breaker import (
    RunBaseline,
    compaction_adjusted_cost,
    evaluate_fleet,
    evaluate_run_live,
    run_baseline,
)
from kew.config import BreakerConfig, BudgetConfig
from kew.dispatch_core import _compute_breaker_baseline
from kew.run_store import RunRecord, SqliteRunStore


class _FakeStore:
    def __init__(self, result):
        self._result = result

    def median_cost_for_bucket(self, model, complexity, run_kind="dispatch"):
        return self._result


def test_run_baseline_dormant_below_min_samples():
    bl = run_baseline(_FakeStore((2.0, 3)), "m", "c", factor=3.0, min_samples=5)
    assert bl.sample_count == 3
    assert bl.anomaly_threshold_usd is None  # dormant


def test_run_baseline_active_threshold():
    bl = run_baseline(_FakeStore((2.0, 5)), "m", "c", factor=3.0, min_samples=5)
    assert bl.median_usd == 2.0
    assert bl.anomaly_threshold_usd == 6.0


def test_run_baseline_empty_bucket_dormant():
    bl = run_baseline(_FakeStore((None, 0)), "m", "c", factor=3.0, min_samples=5)
    assert bl.anomaly_threshold_usd is None


def test_evaluate_run_live_dormant_never_trips():
    bl = RunBaseline(median_usd=None, sample_count=0, anomaly_threshold_usd=None)
    assert evaluate_run_live(1000.0, bl) is None


def test_evaluate_run_live_under_threshold_no_trip():
    bl = RunBaseline(median_usd=2.0, sample_count=5, anomaly_threshold_usd=6.0)
    assert evaluate_run_live(6.0, bl) is None  # boundary: not strictly greater


def test_evaluate_run_live_trips_over_threshold():
    bl = RunBaseline(median_usd=2.0, sample_count=5, anomaly_threshold_usd=6.0)
    trip = evaluate_run_live(7.5, bl)
    assert trip is not None
    assert trip.rule == "cost_anomaly"
    assert trip.scope == "run"
    assert trip.observed_usd == 7.5
    assert trip.threshold_usd == 6.0


# -- anomaly-vs-compaction (#859) --------------------------------------------


def test_compaction_adjusted_cost_no_compaction_is_identity():
    assert compaction_adjusted_cost(7.5, 0, 1.0) == 7.5


def test_compaction_adjusted_cost_subtracts_allowance_per_compaction():
    # 2 compactions * $1.00 allowance each = $2.00 discounted off $7.50.
    assert compaction_adjusted_cost(7.5, 2, 1.0) == 5.5


def test_compaction_adjusted_cost_never_negative():
    assert compaction_adjusted_cost(1.0, 5, 1.0) == 0.0


def test_compaction_adjusted_cost_discounts_every_compaction_by_default():
    # The host anomaly rule discounts every compaction it counted (#859).
    assert compaction_adjusted_cost(100.0, 50, 1.0) == 50.0


def test_compaction_adjusted_cost_discounts_at_most_the_given_compactions():
    # A contained stream can claim any number of compactions; only the first
    # few count, so claimed compactions cannot discount a $100 run away.
    assert compaction_adjusted_cost(100.0, 50, 1.0, max_compactions=3) == 97.0


def test_compaction_adjusted_cost_ignores_negative_inputs():
    assert compaction_adjusted_cost(7.5, -3, 1.0) == 7.5
    assert compaction_adjusted_cost(7.5, 3, -1.0) == 7.5


def test_evaluate_run_live_compaction_explains_apparent_anomaly():
    # Live cost $7.50 is over the $6.00 threshold, but a single autocompaction
    # (allowance $2.00) accounts for the overshoot — not a genuine anomaly.
    bl = RunBaseline(median_usd=2.0, sample_count=5, anomaly_threshold_usd=6.0)
    assert (
        evaluate_run_live(7.5, bl, compaction_count=1, compaction_allowance_usd=2.0)
        is None
    )


def test_evaluate_run_live_genuine_anomaly_survives_compaction_discount():
    # $20 is anomalous even after discounting one $2 compaction allowance.
    bl = RunBaseline(median_usd=2.0, sample_count=5, anomaly_threshold_usd=6.0)
    trip = evaluate_run_live(20.0, bl, compaction_count=1, compaction_allowance_usd=2.0)
    assert trip is not None
    assert trip.rule == "cost_anomaly"
    assert trip.observed_usd == 18.0  # compaction-adjusted


def test_evaluate_run_live_zero_allowance_leaves_check_unchanged():
    bl = RunBaseline(median_usd=2.0, sample_count=5, anomaly_threshold_usd=6.0)
    trip = evaluate_run_live(7.5, bl, compaction_count=3, compaction_allowance_usd=0.0)
    assert trip is not None
    assert trip.observed_usd == 7.5


def test_evaluate_fleet_no_breach():
    assert evaluate_fleet(10.0, 50.0, 100.0, 200.0) is None


def test_evaluate_fleet_daily_before_weekly():
    trip = evaluate_fleet(60.0, 50.0, 250.0, 200.0)
    assert trip is not None
    assert trip.rule == "daily_breach"  # daily checked first
    assert trip.scope == "fleet"


def test_evaluate_fleet_weekly_breach():
    trip = evaluate_fleet(10.0, 50.0, 250.0, 200.0)
    assert trip is not None
    assert trip.rule == "weekly_breach"
    assert trip.observed_usd == 250.0


def test_evaluate_fleet_zero_limit_is_dormant():
    # An unset (zero) limit means "not configured" — never trips, even on spend.
    assert evaluate_fleet(5.0, 0.0, 5.0, 0.0) is None
    # A zero daily limit must not mask a configured weekly breach.
    trip = evaluate_fleet(5.0, 0.0, 250.0, 200.0)
    assert trip is not None
    assert trip.rule == "weekly_breach"


def test_floor_wins_when_median_low():
    # median 1.0 x factor 3.0 = 3.0, but the floor is 15.0 — floor wins.
    bl = run_baseline(
        _FakeStore((1.0, 7)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=15.0,
    )
    assert bl.anomaly_threshold_usd == 15.0


def test_factor_wins_when_median_high():
    # median 6.0 x factor 3.0 = 18.0 > floor 15.0 — factor wins.
    bl = run_baseline(
        _FakeStore((6.0, 7)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=15.0,
    )
    assert bl.anomaly_threshold_usd == 18.0


def test_floor_stays_dormant_below_min_samples():
    # Even with a generous floor, an undersampled bucket never arms the rule.
    bl = run_baseline(
        _FakeStore((1.0, 3)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=15.0,
    )
    assert bl.anomaly_threshold_usd is None


def test_default_floor_is_no_op():
    # Default floor (0.0) leaves the factor-only behaviour unchanged.
    bl = run_baseline(_FakeStore((2.0, 5)), "opus", "medium", factor=3.0, min_samples=5)
    assert bl.anomaly_threshold_usd == 6.0


def _seed_run(store, run_id, *, model, complexity, run_kind, cost, status="success"):
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status=status,
            exit_code=0,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            model=model,
            complexity=complexity,
            run_kind=run_kind,
            estimated_cost_usd=cost,
        )
    )


def test_baseline_separates_run_kinds():
    """A ~$5.37 review run is judged against review-kind medians, not the cheap
    first-dispatch medians that used to false-kill it (#445)."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-6", "medium"
    # Cheap first-dispatch runs in the same (model, complexity) bucket.
    for i in range(6):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=0.50,
        )
    # Heavyweight review runs in the same (model, complexity) bucket.
    for i in range(6):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review",
            cost=5.37,
        )

    review_bl = run_baseline(
        store, model, complexity, "review", factor=3.0, min_samples=5
    )
    # Review baseline is built from the review-kind median (~$5.37), not the
    # $0.50 dispatch median — so a $5.37 review run does NOT trip.
    assert review_bl.median_usd == 5.37
    assert review_bl.run_kind == "review"
    assert evaluate_run_live(5.37, review_bl) is None

    # A first-dispatch run of the same bucket is still judged against the cheap
    # dispatch median — a $5.37 dispatch run WOULD trip.
    dispatch_bl = run_baseline(
        store, model, complexity, "dispatch", factor=3.0, min_samples=5
    )
    assert dispatch_bl.median_usd == 0.50
    trip = evaluate_run_live(5.37, dispatch_bl)
    assert trip is not None
    assert trip.rule == "cost_anomaly"


def test_kew_toml_doubles_breaker_factor_for_all_routes():
    """The owner-approved policy doubles established bucket headroom."""
    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "kew.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["breaker"]["cost_anomaly_factor"] == 6.0


def test_kew_toml_doubles_established_bucket_threshold():
    """The new factor doubles headroom without changing the winning term."""
    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "kew.toml").open("rb") as fh:
        cfg = BreakerConfig(**tomllib.load(fh)["breaker"])
    bl = run_baseline(
        _FakeStore((1.20, 6)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=cfg.cost_anomaly_factor,
        min_samples=cfg.min_samples,
        min_anomaly_threshold_usd=cfg.min_anomaly_threshold_usd,
        floor_multiple=cfg.anomaly_floor_multiple,
    )
    old_bl = run_baseline(
        _FakeStore((1.20, 6)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=cfg.min_samples,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
    )
    assert old_bl.anomaly_threshold_usd == pytest.approx(3.60)
    assert bl.anomaly_threshold_usd == pytest.approx(7.20)
    assert bl.anomaly_threshold_usd == 2 * old_bl.anomaly_threshold_usd
    assert bl.threshold_source == "median_factor"


def test_kew_toml_doubles_absolute_floor_without_replacing_relative_behavior():
    """The doubled backstop absorbs noise while relative terms govern routes."""
    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "kew.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    breaker = cfg["breaker"]
    assert breaker["min_anomaly_threshold_usd"] == 4.0
    assert breaker["anomaly_floor_multiple"] == 6.0
    assert breaker["cost_anomaly_factor"] == breaker["anomaly_floor_multiple"]


def test_kew_toml_doubles_anomaly_terms_and_preserves_runtime_and_fleet_limits():
    """Anomaly terms double; runtime, daily, and weekly spending limits stay fixed."""
    repo_root = Path(__file__).resolve().parents[2]
    with (repo_root / "kew.toml").open("rb") as fh:
        raw = tomllib.load(fh)
    breaker = BreakerConfig(**raw["breaker"])
    budget = BudgetConfig(**raw["budget"])

    old_established = run_baseline(
        _FakeStore((2.0, 5)),
        "model",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
    )
    new_established = run_baseline(
        _FakeStore((2.0, 5)),
        "model",
        "medium",
        factor=breaker.cost_anomaly_factor,
        min_samples=breaker.min_samples,
        min_anomaly_threshold_usd=breaker.min_anomaly_threshold_usd,
        floor_multiple=breaker.anomaly_floor_multiple,
    )
    old_under_sampled = run_baseline(
        _FakeStore((1.0, 3)),
        "model",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.5,
    )
    new_under_sampled = run_baseline(
        _FakeStore((1.0, 3)),
        "model",
        "medium",
        factor=breaker.cost_anomaly_factor,
        min_samples=breaker.min_samples,
        min_anomaly_threshold_usd=breaker.min_anomaly_threshold_usd,
        floor_multiple=breaker.anomaly_floor_multiple,
        reference_cost_usd=0.5,
    )

    old_absolute_floor = run_baseline(
        _FakeStore((0.1, 5)),
        "model",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
    )
    new_absolute_floor = run_baseline(
        _FakeStore((0.1, 5)),
        "model",
        "medium",
        factor=breaker.cost_anomaly_factor,
        min_samples=breaker.min_samples,
        min_anomaly_threshold_usd=breaker.min_anomaly_threshold_usd,
        floor_multiple=breaker.anomaly_floor_multiple,
    )

    assert old_established.anomaly_threshold_usd is not None
    assert new_established.anomaly_threshold_usd == (
        2 * old_established.anomaly_threshold_usd
    )
    assert new_established.threshold_source == "median_factor"
    assert old_under_sampled.anomaly_threshold_usd is not None
    assert new_under_sampled.anomaly_threshold_usd == (
        2 * old_under_sampled.anomaly_threshold_usd
    )
    assert new_under_sampled.threshold_source == "relative_floor"
    assert old_absolute_floor.anomaly_threshold_usd is not None
    assert new_absolute_floor.anomaly_threshold_usd == (
        2 * old_absolute_floor.anomaly_threshold_usd
    )
    assert new_absolute_floor.threshold_source == "abs_floor"
    assert breaker.min_anomaly_threshold_usd == 2 * 2.0
    assert raw["agent"]["max_budget_usd"] == 50.0
    assert budget.per_issue_usd == 75.0
    assert budget.daily_usd == 250.0
    assert budget.weekly_usd == 600.0


def test_trip_detail_names_bucket_and_context():
    bl = run_baseline(
        _FakeStore((5.69, 7)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
    )
    trip = evaluate_run_live(20.0, bl)
    assert trip is not None
    detail = trip.detail
    # Observed vs threshold.
    assert "$20.00" in detail
    assert "$17.07" in detail
    # Median, factor, sample count, and bucket (model/complexity).
    assert "$5.69" in detail
    assert "3.0" in detail
    assert "7 samples" in detail
    assert "opus/medium" in detail


# -- bucket-relative anomaly floor (#1116) -----------------------------------


def test_established_cheap_bucket_gets_a_proportionate_threshold():
    """Drill 1b (#1101): the OpenRouter `simple` route's dispatch bucket had a
    $0.7245 median, and the $50 absolute floor made the threshold 69x it. With
    the floor bucket-relative, median x factor wins and the threshold lands in
    the low single dollars."""
    bl = run_baseline(
        _FakeStore((0.7245, 5)),
        "opencode/deepseek-v4-flash",
        "simple",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
    )
    assert bl.anomaly_threshold_usd is not None
    assert 2.0 < bl.anomaly_threshold_usd < 3.0
    assert bl.threshold_source == "median_factor"


def test_established_expensive_bucket_is_unchanged_by_the_relative_floor():
    """An Opus bucket with a $5.69 median still trips on median x factor — the
    relative floor never loosens an established, well-sampled bucket."""
    bl = run_baseline(
        _FakeStore((5.69, 7)),
        "claude-opus-4-8",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
    )
    assert bl.anomaly_threshold_usd == 5.69 * 3.0
    assert bl.threshold_source == "median_factor"


def test_zero_sample_bucket_stays_dormant_even_with_a_reference_cost():
    """#1116 review (high): the route reference is run-kind-blind, so on a
    bucket with *no* samples the max(reference, median) guard is inert — there
    is no median to raise it. A first-ever `review_remediation` on the cheap
    OpenRouter route would arm at 3 x its $0.7245 dispatch median = $2.17 and
    SIGTERM-kill a legitimate $2.50 remediation: #992, exactly. Nothing has
    been observed about this run kind, so nothing arms."""
    bl = run_baseline(
        _FakeStore((None, 0)),
        "opencode/deepseek-v4-flash",
        "simple",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.7245,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"
    assert evaluate_run_live(2.50, bl) is None


def test_under_sampled_bucket_arms_against_its_own_median_not_the_reference():
    """Once the bucket has been observed to cost at least what the rest of its
    route costs, it arms against its own median x the floor multiple. Three
    $6.00 remediations seen on a $5.00 Opus route: threshold $18 — the
    legitimately-large next remediation lives, a genuine runaway still trips."""
    bl = run_baseline(
        _FakeStore((6.0, 3)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
        reference_cost_usd=5.0,
    )
    assert bl.anomaly_threshold_usd == 18.0
    assert bl.threshold_source == "relative_floor"
    assert evaluate_run_live(10.0, bl) is None
    assert evaluate_run_live(18.01, bl) is not None


def test_bucket_cheaper_than_its_route_stays_dormant_one_sample_in():
    """#1138 review (high): `max(reference, median)` was inert in exactly the
    case that hurts — a bucket whose samples are *cheaper* than the route,
    where the max is won by the reference. That re-created the
    $2.17-kills-a-$2.50-remediation shape a few completed runs after the state
    `test_zero_sample_bucket_stays_dormant_even_with_a_reference_cost` pins.
    The reference gates arming; it never sets the threshold."""
    bl = run_baseline(
        _FakeStore((0.50, 3)),  # three cheap remediations seen
        "opencode/deepseek-v4-flash",
        "simple",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.7245,  # run-kind-blind route median
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"
    assert evaluate_run_live(2.50, bl) is None


def test_a_single_sample_never_arms_an_under_sampled_bucket():
    """#1138 review (high): arming off one observation made the threshold a
    coin flip on the first run's size. A first $0.80 remediation on a $0.7245
    route clears the gate by 8 cents and would arm at $2.40 — SIGTERM-killing
    the same legitimate $2.50 remediation the zero-sample case is written to
    protect. One sample is not a median; the bucket stays dormant."""
    bl = run_baseline(
        _FakeStore((0.80, 1)),
        "opencode/deepseek-v4-flash",
        "simple",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.7245,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"
    assert evaluate_run_live(2.50, bl) is None


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, None), (2, None), (3, 18.0), (4, 18.0)],
)
def test_under_sampled_arming_needs_three_own_samples(count, expected):
    """The under-sampled window is samples 3..min_samples-1 (#1138 review): a
    thinner bucket has no central tendency to be proportionate to, so it falls
    back to the pre-#1116 behaviour of staying dormant."""
    bl = run_baseline(
        _FakeStore((6.0, count)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
        reference_cost_usd=5.0,
    )
    assert bl.anomaly_threshold_usd == expected


def test_empty_bucket_without_a_reference_cost_stays_dormant():
    """No median and no route-derived reference means nothing to be relative
    to — the absolute floor alone must never arm an unsampled bucket."""
    bl = run_baseline(
        _FakeStore((None, 0)),
        "claude-opus-4-8",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"


def test_absolute_floor_still_backstops_a_near_zero_reference():
    """A free/near-zero route would otherwise arm at cents; the absolute
    backstop absorbs that noise."""
    bl = run_baseline(
        _FakeStore((0.02, 3)),
        "free/model",
        "simple",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
        reference_cost_usd=0.02,
    )
    assert bl.anomaly_threshold_usd == 1.0
    assert bl.threshold_source == "abs_floor"


def test_relative_floor_can_win_over_median_factor():
    """When the configured floor multiple exceeds the anomaly factor, the
    relative floor is the winning term and is reported as such."""
    bl = run_baseline(
        _FakeStore((2.0, 9)),
        "opus",
        "medium",
        factor=2.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=5.0,
    )
    assert bl.anomaly_threshold_usd == 10.0
    assert bl.threshold_source == "relative_floor"


def test_floor_armed_trip_detail_does_not_claim_median_x_factor():
    """A floor-armed bucket was never judged against `median x factor` — the
    factor was never applied — so the trip detail must name the term that
    actually set the bar, not the factor."""
    bl = run_baseline(
        _FakeStore((5.0, 3)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=1.0,
        floor_multiple=3.0,
        reference_cost_usd=1.0,
    )
    trip = evaluate_run_live(20.0, bl)
    assert trip is not None
    assert "relative floor over median $5.00" in trip.detail
    assert "x 3.0" not in trip.detail
    assert "relative_floor" in trip.detail


def _breaker_run(store, model, complexity, run_kind):
    return SimpleNamespace(
        config=SimpleNamespace(
            breaker=BreakerConfig(min_anomaly_threshold_usd=2.0),
            budget=BudgetConfig(),
        ),
        store=store,
        effective_model=model,
        complexity=complexity,
        run_kind=run_kind,
    )


def test_dispatch_wiring_arms_an_under_sampled_bucket_that_cleared_its_route(
    caplog,
):
    """The production path feeds the floor multiple and a route-derived
    reference into the baseline (#1116). A remediation bucket that has already
    been seen to cost more than the route's other kinds arms proportionately to
    its own median — not at the bare absolute floor (which killed #992) and not
    left unguarded."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(6):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=5.0,
        )
    # Three remediations seen, dearer than the rest of the route: enough to
    # arm, nowhere near min_samples.
    for i in range(3):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=9.0,
        )
    run = _breaker_run(store, model, complexity, "review_remediation")

    with caplog.at_level(logging.INFO, logger="kew.dispatch_core"):
        threshold, allowance = _compute_breaker_baseline(run)

    # $9.00 bucket median x the 3.0 floor multiple, not the $2.00 backstop and
    # not the $5.00 other-kind reference.
    assert threshold == 27.0
    # The #859 compaction discount keys to the bucket median the threshold is
    # proportionate to: $9.00 x the 0.5 allowance factor.
    assert allowance == 4.5
    assert "via relative_floor" in caplog.text
    assert "$27.00" in caplog.text


def test_dispatch_wiring_leaves_a_bucket_cheaper_than_its_route_dormant(caplog):
    """#1138 review (high), end to end: cheap remediations on a dearer route are
    no evidence that 3x them clears a normal remediation, so the production path
    stays dormant instead of arming off the other-kind route median."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(6):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=5.0,
        )
    for i in range(3):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=1.0,
        )
    run = _breaker_run(store, model, complexity, "review_remediation")

    with caplog.at_level(logging.INFO, logger="kew.dispatch_core"):
        threshold, allowance = _compute_breaker_baseline(run)

    assert threshold is None  # would have been $15.00, killing a $20 remediation
    # The allowance is only read against an armed threshold, so it is inert
    # here; it stays keyed to the bucket median either way.
    assert allowance == 0.5
    assert "dormant" in caplog.text


def test_dispatch_wiring_leaves_a_never_run_bucket_dormant(caplog):
    """#1116 review (high), end to end: a run kind this route has never run has
    no evidence behind it, so the production path must leave the rule dormant
    rather than arm it off the (cheap, run-kind-blind) route history."""
    store = SqliteRunStore(":memory:")
    model, complexity = "opencode/deepseek-v4-flash", "simple"
    for i in range(6):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=0.7245,
        )
    run = _breaker_run(store, model, complexity, "review_remediation")

    with caplog.at_level(logging.INFO, logger="kew.dispatch_core"):
        threshold, allowance = _compute_breaker_baseline(run)

    assert threshold is None  # would have been $2.17, killing a $2.50 run
    assert allowance == 0.0
    assert "dormant" in caplog.text


def test_route_reference_excludes_the_rows_the_bucket_median_excludes():
    """#1116 review (medium): the reference must carry the baseline's own
    exclusions. A history polluted with sub-$0.01 no-op rows (#668) and killed
    rows would otherwise yield a reference of fractions of a cent, which any
    bucket clears — arming a thinly-sampled $1.00 remediation bucket at $3.00
    off rows that represent no real work."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(6):
        _seed_run(
            store,
            f"noop{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=0.004,
        )
    for i in range(2):
        _seed_run(
            store,
            f"killed{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=6.0,
            status="killed",
        )
    for i in range(2):
        _seed_run(
            store,
            f"real{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=6.0,
        )
    for i in range(3):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=1.0,
        )
    run = _breaker_run(store, model, complexity, "review_remediation")

    threshold, _allowance = _compute_breaker_baseline(run)

    # Reference = median of the two real $6.00 rows, which the $1.00 bucket has
    # not cleared, so the rule stays dormant.
    assert dispatch_core._breaker_reference_cost(run) == 6.0
    assert threshold is None


def test_under_sampled_bucket_never_arms_below_its_own_median():
    """#1116 review (critical): the reference measures the route's *other*
    kinds, so on an under-sampled bucket it can sit far below what that bucket
    has already been observed to cost. Arming from it alone would SIGTERM-kill
    runs at the bucket's own median — the exact #992 failure mode, on the
    buckets this change newly arms. The threshold is the bucket's own median."""
    bl = run_baseline(
        _FakeStore((12.0, 3)),  # three $12 remediations seen; min_samples is 5
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.75,  # cheap other-kind route median
    )
    assert bl.anomaly_threshold_usd == 36.0
    assert bl.threshold_source == "relative_floor"
    # A run at the bucket's own observed median survives.
    assert evaluate_run_live(12.0, bl) is None
    # A genuine runaway is still caught.
    assert evaluate_run_live(36.01, bl) is not None


def test_under_sampled_bucket_basis_names_the_floor_over_its_median():
    """An under-sampled bucket was armed by the relative floor, so its basis
    must not claim `median x factor` — the factor was never applied."""
    bl = run_baseline(
        _FakeStore((12.0, 3)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
        reference_cost_usd=0.75,
    )
    trip = evaluate_run_live(50.0, bl)
    assert trip is not None
    assert "relative floor over median $12.00" in trip.detail


def test_under_sampled_median_alone_never_arms_the_rule():
    """A known median does not by itself arm an under-sampled bucket: without a
    route reference there is nothing to gate arming on, and the absolute floor
    alone must never arm (it is what killed #992). A route that has run no
    other kind is exactly this case — there is no reference to be had."""
    bl = run_baseline(
        _FakeStore((12.0, 3)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=3.0,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"


def test_zero_floors_leave_a_reference_armed_bucket_dormant():
    """With both floors at zero the under-sampled path would compute a $0
    threshold and kill the run's first cent. It stays dormant instead."""
    bl = run_baseline(
        _FakeStore((2.0, 3)),
        "opus",
        "medium",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=0.0,
        floor_multiple=0.0,
        reference_cost_usd=1.0,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"


def test_zero_floor_multiple_does_not_arm_at_the_bare_absolute_floor():
    """#1138 review (medium): `anomaly_floor_multiple = 0` is documented as
    disabling the bucket-relative floor. It must not instead hand the
    under-sampled path over to `min_anomaly_threshold_usd` as the *whole*
    threshold — the flat-floor-on-a-fresh-bucket mode that SIGTERM-killed the
    #992 remediation, and which `test_under_sampled_median_alone_never_arms_the_rule`
    already forbids one branch over."""
    bl = run_baseline(
        _FakeStore((12.0, 3)),
        "claude-opus-4-8",
        "medium",
        "review_remediation",
        factor=3.0,
        min_samples=5,
        min_anomaly_threshold_usd=2.0,
        floor_multiple=0.0,
        reference_cost_usd=0.75,
    )
    assert bl.anomaly_threshold_usd is None
    assert bl.threshold_source == "dormant"
    # The $12 bucket's own runs would have been killed at the $2 backstop.
    assert evaluate_run_live(12.0, bl) is None


def test_reference_cost_is_only_resolved_where_it_can_be_consumed():
    """The reference costs a second store read, so it is passed as a thunk and
    forced only on the one path that can consume it: an under-sampled bucket
    already past the three-sample minimum. Established, never-run and
    too-thinly-sampled buckets all discard it."""
    calls = []

    def _reference():
        calls.append(1)
        return 1.0

    def _baseline(result):
        return run_baseline(
            _FakeStore(result),
            "opus",
            "medium",
            factor=3.0,
            min_samples=5,
            floor_multiple=3.0,
            reference_cost_usd=_reference,
        )

    established = _baseline((2.0, 9))
    assert established.anomaly_threshold_usd == 6.0
    assert calls == []

    never_run = _baseline((None, 0))
    assert never_run.anomaly_threshold_usd is None
    assert calls == []

    too_thin = _baseline((4.0, 2))
    assert too_thin.anomaly_threshold_usd is None
    assert calls == []

    under_sampled = _baseline((4.0, 3))
    assert under_sampled.anomaly_threshold_usd == 12.0
    assert calls == [1]


def test_dispatch_wiring_skips_the_route_reference_for_an_established_bucket(
    monkeypatch,
):
    """The production path must not pay for the reference read when the bucket
    can judge itself — the thunk is only forced on an under-sampled bucket."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(6):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=5.0,
        )
    calls = []
    monkeypatch.setattr(
        dispatch_core,
        "_breaker_reference_cost",
        lambda run: calls.append(1),
    )
    run = _breaker_run(store, model, complexity, "review_remediation")

    threshold, allowance = _compute_breaker_baseline(run)

    assert threshold == 15.0  # $5.00 median x the 3.0 factor
    assert allowance == 2.5
    assert calls == []


def test_route_reference_does_not_materialize_the_whole_run_history(monkeypatch):
    """#1116 review (low): the reference is on the dispatch hot path, so it
    must stay the one-column bucket aggregate rather than the `list_all`
    whole-`runs` scan the approval estimate does (~50x slower at 20k rows)."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(6):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=5.0,
        )
    for i in range(3):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=9.0,
        )

    def _no_scan():
        raise AssertionError("reference must not scan the whole run history")

    monkeypatch.setattr(store, "list_all", _no_scan)
    run = _breaker_run(store, model, complexity, "review_remediation")

    assert _compute_breaker_baseline(run)[0] == 27.0


def test_route_reference_excludes_the_buckets_own_run_kind():
    """#1138 review (high): read across every kind, the reference is a superset
    of the bucket, so `median >= reference` is not a comparison against
    independent evidence. The read must exclude the bucket's own kind."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(4):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=5.0,
        )
    for i in range(2):
        _seed_run(
            store,
            f"r{i}",
            model=model,
            complexity=complexity,
            run_kind="review_remediation",
            cost=9.0,
        )

    assert store.median_cost_for_bucket(model, complexity, "dispatch") == (5.0, 4)
    # Only the two remediation rows, never the four dispatch ones.
    assert store.median_cost_for_bucket(
        model, complexity, "dispatch", other_kinds=True
    ) == (9.0, 2)


def test_route_reference_is_absent_when_the_bucket_is_the_only_kind():
    """The complement of a route that has only ever run one kind is empty, so
    there is no reference and nothing gates arming on (#1138 review)."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(4):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=0.20,
        )
    run = _breaker_run(store, model, complexity, "dispatch")

    assert dispatch_core._breaker_reference_cost(run) is None


def test_sole_run_kind_on_a_route_does_not_arm_itself(caplog):
    """#1138 review (high), the reported repro: four completed $0.20 dispatch
    runs on a fresh route, shipped BreakerConfig defaults. Read across all
    kinds the reference *is* the bucket median, so `median < reference` is
    false and the gate passes for free — arming at $0.60 a bucket that was
    dormant before #1116. With the reference read over the other kinds only,
    a route running a single kind has no reference and stays dormant until it
    reaches min_samples."""
    store = SqliteRunStore(":memory:")
    model, complexity = "claude-opus-4-8", "medium"
    for i in range(4):
        _seed_run(
            store,
            f"d{i}",
            model=model,
            complexity=complexity,
            run_kind="dispatch",
            cost=0.20,
        )
    run = SimpleNamespace(
        config=SimpleNamespace(breaker=BreakerConfig(), budget=BudgetConfig()),
        store=store,
        effective_model=model,
        complexity=complexity,
        run_kind="dispatch",
    )

    with caplog.at_level(logging.INFO, logger="kew.dispatch_core"):
        threshold, _allowance = _compute_breaker_baseline(run)

    assert threshold is None  # would have been $0.60 off four cheap samples
    assert "dormant" in caplog.text
