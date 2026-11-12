"""Sanitized Switchyard classifier evidence and benchmark integration."""

from __future__ import annotations

from kew.switchyard_benchmark import (
    BenchmarkCase,
    BenchmarkCorpus,
    BenchmarkExecution,
    BenchmarkMetadata,
    BenchmarkUsage,
    QualityCheck,
    render_benchmark_json,
    run_benchmark,
)
from kew.switchyard_classifier import (
    parse_classifier_evidence,
    parse_classifier_evidence_line,
)
from kew.switchyard_metrics import parse_switchyard_metrics


def _metadata() -> BenchmarkMetadata:
    return BenchmarkMetadata(
        runner="codex",
        requested_model="gpt-5.6-sol",
        runner_version="codex-cli 1.2.3",
        switchyard_version="6ff491a",
        direct_config_sha256="a" * 64,
        passthrough_config_sha256="b" * 64,
    )


def _metrics(requests: int) -> object:
    return parse_switchyard_metrics(
        f'switchyard_requests_total{{route="switchyard/passthrough"}} {requests}\n'
    )


def _corpus() -> BenchmarkCorpus:
    return BenchmarkCorpus(
        name="classifier-fixtures",
        cases=(
            BenchmarkCase(
                case_id="case-1",
                prompt="private prompt must never reach evidence",
                quality_checks=(QualityCheck(name="answer", contains="OK"),),
            ),
        ),
        sha256="c" * 64,
    )


def test_codex_responses_ingress_supports_responses_and_translated_chat_clients():
    responses = parse_classifier_evidence(
        {
            "recommended_route": "capable",
            "p_solve": 0.21,
            "confidence": 0.92,
            "abstain": False,
            "capability_boundary": "supported",
            "selected_tier": "strong",
        },
        ingress_format="openai_responses",
        upstream_format="openai_responses",
    )
    translated_chat = parse_classifier_evidence(
        {
            "recommended_route": "efficient",
            "p_solve": 0.91,
            "confidence": 0.78,
            "abstain": False,
            "capability_boundary": "supported",
            "selected_tier": "weak",
        },
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )

    assert responses.to_json() == {
        "capability_boundary": "supported",
        "confidence": 0.92,
        "confidence_bucket": "very_high",
        "fail_closed_reason": None,
        "fallback_taken": False,
        "ingress_format": "openai_responses",
        "p_solve": 0.21,
        "selected_tier": "strong",
        "upstream_format": "openai_responses",
        "verdict": "selected_strong",
    }
    assert translated_chat.verdict == "selected_weak"
    assert translated_chat.upstream_format == "openai_chat"
    assert translated_chat.ingress_format == "openai_responses"
    assert translated_chat.confidence_bucket == "high"


def test_fail_open_classifier_record_is_observable_without_raw_error():
    evidence = parse_classifier_evidence_line(
        (
            'classifier_signals={"abstain":true,"fail_open":true,'
            '"error":"private prompt and provider response"}'
        ),
        ingress_format="openai_responses",
        upstream_format="openai_responses",
    )

    assert evidence is not None
    assert evidence.verdict == "classifier_unavailable_fallback_strong"
    assert evidence.selected_tier == "strong"
    assert evidence.confidence is None
    assert evidence.confidence_bucket == "unavailable"
    assert evidence.fallback_taken is True
    assert evidence.fail_closed_reason == "classifier_unavailable"
    serialized = repr(evidence.to_json())
    assert "private prompt" not in serialized
    assert "provider response" not in serialized

    error_only = parse_classifier_evidence_line(
        'classifier_signals={"confidence":0.9,"selected_tier":"strong",'
        '"error":"private parse failure"}',
        ingress_format="openai_responses",
        upstream_format="openai_responses",
    )
    assert error_only is not None
    assert error_only.verdict == "classifier_unavailable_fallback_strong"
    assert error_only.fail_closed_reason == "classifier_unavailable"
    assert "private parse failure" not in repr(error_only.to_json())


def test_selected_strong_is_distinct_from_invalid_classifier_fallback():
    selected = parse_classifier_evidence(
        {
            "confidence": 0.5,
            "abstain": False,
            "selected_tier": "strong",
        },
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )
    selected_from_verdict = parse_classifier_evidence(
        {"confidence": 0.5, "verdict": "selected_strong"},
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )
    fallback = parse_classifier_evidence(
        {"confidence": "not-a-number", "selected_tier": "strong"},
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )

    assert selected.verdict == "selected_strong"
    assert selected.fallback_taken is False
    assert selected.fail_closed_reason is None
    assert selected_from_verdict.verdict == "selected_strong"
    assert fallback.verdict == "classifier_unavailable_fallback_strong"
    assert fallback.fallback_taken is True
    assert fallback.fail_closed_reason == "classifier_invalid_verdict"

    explicit_fallback = parse_classifier_evidence(
        {
            "fallback_taken": True,
            "fail_closed_reason": "parse_error",
        },
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )
    assert explicit_fallback.fail_closed_reason == "classifier_invalid_verdict"


def test_classifier_evidence_is_attached_per_case_and_report_is_sanitized():
    strong = parse_classifier_evidence(
        {
            "confidence": 0.94,
            "abstain": False,
            "selected_tier": "strong",
        },
        ingress_format="openai_responses",
        upstream_format="openai_responses",
    )
    fallback = parse_classifier_evidence_line(
        'classifier_signals={"fail_open":true,"error":"secret model output"}',
        ingress_format="openai_responses",
        upstream_format="openai_chat",
    )
    assert fallback is not None

    report = run_benchmark(
        corpus=_corpus(),
        metadata=_metadata(),
        execute=lambda mode, _case: BenchmarkExecution(
            response_text="OK private model response",
            latency_ms=1,
            usage=BenchmarkUsage(),
            classifier_evidence=strong if mode == "direct" else fallback,
        ),
        proxy_baseline=_metrics(0),
        proxy_final=_metrics(1),
        exclusive_use=True,
        unrelated_traffic=False,
    )

    payload = report.to_json()
    results = payload["results"]
    assert isinstance(results, list)
    assert results[0]["classifier_evidence"]["verdict"] == "selected_strong"
    assert (
        results[1]["classifier_evidence"]["verdict"]
        == "classifier_unavailable_fallback_strong"
    )
    assert results[1]["classifier_evidence"]["fallback_taken"] is True
    rendered = render_benchmark_json(report)
    assert "private model response" not in rendered
    assert "secret model output" not in rendered
    assert "private prompt" not in rendered
