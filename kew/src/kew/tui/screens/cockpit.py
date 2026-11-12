"""Cockpit screen — 2-column home layout."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static


class _FocusableStatic(Static):
    """A Static widget that can receive focus."""

    can_focus = True


class CockpitScreen(Screen):  # type: ignore[type-arg]
    """2x2 home view: Queue+Workers (top), Reviews+Analytics (bottom)."""

    BINDINGS = [
        ("1", "switch_queue", "Queue"),
        ("2", "switch_workers", "Workers"),
        ("3", "switch_reviews", "Reviews"),
        ("4", "switch_analytics", "Analytics"),
        ("5", "switch_holds", "Holds & Queue"),
        ("tab", "cycle_focus", "Next panel"),
        ("d", "dispatch", "Dispatch"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("x", "kill_worker", "Kill"),
        ("t", "toggle_ready", "Ready"),
        ("r", "retry", "Retry"),
        ("R", "review_pr", "Review PR"),
        ("M", "resolve_pr", "Resolve Conflicts"),
        ("A", "approve", "Approve"),
        ("b", "toggle_budget", "Budget"),
        ("question_mark", "toggle_help", "Help"),
        ("slash", "filter", "Filter"),
        ("escape", "escape_hint", "Esc"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        with Horizontal():
            with Vertical(classes="panel-box"):
                with Vertical():
                    yield Static(
                        "[1] QUEUE",
                        id="queue-header",
                        classes="panel-header",
                    )
                    yield ListView(id="queue-panel", classes="panel")
                with Vertical():
                    yield Static(
                        "[3] REVIEWS",
                        id="reviews-header",
                        classes="panel-header",
                    )
                    yield ListView(id="reviews-panel", classes="panel")
            with Vertical(classes="panel-box"):
                with Vertical():
                    yield Static(
                        "[2] WORKERS",
                        id="workers-header",
                        classes="panel-header",
                    )
                    yield ListView(id="workers-panel", classes="panel")
                with Vertical():
                    yield Static(
                        "[4] ANALYTICS",
                        id="analytics-header",
                        classes="panel-header",
                    )
                    yield _FocusableStatic(
                        "[dim]Loading...[/dim]",
                        id="analytics-panel",
                    )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a queue/review item opens the expanded view."""
        focused = self.focused
        if isinstance(focused, ListView):
            if focused.id == "queue-panel":
                self.action_switch_queue()
            elif focused.id == "reviews-panel":
                self.action_switch_reviews()

    def action_switch_queue(self) -> None:
        from kew.tui.screens.expanded import ExpandedScreen

        self.app.push_screen(ExpandedScreen(panel="queue"))

    def action_switch_workers(self) -> None:
        from kew.tui.screens.logs import LogsScreen

        self.app.push_screen(LogsScreen())

    def action_switch_reviews(self) -> None:
        from kew.tui.screens.expanded import ExpandedScreen

        self.app.push_screen(ExpandedScreen(panel="reviews"))

    def action_switch_analytics(self) -> None:
        from kew.tui.screens.analytics import AnalyticsScreen

        self.app.push_screen(AnalyticsScreen())

    def action_switch_holds(self) -> None:
        from kew.tui.screens.holds_queue import HoldsQueueScreen

        self.app.push_screen(HoldsQueueScreen())

    _FOCUS_ORDER = ("queue-panel", "workers-panel", "reviews-panel", "analytics-panel")

    def action_cycle_focus(self) -> None:
        current_id = getattr(self.focused, "id", None)
        try:
            idx = self._FOCUS_ORDER.index(current_id)
            next_id = self._FOCUS_ORDER[(idx + 1) % len(self._FOCUS_ORDER)]
        except ValueError:
            next_id = self._FOCUS_ORDER[0]
        widget = self.query_one(f"#{next_id}")
        widget.focus()

    def action_dispatch(self) -> None:
        """Dispatch the selected queue issue (ready items only)."""
        focused = self.focused
        if not (isinstance(focused, ListView) and focused.id == "queue-panel"):
            return
        from kew.tui.selection import is_selected_held, is_selected_ready

        if is_selected_held(self):
            self.app.notify("Approve it first (press A)", severity="warning", timeout=3)
            return
        if not is_selected_ready(self):
            self.app.notify("Mark as ready first", severity="warning", timeout=3)
            return
        from kew.tui.actions import handle_dispatch

        handle_dispatch(self.app)

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_up()

    def action_kill_worker(self) -> None:
        """Kill the selected worker (workers panel only)."""
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "workers-panel":
            from kew.tui.actions import handle_kill

            handle_kill(self.app)

    def action_toggle_ready(self) -> None:
        """Toggle ready label on selected queue item."""
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "queue-panel":
            from kew.tui.actions import handle_toggle_ready

            handle_toggle_ready(self.app)

    def action_retry(self) -> None:
        """Retry the selected run."""
        from kew.tui.actions import handle_retry

        handle_retry(self.app)

    def action_review_pr(self) -> None:
        """Dispatch review on selected PR (only when reviews panel focused)."""
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "reviews-panel":
            from kew.tui.actions import handle_review

            handle_review(self.app)

    def action_resolve_pr(self) -> None:
        """Dispatch conflict resolution on selected PR (reviews panel only)."""
        focused = self.focused
        if isinstance(focused, ListView) and focused.id == "reviews-panel":
            from kew.tui.actions import handle_resolve

            handle_resolve(self.app)

    def action_escape_hint(self) -> None:
        """Hint that q is the quit key on the root screen."""
        self.app.notify("Press q to quit", timeout=2)

    def action_approve(self) -> None:
        """Approve/decline the selected needs-approval held issue."""
        focused = self.focused
        if not (isinstance(focused, ListView) and focused.id == "queue-panel"):
            return
        from kew.tui.selection import is_selected_held

        if not is_selected_held(self):
            return
        from kew.tui.actions import handle_approve

        handle_approve(self.app)

    def action_toggle_budget(self) -> None:
        from kew.tui.widgets.budget_overlay import BudgetOverlay, compute_budget_summary

        self.app.push_screen(BudgetOverlay(compute_budget_summary(self.app)))

    def action_toggle_help(self) -> None:
        from kew.tui.widgets.help_overlay import HelpOverlay

        self.app.push_screen(HelpOverlay("cockpit"))

    def action_quit(self) -> None:
        from kew.tui.actions import handle_quit

        handle_quit(self.app)

    def action_filter(self) -> None:
        from kew.tui.screens.filter_overlay import FilterOverlay

        app: Any = self.app
        focused = self.focused
        panel_id = getattr(focused, "id", "queue-panel") or "queue-panel"
        current = app.state.filter_text.get(panel_id, "") if app.state else ""

        def on_result(value: str | None) -> None:
            if app.state is not None:
                if value:
                    app.state.filter_text[panel_id] = value
                else:
                    app.state.filter_text.pop(panel_id, None)
                app._update_widgets()

        app.push_screen(FilterOverlay(panel_id, current), callback=on_result)
