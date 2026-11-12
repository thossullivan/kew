"""Unit tests for delegation trigger recognition + KEW_MEMBER_ID resolution (D2)."""

from __future__ import annotations

import pytest

from kew.delegate.recognize import (
    ACTIONABLE_STATES,
    CandidateIssue,
    MemberIdResolver,
    is_actionable,
    is_delegated_and_actionable,
    is_delegated_to_kew,
)

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
LABEL = "kew:ready"


def _issue(*, assignee_id=None, labels=(), state="Todo") -> CandidateIssue:
    return CandidateIssue(
        assignee_id=assignee_id, labels=tuple(labels), state=state, state_id=state
    )


def test_assignee_exact_match_is_delegated() -> None:
    issue = _issue(assignee_id=MEMBER_ID)
    assert is_delegated_to_kew(issue, MEMBER_ID, LABEL)
    assert is_delegated_and_actionable(issue, MEMBER_ID, LABEL)


def test_label_match_is_delegated() -> None:
    issue = _issue(assignee_id="someone-else", labels=["backend", LABEL])
    assert is_delegated_to_kew(issue, MEMBER_ID, LABEL)
    assert is_delegated_and_actionable(issue, MEMBER_ID, LABEL)


def test_wrong_assignee_no_label_is_rejected() -> None:
    issue = _issue(assignee_id="99999999-aaaa-bbbb-cccc-dddddddddddd", labels=["bug"])
    assert not is_delegated_to_kew(issue, MEMBER_ID, LABEL)
    assert not is_delegated_and_actionable(issue, MEMBER_ID, LABEL)


def test_unassigned_no_label_is_rejected() -> None:
    issue = _issue(assignee_id=None, labels=[])
    assert not is_delegated_to_kew(issue, MEMBER_ID, LABEL)


def test_assignee_match_is_exact_never_substring() -> None:
    # A member id that is a substring of the issue's assignee must NOT match.
    issue = _issue(assignee_id=MEMBER_ID + "-extra")
    assert not is_delegated_to_kew(issue, MEMBER_ID, LABEL)
    # ...and the reverse: the issue id being a substring of the member id.
    issue2 = _issue(assignee_id=MEMBER_ID[:-5])
    assert not is_delegated_to_kew(issue2, MEMBER_ID, LABEL)


def test_non_actionable_state_is_rejected() -> None:
    # Delegated but already in flight / terminal -> not actionable.
    for state in ("In Progress", "In Review", "Done", "Canceled"):
        issue = _issue(assignee_id=MEMBER_ID, state=state)
        assert is_delegated_to_kew(issue, MEMBER_ID, LABEL)
        assert not is_actionable(issue)
        assert not is_delegated_and_actionable(issue, MEMBER_ID, LABEL)


def test_actionable_states_are_todo_and_backlog() -> None:
    assert frozenset({"Todo", "Backlog"}) == ACTIONABLE_STATES
    for state in ("Todo", "Backlog"):
        assert is_actionable(_issue(state=state))


def test_epic_admitted_item_is_not_delegated_even_when_assigned_to_kew() -> None:
    issue = _issue(
        assignee_id=MEMBER_ID,
        labels=["kew", LABEL],
    )
    issue = CandidateIssue(
        assignee_id=issue.assignee_id,
        labels=issue.labels,
        state=issue.state,
        state_id=issue.state,
        epic_admitted=True,
    )

    assert not is_delegated_to_kew(issue, MEMBER_ID, LABEL)
    assert not is_delegated_and_actionable(issue, MEMBER_ID, LABEL)


class _FakeSource:
    def __init__(self, member_id: str) -> None:
        self._member_id = member_id
        self.calls: list[str] = []

    def resolve_member_id(self, name: str) -> str:
        self.calls.append(name)
        return self._member_id


def test_member_resolution_caches_after_first_lookup() -> None:
    source = _FakeSource(MEMBER_ID)
    resolver = MemberIdResolver(source, "kew")

    assert resolver.resolve() == MEMBER_ID
    assert resolver.resolve() == MEMBER_ID
    # Resolved once from DelegationConfig.assignee, then served from cache.
    assert source.calls == ["kew"]


def test_member_resolution_propagates_lookup_failure() -> None:
    class _Missing:
        def resolve_member_id(self, name: str) -> str:
            raise RuntimeError(f"No Linear member named {name!r}.")

    resolver = MemberIdResolver(_Missing(), "ghost")
    with pytest.raises(RuntimeError, match="No Linear member"):
        resolver.resolve()
