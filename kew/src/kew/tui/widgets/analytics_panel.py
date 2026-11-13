"""Compact analytics widget for the cockpit quadrant."""

from __future__ import annotations

from typing import Any

from kew.tui.palette import (
    ATTESTED,
    FAINT,
    JUDGMENT,
    MACHINE,
    MONEY,
    MUTED,
    SURFACE,
    WARN,
)


def _mini_bar(ratio: float, width: int = 10) -> str:
    """Render a compact inline bar."""
    clamped = max(0.0, min(ratio, 1.0))
    filled = int(clamped * width)
    empty = width - filled
    if ratio >= 0.9:
        color = JUDGMENT
    elif ratio >= 0.7:
        color = MONEY
    else:
        color = MACHINE
    return (
        f"[{color}]{'▓' * filled}[/{color}][dim {SURFACE}]{'░' * empty}[/dim {SURFACE}]"
    )


def format_analytics_compact(
    stats: dict[str, Any],
    *,
    daily_budget: float | None = None,
) -> str:
    """Format analytics stats as Rich markup for the cockpit Static widget."""
    runs = stats.get("runs", 0)
    rate = stats.get("success_rate", 0.0)
    avg_cost = stats.get("avg_cost", 0.0)
    avg_dur = stats.get("avg_duration", 0.0)
    total = stats.get("total_spend", 0.0)

    avg_min = avg_dur / 60 if avg_dur else 0.0

    if rate >= 80:
        rate_color = ATTESTED
    elif rate >= 60:
        rate_color = WARN
    else:
        rate_color = JUDGMENT

    dm = f"dim {MUTED}"  # dim muted color
    rc = rate_color
    yl = MONEY
    lines = [
        f"[{dm}]Today[/{dm}]       [{rc}]{runs} runs[/{rc}]",
        f"[{dm}]Success[/{dm}]     [{rc}]{rate:.0f}%[/{rc}]",
        f"[{dm}]Avg cost[/{dm}]    [{yl}]${avg_cost:.2f}[/{yl}]",
        f"[{dm}]Avg time[/{dm}]     {avg_min:.1f}m",
    ]
    if daily_budget is not None and daily_budget > 0:
        budget_ratio = total / daily_budget if daily_budget > 0 else 0.0
        lines.append(
            f"[dim {MUTED}]Budget[/dim {MUTED}]"
            f"      [{MONEY}]${total:.2f}[/{MONEY}]"
            f" [dim {FAINT}]/ ${daily_budget:.2f}[/dim {FAINT}]"
        )
        lines.append(f"  {_mini_bar(budget_ratio)}")
    else:
        lines.append(
            f"[dim {MUTED}]Spent[/dim {MUTED}]       [{MONEY}]${total:.2f}[/{MONEY}]"
        )

    if runs < 5:
        lines.append(f"[{WARN}]◈ Insufficient data[/{WARN}]")

    return "\n".join(lines)
