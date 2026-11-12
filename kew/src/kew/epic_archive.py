"""Which epics an operator has retired from the active views (#2206).

An epic has no end. `complete` is a signal the driver may re-read as
`awaiting_merges` the moment a merged PR reopens something, and an abandoned
epic never reaches a terminal signal at all — so the roster only ever grows.
Sixty-six epics, most of them months cold, pushed twelve dead drains into the
console's "needs you" count, and the only way to retire one was to edit the
ledger by hand.

`kew epic archive` retires one. The decision is recorded as ONE attested audit
event and nothing else: no column, no row, no deletion. The archived set is
therefore DERIVED here — the latest ``epic_archived``/``epic_unarchived`` event
per epic ref wins — which is what makes `kew epic unarchive` a new fact rather
than a retraction, and what keeps the whole history of a retirement readable
with `kew audit tail`.

Four readers ask this module, and must not disagree:

* the Epics list (:mod:`kew.service.epics`) splits the roster on it;
* the judgment queue (:mod:`kew.service.dashboard`) drops EVERY entry an
  archived epic owns — its stalled row, its nodes' journaled waits and its
  driver's open holds — so none of them counts against "needs you" (#2243),
  and the epic legend's join (:mod:`kew.service.attention`) drops the same
  ones so the two numbers agree;
* ``kew epic run --all`` (:mod:`kew.cli.epic`) does not drive one;
* the archive verbs themselves (:mod:`kew.service.verbs`) refuse a no-op.

``kew.service`` may not import ``kew.graph`` (import-linter contract) and this
is read from below the CLI as well, so — exactly like :mod:`kew.epic_holds` —
the shared vocabulary lives in its own top-level module with no kew imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# The two attested facts. Deliberately NOT a shared `event_prefix`: `epic_` is
# already the prefix of `epic_node_retry`/`epic_node_unhold`, and a prefix read
# would pull a driver's whole operator history to answer a yes/no question.
EPIC_ARCHIVED_EVENT = "epic_archived"
EPIC_UNARCHIVED_EVENT = "epic_unarchived"


class _Store(Protocol):
    def list_audit_events(
        self, limit: int | None = 100, *, event: str | None = None
    ) -> list[Any]: ...


@dataclass(frozen=True)
class ArchiveState:
    """The latest archive decision recorded for one epic ref."""

    epic_ref: str
    archived: bool
    at: str
    actor: str | None
    reason: str


def _decision(row: Any) -> ArchiveState | None:
    """The archive decision one audit row records, or None when it records none.

    Rows are classified by the ``event`` name ON THE ROW, not by which query
    returned them, so a store that widens or ignores the ``event=`` filter
    cannot have an unrelated event read as an archive decision. A decision with
    no ``epic_ref`` names no epic: skipping it is the only honest reading —
    guessing one would retire the wrong drain.
    """
    name = str(getattr(row, "event", "") or "")
    if name not in (EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT):
        return None
    payload = getattr(row, "payload", None) or {}
    ref = payload.get("epic_ref")
    if not isinstance(ref, str) or not ref:
        return None
    return ArchiveState(
        epic_ref=ref,
        archived=name == EPIC_ARCHIVED_EVENT,
        at=str(getattr(row, "timestamp", "") or ""),
        actor=getattr(row, "actor_id", None),
        reason=str(payload.get("reason") or ""),
    )


def archive_states(store: _Store) -> dict[str, ArchiveState]:
    """The latest archive decision per epic ref, keyed by ref.

    Ordering is by audit-event ``id`` — the chain's own insertion order —
    because two decisions can share a timestamp to the microsecond but never an
    id.

    Never catches: an unreadable ledger is the caller's failure to report, not
    a quiet "nothing is archived" (which would silently resurrect every retired
    epic in the needs-you count).
    """
    latest: dict[str, tuple[int, ArchiveState]] = {}
    for event in (EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT):
        for row in store.list_audit_events(limit=None, event=event):
            state = _decision(row)
            if state is None:
                continue
            row_id = int(getattr(row, "id", 0) or 0)
            prior = latest.get(state.epic_ref)
            if prior is None or prior[0] < row_id:
                latest[state.epic_ref] = (row_id, state)
    return {ref: state for ref, (_, state) in latest.items()}


def archived_epic_refs(store: _Store) -> frozenset[str]:
    """The epic refs currently retired from the active views."""
    return frozenset(
        ref for ref, state in archive_states(store).items() if state.archived
    )


def is_archived(store: _Store, epic_ref: str) -> bool:
    """Whether `epic_ref` is currently archived."""
    state = archive_states(store).get(epic_ref)
    return state is not None and state.archived


# The driver signals that mean an epic finished of its own accord. A `complete`
# epic leaves the active views for the same reason an archived one does: there
# is no remaining work left for an entry to be about. Mirrored in
# frontend/src/lib/epic/roster.ts's `TERMINAL_SIGNALS`.
TERMINAL_SIGNALS = frozenset({"complete"})


def epic_is_retired(signal: Any, *, archived: bool) -> bool:
    """Whether this epic is out of the active views — retired, or finished.

    ONE predicate for the two readers that may never disagree about it: the
    needs-you set drops a retired epic's node waits and open holds
    (``_epic_roster`` in :mod:`kew.service.needs_you`), and the epic detail
    route composes no needs-you join for one (:mod:`kew.service.epics`). Split,
    the legend counted waiting nodes on an epic that no surface reading
    ``rows`` counted — the four-way disagreement #2207 exists to end, one epic
    over.
    """
    return archived or str(signal or "") in TERMINAL_SIGNALS


__all__ = [
    "EPIC_ARCHIVED_EVENT",
    "EPIC_UNARCHIVED_EVENT",
    "TERMINAL_SIGNALS",
    "ArchiveState",
    "archive_states",
    "archived_epic_refs",
    "epic_is_retired",
    "is_archived",
]
