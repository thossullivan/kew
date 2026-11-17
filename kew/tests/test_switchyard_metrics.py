"""Switchyard Prometheus observations are bounded and never billing truth."""

from __future__ import annotations

from pathlib import Path

import pytest

from kew.switchyard_metrics import (
    HttpSwitchyardMetricsTransport,
    MetricsResponse,
    ProxyMetricsObservation,
    SwitchyardMetricsError,
    managed_metrics_delta,
    observe_switchyard_metrics,
    parse_switchyard_metrics,
)

_SCOPE = 'otel_scope_name="switchyard"'
_DECISION = 'algorithm="passthrough",selected_model="gpt-5.6-sol"'
# The families the pinned Switchyard build emits, in the label shapes it emits them
# (upstream README "Metrics" table; switchyard_decisions_total is libsy's
# routing-decision counter). The recorded fixture below has no failed call, so
# errors appear only here.
_DOCUMENTED_METRICS = f"""\
# HELP switchyard_requests_total Successful final routed calls.
# TYPE switchyard_requests_total counter
switchyard_requests_total{{model="gpt-5.6-sol",{_SCOPE}}} 3
# TYPE switchyard_errors_total counter
switchyard_errors_total{{model="gpt-5.6-sol",{_SCOPE}}} 1
# TYPE switchyard_total_latency_ms histogram
switchyard_total_latency_ms_bucket{{model="gpt-5.6-sol",{_SCOPE},le="500"}} 2
switchyard_total_latency_ms_sum{{model="gpt-5.6-sol",{_SCOPE}}} 750
switchyard_total_latency_ms_count{{model="gpt-5.6-sol",{_SCOPE}}} 3
# TYPE switchyard_routing_overhead_ms histogram
switchyard_routing_overhead_ms_sum{{algorithm="passthrough",{_SCOPE}}} 0.3
# TYPE switchyard_decisions_total counter
switchyard_decisions_total{{{_DECISION},{_SCOPE}}} 3
# TYPE switchyard_prompt_tokens_total counter
switchyard_prompt_tokens_total{{model="gpt-5.6-sol",{_SCOPE}}} 120
# TYPE switchyard_completion_tokens_total counter
switchyard_completion_tokens_total{{model="gpt-5.6-sol",{_SCOPE}}} 45
# TYPE switchyard_llm_calls_total counter
switchyard_llm_calls_total{{{_DECISION},outcome="ok",{_SCOPE}}} 3
python_gc_objects_collected_total{{generation="0"}} 999
"""
_RECORDED_METRICS = Path(__file__).parent / "fixtures" / "switchyard" / "metrics.txt"


def test_parse_switchyard_documented_metric_families() -> None:
    observation = parse_switchyard_metrics(_DOCUMENTED_METRICS)

    assert observation.scope == "process_wide_proxy"
    assert observation.accounting_authority == "runner_reported_usage"
    assert observation.per_run_attribution is False
    assert {sample.family for sample in observation.samples} == {
        "requests",
        "errors",
        "request_latency",
        "routing_overhead",
        "model_selection",
        "tokens",
    }
    assert not any(sample.name.startswith("python_") for sample in observation.samples)
    assert not any(
        sample.name.startswith("switchyard_llm_") for sample in observation.samples
    )


def test_the_recorded_proxy_metrics_yield_every_family_it_emits() -> None:
    """A real /metrics body from the pinned image, not a hand-written one.

    One passthrough call of 120 input and 45 output tokens (PROVENANCE.md).
    """
    observation = parse_switchyard_metrics(_RECORDED_METRICS.read_text())

    assert {sample.family for sample in observation.samples} == {
        "requests",
        "request_latency",
        "routing_overhead",
        "model_selection",
        "tokens",
    }
    by_name = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for sample in observation.samples
    }
    assert by_name[("switchyard_requests_total", (("model", "synthetic/model"),))] == 1
    assert (
        by_name[
            (
                "switchyard_decisions_total",
                (("algorithm", "passthrough"), ("selected_model", "synthetic/model")),
            )
        ]
        == 1
    )
    assert (
        by_name[("switchyard_prompt_tokens_total", (("model", "synthetic/model"),))]
        == 120
    )
    assert (
        by_name[("switchyard_completion_tokens_total", (("model", "synthetic/model"),))]
        == 45
    )


