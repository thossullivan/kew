"""The assign-to-kew claim + dispatch seam — the money-path reuse core (D4).

For a recognized, delegated-to-kew issue this module runs the atomic
claim → re-verify → dispatch handoff, reusing every proven primitive rather
than reimplementing any of them:

1. **Claim** via :meth:`RunStore.reserve_epic_node_item` — the same atomic
   double-dispatch guard the epic driver uses (an ``INSERT`` that a losing
   caller cannot repeat). A losing claim is a clean no-op skip: the loser
   never re-verifies, transitions, comments, or dispatches.
2. **Re-verify live** state after winning the reservation. A candidate snapshot
   may be stale. The item must remain actionable and assigned to kew or carry
   the configured label. Otherwise, release the reservation and skip.
   Label-triggered items may remain unassigned until dispatch admission.
3. **Dispatch** through the existing detached sink
   (:mod:`kew.graph.driver.dispatch`) tagged ``run_kind="delegation"`` — the
   same budget/breaker/containment path an epic node rides, so delegation
   spend buckets under :data:`kew.budget.DELEGATION_RUN_KIND`. The child claims
   Linear only after all admission gates and the pending-row transaction commit.

On a budget/preflight refusal the seam writes the terminal refusal row (#898,
via :func:`kew.dispatch_core._record_budget_refusal_row` — reused, not
duplicated) AND posts a refusal comment on the Linear issue so the human sees
exactly why kew declined and which knob to raise — but only once per refusal
*episode* (Task 1, #971), so a persistently over-budget item is not re-commented
on every cooldown expiry. Post-merge close is already handled by ``tracker.close``
in ``graph/driver/merge.py`` — not our job here.

Config is always resolved through the one shared resolver,
``load_config(store=store)`` (precedence env > DB override > kew.toml >
default), via :func:`kew.budget.delegation_budget_preflight` — D1's parity test
guards this seam against ever reading file-only config.
"""

from __future__ import annotations

import enum
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from kew.budget import BudgetExceeded, delegation_budget_preflight
from kew.claim_retention import orphaned_claim
from kew.config import load_config
from kew.delegate.recognize import CandidateIssue, is_delegated_and_actionable
from kew.dispatch_core import _emit_early_exit, _record_budget_refusal_row
from kew.graph.driver.dispatch import DispatchSink, dispatch_reserved_child

if TYPE_CHECKING:
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

# All hand-assigned delegation work shares one ephemeral epic ref so the
# fleet-cap accounting (`live_reservation_counts_by_epic`, #864) counts it as a
# single epic's footprint alongside real epics — delegated and epic work share
# one ceiling (D5). Sourced from the shared pseudo-epic registry (#979-F1) so the
# driver reconcile and `kew doctor` sweep every pseudo-epic from one list;
# re-exported here for the many existing importers of this name.
from kew.pseudo_epics import DELEGATION_EPIC_REF  # noqa: E402

# Linear items carry a durable string id, not a GitHub issue number; the
# reservation stores ``issue_num=NULL`` for them (an auto-rowid, distinct per
# row). Budget preflight keys per-issue spend on the durable ``item_id``, so the
# numeric issue_num is the harmless ``0`` sentinel the Linear dispatch path
# already uses (see ``LinearTracker.fetch_issue``).
_LINEAR_ISSUE_SENTINEL = 0

# Legacy rollback markers are drained without mutating Linear.
_STRANDED_ROLLBACK_META_KEY = "delegation_stranded_rollbacks"


class ClaimOutcome(enum.Enum):
    """The terminal decision for one claim attempt."""

    #: Reserved, re-verified, and handed to the dispatch sink.
    DISPATCHED = "dispatched"
    #: Lost the reservation race — a clean no-op (the double-dispatch guard).
    SKIPPED_LOST_CLAIM = "skipped_lost_claim"
    #: Won the reservation but the live assignee is no longer the kew member.
    SKIPPED_REASSIGNED = "skipped_reassigned"
    #: Won the reservation, but live F1 facts now assign the item to an epic.
    SKIPPED_EPIC_ADMITTED = "skipped_epic_admitted"
    #: Budget/preflight refused: a terminal refusal row + comment were written.
    REFUSED = "refused"
    #: The shared fleet cap was already full at the atomic reserve — retried next
    #: tick (no reservation, no tracker I/O). The authoritative cap gate (N10).
    QUEUED_FLEET_FULL = "queued_fleet_full"


class DelegationTracker(Protocol):
    """The tracker operations the claim seam depends on.

    Kept as a Protocol so the seam is unit-testable with a fake and never
    imports a concrete Linear client — the CLI (D5) supplies the live adapter.
    """

    def fetch_live(self, item_id: str) -> CandidateIssue:
        """Fetch the issue's LIVE assignee/labels/state (never a snapshot)."""
        ...

    def transition_in_progress(self, item_id: str) -> None:
        """Move the issue into the started (In Progress) state."""
        ...

    def comment(self, item_id: str, body: str) -> None:
        """Post ``body`` as a comment on the issue."""
        ...


