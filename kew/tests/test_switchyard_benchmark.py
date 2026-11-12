"""Direct-versus-passthrough benchmark evidence stays deterministic and honest."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.switchyard_metrics import parse_switchyard_metrics


def _benchmark_api():
    from kew.switchyard_benchmark import (
        BenchmarkCase,
        BenchmarkCorpus,
        BenchmarkExecution,
        BenchmarkMetadata,
        BenchmarkUsage,
        QualityCheck,
        load_benchmark_corpus,
        render_benchmark_json,
        run_benchmark,
    )

    return {
        "BenchmarkCase": BenchmarkCase,
        "BenchmarkCorpus": BenchmarkCorpus,
        "BenchmarkExecution": BenchmarkExecution,
        "BenchmarkMetadata": BenchmarkMetadata,
        "BenchmarkUsage": BenchmarkUsage,
        "QualityCheck": QualityCheck,
        "load_benchmark_corpus": load_benchmark_corpus,
        "render_benchmark_json": render_benchmark_json,
        "run_benchmark": run_benchmark,
    }


def _write_corpus(path: Path) -> bytes:
    raw = (
        b'{"schema_version":1,"name":"fixed-smoke","cases":['
        b'{"id":"alpha","prompt":"Reply with ALPHA.",'
        b'"quality_checks":[{"name":"answer","contains":"ALPHA"}],'
        b'"expected_proxy_requests":1},'
        b'{"id":"beta","prompt":"Reply with BETA.",'
        b'"quality_checks":[{"name":"answer","contains":"BETA"}],'
        b'"expected_proxy_requests":2}]}'
    )
    path.write_bytes(raw)
    return raw


def _metadata(api):
    return api["BenchmarkMetadata"](
        runner="codex",
        requested_model="gpt-5.6-sol",
        runner_version="codex-cli 1.2.3",
        switchyard_version="6ff491a",
        direct_config_sha256="a" * 64,
        passthrough_config_sha256="b" * 64,
    )


def _metrics(requests: int, *, model: int, tier: int, tokens: int, seconds: float):
    return parse_switchyard_metrics(
        f'switchyard_requests_total{{route="switchyard/passthrough"}} {requests}\n'
        f'switchyard_model_selections_total{{model="gpt-5.6-sol"}} {model}\n'
        f'switchyard_tier_selections_total{{tier="passthrough"}} {tier}\n'
        f'switchyard_prompt_tokens_total{{model="gpt-5.6-sol"}} {tokens}\n'
        "switchyard_request_duration_seconds_sum"
        f'{{route="switchyard/passthrough"}} {seconds}\n'
    )


def test_load_fixed_corpus_hashes_exact_bytes_and_rejects_duplicate_ids(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    raw = _write_corpus(corpus_path)

    corpus = api["load_benchmark_corpus"](corpus_path)

    assert corpus.name == "fixed-smoke"
    assert [case.case_id for case in corpus.cases] == ["alpha", "beta"]
    assert corpus.sha256 == hashlib.sha256(raw).hexdigest()
    assert sum(case.expected_proxy_requests for case in corpus.cases) == 3

    corpus_path.write_text(
        '{"schema_version":1,"name":"bad","cases":['
        '{"id":"same","prompt":"one",'
        '"quality_checks":[{"name":"answer","contains":"ONE"}]},'
        '{"id":"same","prompt":"two",'
        '"quality_checks":[{"name":"answer","contains":"TWO"}]}]}'
    )
    with pytest.raises(ValueError, match="duplicate case id"):
        api["load_benchmark_corpus"](corpus_path)


def test_benchmark_runs_direct_then_passthrough_and_records_runner_authority(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    corpus = api["load_benchmark_corpus"](corpus_path)
    calls: list[tuple[str, str]] = []

    def execute(mode, case):
        calls.append((mode, case.case_id))
        expected = case.quality_checks[0].contains
        return api["BenchmarkExecution"](
            response_text=f"result: {expected}",
            latency_ms=125,
            usage=api["BenchmarkUsage"](
                input_tokens=10,
                output_tokens=3,
                cache_creation_tokens=0,
                cache_read_tokens=2,
                cost_usd=0.004,
                model="gpt-5.6-sol",
            ),
        )

    report = api["run_benchmark"](
        corpus=corpus,
        metadata=_metadata(api),
        execute=execute,
        proxy_baseline=_metrics(7, model=7, tier=7, tokens=100, seconds=2.0),
        proxy_final=_metrics(10, model=10, tier=10, tokens=145, seconds=2.6),
        exclusive_use=True,
        unrelated_traffic=False,
    )
    payload = report.to_json()

    assert calls == [
        ("direct", "alpha"),
        ("direct", "beta"),
        ("passthrough", "alpha"),
        ("passthrough", "beta"),
    ]
    assert payload["adaptive_routing_enabled"] is False
    assert payload["exit_code"] == 0
    assert [result["mode"] for result in payload["results"]] == [
        "direct",
        "direct",
        "passthrough",
        "passthrough",
    ]
    assert all(
        result["runner_usage"]["accounting_authority"] == "runner_reported_usage"
        for result in payload["results"]
    )
    assert all(
        result["quality_checks"] == [{"name": "answer", "passed": True}]
        for result in payload["results"]
    )
    assert payload["proxy_observations"]["valid"] is True
    assert payload["proxy_observations"]["observed_requests"] == 3
    assert payload["proxy_observations"]["accounting_authority"] == (
        "runner_reported_usage"
    )
    assert payload["proxy_observations"]["model_selections"] == [
        {"labels": {"model": "gpt-5.6-sol"}, "value": 3.0}
    ]
    assert payload["proxy_observations"]["tier_selections"] == [
        {"labels": {"tier": "passthrough"}, "value": 3.0}
    ]


@pytest.mark.parametrize(
    ("exclusive_use", "unrelated_traffic", "final_requests", "reason"),
    [
        (False, False, 10, "exclusive_use_not_proven"),
        (True, True, 10, "unrelated_traffic_detected"),
        (True, False, 6, "proxy_counter_reset"),
        (True, False, 11, "request_count_mismatch"),
    ],
)
def test_proxy_observations_are_all_invalid_when_window_is_not_isolated(
    tmp_path, exclusive_use, unrelated_traffic, final_requests, reason
):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)

    def execute(mode, case):
        return api["BenchmarkExecution"](
            response_text=case.quality_checks[0].contains,
            latency_ms=1,
            usage=api["BenchmarkUsage"](input_tokens=1, output_tokens=1),
        )

    report = api["run_benchmark"](
        corpus=api["load_benchmark_corpus"](corpus_path),
        metadata=_metadata(api),
        execute=execute,
        proxy_baseline=_metrics(7, model=7, tier=7, tokens=100, seconds=2.0),
        proxy_final=_metrics(
            final_requests, model=10, tier=10, tokens=145, seconds=2.6
        ),
        exclusive_use=exclusive_use,
        unrelated_traffic=unrelated_traffic,
    )
    observations = report.to_json()["proxy_observations"]

    assert observations["valid"] is False
    assert observations["invalid_reason"] == reason
    assert observations["model_selections"] == []
    assert observations["tier_selections"] == []
    assert observations["samples"] == []


def test_json_rendering_is_canonical_and_contains_version_digests(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)

    def execute(mode, case):
        return api["BenchmarkExecution"](
            response_text=case.quality_checks[0].contains,
            latency_ms=1,
            usage=api["BenchmarkUsage"](input_tokens=1, output_tokens=1),
        )

    report = api["run_benchmark"](
        corpus=api["load_benchmark_corpus"](corpus_path),
        metadata=_metadata(api),
        execute=execute,
        proxy_baseline=_metrics(0, model=0, tier=0, tokens=0, seconds=0.0),
        proxy_final=_metrics(3, model=3, tier=3, tokens=6, seconds=0.5),
        exclusive_use=True,
        unrelated_traffic=False,
    )

    rendered = api["render_benchmark_json"](report)
    assert rendered.endswith("\n")
    assert rendered == api["render_benchmark_json"](report)
    assert ", " not in rendered
    payload = json.loads(rendered)
    assert payload == report.to_json()
    assert (
        payload["versions"]["runner"]["sha256"]
        == hashlib.sha256(b"codex-cli 1.2.3").hexdigest()
    )
    assert (
        payload["versions"]["switchyard"]["sha256"]
        == hashlib.sha256(b"6ff491a").hexdigest()
    )


def test_failed_quality_check_sets_report_exit_one_without_emitting_response(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)

    report = api["run_benchmark"](
        corpus=api["load_benchmark_corpus"](corpus_path),
        metadata=_metadata(api),
        execute=lambda mode, case: api["BenchmarkExecution"](
            response_text="private wrong answer",
            latency_ms=5,
            usage=api["BenchmarkUsage"](),
        ),
        proxy_baseline=_metrics(0, model=0, tier=0, tokens=0, seconds=0),
        proxy_final=_metrics(3, model=3, tier=3, tokens=0, seconds=1),
        exclusive_use=True,
        unrelated_traffic=False,
    )
    rendered = api["render_benchmark_json"](report)

    assert report.exit_code == 1
    assert "private wrong answer" not in rendered
    assert "Reply with ALPHA" not in rendered


def test_switchyard_benchmark_cli_emits_only_harness_json(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    corpus = api["load_benchmark_corpus"](corpus_path)

    def execute(mode, case):
        return api["BenchmarkExecution"](
            response_text=case.quality_checks[0].contains,
            latency_ms=1,
            usage=api["BenchmarkUsage"](input_tokens=1, output_tokens=1),
        )

    report = api["run_benchmark"](
        corpus=corpus,
        metadata=_metadata(api),
        execute=execute,
        proxy_baseline=_metrics(0, model=0, tier=0, tokens=0, seconds=0),
        proxy_final=_metrics(3, model=3, tier=3, tokens=0, seconds=1),
        exclusive_use=True,
        unrelated_traffic=False,
    )

    with patch("kew.cli.run_live_switchyard_benchmark", return_value=report) as live:
        result = CliRunner().invoke(
            app,
            [
                "switchyard",
                "benchmark",
                "--corpus",
                str(corpus_path),
                "--runner",
                "codex",
                "--model",
                "gpt-5.6-sol",
            ],
        )

    assert result.exit_code == 0
    assert result.stdout == api["render_benchmark_json"](report)
    assert live.call_args.kwargs["corpus_path"] == corpus_path
    assert live.call_args.kwargs["runner_name"] == "codex"
    assert live.call_args.kwargs["model"] == "gpt-5.6-sol"


def test_switchyard_benchmark_cli_maps_operational_errors_to_exit_two(tmp_path):
    from kew.switchyard_benchmark import BenchmarkError

    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)

    with patch(
        "kew.cli.run_live_switchyard_benchmark",
        side_effect=BenchmarkError("required tools unavailable"),
    ):
        result = CliRunner().invoke(
            app,
            [
                "switchyard",
                "benchmark",
                "--corpus",
                str(corpus_path),
                "--runner",
                "codex",
                "--model",
                "gpt-5.6-sol",
            ],
        )

    assert result.exit_code == 2


def test_switchyard_benchmark_cli_preserves_failed_report_exit_one(tmp_path):
    api = _benchmark_api()
    corpus_path = tmp_path / "corpus.json"
    _write_corpus(corpus_path)
    report = api["run_benchmark"](
        corpus=api["load_benchmark_corpus"](corpus_path),
        metadata=_metadata(api),
        execute=lambda mode, case: api["BenchmarkExecution"](
            response_text="wrong answer",
            latency_ms=1,
            usage=api["BenchmarkUsage"](),
        ),
        proxy_baseline=_metrics(0, model=0, tier=0, tokens=0, seconds=0),
        proxy_final=_metrics(3, model=3, tier=3, tokens=0, seconds=1),
        exclusive_use=True,
        unrelated_traffic=False,
    )

    with patch("kew.cli.run_live_switchyard_benchmark", return_value=report):
        result = CliRunner().invoke(
            app,
            [
                "switchyard",
                "benchmark",
                "--corpus",
                str(corpus_path),
                "--runner",
                "codex",
                "--model",
                "gpt-5.6-sol",
            ],
        )

    assert report.exit_code == 1
    assert result.exit_code == 1


def test_runner_terminal_totals_are_the_authoritative_usage_summary():
    from kew.runners.protocol import RUN_TOTALS_KEY, RunTotals, StreamEvent
    from kew.switchyard_benchmark import _runner_usage

    usage = _runner_usage(
        [
            StreamEvent(event_type="usage", raw_line="", input_tokens=99),
            StreamEvent(
                event_type="result",
                raw_line="",
                metadata={
                    RUN_TOTALS_KEY: RunTotals(
                        input_tokens=12,
                        output_tokens=4,
                        cache_read_tokens=3,
                        cost_usd=0.025,
                        model="runner-reported-model",
                    )
                },
            ),
        ],
        "requested-model",
    )

    assert usage.input_tokens == 12
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 3
    assert usage.cost_usd == 0.025
    assert usage.model == "runner-reported-model"


@pytest.mark.parametrize(
    ("mode", "route", "managed", "model", "reason"),
    [
        (
            "direct",
            "switchyard/passthrough",
            True,
            "gpt-5.6-sol",
            "requires Switchyard mode",
        ),
        (
            "switchyard",
            "switchyard/random",
            True,
            "gpt-5.6-sol",
            "requires switchyard/passthrough",
        ),
        (
            "switchyard",
            "switchyard/passthrough",
            False,
            "gpt-5.6-sol",
            "requires a dedicated managed Switchyard",
        ),
        (
            "switchyard",
            "switchyard/passthrough",
            True,
            " ",
            "requires an explicit model",
        ),
    ],
)
def test_live_contract_refuses_every_unsafe_configuration(
    tmp_path, mode, route, managed, model, reason
):
    from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
    from kew.switchyard_benchmark import _require_live_contract

    config_path = tmp_path / "switchyard.json"
    binary_path = tmp_path / "switchyard"
    config_path.write_text("{}")
    binary_path.write_text("binary")
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode=mode,
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route=route,
                expected_version="6ff491a",
                managed=managed,
                config_path=config_path if managed else None,
                binary=binary_path if managed else None,
                binary_sha256="a" * 64 if managed else "",
            ),
        )
    )

    with pytest.raises(ValueError, match=reason):
        _require_live_contract(config, object(), model)  # type: ignore[arg-type]


def test_live_benchmark_normalizes_preflight_exit():
    from kew.config import DispatchConfig
    from kew.switchyard_benchmark import BenchmarkError, run_live_switchyard_benchmark

    with (
        patch("kew.switchyard_benchmark.preflight_tools", side_effect=SystemExit(1)),
        pytest.raises(BenchmarkError, match="required tools are unavailable"),
    ):
        run_live_switchyard_benchmark(
            config=DispatchConfig(),
            corpus_path=Path("unused.json"),
            runner_name="codex",
            model="gpt-5.6-sol",
        )


def test_runner_timeout_bounds_cleanup_when_group_kill_fails(tmp_path):
    from kew.switchyard_benchmark import BenchmarkError, _capture_runner_output

    proc = MagicMock(pid=123)
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd="codex", timeout=3),
        subprocess.TimeoutExpired(cmd="codex", timeout=5),
        ("", None),
    ]
    with (
        patch("kew.switchyard_benchmark.popen", return_value=proc),
        patch("kew.switchyard_benchmark.os.getpgid", return_value=456),
        patch("kew.switchyard_benchmark.os.killpg", side_effect=PermissionError),
        pytest.raises(BenchmarkError, match="runner benchmark timed out"),
    ):
        _capture_runner_output(["codex"], directory=tmp_path, env={}, timeout_seconds=3)

    assert proc.communicate.call_args_list == [
        call(timeout=3),
        call(timeout=5),
        call(timeout=5),
    ]
    proc.kill.assert_called_once_with()


def test_runner_timeout_reports_bounded_cleanup_failure(tmp_path):
    from kew.switchyard_benchmark import BenchmarkError, _capture_runner_output

    proc = MagicMock(pid=123)
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="codex", timeout=5)
    with (
        patch("kew.switchyard_benchmark.popen", return_value=proc),
        patch("kew.switchyard_benchmark.os.getpgid", return_value=456),
        patch("kew.switchyard_benchmark.os.killpg"),
        pytest.raises(BenchmarkError, match="runner benchmark cleanup timed out"),
    ):
        _capture_runner_output(["codex"], directory=tmp_path, env={}, timeout_seconds=3)

    assert proc.communicate.call_args_list == [
        call(timeout=3),
        call(timeout=5),
        call(timeout=5),
    ]
