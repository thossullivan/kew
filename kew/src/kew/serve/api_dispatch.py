"""Dispatch launchpad read API (#2302) — ``GET /api/dispatch/resolve``.

Read-only, behind ``read_guard``, and it makes **no GitHub call**: the
complexity is the caller's (kew's own classifier reads the item's labels, which
would be a tracker call), and the host facts behind containment come from the
health node's probe cache, which the serve sweep warms.

It also writes nothing — no audit row, no reservation, no probe lease. That is
the same rule ``kew dispatch --dry-run`` was breaking by emitting a
``routing_decision`` row for a dispatch that never happened, and it is the one
property that lets an operator open this page as often as they like.

The console does not dispatch. This endpoint hands back the resolved route and
the exact command; running it stays a deliberate act in a terminal.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from kew.serve.api_health import health_reader
from kew.serve.security import read_guard
from kew.service.dispatch_plan import RouteResolver, resolve_dispatch_plan

dispatch_router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

#: The longest item reference the endpoint will look at. A tracker key is a
#: handful of characters; anything longer is not one, and a bounded parameter
#: cannot become a bounded-query problem further down.
ITEM_MAX = 64


def _resolver(request: Request) -> RouteResolver:
    """The app's route resolver, created on first use.

    Held on ``app.state`` for the same reason the health reader is: resolving a
    contained route inspects the container runtime, and the launchpad re-reads
    every time the operator changes a choice. It shares the health reader's
    probe cache, so "is the image here?" has ONE answer on both pages and the
    sweep's warm reaches this endpoint too.

    The lazy create is deliberately unlocked (api_health's reasoning): two
    concurrent first requests can build two resolvers and one loses, which
    costs one extra resolution and cannot produce a wrong answer.
    """
    resolver = getattr(request.app.state, "dispatch_resolver", None)
    if resolver is None:
        cache = health_reader(request).cache
        resolver = RouteResolver() if cache is None else RouteResolver(cache=cache)
        request.app.state.dispatch_resolver = resolver
    return resolver


@dispatch_router.get("/resolve", dependencies=[Depends(read_guard)])
def get_dispatch_resolve(
    request: Request,
    item: Annotated[str | None, Query(max_length=ITEM_MAX)] = None,
    complexity: Annotated[
        Literal["simple", "medium", "complex"] | None, Query()
    ] = None,
    lane: Annotated[
        Literal["implement", "review", "remediation"], Query()
    ] = "implement",
) -> dict[str, Any]:
    """What would run if this item were dispatched now, field by field, with sources.

    An absent ``complexity`` is not an error: it is the tier label the ready
    queue's last read found on the item (#2785), and failing that ``unknown``
    with ``known: false`` and the default tier's route, because the label that
    would answer it lives on the tracker.
    """
    with request.app.state.store_factory() as store:
        return resolve_dispatch_plan(
            store,
            request.app.state.config,
            item=item,
            complexity=complexity,
            lane=lane,
            resolver=_resolver(request),
        )
