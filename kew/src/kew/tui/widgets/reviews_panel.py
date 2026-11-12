"""Reviews panel — PRs with pending review comments."""

from __future__ import annotations

from datetime import UTC, datetime

from kew.models import GhPullRequest
from kew.tui.palette import ATTESTED, JUDGMENT, MACHINE, WARN


def _comment_count(pr: GhPullRequest) -> int:
    """Extract comment count from PR dict."""
    comments = pr.get("comments", [])
    if isinstance(comments, list):
        return len(comments)
    if isinstance(comments, int):
        return comments
    return 0


def _age_str(created_at: str) -> str:
    """Format age as '1d', '2d', etc."""
    if not created_at:
        return ""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        delta = datetime.now(UTC) - created
        days = delta.days
        if days < 1:
            hours = int(delta.total_seconds() / 3600)
            return f"{hours}h" if hours > 0 else "<1h"
        return f"{days}d"
    except (ValueError, TypeError):
        return ""


def _ci_status(pr: GhPullRequest) -> tuple[str, str]:
    """Return (indicator, color) for CI check status."""
    checks = pr.get("statusCheckRollup", [])
    if not checks:
        return "\u00b7\u00b7", WARN  # pending/unknown
    all_done = all(c.get("status", "").upper() == "COMPLETED" for c in checks)
    if not all_done:
        return "\u00b7\u00b7", WARN  # pending
    any_failed = any(
        c.get("conclusion", "").upper() in ("FAILURE", "CANCELLED", "TIMED_OUT")
        for c in checks
    )
    if any_failed:
        return "\u2717CI", JUDGMENT  # ✗CI red
    return "\u2713CI", ATTESTED  # ✓CI attested


def format_review_row_compact(pr: GhPullRequest) -> str:
    """Format a PR dict as a Rich markup string for the reviews panel."""
    from rich.markup import escape

    num = pr.get("number", 0)
    title = escape(pr.get("title", ""))
    author_data = pr.get("author", {})
    author = author_data.get("login", "") if isinstance(author_data, dict) else ""
    count = _comment_count(pr)
    age = _age_str(pr.get("createdAt", ""))

    ci_icon, ci_color = _ci_status(pr)

    max_title = 22
    if len(title) > max_title:
        title = title[: max_title - 1] + "\u2026"

    count_str = f"{count} comment{'s' if count != 1 else ''}"

    return (
        f"[{ci_color}]{ci_icon:<3}[/{ci_color}] "
        f"[{MACHINE}]PR #{num:<4}[/{MACHINE}] "
        f"{title:<{max_title}}  "
        f"[dim]{author:<12}[/dim] "
        f"[dim]{count_str}[/dim]  "
        f"[dim]{age}[/dim]"
    )
