"""Who else already holds a PR a hand-started ``kew review <PR>`` would take (#3047).

A standalone review launches its agent in a container named ``kew-<pr>``; the
drive's governed review and remediation name theirs for the PR's item
(``kew-<item>``, ``branch_slug(item_id or issue_num)`` in ``dispatch_core`` and
the review launcher). When the drive's ``kew epic _review-node`` child is already
reviewing or remediating that PR, a second launch used to get as far as
``docker create`` and fail there on the name, after it had checked out a review
worktree it then left behind — reporting a container failure, not that the drive
held the PR.

:func:`review_occupant` asks before anything is checked out: does a live holder
keep the PR's item under the review pseudo-epic, and does a container of any
name a run on this PR would take already exist. Either answer names the holder,
and the review refuses on it.

A holder is live by the rules the codebase already reads a dead one by, so a
child that was killed, or a drive that stopped after reserving, never blocks
the review for good:

* the recorded owner (``identity_at_admission``) is probed first: an owner
  provably alive holds at any age, and one provably gone — or whose host
  rebooted under it — does not, as the reaper proves it;
* only when the owner cannot be probed (no recorded identity, another
  machine) does the row's age decide: past
  :func:`kew.service.work.pending_run_max_age_s` it is the dead row
  ``review_in_flight`` reads it as;
* a review-stage row whose owner is dead still holds while any run its child
  started (``tick._reap_stage_children``'s rows) is still working — judged by
  its recorded container or sandbox through the reaper's own liveness probe,
  never by the dead leader's grace — and that run is named as the holder;
* a review-stage row its child never claimed, and a reservation whose run row
  was never written, are live only within the reaper's startup grace.

This check alone is not a lock (#3047). The lock is the item's review
reservation, which a hand-run review now takes too (#3208):
:func:`reserve_standalone_review` takes it after this check passes, so a drive
tick that reserves between the two refuses the review, and a drive tick that
comes after finds the item reserved and launches nothing. The reservation, not
a container name, is what both launchers collide on: a ``docker inspect`` that
cannot answer leaves the reservation standing. While the hand-run review's own
process lives (:func:`hand_run_review_running`), nothing but that review gives
the reservation back: not the tick's reconciler, not the drive's sweep, and not
the kill of a merged or closed PR's reviewers.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.fleet_placement import is_remote
from kew.ids import branch_slug
from kew.process_groups import (
    identity_of_process,
    machine_boot,
    owner_alive,
    pid_exists,
    process_alive,
)
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_liveness import host_died_under
from kew.run_store import (
    REVIEW_STAGE_RUN_KIND,
    AuditEvent,
    EpicReservation,
    RunRecord,
    RunStore,
)
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.service.work import pending_run_max_age_s, run_executing
from kew.stop_recovery import container_missing

_INSPECT_TIMEOUT_SECONDS = 10.0

_logger = logging.getLogger(__name__)

#: The audit event a hand-run ``kew review`` records when it takes an item's
#: review reservation (#3208). It names the process holding the reservation,
#: so the drive can name the holder and the reconciler can prove it dead
#: before the review has written a run row.
STANDALONE_REVIEW_RESERVED = "standalone_review_reserved"


def review_container_names(pr_number: int, items: Iterable[str]) -> tuple[str, ...]:
    """Every container name a run on *pr_number* would launch under.

    The standalone review's own ``kew-<pr>`` first, then ``kew-<item>`` for each
    tracker item the PR may belong to — the name a governed run takes.
    """
    names = [f"kew-{branch_slug(str(pr_number))}"]
    names.extend(f"kew-{branch_slug(item)}" for item in items)
    return tuple(dict.fromkeys(names))


def review_occupant(
    pr_number: int,
    *,
    items: Iterable[str | None],
    config: DispatchConfig,
    store: RunStore | None,
    now: datetime | None = None,
) -> str | None:
    """Name whatever already holds *pr_number*, or ``None`` when nothing does.

    *items* are the tracker items the PR may belong to (the head branch's owner
    in the ledger, the issue its branch, body or producing run names); a review
    holder keeps the PR when it holds one of them. The answer is one clause that
    names the holder and what to do about it.
    """
    wanted = tuple(dict.fromkeys(item for item in items if item))
    moment = now or datetime.now(UTC)
    holder = _live_review_holder(store, wanted, config, moment)
    if holder is not None:
        return holder
    return _container_holder(
        config.sandbox.runtime, review_container_names(pr_number, wanted)
    )


def _live_review_holder(
    store: RunStore | None,
    wanted: tuple[str, ...],
    config: DispatchConfig,
    now: datetime,
) -> str | None:
    if store is None or not wanted:
        return None
    for reservation in store.list_epic_reservations(REVIEW_EPIC_REF):
        if reservation.item_id not in wanted:
            continue
        holder = _reservation_holder(store, reservation, config, now)
        if holder is not None:
            return holder
    return None


def _reservation_holder(
    store: RunStore,
    reservation: EpicReservation,
    config: DispatchConfig,
    now: datetime,
) -> str | None:
    """Name the live actor behind one review reservation, or ``None``."""
    run8 = reservation.run_id[:8]
    row = store.get(reservation.run_id)
    if row is None:
        return _rowless_holder(store, reservation, now)
    if row.status != "pending":
        return None  # finished; the tick releases its reservation
    if _holder_alive(row, config, now):
        who = (
            "a `kew epic _review-node` child"
            if row.run_kind == REVIEW_STAGE_RUN_KIND
            else f"a {row.run_kind} run"
        )
        return (
            f"{who} (run {run8}, kind {row.run_kind}) holds item "
            f"{reservation.item_id}; run it again once that run finishes"
        )
    if row.run_kind != REVIEW_STAGE_RUN_KIND:
        return None
    child = _live_stage_child(store, reservation.item_id, row, config, now)
    if child is None:
        return None
    return (
        f"a {child.run_kind} run (run {child.run_id[:8]}) the dead "
        f"`kew epic _review-node` child (run {run8}) started still holds item "
        f"{reservation.item_id}; run it again once that run finishes"
    )


def _rowless_holder(
    store: RunStore, reservation: EpicReservation, now: datetime
) -> str | None:
    """Name the holder of a reservation whose run row is not written yet."""
    # A hand-run review recorded its process when it reserved (#3208): that
    # process, probed, decides, as the tick's reconciler reads it.
    owner = standalone_reservation_owner(store, reservation.run_id)
    if owner is not None:
        return reservation_holder(store, reservation.item_id) if owner else None
    # The drive reserves first and writes the stage row second
    # (``review_detach.launch_detached``): a fresh reservation with no row is
    # a holder starting up. Past the grace it is one that died before its
    # row, which the tick reclaims (``_reconcile_missing_run``).
    if _age(now, reservation.reserved_at) > _startup_grace():
        return None
    return (
        f"item {reservation.item_id} is reserved for review (run "
        f"{reservation.run_id[:8]}) and its run is starting; run it again once "
        "that run finishes"
    )


def _holder_alive(row: RunRecord, config: DispatchConfig, now: datetime) -> bool:
    """Whether pending *row* still names a live holder, by the reaper's evidence.

    The owner is probed first: its answer, when it has one, is final at any
    age. The in-flight age bound is the work view's reading, not proof of
    death (#2337), so it decides only for an owner nobody can probe.
    """
    if not row.owner_pid:
        # A stage row its child never claimed (``_reap_unstarted_stage``) is
        # live only within the startup grace; any other ownerless row cannot be
        # probed from here.
        if row.run_kind == REVIEW_STAGE_RUN_KIND:
            return _age(now, row.started_at) <= _startup_grace()
        return _within_in_flight_bound(row, config, now)
    owner = _owner_verdict(row)
    if owner is None:
        return _within_in_flight_bound(row, config, now)
    return owner


def _within_in_flight_bound(
    row: RunRecord, config: DispatchConfig, now: datetime
) -> bool:
    return run_executing(row, now=now, max_age_s=pending_run_max_age_s(config))


def _owner_verdict(row: RunRecord) -> bool | None:
    """The recorded owner's liveness: True, False, or None when unprobeable."""
    if is_remote(row):
        return None  # another machine's process table is not ours to read
    if host_died_under(row):
        return False
    return owner_alive(
        row.pgid or 0,
        row.owner_pid,
        row.owner_start_time,
        row.owner_ppid,
        row.owner_command,
        row.owner_start_token,
        row.owner_boot_id,
    )


def _live_stage_child(
    store: RunStore,
    item_id: str,
    stage: RunRecord,
    config: DispatchConfig,
    now: datetime,
) -> RunRecord | None:
    """A run the dead stage owner started whose work is still going on.

    The reaper keeps the stage's reservation until every pending row the child
    owned is reaped (``tick._reap_stage_children``), under the probe it reaps
    with — the OpenShell gateway's included. The same rows are read here: a
    reviewer or remediation still running under a killed ``_review-node``
    holds the PR as surely as the child did (see :func:`_child_holds`). The
    probe only reads (an ``inspect``, a gateway ``sandbox get``, and on
    OpenShell a read-only exec probe inside the sandbox, under their own
    timeouts); nothing here reaps, writes or kills.
    """
    if stage.owner_pid is None:
        # A stage row its child never claimed started nothing, so no run on the
        # item is its child (the reaper returns early here too).
        return None
    from kew.graph.driver.tick import default_run_liveness

    base = default_run_liveness(config)
    children = (
        run
        for run in store.list_by_item(item_id)
        if run.run_id != stage.run_id
        and run.status == "pending"
        and run.owner_pid == stage.owner_pid
        and run.owner_start_time == stage.owner_start_time
    )
    return next((run for run in children if _child_holds(run, base, config, now)), None)


def _child_holds(
    run: RunRecord,
    base: Callable[[RunRecord], bool | None],
    config: DispatchConfig,
    now: datetime,
) -> bool:
    """Whether *run*, left behind by a dead leader, still holds the PR.

    A run that recorded a workload (a container, or an OpenShell sandbox) is
    judged by that workload, through the reaper's own probe: alive holds, gone
    frees, and an answer nobody can give falls back to the in-flight bound.
    The reaper's shortcut — a contained run under a dead owner is dead once
    past the startup grace — is safe only because the reaper then kills the
    workload; this check never kills, so it never takes the shortcut. A
    workload that reads gone within the startup grace still holds: the name is
    recorded before the create, so a create the dead leader started can still
    commit. A run with no workload is dead the moment its owner is, by the
    reaper's rule.
    """
    if run.container_name:
        workload = base(run)
        if workload is None:
            return _within_in_flight_bound(run, config, now)
        return workload or _age(now, run.started_at) <= _startup_grace()
    from kew.graph.driver.tick import _pending_run_is_dead

    verdicts: list[bool | None] = []

    def probe(record: RunRecord) -> bool | None:
        verdicts.append(base(record))
        return verdicts[-1]

    if _pending_run_is_dead(run, probe, now=now):
        return False
    if verdicts and verdicts[-1] is None:
        return _within_in_flight_bound(run, config, now)
    return True


def _startup_grace() -> timedelta:
    """The reaper's grace for a holder that has not written or claimed its row."""
    from kew.graph.driver.tick import _RESERVATION_STARTUP_GRACE_SECONDS

    return timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)