def test_the_recorded_metrics_evidence_the_served_model_and_no_tier() -> None:
    observation = parse_switchyard_metrics(_RECORDED_METRICS.read_text())

    evidence = observation.selection_evidence(
        route="switchyard/passthrough",
        runner_input_tokens=120,
        runner_output_tokens=45,
    )

    assert evidence.selected_model == "synthetic/model"
    assert evidence.model_candidates == ("synthetic/model",)
    assert evidence.selected_model_source == "switchyard_proxy_metrics"
    # The pinned Switchyard build puts no tier label on any metric: the tier is absent,
    # not guessed.
    assert evidence.selected_tier is None
    assert evidence.to_json()["selected_tier"] is None


def test_a_tier_shaped_metric_is_not_tier_evidence() -> None:
    """Names kew read before 0.3.0 are not on the 0.3.0 surface; a body that
    still carries them evidences nothing from them."""
    observation = parse_switchyard_metrics(
        'switchyard_tier_selections_total{tier="strong"} 1\n'
        'switchyard_model_selections_total{model="gpt-5.6-sol"} 1\n'
        'switchyard_requests_total{model="gpt-5.6-sol",tier="strong"} 1\n'
    )

    assert [sample.name for sample in observation.samples] == [
        "switchyard_requests_total"
    ]
    assert observation.samples[0].labels == {"model": "gpt-5.6-sol"}
    evidence = observation.selection_evidence(route="switchyard/llm_classifier")
    assert evidence.selected_tier is None
    assert evidence.model_candidates == ()


def test_switchyard_metric_json_schema_is_explicitly_observational() -> None:
    payload = parse_switchyard_metrics(_DOCUMENTED_METRICS).to_json()

    assert set(payload) == {
        "kind",
        "scope",
        "accounting_authority",
        "per_run_attribution",
        "samples",
    }
    assert payload["kind"] == "switchyard_proxy_observation"
    assert payload["scope"] == "process_wide_proxy"
    assert payload["accounting_authority"] == "runner_reported_usage"
    assert payload["per_run_attribution"] is False
    assert all(
        set(sample) == {"family", "name", "labels", "value"}
        for sample in payload["samples"]
    )
    assert "cost_usd" not in repr(payload)
    assert "run_id" not in repr(payload)


def test_switchyard_metric_labels_are_allowlisted() -> None:
    payload = (
        'switchyard_requests_total{route="safe",api_key="secret",prompt="private"} 1\n'
        "switchyard_secret_tokens_total 100\n"
    )
    observation = parse_switchyard_metrics(payload)

    assert observation.samples[0].labels == {"route": "safe"}
    assert len(observation.samples) == 1
    assert "secret" not in repr(observation)
    assert "private" not in repr(observation)


def test_undocumented_non_finite_metric_does_not_hide_documented_samples() -> None:
    observation = parse_switchyard_metrics(
        'go_gc_duration_seconds{quantile="0.99"} NaN\nswitchyard_requests_total 2\n'
    )

    assert [(sample.name, sample.value) for sample in observation.samples] == [
        ("switchyard_requests_total", 2)
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "switchyard_requests_total nope\n",
        "switchyard_requests_total{route=unquoted} 1\n",
        "switchyard_requests_total 1 trailing\n",
        "switchyard_requests_total NaN\n",
    ],
)
def test_malformed_switchyard_metrics_are_sanitized(payload: str) -> None:
    with pytest.raises(SwitchyardMetricsError, match="malformed_metrics") as exc_info:
        parse_switchyard_metrics(payload)

    assert payload.strip() not in str(exc_info.value)


