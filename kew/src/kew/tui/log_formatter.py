"""Format event log data for human-readable display in the log viewer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rich.markup import escape

from kew.tui.palette import ATTESTED, JUDGMENT, MACHINE, MONEY


def fmt_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _fmt_tokens(n: int) -> str:
    """Format token count with comma separators."""
    return f"{n:,}"


def _build_summary(events: list[dict[str, Any]]) -> list[str]:
    """Build summary header lines from event data."""
    runner = ""
    model = ""
    start_ts = 0.0
    end_ts = 0.0
    total_cost = 0.0
    token_cost = 0.0
    total_input = 0
    total_output = 0
    exit_code: int | None = None

    for ev in events:
        etype = ev.get("type", "")
        if etype == "started":
            runner = ev.get("runner", "")
            model = ev.get("model", "")
            start_ts = ev.get("ts", 0.0)
        elif etype == "finished":
            end_ts = ev.get("ts", 0.0)
            total_cost = ev.get("cost_usd", 0.0)
            exit_code = ev.get("exit_code", -1)
        elif etype == "tokens":
            total_input = ev.get("input", 0)
            total_output = ev.get("output", 0)
            token_cost = ev.get("cost_usd", 0.0)

    if not runner and not model:
        return []

    lines: list[str] = []
    sep = "[dim]" + "\u2500" * 44 + "[/dim]"
    lines.append(sep)

    if runner or model:
        parts = []
        if runner:
            parts.append(f"[bold]Runner:[/bold] {escape(str(runner))}")
        if model:
            parts.append(f"[bold]Model:[/bold] {escape(str(model))}")
        lines.append("  ".join(parts))

    info_parts: list[str] = []
    if start_ts and end_ts:
        duration = end_ts - start_ts
        info_parts.append(f"[bold]Duration:[/bold] {fmt_duration(duration)}")
    elif start_ts and not end_ts:
        info_parts.append(f"[bold]Status:[/bold] [{MACHINE}]in progress[/{MACHINE}]")

    display_cost = total_cost or token_cost
    if display_cost:
        info_parts.append(f"[bold]Cost:[/bold] [{MONEY}]${display_cost:.2f}[/{MONEY}]")
    if info_parts:
        lines.append("  ".join(info_parts))

    if total_input or total_output:
        total = total_input + total_output
        lines.append(
            f"[bold]Tokens:[/bold] {_fmt_tokens(total)} "
            f"[dim]({_fmt_tokens(total_input)} in / "
            f"{_fmt_tokens(total_output)} out)[/dim]"
        )

    if exit_code is not None:
        color = ATTESTED if exit_code == 0 else JUDGMENT
        lines.append(f"[bold]Result:[/bold] [{color}]exit {exit_code}[/{color}]")

    lines.append(sep)
    lines.append("")
    return lines


_INTERNAL_EVENT_TYPES = frozenset({"pid"})


def format_event(event: dict[str, Any]) -> str | None:
    """Format a single event dict as a Rich markup line.

    Returns None for internal event types that should be suppressed.
    """
    etype = event.get("type", "")
    if etype in _INTERNAL_EVENT_TYPES:
        return None

    ts = event.get("ts", 0.0)
    time_str = datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M:%S") if ts else ""
    ts_prefix = f"[dim]{time_str}[/dim] " if time_str else ""

    if etype == "activity":
        return f"{ts_prefix}{escape(str(event.get('msg', '')))}"
    if etype == "tool_use":
        tool = event.get("tool", "")
        target = event.get("target", "")
        return (
            f"{ts_prefix}[{MACHINE}]Tool:[/{MACHINE}] "
            f"[bold]{escape(str(tool))}[/bold] "
            f"[dim]{escape(str(target))}[/dim]"
        )
    if etype == "tokens":
        inp = event.get("input", 0)
        out = event.get("output", 0)
        cost = event.get("cost_usd", 0.0)
        return (
            f"{ts_prefix}[dim]tokens:[/dim] {inp + out:,}  "
            f"[{MONEY}]${cost:.2f}[/{MONEY}]"
        )
    if etype == "started":
        runner = event.get("runner", "")
        model = event.get("model", "")
        return (
            f"{ts_prefix}[{ATTESTED}]Started:[/{ATTESTED}] "
            f"[bold]{escape(str(runner))}/{escape(str(model))}[/bold]"
        )
    if etype == "finished":
        exit_code = event.get("exit_code", -1)
        cost = event.get("cost_usd", 0.0)
        color = ATTESTED if exit_code == 0 else JUDGMENT
        return (
            f"{ts_prefix}[{color}]Finished (exit {exit_code})[/{color}]"
            f"  [{MONEY}]total: ${cost:.2f}[/{MONEY}]"
        )
    if etype == "error":
        return (
            f"{ts_prefix}[{JUDGMENT}]Error:[/{JUDGMENT}] "
            f"[bold {JUDGMENT}]{escape(str(event.get('msg', '')))}[/bold {JUDGMENT}]"
        )

    return f"{ts_prefix}[dim]{escape(str(etype))}:[/dim] {escape(str(event))}"


def _format_token_delta(delta: int) -> str:
    """Format a token delta as a subtle separator line."""
    sign = "+" if delta >= 0 else "-"
    return f"[dim]          \u254c\u254c {sign}{abs(delta):,} tokens \u254c\u254c[/dim]"


def format_log_view(events: list[dict[str, Any]]) -> list[str]:
    """Format a list of event dicts into human-readable Rich markup lines.

    Returns a list of strings with Rich markup, suitable for writing
    to a RichLog widget. Includes a summary header when a 'started'
    event is present.
    """
    if not events:
        return []

    lines: list[str] = []
    lines.extend(_build_summary(events))

    prev_total = 0
    for event in events:
        formatted: str | None
        if event.get("type") == "tokens":
            inp = event.get("input", 0)
            out = event.get("output", 0)
            current_total = inp + out
            delta = current_total - prev_total
            prev_total = current_total
            formatted = _format_token_delta(delta)
        else:
            formatted = format_event(event)
        if formatted is not None:
            lines.append(formatted)

    return lines