def _age(now: datetime, then: datetime) -> timedelta:
    return now - (then if then.tzinfo is not None else then.replace(tzinfo=UTC))


def _container_holder(runtime: str, names: Iterable[str]) -> str | None:
    """Name the first existing container among *names*, running or stopped."""
    if not runtime or runtime == OPENSHELL_RUNTIME:
        return None  # a gateway sandbox, not a ``docker inspect`` target
    for name in names:
        state = _container_state(runtime, name)
        if state == "running":
            return f"container {name!r} is already running; run it again once it exits"
        if state == "stopped":
            return (
                f"a stopped container {name!r} still holds that name; remove it "
                f"with `{runtime} rm {name}`, then run it again"
            )
    return None


def _container_state(runtime: str, name: str) -> str:
    """``running``, ``stopped`` or ``absent`` for the container named *name*.

    A failure to ask — no runtime installed, a daemon that does not answer —
    reads as absent, with a warning naming why: the launch's own name check
    still stands behind this.
    """
    try:
        result = subprocess_utils.run(
            [
                runtime,
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.State.Running}}",
                name,
            ],
            check=False,
            timeout=_INSPECT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn_unasked(runtime, name, str(exc))
        return "absent"
    if result.returncode == 0:
        return "running" if result.stdout.strip() == "true" else "stopped"
    if not container_missing(result.stderr or "", "", name):
        _warn_unasked(runtime, name, (result.stderr or "").strip())
    return "absent"


