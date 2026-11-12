"""The factory header's judgment count knows every source its rows come from.

``frontend/src/lib/shell/header-state.ts`` decides the "needs you" counter, and
the one thing it must never do is print a confident ``0`` for a queue nobody
read. ``/api/dashboard`` is fail-soft *per source*: a reader that raises is named
in ``unavailable`` and contributes no rows, so ``rows.length`` is a floor, not a
count. The header therefore blanks the counter when any source the rows are
assembled from is in ``unavailable``, and it names those sources in
``JUDGMENT_ROW_SOURCES`` — the backend's vocabulary transcribed into a second
language.

The rows themselves are the needs-you set, composed in
:func:`kew.service.needs_you.needs_you_set` and carried through
:func:`kew.service.dashboard.assemble` verbatim (#2207) — so the row sources are
read out of the first and the glance-only sources out of the second.

Nothing tied the two lists together, which is the whole reason for this module:
a seventh row source added to the set would leave the Python suite green
while the header served a confident count for a queue it had only partly read —
the exact bug ``JUDGMENT_ROW_SOURCES`` exists to prevent, reintroduced by an
edit that never opened the TypeScript.

Nothing here asserts what the header *does* with the list (that is
``header-state.test.ts``); this module only asserts that the list is the one
``assemble`` actually serves.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from support.source_ast import calls, definition, dotted

from kew.service import dashboard as dashboard_service
from kew.service import needs_you as needs_you_service

# Every check here reads the needs-you set and the glance off their syntax trees:
# the two languages' lists are one rule, kept in sync (#2491).
pytestmark = pytest.mark.policy

_HEADER_TS = "frontend/src/lib/shell/header-state.ts"
_ATTENTION_TS = "frontend/src/lib/epic/attention.ts"

# A `'name'` element of a TypeScript string array.
_TS_ITEM_RE = re.compile(r"'([a-z_]+)'")

# The work projection: not a row source itself, but the reading `work_rows`
# renders. When it fails, `_work_rows` succeeds over a `None` projection and
# stays silent, so its name is the only one that reaches `unavailable` — which
# is why the header watches it alongside the true row sources.
_PROJECTION_SOURCE = "pr_reviews"


def _frontend_file(relative: str) -> Path:
    """Locate a frontend source file by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _watched_sources() -> set[str]:
    """`JUDGMENT_ROW_SOURCES` in header-state.ts, parsed from its source."""
    return _ts_array(_HEADER_TS, "JUDGMENT_ROW_SOURCES")


def _bucket_sources() -> set[str]:
    """What an epic's needs-you count watches: `EPIC_BUCKET_SOURCES`.

    Composed in attention.ts as the header's watch list plus
    ``EPIC_PLACEMENT_SOURCES``, and read here the same way rather than
    transcribed — the spread is the point, so the two lists cannot drift.
    """
    return _watched_sources() | _ts_array(_ATTENTION_TS, "EPIC_PLACEMENT_SOURCES")


def _ts_array(relative: str, name: str) -> set[str]:
    """A `export const <name>` string-array literal, parsed from its source."""
    text = _frontend_file(relative).read_text(encoding="utf-8")
    _, sep, after = text.partition(f"export const {name}")
    assert sep, f"{name} not found in {relative}"
    # `= [`, not `[`: the declaration's `readonly string[]` annotation comes
    # first and would otherwise be mistaken for the start of the array.
    _, sep, body = after.partition("= [")
    assert sep, f"{name} is not an array literal in {relative}"
    body, sep, _ = body.partition("]")
    assert sep, f"{name} array not terminated in {relative}"
    names = set(_TS_ITEM_RE.findall(body))
    assert names, f"parsed no source names out of {name} in {relative}"
    return names


def _source_name(node: ast.expr) -> str | None:
    """The name a fail-soft ``source("name", ...)`` call reads under, if it is one.

    The name is the call's first argument and always a plain string literal; a
    computed one is a source no parity check could see, so it fails loudly.
    """
    if not (isinstance(node, ast.Call) and dotted(node.func) == "source"):
        return None
    assert node.args, f"a source() call names nothing (line {node.lineno})"
    first = node.args[0]
    assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
        f"a source() call is named by something other than a string literal "
        f"(line {node.lineno}) — this parity check can no longer read it."
    )
    return first.value


def _sources_in(*functions: object) -> set[str]:
    """Every fail-soft source the given functions read, off their syntax trees."""
    return {
        name
        for function in functions
        for call in calls(definition(function))
        if (name := _source_name(call)) is not None
    }


def _row_sources() -> set[str]:
    """Every fail-soft source in the needs-you set whose result becomes a row.

    A source becomes a row by being handed straight to ``.extend(...)`` on the
    set's entry list.
    """
    names = {
        name
        for call in calls(definition(needs_you_service.needs_you_set))
        if (dotted(call.func) or "").endswith(".extend")
        for arg in call.args
        if (name := _source_name(arg)) is not None
    }
    assert names, "parsed no row sources out of needs_you_set()"
    return names


