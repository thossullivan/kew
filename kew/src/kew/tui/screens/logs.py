"""Unified logs screen — the compact live recorder.

Logs answers "what is this worker doing right now?" and nothing else. What the
*work* is comes from the canonical projection (:func:`kew.tui.state.work_display`)
and is shown verbatim — badge, exception, safest action, evidence gaps and the
view revision behind them. Anything deeper (the full step history, diffs,
receipts) is the web detail this screen links to rather than rebuilds: there is
one canonical work surface, and a second scrubber in the terminal would be a
second answer.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, RichLog, Static

from kew.service.models import SandboxPostureDTO
from kew.service.runs import RunDetail, sandbox_posture_summary, select_run_detail
from kew.service.work import pending_run_max_age_s
from kew.tui.palette import ATTESTED, FAINT, JUDGMENT, MACHINE, MONEY
from kew.tui.selection import RecentListItem, WorkerListItem, WorkItemRef
from kew.tui.state import WorkDisplay, matches_filter, work_display

_LOOKBACK_CYCLE: list[str] = ["today", "7d", "30d", "all"]

#: Where the full, canonical history of a run lives. The recorder links here
#: instead of growing a second timeline surface of its own.
_WEB_DETAIL = "kew serve \u2192 /runs/{run_id}"


def _row_ref(run: Any, display: WorkDisplay) -> str:
    """The row's display reference: the projection's, or the record's own.

    Never ``#0``: a Linear item whose ``issue_num`` column had to hold the
    sentinel is still ``SFD-12`` here.
    """
    return display.ref or WorkItemRef.from_record(run).display


def _title(run: Any, ref: str) -> str:
    return str(getattr(run, "title", "") or f"Issue {ref}")


def pending_row_text(run: Any, display: WorkDisplay) -> str:
    """One in-flight worker row: canonical badge, canonical ref, elapsed."""
    ref = _row_ref(run, display)
    elapsed = ""
    if run.started_at:
        secs = (datetime.now(UTC) - run.started_at).total_seconds()
        mins = int(secs // 60)
        elapsed = f"{mins}m" if mins > 0 else f"{int(secs)}s"
    return (
        f"{display.markup} [bold]{ref:<7}[/bold] "
        f"{_title(run, ref)[:20]:<20}  [dim]{elapsed}[/dim]"
    )


def completed_row_text(run: Any, display: WorkDisplay) -> str:
    """One finished-run row.

    The badge is the canonical projection's, so preserved-but-unpublished work
    is never painted as a failure the operator should retry.
    """
    ref = _row_ref(run, display)
    duration = f"{int(run.duration_seconds / 60)}m" if run.duration_seconds else ""
    cost = f"${run.estimated_cost_usd:.2f}" if run.estimated_cost_usd else ""
    return (
        f"{display.markup} [bold]{ref:<7}[/bold] "
        f"{_title(run, ref)[:20]:<20}  [dim]{duration} {cost}[/dim]"
    )


def work_detail_line(display: WorkDisplay, run_id: str) -> str:
    """The recorder's one canonical work line for the selected run.

    Exception, safest action, how good the evidence behind them is, and the
    revision they were composed at — then the route to the canonical web
    detail. No word here is invented by this screen.
    """
    detail = _WEB_DETAIL.format(run_id=run_id)
    if not display.read:
        return (
            f"[{FAINT}]work: not read \u2014 the canonical view was not"
            f" composed[/{FAINT}]  [dim]{detail}[/dim]"
        )
    gaps = f" ({display.evidence_detail})" if display.evidence_detail else ""
    return (
        f"{display.markup} [bold]{display.ref}[/bold] "
        f"[dim]{display.exception} \u2192 {display.action or 'nothing to do'}[/dim]  "
        f"[dim]evidence {display.evidence}{gaps}  {display.revision}  "
        f"{detail}[/dim]"
    )


def posture_detail_line(
    posture: SandboxPostureDTO, *, attempt_number: int | None = None
) -> str:
    """Render the service-owned effective posture for one run."""
    attempt = "" if attempt_number is None else f"; attempt={attempt_number}"
    return f"[dim]posture: {sandbox_posture_summary(posture)}{attempt}[/dim]"


class LogsScreen(Screen):  # type: ignore[type-arg]
    """Full-screen logs view — run list (left) + event stream (right)."""

    BINDINGS = [
        ("escape", "pop_screen", "Back"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("J", "scroll_stream_down", "Stream\u2193"),
        ("K", "scroll_stream_up", "Stream\u2191"),
        ("h", "toggle_lookback", "History"),
        ("slash", "filter", "Filter"),
        ("b", "toggle_budget", "Budget"),
        ("question_mark", "toggle_help", "Help"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, pre_select_run_id: str | None = None) -> None:
        super().__init__()
        self.pre_select_run_id = pre_select_run_id
        self.lookback: str = "today"
        self._tail_offset: int = 0
        self._tail_buffer: bytes = b""
        self._tail_path: Path | None = None
        self._tail_timer: Any = None
        self._selected_run_id: str | None = None
        self._selected_record_key: tuple[Any, ...] | None = None
        self._detail_timer: Any = None
        self._live_tokens: int = 0
        self._live_cost: float = 0.0
        self._live_start_ts: float = 0.0

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        yield Static(
            "[dim]Esc:back[/dim]   [bold]Workers[/bold]  [dim]\\[today][/dim]",
            id="logs-breadcrumb",
        )
        with Horizontal():
            yield ListView(id="logs-list", classes="panel logs-list")
            with Vertical(id="logs-stream-container"):
                yield Static("", id="logs-summary")
                yield Static(work_detail_line(work_display(None), ""), id="logs-work")
                yield Static(
                    posture_detail_line(SandboxPostureDTO()), id="logs-posture"
                )
                yield RichLog(id="logs-stream", wrap=True, max_lines=2000, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._update_run_list()
        self._tail_timer = self.set_interval(1.0, self._tail_events)

    def on_unmount(self) -> None:
        if self._tail_timer is not None:
            self._tail_timer.stop()

    # -- Run list management --

    def _update_run_list(self) -> None:
        """Rebuild the run list from TuiState."""
        app: Any = self.app
        if app.state is None:
            return
        try:
            lv = self.query_one("#logs-list", ListView)
        except Exception:
            return

        filter_str = app.state.filter_text.get("logs-list", "")

        # Capture current selection before clearing
        target_run_id = self._selected_run_id
        if self.pre_select_run_id:
            target_run_id = self.pre_select_run_id
            self.pre_select_run_id = None

        lv.clear()
        appended, restore_idx = self._append_pending_runs(
            app, lv, filter_str, target_run_id
        )

        appended, restore_idx = self._append_completed_runs(
            app, lv, filter_str, target_run_id, appended, restore_idx
        )

        # Empty state
        if appended == 0:
            lv.append(
                ListItem(
                    Static(
                        f"\n\n[{FAINT}]    /// LOG ARCHIVE \\\\\\\n"
                        f"    > NO RUNS FOUND[/{FAINT}]"
                    ),
                    disabled=True,
                )
            )

        if restore_idx is not None:
            lv.index = restore_idx

    def _append_completed_runs(
        self,
        app: Any,
        lv: ListView,
        filter_str: str,
        target_run_id: str | None,
        appended: int,
        restore_idx: int | None,
    ) -> tuple[int, int | None]:
        completed = self._get_completed_runs(app)
        pending_count = appended
        divider_added = False

        # Completed runs
        for run in completed:
            text = completed_row_text(run, app.state.work_display_for_run(run))
            if filter_str and not matches_filter(text, filter_str):
                continue
            if not divider_added and pending_count > 0:
                lv.append(
                    ListItem(
                        Static(
                            "[dim]\u2500\u2500\u2500 completed \u2500\u2500\u2500[/dim]"
                        ),
                        disabled=True,
                    )
                )
                appended += 1
                divider_added = True
            lv.append(
                RecentListItem(
                    run_id=run.run_id, text=text, work_ref=WorkItemRef.from_record(run)
                )
            )
            if target_run_id and run.run_id == target_run_id:
                restore_idx = appended
            appended += 1

        return appended, restore_idx

    def _append_pending_runs(
        self, app: Any, lv: ListView, filter_str: str, target_run_id: str | None
    ) -> tuple[int, int | None]:
        appended = 0
        restore_idx: int | None = None

        # Pending workers at top
        for run in app.state.pending_runs:
            text = pending_row_text(run, app.state.work_display_for_run(run))
            if filter_str and not matches_filter(text, filter_str):
                continue
            lv.append(
                WorkerListItem(
                    run_id=run.run_id, text=text, work_ref=WorkItemRef.from_record(run)
                )
            )
            if target_run_id and run.run_id == target_run_id:
                restore_idx = appended
            appended += 1

        return appended, restore_idx

    def _get_completed_runs(self, app: Any) -> list[Any]:
        """Get completed runs based on lookback setting."""
        if self.lookback == "today":
            return list(app.state.recent_runs)
        all_runs = app.state.store.list_all()
        completed = [r for r in all_runs if r.status != "pending"]
        if self.lookback == "all":
            return completed
        days = 7 if self.lookback == "7d" else 30
        cutoff = datetime.now(UTC) - timedelta(days=days)
        return [
            r for r in completed if r.started_at is not None and r.started_at >= cutoff
        ]

    # -- Event stream (right pane) --

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update event stream when selection changes (debounced)."""
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.15, self._load_selected_run)

    def _load_selected_run(self) -> None:
        """Load events for the currently selected run."""
        self._detail_timer = None
        app: Any = self.app
        if app.state is None:
            return

        try:
            lv = self.query_one("#logs-list", ListView)
        except Exception:
            return

        item = lv.highlighted_child
        run_id: str | None = None
        if isinstance(item, (WorkerListItem, RecentListItem)):
            run_id = item.run_id

        if run_id is None or run_id == self._selected_run_id:
            return

        self._selected_run_id = run_id

        self._render_selected_run(run_id, self._select_detail(run_id))

    def _select_detail(self, run_id: str) -> RunDetail:
        app: Any = self.app
        config = getattr(app.state, "config", None)
        project = getattr(config, "project", None)
        return select_run_detail(
            app.state.store,
            run_id,
            delivery_repo=getattr(project, "repo", None) or None,
            # The same in-flight bound the dashboard's projection is composed
            # under, so one screen never reads a pending row two ways.
            pending_max_age_s=pending_run_max_age_s(config),
        )

    @staticmethod
    def _record_key(run: Any) -> tuple[Any, ...]:
        return tuple(
            getattr(run, name, None)
            for name in ("attempt_number", "log_file", "status")
        )

    def _update_selected_facts(self, run_id: str, detail: RunDetail) -> None:
        """Render work and posture from the same selection on every refresh."""
        work = self.query_one("#logs-work", Static)
        posture = self.query_one("#logs-posture", Static)
        work.update(work_detail_line(work_display(detail.work), run_id))
        posture.update(
            posture_detail_line(detail.posture, attempt_number=detail.attempt_number)
        )

    def _render_selected_run(self, run_id: str, detail: RunDetail) -> None:
        """Render the selected attempt's facts and load its matching log."""
        run = detail.record
        log = self.query_one("#logs-stream", RichLog)
        summary = self.query_one("#logs-summary", Static)
        self._selected_record_key = self._record_key(run)
        self._update_selected_facts(run_id, detail)
        log.clear()
        self._tail_offset = 0
        self._tail_buffer = b""
        self._tail_path = None
        self._live_tokens = 0
        self._live_cost = 0.0
        self._live_start_ts = 0.0
        if run is None or getattr(run, "log_file", None) is None:
            summary.update("[dim]No log file[/dim]")
            return
        self._load_run_log(run, log, summary)

    def _load_run_log(self, run: Any, log: RichLog, summary: Static) -> None:
        event_path = run.log_file.with_suffix(".events.jsonl")
        from kew.tui.log_formatter import format_event

        if event_path.exists():
            # Load existing events — read once as bytes
            events: list[dict[str, Any]] = []
            raw_data = b""
            with contextlib.suppress(OSError):
                raw_data = event_path.read_bytes()

            # Only parse fully newline-terminated records
            last_nl = raw_data.rfind(b"\n")
            if last_nl != -1:
                complete = raw_data[: last_nl + 1]
                trailing = raw_data[last_nl + 1 :]
            else:
                complete = b""
                trailing = raw_data

            events = self._parse_events(complete)

            if events:
                for ev in events:
                    formatted = format_event(ev)
                    if formatted is not None:
                        log.write(formatted)
                summary.update(
                    self._build_live_summary(events)
                    if run.status == "pending"
                    else self._build_completed_summary(events)
                )
            else:
                summary.update("[dim]No events[/dim]")

            # If pending, start tailing
            if run.status == "pending":
                self._update_live_totals(events)
                self._tail_path = event_path
                self._tail_offset = len(raw_data)
                self._tail_buffer = trailing
            else:
                self._tail_path = None
        else:
            # Fallback to raw .log file
            try:
                content = run.log_file.read_text(errors="replace")
                for raw_line in content.splitlines()[-2000:]:
                    log.write(raw_line)
            except OSError:
                pass
            summary.update("[dim]Raw log (no events)[/dim]")
            self._tail_path = None

    @staticmethod
    def _parse_events(data: bytes) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
        return events

    def _update_live_totals(self, events: list[dict[str, Any]]) -> None:
        for ev in events:
            if ev.get("type") == "tokens":
                self._live_tokens = ev.get("input", 0) + ev.get("output", 0)
                self._live_cost = ev.get("cost_usd", 0.0)
            elif ev.get("type") == "started":
                self._live_start_ts = ev.get("ts", 0.0)

    @staticmethod
    def _build_live_summary(
        events: list[dict[str, Any]] | None = None,
        *,
        tokens: int = 0,
        cost: float = 0.0,
        start_ts: float = 0.0,
    ) -> str:
        """Build a one-line summary for a live worker.

        Can be called with an events list (extracts totals) or with
        pre-computed values (used by _tail_events to avoid recomputing).
        """
        from kew.tui.log_formatter import fmt_duration

        if events is not None:
            for ev in events:
                if ev.get("type") == "tokens":
                    tokens = ev.get("input", 0) + ev.get("output", 0)
                    cost = ev.get("cost_usd", 0.0)
                elif ev.get("type") == "started":
                    start_ts = ev.get("ts", 0.0)

        elapsed_str = ""
        if start_ts:
            elapsed = time.time() - start_ts
            if elapsed > 0:
                elapsed_str = f"  [dim]{fmt_duration(elapsed)}[/dim]"

        return (
            f"[{MACHINE}]\u25cf[/{MACHINE}] [bold]LIVE[/bold]  "
            f"{tokens:,} tokens  "
            f"[{MONEY}]${cost:.2f}[/{MONEY}]"
            f"{elapsed_str}"
        )

    @staticmethod
    def _build_completed_summary(events: list[dict[str, Any]]) -> str:
        """Build a one-line summary for a completed run."""
        from kew.tui.log_formatter import fmt_duration

        total_tokens = 0
        token_cost = 0.0
        finished_cost: float | None = None
        start_ts = 0.0
        end_ts = 0.0
        exit_code: int | None = None

        for ev in events:
            etype = ev.get("type", "")
            if etype == "tokens":
                total_tokens = ev.get("input", 0) + ev.get("output", 0)
                token_cost = ev.get("cost_usd", 0.0)
            elif etype == "started":
                start_ts = ev.get("ts", 0.0)
            elif etype == "finished":
                end_ts = ev.get("ts", 0.0)
                finished_cost = ev.get("cost_usd", 0.0)
                exit_code = ev.get("exit_code")

        if not events:
            return "[dim]Replay[/dim]"

        # Prefer finished event cost (authoritative), fall back to
        # accumulated token costs
        total_cost = finished_cost if finished_cost is not None else token_cost

        exit_str = LogsScreen._exit_summary(exit_code)

        # Duration
        duration_str = ""
        if start_ts and end_ts:
            duration_str = f"  {fmt_duration(end_ts - start_ts)}"

        # Token display — omit "0 tokens" when no token events exist
        token_str = f"{total_tokens:,} tokens  " if total_tokens else ""

        return (
            f"[bold]COMPLETE[/bold] {exit_str}  "
            f"{token_str}"
            f"[{MONEY}]${total_cost:.2f}[/{MONEY}]"
            f"{duration_str}"
        )

    @staticmethod
    def _exit_summary(exit_code: int | None) -> str:
        if exit_code is None:
            return "[dim]exit:?[/dim]"
        color = ATTESTED if exit_code == 0 else JUDGMENT
        return f"[{color}]exit:{exit_code}[/{color}]"

    def _refresh_selected_posture(self) -> None:
        """Refresh audit posture even when selection and log bytes stay unchanged."""
        app: Any = self.app
        if app.state is None or self._selected_run_id is None:
            return
        run_id = self._selected_run_id
        detail = self._select_detail(run_id)
        if self._record_key(detail.record) != self._selected_record_key:
            self._render_selected_run(run_id, detail)
            return
        self._update_selected_facts(run_id, detail)

    def _tail_events(self) -> None:
        """Tail new events from the selected pending worker's event file."""
        self._refresh_selected_posture()
        if self._tail_path is None:
            return
        if not self._tail_path.exists():
            return
        try:
            log = self.query_one("#logs-stream", RichLog)
            summary = self.query_one("#logs-summary", Static)
        except Exception:
            return
        try:
            with open(self._tail_path, "rb") as f:
                f.seek(self._tail_offset)
                new_data = f.read()
        except OSError:
            return
        if not new_data:
            return
        combined = self._tail_buffer + new_data
        last_nl = combined.rfind(b"\n")
        self._tail_offset += len(new_data)
        if last_nl == -1:
            self._tail_buffer = combined
            return
        complete_chunk = combined[: last_nl + 1]
        self._tail_buffer = combined[last_nl + 1 :]

        from kew.tui.log_formatter import format_event

        new_events = self._parse_events(complete_chunk)
        for event in new_events:
            formatted = format_event(event)
            if formatted is not None:
                log.write(formatted)

        if new_events:
            self._update_live_totals(new_events)
            summary.update(
                self._build_live_summary(
                    tokens=self._live_tokens,
                    cost=self._live_cost,
                    start_ts=self._live_start_ts,
                )
            )

    # -- Actions --

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_up()

    def action_scroll_stream_down(self) -> None:
        try:
            log = self.query_one("#logs-stream", RichLog)
            log.scroll_relative(y=3, animate=False)
        except NoMatches:
            pass

    def action_scroll_stream_up(self) -> None:
        try:
            log = self.query_one("#logs-stream", RichLog)
            log.scroll_relative(y=-3, animate=False)
        except NoMatches:
            pass

    def _update_breadcrumb(self) -> None:
        """Update breadcrumb to reflect lookback and active filter."""
        try:
            bc = self.query_one("#logs-breadcrumb", Static)
            lb = self.lookback
            text = f"[dim]Esc:back[/dim]   [bold]Workers[/bold]  [dim]\\[{lb}][/dim]"
            app: Any = self.app
            filt = ""
            if app.state:
                filt = app.state.filter_text.get("logs-list", "")
            if filt:
                text += f"  [dim]/{filt}[/dim]"
            bc.update(text)
        except NoMatches:
            pass

    def action_toggle_lookback(self) -> None:
        idx = _LOOKBACK_CYCLE.index(self.lookback)
        self.lookback = _LOOKBACK_CYCLE[(idx + 1) % len(_LOOKBACK_CYCLE)]
        self._update_breadcrumb()
        self._update_run_list()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_toggle_budget(self) -> None:
        from kew.tui.widgets.budget_overlay import BudgetOverlay, compute_budget_summary

        self.app.push_screen(BudgetOverlay(compute_budget_summary(self.app)))

    def action_toggle_help(self) -> None:
        from kew.tui.widgets.help_overlay import HelpOverlay

        self.app.push_screen(HelpOverlay("logs"))

    def action_quit(self) -> None:
        from kew.tui.actions import handle_quit

        handle_quit(self.app)

    def action_filter(self) -> None:
        from kew.tui.screens.filter_overlay import FilterOverlay

        app: Any = self.app
        current = app.state.filter_text.get("logs-list", "") if app.state else ""

        def on_result(value: str | None) -> None:
            if app.state is not None:
                if value:
                    app.state.filter_text["logs-list"] = value
                else:
                    app.state.filter_text.pop("logs-list", None)
                self._update_run_list()
                self._update_breadcrumb()

        app.push_screen(FilterOverlay("logs-list", current), callback=on_result)
