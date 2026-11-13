"""Artifact freshness: what a review verdict was about, and what moved since.

The console's staleness badges (#2303) carry their CAUSE, and there are exactly
two of them:

* **review stale** — a SELF cause. The head moved after the verdict; the review
  is about a commit that is no longer what would merge.
* **needs rebase** — an UPSTREAM cause. The base moved. Conflicted or not, the
  thing that changed is not this branch.

Both are read off facts the driver already had and now journals (``head_oid``,
``merge_state``, ``pr_pushed_at`` on the snapshot node, ``journal._node_entry``),
so this module makes **no GitHub call** — the whole point of the endpoint over it
being bounded and cacheable. Nothing here reconciles, repairs or writes.

Silence on failure: a row never reports ``clean``/``current`` for a fact nobody
could read. An unjournaled merge state is ``base.state == "unknown"`` with a
reason naming why, an unjournaled head is ``latest_head.source == "unobserved"``
and makes no staleness claim at all, and every read that did not happen is named
in ``unavailable`` rather than collapsing into an empty page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.review_delta import VERDICT_NEEDS_FULL
from kew.service.attention import rec_item_id
from kew.service.models import (
    BaseStateDTO,
    LatestHeadDTO,
    PrFreshnessDTO,
    PrFreshnessRowDTO,
    ReviewVerdictRefDTO,
)
from kew.service.work import pending_run_max_age_s, review_in_flight

if TYPE_CHECKING:
    from kew.config import DispatchConfig

#: The default page size, and the ceiling a query string may not raise past.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Verdicts after which kew — not a person — owes the next round. Mirrors
#: ``kew.service.work._KEW_OWES_THE_ROUND``; a node between two of its own
#: rounds is REMEDIATING, and calling that "stale" would spend a badge on work
#: already under way.
_KEW_OWES_THE_ROUND = frozenset({"NEEDS_CHANGES", VERDICT_NEEDS_FULL})

#: GitHub ``mergeStateStatus`` -> (``base.state``, the sentence a reader gets).
#: ``UNSTABLE`` is about this head's own non-required checks, not about the base,
#: so the BASE reads clean and the sentence says what is actually red.
_BASE_BY_MERGE_STATE: dict[str, tuple[str, str]] = {
    "CLEAN": ("clean", "the head merges into its base"),
    "HAS_HOOKS": (
        "clean",
        "the head merges into its base; a repository hook runs on merge",
    ),
    "UNSTABLE": (
        "clean",
        "the head merges into its base; a non-required check is failing",
    ),
    "BEHIND": ("behind", "the base moved ahead of this head"),
    "DIRTY": ("conflicted", "the head and its base have conflicting changes"),
    "BLOCKED": ("blocked", "a branch-protection requirement on the base is not met"),
    "DRAFT": ("blocked", "the pull request is still a draft"),
}

#: The one ``base.state`` a person — not the factory — has to clear. The console
#: rations magenta for exactly this (DESIGN.md), and a rebase kew could do on its
#: own (``behind``) is deliberately not in it.
NEEDS_A_PERSON_BASE_STATES = frozenset({"conflicted"})

_PR_NUMBER = re.compile(r"/(?:pull|pulls|merge_requests)/(\d+)")

_UNREADABLE_MERGE_STATE = (
    "the driver has not journaled a merge state for this pull request yet"
)
_UNRECOGNISED_MERGE_STATE = "the forge reported a merge state kew does not know: "


def pr_number(pr_url: str | None) -> int | None:
    """The PR number off its own URL, or None — never composed from a slug."""
    found = _PR_NUMBER.search(pr_url or "")
    return int(found.group(1)) if found else None


def base_state(merge_state: str | None) -> tuple[str, str]:
    """``(state, reason)`` for one journaled ``mergeStateStatus``.

    An absent state is ``unknown`` with the reason that says so, and an
    unrecognised one is ``unknown`` NAMING the word the forge sent — a new
    GitHub value must read as "we do not know" rather than silently as clean.
    """
    word = (merge_state or "").strip().upper()
    if not word:
        return "unknown", _UNREADABLE_MERGE_STATE
    known = _BASE_BY_MERGE_STATE.get(word)
    if known is None:
        return "unknown", f"{_UNRECOGNISED_MERGE_STATE}{word}"
    return known


@dataclass(frozen=True)
class _Round:
    """The one review round this composition needs, provider-neutrally."""

    round: int
    verdict: str
    head_sha: str
    recorded_at: str


def review_state(
    *,
    last: _Round | None,
    latest_head: str,
    reviewing: bool,
) -> str:
    """Which of the five review states one item is in.

    Precedence, and why it is this one:

    1. ``reviewing`` — a round is reading the artifact RIGHT NOW. It outranks
       everything because whatever the last verdict said, the answer is being
       recomputed; a badge here would ask for a move nobody has to make.
    2. ``no_round`` — nothing has reviewed it. "Not stale" and "never reviewed"
       are different worlds, and only one of them is reassuring.
    3. ``remediating`` — the last verdict was NEEDS_CHANGES/NEEDS_FULL, so kew
       owes the next round. The head is EXPECTED to move; saying "review stale"
       of a head kew itself is moving is noise.
    4. ``stale`` — the head moved after the verdict. Needs a head to compare
       against: an unobserved one proves nothing, so it falls through.
    5. ``current``.
    """
    if reviewing:
        return "reviewing"
    if last is None:
        return "no_round"
    if last.verdict.strip().upper() in _KEW_OWES_THE_ROUND:
        return "remediating"
    if latest_head and last.head_sha and latest_head != last.head_sha:
        return "stale"
    return "current"


def _latest_round(rounds: list[Any]) -> _Round | None:
    """The newest recorded round, or None.

    Read off the epic's whole history (one query per epic) rather than
    ``latest_review_round`` per item (one query each). Cycle boundaries are
    deliberately NOT applied: the question here is "what head was the last
    verdict about", and a retry does not un-review the commit it read.
    """
    if not rounds:
        return None
    newest = rounds[-1]
    recorded = getattr(newest, "recorded_at", None)
    return _Round(
        round=int(getattr(newest, "round", 0) or 0),
        verdict=str(getattr(newest, "verdict", "") or ""),
        head_sha=str(getattr(newest, "head_sha", "") or ""),
        recorded_at=_iso(recorded),
    )


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value or "")


def _record_unread(unavailable: list[str], dimension: str, exc: Exception) -> None:
    """Name a read that did not happen, so an empty page is never a clean one.

    Every ``except`` here routes through this: "nothing is stale" and "we could
    not tell" must not be the same answer on the wire, and the only thing that
    separates them is a line in ``unavailable`` (CONTRIBUTING.md, "No silent
    clean defaults").
    """
    unavailable.append(f"{dimension}: {exc}")


def _rounds_by_item(
    store: Any, epic_refs: list[str], unavailable: list[str]
) -> dict[str, list[Any]]:
    """Every epic's review history, keyed by item id. One query per epic.

    A refused read is NAMED rather than returning an empty history: "no round
    recorded" and "the round table could not be read" would otherwise both read
    as ``no_round``, and only one of them means nothing has reviewed the work.
    """
    out: dict[str, list[Any]] = {}
    for ref in epic_refs:
        try:
            rounds = store.list_review_rounds_for_epic(ref)
        except Exception as exc:  # noqa: BLE001 - a refused read is reported
            _record_unread(unavailable, f"review_rounds:{ref}", exc)
            continue
        for row in rounds:
            out.setdefault(str(getattr(row, "item_id", "") or ""), []).append(row)
    return out


def _reviewing_items(
    store: Any, config: DispatchConfig | None, now: datetime, unavailable: list[str]
) -> frozenset[str]:
    """Item ids with one of kew's own review runs in flight.

    ONE read of the pending rows (they are bounded by the host's concurrency),
    then the SHARED predicate — :func:`kew.service.work.review_in_flight`, under
    the operator's own bound — so this surface and the Work screen cannot
    disagree about whether a round is running.
    """
    try:
        pending = list(store.list_pending())
    except Exception as exc:  # noqa: BLE001 - a refused read is reported
        _record_unread(unavailable, "pending_runs", exc)
        return frozenset()
    bound = pending_run_max_age_s(config) if config is not None else None
    by_item: dict[str, list[Any]] = {}
    for rec in pending:
        by_item.setdefault(rec_item_id(rec), []).append(rec)
    return frozenset(
        item
        for item, runs in by_item.items()
        if item and review_in_flight(runs, now=now, max_age_s=bound)
    )


def _row(
    facts: Any,
    last: _Round | None,
    *,
    reviewing: bool,
) -> PrFreshnessRowDTO:
    """One snapshot node, composed into one freshness row."""
    head = str(facts.head_oid or "")
    if head:
        latest = LatestHeadDTO(sha=head, source="snapshot", at=facts.pr_pushed_at)
    elif last is not None and last.head_sha:
        latest = LatestHeadDTO(
            sha=last.head_sha, source="review_round", at=last.recorded_at or None
        )
    else:
        latest = LatestHeadDTO()
    state, reason = base_state(facts.merge_state)
    return PrFreshnessRowDTO(
        item_id=facts.item_id,
        epic_ref=facts.epic_ref,
        pr_number=pr_number(facts.pr_url),
        pr_url=facts.pr_url,
        last_verdict=None
        if last is None
        else ReviewVerdictRefDTO(
            round=last.round,
            verdict=last.verdict,
            head_sha=last.head_sha,
            recorded_at=last.recorded_at,
        ),
        latest_head=latest,
        review_state=review_state(
            last=last,
            # Only a head read off the snapshot can contradict a verdict's head:
            # the round's own sha compared against itself is never "stale".
            latest_head=head,
            reviewing=reviewing,
        ),
        base=BaseStateDTO(
            state=state,
            reason=reason,
            since=facts.merge_state_since,
            # When the state was last READ, which the row's ``observed_at``
            # does not answer: the driver carries a merge state forward over a
            # tick GitHub answered UNKNOWN on, so the value can predate the
            # tick that reported it (``graph/driver/journal.py``).
            at=getattr(facts, "merge_state_at", None),
            source="snapshot" if facts.merge_state else "unobserved",
        ),
        observed_at=facts.observed_at,
    )


def compose_freshness(
    store: Any,
    config: DispatchConfig | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    epic: str | None = None,
    now: datetime | None = None,
) -> PrFreshnessDTO:
    """A bounded page of freshness over the OPEN items with a linked artifact.

    Three reads, none of them per row: the journaled PR facts (one query, every
    filter SQL-side), each epic's review history (one query per epic on the
    page), and the pending runs (one query). No GitHub call, so this is safe to
    poll and safe to cache.

    *cursor* is a previous page's ``next_cursor`` (``epic_ref|item_id``); an
    unparseable one is ignored and NAMED in ``unavailable`` rather than silently
    serving page one as if it were the page that was asked for.

    *epic* narrows the page to one epic, pushed into the store's own query. A
    surface that draws ONE epic has to be able to ask for it: a page bounded by
    *limit* over every epic can fill before it reaches the epic on screen, and
    the caller cannot tell that from "nothing here is stale" (#2303).
    """
    moment = now or datetime.now(UTC)
    unavailable: list[str] = []
    bounded = max(1, min(int(limit), MAX_LIMIT))
    after = _parse_cursor(cursor, unavailable)
    try:
        facts = list(
            store.latest_node_pr_facts(limit=bounded, after=after, epic_ref=epic)
        )
    except Exception as exc:  # noqa: BLE001 - a refused read is reported
        _record_unread(unavailable, "snapshot_pr_facts", exc)
        facts = []
    rounds = _rounds_by_item(store, sorted({f.epic_ref for f in facts}), unavailable)
    reviewing = _reviewing_items(store, config, moment, unavailable)
    rows = [
        _row(
            f,
            _latest_round(rounds.get(f.item_id, [])),
            reviewing=f.item_id in reviewing,
        )
        for f in facts
    ]
    full = len(rows) == bounded
    return PrFreshnessDTO(
        rows=rows,
        next_cursor=f"{rows[-1].epic_ref}|{rows[-1].item_id}"
        if full and rows
        else None,
        unavailable=unavailable,
        composed_at=moment.isoformat(),
    )


def _parse_cursor(cursor: str | None, unavailable: list[str]) -> tuple[str, str] | None:
    """``"epic|item"`` -> ``(epic, item)``, or None with the reason recorded."""
    if cursor is None or cursor == "":
        return None
    epic, _, item = cursor.partition("|")
    if not epic or not item:
        unavailable.append(f"cursor: {cursor!r} is not 'epic_ref|item_id'")
        return None
    return epic, item
