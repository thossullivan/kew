"""Run requests: a kew command queued in the ledger for the drive to run (#2537).

The console shows an operator the exact command that clears a row. A *run
request* lets it hand that command over instead of asking for a paste: the
console (or ``kew run-request queue``) writes the command, the item and the
operator to the ledger, and the supervised ``kew drive`` — which already runs
as the operator — picks it up and runs it as a fresh kew child with
``KEW_ACTOR`` set to that operator, so the child writes exactly the audit rows
the same command typed in a terminal would. The console process itself still
never launches an agent or holds a credential.

What may be queued is a closed set (:data:`RUNNABLE`): each entry is a kew verb
path and the number of positional arguments it takes, and every argument must
be one plain token (``waits._RUNNABLE_ARG``). No option, no shell, no second
command — a request is parsed into an argv here and nowhere else, and the drive
re-parses the stored text before it runs anything, so a row written around this
module runs nothing either.

Each request is one ``meta`` row, ``run_request:<id>``, whose JSON carries its
state. Every transition is a compare-and-set against the exact row last read:

* ``queued`` → ``running`` is the drive's claim, so one request runs once even
  if two drives raced for it;
* ``queued`` → ``refused`` is the drive declining it (fleet stopped, drive held,
  daily budget reached), with the reason the issuing block prints;
* ``queued`` → ``superseded`` is the drive's fence (#2975): a ``kew dispatch``
  whose item has a run in flight, or a dispatch run that started after the
  request was queued — the same command copied and run in a terminal — is not
  run again. The claim reads the runs and writes the row in one transaction,
  and the request names the run that superseded it;
* ``running`` → ``done`` / ``failed`` records the child's exit code and the
  tail of its output; ``running`` → ``unknown`` is a request whose drive exited
  before its child did, and whose child is gone — it is never re-run, because
  nobody can say whether it already took effect.

A ``kew dispatch`` request may also carry a model (#2741), chosen in the
console's launchpad. It is not an option in the command text — the grammar
above stays option-free — but a separate field, checked against the model
catalog and the runner the dispatch resolves (:func:`check_model`) when it is
queued and again when the drive takes it, and run as ``--model <id>``.

A second row, ``run_request_active:<digest>``, points at the one live request
for a command and model, so a double-click — or the console and a terminal
queueing the same command at once — gets the existing request back instead of a
second run.

A third, ``run_request_key:<digest>``, is the console's submission key (#2975):
one random key per Run press, sent with every attempt at its POST. The key, the
request row, the active pointer and the request's strict ``run_request_queued``
audit fact are written in one ``BEGIN IMMEDIATE`` transaction, so a key never
exists without the request it queued, and a request a key queued is never
without its key: an attempt whose answer was lost on the way back is asked
again with the same key and gets that request back — whatever it has since
become — never a second one. An attempt that wrote nothing left nothing under
its key, and asking again decides afresh. A pair whose request is still queued
or running refuses another key with :class:`PairBusy`: one press is one
request.

No request exists without its queued fact, nor a fact without its request: a
queuer killed before that transaction commits wrote nothing, and one killed
after it left a request the drive runs once and a retry of the same press gets
back. A fact that cannot be written rolls the whole transaction back, and the
queue is :class:`QueuedFactFailed` with nothing queued.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shlex
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from kew.waits import _RUNNABLE_ARG

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

REQUEST_PREFIX = "run_request:"
ACTIVE_PREFIX = "run_request_active:"
KEY_PREFIX = "run_request_key:"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REFUSED = "refused"
UNKNOWN = "unknown"
SUPERSEDED = "superseded"
TERMINAL = frozenset({DONE, FAILED, REFUSED, UNKNOWN, SUPERSEDED})
STATES = frozenset({QUEUED, RUNNING}) | TERMINAL

#: The verbs a request may carry: verb path → positional argument count. Each
#: is a bounded command the console prints in a Copy block; a long-lived loop
#: (``kew epic run``, which would fight the drive for the epic's lock) or
#: anything taking options stays a command to copy.
RUNNABLE: dict[tuple[str, ...], int] = {
    ("dispatch",): 1,
    ("epic", "continue"): 2,
    ("epic", "retry"): 2,
    ("epic", "unhold"): 2,
}

#: The verbs that authorize new agent spend, and so are refused while the fleet
#: is stopped, the drive is held, or today's budget is spent.
SPENDS: frozenset[tuple[str, ...]] = frozenset(RUNNABLE)

#: The verbs a request may carry a model for: the one that launches an agent.
MODEL_VERBS: frozenset[tuple[str, ...]] = frozenset({("dispatch",)})

#: What a launchpad model override is attested as, on the dispatch's own
#: ``routing_decision`` row. The drive hands it to the child it starts in
#: :data:`MODEL_SOURCE_ENV`.
LAUNCHPAD_MODEL_SOURCE = "console launchpad"
MODEL_SOURCE_ENV = "KEW_MODEL_OVERRIDE_SOURCE"

#: Every tier a dispatch can resolve to: ``None`` is an item with no tier label.
_TIERS: tuple[str | None, ...] = (None, "simple", "medium", "complex")

#: How long a request may wait in the queue. One the drive reaches later — it
#: was queued before the drive stopped, or sat behind a long run — is refused
#: rather than run on a click that old. Until it does, the stale request still
#: holds its (command, model) pair (#2975): queueing the command again returns
#: it, or with a submission key is refused, and is a new request once refused.
QUEUE_TTL = timedelta(hours=1)

#: How much of a child's output a finished request keeps for the block.
DETAIL_LIMIT = 600

# A compare-and-set that loses this many races in a row is reported.
_CAS_ATTEMPTS = 8


class RequestStore(Protocol):
    def get_meta(self, key: str) -> str | None: ...
    def compare_set_meta(self, key: str, expected: str | None, value: str) -> bool: ...
    def append_audit_event_with_metas(
        self,
        writes: Sequence[tuple[str, str | None, str]],
        *,
        timestamp: str,
        event: str,
        item_id: str | None = None,
        repo: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        policy_hash: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool: ...
    def list_meta(
        self, prefix: str, *, after: str = "", limit: int = 100
    ) -> list[tuple[str, str]]: ...
    def claim_meta_unless_item_ran(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        item_id: str,
        since: datetime,
        superseded: Callable[[str], str],
    ) -> tuple[bool, str | None]: ...


class NotRunnable(ValueError):
    """The command is not one a run request may carry."""


class ModelRefused(NotRunnable):
    """The model a request carries is not one this dispatch may run."""


class RequestStateError(RuntimeError):
    """A request row could not be read or written as asked."""


class KeyReused(ValueError):
    """A submission key sent again with another command or model."""


class PairBusy(ValueError):
    """The command and model already have a request queued or running, and
    this submission key did not queue it (#2975). *request* is that one."""

    def __init__(self, message: str, request: RunRequest) -> None:
        super().__init__(message)
        self.request = request


class QueuedFactFailed(RuntimeError):
    """A new request and its queued fact could not be written together, so
    neither was: this attempt queued nothing (#2975)."""


def _now() -> datetime:
    return datetime.now(UTC)


def parse_command(text: str) -> list[str]:
    """The argv (without ``kew``) a runnable *text* names, or :class:`NotRunnable`.

    The text must be exactly ``kew <verb path> <args>`` with the verb in
    :data:`RUNNABLE`, the argument count it takes, and every argument one plain
    token — so nothing a shell would treat specially, and no option.
    """
    try:
        words = shlex.split(text, posix=True)
    except ValueError as exc:
        raise NotRunnable(f"not one command: {exc}") from None
    if not words or words[0] != "kew":
        raise NotRunnable("a run request carries a kew command")
    rest = words[1:]
    for verb, arity in RUNNABLE.items():
        if tuple(rest[: len(verb)]) != verb:
            continue
        args = rest[len(verb) :]
        if len(args) != arity:
            raise NotRunnable(
                f"`kew {' '.join(verb)}` takes {arity} argument(s), got {len(args)}"
            )
        for arg in args:
            if not _RUNNABLE_ARG.fullmatch(arg):
                raise NotRunnable(f"{arg!r} is not a plain argument")
        return rest
    raise NotRunnable(
        "the drive runs only "
        + ", ".join(f"`kew {' '.join(v)}`" for v in RUNNABLE)
        + "; copy any other command and run it"
    )


def canonical(argv: list[str]) -> str:
    """The one spelling of a parsed request, as the console prints it."""
    return " ".join(["kew", *argv])


def verb_of(argv: list[str]) -> tuple[str, ...]:
    for verb in RUNNABLE:
        if tuple(argv[: len(verb)]) == verb:
            return verb
    raise NotRunnable(f"no runnable verb in {argv!r}")


def _dispatch_runners(config: DispatchConfig) -> set[str]:
    """The runner each tier resolves to. The tier is read off the item's labels
    by the dispatch itself, so a model must fit every tier it could land on."""
    from kew.runners.registry import resolve_runner

    try:
        return {resolve_runner(config, tier).runner.name for tier in _TIERS}
    except ValueError as exc:
        raise ModelRefused(f"the dispatch route does not resolve: {exc}") from None


def check_model(config: DispatchConfig, argv: list[str], model: str) -> None:
    """Raise :class:`ModelRefused` unless *model* may ride the dispatch *argv*.

    The model must be a catalog id (not an alias), neither retired nor
    unroutable, on the runner the dispatch resolves. A model only replaces the
    route's model — the runner still comes from the config — so a model of
    another runner would be handed to a CLI that cannot run it.
    """
    from kew.model_catalog import load_catalog

    if verb_of(argv) not in MODEL_VERBS:
        raise ModelRefused(f"`{canonical(argv)}` takes no model")
    if not _RUNNABLE_ARG.fullmatch(model):
        raise ModelRefused(f"{model!r} is not a plain model id")
    row = load_catalog().get(model)
    if row is None:
        raise ModelRefused(f"{model!r} is not a model id in the catalog")
    if row.lifecycle.status == "retired":
        raise ModelRefused(f"{model!r} is retired")
    if not row.routable:
        raise ModelRefused(f"{model!r} is not routable")
    runners = _dispatch_runners(config)
    if runners != {row.runner}:
        raise ModelRefused(
            f"{model!r} runs on {row.runner}, but this dispatch runs on "
            + " or ".join(sorted(runners))
        )


def request_argv(request: RunRequest) -> list[str]:
    """The argv (without ``kew``) the drive runs for *request*.

    Re-parses the stored text, so a row written around :func:`queue` runs
    nothing either; a model rides as ``--model`` on a verb that takes one.
    """
    argv = parse_command(request.command)
    if request.model is None:
        return argv
    if verb_of(argv) not in MODEL_VERBS or not _RUNNABLE_ARG.fullmatch(request.model):
        raise ModelRefused(f"`{request.command}` cannot carry {request.model!r}")
    return [*argv, "--model", request.model]


@dataclass(frozen=True)
class RunRequest:
    """One queued command and everything that has happened to it."""

    id: str
    command: str
    operator: str
    requested_at: str
    source: str
    state: str = QUEUED
    item_id: str | None = None
    drive: str | None = None
    host: str | None = None
    pid: int | None = None
    log: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    detail: str | None = None
    model: str | None = None
    #: The run a ``superseded`` request was fenced by (#2975).
    superseded_by: str | None = None
    #: Seconds the request has waited on a running local verify (#3190), which
    #: its :data:`QUEUE_TTL` does not count, and the last time a drive pass saw
    #: it waiting on one. Host-load waits are not credited (#2864).
    waited_seconds: float = 0.0
    waited_at: str | None = None

    @property
    def key(self) -> str:
        return REQUEST_PREFIX + self.id

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def stale(self, now: datetime | None = None) -> bool:
        """Whether this request has been queued longer than :data:`QUEUE_TTL`,
        not counting the time it waited on a running local verify."""
        if self.state != QUEUED:
            return False
        try:
            at = datetime.fromisoformat(self.requested_at)
        except ValueError:
            return True
        waited = timedelta(seconds=self.waited_seconds)
        return (now or _now()) - at - waited > QUEUE_TTL

    def encode(self) -> str:
        # Every transition is a compare-and-set on this text, so a row written
        # before a field existed must still encode as stored: ``superseded_by``
        # is left out until it names a run (#2975), the wait bookkeeping until
        # the request has waited (#3190).
        data = asdict(self)
        for name, default in _UNTIL_SET.items():
            if data[name] == default:
                del data[name]
        return json.dumps(data, sort_keys=True)

    @classmethod
    def parse(cls, raw: str) -> RunRequest:
        try:
            data = json.loads(raw)
            request = cls(**data)
        except (ValueError, TypeError) as exc:
            raise RequestStateError(f"unreadable run request: {exc}") from None
        if request.state not in STATES:
            raise RequestStateError(f"run request in unknown state {request.state!r}")
        return request

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for name in _BOOKKEEPING:  # the drive's own clock, not the console's
            del data[name]
        return data


#: Fields a row carries only once they leave their default, with that default.
_UNTIL_SET: dict[str, object] = {
    "superseded_by": None,
    "waited_seconds": 0.0,
    "waited_at": None,
}

#: The wait bookkeeping :meth:`RunRequest.to_json` leaves out.
_BOOKKEEPING = ("waited_seconds", "waited_at")

#: How often a waiting request's credit is written: a pass runs every sleep
#: step, and a write per step would churn the row for nothing.
WAIT_CREDIT_STEP = timedelta(minutes=1)

#: The longest gap between two passes that saw a request waiting that still
#: counts as one wait: a drive that was down, or a pass that never reached the
#: request, earns no credit for the time between.
WAIT_CREDIT_GAP = timedelta(minutes=5)


_ID_STAMP = "%Y%m%dT%H%M%S%f"


def _new_id(at: datetime) -> str:
    # Sortable by time first, so the ledger's key order is queue order.
    return f"{at.astimezone(UTC).strftime(_ID_STAMP)}-{secrets.token_hex(4)}"


def _active_key(command: str, model: str | None = None) -> str:
    # A model is part of what runs, so the same command with another model
    # (or none) is another request, never the live one handed back.
    text = command if model is None else f"{command}\0{model}"
    return ACTIVE_PREFIX + hashlib.sha256(text.encode()).hexdigest()[:32]


def get_request(store: RequestStore, request_id: str) -> RunRequest | None:
    raw = store.get_meta(REQUEST_PREFIX + request_id)
    return None if not raw else RunRequest.parse(raw)


def list_requests(
    store: RequestStore, *, since: datetime | None = None
) -> list[RunRequest]:
    """Every request row queued at or after *since* (all of them when None),
    oldest first; an unreadable one is an error.

    Rows are never deleted, so a reader that runs often bounds its read with
    *since*: ids begin with the queue time, so the window is a key range.
    """
    out: list[RunRequest] = []
    after = (
        ""
        if since is None
        else REQUEST_PREFIX + since.astimezone(UTC).strftime(_ID_STAMP)
    )
    while True:
        page = store.list_meta(REQUEST_PREFIX, after=after, limit=100)
        if not page:
            return out
        out.extend(RunRequest.parse(raw) for _, raw in page)
        after = page[-1][0]


def _transition(
    store: RequestStore, request: RunRequest, expected_state: str, new: RunRequest
) -> RunRequest | None:
    """Write *new* over *request* if the row still reads as it did."""
    if request.state != expected_state:
        return None
    if not store.compare_set_meta(request.key, request.encode(), new.encode()):
        return None
    if new.terminal:
        # Free the command for a new request; a pointer left behind by a crash
        # here is replaced by the next queue, which sees it points at a
        # terminal request.
        active = _active_key(new.command, new.model)
        store.compare_set_meta(active, request.id, "")
    return new


def queue(
    store: RequestStore,
    command: str,
    *,
    operator: str,
    source: str,
    item_id: str | None = None,
    model: str | None = None,
    key: str | None = None,
    now: datetime | None = None,
) -> tuple[RunRequest, bool]:
    """Queue *command*; returns the request and whether it is a new one.

    When the same command is already queued or running, that request comes
    back (``False``) instead of a second one being written — even one that has
    sat queued past :data:`QUEUE_TTL` (#2975): the drive refuses that one when
    it reaches it, and only then is the pair free. A new request is written with its
    strict ``run_request_queued`` fact in one transaction, or not at all
    (:class:`QueuedFactFailed`).

    With a submission *key* (#2975), the request that key queued comes back
    (:func:`keyed_request`), and a pair still queued or running under another
    key is :class:`PairBusy` rather than joined. The key, the row, the active
    pointer and the fact are one transaction.
    """
    text = canonical(parse_command(command))
    at = now or _now()
    active = _active_key(text, model)
    for _ in range(_CAS_ATTEMPTS):
        if key is not None and (mine := keyed_request(store, key, text, model)):
            return mine, False
        pointer = store.get_meta(active)
        existing = _live(store, pointer)
        if existing is not None:
            # Read after the pointer: a same-key attempt that wrote both since
            # this one's first look is seen here, and is this press's request.
            if key is not None and (mine := keyed_request(store, key, text, model)):
                return mine, False
            return _join(existing, key), False
        request = RunRequest(
            id=_new_id(at),
            command=text,
            operator=operator,
            requested_at=at.isoformat(),
            source=source,
            item_id=item_id,
            model=model,
        )
        # The pointer, the row, the key and the fact land in one transaction:
        # a pointer naming a row not written yet would read as free to a racing
        # queue, a request whose key is not written yet could be queued again
        # by a retry of the same press, and one without its fact must not run.
        writes: list[tuple[str, str | None, str]] = [
            (active, pointer, request.id),
            (request.key, None, request.encode()),
        ]
        if key is not None:
            writes.append((_key_row(key), None, request.id))
        if _write_queued(store, writes, request):
            return request, True
    raise RequestStateError(f"the active row for {text!r} kept changing")


def _live(store: RequestStore, pointer: str | None) -> RunRequest | None:
    """The request *pointer* names, if it still holds its pair: queued or
    running, whatever its age (#2975). One left queued past :data:`QUEUE_TTL`
    holds it too, until the drive refuses it: a pair has one nonterminal
    request at a time, and the drive never sees a second beside it."""
    existing = get_request(store, pointer) if pointer else None
    if existing is None or existing.terminal:
        return None
    return existing


def active_request(
    store: RequestStore, command: str, model: str | None = None
) -> RunRequest | None:
    """The request holding (*command*, *model*): queued or running, however
    long ago it was queued (#2975). None when the pair is free."""
    text = canonical(parse_command(command))
    return _live(store, store.get_meta(_active_key(text, model or None)))


def _join(existing: RunRequest, key: str | None) -> RunRequest:
    """The live request a queue without a key joins; a keyed one is refused."""
    if key is not None:
        raise PairBusy(
            f"{existing.command} is already {existing.state} as request "
            f"{existing.id}; it runs once",
            existing,
        )
    return existing


def _key_row(key: str) -> str:
    return KEY_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:32]