def test_unavailable_switchyard_observation_has_stable_schema() -> None:
    payload = ProxyMetricsObservation.unavailable("unreachable").to_json()

    assert payload == {
        "kind": "switchyard_proxy_observation",
        "scope": "process_wide_proxy",
        "accounting_authority": "runner_reported_usage",
        "per_run_attribution": False,
        "samples": [],
        "unavailable_reason": "unreachable",
    }


def test_managed_metrics_delta_is_scoped_to_the_owned_run() -> None:
    baseline = parse_switchyard_metrics(
        "\n".join(
            (
                'switchyard_requests_total{model="strong"} 4',
                "switchyard_decisions_total"
                '{algorithm="llm_classifier",selected_model="strong"} 3',
                "switchyard_decisions_total"
                '{algorithm="llm_classifier",selected_model="stale"} 2',
                'switchyard_prompt_tokens_total{model="strong"} 90',
            )
        )
        + "\n"
    )
    final = parse_switchyard_metrics(
        "\n".join(
            (
                'switchyard_requests_total{model="strong"} 6',
                "switchyard_decisions_total"
                '{algorithm="llm_classifier",selected_model="strong"} 5',
                "switchyard_decisions_total"
                '{algorithm="llm_classifier",selected_model="stale"} 2',
                'switchyard_prompt_tokens_total{model="strong"} 150',
            )
        )
        + "\n"
    )

    delta = managed_metrics_delta(baseline, final)

    assert delta.scope == "managed_run_delta"
    assert delta.per_run_attribution is True
    assert {(sample.family, sample.value) for sample in delta.samples} == {
        ("requests", 2.0),
        ("model_selection", 2.0),
        ("tokens", 60.0),
    }
    assert "stale" not in repr(delta)


def test_managed_metrics_delta_fails_closed_on_counter_reset() -> None:
    baseline = parse_switchyard_metrics("switchyard_requests_total 4\n")
    final = parse_switchyard_metrics("switchyard_requests_total 1\n")

    delta = managed_metrics_delta(baseline, final)

    assert delta.samples == ()
    assert delta.unavailable_reason == "counter_reset"


class _MetricsTransport:
    def __init__(self, response: MetricsResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, float]] = []

    def get_text(self, url: str, timeout_seconds: float) -> MetricsResponse:
        self.calls.append((url, timeout_seconds))
        return self.response


def test_observe_switchyard_metrics_uses_exact_bounded_endpoint() -> None:
    transport = _MetricsTransport(
        MetricsResponse(status_code=200, text="switchyard_requests_total 4\n")
    )

    observation = observe_switchyard_metrics(
        endpoint="https://switchyard.example:4000",
        transport=transport,
        timeout_seconds=1.5,
    )

    assert transport.calls == [("https://switchyard.example:4000/metrics", 1.5)]
    assert observation.samples[0].value == 4


def test_observe_switchyard_metrics_sanitizes_http_failure() -> None:
    transport = _MetricsTransport(MetricsResponse(status_code=503, text="private"))

    with pytest.raises(SwitchyardMetricsError, match="metrics_unavailable") as exc_info:
        observe_switchyard_metrics(
            endpoint="https://switchyard.example", transport=transport
        )

    assert "private" not in str(exc_info.value)


def test_observe_switchyard_metrics_rejects_non_origin_before_transport() -> None:
    transport = _MetricsTransport(MetricsResponse(status_code=200, text=""))

    with pytest.raises(SwitchyardMetricsError, match="invalid_endpoint"):
        observe_switchyard_metrics(
            endpoint="https://token@switchyard.example/private?key=secret",
            transport=transport,
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://token@switchyard.example/metrics",
        "https://switchyard.example/private",
        "https://switchyard.example/metrics?key=secret",
        "https://switchyard.example/metrics#private",
    ],
)
def test_http_transport_rejects_urls_outside_exact_metrics_endpoint(url: str) -> None:
    with pytest.raises(SwitchyardMetricsError, match="invalid_endpoint"):
        HttpSwitchyardMetricsTransport().get_text(url, timeout_seconds=1.0)


