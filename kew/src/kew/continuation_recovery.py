"""Shared authority for replacing an explicitly abandoned epic checkout."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kew import audit
from kew.publication import PublicationError
from kew.run_store import (
    PR_SUBJECT_RUN_KINDS,
    AuditEvent,
    ContinuationExpiry,
    ContinuationRecord,
    RunRecord,
    RunStore,
    continuation_checkout_matches,
)
from kew.run_store_work import TRACKER_WORK_RUN_KINDS
from kew.waits import (
    HOST_RESTART_REPEATED,
    HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
    HOST_RESTART_REPEATED_PRESERVED_REASON,
    HOST_RESTART_REPEATED_REASON,
    HOST_RESTART_REPEATED_REASONS,
    HOST_RESTART_REPEATED_TAKEN_UP_REASON,
)

_IMPLEMENTATION_RUN_KINDS = TRACKER_WORK_RUN_KINDS - PR_SUBJECT_RUN_KINDS


class CheckoutAdmissionRefusal(RuntimeError):
    """Checkout prerequisites refuse dispatch before setup or worker launch."""

    category = "checkout_admission_refused"


class CheckoutGenerationRefusal(CheckoutAdmissionRefusal):
    """Abandonment cannot bind a valid checkout generation.

    ``cause`` names what the evidence failed at, without the recovery verbs
    the full message carries: a caller whose own recovery verb differs (a
    loop cannot be abandoned as an epic node) reports the cause and names its
    own verb (governed review round 2, 2026-09-21).
    """

    cause = "checkout generation is unusable"

    def __init__(self, message: str, *, cause: str = "") -> None:
        super().__init__(message)
        if cause:
            self.cause = cause


class ActiveContinuationRefusal(CheckoutAdmissionRefusal):
    """A current continuation owns the requested checkout across all epics."""

    category = "active_continuation_refused"


class RetainedReplacementRefusal(CheckoutAdmissionRefusal):
    """A fresh dispatch would discard a retained replacement whose history is gone."""


def checkout_refusal_categories() -> frozenset[str]:
    """Every category a checkout refusal reports, read from the hierarchy.

    A caller that enumerates these strings by hand drifts: the checkout
    identity refusal carries its own category and was missing from the CLI
    guard, so a refused dispatch exited 0 and read as an empty queue
    (governed review round 4, 2026-09-21). Subclasses defined in modules the
    caller has not imported cannot refuse its dispatch either, so walking the
    loaded hierarchy names exactly the reachable set.
    """
    seen: set[type[CheckoutAdmissionRefusal]] = {CheckoutAdmissionRefusal}
    frontier = [CheckoutAdmissionRefusal]
    while frontier:
        for subclass in frontier.pop().__subclasses__():
            if subclass not in seen:
                seen.add(subclass)
                frontier.append(subclass)
    return frozenset(
        category
        for category in (getattr(cls, "category", "") for cls in seen)
        if category
    )


def _run_checkout_binding_matches(
    event: AuditEvent, run_id: str, branch: str, checkout: Path
) -> bool:
    """Match one run binding without reading agent-written Git data."""
    payload = event.payload or {}
    return (
        event.run_id == run_id
        and payload.get("branch") == branch
        and continuation_checkout_matches(payload.get("checkout", ""), checkout)
    )


def recorded_run_checkout_generation(
    store: RunStore, *, run_id: str, item_id: str, branch: str, checkout: Path
) -> str:
    """Read a checkout-mode producer's identity without adopting present bytes."""
    generations = {
        (event.payload or {}).get("checkout_id", "")
        for event in store.list_audit_events(
            limit=None, event="dispatch_checkout_bound", item_id=item_id
        )
        if _run_checkout_binding_matches(event, run_id, branch, checkout)
    }
    if not generations:
        return ""
    if len(generations) != 1:
        raise CheckoutGenerationRefusal("producer has conflicting checkout generations")
    generation = generations.pop()
    if not isinstance(generation, str):
        raise CheckoutGenerationRefusal(
            "producer has an unreadable checkout generation"
        )
    return generation


