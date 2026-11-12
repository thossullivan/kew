"""Run-request API (#2537): the console's Run button and what became of it.

``POST /api/run-requests`` queues one command for the supervised ``kew drive``;
the two reads report what the drive did with it. The console process writes a
ledger row and an audit fact — it never launches the command, holds a
credential, or spawns anything: the drive, which already runs as the operator,
does. Thin shims over :mod:`kew.service.run_requests` (``kew.serve`` may not
import ``kew.run_store`` / ``kew.audit``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from kew.run_requests import NotRunnable, QueuedFactFailed, RequestStateError
from kew.serve.security import read_guard, require_write
from kew.service.run_requests import (
    KeyReused,
    NoDriveRunning,
    PairBusy,
    drive_state,
    get_request,
    list_requests,
    queue_request,
)
from kew.service.verbs import VerbDenied

run_requests_router = APIRouter(prefix="/api/run-requests", tags=["run-requests"])

#: Generous bounds on what the console sends: a runnable command is a handful
#: of short tokens, an item a tracker key.
COMMAND_MAX = 200
ITEM_MAX = 64
MODEL_MAX = 128
#: A submission key (#2975): the console's random hex, one per Run press.
KEY_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"


class RunRequestBody(BaseModel):
    command: str = Field(max_length=COMMAND_MAX)
    item_id: str | None = Field(default=None, max_length=ITEM_MAX)
    #: The launchpad's per-dispatch model (#2741); absent sends no override.
    model: str | None = Field(default=None, max_length=MODEL_MAX)
    #: The Run press this attempt is for (#2975): every attempt at one press
    #: sends the same key, and gets back the request the press queued.
    key: str | None = Field(default=None, pattern=KEY_PATTERN)


@run_requests_router.post("", response_model=dict[str, Any])
def post_run_request(
    body: RunRequestBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any] | JSONResponse:
    try:
        with request.app.state.store_factory() as store:
            return queue_request(
                store,
                command=body.command,
                item_id=body.item_id,
                source="web",
                model=body.model,
                config=request.app.state.config,
                key=body.key,
            )
    except NotRunnable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except PairBusy as exc:
        # The request holding the pair rides beside the sentence (#2975), so a
        # caller can follow it rather than read the ledger for it.
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "request": exc.request.to_json()},
        )
    except (NoDriveRunning, KeyReused) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except QueuedFactFailed as exc:
        # The request and its audit fact roll back together: nothing was
        # queued, a settled answer for the console to show, not to retry.
        raise HTTPException(status_code=424, detail=str(exc)) from None
    except RequestStateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@run_requests_router.get("", dependencies=[Depends(read_guard)])
def get_run_requests(
    request: Request,
    command: Annotated[str | None, Query(max_length=COMMAND_MAX)] = None,
    item: Annotated[str | None, Query(max_length=ITEM_MAX)] = None,
    model: Annotated[str | None, Query(max_length=MODEL_MAX)] = None,
) -> dict[str, Any]:
    """The newest requests (for one command, item or model — an empty model is
    a request sent without one), and whether a drive runs."""
    try:
        with request.app.state.store_factory() as store:
            return {
                "requests": list_requests(
                    store, command=command, item_id=item, model=model
                ),
                "drive": drive_state(store),
            }
    except NotRunnable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RequestStateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@run_requests_router.get("/{request_id}", dependencies=[Depends(read_guard)])
def get_run_request(
    request_id: Annotated[str, Path(max_length=ITEM_MAX)], request: Request
) -> dict[str, Any]:
    try:
        with request.app.state.store_factory() as store:
            found = get_request(store, request_id)
    except RequestStateError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    if found is None:
        raise HTTPException(status_code=404, detail="no such run request")
    return found
