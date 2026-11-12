"""Reactive data store for the TUI — polls SQLite + tails event files.

What a work item *is* comes from :mod:`kew.service.work`, never from a
``RunRecord.status`` this module reinterprets: a run row knows its own exit
code and nothing about publication, judgment, source synchronization or review
capacity, so every terminal surface that read it was guessing the rest. The
projection is retained here with its revision, and :func:`work_display` is the
ONE mapping from that projection to a terminal row — no screen keeps a second.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew.config import DispatchConfig
from kew.models import GhIssue, GhPullRequest
from kew.run_store import EpicHold, OrphanPRRecord, RunRecord, RunStore
from kew.runners.registry import resolve_runner
from kew.selector import (
    get_complexity,
    list_queue_issues,
    list_review_prs,
)
from kew.service.models import WorkProjectionDTO, WorkViewDTO
from kew.service.work import (
    cached_pull_request,
    compose_work_projection,
    pending_run_max_age_s,
)
from kew.tui.palette import ATTESTED, FAINT, JUDGMENT, MACHINE, WARN


def _build_token_buckets(
    all_runs: list[RunRecord],
) -> dict[str, list[int]]:
    """Build token history by complexity for progress bar estimation."""
    completed = [r for r in all_runs if r.status == "success" and r.total_tokens > 0]
    buckets: dict[str, list[int]] = {}
    for r in completed:
        bucket = r.complexity or "all"
        buckets.setdefault(bucket, []).append(r.total_tokens)
        if bucket != "all":
            buckets.setdefault("all", []).append(r.total_tokens)
    return buckets


# --- the ONE canonical-projection -> terminal mapping (I22) -----------------
#
# ``_BADGE_BY_OUTCOME`` is a RENDERING table, not a state machine: it turns an
# already-derived :class:`WorkOutcomeDTO` state into a five-column glyph. No
# ``RunStatus`` appears in it, and no screen may keep another one — what the
# work *is* was decided once, in :mod:`kew.service.work`.

_BADGE_BY_OUTCOME: dict[str, tuple[str, str]] = {
    "delivered": ("OK", ATTESTED),
    "failed": ("ERR", JUDGMENT),
    "abandoned": ("DECL", JUDGMENT),
    "unknown": ("??", FAINT),
}

#: What a row shows when there is no canonical view for it — deliberately
#: neither "OK" nor "ERR": nothing was read, so nothing may be claimed.
UNREAD_BADGE = "--"


@dataclass(frozen=True)
class WorkDisplay:
    """One work item as a terminal row: badge, canonical words, evidence.

    Every field is *read* from the projection rather than derived here. The
    revision travels with it so a surface can say which facts it is showing —
    and, in I23, decide an action against exactly those facts.
    """

    badge: str
    color: str
    ref: str
    exception: str
    detail: str
    action: str
    #: Worst availability across the view's remote readings: an unread remote
    #: is ``unavailable``, never a successful empty.
    evidence: str
    evidence_detail: str
    revision: str
    read: bool

    @property
    def markup(self) -> str:
        """The badge, painted — the only place a badge gets its colour."""
        return f"[{self.color}]{self.badge}[/{self.color}]"


def _badge(view: WorkViewDTO) -> tuple[str, str]:
    """Badge + colour from the canonical dimensions, in that order of truth."""
    if view.execution.state == "in_flight":
        return ("LIVE", MACHINE)
    if view.outcome.state == "in_progress":
        # Preserved-but-unpublished work lands here, which is why it can never
        # read as ERR: the implementation exists and is waiting on a step.
        return ("HOLD", JUDGMENT) if view.exception.blocking else ("WAIT", WARN)
    return _BADGE_BY_OUTCOME.get(view.outcome.state, ("??", FAINT))


def _evidence(view: WorkViewDTO) -> tuple[str, str]:
    """Compact availability: the worst reading behind this view, and which."""
    gaps = [o for o in view.observations if o.availability != "available"]
    if not gaps:
        return "current", ""
    worst = (
        "unavailable" if any(o.availability == "unavailable" for o in gaps) else "stale"
    )
    return worst, ", ".join(f"{o.dimension}:{o.availability}" for o in gaps)


def work_display(view: WorkViewDTO | None, *, ref: str = "") -> WorkDisplay:
    """Render one canonical work view for a terminal row.

    ``view`` is None when the projection could not be composed at all. That
    row says so — an unread item is not an item with nothing wrong.
    """
    if view is None:
        return WorkDisplay(
            badge=UNREAD_BADGE,
            color=FAINT,
            ref=ref,
            exception="unread",
            detail="the canonical work view was not read",
            action="",
            evidence="unread",
            evidence_detail="",
            revision="",
            read=False,
        )
    badge, color = _badge(view)
    evidence, evidence_detail = _evidence(view)
    return WorkDisplay(
        badge=badge,
        color=color,
        ref=view.identity.item_ref or ref,
        exception=view.exception.state,
        detail=view.exception.detail or view.safest_action.reason,
        action=view.safest_action.action,
        evidence=evidence,
        evidence_detail=evidence_detail,
        revision=view.view_revision,
        read=True,
    )


#: How far back the canonical projection reaches for *finished* work. The
#: recorder can look back further than this; those rows say they were not read
#: rather than guessing a state for a run nobody projected.
WORK_HISTORY_LIMIT = 50


def _newest_first(runs: list[RunRecord]) -> list[RunRecord]:
    """Runs by most recent activity — the order history is projected in."""
    return sorted(
        runs,
        key=lambda r: r.finished_at or r.started_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def work_item_id(record: Any) -> str:
    """A run record's durable item id, falling back to its numeric id.

    Mirrors :func:`kew.service.runs._work_item_id` so the id a row is drawn
    with is the id the projection was composed under.
    """
    return str(
        getattr(record, "item_id", None) or getattr(record, "issue_num", "") or ""
    )


def hold_item_id(hold: Any) -> str:
    """An epic hold's durable item id — never the ``0`` numeric sentinel."""
    raw = str(getattr(hold, "item_id", "") or "")
    if raw and raw != "0":
        return raw
    return str(getattr(hold, "issue_num", "") or "")


