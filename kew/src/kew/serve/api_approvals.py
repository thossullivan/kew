"""Read holds + the approve/deny write (the inbox's core write)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from kew.serve.security import read_guard, require_write
from kew.service.approval import decide_hold, list_open_holds
from kew.service.models import HoldDTO

approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])

_STATUS_HTTP = {
    "released": 200,
    "denied": 200,
    "not_held": 409,
    "gh_error": 502,
    "unauthorized": 403,
}


class DecisionBody(BaseModel):
    action: str  # "approve" | "deny"
    reason: str = ""


@approvals_router.get("", dependencies=[Depends(read_guard)])
def holds(request: Request) -> list[HoldDTO]:
    with request.app.state.store_factory() as store:
        return list_open_holds(store)


@approvals_router.post("/{issue}")
def decide(
    issue: str,
    body: DecisionBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    if body.action not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="action must be approve|deny")
    # Accept both id currencies in the path (the frontend sends the durable
    # item id). Budget-approval holds are int-keyed end to end today — the
    # non-GitHub approvals vertical is the deferred wave — so a non-numeric id
    # has no hold to decide: 404, mirroring the queue route's pattern.
    try:
        issue_num = int(issue)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"no approval hold for item {issue!r}"
        ) from None
    result = decide_hold(
        body.action,
        issue_num=issue_num,
        reason=body.reason,
        config=request.app.state.config,
        source="web",
    )
    code = _STATUS_HTTP.get(result.status, 500)
    if code >= 400:
        raise HTTPException(status_code=code, detail=result.status)
    return {"status": result.status, "issue": issue}
