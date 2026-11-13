"""Extract an item's acceptance criteria from the body it already states them in.

This is the extract-only half of the triage station (#2290): no model, no
network, no tracker read of its own. The host hands in the issue body it already
fetched for the dispatch prompt and gets back the criteria the body states, in
body order, verbatim.

Why verbatim: kew's adversarial reviewer is told to judge a diff against "the
acceptance criteria it claims to satisfy", and a paraphrased criterion is a
different contract. The extractor therefore keeps each statement exactly as
written and never invents a ``check``. The one markup it reads is a trailing
``(kind: test|behavior|static)`` marker the epic planner writes (#2727): that is
recorded as the criterion's ``kind`` and stripped from the statement, like a
task checkbox. A criterion without the marker stays ``behavior``.

Scope of the grammar, deliberately small:

- The section is the FIRST heading matching :data:`_ACCEPTANCE_HEADING`
  (``##``..``####``, "Acceptance" or "Acceptance Criteria"). A ``#`` title and a
  ``#####`` sub-sub-heading are not section headings here.
- It ends at the next heading of the same or higher level. A DEEPER heading
  stays inside, so a ``### Gates`` block under ``## Acceptance`` still counts.
- Inside it, every bullet (``-``/``*``/``+``), numbered item (``1.``/``1)``) and
  task checkbox is one criterion — at any indent. A nested bullet is a criterion
  too: dropping it would silently lose part of the contract.
- One criterion may span several physical lines. A line that opens no item of
  its own continues the item above it, because a soft-wrapped criterion is one
  statement and keeping only its first line would record a truncated contract
  as if it were the whole one. A blank line or a heading closes the item.
- Fenced code is invisible to all of the above, so a heading or a bullet quoted
  in a ``` block is never mistaken for the item's own contract.

No acceptance section means an empty list. That is a real answer — "this body
states no criteria" — not a failure, and the ledger row records
``criteria_count = 0`` for it.
"""

from __future__ import annotations

import hashlib
import json
import re

from kew.triage.schema import AcceptanceCriterion, CriterionKind, ItemPlan

_ACCEPTANCE_HEADING = re.compile(r"^(#{2,4})\s*acceptance( criteria)?\b", re.IGNORECASE)
"""The section heading that opens an item's criteria."""

_HEADING = re.compile(r"^(#{1,6})\s+\S")

_FENCE = re.compile(r"^\s*(```+|~~~+)")

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<statement>\S.*?)\s*$")

_CHECKBOX = re.compile(r"^\[[ xX]\]\s+(?P<statement>\S.*)$")

_KIND_MARKER = re.compile(
    r"^(?P<statement>\S.*?)\s*\(kind:\s*(?P<kind>test|behavior|static)\)$"
)
"""The planner's trailing kind marker, e.g. ``... (kind: test)``."""

_KINDS: dict[str, CriterionKind] = {
    "test": "test",
    "behavior": "behavior",
    "static": "static",
}


def _unfenced_lines(body: str) -> list[tuple[str, bool]]:
    """Each line paired with whether it sits inside a fenced code block.

    Only the opening fence's marker closes the block, so a ``~~~`` inside a
    ``` block does not end it early.
    """
    paired: list[tuple[str, bool]] = []
    open_marker: str | None = None
    for line in body.splitlines():
        fence = _FENCE.match(line)
        if open_marker is None and fence is not None:
            open_marker = fence.group(1)[:3]
            paired.append((line, True))
            continue
        if open_marker is not None:
            paired.append((line, True))
            if fence is not None and fence.group(1).startswith(open_marker):
                open_marker = None
            continue
        paired.append((line, False))
    return paired


def _section_lines(body: str) -> list[str]:
    """The prose lines of the first acceptance section, fenced code excluded."""
    lines = _unfenced_lines(body)
    level: int | None = None
    collected: list[str] = []
    for line, fenced in lines:
        if fenced:
            continue
        heading = _HEADING.match(line)
        if level is None:
            opener = _ACCEPTANCE_HEADING.match(line)
            if opener is not None:
                level = len(opener.group(1))
            continue
        if heading is not None and len(heading.group(1)) <= level:
            break
        collected.append(line)
    return collected


def _opening_statement(line: str) -> str | None:
    """The statement *line* opens as a list item, or None if it opens none.

    A task checkbox is markup rather than part of the contract, so ``[ ]`` /
    ``[x]`` is stripped from the front of the statement it marks.
    """
    item = _BULLET.match(line)
    if item is None:
        return None
    statement = item.group("statement")
    checkbox = _CHECKBOX.match(statement)
    return checkbox.group("statement") if checkbox is not None else statement


def _folded_statements(lines: list[str]) -> list[str]:
    """One statement per item, soft-wrapped continuation lines folded in.

    A criterion written (or editor-wrapped) across several lines is still one
    criterion, and recording only its first physical line would store part of a
    statement as if it were the whole contract. So a line that opens no item of
    its own extends the item above it, joined by a single space — the wrap's
    indentation is cosmetic, and markdown's lazy continuation allows none at
    all. A blank line or a heading closes the open item, so a paragraph
    trailing the last bullet is not absorbed into it.
    """
    statements: list[str] = []
    open_item = False
    for line in lines:
        opening = _opening_statement(line)
        continuation = line.strip()
        if opening is not None:
            statements.append(opening)
            open_item = True
        elif not continuation or _HEADING.match(line) is not None:
            open_item = False
        elif open_item:
            statements[-1] = f"{statements[-1]} {continuation}"
    return statements


def extract_criteria(body: str | None) -> list[AcceptanceCriterion]:
    """The criteria *body* states, as ``AC1``..``ACn`` in body order.

    Returns an empty list when the body states no acceptance section.
    """
    statements = _folded_statements(_section_lines(body or ""))
    return [
        _criterion(f"AC{index}", statement)
        for index, statement in enumerate(statements, start=1)
    ]


def _criterion(criterion_id: str, statement: str) -> AcceptanceCriterion:
    """One criterion, its kind read off a trailing marker (``behavior`` if none)."""
    marker = _KIND_MARKER.match(statement)
    if marker is None:
        return AcceptanceCriterion(id=criterion_id, statement=statement)
    return AcceptanceCriterion(
        id=criterion_id,
        statement=marker.group("statement"),
        kind=_KINDS[marker.group("kind")],
    )


def extract_plan(body: str | None) -> ItemPlan:
    """The plan an extract-only station can record: the criteria and nothing else.

    The analyst fields stay ``None``/empty on purpose — a later station fills
    them from a model, and a reader can tell an extracted plan from a drafted
    one by the row's ``source`` plus these empty fields.
    """
    return ItemPlan(acceptance_criteria=extract_criteria(body))


def canonical_plan_json(plan: ItemPlan) -> str:
    """The plan's one canonical serialization — what ``plan_sha`` digests.

    Sorted keys and tight separators, so the same plan always hashes the same
    way regardless of field declaration order or pydantic version.
    """
    return json.dumps(
        plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def plan_sha(plan: ItemPlan) -> str:
    """sha256 of :func:`canonical_plan_json` — the plan's identity.

    Two bodies that state the same criteria share a plan sha even when the prose
    around them differs; that is the point. The BODY's identity is
    :func:`body_digest`, and supersession keys off that.
    """
    return hashlib.sha256(canonical_plan_json(plan).encode("utf-8")).hexdigest()


def body_digest(body: str | None) -> str:
    """sha256 of the issue body exactly as the host read it."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()
