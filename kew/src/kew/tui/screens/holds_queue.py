"""Holds & Queue screen — epic holds, ready-queue issues, and orphan PRs (#685).

A minimal, keyboard-selectable pane listing the three operator-actionable
collections from Group 1's store queries. Verbs (retry-node, queue-issue,
ack-orphan, resume-fleet) reach the service layer via ``kew.tui.actions`` —
this screen never touches ``run_store`` or ``gh`` directly.

The two canonical keys (``w``, ``W``) do not name an action at all: they ask
the projection what is safe for the highlighted row and hand that answer,
with the revision it was derived from, to ``perform_work_action``. A pane that
cannot name its own verbs cannot invent one the projection would refuse.

What each row *is* comes from the canonical work projection
(:func:`kew.tui.state.work_display`), and each row is keyed on the opaque
:class:`~kew.tui.selection.WorkItemRef` behind it. This screen keeps no
status table of its own: it never sees a ``RunStatus``, so it can never
disagree with the console, the CLI or the API about the same item.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from kew.tui.palette import FAINT, JUDGMENT, MACHINE, WARN
from kew.tui.selection import (
    EpicHoldListItem,
    OrphanListItem,
    ReadyQueueListItem,
    SectionHeaderListItem,
    WorkItemRef,
)
from kew.tui.state import hold_item_id, work_display

if TYPE_CHECKING:
    from kew.tui.state import TuiState


def _canonical(state: TuiState | None, item_id: str) -> Any:
    """The shared display for one item — *not read* when there is no view."""
    view = state.work_view(item_id) if state is not None else None
    return work_display(view)


def _work_suffix(state: TuiState | None, item_id: str) -> str:
    """The canonical badge and safest action, as a row suffix."""
    display = _canonical(state, item_id)
    tail = display.action or display.exception
    return f"{display.markup} [dim]{tail}[/dim]"


def _unreadable_rows(state: TuiState | None) -> list[ListItem]:
    """Items the projection NAMED as unreadable — dropping them would render
    a shorter, healthier world than the real one."""
    names = list(state.work_unreadable()) if state is not None else []
    if not names:
        return []
    refs = ", ".join(WorkItemRef(item_id=name).display for name in names)
    return [
        SectionHeaderListItem(text=f"[bold {JUDGMENT}]UNREADABLE[/bold {JUDGMENT}]"),
        SectionHeaderListItem(
            text=f"[{JUDGMENT}]  {refs} — work facts could not be read[/{JUDGMENT}]"
        ),
    ]


def _epic_hold_rows(state: TuiState | None) -> list[ListItem]:
    rows: list[ListItem] = [
        SectionHeaderListItem(text=f"[bold {WARN}]EPIC HOLDS[/bold {WARN}]")
    ]
    holds = list(state.epic_holds) if state is not None else []
    if not holds:
        rows.append(SectionHeaderListItem(text="[dim]  (no epic holds)[/dim]"))
        return rows
    for hold in holds:
        ref = WorkItemRef.from_record(hold)
        text = (
            f"{_work_suffix(state, hold_item_id(hold))}  "
            f"[bold]{ref.display}[/bold]  "
            f"[dim]{hold.epic_ref}[/dim]  "
            f"[{WARN}]{hold.reason}[/{WARN}]"
        )
        rows.append(
            EpicHoldListItem(
                issue_num=hold.issue_num,
                epic_ref=hold.epic_ref,
                text=text,
                work_ref=ref,
            )
        )
    return rows


def _ready_rows(state: TuiState | None) -> list[ListItem]:
    rows: list[ListItem] = [
        SectionHeaderListItem(text=f"[bold {MACHINE}]READY QUEUE[/bold {MACHINE}]")
    ]
    ready = list(state.queue_ready) if state is not None else []
    if not ready:
        rows.append(SectionHeaderListItem(text="[dim]  (queue empty)[/dim]"))
        return rows
    for issue in ready:
        ref = WorkItemRef.from_issue(issue)
        text = (
            f"{_work_suffix(state, ref.item_id)}  "
            f"[bold]{ref.display}[/bold]  {issue.get('title', '')}"
        )
        rows.append(
            ReadyQueueListItem(
                issue_num=issue.get("number", 0), text=text, work_ref=ref
            )
        )
    return rows


def _orphan_rows(state: TuiState | None) -> list[ListItem]:
    rows: list[ListItem] = [
        SectionHeaderListItem(text=f"[bold {JUDGMENT}]ORPHAN PRS[/bold {JUDGMENT}]")
    ]
    orphans = list(state.orphan_prs) if state is not None else []
    if not orphans:
        rows.append(SectionHeaderListItem(text="[dim]  (no orphan PRs)[/dim]"))
        return rows
    for orphan in orphans:
        text = (
            f"[bold]PR #{orphan.pr_number}[/bold]  "
            f"[dim]{orphan.branch}[/dim]  "
            f"[{JUDGMENT}]{orphan.reason}[/{JUDGMENT}]"
        )
        rows.append(OrphanListItem(pr_number=orphan.pr_number, text=text))
    return rows


def holds_queue_revision_line(state: TuiState | None) -> str:
    """The breadcrumb's provenance: which projection this pane is showing."""
    revision = state.work_revision() if state is not None else ""
    shown = revision or "not read"
    return (
        f"[dim]Esc:back[/dim]   [bold]Holds & Queue[/bold]  [{FAINT}]{shown}[/{FAINT}]"
    )