def keyed_request(
    store: RequestStore, key: str, command: str, model: str | None
) -> RunRequest | None:
    """The request submission *key* queued, whatever it has since become;
    None when the key queued nothing.

    Raises :class:`KeyReused` when *key* queued another command or model.
    """
    request_id = store.get_meta(_key_row(key))
    if not request_id:
        return None
    request = get_request(store, request_id)
    if request is None:
        raise RequestStateError(
            f"a submission key names run request {request_id}, not found"
        )
    text = canonical(parse_command(command))
    if (request.command, request.model or None) != (text, model or None):
        raise KeyReused("this submission key was sent with another command or model")
    return request


#: Why a queue whose fact could not be written queued nothing.
UNAUDITED_DETAIL = (
    "its queued fact could not be written, so nothing was queued; press Run again"
)


def _write_queued(
    store: RequestStore,
    writes: list[tuple[str, str | None, str]],
    request: RunRequest,
) -> bool:
    """Commit *writes* with new *request*'s strict queued fact (#2975); False
    when a key moved first. A failure of any of it writes none of it, and is
    :class:`QueuedFactFailed`."""
    from kew.audit import Actor, event_fields

    fields = event_fields(
        actor=Actor("human", request.operator),
        item_id=request.item_id,
        payload=_payload(request),
    )
    try:
        return store.append_audit_event_with_metas(
            writes, event="run_request_queued", **fields
        )
    except Exception as exc:
        raise QueuedFactFailed(UNAUDITED_DETAIL) from exc


