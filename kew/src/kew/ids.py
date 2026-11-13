"""Opaque work-item id validation and formatting.

Ids come from external trackers and reach branch names, filesystem paths
(worktrees), audit payloads, and logs — validate at the boundary, reject
(never sanitize) anything non-canonical. Spec:
docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §A1/§A4.
"""

import re
from typing import Protocol

GITHUB_ID_RE = re.compile(r"^[0-9]+$")
LINEAR_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
GITHUB_CLOSES_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<item_id>\d+)",
    re.IGNORECASE,
)

#: Widest a decimal issue number can be and still bind as a SQLite integer.
#: int64 is SQLite's widest integer type: every 18-digit decimal fits, 19 may
#: not, and sqlite3 raises ``OverflowError`` rather than binding a wider one.
#: An id that long is no issue number either way, so readers drop it alongside
#: the non-numeric ids instead of failing the read.
MAX_ISSUE_NUM_DIGITS = 18

#: The same limit as a range, for values that arrive already parsed as ints.
#: An integer query param is bound straight into SQLite, so a reader must bound
#: it at the boundary or sqlite3 answers the request with ``OverflowError`` —
#: HTTP 500 for a value no column could hold (#2247).
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1

_PATTERNS: dict[str, re.Pattern[str]] = {
    "github": GITHUB_ID_RE,
    "linear": LINEAR_ID_RE,
}


class InvalidItemIdError(ValueError):
    """Work-item id does not match the provider's canonical form."""


def validate_item_id(raw: str, provider: str = "github") -> str:
    """Return the canonical item id or raise InvalidItemIdError."""
    pattern = _PATTERNS.get(provider)
    if pattern is None:
        raise InvalidItemIdError(f"unknown tracker provider: {provider!r}")
    if not pattern.fullmatch(raw):
        raise InvalidItemIdError(
            f"item id {raw!r} is not canonical for provider {provider!r}"
        )
    return raw


def branch_slug(item_id: str) -> str:
    """Branch/path-safe form of a validated item id (lowercase)."""
    return item_id.lower()


def item_id_sort_key(item_id: str) -> tuple[int, int | str]:
    """Numeric-aware sort key: numeric ids first in numeric order."""
    if GITHUB_ID_RE.fullmatch(item_id):
        return (0, int(item_id))
    return (1, item_id)


def item_ref(item_id: str) -> str:
    """Human-facing reference for a work item.

    ``#N`` only when the id matches :data:`GITHUB_ID_RE` (GitHub renders that as
    a live cross-link); any other tracker's id is shown bare (``SFD-12``).
    """
    if GITHUB_ID_RE.fullmatch(item_id):
        return f"#{item_id}"
    return item_id


def item_ref_or_num(item_id: str | None, issue_num: int) -> str:
    """Display ref for a record that may predate the durable ``item_id``.

    Prefers the opaque ``item_id`` (``#5`` for GitHub, ``SFD-12`` bare via
    :func:`item_ref`); falls back to the legacy int ``issue_num`` rendered as
    ``#N`` for rows written before the ``item_id`` column existed (or in-memory
    records that never set it). Byte-identical to the old ``#{issue_num}`` for
    every GitHub id.
    """
    if item_id is not None:
        return item_ref(item_id)
    return f"#{issue_num}"


def closes_marker(item_id: str) -> str:
    """PR-body closing reference for a work item.

    GitHub numeric ids get the ``Closes #N`` auto-close marker GitHub resolves
    on merge. Non-numeric tracker ids (Linear) carry the bare item id instead —
    never a ``Closes`` marker GitHub can't resolve; the PR↔item link is recovered
    from the branch lookup (A6).
    """
    if GITHUB_ID_RE.fullmatch(item_id):
        return f"Closes #{item_id}"
    return item_id


def ensure_closes_marker(body: str, item_id: str) -> str:
    """Return a GitHub PR body carrying this item's closing reference."""
    if not GITHUB_ID_RE.fullmatch(item_id):
        return body
    if any(match["item_id"] == item_id for match in GITHUB_CLOSES_RE.finditer(body)):
        return body
    return f"{body}\n\n{closes_marker(item_id)}"


class _TrackerLike(Protocol):
    # Read-only (property) so a concrete ``type: Literal["github","linear"]``
    # is accepted — a plain mutable ``type: str`` attribute would be invariant.
    @property
    def type(self) -> str: ...


class _ConfigLike(Protocol):
    @property
    def tracker(self) -> _TrackerLike: ...


def provider_from_config(config: _ConfigLike) -> str:
    """Return the tracker provider string from a config object.

    Reads config.tracker.type — a Literal["github","linear"] set in TrackerConfig.
    """
    return config.tracker.type