def _warn_unasked(runtime: str, name: str, why: str) -> None:
    _logger.warning(
        "could not ask %s whether container %r exists (%s); treating it as absent",
        runtime,
        name,
        why,
    )


def reserve_standalone_review(
    store: RunStore,
    item_id: str,
    run_id: str,
    *,
    pr_number: int,
    repo: str | None,
) -> str | None:
    """Take *item_id*'s review reservation for a hand-run review (#3208).

    Returns ``None`` once it is held, else the clause naming who holds it. The
    reserve is the store's atomic insert, so of a hand-run review and a drive
    tick racing for one item exactly one wins. The record naming the holder's
    process commits with it, so no reader ever sees the reservation as a
    drive reviewer's, and no reservation stands that nothing could prove dead.

    The process's own birth is recorded whether or not it leads its process
    group: ``uv run kew review``, a script or a pipeline stage does not, and
    joining a group of its own would change the shell's job control.
    """
    pid = os.getpid()
    recorded = store.append_audit_event_reserving_item(
        REVIEW_EPIC_REF,
        item_id,
        run_id,
        event=STANDALONE_REVIEW_RESERVED,
        actor_type="human",
        actor_id="kew review",
        repo=repo,
        payload={
            "pr_number": pr_number,
            "item_id": item_id,
            "owner_pid": pid,
            **identity_of_process(pid),
        },
    )
    return None if recorded else reservation_holder(store, item_id)


