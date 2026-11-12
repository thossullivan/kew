"""Regression coverage for adaptive Switchyard accounting evidence."""

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from kew.config import (
    DispatchConfig,
    InferenceTransportConfig,
    RetryConfig,
    SwitchyardConfig,
)
from kew.inference_transport import RunnerTransportCapability
from kew.run_store import RunRecord, SqliteRunStore
from kew.runners.codex import CodexRunner
from kew.switchyard_classifier import parse_classifier_evidence
from kew.switchyard_metrics import managed_metrics_delta, parse_switchyard_metrics
from kew.tracking import DispatchResult


def _adaptive_metrics():
    return parse_switchyard_metrics(
        "\n".join(
            (
                "switchyard_requests_total{"
                'route="switchyard/llm_classifier",status="ok"} 1',
                "switchyard_model_selections_total{"
                'model="gpt-5.6-sol",route="switchyard/llm_classifier"} 1',
                "switchyard_model_selections_total{"
                'model="gpt-5.6-luna",route="switchyard/llm_classifier"} 1',
                "switchyard_tier_selections_total{"
                'tier="strong",route="switchyard/llm_classifier"} 1',
                'switchyard_prompt_tokens_total{model="gpt-5.6-sol"} 120',
                'switchyard_completion_tokens_total{model="gpt-5.6-sol"} 45',
                'switchyard_prompt_tokens_total{model="gpt-5.6-luna"} 33',
                'switchyard_completion_tokens_total{model="gpt-5.6-luna"} 7',
            )
        )
        + "\n"
    )


def test_switchyard_metrics_keep_served_usage_separate_from_classifier_overhead():
    evidence = _adaptive_metrics().selection_evidence(
        route="switchyard/llm_classifier",
        served_model="gpt-5.6-sol",
    )

    assert evidence.selected_model == "gpt-5.6-sol"
    assert evidence.selected_tier == "strong"
    assert evidence.classifier_model == "gpt-5.6-luna"
    assert evidence.classifier_input_tokens == 33
    assert evidence.classifier_output_tokens == 7
    assert evidence.observation.to_json()["accounting_authority"] == (
        "runner_reported_usage"
    )
    assert evidence.to_json()["classifier_overhead"]["accounting_authority"] == (
        "switchyard_proxy_observation"
    )


def test_managed_weak_tier_reconciles_same_model_classifier_overhead():
    final = parse_switchyard_metrics(
        "\n".join(
            (
                "switchyard_model_selections_total{"
                'model="gpt-5.6-luna",route="switchyard/llm_classifier"} 1',
                "switchyard_tier_selections_total{"
                'tier="weak",route="switchyard/llm_classifier"} 1',
                'switchyard_prompt_tokens_total{model="gpt-5.6-luna"} 153',
                'switchyard_completion_tokens_total{model="gpt-5.6-luna"} 52',
            )
        )
        + "\n"
    )
    observation = managed_metrics_delta(parse_switchyard_metrics(""), final)

    evidence = observation.selection_evidence(
        route="switchyard/llm_classifier",
        runner_input_tokens=120,
        runner_output_tokens=45,
    )

    assert evidence.selected_model == "gpt-5.6-luna"
    assert evidence.selected_tier == "weak"
    assert evidence.classifier_model == "gpt-5.6-luna"
    assert evidence.classifier_input_tokens == 33
    assert evidence.classifier_output_tokens == 7


def test_switchyard_route_alias_is_reconciled_from_runner_usage(tmp_path):
    from kew.orchestrator import _reconcile_switchyard_evidence

    evidence = _adaptive_metrics().selection_evidence(
        route="switchyard/llm_classifier",
        served_model="switchyard/llm_classifier",
        runner_input_tokens=120,
        runner_output_tokens=45,
    )

    assert evidence.selected_model == "gpt-5.6-sol"
    assert evidence.selected_model_source == "switchyard_proxy_metrics"
    assert evidence.classifier_model == "gpt-5.6-luna"

    result = DispatchResult(
        exit_code=0,
        issue_num=1318,
        log_file=tmp_path / "dispatch.log",
        resolved_model="switchyard/llm_classifier",
    )
    _reconcile_switchyard_evidence(
        result,
        evidence,
        route="switchyard/llm_classifier",
    )
    assert result.resolved_model == "gpt-5.6-sol"


