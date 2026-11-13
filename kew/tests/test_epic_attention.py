"""The epic driver's one operator-attention event stream (#2150).

Five stalls used to surface in five different places — a READY verdict in the
ledger, an ``awaiting merge:`` tick line, a ``Review skipped`` line, a
``Sandbox:`` line, and a held node visible only in ``kew epic status``. Each
one now produces exactly ONE event naming the item and the action.
"""

from __future__ import annotations

import pytest

from kew.epic_holds import (
    REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
    REVIEW_RATE_LIMITED_WAIT_PREFIX,
)
from kew.graph.driver import review_stage
from kew.graph.driver.attention import (
    AttentionEvent,
    AttentionKind,
    attention_events,
    bucket_for,
)
from kew.graph.driver.attention_stream import AttentionStream
from kew.graph.driver.result import StuckNode, TerminalSignal, TickSummary
from kew.runner_quota import RunCredential

EPIC = "dag-1"


def _summary(
    *,
    waiting: tuple[StuckNode, ...] = (),
    stuck: tuple[StuckNode, ...] = (),
    held: tuple[str, ...] = (),
    signal: TerminalSignal = TerminalSignal.STALLED,
    remaining: int = 1,
    identity: dict[str, str] | None = None,
) -> TickSummary:
    return TickSummary(
        counts={},
        frontier=(),
        in_flight=(),
        held=held,
        failed=(),
        cancelled=(),
        stuck=stuck,
        done=(),
        remaining=remaining,
        signal=signal,
        waiting=waiting,
        identity=identity or {},
    )


def _one(summary: TickSummary) -> AttentionEvent:
    events = attention_events(EPIC, summary)
    assert len(events) == 1, events
    return events[0]


# ---------------------------------------------------------------------------
# the five stalls, one event each
# ---------------------------------------------------------------------------


def test_a_pr_awaiting_a_human_merge_is_one_merge_ready_event() -> None:
    """A READY review verdict lands the node in_review awaiting a merge."""
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ("43",)),)
    )
    event = _one(summary)
    assert event.kind is AttentionKind.MERGE_READY
    assert event.item == "42"
    assert event.epic == EPIC
    assert "PR #7" in event.reason
    assert event.action
    assert event.blast_radius == ("43",)


def test_a_head_check_gate_on_an_in_review_node_is_still_one_merge_ready_event() -> (
    None
):
    """The merge pass rewrites `awaiting merge` to name the gate (#2033); the
    node is still one PR waiting on the same person."""
    summary = _summary(
        waiting=(
            StuckNode(
                "42",
                "in_review — ci-approved: tests did not run (0 checks) (PR #7)",
                (),
            ),
        )
    )
    assert _one(summary).kind is AttentionKind.MERGE_READY


def test_a_spent_remediation_pool_is_one_budget_exhausted_event() -> None:
    """Previously only a `Review skipped (remediation pool exhausted)` line."""
    summary = _summary(
        waiting=(
            StuckNode(
                "42",
                "review_budget: pool $2.00 spent — raise review.remediation_budget_usd",
                (),
            ),
        ),
        held=("42",),
    )
    event = _one(summary)
    assert event.kind is AttentionKind.BUDGET_EXHAUSTED
    assert event.item == "42"


def test_a_budget_preflight_hold_is_one_budget_exhausted_event() -> None:
    summary = _summary(
        waiting=(
            StuckNode(
                "42",
                "budget_preflight: spent $17.08 > per_issue cap $15.00 — "
                "raise budget.per_issue_usd in kew.toml",
                (),
            ),
        ),
        held=("42",),
    )
    assert _one(summary).kind is AttentionKind.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    "reason",
    [
        "sandbox_containment: runner 'claude' has no container image mapped — "
        "un-hold with `kew epic unhold 42`",
        "review_sandbox_refused",
        # With the refusal's own fix in the reason (#2043) it is still ONE
        # sandbox_refused event — the bucket matches the prefix.
        "review_sandbox_refused: the reviewer never started — run kew sandbox "
        "build --runner claude-code, or map [sandbox] images.claude-code",
    ],
)
def test_a_refused_sandbox_is_one_sandbox_refused_event(reason: str) -> None:
    """Previously only a transient `Sandbox:` line on a detached child."""
    summary = _summary(waiting=(StuckNode("42", reason, ()),), held=("42",))
    event = _one(summary)
    assert event.kind is AttentionKind.SANDBOX_REFUSED
    assert event.item == "42"


