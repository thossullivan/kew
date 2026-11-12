"""Palette ask mode — ``POST /api/palette/ask`` (#2518).

The browser cannot call a model (the CSP forbids it), and an agent holding the
operator's session would act with the operator's whole authority. So the
server asks instead, and the answer can only be a ranking of entries the
palette already offered: ``kew.service.palette_ask`` drops every id the request
did not carry. Selecting a ranked entry is the browser's ordinary entry press,
confirm step included — nothing here runs anything.

A write route, not a read: each ask spends on a model call, so it needs the
session, the CSRF token and an allowed Origin like every other spend. Off by
default (``[serve] palette_ask``); while off the route answers 404 to an
authenticated caller, the same as a route that does not exist. Once the day's
spend has reached ``[budget] daily_usd`` it answers 429 with one sentence
naming the cap, before any model call and without recording anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kew.serve.security import require_write
from kew.service.palette_ask import (
    PaletteAskUnavailable,
    PaletteEntryIn,
    RankedEntry,
    daily_budget_refusal,
    filter_ranking,
    palette_ask_enabled,
    record_cost,
)

palette_router = APIRouter(
    prefix="/api/palette", tags=["palette"], dependencies=[Depends(require_write)]
)

MAX_ENTRIES = 500
"""More than the palette ever draws; a bound on what one prompt may carry."""


class PaletteAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    entries: list[PaletteEntryIn] = Field(min_length=1, max_length=MAX_ENTRIES)


class PaletteAskResponse(BaseModel):
    results: list[RankedEntry]
    cost_usd: float


@palette_router.post("/ask")
def ask(body: PaletteAskRequest, request: Request) -> PaletteAskResponse:
    config = request.app.state.config
    if not palette_ask_enabled(config):
        raise HTTPException(status_code=404, detail="not found")
    with request.app.state.store_factory() as store:
        refusal = daily_budget_refusal(store, config)
    if refusal is not None:
        raise HTTPException(status_code=429, detail=refusal)
    try:
        answer = request.app.state.palette_asker(body.query, body.entries, config)
    except PaletteAskUnavailable as exc:
        if exc.spent is not None:
            with request.app.state.store_factory() as store:
                record_cost(store, exc.spent, config)
        raise HTTPException(
            status_code=503, detail=f"palette ask unavailable: {exc}"
        ) from exc
    with request.app.state.store_factory() as store:
        cost = record_cost(store, answer, config)
    return PaletteAskResponse(
        results=filter_ranking(answer.ranked, body.entries), cost_usd=cost
    )
