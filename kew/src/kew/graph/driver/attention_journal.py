"""Operator-attention events read off the driver's JOURNALED observations (#2469).

``kew epic watch`` existed to be polled. It was not cheap enough to be: a
one-shot watch of a 23-node epic took 92.6 s wall for 3.8 s of CPU on an idle
host, because the read it shares with ``kew epic status`` re-queries the forge
per node — a ``gh pr view`` for every ledger and closing-reference candidate the
tick's one ``gh pr list`` batch did not cover, plus a head-check read per
approved PR. Operator pollers gave the call 240 s and still timed out, so holds
and merge-ready PRs arrived late or not at all.

The driver already wrote every one of those observations down. Each tick
journals a snapshot (``epic_ticks``) carrying each node's overlay
``DriverStatus``, its recorded stuck reason, its PR URL and the PR facts
``#2303`` added — head oid, merge state, last push. This module reads that
snapshot back and composes the SAME attention events from it, with no
subprocess and no driver lock in the path: a watch is read-only, so it must
never wait on a tick that is holding either.

What it is NOT is a second copy of the rules. The waiting-reason wording, the
identity and the classification are the driver's own functions, imported from
:mod:`kew.graph.driver.tick` and :mod:`kew.waits` and handed journaled facts
instead of live ones — "two implementations of one rule is how one system
starts telling one user two stories".

Three divergences from a live read are deliberate and bounded, because the
journal does not carry the fact they turn on:

* A node at a head-check human gate (``ci.approval_gated_checks``) has the gate
  named on its line by a live read (``tick._status_ci_gate_reasons``, one
  ``gh`` call per approved PR — the per-node call this path exists to drop).
  Here it reads as a plain ``in_review`` awaiting merge: the same item, the same
  ``merge_ready`` kind, one clause less of detail.
* A node whose PR merged while its issue stayed open is ``merged_unclosed`` —
  nobody's job, since the next tick closes the issue — and the only evidence is
  the PR's own state, which the snapshot does not carry. A journaled ``held``
  node with no hold row is therefore read as exactly that; one WITH a hold row
  keeps the hold's reason, where a live read would have preferred
  ``merged_unclosed``.
* A live read emits ``summary.waiting`` before ``summary.stuck`` — the richer
  reason first, so a stall relabel can never overwrite it. The journal keeps
  ONE reason per node (``tick._write_journal`` writes every waiting reason into
  the snapshot's ``nodes[].stuck`` map alongside every stuck one, which is how
  the epics DTO's ``stuck`` field carries the held and in_review lines too), so
  there is no split left to order by and nothing to overwrite. Events come out
  in node order instead — the plain ``node_id`` string order ``tick`` itself
  sorts ``summary.waiting`` and its stuck rows with, NOT the natural
  ``item_id_sort_key``, so a journaled read and a live one put node ``10`` in
  the same place relative to node ``9``. A nicer order for a human is a change
  to make on the driver's side, where both reads would pick it up.

Two snapshots are refused outright instead, both for the one reason — a
snapshot this module cannot read ALL of would report a frozen epic as quiet,
and quiet is the answer an operator stops polling on:

* The one an INVALID-GRAPH tick wrote (:func:`_is_invalid_graph_snapshot`). It
  is journaled before any observation is made, so it carries raw tracker
  statuses and one unbucketed reason per node — nothing a person can be named
  off. The live read rebuilds the graph, so it is also the read that has the
  answer as soon as an operator breaks the cycle.
* One carrying a status word this build has no ``DriverStatus`` member for
  (:func:`_unreadable_status`), which a ledger a NEWER kew wrote can. The node
  is the unit a reader is tempted to drop instead, and dropping it is the same
  failure one node down: the epic's only waiting node goes unreported and the
  watch exits 0 while a real hold stands.

Every event names the instant its observations were last recorded, so a reader
can age them instead of mistaking a stale read for a current one — and that
instant is the DRIVER's last finished tick, not the journal row's, because
``epic_ticks`` dedups on the snapshot hash and so stops advancing the moment a
stable epic's state first appeared (:func:`_last_recorded_at`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.continuation_recovery import ContinuationHistory, preserved_host_death_reason
from kew.epic_holds import holds_node_out_of_frontier, safe_hold_reason
from kew.graph.driver.attention import (
    EPIC_COMPLETE_HINT,
    EPIC_COMPLETE_REASON,
    AttentionEvent,
    token_warning_events,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import (
    _FIXED_WAITING_REASONS,
    INVALID_GRAPH_REASON_PREFIX,
    MERGED_UNCLOSED_WAIT_REASON,
    _awaiting_publication_reason,
    _continuation_histories,
    _continuation_reason_map,
    _holds_by_item,
    _in_review_reason,
    _publication_identity,
    _review_identity,
)
from kew.runners.registry import active_runner_token_warnings
from kew.waits import AttentionKind, classify_wait

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kew.run_store import EpicDriverState, EpicTick, RunStore

_logger = logging.getLogger(__name__)

__all__ = [
    "JournaledObservations",
    "journaled_attention_events",
    "read_journaled_observations",
]

#: The PR number in a canonical pull-request URL. Repo-agnostic on purpose: the
#: URL being read back is one kew itself journaled for this node, so the
#: question here is only "which number", never "is this our repository" (that
#: is ``GitHubFactsReader.pr_number_from_url``, which needs a repo and a host).
_PR_URL_NUMBER = re.compile(r"/pull/(\d+)/?$")

#: Statuses no attention event can be about, so their nodes are never read.
#: ``todo`` is the one that needs saying: a blocked TODO's reason is a
#: dependency wait, which ``kew.waits`` maps to nobody — and reproducing it
#: would need the frontier the snapshot does not journal.
_QUIET_STATUSES = frozenset(
    {
        DriverStatus.TODO.value,
        DriverStatus.IN_PROGRESS.value,
        DriverStatus.DONE.value,
    }
)


@dataclass(frozen=True)
class JournaledObservations:
    """One tick's recorded per-node observations, read back off the journal.

    ``recorded_at`` is the instant these observations were LAST recorded — what
    a reader ages them by, and what ``kew epic watch`` reports as
    ``observations stale since``. It is the driver's last finished tick where
    that is later than the journal row the observations came out of, because
    the row alone cannot date them: see :func:`_last_recorded_at`.

    ``reasons`` is every reason the driver recorded against a node, and it is
    NOT a live summary's ``stuck``: ``tick._write_journal`` folds
    ``result.waiting`` AND ``result.stuck`` into the single snapshot
    ``nodes[].stuck`` map (#661, which is how the epics DTO's ``stuck`` field
    came to carry the held, in_review and blocked lines as well). The field is
    named for what it holds because reading that map as "the stuck ones" makes
    every waiting node look stuck — and a HELD node not recognized as held
    loses the ``UNBUCKETED_HELD_HINT`` fallback ``kew.waits`` promises every
    honored hold.
    """

    recorded_at: str
    signal: str
    statuses: dict[str, DriverStatus] = field(default_factory=dict)
    node_facts: dict[str, NodeFacts] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    dependents: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _pr_number(pr_url: Any) -> int | None:
    """The PR number a journaled URL names, or None when it names none."""
    if not isinstance(pr_url, str):
        return None
    found = _PR_URL_NUMBER.search(pr_url)
    return int(found.group(1)) if found else None


#: Every overlay status word THIS build can read, by its journaled spelling.
_STATUS_WORDS = {status.value: status for status in DriverStatus}


def _driver_status(status: Any) -> DriverStatus | None:
    """The journaled status word as a ``DriverStatus``, or None for a word this
    build has no member for.

    A snapshot is JSON written by whichever kew ran the tick, so an unknown
    word is never a crash — and never a row to quietly pass over either, since
    a node nobody can read is a node whose attention nobody reports. The whole
    snapshot is refused instead; :func:`_unreadable_status` is where that is
    decided and said out loud.
    """
    return _STATUS_WORDS.get(str(status))


def _journaled_facts(node: dict[str, Any], nid: str) -> NodeFacts:
    """The node's journaled GitHub facts, as the ``NodeFacts`` readers expect.

    Only the fields the driver actually wrote are filled. ``state``,
    ``labels``, ``review_decision`` and ``pr_state`` are not in the snapshot,
    and they are left empty rather than guessed: the reason and identity
    functions this feeds read the PR number, the head and the last push, and a
    fact nobody recorded must not be invented from a status word.
    """
    pr_url = node.get("pr_url")
    return NodeFacts(
        issue_num=int(nid) if nid.isdigit() else 0,
        state="",
        state_reason=None,
        labels=frozenset(),
        pr_number=_pr_number(pr_url),
        pr_url=pr_url if isinstance(pr_url, str) and pr_url else None,
        review_decision=None,
        merge_state_status=node.get("merge_state"),
        head_oid=node.get("head_oid"),
        pr_pushed_at=node.get("pr_pushed_at"),
    )


def _dependents(edges: Any, node_ids: set[str]) -> dict[str, tuple[str, ...]]:
    """Each node's transitive dependents, off the journaled edge list.

    The same closure ``Graph.transitive_dependents`` walks (``[src, dst]``:
    ``dst`` depends on ``src``), computed once for every node rather than
    re-walked per reported row.
    """
    direct: dict[str, set[str]] = {}
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, list) or len(edge) != 2:
            continue
        src, dst = str(edge[0]), str(edge[1])
        if src != dst:
            direct.setdefault(src, set()).add(dst)
    out: dict[str, tuple[str, ...]] = {}
    for nid in node_ids:
        seen: set[str] = set()
        stack = [nid]
        while stack:
            for dep in direct.get(stack.pop(), ()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        out[nid] = tuple(sorted(seen))
    return out


def _is_invalid_graph_snapshot(nodes: list[Any]) -> bool:
    """Whether this snapshot is the one an INVALID-GRAPH tick wrote.

    That branch (``tick._journal_invalid``) journals to say WHY the drain
    froze, and it is the one snapshot that records no observations: it runs
    before any overlay or facts read, so ``_write_journal`` gets no statuses
    and no node facts, and every node lands in the snapshot with its RAW
    TRACKER status and the same ``invalid-graph:`` reason.

    Nothing in it can be read as attention. The raw status says nothing about
    holding — a node held on a budget preflight reads ``todo`` — and the one
    reason no ``STALL_BUCKETS`` prefix names drowns out the per-node reasons
    the tick never got far enough to compute, so every node classifies as
    nobody's job. Composing off it reports a frozen epic as QUIET while real
    holds and merge-ready PRs wait; the graph is also the one thing a live read
    rebuilds from scratch, so once an operator breaks the cycle the live read
    has the answer and this snapshot still does not.

    Tested on the reason rather than on a writer-side marker so the already
    journaled rows of every epic that has ever stalled this way are covered
    too. A reason that merely looks like one (an operator hold worded to match)
    costs a live read, which is the safe direction.
    """
    return any(
        isinstance(node, dict)
        and str(node.get("stuck") or "").startswith(INVALID_GRAPH_REASON_PREFIX)
        for node in nodes
    )


def _unreadable_status(nodes: list[Any]) -> str | None:
    """The first journaled status word no ``DriverStatus`` member names, or
    None when every node row carries one this build can read.

    A snapshot written by a NEWER kew can carry an overlay status this one has
    never heard of, and the tempting reading is per-node: skip the row, report
    the rest. That is the failure this module already refuses one level up
    (:func:`_is_invalid_graph_snapshot`) reproduced one node down — a snapshot
    watch of an epic whose only waiting node carries that word emits nothing
    and exits 0, which an operator reads as "nobody is waiting", while the
    ``epic_holds`` row behind it stands. The live read knows the status from
    the overlay it computes itself, so it is the read that can answer.

    Version skew is not the only source: ``journal.build_snapshot`` writes a
    node's RAW TRACKER status where the overlay produced none for it (a
    terminally stuck node), and ``NodeStatus`` has one member ``DriverStatus``
    does not — ``ready``, which no dependency source actually assigns. So the
    worst a mapping gap costs is the live read, never a wrong answer.
    """
    for node in nodes:
        if not isinstance(node, dict) or node.get("external"):
            continue
        if _driver_status(node.get("status")) is None:
            return str(node.get("status"))
    return None


def _is_refused(nodes: list[Any]) -> bool:
    """Whether no attention at all may be composed off this snapshot.

    The two refusals the module docstring names, each saying so on its way out:
    an operator who pays for a live read is entitled to know why.
    """
    if _is_invalid_graph_snapshot(nodes):
        _logger.debug(
            "the last journaled tick of this epic reports an invalid graph, "
            "so it recorded no per-node observations; reading live instead"
        )
        return True
    unreadable = _unreadable_status(nodes)
    if unreadable is None:
        return False
    _logger.warning(
        "the last journaled tick of this epic records node status %r, which "
        "this kew has no member for, so no attention event can be composed "
        "for that node; reading live instead",
        unreadable,
    )
    return True


def _instant(value: str) -> datetime | None:
    """One journaled ISO-8601 stamp as a datetime, or None when it is not one.

    Naive stamps are read as UTC, exactly as ``run_store._iso_to_dt`` reads the
    rows written before the column was tz-aware. A stamp that is neither is
    NAMED and then not used to date anything (:func:`_last_recorded_at`): a
    reader that cannot age an observation must not be handed a stamp nobody
    can parse.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _logger.warning(
            "journaled epic instant %r is not ISO-8601, so it cannot date "
            "these observations",
            value,
        )
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def _last_recorded_at(tick_ts: str, driver_state: EpicDriverState | None) -> str:
    """When these observations were last RECORDED, not when they first were.

    ``epic_ticks`` is a change journal — ``append_epic_tick`` dedups on the
    snapshot hash — so a stable epic's latest row freezes at the instant its
    state first appeared and about nine ticks in ten leave no row at all. Aged
    by that stamp alone, a driver that ticked a minute ago and found nothing
    changed is indistinguishable from one that died days ago (#2476 review),
    which is the exact mistake ``observations_stale_since`` exists to prevent.

    ``epic_driver_state.last_tick_ts`` is written by every FINISHED tick, and a
    tick that finished after the latest journal row either appended that row or
    matched its hash — so where it is the later of the two it IS the instant
    these same observations were re-observed. (A tick that froze on an invalid
    graph journals a row of its own, which is refused before this is reached;
    one that raised, or was locked out, never reaches its heartbeat at all.)

    The journal row wins when IT is the later — a tick that journaled and then
    died before its heartbeat landed — so the stamp is never younger than the
    facts it dates. So does an unparseable heartbeat: a stamp nobody can age is
    worse than a conservative one.
    """
    heartbeat = driver_state.last_tick_ts if driver_state is not None else None
    if not heartbeat:
        return tick_ts
    recorded, beat = _instant(tick_ts), _instant(heartbeat)
    if recorded is None or beat is None or beat <= recorded:
        return tick_ts
    return heartbeat