def build_holds_queue_items(state: TuiState | None) -> list[ListItem]:
    """Build the flat ListView contents: labelled sections (#685).

    Header rows are non-selectable; each data row is a typed ListItem carrying
    the canonical identity its verb needs. Empty sections still render their
    header with a dimmed placeholder so the operator can see the pane is live,
    not broken — and an item the projection could not read is named at the top
    rather than quietly missing.
    """
    return [
        *_unreadable_rows(state),
        *_epic_hold_rows(state),
        *_ready_rows(state),
        *_orphan_rows(state),
    ]


class HoldsQueueScreen(Screen):  # type: ignore[type-arg]
    """Full-screen Holds & Queue pane with per-section verbs."""

    BINDINGS = [
        ("escape", "pop_screen", "Back"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("r", "retry_node", "Retry node"),
        ("w", "work_action", "Safe action"),
        ("W", "work_counterpart", "Counterpart"),
        ("Q", "queue_issue", "Queue"),
        ("a", "ack_orphan", "Ack orphan"),
        ("F", "resume_fleet", "Resume fleet"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        yield Static(
            holds_queue_revision_line(None),
            id="holds-queue-breadcrumb",
        )
        yield ListView(id="holds-queue-panel", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_content()
        lv = self.query_one("#holds-queue-panel", ListView)
        lv.focus()

    def refresh_content(self) -> None:
        """Rebuild the pane from current app state."""
        app: Any = self.app
        state = getattr(app, "state", None)
        lv = self.query_one("#holds-queue-panel", ListView)
        lv.clear()
        for item in build_holds_queue_items(state):
            lv.append(item)
        self.query_one("#holds-queue-breadcrumb", Static).update(
            holds_queue_revision_line(state)
        )

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#holds-queue-panel", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#holds-queue-panel", ListView).action_cursor_up()

    def action_retry_node(self) -> None:
        from kew.tui.actions import handle_retry_node

        handle_retry_node(self.app)

    def action_work_action(self) -> None:
        """Perform whatever the projection says is safest for this row."""
        from kew.tui.actions import handle_work_action

        handle_work_action(self.app)

    def action_work_counterpart(self) -> None:
        """Perform the authorized decision beside the safest one."""
        from kew.tui.actions import handle_work_action

        handle_work_action(self.app, counterpart=True)

    def action_queue_issue(self) -> None:
        from kew.tui.actions import handle_queue_issue

        handle_queue_issue(self.app)

    def action_ack_orphan(self) -> None:
        from kew.tui.actions import handle_ack_orphan

        handle_ack_orphan(self.app)

    def action_resume_fleet(self) -> None:
        from kew.tui.actions import handle_resume_fleet

        handle_resume_fleet(self.app)

    def action_quit(self) -> None:
        from kew.tui.actions import handle_quit

        handle_quit(self.app)
