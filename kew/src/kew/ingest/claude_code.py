"""Claude Code OTel adapter: the only module that knows CC's metric/attribute
vocabulary (#256). Maps OTLP DataPoints -> runner-neutral InteractiveUsage.

Drift containment: the names below are the sole CC-specific surface. A rename
upstream edits one constant. Metric/attribute reference:
https://code.claude.com/docs/en/monitoring-usage
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kew.ingest.otlp import DataPoint
from kew.run_store import InteractiveUsage

logger = logging.getLogger(__name__)

RUNNER = "claude-code"
# "token" in the constant name trips bandit's hardcoded-password heuristic;
# this is a public OTel metric name, not a credential.
TOKEN_METRIC = "claude_code.token.usage"  # nosec B105
COST_METRIC = "claude_code.cost.usage"
_ATTR_TYPE = "type"
_ATTR_MODEL = "model"
_ATTR_SESSION = "session.id"
_ATTR_QUERY_SOURCE = "query_source"
_ATTR_AGENT_NAME = "agent.name"
_TEMPORALITY_CUMULATIVE = 2

# token `type` attribute (lower-cased) -> InteractiveUsage column
_TYPE_COLUMN = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheread": "cache_read_tokens",
    "cachecreation": "cache_creation_tokens",
}


@dataclass
class AssembleResult:
    records: list[InteractiveUsage]
    skipped: int
    seen_metrics: set[str] = field(default_factory=set)


@dataclass
class _Group:
    model: str
    session_id: str | None
    time_unix_nano: str
    query_source: str | None = None
    agent_name: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0


def _clean_str(value: str, limit: int = 256) -> str:
    printable = "".join(ch for ch in value if ch.isprintable())
    return printable[:limit]


def _clamp(value: float) -> float:
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _identity(
    p: DataPoint,
) -> tuple[str, str, str | None, str | None, str]:
    model = _clean_str(p.attributes.get(_ATTR_MODEL, ""))
    session = _clean_str(p.attributes.get(_ATTR_SESSION, ""))
    raw_qs = p.attributes.get(_ATTR_QUERY_SOURCE)
    query_source = _clean_str(raw_qs or "") or None
    raw_an = p.attributes.get(_ATTR_AGENT_NAME)
    agent_name = _clean_str(raw_an or "") or None
    return (model, session, query_source, agent_name, p.time_unix_nano)


def _dedup_key(
    model: str,
    session: str,
    time_unix_nano: str,
    query_source: str | None = None,
    agent_name: str | None = None,
) -> str:
    raw = f"{RUNNER}|{model}|{session}|{time_unix_nano}"
    if (query_source and query_source != "main") or agent_name:
        raw += f"|{query_source or ''}|{agent_name or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_group(
    groups: dict[tuple[str, str, str | None, str | None, str], _Group],
    p: DataPoint,
) -> _Group:
    model, session, query_source, agent_name, tun = _identity(p)
    key = (model, session, query_source, agent_name, tun)
    group = groups.get(key)
    if group is None:
        group = _Group(
            model=model,
            session_id=session or None,
            time_unix_nano=tun,
            query_source=query_source,
            agent_name=agent_name,
        )
        groups[key] = group
    return group


def _iso_from_nanos(time_unix_nano: str) -> str:
    """OTLP nanosecond epoch string -> ISO-8601 UTC. Empty/garbage -> ""."""
    try:
        seconds = int(time_unix_nano) / 1_000_000_000
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def assemble(points: list[DataPoint], *, ingest_source: str) -> AssembleResult:
    groups: dict[tuple[str, str, str | None, str | None, str], _Group] = {}
    seen: set[str] = set()
    skipped = 0
    for p in points:
        if p.metric_name not in (TOKEN_METRIC, COST_METRIC):
            skipped += 1
            continue
        if p.temporality == _TEMPORALITY_CUMULATIVE:
            skipped += 1
            continue
        seen.add(p.metric_name)
        if p.metric_name == TOKEN_METRIC:
            column = _TYPE_COLUMN.get(p.attributes.get(_ATTR_TYPE, "").lower())
            if column is None:
                skipped += 1  # unknown type: never create an empty group
                continue
            group = _get_group(groups, p)
            setattr(group, column, getattr(group, column) + int(_clamp(p.value)))
        else:  # COST_METRIC
            group = _get_group(groups, p)
            group.cost_usd += _clamp(p.value)

    if TOKEN_METRIC in seen and COST_METRIC not in seen:
        # Static drift-warning string + a public metric name — no secret logged.
        logger.warning(  # noqa: E501  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            "Claude Code OTel: token usage seen but %s absent; "
            "interactive cost reported as 0 (possible upstream rename)",
            COST_METRIC,
        )

    records = []
    for g in groups.values():
        record = _to_record(g, ingest_source)
        if record is not None:
            records.append(record)
        else:
            skipped += 1
    return AssembleResult(records=records, skipped=skipped, seen_metrics=seen)


def _to_record(g: _Group, ingest_source: str) -> InteractiveUsage | None:
    """Build an InteractiveUsage from a group, or None if timestamp is missing."""
    observed_at = _iso_from_nanos(g.time_unix_nano)
    if not observed_at:
        return None
    return InteractiveUsage(
        runner=RUNNER,
        model=g.model,
        observed_at=observed_at,
        input_tokens=g.input_tokens,
        output_tokens=g.output_tokens,
        cache_creation_tokens=g.cache_creation_tokens,
        cache_read_tokens=g.cache_read_tokens,
        cost_usd=g.cost_usd,
        session_id=g.session_id,
        ingest_source=ingest_source,
        dedup_key=_dedup_key(
            g.model,
            g.session_id or "",
            g.time_unix_nano,
            g.query_source,
            g.agent_name,
        ),
        query_source=g.query_source,
        agent_name=g.agent_name,
    )
