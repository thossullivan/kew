"""Pricing, routing and lifecycle read the model catalog (#2709).

These check rules over ``models.json``, never its contents: adding a model is
a data change, so no test here names a list of models or a row count.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kew import model_catalog
from kew.config import ConfigError, load_config
from kew.dispatch_core import _builtin_default_model
from kew.model_catalog import PACKAGED_CATALOG_PATH, load_catalog
from kew.model_eol_snapshot import check_lifecycle, missing_registry_rows
from kew.pricing import (
    UnknownModelCostError,
    build_pricing_overrides,
    catalog_pricing,
    effective_pricing,
    estimate_cost,
    get_model_cache_params,
    get_provider_cache_params,
    is_priced_model,
    require_pricing_key,
    resolve_pricing_key,
)

NEW_MODEL = "claude-opus-5-9"


def _packaged() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PACKAGED_CATALOG_PATH.read_text("utf-8"))
    return payload


def _new_row(payload: dict[str, Any]) -> dict[str, Any]:
    base = next(r for r in payload["models"] if r["id"] == "claude-opus-5-5")
    row: dict[str, Any] = copy.deepcopy(base)
    row.update(
        id=NEW_MODEL,
        display_name="Claude Opus 5.9",
        price={"input_per_mtok": 6.0, "output_per_mtok": 30.0, "tiers": []},
        default_lanes=["default"],
        aliases=["claude-opus-5-9-20261101"],
    )
    return row


@pytest.fixture
def install_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, Any]], None]]:
    """Point the catalog loader at a temporary ``models.json``."""

    def install(payload: dict[str, Any]) -> None:
        path = tmp_path / "models.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(model_catalog, "PACKAGED_CATALOG_PATH", path)
        load_catalog.cache_clear()

    yield install
    load_catalog.cache_clear()


def _write_config(tmp_path: Path, model: str) -> Path:
    toml = tmp_path / "kew.toml"
    toml.write_text(
        f'[routing]\nsimple = {{ runner = "claude-code", model = "{model}" }}\n'
        'default = { runner = "claude-code", model = "claude-opus-5" }\n',
        encoding="utf-8",
    )
    return toml


# --- AC2: no prefix ride ----------------------------------------------------


def test_an_id_that_extends_a_priced_id_gets_no_price() -> None:
    assert load_catalog().get(NEW_MODEL) is None
    assert load_catalog().get("claude-opus-5") is not None
    assert resolve_pricing_key(NEW_MODEL, catalog_pricing()) is None
    assert not is_priced_model(NEW_MODEL)
    with pytest.raises(UnknownModelCostError):
        require_pricing_key(NEW_MODEL)
    with pytest.warns(UserWarning, match="Unknown model"):
        assert estimate_cost(NEW_MODEL, input_tokens=10**6, output_tokens=0) == 0.0


def test_a_dated_id_with_no_alias_gets_no_price() -> None:
    assert resolve_pricing_key("claude-opus-5-20260401", catalog_pricing()) is None


def test_every_priced_catalog_id_prices_exactly_itself() -> None:
    prices = catalog_pricing()
    for row in load_catalog().rows:
        if row.price is None:
            assert row.id not in prices
            continue
        for key in (row.id, *row.aliases):
            assert resolve_pricing_key(key, prices) == key
            assert prices[key] == (row.price.input_per_mtok, row.price.output_per_mtok)


def test_catalog_cache_multipliers_reach_the_estimate() -> None:
    for row in load_catalog().rows:
        price = row.price
        if price is None or price.cache_read_multiplier is None:
            continue
        params = get_model_cache_params(row.id, row.provider)
        assert params.read_mult == price.cache_read_multiplier, row.id


def test_a_row_without_a_cache_ttl_keeps_the_provider_default(
    install_catalog: Callable[[dict[str, Any]], None],
) -> None:
    payload = _packaged()
    sol = next(r for r in payload["models"] if r["id"] == "gpt-5.6-sol")
    sol["price"]["cache_ttl_seconds"] = 1200
    luna = next(r for r in payload["models"] if r["id"] == "gpt-5.6-luna")
    luna["price"].pop("cache_ttl_seconds", None)
    install_catalog(payload)
    provider_ttl = get_provider_cache_params("openai").ttl_seconds
    assert get_model_cache_params("gpt-5.6-sol", "openai").ttl_seconds == 1200
    luna_params = get_model_cache_params("gpt-5.6-luna", "openai")
    assert luna_params.ttl_seconds == provider_ttl
    # The row's multipliers still apply when only the TTL is absent.
    assert luna_params.read_mult == luna["price"]["cache_read_multiplier"]


# --- AC3: adding a model is a data change ------------------------------------


def test_a_new_catalog_row_prices_routes_and_passes_lifecycle(
    tmp_path: Path, install_catalog: Callable[[dict[str, Any]], None]
) -> None:
    payload = _packaged()
    for row in payload["models"]:
        row["default_lanes"] = []
    payload["models"].append(_new_row(payload))
    payload["models"].sort(key=lambda r: r["id"])
    install_catalog(payload)

    # Prices, by id and by declared alias.
    assert estimate_cost(NEW_MODEL, input_tokens=10**6, output_tokens=10**6) == 36.0
    assert require_pricing_key(NEW_MODEL) == NEW_MODEL
    assert is_priced_model("claude-opus-5-9-20261101")
    # Routes: the router may emit it, and it is the built-in default lane.
    assert NEW_MODEL in load_catalog().routable_ids()
    assert _builtin_default_model() == NEW_MODEL
    # Passes the lifecycle check at config load and in `kew model-eol check`.
    config = load_config(config_path=_write_config(tmp_path, NEW_MODEL), env={})
    entries = config.model_eol.snapshot_models
    assert missing_registry_rows([NEW_MODEL], entries) == []
    assert check_lifecycle([NEW_MODEL], entries) == []


def test_without_the_row_the_same_model_is_unpriced_and_unrouted(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError):
        load_config(config_path=_write_config(tmp_path, NEW_MODEL), env={})
    assert not is_priced_model(NEW_MODEL)


# --- AC1: a routed model needs a lifecycle row --------------------------------


def test_routing_a_model_with_no_lifecycle_row_refuses_at_config_load(
    tmp_path: Path,
) -> None:
    toml = _write_config(tmp_path, "vendor/no-such-model")
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path=toml, env={})
    error = excinfo.value
    assert error.key == "routing.simple.model"
    assert error.kind == "invalid_value"
    assert "vendor/no-such-model: registry gap (no curated lifecycle row)" in str(error)


def test_routing_catalog_and_free_models_loads(tmp_path: Path) -> None:
    for model in ("claude-opus-5", "ollama/qwen3-coder"):
        load_config(config_path=_write_config(tmp_path, model), env={})


def test_routed_lifecycle_rule_reads_the_snapshot_path_override(
    tmp_path: Path,
) -> None:
    registry = {
        "schema": "kew/model-lifecycle-registry@1",
        "reviewedAt": "2026-10-11",
        "models": [
            {
                "id": "claude-opus-5",
                "provider": "anthropic",
                "status": "ok",
                "shutdown": None,
                "replacement": None,
                "evidence": [
                    {
                        "kind": "provider-catalog",
                        "url": "https://example.invalid/models",
                        "checkedAt": "2026-10-11",
                    }
                ],
            }
        ],
    }
    (tmp_path / "lifecycle.json").write_text(json.dumps(registry), encoding="utf-8")
    toml = _write_config(tmp_path, "claude-sonnet-5-5")
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + '\n[model_eol]\nsnapshot_path = "lifecycle.json"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="claude-sonnet-5-5: registry gap"):
        load_config(config_path=toml, env={})


# --- AC4: [pricing] overrides win over catalog prices --------------------------


def test_pricing_override_wins_over_the_catalog_price() -> None:
    row = load_catalog().get("claude-opus-5")
    assert row is not None and row.price is not None
    overrides = build_pricing_overrides({"claude-opus-5": [1.0, 2.0]})
    assert effective_pricing(overrides)["claude-opus-5"] == (1.0, 2.0)
    assert (
        estimate_cost(
            "claude-opus-5",
            input_tokens=10**6,
            output_tokens=10**6,
            overrides=overrides,
        )
        == 3.0
    )
    assert estimate_cost("claude-opus-5", input_tokens=10**6, output_tokens=0) == (
        row.price.input_per_mtok
    )


def test_pricing_override_prices_an_id_the_catalog_does_not_name() -> None:
    overrides = build_pricing_overrides({NEW_MODEL: [1.0, 2.0]})
    assert require_pricing_key(NEW_MODEL, effective_pricing(overrides)) == NEW_MODEL
    assert is_priced_model(NEW_MODEL, overrides)


def test_openrouter_rows_are_priced_by_the_catalog_alone() -> None:
    # The OpenRouter overlay is gone (#2711): `kew models refresh` writes these
    # prices into the catalog, so no config layer is needed to price them.
    rows = [r for r in load_catalog().rows if r.price_source == "openrouter"]
    assert rows
    priced = [r for r in rows if r.price is not None]
    assert priced
    for row in priced:
        assert is_priced_model(row.id, None), row.id
