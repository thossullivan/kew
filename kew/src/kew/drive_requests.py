"""The drive's run-request executor (#2537): run what the console queued.

One pass, called by ``kew drive`` before every round, after every epic tick and
on every step of its sleep, so a Run pressed while the drive sleeps starts
within one sleep step:

1. reap this drive's own child when it has exited, recording its exit code and
   the tail of its output on the request;
2. settle any request a *previous* drive left ``running``: while its child is
   still alive on this host it is left alone (and nothing new starts), once it
   is gone the request is ``unknown`` — never re-run, since nobody can say
   whether it took effect. A request this drive's own token left running with
   no child in hand is one its self before a re-exec started (#3197): the
   exec kept that child, so the pass waits on it the same way and collects
   it, recording its exit code, once it ends;
3. take the oldest queued request: refuse it (fleet stopped, drive held by
   the operator, daily budget reached), leave it queued with the reason while
   a host-load episode holds admission (it starts once the episode ends) or,
   for a dispatch or a continuation, while a local merge verify runs (it
   starts once the verify ends, #3190), or
   claim it and start it as a fresh kew child. A spend-bearing request reads
   the host first (:attr:`RequestRunner.gate`), so the host-load check sees
   the host as it is now, not as the last epic tick saw it. A ``kew dispatch``
   whose item has a run in flight, or a dispatch run newer than the request,
   is ``superseded`` by the claim instead (#2975): a copied command already
   ran it.

Requests run one at a time, in queue order. Request rows are never deleted, so
after one full read at start a pass reads only the last day's rows plus the
requests it already knows are unsettled (:attr:`RequestRunner.unsettled`).
Each step is a compare-and-set on the request row (:mod:`kew.run_requests`), so
a request is claimed once and a second drive racing the first cannot run it
again.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew import run_requests as rr
from kew.drive import DriveLease, _pid_alive, drive_hold, host_load_episode
from kew.verify_lease import verify_deferral

REQUEST_LOG_DIR = "run-requests"

#: How far back a pass re-reads after the first full read: a request queued
#: since then lands inside it whatever clock skew separates two writers.
RESCAN_WINDOW = timedelta(days=1)

#: How long a request a previous drive claimed on this host, but never recorded
#: a pid for, holds the queue: its child may still be running, and nothing
#: says when it ends.
UNSEEN_CHILD_GRACE = timedelta(hours=1)

#: Starts one request's child: (argv without ``kew``, log path, operator, and
#: the variables the child's environment gains beyond ``KEW_ACTOR``).
Spawn = Callable[[list[str], Path, str, dict[str, str]], "subprocess.Popen[bytes]"]


def _tail(log: str | None, base: Path, limit: int = rr.DETAIL_LIMIT) -> str:
    """The last lines of a request's output, for the block that issued it.

    Only a log inside the drive's own ``run-requests`` directory is read: the
    path comes from the request row, and the tail is shown in the console.
    """
    if not log:
        return ""
    if Path(log).resolve().parent != (base / REQUEST_LOG_DIR).resolve():
        return f"(its output is not in {base / REQUEST_LOG_DIR}; not read)"
    try:
        with open(log, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit * 2))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"(its output could not be read: {exc})"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-6:])[-limit:]


def refusal(
    store: Any,
    config: Any,
    argv: list[str],
    request: rr.RunRequest | None = None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Why the drive declines to run *argv* now, or None.

    A *request* queued longer than :data:`kew.run_requests.QUEUE_TTL` ago is
    refused whatever it carries; time it spent waiting on a running local
    verify does not count (:func:`kew.run_requests.waited`), time it waited on
    host load does.
    Every other reason is a state that already
    refuses the same work elsewhere, said before the child would only discover
    it: an operator no longer in ``[approval].approvers``, a stopped fleet, an
    operator hold on the drive's admission, a spent daily budget. A host-load
    episode refuses nothing: it lifts by itself (:func:`host_wait`).
    """
    if request is not None and request.stale(now):
        return (
            f"it was queued at {request.requested_at}, more than "
            f"{rr.QUEUE_TTL} ago (not counting time it waited on a running "
            "verify), and never started; queue it again to run it"
        )
    if request is not None:
        why = _request_refusal(store, config, argv, request)
        if why is not None:
            return why
    if rr.verb_of(argv) not in rr.SPENDS:
        return None
    return _spend_refusal(store, config)


