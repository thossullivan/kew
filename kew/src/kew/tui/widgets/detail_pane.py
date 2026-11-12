"""Detail pane — k9s-style describe view for a GitHub issue."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from rich.console import ConsoleRenderable, Group, RichCast
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from kew.models import GhIssue
from kew.selector import get_complexity
from kew.tui.palette import JUDGMENT, MACHINE, WARN
from kew.tui.widgets.queue_panel import _PRIORITY_COLORS, _priority_label

# Module-level LRU cache for rendered issue content (survives screen transitions)
_RENDERED_CACHE: OrderedDict[str, Group] = OrderedDict()
_RENDERED_CACHE_MAX = 20


def _cache_key(issue: GhIssue) -> str:
    """Cache key from issue number + content hash (body + all header fields)."""
    content = repr(
        (
            issue.get("number", 0),
            issue.get("title", ""),
            issue.get("body", "") or "",
            issue.get("createdAt", ""),
            tuple(
                lbl.get("name", "")
                for lbl in issue.get("labels", [])
                if isinstance(lbl, dict)
            ),
            tuple(
                a.get("login", "")
                for a in issue.get("assignees", [])
                if isinstance(a, dict)
            ),
        )
    )
    h = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"{issue.get('number', 0)}:{h}"


def _get_cached_renderable(issue: GhIssue) -> Group:
    """Return cached Rich Group for an issue, or render and cache it."""
    body_text = issue.get("body", "") or ""
    key = _cache_key(issue)
    if key in _RENDERED_CACHE:
        _RENDERED_CACHE.move_to_end(key)
        return _RENDERED_CACHE[key]
    header = Text.from_markup(format_header(issue))
    parts: list[ConsoleRenderable | RichCast | str] = [header, Text("")]
    if body_text.strip():
        if issue.get("_rich_markup"):
            # PR detail body uses Rich markup, render directly
            parts.append(Text.from_markup(body_text))
        else:
            parts.append(RichMarkdown(body_text))
    group = Group(*parts)
    _RENDERED_CACHE[key] = group
    if len(_RENDERED_CACHE) > _RENDERED_CACHE_MAX:
        _RENDERED_CACHE.popitem(last=False)
    return group


def format_header(issue: GhIssue) -> str:
    """Format issue metadata as Rich markup string."""
    number = issue.get("number", 0)
    title = issue.get("title", "")
    labels = issue.get("labels", [])
    assignees = issue.get("assignees", [])
    created_at = issue.get("createdAt", "")

    priority_str, priority_num = _priority_label(labels)
    pc = _PRIORITY_COLORS.get(priority_num, "dim")
    if priority_str == "--":
        priority_rich = "[dim]--[/dim]"
    else:
        priority_rich = f"[bold {pc}]{priority_str}[/bold {pc}]"

    complexity = get_complexity(labels)
    cc = {"simple": MACHINE, "medium": WARN, "complex": JUDGMENT}.get(complexity, "dim")
    complexity_rich = f"[{cc}]{complexity}[/{cc}]"

    label_names = ", ".join(
        lbl.get("name", "") for lbl in labels if isinstance(lbl, dict)
    )
    if not label_names:
        label_names = "[dim](none)[/dim]"

    if assignees:
        assignee_str = ", ".join(
            a.get("login", "") for a in assignees if isinstance(a, dict)
        )
    else:
        assignee_str = "[dim](none)[/dim]"

    created_display = created_at[:10] if created_at else ""

    lines = [
        f"[bold]#{number}[/bold]  {title}",
        f"Priority: {priority_rich}  Complexity: {complexity_rich}",
        f"Labels: {label_names}",
    ]
    if created_display:
        lines.append(f"Assignees: {assignee_str}  Created: {created_display}")
    else:
        lines.append(f"Assignees: {assignee_str}")
    return "\n".join(lines)


def format_detail_lines(issue: GhIssue) -> list[str]:
    """Format an issue dict as a list of lines (legacy helper)."""
    body = issue.get("body", "") or ""
    header_lines = format_header(issue).splitlines()
    header_lines.append("")
    header_lines.extend(body.splitlines())
    return header_lines


def format_detail(issue: GhIssue) -> str:
    """Format an issue dict as a string."""
    return "\n".join(format_detail_lines(issue))


class DetailPane(VerticalScroll):
    """Scrollable detail pane for viewing GitHub issues.

    Uses VerticalScroll + Static with Rich renderables passed directly
    (no ANSI string intermediary). This gives Textual correct height
    measurement for proper scroll region calculation.
    """

    DEFAULT_CSS = """
    DetailPane {
        height: 1fr;
    }
    DetailPane > #detail-content {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup("[dim]Select an issue to view details[/dim]"),
            id="detail-content",
        )

    def update_issue(self, issue: GhIssue | None) -> None:
        """Update the displayed content. Pass None to show the placeholder."""
        try:
            content = self.query_one("#detail-content", Static)
        except Exception:
            return
        if issue is None:
            content.update(
                Text.from_markup("[dim]Select an issue to view details[/dim]")
            )
        else:
            content.update(_get_cached_renderable(issue))
        self.scroll_home(animate=False)
