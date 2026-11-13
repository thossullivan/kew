"""FastAPI app factory.

A `store_factory` (not a live store) is injected: each request opens a fresh,
short-lived store in its own thread. kew.serve never imports run_store, so the
import-linter contract stays clean.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from kew.serve.api_approvals import approvals_router
from kew.serve.api_audit import audit_router
from kew.serve.api_auth import auth_router
from kew.serve.api_config import config_router
from kew.serve.api_dashboard import dashboard_router
from kew.serve.api_dispatch import dispatch_router
from kew.serve.api_epics import router as epics_router
from kew.serve.api_fleet import fleet_router
from kew.serve.api_health import (
    ReadinessCache,
    ReadinessChecks,
    health_router,
    readiness_router,
)
from kew.serve.api_models import models_router
from kew.serve.api_palette import palette_router
from kew.serve.api_prompts import prompts_router
from kew.serve.api_prs import prs_router
from kew.serve.api_report import report_router
from kew.serve.api_run_requests import run_requests_router
from kew.serve.api_runs import router as runs_router
from kew.serve.api_verbs import verbs_router
from kew.serve.api_work import work_router
from kew.serve.auth import SessionStore
from kew.serve.bundle import BundleStatus, evaluate_bundle, missing_build_html
from kew.serve.security import (
    HostHeaderMiddleware,
    SecurityHeadersMiddleware,
    allowed_hosts,
    allowed_origins,
)
from kew.serve.sweep import SweepWorker
from kew.serve.watch import EpicWatchHub
from kew.service.epics import fetch_epic_frames
from kew.service.models import BundleDTO, MetaDTO
from kew.service.palette_ask import ask_model, palette_ask_enabled
from kew.service.prompts import PromptRenderers

StoreFactory = Callable[[], AbstractContextManager[Any]]


class SPAStaticFiles(StaticFiles):
    """`StaticFiles(html=True)` only serves `index.html` at the mount root —
    it has no SPA fallback — so a hard refresh (or a shared link) on a
    client-side route like `/epics/{ref}` or `/loops/{id}` 404s. This
    subclass adds that fallback: any unmatched GET path outside `/api`
    gets the shell back with `cache-control: no-cache` (so a redeploy is
    never masked by a stale cached shell), while unmatched `/api` paths
    stay JSON 404s — the fallback must never swallow an API 404 into an
    HTML page. Non-GET requests never fall back (StaticFiles itself 405s
    them before this method's fallback branch runs).
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            response: Response | None = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None
        if response is not None and response.status_code != 404:
            return response
        if scope["method"] != "GET" or path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        shell = await super().get_response("index.html", scope)
        shell.headers["cache-control"] = "no-cache"
        return shell


def _kew_version() -> str:
    """Installed distribution version, with a source-checkout fallback."""
    try:
        return importlib.metadata.version("kew-cli")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _wire_auth_state(
    app: FastAPI,
    *,
    config: Any | None,
    token: str,
    sessions: SessionStore | None,
    bind_host: str,
    bind_port: int,
    expose: bool,
    extra_origins: list[str] | None = None,
) -> None:
    """Set auth-related state unconditionally; middleware/deps never AttributeError."""
    app.state.config = config
    app.state.token = token
    app.state.sessions = sessions or SessionStore()
    app.state.expose = expose
    app.state.allowed_hosts = allowed_hosts(bind_host, bind_port, expose)
    app.state.allowed_origins = allowed_origins(
        bind_host, bind_port, expose, extra_origins or ()
    )


def _mount_web(app: FastAPI, static_dir: Path | None) -> None:
    """Mount the SPA bundle, or — when it is absent — a loud hint page.

    Without a build, unmatched non-API GETs would 404 blankly; instead they
    render `missing_build_html()` so the operator sees what to do. API 404s and
    non-GET requests stay JSON (the fallback must never mask an API error).
    """
    if static_dir and Path(static_dir).is_dir():
        app.mount(
            "/", SPAStaticFiles(directory=str(static_dir), html=True), name="static"
        )
        return

    hint = missing_build_html()

    @app.get("/{_path:path}", include_in_schema=False)
    async def _missing_build(_path: str) -> Response:
        if _path == "api" or _path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return HTMLResponse(hint, status_code=200)


def create_app(
    store_factory: StoreFactory,
    repo_dir: Path,
    static_dir: Path | None = None,
    meta: MetaDTO | None = None,
    *,
    config: Any | None = None,
    token: str = "",
    sessions: SessionStore | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8765,
    expose: bool = False,
    extra_origins: list[str] | None = None,
    sweep: SweepWorker | None = None,
    frontend_dir: Path | None = None,
    health_reader: Any | None = None,
    readiness_checks: ReadinessChecks | None = None,
    prompt_renderers: PromptRenderers | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # The outcome sweep is opt-in (None in tests by default): production
        # `kew serve` injects one, the test harness leaves it off so no
        # background thread runs during unit tests.
        if sweep is not None:
            sweep.start()
        try:
            yield
        finally:
            if sweep is not None:
                sweep.stop()

    # No schema or docs pages on the wire: they map every route for a caller
    # that has no session. `app.openapi()` still builds the schema in-process
    # for scripts/export_openapi.py.
    app = FastAPI(
        title="kew",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store_factory = store_factory
    app.state.sweep = sweep
    # The health reader holds the probe cache the serve sweep warms. None is a
    # fine state: `api_health` creates one lazily, which is what every test and
    # every sweep-less serve gets.
    app.state.health_reader = health_reader
    # `kew doctor`'s checks, injected by `kew serve` (serve cannot import the
    # CLI). None leaves /api/readiness answering 503, as in most tests.
    app.state.readiness_cache = (
        ReadinessCache(readiness_checks) if readiness_checks is not None else None
    )
    # The prompt catalog's per-role preview builders (#3018), injected by
    # `kew serve`: they reach kew.graph, which serve cannot import. None leaves
    # GET /api/prompts/{role} answering 503.
    app.state.prompt_renderers = prompt_renderers
    # The launchpad's route resolver (#2302), created on first use by
    # `api_dispatch` over the health reader's probe cache.
    app.state.dispatch_resolver = None
    app.state.repo_dir = Path(repo_dir)
    app.state.meta = meta or MetaDTO(
        billing="metered",
        plan_label="",
        plan_monthly_usd=0.0,
        version=_kew_version(),
    )
    # Whether the palette offers ask mode (#2518) is the config's answer, not
    # the injected meta's, so every caller of create_app agrees with the route.
    app.state.meta = app.state.meta.model_copy(
        update={"palette_ask": palette_ask_enabled(config)}
    )
    # The model call behind POST /api/palette/ask; tests inject a fake.
    app.state.palette_asker = ask_model
    _wire_auth_state(
        app,
        config=config,
        token=token,
        sessions=sessions,
        bind_host=bind_host,
        bind_port=bind_port,
        expose=expose,
        extra_origins=extra_origins,
    )

    # Added last so it wraps every response — including HostHeaderMiddleware's
    # 403 — with the baseline hardening headers.
    app.add_middleware(HostHeaderMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(runs_router)
    app.include_router(auth_router)
    app.include_router(approvals_router)
    app.include_router(config_router)
    app.include_router(fleet_router)
    app.include_router(epics_router)
    app.include_router(report_router)
    app.include_router(audit_router)
    app.include_router(verbs_router)
    app.include_router(dashboard_router)
    app.include_router(work_router)
    app.include_router(prs_router)
    app.include_router(health_router)
    app.include_router(readiness_router)
    app.include_router(dispatch_router)
    app.include_router(palette_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(run_requests_router)

    def _epic_fetcher(
        epic_ref: str,
    ) -> Callable[
        [dict[str, Any]], tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]
    ]:
        # Runs inside ONE worker thread per poll (kew.serve.watch's pattern):
        # opens, queries, and closes a fresh store there rather than sharing
        # a long-lived connection across threads.
        def fetch(
            cursor: dict[str, Any],
        ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
            with store_factory() as store:
                # Same config the detail route passes (FUI-10): a streamed
                # snapshot replaces the detail read's whole snapshot, so
                # without it the live DAG would blank the needs-you join and
                # contradict the judgment queue on a ticking epic.
                return fetch_epic_frames(store, epic_ref, cursor, config=config)

        return fetch

    app.state.epic_hub = EpicWatchHub(_epic_fetcher)

    status: BundleStatus = evaluate_bundle(static_dir, frontend_dir)
    app.state.bundle_status = status
    app.state.bundle = BundleDTO(
        present=status.present,
        git_sha=status.git_sha,
        built_at=status.built_at,
        stale=status.stale,
    )

    _mount_web(app, static_dir)
    return app
