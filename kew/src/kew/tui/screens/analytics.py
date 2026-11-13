"""Analytics screen — full-screen fleet performance dashboard."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Static

from kew.tui.palette import (
    ATTESTED,
    FAINT,
    FG,
    JUDGMENT,
    MACHINE,
    MONEY,
    MUTED,
    SURFACE,
    SURFACE_2,
    WARN,
)

_LOOKBACK_OPTIONS: list[tuple[str, int | None]] = [
    ("today", 0),
    ("7d", 7),
    ("30d", 30),
    ("all", None),
]


class AnalyticsScreen(Screen):  # type: ignore[type-arg]
    """Full-screen analytics dashboard."""

    BINDINGS = [
        ("escape", "pop_screen", "Back"),
        ("h", "toggle_lookback", "Period"),
        ("J", "scroll_down", "Scroll\u2193"),
        ("K", "scroll_up", "Scroll\u2191"),
        ("b", "toggle_budget", "Budget"),
        ("e", "export", "Export"),
        ("P", "purge", "Purge"),
        ("question_mark", "toggle_help", "Help"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._lookback_idx: int = 1  # default to 7d
        self._refresh_timer: Timer | None = None
        self._insufficient_data: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        yield Static("", id="analytics-breadcrumb")
        yield Horizontal(
            Static("", classes="stat-card", id="stat-runs"),
            Static("", classes="stat-card", id="stat-success"),
            Static("", classes="stat-card", id="stat-avg-cost"),
            Static("", classes="stat-card", id="stat-avg-time"),
            Static("", classes="stat-card", id="stat-total-spend"),
            id="analytics-stats-row",
        )
        with Horizontal():
            with VerticalScroll(id="analytics-left-scroll"):
                yield Static("", id="analytics-left")
            with VerticalScroll(id="analytics-right-scroll"):
                yield Static("", id="analytics-right")
        yield Footer()

    def on_mount(self) -> None:
        self._schedule_refresh()
        self._refresh_timer = self.set_interval(2.0, self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        """Schedule analytics computation in a background worker."""
        self.run_worker(self._async_refresh(), exclusive=True)

    def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    # -- Actions ---------------------------------------------------------------

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_toggle_lookback(self) -> None:
        self._lookback_idx = (self._lookback_idx + 1) % len(_LOOKBACK_OPTIONS)
        self._schedule_refresh()

    def action_toggle_budget(self) -> None:
        from kew.tui.widgets.budget_overlay import BudgetOverlay, compute_budget_summary

        self.app.push_screen(BudgetOverlay(compute_budget_summary(self.app)))

    def action_toggle_help(self) -> None:
        from kew.tui.widgets.help_overlay import HelpOverlay

        self.app.push_screen(HelpOverlay("analytics"))

    def action_scroll_down(self) -> None:
        """Scroll both analytics columns down."""
        for sid in ("#analytics-left-scroll", "#analytics-right-scroll"):
            with contextlib.suppress(NoMatches):
                self.query_one(sid, VerticalScroll).scroll_relative(y=3, animate=False)

    def action_scroll_up(self) -> None:
        """Scroll both analytics columns up."""
        for sid in ("#analytics-left-scroll", "#analytics-right-scroll"):
            with contextlib.suppress(NoMatches):
                self.query_one(sid, VerticalScroll).scroll_relative(y=-3, animate=False)

    def action_export(self) -> None:
        from kew.tui.actions import handle_export

        handle_export(self.app)

    def action_purge(self) -> None:
        from kew.tui.actions import handle_purge

        handle_purge(self.app)

    def action_quit(self) -> None:
        from kew.tui.actions import handle_quit

        handle_quit(self.app)

    # -- Data refresh ----------------------------------------------------------

    def _get_since(self) -> datetime | None:
        """Compute the `since` datetime for the current lookback.

        Uses calendar-day boundaries so "7d" means the last 7 full days
        including today, not "168 hours ago".
        """
        _label, days = _LOOKBACK_OPTIONS[self._lookback_idx]
        if days is None:
            return None
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if days == 0:
            return today
        return today - timedelta(days=days - 1)

    async def _async_refresh(self) -> None:
        """Compute analytics in a background thread, then update widgets."""
        app: Any = self.app
        state = getattr(app, "state", None)
        store = getattr(state, "store", None) if state else None

        label, days = _LOOKBACK_OPTIONS[self._lookback_idx]
        since = self._get_since()

        # -- Breadcrumb (lightweight, update directly) -------------------------
        try:
            bc = self.query_one("#analytics-breadcrumb", Static)
            bc.update(
                f"[dim {FAINT}]Esc:back[/dim {FAINT}]   "
                f"[bold {FG}]Analytics[/bold {FG}]  "
                f"[{JUDGMENT}]▸[/{JUDGMENT}] [dim {MUTED}]{label}[/dim {MUTED}]"
            )
        except NoMatches:
            pass

        if store is None:
            return

        # "today" (days=0) shows 1 day of activity
        chart_days = max(days, 1) if days is not None else 0

        # Offload all store queries to a background thread
        data = await asyncio.to_thread(
            self._compute_analytics, store, since, chart_days
        )
        stats, failures, models, complexities, activity = data

        # -- Update widgets (back on UI thread) --------------------------------
        self._insufficient_data = stats.get("runs", 0) < 5
        self._update_stat_cards(stats)

        # -- Insufficient data warning (status bar) ----------------------------
        warning = self._render_insufficient_data_warning()
        with contextlib.suppress(NoMatches):
            self.query_one("#status-bar", Static).update(warning)

        # -- Left column: daily activity + budget gauges -----------------------
        left_parts: list[str] = []
        left_parts.append(self._render_daily_activity(activity))
        left_parts.append("")
        left_parts.append(self._render_budget_gauges(app))
        with contextlib.suppress(NoMatches):
            self.query_one("#analytics-left", Static).update("\n".join(left_parts))

        # -- Right column: failures + model + complexity -----------------------
        right_parts: list[str] = []
        right_parts.append(self._render_failure_breakdown(failures))
        right_parts.append("")
        right_parts.append(self._render_model_breakdown(models, app))
        right_parts.append("")
        right_parts.append(self._render_complexity_breakdown(complexities))
        with contextlib.suppress(NoMatches):
            self.query_one("#analytics-right", Static).update("\n".join(right_parts))

    @staticmethod
    def _compute_analytics(
        store: Any, since: datetime | None, chart_days: int
    ) -> tuple[
        dict[str, Any],
        list[tuple[str, int]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[tuple[str, int, int]],
    ]:
        """Run all analytics queries (called in background thread)."""
        from kew.analytics import (
            aggregate_stats,
            complexity_breakdown,
            daily_activity,
            failure_breakdown,
            model_breakdown,
        )

        stats = aggregate_stats(store, since=since)
        fails = failure_breakdown(store, since=since)
        models = model_breakdown(store, since=since)
        complexities = complexity_breakdown(store, since=since)
        activity = daily_activity(store, days=chart_days) if chart_days > 0 else []
        return stats, fails, models, complexities, activity

    def _render_insufficient_data_warning(self) -> str:
        """Return a warning banner when data is sparse, or empty string."""
        if not self._insufficient_data:
            return ""
        return (
            f"[bold {WARN}]  ◈ Insufficient data[/bold {WARN}]"
            f"[dim {MUTED}] — statistics may not be meaningful "
            f"(< 5 completed runs)[/dim {MUTED}]"
        )

    # -- Rendering helpers -----------------------------------------------------

    def _update_stat_cards(self, stats: dict[str, Any]) -> None:
        runs = stats.get("runs", 0)
        rate = stats.get("success_rate", 0.0)
        avg_cost = stats.get("avg_cost", 0.0)
        avg_dur = stats.get("avg_duration", 0.0)
        total = stats.get("total_spend", 0.0)

        avg_min = avg_dur / 60 if avg_dur else 0.0

        # Success rate color
        if rate >= 80:
            rate_color = ATTESTED
        elif rate >= 60:
            rate_color = WARN
        else:
            rate_color = JUDGMENT

        # If < 5 runs, show dash instead of misleading percentage
        rate_display = f"{rate:.0f}%" if runs >= 5 else "\u2014"

        rc = rate_color
        cy = MACHINE
        yl = MONEY
        pk = JUDGMENT
        cards: list[tuple[str, str, str, str]] = [
            ("stat-runs", "RUNS", f"[bold {cy}]{runs}[/bold {cy}]", "◈"),
            ("stat-success", "SUCCESS", f"[bold {rc}]{rate_display}[/bold {rc}]", "◉"),
            (
                "stat-avg-cost",
                "AVG COST",
                f"[bold {yl}]${avg_cost:.2f}[/bold {yl}]",
                "◆",
            ),
            (
                "stat-avg-time",
                "AVG TIME",
                f"[bold {cy}]{avg_min:.1f}m[/bold {cy}]",
                "⏱",
            ),
            (
                "stat-total-spend",
                "TOTAL",
                f"[bold {yl}]${total:.2f}[/bold {yl}]",
                "▲",
            ),
        ]
        for widget_id, label_text, value_text, icon in cards:
            try:
                w = self.query_one(f"#{widget_id}", Static)
                w.update(
                    f"[{pk}]{icon}[/{pk}]\n"
                    f"[dim {MUTED}]{label_text}[/dim {MUTED}]\n"
                    f"{value_text}"
                )
            except NoMatches:
                pass

    @staticmethod
    def _section_header(title: str) -> str:
        """Render a section header with decorative line."""
        return (
            f"[{JUDGMENT}]╺[/{JUDGMENT}] "
            f"[bold {MACHINE}]{title.upper()}[/bold {MACHINE}]\n"
            f"  [dim {SURFACE_2}]{'━' * 38}[/dim {SURFACE_2}]"
        )

    def _render_daily_activity(self, activity: list[tuple[str, int, int]]) -> str:
        lines: list[str] = [self._section_header("Daily Activity")]
        if not activity:
            lines.append(f"  [dim {FAINT}]No daily data for this period[/dim {FAINT}]")
            return "\n".join(lines)

        max_total = max((s + f) for _, s, f in activity) if activity else 1
        if max_total == 0:
            max_total = 1
        bar_width = 30

        for day_label, successes, failures in activity:
            s_len = int(successes / max_total * bar_width) if max_total else 0
            f_len = int(failures / max_total * bar_width) if max_total else 0
            empty = bar_width - s_len - f_len
            bar = (
                f"[{ATTESTED}]{'▓' * s_len}[/{ATTESTED}]"
                f"[{JUDGMENT}]{'▓' * f_len}[/{JUDGMENT}]"
                f"[dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
            )
            total = successes + failures
            lines.append(
                f"  [dim {MUTED}]{day_label}[/dim {MUTED}]  {bar} [bold]{total}[/bold]"
            )

        # Legend
        lines.append("")
        lines.append(
            f"  [{ATTESTED}]▓[/{ATTESTED}] [dim {MUTED}]success[/dim {MUTED}]  "
            f"[{JUDGMENT}]▓[/{JUDGMENT}] [dim {MUTED}]failed[/dim {MUTED}]"
        )

        return "\n".join(lines)

    def _render_budget_gauges(self, app: Any) -> str:
        state = getattr(app, "state", None)
        config = getattr(app, "config", None)
        if not state or not config:
            return (
                self._section_header("Budget")
                + f"\n  [dim {FAINT}]No budget data[/dim {FAINT}]"
            )

        lines: list[str] = [self._section_header("Budget")]
        gauges: list[tuple[str, float, float]] = [
            ("Daily", state.daily_spend, config.budget.daily_usd),
            ("Weekly", state.weekly_spend(), config.budget.weekly_usd),
            ("Per Issue", state.per_issue_avg(), config.budget.per_issue_usd),
        ]
        for label, spent, limit in gauges:
            lines.append(self._format_gauge(label, spent, limit))

        # Burn rate footer
        burn = state.burn_rate()
        hrs = state.hours_until_limit(config.budget.daily_usd)
        lines.append("")
        burn_line = (
            f"  [dim {MUTED}]Burn rate[/dim {MUTED}]  [{MONEY}]${burn:.2f}/hr[/{MONEY}]"
        )
        if hrs < float("inf"):
            tc = JUDGMENT if hrs < 2 else MONEY
            burn_line += (
                f"  [dim {MUTED}]·[/dim {MUTED}]  [{tc}]~{hrs:.1f}h to limit[/{tc}]"
            )
        lines.append(burn_line)

        return "\n".join(lines)

    @staticmethod
    def _format_gauge(label: str, spent: float, limit: float) -> str:
        width = 24
        ratio = 0.0 if limit <= 0 else min(spent / limit, 1.0)
        filled = int(ratio * width)
        empty = width - filled
        pct = int(ratio * 100)

        if ratio >= 0.9:
            color = JUDGMENT
        elif ratio >= 0.7:
            color = MONEY
        else:
            color = MACHINE

        bar = (
            f"[dim {SURFACE_2}]▐[/dim {SURFACE_2}]"
            f"[{color}]{'█' * filled}[/{color}]"
            f"[dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
            f"[dim {SURFACE_2}]▌[/dim {SURFACE_2}]"
        )
        return (
            f"  [{MUTED}]{label:<10}[/{MUTED}] {bar}"
            f" [{color}]{pct:>3}%[/{color}]"
            f"  [dim {FAINT}]${spent:.2f} / ${limit:.0f}[/dim {FAINT}]"
        )

    def _render_failure_breakdown(self, failures: list[tuple[str, int]]) -> str:
        lines: list[str] = [self._section_header("Failure Breakdown")]
        if not failures:
            lines.append(f"  [dim {FAINT}]No failures \u2014 clean slate[/dim {FAINT}]")
            return "\n".join(lines)

        total = sum(c for _, c in failures) or 1
        max_count = max((c for _, c in failures), default=1) or 1
        bar_width = 20
        for category, count in failures:
            bar_len = int(count / max_count * bar_width) if max_count else 0
            empty = bar_width - bar_len
            pct = int(count / total * 100)
            bar = (
                f"[{JUDGMENT}]{'▓' * bar_len}[/{JUDGMENT}]"
                f"[dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
            )
            lines.append(
                f"  [{FG}]{category:<18}[/{FG}] {bar}"
                f" [{JUDGMENT}]{count:>3}[/{JUDGMENT}]"
                f" [dim {FAINT}]{pct}%[/dim {FAINT}]"
            )

        return "\n".join(lines)

    def _render_model_breakdown(self, models: list[dict[str, Any]], app: Any) -> str:
        lines: list[str] = [self._section_header("By Model")]
        if not models:
            lines.append(f"  [dim {FAINT}]No data[/dim {FAINT}]")
            return "\n".join(lines)

        config = getattr(app, "config", None)
        lifecycle_map = (
            config.model_eol.snapshot_models
            if config and hasattr(config, "model_eol")
            else {}
        )

        for m in models:
            model_name = m["model"]
            runs = m["runs"]
            rate = m["success_rate"]
            avg_cost = m["avg_cost"]

            lifecycle_label = self._lifecycle_label(lifecycle_map.get(model_name))

            line = self._model_line(model_name, runs, rate, avg_cost, lifecycle_label)
            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _lifecycle_label(entry: Any | None) -> str:
        if entry is None or entry.status not in (
            "retired",
            "retiring",
            "scheduled",
            "watch",
        ):
            return ""
        lc = JUDGMENT if entry.status == "retired" else WARN
        label = "watching" if entry.status == "watch" else entry.status
        return f"  [{lc}]{label}[/{lc}]"

    @staticmethod
    def _model_line(
        name: str, runs: int, rate: float, avg_cost: float, lifecycle_label: str
    ) -> str:
        rate_display = f"{rate:.0f}%" if runs >= 5 else "\u2014"
        if runs >= 5:
            bar_fill = min(int(rate / 100 * 8), 8)
            bar_color = ATTESTED if rate >= 80 else WARN if rate >= 60 else JUDGMENT
            mini_bar = (
                f"[{bar_color}]{'▮' * bar_fill}[/{bar_color}]"
                f"[dim {SURFACE_2}]{'▯' * (8 - bar_fill)}[/dim {SURFACE_2}]"
            )
        else:
            mini_bar = f"[dim {SURFACE_2}]{'▯' * 8}[/dim {SURFACE_2}]"
        return (
            f"  [{FG}]{name:<22}[/{FG}]"
            f" [dim {FAINT}]{runs:>3} runs[/dim {FAINT}]"
            f"  {mini_bar} {rate_display}"
            f"  [{MONEY}]${avg_cost:.2f}[/{MONEY}]"
            f"{lifecycle_label}"
        )

    def _render_complexity_breakdown(self, complexities: list[dict[str, Any]]) -> str:
        color_map = {
            "simple": MACHINE,
            "medium": WARN,
            "complex": JUDGMENT,
        }
        icon_map = {"simple": "▸", "medium": "▸▸", "complex": "▸▸▸"}
        lines: list[str] = [self._section_header("By Complexity")]
        if not complexities:
            lines.append(f"  [dim {FAINT}]No data[/dim {FAINT}]")
            return "\n".join(lines)

        for c in complexities:
            cx = c["complexity"]
            runs = c["runs"]
            rate = c["success_rate"]
            avg_cost = c["avg_cost"]
            avg_dur = c["avg_duration"]
            avg_min = avg_dur / 60 if avg_dur else 0.0
            color = color_map.get(cx, FAINT)
            icon = icon_map.get(cx, "▸")
            rate_display = f"{rate:.0f}%" if runs >= 5 else "\u2014"

            # Mini success bar (6 chars)
            if runs >= 5:
                bar_fill = int(rate / 100 * 6)
                bar_color = ATTESTED if rate >= 80 else WARN if rate >= 60 else JUDGMENT
                mini_bar = (
                    f"[{bar_color}]{'▮' * bar_fill}[/{bar_color}]"
                    f"[dim {SURFACE_2}]{'▯' * (6 - bar_fill)}[/dim {SURFACE_2}]"
                )
            else:
                mini_bar = f"[dim {SURFACE_2}]{'▯' * 6}[/dim {SURFACE_2}]"

            lines.append(
                f"  [{color}]{icon:<4}[/{color}]"
                f" [{color}]{cx:<8}[/{color}]"
                f" [dim {FAINT}]{runs:>3} runs[/dim {FAINT}]"
                f"  {mini_bar} {rate_display}"
                f"  [{MONEY}]${avg_cost:.2f}[/{MONEY}]"
                f"  [dim {FAINT}]{avg_min:.1f}m[/dim {FAINT}]"
            )

        return "\n".join(lines)