def _read_nodes(
    nodes: list[Any],
) -> tuple[dict[str, DriverStatus], dict[str, NodeFacts], dict[str, str]]:
    """Every readable node row as statuses, facts and recorded reasons."""
    statuses: dict[str, DriverStatus] = {}
    node_facts: dict[str, NodeFacts] = {}
    reasons: dict[str, str] = {}
    for node in nodes:
        read = _read_node(node)
        if read is None:
            continue
        nid, status, facts, reason = read
        statuses[nid] = status
        node_facts[nid] = facts
        if reason:
            reasons[nid] = reason
    return statuses, node_facts, reasons


def read_journaled_observations(
    tick: EpicTick | None, *, driver_state: EpicDriverState | None = None
) -> JournaledObservations | None:
    """Read one journaled tick into the observations it recorded.

    ``driver_state`` is this epic's heartbeat row — one point read on the same
    connection — and it only dates the result: the observations themselves are
    the snapshot's (see :func:`_last_recorded_at`). Omitting it dates them by
    the journal row, which is correct but can only be too old.

    ``None`` when there is nothing to read — no tick, a snapshot whose shape
    this build does not recognize, or one of the two refusals
    (:func:`_is_refused`). The caller falls back to a live read there rather
    than reporting an epic as quiet on the strength of a row it could not read.
    """
    if tick is None:
        return None
    snapshot = tick.snapshot
    if not isinstance(snapshot, dict):
        return None
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or _is_refused(nodes):
        return None
    statuses, node_facts, reasons = _read_nodes(nodes)
    return JournaledObservations(
        recorded_at=_last_recorded_at(str(tick.tick_ts or ""), driver_state),
        signal=str(snapshot.get("signal") or ""),
        statuses=statuses,
        node_facts=node_facts,
        reasons=reasons,
        dependents=_dependents(snapshot.get("edges"), set(statuses)),
    )


