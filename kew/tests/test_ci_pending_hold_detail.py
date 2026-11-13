"""The CI gate's hold reason now carries the merge state that earned it (#2303).

``ci_pending`` was one word for two very different worlds: a tree a person has
to rebase (``DIRTY``) and a base the factory catches up to on its own
(``BEHIND``). Nothing downstream could tell them apart, so the console had to
guess which of the two a held node meant.

The detail is additive, and that is the whole risk: every reader that used to
compare to the bare word has to match by PREFIX instead, or a held node silently
stops being recognised — it leaves the HELD routing, the stall clock stops
reading its row, and the merge pass starts treating its own hold as somebody
else's. This module pins the vocabulary and that predicate; the end-to-end write
is pinned in ``test_driver_merge.py``.
"""

from __future__ import annotations

import pytest

from kew.epic_holds import (
    CI_PENDING_HOLD_PREFIX,
    CI_PENDING_HOLD_REASON,
    ci_pending_hold_reason,
    ci_pending_merge_state,
    holds_node_out_of_frontier,
    is_ci_pending_hold,
    is_dispatch_refusal_hold,
)
from kew.waits import classify_wait


@pytest.mark.parametrize(
    ("merge_state", "expected"),
    [
        ("DIRTY", "ci_pending:dirty"),
        ("BEHIND", "ci_pending:behind"),
        ("BLOCKED", "ci_pending:blocked"),
        ("dirty", "ci_pending:dirty"),
        ("  BEHIND  ", "ci_pending:behind"),
    ],
)
def test_the_reason_carries_the_merge_state(merge_state: str, expected: str) -> None:
    assert ci_pending_hold_reason(merge_state) == expected


@pytest.mark.parametrize("merge_state", [None, "", "   ", "UNKNOWN", "unknown"])
def test_a_state_nobody_read_adds_no_detail(merge_state: str | None) -> None:
    """Writing the word ``unknown`` would make an unread fact look observed."""
    assert ci_pending_hold_reason(merge_state) == CI_PENDING_HOLD_REASON


@pytest.mark.parametrize(
    "reason", ["ci_pending", "ci_pending:dirty", "ci_pending:behind"]
)
def test_the_gate_is_recognised_with_or_without_its_detail(reason: str) -> None:
    assert is_ci_pending_hold(reason)
    # The three readings that would break if a reader compared to the bare word.
    assert holds_node_out_of_frontier(reason)
    assert not is_dispatch_refusal_hold(reason)
    # The CI gate is a WAIT, never a person's move — an operator cannot make a
    # check finish, and the detail must not turn it into a needs-you.
    assert classify_wait(reason, held=True) is None


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "ci_pending_other",
        "budget_preflight: $41.20 observed against a $40.00 cap",
        "review_rounds_exhausted",
    ],
)
def test_nothing_else_is_mistaken_for_the_gate(reason: str | None) -> None:
    assert not is_ci_pending_hold(reason)


def test_the_merge_state_reads_back_off_the_reason() -> None:
    assert ci_pending_merge_state("ci_pending:dirty") == "dirty"
    assert ci_pending_merge_state(CI_PENDING_HOLD_REASON) is None
    assert ci_pending_merge_state("review_rounds_exhausted") is None
    assert ci_pending_merge_state(None) is None


def test_the_prefix_is_the_bare_reason_so_the_vocabulary_stays_one_word() -> None:
    """Monitor's hold table matches by prefix, so no second entry is needed."""
    assert CI_PENDING_HOLD_PREFIX == CI_PENDING_HOLD_REASON
