"""The drive's sweep of every epic's reservations (#3192).

Only an epic's own tick reconciles its reservations, so a reservation under an
epic nobody ticks any more — archived, on another tracker, never rostered —
held its fleet slot forever: ``live_run_count`` counts a reservation with no
run row as live. ``kew drive`` runs this sweep once per round over the
reservations of EVERY epic ref and applies the rules that need no liveness
probe:

- a reservation with no run row past the startup grace is a child that never
  started — the tick's own missing-run rule settles it;
- one whose run ended is released, after the tick's own terminal settlement;
- one with no run row whose tracker item is closed is released.

A ``pending`` run is never touched here: proving its owner dead is the owning
tick's job. Nor is the reservation of a hand-run ``kew review`` whose process
still lives, whatever its run row says (#3208): that review gives it back.
Each release is generation-conditional, so a reservation another actor
re-wrote between the read and the release survives.

The sweep holds no epic's driver lock, so every other actor that settles or
re-reserves an item — an operator ``kew epic advance``, a console request
child, the delegate watcher, a dispatch child admitting — may act between the
sweep's snapshot and its writes. Each write therefore re-tests what it acts
on: a reservation is settled only while it is still held, the closed-issue
release re-reads the run rows inside its own delete transaction, and a
refusal hold is retired only if it predates the sweep's check and no newer
generation holds the item.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from kew.continuation_recovery import CheckoutAdmissionRefusal
from kew.epic_holds import PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX
from kew.fleet_placement import is_remote
from kew.graph.driver.tick import (
    _RESERVATION_STARTUP_GRACE_SECONDS,
    _continuation_owned_by_run,
    _hold_unreconcilable_reservation,
    _owner_alive,
    _PreexistingWorktreeRefusal,
    _reconcile_missing_run,
    _terminal_reservation_may_release,
    resolve_reservation_run,
)
from kew.ids import item_ref
from kew.pseudo_epics import PSEUDO_EPIC_REFS

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import EpicHold, EpicReservation, RunRecord, RunStore

_logger = logging.getLogger(__name__)

#: Whether a reservation's tracker item is closed: True, False, or None when
#: it could not be read — an unread item is never released on.
IssueClosed = Callable[["EpicReservation"], "bool | None"]

#: Whether a run's owner is alive: True, False, or None when unprobeable.
OwnerProbe = Callable[["RunRecord"], "bool | None"]


@dataclass(frozen=True)
class SweptReservation:
    """One reservation the sweep released, and why."""

    epic_ref: str
    item_id: str
    run_id: str
    reserved_at: datetime
    why: str

    def describe(self) -> str:
        return (
            f"epic {self.epic_ref}: released the reservation of "
            f"{item_ref(self.item_id)} (run {self.run_id}, reserved "
            f"{self.reserved_at.isoformat()}) — {self.why}"
        )


@dataclass(frozen=True)
class SlotHolder:
    """One reservation holding a fleet slot, as ``kew drive status`` shows it:
    the run's owner pid, what is known of that owner, and what the sweep makes
    of the reservation."""

    reservation: EpicReservation
    owner_pid: int | None
    owner: str
    verdict: str


def all_reservations(store: RunStore) -> list[EpicReservation]:
    """Every reservation in the ledger, across every epic ref."""
    return [
        reservation
        for epic_ref in sorted(store.live_reservation_counts_by_epic())
        for reservation in store.list_epic_reservations(epic_ref)
    ]


def _still_held(store: RunStore, reservation: EpicReservation) -> bool:
    """Whether this exact generation (item AND run id) still holds its slot."""
    return any(
        held.item_id == reservation.item_id and held.run_id == reservation.run_id
        for held in store.list_epic_reservations(reservation.epic_ref)
    )


def _item_reserved(store: RunStore, item_id: str) -> bool:
    """Whether any generation, under any epic, holds ``item_id`` now."""
    return any(r.item_id == item_id for r in all_reservations(store))


def _owned_now(store: RunStore, reservation: EpicReservation) -> bool:
    """Whether a run row or a live continuation owns the reservation — the
    two facts the closed-issue rule decided on, read again."""
    owner = _continuation_owned_by_run(store, reservation.epic_ref, reservation.run_id)
    return owner is not None or resolve_reservation_run(store, reservation) is not None


def _settle(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    *,
    now: datetime,
    issue_closed: IssueClosed | None,
) -> str | None:
    """Apply the probe-free rules to one reservation; the reason it may go."""
    from kew.review_occupancy import hand_run_review_running

    if hand_run_review_running(store, reservation):
        # Its run row ends before it publishes (#3208); the review gives the
        # reservation back itself.
        return None
    owner = _continuation_owned_by_run(store, reservation.epic_ref, reservation.run_id)
    run = (
        store.get(reservation.run_id)
        if owner is not None
        else resolve_reservation_run(store, reservation)
    )
    if run is not None:
        return _release_ended(store, config, reservation, run, owner, now=now)
    grace = timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)
    if now - reservation.reserved_at > grace:
        _reconcile_missing_run(store, config, reservation, now=now)
        return f"no run row within the {_RESERVATION_STARTUP_GRACE_SECONDS}s grace"
    if owner is not None or issue_closed is None or not issue_closed(reservation):
        return None
    # The tracker read takes seconds; a dispatch child may have admitted under
    # this reservation meanwhile. Re-read inside the delete.
    released = store.release_epic_node_item_unless(
        reservation.item_id,
        reservation.run_id,
        keep=lambda: _owned_now(store, reservation),
    )
    return "its tracker item is closed" if released else None


def _release_ended(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    owner: object,
    *,
    now: datetime,
) -> str | None:
    """Release a reservation whose run ended, after the tick's settlement."""
    if run.status == "pending" or not _terminal_reservation_may_release(
        store, config, reservation, run, owner, now=now
    ):
        return None
    store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
    return f"its run {run.run_id} ended {run.status}"