def _best_effort(action: Callable[[], None], what: str, item_id: str) -> None:
    """Run a terminal-cleanup tracker write best-effort (N7/N3).

    A refusal is terminal for this attempt, so a failing cleanup write — most
    plausibly a rate-limited ``comment`` under the exact N3 conditions — must
    never propagate and leave the item stranded In Progress with its fleet slot
    pinned. Log and swallow so the
    reservation release and the REFUSED outcome always stand.
    """
    try:
        action()
    except Exception:
        _logger.warning(
            "delegation: %s failed for %s (ignored on terminal refusal)",
            what,
            item_id,
            exc_info=True,
        )


def _refusal_comment(item_id: str, exc: BudgetExceeded) -> str:
    """Human-facing refusal comment naming the cap that bound and the knob."""
    return (
        f"kew could not start delegation for **{item_id}**: budget pre-check "
        f"refused — spent ${exc.spent_usd:.2f} exceeds the {exc.which_limit} cap "
        f"of ${exc.limit_usd:.2f}. Raise `budget.{exc.which_limit}_usd` (kew.toml "
        "or the /config surface); the watcher retries after the refusal cooldown."
    )


def _is_new_refusal_episode(store: RunStore, item_id: str) -> bool:
    """True when this refusal opens a NEW refusal episode (Task 1, #971).

    An episode is a contiguous run of budget refusals for one item. We post the
    human-facing refusal comment once per episode: the FIRST refusal (the newest
    existing run row is not itself a budget refusal) comments; every subsequent
    refusal inside the same episode updates state silently. Without this, each
    ``refusal_cooldown_seconds`` expiry re-claims → refuses → comments, spamming ~288
    Linear comments/day per item that sits over budget. Checked BEFORE this
    attempt's refusal row is written, so the newest row still reflects the prior
    episode state.
    """
    from kew.dispatch_core import _BUDGET_REFUSAL_CATEGORY

    rows = store.list_by_item(item_id)
    if not rows:
        return True
    # list_by_item orders by started_at ASC, so the last row is the newest.
    return rows[-1].error_category != _BUDGET_REFUSAL_CATEGORY


