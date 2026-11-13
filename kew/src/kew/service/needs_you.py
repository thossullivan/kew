"""ONE query for the needs-you set (#2207).

Four surfaces print "a person is needed here": the factory header's counter,
Monitor's holds-and-blocks list, an epic's needs-you legend entry, and the Work
screen's needs-you filter. Each used to decide for itself what the words meant,
and they came apart in the way that disagreement always comes apart — the header
said 16, Monitor said 4, a go-live epic's legend said 0 over six waiting nodes,
and Work showed one row under a banner apologising for the rest.

So the set is composed once, here, and every surface reads it:

* :func:`needs_you_set` is the query. It returns one entry per thing waiting on
  a person — an open spend approval, an epic hold, a stalled driver, an epic
  node awaiting a merge or a continuation, a work-projection exception, an
  orphan PR — each naming its **epic**, its **item** and the **action**.
* ``kew.service.dashboard.assemble`` serves those entries as the judgment queue
  (``rows``), which is what the header counts, what Monitor's column lists and
  what Work's filter and list read. One field, so no two of the three can
  diverge.
* The epic surfaces read the same entries node-scoped, through
  ``kew.service.attention.compose_node_attention``, which classifies a node's
  wait with the same ``kew.waits`` table used here.
* :func:`needs_you_by_epic` counts the SAME entries per epic, served beside
  them as ``DashboardDTO.needs_you_by_epic`` (#2279). The epic surfaces that
  show a NUMBER rather than a row — each epic page's legend, and every row of
  the Epics list — read a bucket of that map. They used to count for
  themselves, and did not agree: 1 on the header over legends saying 1 and 2,
  with the Epics list showing none at all.

Fail-soft per source, like the payload it feeds: a reader that raises is named
in ``unavailable`` and contributes no entries, so a count is a floor rather than
a lie. "Nobody is waiting" and "we could not tell" stay different answers.

The entry type is :class:`~kew.service.models.DashboardRowDTO` — the shipped
judgment-row wire contract, which already carries the type, the consequence, the
reason, the action, the item, the epic and the URL. Reusing it is what lets the
header and Monitor read the set without a second payload, and keeps every client
that already groups on ``type`` working unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.epic_archive import archived_epic_refs, epic_is_retired
from kew.epic_holds import TEST_DECLARATIONS_HOLD_PREFIX
from kew.ids import item_ref
from kew.report import open_approval_holds
from kew.service.attention import (
    PR_REVIEW_EXCEPTIONS,
    approval_reason,
    closing_item,
    done_node_ids,
    epic_hold_attention,
    exception_kind,
    executing_items,
    hold_item_id,
    merge_items,
    merge_verify_failures,
    merged_pr_waits,
    needs_a_person,
    node_wait,
    pr_wait_merged,
    rec_item_id,
    say_wait,
    stale_hold_wait,
    wait_reason,
    work_projection,
    work_reason,
)
from kew.service.epics import driver_liveness, reconciled_epic_snapshots
from kew.service.models import DashboardRowDTO, WorkProjectionDTO, WorkViewDTO
from kew.waits import (
    EPIC_RUN_COMMAND,
    UNBUCKETED_HELD_COMMAND,
    bucket_for,
    fill_command,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from kew.config import DispatchConfig

# Urgency class per row type (money -> epic -> review -> wait -> hygiene). New
# types are added here; a row whose type is absent falls to the lowest priority.
CONSEQUENCE = {
    "approval_hold": "money",
    "budget_hold": "money",
    "stalled_epic": "epic",
    "pr_review": "review",
    "work_exception": "review",
    "epic_wait": "wait",
    "orphan_pr": "hygiene",
    "work_unreadable": "hygiene",
    # A triage proposal is a judgment waiting on a person, so it ranks with the
    # other review-pressure rows: above a bare wait, below an open epic hold
    # and below money. Nothing dispatches off it (#2293).
    "triage_proposal": "review",
    # A merge queue stopped on an unverified main merges nothing for anybody
    # until a person repairs main and resumes it, so it ranks with a stalled
    # epic (#2883).
    "merge_queue_suspect": "epic",
}
CONSEQUENCE_ORDER = {"money": 0, "epic": 1, "review": 2, "wait": 3, "hygiene": 4}

_logger = logging.getLogger(__name__)


def _iso(value: Any) -> str:
    """Best-effort ISO-8601 string from a datetime | str | None ("" for None)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _humanize_age(seconds: int) -> str:
    """Format a duration in seconds as a short human string (e.g. '6d 8h')."""
    if seconds >= 86400:
        d, rem = divmod(seconds, 86400)
        return f"{d}d {rem // 3600}h"
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        return f"{h}h {rem // 60}m"
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    return f"{seconds}s"


@dataclass(frozen=True)
class NeedsYouSet:
    """Everything waiting on a person, and what could not be read to say so.

    ``projection`` is the work projection the entries were composed from, handed
    back so a caller needing the review-pressure reading takes it from THIS
    snapshot rather than reading the store a second time — which is how two
    halves of one console start disagreeing.
    """

    entries: list[DashboardRowDTO] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    as_of: dict[str, str] = field(default_factory=dict)
    projection: WorkProjectionDTO | None = None


