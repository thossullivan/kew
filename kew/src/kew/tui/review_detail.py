"""Review detail rendering — verdict computation and PR status formatting."""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from kew.tui.palette import ATTESTED, JUDGMENT, MACHINE, WARN


def _analyze_checks(
    checks: list[dict[str, Any]],
) -> tuple[bool, bool, list[str]]:
    """Analyze CI check status. Returns (ci_pass, ci_pending, failed_names)."""
    ci_pass = True
    ci_pending = False
    failed: list[str] = []
    for check in checks:
        status = (check.get("status") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        if status != "COMPLETED":
            ci_pending = True
            ci_pass = False
        elif conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT"):
            ci_pass = False
            failed.append(escape(check.get("name", "unknown")))
    return ci_pass, ci_pending, failed


def _ci_failing_verdict(failed_checks: list[str], total_checks: int) -> dict[str, str]:
    """Build verdict for CI failure state."""
    n = len(failed_checks)
    subtitle = ", ".join(failed_checks[:3])
    if n > 3:
        subtitle += f" +{n - 3} more"
    return {
        "status": "ci_failing",
        "title": f"CI failing ({n}/{total_checks} checks)",
        "subtitle": subtitle,
        "color": JUDGMENT,
    }


def _changes_requested_verdict(pr_data: dict[str, Any]) -> dict[str, str]:
    """Build verdict for changes-requested state."""
    reviewers = [
        r.get("author", {}).get("login", "")
        for r in pr_data.get("reviews", [])
        if isinstance(r.get("author"), dict)
        and (r.get("state") or "").upper() == "CHANGES_REQUESTED"
    ]
    subtitle = ", ".join(f"@{escape(r)}" for r in reviewers if r) if reviewers else ""
    return {
        "status": "changes_requested",
        "title": "Changes requested",
        "subtitle": subtitle,
        "color": JUDGMENT,
    }


def _awaiting_review_verdict(
    pr_data: dict[str, Any],
    checks: list[dict[str, Any]],
    ci_pending: bool,
) -> dict[str, str]:
    """Build verdict for awaiting-review state."""
    parts = []
    if checks:
        parts.append("CI pending" if ci_pending else "CI passing")
    else:
        parts.append("No checks")
    requested = pr_data.get("reviewRequests", [])
    if requested:
        names = [
            r.get("login", "")
            for r in requested
            if isinstance(r, dict) and r.get("login")
        ]
        if names:
            parts.append("Requested: " + ", ".join(f"@{escape(n)}" for n in names))
    return {
        "status": "awaiting_review",
        "title": "Awaiting review",
        "subtitle": " \u00b7 ".join(parts),
        "color": WARN,
    }


_VERDICT_PENDING = {
    "status": "awaiting_review",
    "title": "Awaiting review",
    "subtitle": "CI pending",
    "color": WARN,
}
_VERDICT_CONFLICTS = {
    "status": "conflicts",
    "title": "Has conflicts",
    "subtitle": "Approved but cannot merge",
    "color": JUDGMENT,
}
_VERDICT_READY = {
    "status": "ready",
    "title": "Ready to merge",
    "subtitle": "CI passing \u00b7 Approved \u00b7 No conflicts",
    "color": ATTESTED,
}


def _try_ci_failing(
    ci_pass: bool,
    ci_pending: bool,
    checks: list[dict[str, Any]],
    failed: list[str],
) -> dict[str, str] | None:
    """Return CI failing verdict, or None if not applicable."""
    if not ci_pass and not ci_pending and checks:
        return _ci_failing_verdict(failed, len(checks))
    return None


def _try_review_state(
    ci_pass: bool,
    ci_pending: bool,
    review_decision: str,
    pr_data: dict[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Return review-related verdict, or None if approved."""
    if ci_pass and review_decision == "CHANGES_REQUESTED":
        return _changes_requested_verdict(pr_data)
    if (ci_pass or ci_pending) and review_decision != "APPROVED":
        return _awaiting_review_verdict(pr_data, checks, ci_pending)
    return None


def _merge_readiness(ci_pending: bool, mergeable: str) -> dict[str, str]:
    """Resolve final verdict for approved PRs."""
    if ci_pending:
        return dict(_VERDICT_PENDING)
    if mergeable not in ("MERGEABLE", ""):
        return dict(_VERDICT_CONFLICTS)
    return dict(_VERDICT_READY)


def compute_verdict(pr_data: dict[str, Any]) -> dict[str, str]:
    """Compute the actionable verdict for a PR.

    Returns dict with keys: status, title, subtitle, color.
    Priority: ci_failing > changes_requested > awaiting_review > ready.
    """
    checks = pr_data.get("statusCheckRollup", [])
    review_decision = (pr_data.get("reviewDecision") or "").upper()
    mergeable = (pr_data.get("mergeable") or "").upper()
    ci_pass, ci_pending, failed_checks = _analyze_checks(checks)

    return (
        _try_ci_failing(ci_pass, ci_pending, checks, failed_checks)
        or _try_review_state(ci_pass, ci_pending, review_decision, pr_data, checks)
        or _merge_readiness(ci_pending, mergeable)
    )


def format_verdict_bar(verdict: dict[str, str]) -> str:
    """Format the verdict as Rich markup for the detail pane header."""
    color = verdict["color"]
    title = escape(verdict["title"])
    subtitle = escape(verdict["subtitle"])
    lines = [f"[bold {color}]{title}[/bold {color}]"]
    if subtitle:
        lines.append(f"[dim]{subtitle}[/dim]")
    return "\n".join(lines)


def format_checks_section(checks: list[dict[str, Any]]) -> str:
    """Format CI checks as a Rich markup checklist."""
    if not checks:
        return "[bold dim]CHECKS[/bold dim]\n  [dim]No checks[/dim]"
    lines = ["[bold dim]CHECKS[/bold dim]"]
    for check in checks:
        name = escape(check.get("name", "unknown"))
        status = (check.get("status") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        if status != "COMPLETED":
            icon = f"[{WARN}]\u00b7\u00b7[/{WARN}]"
        elif conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT"):
            icon = f"[{JUDGMENT}]\u2717[/{JUDGMENT}]"
        else:
            icon = f"[{ATTESTED}]\u2713[/{ATTESTED}]"
        lines.append(f"  {icon} {name}")
    return "\n".join(lines)


def format_reviews_section(reviews: list[dict[str, Any]]) -> str:
    """Format review state as a Rich markup section."""
    if not reviews:
        return "[bold dim]REVIEWS[/bold dim]\n  [dim]No reviews[/dim]"
    lines = ["[bold dim]REVIEWS[/bold dim]"]
    for r in reviews:
        author = (
            r.get("author", {}).get("login", "unknown")
            if isinstance(r.get("author"), dict)
            else "unknown"
        )
        state = (r.get("state") or "").upper()
        if state == "APPROVED":
            icon = f"[{ATTESTED}]\u2713[/{ATTESTED}]"
            label = f"[{ATTESTED}]approved[/{ATTESTED}]"
        elif state == "CHANGES_REQUESTED":
            icon = f"[{JUDGMENT}]\u2717[/{JUDGMENT}]"
            label = f"[{JUDGMENT}]changes requested[/{JUDGMENT}]"
        else:
            icon = "[dim]\u00b7\u00b7[/dim]"
            label = f"[dim]{state.lower()}[/dim]"
        lines.append(f"  {icon} @{escape(author)} \u2014 {label}")
    return "\n".join(lines)


def format_merge_section(pr_data: dict[str, Any]) -> str:
    """Format merge readiness as a Rich markup section."""
    mergeable = (pr_data.get("mergeable") or "").upper()
    lines = ["[bold dim]MERGE[/bold dim]"]
    if mergeable == "MERGEABLE":
        lines.append(f"  [{ATTESTED}]\u2713[/{ATTESTED}] No conflicts")
    elif mergeable == "CONFLICTING":
        lines.append(f"  [{JUDGMENT}]\u2717[/{JUDGMENT}] Has conflicts")
    else:
        lines.append("  [dim]\u00b7\u00b7[/dim] Unknown")
    return "\n".join(lines)


def format_comments_section(comments: list[dict[str, Any]]) -> str:
    """Format review comments as Rich markup, grouped by file."""
    if not comments:
        return ""
    lines = [f"[bold {MACHINE}]COMMENTS ({len(comments)})[/bold {MACHINE}]"]
    for c in comments:
        author = c.get("author", "unknown")
        path = c.get("path")
        line_num = c.get("line")
        body = c.get("body", "")
        loc = (f"{path}:{line_num}" if line_num else path) if path else "general"
        lines.append("")
        lines.append(
            f"  [{MACHINE}]\u2502[/{MACHINE}] "
            f"[bold {MACHINE}]@{escape(author)}[/bold {MACHINE}] "
            f"[dim]\u00b7 {escape(loc)}[/dim]"
        )
        for body_line in body.strip().splitlines():
            lines.append(f"  [{MACHINE}]\u2502[/{MACHINE}]  {escape(body_line)}")
        lines.append(f"  [{MACHINE}]\u2502[/{MACHINE}]")
    return "\n".join(lines)
