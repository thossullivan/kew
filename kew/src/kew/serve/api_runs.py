"""REST + SSE routes over the kew.service surface.

Each request opens a short-lived store via the injected `store_factory`, so the
DB connection is created and used in the request's own thread (FastAPI runs sync
routes in a worker threadpool — a long-lived SQLite connection would fail the
"created in a different thread" check).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from kew.ids import SQLITE_INT_MAX
from kew.serve.security import read_guard, sensitive_read, stream_authorization
from kew.serve.sse import event_stream, journal_stream
from kew.service.audit import MAX_RUN_AUDIT, run_audit_page
from kew.service.loops import MAX_LOOP_AUDIT, MAX_LOOP_ITERATIONS, list_loop_audit
from kew.service.loops import get_loop as get_loop_detail
from kew.service.loops import list_iterations as list_loop_iterations
from kew.service.loops import list_loops as list_loop_dtos
from kew.service.models import (
    BundleDTO,
    DiffDTO,
    FlightTimelineDTO,
    IterationDTO,
    LoopAuditDTO,
    LoopDetailDTO,
    LoopDTO,
    MetaDTO,
    ProtectedPromptDTO,
    RunAuditPageDTO,
    RunDTO,
    TimelineDTO,
)
from kew.service.runs import (
    MAX_RUN_PAGE,
    event_file_for,
    get_diff,
    get_flight_timeline,
    get_run,
    get_run_prompt,
    get_timeline,
    list_flight_events,
    list_runs,
    protected_prompt_root,
    run_exists,
)


def _not_found() -> HTTPException:
    """The one answer every "this store never heard of that run" path gives."""
    return HTTPException(status_code=404, detail="run not found")


def _require_run(request: Request, run_id: str) -> None:
    """404 a run this store has never heard of, before opening a stream.

    The existence question is :func:`run_exists` — one point read of the run
    row — not the ``get_run`` projection: a stream needs only the 404, and
    paying for posture, stage and the detail path's verdict reads on every
    poll, then discarding all of them, is the per-beat cost C3 exists to
    remove.
    """
    with request.app.state.store_factory() as store:
        if not run_exists(store, run_id):
            raise _not_found()


# Session-gated reads (audit 2026-07-01 F7): loopback-only while not exposed;
# under --expose every read — incl. SSE agent stdout and diffs — needs a session.
router = APIRouter(prefix="/api", tags=["runs"], dependencies=[Depends(read_guard)])


@router.get("/meta")
def meta(request: Request) -> MetaDTO:
    return cast(MetaDTO, request.app.state.meta)


@router.get("/bundle")
def bundle(request: Request) -> BundleDTO:
    return cast(BundleDTO, request.app.state.bundle)


@router.get("/runs")
def runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=MAX_RUN_PAGE),
    before: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=64),
    kind: str | None = Query(default=None, max_length=64),
) -> list[RunDTO]:
    """One bounded page of runs, newest first (C3).

    The console polls this every few seconds. The window, the status filter
    and the kind filter are SQL predicates, so the cost of a poll is the page,
    not the ledger. ``before`` is the previous page's last ``started_at``.
    """
    with request.app.state.store_factory() as store:
        return list_runs(store, limit=limit, before=before, status=status, kind=kind)


@router.get("/runs/{run_id}")
def run(run_id: str, request: Request) -> RunDTO:
    with request.app.state.store_factory() as store:
        dto = get_run(store, run_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="run not found")
    return dto


@router.get("/runs/{run_id}/timeline")
def timeline(run_id: str, request: Request) -> TimelineDTO:
    with request.app.state.store_factory() as store:
        dto = get_timeline(store, run_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="run not found")
    return dto


@router.get("/runs/{run_id}/audit")
def run_audit(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0, le=SQLITE_INT_MAX),
    limit: int = Query(default=MAX_RUN_AUDIT, ge=1, le=MAX_RUN_AUDIT),
) -> RunAuditPageDTO:
    """One forward page of this run's ledger events, oldest first (C3/#2300).

    `/api/audit` can only page backwards, which is the wrong direction for a
    run page: it opens at the run's first event and walks forward, and a live
    run resumes from the cursor it last read. Both the window and the run
    narrowing are SQL predicates over the ``audit_events.run_id`` index, and no
    GitHub call is made — this is the ledger, read.

    A run this store has never heard of is a 404, the same answer `/flight`
    gives: "no such run" and "that run recorded nothing" are different
    statements, and an empty page is the second one. That check is a point read
    of the run row inside this request's own store scope, so a live tail's
    poll costs one ``runs`` read and one ``audit_events`` page — never the
    detail projection's posture, stage and verdict reads on top.
    """
    with request.app.state.store_factory() as store:
        if not run_exists(store, run_id):
            raise _not_found()
        return run_audit_page(store, run_id, after_seq=after_seq, limit=limit)


@router.get("/runs/{run_id}/nodes/{node_id}/diff")
def diff(run_id: str, node_id: str, request: Request) -> DiffDTO:
    with request.app.state.store_factory() as store:
        dto = get_diff(store, run_id, node_id, request.app.state.repo_dir)
    if dto is None:
        raise HTTPException(status_code=404, detail="no diff at this node")
    return dto


@router.get(
    "/runs/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def events(run_id: str, request: Request, replay: bool = True) -> StreamingResponse:
    with request.app.state.store_factory() as store:
        event_file = event_file_for(store, run_id)
    if event_file is None:
        raise HTTPException(status_code=404, detail="run not found")

    return StreamingResponse(
        event_stream(
            event_file, replay=replay, is_authorized=stream_authorization(request)
        ),
        media_type="text/event-stream",
    )


@router.get("/runs/{run_id}/flight")
def flight(
    run_id: str, request: Request, attempt: int | None = None
) -> FlightTimelineDTO:
    """The canonical receipt-backed evidence projection for one run (I16).

    A run with no evidence still answers, with every dimension marked
    unavailable — that is a different statement from "no such run", and only
    the second one is a 404.
    """
    with request.app.state.store_factory() as store:
        if get_run(store, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return get_flight_timeline(
            store,
            run_id,
            attempt_number=attempt,
            project_root=request.app.state.repo_dir,
        )


def _journal_cursor(request: Request, after_seq: int) -> int:
    """Where to resume: the browser's Last-Event-ID wins over the query param.

    A reconnecting EventSource replays its own cursor in the header without
    the client's help, so honouring it is what makes a dropped connection —
    including one dropped by a server restart — resume rather than repeat.
    """
    header = request.headers.get("last-event-id", "")
    return int(header) if header.isdigit() else after_seq


@router.get(
    "/runs/{run_id}/journal",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def journal(run_id: str, request: Request, after_seq: int = 0) -> StreamingResponse:
    """Tail the run's evidence journal — the durable ledger, not the log file.

    Each frame is one :class:`FlightEventDTO` carrying its ledger position as
    the SSE id, so this stream and ``/flight`` are the same sequence read two
    ways rather than two answers that can drift.
    """
    _require_run(request, run_id)
    state = request.app.state

    def _fetch(cursor: int) -> list[dict[str, Any]]:
        # Runs in a worker thread per poll: opens, queries and closes its own
        # store there rather than sharing a connection across threads.
        with state.store_factory() as store:
            return [
                event.model_dump()
                for event in list_flight_events(store, run_id, after_seq=cursor)
            ]

    return StreamingResponse(
        journal_stream(
            _fetch,
            after_seq=_journal_cursor(request, after_seq),
            is_authorized=stream_authorization(request),
        ),
        media_type="text/event-stream",
    )


@router.get("/runs/{run_id}/prompt", dependencies=[Depends(sensitive_read)])
def prompt(
    run_id: str, request: Request, attempt: int | None = None
) -> ProtectedPromptDTO:
    """The exact compiled prompt — the only payload that may carry the bytes.

    Never preloaded and never embedded: no list, detail, timeline, diff or
    stream reaches it, and the SPA fetches it only when an operator explicitly
    asks to see the prompt. `sensitive_read` is what makes that ask cost a
    real session and marks the answer uncacheable.
    """
    state = request.app.state
    with state.store_factory() as store:
        dto = get_run_prompt(
            store,
            run_id,
            prompt_root=protected_prompt_root(state.config, state.repo_dir),
            attempt_number=attempt,
        )
    if dto is None:
        raise HTTPException(status_code=404, detail="no retained prompt for this run")
    return dto


@router.get("/loops")
def loops(request: Request, status: str | None = None) -> list[LoopDTO]:
    with request.app.state.store_factory() as store:
        return list_loop_dtos(store, status=status)


@router.get("/loops/{loop_id}")
def loop(loop_id: str, request: Request) -> LoopDetailDTO:
    with request.app.state.store_factory() as store:
        dto = get_loop_detail(store, loop_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="loop not found")
    return dto


@router.get("/loops/{loop_id}/iterations")
def loop_iterations(
    loop_id: str,
    request: Request,
    limit: int = Query(default=MAX_LOOP_ITERATIONS, ge=1, le=MAX_LOOP_ITERATIONS),
) -> list[IterationDTO]:
    """One bounded page of a loop's newest iterations, oldest-first (#2304).

    The loops roster draws an iteration strip per row and the loop page draws a
    full one; both used to get their iterations from `/loops/{id}`, which
    returns every iteration a loop ever ran. No 404 — a loop with no
    iterations returns [].
    """
    with request.app.state.store_factory() as store:
        return list_loop_iterations(store, loop_id, limit=limit)


@router.get("/loops/{loop_id}/audit")
def loop_audit(
    loop_id: str,
    request: Request,
    limit: int = Query(default=MAX_LOOP_AUDIT, ge=1, le=MAX_LOOP_AUDIT),
) -> list[LoopAuditDTO]:
    # Read-only audit timeline, bounded in SQL. No 404 — a loop with no
    # events returns [].
    with request.app.state.store_factory() as store:
        return list_loop_audit(store, loop_id, limit=limit)