#: The two events a dispatch binds a checkout with — one per publication
#: mode, written by the same ``_bind_publication_base`` call — so a LAUNCH
#: records at most one of them whichever mode it ran under. A recovery that
#: binds a second checkout to the same run afterwards is the one exception
#: (see :func:`recorded_run_checkouts`).
_RUN_CHECKOUT_BINDING_EVENTS = ("dispatch_checkout_bound", "publication_base")


def recorded_run_checkout(
    store: RunStore, *, run_id: str, item_id: str, branch: str
) -> tuple[Path, str] | None:
    """The checkout a run bound at launch, and the generation it bound it at.

    The inverse of :func:`recorded_run_checkout_generation`, which can only
    confirm the identity of a path its caller already holds. A recovery that
    has to WRITE into a dead run's checkout cannot derive that path: a
    CONTAINED dispatch's checkout is the standalone clone
    ``dispatch_core._materialize_checkout`` makes under the system temp
    directory, not the deterministic ``<worktree_base>/agent/issue-<slug>``
    the host path uses. So the path is read back off the run's own durable
    launch evidence instead of recomputed from the item id.

    ``None`` when the run bound no checkout and when the evidence disagrees
    with itself — both mean "this run has no checkout a writer may touch" —
    and the generation is returned with it so the caller can require that
    exact directory before it writes a byte.
    """
    bindings: set[tuple[Any, Any]] = set()
    for event in _RUN_CHECKOUT_BINDING_EVENTS:
        bindings |= _run_checkout_bindings(
            store, run_id=run_id, item_id=item_id, branch=branch, event=event
        )
    if len(bindings) != 1:
        return None
    checkout, generation = bindings.pop()
    if not isinstance(checkout, str) or not isinstance(generation, str):
        return None
    return (Path(checkout), generation) if checkout and generation else None


def recorded_run_checkouts(
    store: RunStore, *, run_id: str, item_id: str, branch: str
) -> frozenset[str]:
    """Every checkout path a run's launch evidence names, as recorded.

    :func:`recorded_run_checkout` answers "which single checkout may a writer
    touch" and deliberately answers nothing once the evidence names more than
    one. A reader that only has to know WHETHER a given path is among them
    asks this instead, and a gateway-restart recovery is why: it merges a
    restarted sandbox into the host dispatch checkout and binds the run there
    as well as at the clone it launched from, so from then on the run names
    two — and the orphan path still has to recognize the host one as its own.

    Unvalidated, like the bindings it reads: a caller deciding "is this path
    one of mine" is not harmed by a second path it does not recognize, and
    the writer-facing accessor above is the one that must refuse ambiguity.
    """
    bindings: set[tuple[Any, Any]] = set()
    for event in _RUN_CHECKOUT_BINDING_EVENTS:
        bindings |= _run_checkout_bindings(
            store, run_id=run_id, item_id=item_id, branch=branch, event=event
        )
    return frozenset(
        checkout for checkout, _ in bindings if isinstance(checkout, str) and checkout
    )


def _run_checkout_bindings(
    store: RunStore, *, run_id: str, item_id: str, branch: str, event: str
) -> set[tuple[Any, Any]]:
    """Every (checkout, generation) pair one event binds to this run's branch.

    Unvalidated on purpose: the caller decides what a disagreement means, and
    it cannot tell one apart from a single binding unless every recorded pair
    reaches it.
    """
    found: set[tuple[Any, Any]] = set()
    for recorded in store.list_audit_events(limit=None, event=event, item_id=item_id):
        payload = recorded.payload or {}
        if recorded.run_id == run_id and payload.get("branch") == branch:
            found.add((payload.get("checkout"), payload.get("checkout_id")))
    return found