def _read_node(node: Any) -> tuple[str, DriverStatus, NodeFacts, str] | None:
    """One journaled node as id, status, facts and recorded stuck reason.

    ``None`` for a row no event can be about: a malformed entry or an external
    boundary node (not an epic member, and carrying no facts of its own). A
    status word this build has no member for never reaches here — the snapshot
    carrying it is refused whole (:func:`_unreadable_status`).
    """
    if not isinstance(node, dict) or node.get("external"):
        return None
    nid = str(node.get("id") or "")
    status = _driver_status(node.get("status"))
    if not nid or status is None:
        return None
    reason = node.get("stuck")
    return (
        nid,
        status,
        _journaled_facts(node, nid),
        reason if isinstance(reason, str) else "",
    )


def _journaled_waiting_reason(
    nid: str,
    status: DriverStatus,
    observations: JournaledObservations,
    *,
    store: RunStore,
    holds_by_item: dict[str, Any],
    continuation_reason_by_item: dict[str, str],
) -> str | None:
    """The waiting line this journaled node earns, in the driver's own words.

    ``tick._waiting_reason``'s branches, minus the TODO one: a blocked TODO's
    reason is a dependency wait that needs nobody and the frontier it is
    computed from is not journaled (see ``_QUIET_STATUSES``).
    """
    if status is DriverStatus.IN_REVIEW:
        return _in_review_reason(nid, observations.node_facts, store)
    if status is DriverStatus.AWAITING_PUBLICATION:
        return _awaiting_publication_reason(
            nid, store, continuation_reason_by_item.get(nid)
        )
    if status in _FIXED_WAITING_REASONS:
        return _FIXED_WAITING_REASONS[status]
    if status is DriverStatus.HELD:
        return _journaled_held_reason(
            nid,
            holds_by_item=holds_by_item,
            continuation_reason_by_item=continuation_reason_by_item,
        )
    return None


