"""Read-only audit-log surface (hash chain). Loopback read via read_guard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from kew.ids import SQLITE_INT_MAX, SQLITE_INT_MIN
from kew.serve.security import read_guard
from kew.service.audit import (
    MAX_LIMIT,
    audit_histogram,
    audit_status,
    bucket_count,
    list_audit,
)
from kew.service.models import AuditHistogramDTO, AuditListDTO, AuditStatusDTO

audit_router = APIRouter(prefix="/api/audit", dependencies=[Depends(read_guard)])

# Shared query params. `since`/`until` bound the window (`until` exclusive);
# `item` is a durable item id (or an issue number); `epic` is an epic ref;
# `event_prefix` repeats and the prefixes OR together.
_SINCE = Query(default=None, description="ISO timestamp, inclusive")
_UNTIL = Query(default=None, description="ISO timestamp, exclusive")
_ITEM = Query(default=None, description="Durable item id, or issue number")
_EPIC = Query(default=None, description="Epic ref: its nodes + epic-level events")
_PREFIX = Query(default=None, description="Event name prefix; repeatable (OR)")

# Every integer param here is bound straight into SQLite, which refuses an int
# wider than int64 (``OverflowError`` — a 500). Declaring the bind range on the
# param makes an out-of-range value a 422 from the boundary instead, and puts
# the range in the OpenAPI schema. Purely widening: no value that ever answered
# is excluded, since binding it is what answering required (#2247).
_ISSUE = Query(default=None, ge=SQLITE_INT_MIN, le=SQLITE_INT_MAX)
_AFTER = Query(default=None, ge=SQLITE_INT_MIN, le=SQLITE_INT_MAX)


@audit_router.get("")
def audit_events(
    request: Request,
    event: str | None = Query(default=None),
    actor_type: str | None = Query(default=None),
    issue: int | None = _ISSUE,
    since: datetime | None = _SINCE,
    until: datetime | None = _UNTIL,
    item: str | None = _ITEM,
    epic: str | None = _EPIC,
    event_prefix: list[str] | None = _PREFIX,
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    after: int | None = _AFTER,
) -> AuditListDTO:
    with request.app.state.store_factory() as store:
        return list_audit(
            store,
            event=event,
            actor_type=actor_type,
            issue=issue,
            since=since,
            until=until,
            item=item,
            epic=epic,
            event_prefixes=event_prefix,
            limit=limit,
            after=after,
        )


@audit_router.get("/histogram")
def audit_event_histogram(
    request: Request,
    since: Annotated[datetime, Query(description="ISO timestamp, inclusive")],
    until: Annotated[datetime, Query(description="ISO timestamp, exclusive")],
    bucket_s: Annotated[
        int,
        Query(ge=1, le=SQLITE_INT_MAX, description="Bucket width in seconds"),
    ],
    event: str | None = Query(default=None),
    actor_type: str | None = Query(default=None),
    issue: int | None = _ISSUE,
    item: str | None = _ITEM,
    epic: str | None = _EPIC,
    event_prefix: list[str] | None = _PREFIX,
) -> AuditHistogramDTO:
    """Dense event counts over [since, until) — the feed's filters, aggregated.

    A window that asks for more than ``MAX_BUCKETS`` buckets (or runs
    backwards, or sets a width past the SQLite bind range) is a 422, not a
    silently coarsened answer.
    """
    # Validated before the store is opened, and separately from the read, so a
    # 422 can only ever mean "that window is not answerable".
    try:
        bucket_count(since, until, bucket_s)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with request.app.state.store_factory() as store:
        return audit_histogram(
            store,
            since=since,
            until=until,
            bucket_s=bucket_s,
            event=event,
            actor_type=actor_type,
            issue=issue,
            item=item,
            epic=epic,
            event_prefixes=event_prefix,
        )


@audit_router.get("/status")
def audit_chain_status(request: Request) -> AuditStatusDTO:
    repo_dir = Path(request.app.state.repo_dir)
    with request.app.state.store_factory() as store:
        return audit_status(store, repo_dir=repo_dir)
