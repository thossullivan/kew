"""Operator-verb write API — resume / epic-retry / queue / orphan-ack (#684),
the no-change decisions (I11), and the one guarded work action (I20).

Thin shims over the ``kew.service.verbs`` functions (kew.serve may not import
kew.audit / kew.run_store): the validate → gate → audit → effect → read-back
flow lives in the service layer. Every write route depends on ``require_write``
(session + CSRF + Origin) and every read on ``read_guard``; queue, resume and
epic-retry are additionally approver-gated and require an explicit
``confirm: true``.

The I20 pair at the bottom is the direction this surface is heading: instead of
one route per verb, a client reads the projection and asks for an action the
projection itself derived. No new action vocabulary lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from kew.no_change import NoChangeError
from kew.serve.security import read_guard, require_write
from kew.service import verbs
from kew.service.models import WorkActionResultDTO, WorkViewDTO
from kew.service.verbs import (
    ConfirmRequired,
    NoChangeNotFound,
    OutcomeStale,
    VerbDenied,
    WorkActionStale,
    WorkActionUnavailable,
    ack_via_serve,
    queue_via_serve,
    resume_via_serve,
    retry_via_serve,
)
from kew.service.work import get_work_view, pending_run_max_age_s

verbs_router = APIRouter(prefix="/api/verbs", tags=["verbs"])


class ConfirmBody(BaseModel):
    confirm: bool = False


@verbs_router.post("/resume")
def resume(
    body: ConfirmBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    try:
        with request.app.state.store_factory() as store:
            return resume_via_serve(
                store, config=request.app.state.config, confirm=body.confirm
            )
    except ConfirmRequired as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@verbs_router.post("/queue/{issue}")
def queue(
    issue: str,
    body: ConfirmBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    # Accept both id currencies in the path. Queueing is GitHub-label-based
    # (its candidate set is `gh issue list`), so a non-numeric tracker id has
    # nothing to queue — reject it as an unknown issue rather than 422.
    try:
        issue_num = int(issue)
    except ValueError:
        raise HTTPException(
            status_code=404, detail=f"{issue!r} is not a queueable GitHub issue"
        ) from None
    try:
        with request.app.state.store_factory() as store:
            return queue_via_serve(
                store,
                issue=issue_num,
                config=request.app.state.config,
                confirm=body.confirm,
                source="web",
            )
    except ConfirmRequired as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@verbs_router.post("/epics/{ref}/retry/{item}")
def retry(
    ref: str,
    item: str,
    request: Request,
    body: ConfirmBody | None = None,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    # ``item`` accepts both id currencies (a GitHub number or a bare tracker
    # id); ``retry_via_serve`` validates it against the configured tracker and
    # raises ``InvalidItemIdError`` (a ``ValueError``) for a non-canonical id.
    # The body is optional so a bare POST (this route's pre-#2546 shape) is the
    # verb's own 400 "confirm required", not a schema 422.
    try:
        with request.app.state.store_factory() as store:
            return retry_via_serve(
                store,
                epic_ref=ref,
                item_id=item,
                config=request.app.state.config,
                confirm=body is not None and body.confirm,
            )
    except ConfirmRequired as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@verbs_router.post("/orphans/{pr}/ack")
def ack(
    pr: int,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    with request.app.state.store_factory() as store:
        return ack_via_serve(
            store, pr_number=pr, config=request.app.state.config, source="web"
        )


class RejectBody(BaseModel):
    reason: str = ""


# The I11 no-change verbs. Confirming one closes an item that has no diff to
# read afterwards, so the route adds nothing of its own: authority, freshness
# and the audit event all live in the service verb, and this shim only maps
# its refusals onto status codes. ``require_write`` (session + CSRF + Origin)
# gates all three, exactly like every other write route.


@verbs_router.post("/no-change/{judgment_id}/confirm")
def confirm_no_change(
    judgment_id: str,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    return _no_change_action(
        request,
        lambda store, config: verbs.confirm_no_change(
            store, judgment_id=judgment_id, config=config, source="web"
        ),
    )


@verbs_router.post("/no-change/{judgment_id}/reject")
def reject_no_change(
    judgment_id: str,
    body: RejectBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    return _no_change_action(
        request,
        lambda store, config: verbs.reject_no_change(
            store,
            judgment_id=judgment_id,
            reason=body.reason,
            config=config,
            source="web",
        ),
    )


@verbs_router.post("/no-change/{judgment_id}/retry-sync")
def retry_no_change_sync(
    judgment_id: str,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    return _no_change_action(
        request,
        lambda store, config: verbs.retry_no_change_sync(
            store, judgment_id=judgment_id, config=config, source="web"
        ),
    )


def _no_change_action(
    request: Request, action: Callable[[Any, Any], dict[str, Any]]
) -> dict[str, Any]:
    """Run one no-change verb and translate its refusals.

    403 is "you may not decide this", 409 is "this outcome is not decidable
    right now" (a settled judgment, or a base that moved), 404 is an unknown
    judgment and 400 is a malformed request. None of them is ever a silent
    success.
    """
    try:
        with request.app.state.store_factory() as store:
            return action(store, request.app.state.config)
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except NoChangeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except OutcomeStale as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except NoChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# --------------------------------------------------------------------------- #
# I20: the one guarded work action.
#
# This route deliberately exposes NO action names of its own. The body names an
# action the projection already offered on ``/api/dashboard/work`` and the
# revision it was read at; everything else — which run, judgment, job or pull
# request that action touches, and whether it may be requested at all — is
# resolved server-side from the same facts. A client therefore cannot construct
# a request the projection does not stand behind, and in particular cannot ask
# for a dispatch over work that is preserved: no such action is ever offered.
# --------------------------------------------------------------------------- #


@verbs_router.get("/work/{item_id}", dependencies=[Depends(read_guard)])
def work_view(item_id: str, request: Request) -> WorkViewDTO:
    """The exact view an action is decided against, revision included.

    The aggregate projection on ``/api/dashboard/work`` covers the items with
    a live delivery artifact. Preserved, unpublished and never-started work has
    no artifact yet and is precisely where the wrong action is most costly, so
    the action surface publishes its own single-item read rather than leaving a
    client to guess a revision — or to act without one.

    Composed under THIS host's in-flight bound, like the aggregate projection
    and like ``work_action`` below: ``view_revision`` digests the composed view,
    so reading a pending row against the bare floor here while every other
    surface reads it against ``pending_run_max_age_s`` would publish a revision
    no action could ever satisfy — and a different exception from the diamonds
    beside it.
    """
    with request.app.state.store_factory() as store:
        return get_work_view(
            store,
            item_id,
            pending_max_age_s=pending_run_max_age_s(request.app.state.config),
        )


class WorkActionBody(BaseModel):
    action: str
    expected_revision: str
    reason: str = ""


@verbs_router.post("/work/{item_id}/action")
def work_action(
    item_id: str,
    body: WorkActionBody,
    request: Request,
    _: object = Depends(require_write),
) -> WorkActionResultDTO:
    """Perform one derived-safe action and return the post-write projection.

    409 is the fail-closed answer to both "the world moved" (a stale expected
    revision) and "that is not on offer here" — in each case the caller must
    re-read the projection before deciding again, which is exactly the point.
    ``item_id`` is opaque: a Linear id rides through the path unchanged.
    """
    try:
        with request.app.state.store_factory() as store:
            return verbs.perform_work_action(
                store,
                item_id=item_id,
                action=body.action,
                expected_revision=body.expected_revision,
                reason=body.reason,
                config=request.app.state.config,
                source="web",
            )
    except VerbDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except (WorkActionStale, WorkActionUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except NoChangeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
