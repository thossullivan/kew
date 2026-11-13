"""Reusable yes/no confirmation dialog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from kew.tui.palette import JUDGMENT


class ConfirmDialog(ModalScreen[bool]):
    """Modal yes/no confirmation."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("enter", "confirm", "Confirm"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]{self.prompt}[/bold]", id="confirm-prompt")
        yield Static(
            f"[bold {JUDGMENT}]y[/bold {JUDGMENT}][dim]:confirm[/dim]  "
            f"[bold {JUDGMENT}]n[/bold {JUDGMENT}][dim]/Esc:cancel[/dim]",
            id="confirm-hint",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_quit(self) -> None:
        self.app.exit()
