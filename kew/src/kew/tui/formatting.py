"""Shared formatting utilities for TUI widgets."""


def format_tokens(n: int) -> str:
    """Format token count as compact string: 0, 1.2k, 45.0k, 1.2M."""
    if n == 0:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"
