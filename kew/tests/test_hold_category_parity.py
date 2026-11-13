"""Monitor's hold categories come from ``kew.epic_holds``, not from a default.

#2208. The web console's holds column used to read the dashboard row TYPE as
the category, and every ``epic_holds`` row is served under ``budget_hold``
whatever held the node (``kew.service.needs_you._budget_rows`` — the name is
historical, the budget preflight was that table's first writer). So a node held
on the review round cap was drawn ``BUDGET · review_rounds_exhausted ·
Approve this hold``: the wrong category, the raw reason in place of words, and
a button that could only ever answer ``not_held``.

The column now reads the REASON, through a table in
``frontend/src/lib/monitor/hold-reasons.ts``. That table is a second copy of
this package's hold-reason vocabulary, and the failure mode of a second copy is
silence: a prefix added here with no entry there reads as ``other`` on that
surface alone, with nobody told. So the two halves are held together from this
side, where the vocabulary lives — the ``frontend/`` half is covered by
``hold-reasons.test.ts`` and ``holds.test.ts``.

Nothing here asserts what the frontend DOES with a category (the words, the
links, the since-when); those are its own suite's business. This is only the
two directions of coverage, plus the one hint string both sides print.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kew import epic_holds
from kew.epic_holds import (
    BUDGET_HOLD_PREFIX,
    CI_PENDING_HOLD_REASON,
    REVIEW_BUDGET_HOLD_PREFIX,
    ROUNDS_EXHAUSTED_HOLD_REASON,
    holds_node_out_of_frontier,
    is_dispatch_refusal_hold,
)
from kew.service.attention import wait_sentence
from kew.waits import UNBUCKETED_HELD_HINT

_HOLD_REASONS_TS = "frontend/src/lib/monitor/hold-reasons.ts"

# One `{ prefix: '…', category: '…', …}` entry of `HOLD_SPECS`, in the order the
# table declares them (first match wins, as in `waits.bucket_for`).
_SPEC_RE = re.compile(r"prefix: '([^']+)',\s*\n\s*category: '([^']+)'")
# `export const UNKNOWN_HOLD_ACTION = '…';`
_UNKNOWN_RE = re.compile(r"export const UNKNOWN_HOLD_ACTION\s*=\s*'([^']+)'")


def _hold_reasons_ts() -> str:
    """The frontend's hold-reason table, as source."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _HOLD_REASONS_TS
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    pytest.skip(f"{_HOLD_REASONS_TS} not present in this checkout")


def _categories() -> dict[str, str]:
    """``{prefix: category}``, parsed from the frontend's own source."""
    specs = _SPEC_RE.findall(_hold_reasons_ts())
    assert specs, (
        f"no HOLD_SPECS entries found in {_HOLD_REASONS_TS} — the table this "
        "module exists to hold the vocabulary to has moved or changed shape"
    )
    return dict(specs)


def _vocabulary() -> set[str]:
    """Every hold reason or reason prefix this package defines.

    Collected off the module rather than listed, so a prefix added to
    ``epic_holds`` is covered the day it lands: that is the drift this module is
    here to catch, and a hand-kept list would simply be a third copy.
    """
    named = {
        value
        for name, value in vars(epic_holds).items()
        if isinstance(value, str)
        and (name.endswith("_HOLD_PREFIX") or name.endswith("_HOLD_REASON"))
    }
    return named | set(epic_holds.REVIEW_HOLD_REASONS)


def test_every_hold_reason_this_package_writes_has_a_category() -> None:
    categories = _categories()
    missing = sorted(reason for reason in _vocabulary() if reason not in categories)
    assert not missing, (
        f"{_HOLD_REASONS_TS} has no entry for {missing} — a hold reason with no "
        "entry draws as `other` on the Monitor's holds column, which is honest "
        "but unhelpful: give it a category, the reason in words, and the action "
        "that clears it"
    )


def test_the_frontend_claims_no_reason_this_package_never_writes() -> None:
    # The other direction: an entry for a prefix nothing writes is dead copy
    # that reads as current, and the suite that covers it proves only itself.
    unknown = sorted(set(_categories()) - _vocabulary())
    assert not unknown, (
        f"{_HOLD_REASONS_TS} names {unknown}, which kew.epic_holds does not "
        "define — delete the entry or add the reason"
    )


def test_every_categorised_reason_is_one_the_driver_honors() -> None:
    for prefix in _categories():
        assert holds_node_out_of_frontier(prefix) or is_dispatch_refusal_hold(prefix), (
            f"{prefix!r} is categorised for the holds column but no driver pass "
            "routes it to HELD or recognises it as a dispatch refusal"
        )


def test_the_round_cap_is_not_a_budget_hold() -> None:
    # The row from the screenshots that named this issue. The cap it reached is
    # a count of review rounds, not money, and the operator move it earns is a
    # `kew epic unhold` (REVIEW_CYCLE_RESET_HOLD_REASONS), not a raised cap.
    categories = _categories()
    assert categories[ROUNDS_EXHAUSTED_HOLD_REASON] == "review"
    assert categories[BUDGET_HOLD_PREFIX] == "budget"
    assert categories[REVIEW_BUDGET_HOLD_PREFIX] == "budget"


def test_the_ci_gate_is_its_own_category_nobody_can_clear() -> None:
    # `classify_wait` answers None for it — an operator cannot make a check
    # finish — so the column must not file it with the holds a person clears.
    assert _categories()[CI_PENDING_HOLD_REASON] == "ci"


def test_the_unbucketed_hint_is_the_same_sentence_on_both_sides() -> None:
    match = _UNKNOWN_RE.search(_hold_reasons_ts())
    assert match, (
        f"UNKNOWN_HOLD_ACTION is not an exported literal in {_HOLD_REASONS_TS}"
    )
    assert match.group(1) == UNBUCKETED_HELD_HINT


def test_the_server_sentence_never_prints_a_hold_prefix_raw() -> None:
    """The console prints the server's sentence verbatim (#2539), so the guard
    the console's own table used to keep — no hold prefix printed raw, for any
    prefix it knows — is the server's to keep now, over that same vocabulary."""
    for prefix in _categories():
        sentence = wait_sentence("held", f"{prefix}: what it carries")
        assert not sentence.startswith(prefix), (prefix, sentence)
        assert "what it carries" in sentence, (prefix, sentence)
