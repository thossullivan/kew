"""Which audit rows belong to a repository that was renamed (#3527).

``kew rename-repo`` moves a project's stored rows from an old repository slug
to the configured one, but never ``audit_events``: each row's ``repo`` is in
its ``entry_hash`` preimage, so rewriting it would break ``kew audit verify``
and every published anchor. The command appends one ``repo_renamed`` event
instead, recorded against the new slug with ``{"from": OLD, "to": NEW}`` in its
payload.

A reader that scopes audit rows to a repository asks :func:`repo_match`. A row
recorded under slug ``S`` counts for the repository when
``floor(S) < row.id < cutoff(S)``:

* the repository's own slug has no cutoff; its floor is the newest event that
  renamed it AWAY, since rows before that belong to the repository it became
  (the name was later reused);
* a slug renamed INTO the repository's lineage has, as its cutoff, the id of
  the EARLIEST event that renamed it there: a row recorded under the old name
  after that comes from whatever repository holds the name now. Its floor is
  the newest earlier event that renamed that slug somewhere else.

No floor is no lower bound. Renames chain: after ``a -> b`` then ``b -> c``,
``a``'s rows before the first event count for ``c``. A rename into a name only
joins the lineage while that name still belonged to it: ``a -> b`` after
``b -> c`` makes ``a``'s rows the new ``b``'s, not ``c``'s.

This read is permanent, since hashed history keeps the old slug.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The audit event ``kew rename-repo`` appends.
REPO_RENAMED_EVENT = "repo_renamed"

#: One ``repo_renamed`` event: ``(from, to, id)``.
Rename = tuple[str, str, int]


@dataclass(frozen=True)
class RepoMatch:
    """Whether an audit row was recorded against *repo*, renames included."""

    repo: str
    #: Each slug renamed (directly or through a chain) into *repo*, to the id
    #: of the event that renamed it: its rows count while their id is below it.
    renamed: Mapping[str, int] = field(default_factory=dict)
    #: Each slug, *repo*'s own included, to the id its rows must be above.
    floors: Mapping[str, int] = field(default_factory=dict)

    def _bounds(self, slug: object) -> tuple[int | None, int | None] | None:
        """``(floor, cutoff)`` of *slug*'s rows, None when none can count."""
        if not self.repo or not isinstance(slug, str) or not slug:
            return None
        if slug == self.repo:
            return self.floors.get(slug), None
        if slug not in self.renamed:
            return None
        return self.floors.get(slug), self.renamed[slug]

    def holds(self, row: Any) -> bool:
        """Whether *row* is *repo*'s. Fails closed: no repo matches nothing,
        and a row whose id cannot be compared to a bound matches nothing."""
        bounds = self._bounds(getattr(row, "repo", None))
        if bounds is None:
            return False
        if bounds == (None, None):
            return True
        floor, cutoff = bounds
        row_id = getattr(row, "id", None)
        return (
            isinstance(row_id, int)
            and (floor is None or row_id > floor)
            and (cutoff is None or row_id < cutoff)
        )


def _rename_of(row: Any) -> Rename | None:
    """``(from, to, id)`` of a well-formed ``repo_renamed`` row, else None."""
    if getattr(row, "event", None) != REPO_RENAMED_EVENT:
        return None
    payload = getattr(row, "payload", None)
    row_id = getattr(row, "id", None)
    if not isinstance(payload, Mapping) or not isinstance(row_id, int):
        return None
    old, new = payload.get("from"), payload.get("to")
    if not (isinstance(old, str) and old and isinstance(new, str) and new):
        return None
    # The event is recorded against the slug it renamed to; a row that
    # disagrees with its own payload vouches for nothing.
    if getattr(row, "repo", None) != new or old == new:
        return None
    return old, new, row_id


def _renames(rows: Iterable[Any]) -> list[Rename]:
    return [rename for row in rows if (rename := _rename_of(row)) is not None]


def _floor(slug: str, below: int | None, renames: Iterable[Rename]) -> int | None:
    """The newest event that renamed *slug* away before id *below* (before
    any id when None): *slug*'s rows up to it belong to where it went."""
    return max(
        (
            event_id
            for old, _new, event_id in renames
            if old == slug and (below is None or event_id < below)
        ),
        default=None,
    )


def _joins(
    repo: str, rename: Rename, cutoffs: Mapping[str, int], events: list[Rename]
) -> bool:
    """Whether *rename* moved its old slug into *repo*'s lineage: into *repo*
    or a lineage name, while that name was the lineage's (above its floor and,
    for a lineage name, below its own cutoff)."""
    old, new, event_id = rename
    if old == repo:
        return False
    reach = None if new == repo else cutoffs.get(new)
    if new != repo and (reach is None or event_id >= reach):
        return False
    floor = _floor(new, reach, events)
    return floor is None or event_id > floor


def renamed_into(repo: str, renames: Iterable[Rename]) -> dict[str, int]:
    """Each slug renamed into *repo*'s lineage, to the id its rows count below.

    A slug's cutoff is the earliest event that renamed it into a name of the
    lineage while that name belonged to the lineage: after the name's own
    floor (its newest earlier rename away) and before its own cutoff. Each
    pass recomputes every cutoff from the previous pass, so a chain ``n``
    renames long settles in ``n`` passes; the pass count is bounded by the
    event count, so a cycle cannot spin.
    """
    events = list(renames)
    cutoffs: dict[str, int] = {}
    for _ in range(len(events) + 1):
        following: dict[str, int] = {}
        for rename in events:
            if _joins(repo, rename, cutoffs, events):
                old, event_id = rename[0], rename[2]
                following[old] = min(event_id, following.get(old, event_id))
        if following == cutoffs:
            break
        cutoffs = following
    return cutoffs


def floors_of(
    repo: str, cutoffs: Mapping[str, int], renames: Iterable[Rename]
) -> dict[str, int]:
    """The id each slug's rows must be above to count for *repo*.

    *repo*'s own floor is its newest rename away. A renamed slug's floor is
    the newest event that renamed it elsewhere before its lineage event.
    """
    events = list(renames)
    bounds: list[tuple[str, int | None]] = [(repo, None), *cutoffs.items()]
    floors: dict[str, int] = {}
    for slug, below in bounds:
        floor = _floor(slug, below, events)
        if floor is not None:
            floors[slug] = floor
    return floors


def repo_match(store: Any, repo: str) -> RepoMatch:
    """*repo*'s matcher, with every rename the store's audit records.

    A store with no audit reader (a test double) matches *repo* alone.
    """
    reader = getattr(store, "list_audit_events", None)
    if not repo or not callable(reader):
        return RepoMatch(repo)
    renames = _renames(reader(limit=None, event=REPO_RENAMED_EVENT))
    cutoffs = renamed_into(repo, renames)
    return RepoMatch(repo, cutoffs, floors_of(repo, cutoffs, renames))