def _journaled_held_reason(
    nid: str,
    *,
    holds_by_item: dict[str, Any],
    continuation_reason_by_item: dict[str, str],
) -> str:
    """Why this journaled ``held`` node is held, in ``tick._held_wait_reason``'s
    own order — with its merged-unclosed test reached by elimination.

    That test is on the PR's own state, which the snapshot does not carry. It
    does not have to: the overlay reaches HELD through a hold row, a wedged
    remediation (which also has one) or ``merged_unclosed``, and nothing else
    (``overlay._provider_or_hold_status``). A ``held`` node with no row in
    ``epic_holds`` and no continuation marker is therefore that third case —
    the merge pass closes its issue on the next tick, so nobody is waited on.
    """
    continuation = continuation_reason_by_item.get(nid)
    if continuation:
        return continuation
    hold = holds_by_item.get(nid)
    return hold.reason if hold is not None else MERGED_UNCLOSED_WAIT_REASON


def _journaled_identity(
    nid: str, status: DriverStatus, observations: JournaledObservations, store: RunStore
) -> str:
    """The identity that makes this node's state distinct (``#2183``).

    The driver's own ``tick`` functions, reading the journaled head rather than
    a fresh one — which is what a ``--follow`` watch's de-dup is keyed on, so
    the two paths must agree about it.
    """
    if status is DriverStatus.AWAITING_PUBLICATION:
        return _publication_identity(nid, store)
    return _review_identity(nid, observations.node_facts, store)


