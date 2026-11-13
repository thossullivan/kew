"""Artifact freshness read surface (#2303). Loopback read via read_guard.

A thin shim, like every other router here: the composition lives in
``kew.service.freshness`` (serve may not import ``kew.run_store``). Read-only,
bounded by ``limit``/``cursor``, every filter pushed into the store's own query,
and no GitHub call per request — the facts were journaled by the driver.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from kew.serve.security import read_guard
from kew.service.freshness import DEFAULT_LIMIT, MAX_LIMIT, compose_freshness
from kew.service.models import PrFreshnessDTO

prs_router = APIRouter(prefix="/api/prs", dependencies=[Depends(read_guard)])


@prs_router.get("/freshness")
def pr_freshness(
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None, max_length=256),
    epic: str | None = Query(None, max_length=256),
) -> PrFreshnessDTO:
    """One page of "what moved since the verdict" over every open artifact.

    ``epic`` narrows the page to one epic ref, pushed into the store's own
    query. The epic console draws one epic, and a global page can fill before
    reaching it — which on screen is indistinguishable from "nothing here is
    stale" (#2303).
    """
    with request.app.state.store_factory() as store:
        return compose_freshness(
            store, request.app.state.config, limit=limit, cursor=cursor, epic=epic
        )
