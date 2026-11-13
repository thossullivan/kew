"""Queue panel — list of ready issues sorted by priority."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from kew.models import GhIssue, GhLabel
from kew.selector import get_complexity
from kew.tui.palette import ELEVATED, FAINT, JUDGMENT, MACHINE, WARN

_PRIORITY_COLORS = {
    0: JUDGMENT,
    1: ELEVATED,
    2: MACHINE,
    3: FAINT,
}
_COMPLEXITY_COLORS = {
    "simple": MACHINE,
    "medium": WARN,
    "complex": JUDGMENT,
}


def _relative_age(created_at: str | None) -> str:
    """Return compact relative age string from an ISO datetime."""
    if not created_at:
        return ""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    delta = datetime.now(UTC) - dt
    days = delta.days
    if days < 0:
        days = 0
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _priority_label(labels: list[GhLabel]) -> tuple[str, int]:
    """Extract the highest priority label (lowest P number)."""
    best = 9
    for lbl in labels:
        if not isinstance(lbl, dict):
            continue
        name = str(lbl.get("name", "")).lower()
        for i in range(4):
            if name.startswith(f"p{i}"):
                best = min(best, i)
    if best <= 3:
        return f"P{best}", best
    return "--", 9


def format_queue_row_compact(
    issue: GhIssue,
    *,
    routed_runner: str,
    routed_model: str,
) -> str:
    num = issue.get("number", 0)
    title = issue.get("title", "")
    labels = issue.get("labels", [])
    priority_str, priority_num = _priority_label(labels)
    complexity = get_complexity(labels)
    max_title = 30
    if len(title) > max_title:
        title = title[: max_title - 1] + "\u2026"
    if routed_model:
        parts = routed_model.split("/")[-1].split("-")
        model_short = "-".join(parts[-2:])[:8] if len(parts) > 1 else parts[-1][:8]
    else:
        model_short = ""
    pc = _PRIORITY_COLORS.get(priority_num, "dim")
    cc = _COMPLEXITY_COLORS.get(complexity, "dim")
    return (
        f"[bold {pc}]{priority_str}[/bold {pc}] "
        f"[bold]#{num:<4}[/bold] "
        f"{title:<{max_title}}  "
        f"[{cc}]{complexity:<7}[/{cc}] "
        f"[dim]{model_short}[/dim]"
    )


_EXCLUDE_LABELS = {
    "ready",
    "in-progress",
    "agent-working",
    "agent-failed",
    "agent-pr",
    "pr-pending-review",
    "blocked",
    "agent-completed-no-changes",
    "agent-needs-help",
    "budget-exceeded",
    "large-pr",
    "ready-to-merge",
    "simple",
    "medium",
    "complex",
}

_PRIORITY_RE_PREFIX = re.compile(r"^p\d", re.IGNORECASE)


def format_backlog_row_compact(issue: GhIssue) -> str:
    """Format a backlog issue row — same columns as ready, but dimmed."""
    num = issue.get("number", 0)
    title = issue.get("title", "")
    labels = issue.get("labels", [])
    created_at = issue.get("createdAt", "")
    priority_str, priority_num = _priority_label(labels)
    complexity = get_complexity(labels)
    max_title = 30
    if len(title) > max_title:
        title = title[: max_title - 1] + "\u2026"
    age = _relative_age(created_at)
    pc = _PRIORITY_COLORS.get(priority_num, "dim")
    cc = _COMPLEXITY_COLORS.get(complexity, "dim")
    return (
        f"[{pc}]{priority_str}[/{pc}] "
        f"[dim]#{num:<4}[/dim] "
        f"[dim]{title:<{max_title}}[/dim]  "
        f"[dim {cc}]{complexity:<7}[/dim {cc}] "
        f"[dim]{age}[/dim]"
    )


def format_separator(ready_count: int, backlog_count: int) -> str:
    """Format the separator between ready and backlog sections."""
    return "[dim]────────── backlog ──────────[/dim]"


_FAILED_EXTRA_LABELS = {"budget-exceeded", "agent-needs-help"}


def format_failed_row_compact(issue: GhIssue) -> str:
    """Format a failed issue row — red/orange, with priority and triage labels."""
    num = issue.get("number", 0)
    title = issue.get("title", "")
    labels = issue.get("labels", [])
    priority_str, _ = _priority_label(labels)
    max_title = 30
    if len(title) > max_title:
        title = title[: max_title - 1] + "\u2026"
    # Show triage-relevant extra labels
    extra = []
    for lbl in labels:
        if isinstance(lbl, dict):
            name = lbl.get("name", "")
            if name in _FAILED_EXTRA_LABELS:
                extra.append(name)
    extra_str = ", ".join(extra[:2]) if extra else ""
    return (
        f"[{JUDGMENT}]{priority_str} #{num:<4} "
        f"{title:<{max_title}}  {extra_str}[/{JUDGMENT}]"
    )


def format_failed_separator() -> str:
    """Format the separator before the failed section."""
    return f"[{JUDGMENT}]────────── failed ──────────[/{JUDGMENT}]"
