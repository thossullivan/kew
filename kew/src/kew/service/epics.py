"""Epic DAG reads over the tick journal (#454 Phase B). The store is
injected; snapshots are parsed as plain JSON — this module NEVER imports
kew.graph.* (import-linter contract: the snapshot JSON is the layering
boundary between kew.graph and kew.serve/kew.service).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import unquote

from kew.continuation_recovery import (
    ContinuationHistory,
    continuation_histories,
    preserved_host_death_reason,
)
from kew.drive_heartbeat import DRIVE_GONE, DriveHeartbeat, read_drive_heartbeat
from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S, cadence_lapsed
from kew.epic_archive import ArchiveState, archive_states
from kew.epic_node_states import (
    TERMINAL_NODE_STATES,
    epic_signal,
    journaled_merged_unclosed,
    node_counts,
    settle_landed,
)
from kew.service.attention import (
    UNREADABLE_PROJECTION,
    AttentionJoin,
    compose_node_attention,
)
from kew.service.models import (
    EpicAttentionDTO,
    EpicDetailDTO,
    EpicEconomicsDTO,
    EpicHeartbeatDTO,
    EpicNodeDetailDTO,
    EpicNodeDTO,
    EpicNodeRunDTO,
    EpicRecoveryDTO,
    EpicSnapshotDTO,
    EpicSummaryDTO,
    EpicTickDTO,
    EpicTickStripDTO,
    EpicTimelineBandDTO,
    EpicTimelineDTO,
    EpicTimelineNodeDTO,
    EpicTimelineRoundDTO,
    EpicTimelineRunDTO,
    EpicTimelineSpanDTO,
    NodeRecoveryDTO,
)
from kew.waits import HOST_RESTART_REPEATED_REASONS, failed_node_command

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)


class _Store(Protocol):
    def list_epic_refs(self) -> list[str]: ...
    def latest_epic_tick(self, epic_ref: str) -> Any | None: ...
    def list_epic_ticks(
        self, epic_ref: str, *, after_id: int = 0, limit: int = 1000
    ) -> list[Any]: ...
    def list_epic_tick_log_tail(
        self, epic_ref: str, *, limit: int = 200
    ) -> list[Any]: ...
    def list_epic_tick_log_marks(
        self, epic_ref: str, *, limit: int = 200
    ) -> list[Any]: ...
    def epic_tick_log_stamp(self, epic_ref: str) -> str: ...
    def count_epic_ticks(self, epic_ref: str) -> int: ...
    def first_epic_tick_ts(self, epic_ref: str) -> str | None: ...
    def get_epic_driver_state(self, epic_ref: str) -> Any | None: ...
    def list_by_issue(self, issue_num: int) -> list[Any]: ...
    def list_by_item(self, item_id: str) -> list[Any]: ...
    def sum_costs_by_item(
        self, item_ids: list[str], *, since: str, until: str | None = None
    ) -> dict[str, float]: ...
    def list_by_items(self, item_ids: list[str]) -> dict[str, list[Any]]: ...
    def list_review_rounds_for_epic(self, epic_ref: str) -> list[Any]: ...
    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        event_prefix: str | None = None,
        epic_ref: str | None = None,
        item_ids: Sequence[str] | None = None,
        since: Any | None = None,
    ) -> list[Any]: ...


# Cap on how many snapshot frames a single fetch_epic_frames call emits when
# a subscriber has fallen far behind (or is catching up from an empty
# cursor). Snapshots are idempotent full-state, so skipping the older ones
# and jumping the cursor straight to the newest is safe for a live view —
# full history remains available via list_ticks()/the /ticks endpoint.
_MAX_SNAPSHOT_FRAMES = 50


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _utc_stamp(raw: Any, *, subject: str) -> datetime | None:
    """One ISO-8601 stamp as an aware UTC datetime, or None when unreadable.

    A naive stamp is read as UTC, the only timezone any kew writer stamps in.
    An unreadable one is tolerated and compared with nothing, never repaired
    (operator rule) — and it is LOGGED against *subject*, the thing the caller
    is then unable to date, because every reading that follows from "no time"
    deserves to be explainable.
    """
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        _logger.warning("%s %r is unreadable; it dates nothing", subject, raw)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# Statuses for which the driver-recorded reason IS a blocked reason. A reason
# recorded against a node that is merely waiting its turn is not a block, and
# rendering it as one would invent a cause the driver never claimed.
#
# FAILED and CANCELLED belong here: the driver records their reason precisely
# to surface a dead node's blast radius to an operator (tick.py's
# _terminal_ds_stuck, "terminal-but-blocking"). HELD does not: its reasons are
# waits the driver expects to clear itself ("merged_unclosed — the merge pass
# closes the issue on this tick"), the same class as `in_review` or `todo`.
_BLOCKED_STATUSES = frozenset({"blocked", "failed", "cancelled"})


def _blocked_reason(status: str, stuck: Any) -> str | None:
    """What the driver recorded about a blocked node, or None — never a
    manufactured cause. A blocked node with nothing recorded stays None so a
    reader renders the status word alone."""
    if status not in _BLOCKED_STATUSES:
        return None
    reason = str(stuck) if stuck else ""
    return reason or None


def _recovery_command(status: str, stuck: Any, epic_ref: str, node_id: str) -> str:
    """A failed node's recovery command, or ``""`` for every other status."""
    if status != "failed":
        return ""
    reason = stuck if isinstance(stuck, str) else None
    return failed_node_command(reason, epic=epic_ref, item=node_id)


def _node_dto(
    n: dict[str, Any],
    attention: AttentionJoin,
    ledger_prs: dict[str, str] | None = None,
    epic_ref: str = "",
) -> EpicNodeDTO:
    """Defensive mapping from a raw snapshot node dict to EpicNodeDTO.

    Unknown keys are dropped (we only ever read known fields); missing
    fields fall back to EpicNodeDTO's own defaults. Never raises. A node the
    journal recorded without a PR takes the ledger's (see ``_ledger_pr_urls``).
    A failed node carries its recovery command from its own stuck reason
    (:func:`~kew.waits.failed_node_command`), never from its attention (#2960).
    """
    node_id = str(n.get("id", ""))
    status = str(n.get("status") or "")
    return EpicNodeDTO(
        id=node_id,
        title=str(n.get("title") or ""),
        status=status,
        status_since=n.get("status_since"),
        url=n.get("url"),
        pr_url=n.get("pr_url") or (ledger_prs or {}).get(node_id),
        external=bool(n.get("external", False)),
        stuck=n.get("stuck"),
        attention=attention.by_node.get(node_id),
        blocked_reason=_blocked_reason(status, n.get("stuck")),
        recovery_command=_recovery_command(status, n.get("stuck"), epic_ref, node_id),
        # `motion` and `alive` are not passed: `MotionDTO` derives both from
        # `status`, and a node — unlike a run or a loop — needs no second fact
        # to say whether it is moving (#2305).
    )


def _coerce_edges(raw_edges: Any) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for e in raw_edges or []:
        if isinstance(e, list | tuple) and len(e) == 2:
            edges.append((str(e[0]), str(e[1])))
    return edges


def _item_runs(store: _Store, node_id: str) -> list[Any]:
    """The node's runs, or none when they cannot be read: a link is not worth
    failing the epic read (see ``_ledger_pr_urls``). The failure is logged, so
    an unreadable ledger never passes as a node with no PR."""
    try:
        return list(store.list_by_item(node_id))
    except Exception as exc:  # noqa: BLE001 — fail soft per node, logged
        _logger.warning(
            "epic node %s: runs unreadable, PR link left empty: %s", node_id, exc
        )
        return []


def _ledger_pr_urls(store: _Store, snapshot: dict[str, Any] | None) -> dict[str, str]:
    """The newest PR the ledger recorded for each node the journal left without one.

    The Linear adapter reads no PR for a CLOSED issue (its facts carry every
    PR field as None), so a done Linear node is journaled with ``pr_url``
    None and the console said "no PR" on all 39 nodes of a finished Linear
    epic, every one of which merged a PR (reported 2026-10-03). The runs
    still carry the URL. Only nodes missing one are read, so a GitHub epic,
    whose journal keeps its PRs, costs no reads. A node whose runs cannot be
    read keeps the journal's None: a link is not worth failing the epic read.
    Callers read it ONCE per request, from the newest snapshot they serve, and
    hand it to every tick they build from it.
    """
    found: dict[str, str] = {}
    for n in _node_dicts(snapshot):
        node_id = str(n.get("id", ""))
        if not node_id or n.get("pr_url") or n.get("external"):
            continue
        runs = [r for r in _item_runs(store, node_id) if getattr(r, "pr_url", None)]
        if runs:
            newest = max(runs, key=lambda r: str(getattr(r, "started_at", "") or ""))
            found[node_id] = str(newest.pr_url)
    return found


def _snapshot_json(tick: Any, override: dict[str, Any] | None) -> dict[str, Any]:
    """The snapshot JSON a DTO is built from: the caller's reading, or the tick's."""
    if override is not None:
        return override
    journaled: dict[str, Any] | None = tick.snapshot
    return journaled or {}


