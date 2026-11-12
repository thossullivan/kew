"""Loop + iteration + audit reads. Read-only surface for serve (and any future TUI).

The store is injected (a RunStore-shaped object). This module avoids importing
run_store/orchestrator/audit directly — NOT because the import-linter contract
names kew.service (it does not; the contract's source_modules is ["kew.serve"]),
but to MIRROR ``kew.service.runs`` and keep the service layer dependency-light /
forward-compatible. ``drive_mode`` is DERIVED from the runner's loop_style
(spec R3) via Plan 1's shared ``loop_style_to_drive_mode`` helper, never read
from a stored column.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from kew.motion import loop_motion
from kew.runners.protocol import loop_style_to_drive_mode
from kew.runners.registry import _get_runner_factories
from kew.service.models import (
    IterationDTO,
    LoopAuditDTO,
    LoopDetailDTO,
    LoopDTO,
)

_logger = logging.getLogger(__name__)

# Plan 2 emits per-loop audit events (loop_started, iteration_recorded,
# verification_checked, needs_human_raised, loop_finished) with
# payload={"loop_id": ...}. list_loop_audit() filters purely on that loop_id —
# NOT on a hard-coded event-name set, so any new loop event Plan 2 adds later is
# surfaced automatically rather than silently dropped. The card shows them in
# store order (newest-first).

_logger = logging.getLogger(__name__)

MAX_LOOP_AUDIT = 200
"""Largest page of loop audit events one request may ask for. The payload
``$.loop_id`` arm is unindexed, so an unbounded read scans the ledger."""


class _Store(Protocol):
    def list_loops(self, status: str | None = None) -> list[Any]: ...
    def get_loop(self, loop_id: str) -> Any | None: ...
    def list_iterations(self, loop_id: str) -> list[Any]: ...
    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        since: Any | None = None,
        loop_id: str | None = None,
    ) -> list[Any]: ...

    # ``last_iteration_at_by_loop`` and ``list_iterations_tail`` are
    # deliberately NOT declared above: both are read through getattr, so a
    # staleness reading degrades to the ``updated_at`` stamps and an iteration
    # page degrades to a Python-side trim against a store without them, instead
    # of refusing to serve the page.


def _iso(value: Any) -> str | None:
    return (
        value.isoformat()
        if hasattr(value, "isoformat")
        else (str(value) if value else None)
    )


def _s(rec: Any, name: str) -> str:
    return str(getattr(rec, name, "") or "")


def _i(rec: Any, name: str) -> int:
    return int(getattr(rec, name, 0) or 0)


def _f(rec: Any, name: str) -> float:
    return float(getattr(rec, name, 0.0) or 0.0)


def _drive_mode_for(runner_name: str) -> str:
    """Map a runner name to its display drive mode via its loop_style.

    native -> "delegated" (kew supervises the runner's own loop);
    unresolvable runner, OR a runner instance with no ``loop_style`` -> "unknown".
    """
    factory = _get_runner_factories().get(runner_name)
    if factory is None:
        return "unknown"
    style = getattr(factory(), "loop_style", None)
    if style is None:
        return "unknown"
    return loop_style_to_drive_mode(style)


#: A `pursuing` loop is STALE once it has gone quiet for this many of its own
#: cadences. 2× absorbs one fully-missed iteration before the loop is called
#: stuck, exactly as the epic driver's heartbeat threshold does.
LOOP_STALE_CADENCES = 2.0

#: The cadence assumed for a loop that has no recorded one. Loops carry no
#: iteration interval, so "has it moved lately?" needs an expectation. 30
#: minutes — with the 2x threshold, a loop that has gone quiet for an hour
#: reads stale, whatever its runner's real rhythm (#2297).
LOOP_CADENCE_S = 1800.0

#: Statuses a staleness reading applies to. A terminal loop is not stale, it is
#: finished; a paused one is stopped on purpose.
_STALE_ELIGIBLE = ("pursuing",)


def _as_dt(value: Any, *, loop_id: str) -> datetime | None:
    """One loop timestamp as an aware datetime, or None when unreadable.

    An unparseable stamp is tolerated, never repaired (operator rule), and it is
    logged: it flows into "not stale", and a surface that stays quiet about a
    quiet loop deserves to be explainable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            _logger.warning(
                "loop %s has an unreadable timestamp %r; staleness is not assessed",
                loop_id,
                value,
            )
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _quiet_window_s(rec: Any) -> float:
    """How long this loop may legitimately write NOTHING before it reads stale.

    A single agent run may burn the loop's whole ``wall_clock_secs`` budget
    without touching the store — iterations and ``updated_at`` are written when
    a run ENDS — so the loop's own wall clock is the floor of the window, plus
    one cadence of slack for the finalizing write. Without that floor a loop
    working normally inside a default 2-hour budget read stale after an hour
    (#2297). The 2× cadence keeps the window sane for a loop whose budget is
    smaller than one plausible iteration gap.
    """
    return max(
        LOOP_STALE_CADENCES * LOOP_CADENCE_S,
        float(_i(rec, "wall_clock_secs")) + LOOP_CADENCE_S,
    )


def _quiet_since(rec: Any, last_iteration_at: Any) -> datetime | None:
    """When this loop last wrote ANYTHING, or None when silence is unprovable.

    The NEWEST thing the loop wrote: its last recorded iteration
    (``add_iteration``, which does not touch ``updated_at``), else its
    ``updated_at`` (session start / resume / terminal), else ``created_at``.
    Reading ``updated_at`` alone called a loop on its third iteration stale.

    DERIVED on read, never written: the two loops that have sat at ``pursuing``
    since 2026-07-15 are history, and history is tolerated, not repaired
    (operator rule). A missing stamp is simply skipped; a stamp that is PRESENT
    but unreadable abandons the reading entirely — an unreadable stamp could be
    the most recent one, so it is never evidence of silence (the same fail-safe
    the driver heartbeat uses), and None here establishes no silence at all.

    ONE reading, because ``stale`` and ``motion`` are two statements about the
    same fact and a DTO that said a loop was both moving and waiting on kew was
    unreadable: both sit on this stamp.
    """
    loop_id = str(getattr(rec, "loop_id", "?"))
    present = [
        value
        for value in (
            last_iteration_at,
            getattr(rec, "updated_at", None),
            getattr(rec, "created_at", None),
        )
        if value is not None
    ]
    readable = [_as_dt(value, loop_id=loop_id) for value in present]
    if not present or not all(readable):
        return None
    return max(stamp for stamp in readable if stamp is not None)


def _quiet_secs(rec: Any, now: datetime, last_iteration_at: Any) -> float | None:
    """How long this loop has been quiet, or None when that is unknowable."""
    newest = _quiet_since(rec, last_iteration_at)
    return None if newest is None else (now - newest).total_seconds()


def _loop_stale(rec: Any, quiet_secs: float | None) -> bool:
    """Whether this loop has gone quiet past its :func:`_quiet_window_s`.

    ``quiet_secs`` is :func:`_quiet_secs`; None (no stamp, or one that is
    present but unreadable) is not evidence of silence and reads not-stale.
    """
    if _s(rec, "status") not in _STALE_ELIGIBLE:
        return False
    return quiet_secs is not None and quiet_secs > _quiet_window_s(rec)


def _loop_to_dto(
    rec: Any, *, now: datetime | None = None, last_iteration_at: Any = None
) -> LoopDTO:
    status = _s(rec, "status")
    # A `pursuing` loop only animates while something is still advancing it:
    # `loop_motion` reads an untouched one as waiting on kew rather than
    # letting the console run a halo on a loop nobody is driving (#2305).
    #
    # ONE reading behind both words: the same quiet stamp (so a loop whose only
    # recent write is an iteration — `add_iteration` leaves `updated_at` alone —
    # is not drawn still) AND the same window (`_quiet_window_s`, the loop's own
    # budget, not a global 3h). `motion == "waiting_kew"` beside `stale=False`
    # on one DTO is unreadable, so it is made unreachable here.
    quiet = _quiet_secs(rec, now or datetime.now(UTC), last_iteration_at)
    return LoopDTO(
        loop_id=str(rec.loop_id),
        motion=loop_motion(
            status, age_secs=quiet, stale_after_secs=_quiet_window_s(rec)
        ),
        source=_s(rec, "source"),
        issue_num=getattr(rec, "issue_num", None),
        repo=getattr(rec, "repo", None),
        goal_text=_s(rec, "goal_text"),
        runner_name=_s(rec, "runner_name"),
        model=_s(rec, "model"),
        drive_mode=_drive_mode_for(_s(rec, "runner_name")),
        status=status,
        status_reason=_s(rec, "status_reason"),
        needs_human=status == "needs_human",
        stale=_loop_stale(rec, quiet),
        budget_usd=getattr(rec, "budget_usd", None),
        spent_usd=_f(rec, "spent_usd"),
        wall_clock_secs=_i(rec, "wall_clock_secs"),
        max_steps=_i(rec, "max_steps"),
        step_count=_i(rec, "step_count"),
        created_at=_iso(getattr(rec, "created_at", None)),
        updated_at=_iso(getattr(rec, "updated_at", None)),
        finished_at=_iso(getattr(rec, "finished_at", None)),
    )


def _iteration_to_dto(rec: Any) -> IterationDTO:
    return IterationDTO(
        iteration_id=str(rec.iteration_id),
        idx=_i(rec, "idx"),
        kind=_s(rec, "kind"),
        status=_s(rec, "status"),
        cost_usd=_f(rec, "cost_usd"),
        tokens=_i(rec, "tokens"),
        run_id=getattr(rec, "run_id", None),
        started_at=_iso(getattr(rec, "started_at", None)),
        finished_at=_iso(getattr(rec, "finished_at", None)),
    )


def _audit_summary(payload: dict[str, Any] | None) -> str:
    """Best-effort short human string from an audit payload (never raw-dump)."""
    if not isinstance(payload, dict):
        return ""
    for key in ("summary", "reason", "status_reason", "detail", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _audit_to_dto(rec: Any) -> LoopAuditDTO:
    payload = getattr(rec, "payload", None)
    loop_id = payload.get("loop_id") if isinstance(payload, dict) else None
    return LoopAuditDTO(
        id=int(getattr(rec, "id", 0) or 0),
        timestamp=str(getattr(rec, "timestamp", "") or ""),
        event=str(getattr(rec, "event", "") or ""),
        actor_type=getattr(rec, "actor_type", None),
        actor_id=getattr(rec, "actor_id", None),
        loop_id=str(loop_id) if loop_id is not None else None,
        summary=_audit_summary(payload),
    )


def _last_iteration_at(store: _Store) -> dict[str, str]:
    """loop_id -> newest recorded iteration stamp, in ONE grouped query.

    Tolerated as absent (a store double, or a reader predating the read) so a
    staleness reading degrades to the ``updated_at`` stamps rather than raising.
    """
    reader = getattr(store, "last_iteration_at_by_loop", None)
    if reader is None:
        return {}
    return dict(reader())


def _newest_iteration_at(iterations: list[IterationDTO]) -> str | None:
    """The newest stamp among already-read iterations (no extra query).

    Compared as text: every writer stamps UTC ISO-8601, where lexical order is
    chronological order.
    """
    stamps = [
        stamp
        for it in iterations
        for stamp in (it.finished_at, it.started_at)
        if stamp is not None
    ]
    return max(stamps) if stamps else None


def list_loops(
    store: _Store, *, status: str | None = None, now: datetime | None = None
) -> list[LoopDTO]:
    # One clock for the whole page, so two rows cannot be aged (motion) or
    # judged stale against two different readings of now (#2305).
    now = now or datetime.now(UTC)
    last_iteration = _last_iteration_at(store)
    return [
        _loop_to_dto(
            rec, now=now, last_iteration_at=last_iteration.get(str(rec.loop_id))
        )
        for rec in store.list_loops(status)
    ]


def get_loop(
    store: _Store, loop_id: str, *, now: datetime | None = None
) -> LoopDetailDTO | None:
    rec = store.get_loop(loop_id)
    if rec is None:
        return None
    iterations = [_iteration_to_dto(it) for it in store.list_iterations(loop_id)]
    # Construct LoopDetailDTO directly from the source record (NOT by re-validating
    # a LoopDTO.model_dump()) so mypy-strict stays happy and we don't double-validate.
    base = _loop_to_dto(
        rec, now=now, last_iteration_at=_newest_iteration_at(iterations)
    )
    return LoopDetailDTO.model_validate({**base.model_dump(), "iterations": iterations})


#: Default/maximum iteration-strip page. A loop's strip reads the tail, never
#: the whole history: `list_iterations` is unbounded and a long-lived loop has
#: thousands of rows (#2304).
MAX_LOOP_ITERATIONS = 200


def list_iterations(
    store: _Store, loop_id: str, *, limit: int = MAX_LOOP_ITERATIONS
) -> list[IterationDTO]:
    """One bounded page of a loop's newest iterations, oldest-first.

    Bounded in SQL by the store's own tail read. A store double that has no
    tail read falls back to the full list and is trimmed here, so the bound
    holds on the wire either way.
    """
    bounded = max(1, min(limit, MAX_LOOP_ITERATIONS))
    tail = getattr(store, "list_iterations_tail", None)
    if tail is not None:
        return [_iteration_to_dto(rec) for rec in tail(loop_id, limit=bounded)]
    records = list(store.list_iterations(loop_id))[-bounded:]
    return [_iteration_to_dto(rec) for rec in records]


def list_loop_audit(
    store: _Store, loop_id: str, *, limit: int = MAX_LOOP_AUDIT
) -> list[LoopAuditDTO]:
    """One bounded page of a loop's audit events, newest first.

    A loop stamps its id inside ``payload`` (there is no top-level loop_id
    column on audit_events), and this read used to ask for ``limit=None`` and
    filter in Python — the whole ledger, on every open of a loop page
    (C3/#2296). The id is now a SQL predicate and the read is bounded.

    The Python check stays as the backstop it is: a store that ignores the
    ``loop_id`` keyword must not be able to widen the answer to another loop's
    events.
    """
    events = store.list_audit_events(limit=limit, loop_id=loop_id)
    return [rec for rec in map(_audit_to_dto, events) if rec.loop_id == loop_id]
