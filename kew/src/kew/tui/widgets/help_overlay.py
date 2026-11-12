"""Help overlay — keybinding reference."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from kew.tui.palette import JUDGMENT, MACHINE

ASCII_LOGO = rf"""
[{JUDGMENT}]
    __                
   / /_____ _      __ 
  / //_/ _ \ | /| / / 
 / ,< /  __/ |/ |/ /  
/_/|_|\___|__/|__/   
[/{JUDGMENT}]
[{MACHINE}]AGENT CONTROL PLANE[/{MACHINE}]
"""


def format_help_text(screen: str = "cockpit") -> str:
    sections: list[str] = [ASCII_LOGO, "", "[bold]Keyboard Shortcuts[/bold]", ""]

    a = sections.append
    a(f"  [bold {MACHINE}]Navigation[/bold {MACHINE}]")
    if screen in ("cockpit", "expanded"):
        a(
            "    [bold]1/2/3/4[/bold]     "
            "[dim]Switch Queue/Workers/Reviews/Analytics[/dim]"
        )
    if screen == "cockpit":
        a("    [bold]Tab[/bold]         [dim]Cycle panel focus[/dim]")
    if screen in ("expanded", "logs", "analytics"):
        a("    [bold]Esc[/bold]         [dim]Back[/dim]")
    a("    [bold]j/k[/bold]         [dim]Navigate rows[/dim]")
    if screen in ("expanded", "logs"):
        a("    [bold]J/K[/bold]         [dim]Scroll detail/stream pane[/dim]")
    if screen in ("logs", "analytics"):
        a("    [bold]h[/bold]           [dim]Cycle lookback period[/dim]")

    if screen in ("cockpit", "expanded", "logs"):
        a("")
        a(f"  [bold {MACHINE}]Actions[/bold {MACHINE}]")
        a("    [bold]d[/bold]           [dim]Dispatch (ready only)[/dim]")
        if screen in ("cockpit", "logs"):
            a("    [bold]x[/bold]           [dim]Kill selected worker[/dim]")
        a("    [bold]t[/bold]           [dim]Toggle ready label[/dim]")
        a("    [bold]r[/bold]           [dim]Retry run[/dim]")
        a("    [bold]R[/bold]           [dim]Review PR[/dim]")

    sections.append("")
    sections.append(f"  [bold {MACHINE}]Overlays[/bold {MACHINE}]")
    sections.append("    [bold]b[/bold]           [dim]Toggle budget[/dim]")
    sections.append("    [bold]/[/bold]           [dim]Filter/search[/dim]")
    sections.append("    [bold]?[/bold]           [dim]Toggle this help[/dim]")
    sections.append("    [bold]q[/bold]           [dim]Quit[/dim]")

    return "\n".join(sections)


class HelpOverlay(ModalScreen):  # type: ignore[type-arg]
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def __init__(self, screen: str = "cockpit") -> None:
        super().__init__()
        self._screen_context = screen

    def compose(self) -> ComposeResult:
        yield Static(format_help_text(self._screen_context), id="help-content")