def record_run_checkout_generation(
    store: RunStore,
    *,
    run_id: str,
    item_id: str,
    branch: str,
    checkout: Path,
    generation: str,
) -> None:
    """Persist a checkout-mode run binding before launch or continuation creation."""
    recorded = recorded_run_checkout_generation(
        store, run_id=run_id, item_id=item_id, branch=branch, checkout=checkout
    )
    if recorded and recorded != generation:
        raise CheckoutGenerationRefusal(
            "checkout identity changed from the recorded generation"
        )
    if recorded:
        return
    if not generation:
        raise CheckoutGenerationRefusal("producer has no checkout generation to bind")
    inserted = store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="dispatch_checkout_bound",
        run_id=run_id,
        item_id=item_id,
        actor_type="policy",
        actor_id="dispatch-checkout",
        payload={
            "branch": branch,
            "checkout": str(checkout.resolve()),
            "checkout_id": generation,
        },
    )
    if inserted is None:
        raise CheckoutGenerationRefusal(
            "dispatch checkout binding was not durably recorded"
        )


def require_unchanged_checkout_binding(
    store: RunStore,
    *,
    event: str,
    run_id: str,
    branch: str,
    checkout: Path,
    generation: str,
) -> None:
    """A later write cannot change a run's immutable directory evidence."""
    for recorded in store.list_audit_events(limit=None, event=event):
        if not _run_checkout_binding_matches(recorded, run_id, branch, checkout):
            continue
        if (recorded.payload or {}).get("checkout_id") != generation:
            recovery = (
                "start a fresh loop with kew dispatch <issue> --loop"
                if event.startswith("loop_")
                else "use kew epic abandon"
            )
            raise CheckoutGenerationRefusal(
                f"recorded checkout binding cannot change; {recovery}"
            )


def recorded_continuation_generation(
    store: RunStore, record: ContinuationRecord
) -> str:
    """Require the immutable checkout identity recorded at registration."""
    generation = store.continuation_checkout_generation(record)
    if not generation:
        raise CheckoutGenerationRefusal(
            f"{record.item_id}: continuation has no recorded checkout generation; "
            f"use kew epic abandon {record.epic_ref} {record.item_id}"
        )
    return generation


def require_continuation_generation(
    store: RunStore,
    record: ContinuationRecord,
    *,
    validate_generation: Callable[[str], None],
) -> str:
    """Refuse replacement bytes before any Git inspection or launch."""
    expected = recorded_continuation_generation(store, record)
    try:
        validate_generation(expected)
    except CheckoutGenerationRefusal as exc:
        raise CheckoutGenerationRefusal(
            f"{record.item_id}: checkout identity changed; {exc}; use kew epic abandon "
            f"{record.epic_ref} {record.item_id}"
        ) from exc
    return expected


def current_continuation(
    store: RunStore, record: ContinuationRecord, item_id: str
) -> bool:
    """Retain abandonment until a fresh implementation materializes a checkout."""
    if record.item_id != item_id:
        return False
    if record.terminal_at is None:
        return True
    authority = store.continuation_abandonment_identity(record)
    if authority is None:
        return not store.continuation_is_complete(record)
    if store.continuation_expiry(record) is not None:
        # Withdrawal removes the unused replacement, never the consumed decision.
        return False
    # Discover legacy producers from retained events, even after run retention.
    # New setup persists expiry before launch and never needs this backfill.
    for event in store.continuation_replacement_events(record):
        if not _legacy_replacement_event_matches(record, authority, event):
            continue
        assert event.run_id is not None
        run = store.get(event.run_id)
        if run is None or _replacement_run_is_eligible(store, record, run):
            _persist_replacement(store, record, authority, event)
            return False
    return True


def _legacy_replacement_event_matches(
    record: ContinuationRecord, authority: str, event: AuditEvent
) -> bool:
    """Rebind events need explicit expiry; interrupted setup retains authority."""
    return (
        event.payload is not None
        and "rebound_from" not in event.payload
        and _replacement_event_matches(record, authority, event)
    )


def _replacement_run_is_eligible(
    store: RunStore, record: ContinuationRecord, run: RunRecord
) -> bool:
    """Retain replacement evidence when execution ends with publication refusal."""
    return not (
        record.terminal_at is None
        or run.started_at <= record.terminal_at
        or run.item_id != record.item_id
        or run.run_kind not in _IMPLEMENTATION_RUN_KINDS
        or run.branch != record.branch
        or (run.status == "refused" and not _has_execution_evidence(store, run))
        or run.error_category == "dispatch_child_died"
    )