def test_switchyard_route_alias_is_cleared_without_trustworthy_identity(tmp_path):
    from kew.orchestrator import _reconcile_switchyard_evidence

    evidence = _adaptive_metrics().selection_evidence(
        route="switchyard/llm_classifier",
        served_model="switchyard/llm_classifier",
    )

    assert evidence.selected_model is None

    result = DispatchResult(
        exit_code=0,
        issue_num=1318,
        log_file=tmp_path / "dispatch.log",
        resolved_model="switchyard/llm_classifier",
        resolved_model_source="runner_reported",
    )
    _reconcile_switchyard_evidence(
        result,
        evidence,
        route="switchyard/llm_classifier",
    )
    assert result.resolved_model == ""
    # The source must clear with the served model, or persistence would attest
    # a runner-reported identity behind an empty served-model slot.
    assert result.resolved_model_source == ""


def test_unsanitizable_served_model_is_labelled_proxy_evidence():
    evidence = _adaptive_metrics().selection_evidence(
        route="switchyard/llm_classifier",
        served_model="\x00\x01",
        runner_input_tokens=120,
        runner_output_tokens=45,
    )

    assert evidence.selected_model == "gpt-5.6-sol"
    assert evidence.selected_model_source == "switchyard_proxy_metrics"


def test_proxy_token_counters_round_rather_than_truncate():
    observation = parse_switchyard_metrics(
        "\n".join(
            (
                'switchyard_prompt_tokens_total{model="gpt-5.6-sol"} 119.9999999',
                'switchyard_completion_tokens_total{model="gpt-5.6-sol"} 45.0000001',
                'switchyard_prompt_tokens_total{model="gpt-5.6-luna"} 33',
                'switchyard_completion_tokens_total{model="gpt-5.6-luna"} 7',
            )
        )
        + "\n"
    )

    evidence = observation.selection_evidence(
        route="switchyard/llm_classifier",
        runner_input_tokens=120,
        runner_output_tokens=45,
    )

    assert evidence.selected_model == "gpt-5.6-sol"


def test_adaptive_reconciliation_uses_proxy_identity_without_overwriting_route(
    tmp_path,
):
    from kew.orchestrator import _reconcile_switchyard_evidence

    result = DispatchResult(
        exit_code=0,
        issue_num=1318,
        log_file=tmp_path / "dispatch.log",
        input_tokens=120,
        output_tokens=45,
    )
    evidence = _adaptive_metrics().selection_evidence(
        route="switchyard/llm_classifier",
        served_model="gpt-5.6-sol",
    )

    _reconcile_switchyard_evidence(
        result,
        evidence,
        route="switchyard/llm_classifier",
    )

    assert result.resolved_model == "gpt-5.6-sol"
    assert result.resolved_model_source == "switchyard_proxy_metrics"
    assert result.selected_tier == "strong"
    assert result.classifier_overhead_input_tokens == 33
    assert result.classifier_overhead_output_tokens == 7
    assert result.switchyard_proxy_observation is not None


def test_partial_classifier_records_are_unreconciled(tmp_path):
    from kew.orchestrator import reconcile_managed_switchyard_lifecycle

    observation = parse_switchyard_metrics(
        "\n".join(
            (
                "switchyard_requests_total{"
                'route="switchyard/llm_classifier",status="ok"} 2',
                "switchyard_model_selections_total{"
                'model="gpt-5.6-sol",route="switchyard/llm_classifier"} 2',
                "switchyard_tier_selections_total{"
                'tier="strong",route="switchyard/llm_classifier"} 2',
                'switchyard_prompt_tokens_total{model="gpt-5.6-luna"} 33',
                'switchyard_completion_tokens_total{model="gpt-5.6-luna"} 7',
            )
        )
        + "\n"
    )
    supervisor = MagicMock()
    supervisor.accounting_evidence.return_value = observation.selection_evidence(
        route="switchyard/llm_classifier"
    )
    supervisor.classifier_evidence = (
        parse_classifier_evidence(
            {"confidence": 0.91, "selected_tier": "strong"},
            ingress_format="openai_responses",
            upstream_format="openai_chat",
        ),
    )
    result = DispatchResult(
        exit_code=0,
        issue_num=1339,
        log_file=tmp_path / "dispatch.log",
    )

    reconcile_managed_switchyard_lifecycle(
        supervisor,
        result,
        route="switchyard/llm_classifier",
    )

    assert result.accounting_status == "unreconciled"
    assert result.accounting_unreconciled_reasons == ["classifier_evidence_incomplete"]


