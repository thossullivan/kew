"""The Work screen re-reads a description only where the tracker was never asked.

:mod:`kew.service.description` refuses a read for two quite different sorts of
reason, and the difference is invisible in the payload's ``availability``:

* a refusal at one of its three gates — another read of the same item is out,
  the process-wide in-flight bound is full, the minute's read budget is spent —
  where the request never left the box, so it cost the tracker nothing and will
  clear on its own within a window;
* a reading that REACHED the tracker and failed (``gh`` could not read the issue,
  the Linear read failed, the response would not parse), which already spent one
  of the :data:`~kew.service.description.MAX_READS_PER_WINDOW` reads a minute the
  whole process shares with the dispatch driver.

``rereadsDescription`` in ``frontend/src/lib/work/evidence.ts`` takes the first
sort again on the shell's 7s pulse, up to ``DESCRIPTION_RETRIES`` times, and must
never take the second: eleven reads of one unreadable item, to be told the same
thing eleven times, ration away every other item's — and every other operator's —
cold read for the rest of the window (the #2155 review finding).

It tells them apart by the words the gates end with, transcribed into
``DESCRIPTION_UNATTEMPTED``. That transcription is what this module anchors:
the marker must be carried by the three gate refusals and by nothing else this
module can raise. Reword a gate, or word a tracker failure with the same tail,
and the retry silently changes which reads it multiplies — with nothing in
either language's own tests to notice, because each holds only its own copy.

Nothing here asserts what the frontend DOES with the marker (``evidence.test.ts``
pins that). This module asserts that the marker still means what it says.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from support.source_ast import module_tree

from kew.service import description as description_service

_EVIDENCE_TS = "frontend/src/lib/work/evidence.ts"

#: The refusals that never reach the tracker AND clear on their own, which is
#: the whole set the Work screen may take again. The settled free refusals (no
#: ``project.repo``, an id that is not an issue number, no Linear key, a tracker
#: with no read) cost nothing either, but a tick changes none of them.
_UNATTEMPTED = frozenset({"BUSY_REASON", "SAME_ITEM_REASON", "BUDGET_REASON"})

# `export const DESCRIPTION_UNATTEMPTED = '…';` — a single-quoted literal, as
# every other exported string in that module is.
_MARKER_RE = re.compile(r"export const DESCRIPTION_UNATTEMPTED\s*=\s*'([^']+)'")
# The one function that is allowed to decide a re-read, and its body up to the
# closing brace at column 0.
_REREADS_RE = re.compile(
    r"^export function rereadsDescription\((?:.|\n)*?^\}$", re.MULTILINE
)


def _frontend_file(relative: str) -> Path:
    """Locate a frontend source file by walking up from this test."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    pytest.skip(f"{relative} not present in this checkout")


def _evidence_ts() -> str:
    return _frontend_file(_EVIDENCE_TS).read_text(encoding="utf-8")


def _marker() -> str:
    """``DESCRIPTION_UNATTEMPTED``, parsed from the frontend's own source."""
    match = _MARKER_RE.search(_evidence_ts())
    assert match, (
        "DESCRIPTION_UNATTEMPTED is not an exported string literal in "
        f"{_EVIDENCE_TS} — it is the one thing that tells a free refusal from a "
        "spent tracker read, and this check cannot read it."
    )
    marker = match.group(1)
    # A short marker would match half the reasons in the module by accident, and
    # an empty one matches all of them: the retry would be back where it started.
    assert len(marker) >= len("not attempted"), (
        f"DESCRIPTION_UNATTEMPTED in {_EVIDENCE_TS} is {marker!r}, too short to "
        "identify a refusal — a loose substring would re-read tracker failures "
        "as well, which is the spend this marker exists to prevent."
    )
    return marker