def _has_checkout_replacement(
    store: RunStore, record: ContinuationRecord, run_id: str
) -> bool:
    """Match replacement to the abandoned path, generation, and authority."""
    assert record.terminal_at is not None
    authority = store.continuation_abandonment_identity(record)
    if authority is None:
        return False
    for event in store.list_audit_events(
        limit=None, event="checkout_replaced", run_id=run_id
    ):
        if _replacement_event_matches(record, authority, event):
            _persist_replacement(store, record, authority, event)
            return True
    return False


def _replacement_event_matches(
    record: ContinuationRecord, authority: str, event: AuditEvent
) -> bool:
    assert record.terminal_at is not None
    return (
        bool(event.run_id)
        and event.timestamp > record.terminal_at.isoformat()
        and isinstance(event.payload, dict)
        and _replacement_payload_matches(record, authority, event.payload)
    )


def _persist_replacement(
    store: RunStore, record: ContinuationRecord, authority: str, event: AuditEvent
) -> None:
    assert event.run_id is not None and event.payload is not None
    expiry = ContinuationExpiry(
        authority,
        record.generation,
        event.payload["checkout_id"],
        event.run_id,
        event.timestamp,
    )
    store.record_continuation_expiry(record, expiry)
    if store.continuation_expiry(record) != expiry:
        raise RuntimeError(
            f"checkout replacement expiry for run {event.run_id!r} "
            "was not durably recorded"
        )


def _replacement_payload_matches(
    record: ContinuationRecord, authority: str, payload: dict[str, Any]
) -> bool:
    expected = {
        "epic_ref": record.epic_ref,
        "item_id": record.item_id,
        "original_run_id": record.original_run_id,
        "branch": record.branch,
        "generation": record.generation,
        "abandonment_identity": authority,
    }
    return (
        type(payload.get("generation")) is int
        and isinstance(payload.get("checkout_id"), str)
        and bool(payload["checkout_id"])
        and isinstance(payload.get("checkout"), str)
        and _replacement_checkout_matches(record, payload)
        and all(payload.get(key) == value for key, value in expected.items())
    )


def _replacement_checkout_matches(
    record: ContinuationRecord, payload: dict[str, Any]
) -> bool:
    """Keep explicit rebind evidence attached to its immutable original family."""
    if "rebound_from" in payload or "rebound_reason" in payload:
        return (
            payload.get("rebound_from") == record.worktree_identity
            and payload.get("rebound_reason") == "recorded checkout missing"
        )
    return continuation_checkout_matches(record.worktree_identity, payload["checkout"])


def expire_checkout_abandonment(
    store: RunStore, *, epic_ref: str, item_id: str, run_id: str
) -> None:
    """Validate and persist setup's replacement before the worker can write."""
    for record in store.list_continuations(epic_ref):
        if (
            record.item_id == item_id
            and store.continuation_was_abandoned(record)
            and _has_checkout_replacement(store, record, run_id)
        ):
            return
    raise RuntimeError(
        f"checkout replacement for run {run_id!r} has no validated abandonment expiry"
    )


def require_retained_replacement(
    store: RunStore, *, epic_ref: str, item_id: str
) -> None:
    """Require cleared holds, withdrawn refusals, and retained producer history."""
    records = store.list_continuations(epic_ref)
    if any(
        record.item_id == item_id and store.continuation_was_abandoned(record)
        for record in records
    ):
        _require_cleared_holds(store, epic_ref, item_id)
    registered = {record.original_run_id for record in records}
    for record in records:
        if record.item_id != item_id:
            continue
        current_continuation(store, record, item_id)
        _require_replacement_history(store, record, registered)


