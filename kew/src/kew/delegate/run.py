"""Foreground delegation watch-loop assembly (D5).

Wires the pieces the earlier nodes built into one foreground tick loop:

    D3 watcher (:mod:`kew.delegate.watch`) emits deduped candidate ids
      → D2 recognition (:mod:`kew.delegate.recognize`) filters delegated-and-
        actionable issues off their LIVE fields
        → the shared fleet cap gates admission (delegated + epic reservations
          share one ceiling, #864)
          → D4 claim/dispatch (:mod:`kew.delegate.claim`) does the money-path
            reservation + hand-off.

This module is pure orchestration: every external dependency — the watcher,
tracker, dispatch sink, run store, and the loop's ``sleep`` — is injected, so
the whole loop is unit-testable with fakes and the CLI (``cli/delegate.py``)
supplies the live Linear wiring. ``--dry-run`` is threaded through as a flag
that short-circuits before any reservation or spend, logging intent only.
"""

from __future__ import annotations

import enum
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from kew.claim_retention import (
    LINEAR_CLAIM_RECOVERY,
    record_retained_claim,
    retained_claim_lines,
)
from kew.delegate.claim import (
    ClaimOutcome,
    DelegationTracker,
    claim_and_dispatch,
    clear_stranded_rollback,
    is_recoverable_orphan,
    load_stranded_rollbacks,
)
from kew.delegate.errors import FatalTrackerError, ItemGone, TransientTrackerError
from kew.delegate.recognize import is_delegated_and_actionable, is_delegated_to_kew
from kew.pseudo_epics import DELEGATION_EPIC_REF

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.delegate.recognize import CandidateIssue
    from kew.delegate.watch import Candidate, DelegationWatcher
    from kew.graph.driver.dispatch import DispatchSink
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

# Non-zero exit code the watch loop terminates with on a fatal tracker error
# (Task 3, #971) — mirrors the CLI's key-not-set exit (both are auth problems).
_FATAL_TRACKER_EXIT_CODE = 2


class WatchOutcome(enum.Enum):
    """The terminal decision for one candidate in a single tick."""

    #: Recognized, under the cap, and handed to :func:`claim_and_dispatch`.
    DISPATCHED = "dispatched"
    #: Live fields say it is not delegated-to-kew or not in a claimable state.
    SKIPPED_NOT_RECOGNIZED = "skipped_not_recognized"
    RETAINED_CLAIM = "retained_claim"
    #: Recognized, but the shared fleet cap is full — retried next tick.
    QUEUED_FLEET_FULL = "queued_fleet_full"
    #: ``--dry-run``: logged intent without reserving or spending.
    DRY_RUN = "dry_run"
    #: Lost the reservation race to another surface (a clean no-op).
    SKIPPED_LOST_CLAIM = "skipped_lost_claim"
    #: Won the claim but the live assignee was no longer the kew member.
    SKIPPED_REASSIGNED = "skipped_reassigned"
    #: The item became admitted to an epic before claim-time re-verification.
    SKIPPED_EPIC_ADMITTED = "skipped_epic_admitted"
    #: Budget/preflight refused: a refusal row + comment were written.
    REFUSED = "refused"
    #: The item is permanently gone (deleted / not found): a terminal decision
    #: was recorded so the cursor advances past it (N3). Never crash-loops.
    SKIPPED_ITEM_GONE = "skipped_item_gone"
    #: A transient tracker error (rate-limit / blip): backed off and deferred to
    #: a later tick, leaving the candidate undecided so it retries (N3).
    DEFERRED_TRANSIENT = "deferred_transient"
    #: Recognized, but a budget refusal for this item is still inside the
    #: ``refusal_cooldown_seconds`` window — skipped without re-claiming to damp the
    #: refuse → comment churn loop a surviving trigger label would otherwise
    #: drive every poll interval.
    SKIPPED_RECENT_REFUSAL = "skipped_recent_refusal"