def claim(
    store: RequestStore,
    request: RunRequest,
    *,
    drive: str,
    host: str,
    log: str,
    now: datetime | None = None,
) -> RunRequest | None:
    """Move a queued request to running for *drive*; None when another actor
    claimed or changed it first. The claim lands BEFORE the child starts, so a
    request is never run by two drives; :func:`started` adds the child's pid.

    A ``kew dispatch`` is fenced (#2975): when its item has a run in flight, or
    a dispatch run that started after the request was queued, the request is
    ``superseded`` instead, naming that run, in the same transaction — the
    work it asked for is already under way or done, so it never runs twice.
    """
    at = now or _now()
    claimed = replace(
        request,
        state=RUNNING,
        detail=None,
        drive=drive,
        host=host,
        log=log,
        started_at=at.isoformat(),
    )
    item = fenced_item(request)
    if item is None:
        return _transition(store, request, QUEUED, claimed)
    if request.state != QUEUED:
        return None

    def superseded(run_id: str) -> str:
        return _superseded(request, run_id, drive=drive, at=at).encode()

    wrote, run_id = store.claim_meta_unless_item_ran(
        request.key,
        request.encode(),
        claimed.encode(),
        item_id=item,
        since=_queued_at(request),
        superseded=superseded,
    )
    if not wrote:
        return None
    if run_id is None:
        return claimed
    # Free the pair, as any settled request does (:func:`_transition`).
    store.compare_set_meta(_active_key(request.command, request.model), request.id, "")
    return _superseded(request, run_id, drive=drive, at=at)