# --------------------------------------------------------------------------
# The metric names Switchyard ACTUALLY exports (#1565 follow-up).
#
# The fixture above is a documented-shape invention: it gives token counters a
# ``direction`` label on a metric called ``switchyard_tokens_total``. Switchyard
# emits neither. ``crates/switchyard-server/src/usage_metrics.rs`` registers
# ``switchyard.prompt_tokens`` / ``switchyard.completion_tokens`` with exactly
# one attribute, ``model``, and the Prometheus exporter renders those with the
# ``_total`` suffix. Because kew looked only for names that do not exist, the
# token evidence behind ``_unique_usage_match`` was never once collected, and
# every adaptive run fell through to ``served_model_missing``.
#
# These fixtures are transcribed from the real exposition, not invented.
# --------------------------------------------------------------------------

_REAL_EXPOSITION = """\
# TYPE switchyard_requests_total counter
switchyard_requests_total{model="deepseek/deepseek-v4-flash-0731",tier="weak"} 1
switchyard_requests_total{model="openai/gpt-5.6-sol"} 45
# TYPE switchyard_prompt_tokens_total counter
switchyard_prompt_tokens_total{model="openai/gpt-5.6-sol"} 1452705
switchyard_prompt_tokens_total{model="deepseek/deepseek-v4-flash-0731"} 812
# TYPE switchyard_completion_tokens_total counter
switchyard_completion_tokens_total{model="openai/gpt-5.6-sol"} 15270
switchyard_completion_tokens_total{model="deepseek/deepseek-v4-flash-0731"} 24
# TYPE switchyard_cached_tokens_total counter
switchyard_cached_tokens_total{model="openai/gpt-5.6-sol"} 1383651
"""


def test_real_token_counter_names_are_collected_as_token_evidence() -> None:
    """The names kew looked for did not exist; these do."""
    observation = parse_switchyard_metrics(_REAL_EXPOSITION)

    token_samples = [s for s in observation.samples if s.family == "tokens"]
    assert {s.name for s in token_samples} == {
        "switchyard_prompt_tokens_total",
        "switchyard_completion_tokens_total",
    }


def test_direction_comes_from_the_metric_name_when_there_is_no_label() -> None:
    """Switchyard encodes direction in the name, not in a label."""
    from kew.switchyard_metrics import _token_totals_by_model

    totals = _token_totals_by_model(parse_switchyard_metrics(_REAL_EXPOSITION))

    assert totals["openai/gpt-5.6-sol"] == {"input": 1452705, "output": 15270}
    assert totals["deepseek/deepseek-v4-flash-0731"] == {"input": 812, "output": 24}


def test_cached_tokens_are_not_counted_a_second_time() -> None:
    """The proxy folds cached/cache-creation INTO prompt_tokens before
    recording it. Admitting ``switchyard_cached_tokens_total`` as input would
    double-count the prompt side and defeat the exact usage match."""
    from kew.switchyard_metrics import _token_totals_by_model

    totals = _token_totals_by_model(parse_switchyard_metrics(_REAL_EXPOSITION))

    # 1452705 alone, NOT 1452705 + 1383651.
    assert totals["openai/gpt-5.6-sol"]["input"] == 1452705