# Pass-through mapping from the D4 claim decision to the watch-loop outcome, so
# a caller sees one flat enum regardless of which layer decided.
_CLAIM_OUTCOME_MAP = {
    ClaimOutcome.DISPATCHED: WatchOutcome.DISPATCHED,
    ClaimOutcome.SKIPPED_LOST_CLAIM: WatchOutcome.SKIPPED_LOST_CLAIM,
    ClaimOutcome.SKIPPED_REASSIGNED: WatchOutcome.SKIPPED_REASSIGNED,
    ClaimOutcome.SKIPPED_EPIC_ADMITTED: WatchOutcome.SKIPPED_EPIC_ADMITTED,
    ClaimOutcome.REFUSED: WatchOutcome.REFUSED,
    ClaimOutcome.QUEUED_FLEET_FULL: WatchOutcome.QUEUED_FLEET_FULL,
}

ClaimFn = Callable[..., ClaimOutcome]


def make_is_terminally_decided(
    store: RunStore, epic_ref: str = DELEGATION_EPIC_REF
) -> Callable[[str], bool]:
    """Build the watcher's ``is_terminally_decided`` predicate (D3 injection).

    An item is decided once kew has committed to it: it either holds a live
    delegation reservation (claimed → dispatched) or has a run row (a dispatch
    child or a terminal refusal, #898). Anything else is still undecided, so the
    watcher leaves the cursor behind it and a crash re-emits it safely — the
    reservation, not the cursor, is the double-dispatch guard.
    """

    def _decided(item_id: str) -> bool:
        reserved = any(
            r.item_id == item_id for r in store.list_epic_reservations(epic_ref)
        )
        return reserved or bool(store.list_by_item(item_id))

    return _decided


def reconcile_delegation_reservations(
    store: RunStore,
    config: DispatchConfig,
    *,
    now: datetime,
    epic_ref: str = DELEGATION_EPIC_REF,
) -> set[str]:
    """Release delegation reservations whose run has terminated (N1).

    The epic driver reconciles its own reservations inside every tick, but
    delegated work parks under the ``DELEGATION_EPIC_REF`` pseudo-epic that no
    driver ever ticks — so without this pass a completed delegated run holds its
    fleet slot forever and ``live_run_count()`` never returns to baseline,
    deadlocking every dispatch path in the fleet after ``max_workers``
    completions. Reuses the driver's exact reconciler (terminal-run release +
    dead-child reclaim past the startup grace) scoped to the delegation
    pseudo-epic. Returns the item ids still reserved after the pass.
    """
    from kew.graph.driver.tick import reconcile_epic_reservations

    return reconcile_epic_reservations(epic_ref, store, config, now=now)


def reconcile_stranded_rollbacks(
    store: RunStore,
    tracker: DelegationTracker,
) -> set[str]:
    """Retire legacy automatic rollback requests without mutating Linear."""
    for item_id in load_stranded_rollbacks(store):
        record_retained_claim(
            store, item_id, reason="legacy delegation rollback retired"
        )
        _logger.warning(
            "delegation: claim retained for %s: %s", item_id, LINEAR_CLAIM_RECOVERY
        )
        clear_stranded_rollback(store, item_id)
    return set()


def _recently_refused(
    store: RunStore,
    item_id: str,
    *,
    cooldown_s: float,
    now: Callable[[], datetime],
) -> bool:
    """True while a budget refusal for ``item_id`` is inside its cooldown (N1).

    Damps the refuse → comment churn loop: releasing the reservation on a
    budget refusal (correct for N1) removes the accidental damping the leak
    provided, so a *label*-triggered item — whose trigger label survives the
    retained claim — is re-surfaced by the poll floor every
    ``poll_interval_seconds`` and would re-claim → refuse → comment indefinitely while
    over budget. Skipping the re-claim while the newest run row is a recent
    budget refusal bounds that to one refusal per ``cooldown_s`` and still
    retries once the budget is raised (after at most one cooldown). ``0``
    disables the check.
    """
    from kew.dispatch_core import _BUDGET_REFUSAL_CATEGORY

    if cooldown_s <= 0:
        return False
    rows = store.list_by_item(item_id)
    if not rows:
        return False
    # list_by_item orders by started_at ASC, so the last row is the newest.
    newest = rows[-1]
    if newest.error_category != _BUDGET_REFUSAL_CATEGORY or newest.finished_at is None:
        return False
    return (now() - newest.finished_at).total_seconds() < cooldown_s


