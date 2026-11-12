"""Acceptance criteria on a planned issue: read them, refuse boilerplate.

`kew epic plan` used to stamp the same four lines into every issue it
generated ("All new and existing tests pass", "CI gate passes", ...). A review
station that extracts an issue's criteria and judges the merged work against
them would, given that body, judge it against nothing. So the planning
contract now demands 2-6 item-specific, checkable criteria per issue — each a
statement of an observable outcome plus, where possible, the command that
proves it — and this module is the gate.

:func:`extract_criteria` reads the ``## Acceptance Criteria`` checklist out of
a rendered issue body; :func:`criteria_problems` reports the four ways a draft
fails it (missing, too few or too many, generic, still a placeholder). The
deny-list is fixed and deliberately small: it names the boilerplate kew itself
used to emit, so a draft that merely copies the old template is refused by name
rather than silently accepted.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: A reviewer cannot judge intent from one line; more than six means the node
#: is really several nodes. Both bounds are the planning contract, quoted to
#: the agent by `_templates/epic-plan.md.j2` and enforced by `graph/plan.py`.
CRITERIA_MIN = 2
CRITERIA_MAX = 6

_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s*(\S.*?)\s*$")
_CRITERIA_HEADING = "acceptance criteria"

# Dropped before the deny-list match: the proving command rides in backticks
# and parentheses, and it is the *statement* that has to be specific.
_PARENTHETICAL = re.compile(r"\([^()]*\)")
_NOISE = re.compile(r"[`*_\"'.,;:!]")

# An unfilled placeholder. The template and the plan-to-issues skill print a
# bracketed skeleton ("- [ ] [Observable outcome these tasks produce]"), and a
# draft that copies it has written no criterion at all — the deny-list cannot
# see that, because the words differ per skeleton line. The prompt promises any
# `[...]` is refused, so one word is enough to be a placeholder: `[Outcome]`
# and `[TODO]` are stubs too. What the brackets must NOT be is syntax, which is
# what the three guards spare: the lookbehind spares a subscript or annotation
# written against the preceding token (`rows[0]`, `list[str]`, `f(x)[i]`), the
# letter spares an empty or numeric span (`returns []`), and the lookahead
# spares a markdown link. Code spans are dropped first, so a bracket inside
# backticks never counts at all.
_CODE_SPAN = re.compile(r"`[^`]*`")
_PLACEHOLDER = re.compile(r"(?<![\w\])])\[[^\]]*[^\W\d_][^\]]*\](?!\()")

# Fixed deny-list of criteria that are true of every issue and so prove
# nothing about this one. Matched against the normalized statement in full.
_GENERIC = (
    r"(all )?(the )?(new and existing |existing |new |unit |full )*tests?"
    r"( suite)? (pass|passes|passing|are passing|is green|are green)",
    r"(the )?(full )?test suite (passes|pass|is green|runs clean)",
    r"ci( gate| check| checks| pipeline| run)? (pass|passes|is green|are green)",
    r"(all )?ci checks? (pass|passes|are green)",
    r"(the )?(format|formatting|lint|linting|style|type) "
    r"(check|checks|gate)? ?(pass|passes|is clean|are clean)",
    r"(ruff|mypy|pytest|xenon|bandit)( check| format)*( \.)? ?"
    r"(pass|passes|is clean|runs clean)",
    # The range is written as digits in a real draft and as "N-M" in the
    # template that names this criterion as refused, so the span is any word
    # either side of a dash, en dash or "to".
    r"all tasks( \w+ ?[-–—] ?\w+| \w+ to \w+)?( from the plan)? "
    r"(are )?(implemented|complete|completed|done)",
    r"(the )?code (is )?(reviewed|clean|working)",
    r"(the )?docs?(umentation)? (is |are )?(updated|written)",
    r"no regressions?",
    r"nothing (is )?broken",
    r"it works( as expected)?",
    r"(the )?(feature|change|work) (is )?(done|complete|implemented|working)",
    r"(full )?verification (passes|pass|runs clean)",
    r"coverage (does not drop|is maintained|holds)",
)
_GENERIC_RE = tuple(re.compile(pattern) for pattern in _GENERIC)


def extract_criteria(body: str | None) -> list[str]:
    """Return the checklist items under the body's ``## Acceptance Criteria``.

    The first such heading wins and the section ends at the next heading of any
    level, so a body that repeats the words later cannot smuggle extra
    criteria in. Prose, blank lines and plain (uncheckboxed) bullets inside the
    section are ignored, so an introductory sentence does not break the read.
    """
    if not body:
        return []
    items: list[str] = []
    inside = False
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if inside:
                break
            inside = heading.group(1).strip().rstrip(":").lower() == _CRITERIA_HEADING
            continue
        item = _CHECKBOX.match(line) if inside else None
        if item:
            items.append(item.group(1))
    return items


def _normalize(criterion: str) -> str:
    """The criterion's statement, lowercased, with command and punctuation out."""
    without_command = _PARENTHETICAL.sub(" ", criterion)
    return " ".join(_NOISE.sub(" ", without_command.lower()).split())


