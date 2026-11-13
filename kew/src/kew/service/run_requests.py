"""Queue and read run requests for the console and the CLI (#2537).

Both surfaces queue through :func:`queue_request`, so a Run pressed in the
console and ``kew run-request queue`` typed in a terminal write the same row and
the same ``run_request_queued`` audit fact. The operator is the actor this
process resolves (``KEW_ACTOR``, else the OS user) — the same identity the CLI
would have written had the command been pasted.

A request is refused here, before anything is written, when no drive is running
to pick it up: a Run must say "no drive running" rather than queue into
nothing. Every runnable verb authorizes agent spend, so queueing one is gated
on ``[approval].approvers`` exactly as the console's other spend verbs are
(:func:`kew.service.verbs._approver_gate`), and the queued fact carries the
bound policy's hash. Everything else that refuses a request — a stopped
fleet, a held drive, a spent budget — is the drive's call when it reaches the
request, so the refusal the block shows is the one the executor actually made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kew import run_requests as rr
from kew.audit import resolve_actor
from kew.config import ConfigError, DispatchConfig, load_config
from kew.drive_heartbeat import DRIVE_LIVE, read_drive_heartbeat
from kew.service.verbs import _approver_gate

#: How many requests a list read returns at most, and how far back it looks.
LIST_LIMIT = 20
LIST_WINDOW = timedelta(days=7)


class NoDriveRunning(RuntimeError):
    """No live ``kew drive`` holds this ledger, so nothing would run a request."""


#: A submission key sent again with another command or model (#2975).
KeyReused = rr.KeyReused
#: A pair still queued or running, pressed again under another key (#2975).
PairBusy = rr.PairBusy


def drive_state(store: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Whether a drive runs to pick a request up — the reading the console's
    driver health shows (#2617): the lock holder's own heartbeat, inside its
    stall line. ``state`` is ``live``, ``gone`` (holds the lock, stopped
    beating) or ``none`` (no drive holds the lock)."""
    beat = read_drive_heartbeat(store)
    if beat is None:
        return {"state": "none", "running": False, "reason": "no drive holds the lock"}
    at = now or datetime.now(UTC)
    state = beat.status(at)
    reason = None
    if state != DRIVE_LIVE:
        reason = f"its heartbeat is {beat.age_s(at):.0f}s old"
    return {"state": state, "running": state == DRIVE_LIVE, "reason": reason}


def _no_drive_sentence(drive: dict[str, Any]) -> str:
    return (
        f"no drive running ({drive['reason']}) — start `kew drive`, or copy "
        "the command and run it"
    )


def queue_request(
    store: Any,
    *,
    command: str,
    item_id: str | None,
    source: str,
    config: DispatchConfig,
    model: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Queue *command* for the drive; the request and whether it is new.

    A submission *key* (#2975) makes the call safe to repeat: the console sends
    one per Run press with every attempt, and an attempt whose answer was lost
    is asked again. The request the key queued comes back (``created`` false)
    before any check, since a check that has changed since — the drive
    stopped — must not hide a request already written; a key that queued
    nothing decides afresh. See :func:`kew.run_requests.queue` for the
    refusals a key adds.
    """
    if key is not None:
        mine = rr.keyed_request(store, key, command, model)
        if mine is not None:
            drive = drive_state(store)
            return {"request": mine.to_json(), "created": False, "drive": drive}
    return _queue(store, command, item_id, source, config, model, key)


def _queue(
    store: Any,
    command: str,
    item_id: str | None,
    source: str,
    config: DispatchConfig,
    model: str | None,
    key: str | None,
) -> dict[str, Any]:
    """Queue *command* for the drive; the request and whether it is new.

    A *model* (#2741, the launchpad's per-dispatch choice) must be a catalog id
    on the runner the dispatch resolves under the effective config — the file,
    the DB overrides and the environment the drive's child loads too — and the
    drive checks it again when it takes the request.

    Raises :class:`kew.run_requests.NotRunnable` for a command a request may
    not carry (:class:`kew.run_requests.ModelRefused` for a model it may
    not), :class:`kew.service.verbs.VerbDenied` for an actor outside
    ``[approval].approvers`` and :class:`NoDriveRunning` when nothing would
    pick it up — all before any row is written.
    """
    argv = rr.parse_command(command)
    if model is not None:
        try:
            effective = load_config(store=store)
        except ConfigError as exc:
            raise rr.ModelRefused(
                f"the model cannot be checked, the config does not load: {exc}"
            ) from None
        rr.check_model(effective, argv, model)
    if rr.verb_of(argv) in rr.SPENDS:
        _approver_gate(store, config)
    drive = drive_state(store)
    if not drive["running"]:
        raise NoDriveRunning(_no_drive_sentence(drive))
    request, created = rr.queue(
        store,
        command,
        operator=resolve_actor().id,
        source=source,
        item_id=item_id,
        model=model,
        key=key,
    )
    return {"request": request.to_json(), "created": created, "drive": drive}


def get_request(store: Any, request_id: str) -> dict[str, Any] | None:
    request = rr.get_request(store, request_id)
    return None if request is None else request.to_json()


def list_requests(
    store: Any,
    *,
    command: str | None = None,
    item_id: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """The newest requests first, optionally only one command's or one item's.

    *command* is compared in its canonical spelling, so the block that printed
    it finds the request it issued however the text was spaced. *model* keeps
    only one model's requests, an empty one only those sent without a model:
    the drive holds one active request per (command, model), and the filter
    runs before the limit, so a model's queued request behind
    :data:`LIST_LIMIT` newer ones for other models is still found (#2878).
    Only the last :data:`LIST_WINDOW` is read; `kew run-request show` reads
    any one request — except a pair's own (*command* and *model* both given):
    the request still holding it comes first however old it is (#2975), since
    it is what blocks another Run of the pair, and the console gates Run and
    Copy on this read.
    """
    wanted = None
    if command is not None:
        wanted = rr.canonical(rr.parse_command(command))
    out = []
    active = _pair_active(store, wanted, item_id, model)
    if active is not None:
        out.append(active.to_json())
    since = datetime.now(UTC) - LIST_WINDOW
    for request in reversed(rr.list_requests(store, since=since)):
        if len(out) >= LIST_LIMIT:
            break
        if active is not None and request.id == active.id:
            continue
        if _matches(request, wanted, item_id, model):
            out.append(request.to_json())
    return out


def _pair_active(
    store: Any, command: str | None, item_id: str | None, model: str | None
) -> rr.RunRequest | None:
    """The request holding the pair a list read names, if it passes the read's
    filters; None for a read that names no pair."""
    if command is None or model is None:
        return None
    active = rr.active_request(store, command, model or None)
    if active is None or not _matches(active, command, item_id, model):
        return None
    return active


def _matches(
    request: rr.RunRequest,
    command: str | None,
    item_id: str | None,
    model: str | None,
) -> bool:
    """Whether *request* passes each filter given; None is no filter, and a
    *model* of ``""`` matches a request sent without one."""
    return (
        (command is None or request.command == command)
        and (item_id is None or request.item_id == item_id)
        and (model is None or (request.model or "") == model)
    )
