"""Shared token accumulation over a runner's StreamEvents (#265).

The single source of truth for turning parsed stream events into run token
totals, used by BOTH the orchestrator (production metering) and the canary, so
they can never diverge.
"""

from __future__ import annotations

from dataclasses import dataclass

from kew.runners.protocol import StreamEvent


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tool_uses: int = 0


def event_contributes_usage(ev: StreamEvent) -> bool:
    """True when an event carries usage and is NOT a 'step' event.

    claude-code and opencode each emit a paired 'usage'+'step' for the same turn
    (claude_code.py / opencode.py); summing both double-counts tokens (#310/#311).
    'step' is therefore excluded from accumulation here — the one gate both the
    orchestrator and the canary share.
    """
    has_tokens = bool(
        ev.input_tokens
        or ev.output_tokens
        or ev.cache_creation_tokens
        or ev.cache_read_tokens
    )
    return bool(has_tokens or ev.tool_uses) and ev.event_type != "step"


def accumulate_usage(events: list[StreamEvent]) -> UsageTotals:
    """Sum token usage across events, skipping 'step' (see event_contributes_usage)."""
    inp = out = cache_create = cache_read = tools = 0
    for ev in events:
        if event_contributes_usage(ev):
            inp += ev.input_tokens
            out += ev.output_tokens
            cache_create += ev.cache_creation_tokens
            cache_read += ev.cache_read_tokens
            tools += ev.tool_uses
    return UsageTotals(inp, out, cache_create, cache_read, tools)
