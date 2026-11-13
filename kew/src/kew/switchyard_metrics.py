"""Bounded, sanitized observations of Switchyard Prometheus metrics.

Switchyard exposes process-wide counters.  Raw snapshots are useful for proxy
diagnosis but unsuitable for per-run usage or billing.  A fresh managed proxy
can additionally produce a baseline/final run delta; runner-reported task usage
remains authoritative in either case.
"""

from __future__ import annotations

import json
import math
import re
import ssl
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Literal, Protocol
from urllib.parse import urlsplit

MetricFamily = Literal[
    "requests",
    "errors",
    "request_latency",
    "routing_overhead",
    "model_selection",
    "tier_selection",
    "tokens",
]

_MAX_METRICS_BYTES = 1024 * 1024
_MAX_METRIC_LINE_BYTES = 4096
_MAX_METRIC_SAMPLES = 10_000
_METRIC_NAME = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?=[{\s])")
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?:\s+\d+)?$"
)
_LABEL = re.compile(r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')
_HISTOGRAM_SUFFIX = re.compile(r"_(?:bucket|sum|count)$")
_DOCUMENTED_FAMILIES: dict[str, MetricFamily] = {
    "switchyard_requests_total": "requests",
    "switchyard_errors_total": "errors",
    "switchyard_request_duration_seconds": "request_latency",
    "switchyard_routing_overhead_seconds": "routing_overhead",
    "switchyard_model_selections_total": "model_selection",
    "switchyard_tier_selections_total": "tier_selection",
    # Switchyard names token counters per direction rather than carrying a
    # ``direction`` label: ``usage_metrics.rs`` registers switchyard.prompt_tokens
    # and switchyard.completion_tokens, which the Prometheus exporter renders
    # with the ``_total`` suffix. Both carry exactly one label, ``model``.
    "switchyard_prompt_tokens_total": "tokens",
    "switchyard_completion_tokens_total": "tokens",
}

# Direction for token families that encode it in the metric name (see above).
#
# ``switchyard_cached_tokens_total`` and ``switchyard_cache_creation_tokens_total``
# are deliberately absent: the proxy folds both into ``prompt_tokens`` before
# recording it, so admitting them here would double-count the prompt side and
# break the exact usage match this map exists to serve.
_TOKEN_DIRECTIONS: dict[str, str] = {
    "switchyard_prompt_tokens_total": "input",
    "switchyard_completion_tokens_total": "output",
}
_SAFE_LABELS = frozenset(
    {
        "direction",
        "error",
        "error_type",
        "le",
        "method",
        "model",
        "protocol",
        "provider",
        "route",
        "status",
        "tier",
        "token_type",
    }
)


class SwitchyardMetricsError(ValueError):
    """A sanitized metrics failure safe for CLI and audit-adjacent output."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MetricsResponse:
    """The bounded response surface accepted from an injected transport."""

    status_code: int
    text: str


class SwitchyardMetricsTransport(Protocol):
    """Injectable HTTP boundary for static tests and local proxy observation."""

    def get_text(self, url: str, timeout_seconds: float) -> MetricsResponse: ...


@dataclass(frozen=True)
class ProxyMetricSample:
    """One allowlisted sample from a documented Switchyard metric family."""

    family: MetricFamily
    name: str
    labels: dict[str, str]
    value: float

    def to_json(self) -> dict[str, object]:
        return {
            "family": self.family,
            "name": self.name,
            "labels": dict(sorted(self.labels.items())),
            "value": self.value,
        }


@dataclass(frozen=True)
class ProxyMetricsObservation:
    """A process-wide proxy snapshot that cannot be mistaken for run usage."""

    samples: tuple[ProxyMetricSample, ...]
    unavailable_reason: str | None = None
    kind: str = "switchyard_proxy_observation"
    scope: str = "process_wide_proxy"
    accounting_authority: str = "runner_reported_usage"
    per_run_attribution: bool = False

    @classmethod
    def unavailable(cls, reason: str) -> ProxyMetricsObservation:
        return cls(samples=(), unavailable_reason=reason)

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "scope": self.scope,
            "accounting_authority": self.accounting_authority,
            "per_run_attribution": self.per_run_attribution,
            "samples": [sample.to_json() for sample in self.samples],
        }
        if self.unavailable_reason is not None:
            payload["unavailable_reason"] = self.unavailable_reason
        return payload

    def selection_evidence(
        self,
        *,
        route: str | None = None,
        served_model: str | None = None,
        runner_input_tokens: int = 0,
        runner_output_tokens: int = 0,
    ) -> SwitchyardSelectionEvidence:
        """Extract bounded model/tier evidence without making it billing truth.

        ``/metrics`` is a counter surface.  This helper reports only identities
        and a separately labelled classifier-overhead observation; it never
        turns proxy token counters into runner task usage.
        When the runner has already reported a structured served model, that
        identity is used only to separate a classifier model from the served
        model.  Otherwise a model/tier is admitted only when the corresponding
        positive counter has one unambiguous label, or when one model's
        observed token totals uniquely match the runner-reported usage.
        """
        model_candidates = tuple(sorted(_selection_samples(self, "model", route)))
        tier_candidates = tuple(sorted(_selection_samples(self, "tier", route)))
        token_totals = _token_totals_by_model(self, route)
        selected, from_runner = _resolve_selected_model(
            served_model=served_model,
            model_candidates=model_candidates,
            token_totals=token_totals,
            runner_input_tokens=runner_input_tokens,
            runner_output_tokens=runner_output_tokens,
        )
        classifier_model, classifier_input, classifier_output = _classifier_overhead(
            route=route,
            selected=selected,
            token_totals=token_totals,
            runner_input_tokens=max(0, runner_input_tokens),
            runner_output_tokens=max(0, runner_output_tokens),
            per_run_attribution=self.per_run_attribution,
        )
        return SwitchyardSelectionEvidence(
            observation=self,
            selected_model=selected,
            selected_tier=tier_candidates[0] if len(tier_candidates) == 1 else None,
            model_candidates=model_candidates,
            tier_candidates=tier_candidates,
            # The source names the path that actually produced the identity,
            # never the shape of the raw argument: a served model the bounded
            # sanitizer rejected leaves proxy-derived evidence labelled proxy.
            selected_model_source=(
                "runner_reported" if from_runner else "switchyard_proxy_metrics"
            ),
            classifier_model=classifier_model,
            classifier_input_tokens=classifier_input,
            classifier_output_tokens=classifier_output,
        )


def managed_metrics_delta(
    baseline: ProxyMetricsObservation,
    final: ProxyMetricsObservation,
) -> ProxyMetricsObservation:
    """Return the positive counter delta for one owned managed process.

    Managed dispatch starts a fresh proxy and brackets all agent traffic with
    two snapshots.  Subtracting the baseline prevents startup probes or any
    pre-run activity from becoming selection/accounting evidence.  A missing
    snapshot or counter reset invalidates the whole delta: mixing pre- and
    post-reset samples could select and price the wrong served identity.
    """
    if baseline.unavailable_reason is not None:
        return _unavailable_managed_delta("baseline_unavailable")
    if final.unavailable_reason is not None:
        return _unavailable_managed_delta("final_unavailable")

    def keyed(
        observation: ProxyMetricsObservation,
    ) -> dict[tuple[str, str, tuple[tuple[str, str], ...]], ProxyMetricSample]:
        return {
            (sample.family, sample.name, tuple(sorted(sample.labels.items()))): sample
            for sample in observation.samples
        }

    before = keyed(baseline)
    after = keyed(final)
    if any(
        key not in after or after[key].value < sample.value
        for key, sample in before.items()
    ):
        return _unavailable_managed_delta("counter_reset")

    samples: list[ProxyMetricSample] = []
    for key, sample in sorted(after.items()):
        value = sample.value - before[key].value if key in before else sample.value
        if value > 0:
            samples.append(
                ProxyMetricSample(
                    family=sample.family,
                    name=sample.name,
                    labels=sample.labels,
                    value=value,
                )
            )
    return ProxyMetricsObservation(
        samples=tuple(samples),
        scope="managed_run_delta",
        per_run_attribution=True,
    )


def _unavailable_managed_delta(reason: str) -> ProxyMetricsObservation:
    """Return an invalid managed window without claiming run attribution."""
    return ProxyMetricsObservation(
        samples=(),
        unavailable_reason=reason,
        scope="managed_run_delta",
    )


@dataclass(frozen=True)
class SwitchyardSelectionEvidence:
    """Sanitized identity evidence derived from a proxy observation.

    The nested observation declares whether it is a raw process-wide snapshot
    or a bracketed managed-run delta.  The selected model and tier can reconcile
    an adaptive route's identity, but the runner's token/cost report remains
    the task accounting authority.
    """

    observation: ProxyMetricsObservation
    selected_model: str | None
    selected_tier: str | None
    model_candidates: tuple[str, ...] = ()
    tier_candidates: tuple[str, ...] = ()
    selected_model_source: str = "switchyard_proxy_metrics"
    classifier_model: str | None = None
    classifier_input_tokens: int = 0
    classifier_output_tokens: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "switchyard_selection_evidence",
            "selected_model": self.selected_model,
            "selected_model_source": self.selected_model_source,
            "selected_tier": self.selected_tier,
            "classifier_overhead": {
                "accounting_authority": "switchyard_proxy_observation",
                "input_tokens": self.classifier_input_tokens,
                "model": self.classifier_model,
                "output_tokens": self.classifier_output_tokens,
            },
            "proxy_observation": self.observation.to_json(),
        }


def _bounded_identity(value: object) -> str | None:
    from kew.runner_fields import reported_identity

    if not isinstance(value, str):
        return None
    value = reported_identity(value, "model")
    cleaned = "".join(char for char in value if char.isprintable()).strip()[:256]
    return reported_identity(cleaned, "model") or None


def _resolve_selected_model(
    *,
    served_model: str | None,
    model_candidates: tuple[str, ...],
    token_totals: dict[str, dict[str, int]],
    runner_input_tokens: int,
    runner_output_tokens: int,
) -> tuple[str | None, bool]:
    """Resolve the served model and report whether the runner supplied it.

    A runner can echo the transport alias when the provider response does not
    expose its final identity.  That is not served-model evidence and must not
    suppress an unambiguous proxy observation.
    """
    selected = _bounded_identity(served_model)
    if selected is not None and selected.startswith("switchyard/"):
        selected = None
    if selected is not None:
        return selected, True
    matched = _unique_usage_match(
        token_totals,
        max(0, runner_input_tokens),
        max(0, runner_output_tokens),
    )
    if matched is not None:
        return matched, False
    if len(model_candidates) == 1:
        return model_candidates[0], False
    return None, False


def _unique_usage_match(
    token_totals: dict[str, dict[str, int]],
    runner_input: int,
    runner_output: int,
) -> str | None:
    """Return the one model whose observed totals equal the runner's usage."""
    if not (runner_input or runner_output):
        return None
    matches = tuple(
        sorted(
            model
            for model, directions in token_totals.items()
            if directions.get("input", 0) == runner_input
            and directions.get("output", 0) == runner_output
        )
    )
    return matches[0] if len(matches) == 1 else None


def _classifier_overhead(
    *,
    route: str | None,
    selected: str | None,
    token_totals: dict[str, dict[str, int]],
    runner_input_tokens: int,
    runner_output_tokens: int,
    per_run_attribution: bool,
) -> tuple[str | None, int, int]:
    """Identify separately evidenced classifier traffic on a classifier route.

    The classifier model is admitted only when exactly one other model carries
    observed tokens, so overhead is never inferred from the served model.
    """
    if selected is None or not route or "classifier" not in route.lower():
        return None, 0, 0
    other_models = tuple(
        sorted(
            model
            for model, directions in token_totals.items()
            if model != selected
            and (directions.get("input", 0) or directions.get("output", 0))
        )
    )
    if len(other_models) == 1:
        return _other_model_classifier_overhead(other_models[0], token_totals)
    return _same_model_classifier_overhead(
        selected=selected,
        token_totals=token_totals,
        runner_input_tokens=runner_input_tokens,
        runner_output_tokens=runner_output_tokens,
        per_run_attribution=per_run_attribution,
        ambiguous_other_models=bool(other_models),
    )


def _other_model_classifier_overhead(
    classifier: str,
    token_totals: dict[str, dict[str, int]],
) -> tuple[str, int, int]:
    """Return independently labelled classifier traffic."""
    return (
        classifier,
        token_totals[classifier].get("input", 0),
        token_totals[classifier].get("output", 0),
    )


def _same_model_classifier_overhead(
    *,
    selected: str,
    token_totals: dict[str, dict[str, int]],
    runner_input_tokens: int,
    runner_output_tokens: int,
    per_run_attribution: bool,
    ambiguous_other_models: bool,
) -> tuple[str | None, int, int]:
    """Resolve classifier residual when weak task and classifier share a model."""
    if (
        ambiguous_other_models
        or not per_run_attribution
        or not (runner_input_tokens or runner_output_tokens)
    ):
        return None, 0, 0

    # The weak task model can also be the classifier model.  Only a managed
    # run delta can safely attribute the residual above runner-authoritative
    # task usage to that same identity; a process-wide snapshot cannot.
    selected_totals = token_totals.get(selected, {})
    observed_input = selected_totals.get("input", 0)
    observed_output = selected_totals.get("output", 0)
    if observed_input < runner_input_tokens or observed_output < runner_output_tokens:
        return None, 0, 0
    classifier_input = observed_input - runner_input_tokens
    classifier_output = observed_output - runner_output_tokens
    if not (classifier_input or classifier_output):
        return None, 0, 0
    return selected, classifier_input, classifier_output


def _selection_samples(
    observation: ProxyMetricsObservation,
    label: str,
    route: str | None,
) -> set[str]:
    values: set[str] = set()
    family = "model_selection" if label == "model" else "tier_selection"
    for sample in observation.samples:
        if sample.family != family:
            continue
        if sample.value <= 0:
            continue
        if route is not None and sample.labels.get("route") not in {None, route}:
            continue
        value = _bounded_identity(sample.labels.get(label))
        if value is not None:
            values.add(value)
    return values


def _token_totals_by_model(
    observation: ProxyMetricsObservation,
    route: str | None = None,
) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for sample in observation.samples:
        if sample.family != "tokens" or sample.value <= 0:
            continue
        if route is not None and sample.labels.get("route") not in {None, route}:
            continue
        model = _bounded_identity(sample.labels.get("model"))
        direction = (
            sample.labels.get("direction")
            or sample.labels.get("token_type")
            or _TOKEN_DIRECTIONS.get(sample.name)
        )
        if model is None or direction not in {"input", "output"}:
            continue
        bucket = totals.setdefault(model, {"input": 0, "output": 0})
        # Prometheus exposes counters as floats.  Truncating a value such as
        # 119.9999999 would break the exact usage match against runner-reported
        # integer token counts, so round to the nearest whole token.
        bucket[direction] += max(0, round(sample.value))
    return totals


class HttpSwitchyardMetricsTransport:
    """Small direct client that neither proxies nor follows redirects."""

    def get_text(self, url: str, timeout_seconds: float) -> MetricsResponse:
        parts = urlsplit(url)
        host = parts.hostname
        if (
            host is None
            or parts.scheme not in {"http", "https"}
            or any((parts.username, parts.password, parts.query, parts.fragment))
            or parts.path != "/metrics"
        ):
            raise SwitchyardMetricsError("invalid_endpoint")
        connection_type = HTTPSConnection if parts.scheme == "https" else HTTPConnection
        connection = connection_type(host, parts.port, timeout=timeout_seconds)
        try:
            connection.request("GET", parts.path)
            response = connection.getresponse()
            payload = response.read(_MAX_METRICS_BYTES + 1)
            if len(payload) > _MAX_METRICS_BYTES:
                raise SwitchyardMetricsError("response_too_large")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SwitchyardMetricsError("malformed_metrics") from exc
            return MetricsResponse(status_code=response.status, text=text)
        finally:
            connection.close()


def _metric_family(name: str) -> MetricFamily | None:
    """Map only documented Switchyard semantic families; ignore runtime noise."""
    base = _HISTOGRAM_SUFFIX.sub("", name)
    return _DOCUMENTED_FAMILIES.get(base)


def _metric_family_from_line(line: str) -> MetricFamily | None:
    """Identify a family without parsing an undocumented sample's contents."""
    match = _METRIC_NAME.match(line)
    if match is None:
        raise SwitchyardMetricsError("malformed_metrics")
    return _metric_family(match.group("name"))


def _parse_labels(raw: str | None) -> dict[str, str]:
    if raw is None or raw == "":
        return {}
    labels: dict[str, str] = {}
    position = 0
    while position < len(raw):
        match = _LABEL.match(raw, position)
        if match is None:
            raise SwitchyardMetricsError("malformed_metrics")
        name = match.group("name")
        if name in _SAFE_LABELS:
            try:
                value = json.loads(f'"{match.group("value")}"')
            except json.JSONDecodeError as exc:
                raise SwitchyardMetricsError("malformed_metrics") from exc
            if not isinstance(value, str):
                raise SwitchyardMetricsError("malformed_metrics")
            from kew.runner_fields import reported_identity

            labels[name] = reported_identity(value, name)[:256]
        position = match.end()
        if position == len(raw):
            break
        if raw[position] != ",":
            raise SwitchyardMetricsError("malformed_metrics")
        position += 1
    return labels


def parse_switchyard_metrics(text: str) -> ProxyMetricsObservation:
    """Parse a bounded Prometheus snapshot into allowlisted proxy observations."""
    if len(text.encode("utf-8")) > _MAX_METRICS_BYTES:
        raise SwitchyardMetricsError("response_too_large")
    samples: list[ProxyMetricSample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.encode("utf-8")) > _MAX_METRIC_LINE_BYTES:
            raise SwitchyardMetricsError("malformed_metrics")
        family = _metric_family_from_line(line)
        if family is None:
            continue
        match = _SAMPLE.fullmatch(line)
        if match is None:
            raise SwitchyardMetricsError("malformed_metrics")
        value = float(match.group("value"))
        if not math.isfinite(value):
            raise SwitchyardMetricsError("malformed_metrics")
        samples.append(
            ProxyMetricSample(
                family=family,
                name=match.group("name"),
                labels=_parse_labels(match.group("labels")),
                value=value,
            )
        )
        if len(samples) > _MAX_METRIC_SAMPLES:
            raise SwitchyardMetricsError("response_too_large")
    return ProxyMetricsObservation(samples=tuple(samples))


def observe_switchyard_metrics(
    *,
    endpoint: str,
    transport: SwitchyardMetricsTransport | None = None,
    timeout_seconds: float = 5.0,
) -> ProxyMetricsObservation:
    """Fetch one process-wide snapshot, returning only sanitized known families."""
    metrics_url = _metrics_url(endpoint)
    active_transport = transport or HttpSwitchyardMetricsTransport()
    response = _fetch_metrics(active_transport, metrics_url, timeout_seconds)
    if response.status_code != 200:
        raise SwitchyardMetricsError("metrics_unavailable")
    return parse_switchyard_metrics(response.text)


def _metrics_url(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https"} or parts.hostname is None:
        raise SwitchyardMetricsError("invalid_endpoint")
    if any((parts.username, parts.password, parts.path, parts.query, parts.fragment)):
        raise SwitchyardMetricsError("invalid_endpoint")
    return f"{endpoint}/metrics"


def _fetch_metrics(
    transport: SwitchyardMetricsTransport, url: str, timeout_seconds: float
) -> MetricsResponse:
    try:
        return transport.get_text(url, timeout_seconds=timeout_seconds)
    except SwitchyardMetricsError:
        raise
    except ssl.SSLError as exc:
        raise SwitchyardMetricsError("tls_error") from exc
    except (OSError, TimeoutError, ValueError, TypeError, HTTPException) as exc:
        raise SwitchyardMetricsError("unreachable") from exc