def journaled_attention_events(
    epic: str, observations: JournaledObservations, *, store: RunStore
) -> tuple[AttentionEvent, ...]:
    """Every state these recorded observations say needs a person.

    The journal's counterpart to :func:`kew.graph.driver.attention
    .attention_events`, and deliberately the same shape: a COMPLETE epic
    reports its completion and nothing else, and each item earns at most one
    event. The live read's waiting-before-stuck order is the one part it cannot
    copy — the snapshot keeps one reason per node, with no split to order by —
    so events come out in node order (see the module docstring).
    """
    if observations.signal == TerminalSignal.COMPLETE.value:
        return (
            AttentionEvent(
                kind=AttentionKind.EPIC_COMPLETE,
                epic=epic,
                item=epic,
                reason=EPIC_COMPLETE_REASON,
                action=EPIC_COMPLETE_HINT,
            ),
        )
    holds_by_item = _holds_by_item(store, epic)
    histories = _continuation_histories(store, epic)
    continuation_reason_by_item = _continuation_reason_map(store, epic, histories)
    events: dict[str, AttentionEvent] = {}
    # Plain string order: the key `tick._waiting_nodes` and the stuck rows
    # are sorted with, so the two reads cannot disagree about event order.
    for nid in sorted(observations.statuses):
        event = _node_event(
            epic,
            nid,
            observations,
            store=store,
            holds_by_item=holds_by_item,
            continuation_reason_by_item=continuation_reason_by_item,
            histories=histories,
        )
        if event is not None:
            events[nid] = event
    # Read now, not journaled: the warning is about the agent's token, which
    # the snapshot never recorded (#3329). The store alone decides, so a
    # watcher's own environment cannot hide it.
    warnings = active_runner_token_warnings(store)
    return (*events.values(), *token_warning_events(epic, warnings))