def test_a_reviewer_that_never_started_asks_for_its_own_fix() -> None:
    """#2043: the hold is bucketed as its own state, so the operator's action is
    the launch fix its reason names — not "check the reviewer"."""
    reason = (
        "review_launch_failed: the reviewer never started — start the "
        "container runtime [sandbox] runtime names"
    )
    summary = _summary(waiting=(StuckNode("42", reason, ()),), held=("42",))
    event = _one(summary)
    assert event.kind is AttentionKind.HELD
    assert event.reason == reason
    assert "fix what its reason names" in event.action


def test_a_held_node_is_one_held_event_naming_the_unhold() -> None:
    """Previously visible only in `kew epic status`."""
    summary = _summary(
        waiting=(
            StuckNode("42", "admission_refused: no pricing entry for model 'x'", ()),
        ),
        held=("42",),
    )
    event = _one(summary)
    assert event.kind is AttentionKind.HELD
    assert "unhold" in event.action


def test_a_preserved_publication_is_one_publication_held_event() -> None:
    summary = _summary(
        waiting=(
            StuckNode(
                "42",
                "awaiting_publication — github unavailable; open an external PR "
                "and reconcile, or use `kew finalize run-9`",
                (),
            ),
        )
    )
    event = _one(summary)
    assert event.kind is AttentionKind.PUBLICATION_HELD
    assert event.item == "42"


def test_a_complete_epic_is_exactly_one_event_naming_the_epic() -> None:
    summary = _summary(signal=TerminalSignal.COMPLETE, remaining=0)
    event = _one(summary)
    assert event.kind is AttentionKind.EPIC_COMPLETE
    assert event.item == EPIC
    assert event.action


def test_a_complete_epic_reports_only_its_completion() -> None:
    """A stale waiting row cannot add a second event to a finished epic."""
    summary = _summary(
        signal=TerminalSignal.COMPLETE,
        remaining=0,
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ()),),
    )
    assert _one(summary).kind is AttentionKind.EPIC_COMPLETE


# ---------------------------------------------------------------------------
# what is NOT a person's problem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "ci_pending",
        "blocked on #41",
        "merged_unclosed — the merge pass closes the issue on this tick",
    ],
)
def test_a_wait_that_clears_itself_names_no_person(reason: str) -> None:
    summary = _summary(waiting=(StuckNode("42", reason, ()),), held=("42",))
    assert attention_events(EPIC, summary) == ()


def test_a_progressing_epic_with_nothing_waiting_emits_nothing() -> None:
    assert attention_events(EPIC, _summary(signal=TerminalSignal.PROGRESSING)) == ()


# ---------------------------------------------------------------------------
# a reviewer usage limit: the wait is nobody's job, the spent wait is (#2219)
# ---------------------------------------------------------------------------

# The real strings the review stage writes, not copies of them: a bucket is a
# table of PREFIXES, and a reason the table no longer matches is exactly how one
# of these states would go silent.
_LIMIT_TOKEN = RunCredential(
    "claude-code", ("CLAUDE_CODE_OAUTH_TOKEN",), "1a2b3c4d5e6f"
)
_LIMIT_WAIT = review_stage._rate_limit_wait_reason(_LIMIT_TOKEN)
_LIMIT_HELD = review_stage._rate_limit_exhausted_hold_reason(_LIMIT_TOKEN)


def test_the_limit_reasons_the_review_stage_writes_are_the_ones_bucketed() -> None:
    """Both halves of #2219 land in their own bucket, neither in the other's."""
    assert _LIMIT_WAIT.startswith(REVIEW_RATE_LIMITED_WAIT_PREFIX)
    assert _LIMIT_HELD.startswith(REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX)
    assert bucket_for(_LIMIT_WAIT) is not None
    assert bucket_for(_LIMIT_WAIT).prefix == REVIEW_RATE_LIMITED_WAIT_PREFIX
    assert bucket_for(_LIMIT_HELD) is not None
    assert bucket_for(_LIMIT_HELD).prefix == REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX


def test_a_reviewer_usage_limit_wait_names_no_person() -> None:
    """A provider window resetting is not anybody's job: the next round runs by
    itself. Mapped to a kind, every poll of a benign 15-minute wait told the
    operator to go raise their usage limit."""
    summary = _summary(waiting=(StuckNode("42", _LIMIT_WAIT, ()),))
    assert attention_events(EPIC, summary) == ()


