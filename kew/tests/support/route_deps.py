"""What a mounted FastAPI route actually depends on, read off the live router.

A read-only router is one no route of which resolves ``require_write``, and a
guarded one is one whose every route resolves ``read_guard``. Both are facts
about the dependency tree FastAPI builds, so the tests ask that tree rather
than grep the module for the names (#2492).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from fastapi.routing import APIRoute, APIRouter


def dependency_calls(route: APIRoute) -> set[Callable[..., Any]]:
    """Every callable FastAPI resolves to serve *route*, at any depth."""

    def walk(dependant: Any) -> Iterator[Callable[..., Any]]:
        for sub in dependant.dependencies:
            if sub.call is not None:
                yield sub.call
            yield from walk(sub)

    return set(walk(route.dependant))


def api_routes(router: APIRouter) -> list[APIRoute]:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert routes, "the router mounts no API route"
    return routes