def _mark_item_gone(store: RunStore, item_id: str) -> None:
    """Record a terminal decision for a permanently-gone item (N3).

    ``is_terminally_decided`` keys on a run row via ``list_by_item``; writing a
    terminal row makes the watcher's cursor advance past the ghost event so a
    deleted issue is never re-fetched forever. Distinct ``error_category`` keeps
    it out of the budget/dead-child reconcilers' interpretation.
    """
    from kew.run_store import RunRecord

    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=str(uuid.uuid4()),
            issue_num=0,
            item_id=item_id,
            branch=item_id,
            status="failure",
            exit_code=1,
            started_at=now,
            finished_at=now,
            error_category="delegation_item_gone",
            run_kind="delegation",
        )
    )


def _retained_candidate(
    store: RunStore, item_id: str, live: CandidateIssue, member_id: str, label: str
) -> bool:
    """Surface an unresolved retained claim before the starting-state filter."""
    if live.state in ("Todo", "Backlog") or not is_delegated_to_kew(
        live, member_id, label
    ):
        return False
    retained = retained_claim_lines(store, [item_id])
    if not retained:
        return False
    _logger.warning("delegation: %s", retained[0].strip())
    return True


def _recognize_candidate(
    store: RunStore,
    candidate: Candidate,
    live: CandidateIssue,
    member_id: str,
    label: str,
    epic_ref: str = DELEGATION_EPIC_REF,
) -> WatchOutcome | None:
    item_id = candidate.item_id
    if is_recoverable_orphan(store, item_id, live, member_id, epic_ref):
        return None
    if _retained_candidate(store, item_id, live, member_id, label):
        return WatchOutcome.RETAINED_CLAIM
    if not is_delegated_and_actionable(live, member_id, label):
        _logger.info(
            "delegation: %s (%s) not delegated-to-kew or not actionable — skipping",
            item_id,
            candidate.source,
        )
        return WatchOutcome.SKIPPED_NOT_RECOGNIZED
    return None