def test_missing_classifier_overhead_is_unreconciled(tmp_path):
    from kew.orchestrator import reconcile_managed_switchyard_lifecycle

    observation = parse_switchyard_metrics(
        "\n".join(
            (
                "switchyard_requests_total{"
                'route="switchyard/llm_classifier",status="ok"} 1',
                "switchyard_model_selections_total{"
                'model="gpt-5.6-sol",route="switchyard/llm_classifier"} 1',
                "switchyard_tier_selections_total{"
                'tier="strong",route="switchyard/llm_classifier"} 1',
            )
        )
        + "\n"
    )
    supervisor = MagicMock()
    supervisor.accounting_evidence.return_value = observation.selection_evidence(
        route="switchyard/llm_classifier"
    )
    supervisor.classifier_evidence = (
        parse_classifier_evidence(
            {"confidence": 0.91, "selected_tier": "strong"},
            ingress_format="openai_responses",
            upstream_format="openai_chat",
        ),
    )
    result = DispatchResult(
        exit_code=0,
        issue_num=1339,
        log_file=tmp_path / "dispatch.log",
    )

    reconcile_managed_switchyard_lifecycle(
        supervisor,
        result,
        route="switchyard/llm_classifier",
    )

    assert result.accounting_status == "unreconciled"
    assert result.accounting_unreconciled_reasons == ["classifier_overhead_missing"]


def test_selected_model_prices_runner_usage_and_classifier_overhead_separately(
    tmp_path,
):
    result = DispatchResult(
        exit_code=0,
        issue_num=1318,
        log_file=tmp_path / "dispatch.log",
    )
    result.accumulate_usage(input_tokens=120, output_tokens=45)

    result.compute_cost(
        model="gpt-5.6-sol",
        classifier_model="gpt-5.6-luna",
        classifier_input_tokens=33,
        classifier_output_tokens=7,
    )

    assert result.estimated_cost_usd > 0.0
    assert result.classifier_overhead_cost_usd > 0.0
    assert result.estimated_cost_usd > result.classifier_overhead_cost_usd


def test_codex_captures_structured_model_and_service_tier_evidence():
    events = CodexRunner().parse_line(
        '{"type":"turn.completed","model":"gpt-5.6-sol",'
        '"service_tier":"fast","usage":{"input_tokens":12,'
        '"output_tokens":3}}'
    )

    assert events
    assert events[0].metadata["model"] == "gpt-5.6-sol"
    assert events[0].metadata["service_tier"] == "fast"


def test_codex_does_not_take_model_or_tier_from_agent_text():
    events = CodexRunner().parse_line(
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"model=gpt-5.6-sol service_tier=fast"}}'
    )

    assert events
    assert "model" not in events[0].metadata
    assert "service_tier" not in events[0].metadata


def test_codex_prefers_nested_provider_identity_over_route_echo():
    events = CodexRunner().parse_line(
        '{"type":"turn.completed","model":"switchyard/llm_classifier",'
        '"response":{"model":"gpt-5.6-sol","service_tier":"fast"},'
        '"usage":{"input_tokens":12,"output_tokens":3}}'
    )

    assert events
    assert events[0].metadata["model"] == "gpt-5.6-sol"
    assert events[0].metadata["service_tier"] == "fast"


