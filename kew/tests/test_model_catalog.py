"""The packaged model catalog (``models.json``) and its strict loader (#2708)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from kew.model_catalog import (
    PACKAGED_CATALOG_PATH,
    CatalogError,
    ModelCatalog,
    load_catalog,
    parse_catalog,
)


def _packaged() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(PACKAGED_CATALOG_PATH.read_text("utf-8"))
    return payload


def _row(payload: dict[str, Any], model_id: str) -> dict[str, Any]:
    row: dict[str, Any] = next(r for r in payload["models"] if r["id"] == model_id)
    return row


def test_packaged_catalog_validates() -> None:
    catalog = load_catalog()
    assert isinstance(catalog, ModelCatalog)
    assert catalog.rows
    assert parse_catalog(_packaged()) == catalog


def test_catalog_loads_once_per_process() -> None:
    assert load_catalog() is load_catalog()


def test_rows_are_frozen() -> None:
    row = load_catalog().rows[0]
    with pytest.raises(AttributeError):
        row.id = "other"  # type: ignore[misc]


def test_every_routable_row_has_a_price_and_an_evidence_url() -> None:
    routable = [row for row in load_catalog().rows if row.routable]
    assert routable
    for row in routable:
        assert row.price is not None, row.id
        assert any(e.url.startswith("https://") for e in row.lifecycle.evidence), row.id


def test_every_row_has_a_display_name_that_is_not_its_id() -> None:
    for row in load_catalog().rows:
        assert row.display_name != row.id


def _drop_price(p: dict[str, Any]) -> tuple[str, str]:
    _row(p, "claude-opus-5-5")["price"] = None
    return "claude-opus-5-5", "price"


def _unsort(p: dict[str, Any]) -> tuple[str, str]:
    models = p["models"]
    models[0], models[1] = models[1], models[0]
    return models[1]["id"], "id"


def _non_monotonic_tiers(p: dict[str, Any]) -> tuple[str, str]:
    _row(p, "gpt-5.6-sol")["price"]["tiers"] = [
        {"above_prompt_tokens": 272000, "input_per_mtok": 10, "output_per_mtok": 45},
        {"above_prompt_tokens": 128000, "input_per_mtok": 12, "output_per_mtok": 50},
    ]
    return "gpt-5.6-sol", "price.tiers"


def _unknown_lane(p: dict[str, Any]) -> tuple[str, str]:
    _row(p, "claude-sonnet-5-5")["default_lanes"] = ["simple", "trivial"]
    return "claude-sonnet-5-5", "default_lanes"


def _unknown_status(p: dict[str, Any]) -> tuple[str, str]:
    _row(p, "claude-opus-5")["lifecycle"]["status"] = "sunset"
    return "claude-opus-5", "lifecycle.status"


def _duplicate_id(p: dict[str, Any]) -> tuple[str, str]:
    models = p["models"]
    index = next(i for i, r in enumerate(models) if r["id"] == "claude-opus-5")
    models.insert(index + 1, copy.deepcopy(models[index]))
    return "claude-opus-5", "id"


def _alias_is_an_id(p: dict[str, Any]) -> tuple[str, str]:
    _row(p, "claude-opus-5-5")["aliases"] = ["claude-opus-5"]
    return "claude-opus-5-5", "aliases"


def _second_lane_default(p: dict[str, Any]) -> tuple[str, str]:
    for row in p["models"]:
        row["default_lanes"] = []
    _row(p, "claude-opus-5")["default_lanes"] = ["medium"]
    _row(p, "claude-opus-5-5")["default_lanes"] = ["medium"]
    return "claude-opus-5-5", "default_lanes"


def _unroutable_lane_default(p: dict[str, Any]) -> tuple[str, str]:
    for row in p["models"]:
        row["default_lanes"] = []
    _row(p, "claude-opus-4")["default_lanes"] = ["complex"]
    return "claude-opus-4", "default_lanes"


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_price,
        _unsort,
        _non_monotonic_tiers,
        _unknown_lane,
        _unknown_status,
        _duplicate_id,
        _alias_is_an_id,
        _second_lane_default,
        _unroutable_lane_default,
    ],
    ids=[
        "routable-without-price",
        "unsorted-ids",
        "non-monotonic-tiers",
        "unknown-lane",
        "unknown-lifecycle-status",
        "duplicate-id",
        "alias-is-an-id",
        "second-lane-default",
        "unroutable-lane-default",
    ],
)
def test_invalid_catalog_names_the_row_and_the_field(
    mutate: Callable[[dict[str, Any]], tuple[str, str]],
) -> None:
    payload = _packaged()
    parse_catalog(payload)  # the unmutated document is valid
    row, field = mutate(payload)
    with pytest.raises(CatalogError) as excinfo:
        parse_catalog(payload)
    message = str(excinfo.value)
    assert f"row {row!r}" in message
    assert f"field {field!r}" in message


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "1800", None])
def test_malformed_cache_ttl_is_refused(value: Any) -> None:
    payload = _packaged()
    _row(payload, "gpt-5.6-sol")["price"]["cache_ttl_seconds"] = value
    with pytest.raises(CatalogError) as excinfo:
        parse_catalog(payload)
    message = str(excinfo.value)
    assert "row 'gpt-5.6-sol'" in message
    assert "field 'price.cache_ttl_seconds'" in message


def test_cache_ttl_parses_onto_the_price() -> None:
    price = parse_catalog(_packaged()).get("gpt-5.6-luna").price
    assert price is not None
    assert price.cache_ttl_seconds == 1800


def test_monotonic_tiers_parse() -> None:
    payload = _packaged()
    _row(payload, "gpt-5.6-sol")["price"]["tiers"] = [
        {"above_prompt_tokens": 128000, "input_per_mtok": 8, "output_per_mtok": 40},
        {"above_prompt_tokens": 272000, "input_per_mtok": 10, "output_per_mtok": 45},
    ]
    sol = parse_catalog(payload).get("gpt-5.6-sol")
    assert sol is not None and sol.price is not None
    assert [t.above_prompt_tokens for t in sol.price.tiers] == [128000, 272000]


def test_a_row_without_an_id_is_named_by_position() -> None:
    payload = _packaged()
    del payload["models"][2]["id"]
    with pytest.raises(CatalogError, match=r"row '#2' field '\(row\)'"):
        parse_catalog(payload)


def test_unknown_keys_are_refused() -> None:
    payload = _packaged()
    _row(payload, "claude-opus-5")["price"]["currency"] = "EUR"
    with pytest.raises(CatalogError, match=r"row 'claude-opus-5' field 'price'"):
        parse_catalog(payload)


def test_lookup_is_exact_id_or_declared_alias() -> None:
    payload = _packaged()
    _row(payload, "claude-opus-5-5")["aliases"] = ["claude-opus-5-5-20261008"]
    catalog = parse_catalog(payload)
    row = catalog.get("claude-opus-5-5")
    assert catalog.lookup("claude-opus-5-5") is row
    assert catalog.lookup("claude-opus-5-5-20261008") is row
    assert catalog.lookup("claude-opus-5-5-20261009") is None
    assert catalog.lookup("claude-opus-5-9") is None


def test_each_lane_has_at_most_one_routable_default() -> None:
    catalog = load_catalog()
    for lane in ("default", "simple", "medium", "complex"):
        owners = [row for row in catalog.rows if lane in row.default_lanes]
        assert len(owners) <= 1, lane
        assert all(row.routable for row in owners), lane
        assert catalog.lane_default(lane) == (owners[0].id if owners else None)


def test_reviewed_at_parses_as_a_date() -> None:
    assert load_catalog().reviewed_at == date(2026, 10, 8)


@pytest.mark.parametrize(
    "value",
    [None, "", 20261008, "2026-13-01", "2026-02-30", "2026-W41-1", "2026-281"],
)
def test_malformed_reviewed_at_is_refused(value: Any) -> None:
    payload = _packaged()
    payload["reviewedAt"] = value
    with pytest.raises(CatalogError, match="reviewedAt must be an ISO YYYY-MM-DD"):
        parse_catalog(payload)


def test_missing_reviewed_at_is_refused() -> None:
    payload = _packaged()
    del payload["reviewedAt"]
    with pytest.raises(CatalogError, match="reviewedAt"):
        parse_catalog(payload)