def fenced_item(request: RunRequest) -> str | None:
    """The item whose runs fence *request*'s claim: a dispatch's own; None for
    a verb the drive fences on nothing. A command that no longer parses is
    :class:`NotRunnable`, which the drive refuses before it claims."""
    argv = parse_command(request.command)
    return argv[1] if verb_of(argv) == ("dispatch",) else None


def _queued_at(request: RunRequest) -> datetime:
    """When *request* was queued; an unreadable time is the earliest one, so
    every dispatch run of its item fences it."""
    try:
        at = datetime.fromisoformat(request.requested_at)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _superseded(
    request: RunRequest, run_id: str, *, drive: str, at: datetime
) -> RunRequest:
    return replace(
        request,
        state=SUPERSEDED,
        drive=drive,
        finished_at=at.isoformat(),
        superseded_by=run_id,
        detail=(
            f"superseded by run {run_id}: the item already had that run in "
            "flight, or it started after this request was queued; not run again"
        )[:DETAIL_LIMIT],
    )


def waited(request: RunRequest, now: datetime) -> RunRequest:
    """*request* with the time since a pass last saw it waiting credited, so
    :meth:`RunRequest.stale` does not count it; a gap longer than
    :data:`WAIT_CREDIT_GAP` earns nothing and starts the wait afresh."""
    try:
        seen = (
            None
            if request.waited_at is None
            else datetime.fromisoformat(request.waited_at)
        )
    except ValueError:
        _logger.warning(
            "run request %s: unreadable wait time %r; its wait starts afresh",
            request.id,
            request.waited_at,
        )
        seen = None
    gap = None if seen is None else now - seen
    credit = gap if gap is not None and timedelta(0) <= gap <= WAIT_CREDIT_GAP else None
    return replace(
        request,
        waited_seconds=request.waited_seconds
        + (0.0 if credit is None else credit.total_seconds()),
        waited_at=now.isoformat(),
    )