def test_requested_and_reported_service_tiers_round_trip(tmp_path):
    now = datetime.now(UTC)
    record = RunRecord(
        run_id="tiered-run",
        issue_num=1318,
        branch="agent/issue-1318",
        status="success",
        exit_code=0,
        started_at=now,
        model="switchyard/llm_classifier",
        reported_model="gpt-5.6-sol",
        resolved_model_source="switchyard_proxy_metrics",
        selected_tier="strong",
        requested_service_tier="fast",
        reported_service_tier="fast",
        classifier_overhead_model="gpt-5.6-luna",
        classifier_overhead_input_tokens=33,
        classifier_overhead_output_tokens=7,
        classifier_overhead_cost_usd=0.0001,
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(record)
        retrieved = store.get("tiered-run")

    assert retrieved is not None
    assert retrieved.model == "switchyard/llm_classifier"
    assert retrieved.reported_model == "gpt-5.6-sol"
    assert retrieved.resolved_model_source == "switchyard_proxy_metrics"
    assert retrieved.selected_tier == "strong"
    assert retrieved.requested_service_tier == "fast"
    assert retrieved.reported_service_tier == "fast"
    assert retrieved.classifier_overhead_model == "gpt-5.6-luna"
    assert retrieved.classifier_overhead_input_tokens == 33
    assert retrieved.classifier_overhead_output_tokens == 7
    assert retrieved.classifier_overhead_cost_usd == pytest.approx(0.0001)


def test_requested_service_tier_is_taken_from_codex_config_on_dispatch_result():
    from kew.orchestrator import _requested_service_tier

    config = DispatchConfig()
    config.agent.codex.service_tier = "fast"

    assert _requested_service_tier(config, "codex") == "fast"
    assert _requested_service_tier(config, "claude-code") is None


def test_switchyard_accounting_is_visible_in_run_report():
    from kew.config import BudgetConfig
    from kew.report import render_report

    record = RunRecord(
        run_id="reportable-switchyard-run",
        issue_num=1318,
        branch="agent/issue-1318",
        status="success",
        exit_code=0,
        started_at=datetime.now(UTC),
        estimated_cost_usd=0.42,
        model="switchyard/llm_classifier",
        transport_mode="switchyard",
        transport_route="switchyard/llm_classifier",
        reported_model="gpt-5.6-sol",
        resolved_model_source="switchyard_proxy_metrics",
        selected_tier="strong",
        requested_service_tier="fast",
        reported_service_tier="fast",
        classifier_overhead_model="gpt-5.6-luna",
        classifier_overhead_input_tokens=33,
        classifier_overhead_output_tokens=7,
        classifier_overhead_cost_usd=0.01,
    )

    report = render_report(
        [record],
        repo="o/r",
        days=7,
        now=datetime.now(UTC),
        budget=BudgetConfig(),
        pr_states={},
    )

    assert "## Switchyard accounting" in report
    assert "switchyard/llm_classifier" in report
    assert "gpt-5.6-sol" in report
    assert "gpt-5.6-luna: 33 in / 7 out" in report
    assert "fast" in report


def test_passthrough_run_report_has_no_adaptive_accounting_section():
    from kew.config import BudgetConfig
    from kew.report import render_report

    record = RunRecord(
        run_id="reportable-passthrough-run",
        issue_num=1318,
        branch="agent/issue-1318",
        status="success",
        exit_code=0,
        started_at=datetime.now(UTC),
        estimated_cost_usd=0.42,
        model="switchyard/passthrough",
        transport_mode="switchyard",
        transport_route="switchyard/passthrough",
        reported_model="switchyard/passthrough",
    )

    report = render_report(
        [record],
        repo="o/r",
        days=7,
        now=datetime.now(UTC),
        budget=BudgetConfig(),
        pr_states={},
    )

    assert "## Switchyard accounting" not in report


def test_managed_adaptive_terminal_accounting_keeps_route_and_served_model(
    tmp_path,
):
    from kew.dispatch_core import run_tracked_agent

    binary = tmp_path / "switchyard-server"
    binary.write_text("binary")
    binary.chmod(0o755)
    route_config = tmp_path / "routes.toml"
    route_config.write_text(
        "schema_version = 1\n"
        "[llm_clients.local]\n"
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "local"\n'
        "[routes.classifier]\n"
        'id = "switchyard/llm_classifier"\n'
        'type = "llm_classifier"\n'
        'classifier_target = "strong"\n'
    )
    config = DispatchConfig(
        retry=RetryConfig(max_attempts=1),
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            allow_adaptive=True,
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route="switchyard/llm_classifier",
                expected_version="pinned",
                managed=True,
                config_path=route_config,
                binary=binary,
                binary_sha256=hashlib.sha256(b"binary").hexdigest(),
            ),
        ),
    )
    config.agent.codex.service_tier = "fast"
    runner = MagicMock()
    runner.name = "codex"
    runner.transport_capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )
    runner.supports_resume.return_value = False
    result = DispatchResult(
        exit_code=0,
        issue_num=1318,
        log_file=tmp_path / "dispatch.log",
        input_tokens=120,
        output_tokens=45,
        total_tokens=165,
        resolved_model="switchyard/llm_classifier",
        resolved_model_source="runner_reported",
        reported_service_tier="fast",
    )
    supervisor = MagicMock()
    supervisor.observed_version = "pinned"
    supervisor.config_sha256 = "b" * 64
    supervisor.accounting_evidence.return_value = (
        _adaptive_metrics().selection_evidence(
            route="switchyard/llm_classifier",
            served_model="switchyard/llm_classifier",
            runner_input_tokens=120,
            runner_output_tokens=45,
        )
    )
    drained_classifier_evidence = (
        parse_classifier_evidence(
            {"confidence": 0.91, "selected_tier": "strong"},
            ingress_format="openai_responses",
            upstream_format="openai_chat",
        ),
    )
    supervisor.classifier_evidence = ()
    managed_context = MagicMock()
    managed_context.__enter__.return_value = supervisor
    managed_context.__exit__.side_effect = lambda *_args: setattr(
        supervisor, "classifier_evidence", drained_classifier_evidence
    )

    with (
        patch(
            "kew.dispatch_core.managed_switchyard_context",
            return_value=managed_context,
        ),
        patch("kew.dispatch_core.check_pr_exists", return_value=None),
        patch("kew.dispatch_core.run_agent", return_value=result),
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
    ):
        run_tracked_agent(
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1318,
            store=store,
            runner=runner,
            model="requested-model",
            branch="agent/issue-1318",
        )
        record = store.list_by_issue(1318)[0]
        event = store.list_audit_events(event="dispatch_completed")[0]

    assert record.model == "switchyard/llm_classifier"
    assert record.transport_route == "switchyard/llm_classifier"
    assert record.reported_model == "gpt-5.6-sol"
    assert record.resolved_model_source == "switchyard_proxy_metrics"
    assert record.requested_service_tier == "fast"
    assert record.reported_service_tier == "fast"
    assert record.estimated_cost_usd > 0.0
    assert record.classifier_overhead_cost_usd > 0.0
    assert record.accounting_status == "reconciled"
    assert event.payload["admitted_route"] == "switchyard/llm_classifier"
    assert event.payload["reported_model"] == "gpt-5.6-sol"
    assert event.payload["classifier_overhead"]["model"] == "gpt-5.6-luna"
    assert event.payload["classifier_evidence"][0]["verdict"] == "selected_strong"
    assert event.payload["accounting_status"] == "reconciled"


