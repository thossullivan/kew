"""Proactive completeness guard: every emittable model must be priced.

A model kew can route to that has no catalog price silently meters at $0
(``estimate_cost`` returns 0.0 and warns). A $0 run never accrues against
budgets, never moves the circuit-breaker baseline, and reads as free in the
cost-per-merged-PR headline — corrupting the three surfaces the thesis sells.

These are rules over the model catalog (``models.json``), not a list of
models: adding a routable row without a price, or routing the committed
``kew.toml`` at an unpriced model, fails the PR gate. It is the static,
ship-time counterpart to ``pricing_audit.find_missing_models`` (which is
run-history-driven).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from kew.model_catalog import load_catalog
from kew.pricing import (
    build_pricing_overrides,
    catalog_pricing,
    effective_pricing,
    is_free_model,
    resolve_pricing_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOGFOOD_KEW_TOML = REPO_ROOT / "kew.toml"


def _resolves(model: str, pricing: dict[str, tuple[float, float]]) -> bool:
    """True if *model* is free-by-design or resolves to a pricing key."""
    return is_free_model(model) or resolve_pricing_key(model, pricing) is not None


def test_every_routable_catalog_model_has_a_positive_price() -> None:
    """A routable row resolves to a price, and that price is not $0.

    A $0-priced key resolves to non-None yet still meters as free, so the
    resolve check alone is not enough.
    """
    prices = catalog_pricing()
    routable = load_catalog().routable_ids()
    assert routable
    for model in routable:
        key = resolve_pricing_key(model, prices)
        assert key == model, f"routable {model!r} has no catalog price"
        in_cost, out_cost = prices[key]
        assert in_cost > 0 and out_cost > 0, f"routable {model!r} is priced at $0"


def test_every_lane_default_is_routable_and_priced() -> None:
    catalog = load_catalog()
    prices = catalog_pricing()
    for lane in ("default", "simple", "medium", "complex"):
        model = catalog.lane_default(lane)
        if model is None:
            continue
        assert model in catalog.routable_ids(), (lane, model)
        assert _resolves(model, prices), (lane, model)


def _collect_config_models(data: dict[str, Any]) -> set[str]:
    """Pull every model string the dogfood config can emit as a route."""
    models: set[str] = set()
    agent = data.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("model"), str):
        models.add(agent["model"])
    routing = data.get("routing")
    if isinstance(routing, dict):
        for entry in routing.values():
            if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                models.add(entry["model"])
    return {m for m in models if m}


def test_dogfood_config_models_are_priced() -> None:
    """Every model the committed kew.toml routes to resolves to a price.

    Guards the live failure mode directly: editing kew.toml to route a tier at
    an unpriced model (e.g. simple → a new Sonnet) fails CI here, not silently
    in production at $0.
    """
    assert DOGFOOD_KEW_TOML.is_file(), f"missing dogfood config: {DOGFOOD_KEW_TOML}"
    data = tomllib.loads(DOGFOOD_KEW_TOML.read_text(encoding="utf-8"))

    # Honor the config's own [pricing] overrides — an operator-priced model is
    # not "missing" — merged over the catalog exactly as dispatch meters.
    raw_overrides = data.get("pricing", {})
    overrides = {}
    if isinstance(raw_overrides, dict) and isinstance(
        raw_overrides.get("overrides"), dict
    ):
        valid = {
            k: v for k, v in raw_overrides["overrides"].items() if isinstance(v, list)
        }
        overrides = build_pricing_overrides(valid) or {}
    effective = effective_pricing(overrides)

    unpriced = sorted(
        m for m in _collect_config_models(data) if not _resolves(m, effective)
    )
    assert not unpriced, (
        f"{DOGFOOD_KEW_TOML.name} routes to unpriced models (they meter at $0): "
        f"{unpriced}. Add a priced row to kew/src/kew/models.json or a [pricing] "
        "override."
    )
