"""Analytics query functions — aggregate stats from SQLite RunStore."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew.run_store import RunRecord, RunStore


def _completed_runs(store: RunStore, since: datetime | None) -> list[RunRecord]:
    """Return non-pending runs, optionally filtered by start time."""
    all_runs = store.list_all()
    if since is not None:
        return [r for r in all_runs if r.started_at >= since and r.status != "pending"]
    return [r for r in all_runs if r.status != "pending"]


def _success_rate(runs: list[RunRecord]) -> float:
    """Return success rate as a percentage (0-100)."""
    if not runs:
        return 0.0
    successes = sum(1 for r in runs if r.status == "success")
    return round(successes / len(runs) * 100, 1)


def _group_stats(
    runs: list[RunRecord], *, include_duration: bool = False
) -> dict[str, Any]:
    """Compute common stats for a group of runs."""
    stats: dict[str, Any] = {
        "runs": len(runs),
        "success_rate": _success_rate(runs),
        "avg_cost": (
            sum(r.estimated_cost_usd for r in runs) / len(runs) if runs else 0.0
        ),
    }
    if include_duration:
        stats["avg_duration"] = (
            sum(r.duration_seconds for r in runs) / len(runs) if runs else 0.0
        )
    return stats


def aggregate_stats(store: RunStore, *, since: datetime | None) -> dict[str, Any]:
    """Return aggregate stats for completed runs."""
    runs = _completed_runs(store, since)
    if not runs:
        return {
            "runs": 0,
            "success_rate": 0.0,
            "avg_cost": 0.0,
            "avg_duration": 0.0,
            "total_spend": 0.0,
        }
    stats = _group_stats(runs, include_duration=True)
    stats["total_spend"] = sum(r.estimated_cost_usd for r in runs)
    return stats


def failure_breakdown(
    store: RunStore, *, since: datetime | None
) -> list[tuple[str, int]]:
    """Return failure categories ranked by count."""
    runs = _completed_runs(store, since)
    counts: dict[str, int] = {}
    for r in runs:
        if r.status == "success":
            continue
        cat = r.error_category or "unknown"
        counts[cat] = counts.get(cat, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)


def model_breakdown(store: RunStore, *, since: datetime | None) -> list[dict[str, Any]]:
    """Return per-model stats."""
    runs = _completed_runs(store, since)
    by_model: dict[str, list[RunRecord]] = {}
    for r in runs:
        by_model.setdefault(r.model or "unknown", []).append(r)
    return [
        {"model": model, **_group_stats(group)}
        for model, group in sorted(
            by_model.items(), key=lambda x: len(x[1]), reverse=True
        )
    ]


def complexity_breakdown(
    store: RunStore, *, since: datetime | None
) -> list[dict[str, Any]]:
    """Return per-complexity stats."""
    runs = _completed_runs(store, since)
    by_complexity: dict[str, list[RunRecord]] = {}
    for r in runs:
        by_complexity.setdefault(r.complexity or "unknown", []).append(r)
    order = {"simple": 0, "medium": 1, "complex": 2, "unknown": 3}
    return [
        {"complexity": cx, **_group_stats(group, include_duration=True)}
        for cx, group in sorted(
            by_complexity.items(), key=lambda x: order.get(x[0], 99)
        )
    ]


def _line_assistant_cache_read(stripped: str) -> int | None:
    """Return a stream-json line's assistant ``cache_read_input_tokens``.

    Returns the cache-read count (missing key counts as 0), or None when the
    line is not a usage-bearing ``assistant`` event.  Tolerant of non-JSON.
    """
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("type") != "assistant":
        return None
    message = data.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    value = usage.get("cache_read_input_tokens")
    try:
        return int(value) if value is not None else 0
    except (ValueError, TypeError):
        return None


def _first_turn_cache_read_tokens(log_file: Path | None) -> int | None:
    """Extract a run's *first-turn* ``cache_read_input_tokens`` from its
    stream-json transcript.

    Spec D3 requires run 2's **first-turn** cache reads, not the whole-run
    total. Every multi-turn run self-caches from turn 2 onward, so the run-level
    ``cache_read_tokens`` total is almost always large — using it would mask the
    exact miss D3 is built to catch (the first turn served zero *cross-agent*
    cache read). We therefore read the first usage-bearing ``assistant`` event
    in the transcript, which is the only turn whose cache reads reflect a warm
    line left by a *sibling* run rather than this run's own earlier turns.

    Returns the first turn's ``cache_read_input_tokens`` (0 counts — that is the
    D3 kill signal), or None when the transcript is absent, unreadable, or has
    no usage-bearing assistant event.
    """
    if log_file is None:
        return None
    try:
        text = log_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cache_read = _line_assistant_cache_read(stripped)
        if cache_read is not None:
            return cache_read
    return None


def prefix_pair_probe(
    store: RunStore,
    *,
    ttl_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Find run pairs with equal prefix_key started within ttl_seconds and
    report each second run's first-turn cache_read tokens.

    D3 falsification test (context-memory design decision D3):
    if run 2's first-turn cache_read tokens ≈ 0, fleet amortization is not
    occurring and the amortization language should be deleted from the docs.
    The first-turn value is parsed from run 2's stream-json transcript
    (``log_file``) — the run-level total would self-cache and never falsify.

    Only completed (non-``pending``) runs are considered; a still-pending run
    carries a placeholder ``cache_read_tokens=0`` that would inject a spurious
    cache-miss verdict.

    Returns one entry per consecutive pair (sorted by started_at) with delta
    strictly less than ttl_seconds.  Runs without a prefix_key are excluded.
    ``run2_first_turn_cache_read_tokens`` is None when run 2's transcript is
    unavailable.
    """
    all_runs = store.list_all()
    keyed = [r for r in all_runs if r.prefix_key is not None and r.status != "pending"]

    by_key: dict[str, list[RunRecord]] = {}
    for r in keyed:
        key = r.prefix_key
        assert key is not None  # filtered above
        by_key.setdefault(key, []).append(r)

    results: list[dict[str, Any]] = []
    for key, runs in by_key.items():
        sorted_runs = sorted(runs, key=lambda r: r.started_at)
        for i in range(len(sorted_runs) - 1):
            r1, r2 = sorted_runs[i], sorted_runs[i + 1]
            delta = (r2.started_at - r1.started_at).total_seconds()
            if delta < ttl_seconds:
                results.append(
                    {
                        "run1_id": r1.run_id,
                        "run2_id": r2.run_id,
                        "prefix_key": key,
                        "delta_seconds": delta,
                        "ttl_seconds": ttl_seconds,
                        "run2_first_turn_cache_read_tokens": (
                            _first_turn_cache_read_tokens(r2.log_file)
                        ),
                    }
                )
    return results


def daily_activity(store: RunStore, *, days: int = 7) -> list[tuple[str, int, int]]:
    """Return daily activity for the last N days.

    Returns [(day_label, successes, failures), ...].
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    runs = [r for r in _completed_runs(store, since=cutoff)]
    result: list[tuple[str, int, int]] = []
    for i in range(days):
        day = now - timedelta(days=days - 1 - i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_runs = [r for r in runs if day_start <= r.started_at < day_end]
        successes = sum(1 for r in day_runs if r.status == "success")
        failures = len(day_runs) - successes
        label = day_start.strftime("%a")
        result.append((label, successes, failures))
    return result