def needs_you_set(
    store: Any, config: DispatchConfig, *, now: datetime | None = None
) -> NeedsYouSet:
    """The needs-you set: one entry per thing waiting on a person.

    Every source is fail-soft and named in ``unavailable`` when it raises, so
    one flaky reader degrades the set instead of sinking the payload it feeds.
    """
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    unavailable: list[str] = []
    as_of: dict[str, str] = {}

    def source(name: str, fn: Callable[[], Any], default: Any) -> Any:
        """Run a reader; on any error, mark it unavailable and degrade."""
        try:
            value = fn()
        except Exception:  # noqa: BLE001 — fail-soft: one source never sinks the set
            unavailable.append(name)
            return default
        as_of[name] = now_iso
        return value

    projection: WorkProjectionDTO | None = source(
        "pr_reviews", lambda: work_projection(store, config, now=now), None
    )
    # The epic walk reads the archive ledger once and deserializes a full
    # snapshot per epic, and THREE sources below need its answer, so it is read
    # once per set rather than once each: ten `latest_epic_tick` calls for five
    # epics, on a 5-10s poll, for one answer. Memoized rather than hoisted so a
    # failure still reaches each source's own `source()` and is named there —
    # the set has no "epic_roster" name of its own, and a raise outside the
    # wrappers would sink the whole payload.
    roster = _epic_roster_reader(store)
    # The open holds answer two questions too — which nodes a person has to
    # unblock, and which journaled hold an operator has since cleared — so they
    # are read once per set, under the same success-only memo.
    epic_holds = _memoize(lambda: list(store.list_all_epic_holds()))

    def held_nodes() -> dict[str, frozenset[str]] | None:
        """The live hold table for the stale-hold check, or ``None`` for unread.

        Read off ``budget_holds``'s OWN confession rather than a second
        handler: that source runs first over the same memo, so a failure is
        named once, where it is already named, and asking again here is a cache
        hit. ``None`` suppresses nothing (see
        :func:`kew.service.attention.stale_hold_wait`) — a read that did not
        happen cannot prove a hold was cleared, so ``epic_waits`` serves its
        journaled waits rather than following ``budget_holds`` down.
        """
        if "budget_holds" in unavailable:
            return None
        return _held_by_epic(epic_holds())

    entries: list[DashboardRowDTO] = []
    entries.extend(source("approval_holds", lambda: _approval_rows(store), []))
    entries.extend(
        source("budget_holds", lambda: _budget_rows(epic_holds(), roster, store), [])
    )
    entries.extend(
        source("stalled_epics", lambda: _stalled_rows(store, now, roster().live), [])
    )
    entries.extend(
        source(
            "work_rows",
            lambda: _work_rows(projection, store, config.branches.prefix),
            [],
        )
    )
    entries.extend(
        source(
            "epic_waits",
            lambda: _epic_wait_rows(
                roster().live, held_nodes(), _merged_waits(store, roster().live)
            ),
            [],
        )
    )
    entries.extend(source("orphan_prs", lambda: _orphan_rows(store), []))
    entries.extend(source("triage_proposals", lambda: _triage_rows(store), []))
    entries.extend(
        source(
            "merge_queue",
            lambda: _merge_queue_rows(store, config.project.repo, now),
            [],
        )
    )
    if not config.project.repo and any(
        entry.type == "merge_queue_suspect" for entry in entries
    ):
        # Stops this console cannot place in a project are served, not
        # hidden, and the source is named so no count reads as complete.
        unavailable.append("merge_queue")
        as_of.pop("merge_queue", None)

    collapsed = _without_executing(
        _collapse(entries),
        source(
            "executing_runs",
            lambda: executing_items(store, config, now),
            frozenset(),
        ),
    )
    _place_in_epics(
        collapsed, source("node_epics", lambda: _epic_by_item(roster()), {})
    )
    failures = merge_verify_failures(
        store,
        merge_items((e.item_id or "", e, e.epic_ref) for e in collapsed),
    )
    for entry in collapsed:
        item = entry.item_id or ""
        say_wait(
            entry,
            entry.attention_kind,
            item_id=item,
            failure=failures.get(item, ""),
        )

    return NeedsYouSet(
        entries=collapsed,
        unavailable=unavailable,
        as_of=as_of,
        projection=projection,
    )


def epic_entries(
    entries: Iterable[DashboardRowDTO], epic_ref: str
) -> list[DashboardRowDTO]:
    """The set, narrowed to one epic — the ENTRIES behind that epic's count.

    :func:`needs_you_by_epic` is the same filter, counted, and is what the
    console's epic surfaces read. This is for a caller that needs the rows
    themselves, and for the tests that hold the two to each other.
    """
    return [e for e in entries if (e.epic_ref or "") == epic_ref]