def test_usage_match_names_the_served_model_among_several_candidates() -> None:
    """The #1565 shape, resolved.

    Two models served traffic on a classifier route and the proxy exports no
    model-selection counter, so identity can only come from matching observed
    token totals against the runner's own reported usage. That match had no
    data before this fixture's metric names were recognised.
    """
    evidence = parse_switchyard_metrics(_REAL_EXPOSITION).selection_evidence(
        route="switchyard/llm_classifier",
        served_model=None,
        runner_input_tokens=1452705,
        runner_output_tokens=15270,
    )

    assert evidence.selected_model == "openai/gpt-5.6-sol"
    assert evidence.selected_model_source == "switchyard_proxy_metrics"


def test_a_usage_total_that_matches_nothing_still_refuses_to_guess() -> None:
    """Fail-safe: a mismatch leaves identity unresolved, exactly as before.

    This fix can only ever turn an unresolved run into a resolved one; it can
    never rename a run kew would previously have identified correctly.
    """
    evidence = parse_switchyard_metrics(_REAL_EXPOSITION).selection_evidence(
        route="switchyard/llm_classifier",
        served_model=None,
        runner_input_tokens=999,
        runner_output_tokens=7,
    )

    assert evidence.selected_model is None


def test_a_selected_model_that_fell_back_is_not_the_served_model() -> None:
    """The router picked ``weak``; the call failed over to ``strong``.

    ``switchyard_decisions_total`` (libsy's ``record_decision``) names the
    model the router picked when it published its decision; a fallback to
    another target does not change it, so the decision is not served-model
    evidence when the successful call went elsewhere. Nothing here identifies
    the served model by usage either, so it stays unresolved and no
    strong-model tokens become classifier overhead.
    """
    observation = managed_metrics_delta(
        parse_switchyard_metrics(""),
        parse_switchyard_metrics(
            'switchyard_decisions_total{algorithm="llm_classifier",'
            'selected_model="weak"} 1\n'
            'switchyard_errors_total{model="weak"} 1\n'
            'switchyard_requests_total{model="strong"} 1\n'
            'switchyard_prompt_tokens_total{model="strong"} 1200\n'
            'switchyard_completion_tokens_total{model="strong"} 300\n'
        ),
    )

    evidence = observation.selection_evidence(
        route="switchyard/llm_classifier",
        runner_input_tokens=200,
        runner_output_tokens=300,
    )

    assert evidence.selected_model is None
    assert evidence.model_candidates == ()
    assert evidence.classifier_model is None
    assert evidence.classifier_input_tokens == 0
    assert evidence.classifier_output_tokens == 0


def test_a_window_where_one_turn_fell_back_leaves_the_served_model_unresolved() -> None:
    """Two turns both selected ``weak``; the second fell back to ``strong``.

    Successful final calls name two models, so no single served model exists
    for the window and the decision counter cannot pick one.
    """
    observation = managed_metrics_delta(
        parse_switchyard_metrics(""),
        parse_switchyard_metrics(
            'switchyard_decisions_total{algorithm="llm_classifier",'
            'selected_model="weak"} 2\n'
            'switchyard_errors_total{model="weak"} 1\n'
            'switchyard_requests_total{model="weak"} 1\n'
            'switchyard_requests_total{model="strong"} 1\n'
            'switchyard_prompt_tokens_total{model="weak"} 100\n'
            'switchyard_prompt_tokens_total{model="strong"} 1200\n'
        ),
    )

    evidence = observation.selection_evidence(route="switchyard/llm_classifier")

    assert evidence.selected_model is None
    assert evidence.model_candidates == ()


def test_a_decision_matching_the_one_served_model_names_it() -> None:
    """Every turn selected ``strong`` and every final call succeeded on it."""
    observation = managed_metrics_delta(
        parse_switchyard_metrics(""),
        parse_switchyard_metrics(
            'switchyard_decisions_total{algorithm="llm_classifier",'
            'selected_model="strong"} 2\n'
            'switchyard_requests_total{model="strong"} 2\n'
        ),
    )

    evidence = observation.selection_evidence(route="switchyard/llm_classifier")

    assert evidence.selected_model == "strong"
    assert evidence.model_candidates == ("strong",)