def process_candidate(
    candidate: Candidate,
    *,
    member_id: str,
    label: str,
    store: RunStore,
    tracker: DelegationTracker,
    sink: DispatchSink,
    fleet_cap: int,
    dry_run: bool,
    config_path: Path | None = None,
    epic_ref: str = DELEGATION_EPIC_REF,
    claim: ClaimFn = claim_and_dispatch,
    backoff: Callable[[], None] = lambda: None,
    refusal_cooldown_seconds: float = 0.0,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WatchOutcome:
    """Recognize, then claim/dispatch (or dry-run) one candidate.

    Recognition reads live fields and rejects stale or non-actionable candidates.
    Dry runs return before reservation or spend.
    The claim prepares owner-aware admission before atomically checking capacity
    and inserting the reservation.

    Hard tracker errors are handled per-candidate so the loop never crash-loops
    (N3): a permanently-gone item records a terminal decision (cursor advances,
    no re-fetch), and a transient failure backs off and defers to a later tick.
    """
    item_id = candidate.item_id
    try:
        live = tracker.fetch_live(item_id)
    except ItemGone:
        _logger.warning(
            "delegation: %s (%s) is gone (deleted / not found) — recording "
            "terminal decision and advancing past it",
            item_id,
            candidate.source,
        )
        _mark_item_gone(store, item_id)
        return WatchOutcome.SKIPPED_ITEM_GONE
    except TransientTrackerError:
        _logger.warning(
            "delegation: transient tracker error on %s (%s) — backing off, "
            "will retry next tick",
            item_id,
            candidate.source,
            exc_info=True,
        )
        backoff()
        return WatchOutcome.DEFERRED_TRANSIENT
    recognition = _recognize_candidate(
        store, candidate, live, member_id, label, epic_ref
    )
    if recognition is not None:
        return recognition

    # Refusal-churn damping (N1): a budget-refused, label-triggered item keeps
    # its trigger label without a claim, so the poll floor re-surfaces it every
    # interval. Skip the re-claim while a refusal is still inside its cooldown so
    # we don't re-refuse + re-comment on every poll; the window still lets a
    # raised budget retry.
    if _recently_refused(store, item_id, cooldown_s=refusal_cooldown_seconds, now=now):
        _logger.info(
            "delegation: %s (%s) refused within the last %ss — skipping re-claim "
            "to avoid churn (retries after cooldown)",
            item_id,
            candidate.source,
            refusal_cooldown_seconds,
        )
        return WatchOutcome.SKIPPED_RECENT_REFUSAL

    if dry_run:
        _logger.info(
            "delegation: [dry-run] would claim / would dispatch %s (no reserve/spend)",
            item_id,
        )
        return WatchOutcome.DRY_RUN

    try:
        outcome = claim(
            item_id,
            member_id=member_id,
            label=label,
            store=store,
            tracker=tracker,
            sink=sink,
            config_path=config_path,
            epic_ref=epic_ref,
            fleet_cap=fleet_cap,
        )
    except ItemGone:
        # The live re-verify inside the claim saw the item vanish AFTER the cheap
        # recognition fetch (the claim already released its reservation before
        # re-raising). Record a terminal decision so the cursor advances (N3).
        _logger.warning(
            "delegation: %s went gone during claim — recording terminal decision",
            item_id,
        )
        _mark_item_gone(store, item_id)
        return WatchOutcome.SKIPPED_ITEM_GONE
    except TransientTrackerError:
        _logger.warning(
            "delegation: transient tracker error claiming %s — backing off, "
            "will retry next tick",
            item_id,
            exc_info=True,
        )
        backoff()
        return WatchOutcome.DEFERRED_TRANSIENT
    return _CLAIM_OUTCOME_MAP[outcome]


def run_watch_loop(
    *,
    watcher: DelegationWatcher,
    process_one: Callable[[Candidate], WatchOutcome],
    sleep: Callable[[float], None],
    fast_interval_seconds: float,
    reconcile: Callable[[], None] = lambda: None,
    reap: Callable[[], None] = lambda: None,
) -> None:
    """Drive the watcher at ``fast_interval_seconds``, processing each candidate.

    Foreground and unbounded by design (like ``kew ingest serve``): it ticks
    until a ``KeyboardInterrupt`` propagates out of ``watcher.tick()`` or
    ``sleep`` — the CLI catches that for a clean Ctrl-C shutdown. The remote
    poll's own throttle to ``poll_interval_seconds`` lives inside the watcher, so the
    two cadences stay independent while this loop only paces the fast read.

    ``reconcile`` runs once at the head of every tick (N1): it releases
    delegation reservations whose run terminated so a finished delegated run
    frees its fleet slot BEFORE this tick's cap check, exactly as the epic
    driver reconciles at the head of its own tick. A crash-hardened
    ``process_one`` (D-N3) keeps one bad candidate from killing the loop.

    A :class:`FatalTrackerError` (an expired/revoked key, Task 3, #971) — from
    the poll leg inside ``watcher.tick()`` or the per-candidate fetch — is NOT
    survivable: it terminates the loop with a loud, actionable message and a
    non-zero exit rather than retrying forever, silently doing nothing.
    """
    while True:
        reap()
        reconcile()
        try:
            candidates = watcher.tick()
        except FatalTrackerError as exc:
            _fatal_tracker_exit(exc)
        for candidate in candidates:
            try:
                process_one(candidate)
            except KeyboardInterrupt:
                raise
            except FatalTrackerError as exc:
                _fatal_tracker_exit(exc)
            except Exception:
                # Last-resort guard (N3): the classified tracker errors are
                # handled inside ``process_candidate``; this catches anything
                # else (an unmapped API error from a tracker write, a bug) so a
                # single bad candidate can never crash-loop the foreground watch.
                # The reservation seam already releases on its own failures, so
                # the item is left undecided and retried on a later tick.
                _logger.exception(
                    "delegation: unhandled error processing %s — skipping this "
                    "candidate, loop continues",
                    candidate.item_id,
                )
        sleep(fast_interval_seconds)


def _fatal_tracker_exit(exc: FatalTrackerError) -> NoReturn:
    """Terminate the watch loop on a fatal tracker error (Task 3, #971).

    Logs a loud, actionable message and exits non-zero. An auth failure is not
    transient: retrying only spins forever doing nothing, so the operator must
    fix the key and restart rather than let a dead watcher look alive.
    """
    _logger.error(
        "kew delegate watch: fatal tracker error — %s. Linear authentication "
        "failed (expired/revoked LINEAR_API_KEY); the watcher cannot continue "
        "and is exiting. Fix the key and restart.",
        exc,
    )
    raise SystemExit(_FATAL_TRACKER_EXIT_CODE)
