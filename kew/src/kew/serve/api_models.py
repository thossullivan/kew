"""Model catalog read API (#2542) — ``GET /api/models``.

Serves the packaged catalog (``kew.model_catalog``) so the console offers a
model from a list instead of validating free text after the fact. Read-only,
behind ``read_guard``, and it makes no network call.

A retired model is left out unless the effective config still names it — as
``agent.model``, a ``[routing]`` entry's model or the reviewer model — because
then the operator has to see it, flagged, to move off it. The config read is
the same layering ``/api/config/overridable`` reports, DB overrides included.

Two lane marks, never confused: ``routed_lanes`` are the lanes this project's
effective config sends to the model (what the console calls "default for");
``default_lanes`` are kew's shipped defaults from the catalog, which a project's
``[routing]`` may override.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from kew.config import DispatchConfig, RouteEntry, load_config
from kew.model_catalog import ModelCatalog, ModelRow, load_catalog
from kew.runners.registry import resolve_runner
from kew.serve.security import read_guard

models_router = APIRouter(
    prefix="/api/models", tags=["models"], dependencies=[Depends(read_guard)]
)

_ROUTE_LANES = ("default", "simple", "medium", "complex")


def configured_model_names(config: DispatchConfig) -> set[str]:
    """Every model id the config names, as written (an alias stays an alias)."""
    names = {config.agent.model, config.review.model}
    routing = config.routing
    if routing is not None:
        for lane in _ROUTE_LANES:
            entry = getattr(routing, lane)
            # A bare string is the runner-name shorthand: it names no model.
            if isinstance(entry, RouteEntry):
                names.add(entry.model)
    return {name for name in names if name}


def effective_lane_models(config: DispatchConfig) -> dict[str, str]:
    """The model each lane runs under the effective config, as written.

    Read from ``resolve_runner`` — the call dispatch makes — so the console's
    marks cannot drift from what runs; the ``default`` lane is its ``None``
    tier. A lane that resolves to no model is left out (the runner picks it,
    so the console marks no row for it), as is one dispatch could not resolve.
    """
    lanes: dict[str, str] = {}
    for lane in _ROUTE_LANES:
        tier = None if lane == "default" else lane
        try:
            model = resolve_runner(config, tier).model
        except ValueError:
            continue
        if model:
            lanes[lane] = model
    return lanes


def _routed_lanes(
    catalog: ModelCatalog, lane_models: dict[str, str]
) -> dict[str, list[str]]:
    """Catalog row id -> the lanes routed to it, aliases resolved to their row."""
    routed: dict[str, list[str]] = {}
    for lane, name in lane_models.items():
        row = catalog.lookup(name)
        if row is not None:
            routed.setdefault(row.id, []).append(lane)
    return routed


def _row_payload(
    row: ModelRow, configured: bool, routed_lanes: list[str]
) -> dict[str, Any]:
    price = row.price
    return {
        "id": row.id,
        "display_name": row.display_name,
        "provider": row.provider,
        "runner": row.runner,
        "price": None
        if price is None
        else {
            "input_per_mtok": price.input_per_mtok,
            "output_per_mtok": price.output_per_mtok,
        },
        "lifecycle": {
            "status": row.lifecycle.status,
            "shutdown": row.lifecycle.shutdown,
            "replacement": row.lifecycle.replacement,
        },
        "routable": row.routable,
        "routed_lanes": routed_lanes,
        "default_lanes": list(row.default_lanes),
        "aliases": list(row.aliases),
        "configured": configured,
    }


def models_payload(
    catalog: ModelCatalog,
    configured: set[str],
    lane_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The catalog rows the console may offer, retired ones only if configured."""
    configured_ids = {
        row.id for name in configured if (row := catalog.lookup(name)) is not None
    }
    routed = _routed_lanes(catalog, lane_models or {})
    rows = [
        _row_payload(row, row.id in configured_ids, routed.get(row.id, []))
        for row in catalog.rows
        if row.lifecycle.status != "retired" or row.id in configured_ids
    ]
    return {"reviewed_at": catalog.reviewed_at.isoformat(), "models": rows}


@models_router.get("")
def get_models(request: Request) -> dict[str, Any]:
    """The model catalog the console picks from, retired rows only if configured."""
    with request.app.state.store_factory() as store:
        effective = load_config(store=store)
    return models_payload(
        load_catalog(),
        configured_model_names(effective),
        effective_lane_models(effective),
    )
