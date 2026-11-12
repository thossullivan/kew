"""Retain Linear claims when automatic recovery cannot use a version precondition."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, RunStore

LINEAR_CLAIM_RECOVERY = (
    "The item stays assigned in its claim state. A replacement dispatch re-claims it. "
    "For manual recovery, unassign the item in Linear and move it back to its "
    "unstarted state."
)


def retained_claim_lines(store: RunStore, item_ids: Iterable[str]) -> list[str]:
    """Read each item's latest retention reason with current recovery guidance."""
    lines = []
    for item_id in sorted(item_ids):
        events = store.list_audit_events(
            limit=1, event="linear_claim_retained", item_id=item_id
        )
        if events:
            reason = (events[0].payload or {}).get("reason", "unspecified")
            # Older events advertised unavailable recovery commands. Preserve
            # their recorded reason, but never repeat obsolete instructions.
            lines.append(
                f"  retained claim: {item_id}: {reason}. {LINEAR_CLAIM_RECOVERY}"
            )
    return lines


def orphaned_claim(store: RunStore, item_id: str, epic_ref: str) -> AuditEvent | None:
    """Return orphan evidence until another claim completes or is retained."""
    markers = store.list_audit_events(
        limit=1, event="linear_claim_orphaned", item_id=item_id
    )
    if not markers:
        return None
    marker = markers[0]
    payload = marker.payload or {}
    if payload.get("epic_ref") != epic_ref:
        return None
    # A replacement may complete before reconciliation reaps the older owner.
    started_id = payload.get("started_event_id", marker.id)
    for event in ("linear_claim_completed", "linear_claim_retained"):
        settled = store.list_audit_events(limit=None, event=event, item_id=item_id)
        if any(_supersedes_orphan(entry, marker, started_id) for entry in settled):
            return None
    return marker


def _supersedes_orphan(event: AuditEvent, marker: AuditEvent, started_id: int) -> bool:
    """Ignore the orphan's own completion; accept replacement claim evidence."""
    if event.id <= started_id:
        return False
    return not (
        event.event == "linear_claim_completed"
        and event.run_id == marker.run_id
        and event.id <= (marker.payload or {}).get("completed_event_id", started_id)
    )


def require_claim_recovery(store: RunStore | None, run_id: str) -> None:
    """Keep unlaunched claims pending until their owner delivers recovery evidence."""
    if store is None:
        return
    started = store.list_audit_events(
        limit=1, event="linear_claim_started", run_id=run_id
    )
    if not started:
        return
    for event in (
        "linear_claim_launched",
        "linear_claim_retained",
        "linear_claim_orphaned",
    ):
        if store.list_audit_events(limit=1, event=event, run_id=run_id):
            return
    raise RuntimeError(f"Linear claim requires recovery before finalization: {run_id}")


def claim_recovery_pending(store: RunStore | None, run_id: str) -> bool:
    """Whether an unlaunched tracker claim still keeps this run's row pending."""
    try:
        require_claim_recovery(store, run_id)
    except RuntimeError:
        return True
    return False


@dataclass
class RefusalDelivery:
    """Retry refusal delivery; finish only after the callback confirms persistence."""

    callback: Callable[[str], None] | None
    handled: bool = False

    def __call__(self, reason: str) -> None:
        if self.handled:
            return
        if self.callback is not None:
            self._deliver(reason)
        self.handled = True

    def _deliver(self, reason: str) -> None:
        # Share the store's bounded persistence backoff, including its final try.
        from kew.run_store import _BUSY_BASE_DELAY, _BUSY_MAX_RETRIES

        assert self.callback is not None
        for attempt in range(_BUSY_MAX_RETRIES + 1):
            try:
                self.callback(reason)
                return
            except Exception:
                if attempt == _BUSY_MAX_RETRIES:
                    raise
                time.sleep(_BUSY_BASE_DELAY * (2**attempt))


def record_retained_claim(
    store: RunStore,
    item_id: str,
    *,
    reason: str,
    run_id: str | None = None,
    epic_ref: str | None = None,
    reservation_run_id: str | None = None,
    reservation_generation: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Record the original claim identity without reading or mutating Linear."""
    payload = {
        "reason": reason,
        "epic_ref": epic_ref,
        "reservation_run_id": reservation_run_id,
        "reservation_generation": reservation_generation,
        "snapshot": snapshot,
        "updatedAt": (snapshot or {}).get("updatedAt"),
        "stateId": ((snapshot or {}).get("state") or {}).get("id"),
        "assigneeId": ((snapshot or {}).get("assignee") or {}).get("id"),
        "recovery": LINEAR_CLAIM_RECOVERY,
    }
    append = store.append_audit_event
    if run_id is not None:
        append = partial(
            store.append_audit_event_once,
            dedup_key=f"linear_claim_retained:{run_id}:{item_id}",
        )
    event_id = append(
        timestamp=datetime.now(UTC).isoformat(),
        event="linear_claim_retained",
        actor_type="policy",
        actor_id="dispatch-admission",
        run_id=run_id,
        item_id=item_id,
        payload=payload,
    )
    events = store.list_audit_events(
        limit=1, event="linear_claim_retained", run_id=run_id, item_id=item_id
    )
    if not events or events[0].id != event_id:
        raise RuntimeError(f"Linear claim retention was not durable for {item_id}")