def _request_refusal(
    store: Any, config: Any, argv: list[str], request: rr.RunRequest
) -> str | None:
    """What refuses this one request: its model, or its operator's authority."""
    if request.model is not None:
        why = _model_refusal(store, argv, request.model)
        if why is not None:
            return why
    if rr.verb_of(argv) not in rr.SPENDS:
        return None
    from kew.approval import _authorized

    if not _authorized(config, request.operator):
        return f"{request.operator!r} is no longer in [approval].approvers"
    return None


def _spend_refusal(store: Any, config: Any) -> str | None:
    """What refuses any new agent spend now: a stopped fleet, a held drive, a
    spent daily budget."""
    stopped = store.get_fleet_stopped()
    if stopped is not None:
        why = stopped.get("reason") or "no reason recorded"
        return f"the fleet is stopped ({why}); `kew resume` lifts it"
    held = drive_hold(store)
    if held is not None:
        return (
            "the drive is held — admitting nothing new since "
            f"{held.since.isoformat(timespec='seconds')}; `kew drive release` lifts it"
        )
    from kew.service.palette_ask import daily_budget_reached

    reached = daily_budget_reached(store, config)
    if reached is not None:
        spent, cap = reached
        return (
            f"today's spend (${spent:.2f}) has reached the daily budget of "
            f"${cap:.2f} ([budget] daily_usd)"
        )
    return None


def host_wait(store: Any, config: Any, argv: list[str]) -> str | None:
    """Why a spend-bearing *argv* waits in the queue now: a standing host-load
    episode (#2813), or None. The request stays queued and starts once the
    drive's ticks end the episode."""
    if rr.verb_of(argv) not in rr.SPENDS or not config.drive.host_gated:
        return None
    host = host_load_episode(store)
    if host is None:
        return None
    return (
        f"waiting: the drive is holding admission for host load — "
        f"{host.describe()}; it starts once admission resumes"
    )


#: The request verbs that launch an agent on this machine: a dispatch, and the
#: continuation of preserved work. A retry or an unhold only edits the ledger,
#: and the tick that then launches yields to the verify itself.
LAUNCHES: frozenset[tuple[str, ...]] = frozenset({("dispatch",), ("epic", "continue")})


def verify_wait(
    store: Any, config: Any, argv: list[str], request: rr.RunRequest
) -> str | None:
    """Why a launching *argv* waits in the queue now: a running local merge
    verify (#3190), or None. The request stays queued, with one
    ``launch_deferred`` event per verify, and starts once the verify ends —
    as the drive's own launches do. A hand-typed command only warns."""
    if rr.verb_of(argv) not in LAUNCHES:
        return None
    deferral = verify_deferral(
        store, config, item_id=request.item_id or request.id, kind="run-request"
    )
    if deferral is None:
        return None
    return f"waiting: {deferral}; it starts once that verify ends"


def _effective_config(store: Any) -> Any:
    """The config the dispatch child will load: file, DB overrides, env."""
    from kew.config import load_config

    return load_config(store=store)


def _model_refusal(store: Any, argv: list[str], model: str) -> str | None:
    """Why *model* may no longer ride *argv*, checked again as the drive takes
    it: the route it was queued against may have changed while it waited."""
    from kew.config import ConfigError

    try:
        rr.check_model(_effective_config(store), argv, model)
    except rr.ModelRefused as exc:
        return f"model override refused: {exc}"
    except ConfigError as exc:
        return f"model override not checked, the config does not load: {exc}"
    return None


def child_env(request: rr.RunRequest) -> dict[str, str]:
    """What the child's environment gains for *request*: the attested source of
    a model the console's launchpad chose, so the dispatch's own
    ``routing_decision`` row names where its model came from."""
    if request.model is None or request.source != "web":
        return {}
    return {rr.MODEL_SOURCE_ENV: rr.LAUNCHPAD_MODEL_SOURCE}


def collect(pid: int) -> int | None:
    """The exit code of this process's child *pid* once it has exited —
    collecting it, so it never lingers as a zombie — or None while it runs.
    Raises :class:`ChildProcessError` when *pid* is not this process's child.
    A child a drive started before it re-exec'd itself is still its child
    after the exec, with no ``Popen`` left to reap it (#3197)."""
    done, status = os.waitpid(pid, os.WNOHANG)
    if done == 0:
        return None
    return os.waitstatus_to_exitcode(status)


