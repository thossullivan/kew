"""Structural authorization coverage for EVERY route the serve app exposes.

The #1662 archetype: an unguarded route passed every suite because nothing
enumerated ``app.routes``. This walk is total — mounts, nested routers and
app-level routes included — and every route must land in exactly one bucket:

* an API route carrying the guard its method shape requires;
* one of the two auth-bootstrap exemptions;
* a NAMED non-API exemption with its reason.

Anything else fails, so a new route (or a new mount) cannot slip in unguarded.
"""

from contextlib import nullcontext
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route, WebSocketRoute

from kew.serve.app import create_app
from kew.serve.security import read_guard, require_write, sensitive_read

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Login bootstraps the session required by every guarded route. Session is the
# unauthenticated health probe used by the client to decide whether login is
# required. Neither route can depend on the credential it exists to establish.
_UNGUARDED_API_ROUTES = {
    ("/api/auth/login", frozenset({"POST"})): "session bootstrap",
    ("/api/auth/session", frozenset({"GET"})): "authentication health probe",
}

# Routes that carry no application data. Each is named with the reason it
# needs no guard; an unlisted non-API route fails the walk.
_EXEMPT_NON_API_ROUTES = {
    ("/{_path:path}", frozenset({"GET"})): "missing-build placeholder (reads nothing)",
}

# The one mount that serves files only: the SPA bundle at the root. Exempt by
# TYPE and PATH, not by name, so `app.mount("/api/x", sub, name="static")`
# cannot hide behind it (review round 2 showed a name-only exemption could).
_EXEMPT_MOUNT_PATH = ""


def _is_exempt_mount(route: Mount) -> bool:
    return route.path == _EXEMPT_MOUNT_PATH and isinstance(route.app, StaticFiles)


# Reads that must ALSO carry the sensitive-read stamp (router-level read_guard
# alone satisfies the method check, so pin them explicitly).
_SENSITIVE_READS = {("/api/runs/{run_id}/prompt", frozenset({"GET"}))}


