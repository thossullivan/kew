"""Read-only DAG API (#454). Session-gated per `read_guard` (audit finding
F7 / PR #446): the dependency is ROUTER-LEVEL so every route on this router
is gated the same way, with no per-route opt-out possible. This module has
no write path, so the CSRF-checking write dependency next to `read_guard`
in `kew.serve.security` must never be imported here, and no route may use a
greedy path-segment converter (that would let a hostile `epic_ref`/`node_id`
segment traverse into other refs) — both are asserted by a source-inspection
test in `tests/test_serve_api_epics.py` (via `inspect.getsource`).

`epic_ref` is validated twice before it ever reaches the service layer:
first a charset regex (rejects path-traversal-shaped input like "../etc"
or anything with a "/"), then exact membership in the store's known refs
(rejects charset-valid but nonexistent refs) — else 404. `node_id` is
validated the same way in two stages: printable length/path bounds (including
a percent-decoded "/" that reaches the handler after routing) here, then exact
membership in THAT epic's latest snapshot in the service layer — else 404.
Provider punctuation is deliberately opaque: "SFD-12" and "TEAM:12" are real
members when the snapshot says they are.

The `/stream` route serves the SSE transport #454 Decision 3 defines
(typed, data-only frames: snapshot/economics/heartbeat) via the shared
per-epic poller in `kew.serve.watch`; the client-side deep link for
`/epics/{ref}` (and the `/loops/{id}` deep link it repairs alongside) is
served by `SPAStaticFiles` in `kew.serve.app`, not this module.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from kew.serve.security import read_guard, stream_authorization
from kew.serve.watch import epic_stream
from kew.service.epics import (
    TICK_STRIP_LIMIT,
    TICK_STRIP_MAX,
    get_epic_detail,
    get_epic_timeline,
    get_node_detail,
    known_epic_refs,
    list_epics,
    list_ticks,
    tick_strip,
    valid_node_id,
)
from kew.service.models import (
    EpicDetailDTO,
    EpicNodeDetailDTO,
    EpicSummaryDTO,
    EpicTickDTO,
    EpicTickStripDTO,
    EpicTimelineDTO,
)

_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

router = APIRouter(
    prefix="/api/epics", tags=["epics"], dependencies=[Depends(read_guard)]
)


def _validated_ref(epic_ref: str, request: Request) -> str:
    if not _REF_RE.fullmatch(epic_ref):
        raise HTTPException(status_code=404, detail="epic not found")
    with request.app.state.store_factory() as store:
        if epic_ref not in known_epic_refs(store):
            raise HTTPException(status_code=404, detail="epic not found")
    return epic_ref


@router.get("")
def epics(request: Request) -> list[EpicSummaryDTO]:
    with request.app.state.store_factory() as store:
        return list_epics(store)


@router.get("/{epic_ref}")
def epic_detail(epic_ref: str, request: Request) -> EpicDetailDTO:
    ref = _validated_ref(epic_ref, request)
    with request.app.state.store_factory() as store:
        # The dispatch config is what lets the service join the judgment queue
        # onto the DAG's nodes (FUI-10); without it the snapshot would honestly
        # report the needs-you join as unavailable on every node.
        dto = get_epic_detail(store, ref, config=request.app.state.config)
    if dto is None:
        raise HTTPException(status_code=404, detail="epic not found")
    return dto


@router.get("/{epic_ref}/ticks")
def ticks(
    epic_ref: str,
    request: Request,
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
) -> list[EpicTickDTO]:
    ref = _validated_ref(epic_ref, request)
    with request.app.state.store_factory() as store:
        return list_ticks(store, ref, after_id=after, limit=limit)


@router.get("/{epic_ref}/tick-strip")
def tick_strip_route(
    epic_ref: str,
    request: Request,
    limit: int = Query(TICK_STRIP_LIMIT, ge=1, le=TICK_STRIP_MAX),
    changes: bool = Query(False),
) -> list[EpicTickStripDTO]:
    """The epic driver's last `limit` tick ATTEMPTS, oldest-first (#2304).

    A separate read from `/ticks` on purpose: that route returns full DAG
    snapshots, and building a strip out of them would parse two hundred
    snapshots to draw two hundred coloured slots. This one reads the
    `epic_tick_log` table, which is bounded in SQL and makes no GitHub call.
    `changes=true` serves only the rows a change strip draws (#2844 D23).
    """
    ref = _validated_ref(epic_ref, request)
    with request.app.state.store_factory() as store:
        return tick_strip(store, ref, limit=limit, changes=changes)


@router.get("/{epic_ref}/timeline")
def timeline(epic_ref: str, request: Request) -> EpicTimelineDTO:
    ref = _validated_ref(epic_ref, request)
    with request.app.state.store_factory() as store:
        dto = get_epic_timeline(store, ref)
    if dto is None:
        raise HTTPException(status_code=404, detail="epic not found")
    return dto


@router.get(
    "/{epic_ref}/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def stream(epic_ref: str, request: Request) -> StreamingResponse:
    ref = _validated_ref(epic_ref, request)
    return StreamingResponse(
        epic_stream(
            request.app.state.epic_hub,
            ref,
            is_authorized=stream_authorization(request),
        ),
        media_type="text/event-stream",
    )


@router.get("/{epic_ref}/nodes/{node_id}")
def node_detail(epic_ref: str, node_id: str, request: Request) -> EpicNodeDetailDTO:
    ref = _validated_ref(epic_ref, request)
    if not valid_node_id(node_id):
        raise HTTPException(status_code=404, detail="node not found")
    with request.app.state.store_factory() as store:
        dto = get_node_detail(store, ref, node_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="node not found")
    return dto
