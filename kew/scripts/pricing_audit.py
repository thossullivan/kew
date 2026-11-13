"""Offline pricing drift audit for the freshness loop (#269).

Compares the shipped model catalog's prices against a locally saved snapshot
of LiteLLM's ``model_prices_and_context_window.json``. This module performs no
network I/O — the scheduled GitHub workflow downloads the snapshot and invokes
``python scripts/pricing_audit.py`` with it; the resulting Markdown report becomes
the body of a ``ready``-labeled issue.
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from kew.model_catalog import load_catalog
from kew.pricing import (
    build_pricing_overrides,
    catalog_pricing,
    is_free_model,
    resolve_pricing_key,
)

Pricing = dict[str, tuple[float, float]]

_DATED_SUFFIX = re.compile(r"\d{8}")
# Relative tolerance for price comparison; upstream floats like 2.5e-05 * 1e6
# can carry representation noise that is not real drift.
_REL_TOL = 1e-6

_ANTHROPIC_PRICING_DOCS = "https://platform.claude.com/docs/en/pricing"


@dataclass
class DriftEntry:
    """One model whose shipped pricing differs from the upstream snapshot."""

    model: str
    shipped: tuple[float, float]
    upstream: tuple[float, float]


@dataclass
class RunImpact:
    """Cost delta over recent runs for one drifted model."""

    model: str
    run_count: int
    shipped_cost: float
    upstream_cost: float


class AuditRun(NamedTuple):
    """The slice of a run record the audit needs."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


def parse_litellm_snapshot(data: dict[str, object]) -> Pricing:
    """Extract model pricing from a LiteLLM snapshot dict.

    Provider-agnostic: keeps any entry with numeric per-token costs (drift
    comparison is restricted to shipped keys anyway, and kew's runners can
    route to non-Anthropic models). Normalized to USD per 1M tokens. Booleans
    are rejected explicitly — ``bool`` is an ``int`` subclass.
    """
    pricing: Pricing = {}
    for model, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if model == "gpt-5.6-sol":
            input_cost = entry.get("input_cost_per_token_above_272k_tokens")
            output_cost = entry.get("output_cost_per_token_above_272k_tokens")
        else:
            input_cost = entry.get("input_cost_per_token")
            output_cost = entry.get("output_cost_per_token")
        if isinstance(input_cost, bool) or isinstance(output_cost, bool):
            continue
        if not isinstance(input_cost, int | float) or not isinstance(
            output_cost, int | float
        ):
            continue
        pricing[model] = (input_cost * 1_000_000, output_cost * 1_000_000)
    return pricing


def _match_upstream(model: str, upstream: Pricing) -> tuple[float, float] | None:
    """Find the upstream price for a shipped key: exact, else latest dated form."""
    if model in upstream:
        return upstream[model]
    best_date = ""
    best: tuple[float, float] | None = None
    for key, price in upstream.items():
        suffix = key.removeprefix(model + "-")
        if suffix == key or not _DATED_SUFFIX.fullmatch(suffix):
            continue
        if suffix > best_date:
            best_date = suffix
            best = price
    return best