def test_empty_managed_adaptive_evidence_is_loudly_unreconciled(tmp_path):
    from kew.dispatch_core import run_tracked_agent
    from kew.report import render_report

    binary = tmp_path / "switchyard-server"
    binary.write_text("binary")
    binary.chmod(0o755)
    route_config = tmp_path / "routes.toml"
    route_config.write_text(
        "schema_version = 1\n"
        "[llm_clients.local]\n"
        "[targets.strong]\n"
        'id = "strong"\n'
        'llm_client = "local"\n'
        "[routes.classifier]\n"
        'id = "switchyard/llm_classifier"\n'
        'type = "llm_classifier"\n'
        'classifier_target = "strong"\n'
    )
    config = DispatchConfig(
        retry=RetryConfig(max_attempts=1),
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            allow_adaptive=True,
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route="switchyard/llm_classifier",
                expected_version="pinned",
                managed=True,
                config_path=route_config,
                binary=binary,
                binary_sha256=hashlib.sha256(b"binary").hexdigest(),
            ),
        ),
    )
    runner = MagicMock()
    runner.name = "codex"
    runner.transport_capability = RunnerTransportCapability(
        incoming_api_formats=("openai-responses",),
        endpoint_override=True,
        model_override=True,
    )
    runner.supports_resume.return_value = False
    result = DispatchResult(
        exit_code=0,
        issue_num=1339,
        log_file=tmp_path / "dispatch.log",
        input_tokens=120,
        output_tokens=45,
        total_tokens=165,
        resolved_model="switchyard/llm_classifier",
    )
    supervisor = MagicMock()
    supervisor.observed_version = "pinned"
    supervisor.config_sha256 = "b" * 64
    supervisor.accounting_evidence.return_value = parse_switchyard_metrics(
        ""
    ).selection_evidence(route="switchyard/llm_classifier")
    supervisor.classifier_evidence = ()
    managed_context = MagicMock()
    managed_context.__enter__.return_value = supervisor

    with (
        patch(
            "kew.dispatch_core.managed_switchyard_context",
            return_value=managed_context,
        ),
        patch("kew.dispatch_core.check_pr_exists", return_value=None),
        patch("kew.dispatch_core.run_agent", return_value=result),
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
    ):
        run_tracked_agent(
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1339,
            store=store,
            runner=runner,
            model="requested-model",
            branch="agent/issue-1339",
        )
        record = store.list_by_issue(1339)[0]
        event = store.list_audit_events(event="dispatch_completed")[0]
        report = render_report(
            [record],
            repo="o/r",
            days=7,
            now=datetime.now(UTC),
            budget=config.budget,
            pr_states={},
        )

    assert record.accounting_status == "unreconciled"
    assert record.reported_model is None
    assert event.payload["accounting_status"] == "unreconciled"
    # "unpriced_model" joins the adaptive reasons rather than replacing them:
    # this run burned 165 tokens against a model with no rate card, so its
    # $0.00 is an unknown price, not a free run (#1565).
    assert event.payload["accounting_unreconciled_reasons"] == [
        "classifier_evidence_missing",
        "served_model_missing",
        "unpriced_model",
    ]
    assert "UNRECONCILED" in report


