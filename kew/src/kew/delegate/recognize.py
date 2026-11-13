"""Pure delegation trigger recognition + one-time KEW_MEMBER_ID resolution (D2).

Given a candidate Linear issue's live fields (assignee id, labels, state) plus
the resolved kew member id and the configured trigger label, decide whether the
issue is delegated-to-kew AND actionable. An issue is delegated-to-kew when its
assignee id EXACTLY matches the kew member id (never a substring match) OR it
carries the configured label. It is actionable only from a claimable starting
state ({Todo, Backlog}); anything already started / in review / done / canceled
is in-flight or terminal and must not be re-claimed.

The only I/O is a one-time member lookup, cached by MemberIdResolver, so the
watcher resolves KEW_MEMBER_ID once at startup rather than per candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Linear starting states from which a delegated issue may be claimed. Every
# other state (started/In Progress, In Review, Done, Canceled) is in-flight or
# terminal, so re-claiming it would double-dispatch or resurrect finished work.
ACTIONABLE_STATES = frozenset({"Todo", "Backlog"})


@dataclass(frozen=True)
class CandidateIssue:
    """The live issue fields recognition needs — nothing more.

    ``assignee_id`` is None for an unassigned issue; ``labels`` are the issue's
    current label names; ``state`` is the workflow state name (e.g. "Todo").
    ``epic_admitted`` is the provider's live ownership fact. It is separate
    from ordinary project membership: only an explicitly admitted item owns
    the epic rail.
    """

    assignee_id: str | None
    labels: tuple[str, ...]
    state: str
    state_id: str
    epic_admitted: bool = False


def is_delegated_to_kew(issue: CandidateIssue, member_id: str, label: str) -> bool:
    """True if the issue is assigned to the kew member OR carries the label.

    Assignee match is EXACT (``==``), never a substring, so a member id that is
    a prefix/suffix of some other assignee id can never trigger a false claim.
    An epic-admitted item is owned by the epic rail even when it is also
    assigned to kew or carries the delegation label.
    """
    if issue.epic_admitted:
        return False
    if issue.assignee_id is not None and issue.assignee_id == member_id:
        return True
    return label in issue.labels


def is_actionable(issue: CandidateIssue) -> bool:
    """True if the issue is in a claimable starting state ({Todo, Backlog})."""
    return issue.state in ACTIONABLE_STATES


def is_delegated_and_actionable(
    issue: CandidateIssue, member_id: str, label: str
) -> bool:
    """True only when the issue is both delegated-to-kew and actionable."""
    return is_delegated_to_kew(issue, member_id, label) and is_actionable(issue)


class _MemberSource(Protocol):
    """The one adapter method KEW_MEMBER_ID resolution depends on."""

    def resolve_member_id(self, name: str) -> str: ...


class MemberIdResolver:
    """Resolves KEW_MEMBER_ID from ``DelegationConfig.assignee`` once and caches it.

    The Linear member id never changes for a given seat, so the watcher pays the
    lookup a single time at startup and reuses the cached id for every candidate.
    """

    def __init__(self, source: _MemberSource, assignee: str) -> None:
        self._source = source
        self._assignee = assignee
        self._member_id: str | None = None

    def resolve(self) -> str:
        if self._member_id is None:
            self._member_id = self._source.resolve_member_id(self._assignee)
        return self._member_id