def _snapshot_dto(
    tick: Any,
    attention: AttentionJoin | None = None,
    ledger_prs: dict[str, str] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> EpicSnapshotDTO:
    """One journaled snapshot as a DTO.

    ``attention`` is the live needs-you join, composed once per request by the
    caller and applied to the snapshot it was composed against. Without it the
    snapshot names ``attention`` unavailable rather than reporting an epic
    where nobody is waiting — a historical tick is a record of the DAG, not a
    reading of who needs a person now.

    ``snapshot`` overrides the tick's own JSON with the caller's reconciled
    reading of it (:func:`_reconciled`). The surfaces that answer "what is
    this epic's state NOW" pass it; ``/ticks``, which serves history, does not
    — a journaled tick is the record of what that tick observed.
    """
    join = (
        attention
        if attention is not None
        else AttentionJoin({}, [UNREADABLE_PROJECTION])
    )
    snap: dict[str, Any] = _snapshot_json(tick, snapshot)
    raw_nodes = snap.get("nodes") or []
    epic_ref = str(snap.get("epic_ref") or tick.epic_ref)
    nodes = [
        _node_dto(n, join, ledger_prs, epic_ref)
        for n in raw_nodes
        if isinstance(n, dict)
    ]
    edges = _coerce_edges(snap.get("edges"))
    return EpicSnapshotDTO(
        epic_ref=epic_ref,
        tick_id=int(tick.id),
        tick_ts=str(tick.tick_ts),
        signal=str(snap.get("signal") or ""),
        # counts pass through untransformed from the snapshot JSON — they
        # include external boundary nodes at their graph status (Phase A
        # review note). Callers needing member-only counts should filter
        # snapshot.nodes on external=False themselves.
        counts=dict(snap.get("counts") or {}),
        frontier=list(snap.get("frontier") or []),
        nodes=nodes,
        edges=edges,
        unavailable=list(join.unavailable),
    )


def _node_dicts(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The snapshot's node dicts — what the needs-you join reads a node's own
    wait (awaiting a merge, a continuation, a publication) off of."""
    if not snapshot:
        return []
    return [n for n in (snapshot.get("nodes") or []) if isinstance(n, dict)]


def _node_ids(snapshot: dict[str, Any] | None) -> list[str]:
    return [str(n.get("id", "")) for n in _node_dicts(snapshot)]


def _signal(snapshot: dict[str, Any] | None) -> Any:
    """The driver signal this snapshot recorded — the retirement gate's half
    that costs no read (see ``compose_node_attention``'s ``signal``)."""
    return snapshot.get("signal") if snapshot else None


def _candidate_nodes(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """This epic's OWN nodes a tick left in a non-terminal state.

    Nothing else can be repaired, and nothing else needs to be: a ``done`` or
    ``cancelled`` node is already settled, and a boundary copy of another
    epic's node is that epic's reading to make.

    A mid-flight epic DOES yield candidates — every in-review, held and
    remediating node it has — so this set is a narrowing, not a short circuit:
    the evidence read is free only for an epic with nothing live at all. What
    keeps it cheap on a 1s poll is that it is scoped to these ids
    (:func:`_attested_merges`), so the cost tracks the epic's own size rather
    than the ledger's age (#2392 review).
    """
    return [
        n
        for n in _node_dicts(snapshot)
        if str(n.get("id", ""))
        and not n.get("external")
        and str(n.get("status") or "") not in TERMINAL_NODE_STATES
    ]


@dataclass(frozen=True)
class _AttestedMerge:
    """What the LEDGER says about ONE item a tick left unsettled: the pull
    requests kew's own ``epic_merge`` attestations for it name, and when the
    newest of them was written.

    That event is the one thing in the ledger carrying BOTH halves of
    :func:`~kew.epic_node_states.is_closed_with_merged_pr`: the merge pass emits
    it only after the pull request merged AND it read the tracker item CLOSED
    (``merge._verify_closed``, whose failure arm journals ``merged-not-closed``
    and attests nothing). So it is the closest thing serve has to the tracker
    read ``kew epic status`` makes for itself.

    *prs* is every pull request the item's attestations name, because each one
    is a standing statement that that pull request merged behind a closed item.
    An EMPTY set is an attestation whose payload recorded no PR number, which
    is still evidence that some pull request for the item merged and the item
    closed behind it.

    *attested_at* is the NEWEST of those attestations' timestamps, which is
    what makes the evidence datable against the journal it would repair
    (:func:`_moved_after`). None when none of them carried a readable one.
    """

    prs: frozenset[str]
    attested_at: datetime | None


#: Per item, the merge evidence :func:`_landed_from` weighs against the journal.
_LandedEvidence = dict[str, _AttestedMerge]

#: A pull request's number is the last path segment of its URL. Matched
#: loosely on purpose — ``/pull/`` is GitHub's spelling and the only one kew
#: merges through, but the comparison this feeds only ever REFUSES a landing,
#: so a URL shape it cannot read must leave the attestation standing rather
#: than silently start rejecting every one of them.
_PR_NUMBER_IN_URL = re.compile(r"/(\d+)/?$")


def _pr_number(pr_url: str) -> str:
    """*pr_url*'s pull request number, or ``""`` when it carries none."""
    found = _PR_NUMBER_IN_URL.search(pr_url.strip())
    return found.group(1) if found else ""


def _event_node_id(ev: Any) -> str | None:
    """The node an audit event is addressed to — durable id, legacy number."""
    node_id = getattr(ev, "item_id", None)
    if node_id is not None:
        return str(node_id)
    issue_num = getattr(ev, "issue_num", None)
    return str(issue_num) if issue_num is not None else None


def _attested_merges(store: _Store, item_ids: set[str]) -> _LandedEvidence:
    """Which merge each of *item_ids* has an attestation for, and when.

    Scoped to the nodes asked about, not the whole ledger: this read is on the
    1s live-view poll of every mid-flight epic, and an epic's unsettled nodes
    are a handful however many merges the host has ever performed (#2392
    review). ``idx_audit_events_item_event`` serves it.

    EVERY attestation an item has contributes its PR, not just the newest: each
    one is a standing statement that that pull request merged, and which of
    them the journal is pointing at is :func:`_landed_from`'s question.

    An unreadable audit ledger names no merges rather than failing the epic
    read: the journal's own statuses then stand, which is the reading serve
    already served. The failure is logged, so silence is never mistaken for
    "nothing merged".
    """
    if not item_ids:
        return {}
    try:
        events = store.list_audit_events(
            limit=None, event="epic_merge", item_ids=sorted(item_ids)
        )
    except Exception as exc:  # noqa: BLE001 — fail soft, logged
        _logger.warning("epic merge attestations unreadable: %s", exc)
        return {}
    by_node: dict[str, list[Any]] = {}
    for ev in events:
        node_id = _event_node_id(ev)
        if node_id is not None:
            by_node.setdefault(node_id, []).append(ev)
    return {node_id: _merge_evidence(evs) for node_id, evs in by_node.items()}


def _merge_evidence(events: list[Any]) -> _AttestedMerge:
    """One item's ``epic_merge`` attestations, read as its merge evidence.

    An attestation that named no pull request contributes none, and one whose
    timestamp will not parse dates nothing — both leave the rest of the
    evidence exactly as it was, because neither is a statement that some OTHER
    pull request merged or that the merge happened later than recorded.
    """
    prs = {
        str(pr)
        for ev in events
        if (pr := (getattr(ev, "payload", None) or {}).get("pr")) is not None
    }
    stamps = (
        _utc_stamp(getattr(ev, "timestamp", None), subject="epic_merge event")
        for ev in events
    )
    readable = [at for at in stamps if at is not None]
    return _AttestedMerge(frozenset(prs), max(readable, default=None))


def _moved_after(node: dict[str, Any], attested_at: datetime | None) -> bool:
    """Did the journal record this node entering its CURRENT status AFTER the
    merge the ledger attests?

    ``status_since`` is when the node first read the status the snapshot
    carries — ``journal._node_entry`` carries it forward across every later
    tick that reads the same one — so a value later than the attestation is the
    driver saying it put this node back to work after that merge. The journal
    is then the LATER of the two reads, and the attestation is about finished
    history, not about the state it would repair.

    That recency test is what answers for the node a reopen left with no pull
    request at all: nothing in the snapshot then names a second pull request
    for :func:`_attests_node`'s comparison to refuse on, and a node reopened
    before its next PR exists was being served ``done`` on the old attestation
    while ``kew epic status`` read it live as ``todo`` (#2392 review).

    Only a datable pair answers: no attestation timestamp, an unreadable one,
    or a snapshot carrying no ``status_since`` leaves the attestation standing,
    because none of them dates the journal after the merge.
    """
    if attested_at is None:
        return False
    since = _utc_stamp(node.get("status_since"), subject="node status_since")
    return since is not None and since > attested_at


def _attests_node(attested: _LandedEvidence, node: dict[str, Any]) -> bool:
    """Does an ``epic_merge`` attestation answer for THIS node's state?

    An attestation is evidence about one pull request, merged at one time. A
    node the journal left pointing at a different pull request, or recorded
    moving back into live work after that merge, was reopened for more work;
    crediting it with the old attestation would retire live work, which is the
    whole hazard :func:`_landed_from` guards (#2392 review).

    Only a PROVEN mismatch refuses: a node the journal recorded without a PR
    (every done Linear node), an attestation whose payload carried no PR
    number, a URL whose number cannot be read, and an unreadable stamp on
    either side of the recency test all leave the attestation standing, because
    none of them names a second pull request or dates the journal after the
    merge.
    """
    evidence = attested.get(str(node.get("id", "")))
    if evidence is None:
        return False
    if _moved_after(node, evidence.attested_at):
        return False
    number = _pr_number(str(node.get("pr_url") or ""))
    return not evidence.prs or not number or number in evidence.prs


def _read_landed_evidence(
    store: _Store, node_groups: list[list[dict[str, Any]]]
) -> _LandedEvidence:
    """The ledger evidence for every unsettled node of every given snapshot.

    Batched across the whole group so the Epics roster pays ONE read for the
    list rather than one per row (the C3/#2296 budget).
    """
    candidates = [n for group in node_groups for n in group]
    if not candidates:
        return {}
    return _attested_merges(store, {str(n.get("id", "")) for n in candidates})


def _landed_from(
    nodes: list[dict[str, Any]], attested: _LandedEvidence
) -> frozenset[str]:
    """Which of *nodes* the ledger ATTESTS closed behind a merged pull request.

    kew's own ``epic_merge`` attestation is the only evidence that lands a
    node, because it is the only evidence serve holds for both halves of the
    rule (:data:`_LandedEvidence`). A merged run row is NOT a second arm: it
    proves that a pull request merged and says nothing whatever about the
    tracker item, and kew's own merge pass is the proof that the two do not
    follow from each other — it closes the item itself and re-reads the tracker
    (``merge._verify_closed``) precisely because a merge does not reliably close
    an item (a missing closing keyword on GitHub; Linear, where no keyword
    closes anything). Promoting on a merged row alone therefore read ``done``
    over live work: the node's own next round was still open, the epic was
    served ``complete``, its attention join went empty behind the retirement
    gate — and the needs-you set, which reads the item's work facts rather than
    this journal, went on filing the very merge decision the promotion had just
    declared finished (#2392 review).

    An attestation lands only the node it names a pull request FOR, and only
    one the journal has not already recorded moving on from: a node reopened
    for more work after an earlier merge carries a different, still-open PR —
    or, before that PR exists, no PR and a ``status_since`` later than the
    merge — and crediting it with the old attestation would retire live work
    (:func:`_attests_node`, :func:`_moved_after`). A node the journal recorded
    without a PR and without moving since — every done Linear node, whose
    adapter reads no PR for a closed issue (see ``_ledger_pr_urls``) — names no
    second pull request, so its attestation stands.

    A node the driver journaled as "PR merged, tracker item still OPEN" is not
    landed either
    (:func:`~kew.epic_node_states.journaled_merged_unclosed`, which answers for
    both spellings of that one state — the tick's facts-computed wait and the
    merge pass's ``merged-not-closed``): the driver read the tracker, found the
    item still open behind that very merge, and held. That is a LIVE tracker
    read against the merge, so it outranks anything the ledger holds, and
    reading it as a landing would report ``done``/``complete`` with the stuck
    reason erased for a node the CLI and the overlay both still hold
    (#2392 review).
    """
    return frozenset(
        str(n.get("id", ""))
        for n in nodes
        if not journaled_merged_unclosed(n.get("stuck")) and _attests_node(attested, n)
    )


def _landed_ids(store: _Store, snapshot: dict[str, Any] | None) -> frozenset[str]:
    """One snapshot's landed nodes — the single-epic form of the roster read."""
    candidates = _candidate_nodes(snapshot)
    if not candidates:
        return frozenset()
    return _landed_from(candidates, _read_landed_evidence(store, [candidates]))


def _settled_node(node: dict[str, Any]) -> dict[str, Any]:
    """One journaled node, read as ``done``.

    ``status_since`` and ``stuck`` are the journal's record of the state being
    replaced — when the node became *held*, and why. Carried forward they
    would date and explain a ``done`` the tick never wrote, so they are
    dropped: an absent reading is the honest one here.
    """
    return {**node, "status": "done", "status_since": None, "stuck": None}


def _settled_frontier(snapshot: dict[str, Any], promoted: frozenset[str]) -> list[Any]:
    """The journal's ready frontier without the nodes this repair just settled.

    The frontier is "what the driver could dispatch NOW": the epic page rings
    each of its nodes and counts them as *next up*. A node the ledger has
    settled is not dispatchable, and left on the list it was served
    ``status=done`` and ringed as ready on one page, inside a snapshot whose
    signal said the epic was ``complete`` — every number re-derived except this
    one (#2392 review).

    Entries are passed through as the journal wrote them, matched on their
    string form: a frontier id is the node id, and a reader that rewrote them
    would change what the DAG matches against (``DagView``).
    """
    return [
        node_id
        for node_id in (snapshot.get("frontier") or [])
        if str(node_id) not in promoted
    ]


def _journaled_states(nodes: list[dict[str, Any]]) -> dict[str, str]:
    """``node id -> the status this tick journaled for it``."""
    return {str(n.get("id", "")): str(n.get("status") or "") for n in nodes}


def _promoted_ids(
    states: Mapping[str, str], settled: Mapping[str, str]
) -> frozenset[str]:
    """Which nodes the repair actually MOVED.

    Only a node whose state changed is rewritten, and only such a node leaves
    the frontier: a node the tick already journaled ``done`` keeps its own
    ``status_since``, its own reason, and whatever the driver said about it.
    """
    return frozenset(
        node_id for node_id, status in states.items() if settled[node_id] != status
    )


def _settled_nodes(
    nodes: list[dict[str, Any]], promoted: frozenset[str]
) -> list[dict[str, Any]]:
    """*nodes* with every promoted one read as ``done``."""
    return [_settled_node(n) if str(n.get("id", "")) in promoted else n for n in nodes]


def _reconciled(
    snapshot: dict[str, Any] | None,
    landed: frozenset[str],
    histories: Mapping[str, ContinuationHistory] | None = None,
) -> dict[str, Any] | None:
    """*snapshot* with every landed node read as ``done``, and every reading
    derived from the node states — counts, signal, frontier — re-derived from
    the result (#2392).

    *histories* (:func:`_preserved_histories`) also rereads each journaled
    repeated-host-death reason against the epic's current continuation
    history, so a journal written before the preserved reason existed stops
    offering the retry that would dispatch past the preserved work (#2960).

    The rule and the derivations are :mod:`kew.epic_node_states`' — the same
    module the tick's overlay reads — so this repairs an already-stale journal
    with the sentence the driver now writes, rather than a second opinion.
    Returns the snapshot unchanged when nothing landed, so an epic whose
    journal is current serves byte-identical counts.
    """
    snapshot = _with_preserved_work(snapshot, histories)
    if not snapshot or not landed:
        return snapshot
    nodes = _node_dicts(snapshot)
    states = _journaled_states(nodes)
    settled = settle_landed(states, landed)
    promoted = _promoted_ids(states, settled)
    external = [str(n.get("id", "")) for n in nodes if n.get("external")]
    return {
        **snapshot,
        "nodes": _settled_nodes(nodes, promoted),
        "counts": node_counts(settled),
        "signal": epic_signal(settled, _signal(snapshot), external=external),
        "frontier": _settled_frontier(snapshot, promoted),
    }


def _preserved_histories(
    store: Any, epic_ref: str, *snapshots: dict[str, Any] | None
) -> dict[str, ContinuationHistory] | None:
    """The epic's continuation histories, read only when one of *snapshots*
    journals a repeated host death — the reasons they can change (#2960):
    every variant, so a preserved one stops offering continue once a
    continuation owns the work or abandon re-admitted it, without a tick.

    None when nothing is to be reread: every other poll pays nothing, and a
    store with no continuation table (a read-only fake) has no history to
    read. An empty map is a reading — every preserved row was abandoned.
    """
    journaled = any(
        isinstance(n.get("stuck"), str)
        and n.get("stuck") in HOST_RESTART_REPEATED_REASONS
        for snapshot in snapshots
        for n in _node_dicts(snapshot)
    )
    if not journaled or not callable(getattr(store, "list_continuations", None)):
        return None
    return continuation_histories(store, epic_ref)


def _with_preserved_work(
    snapshot: dict[str, Any] | None,
    histories: Mapping[str, ContinuationHistory] | None,
) -> dict[str, Any] | None:
    """*snapshot* with each node's stuck reason read through
    :func:`~kew.continuation_recovery.preserved_host_death_reason` — the rule
    the driver writes the reason with today. Unchanged when nothing moved, or
    when no history was read (None); an empty map rereads every node."""
    if not snapshot or histories is None:
        return snapshot
    raw = snapshot.get("nodes") or []
    nodes = [_preserved_node(n, histories) if isinstance(n, dict) else n for n in raw]
    if all(new is old for new, old in zip(nodes, raw, strict=True)):
        return snapshot
    return {**snapshot, "nodes": nodes}


def _preserved_node(
    node: dict[str, Any], histories: Mapping[str, ContinuationHistory]
) -> dict[str, Any]:
    stuck = node.get("stuck")
    if not isinstance(stuck, str):
        return node
    reason = preserved_host_death_reason(stuck, histories.get(str(node.get("id", ""))))
    return node if reason == stuck else {**node, "stuck": reason}


def _run_in_window(rec: Any, window_start: str, upper: str | None) -> bool:
    finished = getattr(rec, "finished_at", None)
    if finished is None:
        return False
    finished_iso = _iso(finished)
    if finished_iso is None or finished_iso < window_start:
        return False
    return not (upper is not None and finished_iso > upper)


def _item_cost_in_window(
    store: _Store, item_id: str, window_start: str, upper: str | None
) -> float:
    return sum(
        float(getattr(rec, "estimated_cost_usd", 0.0) or 0.0)
        for rec in store.list_by_item(item_id)
        if _run_in_window(rec, window_start, upper)
    )


def _drain_costs(
    store: _Store,
    node_ids: list[str],
    window_start: str | None,
    *,
    upper: str | None = None,
) -> dict[str, float]:
    """Per node id (the durable ``item_id``), sum estimated_cost_usd of runs
    finished within [window_start, upper] (upper inclusive, unbounded when
    None). ISO-8601 UTC string comparison is intentional and sufficient (see
    #454 Phase A review note on RunRecord.finished_at datetime -> isoformat
    compares).

    Keyed on the item id, NOT an int-coerced issue number: a non-numeric
    tracker's node (``SFD-12``) would silently drop out of an int() coercion,
    zeroing its drain badge. A GitHub node's item id is its stringified issue
    number, so numeric ids key byte-identically.

    This recomputes on every call (no cross-poll cache), by design: the
    live-view telemetry layer's poll-cadence contract requires cost badges
    to reflect current spend on in_progress nodes on every ~1s poll (#454
    Phase 3 spec), so a staleness cache here would silently break that
    binding behavior. Instead of caching, the *cost* of recomputing is cut
    by preferring a single aggregate query (RunStore.sum_costs_by_item,
    #458 review) over one store.list_by_item scan per DAG node; the
    per-node loop below is kept as a fallback for stores/fakes that don't
    implement the bulk method.
    """
    if window_start is None:
        return {}
    item_ids = [nid for nid in node_ids if nid]
    if not item_ids:
        return {}
    bulk_sum = getattr(store, "sum_costs_by_item", None)
    if callable(bulk_sum):
        totals = bulk_sum(item_ids, since=window_start, until=upper)
        return {iid: float(totals.get(iid, 0.0)) for iid in item_ids}
    return {
        iid: _item_cost_in_window(store, iid, window_start, upper) for iid in item_ids
    }


def _economics(
    store: _Store,
    epic_ref: str,
    snapshot: dict[str, Any] | None,
    *,
    upper: str | None = None,
) -> EpicEconomicsDTO:
    window_start = store.first_epic_tick_ts(epic_ref)
    node_drain = _drain_costs(store, _node_ids(snapshot), window_start, upper=upper)
    return EpicEconomicsDTO(
        window_start=window_start,
        drain_total_usd=sum(node_drain.values()),
        node_drain_usd=node_drain,
    )


def _heartbeat_dto(
    store: _Store, epic_ref: str, *, now: datetime | None = None
) -> EpicHeartbeatDTO | None:
    state = store.get_epic_driver_state(epic_ref)
    stamp = _tick_attempt_stamp(store, epic_ref)
    if state is None:
        return _attempt_only_heartbeat(stamp)
    return _heartbeat_from(
        state,
        epic_ref,
        store.count_epic_ticks(epic_ref),
        tick_attempt_stamp=stamp,
        now=now,
        drive=read_drive_heartbeat(store),
    )


def _attempt_only_heartbeat(tick_attempt_stamp: str) -> EpicHeartbeatDTO | None:
    """The heartbeat of an epic that has ATTEMPTED ticks and recorded none.

    ``epic_driver_state`` is only written past the single-driver lock, the tool
    preflight and the non-empty-graph guard, while the attempt log records the
    attempt itself — so an epic every one of whose ticks was refused there has
    ``epic_tick_log`` rows and no driver row at all, which ``list_epic_refs``
    now makes a reachable epic. Answering ``None`` for it left the live stream
    with nothing to carry the stamp in, so a subscriber's tick strip — redrawn
    on ``tick_attempt_stamp`` — was read once at mount and then froze through
    exactly the refused attempts the strip exists to show (#2304 review round
    4).

    Every field but the stamp is the absence it is: no tick has finished, so
    there is no instant and no cadence to threshold, and ``driver_status``
    stays ``unknown`` — the reading the console already words "no heartbeat
    yet", which is what it drew while this answered ``None``.
    """
    if not tick_attempt_stamp:
        return None
    return EpicHeartbeatDTO(
        last_tick_started_at=None,
        last_tick_ts=None,
        last_tick_duration_s=None,
        tick_count=0,
        interval_s=None,
        last_signal=None,
        change_count=0,
        tick_attempt_stamp=tick_attempt_stamp,
    )


def _tick_attempt_stamp(store: _Store, epic_ref: str) -> str:
    """This epic's tick-attempt stamp, or ``""`` if the store cannot say.

    Duck-typed for the same reason the roster's batched reads are: a store
    predating the ``epic_tick_log`` table (or a test double standing in for
    one) answers ``""``, and a reader that stamps on ``""`` forever simply
    falls back to reading the strip once — never to a crash on a route that
    has nothing to do with the strip.
    """
    reader = getattr(store, "epic_tick_log_stamp", None)
    return str(reader(epic_ref)) if callable(reader) else ""


def _heartbeat_from(
    state: Any,
    epic_ref: str,
    change_count: int,
    *,
    tick_attempt_stamp: str = "",
    now: datetime | None = None,
    drive: DriveHeartbeat | None = None,
) -> EpicHeartbeatDTO:
    """One heartbeat DTO, composed from an already-read driver row.

    The point read (``/api/epics/<ref>``) and the roster read (the Epics list)
    both compose their row here, off the SAME reading — composing them
    separately is what served the list a raw ``interval_s`` the reading had
    already thresholded past, so one surface drew "no heartbeat" over a driver
    the other called live (#2297 review).
    """
    # The reading travels with the heartbeat it was derived from, so a reader
    # never has to re-threshold the age itself (and disagree with the rest of
    # the console about whether this driver is alive).
    liveness = liveness_from_state(state, epic_ref, now=now, drive=drive)
    return EpicHeartbeatDTO(
        last_tick_started_at=state.last_tick_started_at,
        last_tick_ts=state.last_tick_ts,
        last_tick_duration_s=state.last_tick_duration_s,
        tick_count=state.tick_count,
        # The cadence the READING used, which for an externally-driven epic is
        # the configured expectation — ``cadence_source`` says which it was. A
        # client thresholds the same number the server did, instead of reading
        # NULL off a poller-less drive and withdrawing every live claim.
        interval_s=liveness.interval_s,
        last_signal=state.last_signal,
        change_count=change_count,
        tracker=getattr(state, "tracker", None),
        driver_status=liveness.status,
        cadence_source=liveness.cadence_source,
        tick_attempt_stamp=tick_attempt_stamp,
    )


#: The three readings ``driver_liveness`` can return. ``unknown`` is NOT
#: "not gone": it is the honest answer when no cadence and no lock evidence
#: exist to threshold against, so a health page can say "unknown" instead of
#: painting a dead process green (#2297).
DRIVER_LIVE = "live"
DRIVER_GONE = "gone"
DRIVER_UNKNOWN = "unknown"
#: The heartbeat has lapsed but the lock's holder is provably alive: a driver
#: streaming one long foreground stage cannot tick until it returns (#2534).
#: Work in progress, so never ``gone`` and never a driver to restart.
DRIVER_BUSY = "busy"


@dataclass(frozen=True)
class DriverLockFacts:
    """The driver lock file's own evidence: which process holds the epic's lock,
    whether that pid is still alive, and the heartbeat it last refreshed.

    Built by a caller that can read the lock (``kew.graph.driver.lock``), which
    this module must never import — the service layer sits the far side of the
    import-linter boundary, so the facts are passed in as plain data.
    """

    pid: int | None = None
    pid_alive: bool | None = None
    heartbeat_at: str | None = None
    #: When the lock was READ. None means "just now" (a caller holding the file
    #: open, e.g. ``kew epic status``). A stored snapshot carries the instant the
    #: serve sweep looked, because ``pid_alive`` is only conclusive while that
    #: observation is current: believed forever it would keep a dead driver green
    #: — or call a restarted one gone (#2297 review).
    observed_at: str | None = None

    @classmethod
    def from_holder(cls, holder: Any | None) -> DriverLockFacts | None:
        """Adapt a ``LockHolder``-shaped record (duck-typed), or None."""
        if holder is None:
            return None
        heartbeat = getattr(holder, "heartbeat", None)
        alive = getattr(holder, "is_alive", None)
        return cls(
            pid=getattr(holder, "pid", None),
            pid_alive=bool(alive()) if callable(alive) else None,
            heartbeat_at=heartbeat.isoformat() if heartbeat is not None else None,
        )

    @classmethod
    def from_state(cls, state: Any | None) -> DriverLockFacts | None:
        """The lock snapshot the serve sweep stored on this epic's driver row.

        This is how a serve-side reading gets the lock's evidence at all: the
        import-linter boundary forbids ``kew.service`` from reading the lock file
        itself, so the sweep (CLI-side) copies what it saw into the ledger and
        every reading here picks it up (#2297 review). None when no sweep has
        looked, or when it looked and found the lock free — between ticks a free
        lock is normal and proves nothing either way.
        """
        observed_at = getattr(state, "lock_observed_at", None)
        if state is None or observed_at is None:
            return None
        return cls(
            pid=getattr(state, "lock_pid", None),
            pid_alive=getattr(state, "lock_pid_alive", None),
            heartbeat_at=getattr(state, "lock_heartbeat_at", None),
            observed_at=observed_at,
        )


@dataclass(frozen=True)
class DriverLiveness:
    """Per-epic driver liveness from the heartbeat plus the lock file.

    ``status`` is the reading: ``gone`` only where it can be PROVEN — a
    heartbeat that has lapsed against its cadence, or a lock file whose
    holding pid is no longer a live process — ``live`` where a cadence
    exists and the heartbeat is inside it, and ``unknown`` where neither a
    cadence nor lock evidence can settle it. ``cadence_source`` says which
    cadence was used: ``recorded`` (the driver's own ``interval_s``),
    ``expected`` (a configured expectation — the caller's live config, else the
    one a drive last recorded in ``expected_interval_s``) or ``none``.

    ``driver_gone`` stays the boolean the existing alarm paths read, and is
    exactly ``status == "gone"`` — an unknown reading never raises an alarm.
    """

    epic_ref: str
    present: bool
    last_heartbeat_at: str | None
    age_s: float | None
    interval_s: float | None
    driver_gone: bool
    status: str = DRIVER_UNKNOWN
    cadence_source: str = "none"
    pid: int | None = None
    pid_alive: bool | None = None
    tracker: str | None = None
    #: The project drive's own reading (``live``/``gone``) when this epic is in
    #: its rotation and the reading above is the drive's (#2617); else None.
    drive: str | None = None


def _read_stamp(raw: str | None) -> datetime | None:
    """Parse one driver heartbeat stamp, or None when it cannot be read.

    An absent heartbeat is not a failure; an unreadable one flows into an
    ``unknown`` liveness reading, which is why ``_utc_stamp`` logs it.
    """
    return None if raw is None else _utc_stamp(raw, subject="driver heartbeat")


def _freshest_heartbeat(
    *candidates: str | None,
) -> tuple[str | None, datetime | None]:
    """The NEWEST of the heartbeats this driver writes, with its parsed value.

    Not the first present one: the tick stamps and the lock file's own heartbeat
    are independent writers of the same fact, and the lock is the one that keeps
    beating mid-tick (``_lock_keepalive``). Picking the first would read a
    provably-live driver halfway through a long tick as ``gone`` — the exact
    case that keepalive exists for (#2297).

    Falls back to the first present-but-unreadable stamp so the reading still
    REPORTS a heartbeat it cannot threshold (age None -> ``unknown``).
    """
    present = [raw for raw in candidates if raw is not None]
    readable = [(dt, raw) for raw in present if (dt := _read_stamp(raw)) is not None]
    if not readable:
        return (present[0] if present else None), None
    newest_dt, newest_raw = max(readable, key=lambda pair: pair[0])
    return newest_raw, newest_dt


def _age_s(beat_at: datetime | None, now: datetime) -> float | None:
    """Seconds since the heartbeat, or None without one. A naive ``now`` is read
    as UTC so a caller passing one still gets a reading (the pre-#2297 code
    swallowed the resulting TypeError and read unknown)."""
    if beat_at is None:
        return None
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return (reference - beat_at).total_seconds()


def _cadence(
    interval_s: float | None, *expectations: float | None
) -> tuple[float | None, str]:
    """The cadence to threshold against, and where it came from.

    The driver's own recorded ``interval_s`` wins; otherwise the first usable
    expectation in order (the caller's live config, then the one last recorded
    by a tick) — reported as ``expected``, never as ``recorded``, because an
    expectation is a guess (#2297).

    Non-positive intervals are unthresholdable (age > 2*0 would flag every live
    driver as gone), so they fall through to the expectations, then to none.
    """
    if interval_s is not None and interval_s > 0:
        return interval_s, "recorded"
    for expected in expectations:
        if expected is not None and expected > 0:
            return expected, "expected"
    return None, "none"


#: How old a STORED lock observation may be and still settle the pid question.
#: The serve sweep refreshes it once a cycle (``kew.serve.sweep``'s 60s default),
#: so three cycles tolerate a slow or skipped sweep, while a snapshot left behind
#: by a serve that has stopped expires instead of being believed forever.
_LOCK_OBSERVATION_FRESH_S = 180.0


def _conclusive_pid_alive(
    lock: DriverLockFacts, beat_at: datetime | None, now: datetime
) -> bool | None:
    """``lock.pid_alive``, or None where that observation cannot settle anything.

    A dead holder pid is the one conclusive gone verdict (#970), so it is only
    honored while it is still an observation OF THIS DRIVER, NOW:

    * a live read (``observed_at is None``) always counts — the caller had the
      lock file open;
    * a stored snapshot older than ``_LOCK_OBSERVATION_FRESH_S`` is expired: the
      sweep that took it is no longer running, and a dead pid it saw says nothing
      about the driver that may have started since;
    * a snapshot taken BEFORE the freshest heartbeat is stale by evidence —
      something beat after we looked, so the pid we found dead has been replaced.

    An unreadable ``observed_at`` is treated as the expired case, never as a live
    read: an unparseable stamp must not win an argument against a heartbeat.
    """
    if lock.pid_alive is None:
        return None
    if lock.observed_at is None:
        return lock.pid_alive
    observed = _read_stamp(lock.observed_at)
    if observed is None:
        return None
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if (reference - observed).total_seconds() > _LOCK_OBSERVATION_FRESH_S:
        return None
    if beat_at is not None and observed < beat_at:
        return None
    return lock.pid_alive


def _driver_status(
    age_s: float | None, interval_s: float | None, *, pid_alive: bool | None
) -> str:
    """The reading: a dead holder pid is conclusive (that is the #970 shape, a
    TaskStop'd driver leaving its lock behind); otherwise the heartbeat age is
    thresholded against the cadence, and `unknown` is the answer when either is
    missing — never "alive". A lapsed heartbeat whose lock holder is
    conclusively alive is `busy` (#2534): the driver is inside one long
    foreground stage and cannot tick until it returns, and calling that `gone`
    sent people to restart a loop that was working. That reading is bounded by
    the lock layer's own stale line (``LOCK_HEARTBEAT_STALE_S``): ``is_alive``
    answers True for a pid it cannot prove dead (a legacy pid 0, another user's
    pid, a recycled one), so past that line a "live" holder is one the lock
    layer would reclaim, and it reads `gone` (#2534 review).

    The threshold is `kew.driver_cadence.cadence_lapsed`, which is also what
    `finish_epic_tick` asks before retiring a recorded cadence: the reader and
    the writer of `interval_s` must not draw that line in different places, or
    a cadence gets dropped while this reading still believes it (#2297 review).
    """
    if pid_alive is False:
        return DRIVER_GONE
    if age_s is None or interval_s is None:
        return DRIVER_UNKNOWN
    if not cadence_lapsed(age_s, interval_s):
        return DRIVER_LIVE
    if pid_alive and age_s <= LOCK_HEARTBEAT_STALE_S:
        return DRIVER_BUSY
    return DRIVER_GONE


#: A loop that is alive and holding off ON PURPOSE — parked in the operator's
#: shared slot gate, say — says so with ``kew epic heartbeat --waiting`` (#2566).
#: Without it a waiting loop and a dead one look the same: neither ticks.
DRIVER_WAITING = "waiting"
#: The meta key prefix one epic's last waiting heartbeat is stored under.
DRIVER_WAITING_META_PREFIX = "epic_driver_waiting:"


@dataclass(frozen=True)
class DriverWaiting:
    """A loop's last "I am waiting on purpose" heartbeat: when, and on what."""

    at: str
    reason: str


def record_driver_waiting(
    store: Any, epic_ref: str, reason: str, *, now: datetime | None = None
) -> DriverWaiting:
    """Store *epic_ref*'s waiting heartbeat. One row per epic, overwritten."""
    beat = DriverWaiting(at=(now or datetime.now(UTC)).isoformat(), reason=reason)
    store.set_meta(
        DRIVER_WAITING_META_PREFIX + epic_ref,
        json.dumps({"at": beat.at, "reason": beat.reason}),
    )
    return beat


def read_driver_waiting(store: Any, epic_ref: str) -> DriverWaiting | None:
    """*epic_ref*'s last waiting heartbeat, or None when there is none to read."""
    try:
        raw = store.get_meta(DRIVER_WAITING_META_PREFIX + epic_ref)
        data = json.loads(raw) if raw else None
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("at"), str):
        return None
    return DriverWaiting(at=data["at"], reason=str(data.get("reason") or ""))


def waiting_is_fresh(
    waiting: DriverWaiting | None, interval_s: float | None, now: datetime
) -> bool:
    """Whether a waiting heartbeat still speaks for the loop: inside TWICE its
    cadence. No cadence, or a stamp that will not parse, is not fresh — a
    reading that cannot be thresholded never turns a silent driver quiet."""
    if waiting is None or interval_s is None:
        return False
    age = _age_s(_read_stamp(waiting.at), now)
    return age is not None and age <= 2 * interval_s


#: ``driver_liveness``'s default: read the drive's heartbeat off the store.
_READ_DRIVE: Any = object()


def driver_liveness(
    store: _Store,
    epic_ref: str,
    *,
    now: datetime | None = None,
    lock: DriverLockFacts | None = None,
    expected_interval_s: float | None = None,
    drive: DriveHeartbeat | None = _READ_DRIVE,
) -> DriverLiveness:
    """Derive whether the epic's driver has gone silent.

    Reads this epic's driver row — and, unless the caller already read it, the
    project drive's heartbeat — and hands both to :func:`liveness_from_state`;
    every caller that already HAS the row (the Epics list reads the whole roster
    in one query) calls that directly, so the two can never disagree.
    """
    return liveness_from_state(
        store.get_epic_driver_state(epic_ref),
        epic_ref,
        now=now,
        lock=lock,
        expected_interval_s=expected_interval_s,
        drive=read_drive_heartbeat(store) if drive is _READ_DRIVE else drive,
    )


def _drive_reading(
    reading: DriverLiveness, drive: DriveHeartbeat | None, now: datetime
) -> DriverLiveness:
    """``reading``, as the project drive's heartbeat settles it (#2617).

    An epic in a running drive's rotation is driven by that drive, which ticks
    its epics in turn and hands each review to a detached stage: between its
    turns an epic has no fresh tick stamp and no lock of its own, and that is
    work in progress, not a dead driver (#2534). So, while the drive's
    heartbeat is inside its stall line, the epic reads ``live`` if its own
    stamps say so and ``busy`` otherwise — never ``gone``. Once the drive's
    heartbeat stalls, every epic in its rotation reads ``gone`` — the drive IS
    their driver — except one whose own tick is still inside its cadence: that
    tick is direct evidence something is driving it. An epic outside the
    rotation (archived, paused, stopped, on another tracker) keeps its own
    reading untouched.
    """
    if drive is None or not drive.drives(reading.epic_ref):
        return reading
    drive_status = drive.status(now)
    if reading.status == DRIVER_LIVE:
        status = DRIVER_LIVE
    else:
        status = DRIVER_GONE if drive_status == DRIVE_GONE else DRIVER_BUSY
    return replace(
        reading,
        present=True,
        last_heartbeat_at=reading.last_heartbeat_at or drive.at.isoformat(),
        age_s=reading.age_s if reading.age_s is not None else drive.age_s(now),
        status=status,
        driver_gone=status == DRIVER_GONE,
        drive=drive_status,
    )


def liveness_from_state(
    state: Any | None,
    epic_ref: str,
    *,
    now: datetime | None = None,
    lock: DriverLockFacts | None = None,
    expected_interval_s: float | None = None,
    drive: DriveHeartbeat | None = None,
) -> DriverLiveness:
    """The reading, from an already-read driver row — no query of its own.

    The heartbeat thresholded is the NEWEST of the three a driver writes:
    ``last_tick_started_at`` (stamped at the start of every tick, before any
    work), ``last_tick_ts``, and the driver lock's own heartbeat — which is the
    only one that keeps beating mid-tick (``_lock_keepalive``), so a long tick
    stays live instead of reading gone. A lock whose holding pid is dead is
    conclusive on its own — that is the #970 shape, a TaskStop'd driver leaving
    its lock behind — so it reads ``gone`` whatever the heartbeat says, as long
    as that observation is still current (:func:`_conclusive_pid_alive`).

    The lock's facts come either from a caller that read the file (``kew epic
    status``) or, failing that, from the snapshot the serve sweep stored on this
    very row: without it every serve-side reading thresholded the tick stamps
    alone and called a provably-alive driver gone (#2297 review). ``now`` is
    injectable for deterministic fixture-timestamp tests.

    ``drive`` is the project drive's heartbeat (``read_drive_heartbeat``),
    which settles the reading of every epic in its rotation
    (:func:`_drive_reading`).
    """
    now = now or datetime.now(UTC)
    return _drive_reading(
        _own_liveness(state, epic_ref, now, lock, expected_interval_s), drive, now
    )


def _own_liveness(
    state: Any | None,
    epic_ref: str,
    now: datetime,
    lock: DriverLockFacts | None,
    expected_interval_s: float | None,
) -> DriverLiveness:
    """The epic's reading from its own driver row and lock alone."""
    lock = lock or DriverLockFacts.from_state(state) or DriverLockFacts()
    if state is None and lock.heartbeat_at is None and lock.pid_alive is None:
        return DriverLiveness(epic_ref, False, None, None, None, False)

    heartbeat, beat_at = _freshest_heartbeat(
        getattr(state, "last_tick_started_at", None),
        getattr(state, "last_tick_ts", None),
        lock.heartbeat_at,
    )
    interval, source = _cadence(
        getattr(state, "interval_s", None),
        expected_interval_s,
        getattr(state, "expected_interval_s", None),
    )
    age_s = _age_s(beat_at, now)
    pid_alive = _conclusive_pid_alive(lock, beat_at, now)
    status = _driver_status(age_s, interval, pid_alive=pid_alive)
    return DriverLiveness(
        epic_ref=epic_ref,
        present=True,
        last_heartbeat_at=heartbeat,
        age_s=age_s,
        interval_s=interval,
        driver_gone=status == DRIVER_GONE,
        status=status,
        cadence_source=source,
        pid=lock.pid,
        pid_alive=pid_alive,
        tracker=getattr(state, "tracker", None),
    )


def _approver_by_issue(store: _Store) -> dict[str, str | None]:
    """Newest-approval-wins map of opaque node id -> approver actor.

    list_audit_events returns newest-first, so the first hit for a given
    node via setdefault is the most recent epic_merge event for it.

    Unscoped, because its callers annotate a whole timeline. The read that is
    on the 1s poll is :func:`_attested_merges`, which asks the same event its
    own, scoped question.
    """
    result: dict[str, str | None] = {}
    for ev in store.list_audit_events(limit=None, event="epic_merge"):
        node_id = _event_node_id(ev)
        if node_id is None:
            continue
        payload = getattr(ev, "payload", None) or {}
        result.setdefault(node_id, payload.get("actor"))
    return result


def _actions_enriched(
    actions: dict[str, Any], approvers: dict[str, str | None]
) -> dict[str, Any]:
    out = dict(actions)
    merged = out.get("merged") or []
    enriched = []
    for nid in merged:
        node_id = str(nid)
        enriched.append(
            {
                "id": node_id,
                "approved_by": approvers.get(node_id),
            }
        )
    out["merged"] = enriched
    return out


def known_epic_refs(store: _Store) -> set[str]:
    return set(store.list_epic_refs())


def _archive_fields(state: ArchiveState | None) -> tuple[bool, str | None, str]:
    """The summary's three archive columns: ``(archived, at, reason)``.

    An unarchived epic is an ACTIVE epic, so it carries no retirement — the
    shelf's date and reason stay empty rather than echoing a decision that has
    been reversed (#2206).
    """
    if state is None or not state.archived:
        return False, None, ""
    return True, state.at, state.reason


@dataclass(frozen=True)
class _RosterFacts:
    """Every per-epic fact the Epics list needs, read for the WHOLE roster.

    One row of that list used to cost five queries — latest tick, heartbeat
    row, tick count, first tick ts, drain sum — so a 70-epic roster cost ~350
    queries per load (C3/#2296). Each of the five is now one batched read for
    the whole roster, and :func:`_summary_dto` composes a row from this bundle
    without touching the store again.

    Dicts omit epics the store has no row for, exactly as the point reads
    returned None, so a missing fact still reads as absent rather than zero.
    """

    ticks: dict[str, Any]
    states: dict[str, Any]
    spans: dict[str, tuple[int, str | None]]
    # Per epic, the nodes the ledger says closed behind a merged PR while the
    # journal still calls them live (#2392). Read for the whole roster at once.
    landed: dict[str, frozenset[str]]
    #: epic_ref -> tick-ATTEMPT stamp (:data:`EpicSummaryDTO.tick_attempt_stamp`).
    #: Absent for an epic with no attempt rows, which reads as ``""``.
    tick_stamps: dict[str, str]
    # Keyed by (node id, window start), NOT by node id: two epics can share a
    # DAG node and open their drain windows at different first ticks, so that
    # node has one drain per window (#2375 review).
    node_drain_usd: dict[tuple[str, str], float]
    #: The project drive's heartbeat, read once for the whole roster (#2617).
    drive: DriveHeartbeat | None = None
    #: epic_ref -> its newest tick ATTEMPT (#2844 N18). Absent for an epic with
    #: no attempt rows.
    attempts: Mapping[str, Any] = field(default_factory=dict)


def _roster_ticks(store: _Store, refs: list[str]) -> dict[str, Any]:
    batch = getattr(store, "latest_epic_ticks", None)
    if callable(batch):
        ticks: dict[str, Any] = batch()
        return {ref: ticks[ref] for ref in refs if ref in ticks}
    found = {ref: store.latest_epic_tick(ref) for ref in refs}
    return {ref: tick for ref, tick in found.items() if tick is not None}


def _roster_states(store: _Store, refs: list[str]) -> dict[str, Any]:
    batch = getattr(store, "list_epic_driver_states", None)
    if callable(batch):
        states: dict[str, Any] = batch()
        return {ref: states[ref] for ref in refs if ref in states}
    found = {ref: store.get_epic_driver_state(ref) for ref in refs}
    return {ref: state for ref, state in found.items() if state is not None}


def _roster_spans(store: _Store, refs: list[str]) -> dict[str, tuple[int, str | None]]:
    batch = getattr(store, "epic_tick_spans", None)
    if callable(batch):
        spans: dict[str, tuple[int, str | None]] = batch()
        return {ref: spans[ref] for ref in refs if ref in spans}
    return {
        ref: (store.count_epic_ticks(ref), store.first_epic_tick_ts(ref))
        for ref in refs
    }


def _roster_tick_stamps(store: _Store, refs: list[str]) -> dict[str, str]:
    """Every row's tick-attempt stamp, batched — see :func:`_tick_attempt_stamp`.

    One grouped query for the roster, with the per-epic point read as the
    fallback, exactly as the other four roster facts degrade (C3/#2296).
    """
    batch = getattr(store, "epic_tick_log_stamps", None)
    if callable(batch):
        stamps: dict[str, str] = batch(refs)
        return stamps
    return {ref: _tick_attempt_stamp(store, ref) for ref in refs}


def _roster_windows(
    refs: list[str],
    ticks: dict[str, Any],
    spans: dict[str, tuple[int, str | None]],
) -> dict[str, list[tuple[str, str]]]:
    """Per epic, the ``(node id, window start)`` pairs its drain is summed over.

    The window start is part of the key because it is part of the question:
    the same node in two epics is two drains whenever the two epics opened
    their journals at different ticks (#2375 review).
    """
    per_epic: dict[str, list[tuple[str, str]]] = {}
    for ref in refs:
        window_start = spans.get(ref, (0, None))[1]
        if window_start is None:
            continue
        snapshot = getattr(ticks.get(ref), "snapshot", None)
        per_epic[ref] = [
            (node_id, window_start) for node_id in _node_ids(snapshot) if node_id
        ]
    return per_epic


def _roster_drain(
    store: _Store,
    refs: list[str],
    ticks: dict[str, Any],
    spans: dict[str, tuple[int, str | None]],
) -> dict[tuple[str, str], float]:
    """Each roster node's drain, in one windowed aggregate when possible.

    An epic's window starts at its own first tick, so the whole roster is one
    query only if the window travels with the item. A store without
    ``sum_costs_by_item_windows`` falls back to the per-epic aggregate, which
    is what every epic used to pay.
    """
    per_epic = _roster_windows(refs, ticks, spans)
    keys = {key for pairs in per_epic.values() for key in pairs}
    batch = getattr(store, "sum_costs_by_item_windows", None)
    if callable(batch):
        totals: dict[tuple[str, str], float] = batch(keys)
        return {key: float(totals.get(key, 0.0)) for key in keys}
    merged: dict[tuple[str, str], float] = {}
    for pairs in per_epic.values():
        if not pairs:
            continue
        window_start = pairs[0][1]
        node_ids = [node_id for node_id, _ in pairs]
        for node_id, total in _drain_costs(store, node_ids, window_start).items():
            merged[(node_id, window_start)] = total
    return merged


def _roster_landed(
    store: _Store, refs: list[str], ticks: dict[str, Any]
) -> dict[str, frozenset[str]]:
    """Every roster row's landed nodes, from ONE pass over the ledger.

    Composed here rather than per row for the reason the rest of this bundle
    is: a 70-epic list must not pay 140 reads to tell an operator that the
    finished epics are finished.
    """
    groups = {
        ref: _candidate_nodes(getattr(ticks.get(ref), "snapshot", None)) for ref in refs
    }
    evidence = _read_landed_evidence(store, list(groups.values()))
    found = {ref: _landed_from(nodes, evidence) for ref, nodes in groups.items()}
    return {ref: landed for ref, landed in found.items() if landed}


def reconciled_epic_snapshots(
    store: _Store, ticks: Mapping[str, Any]
) -> dict[str, dict[str, Any] | None]:
    """Each epic's latest journaled snapshot, read through :func:`_reconciled`.

    The batched, public form of the reconciliation, for the other service that
    answers "what is this epic's state NOW" off the same journal:
    :mod:`kew.service.needs_you`. It read the raw journaled signal and the raw
    per-node rows while these routes read them repaired, which is the #2207
    one-page-two-answers failure wearing a new hat — a finished epic's page
    reporting ``complete`` with no node attention while the judgment queue the
    very same payload carries lists its nodes as waiting on a person (#2392
    review).

    Keyed by epic ref, value ``None`` for an epic with no tick at all. One
    ledger pass covers the whole mapping, so the needs-you walk pays what the
    Epics roster pays rather than two reads per epic.
    """
    refs = list(ticks)
    landed = _roster_landed(store, refs, dict(ticks))
    snapshots = {ref: getattr(ticks.get(ref), "snapshot", None) for ref in refs}
    return {
        ref: _reconciled(
            snapshots[ref],
            landed.get(ref, frozenset()),
            _preserved_histories(store, ref, snapshots[ref]),
        )
        for ref in refs
    }


def _roster_facts(
    store: _Store, refs: list[str], archive: dict[str, ArchiveState]
) -> _RosterFacts:
    ticks = _roster_ticks(store, refs)
    spans = _roster_spans(store, refs)
    live = _unarchived(refs, archive)
    return _RosterFacts(
        ticks=ticks,
        states=_roster_states(store, refs),
        spans=spans,
        node_drain_usd=_roster_drain(store, refs, ticks, spans),
        landed=_roster_landed(store, refs, ticks),
        tick_stamps=_roster_tick_stamps(store, live),
        drive=read_drive_heartbeat(store),
        attempts=_roster_attempts(store, live),
    )


def _unarchived(refs: list[str], archive: dict[str, ArchiveState]) -> list[str]:
    """The refs no archive decision currently retires: an archived row gets no
    attempt facts and no tick-attempt stamp, so the poll never reads its
    attempt rows (#2900 C4)."""
    return [ref for ref in refs if not getattr(archive.get(ref), "archived", False)]


def _roster_attempts(store: _Store, refs: list[str]) -> dict[str, Any]:
    """Every row's newest tick attempt, batched; nothing on a store without
    the batch read, which the row reads as "no attempt" (#2844 N18)."""
    batch = getattr(store, "latest_epic_tick_attempts", None)
    if not callable(batch):
        return {}
    attempts: dict[str, Any] = batch(refs)
    return attempts


def _roster_heartbeat(
    ref: str,
    state: Any | None,
    change_count: int,
    *,
    tick_attempt_stamp: str = "",
    now: datetime | None = None,
    drive: DriveHeartbeat | None = None,
) -> EpicHeartbeatDTO | None:
    """:func:`_heartbeat_dto` over already-read facts — same reading, no query."""
    if state is None:
        return None
    return _heartbeat_from(
        state,
        ref,
        change_count,
        tick_attempt_stamp=tick_attempt_stamp,
        now=now,
        drive=drive,
    )


def _roster_economics(
    snapshot: dict[str, Any] | None,
    window_start: str | None,
    node_drain_usd: dict[tuple[str, str], float],
) -> EpicEconomicsDTO:
    """``_economics`` over already-read facts — same shape, no query.

    An epic with no first tick has no window, so it has no per-node drain to
    report: the same empty answer :func:`_drain_costs` gives for ``None``.
    The bundle is keyed by this epic's own window, so a node it shares with
    another epic reads THIS epic's drain.
    """
    node_drain = (
        {
            nid: float(node_drain_usd.get((nid, window_start), 0.0))
            for nid in _node_ids(snapshot)
            if nid
        }
        if window_start
        else {}
    )
    return EpicEconomicsDTO(
        window_start=window_start,
        drain_total_usd=sum(node_drain.values()),
        node_drain_usd=node_drain,
    )


def _summary_dto(
    ref: str,
    retired: ArchiveState | None,
    facts: _RosterFacts,
    *,
    now: datetime | None = None,
) -> EpicSummaryDTO:
    """One row of the Epics list, composed from the roster's batched facts.

    The snapshot is read through :func:`_reconciled`, so a row whose journal
    stopped before the merges lands on the same counts and the same signal
    ``kew epic status`` reads live off the tracker (#2392).
    """
    tick = facts.ticks.get(ref)
    snapshot: dict[str, Any] | None = _reconciled(
        tick.snapshot if tick is not None else None,
        facts.landed.get(ref, frozenset()),
    )
    tick_count, window_start = facts.spans.get(ref, (0, None))
    tick_stamp = facts.tick_stamps.get(ref, "")
    heartbeat = _roster_heartbeat(
        ref,
        facts.states.get(ref),
        tick_count,
        tick_attempt_stamp=tick_stamp,
        now=now,
        drive=facts.drive,
    )
    economics = _roster_economics(snapshot, window_start, facts.node_drain_usd)
    last_tick_ts = (
        heartbeat.last_tick_ts
        if heartbeat is not None
        else (tick.tick_ts if tick is not None else None)
    )
    change_count = heartbeat.change_count if heartbeat is not None else tick_count
    archived, archived_at, archive_reason = _archive_fields(retired)
    return EpicSummaryDTO(
        epic_ref=ref,
        signal=_signal(snapshot),
        counts=dict(snapshot.get("counts") or {}) if snapshot else {},
        drain_total_usd=economics.drain_total_usd,
        last_tick_ts=last_tick_ts,
        **_summary_beat(heartbeat, facts.attempts.get(ref)),
        change_count=change_count,
        label=economics.label,
        archived=archived,
        archived_at=archived_at,
        archive_reason=archive_reason,
        # Off the attempt log, not the heartbeat: a row whose driver is locked
        # out, failing preflight or finding an empty graph moves no heartbeat
        # field at all, and the strip of that row is precisely the one a
        # watcher needs re-read (#2304 review round 3).
        tick_attempt_stamp=tick_stamp,
    )


def _summary_beat(
    heartbeat: EpicHeartbeatDTO | None, attempt: Any | None
) -> dict[str, Any]:
    """A roster row's heartbeat fields: the reading the epic page takes (#2844).

    Everything the page's freshness window reads, so the row and the page take
    one reading of one driver: the start stamp, the cadence, the last tick's
    duration and the newest tick attempt's instants.
    """
    if heartbeat is None:
        beat: dict[str, Any] = {
            "last_tick_started_at": None,
            "interval_s": None,
            "last_tick_duration_s": None,
            "driver_status": DRIVER_UNKNOWN,
        }
    else:
        beat = {
            # No fallback to the journal's tick_ts here: a started-at the
            # driver state does not hold is "we don't know when this tick
            # began", and the list reads that as the absence it is rather
            # than as a finish stamp.
            "last_tick_started_at": heartbeat.last_tick_started_at,
            # The cadence the server's OWN reading was thresholded against,
            # never the raw recorded column: an externally-driven epic records
            # no cadence of its own, so the list served interval_s=None and
            # every row of it printed "no heartbeat" while the epic page called
            # the same driver live (#2297 review).
            "interval_s": heartbeat.interval_s,
            "last_tick_duration_s": heartbeat.last_tick_duration_s,
            # The reading this row's heartbeat was already taken against,
            # forwarded rather than re-derived: the row's tick strip has to
            # know whether an OPEN attempt belongs to a driver still beating,
            # and an epic with no driver row at all is honestly `unknown`
            # (#2304 review round 5).
            "driver_status": heartbeat.driver_status,
        }
    # The newest tick ATTEMPT, which moves where no heartbeat field does — an
    # attempt turned away before the tick began, or one that changed nothing.
    beat["last_attempt_started_at"] = getattr(attempt, "ts_started", None)
    beat["last_attempt_finished_at"] = getattr(attempt, "ts_finished", None)
    beat["last_attempt_outcome"] = getattr(attempt, "outcome", None)
    beat["last_attempt_reached_tick"] = getattr(attempt, "reached_tick", False) is True
    return beat


def list_epics(store: _Store, *, now: datetime | None = None) -> list[EpicSummaryDTO]:
    # Every fact this list needs is read ONCE for the whole roster: the
    # archive decisions (#2206) and, since C3/#2296, the five per-epic reads
    # a row used to make for itself. A 70-epic list costs a fixed handful of
    # queries rather than ~350.
    archive = archive_states(store)
    refs = sorted(store.list_epic_refs())
    facts = _roster_facts(store, refs, archive)
    return [_summary_dto(ref, archive.get(ref), facts, now=now) for ref in refs]


def get_epic_detail(
    store: _Store,
    epic_ref: str,
    *,
    config: DispatchConfig | None = None,
    now: datetime | None = None,
) -> EpicDetailDTO | None:
    """The epic's latest snapshot, joined to who currently needs a person.

    The needs-you join is composed ONCE here, from the same work projection the
    console's judgment queue reads (``kew.service.attention``), against the
    node ids of the very snapshot it annotates. Passing no ``config`` composes
    no join and says so in ``snapshot.unavailable``.
    """
    if epic_ref not in known_epic_refs(store):
        return None
    tick = store.latest_epic_tick(epic_ref)
    journaled = tick.snapshot if tick is not None else None
    # Reconciled BEFORE the needs-you join is composed, so a node the ledger
    # has settled is not lit as waiting on a person by the very row that went
    # stale on it (#2392).
    snapshot = _reconciled(
        journaled,
        _landed_ids(store, journaled),
        _preserved_histories(store, epic_ref, journaled),
    )
    attention = _detail_attention(store, config, epic_ref, snapshot, now)
    snapshot_dto = (
        _snapshot_dto(
            tick, attention, _ledger_pr_urls(store, snapshot), snapshot=snapshot
        )
        if tick is not None
        else None
    )
    # The SAME ``now`` the attention join read: a request whose liveness reading
    # is taken against a different instant than the rest of its own answer can
    # contradict itself, and the tests that pin a fixture clock could not reach
    # this reading at all.
    heartbeat = _heartbeat_dto(store, epic_ref, now=now)
    economics = _economics(store, epic_ref, snapshot)
    return EpicDetailDTO(
        epic_ref=epic_ref,
        snapshot=snapshot_dto,
        heartbeat=heartbeat,
        economics=economics,
    )


def _detail_attention(
    store: _Store,
    config: DispatchConfig | None,
    epic_ref: str,
    snapshot: dict[str, Any] | None,
    now: datetime | None,
) -> AttentionJoin:
    """The needs-you join for this epic, handed the snapshot it annotates.

    The journaled node dicts carry the epic's own waits, and the signal is what
    lets the join drop them for a retired epic — both sides of the gate
    ``kew.service.attention.compose_node_attention`` owns, so this route and the
    live stream cannot read one epic differently (#2207).
    """
    return compose_node_attention(
        store,
        config,
        _node_ids(snapshot),
        nodes=_node_dicts(snapshot),
        epic_ref=epic_ref,
        signal=_signal(snapshot),
        now=now,
    )


#: Default/maximum strip length. Two hundred ticks is about the width a strip
#: of fixed slots can show without a slot narrower than a pixel, and the store
#: refuses more than :data:`kew.run_store.EPIC_TICK_LOG_TAIL_MAX`.
TICK_STRIP_LIMIT = 200
TICK_STRIP_MAX = 500


def tick_strip(
    store: _Store,
    epic_ref: str,
    *,
    limit: int = TICK_STRIP_LIMIT,
    changes: bool = False,
) -> list[EpicTickStripDTO]:
    """The epic's last ``limit`` tick ATTEMPTS, oldest-first (#2304).

    Read off ``epic_tick_log`` and nothing else. The change journal is not an
    answer to this question — it drops the roughly nine ticks in ten that
    changed nothing, and never saw the failed, locked-out and empty-graph
    attempts at all — and :func:`list_ticks` must never be made to serve the
    strip: it parses a full DAG snapshot per row, which at 200 rows is the read
    that made the epic page slow (C3/#2296).

    A row whose ``outcome`` this build has no word for is passed through as it
    was stored. Tolerating a bad historical row is the operator rule; the strip
    draws an unknown outcome as quiet rather than rewriting it.

    ``changes`` reads only the attempts the Epics list's change strip draws —
    the ones that journaled a change, failed, or are still running — so a
    change is not lost behind a page of no-op ticks (#2844 D23).
    """
    bounded = max(1, min(limit, TICK_STRIP_MAX))
    rows = (
        store.list_epic_tick_log_marks(epic_ref, limit=bounded)
        if changes
        else store.list_epic_tick_log_tail(epic_ref, limit=bounded)
    )
    return [
        EpicTickStripDTO(
            id=int(getattr(row, "id", 0) or 0),
            ts_started=str(getattr(row, "ts_started", "") or ""),
            ts_finished=getattr(row, "ts_finished", None),
            outcome=getattr(row, "outcome", None),
            signal=getattr(row, "signal", None),
            n_dispatched=int(getattr(row, "n_dispatched", 0) or 0),
            n_merged=int(getattr(row, "n_merged", 0) or 0),
            n_held=int(getattr(row, "n_held", 0) or 0),
            n_stuck=int(getattr(row, "n_stuck", 0) or 0),
            journal_tick_id=getattr(row, "journal_tick_id", None),
            error_class=getattr(row, "error_class", None),
            reached_tick=getattr(row, "reached_tick", False) is True,
        )
        for row in rows
    ]


def list_ticks(
    store: _Store, epic_ref: str, *, after_id: int = 0, limit: int = 500
) -> list[EpicTickDTO]:
    ticks = store.list_epic_ticks(epic_ref, after_id=after_id, limit=limit)
    approvers = _approver_by_issue(store) if ticks else {}
    prs = _ledger_pr_urls(store, ticks[-1].snapshot) if ticks else {}
    return [
        _tick_dto(store, epic_ref, tick, approvers, ledger_prs=prs) for tick in ticks
    ]


def _node_run_dto(rec: Any) -> EpicNodeRunDTO:
    return EpicNodeRunDTO(
        run_id=str(getattr(rec, "run_id", "")),
        status=str(getattr(rec, "status", "") or ""),
        model=str(getattr(rec, "model", "") or ""),
        runner=str(getattr(rec, "runner_name", "") or ""),
        cost_usd=float(getattr(rec, "estimated_cost_usd", 0.0) or 0.0),
        total_tokens=int(getattr(rec, "total_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(rec, "cache_creation_tokens", 0) or 0),
        cache_read_tokens=int(getattr(rec, "cache_read_tokens", 0) or 0),
        pr_url=getattr(rec, "pr_url", None),
        started_at=_iso(getattr(rec, "started_at", None)),
        finished_at=_iso(getattr(rec, "finished_at", None)),
    )


MAX_NODE_ID_LEN = 64
"""Upper bound on an epic node id — a tracker key, never a path or a sentence.

Generous enough for every real tracker form (``101``, ``SFD-12``,
``other-repo#5``) and small enough that an oversized segment is rejected before
it is ever used as a lookup key.
"""

# Tracker keys are provider-defined, so punctuation is not shape validation.
# Bounds exclude empty, non-printable, path, and traversal inputs; exact
# snapshot membership is the authoritative existence gate.


def valid_node_id(node_id: str) -> bool:
    """True when *node_id* is a bounded, non-path-shaped opaque id."""
    decoded = unquote(node_id)
    return (
        0 < len(node_id) <= MAX_NODE_ID_LEN
        and bool(node_id.strip())
        and node_id.isprintable()
        and decoded.isprintable()
        and "/" not in decoded
        and "\\" not in decoded
        and ".." not in decoded
    )


def epic_member_ids(store: _Store, epic_ref: str) -> set[str]:
    """The exact node ids of *epic_ref*, from its latest journaled snapshot.

    The snapshot is the epic's membership record; an epic with no journaled
    tick has no known members, and saying so is honest — inventing an empty
    node detail for an arbitrary id would fabricate a node that does not exist.
    """
    tick = store.latest_epic_tick(epic_ref)
    return {n for n in _node_ids(tick.snapshot if tick is not None else None) if n}


def is_epic_member(store: _Store, epic_ref: str, node_id: str) -> bool:
    """Exact membership of *node_id* in *epic_ref* — tracker-neutral."""
    return valid_node_id(node_id) and node_id in epic_member_ids(store, epic_ref)


def get_node_detail(
    store: _Store, epic_ref: str, node_id: str
) -> EpicNodeDetailDTO | None:
    if epic_ref not in known_epic_refs(store):
        return None
    # Two gates, in order: bounds (is this shaped like an id at all?) then
    # exact membership (is it a node of THIS epic?). Neither one counts digits
    # — a non-numeric tracker's ``SFD-12`` is as real a member as ``101``.
    if not is_epic_member(store, epic_ref, node_id):
        return None
    # The node id IS the durable item id — key run history on it directly.
    item_id = str(node_id)
    records = sorted(
        store.list_by_item(item_id),
        key=lambda r: str(getattr(r, "started_at", "") or ""),
        reverse=True,
    )
    lifetime_usd = sum(
        float(getattr(r, "estimated_cost_usd", 0.0) or 0.0) for r in records
    )
    window_start = store.first_epic_tick_ts(epic_ref)
    drain = _drain_costs(store, [item_id], window_start)
    return EpicNodeDetailDTO(
        id=item_id,
        runs=[_node_run_dto(r) for r in records],
        lifetime_usd=lifetime_usd,
        drain_usd=drain.get(item_id, 0.0),
    )


NODE_STATUS_EVENT = "epic_node_status"
"""The driver's per-node transition fact — what a status band is built from.

Emitted by ``kew.graph.driver.journal.record_tick`` with payload
``{"epic_ref", "from", "to"}`` and keyed on ``item_id``.
"""


def _band_events(store: _Store, epic_ref: str) -> list[Any]:
    """This epic's node-status transitions, oldest first.

    Bounded to one event name and one epic by the store filter, so the read
    never walks the whole chain: ``epic_ref`` matches the payload field the
    driver stamps on every transition it records.
    """
    events = store.list_audit_events(None, event=NODE_STATUS_EVENT, epic_ref=epic_ref)
    return list(reversed(events))


def _bands_by_node(
    events: list[Any], members: set[str]
) -> dict[str, list[EpicTimelineBandDTO]]:
    """Transitions folded into per-node bands: each event closes the node's
    open band and opens a new one, so the last band of a lane stays open
    (``to=None``) — the node is in that status still."""
    bands: dict[str, list[EpicTimelineBandDTO]] = {}
    for event in events:
        node_id = str(getattr(event, "item_id", "") or "")
        payload = getattr(event, "payload", None) or {}
        status = str(payload.get("to") or "")
        at = _iso(getattr(event, "timestamp", None))
        if node_id not in members or not status or not at:
            continue
        lane = bands.setdefault(node_id, [])
        if lane:
            lane[-1].to = at
        lane.append(EpicTimelineBandDTO(status=status, from_=at, to=None))
    return bands


def _timeline_run_dto(rec: Any) -> EpicTimelineRunDTO:
    return EpicTimelineRunDTO(
        run_id=str(getattr(rec, "run_id", "")),
        kind=str(getattr(rec, "run_kind", "") or ""),
        status=str(getattr(rec, "status", "") or ""),
        started_at=_iso(getattr(rec, "started_at", None)),
        finished_at=_iso(getattr(rec, "finished_at", None)),
        cost_usd=float(getattr(rec, "estimated_cost_usd", 0.0) or 0.0),
        attempt_number=int(getattr(rec, "attempt_number", 1) or 1),
    )


def _round_verdict(row: Any) -> str:
    """The disposition a reader should plot for one round.

    ``effective_verdict`` is the driver's acceptance when the review stop rule
    took a low/medium-only NEEDS_CHANGES round as READY_WITH_RESIDUALS (#2151),
    and the reviewer's verdict otherwise. Reading the raw ``verdict`` instead
    draws an accepted round as NEEDS_CHANGES on a lane that also carries its
    merge marker, and contradicts the review cell the same node's pipeline
    serves off this same field.
    """
    return str(
        getattr(row, "effective_verdict", None) or getattr(row, "verdict", "") or ""
    )


def _rounds_by_node(
    rounds: list[Any], members: set[str]
) -> dict[str, list[EpicTimelineRoundDTO]]:
    by_node: dict[str, list[EpicTimelineRoundDTO]] = {}
    for row in rounds:
        node_id = str(getattr(row, "item_id", "") or "")
        at = _iso(getattr(row, "recorded_at", None))
        if node_id not in members or not at:
            continue
        by_node.setdefault(node_id, []).append(
            EpicTimelineRoundDTO(
                round=int(getattr(row, "round", 0) or 0),
                verdict=_round_verdict(row),
                recorded_at=at,
            )
        )
    return by_node


def _merged_at(records: list[Any]) -> str | None:
    """The newest recorded merge among a node's runs, or None if never
    merged. A node can carry several PRs over its life (a retry opens a new
    one); the latest merge is the one that landed the work."""
    stamps = [_iso(getattr(r, "pr_merged_at", None)) for r in records]
    merged = [s for s in stamps if s]
    return max(merged) if merged else None


def _first_activity(node: EpicTimelineNodeDTO) -> str | None:
    """When this node's lane first shows anything — the earliest band start
    or run start. None means the node has never moved and never run."""
    stamps = [band.from_ for band in node.bands]
    stamps += [run.started_at for run in node.runs if run.started_at]
    return min(stamps) if stamps else None


def _timeline_node_dto(
    node: dict[str, Any],
    bands: dict[str, list[EpicTimelineBandDTO]],
    runs: dict[str, list[Any]],
    rounds: dict[str, list[EpicTimelineRoundDTO]],
) -> EpicTimelineNodeDTO:
    node_id = str(node.get("id") or "")
    records = runs.get(node_id, [])
    return EpicTimelineNodeDTO(
        id=node_id,
        title=str(node.get("title") or ""),
        bands=bands.get(node_id, []),
        runs=[_timeline_run_dto(r) for r in records],
        rounds=rounds.get(node_id, []),
        merged_at=_merged_at(records),
    )


def _timeline_members(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The nodes that get a lane: this epic's own, identified. External
    boundary nodes are another epic's work and are left out."""
    return [
        n
        for n in _node_dicts(snapshot)
        if str(n.get("id") or "") and not n.get("external", False)
    ]


def get_epic_timeline(store: _Store, epic_ref: str) -> EpicTimelineDTO | None:
    """Where an epic's time went, per node (#2270) — bands, runs, verdicts,
    merge. Ledger-only and read-only: three bulk reads for the whole epic,
    never one per node. None for an epic the store has never journaled.
    """
    if epic_ref not in known_epic_refs(store):
        return None
    tick = store.latest_epic_tick(epic_ref)
    members = _timeline_members(tick.snapshot if tick is not None else None)
    ids = [str(n["id"]) for n in members]
    member_set = set(ids)
    bands = _bands_by_node(_band_events(store, epic_ref), member_set)
    runs = store.list_by_items(ids)
    rounds = _rounds_by_node(store.list_review_rounds_for_epic(epic_ref), member_set)
    lanes = [
        (_first_activity(dto), dto)
        for dto in (_timeline_node_dto(n, bands, runs, rounds) for n in members)
    ]
    # Stable sort on first activity, so the nodes with none keep snapshot
    # order behind the ones that have some.
    lanes.sort(key=lambda lane: (lane[0] is None, lane[0] or ""))
    now = datetime.now(UTC).isoformat()
    return EpicTimelineDTO(
        epic_ref=epic_ref,
        now=now,
        # The span opens at the earliest activity anywhere in the epic, which
        # the ordering has already put first, and runs to now.
        span=EpicTimelineSpanDTO(start=lanes[0][0] if lanes else None, end=now),
        nodes=[dto for _, dto in lanes],
    )


def _tick_dto(
    store: _Store,
    epic_ref: str,
    tick: Any,
    approvers: dict[str, str | None],
    attention: AttentionJoin | None = None,
    *,
    ledger_prs: dict[str, str] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> EpicTickDTO:
    snapshot_dto = _snapshot_dto(tick, attention, ledger_prs, snapshot=snapshot)
    actions = _actions_enriched(tick.actions or {}, approvers)
    economics = _economics(
        store,
        epic_ref,
        snapshot if snapshot is not None else tick.snapshot,
        upper=tick.tick_ts,
    )
    return EpicTickDTO(
        id=tick.id,
        tick_ts=tick.tick_ts,
        snapshot=snapshot_dto,
        actions=actions,
        economics=economics,
    )


def _emitted_nodes(
    ticks: list[Any],
    landed: frozenset[str],
    histories: Mapping[str, ContinuationHistory] | None = None,
) -> list[dict[str, Any]]:
    """Every node the emitted frames name — one join covers them all.

    Keyed by id, newest frame last, so a node restated by a later tick is read
    at the state that tick recorded: the join annotates the frames a subscriber
    is about to hold, and an older copy's wait is not the one on screen.

    Reconciled with the poll's *landed* set, because these are the dicts the
    frames themselves carry: a node the ledger has settled must not reach the
    join as live work in the stream while the detail route reads it done.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for tick in ticks:
        for node in _node_dicts(_reconciled(tick.snapshot, landed, histories)):
            by_id[str(node.get("id", ""))] = node
    return list(by_id.values())


def _snapshot_frames(
    store: _Store,
    epic_ref: str,
    emitted: list[Any],
    attention: AttentionJoin,
    landed: frozenset[str],
    histories: Mapping[str, ContinuationHistory] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """The ticks of one poll, as ("snapshot", ...) frames.

    A live frame carries the SAME needs-you join the detail route composes.
    The live view replaces its whole snapshot with each frame, so a frame
    without the join would erase the annotation the detail read just made and
    leave a ticking epic contradicting the judgment queue about the identical
    tick. One join covers the whole batch, so every frame of one poll tells one
    story. Unlike ``list_ticks``, this is a live read of now, not of history —
    which is why *landed* reconciles these frames (#2392) and does not touch
    the ``/ticks`` history: app.py replaces a tab's whole snapshot with a
    streamed one, so an unreconciled frame would overwrite the detail route's
    reading with the stale journal it just repaired.
    """
    if not emitted:
        return []
    approvers = _approver_by_issue(store)
    prs = _ledger_pr_urls(store, _reconciled(emitted[-1].snapshot, landed, histories))
    return [
        (
            "snapshot",
            _tick_dto(
                store,
                epic_ref,
                tick,
                approvers,
                attention,
                ledger_prs=prs,
                snapshot=_reconciled(tick.snapshot, landed, histories),
            ).model_dump(),
        )
        for tick in emitted
    ]


def _live_snapshot(
    store: _Store, epic_ref: str, emitted: list[Any]
) -> dict[str, Any] | None:
    """The snapshot this poll's LIVE reading is about.

    The newest frame it is about to emit, or — on a poll with no new journal
    row — the latest journaled tick, which is exactly the DAG a subscriber is
    still holding. Read once per poll and handed to everything that needs it,
    so the ledger reconciliation and the join read one snapshot, not two.
    """
    if emitted:
        snapshot: dict[str, Any] | None = emitted[-1].snapshot
        return snapshot
    latest = store.latest_epic_tick(epic_ref)
    return latest.snapshot if latest is not None else None


def _poll_nodes(
    emitted: list[Any],
    live: dict[str, Any] | None,
    landed: frozenset[str],
    histories: Mapping[str, ContinuationHistory] | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """Which nodes this poll's join must cover, and the newest signal over them.

    The nodes are the union of the frames this poll is about to emit, or — on a
    poll with no new journal row — the latest snapshot's, which is exactly the
    DAG a live subscriber is still holding.

    The node DICTS, not just their ids: the join reads each node's own journaled
    wait (awaiting a merge, a continuation, a publication) off the snapshot, so a
    streamed frame lights the same diamonds the detail route serves (#2207).

    The signal comes from the NEWEST snapshot of the same reading, because it
    gates those same journaled waits: a retired epic's stream must not re-light
    the nodes the detail route and the needs-you set both drop — app.py replaces
    a tab's whole snapshot with a streamed one, so the stream would win.

    *live* is the reconciled newest snapshot (:func:`_live_snapshot` through
    :func:`_reconciled`), so the signal that gates those waits is the one the
    frames and the detail route both carry."""
    if emitted:
        return _emitted_nodes(emitted, landed, histories), _signal(live)
    nodes = [n for n in _node_dicts(live) if str(n.get("id", ""))]
    return nodes, _signal(live)


def _attention_frame(attention: AttentionJoin) -> dict[str, Any]:
    return EpicAttentionDTO(
        by_node=dict(attention.by_node), unavailable=list(attention.unavailable)
    ).model_dump()


def _recovery_frame(snapshot: dict[str, Any] | None, epic_ref: str) -> dict[str, Any]:
    """Each node's stuck reason and recovery command as they read now (#2960),
    from the same reconciled snapshot the detail route serves."""
    epic_ref = str((snapshot or {}).get("epic_ref") or epic_ref)
    by_node: dict[str, NodeRecoveryDTO] = {}
    for n in _node_dicts(snapshot):
        node_id = str(n.get("id", ""))
        if not node_id:
            continue
        status = str(n.get("status") or "")
        stuck = n.get("stuck")
        by_node[node_id] = NodeRecoveryDTO(
            stuck=stuck if isinstance(stuck, str) else None,
            blocked_reason=_blocked_reason(status, stuck),
            recovery_command=_recovery_command(status, stuck, epic_ref, node_id),
        )
    return EpicRecoveryDTO(by_node=by_node).model_dump()


def _attention_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _heartbeat_tuple(store: _Store, epic_ref: str) -> list[Any] | None:
    """What has to move for this poll to re-send the heartbeat frame.

    The tick-ATTEMPT stamp is in the tuple because the other three are not
    enough: they all come from ``begin``/``finish_epic_tick``, which a driver
    only reaches past the lock, the preflight and the non-empty-graph guard, so
    a driver locked out or failing every tick emitted NO frame at all — and a
    live subscriber whose tick strip is redrawn on the heartbeat sat frozen on
    its last reading through exactly the attempts the strip was added to show
    (#2304 review round 3).

    It is also why a missing driver row is not the end of the tuple: an epic
    whose every attempt was refused before ``begin_epic_tick`` has no row to
    read those three fields off, and returning ``None`` there was the same
    silence one layer down — see :func:`_attempt_only_heartbeat`, which is the
    frame this tuple then gates (#2304 review round 4).

    The liveness READING is the last element, and it is the one member that can
    move with nothing but the clock. It has to be here because the frame it
    gates is what a subscriber's tick strip reads an OPEN attempt row against:
    every other member is written by a tick, so a driver that opens a row and
    then dies mid-tick moves none of them ever again — and the strip would hold
    the ``live`` reading of the instant that row opened, painting a tick as
    running for the rest of the epic's life. That is the bound the strip's own
    ``TICK_LIVE_MAX_MS`` keeps when it is given no reading at all; a reading
    that cannot go stale is what lets it keep a long tick live instead
    (#2304 review round 5).
    """
    stamp = _tick_attempt_stamp(store, epic_ref)
    state = store.get_epic_driver_state(epic_ref)
    if state is None:
        return [None, None, None, stamp, DRIVER_UNKNOWN] if stamp else None
    return [
        state.tick_count,
        state.last_tick_ts,
        state.last_signal,
        stamp,
        liveness_from_state(state, epic_ref, drive=read_drive_heartbeat(store)).status,
    ]


def _economics_hash(store: _Store, epic_ref: str) -> tuple[str, EpicEconomicsDTO]:
    latest_tick = store.latest_epic_tick(epic_ref)
    snap = latest_tick.snapshot if latest_tick is not None else None
    economics = _economics(store, epic_ref, snap)
    digest = hashlib.sha256(
        json.dumps(economics.node_drain_usd, sort_keys=True).encode()
    ).hexdigest()
    return digest, economics


def _stream_ticks(store: _Store, epic_ref: str, last_tick_id: int) -> list[Any]:
    """The rows this poll should consider: the tail on a cold cursor.

    A first poll only ever emits the newest ``_MAX_SNAPSHOT_FRAMES``
    snapshots, so reading from row 0 read up to a thousand of them to throw
    almost all away — and left the cursor at the thousandth row rather than
    the newest, making a long-lived epic replay its history poll by poll
    before it caught up (C3/#2296). Seed from the tail instead. A warm cursor
    still seeks forward: those rows are the frames the subscriber has missed.
    """
    if last_tick_id:
        return store.list_epic_ticks(epic_ref, after_id=last_tick_id, limit=1000)
    tail = getattr(store, "list_epic_ticks_tail", None)
    if callable(tail):
        rows: list[Any] = tail(epic_ref, limit=_MAX_SNAPSHOT_FRAMES)
        return rows
    return store.list_epic_ticks(epic_ref, after_id=0, limit=1000)


def fetch_epic_frames(
    store: _Store,
    epic_ref: str,
    cursor: dict[str, Any],
    *,
    config: DispatchConfig | None = None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """One poll's worth of new frames for a live-view subscriber.

    cursor keys: "tick_id" (last emitted journal row id), "attn" (hash of the
    last-sent needs-you join), "recovery" (hash of the last-sent per-node
    stuck reasons and recovery commands, :func:`_recovery_frame`), "hb"
    (last-sent heartbeat tuple), "econ" (hash of last-sent node_drain_usd).
    Frame kinds are independent: a new journal row alone yields a snapshot
    frame only; heartbeat advancing alone (e.g. tick_count) must NOT re-send
    snapshots; a run-cost change alone yields an economics frame only.

    The join is read on EVERY poll, not only when a row was appended, because
    attention moves without the driver: serve's own sweep persisting a PR
    merge, an approval hold opening or clearing. A join bound to the journal
    would leave a live subscriber lit over work nobody is waiting on — and the
    watch hub replays the latest frame of each type, so a stale one would
    overwrite a fresh tab's detail read. The attention frame is emitted after
    the snapshot frames of the same poll (both carry the identical join), and
    the hub replays the latest frame of each kind in the order they were last
    emitted, so a replay settles on the newest join even when an
    attention-only poll came after the last snapshot.

    ``config`` is what lets a frame carry the needs-you join, exactly as the
    detail route does; without it every streamed frame honestly names the join
    unavailable rather than reporting an epic where nobody is waiting.
    """
    last_tick_id = int(cursor.get("tick_id") or 0)
    new_ticks = _stream_ticks(store, epic_ref, last_tick_id)
    # Capped to the newest _MAX_SNAPSHOT_FRAMES (snapshots are idempotent
    # full-state, so skipping older ones is safe for a live view) but the
    # cursor always advances past every new row, capped or not.
    emitted = new_ticks[-_MAX_SNAPSHOT_FRAMES:]
    journaled = _live_snapshot(store, epic_ref, emitted)
    landed = _landed_ids(store, journaled)
    histories = _preserved_histories(
        store, epic_ref, journaled, *(tick.snapshot for tick in emitted)
    )
    live = _reconciled(journaled, landed, histories)
    poll_nodes, poll_signal = _poll_nodes(emitted, live, landed, histories)
    attention = compose_node_attention(
        store,
        config,
        [str(n.get("id", "")) for n in poll_nodes],
        nodes=poll_nodes,
        epic_ref=epic_ref,
        signal=poll_signal,
    )
    frames = _snapshot_frames(store, epic_ref, emitted, attention, landed, histories)

    attn_frame = _attention_frame(attention)
    attn_hash = _attention_hash(attn_frame)
    if attn_hash != cursor.get("attn"):
        frames.append(("attention", attn_frame))

    # After the snapshot frames, so the hub's replay settles on it too: a
    # recovery command moves with the continuation history, not the journal.
    recovery_frame = _recovery_frame(live, epic_ref)
    recovery_hash = _attention_hash(recovery_frame)
    if recovery_hash != cursor.get("recovery"):
        frames.append(("recovery", recovery_frame))

    hb_tuple = _heartbeat_tuple(store, epic_ref)
    if hb_tuple != cursor.get("hb"):
        heartbeat = _heartbeat_dto(store, epic_ref)
        if heartbeat is not None:
            frames.append(("heartbeat", heartbeat.model_dump()))

    econ_hash, economics_full = _economics_hash(store, epic_ref)
    if econ_hash != cursor.get("econ"):
        frames.append(("economics", economics_full.model_dump()))

    new_cursor = {
        "tick_id": new_ticks[-1].id if new_ticks else last_tick_id,
        "attn": attn_hash,
        "recovery": recovery_hash,
        "hb": hb_tuple,
        "econ": econ_hash,
    }
    return new_cursor, frames
