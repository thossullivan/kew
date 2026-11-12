"""Switchyard Prometheus observations are bounded and never billing truth."""

from __future__ import annotations

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

_DOCUMENTED_METRICS = """\
# HELP switchyard_requests_total Requests handled by the proxy.
# TYPE switchyard_requests_total counter
switchyard_requests_total{route="switchyard/passthrough",status="ok"} 3
# TYPE switchyard_errors_total counter
switchyard_errors_total{route="switchyard/passthrough",error="upstream"} 1
# TYPE switchyard_request_duration_seconds histogram
switchyard_request_duration_seconds_bucket{route="switchyard/passthrough",le="0.5"} 2
switchyard_request_duration_seconds_sum{route="switchyard/passthrough"} 0.75
switchyard_request_duration_seconds_count{route="switchyard/passthrough"} 3
# TYPE switchyard_routing_overhead_seconds histogram
switchyard_routing_overhead_seconds_sum{route="switchyard/passthrough"} 0.03
# TYPE switchyard_model_selections_total counter
switchyard_model_selections_total{model="gpt-5.6-sol",route="switchyard/passthrough"} 3
# TYPE switchyard_tier_selections_total counter
switchyard_tier_selections_total{tier="passthrough"} 3
# TYPE switchyard_prompt_tokens_total counter
switchyard_prompt_tokens_total{model="gpt-5.6-sol"} 120
# TYPE switchyard_completion_tokens_total counter
switchyard_completion_tokens_total{model="gpt-5.6-sol"} 45
python_gc_objects_collected_total{generation="0"} 999
"""


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
        "tier_selection",
        "tokens",
    }
    assert not any(sample.name.startswith("python_") for sample in observation.samples)


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
                'switchyard_requests_total{route="adaptive"} 4',
                'switchyard_model_selections_total{model="strong",route="adaptive"} 3',
                'switchyard_model_selections_total{model="stale",route="adaptive"} 2',
                'switchyard_prompt_tokens_total{model="strong"} 90',
            )
        )
        + "\n"
    )
    final = parse_switchyard_metrics(
        "\n".join(
            (
                'switchyard_requests_total{route="adaptive"} 6',
                'switchyard_model_selections_total{model="strong",route="adaptive"} 5',
                'switchyard_model_selections_total{model="stale",route="adaptive"} 2',
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
