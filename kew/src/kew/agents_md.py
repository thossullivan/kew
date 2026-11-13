"""The kew block ``kew setup`` writes into a repository's ``AGENTS.md``.

Codex reads ``AGENTS.md``, and so do other agents, so this is where a repo tells
them kew exists. kew owns only the text between its two marker lines: the file
is created when absent, the block is appended when the file has none, and an
existing block is replaced in place. Every byte outside the markers is left as
it was, line endings included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

BEGIN_MARKER = "<!-- kew:begin -->"
END_MARKER = "<!-- kew:end -->"

AGENTS_MD = "AGENTS.md"

BLOCK_BODY = """\
## kew

This repository is driven by [kew](https://kew.dev), a control plane that
dispatches coding agents onto GitHub issues labelled `ready` and opens pull
requests for human review.

Run these first, in order:

1. `kew doctor --json` — what is broken; each failing check names its `fix`.
2. `kew init --yes` — writes `kew.toml` when the project has none.
3. `kew schema` — every command, option and exit-code family, as JSON.

Approvals, `ready` labels and merges are the human's: never perform them.
The full guide is the `operate-kew` skill (`kew skills install operate-kew`).
"""

BLOCK = f"{BEGIN_MARKER}\n{BLOCK_BODY}{END_MARKER}"

AgentsMdAction = Literal["created", "appended", "updated", "unchanged"]


class MalformedBlockError(ValueError):
    """``AGENTS.md`` has kew markers kew cannot pair up into one block."""


def _replace_block(text: str) -> str | None:
    """Return *text* with its kew block replaced, or None when it has none."""
    begins = text.count(BEGIN_MARKER)
    ends = text.count(END_MARKER)
    if begins == 0 and ends == 0:
        return None
    start = text.find(BEGIN_MARKER)
    stop = text.find(END_MARKER)
    if begins != 1 or ends != 1 or stop < start:
        raise MalformedBlockError(
            f"{AGENTS_MD} needs exactly one {BEGIN_MARKER} followed by one "
            f"{END_MARKER}; found {begins} and {ends}"
        )
    return text[:start] + BLOCK + text[stop + len(END_MARKER) :]


def _appended(text: str) -> str:
    """Return *text* with the block added after a blank line."""
    if not text:
        return f"{BLOCK}\n"
    separator = "\n" if text.endswith("\n") else "\n\n"
    return f"{text}{separator}{BLOCK}\n"


def write_agents_block(root: Path) -> AgentsMdAction:
    """Create, append or replace the kew block in ``root/AGENTS.md``.

    Raises :class:`MalformedBlockError` when the markers do not form one
    block, and ``UnicodeDecodeError`` when the file is not UTF-8; the file is
    left untouched in both cases.
    """
    path = root / AGENTS_MD
    if not path.exists():
        path.write_bytes(f"{BLOCK}\n".encode())
        return "created"
    text = path.read_bytes().decode("utf-8")
    replaced = _replace_block(text)
    new_text = _appended(text) if replaced is None else replaced
    if new_text == text:
        return "unchanged"
    path.write_bytes(new_text.encode())
    return "appended" if replaced is None else "updated"
