"""One source of truth for an epic's node states (#2392).

``kew epic status`` derives a node's state live from the tracker; the console's
``/api/epics`` reads it off the newest journaled snapshot. Those are two
readings of one fact, and on go-live-fixes they disagreed: the CLI reported
``done=6 remaining=0`` with signal ``complete`` while the Epics list served
``done=2 held=2 remediating=2`` with signal ``progressing`` — so an epic whose
every issue had closed in September showed as active with two held nodes, to
the console and to anything else reading that route.

The rule neither surface may re-interpret lives here:

    A node whose tracker item is CLOSED with a MERGED pull request has
    LANDED, and a landed node is ``done`` — whatever per-node row a tick left
    behind on it.

:func:`is_closed_with_merged_pr` is read by
:func:`kew.graph.driver.overlay.settled_status`, so a hold row nobody cleared
no longer outranks the tracker in the tick, in ``kew epic status``, or in the
snapshot the tick journals. :func:`settle_landed` and the two derivations below
are read by :mod:`kew.service.epics`, so a journal that stopped *before* the
merges still serves the same counts and the same signal, from the ledger
evidence serve does have. Fixing only the first leaves every already-stale
journal wrong forever; fixing only the second leaves two implementations of one
sentence, which is how one system starts telling one operator two stories.

Pure by design — no kew imports at all. The driver side lives in ``kew.graph``
and the console side in ``kew.service``, and the import-linter contract "epic
snapshots cross to serve/service as JSON, not imports" forbids an edge between
them; a leaf module both may import is what lets them share the rule instead of
each keeping a faithful-for-now copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

#: The journaled node status a landed node reads as.
DONE = "done"

#: Node states no further observation can move. ``cancelled`` is terminal but
#: NOT done: it still counts against ``remaining``, exactly as the driver's
#: own ``_done_and_remaining`` counts it.
TERMINAL_NODE_STATES = frozenset({DONE, "cancelled"})

#: The tick signal for an epic with nothing remaining — ``TerminalSignal.COMPLETE``
#: by value. Spelled as a string because this module imports no kew code;
#: ``tests/test_epic_node_states.py`` pins it against the enum.
COMPLETE_SIGNAL = "complete"

_CLOSED = "CLOSED"
_MERGED = "MERGED"

#: Prefix of the journaled ``stuck`` reason a tick writes on a node whose pull
#: request MERGED while its tracker item is still OPEN
#: (``kew.graph.driver.tick._held_wait_reason``, bucketed under the same word by
#: ``kew.waits.STALL_BUCKETS``). ``tests/test_epic_node_states.py`` pins it
#: against the reason the tick actually writes.
MERGED_UNCLOSED_REASON = "merged_unclosed"

#: The merge pass's own word for the same condition: it merged (or observed
#: merged) the pull request, then could not verify the tracker item closed, so
#: it journals ``stuck / merged-not-closed``
#: (``kew.graph.driver.merge``, which imports this name). One state, two
#: vocabularies — and this is the spelling that PERSISTS, because the tick's
#: facts-computed wait is recomputed every tick while a failed close leaves
#: this reason on the node until somebody closes the item.
MERGED_NOT_CLOSED_REASON = "merged-not-closed"

#: Every journaled ``stuck`` reason that says "PR merged, tracker item still
#: OPEN". Matched as prefixes: both are written with trailing prose.
MERGED_UNCLOSED_REASONS = (MERGED_UNCLOSED_REASON, MERGED_NOT_CLOSED_REASON)


def _upper(value: str | None) -> str:
    return str(value).strip().upper() if value else ""


def is_closed_with_merged_pr(item_state: str | None, pr_state: str | None) -> bool:
    """Has this node LANDED — tracker item closed, linked pull request merged?

    Both halves are required. A merged PR beside a still-open item is the
    merge pass mid-flight (``merged_unclosed``), not a landing; a closed item
    with no merged PR is a cancellation or a manual close, and whether that is
    ``done`` or ``cancelled`` is the provider's ``state_reason`` to say.
    """
    return _upper(item_state) == _CLOSED and _upper(pr_state) == _MERGED


def journaled_merged_unclosed(stuck: str | None) -> bool:
    """Did the driver journal this node as "PR merged, tracker item still OPEN"?

    True for EITHER vocabulary the driver writes it in
    (:data:`MERGED_UNCLOSED_REASONS`): the tick's facts-computed held wait, and
    the merge pass's ``merged-not-closed`` from a post-merge close that did not
    take. One condition, so one answer — reading only the tick's spelling let
    the longer-lived of the two through (#2392 review).

    The one reading in which the journal carries the second half of
    :func:`is_closed_with_merged_pr` and carries it NEGATED: the driver looked
    at the tracker on its latest tick and found the item open behind the merged
    pull request, which is why it journaled ``held`` rather than ``done``
    (precedence 3a, ``merged_unclosed``).

    A reader whose only evidence is that SOME pull request for the item merged
    — the run ledger's ``pr_state == "MERGED"``, which is all
    :mod:`kew.service.epics` can see without a tracker token — has therefore
    already been answered: the driver weighed that same merge against a live
    tracker read and declined to call it a landing. Promoting such a node to
    ``done`` would erase a stuck reason the tick does mean, and report finished
    an epic the CLI and the overlay both still hold (#2392 review).
    """
    reason = _upper(stuck)
    return any(reason.startswith(r.upper()) for r in MERGED_UNCLOSED_REASONS)


def settle_landed(
    states: Mapping[str, str], landed: AbstractSet[str]
) -> dict[str, str]:
    """*states* with every non-terminal node in *landed* read as ``done``.

    Node ids absent from *states* are ignored rather than added: *landed* is
    evidence about nodes, never a claim that a node exists. An empty *landed*
    returns the states unchanged, which is what keeps this a no-op — and the
    served counts byte-identical to the journal's — for every epic whose
    journal is current.

    An already-terminal state is left exactly as journaled, ``cancelled``
    included. The tick makes the same carve-out (``_closed_status`` reads the
    provider's ``state_reason``, so a cancelled item stays CANCELLED even
    behind a merged PR): a provider cancellation is the provider's to state,
    and the one thing this reconciliation must never do is promote it.
    """
    return {
        node_id: (
            DONE if node_id in landed and status not in TERMINAL_NODE_STATES else status
        )
        for node_id, status in states.items()
    }


def node_counts(states: Mapping[str, str]) -> dict[str, int]:
    """``status -> how many nodes hold it``.

    Counted over every node *states* carries, including the external boundary
    copies of another epic's nodes, because that is what the journal's own
    ``counts`` counts (``kew.graph.driver.journal.build_snapshot``) and a
    reconciliation must not quietly change the denominator as well.
    """
    counts: dict[str, int] = {}
    for status in states.values():
        counts[status] = counts.get(status, 0) + 1
    return counts


def member_states(
    states: Mapping[str, str], external: Iterable[str] = ()
) -> dict[str, str]:
    """*states* without the boundary nodes.

    *external* names out-of-epic dependencies, observed for dependency
    satisfaction only. They are not this epic's members, so they must not
    inflate either total read off them — the same exclusion
    ``_done_and_remaining`` makes in the tick.
    """
    excluded = set(external)
    return {nid: st for nid, st in states.items() if nid not in excluded}


def remaining_count(states: Mapping[str, str], external: Iterable[str] = ()) -> int:
    """How many of this epic's OWN nodes are not ``done``."""
    return sum(
        1 for status in member_states(states, external).values() if status != DONE
    )


def epic_signal(
    states: Mapping[str, str],
    journaled: str | None,
    *,
    external: Iterable[str] = (),
) -> str | None:
    """The signal these states call for, or *journaled* when they call for none.

    Only the COMPLETE arm is derived, and it is derived the way the tick
    derives it: ``remaining == 0`` is the whole of it
    (``_terminal_signal``). Every other arm — progressing, awaiting_merges,
    stalled — turns on live facts (a dispatch, an in-flight run, a frontier,
    a stall clock) that a journal does not carry, so the recorded reading
    stands rather than being guessed at from statuses alone.

    An epic with no MEMBER states — no nodes at all, or nothing but boundary
    copies of another epic's — gets the journaled reading too. Zero remaining
    out of zero read is not "every node is done"; it is the absence of a
    question, and the one way the derived arm could fire on an empty one.
    """
    members = member_states(states, external)
    if not members:
        return journaled
    if remaining_count(members) == 0:
        return COMPLETE_SIGNAL
    return journaled