class TuiState:
    """Central state for the TUI. Widgets read from this; polling updates it."""

    def __init__(self, *, config: DispatchConfig, store: RunStore) -> None:
        self.config = config
        self.store = store

        # Local state (refreshed every 2s)
        self.pending_runs: list[RunRecord] = []
        self.recent_runs: list[RunRecord] = []
        self.daily_spend: float = 0.0
        self._completed_tokens: dict[str, list[int]] = {}  # complexity -> token counts
        self.fleet_stopped: dict[str, str] | None = None  # kew stop latch
        self.queue_held: list[dict[str, Any]] = []
        # Holds & Queue pane (#685): cross-epic DAG holds + persisted orphan PRs
        self.epic_holds: list[EpicHold] = []
        self.orphan_prs: list[OrphanPRRecord] = []

        # Remote state (refreshed every 30s)
        self.queue_ready: list[GhIssue] = []
        self.queue_failed: list[GhIssue] = []
        self.queue_backlog: list[GhIssue] = []
        self.review_prs: list[GhPullRequest] = []

        # Worker live state (from event files)
        self.worker_snapshots: dict[str, dict[str, Any]] = {}  # run_id -> snapshot

        # Canonical work truth (I22) — the shared projection every terminal
        # surface renders. ``None`` means it could not be composed: no surface
        # may fall back to reinterpreting a RunStatus on its own.
        self.work_projection: WorkProjectionDTO | None = None
        self._work_index: dict[str, WorkViewDTO] = {}

        self.filter_text: dict[str, str] = {}  # panel_id -> filter string
        self._all_runs: list[RunRecord] = []

    # -- canonical work projection (I22) --

    def _work_item_ids(self) -> list[str]:
        """Every item on screen, by canonical id, in a stable order.

        In-flight work, held nodes and the ready queue are always projected;
        finished work is projected newest-first up to
        :data:`WORK_HISTORY_LIMIT`. A row past that limit renders as *not
        read* — an honest blank, never an assumed OK.
        """
        ids: dict[str, None] = {}
        for run in self.pending_runs:
            ids[work_item_id(run)] = None
        for run in _newest_first(self._all_runs)[:WORK_HISTORY_LIMIT]:
            ids[work_item_id(run)] = None
        for hold in self.epic_holds:
            ids[hold_item_id(hold)] = None
        for issue in self.queue_ready:
            ids[str(issue.get("number", "") or "")] = None
        ids.pop("", None)
        return list(ids)

    def refresh_work(self) -> None:
        """Recompose the canonical work projection for everything on screen.

        One held read snapshot, one shared review-pressure reading: two rows of
        the same TUI can never disagree about the same world. A projection that
        cannot be composed stays ``None`` rather than becoming a stale or empty
        one — a healthier-looking world than the real one is the failure this
        slice exists to end.
        """
        item_ids = self._work_item_ids()
        wanted = set(item_ids)
        cached = {
            work_item_id(run): cached_pull_request(run)
            for run in self._all_runs
            if getattr(run, "pr_url", None) and work_item_id(run) in wanted
        }
        # The snapshot-bound reader half of the store, which the narrower
        # ``RunStore`` protocol does not name.
        store: Any = self.store
        try:
            self.work_projection = compose_work_projection(
                store,
                item_ids,
                pull_requests=cached,
                delivery_repo=self.config.project.repo or None,
                pending_max_age_s=pending_run_max_age_s(self.config),
            )
        except Exception:  # noqa: BLE001 - unreadable is a state, not a crash
            self.work_projection = None
        self._work_index = {
            view.identity.item_id or "": view
            for view in (self.work_projection.views if self.work_projection else [])
        }

    def work_view(self, item_id: str | None) -> WorkViewDTO | None:
        """The canonical view for one item, or None when it was not read."""
        return self._work_index.get(item_id or "")

    def work_view_for_run(self, record: Any) -> WorkViewDTO | None:
        """The canonical view for the item a run record belongs to."""
        return self.work_view(work_item_id(record))

    def work_display_for_run(self, record: Any) -> WorkDisplay:
        """The shared display for the item a run record belongs to."""
        return work_display(self.work_view_for_run(record))

    def work_revision(self) -> str:
        """The projection revision on screen — empty when nothing was read."""
        return self.work_projection.projection_revision if self.work_projection else ""

    def work_unreadable(self) -> list[str]:
        """Items the projection named as unreadable rather than dropping."""
        return list(self.work_projection.unreadable) if self.work_projection else []

    def active_worker_tokens(self) -> int:
        """Total tokens across all active worker snapshots."""
        return sum(
            snap.get("total_tokens", 0) for snap in self.worker_snapshots.values()
        )

    def active_worker_cost(self) -> float:
        """Total estimated cost from active (in-flight) worker snapshots."""
        return float(
            sum(snap.get("cost_usd", 0.0) for snap in self.worker_snapshots.values())
        )

    def refresh_local(self) -> None:
        """Refresh state from SQLite (fast, called every 2s)."""
        self.pending_runs = self.store.list_pending()
        all_runs = self.store.list_all()
        self._all_runs = all_runs

        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        self.recent_runs = sorted(
            (r for r in all_runs if r.status != "pending" and r.started_at >= today),
            key=lambda r: r.finished_at or r.started_at,
            reverse=True,
        )
        self.daily_spend = sum(r.estimated_cost_usd for r in self.recent_runs)
        self._completed_tokens = _build_token_buckets(all_runs)
        self.fleet_stopped = self.store.get_fleet_stopped()

        from kew.report import open_approval_holds

        self.queue_held = [
            {"number": h.issue_num, "estimate_usd": h.estimate_usd, "basis": h.basis}
            for h in open_approval_holds(self.store)
        ]

        # Holds & Queue pane data (#685) — cross-epic holds + persisted orphans
        self.epic_holds = self.store.list_all_epic_holds()
        self.orphan_prs = self.store.list_orphan_prs()

        self.refresh_work()

    def refresh_remote(self) -> None:
        """Refresh state from GitHub API (slow, called every 30s)."""
        self.queue_ready, self.queue_failed, self.queue_backlog = list_queue_issues(
            config=self.config
        )
        self.review_prs = list_review_prs(config=self.config)
        for issue in self.queue_ready:
            complexity = get_complexity(issue.get("labels", []))
            try:
                route = resolve_runner(self.config, complexity=complexity)
                issue["_routed_runner"] = route.runner.name
                issue["_routed_model"] = route.model
            except Exception:
                issue["_routed_runner"] = ""
                issue["_routed_model"] = ""

    def refresh_workers(self) -> None:
        """Tail event files for pending runs to update worker snapshots."""
        self.worker_snapshots = {}
        for run in self.pending_runs:
            if run.log_file is None:
                continue
            event_path = run.log_file.with_suffix(".events.jsonl")
            events = read_worker_events(event_path)
            if events:
                self.worker_snapshots[run.run_id] = worker_snapshot_from_events(events)

    def expected_tokens(self, complexity: str) -> int:
        """Return p75 token count for progress bar estimation."""
        tokens = self._completed_tokens.get(complexity, [])
        if len(tokens) >= 3:
            return int(statistics.quantiles(tokens, n=4, method="inclusive")[2])  # p75

        all_tokens = self._completed_tokens.get("all", [])
        if len(all_tokens) >= 3:
            return int(statistics.quantiles(all_tokens, n=4, method="inclusive")[2])

        return 100_000  # Default fallback

    def weekly_spend(self) -> float:
        cutoff = datetime.now(UTC) - timedelta(days=7)
        return sum(
            r.estimated_cost_usd
            for r in self._all_runs
            if r.status != "pending" and r.started_at >= cutoff
        )

    def per_issue_avg(self) -> float:
        completed = [r for r in self.recent_runs if r.status != "pending"]
        if not completed:
            return 0.0
        return self.daily_spend / len(completed)

    def burn_rate(self) -> float:
        now = datetime.now(UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hours = (now - midnight).total_seconds() / 3600
        if hours < 5 / 60:  # less than 5 minutes
            return 0.0
        return self.daily_spend / hours

    def hours_until_limit(self, daily_limit: float) -> float:
        rate = self.burn_rate()
        if rate <= 0:
            return float("inf")
        remaining = daily_limit - self.daily_spend
        if remaining <= 0:
            return 0.0
        return remaining / rate


def matches_filter(text: str, filter_str: str) -> bool:
    """Case-insensitive substring match for filtering."""
    return filter_str.lower() in text.lower()


def read_worker_events(path: Path) -> list[dict[str, Any]]:
    """Read all events from an event file. Returns [] if file missing."""
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def tail_events(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read new events from an event file starting at byte offset.

    Returns (new_events, new_offset). Only advances the offset past
    fully-parsed lines — partial lines (from a concurrent writer mid-flush)
    are left for the next call. Uses binary mode to ensure byte-accurate
    offset tracking regardless of encoding.

    If file doesn't exist or can't be read, returns ([], offset).
    """
    if not path.exists():
        return [], offset
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            new_data = f.read()
    except OSError:
        return [], offset
    if not new_data:
        return [], offset
    events: list[dict[str, Any]] = []
    consumed = 0
    lines = new_data.split(b"\n")
    for i, raw_line in enumerate(lines):
        is_last = i == len(lines) - 1
        # The last element after split has no trailing \n
        line_len = len(raw_line) + (0 if is_last else 1)
        stripped = raw_line.strip()
        if not stripped:
            consumed += line_len
            continue
        try:
            events.append(json.loads(stripped.decode("utf-8")))
            consumed += line_len
        except (json.JSONDecodeError, UnicodeDecodeError):
            if is_last:
                # Unterminated partial line from concurrent write — re-read next time
                break
            # Complete but malformed record — skip it, advance past it
            consumed += line_len
    new_offset = offset + consumed
    return events, new_offset


def worker_snapshot_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a current snapshot from a list of worker events."""
    snap: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "last_activity": "",
        "pid": None,
    }
    for event in events:
        etype = event.get("type", "")
        if etype == "tokens":
            snap["input_tokens"] = event.get("input", 0)
            snap["output_tokens"] = event.get("output", 0)
            snap["total_tokens"] = snap["input_tokens"] + snap["output_tokens"]
            snap["cost_usd"] = event.get("cost_usd", 0.0)
        elif etype == "activity":
            snap["last_activity"] = event.get("msg", "")
        elif etype == "tool_use":
            tool = event.get("tool", "")
            target = event.get("target", "")
            snap["last_activity"] = f"{tool}: {target}" if target else tool
        elif etype == "pid":
            snap["pid"] = event.get("pid")
    return snap
