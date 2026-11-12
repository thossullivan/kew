"""Approve/decline dialog for a needs-approval hold (#237)."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from kew.tui.palette import FG, JUDGMENT, MUTED, SURFACE_2


class ApprovalDialog(ModalScreen[str | None]):
    """Modal to approve or decline a held issue.

    Dismisses with "approve" | "decline" | None (cancel).
    """

    BINDINGS = [
        ("a", "approve", "Approve"),
        ("d", "decline", "Decline"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self, *, issue_label: str, estimate_usd: float | None, basis: str
    ) -> None:
        super().__init__()
        self._issue_label = issue_label
        self._estimate_usd = estimate_usd
        self._basis = basis

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold {JUDGMENT}]⏸ Approve held issue[/bold {JUDGMENT}]",
            id="approval-dialog-title",
        )
        yield Static(
            f"\n  [{FG}]{escape(self._issue_label)}[/{FG}]\n",
            id="approval-dialog-issue",
        )
        est = "unknown" if self._estimate_usd is None else f"${self._estimate_usd:.2f}"
        body = f"  Estimated cost: [bold]{est}[/bold]"
        if self._basis:
            body += f"\n  Basis: [dim {MUTED}]{escape(self._basis)}[/dim {MUTED}]"
        body += (
            f"\n\n  [dim {MUTED}]Approve releases the hold to the ready queue "
            f"(it does not dispatch).[/dim {MUTED}]"
        )
        yield Static(body, id="approval-dialog-body")
        hint = (
            f"\n  [bold {JUDGMENT}]a[/bold {JUDGMENT}]"
            f"[dim {MUTED}] approve[/dim {MUTED}]"
            f"  [dim {SURFACE_2}]│[/dim {SURFACE_2}]"
            f"  [bold {JUDGMENT}]d[/bold {JUDGMENT}]"
            f"[dim {MUTED}] decline[/dim {MUTED}]"
            f"  [dim {SURFACE_2}]│[/dim {SURFACE_2}]"
            f"  [bold {JUDGMENT}]Esc[/bold {JUDGMENT}]"
            f"[dim {MUTED}] cancel[/dim {MUTED}]"
        )
        yield Static(hint, id="approval-dialog-hint")

    def action_approve(self) -> None:
        self.dismiss("approve")

    def action_decline(self) -> None:
        self.dismiss("decline")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_quit(self) -> None:
        self.app.exit()
