"""KewApp — main Textual application."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual.app import App
from textual.css.query import NoMatches
from textual.widgets import ListItem, ListView, Static

from kew.audit import DEFAULT_ANCHORS_PATH
from kew.config import DispatchConfig
from kew.ids import item_ref_or_num
from kew.ledger_identity import canonical_project_root, open_managed_store
from kew.models import GhIssue, GhPullRequest
from kew.run_store import SqliteRunStore
from kew.tui.palette import FAINT, JUDGMENT, MACHINE, expand_css
from kew.tui.screens.cockpit import CockpitScreen
from kew.tui.selection import (
    BacklogListItem,
    FailedListItem,
    HeldListItem,
    QueueListItem,
    RecentListItem,
    ReviewListItem,
    WorkerListItem,
)
from kew.tui.state import TuiState, matches_filter

# Monkey-patch Textual's SearchIcon to remove the emoji and fit our brand
try:
    from textual.command import SearchIcon

    SearchIcon.icon = "> "  # type: ignore[assignment]
except ImportError:
    pass


class KewApp(App):  # type: ignore[type-arg]
    """kew terminal dashboard."""

    ENABLE_COMMAND_PALETTE = False

    CSS = expand_css(
        """
    Screen {
        background: $surface;
    }
    .panel {
        height: 1fr;
        border: solid $surface;
        padding: 0 1;
        background: $surface;
    }
    .panel:focus-within {
        border: solid $accent-2;
    }
    .panel-header {
        height: 1;
        color: $faint;
        text-style: bold;
        padding: 0 1;
    }
    .panel-box:focus-within > .panel-header {
        color: $accent;
    }
    .panel-box {
        height: 1fr;
    }
    #status-bar {
        dock: top;
        height: 1;
        background: $accent-2;
        color: $surface;
        padding: 0 1;
        text-style: bold;
    }
    Footer {
        background: $surface;
        color: $faint;
    }
    Footer > .footer--key {
        color: $accent;
        text-style: bold;
        background: $surface;
    }
    Footer > .footer--highlight {
        background: $accent;
        color: $surface;
    }
    Horizontal {
        height: 1fr;
    }
    .logs-list {
        width: 40%;
    }
    #logs-stream-container {
        width: 60%;
        border: solid $surface;
        padding: 0 1;
    }
    #logs-stream-container:focus-within {
        border: solid $accent-2;
    }
    #logs-stream {
        background: $bg;
    }
    #logs-summary {
        height: 1;
        background: $surface-2;
        color: $accent-2;
        text-style: bold;
        padding: 0 1;
    }
    #logs-breadcrumb {
        height: 1;
        background: $surface;
        color: $accent-2;
        text-style: bold;
        padding: 0 1;
    }
    #expanded-breadcrumb {
        height: 1;
        background: $surface;
        color: $accent-2;
        text-style: bold;
        padding: 0 1;
    }
    #detail-pane {
        width: 60%;
        border: solid $surface;
        padding: 1 2;
    }
    #detail-pane:focus-within {
        border: solid $accent-2;
    }
    .queue-list-expanded {
        width: 40%;
    }
    CommandPalette {
        background: $surface 80%;
    }
    CommandPalette > * {
        background: $surface;
        color: $fg;
        border: solid $accent-2;
    }
    CommandPalette OptionList > ListItem:hover {
        background: $accent-2;
        color: $surface;
    }
    CommandPalette Input {
        border: none;
        border-bottom: solid $accent;
        background: $surface;
    }
    CommandPalette Input:focus {
        border-bottom: solid $accent-2;
    }
    #analytics-breadcrumb {
        height: 1;
        background: $bg;
        color: $accent-2;
        text-style: bold;
        padding: 0 2;
    }
    #analytics-stats-row {
        height: 5;
        margin: 0 1;
        padding: 0 0;
    }
    .stat-card {
        width: 1fr;
        text-align: center;
        height: 5;
        border: solid $border;
        margin: 0 1;
        padding: 0 1;
        background: $bg;
    }
    .stat-card:focus {
        border: solid $accent-2;
    }
    #analytics-panel:focus {
        border: solid $accent-2;
    }
    #analytics-left-scroll, #analytics-right-scroll {
        width: 1fr;
        border: solid $border;
        margin: 0 1;
        background: $bg;
    }
    #analytics-left-scroll:focus-within, #analytics-right-scroll:focus-within {
        border: solid $accent-2;
    }
    #analytics-left, #analytics-right {
        height: auto;
        padding: 1 2;
    }
    #budget-content {
        padding: 2 4;
        background: $bg;
        border: solid $border;
        margin: 2 4;
    }
    #budget-dialog-title {
        padding: 0 2;
    }
    #budget-dialog-body {
        padding: 1 2;
    }
    #budget-dialog-hint {
        padding: 0 2;
    }
    BudgetOverlay {
        align: center middle;
        background: $surface 80%;
    }
    BudgetExceededDialog {
        align: center middle;
        background: $surface 80%;
    }
    #budget-dialog-title, #budget-dialog-issue,
    #budget-dialog-body, #budget-dialog-hint {
        background: $bg;
    }
    """
    )

    def __init__(
        self,
        *,
        config: DispatchConfig,
        db_path: str = "",
        project_root: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.db_path = db_path
        self.project_root = project_root
        self._store: SqliteRunStore | None = None
        self.state: TuiState | None = None
        self._detail_timer: Any | None = None
        self._detail_issue_num: int | None = None  # cache: skip if same
        self._detail_pr_num: int | None = None  # cache: skip if same PR
        self._pr_detail_cache: dict[int, GhIssue] = {}
        self._pr_cache_ts: dict[int, float] = {}  # PR cache timestamps
        self._queue_snapshot: tuple[
            list[int],
            list[int],
            list[int],
            list[tuple[int, float | None, str]],
            str,
        ] = (
            [],
            [],
            [],
            [],
            "",
        )  # cache: ready/failed/backlog issue numbers + held (num, est, basis)
        self._queue_lv_id: int = 0  # id() of last populated ListView
        # cache: (run_id, row_type) last rendered
        self._workers_snapshot: list[tuple[str, str]] = []
        self._workers_lv_id: int = 0
        self._workers_filter: str = ""
        self._reviews_snapshot: list[tuple[int, tuple[tuple[str, str], ...]]] = []
        self._reviews_lv_id: int = 0
        self._reviews_detail_lv_id: int = 0
        self._poll_debounce_timer: Any | None = None

    def on_mount(self) -> None:
        self.push_screen(CockpitScreen())
        if self.db_path:
            self._store = self._open_store()
            self.state = TuiState(config=self.config, store=self._store)
            self._start_polling()

    def _open_store(self) -> SqliteRunStore:
        """Open the TUI store, retaining raw in-memory stores for tests only."""
        if self.db_path == ":memory:":
            return SqliteRunStore(":memory:", check_same_thread=False)
        root = canonical_project_root(
            self.project_root or Path(self.db_path).resolve().parent
        )
        return open_managed_store(
            project_root=root,
            db_path=Path(self.db_path).resolve(),
            anchors_path=root / DEFAULT_ANCHORS_PATH,
            read_only=False,
            check_same_thread=False,
        )

    def _start_polling(self) -> None:
        self._poll_local()
        self._poll_remote()
        self.set_interval(2.0, self._poll_local)
        self.set_interval(30.0, self._poll_remote)

    def _poll_local(self) -> None:
        if self.state:
            self.state.refresh_local()
            self.state.refresh_workers()
            self._update_widgets()

    def _update_widgets(self) -> None:
        """Push current state into all visible widgets."""
        if self.state is None:
            return

        # Status bar
        try:
            from kew.tui.widgets.status_bar import format_status_bar

            bar = self.screen.query_one("#status-bar", Static)
            bar.update(
                format_status_bar(
                    project=self.config.project.name or "",
                    daily_spend=self.state.daily_spend,
                    daily_limit=self.config.budget.daily_usd,
                    worker_count=len(self.state.pending_runs),
                    queue_ready_count=len(self.state.queue_ready),
                    queue_backlog_count=len(self.state.queue_backlog),
                    active_tokens=self.state.active_worker_tokens(),
                    active_cost=self.state.active_worker_cost(),
                    fleet_stopped=self.state.fleet_stopped,
                )
            )
        except NoMatches:
            pass  # Widget may not exist during screen transitions

        # Queue panel — skip rebuild if issue list unchanged
        try:
            from kew.tui.widgets.queue_panel import (
                format_backlog_row_compact,
                format_failed_row_compact,
                format_failed_separator,
                format_queue_row_compact,
                format_separator,
            )

            queue_lv = self.screen.query_one("#queue-panel", ListView)
            new_ready_nums = [i.get("number", 0) for i in self.state.queue_ready]
            new_failed_nums = [i.get("number", 0) for i in self.state.queue_failed]
            new_backlog_nums = [i.get("number", 0) for i in self.state.queue_backlog]
            # held rows carry estimate/basis that can change on a re-hold while the
            # issue number stays — include them so a metadata-only change rebuilds
            new_held = [
                (i.get("number", 0), i.get("estimate_usd"), i.get("basis", ""))
                for i in self.state.queue_held
            ]
            queue_filter = self.state.filter_text.get("queue-panel", "")
            new_snapshot = (
                new_ready_nums,
                new_failed_nums,
                new_backlog_nums,
                new_held,
                queue_filter,
            )
            new_widget = id(queue_lv) != self._queue_lv_id
            if new_snapshot != self._queue_snapshot or new_widget:
                self._queue_snapshot = new_snapshot
                self._queue_lv_id = id(queue_lv)
                if new_widget:
                    self._detail_issue_num = None
                queue_sel_id: int | None = None
                queue_sel_child = queue_lv.highlighted_child
                if isinstance(
                    queue_sel_child,
                    (QueueListItem, FailedListItem, BacklogListItem, HeldListItem),
                ):
                    queue_sel_id = queue_sel_child.issue_num
                queue_saved_idx = queue_lv.index or 0
                queue_lv.clear()
                queue_appended = 0
                queue_restore_idx: int | None = None

                # Held (needs approval) — most actionable, at the top
                held_rows = [
                    (h["number"], _format_held_row(h)) for h in self.state.queue_held
                ]
                held_rows = [
                    (n, t)
                    for (n, t) in held_rows
                    if not queue_filter or matches_filter(t, queue_filter)
                ]
                if held_rows:
                    queue_lv.append(ListItem(Static("⏸ NEEDS APPROVAL"), disabled=True))
                    queue_appended += 1
                    for num, text in held_rows:
                        queue_lv.append(HeldListItem(issue_num=num, text=text))
                        if queue_sel_id is not None and num == queue_sel_id:
                            queue_restore_idx = queue_appended
                        queue_appended += 1

                # Ready items
                for issue in self.state.queue_ready:
                    text = format_queue_row_compact(
                        issue,
                        routed_runner=issue.get("_routed_runner", ""),
                        routed_model=issue.get("_routed_model", ""),
                    )
                    if queue_filter and not matches_filter(text, queue_filter):
                        continue
                    queue_lv.append(QueueListItem(issue_num=issue["number"], text=text))
                    if queue_sel_id is not None and issue["number"] == queue_sel_id:
                        queue_restore_idx = queue_appended
                    queue_appended += 1

                # Failed section (conditional)
                has_failed = False
                for issue in self.state.queue_failed:
                    text = format_failed_row_compact(issue)
                    if queue_filter and not matches_filter(text, queue_filter):
                        continue
                    has_failed = True
                    break

                if has_failed:
                    sep_text = format_failed_separator()
                    queue_lv.append(ListItem(Static(sep_text), disabled=True))
                    queue_appended += 1

                for issue in self.state.queue_failed:
                    text = format_failed_row_compact(issue)
                    if queue_filter and not matches_filter(text, queue_filter):
                        continue
                    queue_lv.append(
                        FailedListItem(issue_num=issue["number"], text=text)
                    )
                    if queue_sel_id is not None and issue["number"] == queue_sel_id:
                        queue_restore_idx = queue_appended
                    queue_appended += 1

                # Separator (only if there are filterable backlog items)
                has_backlog = False
                for issue in self.state.queue_backlog:
                    text = format_backlog_row_compact(issue)
                    if queue_filter and not matches_filter(text, queue_filter):
                        continue
                    has_backlog = True
                    break

                if has_backlog:
                    sep_text = format_separator(
                        len(self.state.queue_ready), len(self.state.queue_backlog)
                    )
                    queue_lv.append(ListItem(Static(sep_text), disabled=True))
                    queue_appended += 1

                # Backlog items
                for issue in self.state.queue_backlog:
                    text = format_backlog_row_compact(issue)
                    if queue_filter and not matches_filter(text, queue_filter):
                        continue
                    queue_lv.append(
                        BacklogListItem(issue_num=issue["number"], text=text)
                    )
                    if queue_sel_id is not None and issue["number"] == queue_sel_id:
                        queue_restore_idx = queue_appended
                    queue_appended += 1

                if queue_appended == 0:
                    queue_lv.append(
                        ListItem(
                            Static(
                                f"\n\n[{JUDGMENT}]    /// TARGET ACQUISITION"
                                " \\\\\\\n    > NO TARGETS IN QUEUE"
                                f"\n    > AGENTS IN STANDBY[/{JUDGMENT}]"
                            ),
                            disabled=True,
                        )
                    )
                if queue_restore_idx is not None:
                    queue_lv.index = queue_restore_idx
                elif queue_appended:
                    queue_lv.index = min(queue_saved_idx, queue_appended - 1)
                else:
                    queue_lv.index = 0
        except NoMatches:
            pass  # Widget may not exist during screen transitions

        # Workers panel — snapshot cache with in-place text updates
        # Shows active workers at top, completed runs below a divider
        try:
            from kew.tui.widgets.workers_panel import (
                format_completed_row,
                format_worker_row_compact,
            )

            workers_lv = self.screen.query_one("#workers-panel", ListView)
            workers_filter = self.state.filter_text.get("workers-panel", "")

            # Pre-compute filtered rows: (run_id, text, row_type)
            # row_type: "worker", "divider", "recent"
            filtered_worker_rows: list[tuple[str, str, str]] = []

            # Active workers
            for run in self.state.pending_runs:
                snap = self.state.worker_snapshots.get(run.run_id, {})
                complexity = run.complexity or "medium"
                expected = self.state.expected_tokens(complexity)
                ref = item_ref_or_num(run.item_id, run.issue_num)
                issue_title = run.title or f"Issue {ref}"
                text = format_worker_row_compact(
                    run,
                    snapshot=snap,
                    expected_tokens=expected,
                    issue_title=issue_title,
                )
                if workers_filter and not matches_filter(text, workers_filter):
                    continue
                filtered_worker_rows.append((run.run_id, text, "worker"))

            # Completed runs (up to 10, already sorted most-recent-first)
            filtered_recent_in_workers: list[tuple[str, str]] = []
            now = datetime.now(UTC)
            for run in self.state.recent_runs[:10]:
                text = format_completed_row(run, now=now)
                if workers_filter and not matches_filter(text, workers_filter):
                    continue
                filtered_recent_in_workers.append((run.run_id, text))

            # Insert divider between active and completed (only when both exist)
            has_active = any(t == "worker" for _, _, t in filtered_worker_rows)
            if has_active and filtered_recent_in_workers:
                filtered_worker_rows.append(
                    ("__divider__", "[dim]─── completed ───[/dim]", "divider")
                )
            for run_id, text in filtered_recent_in_workers:
                filtered_worker_rows.append((run_id, text, "recent"))

            new_workers_snapshot = [
                (rid, rtype) for rid, _, rtype in filtered_worker_rows
            ]
            new_workers_widget = id(workers_lv) != self._workers_lv_id
            filter_changed = workers_filter != self._workers_filter
            structure_changed = (
                new_workers_snapshot != self._workers_snapshot
                or new_workers_widget
                or filter_changed
            )
            if structure_changed:
                # Full rebuild — worker set or filter changed
                self._workers_snapshot = new_workers_snapshot
                self._workers_lv_id = id(workers_lv)
                self._workers_filter = workers_filter
                workers_sel_id: str | None = None
                workers_sel_child = workers_lv.highlighted_child
                if isinstance(workers_sel_child, (WorkerListItem, RecentListItem)):
                    workers_sel_id = workers_sel_child.run_id
                workers_saved_idx = workers_lv.index or 0
                workers_lv.clear()
                workers_appended = 0
                workers_restore_idx: int | None = None
                for run_id, text, row_type in filtered_worker_rows:
                    if row_type == "divider":
                        workers_lv.append(ListItem(Static(text), disabled=True))
                        workers_appended += 1
                    elif row_type == "worker":
                        workers_lv.append(WorkerListItem(run_id=run_id, text=text))
                        if workers_sel_id is not None and run_id == workers_sel_id:
                            workers_restore_idx = workers_appended
                        workers_appended += 1
                    elif row_type == "recent":
                        workers_lv.append(RecentListItem(run_id=run_id, text=text))
                        if workers_sel_id is not None and run_id == workers_sel_id:
                            workers_restore_idx = workers_appended
                        workers_appended += 1
                if workers_appended == 0:
                    workers_lv.append(
                        ListItem(
                            Static(
                                f"\n\n[{MACHINE}]    /// DECK CLEAR"
                                " \\\\\\\n    > ALL WORKERS TERMINATED"
                                f"\n    > AWAITING DISPATCH[/{MACHINE}]"
                            ),
                            disabled=True,
                        )
                    )
                if workers_restore_idx is not None:
                    workers_lv.index = workers_restore_idx
                elif workers_appended:
                    workers_lv.index = min(workers_saved_idx, workers_appended - 1)
                else:
                    workers_lv.index = 0
            else:
                # In-place update — same filtered workers, refresh text only
                for idx, (_, text, row_type) in enumerate(filtered_worker_rows):
                    if row_type == "divider":
                        continue  # divider text is static
                    if idx < len(workers_lv._nodes):
                        child = workers_lv._nodes[idx]
                        try:
                            static = child.query_one(Static)
                            static.update(text)
                        except NoMatches:
                            pass
        except NoMatches:
            pass  # Widget may not exist during screen transitions

        # Reviews panel — skip rebuild if PR list unchanged
        try:
            from kew.tui.widgets.reviews_panel import format_review_row_compact

            def _pr_digest(
                p: GhPullRequest,
            ) -> tuple[int, tuple[tuple[str, str], ...]]:
                checks = p.get("statusCheckRollup", [])
                ci_key = tuple(
                    (c.get("name", ""), c.get("conclusion", ""))
                    for c in checks
                    if isinstance(c, dict)
                )
                return (p.get("number", 0), ci_key)

            reviews_lv = self.screen.query_one("#reviews-panel", ListView)
            reviews_filter = self.state.filter_text.get("reviews-panel", "")
            new_reviews_snapshot = [_pr_digest(p) for p in self.state.review_prs]
            new_reviews_widget = id(reviews_lv) != self._reviews_lv_id
            new_reviews_filter_changed = reviews_filter != getattr(
                self, "_reviews_filter", ""
            )
            if (
                new_reviews_snapshot != self._reviews_snapshot
                or new_reviews_widget
                or new_reviews_filter_changed
            ):
                self._reviews_snapshot = new_reviews_snapshot
                self._reviews_lv_id = id(reviews_lv)
                self._reviews_filter = reviews_filter
                reviews_sel_pr: int | None = None
                reviews_sel_child = reviews_lv.highlighted_child
                if isinstance(reviews_sel_child, ReviewListItem):
                    reviews_sel_pr = reviews_sel_child.pr_number
                reviews_saved_idx = reviews_lv.index or 0
                reviews_lv.clear()
                reviews_appended = 0
                reviews_restore_idx: int | None = None
                for pr in self.state.review_prs:
                    text = format_review_row_compact(pr)
                    if reviews_filter and not matches_filter(text, reviews_filter):
                        continue
                    if reviews_appended >= 20:
                        break
                    reviews_lv.append(ReviewListItem(pr_number=pr["number"], text=text))
                    if reviews_sel_pr is not None and pr["number"] == reviews_sel_pr:
                        reviews_restore_idx = reviews_appended
                    reviews_appended += 1
                if reviews_appended == 0:
                    reviews_lv.append(
                        ListItem(
                            Static(
                                f"\n\n[{FAINT}]    /// REVIEW SCAN"
                                " \\\\\\\n    > NO PENDING REVIEWS"
                                f"\n    > SCAN COMPLETE[/{FAINT}]"
                            ),
                            disabled=True,
                        )
                    )
                if reviews_restore_idx is not None:
                    reviews_lv.index = reviews_restore_idx
                elif reviews_appended:
                    reviews_lv.index = min(reviews_saved_idx, reviews_appended - 1)
                else:
                    reviews_lv.index = 0
        except NoMatches:
            pass

        # Analytics panel — compact stats (cockpit only)
        try:
            analytics_static = self.screen.query_one("#analytics-panel", Static)
            from kew.analytics import aggregate_stats
            from kew.tui.widgets.analytics_panel import format_analytics_compact

            today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            stats = aggregate_stats(self.state.store, since=today)
            daily_budget = (
                self.config.budget.daily_usd
                if hasattr(self.config, "budget") and self.config.budget
                else None
            )
            analytics_static.update(
                format_analytics_compact(stats, daily_budget=daily_budget)
            )
        except NoMatches:
            pass

        # Panel headers — show active filter indicator
        _PANEL_HEADERS = {
            "queue-panel": ("queue-header", "[1] QUEUE"),
            "workers-panel": ("workers-header", "[2] WORKERS"),
            "reviews-panel": ("reviews-header", "[3] REVIEWS"),
        }
        for panel_id, (header_id, base) in _PANEL_HEADERS.items():
            try:
                header = self.screen.query_one(f"#{header_id}", Static)
                filt = self.state.filter_text.get(panel_id, "")
                if filt:
                    header.update(f"{base} [dim]/{filt}[/dim]")
                else:
                    header.update(base)
            except NoMatches:
                pass

        # Logs screen — refresh run list if active
        try:
            from kew.tui.screens.logs import LogsScreen

            screen = self.screen
            if isinstance(screen, LogsScreen):
                screen._update_run_list()
        except Exception:  # nosec B110 — polling errors must not crash the TUI
            pass

    def _update_detail_pane(self) -> None:
        """Debounced detail pane update — waits 150ms after last highlight change."""
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.15, self._do_update_detail_pane)

    def _do_update_detail_pane(self) -> None:
        """Actually update the detail pane content."""
        self._detail_timer = None
        if self.state is None:
            return
        # Check if reviews panel with detail pane is active (expanded view only).
        # The cockpit has #reviews-panel but no #detail-pane, so this only
        # triggers in the expanded reviews view.
        try:
            self.screen.query_one("#reviews-panel", ListView)
            self.screen.query_one("#detail-pane")
            self._do_update_review_detail_pane()
            return
        except NoMatches:
            pass
        # Fall through to existing queue detail logic
        try:
            from kew.tui.widgets.detail_pane import DetailPane

            detail = self.screen.query_one("#detail-pane", DetailPane)
            queue_lv = self.screen.query_one("#queue-panel", ListView)
            queue_sel = queue_lv.highlighted_child
            if isinstance(queue_sel, (QueueListItem, FailedListItem, BacklogListItem)):
                # Cache hit — skip if already showing this issue
                if queue_sel.issue_num == self._detail_issue_num:
                    return
                for issue in self.state.queue_ready:
                    if issue.get("number") == queue_sel.issue_num:
                        self._detail_issue_num = queue_sel.issue_num
                        detail.update_issue(issue)
                        return
                for issue in self.state.queue_backlog:
                    if issue.get("number") == queue_sel.issue_num:
                        self._detail_issue_num = queue_sel.issue_num
                        detail.update_issue(issue)
                        return
                for issue in self.state.queue_failed:
                    if issue.get("number") == queue_sel.issue_num:
                        self._detail_issue_num = queue_sel.issue_num
                        detail.update_issue(issue)
                        return
                self._detail_issue_num = None
                detail.update_issue(None)
            else:
                self._detail_issue_num = None
                detail.update_issue(None)
        except NoMatches:
            pass

    def _do_update_review_detail_pane(self) -> None:
        """Update detail pane for reviews panel — async fetch with caching."""
        if self.state is None:
            return
        try:
            from kew.tui.widgets.detail_pane import DetailPane

            detail = self.screen.query_one("#detail-pane", DetailPane)
            reviews_lv = self.screen.query_one("#reviews-panel", ListView)
            # Reset cache on widget change (e.g. after screen push/pop)
            if id(reviews_lv) != self._reviews_detail_lv_id:
                self._reviews_detail_lv_id = id(reviews_lv)
                self._detail_pr_num = None
            review_sel = reviews_lv.highlighted_child
            if isinstance(review_sel, ReviewListItem):
                pr_num = review_sel.pr_number
                if pr_num == self._detail_pr_num:
                    return
                self._detail_pr_num = pr_num
                import time as _time

                _CACHE_TTL = 30.0  # seconds, matches remote poll
                cached = self._pr_detail_cache.get(pr_num)
                cache_age = _time.monotonic() - self._pr_cache_ts.get(pr_num, 0)
                if cached is not None and cache_age < _CACHE_TTL:
                    detail.update_issue(cached)
                    return
                self.run_worker(self._fetch_pr_detail(pr_num))
            else:
                self._detail_pr_num = None
                detail.update_issue(None)
        except NoMatches:
            pass

    async def _fetch_pr_detail(self, pr_number: int) -> None:
        """Fetch PR metadata in background thread and update detail pane."""
        import asyncio
        import json

        from kew.subprocess_utils import run as sp_run

        try:
            result = await asyncio.to_thread(
                sp_run,
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    self.config.project.repo,
                    "--json",
                    "number,title,author,body,headRefName,createdAt,comments,"
                    "statusCheckRollup,reviewDecision,mergeable,reviews,reviewRequests",
                ],
                capture=True,
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                self._detail_pr_num = None  # Allow retry
                return
            pr_data = json.loads(result.stdout)
            author = pr_data.get("author", {})
            author_login = author.get("login", "") if isinstance(author, dict) else ""

            from kew.tui.review_detail import (
                compute_verdict,
                format_checks_section,
                format_comments_section,
                format_merge_section,
                format_reviews_section,
                format_verdict_bar,
            )

            verdict = compute_verdict(pr_data)
            verdict_text = format_verdict_bar(verdict)
            checks_text = format_checks_section(pr_data.get("statusCheckRollup", []))
            reviews_text = format_reviews_section(pr_data.get("reviews", []))
            merge_text = format_merge_section(pr_data)

            # Fetch review comments
            comments_text = ""
            try:
                from kew.review import fetch_review_comments

                review_comments = await asyncio.to_thread(
                    fetch_review_comments, pr_number, self.config, False, None
                )
                comment_dicts = [
                    {"author": c.author, "path": c.path, "line": c.line, "body": c.body}
                    for c in review_comments
                ]
                comments_text = format_comments_section(comment_dicts)
            except Exception as exc:
                import logging

                logging.getLogger(__name__).debug(
                    "Failed to fetch review comments for PR #%d: %s",
                    pr_number,
                    exc,
                )
                comments_text = "[dim]Could not load comments[/dim]"

            # Build enhanced body with status sections
            body_parts = [
                verdict_text,
                "",
                checks_text,
                "",
                reviews_text,
                "",
                merge_text,
            ]
            if comments_text:
                body_parts.extend(["", "\u2500" * 19, "", comments_text])

            enhanced_body = "\n".join(body_parts)

            issue_like: GhIssue = {
                "number": pr_data.get("number", pr_number),
                "title": pr_data.get("title", ""),
                "body": enhanced_body,
                "labels": [],
                "assignees": ([{"login": author_login}] if author_login else []),
                "createdAt": pr_data.get("createdAt", ""),
                "_rich_markup": True,  # Body is Rich markup, not Markdown
            }
            import time as _time

            self._pr_detail_cache[pr_number] = issue_like
            self._pr_cache_ts[pr_number] = _time.monotonic()
            if self._detail_pr_num == pr_number:
                from kew.tui.widgets.detail_pane import DetailPane

                try:
                    detail = self.screen.query_one("#detail-pane", DetailPane)
                    detail.update_issue(issue_like)
                except NoMatches:
                    pass
        except Exception:
            self._detail_pr_num = None  # Allow retry; network errors must not crash TUI

    def _poll_remote(self) -> None:
        if self.state:
            self.run_worker(self._async_poll_remote())

    async def _async_poll_remote(self) -> None:
        import asyncio

        try:
            await asyncio.to_thread(self.state.refresh_remote)  # type: ignore[union-attr]
            self._debounced_update_widgets()
        except Exception:  # nosec B110 — network/auth errors must not crash the TUI
            pass

    def _debounced_update_widgets(self) -> None:
        """Schedule _update_widgets with debounce."""
        if self._poll_debounce_timer is not None:
            self._poll_debounce_timer.stop()
        self._poll_debounce_timer = self.set_timer(0.15, self._do_debounced_update)

    def _do_debounced_update(self) -> None:
        self._poll_debounce_timer = None
        self._update_widgets()

    async def action_quit(self) -> None:
        if self._store is not None:
            self._store.close()
        await super().action_quit()


def _format_held_row(h: dict[str, Any]) -> str:
    """Format a held (needs-approval) issue as a compact queue row."""
    est = "unknown" if h.get("estimate_usd") is None else f"${h['estimate_usd']:.2f}"
    basis = h.get("basis", "")
    tail = f" · {basis}" if basis else ""
    return f"#{h['number']} · est {est}{tail}"