def is_generic_criterion(criterion: str) -> bool:
    """True when the criterion is boilerplate that proves nothing item-specific."""
    normalized = _normalize(criterion)
    return any(pattern.fullmatch(normalized) for pattern in _GENERIC_RE)


def is_placeholder_criterion(criterion: str) -> bool:
    """True when the criterion still carries the template's bracketed skeleton."""
    return bool(_PLACEHOLDER.search(_CODE_SPAN.sub(" ", criterion)))


def _count_problems(criteria: list[str], label: str) -> list[str]:
    """The bounds half of the contract: too few criteria, or too many."""
    problems: list[str] = []
    if len(criteria) < CRITERIA_MIN:
        problems.append(
            f"{label}: {len(criteria)} acceptance criterion is below the minimum "
            f"{CRITERIA_MIN} — a reviewer cannot judge the work from one line"
        )
    if len(criteria) > CRITERIA_MAX:
        problems.append(
            f"{label}: {len(criteria)} acceptance criteria exceeds the maximum "
            f"{CRITERIA_MAX} — a node needing more than {CRITERIA_MAX} is more "
            "than one node; re-split it"
        )
    return problems


#: Per-criterion refusals, each reported once with every offending line named:
#: the predicate that spots it and the problem it reports. Both are ways of
#: writing a criterion that judges the work against nothing.
_REFUSALS: tuple[tuple[Callable[[str], bool], str], ...] = (
    (
        is_generic_criterion,
        "generic acceptance criteria {listed} — each criterion must be specific "
        "to this issue (name the behaviour, symbol or command it proves); "
        "boilerplate true of every issue is refused",
    ),
    (
        is_placeholder_criterion,
        "unfilled acceptance criteria {listed} — the template prints a bracketed "
        "skeleton; replace each one with the outcome these tasks actually "
        "produce and the command that proves it",
    ),
)


def _refusal_problems(criteria: list[str], label: str) -> list[str]:
    """The per-criterion half: one problem per refusal, naming its lines."""
    problems: list[str] = []
    for spots, message in _REFUSALS:
        flagged = [c for c in criteria if spots(c)]
        if flagged:
            listed = ", ".join(repr(c) for c in flagged)
            problems.append(f"{label}: {message.format(listed=listed)}")
    return problems


def criteria_problems(body: str | None, label: str) -> list[str]:
    """Report every way ``body``'s acceptance criteria fail the contract.

    ``label`` prefixes each problem (the draft's ``local_id``, quoted, in the
    `kew epic plan` caller). An empty list means the criteria are creatable.
    """
    criteria = extract_criteria(body)
    if not criteria:
        return [
            f"{label}: no '## Acceptance Criteria' checklist — write "
            f"{CRITERIA_MIN}-{CRITERIA_MAX} item-specific, checkable criteria, "
            "one '- [ ] ' line each, naming the command that proves it"
        ]
    return _count_problems(criteria, label) + _refusal_problems(criteria, label)
