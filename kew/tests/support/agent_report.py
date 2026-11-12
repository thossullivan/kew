"""What an agent report looks like once kew has posted it on a PR.

kew quotes the agent's report, every line under ``> ``, between its own banner
and closing line, and adds zero-width spaces that only break markup. A report
with no ``<``, ``@`` or ``[^`` in it is quoted unchanged; tests that post such
a report compare against :func:`quoted`. :func:`visible_text` is what a
reader of the posted comment sees, line for line.
"""

from __future__ import annotations

_ZERO_WIDTH_SPACE = chr(0x200B)


def quoted(report: str) -> str:
    """*report* as kew posts it: each line quoted, a blank line as ``>``."""
    return "\n".join(
        f"> {line}" if line.strip() else ">" for line in report.splitlines()
    )


def visible_text(body: str) -> str:
    """A posted comment *body* without its quote markers or zero-width spaces."""
    lines = body.replace(_ZERO_WIDTH_SPACE, "").split("\n")
    return "\n".join(
        line[2:] if line.startswith("> ") else line.removeprefix(">") for line in lines
    )
