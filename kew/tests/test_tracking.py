"""Tests for dispatch result tracking."""

from pathlib import Path

import pytest

from kew.tracking import DispatchResult


class TestDispatchResult:
    def test_default_values(self):
        result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=Path("/tmp/log.txt"),
        )
        assert result.duration_seconds == 0.0
        assert result.total_tokens == 0
        assert result.estimated_cost_usd == 0.0

    def test_accumulate_usage(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1000, output_tokens=500, tool_uses=3)
        assert result.input_tokens == 1000
        assert result.output_tokens == 500
        assert result.total_tokens == 1500
        assert result.tool_uses == 3

    def test_accumulate_cache_tokens(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=500,
            cache_read_tokens=200,
        )
        result.accumulate_usage(cache_read_tokens=300)
        assert result.cache_creation_tokens == 500
        assert result.cache_read_tokens == 500
        # total_tokens counts every bucket the model processed.
        assert result.total_tokens == 100 + 50 + 500 + 200 + 300

    def test_merge_usage_includes_cache_tokens(self):
        a = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        a.accumulate_usage(cache_creation_tokens=100, cache_read_tokens=200)
        b = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/y"))
        b.accumulate_usage(cache_creation_tokens=50, cache_read_tokens=25)
        a.merge_usage(b)
        assert a.cache_creation_tokens == 150
        assert a.cache_read_tokens == 225
        # total_tokens is recomputed across all four buckets after the merge.
        assert a.total_tokens == 375

    def test_compute_cost_prices_cache_tokens(self):
        from kew.pricing import estimate_cost

        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=2000,
            cache_read_tokens=8000,
        )
        result.compute_cost("claude-sonnet-4")
        expected = estimate_cost(
            "claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=2000,
            cache_read_tokens=8000,
        )
        assert result.estimated_cost_usd == pytest.approx(expected)
        assert expected > 0

    def test_compute_cost_end_to_end_for_opus_4_8_run(self):
        """An Opus-4.8 run meters at $5/$25 list end-to-end (H1).

        Exercises the full path a real run takes: usage accumulated off the
        stream, then priced through compute_cost -> estimate_cost with the
        runner-reported model ID, which must resolve to the Opus-4.8 catalog
        row, not the $15/$75 family base.
        """
        result = DispatchResult(exit_code=0, issue_num=792, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        result.compute_cost("claude-opus-4-8")
        # 1M input @ $5 + 1M output @ $25 = $30.00.
        assert result.estimated_cost_usd == pytest.approx(30.0)

    def test_compute_cost_undeclared_dated_opus_4_id_borrows_no_price(self):
        """An undeclared dated ID has no catalog row, so it prices at $0 (#2709).

        Prefix matching is gone: the dated ID neither rides its point release
        ($5/$25) nor falls back onto the $15/$75 family base. Pricing it takes
        an alias on its models.json row.
        """
        result = DispatchResult(exit_code=0, issue_num=792, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        with pytest.warns(UserWarning):
            result.compute_cost("claude-opus-4-8-20260115")
        assert result.estimated_cost_usd == 0.0

    def test_default_session_id_is_none(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        assert result.session_id is None

    def test_default_error_category_is_empty(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        assert result.error_category == ""

    def test_session_id_can_be_set(self):
        result = DispatchResult(
            exit_code=1,
            issue_num=1,
            log_file=Path("/tmp/x"),
            session_id="abc-123",
        )
        assert result.session_id == "abc-123"

    def test_error_category_can_be_set(self):
        result = DispatchResult(
            exit_code=1,
            issue_num=1,
            log_file=Path("/tmp/x"),
            error_category="transient",
        )
        assert result.error_category == "transient"

    def test_accumulate_usage_multiple(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=100, output_tokens=50, tool_uses=1)
        result.accumulate_usage(input_tokens=200, output_tokens=100, tool_uses=2)
        assert result.total_tokens == 450
        assert result.tool_uses == 3

    def test_merge_usage(self):
        result1 = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result1.accumulate_usage(input_tokens=1000, output_tokens=500, tool_uses=2)

        result2 = DispatchResult(exit_code=1, issue_num=1, log_file=Path("/tmp/y"))
        result2.accumulate_usage(input_tokens=500, output_tokens=250, tool_uses=1)

        result1.merge_usage(result2)
        assert result1.input_tokens == 1500
        assert result1.output_tokens == 750
        assert result1.total_tokens == 2250
        assert result1.tool_uses == 3

    def test_merge_usage_with_zero_values(self):
        result1 = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result1.accumulate_usage(input_tokens=100, output_tokens=50, tool_uses=1)

        result2 = DispatchResult(exit_code=1, issue_num=1, log_file=Path("/tmp/y"))

        result1.merge_usage(result2)
        assert result1.input_tokens == 100
        assert result1.output_tokens == 50
        assert result1.total_tokens == 150
        assert result1.tool_uses == 1

    def test_breaker_rule_defaults_empty(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        assert result.breaker_rule == ""

    def test_resolved_model_defaults_empty_and_survives_merge(self):
        r = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/x"))
        assert r.resolved_model == ""
        r.resolved_model = "anthropic/claude-sonnet-4-6"
        other = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/x"))
        other.input_tokens = 5
        r.merge_usage(other)  # merges token fields only
        assert r.resolved_model == "anthropic/claude-sonnet-4-6"
        assert r.input_tokens == 5


class TestCostEstimation:
    def test_compute_cost_populates_field(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        result.compute_cost(model="claude-sonnet-4-6")
        assert result.estimated_cost_usd == pytest.approx(18.0)  # $3 + $15

    def test_compute_cost_unknown_model(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        result.compute_cost(model="unknown")
        assert result.estimated_cost_usd == 0.0

    def test_compute_cost_with_overrides(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=0)
        result.compute_cost(model="custom", overrides={"custom": (99.0, 99.0)})
        assert result.estimated_cost_usd == pytest.approx(99.0)

    def test_compute_cost_none_model(self):
        result = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        result.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        result.compute_cost(model=None)
        assert result.estimated_cost_usd == 0.0


class TestReportedCost:
    """Tests for reported_cost_usd (runner-authoritative cost)."""

    def test_reported_cost_defaults_none(self):
        r = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        assert r.reported_cost_usd is None

    def test_compute_cost_prefers_reported_when_present(self):
        r = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        r.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        r.reported_cost_usd = 7.13
        r.compute_cost(model="claude-sonnet-4-6")
        assert r.estimated_cost_usd == pytest.approx(7.13)

    def test_compute_cost_falls_back_to_tokens_when_no_reported(self):
        r = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        r.accumulate_usage(input_tokens=1_000_000, output_tokens=1_000_000)
        r.compute_cost(model="claude-sonnet-4-6")
        assert r.estimated_cost_usd == pytest.approx(18.0)

    def test_merge_usage_sums_reported_costs(self):
        a = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        a.reported_cost_usd = 3.50
        b = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/y"))
        b.reported_cost_usd = 2.25
        a.merge_usage(b)
        assert a.reported_cost_usd == pytest.approx(5.75)

    def test_merge_usage_takes_other_when_self_none(self):
        a = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        b = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/y"))
        b.reported_cost_usd = 4.00
        a.merge_usage(b)
        assert a.reported_cost_usd == pytest.approx(4.00)

    def test_merge_usage_keeps_self_when_other_none(self):
        a = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        a.reported_cost_usd = 3.50
        b = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/y"))
        a.merge_usage(b)
        assert a.reported_cost_usd == pytest.approx(3.50)

    def test_merge_usage_both_none_stays_none(self):
        a = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/x"))
        b = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/tmp/y"))
        a.merge_usage(b)
        assert a.reported_cost_usd is None


def test_round_four_usage_merge_preserves_prior_execution(tmp_path):
    from kew.tracking import DispatchResult

    refused = DispatchResult(1, 42, tmp_path / "run.log")
    prior = DispatchResult(137, 42, tmp_path / "run.log")
    prior.launched = True
    refused.merge_usage(prior)
    assert getattr(refused, "launched", False) is True


def test_round_ten_zero_exit_refusal_is_failure_with_reason(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from kew.dispatch_core import _apply_completion_status, _emit_completion_audit
    from kew.tracking import DispatchResult

    diagnostic = "ERROR: git preparation failed before agent startup"
    result = DispatchResult(
        0,
        42,
        tmp_path / "run.log",
        error_category="sandbox_startup_refused",
        stop_reason=diagnostic,
    )
    row = SimpleNamespace(status="pending", error_category="")
    _apply_completion_status(row, result, 0)
    assert row.status == "failure"
    run = MagicMock(dispatch_start=0.0)
    run.transport.resolved.endpoint = None
    run.transport.resolved.route = None
    with patch("kew.dispatch_core.audit.emit") as emit:
        _emit_completion_audit(run, result, 0, 1, None)
    assert emit.call_args.kwargs["payload"]["reason"] == diagnostic


def _startup_retry_sequence(
    tmp_path, prior=True, *, terminal_refused=True, with_prior=True
):
    """Run real retry merging and completion recording around synthetic streams."""
    from contextlib import nullcontext
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import patch

    from test_dispatch_core import _profile_run

    from kew import dispatch_core
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.sandbox import capture_container_launch

    reason = "ERROR: git preparation failed before agent startup"
    first = DispatchResult(137, 42, tmp_path / "run.log", launched=prior)
    last = DispatchResult(0, 42, tmp_path / "run.log")
    capture_container_launch(
        SimpleNamespace(
            observe_workload_start=lambda: not terminal_refused,
            startup_refusal_reason=reason if terminal_refused else "",
        ),
        last,
    )
    attempts = [first, last] if with_prior else [last]
    pending = iter(attempts)

    def run_attempt(**kwargs):
        result = next(pending)
        if result is first:
            (tmp_path / "unfinished.txt").write_text("first attempt work")
        return result

    with SqliteRunStore(str(tmp_path / "retry.db")) as store:
        run = _profile_run(store, tmp_path)
        run.config.retry.max_attempts = len(attempts)
        run.config.retry.backoff_base_seconds = 0
        store.save(
            RunRecord(
                run_id=run.run_id,
                issue_num=42,
                status="pending",
                branch=run.branch,
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )
        with (
            patch.object(
                dispatch_core, "_check_existing_pr", return_value=(False, None)
            ),
            patch.object(
                dispatch_core,
                "_sidecar_attempt_context",
                side_effect=lambda *a: nullcontext(),
            ),
            patch.object(dispatch_core, "_admit_attempt_route_profile"),
            patch.object(dispatch_core, "_contained_execution_for", return_value=None),
            patch.object(
                dispatch_core, "managed_switchyard_context", return_value=nullcontext()
            ),
            patch.object(dispatch_core, "run_agent", side_effect=run_attempt) as agent,
            patch.object(dispatch_core, "_export_run_report_snapshot"),
        ):
            retry = dispatch_core._run_retry_loop(
                run,
                prompt="fix",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )
            assert retry.early_return is None
            result = dispatch_core._finalize_result(run, retry)
        assert agent.call_count == len(attempts)
        row = store.get(run.run_id)
        assert row.status == ("failure" if terminal_refused else "success")
        assert row.exit_code == 0
        assert row.error_category == result.error_category
        event = store.list_audit_events(event="dispatch_completed")[-1]
        assert event.payload["reason"] == (reason if terminal_refused else "")
    assert result.launched is (
        prior if with_prior and terminal_refused else not terminal_refused
    )
    return result


@pytest.mark.parametrize("prior", [True, None])
def test_round_twelve_tracking_terminal_refusal_retains_execution(tmp_path, prior):
    result = _startup_retry_sequence(tmp_path, prior)
    import kew.tracking as tracking

    derive = getattr(tracking, "command_outcome", lambda result: None)
    outcome = derive(result)
    assert getattr(outcome, "exit_code", None) == 1
    assert outcome.kind == "executed"
    assert outcome.reason == result.stop_reason
    assert result.exit_code == 0