def _require_replacement_history(
    store: RunStore, record: ContinuationRecord, registered: set[str]
) -> None:
    expiry = store.continuation_expiry(record)
    if expiry is None or replacement_was_withdrawn(store, record):
        return
    run = store.get(expiry.replacing_run_id)
    if (
        run is not None
        and run.status == "refused"
        and not _has_execution_evidence(store, run)
    ):
        raise RetainedReplacementRefusal(
            f"{record.epic_ref} item {record.item_id} preserves "
            f"replacement run {run.run_id!r}; "
            "pre-launch refusal has no matching checkout withdrawal; "
            "fresh dispatch is refused"
        )
    if expiry.replacing_run_id not in registered and run is None:
        raise RetainedReplacementRefusal(
            f"{record.item_id} preserves a replacement checkout from "
            f"replacement run {expiry.replacing_run_id!r}; its run history "
            "is unavailable after retention; fresh dispatch is refused"
        )


def _require_cleared_holds(store: RunStore, epic_ref: str, item_id: str) -> None:
    for hold in store.list_epic_holds(epic_ref):
        if hold.item_id == item_id:
            raise RetainedReplacementRefusal(
                f"{epic_ref} item {item_id} has hold {hold.reason!r}; "
                "fresh dispatch is refused"
            )


def replacement_was_withdrawn(store: RunStore, record: ContinuationRecord) -> bool:
    """Require exact cleanup evidence; never revive the consumed abandonment."""
    expiry = store.continuation_expiry(record)
    if expiry is None:
        return False
    return any(
        _withdrawal_event_matches(record, expiry, event)
        for event in store.list_audit_events(
            limit=None,
            event="checkout_replacement_withdrawn",
            run_id=expiry.replacing_run_id,
        )
    )


def selectable_continuation(
    store: RunStore, records: list[ContinuationRecord]
) -> ContinuationRecord | None:
    """The one continuation ``kew epic continue`` would claim from *records*.

    *records* are one item's unabandoned rows. None while one is owned, or
    unless exactly one is recoverable and exactly one is incomplete: anything
    else is an ambiguous history the selector refuses, so no surface may offer
    a command that resolves it (#2960).
    """
    if any(record.state == "continuing" for record in records):
        return None
    awaiting = [r for r in records if store.continuation_is_recoverable(r)]
    incomplete = [r for r in records if not store.continuation_is_complete(r)]
    if len(awaiting) == 1 and len(incomplete) == 1:
        return awaiting[0]
    return None


@dataclass(frozen=True)
class ContinuationHistory:
    """One item's unabandoned continuation rows, as the continue verb reads them.

    ``selected`` is the row ``kew epic continue`` claims
    (:func:`selectable_continuation`), or None. ``preserved`` are the rows a
    retry would dispatch past: recoverable ones that still hold the node's
    work. A history with preserved rows and nothing selected, while nobody
    owns a continuation, is ambiguous: continue and abandon both refuse it,
    so no surface may offer either (#2960).
    """

    rows: tuple[ContinuationRecord, ...]
    preserved: tuple[ContinuationRecord, ...]
    selected: ContinuationRecord | None

    @property
    def owned(self) -> bool:
        return any(row.state == "continuing" for row in self.rows)

    @property
    def ambiguous(self) -> bool:
        return bool(self.preserved) and self.selected is None and not self.owned

    @property
    def terminal(self) -> tuple[ContinuationRecord, ...]:
        """The preserved rows a continuation left terminal and incomplete."""
        return tuple(row for row in self.preserved if row.state == "terminal")


def continuation_histories(
    store: RunStore, epic_ref: str
) -> dict[str, ContinuationHistory]:
    """Per item of *epic_ref* with an unabandoned continuation row, its history.

    Selection uses the same rule as the continue verb
    (:func:`selectable_continuation`), over the same unabandoned rows.
    """
    by_item: dict[str, list[ContinuationRecord]] = {}
    for record in store.list_continuations(epic_ref):
        if not store.continuation_was_abandoned(
            record
        ) and not _closed_by_clean_host_death(store, record):
            by_item.setdefault(record.item_id, []).append(record)
    return {
        item_id: ContinuationHistory(
            rows=tuple(records),
            preserved=tuple(r for r in records if store.continuation_is_recoverable(r)),
            selected=selectable_continuation(store, records),
        )
        for item_id, records in by_item.items()
    }