def _prices_equal(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return all(math.isclose(x, y, rel_tol=_REL_TOL) for x, y in zip(a, b, strict=True))


def _family_fallback_keys(shipped: Pricing) -> set[str]:
    """Shipped keys that are dash-prefixes of other shipped keys.

    These are family fallbacks (e.g. ``claude-opus-4`` next to
    ``claude-opus-4-6``), not specific models — matching them against a dated
    upstream key would compare a fallback to one arbitrary family member.
    """
    # A key kew can route to is a model in its own right even when a point
    # release extends it (``claude-sonnet-5`` beside ``claude-sonnet-5-5``):
    # treating it as a fallback would stop its dated upstream form from ever
    # being compared (#2531 review).
    routable = load_catalog().routable_ids()
    return {
        key
        for key in shipped
        if key not in routable
        and any(other.startswith(key + "-") for other in shipped if other != key)
    }


def diff_pricing(shipped: Pricing, upstream: Pricing) -> list[DriftEntry]:
    """Return drift entries for shipped keys whose upstream price differs.

    Family-fallback keys only drift on an exact upstream match; specific model
    keys also match their latest dated upstream form (``<key>-YYYYMMDD``).
    Shipped keys with no upstream counterpart are skipped — absence upstream
    is not drift.
    """
    fallbacks = _family_fallback_keys(shipped)
    drifts: list[DriftEntry] = []
    for model, shipped_price in sorted(shipped.items()):
        if model in fallbacks:
            upstream_price = upstream.get(model)
        else:
            upstream_price = _match_upstream(model, upstream)
        if upstream_price is None or _prices_equal(shipped_price, upstream_price):
            continue
        drifts.append(
            DriftEntry(model=model, shipped=shipped_price, upstream=upstream_price)
        )
    return drifts


def load_recent_runs(db_path: str, *, days: int = 30) -> list[AuditRun]:
    """Read recent runs from a dispatch DB without ever writing to it.

    Uses a read-only SQLite connection on purpose: the audit may point at a
    live dispatch.db, so it must not take write locks, flip journal modes, or
    apply schema migrations the way ``SqliteRunStore`` does on open.
    """
    since_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # The cache columns arrive in schema V11; this read-only path never
        # migrates, so an older DB lacks them. Pick a fully-static query that
        # selects them when present and 0 otherwise — no string interpolation.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        has_cache = {"cache_creation_tokens", "cache_read_tokens"} <= cols
        # Two fully-static queries (no interpolation): one selects the cache
        # columns, the other falls back to 0 for pre-V11 DBs. Both mirror
        # SqliteRunStore.list_since — legacy rows may lack a tz suffix, so naive
        # timestamps are compared on the date-time part.
        if has_cache:
            query = """
                SELECT model, input_tokens, output_tokens,
                       cache_creation_tokens, cache_read_tokens
                FROM runs
                WHERE started_at >= ?
                   OR (length(started_at) = 19 AND started_at >= ?)
            """
        else:
            query = """
                SELECT model, input_tokens, output_tokens, 0, 0
                FROM runs
                WHERE started_at >= ?
                   OR (length(started_at) = 19 AND started_at >= ?)
            """
        rows = conn.execute(query, (since_iso, since_iso[:19])).fetchall()
    finally:
        conn.close()
    return [
        AuditRun(
            model=m,
            input_tokens=i,
            output_tokens=o,
            cache_creation_tokens=cc,
            cache_read_tokens=cr,
        )
        for m, i, o, cc, cr in rows
    ]


def find_missing_models(runs: list[AuditRun], *, shipped: Pricing) -> list[str]:
    """Models used in recent runs that resolve to $0 (no pricing entry).

    Free-provider models (``ollama/*``) are free by design, not missing.
    ``shipped`` should include any ``[pricing]`` config overrides in effect,
    so operator-priced models are not reported as missing.
    """
    missing: set[str] = set()
    for run in runs:
        if not run.model or is_free_model(run.model):
            continue
        if resolve_pricing_key(run.model, shipped) is None:
            missing.add(run.model)
    return sorted(missing)


def _is_uncovered_upstream_chat(model: str, entry: object, shipped: Pricing) -> bool:
    """True if *model*/*entry* is a claude-* chat model with valid numeric
    pricing that no shipped key already covers.

    The per-entry filter for :func:`find_new_upstream_models`, extracted so that
    function stays within the xenon complexity gate.
    """
    if not model.startswith("claude-"):
        return False
    if not isinstance(entry, dict):
        return False
    mode = entry.get("mode")
    if mode is not None and mode != "chat":
        return False
    input_cost = entry.get("input_cost_per_token")
    output_cost = entry.get("output_cost_per_token")
    if isinstance(input_cost, bool) or isinstance(output_cost, bool):
        return False
    if not isinstance(input_cost, int | float) or not isinstance(
        output_cost, int | float
    ):
        return False
    # A dated snapshot of a shipped id is that model, not a new one: the drift
    # check already compares it (``diff_pricing``), so it is not listed here.
    shipped_ids = set(shipped)
    return resolve_pricing_key(
        model, shipped
    ) is None and not _is_dated_variant_of_candidate(model, shipped_ids)


def _is_dated_variant_of_candidate(model: str, candidates: set[str]) -> bool:
    """True if *model* is a ``<base>-YYYYMMDD`` dated variant whose base is also
    a candidate — such variants are deduped in favor of the base.
    """
    parts = model.rsplit("-", 1)
    return (
        len(parts) == 2
        and _DATED_SUFFIX.fullmatch(parts[1]) is not None
        and parts[0] in candidates
    )


def find_new_upstream_models(
    raw_snapshot: dict[str, object],
    shipped: Pricing,
) -> list[str]:
    """Upstream claude-* chat models not covered by any shipped pricing key.

    Filters to chat models (``mode == 'chat'`` when present; claude-* entries
    with no mode field are included, since LiteLLM doesn't always set it).
    Deduplicates dated variants (``<base>-YYYYMMDD``) when their base is also
    a new upstream candidate — only the base is reported.
    """
    candidates = {
        model
        for model, entry in raw_snapshot.items()
        if _is_uncovered_upstream_chat(model, entry, shipped)
    }
    return sorted(
        model
        for model in candidates
        if not _is_dated_variant_of_candidate(model, candidates)
    )


def load_acknowledged_entries(path: Path, *, today: date | None = None) -> set[str]:
    """Read [pricing.acknowledged] from kew.toml; return non-expired model names.

    Each entry maps a model name to an ISO date string (``YYYY-MM-DD``).
    Entries whose date is today or in the future suppress drift reporting for
    that model; entries in the past are ignored (acknowledgment has expired).
    """
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    pricing_section = data.get("pricing")
    if not isinstance(pricing_section, dict):
        return set()
    raw = pricing_section.get("acknowledged")
    if not isinstance(raw, dict):
        return set()
    _today = today or datetime.now(UTC).date()
    result: set[str] = set()
    for model, until_str in raw.items():
        if not isinstance(until_str, str):
            continue
        try:
            until = datetime.strptime(until_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if _today <= until:
            result.add(model)
    return result


def estimate_run_impact(
    runs: list[AuditRun], drifts: list[DriftEntry], *, shipped: Pricing
) -> list[RunImpact]:
    """Recompute recent run costs under shipped vs upstream prices per drift.

    Each run is attributed to the single exact key in ``shipped`` —
    which must be the same effective table (including any config overrides)
    that ``estimate_cost`` metered it with — so runs are never counted under
    more than one drift entry, and runs governed by an override never land on
    a drifted shipped key.
    """
    grouped: dict[str, list[AuditRun]] = {}
    for run in runs:
        key = resolve_pricing_key(run.model, shipped)
        if key is not None:
            grouped.setdefault(key, []).append(run)
    impacts: list[RunImpact] = []
    for drift in drifts:
        matched = grouped.get(drift.model)
        if not matched:
            continue
        impacts.append(
            RunImpact(
                model=drift.model,
                run_count=len(matched),
                shipped_cost=sum(_cost(r, drift.shipped) for r in matched),
                upstream_cost=sum(_cost(r, drift.upstream) for r in matched),
            )
        )
    return impacts


def _cost(run: AuditRun, price: tuple[float, float]) -> float:
    from kew.pricing import get_model_cache_params, infer_provider

    input_cost, output_cost = price
    cache = get_model_cache_params(run.model, infer_provider(run.model))
    return (
        run.input_tokens * input_cost
        + run.output_tokens * output_cost
        + run.cache_creation_tokens * input_cost * cache.write_mult
        + run.cache_read_tokens * input_cost * cache.read_mult
    ) / 1_000_000


def _pct(shipped: float, upstream: float) -> str:
    if shipped == 0:
        return "n/a"
    return f"{(upstream - shipped) / shipped:+.1%}"


def _cell(value: str) -> str:
    """Escape a value for use inside a Markdown table cell."""
    return value.replace("|", "\\|")


def render_report(
    *,
    drifts: list[DriftEntry],
    missing: list[str],
    impacts: list[RunImpact],
    new_upstream: list[str] | None = None,
    has_run_data: bool = False,
) -> str:
    """Render the Markdown report that becomes the filed issue body."""
    if not drifts and not missing and not new_upstream:
        return (
            "No pricing drift detected: the model catalog matches the latest "
            "LiteLLM snapshot.\n"
        )
    lines = ["## Pricing drift report", ""]
    if missing:
        lines += [
            "### Missing in-use models (metered at $0 today)",
            "",
            *(f"- `{model}` — no model catalog row" for model in missing),
            "",
        ]
    if new_upstream:
        lines += [
            "### New upstream models (absent from the model catalog)",
            "",
            *(f"- `{model}` — not in the model catalog" for model in new_upstream),
            "",
        ]
    if drifts:
        lines += _render_drift_table(drifts)
    lines += _render_impact_section(impacts, has_run_data=has_run_data)
    lines += [
        "### Next steps",
        "",
        f"1. Cross-check the upstream prices against [Anthropic's published "
        f"pricing]({_ANTHROPIC_PRICING_DOCS}) before changing anything.",
        "2. Update the model's row in the model catalog.",
        "3. Pricing ships in the package — no runtime fetching. The `[pricing]` "
        "config override remains the between-releases escape hatch.",
        "",
    ]
    return "\n".join(lines)


def _render_drift_table(drifts: list[DriftEntry]) -> list[str]:
    lines = [
        "### Drifted models",
        "",
        "| Model | Shipped (in/out per 1M) | Upstream (in/out per 1M) | Delta |",
        "|---|---|---|---|",
    ]
    for d in drifts:
        lines.append(
            f"| `{_cell(d.model)}` "
            f"| ${d.shipped[0]:.2f} / ${d.shipped[1]:.2f} "
            f"| ${d.upstream[0]:.2f} / ${d.upstream[1]:.2f} "
            f"| {_pct(d.shipped[0], d.upstream[0])} in, "
            f"{_pct(d.shipped[1], d.upstream[1])} out |"
        )
    lines.append("")
    return lines


def _render_impact_section(
    impacts: list[RunImpact], *, has_run_data: bool
) -> list[str]:
    if not impacts:
        reason = (
            "No drifted models appear in recent runs."
            if has_run_data
            else "No run-store data available — impact shown as price deltas above."
        )
        return ["### Estimated impact on recent runs", "", reason, ""]
    lines = [
        "### Estimated impact on recent runs",
        "",
        "| Model | Runs | Cost (shipped) | Cost (upstream) | Delta |",
        "|---|---|---|---|---|",
    ]
    for i in impacts:
        delta = i.upstream_cost - i.shipped_cost
        lines.append(
            f"| `{_cell(i.model)}` | {i.run_count} | ${i.shipped_cost:.2f} "
            f"| ${i.upstream_cost:.2f} | ${delta:+.2f} |"
        )
    lines.append("")
    return lines


def _load_snapshot(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("snapshot root must be a JSON object")
    return raw


def load_config_overrides(path: Path) -> Pricing:
    """Read ``[pricing] overrides`` from a kew.toml, empty when absent."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    pricing_section = data.get("pricing")
    if not isinstance(pricing_section, dict):
        return {}
    raw = pricing_section.get("overrides")
    if not isinstance(raw, dict):
        return {}
    valid = {k: v for k, v in raw.items() if isinstance(v, list)}
    return build_pricing_overrides(valid) or {}


def _load_overrides_and_acks(config_arg: str) -> tuple[Pricing, set[str]]:
    """Load [pricing] overrides + non-expired acknowledgements from *config_arg*,
    or empties when it is unset or missing.

    Extracted from :func:`_run_audit` to keep it within the xenon complexity gate.
    """
    if config_arg and Path(config_arg).is_file():
        config_path = Path(config_arg)
        return (
            load_config_overrides(config_path),
            load_acknowledged_entries(config_path),
        )
    return {}, set()


def _compute_run_impacts(
    run_store: str,
    days: int,
    effective: Pricing,
    display_drifts: list[DriftEntry],
    overrides: Pricing,
) -> tuple[list[str], list[RunImpact], bool]:
    """Resolve missing-model and drift-impact data from run history, or empties
    when no run store is available (``has_run_data`` is the third element).

    Runs are resolved against the same *effective* table ``estimate_cost``
    metered them with, and drifted keys the operator has overridden are skipped
    — those runs were billed at the override price, so shipped-table drift does
    not touch them. Extracted from :func:`_run_audit`.
    """
    if not (run_store and Path(run_store).is_file()):
        return [], [], False
    runs = load_recent_runs(run_store, days=days)
    missing = find_missing_models(runs, shipped=effective)
    impact_drifts = [d for d in display_drifts if d.model not in overrides]
    impacts = estimate_run_impact(runs, impact_drifts, shipped=effective)
    return missing, impacts, True


def _run_audit(args: argparse.Namespace) -> int:
    raw = _load_snapshot(args.snapshot)
    upstream = parse_litellm_snapshot(raw)
    shipped = catalog_pricing()
    drifts = diff_pricing(shipped, upstream)

    overrides, acknowledged = _load_overrides_and_acks(args.config)
    effective: Pricing = {**shipped, **overrides}

    # Acknowledged drifts are suppressed from the report until their expiry.
    display_drifts = [d for d in drifts if d.model not in acknowledged]
    new_upstream = find_new_upstream_models(raw, effective)
    missing, impacts, has_run_data = _compute_run_impacts(
        args.run_store, args.days, effective, display_drifts, overrides
    )

    args.output.write_text(
        render_report(
            drifts=display_drifts,
            missing=missing,
            impacts=impacts,
            new_upstream=new_upstream,
            has_run_data=has_run_data,
        ),
        encoding="utf-8",
    )
    return 1 if display_drifts or missing or new_upstream else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Exit codes: 0 clean, 1 drift/missing found, 2 errors.

    Exit 1 strictly means "drift report written" — the workflow files an issue
    from the report on 1, so every failure path must map to 2 instead.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-store", default="", help="optional dispatch.db path")
    parser.add_argument("--config", default="", help="optional kew.toml for [pricing]")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    try:
        return _run_audit(args)
    except Exception as exc:  # noqa: BLE001 — any failure must exit 2, not 1
        print(f"error: pricing audit failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