def wait_due(
    request: RunRequest, reason: str, now: datetime, *, credit: bool = False
) -> bool:
    """Whether a pass that finds *request* waiting for *reason* writes the row:
    the reason changed, or — for a credited wait — its credit is a step
    behind; an uncredited wait writes once more to end a credited one."""
    if request.detail != reason[:DETAIL_LIMIT]:
        return True
    if not credit:
        return request.waited_at is not None
    if request.waited_at is None:
        return True
    try:
        return now - datetime.fromisoformat(request.waited_at) >= WAIT_CREDIT_STEP
    except ValueError:
        return True


def wait(
    store: RequestStore,
    request: RunRequest,
    reason: str,
    *,
    now: datetime | None = None,
    credit: bool = False,
) -> RunRequest | None:
    """Say why a queued request is waiting, leaving it queued: a hold that
    lifts by itself (host load #2813, a running local verify #3190) delays a
    click, never refuses it. Only a *credit* wait — a running local verify —
    is credited against the request's :data:`QUEUE_TTL` (:func:`waited`); a
    host-load wait still counts toward it, and ends any credited wait, so the
    time it spans is never credited later. None when another actor changed
    the row first."""
    at = now or _now()
    base = waited(request, at) if credit else replace(request, waited_at=None)
    waiting = replace(base, detail=reason[:DETAIL_LIMIT])
    return _transition(store, request, QUEUED, waiting)