def _retire_refusal_hold(
    store: RunStore, reservation: EpicReservation, *, checked_at: datetime
) -> None:
    """Retire the reconciler's refusal hold of a reservation the sweep settled.

    The epic's own reconcile never sees a reservation the sweep released, so
    the sweep retires the hold that reconcile would have. A hold names no
    reservation, so one is this reservation's only if it was written before
    the sweep saw the reservation held (``checked_at``) and no newer
    generation holds the item now; both are read inside the delete. A hold a
    re-reservation's refused reconcile wrote meanwhile is left standing.
    """
    if reservation.epic_ref in PSEUDO_EPIC_REFS:
        return

    def keep(hold: EpicHold) -> bool:
        return hold.held_at > checked_at or _item_reserved(store, reservation.item_id)

    released = store.remove_epic_hold(
        reservation.epic_ref,
        reservation.item_id,
        only_reason_prefixes=(PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,),
        keep=keep,
    )
    if released is not None:
        _logger.info(
            "epic %s: node %s un-held — its reservation settled, so the "
            "earlier refusal no longer stands (was: %s)",
            reservation.epic_ref,
            reservation.item_id,
            released.reason,
        )


def _sweep_one(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    *,
    now: datetime,
    issue_closed: IssueClosed | None,
) -> SweptReservation | None:
    """Settle one reservation from the round's snapshot, if it is still held."""
    # Taken BEFORE the read below: a hold written at or before it was written
    # while this generation still held the item.
    checked_at = datetime.now(UTC)
    if not _still_held(store, reservation):
        # Another actor settled it since the snapshot; what it left — a new
        # generation, a hold — is that actor's, not this stale reservation's.
        return None
    try:
        why = _settle(store, config, reservation, now=now, issue_closed=issue_closed)
    except (_PreexistingWorktreeRefusal, CheckoutAdmissionRefusal) as exc:
        if _still_held(store, reservation):
            _hold_unreconcilable_reservation(
                store, config, reservation.epic_ref, reservation, exc
            )
        else:
            _logger.warning(
                "epic %s: reservation of %s (run %s) was refused (%s) and "
                "settled by another actor meanwhile; no hold written",
                reservation.epic_ref,
                item_ref(reservation.item_id),
                reservation.run_id,
                exc,
            )
        return None
    if why is None or _still_held(store, reservation):
        return None
    _retire_refusal_hold(store, reservation, checked_at=checked_at)
    return SweptReservation(
        epic_ref=reservation.epic_ref,
        item_id=reservation.item_id,
        run_id=reservation.run_id,
        reserved_at=reservation.reserved_at,
        why=why,
    )


