"""Runner-agnostic OTLP/JSON metrics envelope walker (#256).

Walks the stable CNCF OTLP shape
resourceMetrics -> scopeMetrics -> metrics -> {sum|gauge}.dataPoints
into a flat list of DataPoints. Knows nothing about Claude Code's
vocabulary — that lives in ingest/claude_code.py. Fail-soft throughout:
malformed levels are skipped, never raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _as_list(value: object) -> list:  # type: ignore[type-arg]
    """Return value unchanged if it is already a list, otherwise return []."""
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class DataPoint:
    metric_name: str
    attributes: dict[str, str]
    value: float
    temporality: int  # OTLP enum: 0 unspecified, 1 delta, 2 cumulative
    time_unix_nano: str


def _attr_value(value: object) -> str | None:
    """Unwrap a typed OTLP attribute value into a string, or None."""
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return str(value[key])
    return None


def _attributes(raw: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        val = _attr_value(item.get("value"))
        if isinstance(key, str) and val is not None:
            out[key] = val
    return out


def _point_value(dp: dict[str, Any]) -> float | None:
    """Coerce asInt (JSON string) or asDouble (JSON number) to float."""
    if "asInt" in dp:
        try:
            return float(int(dp["asInt"]))
        except (TypeError, ValueError):
            return None
    if "asDouble" in dp:
        try:
            return float(dp["asDouble"])
        except (TypeError, ValueError):
            return None
    return None


def _extract_body(metric: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Return (dataPoints body, temporality) from a sum or gauge metric."""
    if isinstance(metric.get("sum"), dict):
        body: dict[str, Any] = metric["sum"]
        raw_t = body.get("aggregationTemporality", 0) or 0
        return body, int(raw_t) if isinstance(raw_t, int) else 0
    if isinstance(metric.get("gauge"), dict):
        return metric["gauge"], 0
    return None, 0


def _walk_metric(metric: dict[str, Any]) -> list[DataPoint]:
    name = metric.get("name")
    if not isinstance(name, str):
        return []
    body, temporality = _extract_body(metric)
    if body is None:
        return []
    points: list[DataPoint] = []
    for dp in _as_list(body.get("dataPoints")):
        if not isinstance(dp, dict):
            continue
        value = _point_value(dp)
        if value is None:
            continue
        points.append(
            DataPoint(
                metric_name=name,
                attributes=_attributes(dp.get("attributes")),
                value=value,
                temporality=temporality,
                time_unix_nano=str(dp.get("timeUnixNano", "")),
            )
        )
    return points


def _walk_scope(sm: dict[str, Any]) -> list[DataPoint]:
    """Flatten one scopeMetrics entry into DataPoints."""
    out: list[DataPoint] = []
    for metric in _as_list(sm.get("metrics")):
        if isinstance(metric, dict):
            out.extend(_walk_metric(metric))
    return out


def walk_metrics(doc: object) -> list[DataPoint]:
    """Flatten an OTLP/JSON metrics document into DataPoints (fail-soft)."""
    if not isinstance(doc, dict):
        return []
    out: list[DataPoint] = []
    for rm in _as_list(doc.get("resourceMetrics")):
        if not isinstance(rm, dict):
            continue
        for sm in _as_list(rm.get("scopeMetrics")):
            if isinstance(sm, dict):
                out.extend(_walk_scope(sm))
    return out
