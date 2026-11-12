"""The shared node-state rule, and the two vocabularies it has to match (#2392).

:mod:`kew.epic_node_states` is read from both sides of an import boundary —
``kew.graph.driver.overlay`` and ``kew.service.epics`` — and imports no kew
code itself, which is exactly what lets it be shared. The cost of that purity
is five transcribed strings: ``done``, ``cancelled``, ``complete`` and the two
spellings of ``merged_unclosed``. This module is the anchor that keeps them true, so a
rename of ``DriverStatus.DONE`` or ``TerminalSignal.COMPLETE``, or a rewording
of the tick's held-wait reason, fails here rather than silently turning the
reconciliation into a no-op — or into a promotion — that still passes every
other test.

"PR merged, tracker item still OPEN" is journaled in TWO vocabularies, and the
anchor covers both: the tick's facts-computed held wait, and the merge pass's
``merged-not-closed`` from a close that did not take. Matching only the first
is what let the second — the longer-lived of the two — be promoted to ``done``
with its reason erased (#2392 review).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from kew.epic_node_states import (
    COMPLETE_SIGNAL,
    DONE,
    TERMINAL_NODE_STATES,
    epic_signal,
    is_closed_with_merged_pr,
    journaled_merged_unclosed,
    node_counts,
    remaining_count,
    settle_landed,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.merge import _handle_merged
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import _held_wait_reason

if TYPE_CHECKING:
    from typing import Any


def test_the_transcribed_words_are_the_shipped_ones() -> None:
    assert DriverStatus.DONE.value == DONE
    assert TerminalSignal.COMPLETE.value == COMPLETE_SIGNAL
    assert {
        DriverStatus.DONE.value,
        DriverStatus.CANCELLED.value,
    } == TERMINAL_NODE_STATES


def test_landing_needs_both_halves() -> None:
    """A merged PR beside an OPEN item is the merge pass mid-flight, and a
    closed item with no merge is a cancellation or a manual close — neither is
    the thing this rule is about."""
    assert is_closed_with_merged_pr("CLOSED", "MERGED") is True
    assert is_closed_with_merged_pr("OPEN", "MERGED") is False
    assert is_closed_with_merged_pr("CLOSED", "OPEN") is False
    assert is_closed_with_merged_pr("CLOSED", None) is False
    assert is_closed_with_merged_pr(None, None) is False


def test_the_merged_unclosed_reason_is_the_one_the_tick_writes() -> None:
    """The other transcribed string, pinned the same way: a rewording of the
    tick's held-wait reason must fail here rather than silently letting serve
    read a merged run row as a landing on a node the driver is holding."""
    facts = NodeFacts(
        issue_num=1,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=9,
        pr_url=None,
        pr_state="MERGED",
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
    )

    assert journaled_merged_unclosed(_held_wait_reason("1", {}, {"1": facts})) is True


def test_the_merge_pass_spelling_of_the_same_state_is_read_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merge pass journals its own word for this state: it merged the PR,
    could not verify the item closed, and returns ``stuck/merged-not-closed``.

    Taken from a real verdict rather than retyped, because that reason is the
    one that PERSISTS tick after tick — the tick's wait is recomputed from
    facts, a failed close is not — so a reader that knows only the tick's
    spelling promotes exactly the node the driver has held the longest.
    """
    monkeypatch.setattr(
        "kew.graph.driver.merge._clear_hold_and_strip_label", lambda *a, **k: None
    )
    monkeypatch.setattr("kew.graph.driver.merge._verify_closed", lambda *a, **k: False)

    verdict = _handle_merged(
        "1",
        item_id="1",
        facts_key=1,
        provider="github",
        tracker=None,
        reader=cast("Any", None),
        repo="o/r",
        run=cast("Any", None),
        store=cast("Any", None),
        pending_review_label="pr-pending-review",
    )

    assert verdict.action == "stuck"
    assert journaled_merged_unclosed(verdict.reason) is True


def test_a_node_held_for_anything_else_is_not_merged_unclosed() -> None:
    assert journaled_merged_unclosed("review_rounds_exhausted") is False
    assert journaled_merged_unclosed("merge-failed") is False
    assert journaled_merged_unclosed(None) is False
    assert journaled_merged_unclosed("") is False


def test_provider_casing_and_padding_do_not_change_the_answer() -> None:
    assert is_closed_with_merged_pr(" closed ", "merged") is True


def test_settling_promotes_only_the_landed_and_only_the_unsettled() -> None:
    states = {"1": "held", "2": "remediating", "3": "cancelled", "4": "in_review"}

    settled = settle_landed(states, {"1", "2", "3", "9"})

    # "3" keeps its cancellation: a provider cancellation is the provider's to
    # state, and "9" is evidence about a node this epic does not have.
    assert settled == {
        "1": "done",
        "2": "done",
        "3": "cancelled",
        "4": "in_review",
    }


def test_no_evidence_leaves_the_states_exactly_as_journaled() -> None:
    states = {"1": "held", "2": "in_review"}

    assert settle_landed(states, frozenset()) == states


def test_counts_count_every_node_the_journal_counted() -> None:
    """Including the external boundary copies: the journal's own ``counts``
    includes them, and a reconciliation must not change the denominator."""
    assert node_counts({"1": "done", "2": "done", "3": "held"}) == {
        "done": 2,
        "held": 1,
    }
    assert node_counts({}) == {}


def test_remaining_excludes_the_boundary_nodes_and_counts_cancelled() -> None:
    states = {"1": "done", "2": "cancelled", "3": "done", "out": "todo"}

    assert remaining_count(states, external=["out"]) == 1
    assert remaining_count({"1": "done"}, external=[]) == 0


def test_only_the_complete_arm_is_derived() -> None:
    """Progressing / awaiting_merges / stalled turn on live facts a journal
    does not carry, so the recorded reading stands rather than being guessed."""
    assert epic_signal({"1": "done", "2": "done"}, "progressing") == COMPLETE_SIGNAL
    assert epic_signal({"1": "done", "2": "held"}, "progressing") == "progressing"
    assert epic_signal({"1": "cancelled"}, "stalled") == "stalled"
    assert epic_signal({}, "progressing") == "progressing"
    assert epic_signal({}, None) is None


def test_an_epic_of_boundary_nodes_alone_is_not_declared_complete() -> None:
    """Every node excluded means nothing was READ, not that nothing remains —
    the one way the derived COMPLETE arm could fire on an empty question."""
    assert (
        epic_signal({"out": "todo"}, "progressing", external=["out"]) == "progressing"
    )
