"""One operator-attention event stream for the epic driver (#2150).

Driving an epic used to mean watching five surfaces at once: a READY review
verdict showed up in the ledger, a stop-rule acceptance only as an
``awaiting merge:`` tick line, a spent remediation pool only as a
``Review skipped`` line, a sandbox refusal only as a transient ``Sandbox:``
line, and a held node only in ``kew epic status``. Every one of them means the
same thing — *the epic is waiting on a person* — so they get one stream:
:func:`attention_events` turns a read-only :class:`TickSummary` into at most one
:class:`AttentionEvent` per item, each naming the item and the action.

``STALL_BUCKETS`` is the single owner of the "waiting reason prefix -> operator
label + action" vocabulary. ``kew.cli.epic`` reads the same table for its
dominant-cause stall line, so the stall summary and the event stream cannot
disagree about what a reason means or what to do about it.

Scope: the stream reports nodes that are *waiting* on a person. Terminally
failed and cancelled nodes are deliberately not in it — they are already loud
(``failed:``/``cancelled:`` lines, a STALLED signal, and `kew epic run`'s
non-zero exit) and were not among the silent stalls this stream exists for.

It reads ``stuck`` as well as ``waiting``, because a stall clock expiring moves
a row between them: past ``epic.stall_days`` an in_review PR is re-reported as
``review-stalled`` and an aged hold as stuck (``tick._stale_review_stuck`` /
``tick._held_signal``). Nobody stopped waiting on a person there — the epic got
*worse* — so those rows keep earning the event their un-aged form earned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kew.epic_holds import (
    holds_node_out_of_frontier,
    safe_hold_reason,
)
from kew.graph.driver.result import TerminalSignal

# The waiting-reason vocabulary moved down to `kew.waits` (#2207) so the service
# layer's needs-you set can read the SAME table without importing `kew.graph`,
# which the import contract forbids. Re-exported here because this module was
# its home and `kew.cli.epic` (among others) still reads it from this name.
from kew.waits import (
    STALL_BUCKETS,
    UNBUCKETED_HELD_HINT,
    USAGE_LIMIT_WARNING_HINT,
    AttentionKind,
    StallBucket,
    WaitClassification,
    bucket_for,
    classify_wait,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kew.graph.driver.result import StuckNode, TickSummary
    from kew.runner_quota import RunnerQuotaWarning

__all__ = [
    "DRIVER_LOCK_ITEM",
    "EPIC_COMPLETE_HINT",
    "EPIC_COMPLETE_REASON",
    "STALL_BUCKETS",
    "UNBUCKETED_HELD_HINT",
    "AttentionEvent",
    "AttentionKind",
    "StallBucket",
    "WaitClassification",
    "attention_events",
    "bucket_for",
    "classify_wait",
    "driver_lock_stalled_action",
    "driver_lock_stalled_event",
    "token_warning_events",
]


#: What a finished epic asks of its operator.
EPIC_COMPLETE_REASON = "complete — every node is DONE"
EPIC_COMPLETE_HINT = "nothing is left to drive; close the epic out"

#: The item a stalled driver-lock holder's event names (#3331): the lock, not
#: a node — every node of the epic waits on it.
DRIVER_LOCK_ITEM = "driver-lock"


@dataclass(frozen=True)
class AttentionEvent:
    """One state that needs a person, naming the item and the action."""

    kind: AttentionKind
    epic: str
    item: str
    reason: str
    action: str
    blast_radius: tuple[str, ...] = ()
    #: What changes when this item's state does — the review head and round, or
    #: the run awaiting publication (see `TickSummary.identity`). Empty when the
    #: state carries no such evidence.
    identity: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The JSON-ready wire form (one object per event)."""
        return {
            "kind": self.kind.value,
            "epic": self.epic,
            "item": self.item,
            "reason": self.reason,
            "action": self.action,
            "blast_radius": list(self.blast_radius),
            "identity": self.identity,
        }

    def as_line(self) -> str:
        """One human line; the reason is already flattened to stay on it."""
        line = f"{self.kind.value} {self.item} — {self.reason} → {self.action}"
        return f"{line} [{self.identity}]" if self.identity else line

    @property
    def state_key(self) -> tuple[str, str, str, str]:
        """What makes this event a DISTINCT state, for a watcher's de-dup.

        Item, kind and reason are not enough: #2177 went READY at one head,
        failed the merge verify, took an operator fix and went READY again at
        another — same item, same kind, the same reason text, and the second
        (real) event was dropped by a wrapper keyed on those three. The
        identity is what separates them.
        """
        return (self.kind.value, self.item, self.reason, self.identity)