def started(store: RequestStore, request: RunRequest, pid: int) -> RunRequest:
    """Record the running child's pid on a request this drive claimed."""
    new = replace(request, pid=pid)
    if _transition(store, request, RUNNING, new) is None:
        raise RequestStateError(f"run request {request.id} changed while it started")
    return new


def refuse(
    store: RequestStore,
    request: RunRequest,
    reason: str,
    *,
    drive: str,
    now: datetime | None = None,
) -> RunRequest | None:
    refused = replace(
        request,
        state=REFUSED,
        drive=drive,
        finished_at=(now or _now()).isoformat(),
        detail=reason[:DETAIL_LIMIT],
    )
    return _transition(store, request, QUEUED, refused)


def finish(
    store: RequestStore,
    request: RunRequest,
    *,
    exit_code: int | None,
    detail: str,
    state: str | None = None,
    now: datetime | None = None,
) -> RunRequest | None:
    """Record a running request's end: ``done`` on exit 0, ``failed`` on any
    other exit, ``unknown`` when no exit code could be observed — unless the
    caller names the *state* (a child that never started is ``failed``)."""
    if state is None and exit_code is None:
        state = UNKNOWN
    elif state is None:
        state = DONE if exit_code == 0 else FAILED
    ended = replace(
        request,
        state=state,
        exit_code=exit_code,
        finished_at=(now or _now()).isoformat(),
        detail=detail[-DETAIL_LIMIT:] if detail else None,
    )
    return _transition(store, request, RUNNING, ended)


def record(store: Any, event: str, request: RunRequest, **extra: Any) -> None:
    """One audit row for a request's step, naming the operator who asked.

    The event is the request's own fact (queued, refused, started, finished);
    the command the drive runs writes its own rows besides, as that operator.
    The write is strict: a request whose step could not be audited must not
    proceed as if it had been.
    """
    from kew.audit import Actor, emit

    emit(
        store,
        event,
        actor=Actor("human", request.operator),
        item_id=request.item_id,
        payload=_payload(request, **extra),
        strict=True,
    )


def _payload(request: RunRequest, **extra: Any) -> dict[str, Any]:
    """What *request*'s audit fact says of it, besides who asked."""
    payload: dict[str, Any] = {
        "request_id": request.id,
        "command": request.command,
        "source": request.source,
        "state": request.state,
    }
    if request.model is not None:
        payload["model"] = request.model
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload
