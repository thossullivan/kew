"""Budget exceeded confirmation dialog — shows spend vs limits and allows override."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from kew.tui.palette import FAINT, FG, JUDGMENT, MUTED, SURFACE_2
from kew.tui.widgets.budget_overlay import format_budget_status_lines

if TYPE_CHECKING:
    from kew.budget import BudgetStatus


class BudgetExceededDialog(ModalScreen[bool]):
    """Modal showing budget status with option to override and dispatch anyway."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        title: str,
        issue_label: str,
        status: BudgetStatus,
        confirm_key: str = "d",
        subtitle: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._issue_label = issue_label
        self.budget_status = status
        self._confirm_key = confirm_key
        self._subtitle = subtitle

    def on_mount(self) -> None:
        """Register the dynamic confirm key binding after mount."""
        self._bindings.bind(self._confirm_key, "confirm", description="Confirm")

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold {JUDGMENT}]⚠ {self._title}[/bold {JUDGMENT}]",
            id="budget-dialog-title",
        )
        yield Static(
            f"\n  [{FG}]{self._issue_label}[/{FG}]\n",
            id="budget-dialog-issue",
        )
        from rich.markup import escape

        body = format_budget_status_lines(self.budget_status)
        if self._subtitle:
            body += f"\n\n  [dim {FAINT}]{escape(self._subtitle)}[/dim {FAINT}]"
        yield Static(body, id="budget-dialog-body")
        hint = (
            f"\n  [bold {JUDGMENT}]{self._confirm_key}[/bold {JUDGMENT}]"
            f"[dim {MUTED}] dispatch anyway[/dim {MUTED}]"
            f"  [dim {SURFACE_2}]│[/dim {SURFACE_2}]"
            f"  [bold {JUDGMENT}]Esc[/bold {JUDGMENT}]"
            f"[dim {MUTED}] cancel[/dim {MUTED}]"
        )
        yield Static(hint, id="budget-dialog-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_quit(self) -> None:
        self.app.exit()