#: The actor id of the driver's own continuation closes: the one that closes a
#: continuation the host died under twice over a clean checkout (#2964) is
#: the evidence :func:`_closed_by_clean_host_death` reads.
CLEAN_HOST_DEATH_CLOSE_ACTOR = "driver_reconcile"


def _closed_by_clean_host_death(store: RunStore, record: ContinuationRecord) -> bool:
    """Did the driver close *record* because the host died twice under its
    continuation over a clean checkout (#2964)?

    Such a row holds no bytes and its node is retried, so it is read like an
    abandoned row and preserves nothing (#2960). The proof is the close the
    driver itself records, not the run's category, which the tick's own
    terminal attestation repeats for every terminal row.
    """
    if record.state != "terminal" or record.continuation_run_id is None:
        return False
    return any(
        event.actor_id == CLEAN_HOST_DEATH_CLOSE_ACTOR
        and (event.payload or {}).get("state") == "terminal"
        and (event.payload or {}).get("outcome") == HOST_RESTART_REPEATED
        and (event.payload or {}).get("original_run_id") == record.original_run_id
        for event in store.list_audit_events(
            limit=None,
            event="continuation_terminal",
            run_id=record.continuation_run_id,
        )
    )


def preserved_host_death_reason(
    reason: str, history: ContinuationHistory | None
) -> str:
    """*reason*, read against the item's CURRENT continuation history (#2960).

    A node the host died under twice reads :data:`HOST_RESTART_REPEATED_REASON`,
    whose command is retry. While the node still keeps a recoverable preserved
    continuation, retry would dispatch past those bytes, so the reason becomes
    the preserved one (command: continue) whenever the continue selector picks
    a row — terminal, or awaiting again after a continue whose dispatch exited
    nonzero or raised — or, when the selector would refuse the history, the
    ambiguous one (no command).

    Every variant is read again, not only the plain one, because the history
    moves without a tick: a continuation that owns the work, or preserved work
    abandon re-admitted or a continuation completed, reads
    :data:`HOST_RESTART_REPEATED_TAKEN_UP_REASON` — nobody's move, no command.
    A plain reason with no preserved work in its history stays retry. Every
    other reason is returned as is.

    The driver applies it when it writes the reason; a journal written before
    the history changed is read through it again, because a stalled epic may
    never write another tick.
    """
    if reason not in HOST_RESTART_REPEATED_REASONS:
        return reason
    if history is not None and history.owned:
        return HOST_RESTART_REPEATED_TAKEN_UP_REASON
    if history is not None and history.selected is not None:
        return HOST_RESTART_REPEATED_PRESERVED_REASON
    if history is not None and history.preserved:
        return HOST_RESTART_REPEATED_AMBIGUOUS_REASON
    if reason == HOST_RESTART_REPEATED_REASON:
        return reason
    return HOST_RESTART_REPEATED_TAKEN_UP_REASON


def _withdrawal_event_matches(
    record: ContinuationRecord, expiry: ContinuationExpiry, event: AuditEvent
) -> bool:
    if not _replacement_event_matches(record, expiry.abandonment_identity, event):
        return False
    assert event.payload is not None
    return (
        event.actor_type == "policy"
        and event.actor_id == "dispatch"
        and event.timestamp >= expiry.replaced_at
        and event.payload.get("checkout_id") == expiry.replacing_checkout_id
        and event.payload.get("checkout_removed") is True
        and event.payload.get("worker_launched") is False
        and isinstance(event.payload.get("reason"), str)
        and bool(event.payload["reason"].strip())
    )


def _has_execution_evidence(store: RunStore, run: RunRecord) -> bool:
    """A final refusal label does not erase retained worker execution evidence."""
    if any(
        (
            run.pgid,
            run.attempt_pgid,
            run.container_name,
            run.session_id,
            run.duration_seconds,
            run.total_tokens,
            run.input_tokens,
            run.output_tokens,
            run.cache_creation_tokens,
            run.cache_read_tokens,
            run.estimated_cost_usd,
        )
    ):
        return True
    return bool(store.list_route_profiles(run.run_id)) or any(
        store.list_audit_events(limit=1, event=event, run_id=run.run_id)
        for event in ("dispatch_started", "publication_intent")
    )


