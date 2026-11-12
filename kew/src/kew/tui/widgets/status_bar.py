"""Top status bar widget — project name, spend, workers, queue count."""

from __future__ import annotations

from kew.tui.formatting import format_tokens
from kew.tui.palette import ACCENT_FG, ATTESTED, FG, JUDGMENT, MACHINE, MONEY


def _spend_color(spend: float, limit: float) -> str:
    if limit <= 0:
        return ATTESTED
    ratio = spend / limit
    if ratio >= 0.9:
        return JUDGMENT
    if ratio >= 0.6:
        return MONEY
    return ATTESTED


def format_status_bar(
    *,
    project: str,
    daily_spend: float,
    daily_limit: float,
    worker_count: int,
    queue_ready_count: int,
    queue_backlog_count: int,
    active_tokens: int = 0,
    active_cost: float = 0.0,
    fleet_stopped: dict[str, str] | None = None,
) -> str:
    total_spend = daily_spend + active_cost
    sc = _spend_color(total_spend, daily_limit)
    workers_color = MACHINE if worker_count > 0 else "dim"
    queue_color = MACHINE if queue_ready_count > 0 else "dim"
    backlog_color = "dim"
    spend = f"[{sc}]~${total_spend:.2f}[/{sc}] [dim]/ ${daily_limit:.2f} daily[/dim]"
    workers = f"[{workers_color}]{worker_count}[/{workers_color}] [dim]workers[/dim]"
    ready = f"[{queue_color}]{queue_ready_count}[/{queue_color}] [dim]ready[/dim]"
    backlog = (
        f"[{backlog_color}]{queue_backlog_count}[/{backlog_color}] [dim]backlog[/dim]"
    )
    sep = "  [dim]|[/dim]  "
    result = (
        f" [bold]kew[/bold]  [bold {FG}]{project}[/bold {FG}]"
        f"{sep}{spend}{sep}{workers}{sep}{ready}  {backlog}"
    )
    if active_tokens > 0:
        tok_str = format_tokens(active_tokens)
        result += f"  [dim]|[/dim]  [{MONEY}]{tok_str}[/{MONEY}] [dim]tokens[/dim]"
    if fleet_stopped is not None:
        from kew.fleet import format_relative_time

        reason = fleet_stopped.get("reason") or "no reason given"
        when = format_relative_time(fleet_stopped.get("stopped_at", ""))
        # The kill switch is the Judgment signal at its loudest — an accent
        # fill, with --accent-fg for the text sitting on it.
        fill = f"bold {ACCENT_FG} on {JUDGMENT}"
        banner = (
            f"[{fill}] FLEET STOPPED — {reason}"
            f" ({when}) — kew resume to re-enable [/{fill}]"
        )
        result = f"{banner}{result}"
    return result
