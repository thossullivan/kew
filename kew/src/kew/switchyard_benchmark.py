"""Deterministic direct-versus-Switchyard passthrough benchmark evidence.

The pure harness accepts an injected executor so unit tests never launch a
runner, proxy, or provider request.  The live adapter is deliberately stricter:
it accepts only a managed passthrough route and owns the proxy for the complete
metrics window.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kew import work_dirs
from kew.config import DispatchConfig, InferenceTransportConfig
from kew.inference_transport import resolve_inference_transport
from kew.runners.protocol import RUN_TOTALS_KEY, AgentRunner, RunTotals, StreamEvent
from kew.subprocess_utils import inspect_runner, popen, preflight_tools, run
from kew.switchyard_classifier import ClassifierEvidence
from kew.switchyard_metrics import (
    ProxyMetricSample,
    ProxyMetricsObservation,
    SwitchyardMetricsError,
    observe_switchyard_metrics,
)
from kew.switchyard_supervisor import (
    ManagedSwitchyardSupervisor,
    SwitchyardSupervisorError,
)
from kew.usage import accumulate_usage

if TYPE_CHECKING:
    from collections.abc import Generator

BenchmarkMode = Literal["direct", "passthrough"]
BenchmarkExecutor = Callable[[BenchmarkMode, "BenchmarkCase"], "BenchmarkExecution"]

_MAX_CORPUS_BYTES = 1024 * 1024
_MAX_CASES = 100
_MAX_PROMPT_CHARS = 100_000
_MAX_RUNNER_OUTPUT_BYTES = 2 * 1024 * 1024
_RUNNER_CLEANUP_TIMEOUT_SECONDS = 5


class BenchmarkError(ValueError):
    """A sanitized refusal safe to show at the CLI boundary."""


@dataclass(frozen=True)
class QualityCheck:
    """A named, local assertion over the runner's response text."""

    name: str
    contains: str


@dataclass(frozen=True)
class BenchmarkCase:
    """One immutable prompt and its expected proxy request count."""

    case_id: str
    prompt: str
    quality_checks: tuple[QualityCheck, ...]
    expected_proxy_requests: int = 1


@dataclass(frozen=True)
class BenchmarkCorpus:
    """A fixed corpus identified by the SHA-256 of its exact file bytes."""

    name: str
    cases: tuple[BenchmarkCase, ...]
    sha256: str
    schema_version: int = 1