@dataclass(frozen=True)
class CheckoutAbandonment:
    """An immutable abandonment event scoped to one checkout and branch."""

    evidence_identity: str
    checkout: Path
    branch: str
    original_run_id: str
    generation: int
    epic_ref: str
    item_id: str
    checkout_id: str = ""
    rebound_from: str = ""
    legacy_sidecar_retired: bool = False

    @property
    def rebound_evidence(self) -> dict[str, str]:
        """Identify the original family without rewriting its recorded path."""
        if not self.rebound_from:
            return {}
        return {
            "rebound_from": self.rebound_from,
            "rebound_reason": "recorded checkout missing",
        }


def checkout_abandonment(
    store: RunStore,
    *,
    epic_ref: str | None,
    item_id: str,
    branch: str,
    checkout: Path,
    checkout_id: str = "",
    capture_generation: Callable[[], str] | None = None,
    retire_legacy_generation: Callable[[], bool] | None = None,
) -> CheckoutAbandonment | None:
    """Resolve current abandonment authority under the checkout guard."""
    records = checkout_admission_records(
        store, checkout, item_id=item_id, branch=branch
    )
    if not records:
        return None
    if len(records) != 1:
        raise RuntimeError("ambiguous continuation authority for checkout replacement")
    record = records[0]
    identity = store.continuation_abandonment_identity(record)
    assert identity is not None
    if (
        record.item_id != item_id
        or record.branch != branch
        or not continuation_checkout_matches(
            record.worktree_identity,
            checkout,
            recorded_owner=(record.item_id, record.branch),
            candidate_owner=(item_id, branch),
        )
    ):
        raise RuntimeError(
            "abandonment authority does not match the replacement checkout"
        )
    rebound_from = _missing_recorded_checkout(record)
    legacy_retired = (
        retire_legacy_generation() if retire_legacy_generation is not None else False
    )
    token = capture_generation() if capture_generation is not None else checkout_id
    if not _valid_abandonment_generation(token, checkout, rebound_from):
        raise CheckoutGenerationRefusal(
            "abandonment requires a valid checkout generation"
        )
    return CheckoutAbandonment(
        identity,
        checkout.resolve(),
        branch,
        record.original_run_id,
        record.generation,
        record.epic_ref,
        record.item_id,
        token,
        rebound_from,
        legacy_retired,
    )


def _valid_abandonment_generation(
    generation: str, checkout: Path, rebound_from: str
) -> bool:
    """Require complete captured evidence, or an authorized absent candidate."""
    if not generation:
        return bool(rebound_from) and _checkout_is_missing(checkout)
    try:
        evidence = json.loads(generation)
    except ValueError as exc:
        raise CheckoutGenerationRefusal(
            "abandonment requires a valid checkout generation"
        ) from exc
    return (
        isinstance(evidence, dict)
        and {"token", "nonce", "device", "inode", "birth_field"} <= evidence.keys()
    )


def _missing_recorded_checkout(record: ContinuationRecord) -> str:
    """Only an absent recorded path permits a scoped abandonment rebind."""
    return (
        record.worktree_identity
        if _checkout_is_missing(Path(record.worktree_identity))
        else ""
    )


def _checkout_is_missing(checkout: Path) -> bool:
    """Distinguish an absent checkout from an unreadable checkout."""
    try:
        checkout.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise CheckoutGenerationRefusal(
            "recorded checkout is unverifiable; fresh dispatch is refused"
        ) from exc
    return False


def checkout_admission_records(
    store: RunStore, checkout: Path, *, item_id: str, branch: str
) -> list[ContinuationRecord]:
    """Refuse current owners and retained replacement obligations across epics."""
    checkout_records = [
        record
        for record in store.continuations_for_checkout(
            checkout, item_id=item_id, branch=branch
        )
        if not store.continuation_is_complete(record)
    ]
    records = [
        record
        for record in checkout_records
        if current_continuation(store, record, record.item_id)
    ]
    for record in records:
        _require_abandoned_continuation(store, record)
    for owner_epic, owner_item in {
        (record.epic_ref, record.item_id) for record in checkout_records
    }:
        require_retained_replacement(store, epic_ref=owner_epic, item_id=owner_item)
    return records


