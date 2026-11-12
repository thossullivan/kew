"""The latest merge verify of one item's pull request, read off the audit (#3140).

A ``verify_completed`` row names no item, issue or pull request. What it names
is the commit it verified (``ref_sha``), the base it verified against
(``base_sha``) and, since #3140, the verified commit's parents
(``ref_parents``). The drive's merge verify (``drive_merge._combine``) and an
operator's ad hoc merge both verify a merge commit of the base and the PR head,
so ``ref_sha`` is almost never a PR head. The drive merges main into the PR
head, so the head is the FIRST parent; the ad hoc merge checks out main and
merges the PR in, so there the head is the SECOND parent.

So the join is by head, and this module owns both halves of it:

* :func:`item_pr_heads` — every head the item's pull request is known to have
  had in its CURRENT review cycle: the head the drive's newest snapshot holds,
  the ``review_rounds_in_cycle`` heads, and the heads the newest publication
  scan, credential finding and review verdict name. One of them is the
  current head.
* :func:`latest_merge_verify` — the newest ``verify_completed`` (by
  ``recorded_at``) that joins that set: its ``ref_sha`` is one of the heads,
  or it has exactly two parents, one of them is its ``base_sha``, and the
  OTHER one is one of the heads. Any other parent never joins: a stacked PR's
  bare verify has the PR below it as its parent, and that is not a verify of
  the PR below. Within the head the newest match joined on, the newest row
  that reached a verdict (``passed`` or ``failed``) is served; an
  ``incomplete`` one only when that head has no verdict (#3479), so a
  re-verify that did not finish never hides a failure. The reading says which
  head matched and whether that is the current one.

A verify recorded before ``ref_parents`` existed links only when it verified
the bare head. A malformed row is skipped, never a 500. A caller that names a
``repo`` (``kew report provenance``) joins only the rows recorded against it,
or against a slug ``kew rename-repo`` renamed to it before that rename
(:func:`kew.repo_rename.repo_match`).

Only allowlisted fields cross into the DTO: the outcome, the head, the
``error_category`` and the held-out ids. The event's free-text ``error`` never
does (it is host output), and neither do the parents, which feed the join only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kew.merge_verify import VERIFY_EVENT
from kew.publication_scan import ALREADY_PUBLISHED_EVENT, OUTPUT_SCAN_EVENT
from kew.repo_rename import RepoMatch, repo_match
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_store import MAX_NODE_PR_FACTS_LIMIT
from kew.service.models import MergeVerifyDTO, MergeVerifyHeldOutDTO

_logger = logging.getLogger(__name__)

#: How far back the verify events are read. Same window as
#: ``drive_merge.browser_checked``: the events are not item-keyed, so the read
#: is a bounded scan of the newest rows.
VERIFY_EVENT_WINDOW = 500

#: The verify's own outcome vocabulary (``VerifyResult.outcome``).
_OUTCOMES = frozenset({"passed", "failed", "incomplete"})
#: The outcomes that are a verdict on a head: a verify that did not finish
#: never replaces one at the same head (#3479).
_VERDICTS = frozenset({"passed", "failed"})

#: The audit events whose ``head_sha`` names a head the pull request had: the
#: pushed head the host scanned, the head a credential reached, the reviewed one.
HEAD_EVENTS: tuple[str, ...] = (
    REVIEW_VERDICT_EVENT,
    OUTPUT_SCAN_EVENT,
    ALREADY_PUBLISHED_EVENT,
)


class VerifyUnreadable(Exception):
    """A read the join needs did not answer, so "none linked" is not known."""


@dataclass(frozen=True)
class PrHeads:
    """The heads one item's pull request had in its current review cycle.

    ``current`` is the head it has now (``""`` only when ``known`` is empty);
    ``known`` holds it and every earlier head of the cycle, lowercase.
    """

    current: str = ""
    known: frozenset[str] = frozenset()


def _sha(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _payload(row: Any) -> Mapping[str, Any]:
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _snapshot_head(store: Any, item_id: str, epic_ref: str | None) -> str:
    """The head the drive's newest snapshot of *epic_ref* journaled for the item.

    ``""`` when there is no epic, no snapshot or no head in it. A store that
    cannot answer raises :class:`VerifyUnreadable`: an older head must never be
    read as the current one because the current one went unread.
    """
    reader = getattr(store, "latest_node_pr_facts", None)
    if not epic_ref or not callable(reader):
        return ""
    try:
        rows = reader(epic_ref=epic_ref, open_only=False, limit=MAX_NODE_PR_FACTS_LIMIT)
    except Exception as exc:
        raise VerifyUnreadable(f"the drive snapshot did not answer: {exc}") from exc
    for row in rows:
        if str(getattr(row, "item_id", "")) == item_id:
            return _sha(getattr(row, "head_oid", None))
    return ""


def _cycle_rounds(store: Any, item_id: str) -> tuple[Any, ...]:
    reader = getattr(store, "review_rounds_in_cycle", None)
    if not callable(reader):
        return ()
    try:
        return tuple(reader(item_id))
    except Exception as exc:
        raise VerifyUnreadable(f"the review rounds did not answer: {exc}") from exc


def item_head_events(
    store: Any, item_id: str, *, repo: str | RepoMatch | None = None
) -> tuple[Any, ...]:
    """The newest scan, credential finding and verdict recorded for the item.

    With *repo*, the newest of each recorded against *repo* (:func:`_of_repo`):
    a same-numbered item of another repo, or a row with no repo, names no head.
    """
    reader = getattr(store, "list_audit_events", None)
    if not callable(reader):
        raise VerifyUnreadable("this store records no audit events")
    try:
        match = _matcher(store, repo)
        return tuple(
            row
            for event in HEAD_EVENTS
            for row in _newest_rows(reader, event, item_id, match)
        )
    except Exception as exc:
        raise VerifyUnreadable(f"the audit did not answer: {exc}") from exc


def _matcher(store: Any, repo: str | RepoMatch | None) -> RepoMatch | None:
    """*repo* as a matcher that knows its renames; ``None`` matches every row."""
    if repo is None or isinstance(repo, RepoMatch):
        return repo
    return repo_match(store, repo)


def _newest_rows(
    reader: Any, event: str, item_id: str, repo: RepoMatch | None
) -> list[Any]:
    """The newest *event* row of the item, of *repo* when one is named."""
    if repo is None:
        return list(reader(limit=1, event=event, item_id=item_id))
    rows = reader(limit=None, event=event, item_id=item_id)
    return next(([row] for row in rows if _of_repo(row, repo)), [])


def _newest_event_head(events: Iterable[Any]) -> str:
    """The head named by the newest of *events*, ``""`` when none names one."""
    named = [
        (str(getattr(row, "timestamp", "") or ""), head)
        for row in events
        if row is not None and (head := _sha(_payload(row).get("head_sha")))
    ]
    return max(named)[1] if named else ""


def item_pr_heads(
    store: Any,
    item_id: str,
    *,
    epic_ref: str | None = None,
    rounds: Sequence[Any] | None = None,
    events: Sequence[Any] | None = None,
) -> PrHeads:
    """Every head *item_id*'s pull request had in its current review cycle.

    *rounds* (``review_rounds_in_cycle``, oldest first) and *events* (the
    newest scan, credential and verdict rows) are read here unless the caller
    already holds them. *epic_ref* defaults to the rounds' own.

    The current head is the drive snapshot's, else the newest round's, else the
    one the newest of *events* names.
    """
    held_rounds = _cycle_rounds(store, item_id) if rounds is None else tuple(rounds)
    held_events = item_head_events(store, item_id) if events is None else tuple(events)
    round_heads = [_sha(getattr(r, "head_sha", None)) for r in held_rounds]
    event_heads = [_sha(_payload(row).get("head_sha")) for row in held_events]
    current = _snapshot_head(
        store, item_id, epic_ref or _rounds_epic(held_rounds)
    ) or _current_recorded_head(round_heads, held_events)
    known = frozenset(head for head in (current, *round_heads, *event_heads) if head)
    return PrHeads(current=current, known=known)


def _rounds_epic(rounds: Sequence[Any]) -> str | None:
    """The epic the newest round that names one was recorded under."""
    named = [str(r.epic_ref) for r in rounds if getattr(r, "epic_ref", "")]
    return named[-1] if named else None


def _current_recorded_head(round_heads: Sequence[str], events: Sequence[Any]) -> str:
    """With no snapshot head: the newest round's head, else the newest event's."""
    reviewed = [head for head in round_heads if head]
    return reviewed[-1] if reviewed else _newest_event_head(events)


#: How many ids of each held-out list cross into the DTO, and how long each may
#: be. The payload is the host's: a broken base can fail thousands of tests, and
#: a parametrized id can carry a whole input. The rest is counted, not dropped.
HELD_OUT_IDS_MAX = 50
HELD_OUT_ID_CHARS_MAX = 300


def _clip(node_id: str) -> str:
    """*node_id*, or its head plus a short digest of the whole id when too long.

    The digest keeps two ids that share their first 300 characters apart (the
    page keys its list by id, and marks excused ids by matching them), and the
    same id clips the same way in ``failed``, ``declared`` and ``undeclared``.
    """
    if len(node_id) <= HELD_OUT_ID_CHARS_MAX:
        return node_id
    digest = hashlib.sha256(node_id.encode()).hexdigest()[:8]
    return node_id[: HELD_OUT_ID_CHARS_MAX - len(digest) - 1] + "…" + digest


def _ids(value: object) -> tuple[list[str], int]:
    """The first :data:`HELD_OUT_IDS_MAX` ids, clipped, and how many more."""
    if not isinstance(value, (list, tuple)):
        return [], 0
    ids = [item for item in value if isinstance(item, str) and item]
    kept = [_clip(node_id) for node_id in ids[:HELD_OUT_IDS_MAX]]
    return kept, len(ids) - len(kept)


def _held_out(value: object) -> MergeVerifyHeldOutDTO | None:
    if not isinstance(value, Mapping):
        return None
    outcome = value.get("outcome")
    failed, failed_more = _ids(value.get("failed"))
    declared, declared_more = _ids(value.get("declared"))
    undeclared, undeclared_more = _ids(value.get("undeclared"))
    return MergeVerifyHeldOutDTO(
        outcome=outcome if isinstance(outcome, str) else "",
        failed=failed,
        declared=declared,
        undeclared=undeclared,
        failed_more=failed_more,
        declared_more=declared_more,
        undeclared_more=undeclared_more,
    )


def _recorded_at(row: Any, payload: Mapping[str, Any]) -> str:
    recorded = payload.get("recorded_at")
    if isinstance(recorded, str) and recorded:
        return recorded
    return str(getattr(row, "timestamp", "") or "")


def _merged_head(parents: Sequence[str], base_sha: object) -> str:
    """The PR head a merge of it and the base was made from, else ``""``.

    Only a two-parent commit one of whose parents IS the base the verify
    recorded is such a merge; the other parent is the head, in either order —
    the drive merges main into the PR head (head first), an operator's ad hoc
    merge checks out main and merges the PR in (head second). Any other parent
    list (a stacked PR's single parent, a merge of two branches) names no head.
    """
    base = _sha(base_sha)
    if len(parents) != 2 or not base or base not in parents:
        return ""
    first, second = parents
    return second if first == base else first


def _matched_head(payload: Mapping[str, Any], known: frozenset[str]) -> str | None:
    """The head this row joins on, ``""`` for none, ``None`` for a malformed row.

    The verified commit itself, when it is one of *known*; else the head a
    merge of the base was made from (:func:`_merged_head`). An event recorded
    before ``ref_parents`` existed has none, and joins on ``ref_sha`` alone.
    """
    ref_sha = payload.get("ref_sha")
    parents = payload.get("ref_parents", [])
    if not isinstance(ref_sha, str) or not isinstance(parents, list):
        return None
    if not all(isinstance(parent, str) for parent in parents):
        return None
    verified = _sha(ref_sha)
    if verified in known:
        return verified
    merged = _merged_head([_sha(p) for p in parents], payload.get("base_sha"))
    return merged if merged in known else ""


def _dto(
    row: Any, payload: Mapping[str, Any], head: str, current: str
) -> MergeVerifyDTO:
    outcome = payload.get("outcome")
    category = payload.get("error_category")
    return MergeVerifyDTO(
        # An outcome some newer host wrote is not one this reader can word: it
        # reads incomplete, never passed. A pass is served only when a verify
        # recorded it, and stands when a later verify of its head ends with no
        # verdict (:func:`_served`).
        outcome=outcome
        if isinstance(outcome, str) and outcome in _OUTCOMES
        else "incomplete",
        head=head,
        is_current_head=head == current,
        error_category=category if isinstance(category, str) and category else None,
        held_out=_held_out(payload.get("held_out")),
        recorded_at=_recorded_at(row, payload),
    )


def _of_repo(row: Any, repo: RepoMatch | None) -> bool:
    """Whether *row* may join: always without *repo*, else only *repo*'s row.

    *repo*'s row is one recorded against it, or against a slug renamed to it
    before the rename (:class:`kew.repo_rename.RepoMatch`). Fails closed: with
    a *repo* named, a row with no recorded repo never joins.
    """
    return repo is None or repo.holds(row)


def read_verify_rows(
    store: Any, *, window: int | None = VERIFY_EVENT_WINDOW
) -> Sequence[Any]:
    """The newest *window* ``verify_completed`` rows of *store*; ``None``: all.

    Raises :class:`VerifyUnreadable` when the store cannot be read.
    """
    reader = getattr(store, "list_audit_events", None)
    if not callable(reader):
        raise VerifyUnreadable("this store records no audit events")
    try:
        return list(reader(limit=window, event=VERIFY_EVENT))
    except Exception as exc:
        raise VerifyUnreadable(str(exc)) from exc


#: One joined row: its ``recorded_at``, the row, its payload and its head.
_Match = tuple[str, Any, Mapping[str, Any], str]


def latest_merge_verify(
    store: Any,
    heads: PrHeads,
    *,
    window: int | None = VERIFY_EVENT_WINDOW,
    repo: str | RepoMatch | None = None,
    rows: Sequence[Any] | None = None,
) -> MergeVerifyDTO | None:
    """The newest ``verify_completed`` that joins one of *heads*, or ``None``.

    The newest match picks the head. Of that head's matches, the newest that
    reached a verdict (``passed`` or ``failed``) is served, and an
    ``incomplete`` one only when the head has no verdict: a re-verify that
    did not finish knows nothing new about the head (#3479).

    ``None`` is "no verify is linked to these heads". A store that cannot be
    read raises :class:`VerifyUnreadable` instead, so a caller can name it.
    *window* bounds the scan to the newest events; ``None`` reads them all,
    for a reader that covers the whole history (``kew report provenance``).
    *repo*, when given, joins only the rows recorded against it. *rows*, when
    given, are the ``verify_completed`` rows already read
    (:func:`read_verify_rows`), so a caller joining many items reads them once;
    *window* is then unused.
    """
    if not heads.known:
        return None
    if rows is None:
        rows = read_verify_rows(store, window=window)
    try:
        match = _matcher(store, repo)
    except Exception as exc:
        raise VerifyUnreadable(f"the audit did not answer: {exc}") from exc
    matches: list[_Match] = []
    for row in rows:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, Mapping) or not _of_repo(row, match):
            continue
        head = _matched_head(payload, heads.known)
        if head:
            matches.append((_recorded_at(row, payload), row, payload, head))
    if not matches:
        return None
    _at, row, payload, head = _served(matches)
    return _dto(row, payload, head, heads.current)


def _served(matches: Sequence[_Match]) -> _Match:
    """The match served: the newest verdict at the newest match's head, else
    that newest match (#3479). *matches* is never empty."""
    newest = max(matches, key=lambda match: match[0])
    verdicts = [
        match
        for match in matches
        if match[3] == newest[3] and _is_verdict(match[2].get("outcome"))
    ]
    return max(verdicts, key=lambda match: match[0]) if verdicts else newest


def _is_verdict(outcome: Any) -> bool:
    """Whether *outcome* is ``passed`` or ``failed``; a malformed one is not."""
    return isinstance(outcome, str) and outcome in _VERDICTS


def item_merge_verify(
    store: Any,
    item_id: str,
    *,
    epic_ref: str | None = None,
    rounds: Sequence[Any] | None = None,
    events: Sequence[Any] | None = None,
    window: int | None = VERIFY_EVENT_WINDOW,
    repo: str | RepoMatch | None = None,
    rows: Sequence[Any] | None = None,
) -> MergeVerifyDTO | None:
    """The latest merge verify of *item_id*'s pull request, or ``None``.

    The one join the work page and ``kew report provenance`` (#3143) share.
    *window*, *repo* and *rows* are :func:`latest_merge_verify`'s.
    """
    heads = item_pr_heads(
        store, item_id, epic_ref=epic_ref, rounds=rounds, events=events
    )
    return latest_merge_verify(store, heads, window=window, repo=repo, rows=rows)


#: Why a failed verify failed, read off ``held_out.outcome`` — the words the
#: Work screen's MERGE VERIFY section prints (``FAILED_BECAUSE`` in
#: frontend/src/lib/work/merge-verify.ts), so a merge decision that leads with
#: the failure says what that section says (#3460).
_FAILED_BECAUSE = {
    "passed": "the PR's own checks failed; main's tests pass on it",
    "skipped": "the PR's own checks failed, and main's tests were skipped",
    "failed": "main's tests did not run to a verdict on it",
}
_EXCUSED_ONLY = (
    "the PR's own checks failed; main's tests fail only where a behavior change"
    " is declared"
)


def _undeclared_count(held_out: MergeVerifyHeldOutDTO) -> int:
    """Main's tests nobody declared, counting the ones the DTO left off."""
    declared = set(held_out.declared)
    unexcused = [test for test in held_out.failed if test not in declared]
    return max(len(held_out.undeclared), len(unexcused)) + held_out.undeclared_more


def _tests(count: int) -> str:
    return "1 of main's tests fails" if count == 1 else f"{count} of main's tests fail"


def failed_verify_sentence(verify: MergeVerifyDTO | None) -> str:
    """The failure of a verify that failed at the PR's CURRENT head, else ``""``.

    "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR", with
    no closing period: a merge decision's sentence leads with it (#3460). A
    verify that passed, did not finish, or failed at a head the pull request
    has since moved past is not this head's failure, and says nothing here.
    """
    if verify is None or verify.outcome != "failed" or not verify.is_current_head:
        return ""
    at = f"Merge verify failed at {verify.head[:8]}"
    held_out = verify.held_out
    if held_out is None:
        return at
    undeclared = _undeclared_count(held_out)
    if undeclared > 0:
        return f"{at}: {_tests(undeclared)} on this PR"
    if held_out.outcome == "not_run":
        return f"{at} before main's tests ran"
    because = (
        _EXCUSED_ONLY
        if held_out.outcome == "passed" and held_out.failed
        else _FAILED_BECAUSE.get(held_out.outcome, "")
    )
    return f"{at}: {because}" if because else at