def needs_you_by_epic(entries: Iterable[DashboardRowDTO]) -> dict[str, int]:
    """How many of the set each epic owns — one number per epic, from the set.

    This is the ONE filter (#2279/D6). Before it, the header counted ``rows``,
    each epic page's legend counted the diamonds its own node join had put on
    its own snapshot, and the Epics list counted nothing at all — so the header
    said 1 over legends saying 1 and 2, and no reading could be checked against
    another. Composing it here means the three surfaces read one number each
    out of one set, and :func:`needs_you_set` is the only thing that decides
    what is in it.

    An entry naming no epic is in no bucket, which is why this is a map and not
    a partition: an open spend approval on a loose issue and an orphan PR
    belong to the fleet, not to a drain. The header's count is therefore the
    sum of the buckets PLUS those, and
    ``kew/tests/test_needs_you_one_set.py`` holds it to exactly that.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        ref = entry.epic_ref or ""
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    return counts


def needs_you_without_epic(entries: Iterable[DashboardRowDTO]) -> int:
    """How many of the set name no epic — the "no epic" bucket's size (#2784).

    :func:`needs_you_by_epic` leaves these out, so the header used to read 11
    over an Epics list totalling 10, the eleventh an epic-less PR wait with no
    visible home. Served beside the map so the Epics list can give them a row
    of their own, and the header is the map's sum plus exactly this.
    """
    return sum(1 for entry in entries if not entry.epic_ref)


def _without_executing(
    entries: list[DashboardRowDTO], executing: frozenset[str]
) -> list[DashboardRowDTO]:
    """The set less every entry on an item a run is executing on (#2784)."""
    return [e for e in entries if not (e.item_id and e.item_id in executing)]


def _epic_by_item(roster: _EpicRoster) -> dict[str, str]:
    """Which live epic each node belongs to.

    The map that makes an entry addressable BY EPIC. Three of the set's sources
    start from something that has no epic on it — an approval hold is keyed by
    issue number, the work projection by item, an orphan PR by pull request —
    so a merge decision on an epic node reached the set with ``epic_ref``
    empty, and an epic legend filtering the set by its own ref could not show
    the very row the header was counting (#2279/D6).

    An ``external`` node is skipped for the reason :func:`_epic_wait_rows`
    skips it: ``build_graph`` copies a resolved out-of-epic dependency into
    every epic that depends on it, and the epic that OWNS the node is the one
    where a person can act. First writer wins for the same reason — the walk is
    in the store's own order, so the answer is stable.

    The node join drops those copies too
    (``kew.service.attention.boundary_ids``), and it has to: this map is what
    files an item-keyed entry under ONE epic, so a copy that stayed in the join
    drew a diamond on the dependent epic's DAG while that epic's legend —
    counting this map's buckets — read 0.

    A node belonging to a RETIRED epic is in no bucket either, because the
    roster's ``live`` list is what is walked here. The join is gated on the same
    predicate (``kew.service.attention._retirement``), so the two still agree:
    an archived epic's legend and its DAG both read nobody.
    """
    by_item: dict[str, str] = {}
    for epic_ref, snapshot in roster.live:
        for node in (snapshot or {}).get("nodes") or ():
            if not isinstance(node, dict) or node.get("external"):
                continue
            node_id = str(node.get("id", ""))
            if node_id:
                by_item.setdefault(node_id, epic_ref)
    return by_item


def _place_in_epics(entries: list[DashboardRowDTO], by_item: dict[str, str]) -> None:
    """Fill in the epic an entry belongs to, where its own reader knew none.

    Never overwrites: a reader that named an epic named the one the row is
    about, and the hold table's ``epic_ref`` is the authority on a hold.
    An unreadable walk serves an empty map, which leaves every entry exactly as
    its own source wrote it — the header still counts them, and the legend that
    cannot place them says so through ``unavailable`` rather than through a
    quietly shorter list.
    """
    for entry in entries:
        if not entry.epic_ref and entry.item_id:
            entry.epic_ref = by_item.get(entry.item_id) or None


# --------------------------------------------------------------------------- #
# One entry per waiting thing
# --------------------------------------------------------------------------- #


def _collapse(entries: list[DashboardRowDTO]) -> list[DashboardRowDTO]:
    """One entry per item, in consequence order, with the others folded in.

    An item can be waiting in more than one of the readers' eyes at once: a node
    the driver holds is both an epic hold and a journaled ``held`` state, and a
    node awaiting a merge is both an epic wait and the projection's
    ``awaiting_human_decision``. Each of those is ONE thing a person has to do,
    so counting both would inflate every surface's count by exactly the overlap
    — the header's 16 over Monitor's 4 was this, in the other direction.

    The highest-consequence entry wins (money before epic before review before a
    bare wait), because that is the order the console already ranks rows in. What
    the losers know that the winner does not is folded into it rather than
    dropped: an epic wait is usually the only reader that knows which epic the
    item belongs to, and the epic is half of what makes an entry addressable.

    An entry naming no item is never collapsed: a stalled epic and an orphan PR
    are each distinct, and a shared empty ``item_id`` is not an identity.
    """
    entries.sort(key=lambda r: CONSEQUENCE_ORDER.get(r.consequence, 99))
    kept: dict[str, DashboardRowDTO] = {}
    out: list[DashboardRowDTO] = []
    for entry in entries:
        key = entry.item_id or ""
        if key == "":
            out.append(entry)
            continue
        winner = kept.get(key)
        if winner is None:
            kept[key] = entry
            out.append(entry)
            continue
        _fold_into(winner, entry)
    return out


#: The fields a collapsed entry donates to the one that outranked it, each only
#: when the winner has nothing of its own there. None of them changes what the
#: entry IS; they make it addressable (which epic, which issue, which artifact)
#: — or, for ``work_unread``, they carry forward what the read COULD NOT say.
#:
#: ``work_unread`` is the one that is not addressability. An item in
#: ``projection.unreadable`` reaches the set as a ``work_unreadable`` entry, and
#: that entry is the lowest consequence there is, so an item that is BOTH
#: unreadable and (say) held loses it to the hold — and the payload stopped
#: saying the item's work facts were never read. That fact is what the console's
#: per-item dismissal guard is made of (``unreadItems`` in
#: frontend/src/lib/dashboard.ts): without it a decided review on an unreadable
#: item is drawn again the moment the projection recovers, the CS-6
#: resurrection the dismissal exists to prevent.
_FOLDED_FIELDS = ("epic_ref", "issue_num", "url", "pr_number", "age", "work_unread")


def _fold_into(winner: DashboardRowDTO, loser: DashboardRowDTO) -> None:
    for name in _FOLDED_FIELDS:
        if not getattr(winner, name, None) and getattr(loser, name, None):
            setattr(winner, name, getattr(loser, name))


def _approval_rows(store: Any) -> list[DashboardRowDTO]:
    return [
        DashboardRowDTO(
            type="approval_hold",
            consequence=CONSEQUENCE["approval_hold"],
            reason=approval_reason(hold),
            primary_action="approve",
            issue_num=hold.issue_num,
            item_id=str(hold.issue_num) if hold.issue_num else "",
            estimate_usd=hold.estimate_usd,
            attention_kind="approval",
            # `kew approve <issue>` exists, but the console decides a spend
            # approval with its own button against `/api/approvals`, and the
            # one row that already carries that button is not the place to
            # print a second way of doing it.
            command="",
        )
        for hold in open_approval_holds(store)
    ]


def _budget_rows(
    holds: list[Any], roster: Callable[[], _EpicRoster], store: Any
) -> list[DashboardRowDTO]:
    """One entry per open epic hold a PERSON can clear, on an epic still live.

    Which holds those are is not decided here: ``epic_hold_attention`` is the
    same call the node join makes, so a hold that raises the epic legend's
    diamond is exactly a hold that reaches the header's count. The CI gate is
    the one it takes out (``kew.waits.classify_wait`` — an operator cannot make
    a check finish). Left in, it was the four-surface disagreement this module
    exists to remove, twice over: the header, Monitor and Work counted a
    machine's own wait as somebody's move while the legend showed no diamond
    for it, and — money outranking review in :func:`_collapse` — a CI hold on a
    node with an open PR rewrote that node's real merge decision as
    "budget_hold / ci_pending / approve".

    A RETIRED epic's hold is the other half of that (#2243). An open hold is a
    live row, so it used to be served whatever shelf its epic was on — four
    `budget_hold` rows on two abandoned drains, counting 6 on the header where
    the operator expected 2, with no legend diamond behind any of them, because
    the legend's own epic had already left the active views. `kew epic archive`
    promises the epic "is excluded from the console's needs-you count", so the
    promise is kept for every entry the epic owns, this one included.

    A LANDED node's hold is the per-node form of that same exclusion (#2392
    review). A partially landed epic is still live, so the ref test above lets
    it through, and the uncleared hold of a node whose work has finished went
    on filing a row — one payload serving that node ``status=done, stuck=None``
    while counting it as waiting on a person, with ``kew epic status``
    reporting no hold for it at all. Nothing clears a hold once its node stops
    being dispatched, so the node's own reading decides, exactly as it does in
    the driver's hold precedence (``overlay._hold_stands``).

    *roster* is the shared walk (:func:`_epic_roster`), asked only when there
    is a hold to judge: with an empty table there is nothing to leak, and
    naming this source unavailable over an epic read it did not need would
    blank the header's count for no reason. When there IS a hold, a failed
    archive read propagates to :func:`needs_you_set`'s wrapper, which names
    this source and serves none of them — a leak and a silent drop are both
    worse than "at least N".
    """
    rows = []
    epics = roster() if holds else _EpicRoster([], frozenset(), {})
    for hold in holds:
        attention = epic_hold_attention(hold)
        if attention is None or _hold_is_settled(hold, epics):
            continue
        # EpicHold carries the durable id (backfilled str of issue_num for
        # GitHub); a Linear node's real key rides here, not #0.
        item_id = getattr(hold, "item_id", "") or ""
        label, command = _hold_words(hold, item_id)
        rows.append(
            DashboardRowDTO(
                type="budget_hold",
                consequence=CONSEQUENCE["budget_hold"],
                age=_iso(hold.held_at),
                reason=attention.reason,
                # Unchanged, because the console's own verbs read it
                # (frontend/src/lib/dashboard.ts). It is not the move: an
                # approve answers `not_held` on an `epic_holds` row, which is
                # what `command` is for.
                primary_action="approve",
                issue_num=hold.issue_num,
                item_id=item_id,
                epic_ref=hold.epic_ref,
                attention_kind=attention.kind,
                label=label,
                command=command,
                title=_node_title(epics, str(hold.epic_ref or ""), item_id),
                node_ids=_declaration_node_ids(store, hold, item_id),
            )
        )
    return rows


def _declaration_node_ids(store: Any, hold: Any, item_id: str) -> list[str]:
    """Every id a ``test_declarations_missing`` hold names, in full (#2884)."""
    if not str(hold.reason or "").startswith(TEST_DECLARATIONS_HOLD_PREFIX):
        return []
    return list(store.epic_hold_node_ids(item_id))


def _node_title(epics: _EpicRoster, epic_ref: str, item_id: str) -> str:
    """The title a live epic's snapshot gives *item_id*, or "" (#2786)."""
    for ref, snapshot in epics.live:
        if ref != epic_ref:
            continue
        for node in (snapshot or {}).get("nodes") or ():
            if isinstance(node, dict) and str(node.get("id", "")) == item_id:
                return str(node.get("title") or "")
    return ""


def _hold_is_settled(hold: Any, epics: _EpicRoster) -> bool:
    """Is there nothing left for a person to decide behind this hold?

    Either its whole epic has been retired, or the node it stands on is read
    ``done`` — see :func:`_budget_rows` for why each exclusion is there.
    """
    epic_ref = str(getattr(hold, "epic_ref", "") or "")
    if epic_ref in epics.retired:
        return True
    return hold_item_id(hold) in epics.done.get(epic_ref, frozenset())


def _hold_words(hold: Any, item_id: str) -> tuple[str, str]:
    """One hold's operator label and the command that clears it, filled in.

    Read off the BUCKET rather than off a second ``classify_wait`` so the
    unbucketed fallback lands here in the shape the rest of the table is
    written in: no label — the reason is the only thing anybody wrote, and a
    surface prints that — and an unhold, which is what removes a row in
    ``epic_holds`` whatever wrote it.
    """
    bucket = bucket_for(str(getattr(hold, "reason", "") or ""))
    label = bucket.label if bucket is not None else ""
    template = bucket.command if bucket is not None else UNBUCKETED_HELD_COMMAND
    return label, fill_command(
        template, epic=str(getattr(hold, "epic_ref", "") or ""), item=item_id
    )


@dataclass(frozen=True)
class _EpicRoster:
    """The epics split by whether a person still has work on them.

    ``live`` pairs each epic nobody has retired with its latest SNAPSHOT, for
    the two sources that read one. ``retired`` is every ref on the other side
    of the line — the sources that start from a ROW rather than from the walk
    (an open epic hold) need the exclusion, not the inclusion, and a hold can
    name an epic the walk never reached.

    ``done`` is the per-NODE form of that exclusion, for the same row-first
    sources: which nodes of each live epic the reconciled snapshot reads as
    ``done``. A PARTIALLY landed epic is not retired, so its refs are in
    neither set, and an uncleared hold on one of its finished nodes needs the
    node's own answer (#2392 review).
    """

    live: list[tuple[str, dict[str, Any] | None]]
    retired: frozenset[str]
    done: dict[str, frozenset[str]]


def _epic_roster(store: Any) -> _EpicRoster:
    """Each epic, sorted into the live ones and the retired ones.

    An archived epic has been retired by an operator (#2206): its drain stays in
    the ledger and on the Epics list's Finished shelf, but a driver nobody
    intends to resume is not a thing a person has to act on, so it leaves the
    needs-you set — and with it the header's count. A COMPLETE epic leaves for
    the same reason: there is no remaining work for an entry to be about.

    An epic with no tick at all is LIVE. "This ledger has journaled nothing for
    this epic" is not a retirement decision, and a driver killed before its
    first tick had already written its holds — exactly the ones somebody has to
    clear.

    The snapshot each live entry carries is the RECONCILED one
    (:func:`kew.service.epics.reconciled_epic_snapshots`), which is also what
    decides the retirement: the Epics list and every epic page read the journal
    through that same repair, and a node the ledger has settled reaching this
    walk as live work is the #2207 one-page-two-answers failure — the same
    payload reporting an epic ``complete`` with no node attention while its
    judgment queue lists its nodes as waiting on a person (#2392 review).
    """
    archived = archived_epic_refs(store)
    # Seeded with the archived refs rather than collected from the walk: a hold
    # can name an epic that `list_epic_refs` no longer returns, and the archive
    # decision is the authority on it either way. Archived refs are also dropped
    # ahead of the reconciliation: an archived epic is retired whatever any
    # snapshot of it says, so it is not worth a read.
    retired = set(archived)
    refs = [ref for ref in store.list_epic_refs() if ref not in archived]
    snapshots = reconciled_epic_snapshots(
        store, {r: store.latest_epic_tick(r) for r in refs}
    )
    live: list[tuple[str, dict[str, Any] | None]] = []
    for epic_ref in refs:
        snapshot = snapshots.get(epic_ref)
        signal = snapshot.get("signal") if snapshot else None
        if epic_is_retired(signal, archived=False):
            retired.add(epic_ref)
            continue
        live.append((epic_ref, snapshot))
    return _EpicRoster(
        live=live,
        retired=frozenset(retired),
        # The same reading the node join drops a settled node's diamond by, so
        # the legend and the diamonds under it count one set (#2392 review).
        done={
            ref: done_node_ids((snapshot or {}).get("nodes")) for ref, snapshot in live
        },
    )


def _memoize[T](read: Callable[[], T]) -> Callable[[], T]:
    """*read*, run at most once — and only if somebody asks.

    Only a SUCCESS is cached: a read that raised is re-run by the next caller,
    so each fail-soft source that needed it names itself unavailable on its own
    rather than inheriting a neighbour's cached exception.
    """
    held: list[T] = []

    def once() -> T:
        if not held:
            held.append(read())
        return held[0]

    return once


def _epic_roster_reader(store: Any) -> Callable[[], _EpicRoster]:
    """:func:`_epic_roster`, memoized — see :func:`_memoize`."""
    return _memoize(lambda: _epic_roster(store))


def _stalled_rows(
    store: Any, now: datetime, live: list[tuple[str, dict[str, Any] | None]]
) -> list[DashboardRowDTO]:
    rows = []
    for epic_ref, _snapshot in live:
        liveness = driver_liveness(store, epic_ref, now=now)
        if not liveness.driver_gone:
            continue
        age_s = int(liveness.age_s or 0)
        rows.append(
            DashboardRowDTO(
                type="stalled_epic",
                consequence=CONSEQUENCE["stalled_epic"],
                age=liveness.last_heartbeat_at or "",
                reason=f"driver gone — {epic_ref} — no tick in {_humanize_age(age_s)}",
                primary_action="resume",
                epic_ref=epic_ref,
                # `primary_action` says "resume", which is the console's verb
                # and not a command: `kew resume` clears the fleet-stop latch.
                # What restarts a driver nobody is ticking is `kew epic run`.
                command=fill_command(EPIC_RUN_COMMAND, epic=epic_ref),
            )
        )
    return rows


def _held_by_epic(holds: list[Any]) -> dict[str, frozenset[str]]:
    """Which node each epic still holds.

    Grouped BY EPIC because a hold is keyed by (epic, item) and ``build_graph``
    copies a resolved out-of-epic dependency into every epic that depends on it
    — the owning epic is the one where a person can act, so another epic's hold
    must not keep this epic's boundary copy alive.
    """
    by_epic: dict[str, set[str]] = {}
    for hold in holds:
        ref = str(getattr(hold, "epic_ref", "") or "")
        by_epic.setdefault(ref, set()).add(hold_item_id(hold))
    return {ref: frozenset(ids) for ref, ids in by_epic.items()}


def _merged_waits(
    store: Any, live: list[tuple[str, dict[str, Any] | None]]
) -> frozenset[tuple[str, str]]:
    """Every live epic's journaled PR waits whose PR has merged, in one read."""
    nodes = [n for _, snapshot in live for n in (snapshot or {}).get("nodes") or ()]
    return merged_pr_waits(store, nodes)


def _epic_wait_rows(
    live: list[tuple[str, dict[str, Any] | None]],
    held: dict[str, frozenset[str]] | None,
    merged: frozenset[tuple[str, str]] = frozenset(),
) -> list[DashboardRowDTO]:
    """One entry per epic node whose journaled state waits on a person.

    This is the source the first-pass UI had nowhere: a node awaiting a merge, a
    continuation, a host publication or a no-change decision is waiting on a
    human, and none of those reaches the work projection unless the store can
    also sync a pull request for it. The classification is ``kew.waits``'s, the
    same table the driver's live attention stream reads, so a node's row here
    and its tick line there say the same thing.

    An ``external`` node is skipped: ``build_graph`` records a resolved
    out-of-epic dependency inside every epic that depends on it, and the epic
    that OWNS the node is the one where a person can act.

    The snapshot is a JOURNAL, not live state, so a journaled HOLD is checked
    against the live hold table (*held*, see
    :func:`kew.service.attention.stale_hold_wait`) — the one wait here that has
    a row behind it, and so the one that can be cleared without the snapshot
    ever being rewritten.

    A wait on a pull request is checked the same way, against the PR state the
    run row records (*merged*, see :func:`kew.service.attention.merged_pr_waits`):
    the merge happens on the forge, and the journal that still says "awaiting
    merge" is only rewritten by a tick — which an epic nobody is driving never
    gets (#2538). The node join drops the same waits, so the legend and the
    diamonds under it still count one set.
    """
    rows = []
    for epic_ref, snapshot in live:
        epic_held = None if held is None else held.get(epic_ref, frozenset())
        for node in (snapshot or {}).get("nodes") or ():
            if pr_wait_merged(node, merged):
                continue
            row = _epic_wait_row(
                epic_ref, node, epic_held, (snapshot or {}).get("signal")
            )
            if row is not None:
                rows.append(row)
    return rows


def _epic_wait_row(
    epic_ref: str, node: Any, held: frozenset[str] | None, signal: Any = None
) -> DashboardRowDTO | None:
    if not isinstance(node, dict) or node.get("external"):
        return None
    node_id = str(node.get("id", ""))
    status = str(node.get("status") or "")
    stuck = node.get("stuck")
    if stale_hold_wait(stuck, node_id, held):
        return None
    wait = node_wait(status, stuck, signal=signal)
    if wait is None or not node_id:
        return None
    command = fill_command(wait.command, epic=epic_ref, item=node_id)
    return DashboardRowDTO(
        type="epic_wait",
        consequence=CONSEQUENCE["epic_wait"],
        age=_iso(node.get("status_since")),
        reason=f"{item_ref(node_id)} — {wait_reason(wait, status, stuck, command)}",
        primary_action=wait.action,
        issue_num=int(node_id) if node_id.isdecimal() else None,
        item_id=node_id,
        epic_ref=epic_ref,
        url=node.get("pr_url") or node.get("url"),
        attention_kind=wait.kind.value,
        label=wait.label,
        # The node's own title, the one its epic card prints (#2786).
        title=str(node.get("title") or ""),
        # The one that named this issue: SFD-232's row said "resume with `kew
        # epic resume go-live-2026-09 SFD-232`", and `primary_action` carries
        # the bucket's hint with its `<name> <node>` placeholders still in it.
        command=command,
    )


# One safest action -> one console verb. The action is derived ONCE, in
# kew.service.work; this table only names it for a button.
PRIMARY_ACTION = {
    "await_human_decision": "review",
    "address_review_feedback": "revise",
    "fix_checks": "fix-ci",
    "resolve_conflict": "resolve",
    "synchronize_tracker": "sync",
    "synchronize_source": "sync",
    "confirm_no_change": "confirm",
    "confirm_publication_risk": "confirm",
    "finalize_publication": "publish",
    "observe_remote": "refresh",
    "dispatch": "dispatch",
    "wait": "wait",
    "none": "inspect",
}


#: One safest action -> the command that performs it, as a template over the
#: facts the row already carries. Deliberately short: a command is here only
#: when this projection holds every argument it needs, so `kew review <pr>`,
#: `kew resolve <pr>` and `kew ci-fix <actions-run>` are absent — the view
#: carries a PR *url* and no Actions run id at all, and a command whose
#: argument a surface had to guess is the wrong command, which is the whole of
#: #2244. An action with no entry serves "", and the console keeps the verb it
#: already renders from ``primary_action``.
SAFEST_COMMAND = {
    "finalize_publication": "kew finalize {run}",
    "observe_remote": "kew report sync",
}


def _work_rows(
    projection: WorkProjectionDTO | None, store: Any, prefix: str = ""
) -> list[DashboardRowDTO]:
    """One row per item the projection found waiting on a PERSON, plus the ones
    it could not read at all — an item dropped in silence would render as a
    shorter, healthier world than the real one.

    Off-normal is not the same as needing somebody:
    :data:`kew.service.attention.QUIET_EXCEPTIONS` is the one place that line
    is drawn, and the node join reads it too.

    Every row is keyed by the item its pull request CLOSES (#2576), never by
    the pull request's own number: a run recorded under a PR number on the
    ``<prefix><n>`` branch of item *n* is item *n*'s work. When item *n* has a
    view of its own over the SAME pull request, that view is the one judged —
    it carries the item's review rounds, which the PR-numbered view never sees —
    and the PR-numbered one is dropped rather than counted a second time.

    A branch can carry more than one pull request over time, and either view
    may be the one holding the earlier, dead one. Only the item's CURRENT pull
    request is judged (:func:`_current_prs`): a view about any other answers
    nothing about the live one, and its row would hide it.
    """
    if projection is None:
        return []
    by_item = {rec_item_id(rec): rec for rec in store.list_syncable_runs()}
    own = {
        (view.identity.item_id or "", view.artifact.pr_url)
        for view in projection.views
        if view.artifact.pr_url
    }
    current = _current_prs(projection.views, by_item, prefix)
    rows = [
        row
        for view in projection.views
        if (row := _keyed_work_row(view, by_item, own, current, prefix)) is not None
    ]
    rows.extend(_unreadable_row(item_id) for item_id in projection.unreadable)
    return rows


def _keyed_work_row(
    view: WorkViewDTO,
    by_item: dict[str, Any],
    own: set[tuple[str, str]],
    current: dict[str, str],
    prefix: str,
) -> DashboardRowDTO | None:
    """*view*'s row keyed by the item it closes, or ``None`` when it has none:
    nobody is asked, the view is about a pull request its item has moved past,
    or the closed item's own view of the same pull request answers for it."""
    item_id = view.identity.item_id or ""
    closes = _closes(view, by_item, prefix)
    url = view.artifact.pr_url
    stale = bool(url) and current.get(closes, url) != url
    answered = closes != item_id and (closes, url) in own
    if stale or answered or not needs_a_person(view):
        return None
    return _work_row(view, by_item.get(item_id), closes)


def _current_prs(
    views: list[WorkViewDTO], by_item: dict[str, Any], prefix: str
) -> dict[str, str]:
    """Each item's CURRENT pull request, over every view that closes it.

    An open pull request outranks a closed or merged one — a branch has at most
    one open — and otherwise the latest-started run's does. Which view carries
    it (the item's own, or one recorded under a PR number) says nothing about
    which is newer, so neither is presumed to be.
    """
    best: dict[str, tuple[tuple[bool, str], str]] = {}
    for view in views:
        url = view.artifact.pr_url
        if not url:
            continue
        rec = by_item.get(view.identity.item_id or "")
        is_open = str(getattr(rec, "pr_state", "") or "").lower() == "open"
        rank = (is_open, _iso(getattr(rec, "started_at", None)))
        closes = _closes(view, by_item, prefix)
        if closes not in best or rank > best[closes][0]:
            best[closes] = (rank, url)
    return {item: url for item, (_rank, url) in best.items()}


def _closes(view: WorkViewDTO, by_item: dict[str, Any], prefix: str) -> str:
    item_id = view.identity.item_id or ""
    rec = by_item.get(item_id)
    return closing_item(item_id, getattr(rec, "branch", "") or "", prefix)


def _work_row(view: WorkViewDTO, rec: Any, item_id: str = "") -> DashboardRowDTO:
    own = view.identity.item_id or ""
    item_id = item_id or own
    # A row re-keyed to the item its pull request closes names THAT issue, not
    # the PR number the run was recorded under (#2576).
    issue_num = int(item_id) if item_id != own else getattr(rec, "issue_num", None)
    kind = (
        "pr_review"
        if view.exception.state in PR_REVIEW_EXCEPTIONS
        else "work_exception"
    )
    return DashboardRowDTO(
        type=kind,
        consequence=CONSEQUENCE[kind],
        age=_iso(getattr(rec, "finished_at", None))
        or _iso(getattr(rec, "started_at", None)),
        reason=work_reason(view, item_id),
        primary_action=PRIMARY_ACTION.get(view.safest_action.action, "inspect"),
        issue_num=issue_num,
        item_id=item_id,
        url=view.artifact.pr_url,
        # ``exception_kind`` is the node join's own call, so this row and the
        # DAG diamond over the same item file it under one kind.
        attention_kind=exception_kind(view),
        # No label: the wording in that field is ``kew.waits``'s, and no bucket
        # describes a projection exception — here ``reason`` is the words.
        command=fill_command(
            SAFEST_COMMAND.get(view.safest_action.action, ""),
            run=view.identity.run_id or "",
        ),
    )


def _unreadable_row(item_id: str) -> DashboardRowDTO:
    return DashboardRowDTO(
        type="work_unreadable",
        consequence=CONSEQUENCE["work_unreadable"],
        reason=f"{item_ref(item_id)} — its work facts could not be read; this is"
        " not an item with nothing wrong",
        primary_action="inspect",
        item_id=item_id,
        # Survives :func:`_collapse` even when a higher-consequence entry on the
        # same item takes this one's place — see :data:`_FOLDED_FIELDS`.
        work_unread=True,
    )


def _orphan_rows(store: Any) -> list[DashboardRowDTO]:
    return [
        DashboardRowDTO(
            type="orphan_pr",
            consequence=CONSEQUENCE["orphan_pr"],
            age=_iso(orphan.detected_at),
            reason=f"orphan PR — {orphan.reason}",
            primary_action="ack",
            pr_number=orphan.pr_number,
            url=orphan.url,
        )
        for orphan in store.list_orphan_prs()
    ]


MERGE_QUEUE_RUN_COMMAND = "kew merge queue run"
"""The command that resumes a stopped merge queue (#2883).

A pass verifies main before it merges anything, and a green verify clears the
stop — so this is the resume, run once main is repaired. ``kew merge queue
list`` shows the stop and its reason first.
"""


def _merge_queue_rows(store: Any, repo: str, now: datetime) -> list[DashboardRowDTO]:
    """The rows for this project's merge queue, while it is stopped.

    Read off the ledger's ``merge_queue_suspects`` table, not off the
    ``merge_queue_attention`` audit event: the event is said once, and the row
    has to stay until the queue is resumed — a pass that verifies main green
    deletes the suspect row, and with it this entry.

    Only the configured repo: ``kew merge queue run`` and ``list`` act on the
    project they are run in, so a stopped queue of another repo sharing this
    ledger is that project's row, where its command works as printed. With no
    repo (``kew serve`` pins the detected one, so detection failed) a stop
    cannot be placed, and hiding it would hide the alert: every stop is
    served, each naming the checkout its command runs in, and the caller names
    the source unavailable so no count reads as complete.
    """
    reader = getattr(store, "list_merge_queue_suspects", None)
    if reader is None:
        # A fake store with no merge queue has no stopped queue to serve, as
        # for triage. A FAILED read raises and is named in ``unavailable``.
        return []
    stops = [s for s in reader() if not repo or s.repo == repo]
    rows = []
    for suspect in stops:
        age_s = max(0, int((now - suspect.since).total_seconds()))
        where = "" if repo else f" in {suspect.repo}'s checkout"
        rows.append(
            DashboardRowDTO(
                type="merge_queue_suspect",
                consequence=CONSEQUENCE["merge_queue_suspect"],
                age=_iso(suspect.since),
                reason=(
                    f"merge queue stopped for {_humanize_age(age_s)} — "
                    f"{suspect.repo} — {suspect.state()} after {suspect.reason}; "
                    f"inspect with `kew merge queue list`{where}, repair main, "
                    "then resume"
                ),
                primary_action="resume",
                command=MERGE_QUEUE_RUN_COMMAND,
                attention_kind="merge_queue_suspect",
                # The row's name on Work; ``repo`` is its identity (`rowKey`).
                title=f"merge queue stopped — {suspect.repo}",
                repo=suspect.repo,
            )
        )
    return rows


TRIAGE_DECIDE_COMMAND = "kew triage decide {item}"
"""The command that resolves a triage proposal, filled with the row's item.

This is the ONLY thing that acts on a proposal: the station records what it
proposed and stops (#2293). The command is runnable as printed —
``tests/test_triage_needs_you.py`` walks the Typer app to hold it to that, the
same guard ``kew.waits``'s templates are under (#2244).
"""

#: What each outcome asks a person to do, in the words the row prints. Keyed on
#: the ``triage_decisions.outcome`` vocabulary, so a new outcome shows up as a
#: missing key here rather than as a row with no words.
_TRIAGE_WORDS = {
    "propose_dispatch": "dispatch",
    "ask": "ask",
    "duplicate_candidate": "close as duplicate",
    "human_decision": "decide",
    "split": "split",
    "unavailable": "triage unavailable",
}


def _triage_rows(store: Any) -> list[DashboardRowDTO]:
    """One row per unresolved triage proposal. Fail-soft via its own source.

    A store that cannot serve triage proposals (a ledger predating the
    ``triage_decisions`` table) raises, and :func:`needs_you_set` names
    ``triage_proposals`` in ``unavailable`` — so the count is a floor rather
    than a claim that nobody is waiting.
    """
    reader = getattr(store, "list_unresolved_triage_decisions", None)
    if reader is None:
        # A fake or legacy store predating the station has no proposals to
        # serve. Distinguished from a FAILED read, which raises and is named.
        return []
    return [_triage_row(row) for row in reader()]


def _triage_row(row: Any) -> DashboardRowDTO:
    item_id = str(getattr(row, "item_id", "") or "")
    issue_num = getattr(row, "issue_num", None)
    command = fill_command(TRIAGE_DECIDE_COMMAND, item=item_id)
    return DashboardRowDTO(
        type="triage_proposal",
        consequence=CONSEQUENCE["triage_proposal"],
        age=_iso(getattr(row, "recorded_at", None)),
        reason=_triage_reason(row),
        # The exact command, not a verb: a proposal has no console button, so
        # the one thing a surface can print is what to run (#2293).
        primary_action=command,
        command=command,
        attention_kind="triage_proposal",
        label=_TRIAGE_WORDS.get(str(getattr(row, "outcome", "")), "decide"),
        item_id=item_id,
        issue_num=issue_num if isinstance(issue_num, int) and issue_num else None,
    )


def _triage_reason(row: Any) -> str:
    """The sentence an operator reads: what triage proposed, and off what.

    Taken from the stored ``decision_json`` reason the policy wrote, so the row
    prints the policy's own words rather than a second wording of them. A row
    whose JSON cannot be read says so instead of printing a bare outcome name —
    an unreadable record is not a clean one.
    """
    outcome = str(getattr(row, "outcome", "") or "")
    stored = _triage_decision_reason(row)
    if stored:
        return stored
    words = _TRIAGE_WORDS.get(outcome, outcome or "decide")
    return f"triage proposes: {words} (its recorded reason could not be read)"


def _triage_decision_reason(row: Any) -> str:
    try:
        decision = json.loads(getattr(row, "decision_json", "") or "{}")
    except (TypeError, ValueError):
        _logger.warning(
            "Triage proposal %s has unreadable decision JSON; the needs-you row"
            " falls back to its outcome name",
            getattr(row, "id", "?"),
        )
        return ""
    reason = decision.get("reason") if isinstance(decision, dict) else None
    return str(reason) if isinstance(reason, str) and reason else ""
