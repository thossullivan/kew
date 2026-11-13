"""Budget overlay — modal showing spend, limits, and burn rate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from kew.tui.palette import (
    ATTESTED,
    FAINT,
    JUDGMENT,
    MACHINE,
    MONEY,
    MUTED,
    SURFACE,
    SURFACE_2,
)

if TYPE_CHECKING:
    from kew.budget import BudgetStatus


def _ratio_color(spend: float, limit: float) -> str:
    if limit <= 0:
        return ATTESTED
    ratio = spend / limit
    if ratio >= 0.9:
        return JUDGMENT
    if ratio >= 0.6:
        return MONEY
    return ATTESTED


def format_budget_status_lines(status: BudgetStatus) -> str:
    """Format BudgetStatus as Rich markup lines for the override modal."""
    limits = [
        ("Daily", status.daily_spent_usd, status.daily_limit_usd),
        ("Weekly", status.weekly_spent_usd, status.weekly_limit_usd),
        ("Per-issue", status.issue_spent_usd, status.issue_limit_usd),
    ]
    lines: list[str] = []
    for label, spent, limit in limits:
        exceeded = spent > limit
        if limit > 0:
            pct = int(spent / limit * 100)
            color = _ratio_color(spent, limit)
        else:
            pct = 100 if exceeded else 0
            color = JUDGMENT if exceeded else ATTESTED
        marker = (
            f"[{JUDGMENT}]✗[/{JUDGMENT}]" if exceeded else f"[{ATTESTED}]✓[/{ATTESTED}]"
        )
        # Mini gauge (12 chars)
        ratio = 0.0 if limit <= 0 else min(spent / limit, 1.0)
        filled = int(ratio * 12)
        empty = 12 - filled
        gauge = (
            f"[{color}]{'█' * filled}[/{color}]"
            f"[dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
        )
        lines.append(
            f"  {marker} [{MUTED}]{label:<10}[/{MUTED}]"
            f" [{color}]${spent:.2f}[/{color}]"
            f" [dim {FAINT}]/ ${limit:.2f}[/dim {FAINT}]"
            f"  [{color}]{pct:>3}%[/{color}]"
            f"  {gauge}"
        )
    return "\n".join(lines)


def _gauge_bar(spent: float, limit: float, width: int = 20) -> str:
    """Render a small horizontal gauge bar with brackets."""
    ratio = 0.0 if limit <= 0 else min(spent / limit, 1.0)
    filled = int(ratio * width)
    empty = width - filled
    color = _ratio_color(spent, limit)
    return (
        f"[dim {SURFACE_2}]▐[/dim {SURFACE_2}]"
        f"[{color}]{'█' * filled}[/{color}]"
        f"[dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
        f"[dim {SURFACE_2}]▌[/dim {SURFACE_2}]"
    )


def format_budget_summary(
    *,
    daily_spend: float,
    daily_limit: float,
    weekly_spend: float,
    weekly_limit: float,
    per_issue_avg: float,
    per_issue_limit: float,
    burn_rate_per_hour: float,
    hours_until_daily_limit: float,
) -> str:
    dc = _ratio_color(daily_spend, daily_limit)
    wc = _ratio_color(weekly_spend, weekly_limit)
    ic = _ratio_color(per_issue_avg, per_issue_limit)

    lines = [
        f"[bold {MACHINE}]◈ Budget[/bold {MACHINE}]",
        f"  [dim {SURFACE_2}]{'━' * 34}[/dim {SURFACE_2}]",
        "",
        (
            f"  [{MUTED}]Daily[/{MUTED}]        "
            f"[{dc}]${daily_spend:.2f}[/{dc}]"
            f" [dim {FAINT}]/ ${daily_limit:.2f}[/dim {FAINT}]"
        ),
        f"  {_gauge_bar(daily_spend, daily_limit)}",
        "",
        (
            f"  [{MUTED}]Weekly[/{MUTED}]       "
            f"[{wc}]${weekly_spend:.2f}[/{wc}]"
            f" [dim {FAINT}]/ ${weekly_limit:.2f}[/dim {FAINT}]"
        ),
        f"  {_gauge_bar(weekly_spend, weekly_limit)}",
        "",
        (
            f"  [{MUTED}]Per-issue avg[/{MUTED}]"
            f"  [{ic}]${per_issue_avg:.2f}[/{ic}]"
            f" [dim {FAINT}]/ ${per_issue_limit:.2f}[/dim {FAINT}]"
        ),
        f"  {_gauge_bar(per_issue_avg, per_issue_limit)}",
        "",
        f"  [dim {SURFACE_2}]{'━' * 34}[/dim {SURFACE_2}]",
        (
            f"  [dim {MUTED}]Burn rate[/dim {MUTED}]"
            f"  [{MONEY}]${burn_rate_per_hour:.2f}/hr[/{MONEY}]"
        ),
    ]
    if hours_until_daily_limit < float("inf"):
        tc = JUDGMENT if hours_until_daily_limit < 2 else MONEY
        lines.append(
            f"  [dim {MUTED}]Daily limit in[/dim {MUTED}]"
            f" [{tc}]~{hours_until_daily_limit:.1f}h[/{tc}]"
            f" [dim {FAINT}]at current pace[/dim {FAINT}]"
        )
    else:
        lines.append(f"  [dim {FAINT}]No active spend[/dim {FAINT}]")
    return "\n".join(lines)


def compute_budget_summary(app: object) -> str:
    """Build budget summary from app state, with safe defaults."""
    state = getattr(app, "state", None)
    config = getattr(app, "config", None)
    if state and config:
        return format_budget_summary(
            daily_spend=state.daily_spend,
            daily_limit=config.budget.daily_usd,
            weekly_spend=state.weekly_spend(),
            weekly_limit=config.budget.weekly_usd,
            per_issue_avg=state.per_issue_avg(),
            per_issue_limit=config.budget.per_issue_usd,
            burn_rate_per_hour=state.burn_rate(),
            hours_until_daily_limit=state.hours_until_limit(config.budget.daily_usd),
        )
    return format_budget_summary(
        daily_spend=0.0,
        daily_limit=50.0,
        weekly_spend=0.0,
        weekly_limit=200.0,
        per_issue_avg=0.0,
        per_issue_limit=15.0,
        burn_rate_per_hour=0.0,
        hours_until_daily_limit=float("inf"),
    )


class BudgetOverlay(ModalScreen):  # type: ignore[type-arg]
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("b", "dismiss", "Close"),
    ]

    def __init__(self, summary: str) -> None:
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        yield Static(self.summary, id="budget-content")