def _walk(routes: list[Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Every route reachable from ``routes``, with mount prefixes applied."""
    found: list[tuple[str, Any]] = []
    for route in routes:
        if isinstance(route, Mount):
            if _is_exempt_mount(route):
                continue
            sub = getattr(route.app, "routes", None)
            assert sub is not None, (
                f"unlisted mount {prefix + route.path!r} ({route.name!r})"
            )
            found.extend(_walk(sub, prefix + route.path))
            continue
        # FastAPI >= 0.137 registers `include_router` as an `_IncludedRouter`
        # placeholder whose concrete routes come from `effective_candidates()`.
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates) and not hasattr(route, "path"):
            found.extend(_walk(list(candidates()), prefix))
            continue
        found.append((prefix + route.path, route))
    return found


def _classify(app: FastAPI) -> tuple[list[tuple[str, Any]], list[str]]:
    """Return (api_routes, problems) over the total walk."""
    api: list[tuple[str, Any]] = []
    problems: list[str] = []
    for path, route in _walk(app.routes):
        if isinstance(route, WebSocketRoute):
            problems.append(f"websocket route {path} has no guard model")
            continue
        methods = frozenset(getattr(route, "methods", None) or ())
        if path.startswith("/api/"):
            if not hasattr(route, "dependant"):
                # A raw Starlette route (or a mounted sub-app's own docs) can
                # carry no FastAPI dependency, so it can carry no guard.
                problems.append(
                    f"raw route {','.join(sorted(methods))} {path} cannot carry a guard"
                )
                continue
            api.append((path, route))
            continue
        if (
            isinstance(route, (APIRoute, Route))
            and (path, methods) in _EXEMPT_NON_API_ROUTES
        ):
            continue
        problems.append(f"unlisted non-API route {','.join(sorted(methods))} {path}")
    return api, problems


def _assert_route_guards(app: FastAPI) -> None:
    api, problems = _classify(app)
    assert not problems, "\n".join(problems)

    identities = {(path, frozenset(route.methods)) for path, route in api}
    assert _UNGUARDED_API_ROUTES.keys() <= identities, "an allowlisted route vanished"
    assert identities >= _SENSITIVE_READS, "a sensitive read route vanished"

    for path, route in api:
        identity = (path, frozenset(route.methods))
        if identity in _UNGUARDED_API_ROUTES:
            continue
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        required_guard = require_write if route.methods & _WRITE_METHODS else read_guard
        assert required_guard in dependencies, (
            f"{','.join(sorted(route.methods))} {path} lacks {required_guard.__name__}"
        )
        if identity in _SENSITIVE_READS:
            assert sensitive_read in dependencies, f"{path} lacks sensitive_read"


def _app(static_dir: Path | None = None) -> FastAPI:
    return create_app(
        lambda: nullcontext(object()), repo_dir=Path("."), static_dir=static_dir
    )


def test_every_route_is_guarded_or_named_exempt() -> None:
    _assert_route_guards(_app())


def test_every_named_exemption_still_matches_a_route() -> None:
    # A stale exemption is a hole waiting for a route to fill it: a route that
    # later reappears at that path would pass the walk unguarded. The
    # placeholder exists only without a bundle, so this walks that app.
    routes = {
        (path, frozenset(getattr(route, "methods", None) or ()))
        for path, route in _walk(_app().routes)
    }
    stale = sorted(path for path, _ in _EXEMPT_NON_API_ROUTES.keys() - routes)
    assert not stale, f"exempt entries match no route: {stale}"


def test_every_route_is_guarded_with_the_bundle_mounted(tmp_path: Path) -> None:
    # The production shape: the SPA bundle replaces the missing-build
    # placeholder with a root StaticFiles mount, which is the only exempt mount.
    (tmp_path / "index.html").write_text("<html></html>")
    app = _app(static_dir=tmp_path)
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert [(m.path, type(m.app).__name__) for m in mounts] == [("", "SPAStaticFiles")]
    _assert_route_guards(app)
    api, _ = _classify(app)
    assert len(api) >= 44, len(api)


def test_a_mount_cannot_hide_behind_the_static_name(tmp_path: Path) -> None:
    app = _app()
    (tmp_path / "x.txt").write_text("x")
    app.mount("/api/files", StaticFiles(directory=str(tmp_path)), name="static")
    try:
        _assert_route_guards(app)
    except AssertionError as exc:
        assert "/api/files" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-root mount named 'static' passed the walk")


def test_a_root_mount_must_be_a_file_server(tmp_path: Path) -> None:
    # Pins the isinstance half of the exemption: a FastAPI sub-app at the root
    # carrying the "static" name is walked, and its data route is named.
    app = _app()
    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @sub.get("/leak")
    async def _leak() -> dict[str, str]:  # pragma: no cover - never called
        return {"leak": "x"}

    app.mount("/", sub, name="static")
    try:
        _assert_route_guards(app)
    except AssertionError as exc:
        assert "/leak" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-file-server root mount passed the walk")


def test_api_route_count_is_pinned() -> None:
    # A silently dropped router would shrink the walk; pin the shape.
    api, _ = _classify(_app())
    assert len(api) >= 44, len(api)


def test_walk_catches_an_unguarded_mounted_sub_app() -> None:
    # The #1662 archetype the round-1 review reproduced: an unguarded route
    # behind a mount used to fall outside the enumeration entirely.
    app = _app()
    # No docs routes on the sub-app: they would fail the walk first (as raw
    # routes) and mask the unguarded API route this test is about.
    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @sub.get("/leak")
    async def _leak() -> dict[str, str]:  # pragma: no cover - never called
        return {"leak": "x"}

    app.mount("/api/sub", sub)
    try:
        _assert_route_guards(app)
    except AssertionError as exc:
        assert "/api/sub/leak" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unguarded mounted route passed the walk")


def test_walk_catches_an_unguarded_app_level_route() -> None:
    app = _app()

    @app.get("/export")
    async def _export() -> dict[str, str]:  # pragma: no cover - never called
        return {"data": "x"}

    try:
        _assert_route_guards(app)
    except AssertionError as exc:
        assert "/export" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an unguarded app-level route passed the walk")
