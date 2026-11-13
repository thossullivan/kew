"""Tick result types for the Epic DAG Phase 2a driver.

`advance()` (see `kew.graph.driver.tick`) returns one `TickResult` per tick.
The tri-state `TerminalSignal` is the answer to "should the poll wrapper
(`kew epic run`) keep looping?" — see the spec's "Termination" section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from kew.graph.driver.frontier import FrontierRank
from kew.runner_quota import RUNNER_QUOTA_HELD_PREFIX, RunnerQuotaWarning

#: Reason prefix a tick stamps on a READY node an admission limiter (per-epic
#: worker budget, fleet worker cap, or this epic's fair share of it) kept from
#: dispatching — see ``_DispatchBudget.admission_hold_reason``.
ADMISSION_HELD_PREFIX = "admission-held"

#: Reason prefix for a READY node human-review backpressure withheld — see
#: ``ReviewPressure.reason``.
REVIEW_BACKPRESSURE_PREFIX = "review-backpressure"

#: The reasons that mean "this node WAS ready and the tick deliberately did not
#: start it". A progressing tick prints these (#2094): an epic trickling one
#: node per tick under a limiter must say which limiter, not go quiet until it
#: eventually stalls.
WITHHELD_REASON_PREFIXES = (
    ADMISSION_HELD_PREFIX,
    REVIEW_BACKPRESSURE_PREFIX,
    RUNNER_QUOTA_HELD_PREFIX,
)


class TerminalSignal(StrEnum):
    """The tick's tri-state verdict on whether the epic is making progress.

    COMPLETE: every node is DONE.
    PROGRESSING: something moved (a dispatch, a live in-flight run) or a
      HELD-on-`ci_pending` node is still within its stall clock — an
      approved PR waiting on CI is legitimate progress, not a stall.
    AWAITING_MERGES: the frontier is empty and nothing is in-flight, but the
      remaining work is one or more PRs open in review awaiting a human's
      merge (and nothing else is stuck) — the normal human-gate end state,
      NOT a broken pipeline. A human merging the pending PRs resumes the epic
      with no operator repair. Distinct from STALLED so wrappers can tell a
      benign merge-wait from a genuine no-progress-possible frontier (Node I7).
    STALLED: the frontier is empty, nothing is in-flight, no hold is
      "live", and not everything is DONE — remaining work is
      FAILED/CANCELLED/stuck and needs an operator.
    """

    COMPLETE = "complete"
    PROGRESSING = "progressing"
    AWAITING_MERGES = "awaiting_merges"
    STALLED = "stalled"


@dataclass(frozen=True)
class StuckNode:
    """One node the tick could not make progress on, with its blast radius."""

    node_id: str
    reason: str
    blast_radius: tuple[str, ...]


@dataclass(frozen=True)
class TickResult:
    """The full report of one `advance()` tick."""

    dispatched: tuple[str, ...]
    merged: tuple[str, ...]
    held: tuple[str, ...]
    in_flight: tuple[str, ...]
    failed: tuple[str, ...]
    cancelled: tuple[str, ...]
    stuck: tuple[StuckNode, ...]
    done: tuple[str, ...]
    remaining: int
    signal: TerminalSignal
    locked_out: bool = False
    # Non-terminal nodes that are not making progress (IN_REVIEW awaiting
    # merge, BLOCKED, or HELD) with a human reason line — the "why" behind a
    # STALLED tick (#661). Additive; empty on a healthy tick.
    waiting: tuple[StuckNode, ...] = ()
    # Nodes this tick observed already-merged out-of-band and did terminal
    # bookkeeping for (Linear `tracker.close`, GitHub `Closes #N` auto-close):
    # kew didn't merge them (so they're not in `merged`), but they ARE now
    # terminal-done and are patched to DONE in the tick's own post-action
    # summary/journal (SFD-139). Additive; empty on a normal tick.
    closed: tuple[str, ...] = ()
    # GitHub GraphQL points the tick's reads spent, as the readers metered them
    # (#2582); None when no reader meters itself (a Linear epic, a test double).
    graphql_points: int | None = None
    # Why each dispatched node went when it did — its priority and blast
    # radius — in the order the frontier chose (#2575). Additive.
    dispatch_ranks: tuple[FrontierRank, ...] = ()
    # `(node, why)` for each node whose head went to review without the
    # pre-review gate checking it (#3280). Additive; empty when every
    # configured gate ran and passed.
    gate_skipped: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PrLink:
    """How one node was linked to its PR (W2.6/#1741).

    `source` is "ledger" (the PR URL kew persisted at publication),
    "closing-ref" (GitHub's `Closes #N` keyword reference), or "branch" (an
    open or merged PR on the node's branch). `has_closing_ref` separately
    reports whether GitHub observed a closing keyword for the chosen PR.
    """

    node_id: str
    pr_number: int
    source: str
    has_closing_ref: bool | None


@dataclass(frozen=True)
class TickSummary:
    """A read-only per-node status snapshot (see `kew.graph.driver.tick.summarize`).

    `kew epic status` uses this instead of `TickResult` — it shares the same
    overlay/frontier/stuck-detection logic as one `advance()` tick, but never
    dispatches, merges, or mutates the run-store: `dispatched`/`merged` have
    no equivalent here on purpose.
    """

    counts: dict[str, int]  # DriverStatus.value -> count
    frontier: tuple[str, ...]
    in_flight: tuple[str, ...]
    held: tuple[str, ...]
    failed: tuple[str, ...]
    cancelled: tuple[str, ...]
    stuck: tuple[StuckNode, ...]
    done: tuple[str, ...]
    remaining: int
    signal: TerminalSignal
    # See `TickResult.waiting` — the non-progressing nodes with their reasons,
    # so `kew epic status` can explain a stall (#661). Additive.
    waiting: tuple[StuckNode, ...] = ()
    # Per-node PR linkage provenance, for every node that has a PR. Additive.
    pr_links: tuple[PrLink, ...] = ()
    # Per-node state identity: the evidence that CHANGES when the node's state
    # does — the review head and round for a node in review, the run id for one
    # awaiting publication (#2183). `kew epic watch` carries it on every event
    # so a watcher can tell a repeat of one state from a second, real one; a
    # node with no such evidence is simply absent. Additive.
    identity: dict[str, str] = field(default_factory=dict)
    # Every node's `DriverStatus.value` as this snapshot read it, so a reader
    # (`kew epic status --json`, #2504) can list each node's status rather
    # than infer it from the bucket lists. Empty for an invalid graph. Additive.
    statuses: dict[str, str] = field(default_factory=dict)
    # The provider usage warnings that still apply, for every known runner's
    # agent token and not only the runners this epic dispatches to (#3329).
    # Nothing is held for them; they are said before the refusal stalls the
    # epic. Additive.
    token_warnings: tuple[RunnerQuotaWarning, ...] = ()
