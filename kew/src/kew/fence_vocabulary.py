"""The reserved fence vocabulary, free of the dispatch stack (#3020).

:mod:`kew.prompt` fences untrusted bodies with sentinels drawn from this
vocabulary and strips it from everything else it renders. The service layer
needs the same strip for an operator's prompt-profile addendum, but may not
import :mod:`kew.prompt` (it reaches ``kew.graph`` through ``kew.worktree``),
so the vocabulary lives here and :mod:`kew.prompt` re-exports it.

Reserved fence vocabulary. ``make_sentinel`` draws the hex, but the prefix is
fixed, so a content author who cannot guess the per-render token CAN still
type a sentinel-SHAPED one. Readers that recover the host-authored spans of a
rendered prompt do so by walking its fence boundaries
(:func:`kew.self_check._host_authored`), and a stray sentinel-shaped token in
a quoted body used to shift every span after it. So the vocabulary is removed
from untrusted bodies before they are fenced: a full sentinel-shaped token
when the body spells one, and the bare prefix otherwise, which keeps
``prefix count == 2 * fenced blocks`` true for every prompt kew renders
(#2454 round 3), in the unfenced slots as much as in the fenced ones
(#2454 round 4).
"""

from __future__ import annotations

import re

SENTINEL_PREFIX = "<<<KEW-UNTRUSTED-"
_SENTINEL_IN_BODY = re.compile(rf"{re.escape(SENTINEL_PREFIX)}(?:[0-9a-f]{{32}}>>>)?")
UNTRUSTED_SENTINEL_MARKER = "[kew: sentinel-shaped marker removed]"


def desentinel(text: str, keep: str = "") -> str:
    """Return *text* with every sentinel-shaped token neutralized but *keep*.

    *keep* is the one spelling that survives: the per-render sentinel the host
    is fencing WITH, so a string that already carries a host-authored fence
    passes through intact. Everything else the reserved vocabulary covers — a
    full token with foreign hex, or a bare :data:`SENTINEL_PREFIX` — becomes
    :data:`UNTRUSTED_SENTINEL_MARKER`.

    Typed shapes are neutralized rather than rejected: the prefix is public, so
    refusing one would let any issue block its own dispatch. An EXACT match on
    the per-render token is a different event and the fenced path raises for it
    (:class:`kew.prompt.SentinelCollision`); it is not reachable by guessing.
    """
    if not keep:
        return _SENTINEL_IN_BODY.sub(UNTRUSTED_SENTINEL_MARKER, text)
    return keep.join(
        _SENTINEL_IN_BODY.sub(UNTRUSTED_SENTINEL_MARKER, part)
        for part in text.split(keep)
    )