# --------------------------------------------------------------------------
# An unpriced model is "we could not price this", never "$0.00" (#1565).
# --------------------------------------------------------------------------


def _burned_tokens_result(tmp_path, **kwargs) -> DispatchResult:
    """A result that used real tokens and reports no native cost figure."""
    result = DispatchResult(
        exit_code=0,
        issue_num=1565,
        log_file=tmp_path / "dispatch.log",
        **kwargs,
    )
    result.input_tokens = 1_452_705
    result.output_tokens = 15_270
    return result


def test_an_unpriced_model_that_burned_tokens_is_unreconciled(tmp_path):
    """The route name is not a pricing key, so the $0.00 is unknown, not free.

    This is the #1565 shape: `switchyard/llm_classifier` reaches pricing as the
    model, resolves to no rate, and books $0.00 against 1.47M real tokens.
    """
    from kew.dispatch_core import _mark_unpriced_accounting

    result = _burned_tokens_result(tmp_path)

    _mark_unpriced_accounting(result, "switchyard/llm_classifier", None)

    assert result.accounting_status == "unreconciled"
    assert result.accounting_unreconciled_reasons == ["unpriced_model"]


def test_the_unpriced_reason_is_appended_not_assigned(tmp_path):
    """An adaptive route's own reasons must survive alongside the new one.

    ``_mark_unpriced_accounting`` runs after the Switchyard reconciliation has
    already recorded why the served model could not be identified; assigning
    would erase the more specific diagnosis.
    """
    from kew.dispatch_core import _mark_unpriced_accounting

    result = _burned_tokens_result(tmp_path)
    result.accounting_status = "unreconciled"
    result.accounting_unreconciled_reasons = ["served_model_missing"]

    _mark_unpriced_accounting(result, "switchyard/llm_classifier", None)

    assert result.accounting_unreconciled_reasons == [
        "served_model_missing",
        "unpriced_model",
    ]


def test_a_free_provider_prefix_is_not_marked(tmp_path):
    """Local inference is $0 by design — the zero is the answer, not a gap."""
    from kew.dispatch_core import _mark_unpriced_accounting

    result = _burned_tokens_result(tmp_path)

    _mark_unpriced_accounting(result, "lemonade/Qwen3-Coder-30B-A3B-Instruct", None)

    assert result.accounting_status == ""
    assert result.accounting_unreconciled_reasons is None


def test_a_native_cost_figure_is_never_marked(tmp_path):
    """A provider-billed number is authoritative even without a rate card."""
    from kew.dispatch_core import _mark_unpriced_accounting

    result = _burned_tokens_result(tmp_path)
    result.reported_cost_usd = 2.76

    _mark_unpriced_accounting(result, "switchyard/llm_classifier", None)

    assert result.accounting_status == ""


def test_a_run_that_burned_nothing_is_not_marked(tmp_path):
    """No tokens means nothing to price; $0.00 is simply true."""
    from kew.dispatch_core import _mark_unpriced_accounting

    result = DispatchResult(
        exit_code=1,
        issue_num=1565,
        log_file=tmp_path / "dispatch.log",
    )

    _mark_unpriced_accounting(result, "switchyard/llm_classifier", None)

    assert result.accounting_status == ""


def test_a_pricing_override_makes_the_model_priced(tmp_path):
    """An operator-supplied rate is a usable rate — nothing is unreconciled."""
    from kew.dispatch_core import _mark_unpriced_accounting

    result = _burned_tokens_result(tmp_path)

    _mark_unpriced_accounting(
        result,
        "switchyard/llm_classifier",
        {"switchyard/llm_classifier": (1.0, 5.0)},
    )

    assert result.accounting_status == ""