def _node_event(
    epic: str,
    nid: str,
    observations: JournaledObservations,
    *,
    store: RunStore,
    holds_by_item: dict[str, Any],
    continuation_reason_by_item: dict[str, str],
    histories: Mapping[str, ContinuationHistory],
) -> AttentionEvent | None:
    """The one event this journaled node earns, or None for nobody waited on.

    A recorded repeated-host-death reason is read against the CURRENT
    continuation history: a journal written before the preserved reason
    existed still says retry, and a stalled epic may never tick again to
    rewrite it (#2960).
    """
    status = observations.statuses[nid]
    recorded = preserved_host_death_reason(
        observations.reasons.get(nid, ""), histories.get(nid)
    )
    if not recorded and status.value in _QUIET_STATUSES:
        return None
    reason = recorded or _journaled_waiting_reason(
        nid,
        status,
        observations,
        store=store,
        holds_by_item=holds_by_item,
        continuation_reason_by_item=continuation_reason_by_item,
    )
    if reason is None:
        return None
    # The reading ``service.attention.node_wait`` takes off this very snapshot
    # field: the overlay status IS the hold table here, so a HELD node is held.
    # The reason test is what carries an aged hold that has left
    # ``summary.held`` — the same second chance ``attention._is_held`` gives it
    # — and NOT a substitute for the status test: every waiting reason is in
    # ``reasons`` too, so "has a recorded reason" says nothing about holding.
    held = status is DriverStatus.HELD or holds_node_out_of_frontier(reason)
    classified = classify_wait(reason, held=held)
    if classified is None:
        return None
    return AttentionEvent(
        kind=classified.kind,
        epic=epic,
        item=nid,
        reason=safe_hold_reason(reason),
        action=classified.action,
        blast_radius=observations.dependents.get(nid, ()),
        identity=_journaled_identity(nid, status, observations, store),
    )
