"""The packaged model catalog: ``models.json``, one row per model id.

Each row records a model's display name, provider, runner, list price and where
that price comes from, lifecycle, whether the router may emit it, and the routing
lanes that pick it by default.
The path and the field names are a contract: the console reads the file as is
(#2541) and ``/api/models`` serves these fields (#2542).

Parsing is strict and standard-library only (no ``jsonschema``). Every
validation error names the row and the field, so a bad edit points at its line.
:func:`load_catalog` reads the packaged file once per process.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from kew.model_eol_snapshot import (
    _ALLOWED_EVIDENCE_KINDS,
    _EVIDENCE_KEYS,
    _ISO_DATE,
    _KNOWN_STATUSES,
)

CATALOG_SCHEMA = "kew/models@1"
CATALOG_FILENAME = "models.json"
PACKAGED_CATALOG_PATH = Path(__file__).parent / CATALOG_FILENAME
RUNNERS = frozenset({"claude-code", "codex", "opencode"})
LANES = ("default", "simple", "medium", "complex")
# ``first-party``: the price is the vendor's own list price, kept by hand;
# ``kew models refresh`` only reports an OpenRouter value that disagrees.
# ``openrouter``: the refresh may set the price from the OpenRouter models API.
PRICE_SOURCES = frozenset({"first-party", "openrouter"})

_CATALOG_KEYS = frozenset({"schema", "reviewedAt", "models"})
_ROW_KEYS = frozenset(
    {
        "id",
        "display_name",
        "provider",
        "runner",
        "price",
        "price_source",
        "lifecycle",
        "routable",
        "default_lanes",
        "aliases",
    }
)
_PRICE_KEYS = frozenset({"input_per_mtok", "output_per_mtok"})
_PRICE_OPTIONAL_KEYS = frozenset(
    {"cache_read_multiplier", "cache_write_multiplier", "cache_ttl_seconds", "tiers"}
)
_TIER_KEYS = frozenset({"above_prompt_tokens", "input_per_mtok", "output_per_mtok"})
_LIFECYCLE_KEYS = frozenset({"status", "shutdown", "replacement", "evidence"})


class CatalogError(ValueError):
    """The model catalog document is not usable as written."""


@dataclass(frozen=True)
class PriceTier:
    """List price for prompts longer than ``above_prompt_tokens`` tokens."""

    above_prompt_tokens: int
    input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class Price:
    """List price in USD per million tokens, with any long-prompt tiers."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_multiplier: float | None = None
    cache_write_multiplier: float | None = None
    cache_ttl_seconds: int | None = None
    tiers: tuple[PriceTier, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """One reviewed source backing a lifecycle, checked on ``checked_at``."""

    kind: str
    url: str
    checked_at: str


@dataclass(frozen=True)
class Lifecycle:
    """Lifecycle facts for one model, with the evidence behind them."""

    status: str
    shutdown: str | None
    replacement: str | None
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class ModelRow:
    """One model id and everything kew knows about it."""

    id: str
    display_name: str
    provider: str
    runner: str
    price: Price | None
    price_source: str
    lifecycle: Lifecycle
    routable: bool
    default_lanes: tuple[str, ...]
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ModelCatalog:
    """The parsed catalog: reviewed on ``reviewed_at``, rows sorted by id."""

    schema: str
    reviewed_at: date
    rows: tuple[ModelRow, ...]

    def get(self, model_id: str) -> ModelRow | None:
        """The row whose id is ``model_id``, or ``None``."""
        return next((row for row in self.rows if row.id == model_id), None)

    def lookup(self, model_id: str) -> ModelRow | None:
        """The row whose id or declared alias is exactly ``model_id``.

        There is no prefix matching: an id the catalog does not name, as an id
        or an alias, has no row, so it can never borrow another model's facts.
        """
        return next(
            (row for row in self.rows if model_id in (row.id, *row.aliases)), None
        )

    def routable_ids(self) -> frozenset[str]:
        """Ids of the rows the router may emit."""
        return frozenset(row.id for row in self.rows if row.routable)

    def lane_default(self, lane: str) -> str | None:
        """The id whose ``default_lanes`` names ``lane``, or ``None``."""
        return next((row.id for row in self.rows if lane in row.default_lanes), None)


def _fail(row: str, field: str, problem: str) -> CatalogError:
    return CatalogError(f"models.json row {row!r} field {field!r}: {problem}")


def _object(
    value: Any,
    row: str,
    field: str,
    keys: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(row, field, "must be a JSON object")
    missing = sorted(keys - set(value))
    unexpected = sorted(set(value) - keys - optional)
    if missing or unexpected:
        raise _fail(row, field, f"missing={missing} unexpected={unexpected} keys")
    return value


def _text(value: Any, row: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(row, field, "must be a nonempty string")
    return value


def _choice(value: Any, row: str, field: str, allowed: frozenset[str]) -> str:
    text = _text(value, row, field)
    if text not in allowed:
        raise _fail(row, field, f"{text!r} is not one of {sorted(allowed)}")
    return text


def _date(value: Any, row: str, field: str) -> str:
    text = _text(value, row, field)
    if not _ISO_DATE.fullmatch(text):
        raise _fail(row, field, f"must be an ISO YYYY-MM-DD date: {text!r}")
    return text


def _usd(value: Any, row: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise _fail(row, field, f"must be a non-negative number: {value!r}")
    return float(value)


def _list(value: Any, row: str, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(row, field, "must be a JSON array")
    return value


def _texts(value: Any, row: str, field: str) -> tuple[str, ...]:
    items = tuple(_text(item, row, field) for item in _list(value, row, field))
    if len(set(items)) != len(items):
        raise _fail(row, field, f"has duplicate entries: {list(items)}")
    return items


def _tier(value: Any, row: str) -> PriceTier:
    fields = _object(value, row, "price.tiers", _TIER_KEYS)
    above = fields["above_prompt_tokens"]
    if isinstance(above, bool) or not isinstance(above, int) or above <= 0:
        raise _fail(row, "price.tiers", f"above_prompt_tokens must be > 0: {above!r}")
    return PriceTier(
        above_prompt_tokens=above,
        input_per_mtok=_usd(fields["input_per_mtok"], row, "price.tiers"),
        output_per_mtok=_usd(fields["output_per_mtok"], row, "price.tiers"),
    )


def _tiers(value: Any, row: str) -> tuple[PriceTier, ...]:
    tiers = tuple(_tier(item, row) for item in _list(value, row, "price.tiers"))
    thresholds = [tier.above_prompt_tokens for tier in tiers]
    if any(low >= high for low, high in zip(thresholds, thresholds[1:], strict=False)):
        raise _fail(
            row,
            "price.tiers",
            f"above_prompt_tokens must strictly increase: {thresholds}",
        )
    return tiers


def _optional_usd(fields: Mapping[str, Any], key: str, row: str) -> float | None:
    return _usd(fields[key], row, f"price.{key}") if key in fields else None


def _optional_seconds(fields: Mapping[str, Any], key: str, row: str) -> int | None:
    if key not in fields:
        return None
    value = fields[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(row, f"price.{key}", f"must be a positive integer: {value!r}")
    return int(value)


def _price(value: Any, row: str) -> Price | None:
    if value is None:
        return None
    fields = _object(value, row, "price", _PRICE_KEYS, _PRICE_OPTIONAL_KEYS)
    return Price(
        input_per_mtok=_usd(fields["input_per_mtok"], row, "price.input_per_mtok"),
        output_per_mtok=_usd(fields["output_per_mtok"], row, "price.output_per_mtok"),
        cache_read_multiplier=_optional_usd(fields, "cache_read_multiplier", row),
        cache_write_multiplier=_optional_usd(fields, "cache_write_multiplier", row),
        cache_ttl_seconds=_optional_seconds(fields, "cache_ttl_seconds", row),
        tiers=_tiers(fields.get("tiers", []), row),
    )


def _evidence(value: Any, row: str) -> Evidence:
    field = "lifecycle.evidence"
    fields = _object(value, row, field, _EVIDENCE_KEYS)
    url = _text(fields["url"], row, field)
    if not url.startswith("https://"):
        raise _fail(row, field, f"url must be https: {url!r}")
    return Evidence(
        kind=_choice(fields["kind"], row, field, _ALLOWED_EVIDENCE_KINDS),
        url=url,
        checked_at=_date(fields["checkedAt"], row, field),
    )


def _lifecycle(value: Any, row: str) -> Lifecycle:
    fields = _object(value, row, "lifecycle", _LIFECYCLE_KEYS)
    shutdown, replacement = fields["shutdown"], fields["replacement"]
    evidence = _list(fields["evidence"], row, "lifecycle.evidence")
    return Lifecycle(
        status=_choice(fields["status"], row, "lifecycle.status", _KNOWN_STATUSES),
        shutdown=None
        if shutdown is None
        else _date(shutdown, row, "lifecycle.shutdown"),
        replacement=None
        if replacement is None
        else _text(replacement, row, "lifecycle.replacement"),
        evidence=tuple(_evidence(item, row) for item in evidence),
    )


def _routable(value: Any, row: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(row, "routable", f"must be true or false: {value!r}")
    return value


def _lanes(value: Any, row: str) -> tuple[str, ...]:
    lanes = _texts(value, row, "default_lanes")
    unknown = [lane for lane in lanes if lane not in LANES]
    if unknown:
        raise _fail(row, "default_lanes", f"unknown lanes {unknown}; known {LANES}")
    return lanes


def _require_routable_facts(model: ModelRow) -> None:
    # A routable model with no price meters at $0 and reads as free; one with
    # no evidence has a lifecycle nobody checked.
    if not model.routable:
        return
    if model.price is None:
        raise _fail(model.id, "price", "a routable model must have a price")
    if not model.lifecycle.evidence:
        raise _fail(
            model.id, "lifecycle.evidence", "a routable model needs an evidence url"
        )


def _row(value: Any, index: int) -> ModelRow:
    if isinstance(value, Mapping) and isinstance(value.get("id"), str):
        label = value["id"]
    else:
        label = f"#{index}"
    fields = _object(value, label, "(row)", _ROW_KEYS)
    row = _text(fields["id"], label, "id")
    model = ModelRow(
        id=row,
        display_name=_text(fields["display_name"], row, "display_name"),
        provider=_text(fields["provider"], row, "provider"),
        runner=_choice(fields["runner"], row, "runner", RUNNERS),
        price=_price(fields["price"], row),
        price_source=_choice(
            fields["price_source"], row, "price_source", PRICE_SOURCES
        ),
        lifecycle=_lifecycle(fields["lifecycle"], row),
        routable=_routable(fields["routable"], row),
        default_lanes=_lanes(fields["default_lanes"], row),
        aliases=_texts(fields["aliases"], row, "aliases"),
    )
    _require_routable_facts(model)
    return model


def _require_sorted_unique(rows: tuple[ModelRow, ...]) -> None:
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            raise _fail(row.id, "id", "duplicate id")
        seen.add(row.id)
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.id < previous.id:
            raise _fail(
                current.id, "id", f"rows must be sorted by id: after {previous.id!r}"
            )


def _require_distinct_aliases(rows: tuple[ModelRow, ...]) -> None:
    ids = {row.id for row in rows}
    owners: dict[str, str] = {}
    for row in rows:
        for alias in row.aliases:
            if alias in ids:
                raise _fail(row.id, "aliases", f"{alias!r} is also a model id")
            if alias in owners:
                raise _fail(
                    row.id,
                    "aliases",
                    f"{alias!r} is already an alias of {owners[alias]!r}",
                )
            owners[alias] = row.id


def _require_one_default_per_lane(rows: tuple[ModelRow, ...]) -> None:
    owners: dict[str, str] = {}
    for row in rows:
        for lane in row.default_lanes:
            if lane in owners:
                raise _fail(
                    row.id,
                    "default_lanes",
                    f"lane {lane!r} is already the default of {owners[lane]!r}",
                )
            owners[lane] = row.id
        if row.default_lanes and not row.routable:
            raise _fail(row.id, "default_lanes", "a lane default must be routable")


def _reviewed_at(value: Any) -> date:
    """The catalog's top-level review date, a strict ISO calendar date."""
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise CatalogError(
        f"models.json reviewedAt must be an ISO YYYY-MM-DD date: {value!r}"
    )


def parse_catalog(payload: Any) -> ModelCatalog:
    """Parse a catalog document, refusing anything off-contract."""
    if not isinstance(payload, Mapping) or set(payload) != _CATALOG_KEYS:
        raise CatalogError(
            f"models.json must be a JSON object with exactly {sorted(_CATALOG_KEYS)}"
        )
    if payload["schema"] != CATALOG_SCHEMA:
        raise CatalogError(
            f"models.json schema must be {CATALOG_SCHEMA!r}, got {payload['schema']!r}"
        )
    models = payload["models"]
    if not isinstance(models, list):
        raise CatalogError("models.json models must be a JSON array")
    rows = tuple(_row(item, index) for index, item in enumerate(models))
    _require_sorted_unique(rows)
    _require_distinct_aliases(rows)
    _require_one_default_per_lane(rows)
    return ModelCatalog(
        schema=CATALOG_SCHEMA,
        reviewed_at=_reviewed_at(payload["reviewedAt"]),
        rows=rows,
    )


@cache
def load_catalog() -> ModelCatalog:
    """The packaged catalog, read and validated once per process."""
    with PACKAGED_CATALOG_PATH.open(encoding="utf-8") as handle:
        return parse_catalog(json.load(handle))
