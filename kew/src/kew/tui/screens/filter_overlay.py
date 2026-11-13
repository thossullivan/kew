"""Filter overlay — text input for filtering panel items."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input


class FilterOverlay(ModalScreen[str | None]):
    """Modal with a text input for filtering the focused panel."""

    BINDINGS = [
        ("escape", "clear_and_close", "Clear"),
    ]

    def __init__(self, panel_id: str, initial: str = "") -> None:
        super().__init__()
        self.panel_id = panel_id
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Input(value=self.initial, placeholder="Filter...", id="filter-input")

    def on_mount(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter locks the filter."""
        self.dismiss(event.value or None)

    def action_clear_and_close(self) -> None:
        """Esc clears the filter."""
        self.dismiss(None)