def standalone_reservation(store: RunStore, run_id: str) -> AuditEvent | None:
    """The record a hand-run review wrote when it took reservation *run_id*."""
    events = store.list_audit_events(
        event=STANDALONE_REVIEW_RESERVED, run_id=run_id, limit=1
    )
    return events[0] if events else None


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def standalone_owner_alive(event: AuditEvent) -> bool | None:
    """Whether the hand-run review that recorded *event* is still running.

    Read by the process's own kernel birth token, whatever group it is in.
    ``None`` when its identity could not be recorded or probed (another
    machine, a birth nothing can read); :func:`standalone_reservation_owner`
    decides what that means for the reservation.
    """
    payload = event.payload or {}
    pid = payload.get("owner_pid")
    if not isinstance(pid, int):
        return None
    return process_alive(
        pid,
        _text(payload, "owner_start_time"),
        _text(payload, "owner_start_token"),
        _text(payload, "owner_command"),
        _text(payload, "owner_boot_id"),
    )


def standalone_reservation_owner(store: RunStore, run_id: str) -> bool | None:
    """Whether the hand-run review holding reservation *run_id* still runs.

    ``None`` when *run_id* is not a hand-run review's, or its owner ran
    elsewhere and cannot be probed. A hand-run review's run row is keyed to
    its reservation and is finished before the review publishes, so while this
    reads alive the row's status says nothing about whether the reservation is
    still in use.

    It fails closed on this machine: an owner recorded under this boot whose
    probe cannot answer reads alive while its pid still names a process
    (:func:`_unprobed_owner_present`). Only a pid no process holds, or a
    different boot or birth, gives the reservation up.
    """
    event = standalone_reservation(store, run_id)
    if event is None:
        return None
    alive = standalone_owner_alive(event)
    return alive if alive is not None else _unprobed_owner_present(event, run_id)


