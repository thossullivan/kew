"""Fleet status (read) + stop (write)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from kew.serve.security import read_guard, require_write
from kew.service.fleet import fleet_status, stop_fleet_service
from kew.service.models import FleetStatusDTO, FleetStopResultDTO

fleet_router = APIRouter(prefix="/api/fleet", tags=["fleet"])


class StopBody(BaseModel):
    reason: str = ""


@fleet_router.get("/status", dependencies=[Depends(read_guard)])
def status(request: Request) -> FleetStatusDTO:
    with request.app.state.store_factory() as store:
        return fleet_status(store)


@fleet_router.post("/stop")
def stop(
    body: StopBody, request: Request, _: object = Depends(require_write)
) -> FleetStopResultDTO:
    return stop_fleet_service(
        reason=body.reason,
        config=request.app.state.config,
        store_factory=request.app.state.store_factory,
    )
