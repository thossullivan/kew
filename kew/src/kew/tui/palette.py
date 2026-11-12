"""kew's colour palette — the single system of record for terminal surfaces.

Every colour kew paints in the TUI or the CLI comes from here. Nothing else
under ``kew/tui/`` or ``kew/cli/`` may contain a hex literal or a named ANSI
colour — green, yellow, red and friends, whether in markup brackets, behind
Rich's ``on`` as a background, or held in a variable. Named colours resolve
through whatever theme the *user's terminal* happens to carry, which is exactly
how kew ended up with five disagreeing visual identities.
``tests/tui/test_palette.py`` enforces both rules.

System of record
----------------
The token values below mirror ``frontend/src/app.css`` **exactly** — that file
is the brand's source of truth and this module is its terminal-side transcript.
``docs/brand-guidelines.md`` documents the same set for humans. When a brand
colour changes it changes in ``app.css`` first, then here; the parity test in
``tests/tui/test_palette.py`` fails if the two drift.

The semantic contract
---------------------
Four signals carry meaning. Each one means exactly one thing on every surface —
TUI, web, docs, evidence pack:

===========  =========  =========  ==================================
Signal       Dark       Light      Meaning
===========  =========  =========  ==================================
Judgment     #ff2a6d    #c81e5a    A human must decide — approvals,
                                   denials, kill switch, P0
Machine      #05d9e8    #0892a5    An agent is working — live,
                                   streaming, focused
Money        #ffc94a    #b5790e    Spend, burn, caps. Dollars and
                                   nothing else
Attested     #2ee6a6    #128a5e    Chain-verified — merged, anchored,
                                   hash-matched
===========  =========  =========  ==================================

``JUDGMENT``/``MACHINE``/``MONEY``/``ATTESTED`` are aliases of the ``app.css``
tokens ``accent``/``accent-2``/``warn``/``success``. Prefer the contract name at
call sites that carry one of the four meanings — it is the thing being said —
and the structural name (``SURFACE``, ``BORDER``, ``MUTED``, …) for chrome.

``WARN`` and ``MONEY`` share a value in ``app.css``. That is deliberate and not
a contract violation: ``MONEY`` is for dollars, ``WARN`` is for non-blocking
degradation. They are spelled differently so a future divergence is a one-line
change — so the spelling has to be right at the call site now, while the two
are indistinguishable on screen. Use ``MONEY`` only when the thing being
coloured is spend (a dollar figure, a burn rate, a budget ratio, a token count,
a cap); everything else amber — a middling success rate, medium complexity, an
epic hold, CI still pending, the mid-step of a progress ramp — is ``WARN``.

The TUI is a dark surface, so the module-level constants are the dark theme.
The light values live in :data:`LIGHT` for parity with ``app.css`` and for any
future light-terminal support.
"""

from __future__ import annotations

import re
from typing import Final

# --- app.css token set, verbatim -------------------------------------------

DARK: Final[dict[str, str]] = {
    "bg": "#0d0221",
    "bg-glow": "#1a0b2e",
    "surface": "#1a0b2e",
    "surface-2": "#2d1b4e",
    "surface-3": "#0d0221",
    "border": "#2d1b4e",
    "border-strong": "#533a73",
    "fg": "#e0d8ea",
    "fg-dim": "#b8aad0",
    "muted": "#8b7fa8",
    "faint": "#6c5b7b",
    "accent": "#ff2a6d",
    "accent-bright": "#ff5c8f",
    "accent-dim": "#9c2148",
    "accent-fg": "#0d0221",
    "accent-2": "#05d9e8",
    "accent-2-fg": "#0d0221",
    "success": "#2ee6a6",
    "warn": "#ffc94a",
    "danger": "#ff4365",
    "info": "#05d9e8",
    "status-blocked": "#b48ade",
    "status-cancelled": "#6c5b7b",
    # Factory UI (direction D) — the web-only ink ground, hairlines and text
    # ramp from frontend/design/factory-ui/. Transcribed here because the
    # parity test requires the two token sets to match; the TUI paints on the
    # terminal's own background and uses none of them.
    "ink": "#0a0714",
    "ink-raised": "#0d0a18",
    "ink-selected": "#151025",
    "ink-track": "#1d1730",
    "hair": "#221c33",
    "hair-quiet": "#1a1528",
    "hair-strong": "#342a4a",
    "hair-box": "#2a2240",
    "guide": "#16111f",
    "text-1": "#ebe6f2",
    "text-2": "#b5acc8",
    "text-3": "#8d84a3",
    # Non-text: 3.4:1 on "ink" fails AA. Rules, ticks and marks only.
    "text-4": "#6a6082",
    "text-5": "#5a5070",
    "edge-done": "#3a3052",
}

