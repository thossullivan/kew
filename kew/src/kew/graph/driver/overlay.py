"""Driver status overlay: derive a richer per-node status from facts + runs.

The Phase 1 GitHub adapter (``kew.graph.adapters.github_convention``) never
emits FAILED/IN_PROGRESS and maps any CLOSED issue to DONE — it only has a
coarse, provider-native view. The Phase 2 driver owns a richer status by
combining the provider facts (``NodeFacts``, Task 5) with the local run-store
history and epic-level reservation/hold state (Task 2). This module does the
combining; it does not fetch anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from kew.epic_holds import (
    PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
    admission_refused_reason_prefix,
)
from kew.epic_holds import holds_node_out_of_frontier as _is_held
from kew.epic_node_states import is_closed_with_merged_pr
from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import NodeFacts
from kew.no_change import COMPLETED as _NO_CHANGE_COMPLETED
from kew.no_change import TERMINAL_STATES as _NO_CHANGE_TERMINAL_STATES
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    PR_SUBJECT_RUN_KINDS,
    REVIEW_REMEDIATION_RUN_KIND,
    TRIAGE_RUN_KIND,
    RunRecord,
)

# Persisted verdict strings the overlay reasons about (kew.review_contract owns
# the full parser). These three decide the review lifecycle state; the
# degenerate verdicts (MERGED_UNREVIEWED / PRE_POLICY / DEGRADED, later tasks)
# are attested facts, not live states the overlay derives. NEEDS_FULL
# (kew.review_delta, #2111) is a delta READY still owed a full round.
_VERDICT_READY = "READY"
_VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"
_VERDICT_NEEDS_FULL = "NEEDS_FULL"

_TERMINAL_FAILURE_STATUSES = frozenset(
    {"failure", "killed", "budget_exceeded", "budget_stopped", "budget_stop"}
)


def admission_refused_hold_reason(
    detail: str, item_id: str, *, head: str | None = None, action: str = ""
) -> str:
    """Build the durable operator hold for a run-less admission refusal.

    *head* scopes the hold to the PR head that was refused (#3193): the tick
    releases it once the head moves, so its advice says that first. *action*
    replaces the default advice.
    """
    unhold = f"`kew epic unhold <epic> {item_id}`"
    if not action:
        action = (
            "push a fix (the hold clears when the PR head moves), or un-hold "
            f"with {unhold}"
            if head
            else f"fix the cause, then un-hold with {unhold}"
        )
    return f"{admission_refused_reason_prefix(head)}: {detail} — {action}"


class DriverStatus(StrEnum):
    """Overlay-only status superset.

    TODO/QUEUED/IN_PROGRESS/IN_REVIEW/DONE/BLOCKED/FAILED mirror `NodeStatus`
    members of the same name (see `to_node_status`). AWAITING_PUBLICATION,
    AWAITING_CONTINUATION, CANCELLED, and HELD are overlay-only: completed
    work awaiting host publication, preserved dirty work awaiting an explicit
    continuation, a provider close with a non-COMPLETED `state_reason`, and an
    epic-level CI-pending hold, respectively; none has a `NodeStatus` member.
    """

    TODO = "todo"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    AWAITING_PUBLICATION = "awaiting_publication"
    AWAITING_CONTINUATION = "awaiting_continuation"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HELD = "held"
    # Governed adversarial-review stage (#977). Overlay-only, like HELD/CANCELLED
    # (no NodeStatus equivalent): a policy-covered node sits in ADVERSARIAL_REVIEW
    # while a fresh-context reviewer runs against the recorded head, and in
    # REMEDIATING while the adapted `kew review` path acts on NEEDS_CHANGES
    # findings. Both are non-dispatchable and never satisfy a dependent (they map
    # to the BLOCKED frontier sentinel in `tick.py`), exactly like IN_REVIEW.
    ADVERSARIAL_REVIEW = "adversarial_review"
    REMEDIATING = "remediating"
    # A zero-diff exit whose judgment has not settled (I10B). Overlay-only and
    # non-dispatchable, like AWAITING_PUBLICATION: the run is over, but what
    # its empty diff MEANT is still open, so the node is neither done nor work
    # to redo.
    AWAITING_NO_CHANGE = "awaiting_no_change"


_TO_NODE_STATUS: dict[DriverStatus, NodeStatus] = {
    DriverStatus.TODO: NodeStatus.TODO,
    DriverStatus.QUEUED: NodeStatus.QUEUED,
    DriverStatus.IN_PROGRESS: NodeStatus.IN_PROGRESS,
    DriverStatus.IN_REVIEW: NodeStatus.IN_REVIEW,
    DriverStatus.DONE: NodeStatus.DONE,
    DriverStatus.BLOCKED: NodeStatus.BLOCKED,
    DriverStatus.FAILED: NodeStatus.FAILED,
}


def to_node_status(ds: DriverStatus) -> NodeStatus:
    """Map the `DriverStatus` members shared with `NodeStatus`.

    Raises `ValueError` for CANCELLED/HELD, which have no `NodeStatus`
    equivalent. Callers that need the raw provider TODO for the dispatch
    frontier should read `NodeFacts`/the adapter directly, not this map.
    """
    try:
        return _TO_NODE_STATUS[ds]
    except KeyError:
        raise ValueError(
            f"DriverStatus.{ds.name} has no corresponding NodeStatus"
        ) from None


def _implementation_runs(runs: list[RunRecord]) -> list[RunRecord]:
    """Runs that derive node status: implementation kinds only (F4/#977).

    Governed-review runs (``adversarial_review`` / ``review_remediation``) are
    dropped before status derivation — a live reviewer must not read as the
    node being IN_PROGRESS, and a killed reviewer must not derive FAILED; the
    review crash table owns those outcomes. CI-fix runs are also dropped
    because they operate on an existing PR and do not replace the original
    implementation result. Reservations are already isolated via the
    ``review`` pseudo-epic (N2), so ``reserved`` needs no filtering.

    Triage runs are dropped for the same reason as a reviewer (#2293): a
    classifier READING an item must never read as the item being worked on, and
    a triage run that failed must not derive FAILED for a node nobody has
    started. Triage records its own outcome in ``triage_decisions``.
    """
    return [
        r
        for r in runs
        if r.run_kind not in PR_SUBJECT_RUN_KINDS and r.run_kind != TRIAGE_RUN_KIND
    ]


def _newest_run(runs: list[RunRecord]) -> RunRecord | None:
    """Return the run with the latest `started_at` (falling back to
    `finished_at` only when needed to break/avoid ties), or None if empty."""
    if not runs:
        return None
    return max(runs, key=lambda r: (r.started_at, r.finished_at or r.started_at))


def current_attempt_runs(
    runs: list[RunRecord], superseded_run_ids: frozenset[str]
) -> list[RunRecord]:
    """The node's implementation runs, less those a host-restart marker binds.

    Those runs are superseded by identity whatever the clocks say (#2969), so
    none stands for the node's newest outcome in any precedence check — a
    superseded run whose clock reads ahead would otherwise hide its
    replacement's pending, failed or preserved work.
    """
    return [r for r in _implementation_runs(runs) if r.run_id not in superseded_run_ids]


def _newest_terminal_run(runs: list[RunRecord]) -> RunRecord | None:
    """Return the newest run whose status is not "pending" (i.e. terminal)."""
    terminal = [r for r in runs if r.status != "pending"]
    return _newest_run(terminal)


def starts_fresh_work(runs: list[RunRecord]) -> bool:
    """Whether dispatching this node would START work rather than finish it.

    Human-review backpressure (I15) removes *fresh independent* frontier work
    while the delivery repository is saturated, and only that. A node this
    host has already implemented once — a retry after a failure, a resumed
    continuation, a node whose publication or review is already in flight — is
    not fresh: holding it back would strand work that has already consumed (or
    is already owed) a review slot, which is eviction wearing another name.

    Governed-review and CI-fix runs are filtered out first, exactly as
    everywhere else in this module. These runs do not establish the original
    implementation result.
    """
    return not _implementation_runs(runs)


def has_successful_open_pr(facts: NodeFacts, runs: list[RunRecord]) -> bool:
    """Whether local success and provider truth prove publication completed."""
    newest = _newest_run(_implementation_runs(runs))
    return (
        facts.pr_number is not None
        and newest is not None
        and newest.status == "success"
    )


def _effective_continuation_state(
    facts: NodeFacts, runs: list[RunRecord], continuation_state: str | None
) -> str | None:
    """Discard only a recovery handle disproved by publication evidence."""
    if continuation_state != "awaiting_continuation":
        return continuation_state
    return None if has_successful_open_pr(facts, runs) else continuation_state


def _is_in_progress(
    runs: list[RunRecord],
    *,
    reserved: bool,
    continuation_state: str | None = None,
) -> bool:
    if reserved:
        return True
    newest = _newest_run(runs)
    return continuation_state == "awaiting_continuation" or (
        newest is not None and newest.status == "pending"
    )


def _in_progress_status(continuation_state: str | None) -> DriverStatus:
    status_by_continuation: dict[str | None, DriverStatus] = {
        "awaiting_continuation": DriverStatus.AWAITING_CONTINUATION,
    }
    return status_by_continuation.get(continuation_state, DriverStatus.IN_PROGRESS)


def _failure_superseded(run: RunRecord, retried_at: datetime | None) -> bool:
    """True iff an operator retry (`retried_at`) supersedes this failure run.

    A retry marker only supersedes failures OLDER than it — a failure run
    started AFTER `retried_at` is a fresh failure from the retried attempt,
    not the stale one the operator cleared.
    """
    return retried_at is not None and retried_at > run.started_at


def _is_failed(
    runs: list[RunRecord], facts: NodeFacts, retried_at: datetime | None
) -> bool:
    if "agent-failed" in facts.labels:
        return True
    newest_terminal = _newest_terminal_run(runs)
    if newest_terminal is None:
        return False
    if newest_terminal.status not in _TERMINAL_FAILURE_STATUSES:
        return False
    return not _failure_superseded(newest_terminal, retried_at)


def _no_change_pending(no_change_state: str | None) -> bool:
    """Whether a recorded zero-diff judgment is still deciding what it meant.

    Every live state counts — candidate through effect_pending. Only
    ``completed`` and ``rejected`` are settled, and a settled judgment stops
    projecting: the item is then whatever the provider and the runs say it is.
    """
    return (
        no_change_state is not None
        and no_change_state not in _NO_CHANGE_TERMINAL_STATES
    )


def _closed_status(facts: NodeFacts) -> DriverStatus:
    if facts.state_reason == "COMPLETED":
        return DriverStatus.DONE
    return DriverStatus.CANCELLED


@dataclass(frozen=True)
class ReviewOverlay:
    """The governed-review facts the overlay needs to place an open-PR node in
    the adversarial-review lifecycle (Task 10, #977).

    Assembled by the driver tick from config + run store; ``None`` (or
    ``enabled=False``) means the review stage is off and the node follows the
    pre-existing IN_REVIEW path unchanged.

    - ``reserved_kind``: the run_kind of a LIVE review reservation on the node
      (``adversarial_review`` while the reviewer runs, ``review_remediation``
      while remediation runs), or ``None`` when no reviewer/remediator holds a
      slot. A live reservation is the authoritative "work in flight" signal.
    - ``round_count`` / ``latest_verdict`` / ``latest_head_sha``: the persisted
      review state for the current cycle (``run_store.latest_review_round`` /
      ``review_round_count``). ``round_count == 0`` means no reviewer has ever
      run for this node in this cycle.
    - ``current_head_sha``: the PR's live head SHA (``NodeFacts.head_oid``); a
      READY verdict bound to a stale head is void — the node re-enters review
      (F1 verdict-void-on-head-move).
    - ``observed_status``: the provider's status before the driver overlay. An
      observed ``IN_REVIEW`` is an enrollment fact: it covers PRs that entered
      review outside the driver's implementation lifecycle and have no persisted
      reviewer round yet.
    - ``residuals_accepted``: the review stop rule (#2112) accepted the latest
      ``NEEDS_CHANGES`` round as READY-with-residuals, with its residual issue
      filed and attested AND the acceptance marked on the round itself (#2151) —
      the merge gate's own reading (``acceptance_on_record``). A half-written
      acceptance is False here on purpose: the node stays in the review
      lifecycle, where the stage retries the mark, rather than reading READY to
      an overlay whose merge gate refuses it. It reads as READY while bound to
      the current head, and a moved head voids it back into review just as it
      voids a READY.
    """

    enabled: bool
    reserved_kind: str | None = None
    round_count: int = 0
    latest_verdict: str | None = None
    latest_head_sha: str | None = None
    current_head_sha: str | None = None
    observed_status: NodeStatus | None = None
    residuals_accepted: bool = False
    attempted: bool = False
    """A governed-review run was recorded for the node (#2147). A reviewer that
    ended without a parseable verdict writes no round, so zero rounds alone
    would read as PRE_POLICY — this says the node enrolled and still owes one."""


def _review_status(review: ReviewOverlay, facts: NodeFacts) -> DriverStatus | None:
    """Place an open-PR, review-enabled node in the adversarial-review lifecycle.

    Returns the review-lifecycle `DriverStatus`, or ``None`` to fall through to
    the pre-existing open-PR handling (IN_REVIEW) — used for the READY-and-
    current terminal state and for PRE_POLICY grandfathering.

    Rounds-exhausted → HELD is NOT derived here: the driver writes the
    ``review_rounds_exhausted`` hold (precedence 1) when it caps the cycle, and
    a crash/unparseable/budget outcome likewise routes through a review hold —
    this function only owns the in-flight lifecycle states.
    """
    # A live reviewer/remediator holds a slot: the run_kind names the state
    # directly, independent of what the last persisted verdict was.
    if review.reserved_kind == REVIEW_REMEDIATION_RUN_KIND:
        return DriverStatus.REMEDIATING
    if review.reserved_kind == ADVERSARIAL_REVIEW_RUN_KIND:
        return DriverStatus.ADVERSARIAL_REVIEW

    # No live review run. Decide from the persisted verdict.
    if review.round_count == 0:
        # Reconcile from the provider's observed state as well as the run store.
        # An operator-created PR (or one published after a driver failure) can
        # arrive already IN_REVIEW without ever acquiring a review reservation
        # or recording a round. Do not mistake that missing bookkeeping for a
        # pre-policy approval: the current head still needs a fresh reviewer.
        # A reviewer that already ran for this node without recording a round
        # (a pre-launch sandbox refusal deferred for a bounded retry, #2147; a
        # crashed launch) enrolled it just as visibly: it owes a verdict, and
        # the merge gate refuses to grandfather it, so it must stay in the
        # lifecycle where the next tick re-reviews it.
        if review.observed_status is NodeStatus.IN_REVIEW or review.attempted:
            return DriverStatus.ADVERSARIAL_REVIEW
        # No reviewer has ever run this cycle. Enrolment vs PRE_POLICY grandfather
        # (F9): a node a human has already APPROVED when review first applies is
        # grandfathered — flipping the flag never throws an approved PR back into
        # review. An un-approved fresh PR enrols and awaits its first reviewer.
        if facts.review_decision == "APPROVED":
            return None
        return DriverStatus.ADVERSARIAL_REVIEW
    return _persisted_verdict_status(review)


def _persisted_verdict_status(review: ReviewOverlay) -> DriverStatus | None:
    """The lifecycle status the latest persisted verdict calls for."""
    head_current = review.latest_head_sha == review.current_head_sha
    if review.latest_verdict == _VERDICT_READY:
        # READY is only live while bound to the current head. A post-verdict
        # force-push voids it → back to adversarial_review, round increment
        # owned by the driver (F1).
        return None if head_current else DriverStatus.ADVERSARIAL_REVIEW

    if review.latest_verdict == _VERDICT_NEEDS_CHANGES:
        # The stop rule accepted this round with its residuals filed (#2112):
        # READY for the approval surface while the head has not moved. A push
        # voids the acceptance exactly as it voids a READY, and the driver then
        # dispatches a fresh reviewer round on the new head — so report that,
        # not remediating, which is not what the node is doing.
        if review.residuals_accepted:
            return None if head_current else DriverStatus.ADVERSARIAL_REVIEW
        # Findings returned, remediation cap not yet reached → remediating.
        # At/over the cap the driver holds the node (review_rounds_exhausted);
        # until it does, surface it as still remediating rather than IN_REVIEW.
        return DriverStatus.REMEDIATING

    if review.latest_verdict == _VERDICT_NEEDS_FULL:
        # A review round that still owes an independent full round at this
        # head (a delta READY, or a first full READY or residual-only round on
        # a merge-gate-path head): that round is due.
        return DriverStatus.ADVERSARIAL_REVIEW

    # Any other persisted verdict (crash/unparseable placeholder) is owned by a
    # review hold; fall through until the driver writes it.
    return None


def _open_status(facts: NodeFacts, review: ReviewOverlay | None) -> DriverStatus:
    if "blocked" in facts.labels:
        return DriverStatus.BLOCKED
    # IN_REVIEW requires a PR that is actually open for the node. The
    # `pr-pending-review` issue label is only a hint, never the sole
    # authority: a closed-unmerged PR drops out of GitHub's
    # `closedByPullRequestsReferences` (so `pr_number` goes None) but leaves
    # the label behind on the issue, and honoring the label alone wedged
    # retried nodes as IN_REVIEW with no PR (#753).
    # The negative set preserves third-party fixtures that link a PR without
    # setting ``pr_state``. UNKNOWN is also linked and never dispatchable.
    pr_open = facts.pr_number is not None and facts.pr_state not in {"MERGED", "CLOSED"}
    if not pr_open:
        if facts.admission_status is NodeStatus.QUEUED:
            return DriverStatus.QUEUED
        return DriverStatus.TODO
    if facts.pr_state == "UNKNOWN":
        return DriverStatus.IN_REVIEW
    if review is not None and review.enabled:
        review_ds = _review_status(review, facts)
        if review_ds is not None:
            return review_ds
    return DriverStatus.IN_REVIEW


def _merged_unclosed(facts: NodeFacts) -> bool:
    """Precedence 3a: the PR merged but the item is still open (W2.6)."""
    return facts is not None and facts.pr_state == "MERGED" and facts.state != "CLOSED"


def _hold_stands(facts: NodeFacts, held_reason: str | None) -> bool:
    """Does precedence 1's hold decide this node, or does the provider?

    Not while the PR has merged ahead of the item's closure — precedence 3a
    owns that tick. And not once the node has LANDED (#2392): a hold keeps
    LIVE work out of the frontier, it is not a claim about an item the tracker
    has closed behind a merged PR, and nothing clears a hold after the node it
    stands on stops being dispatched. go-live-fixes journaled `held` on nodes
    whose PRs had merged in September, and every surface reading that journal
    called a finished epic active. The rule is shared with the console
    (:mod:`kew.epic_node_states`) so the two say it once.
    """
    if not _is_held(held_reason) or _merged_unclosed(facts):
        return False
    return not (
        facts is not None and is_closed_with_merged_pr(facts.state, facts.pr_state)
    )


def settled_status(
    facts: NodeFacts,
    held_reason: str | None = None,
    no_change_state: str | None = None,
) -> DriverStatus | None:
    """Precedences 1-3b of `overlay_status`: the provider- and hold-derived
    statuses that outrank every run-derived one. ``None`` hands over to the
    run-derived precedences 4-9, and so means "this node is still live work".

    Public because one gate outside the tick needs exactly this answer: `kew
    epic merge` (:mod:`kew.graph.driver.operator_merge`) must refuse a node the
    driver's own merge pass would never classify as a candidate — an item the
    provider has closed, or one whose zero-diff judgment has not settled — and
    deriving that from here is what keeps it true as these precedences change.
    """
    if _hold_stands(facts, held_reason):
        return DriverStatus.HELD
    # Ahead of provider closure on purpose (I10B): the zero-change path emits
    # zero remote writes, so a CLOSED seen next to a live judgment is not this
    # outcome landing — and reading it as DONE would hand a dependent the one
    # thing an unexplained empty diff must never buy it.
    if _no_change_pending(no_change_state):
        return DriverStatus.AWAITING_NO_CHANGE
    # A provider-closed item is terminal ground truth. In particular, a stale
    # host-publication overlay must not keep a manually completed item alive
    # forever while the next reconciliation retries its CAS update.
    if facts.state == "CLOSED":
        return _closed_status(facts)
    if _merged_unclosed(facts):
        return DriverStatus.HELD
    # A judgment only reaches `completed` after a person authorized it, every
    # authority ACKNOWLEDGED the synchronization, and every dependent it owed
    # an effect acknowledged one (I12). The item is terminal by then, so a
    # provider snapshot that has not caught up must not put the node back in
    # the frontier and re-run the work the absence already settled.
    if no_change_state == _NO_CHANGE_COMPLETED:
        return DriverStatus.DONE
    return None


def overlay_status(
    facts: NodeFacts,
    runs: list[RunRecord],
    *,
    reserved: bool,
    held_reason: str | None,
    retried_at: datetime | None = None,
    superseded_run_ids: frozenset[str] = frozenset(),
    review: ReviewOverlay | None = None,
    continuation_state: str | None = None,
    no_change_state: str | None = None,
    held_at: datetime | None = None,
) -> DriverStatus:
    """Compute the driver's overlay status for one node.

    Precedence (first match wins):
      1. `held_reason == "ci_pending"`, a governed-review hold reason
         (`review_crash` / `review_budget` / `review_unparseable` /
         `review_rounds_exhausted` / `review_pr_closed`, N3), or a
         `budget_preflight`-, `sandbox_containment`- or
         `admission_refused`-prefixed hold -> HELD
         (both are the non-destructive alternative to relabelling an epic node
         a preflight refused, #661/#1144 — they keep the node out of the
         dispatch frontier until the hold clears: automatically once the cap is
         raised for a budget hold, on an operator `kew epic unhold` for a
         containment one). A LANDED node — tracker item CLOSED behind a MERGED
         PR, :func:`kew.epic_node_states.is_closed_with_merged_pr` — is the one
         exemption: a hold governs live work, and nothing clears one once its
         node stops being dispatched, so precedence 3 decides it instead
         (#2392).
      2. a live no-change judgment (`no_change_state`, I10B) ->
         AWAITING_NO_CHANGE. A zero-diff exit writes nothing to the provider,
         so a provider CLOSED observed alongside a live judgment was closed by
         something other than this outcome; the local judgment is the fresher
         truth and outranks it until it settles. Non-dispatchable and never a
         raw DONE, so nothing about the absence of work — not a PR, not a
         merge, not a dependent's eligibility — is credited before a person
         (I11) says what it meant.
      3. `state == "CLOSED"` -> DONE (state_reason COMPLETED) or CANCELLED
         (any other/None state_reason). A provider-closed item is terminal
         ground truth, so it outranks every run-derived status below — and it
         outranks a completed no-change too, so a provider cancellation is
         never overwritten by one.
      3a. the linked PR is `MERGED` while the item is still OPEN -> HELD
         with the facts-computed reason `merged_unclosed` (W2.6). The merge
         pass closes the item during this tick.
      3b. a `completed` no-change judgment (I12) -> DONE. Completion is the
         only no-change state that means the outcome LANDED: authorized,
         acknowledged by every authority, and acknowledged by every dependent
         it owed an effect. Nothing weaker gets here — `authorized` and
         `sync_pending` are still live and were caught by precedence 2.
      4. the newest implementation run is `awaiting_publication` ->
         AWAITING_PUBLICATION (completed work is preserved but cannot satisfy
         dependents or re-enter the frontier)
      5. preserved dirty work is awaiting explicit continuation ->
         AWAITING_CONTINUATION, unless a successful implementation run and an
         open PR prove the publication continuation is stale
      5a. `reserved` with a `preexisting_worktree_refused`-prefixed hold ->
         HELD. A reserved node never reaches the frontier, so that hold there
         is the reconciler's own: kew could not prove what the dead run's
         checkout holds and keeps the reservation over it (#2971). Read as
         live work it would sit IN_PROGRESS forever — unless the newest
         implementation run is pending and started after the hold was written
         (`held_at`): that run is the dispatch which disproved the refusal,
         and a driver killed before it cleared the hold must not read
         executing work as a person's turn. See :func:`_reservation_refused`.
      6. `reserved` or the newest *implementation-kind* run is "pending"
         -> IN_PROGRESS (governed-review runs are filtered out first, F4)
      7. the newest *terminal* implementation run is a failure kind, or the
         "agent-failed" label is present -> FAILED (newest-run-wins: a later
         success supersedes a stale failure). A non-None `retried_at` NEWER than
         that failure run's `started_at` supersedes it too (the operator's
         non-destructive `kew epic retry` marker, #427 Phase 2) — the node
         falls through to precedence 8-9 instead of reporting FAILED. A
         failure run started AFTER `retried_at` is unaffected: it is a
         fresh failure, not the superseded stale one. The runs in
         `superseded_run_ids` (a host-restart marker bound to the run it
         reaped and to those an earlier retry superseded, #2969) are
         superseded whatever the clocks say: they are dropped before every
         precedence check, so the node's newest run, pending run and terminal
         run are all read from its other runs.
      8. "blocked" label -> BLOCKED; a PR actually open for the node
         -> IN_REVIEW (the "pr-pending-review" label is only a hint and
         never the sole authority — a stale label from a closed-unmerged
         PR must not wedge a retried node, #753). When `review` is enabled
         (governed adversarial-review stage, #977) the open-PR node is placed
         in the review lifecycle instead — ADVERSARIAL_REVIEW / REMEDIATING —
         until an evidenced READY verdict bound to the current head flips it to
         IN_REVIEW (see `_review_status`; PRE_POLICY-grandfathered approved PRs
         stay IN_REVIEW).
      8a. a REMEDIATING node from precedence 8 carrying ANY hold, with no live
         remediation run -> HELD. The remediator refuses while a hold stands,
         so this is `remediating` with no hold-free path — the silent stall
         #2217 is about. See :func:`_remediation_wedged`.
      9. Linear facts without both explicit admission markers -> QUEUED;
         otherwise TODO
    """
    settled = settled_status(facts, held_reason, no_change_state)
    if settled is not None:
        return settled
    impl_runs = current_attempt_runs(runs, superseded_run_ids)
    newest = _newest_run(impl_runs)
    if newest is not None and newest.status == "awaiting_publication":
        return DriverStatus.AWAITING_PUBLICATION
    continuation_state = _effective_continuation_state(
        facts, impl_runs, continuation_state
    )
    if _reservation_refused(
        reserved=reserved, held_reason=held_reason
    ) and not _dispatched_since_hold(impl_runs, held_at):
        return DriverStatus.HELD
    if _is_in_progress(
        impl_runs, reserved=reserved, continuation_state=continuation_state
    ):
        return _in_progress_status(continuation_state)
    if _is_failed(impl_runs, facts, retried_at):
        return DriverStatus.FAILED
    status = _open_status(facts, review)
    if status is DriverStatus.REMEDIATING and _remediation_wedged(review, held_reason):
        return DriverStatus.HELD
    return status


def _reservation_refused(*, reserved: bool, held_reason: str | None) -> bool:
    """Is this reservation held by the reconciler's checkout refusal (5a)?

    The frontier writes the same reason on a node it refused to dispatch, and
    there it is deliberately not honored: leaving the node dispatchable is the
    recovery (:mod:`kew.epic_holds`). A RESERVED node is past the frontier, so
    that recovery no longer exists for it — the hold was written by
    ``tick._hold_unreconcilable_reservation`` over a dead run whose work could
    not be accounted for, and nothing will move the node until a person does.
    A dispatch that disproves a frontier refusal clears it in the same pass.
    """
    return (
        reserved
        and held_reason is not None
        and held_reason.startswith(PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX)
    )


def _dispatched_since_hold(runs: list[RunRecord], held_at: datetime | None) -> bool:
    """Did a dispatch start after the refusal hold was written (5a)?

    The reconciler writes its hold over a run that had already started, so
    that run never outranks it. A pending run that started AFTER the hold is
    a later dispatch: the frontier clears the refusal it disproved only once
    the dispatch returns, and a driver killed in between leaves the stale row
    beside a live child (#2971). A hold of unknown age proves nothing either
    way and keeps reading HELD.
    """
    newest = _newest_run(runs)
    return (
        held_at is not None
        and newest is not None
        and newest.status == "pending"
        and newest.started_at > held_at
    )


def _remediation_wedged(review: ReviewOverlay | None, held_reason: str | None) -> bool:
    """Is this REMEDIATING node's only forward action blocked by a hold (#2217)?

    Precedence 8a. Remediation is the one thing that moves a REMEDIATING node,
    and the remediator refuses to run while ANY hold stands on it
    (:mod:`kew.graph.driver.review_launcher`). So a hold precedence 1 did not
    match — the driver's own ``credential_issuer_refused`` /
    ``preexisting_worktree_refused`` refusal, left behind by a dispatch that
    later succeeded — leaves the node reading live work with nothing able to
    advance it: #2201/#2202 sat there for over two hours while every tick
    printed ``signal=progressing``, ``kew epic status`` said ``remediating``
    and ``kew epic watch`` emitted nothing at all. Report the hold instead, so
    the surfaces that can clear it say so.

    A LIVE remediation run outranks the hold: ``reserved_kind`` names real work
    in flight, and whether its successor is refused is the next tick's
    question, not this status's.

    HELD alone was not enough to end the silence: the tick's signal counted any
    hold inside its stall clock as a live wait, so the epic went on reporting
    ``signal: progressing`` over this status. ``tick._hold_is_a_live_wait`` is
    the other half of the same answer.
    """
    return (
        held_reason is not None
        and review is not None
        and review.reserved_kind != REVIEW_REMEDIATION_RUN_KIND
    )