def _is_held(node: StuckNode, held: frozenset[str]) -> bool:
    """Whether this row is a hold an operator still has to clear.

    ``summary.held`` names the nodes the driver is holding now, but a hold past
    its stall clock is reported as stuck and leaves ``held`` — its reason is
    still an honored hold reason, and still that operator's job, so read it as
    one rather than letting the unbucketed fallback drop it.
    """
    return node.node_id in held or holds_node_out_of_frontier(node.reason)


def attention_events(epic: str, summary: TickSummary) -> tuple[AttentionEvent, ...]:
    """Every state of this epic that needs a person, at most one per item.

    A COMPLETE epic reports its completion and nothing else: there is no
    remaining work for a second event to be about. Otherwise each agent token
    near a usage limit is one more event, after the nodes'.
    """
    if summary.signal is TerminalSignal.COMPLETE:
        return (
            AttentionEvent(
                kind=AttentionKind.EPIC_COMPLETE,
                epic=epic,
                item=epic,
                reason=EPIC_COMPLETE_REASON,
                action=EPIC_COMPLETE_HINT,
            ),
        )
    held = frozenset(summary.held)
    events: dict[str, AttentionEvent] = {}
    # `waiting` first: its reason is the richer one (it names the PR, the cap,
    # the runner), so a stall relabel in `stuck` never overwrites it.
    for node in (*summary.waiting, *summary.stuck):
        classified = classify_wait(node.reason, held=_is_held(node, held))
        if classified is None or node.node_id in events:
            continue
        kind, action = classified.kind, classified.action
        events[node.node_id] = AttentionEvent(
            kind=kind,
            epic=epic,
            item=node.node_id,
            reason=safe_hold_reason(node.reason),
            action=action,
            blast_radius=node.blast_radius,
            identity=summary.identity.get(node.node_id, ""),
        )
    return (*events.values(), *token_warning_events(epic, summary.token_warnings))


def token_warning_events(
    epic: str, warnings: Iterable[RunnerQuotaWarning]
) -> tuple[AttentionEvent, ...]:
    """One event per agent token and limit it is near (#3329).

    The item is the runner and the limit, so two windows filling at once are
    two states a watcher remembers apart. The identity is the token's
    fingerprint, the limit and its reset, so a notice repeated within the same
    window is the same state; the reason leaves out the usage percentage for
    the same reason.
    """
    return tuple(
        AttentionEvent(
            kind=AttentionKind.USAGE_LIMIT_WARNING,
            epic=epic,
            item=warning.item,
            reason=warning.reason(usage=False),
            action=USAGE_LIMIT_WARNING_HINT,
            identity=warning.identity,
        )
        for warning in warnings
    )


def driver_lock_stalled_action(pid: int) -> str:
    """What clears a stalled holder. A stopped process acts on a plain ``kill``
    only once it is continued, so the kill named is ``-9``."""
    return (
        f"kill pid {pid} (`kill -9 {pid}`) and the next tick reclaims the lock; "
        f"if it was only stopped (Ctrl-Z), `kill -CONT {pid}` resumes it instead"
    )


def driver_lock_stalled_event(epic: str, *, pid: int, started: str) -> AttentionEvent:
    """The event for an epic whose driver lock is held by a live process that
    stopped beating its heartbeat (#3331). kew never takes the lock from a live
    process, so nothing moves until a person kills it.

    The reason names no clock, so a stall that stands across ticks is one state;
    the identity is the holder (pid and kernel start token), so a different
    stalled holder is a new event.
    """
    return AttentionEvent(
        kind=AttentionKind.HELD,
        epic=epic,
        item=DRIVER_LOCK_ITEM,
        reason=(
            f"driver lock held by pid {pid}, which is alive but has stopped "
            "beating its heartbeat (stopped, suspended or hung); kew never "
            "takes the lock from a live process"
        ),
        action=driver_lock_stalled_action(pid),
        identity=f"pid {pid} started {started}",
    )