def sweep_reservations(
    store: RunStore,
    config: DispatchConfig,
    *,
    now: datetime,
    issue_closed: IssueClosed | None = None,
) -> list[SweptReservation]:
    """Release every reservation, under any epic, that no longer holds work.

    Each release is logged as a warning naming the reservation. A checkout
    refusal keeps the reservation and holds its node, as the tick does; a
    reservation the sweep settles retires that refusal hold, as the tick does.
    A reservation whose settlement fails is logged by name and kept; the rest
    of the round is still swept, and the next round tries it again.
    """
    released: list[SweptReservation] = []
    for reservation in all_reservations(store):
        try:
            swept = _sweep_one(
                store, config, reservation, now=now, issue_closed=issue_closed
            )
        except Exception as exc:  # noqa: BLE001 - one reservation never stops the round
            _logger.error(
                "epic %s: could not sweep the reservation of %s (run %s): %s: %s; "
                "kept, the next round tries again",
                reservation.epic_ref,
                item_ref(reservation.item_id),
                reservation.run_id,
                type(exc).__name__,
                exc,
            )
            continue
        if swept is not None:
            _logger.warning("%s", swept.describe())
            released.append(swept)
    return released


def _owner_word(alive: bool | None) -> str:
    return {True: "alive", False: "dead", None: "unprobeable"}[alive]


def _assess(
    store: RunStore,
    reservation: EpicReservation,
    *,
    now: datetime,
    probe: OwnerProbe,
) -> SlotHolder:
    """What the sweep's rules make of one reservation, without acting."""
    from kew.review_occupancy import hand_run_review_running

    if hand_run_review_running(store, reservation):
        return SlotHolder(
            reservation,
            None,
            "alive",
            "a hand-run `kew review` whose process lives: kept until it ends",
        )
    owner = _continuation_owned_by_run(store, reservation.epic_ref, reservation.run_id)
    run = (
        store.get(reservation.run_id)
        if owner is not None
        else resolve_reservation_run(store, reservation)
    )
    if run is None:
        grace = timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)
        verdict = (
            "no run row past the startup grace: the next sweep releases it"
            if now - reservation.reserved_at > grace
            else "no run row yet, inside the startup grace"
        )
        return SlotHolder(reservation, None, "no run row", verdict)
    if run.status != "pending":
        return SlotHolder(
            reservation,
            run.owner_pid,
            "ended",
            f"run {run.run_id} ended {run.status}: the next sweep settles it",
        )
    return SlotHolder(
        reservation,
        run.owner_pid,
        _owner_word(probe(run)),
        f"run {run.run_id} pending: kept until its epic's tick proves the owner dead",
    )


def local_owner_alive(run: RunRecord) -> bool | None:
    """The dispatch-owner process probe the tick's reaper starts from (pid,
    start time, process group). It reads this machine only, so a run placed
    on another machine reads unprobeable, never dead, and no container is
    inspected."""
    return None if is_remote(run) else _owner_alive(run)


def slot_holders(
    store: RunStore, *, now: datetime, probe: OwnerProbe = local_owner_alive
) -> list[SlotHolder]:
    """Every reservation holding a slot, each with its owner and verdict."""
    holders: list[SlotHolder] = []
    for reservation in all_reservations(store):
        try:
            holders.append(_assess(store, reservation, now=now, probe=probe))
        except Exception as exc:  # noqa: BLE001 - one unreadable row never hides the rest
            holders.append(
                SlotHolder(reservation, None, "unknown", f"unreadable ({exc})")
            )
    return holders