def _require_abandoned_continuation(
    store: RunStore, record: ContinuationRecord
) -> None:
    """Name the operator recovery verbs before setup can change an owned checkout."""
    if store.continuation_abandonment_identity(record) is None:
        raise ActiveContinuationRefusal(
            f"{record.epic_ref} item {record.item_id} owns an active continuation; "
            f"recorded checkout {record.worktree_identity!r}; "
            "unverifiable checkout ownership refuses fresh dispatch; "
            "use `kew epic continue` or `kew epic abandon` before fresh dispatch"
        )


def record_checkout_replacement(
    store: RunStore | None,
    checkout: Path,
    *,
    abandonment: CheckoutAbandonment | None,
    run_id: str | None,
    issue_num: int,
    item_id: str,
    repo: str | None,
    epic_ref: str | None,
    branch: str,
    generation: str,
) -> None:
    """Attest fresh checkout creation before an epic worker can write to it."""
    # Continuations retain their original binding and never reach this writer.
    payload: dict[str, Any] = {
        "epic_ref": epic_ref,
        "item_id": item_id,
        "branch": branch,
        "checkout": str(checkout.expanduser().resolve()),
        "checkout_id": generation,
    }
    if abandonment is not None:
        payload["epic_ref"] = abandonment.epic_ref
        payload["item_id"] = abandonment.item_id
        payload["replacing_epic_ref"] = epic_ref
        payload["abandonment_identity"] = abandonment.evidence_identity
        payload["original_run_id"] = abandonment.original_run_id
        payload["generation"] = abandonment.generation
        payload["abandoned_checkout_id"] = abandonment.checkout_id
        payload["legacy_sidecar_retired"] = abandonment.legacy_sidecar_retired
        payload.update(abandonment.rebound_evidence)
    audit.emit(
        store,
        "checkout_replaced",
        actor=audit.Actor("policy", "dispatch"),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload=payload,
    )
    if store is None or (not epic_ref and abandonment is None):
        return
    # Losing this event must not lend the old abandonment to new worker edits.
    if not any(
        event.payload == payload
        for event in store.list_audit_events(
            limit=None, event="checkout_replaced", run_id=run_id
        )
    ):
        raise PublicationError(
            "unknown",
            f"checkout replacement for {checkout} was not durably recorded",
            host_paths=(checkout,),
        )

    if abandonment is not None and run_id is not None:
        expire_checkout_abandonment(
            store,
            epic_ref=abandonment.epic_ref,
            item_id=abandonment.item_id,
            run_id=run_id,
        )


def record_checkout_withdrawal(
    store: RunStore | None,
    checkout: Path,
    *,
    abandonment: CheckoutAbandonment | None,
    run_id: str | None,
    issue_num: int,
    item_id: str,
    repo: str | None,
    reason: str,
) -> None:
    """Attest removal only for this dispatch's durably bound replacement."""
    authority = abandonment
    if store is None or authority is None or run_id is None:
        return
    record = store.get_continuation(
        authority.epic_ref,
        authority.item_id,
        authority.original_run_id,
        authority.branch,
        authority.rebound_from or authority.checkout,
    )
    if record is None:
        return
    expiry = store.continuation_expiry(record)
    if expiry is None or expiry.replacing_run_id != run_id:
        return
    audit.emit(
        store,
        "checkout_replacement_withdrawn",
        actor=audit.Actor("policy", "dispatch"),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload={
            "epic_ref": record.epic_ref,
            "item_id": record.item_id,
            "original_run_id": record.original_run_id,
            "branch": record.branch,
            "checkout": str(checkout.expanduser().resolve()),
            "checkout_id": expiry.replacing_checkout_id,
            "generation": expiry.abandoned_generation,
            "abandonment_identity": expiry.abandonment_identity,
            "reason": reason,
            "checkout_removed": True,
            "worker_launched": False,
            **authority.rebound_evidence,
        },
    )