@dataclass(frozen=True)
class BenchmarkUsage:
    """Runner-reported usage; proxy metrics are never substituted here."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    model: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "accounting_authority": "runner_reported_usage",
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "model": self.model,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True)
class BenchmarkExecution:
    """Sanitized result of one runner execution.

    ``response_text`` is held only long enough to evaluate quality checks and is
    intentionally absent from report serialization.
    """

    response_text: str
    latency_ms: int
    usage: BenchmarkUsage
    succeeded: bool = True
    failure_reason: str | None = None
    classifier_evidence: ClassifierEvidence | None = None


@dataclass(frozen=True)
class BenchmarkMetadata:
    """Non-secret identities and digests bound to the comparison."""

    runner: str
    requested_model: str
    runner_version: str
    switchyard_version: str
    direct_config_sha256: str
    passthrough_config_sha256: str
    switchyard_config_sha256: str | None = None


@dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    mode: BenchmarkMode
    quality_checks: tuple[tuple[str, bool], ...]
    latency_ms: int
    usage: BenchmarkUsage
    succeeded: bool
    failure_reason: str | None
    classifier_evidence: ClassifierEvidence | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "classifier_evidence": (
                self.classifier_evidence.to_json()
                if self.classifier_evidence is not None
                else None
            ),
            "failure_reason": self.failure_reason,
            "latency_ms": self.latency_ms,
            "mode": self.mode,
            "quality_checks": [
                {"name": name, "passed": passed} for name, passed in self.quality_checks
            ],
            "runner_usage": self.usage.to_json(),
            "succeeded": self.succeeded,
        }


@dataclass(frozen=True)
class ProxyWindowEvidence:
    valid: bool
    invalid_reason: str | None
    expected_requests: int
    observed_requests: int | float
    samples: tuple[ProxyMetricSample, ...]

    def _family(self, family: str) -> list[dict[str, object]]:
        if not self.valid:
            return []
        return [
            {"labels": dict(sorted(sample.labels.items())), "value": sample.value}
            for sample in self.samples
            if sample.family == family and sample.value != 0
        ]

    def to_json(self) -> dict[str, object]:
        return {
            "accounting_authority": "runner_reported_usage",
            "expected_requests": self.expected_requests,
            "invalid_reason": self.invalid_reason,
            "model_selections": self._family("model_selection"),
            "observed_requests": self.observed_requests,
            "samples": [sample.to_json() for sample in self.samples]
            if self.valid
            else [],
            "scope": "exclusive_process_window",
            "tier_selections": self._family("tier_selection"),
            "valid": self.valid,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    corpus: BenchmarkCorpus
    metadata: BenchmarkMetadata
    results: tuple[BenchmarkResult, ...]
    proxy_observations: ProxyWindowEvidence

    @property
    def exit_code(self) -> int:
        checks_passed = all(
            result.succeeded and all(passed for _, passed in result.quality_checks)
            for result in self.results
        )
        return 0 if checks_passed and self.proxy_observations.valid else 1

    def to_json(self) -> dict[str, object]:
        return {
            "adaptive_routing_enabled": False,
            "config_digests": {
                "direct_sha256": self.metadata.direct_config_sha256,
                "passthrough_sha256": self.metadata.passthrough_config_sha256,
                "switchyard_route_config_sha256": (
                    self.metadata.switchyard_config_sha256
                ),
            },
            "corpus": {
                "case_count": len(self.corpus.cases),
                "name": self.corpus.name,
                "sha256": self.corpus.sha256,
            },
            "exit_code": self.exit_code,
            "proxy_observations": self.proxy_observations.to_json(),
            "requested_model": self.metadata.requested_model,
            "results": [result.to_json() for result in self.results],
            "runner": self.metadata.runner,
            "schema_version": 1,
            "versions": {
                "runner": _version_identity(self.metadata.runner_version),
                "switchyard": _version_identity(self.metadata.switchyard_version),
            },
        }


def _version_identity(version: str) -> dict[str, str]:
    return {
        "sha256": hashlib.sha256(version.encode()).hexdigest(),
        "value": version,
    }


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BenchmarkError(f"{description} must be an object")
    return value


def _required_text(payload: dict[str, object], key: str, description: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{description} {key} must be non-empty text")
    return value


def _parse_quality_checks(value: object, case_id: str) -> tuple[QualityCheck, ...]:
    if not isinstance(value, list) or not value:
        raise BenchmarkError(f"case {case_id!r} requires quality_checks")
    checks: list[QualityCheck] = []
    names: set[str] = set()
    for raw_check in value:
        check = _object(raw_check, "quality check")
        if set(check) != {"name", "contains"}:
            raise BenchmarkError("quality check has unsupported fields")
        name = _required_text(check, "name", "quality check")
        contains = _required_text(check, "contains", "quality check")
        if name in names:
            raise BenchmarkError(f"duplicate quality check {name!r}")
        names.add(name)
        checks.append(QualityCheck(name=name, contains=contains))
    return tuple(checks)


def _expected_proxy_requests(item: dict[str, object], case_id: str) -> int:
    expected = item.get("expected_proxy_requests", 1)
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise BenchmarkError(
            f"case {case_id!r} expected_proxy_requests must be positive"
        )
    return expected


def _parse_case(item: dict[str, object], case_id: str) -> BenchmarkCase:
    allowed = {"id", "prompt", "quality_checks", "expected_proxy_requests"}
    if not set(item).issubset(allowed):
        raise BenchmarkError(f"case {case_id!r} has unsupported fields")
    prompt = _required_text(item, "prompt", f"case {case_id!r}")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise BenchmarkError(f"case {case_id!r} prompt exceeds size limit")
    return BenchmarkCase(
        case_id=case_id,
        prompt=prompt,
        quality_checks=_parse_quality_checks(item.get("quality_checks"), case_id),
        expected_proxy_requests=_expected_proxy_requests(item, case_id),
    )


def _parse_cases(value: object) -> tuple[BenchmarkCase, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_CASES:
        raise BenchmarkError(f"corpus cases must contain 1-{_MAX_CASES} entries")
    objects = [_object(item, "corpus case") for item in value]
    ids = [_required_text(item, "id", "corpus case") for item in objects]
    duplicate = next((case_id for case_id in ids if ids.count(case_id) > 1), None)
    if duplicate is not None:
        raise BenchmarkError(f"duplicate case id {duplicate!r}")
    return tuple(
        _parse_case(item, case_id) for case_id, item in zip(ids, objects, strict=True)
    )


def load_benchmark_corpus(path: str | os.PathLike[str]) -> BenchmarkCorpus:
    """Read and validate a fixed corpus exactly once."""
    corpus_path = Path(path)
    try:
        raw = corpus_path.read_bytes()
    except OSError as exc:
        raise BenchmarkError("benchmark corpus is unreadable") from exc
    if not raw or len(raw) > _MAX_CORPUS_BYTES:
        raise BenchmarkError("benchmark corpus exceeds size limit")
    try:
        payload = _object(json.loads(raw), "benchmark corpus")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BenchmarkError("benchmark corpus is malformed JSON") from exc
    if set(payload) != {"schema_version", "name", "cases"}:
        raise BenchmarkError("benchmark corpus has unsupported fields")
    if payload["schema_version"] != 1:
        raise BenchmarkError("benchmark corpus schema_version must be 1")
    return BenchmarkCorpus(
        name=_required_text(payload, "name", "benchmark corpus"),
        cases=_parse_cases(payload["cases"]),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _sample_key(sample: ProxyMetricSample) -> tuple[str, tuple[tuple[str, str], ...]]:
    return sample.name, tuple(sorted(sample.labels.items()))


def _proxy_deltas(
    baseline: ProxyMetricsObservation, final: ProxyMetricsObservation
) -> tuple[tuple[ProxyMetricSample, ...], bool]:
    before = {_sample_key(sample): sample for sample in baseline.samples}
    after = {_sample_key(sample): sample for sample in final.samples}
    deltas: list[ProxyMetricSample] = []
    counter_reset = False
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        value = (new.value if new else 0.0) - (old.value if old else 0.0)
        if value < 0:
            counter_reset = True
        sample = new or old
        assert sample is not None
        deltas.append(
            ProxyMetricSample(
                family=sample.family,
                name=sample.name,
                labels=sample.labels,
                value=value,
            )
        )
    return tuple(deltas), counter_reset


def _request_count(samples: tuple[ProxyMetricSample, ...]) -> int | float:
    value = sum(sample.value for sample in samples if sample.family == "requests")
    return int(value) if value.is_integer() else value


def _proxy_window(
    *,
    baseline: ProxyMetricsObservation,
    final: ProxyMetricsObservation,
    expected_requests: int,
    exclusive_use: bool,
    unrelated_traffic: bool,
) -> ProxyWindowEvidence:
    deltas, counter_reset = _proxy_deltas(baseline, final)
    observed = _request_count(deltas)
    reason: str | None = None
    if not exclusive_use:
        reason = "exclusive_use_not_proven"
    elif unrelated_traffic:
        reason = "unrelated_traffic_detected"
    elif counter_reset:
        reason = "proxy_counter_reset"
    elif observed != expected_requests:
        reason = "request_count_mismatch"
    return ProxyWindowEvidence(
        valid=reason is None,
        invalid_reason=reason,
        expected_requests=expected_requests,
        observed_requests=observed,
        samples=deltas if reason is None else (),
    )


def _evaluate(
    mode: BenchmarkMode, case: BenchmarkCase, execution: BenchmarkExecution
) -> BenchmarkResult:
    return BenchmarkResult(
        case_id=case.case_id,
        mode=mode,
        quality_checks=tuple(
            (check.name, check.contains in execution.response_text)
            for check in case.quality_checks
        ),
        latency_ms=max(0, execution.latency_ms),
        usage=execution.usage,
        succeeded=execution.succeeded,
        failure_reason=execution.failure_reason,
        classifier_evidence=execution.classifier_evidence,
    )


def run_benchmark(
    *,
    corpus: BenchmarkCorpus,
    metadata: BenchmarkMetadata,
    execute: BenchmarkExecutor,
    proxy_baseline: ProxyMetricsObservation,
    proxy_final: ProxyMetricsObservation,
    exclusive_use: bool,
    unrelated_traffic: bool,
) -> BenchmarkReport:
    """Run direct first, then passthrough, one case at a time."""
    modes: tuple[BenchmarkMode, ...] = ("direct", "passthrough")
    results = tuple(
        _evaluate(mode, case, execute(mode, case))
        for mode in modes
        for case in corpus.cases
    )
    expected = sum(case.expected_proxy_requests for case in corpus.cases)
    return BenchmarkReport(
        corpus=corpus,
        metadata=metadata,
        results=results,
        proxy_observations=_proxy_window(
            baseline=proxy_baseline,
            final=proxy_final,
            expected_requests=expected,
            exclusive_use=exclusive_use,
            unrelated_traffic=unrelated_traffic,
        ),
    )


def render_benchmark_json(report: BenchmarkReport) -> str:
    """Serialize without timestamps, host paths, prompts, or raw responses."""
    return json.dumps(report.to_json(), sort_keys=True, separators=(",", ":")) + "\n"


def _run_totals(events: list[StreamEvent]) -> RunTotals | None:
    for event in reversed(events):
        totals = event.metadata.get(RUN_TOTALS_KEY)
        if isinstance(totals, RunTotals):
            return totals
    return None


def _native_cost(events: list[StreamEvent]) -> float | None:
    costs = [
        float(cost)
        for event in events
        if event.event_type == "step"
        and isinstance((cost := event.metadata.get("cost_usd")), (int, float))
    ]
    if costs:
        return sum(costs)
    for event in reversed(events):
        cost = event.metadata.get("cost_usd")
        if isinstance(cost, (int, float)):
            return float(cost)
    return None


def _runner_usage(events: list[StreamEvent], requested_model: str) -> BenchmarkUsage:
    accumulated = accumulate_usage(events)
    totals = _run_totals(events)
    if totals is not None:
        return BenchmarkUsage(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cache_creation_tokens=totals.cache_creation_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cost_usd=totals.cost_usd,
            model=totals.model or requested_model,
        )
    model = next(
        (
            str(value)
            for event in events
            if event.event_type == "init" and (value := event.metadata.get("model"))
        ),
        requested_model,
    )
    return BenchmarkUsage(
        input_tokens=accumulated.input_tokens,
        output_tokens=accumulated.output_tokens,
        cache_creation_tokens=accumulated.cache_creation_tokens,
        cache_read_tokens=accumulated.cache_read_tokens,
        cost_usd=_native_cost(events),
        model=model,
    )


def _runner_environment(runner: AgentRunner) -> dict[str, str]:
    if runner.name == "codex":
        from kew.runners.codex import codex_process_env

        return codex_process_env()
    return dict(os.environ)


def _terminate(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    try:
        proc.communicate(timeout=_RUNNER_CLEANUP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.communicate(timeout=_RUNNER_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError("runner benchmark cleanup timed out") from exc


def _capture_runner_output(
    command: list[str],
    *,
    directory: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, str, int]:
    started = time.monotonic()
    try:
        proc = popen(command, cwd=directory, env=env, start_new_session=True)
    except (OSError, ValueError) as exc:
        raise BenchmarkError("runner launch failed") from exc
    try:
        stdout, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate(proc)
        raise BenchmarkError("runner benchmark timed out") from exc
    raw = stdout or ""
    if len(raw.encode()) > _MAX_RUNNER_OUTPUT_BYTES:
        raise BenchmarkError("runner benchmark output exceeded size limit")
    latency_ms = round((time.monotonic() - started) * 1000)
    return proc.returncode or 0, raw, latency_ms


def _parse_runner_events(runner: AgentRunner, lines: list[str]) -> list[StreamEvent]:
    from kew.progress import display_suppressed

    with display_suppressed():
        return [event for line in lines for event in runner.parse_line(line)]


def _runner_cleanup_complete(runner: AgentRunner, session_id: str | None) -> bool:
    if runner.name != "codex":
        return True
    from kew.canary_live import delete_codex_session

    return delete_codex_session(session_id).status == "pass"


def _execution_failure(
    *, exit_code: int, category: str, cleanup_complete: bool
) -> str | None:
    if not cleanup_complete:
        return "runner_session_cleanup_failed"
    if exit_code != 0 or category:
        return category or f"runner_exit_{exit_code}"
    return None


class _LiveExecutor:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        direct_config: DispatchConfig,
        passthrough_config: DispatchConfig,
        requested_model: str,
        timeout_seconds: int,
    ) -> None:
        self.runner = runner
        self.configs = {
            "direct": direct_config,
            "passthrough": passthrough_config,
        }
        self.models = {
            "direct": requested_model,
            "passthrough": passthrough_config.inference_transport.switchyard.route,
        }
        self.requested_model = requested_model
        self.timeout_seconds = timeout_seconds

    def __call__(self, mode: BenchmarkMode, case: BenchmarkCase) -> BenchmarkExecution:
        config = self.configs[mode]
        env = _runner_environment(self.runner)
        model = self.models[mode]
        try:
            command = self.runner.build_command(
                prompt=case.prompt, config=config, env=env, model=model
            )
        except (OSError, ValueError, KeyError) as exc:
            raise BenchmarkError("runner command construction failed") from exc
        directory = work_dirs.make_work_dir("kew-switchyard-benchmark-")
        try:
            if self.runner.name == "codex":
                initialized = run(["git", "init"], cwd=directory, check=False)
                if initialized.returncode != 0:
                    raise BenchmarkError("benchmark checkout initialization failed")
            return self._communicate(command, config, directory, env)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _communicate(
        self,
        command: list[str],
        config: DispatchConfig,
        directory: Path,
        env: dict[str, str],
    ) -> BenchmarkExecution:
        exit_code, raw, latency_ms = _capture_runner_output(
            command,
            directory=directory,
            env=env,
            timeout_seconds=self.timeout_seconds,
        )
        lines = raw.splitlines()
        events = _parse_runner_events(self.runner, lines)
        classification = self.runner.classify_result(
            exit_code=exit_code,
            last_output=next((line for line in reversed(lines) if line.strip()), None),
            config=config,
        )
        response = "\n".join(
            event.message for event in events if event.event_type == "activity"
        )
        failure_reason = _execution_failure(
            exit_code=exit_code,
            category=classification.category,
            cleanup_complete=_runner_cleanup_complete(
                self.runner, classification.session_id
            ),
        )
        return BenchmarkExecution(
            response_text=response,
            latency_ms=latency_ms,
            usage=_runner_usage(events, self.requested_model),
            succeeded=failure_reason is None,
            failure_reason=failure_reason,
        )


def _snapshot(endpoint: str) -> ProxyMetricsObservation:
    try:
        return observe_switchyard_metrics(endpoint=endpoint, timeout_seconds=1.5)
    except SwitchyardMetricsError as exc:
        raise BenchmarkError(f"Switchyard metrics unavailable: {exc.reason}") from exc


def _requests(observation: ProxyMetricsObservation) -> float:
    return sum(
        sample.value for sample in observation.samples if sample.family == "requests"
    )


@contextmanager
def _exclusive_endpoint_lock(endpoint: str) -> Generator[None]:
    """Prevent overlapping Kew benchmarks for the configured endpoint."""
    digest = hashlib.sha256(endpoint.encode()).hexdigest()[:24]
    lock_path = work_dirs.lock_path(f"kew-switchyard-{digest}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise BenchmarkError("Switchyard exclusive-use lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BenchmarkError("Switchyard endpoint is already in use") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _require_live_contract(
    config: DispatchConfig, runner: AgentRunner, model: str
) -> tuple[DispatchConfig, str, str]:
    settings = config.inference_transport.switchyard
    if config.inference_transport.mode != "switchyard":
        raise BenchmarkError("benchmark requires Switchyard mode")
    if settings.route != "switchyard/passthrough":
        raise BenchmarkError("benchmark requires switchyard/passthrough")
    if not settings.managed:
        raise BenchmarkError("benchmark requires a dedicated managed Switchyard")
    if not model.strip():
        raise BenchmarkError("benchmark requires an explicit model")
    direct = config.model_copy(
        update={"inference_transport": InferenceTransportConfig(mode="direct")}
    )
    direct_resolution = resolve_inference_transport(direct, runner, model)
    passthrough_resolution = resolve_inference_transport(config, runner, model)
    return (
        direct,
        direct_resolution.config_sha256,
        passthrough_resolution.config_sha256,
    )


def _require_runner(
    config: DispatchConfig, runner_name: str
) -> tuple[AgentRunner, str]:
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.codex import CodexRunner

    factories: dict[str, type[AgentRunner]] = {
        "claude-code": ClaudeCodeRunner,
        "codex": CodexRunner,
    }
    factory = factories.get(runner_name)
    if factory is None:
        raise BenchmarkError("benchmark runner lacks proven passthrough support")
    runner = factory()
    reachability = inspect_runner(config, runner_name=runner_name)
    if not reachability.binary_present:
        raise BenchmarkError("benchmark runner binary is unavailable")
    if runner_name == "codex" and reachability.version_supported is not True:
        raise BenchmarkError("benchmark runner version is unsupported")
    if reachability.credential_detected is False:
        raise BenchmarkError("benchmark runner authentication is unavailable")
    return runner, reachability.version or "unknown"


def run_live_switchyard_benchmark(
    *,
    config: DispatchConfig,
    corpus_path: Path,
    runner_name: str,
    model: str,
) -> BenchmarkReport:
    """Run a human-invoked benchmark with one owned, idle managed proxy."""
    try:
        preflight_tools()
    except SystemExit as exc:
        raise BenchmarkError("required tools are unavailable") from exc
    corpus = load_benchmark_corpus(corpus_path)
    runner, runner_version = _require_runner(config, runner_name)
    direct, direct_digest, passthrough_digest = _require_live_contract(
        config, runner, model
    )
    executions: dict[tuple[BenchmarkMode, str], BenchmarkExecution] = {}
    settings = config.inference_transport.switchyard
    unrelated = False
    try:
        with (
            _exclusive_endpoint_lock(settings.endpoint),
            ManagedSwitchyardSupervisor(
                settings,
                classifier_ingress_format=(
                    "openai_responses"
                    if runner_name == "codex"
                    else "anthropic_messages"
                ),
            ) as supervisor,
        ):
            executor = _LiveExecutor(
                runner=runner,
                direct_config=direct,
                passthrough_config=config,
                requested_model=model,
                timeout_seconds=config.agent.timeout_minutes * 60,
            )
            baseline_check = _snapshot(settings.endpoint)
            for case in corpus.cases:
                executions[("direct", case.case_id)] = executor("direct", case)
            baseline = _snapshot(settings.endpoint)
            unrelated = _requests(baseline_check) != _requests(baseline)
            for case in corpus.cases:
                executions[("passthrough", case.case_id)] = executor(
                    "passthrough", case
                )
            final = _snapshot(settings.endpoint)
            final_check = _snapshot(settings.endpoint)
            unrelated = unrelated or _requests(final) != _requests(final_check)
            metadata = BenchmarkMetadata(
                runner=runner_name,
                requested_model=model,
                runner_version=runner_version,
                switchyard_version=supervisor.observed_version or "unknown",
                direct_config_sha256=direct_digest,
                passthrough_config_sha256=passthrough_digest,
                switchyard_config_sha256=supervisor.config_sha256,
            )
        classifier_records = supervisor.classifier_evidence
        if len(classifier_records) == len(corpus.cases):
            for case, evidence in zip(corpus.cases, classifier_records, strict=True):
                key: tuple[BenchmarkMode, str] = ("passthrough", case.case_id)
                executions[key] = replace(executions[key], classifier_evidence=evidence)
    except SwitchyardSupervisorError as exc:
        raise BenchmarkError(str(exc)) from exc

    return run_benchmark(
        corpus=corpus,
        metadata=metadata,
        execute=lambda mode, case: executions[(mode, case.case_id)],
        proxy_baseline=baseline,
        proxy_final=final,
        exclusive_use=True,
        unrelated_traffic=unrelated,
    )
