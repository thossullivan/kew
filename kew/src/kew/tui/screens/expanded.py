"""Expanded screen — full-screen single panel view."""

from __future__ import annotations

from typing import Any, Literal

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static


class ExpandedScreen(Screen):  # type: ignore[type-arg]
    """Full-screen view for a single panel."""

    BINDINGS = [
        ("escape", "pop_screen", "Back"),
        ("1", "switch_queue", "Queue"),
        ("2", "switch_workers", "Workers"),
        ("3", "switch_reviews", "Reviews"),
        ("4", "switch_analytics", "Analytics"),
        ("5", "switch_holds", "Holds & Queue"),
        ("d", "dispatch", "Dispatch"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("J", "scroll_detail_down", "Detail\u2193"),
        ("K", "scroll_detail_up", "Detail\u2191"),
        ("t", "toggle_ready", "Ready"),
        ("r", "retry", "Retry"),
        ("R", "review_pr", "Review PR"),
        ("M", "resolve_pr", "Resolve Conflicts"),
        ("A", "approve", "Approve"),
        ("b", "toggle_budget", "Budget"),
        ("question_mark", "toggle_help", "Help"),
        ("slash", "filter", "Filter"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        panel: Literal["queue", "reviews"] = "queue",
    ) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        from kew.tui.widgets.detail_pane import DetailPane

        yield Static("", id="status-bar")
        yield Static(
            f"[dim]Esc:back[/dim]   [bold]{self.panel.title()}[/bold]",
            id="expanded-breadcrumb",
        )
        if self.panel == "queue":
            with Horizontal():
                yield ListView(id="queue-panel", classes="panel queue-list-expanded")
                yield DetailPane(id="detail-pane")
        elif self.panel == "reviews":
            with Horizontal():
                yield ListView(id="reviews-panel", classes="panel queue-list-expanded")
                yield DetailPane(id="detail-pane")
        yield Footer()

    def on_mount(self) -> None:
        """Ensure the list panel has focus on screen mount."""
        lv = self._get_panel_listview()
        if lv is not None:
            lv.focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Update detail pane when queue/review selection changes."""
        if self.panel in ("queue", "reviews") and hasattr(
            self.app, "_update_detail_pane"
        ):
            self.app._update_detail_pane()

    def action_scroll_detail_down(self) -> None:
        """Scroll the detail pane down while keeping focus on the queue list."""
        if self.panel not in ("queue", "reviews"):
            return
        try:
            from kew.tui.widgets.detail_pane import DetailPane

            detail = self.query_one("#detail-pane", DetailPane)
            detail.scroll_relative(y=3, animate=False)
        except NoMatches:
            pass

    def action_scroll_detail_up(self) -> None:
        """Scroll the detail pane up while keeping focus on the queue list."""
        if self.panel not in ("queue", "reviews"):
            return
        try:
            from kew.tui.widgets.detail_pane import DetailPane

            detail = self.query_one("#detail-pane", DetailPane)
            detail.scroll_relative(y=-3, animate=False)
        except NoMatches:
            pass

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_switch_queue(self) -> None:
        self.app.switch_screen(ExpandedScreen(panel="queue"))

    def action_switch_workers(self) -> None:
        from kew.tui.screens.logs import LogsScreen

        self.app.push_screen(LogsScreen())

    def action_switch_reviews(self) -> None:
        self.app.switch_screen(ExpandedScreen(panel="reviews"))

    def action_switch_analytics(self) -> None:
        from kew.tui.screens.analytics import AnalyticsScreen

        self.app.push_screen(AnalyticsScreen())

    def action_switch_holds(self) -> None:
        from kew.tui.screens.holds_queue import HoldsQueueScreen

        self.app.push_screen(HoldsQueueScreen())

    def action_dispatch(self) -> None:
        """Dispatch the selected queue issue (ready items only)."""
        if self.panel != "queue":
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

    def _get_panel_listview(self) -> ListView | None:
        """Return the primary list panel for this screen."""
        panel_id = "reviews-panel" if self.panel == "reviews" else "queue-panel"
        try:
            return self.query_one(f"#{panel_id}", ListView)
        except NoMatches:
            return None

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            old_idx = focused.index
            focused.action_cursor_down()
            if focused.index == old_idx and hasattr(self.app, "_update_detail_pane"):
                self.app._update_detail_pane()
        else:
            lv = self._get_panel_listview()
            if lv is not None:
                lv.focus()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            old_idx = focused.index
            focused.action_cursor_up()
            if focused.index == old_idx and hasattr(self.app, "_update_detail_pane"):
                self.app._update_detail_pane()
        else:
            lv = self._get_panel_listview()
            if lv is not None:
                lv.focus()

    def action_toggle_ready(self) -> None:
        """Toggle ready label on selected queue item."""
        if self.panel == "queue":
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

    def action_approve(self) -> None:
        """Approve/decline the selected needs-approval held issue."""
        if self.panel != "queue":
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

        self.app.push_screen(HelpOverlay("expanded"))

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
                self._update_breadcrumb()

        app.push_screen(FilterOverlay(panel_id, current), callback=on_result)

    def _update_breadcrumb(self) -> None:
        """Update breadcrumb to reflect active filter."""
        app: Any = self.app
        panel_id = f"{self.panel}-panel"
        filt = ""
        if app.state:
            filt = app.state.filter_text.get(panel_id, "")
        try:
            bc = self.query_one("#expanded-breadcrumb", Static)
            base = f"[dim]Esc:back[/dim]   [bold]{self.panel.title()}[/bold]"
            if filt:
                bc.update(f"{base}  [dim]/{filt}[/dim]")
            else:
                bc.update(base)
        except NoMatches:
            pass