@dataclass
class RequestRunner:
    """The run requests one drive executes, and the child it has in flight."""

    store: Any
    lease: DriveLease
    config: Any
    log_dir: Path
    spawn: Spawn
    say: Callable[[str], None]
    alive: Callable[[int], bool] = _pid_alive
    child: subprocess.Popen[bytes] | None = None
    running: rr.RunRequest | None = None
    _held_for: set[str] = field(default_factory=set)
    #: Ids of requests last read queued or running; None before the first
    #: (full) read.
    unsettled: set[str] | None = None
    #: Reads the host just before a spend-bearing request is admitted (#2813),
    #: so a request never starts on an overload no tick has sampled yet.
    gate: Callable[[], object] | None = None
    #: The drive's clock: the queue TTL and the wait credit read it.
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def token(self) -> str:
        return self.lease.holder.token

    def __call__(self) -> None:
        self._reap()
        requests = self._read()
        if self.child is not None or self._orphan_alive(requests):
            return
        for request in requests:
            if request.state == rr.QUEUED and self._take(request):
                return

    def _read(self) -> list[rr.RunRequest]:
        """The requests that may still need this drive, oldest first."""
        if self.unsettled is None:
            found = rr.list_requests(self.store)
        else:
            since = datetime.now(UTC) - RESCAN_WINDOW
            recent = {r.id: r for r in rr.list_requests(self.store, since=since)}
            for request_id in self.unsettled - set(recent):
                known = rr.get_request(self.store, request_id)
                if known is not None:
                    recent[known.id] = known
            found = sorted(recent.values(), key=lambda r: r.id)
        live = [r for r in found if not r.terminal]
        self.unsettled = {r.id for r in live}
        return live

    def _reap(self) -> None:
        if self.child is None or self.running is None:
            return
        code = self.child.poll()
        if code is None:
            return
        request, self.child, self.running = self.running, None, None
        self._finish(request, code)

    def _finish(self, request: rr.RunRequest, code: int) -> None:
        """Record the exit of *request*'s child, with its output's tail."""
        ended = rr.finish(
            self.store, request, exit_code=code, detail=_tail(request.log, self.log_dir)
        )
        if ended is None:
            self.say(f"run request {request.id}: its row changed under the drive.")
            return
        rr.record(self.store, "run_request_finished", ended, exit_code=code)
        self.say(f"run request {ended.id}: `{ended.command}` {ended.state} ({code}).")

    def busy(self) -> bool:
        """Collect this drive's own child once it has exited; True while it
        still runs. A drive about to re-exec waits on this (#3197): the exec
        would leave the child running with nothing holding it."""
        self._reap()
        return self.child is not None

    def _orphan_alive(self, requests: list[rr.RunRequest]) -> bool:
        """Settle requests a previous drive — or this drive before it
        re-exec'd — left running; True while one of their children still
        runs here, which keeps the queue waiting."""
        busy = False
        for request in requests:
            if request.state != rr.RUNNING:
                continue
            if request.drive == self.token:
                # Never this pass's own child: a pass with one in hand
                # returns before it settles orphans.
                busy = self._own_left_running(request) or busy
                continue
            if self._may_still_run(request):
                self._say_waiting(request, "from an earlier drive")
                busy = True
                continue
            self._settle_unknown(
                request,
                "the drive that ran it exited before it finished, so its "
                "outcome is unknown — check the item before running it again",
                "drive restarted",
            )
        return busy

    def _own_left_running(self, request: rr.RunRequest) -> bool:
        """Settle a request this drive's own token left running with no child
        in hand: its self before a re-exec started it (#3197). The exec kept
        that child, so it is collected here with its exit code once it ends;
        True while it still runs. A pid that is not this process's child is
        judged by whether it lives, as an earlier drive's would be."""
        if request.pid is not None and request.host == socket.gethostname():
            try:
                code = collect(request.pid)
            except ChildProcessError:
                pass  # not this process's child: its liveness is all there is
            else:
                if code is None:
                    self._say_waiting(request, "from before this drive re-exec'd")
                    return True
                self._finish(request, code)
                return False
        if self._may_still_run(request):
            self._say_waiting(request, "from before this drive re-exec'd")
            return True
        self._settle_unknown(
            request,
            "the drive re-exec'd itself before it finished and its child is "
            "gone, so its outcome is unknown — check the item before running "
            "it again",
            "drive re-exec'd",
        )
        return False

    def _say_waiting(self, request: rr.RunRequest, since: str) -> None:
        if request.id in self._held_for:
            return
        self._held_for.add(request.id)
        as_pid = "" if request.pid is None else f" as pid {request.pid}"
        self.say(
            f"run request {request.id}: may still be running{as_pid} {since}; "
            "waiting for it."
        )

    def _settle_unknown(self, request: rr.RunRequest, detail: str, why: str) -> None:
        tail = _tail(request.log, self.log_dir)
        ended = rr.finish(
            self.store,
            request,
            exit_code=None,
            detail=f"{detail}\n{tail}" if tail else detail,
        )
        if ended is not None:
            rr.record(self.store, "run_request_finished", ended, drive=self.token)
            self.say(f"run request {ended.id}: outcome unknown ({why}).")

    def _may_still_run(self, request: rr.RunRequest) -> bool:
        """Whether an earlier drive's child for *request* may still run here."""
        if request.host != socket.gethostname():
            return False
        if request.pid is not None:
            return self.alive(request.pid)
        try:
            claimed_at = datetime.fromisoformat(request.started_at or "")
        except ValueError:
            self.say(
                f"run request {request.id}: no pid and no readable claim time "
                f"({request.started_at!r}); treating its child as gone."
            )
            claimed_at = datetime.min.replace(tzinfo=UTC)
        return datetime.now(UTC) - claimed_at < UNSEEN_CHILD_GRACE

    def _take(self, request: rr.RunRequest) -> bool:
        """Refuse, start or hold *request*; False only when it waits in the
        queue for the host (the pass may take the next one)."""
        try:
            argv = rr.request_argv(request)
            if self.gate is not None and rr.verb_of(argv) in rr.SPENDS:
                self.gate()
            reason = refusal(self.store, self.config, argv, request, now=self.clock())
        except rr.NotRunnable as exc:
            argv, reason = [], str(exc)
        if reason is not None:
            refused = rr.refuse(self.store, request, reason, drive=self.token)
            if refused is not None:
                rr.record(self.store, "run_request_refused", refused, reason=reason)
                self.say(f"run request {request.id}: refused — {reason}.")
            return True
        host = host_wait(self.store, self.config, argv)
        if host is not None:  # still ages toward the TTL (#2864 decides that)
            self._wait(request, host, credit=False)
            return False
        verify = verify_wait(self.store, self.config, argv, request)
        if verify is not None:
            self._wait(request, verify, credit=True)
            return False
        log = self.log_dir / REQUEST_LOG_DIR / f"{request.id}.log"
        claimed = rr.claim(
            self.store,
            request,
            drive=self.token,
            host=socket.gethostname(),
            log=str(log),
        )
        if claimed is None:
            return True  # another actor moved it first; the next pass reads again
        if claimed.state == rr.SUPERSEDED:
            self._superseded(claimed)
            return True
        self._start(claimed, argv, log)
        return True

    def _superseded(self, request: rr.RunRequest) -> None:
        """Audit the fence's refusal (#2975): the item's run already did it."""
        rr.record(
            self.store,
            "run_request_superseded",
            request,
            superseded_by=request.superseded_by,
            drive=self.token,
        )
        self.say(f"run request {request.id}: {request.detail}.")

    def _wait(self, request: rr.RunRequest, reason: str, *, credit: bool) -> None:
        """Leave *request* queued, saying why, for status and the console; a
        *credit* wait (a running local verify) is credited against its queue
        TTL (:func:`kew.run_requests.waited`), a host-load wait is not."""
        now = self.clock()
        if not rr.wait_due(request, reason, now, credit=credit):
            return
        written = rr.wait(self.store, request, reason, now=now, credit=credit)
        if written is not None and not request.detail:
            self.say(f"run request {request.id}: {reason}.")

    def _start(self, claimed: rr.RunRequest, argv: list[str], log: Path) -> None:
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            child = self.spawn(argv, log, claimed.operator, child_env(claimed))
        except (OSError, ValueError) as exc:
            ended = rr.finish(
                self.store,
                claimed,
                exit_code=None,
                state=rr.FAILED,
                detail=f"the drive could not start it: {exc}",
            )
            if ended is not None:
                rr.record(self.store, "run_request_finished", ended)
            self.say(f"run request {claimed.id}: could not start ({exc}).")
            return
        self.child, self.running = child, claimed
        try:
            self.running = rr.started(self.store, claimed, child.pid)
        except rr.RequestStateError as exc:
            # The child runs and this drive reaps it; only its pid is unsaid.
            self.say(f"run request {claimed.id}: {exc}.")
        rr.record(
            self.store,
            "run_request_started",
            self.running,
            pid=child.pid,
            drive=self.token,
        )
        self.say(
            f"run request {claimed.id}: running `{claimed.command}` "
            f"as {claimed.operator}."
        )
