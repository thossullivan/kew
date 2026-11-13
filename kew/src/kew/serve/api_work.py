"""Per-item read surface (`/api/work/{item_id}/…`) — the Work screen's joins.

A thin shim, like every other router here: the projections live in
``kew.service.pipeline`` and ``kew.service.evidence_chain`` (kew.serve may not
import ``kew.run_store`` / ``kew.audit``). Router-level ``read_guard`` —
session-gated under ``--expose`` and for non-loopback peers, loopback-open
otherwise — so a route added to this module is guarded by construction rather
than by remembering to. The description read costs a tracker request rather
than a store query, so it adds ``offbox_read`` on top: what a cross-origin page
can drive is the difference between the two guards.

``item_id`` is opaque: a GitHub number and a Linear key both ride through the
path unchanged, are never parsed here, and reach the service layer as the
store's own key. An item the console knows but nothing has run for answers
four pending stages — an empty pipeline, not a missing one (#2572) — and only
an id nothing here knows is a 404. The evidence chain answers for any id,
because the guarantees its route promises exist before the work does.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from kew.serve.security import offbox_read, read_guard
from kew.service.description import item_description
from kew.service.evidence_chain import item_evidence
from kew.service.models import ItemDescriptionDTO, ItemEvidenceDTO, ItemPipelineDTO
from kew.service.pipeline import route_pipeline

work_router = APIRouter(
    prefix="/api/work", tags=["work"], dependencies=[Depends(read_guard)]
)


@work_router.get("/{item_id}/pipeline")
def pipeline(item_id: str, request: Request) -> ItemPipelineDTO:
    """The lanes and stages this work item has moved through.

    Open-ended on purpose: the payload lists whatever lanes and stages the
    facts support, so a surface renders the answer instead of a fixed grid.
    """
    with request.app.state.store_factory() as store:
        dto = route_pipeline(store, item_id, config=request.app.state.config)
    if dto is None:
        raise HTTPException(status_code=404, detail="item not found")
    return dto


@work_router.get("/{item_id}/evidence")
def evidence(item_id: str, request: Request) -> ItemEvidenceDTO:
    """The ordered guarantees this item's route promises, and their evidence.

    Always answers: four rows all ``pending`` is the honest projection for an
    item nothing has run for, where a 404 would say the id does not exist.
    ``repo_dir`` carries the anchor file, so the chain verdict behind
    ``attested`` is the one ``kew audit verify`` reaches.
    """
    with request.app.state.store_factory() as store:
        return item_evidence(
            store,
            item_id,
            config=request.app.state.config,
            repo_dir=Path(request.app.state.repo_dir),
        )


@work_router.get("/{item_id}/description", dependencies=[Depends(offbox_read)])
def description(item_id: str, request: Request) -> ItemDescriptionDTO:
    """What a person wrote about this item on its tracker (FUI-31).

    The one remote read on this surface, served from a TTL cache. It never
    fails the request: a tracker that is down, absent or misconfigured answers
    ``unavailable`` (or ``stale``, over a reading already held) with the reason,
    which is what the Work screen's "Open on GitHub" fallback renders. The repo
    is the server's own ``project.repo`` — no client-supplied target is honored.

    Synchronous, like its neighbours, so it runs on a worker thread — and the
    service bounds how many tracker reads may be in flight at once, because a
    burst of ids nothing has cached must not hold those threads behind a remote
    tracker. Past that bound the read is refused, which is an answer here.

    It is also the one read here that carries a guard of its own. Because the
    cost of driving it lands off-box — a subprocess and the operator's GitHub
    rate budget, which the dispatch driver shares — ``offbox_read`` adds a
    cross-site refusal on top of the router's ``read_guard``, so a page the
    operator merely visits cannot drive it; and the service caps how many
    tracker reads any caller may spend per minute, so a permitted one cannot
    either.
    """
    return item_description(item_id, config=request.app.state.config)