def _all_sources() -> set[str]:
    """Every name the payload can put in ``unavailable``: both halves of its
    fail-soft reading, the needs-you set (the rows) and the glance ``assemble``
    composes around it."""
    names = _sources_in(needs_you_service.needs_you_set, dashboard_service.assemble)
    assert names, "parsed no sources out of needs_you_set()/assemble()"
    return names


def test_header_watches_every_row_source() -> None:
    """A row source the header does not watch is a confident zero waiting."""
    watched = _watched_sources()
    missing = sorted(name for name in _row_sources() if name not in watched)
    assert not missing, (
        f"needs_you_set() builds judgment rows from {missing}, which "
        f"JUDGMENT_ROW_SOURCES in {_HEADER_TS} does not watch — when one of "
        "these fails the header would print a confident count for a queue it "
        "only partly read. Add each name to that array."
    )


def test_header_watches_no_source_assemble_does_not_serve() -> None:
    """A name that no longer exists watches nothing and reads as watched."""
    served = _all_sources()
    unknown = sorted(name for name in _watched_sources() if name not in served)
    assert not unknown, (
        f"JUDGMENT_ROW_SOURCES in {_HEADER_TS} watches {unknown}, which neither "
        "needs_you_set() nor assemble() ever names in `unavailable` — a renamed "
        "or dropped source leaves a dead entry that silently guards nothing."
    )


def test_header_watches_only_row_sources_and_the_work_projection() -> None:
    """The one documented non-row source is the work projection, and only it.

    A glance-only source (the ready queue, the routing table) cannot produce a
    judgment row, so watching one would blank a count that was taken correctly —
    the opposite mistake, and just as wrong.
    """
    extra = _watched_sources() - _row_sources()
    assert extra == {_PROJECTION_SOURCE}, (
        f"JUDGMENT_ROW_SOURCES in {_HEADER_TS} watches non-row sources "
        f"{sorted(extra)}; only {_PROJECTION_SOURCE!r} belongs there, because "
        "it is the reading `work_rows` renders. Watching a glance-only source "
        "blanks a judgment count that was read correctly."
    )


def test_header_comment_names_every_glance_only_source() -> None:
    """The comment listing the deliberately-unwatched sources stays complete.

    The list is the file's evidence that each excluded source was considered.
    A new source in ``assemble`` that is absent from both the watch list and
    this list has been excluded by nobody — so a new name fails here until
    someone decides which of the two it belongs in.
    """
    text = _frontend_file(_HEADER_TS).read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z_]+)`", text))
    unconsidered = sorted(
        name
        for name in _all_sources() - _row_sources() - {_PROJECTION_SOURCE}
        if name not in named
    )
    assert not unconsidered, (
        f"the payload serves {unconsidered}, named nowhere in {_HEADER_TS} — "
        "decide whether each can produce a judgment row (add it to "
        "JUDGMENT_ROW_SOURCES) or cannot (add it to the glance-only list in "
        "that array's comment, which is the record that it was considered)."
    )


def test_the_epic_bucket_watches_every_source_the_needs_you_set_has() -> None:
    """A per-epic count is unknowable for exactly the set's own failures.

    ``needs_you_by_epic`` is the set filtered by epic, so every source inside
    ``needs_you_set`` can change a bucket — the row sources by losing an entry,
    and the epic walk by leaving an entry in no bucket at all — while nothing
    in ``assemble``'s glance can. ``epicNeedsYou`` therefore reads ``known``
    off exactly this list.

    Both directions are failures with a surface attached. Watching too little
    prints a confident number over a set composed from half its readers.
    Watching too much is what this test was written for: reading ``known`` off
    the WHOLE ``unavailable`` list made the gh-backed ``ready_queue`` — which
    goes unavailable on a 20s timeout and then backs off for 30s — print "?" on
    every epic legend and every Epics-list row while the header beside them
    printed an exact count from the same payload.
    """
    watched = _bucket_sources()
    composed = _sources_in(needs_you_service.needs_you_set)
    assert composed, "parsed no sources out of needs_you_set()"
    assert watched == composed, (
        f"EPIC_BUCKET_SOURCES in {_ATTENTION_TS} watches {sorted(watched)}, but "
        f"needs_you_set() composes the set from {sorted(composed)}. Every source "
        "there can change a per-epic bucket and nothing outside it can, so the "
        "two lists are the same list in two languages."
    )


def test_the_epic_bucket_adds_only_sources_that_place_rather_than_produce() -> None:
    """The extension over the header's list is placement, never a row source.

    ``EPIC_PLACEMENT_SOURCES`` exists because the header deliberately does NOT
    watch ``node_epics``: a failed epic walk loses no row, so the header's
    count stays exact while the buckets below it go short. Anything that
    produces rows belongs in ``JUDGMENT_ROW_SOURCES`` instead, where the header
    sees it too.
    """
    placement = _ts_array(_ATTENTION_TS, "EPIC_PLACEMENT_SOURCES")
    rows = _row_sources()
    overlap = sorted(placement & rows)
    assert not overlap, (
        f"EPIC_PLACEMENT_SOURCES in {_ATTENTION_TS} names {overlap}, which "
        "needs_you_set() builds judgment rows from — a row source belongs in "
        f"JUDGMENT_ROW_SOURCES in {_HEADER_TS}, which the header watches too."
    )
