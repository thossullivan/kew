"""Aggregate dashboard read surface (#700). Loopback read via read_guard.

A thin shim: the fail-soft assembly lives in ``kew.service.dashboard`` and the
snapshot-bound projection it reads — shared with the epic DAG's needs-you join
— lives in ``kew.service.attention`` (serve may not import ``kew.run_store`` /
``kew.audit``). Router-level ``read_guard`` — session-gated under ``--expose``
and for non-loopback peers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from kew.serve.security import read_guard
from kew.service.attention import work_projection
from kew.service.dashboard import assemble
from kew.service.models import DashboardDTO, WorkProjectionDTO

dashboard_router = APIRouter(
    prefix="/api/dashboard", dependencies=[Depends(read_guard)]
)


@dashboard_router.get("")
def dashboard(request: Request) -> DashboardDTO:
    with request.app.state.store_factory() as store:
        return assemble(store, request.app.state.config)


@dashboard_router.get("/work")
def dashboard_work(request: Request) -> WorkProjectionDTO:
    """The snapshot-bound work projection the console's rows are read from.

    Exposed alongside the aggregate so a client can hold ``projection_revision``
    and prove it is showing the same facts the console row set was built from,
    rather than comparing two surfaces at two undefined instants. Read-only,
    and guarded by the router-level ``read_guard`` like every other read here.
    """
    with request.app.state.store_factory() as store:
        return work_projection(store, request.app.state.config)