LIGHT: Final[dict[str, str]] = {
    "bg": "#f4f0fb",
    "bg-glow": "#fbe6f1",
    "surface": "#fcfaff",
    "surface-2": "#f1ebf9",
    "surface-3": "#ece3f7",
    "border": "#e2d7f0",
    "border-strong": "#c9b8e0",
    "fg": "#1a0b2e",
    "fg-dim": "#4a3768",
    "muted": "#6c5b7b",
    "faint": "#9a8db5",
    "accent": "#c81e5a",
    "accent-bright": "#e0246a",
    "accent-dim": "#e59ab6",
    "accent-fg": "#fff5f9",
    "accent-2": "#0892a5",
    "accent-2-fg": "#f2fdff",
    "success": "#128a5e",
    "warn": "#b5790e",
    "danger": "#c81e3c",
    "info": "#0892a5",
    "status-blocked": "#7a4fa3",
    "status-cancelled": "#9a8db5",
    # Factory UI, provisional light: each token mapped onto the existing light
    # token that carries the same job. A designed light theme is design round 3.
    "ink": "#f4f0fb",
    "ink-raised": "#fcfaff",
    "ink-selected": "#f1ebf9",
    "ink-track": "#ece3f7",
    "hair": "#e2d7f0",
    "hair-quiet": "#f1ebf9",
    "hair-strong": "#c9b8e0",
    "hair-box": "#c9b8e0",
    "guide": "#ece3f7",
    "text-1": "#1a0b2e",
    "text-2": "#4a3768",
    "text-3": "#6c5b7b",
    "text-4": "#9a8db5",
    "text-5": "#c9b8e0",
    "edge-done": "#c9b8e0",
}

# --- structural tokens (dark; the TUI is a dark surface) --------------------

BG: Final = DARK["bg"]
BG_GLOW: Final = DARK["bg-glow"]
SURFACE: Final = DARK["surface"]
SURFACE_2: Final = DARK["surface-2"]
SURFACE_3: Final = DARK["surface-3"]
BORDER: Final = DARK["border"]
BORDER_STRONG: Final = DARK["border-strong"]
FG: Final = DARK["fg"]
FG_DIM: Final = DARK["fg-dim"]
MUTED: Final = DARK["muted"]
FAINT: Final = DARK["faint"]
ACCENT: Final = DARK["accent"]
ACCENT_BRIGHT: Final = DARK["accent-bright"]
ACCENT_DIM: Final = DARK["accent-dim"]
ACCENT_FG: Final = DARK["accent-fg"]
ACCENT_2: Final = DARK["accent-2"]
ACCENT_2_FG: Final = DARK["accent-2-fg"]
SUCCESS: Final = DARK["success"]
WARN: Final = DARK["warn"]
DANGER: Final = DARK["danger"]
INFO: Final = DARK["info"]
STATUS_BLOCKED: Final = DARK["status-blocked"]
STATUS_CANCELLED: Final = DARK["status-cancelled"]

# --- the four-signal semantic contract --------------------------------------

#: A human must decide — approvals, denials, kill switch, P0.
JUDGMENT: Final = ACCENT
#: An agent is working — live, streaming, focused.
MACHINE: Final = ACCENT_2
#: Spend, burn, caps. Dollars and nothing else.
MONEY: Final = WARN
#: Chain-verified — merged, anchored, hash-matched.
ATTESTED: Final = SUCCESS

#: One step below :data:`JUDGMENT` — P1, "urgent but not a stop-the-line".
#: Replaces the retired off-palette ``#ff7a59``.
ELEVATED: Final = ACCENT_BRIGHT

_VAR_RE: Final = re.compile(r"\$([a-z0-9-]+)")


def expand_css(source: str) -> str:
    """Expand ``$token`` references in a Textual CSS block to palette hexes.

    Textual CSS is a plain string, so it cannot reference Python constants.
    Wrapping a ``CSS``/``DEFAULT_CSS`` block in this function lets it name
    palette tokens (``background: $surface;``) instead of hardcoding hexes.
    Unknown ``$names`` are left untouched so Textual's own CSS variables still
    work.
    """
    return _VAR_RE.sub(lambda m: DARK.get(m.group(1), m.group(0)), source)