def load_stranded_rollbacks(store: RunStore) -> list[str]:
    """Read legacy rollback requests for retirement without tracker writes."""
    raw = store.get_meta(_STRANDED_ROLLBACK_META_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _set_stranded_rollbacks(store: RunStore, items: list[str]) -> None:
    store.set_meta(_STRANDED_ROLLBACK_META_KEY, json.dumps(items))


def clear_stranded_rollback(store: RunStore, item_id: str) -> None:
    """Drop ``item_id`` after recording its retained claim."""
    items = load_stranded_rollbacks(store)
    if item_id in items:
        items.remove(item_id)
        _set_stranded_rollbacks(store, items)


def _handle_budget_refusal(
    store: RunStore,
    tracker: DelegationTracker,
    item_id: str,
    *,
    exc: BudgetExceeded,
    epic_ref: str,
    config_path: Path | None,
    run_id: str,
) -> None:
    """Publish the unclaimed refusal, release, and comment once per episode."""
    # Episode check runs BEFORE the refusal row is written (Task 1, #971), so the
    # newest run row still reflects the PRIOR episode state.
    new_episode = _is_new_refusal_episode(store, item_id)
    config = load_config(config_path=config_path, store=store)
    repo = config.project.repo or None
    record = _record_budget_refusal_row(
        store,
        config,
        epic_ref,
        _LINEAR_ISSUE_SENTINEL,
        item_id,
        expected_reservation_run_id=run_id,
    )
    # Governance parity (N6): mirror the epic path's terminal dispatch_completed
    # audit event so the tamper-evident ledger records declined delegated work
    # exactly as it records a declined epic node.
    _emit_early_exit(
        store,
        issue_num=_LINEAR_ISSUE_SENTINEL,
        item_id=item_id,
        complexity="",
        runner="",
        model="",
        exit_code=1,
        duration_seconds=0.0,
        error_category=(
            "reservation_replaced"
            if record.error_category == "reservation_replaced"
            else "budget_exceeded"
        ),
        repo=repo,
    )
    if record.error_category == "reservation_replaced":
        return
    comment_body = _refusal_comment(item_id, exc)
    store.release_epic_node_item(item_id, run_id=run_id)
    # Comment once per refusal episode (Task 1, #971): a repeat refusal in the
    # same episode updates state silently, so an over-budget item does not get a
    # fresh Linear comment on every cooldown expiry.
    if new_episode:
        _best_effort(
            lambda: tracker.comment(item_id, comment_body),
            "refusal comment",
            item_id,
        )


def claim_and_dispatch(
    item_id: str,
    *,
    member_id: str,
    label: str = "kew:ready",
    store: RunStore,
    tracker: DelegationTracker,
    sink: DispatchSink,
    config_path: Path | None = None,
    epic_ref: str = DELEGATION_EPIC_REF,
    fleet_cap: int = 0,
    new_run_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> ClaimOutcome:
    """Claim ``item_id`` atomically, re-verify live, then dispatch or refuse.

    Returns the terminal :class:`ClaimOutcome`. The reservation is the SOLE
    double-dispatch guard: a losing caller returns
    :attr:`ClaimOutcome.SKIPPED_LOST_CLAIM` without touching the tracker or
    sink. Any unexpected error after the reservation lands releases it before
    re-raising, so a crash never leaves the item dangling reserved.

    ``fleet_cap`` folds the shared ``[parallel] max_workers`` ceiling into the
    SAME transaction as the reservation insert (N10): the count and the slot
    claim are atomic, so a concurrent watcher+driver admit can never push the
    fleet past the cap. A full fleet returns
    :attr:`ClaimOutcome.QUEUED_FLEET_FULL` (no reservation, no tracker I/O). A
    cap of 0 disables the check.
    """
    run_id = new_run_id()
    reserved = store.reserve_epic_node_item_under_cap(
        epic_ref, item_id, run_id, fleet_cap
    )
    if reserved == "fleet_full":
        _logger.info(
            "delegation: fleet cap %d full — queuing %s (retried next tick)",
            fleet_cap,
            item_id,
        )
        return ClaimOutcome.QUEUED_FLEET_FULL
    if reserved == "lost_claim":
        # Another surface (poll + fast local read) already claimed this item.
        return ClaimOutcome.SKIPPED_LOST_CLAIM

    try:
        # Re-verify against LIVE state — the candidate snapshot may be ~20s
        # stale, so an issue reassigned away from kew after the snapshot must
        # not be dispatched. Exact id match only (never substring).
        live = tracker.fetch_live(item_id)
        if live.epic_admitted:
            # Recognition is repeated after the durable reservation. If the
            # item entered an admitted epic between the watcher's recognition
            # read and this claim-time read, the epic rail still wins.
            store.release_epic_node_item(item_id, run_id=run_id)
            return ClaimOutcome.SKIPPED_EPIC_ADMITTED
        if not (
            is_delegated_and_actionable(live, member_id, label)
            or is_recoverable_orphan(store, item_id, live, member_id, epic_ref)
        ):
            store.release_epic_node_item(item_id, run_id=run_id)
            return ClaimOutcome.SKIPPED_REASSIGNED

        # Cheap budget preflight through the SHARED resolver
        # (load_config(store=store)), exactly as an epic node — D1's parity
        # seam. A refusal writes the terminal refusal row (#898) and comments.
        try:
            delegation_budget_preflight(
                store,
                _LINEAR_ISSUE_SENTINEL,
                config_path=config_path,
                item_id=item_id,
            )
        except BudgetExceeded as exc:
            _handle_budget_refusal(
                store,
                tracker,
                item_id,
                exc=exc,
                epic_ref=epic_ref,
                config_path=config_path,
                run_id=run_id,
            )
            return ClaimOutcome.REFUSED

        # Hand off to the EXISTING detached dispatch sink tagged
        # run_kind="delegation". The child checks budget, worker cap, route,
        # runner, and containment, then commits durable admission before its
        # Linear claim. Spawning the dispatcher does not authorize a claim here.
        dispatch_reserved_child(sink, item_id, run_id)
        return ClaimOutcome.DISPATCHED
    except Exception:
        store.release_epic_node_item(item_id, run_id=run_id)
        raise


def is_recoverable_orphan(
    store: RunStore,
    item_id: str,
    live: CandidateIssue,
    member_id: str,
    epic_ref: str,
) -> bool:
    """Recover only an unfinished claim still assigned to this kew member."""
    if live.epic_admitted or live.assignee_id != member_id:
        return False
    marker = orphaned_claim(store, item_id, epic_ref)
    if marker is None:
        return False
    intended_state_id = (marker.payload or {}).get("stateId")
    if not intended_state_id or live.state_id != intended_state_id:
        _logger.warning(
            "delegation: orphaned Linear claim for %s from run %s needs manual "
            "recovery: live state %s (%s) differs from intended state %s (%s)",
            item_id,
            marker.run_id,
            live.state,
            live.state_id,
            (marker.payload or {}).get("stateName"),
            intended_state_id,
        )
        return False
    _logger.warning(
        "delegation: recovering orphaned Linear claim for %s from run %s",
        item_id,
        marker.run_id,
    )
    return True
