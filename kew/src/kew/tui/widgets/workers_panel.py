"""Workers panel — list of running agents with progress."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kew.ids import item_ref_or_num
from kew.run_store import RunRecord
from kew.tui.formatting import format_tokens
from kew.tui.palette import ATTESTED, FG, JUDGMENT, MACHINE, WARN


def _format_time_ago(dt: datetime, now: datetime) -> str:
    """Format a datetime as a human-readable fixed-width (7-char) string."""
    delta = now - dt
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds:>3}s ago"[:7].ljust(7)
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago".rjust(7)
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago".rjust(7)
    days = hours // 24
    if days < 100:
        return f"{days}d ago".rjust(7)
    return "  >99d "


def format_completed_row(record: RunRecord, *, now: datetime | None = None) -> str:
    """Format a completed run row with fixed-width columns."""
    if now is None:
        now = datetime.now(UTC)
    icon = (
        f"[{ATTESTED}]OK[/{ATTESTED}]"
        if record.status == "success"
        else f"[{JUDGMENT}]ER[/{JUDGMENT}]"
    )
    ref = item_ref_or_num(record.item_id, record.issue_num)
    title = record.title or f"Issue {ref}"
    title = title[:20] if len(title) > 20 else title
    duration = f"{int(record.duration_seconds / 60)}m"
    cost = f"${record.estimated_cost_usd:.2f}"
    if record.finished_at is not None:
        ago = _format_time_ago(record.finished_at, now)
    else:
        ago = "    n/a"
    return (
        f"{icon} [bold]{ref:<5}[/bold] "
        f"{title:<20}  "
        f"[dim]{duration:>5}  {cost:>7}[/dim]  "
        f"[italic dim]{ago}[/italic dim]"
    )


def progress_bar(*, current: int, expected: int, width: int = 10) -> str:
    pct = 0 if expected <= 0 else min(99, int(current / expected * 100))
    filled = int(width * pct / 100)
    empty = width - filled
    bar_color = MACHINE if pct < 70 else (WARN if pct < 90 else JUDGMENT)
    bar = f"[{bar_color}]{'█' * filled}[/{bar_color}][dim]{'▒' * empty}[/dim]"
    return f"{bar} [bold {bar_color}]{pct:>2}%[/bold {bar_color}]"


def format_worker_row_compact(
    record: RunRecord,
    *,
    snapshot: dict[str, Any],
    expected_tokens: int,
    issue_title: str,
) -> str:
    ref = item_ref_or_num(record.item_id, record.issue_num)
    elapsed = datetime.now(UTC) - record.started_at
    elapsed_min = int(elapsed.total_seconds() / 60)
    if record.model:
        parts = record.model.split("/")[-1].split("-")
        model_short = "-".join(parts[-2:])[:6] if len(parts) > 1 else parts[-1][:6]
    else:
        model_short = ""
    total_tokens = snapshot.get("total_tokens", 0)
    tokens_str = format_tokens(total_tokens)
    bar = progress_bar(current=total_tokens, expected=expected_tokens)
    title = issue_title[:20] if len(issue_title) > 20 else issue_title
    return (
        f"[bold]{ref:<5}[/bold] "
        f"{title:<20} "
        f"[dim]{model_short:<6}[/dim] "
        f"[{MACHINE}]{elapsed_min:>3}m[/{MACHINE}] "
        f"{bar} "
        f"[{FG}]{tokens_str:>6}[/{FG}]"
    )