#: When each kept-unknown hand-run reservation was last warned about, by run id.
_KEPT_UNKNOWN_WARNED: dict[str, float] = {}
_KEPT_UNKNOWN_WARN_EVERY_SECONDS = 3600.0


def _unprobed_owner_present(event: AuditEvent, run_id: str) -> bool | None:
    """Whether an owner whose probe could not answer still holds its pid.

    ``None`` unless the owner was recorded under this machine's current boot:
    another machine's pid, or an unreadable boot, says nothing here. ``False``
    once the pid names no process. Otherwise the reservation is kept, with a
    warning at most once an hour per reservation, since a pid that outlived
    its owner would hold it until that pid is gone too.
    """
    payload = event.payload or {}
    pid = payload.get("owner_pid")
    boot = _text(payload, "owner_boot_id")
    if not isinstance(pid, int) or boot is None or boot != machine_boot():
        return None
    if not pid_exists(pid):
        return False
    now = time.monotonic()
    last = _KEPT_UNKNOWN_WARNED.get(run_id)
    if last is None or now - last >= _KEPT_UNKNOWN_WARN_EVERY_SECONDS:
        _KEPT_UNKNOWN_WARNED[run_id] = now
        _logger.warning(
            "hand-run review reservation %s kept: its owner pid %s still exists "
            "but its identity could not be probed; it is released once that pid "
            "is gone",
            run_id,
            pid,
        )
    return True


def hand_run_review_running(store: RunStore, reservation: EpicReservation) -> bool:
    """Whether *reservation* belongs to a hand-run ``kew review`` that still runs.

    The one test every path that can release or kill a reservation asks
    first: the tick's reconciler, the sweep ``kew drive`` runs every round,
    and the kill of a merged or closed PR's reviewers. While it reads True the
    review gives the reservation back itself, from its own ``finally``; only
    once its process is provably gone, or cannot be probed, do the run row and
    the reservation's age decide, as for any other holder.
    """
    return standalone_reservation_owner(store, reservation.run_id) is True


def hand_run_holder(store: RunStore, reservation: EpicReservation) -> str | None:
    """The clause naming the hand-run review behind *reservation*, if it is one."""
    if standalone_reservation(store, reservation.run_id) is None:
        return None
    return reservation_holder(store, reservation.item_id)


def reservation_holder(store: RunStore, item_id: str) -> str:
    """Name whoever holds *item_id*'s reservation: its run id, pid and start."""
    reservation = store.item_reservation(item_id)
    if reservation is None:
        # Released between the refused reserve and this read.
        return (
            f"another run reserved item {item_id} as this one tried and has "
            "since let it go; run it again"
        )
    row = store.get(reservation.run_id)
    event = standalone_reservation(store, reservation.run_id)
    pid: object = row.owner_pid if row is not None else None
    if not pid and event is not None:
        pid = (event.payload or {}).get("owner_pid")
    started = row.started_at if row is not None else reservation.reserved_at
    return (
        f"{_holder_kind(reservation, row, event)} holds item {item_id} (run "
        f"{reservation.run_id}, pid {pid or 'unknown'}, started "
        f"{started.isoformat()}); run it again once that run finishes"
    )


def _holder_kind(
    reservation: EpicReservation, row: RunRecord | None, event: AuditEvent | None
) -> str:
    if event is not None:
        return "a hand-run `kew review`"
    if row is None:
        return f"a {reservation.epic_ref} reservation"
    if row.run_kind == REVIEW_STAGE_RUN_KIND:
        return "a `kew epic _review-node` child"
    return f"a {row.run_kind} run"