def test_a_following_watch_stays_silent_for_the_whole_limit_wait() -> None:
    """#2219: ONE benign wait is at most one state for a watcher.

    The reason is part of what makes an event distinct (`state_key`), so a
    reason that counted the minutes down defeated the de-dup memory outright:
    `kew epic watch --follow` emitted 15 separate held events during a single
    15-minute wait, each telling the operator to raise their provider limit.
    """
    stream = AttentionStream(epic=EPIC)
    summary = _summary(waiting=(StuckNode("42", _LIMIT_WAIT, ()),))

    emitted = [stream.fresh(attention_events(EPIC, summary)) for _ in range(15)]

    assert emitted == [()] * 15


def test_a_usage_limit_that_is_not_resetting_names_one_person_once() -> None:
    """The same incident past its bound IS a person's: the node is held under
    `review_rate_limit_exhausted`, which names raising the limit — and a
    following watch says it exactly once however long the hold stands."""
    stream = AttentionStream(epic=EPIC)
    summary = _summary(waiting=(StuckNode("42", _LIMIT_HELD, ()),), held=("42",))

    first = stream.fresh(attention_events(EPIC, summary))
    rest = [stream.fresh(attention_events(EPIC, summary)) for _ in range(14)]

    (event,) = first
    assert event.kind is AttentionKind.HELD
    assert event.item == "42"
    assert "usage limit" in event.action
    assert rest == [()] * 14


def test_an_aged_out_usage_limit_hold_still_names_its_person() -> None:
    """Past `epic.stall_days` the hold row leaves `held` for `stuck` — the hold
    a waiting node could never have had, which is the whole reason the wait is
    bounded into one."""
    event = _one(_summary(stuck=(StuckNode("42", _LIMIT_HELD, ()),)))
    assert event.kind is AttentionKind.HELD
    assert "usage limit" in event.action


# ---------------------------------------------------------------------------
# stream shape
# ---------------------------------------------------------------------------


def test_every_waiting_node_produces_at_most_one_event() -> None:
    waiting = (
        StuckNode("40", "in_review — awaiting merge (PR #7)", ()),
        StuckNode("41", "review_budget: pool spent", ()),
        StuckNode("42", "sandbox_containment: no image", ()),
        StuckNode("43", "ci_pending", ()),
        StuckNode("44", "review_crash", ()),
    )
    events = attention_events(
        EPIC, _summary(waiting=waiting, held=("41", "42", "43", "44"))
    )
    assert [e.item for e in events] == ["40", "41", "42", "44"]
    assert [e.kind for e in events] == [
        AttentionKind.MERGE_READY,
        AttentionKind.BUDGET_EXHAUSTED,
        AttentionKind.SANDBOX_REFUSED,
        AttentionKind.HELD,
    ]


def test_a_held_node_with_an_unbucketed_reason_still_names_a_person() -> None:
    """A governed-review hold has no stall bucket; it still needs an operator,
    so it lands in the generic `held` kind rather than going silent."""
    summary = _summary(
        waiting=(StuckNode("42", "review_residuals_unfiled", ()),), held=("42",)
    )
    assert _one(summary).kind is AttentionKind.HELD


def test_an_untrusted_hold_reason_is_flattened_to_one_line() -> None:
    """An `admission_refused` reason carries a detached child's stderr, so the
    event's reason must stay one short printable line."""
    summary = _summary(
        waiting=(
            StuckNode("42", "admission_refused: boom\nsecond line\x07" + "x" * 400, ()),
        ),
        held=("42",),
    )
    event = _one(summary)
    assert "\n" not in event.reason
    assert "\x07" not in event.reason
    assert len(event.reason) <= 200


def test_an_event_serializes_to_a_json_ready_mapping() -> None:
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ("43",)),)
    )
    payload = _one(summary).as_dict()
    assert payload["kind"] == "merge_ready"
    assert payload["epic"] == EPIC
    assert payload["item"] == "42"
    assert payload["blast_radius"] == ["43"]
    assert payload["action"]
    assert payload["reason"]


def test_an_event_renders_one_human_line_naming_item_and_action() -> None:
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ()),)
    )
    event = _one(summary)
    line = event.as_line()
    assert "\n" not in line
    assert "merge_ready" in line
    assert "42" in line
    assert event.action in line


# ---------------------------------------------------------------------------
# a stall clock that expires must not silence the person it was waiting for
# ---------------------------------------------------------------------------


def test_a_review_stalled_pr_is_still_one_merge_ready_event() -> None:
    """Past `epic.stall_days` the tick moves the in_review row out of
    `waiting` and into `stuck` as `review-stalled` (see
    `tick._stale_review_stuck`). The PR is still open awaiting the same human
    merge, so the stream must keep naming that person — reading `waiting`
    alone made a merge-ready PR go silent the moment it aged out.
    """
    summary = _summary(stuck=(StuckNode("42", "review-stalled", ("43",)),))
    event = _one(summary)
    assert event.kind is AttentionKind.MERGE_READY
    assert event.item == "42"
    assert event.action
    assert event.blast_radius == ("43",)


