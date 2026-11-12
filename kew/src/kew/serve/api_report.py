"""Read-only model scorecard (cost per model). Loopback read via read_guard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from kew.config import resolved_config_path
from kew.serve.security import read_guard
from kew.service.models import ModelScorecardDTO, OutcomeReportDTO, SpendWindowsDTO
from kew.service.outcomes import DEFAULT_WEEKS, MAX_WEEKS, outcome_report
from kew.service.report import governed_spend_windows, model_scorecard

report_router = APIRouter(prefix="/api/report", dependencies=[Depends(read_guard)])


@report_router.get("/models")
def models(
    request: Request, days: int = Query(default=3, ge=1, le=90)
) -> ModelScorecardDTO:
    cfg = request.app.state.config
    lifecycle_map = cfg.model_eol.snapshot_models if cfg else None
    with request.app.state.store_factory() as store:
        return model_scorecard(store, days=days, lifecycle_map=lifecycle_map)


@report_router.get("/spend")
def spend(request: Request) -> SpendWindowsDTO:
    with request.app.state.store_factory() as store:
        return governed_spend_windows(store, resolved_config_path())


@report_router.get("/outcomes")
def outcomes(
    request: Request,
    weeks: int = Query(default=DEFAULT_WEEKS, ge=1, le=MAX_WEEKS),
) -> OutcomeReportDTO:
    """The weekly outcome scorecard — what the factory delivered, week by week.

    Bounded by ``weeks`` on every arm and GitHub-free: the ledger numbers are
    four aggregate queries and the "merged on main" denominator is a cached
    local ``git log`` against the repo directory serve already holds, read at
    the remote of the configured base branch rather than at ``main`` always.
    """
    with request.app.state.store_factory() as store:
        return outcome_report(
            store,
            weeks=weeks,
            repo_dir=request.app.state.repo_dir,
            config=request.app.state.config,
        )