def _reason_constants() -> dict[str, str]:
    """Every public reason string :mod:`kew.service.description` can serve.

    Read off the module rather than out of its source: the reasons are plain
    module-level constants, and every public upper-case string there is one.
    """
    reasons = {
        name: value
        for name, value in vars(description_service).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    assert reasons, "parsed no reason constants out of kew.service.description"
    missing = sorted(_UNATTEMPTED - set(reasons))
    assert not missing, (
        f"kew.service.description no longer defines {missing} — the Work screen's "
        "re-read is keyed on the wording of those gate refusals, so a rename "
        "here is a retry rule that quietly covers something else. Update "
        "_UNATTEMPTED with the new names."
    )
    return reasons


def _wording(node: ast.expr, reasons: dict[str, str]) -> str:
    """The text a ``TrackerReadError`` argument expression produces.

    A reason constant interpolated into an f-string is resolved — that is how
    every tracker failure here is worded, so leaving it as a placeholder would
    hide exactly the wording this module checks. An interpolated exception or
    limit becomes ``…``: what matters is the wording the module chose, not the
    value it drops into it. Only the forms the module actually uses are
    understood — a bare constant, a ``.format`` of one, a plain literal, and an
    f-string over them — because a reason this check cannot read is one it would
    otherwise pass in silence.
    """
    if isinstance(node, ast.Name):
        assert node.id in reasons, (
            f"TrackerReadError is raised with {node.id}, which is not a public "
            f"reason constant of kew.service.description (line {node.lineno})"
        )
        return reasons[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _wording(node.func.value, reasons)
    if isinstance(node, ast.JoinedStr):
        return "".join(_wording(part, reasons) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        inner = node.value
        if isinstance(inner, ast.Name) and inner.id in reasons:
            return reasons[inner.id]
        return "…"
    raise AssertionError(
        "a TrackerReadError is worded with something this parity check cannot "
        f"read ({ast.dump(node)[:120]}) — it can no longer see whether that "
        "reason carries the re-read marker. Keep the wording a constant, a "
        "literal or an f-string over them, or teach _wording() the new form."
    )


def _named(node: ast.expr, text: str) -> str:
    """The reason constant a wording came from, or the wording itself.

    Only a name makes a useful key — a failure message that interpolates the
    exception into a literal has no constant to be named by, so it is keyed by
    what it says, which is what the frontend matches anyway.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    ):
        return node.func.value.id
    if isinstance(node, ast.Name):
        return node.id
    return text


def _raised_reasons() -> dict[str, str]:
    """Every ``raise TrackerReadError(...)`` wording, by how it was named.

    Keyed by the reason constant where there is one, and by the wording itself
    for the tracker failures that interpolate an exception into a literal.
    """
    reasons = _reason_constants()
    raised: dict[str, str] = {}
    for node in ast.walk(module_tree(description_service)):
        if not (
            isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "TrackerReadError"
        ):
            continue
        assert node.exc.args, (
            f"a TrackerReadError is raised with no reason (line {node.lineno}) — "
            "every refusal this module makes is an answer the panel draws."
        )
        arg = node.exc.args[0]
        text = _wording(arg, reasons)
        raised[_named(arg, text)] = text
    assert raised, "parsed no TrackerReadError raises out of kew.service.description"
    return raised


def test_the_three_gate_refusals_carry_the_re_read_marker() -> None:
    """A reworded gate is a refusal the panel would hold with no body at all.

    These are the refusals the Work screen exists to take again: the panel has
    nothing to draw for one but a reason, and — because a read that took no
    reading names no URL — not even a link to leave by.
    """
    marker = _marker()
    reasons = _reason_constants()
    missing = sorted(name for name in _UNATTEMPTED if marker not in reasons[name])
    assert not missing, (
        f"{missing} no longer contain {marker!r}, which DESCRIPTION_UNATTEMPTED "
        f"in {_EVIDENCE_TS} matches them by — rereadsDescription would stop "
        "taking those refusals again, and the panel would hold one, with no body "
        "and no link, for as long as the row stays selected. Keep the marker in "
        "the wording, or change both sides together."
    )


@pytest.mark.policy
def test_no_reason_from_a_read_that_reached_the_tracker_carries_the_marker() -> None:
    """The spend the #2155 review found: a failed reading re-read eleven times.

    Every reason is checked twice over — as the constant it is declared as, and
    as the wording it is actually raised with, which for every tracker failure
    here is an f-string that interpolates one — because it is the WORDING the
    frontend matches. A tracker failure that happened to end in the same words
    would be re-read on every pulse, and one unreadable item would ration away
    every other item's cold read.
    """
    marker = _marker()
    served = {**_reason_constants(), **_raised_reasons()}
    carried = sorted(
        name
        for name, text in served.items()
        if marker in text and name not in _UNATTEMPTED
    )
    assert not carried, (
        f"{carried} contain {marker!r}, which the Work screen reads as 'this read "
        "never reached the tracker, take it again'. Each of these is a refusal "
        "that either spent a tracker read or cannot change on a retry, so the "
        "pulse would re-read it up to DESCRIPTION_RETRIES times for nothing. "
        "Word it without the marker."
    )


def test_the_work_screen_still_consults_the_marker() -> None:
    """Availability alone cannot tell a free refusal from a spent read.

    ``descriptionUnread`` is true for every answer the panel will not draw a body
    for, which is exactly what the original retry keyed on — and why it re-read
    tracker failures too. The marker is the narrowing; a ``rereadsDescription``
    that stops consulting it is that bug again.
    """
    body = _REREADS_RE.search(_evidence_ts())
    assert body, (
        f"rereadsDescription is not declared in {_EVIDENCE_TS} as this check "
        "expects (an exported function whose body ends at column 0) — it decides "
        "which refusals the pulse spends tracker reads on."
    )
    assert "DESCRIPTION_UNATTEMPTED" in body.group(0), (
        f"rereadsDescription in {_EVIDENCE_TS} no longer consults "
        "DESCRIPTION_UNATTEMPTED, so it re-reads on availability alone: a read "
        "that reached the tracker and failed would be taken again on every "
        "pulse, multiplying one item's spend of the shared read budget by "
        "DESCRIPTION_RETRIES and starving every other item's read."
    )