def test_an_aged_out_ci_gate_names_a_person_instead_of_going_quiet() -> None:
    """`ci_pending` is a wait — until its stall clock expires. The tick then
    promotes it to `ci-stalled` stuck, which nothing clears by itself."""
    summary = _summary(stuck=(StuckNode("42", "ci-stalled", ()),))
    event = _one(summary)
    assert event.kind is AttentionKind.HELD
    assert "PR" in event.action


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("sandbox_containment: no image mapped", AttentionKind.SANDBOX_REFUSED),
        ("budget_preflight: spent $17.08 > cap", AttentionKind.BUDGET_EXHAUSTED),
        ("admission_refused: no pricing entry", AttentionKind.HELD),
        # No bucket names this one; an honored hold still needs an operator.
        ("review_residuals_unfiled", AttentionKind.HELD),
    ],
)
def test_an_aged_out_hold_still_names_its_own_person(
    reason: str, kind: AttentionKind
) -> None:
    """An honored hold older than `epic.stall_days` leaves `summary.held` for
    `summary.stuck`. It is the same hold, needing the same operator — so the
    event keeps the hold's own kind and action, and never disappears.
    """
    summary = _summary(stuck=(StuckNode("42", reason, ()),))
    event = _one(summary)
    assert event.kind is kind
    assert event.item == "42"
    assert event.action


@pytest.mark.parametrize(
    "reason",
    ["failed-terminal (exit 1, agent_error)", "cancelled", "blocked on #41"],
)
def test_a_terminal_stuck_node_stays_out_of_the_stream(reason: str) -> None:
    """Failed/cancelled nodes are already loud (a `failed:` line, the STALLED
    signal, `kew epic run`'s non-zero exit) — reading `stuck` must not start
    duplicating them into the attention stream."""
    assert attention_events(EPIC, _summary(stuck=(StuckNode("42", reason, ()),))) == ()


def test_a_waiting_row_wins_over_the_same_item_in_stuck() -> None:
    """One event per item: the `waiting` reason is the richer one (it names the
    PR), so it must not be overwritten by a stuck row for the same node."""
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ()),),
        stuck=(StuckNode("42", "review-stalled", ()),),
    )
    event = _one(summary)
    assert "PR #7" in event.reason


# ---------------------------------------------------------------------------
# #2183 — an event carries the identity of the state it is about
# ---------------------------------------------------------------------------


def test_an_event_carries_its_node_s_state_identity() -> None:
    """The head and round the summary observed ride on the event, so a watcher
    can tell one state from the next (#2183)."""
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ()),),
        identity={"42": "head:c0404f4d round:1"},
    )
    event = _one(summary)
    assert event.identity == "head:c0404f4d round:1"
    assert event.as_dict()["identity"] == "head:c0404f4d round:1"
    assert "head:c0404f4d" in event.as_line()


def test_two_ready_verdicts_on_different_heads_are_different_states() -> None:
    """#2177 went READY at one head, failed the merge verify, was fixed and went
    READY again — same item, kind and reason. Only the identity separates them,
    and a watcher keyed on the rest dropped the second, real event."""
    reason = "in_review — awaiting merge (PR #2177)"
    first = _one(
        _summary(
            waiting=(StuckNode("77", reason, ()),), identity={"77": "head:c0404f4d"}
        )
    )
    second = _one(
        _summary(
            waiting=(StuckNode("77", reason, ()),), identity={"77": "head:7a40744b"}
        )
    )
    assert first.reason == second.reason
    assert first.kind is second.kind
    assert first.state_key != second.state_key


def test_an_unchanged_state_keeps_the_same_key() -> None:
    summary = _summary(
        waiting=(StuckNode("42", "in_review — awaiting merge (PR #7)", ()),),
        identity={"42": "head:c0404f4d round:1"},
    )
    assert _one(summary).state_key == _one(summary).state_key


def test_a_node_with_no_identity_evidence_still_emits_its_event() -> None:
    """Identity is additive: a state with no head, round or run is still a
    person's job, and must not go silent for lack of one."""
    event = _one(_summary(held=("1",), waiting=(StuckNode("1", "review_crash", ()),)))
    assert event.identity == ""
    assert event.as_dict()["identity"] == ""
