"""The dashboard aggregator's duck-typed store and its rows.

The needs-you and dashboard suites compose over these: a store exposing exactly
the readers ``assemble`` and the needs-you set call, answered as narrowly as
the real store answers them.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kew.run_store import EpicHold, OrphanPRRecord


@dataclass
class FakeRun:
    run_id: str
    issue_num: int
    status: str = "success"
    pr_state: str | None = None
    pr_url: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    estimated_cost_usd: float = 0.0
    title: str = ""
    model: str = ""
    item_id: str = ""
    # The columns the canonical work projection composes over (I19). The
    # console no longer reinterprets ``pr_state`` itself, so the fake has to
    # carry what a real run row carries.
    branch: str = ""
    runner_name: str = ""
    reported_model: str | None = None
    container_name: str | None = None
    attempt_number: int = 1
    error_category: str = ""
    # Which lane produced the row. The projection reads it to tell a run kew
    # is implementing from a governed review round kew is reading an artifact
    # with (``review_in_flight`` in kew/src/kew/service/work.py, #2279).
    run_kind: str = "dispatch"
    # WHERE the run ran (#2398). The route dimension reports the placement, so
    # the fake carries it: "local" and no instance is what every row read as
    # before a run could land on a VM.
    run_host: str = "local"
    run_host_instance: str | None = None


@dataclass
class FakeDriverState:
    epic_ref: str
    last_tick_started_at: str | None
    last_tick_ts: str | None = None
    interval_s: float | None = 60.0


@dataclass
class FakeEpicTick:
    snapshot: dict[str, Any]


class FakeStore:
    """Duck-typed store exposing exactly the readers ``assemble`` calls."""

    def __init__(
        self,
        *,
        audit_events: list[Any] | None = None,
        epic_holds: list[EpicHold] | None = None,
        orphan_prs: list[OrphanPRRecord] | None = None,
        syncable: list[FakeRun] | None = None,
        all_runs: list[FakeRun] | None = None,
        epic_refs: list[str] | None = None,
        driver_states: dict[str, FakeDriverState] | None = None,
        epic_ticks: dict[str, FakeEpicTick] | None = None,
        fleet_stopped: Any = None,
        pending: list[Any] | None = None,
        review_rounds: list[Any] | None = None,
    ) -> None:
        self._audit = audit_events or []
        self._epic_holds = epic_holds or []
        self._orphans = orphan_prs or []
        self._syncable = syncable or []
        self._all = all_runs or []
        self._refs = epic_refs or []
        self._driver_states = driver_states or {}
        self._epic_ticks = epic_ticks or {}
        self._fleet_stopped = fleet_stopped
        self._pending = pending or []
        self._review_rounds = review_rounds or []

    # open_approval_holds reads this newest-first with event_prefix="approval_"
    # and the archive reader (#2206) with an exact event= name, which this
    # honours so a fake cannot answer a narrower query with a wider list.
    def list_audit_events(
        self,
        *,
        limit=None,
        event_prefix=None,
        event=None,
        issue_nums=None,
        item_ids=None,
    ):
        rows = [ev for ev in self._audit if event is None or ev.event == event]
        if issue_nums is not None:
            rows = [ev for ev in rows if ev.issue_num in set(issue_nums)]
        if item_ids is not None:
            # The epic_merge scope (#2392), answered as narrowly as the real
            # store answers it so the fake cannot pass a wider list off as one.
            scope = {str(i) for i in item_ids}
            rows = [
                ev
                for ev in rows
                if str(getattr(ev, "item_id", "") or "") in scope
                or str(ev.issue_num) in scope
            ]
        return rows

    def list_all_epic_holds(self):
        return list(self._epic_holds)

    # The node join reads holds scoped to ONE epic (#2129 narrowing); the fake
    # answers that query narrowly so it cannot pass a wider list off as one.
    def list_epic_holds(self, epic_ref):
        return [h for h in self._epic_holds if h.epic_ref == epic_ref]

    def list_orphan_prs(self, include_acked: bool = False):
        return list(self._orphans)

    def list_syncable_runs(self, *, item_ids=None):
        scope = None if item_ids is None else {str(i) for i in item_ids}
        return [
            r
            for r in self._syncable
            if scope is None or (r.item_id or str(r.issue_num)) in scope
        ]

    # --- the canonical work projection reads these (I19) -------------------
    def read_snapshot(self):
        return nullcontext()

    def list_by_item(self, item_id: str):
        return [
            r
            for r in (*self._syncable, *self._all)
            if (r.item_id or str(r.issue_num)) == item_id
        ]

    def get_admission_identity(self, run_id: str):
        return None

    # The work projection reads the item's latest recorded review round to tell
    # a cycle kew still owes a round to from one awaiting a merge decision
    # (#2328). Newest-last here, as the real store's own id order serves it;
    # cycle scoping (the post-retry window) is the store's business, not a
    # fake's — a test that needs it seeds the round it means.
    def latest_review_round(self, item_id: str):
        rounds = [r for r in self._review_rounds if r.item_id == item_id]
        return rounds[-1] if rounds else None

    def list_all(self):
        return list(self._all)

    def list_epic_refs(self):
        return list(self._refs)

    def get_epic_driver_state(self, epic_ref):
        return self._driver_states.get(epic_ref)

    def latest_epic_tick(self, epic_ref):
        return self._epic_ticks.get(epic_ref)

    def get_fleet_stopped(self):
        return self._fleet_stopped

    def list_pending(self):
        return list(self._pending)

    # spend_windows duck-types on these two
    def list_since(self, since):
        return [r for r in self._all if r.finished_at and r.finished_at >= since]

    def list_interactive_usage_since(self, since):
        return []


@dataclass
class FakeAuditEvent:
    event: str
    issue_num: int | None
    payload: dict[str, Any]
    # The envelope fields the archive reader orders and attributes by (#2206).
    id: int = 0
    timestamp: str = ""
    actor_id: str | None = None
