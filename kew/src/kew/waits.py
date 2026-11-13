"""The "what is this epic waiting on, and who moves it" vocabulary (#2207).

One table owns the mapping from a waiting reason — or, where the driver wrote
none down, a node's own status word — to an operator label, an action, the kind
of attention it is, and the command template that clears it. It lives here, at
the top level, rather than in ``kew.graph.driver.attention`` where it was born,
because two layers that must never disagree about it cannot both reach into
``kew.graph``:

* the driver's attention stream (:mod:`kew.graph.driver.attention`), which reads
  a live :class:`~kew.graph.driver.result.TickSummary`, and
* the service layer's needs-you set (:mod:`kew.service.needs_you`), which reads
  the journaled snapshot the driver left behind.

The import contract in ``pyproject.toml`` ("epic snapshots cross to serve/service
as JSON, not imports") forbids the second from importing the first, and a second
copy of this table is how the header comes to say 16 while an epic legend says 0.
So the table moved down to a module both may import; the driver module re-exports
every name so its own callers are unchanged.

``kew.epic_holds`` is the only kew module imported here, and it has no kew
imports of its own — this module is a leaf by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from kew.epic_holds import (
    ACCEPTANCE_DISPUTED_HOLD_REASON,
    ADMISSION_REFUSED_HEAD_HOLD_PREFIX,
    ADMISSION_REFUSED_HOLD_PREFIX,
    BUDGET_HOLD_PREFIX,
    CONTAINMENT_HOLD_PREFIX,
    CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
    DECLARATION_RECORD_FAILED_HOLD_PREFIX,
    MERGE_CLAIMED_WAIT_PREFIX,
    MERGE_GATE_INTEGRITY_PART,
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    MERGE_GATE_UNCHECKED_PART,
    PR_BODY_UNREADABLE_HOLD_PREFIX,
    PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX,
    PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
    PUBLICATION_HELD_HOLD_PREFIX,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    RESOURCE_RETRIES_SPENT_HOLD_PREFIX,
    REVIEW_BUDGET_HOLD_PREFIX,
    REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX,
    REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX,
    REVIEW_LAUNCH_FAILED_HOLD_PREFIX,
    REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX,
    REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
    REVIEW_RATE_LIMITED_WAIT_PREFIX,
    REVIEW_RETRYING_WAIT_PREFIX,
    REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
    ROUNDS_EXHAUSTED_HOLD_REASON,
    TEST_DECLARATIONS_HOLD_PREFIX,
    WINDOW_BUDGET_HOLD_PREFIX,
    admission_refused_head,
    is_ci_pending_hold,
    merge_gate_parts,
)


class AttentionKind(StrEnum):
    """The states that need a person, in the stream's own vocabulary.

    Readers must tolerate an unknown kind: the vocabulary is additive.
    """

    MERGE_READY = "merge_ready"
    HELD = "held"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PUBLICATION_HELD = "publication_held"
    SANDBOX_REFUSED = "sandbox_refused"
    #: A node the host restarted under twice (#2812). Nothing holds it — it
    #: failed — and a person decides whether to retry it (#2971).
    HOST_RESTART_REPEATED = "host_restart_repeated"
    #: An agent token near a provider usage limit (#3329). Nothing is held
    #: yet; a person decides whether to replace the token before it is refused.
    USAGE_LIMIT_WARNING = "usage_limit_warning"
    EPIC_COMPLETE = "epic_complete"


#: What a token usage warning asks of its operator (#3329).
USAGE_LIMIT_WARNING_HINT = (
    "replace the token before the limit refuses it, "
    "or dispatches and reviews wait for the reset"
)


#: The commands a bucket can name, as templates over the two tokens a row
#: fills: ``{epic}`` (an ``epic_ref``) and ``{item}`` (a durable node id). They
#: are templates and not sentences because the only useful form of "resume with
#: `kew epic continue`" is the one an operator can paste — see
#: :func:`fill_command`, which is the ONLY thing that fills them.
EPIC_UNHOLD_COMMAND = "kew epic unhold {epic} {item}"
EPIC_CONTINUE_COMMAND = "kew epic continue {epic} {item}"
EPIC_ABANDON_COMMAND = "kew epic abandon {epic} {item}"
EPIC_RETRY_COMMAND = "kew epic retry {epic} {item}"
EPIC_RUN_COMMAND = "kew epic run {epic}"

#: Why a run was reaped when the host restarted under it (#2812): the reason
#: on its reap and on the retry marker that re-admits its node once.
HOST_RESTART_REASON = "host_restart"
#: The reaped run's category the first time the host dies under a node — the
#: node is re-admitted, and the run never counts as its failure.
HOST_RESTART_REAPED = "host_restart_reaped"
#: The category the second time: the node fails, and a person is asked.
HOST_RESTART_REPEATED = "host_restart_repeated"
#: The failed-terminal reason that run leaves (``tick._failed_terminal_reason``:
#: a reaped run's exit code is always 137, kill -9).
HOST_RESTART_REPEATED_REASON = f"failed-terminal (exit 137, {HOST_RESTART_REPEATED})"
#: The same failure when the node still keeps a preserved continuation that
#: ``kew epic continue`` can resume (#2960): retry would record a marker and
#: dispatch past the retained bytes, so this reason names continue instead.
HOST_RESTART_REPEATED_PRESERVED_REASON = (
    f"failed-terminal (exit 137, {HOST_RESTART_REPEATED}, preserved work)"
)
#: The same failure when the node keeps more than one such continuation and
#: the continue selector refuses the history as ambiguous: abandon refuses it
#: the same way, so this reason offers no command at all (#2960).
HOST_RESTART_REPEATED_AMBIGUOUS_REASON = (
    f"failed-terminal (exit 137, {HOST_RESTART_REPEATED}, ambiguous preserved work)"
)
#: The same failure once its preserved work was taken up after the tick that
#: journaled it: a continuation owns it, or abandon re-admitted the node, or
#: the continuation completed. Nobody is asked, and no command is offered —
#: continue and retry both refuse an owned history, and an abandoned one is
#: already re-admitted (#2960).
HOST_RESTART_REPEATED_TAKEN_UP_REASON = (
    f"failed-terminal (exit 137, {HOST_RESTART_REPEATED}, preserved work taken up)"
)
#: Every reason a node the host died under twice can read, whichever history
#: the tick that journaled it saw. A reader normalizes any of them against the
#: CURRENT history (``kew.continuation_recovery.preserved_host_death_reason``).
HOST_RESTART_REPEATED_REASONS = frozenset(
    {
        HOST_RESTART_REPEATED_REASON,
        HOST_RESTART_REPEATED_PRESERVED_REASON,
        HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
        HOST_RESTART_REPEATED_TAKEN_UP_REASON,
    }
)

#: The waiting reason of a node whose continuation history the continue
#: selector refuses as ambiguous: more than one preserved continuation, or one
#: awaiting beside a terminal one from another run. Continue and abandon both
#: refuse it, so the row offers no command (#2960). It does not start with
#: ``awaiting_continuation``, whose bucket offers continue.
CONTINUATION_AMBIGUOUS_REASON = (
    "ambiguous_continuation — more than one preserved continuation is recorded; "
    "continue and abandon refuse it until the history is resolved"
)
#: The audit event a host death under DIRTY work leaves (#2964). It counts
#: toward the node's once-only host-death budget exactly as a clean
#: re-admission's ``host_restart`` retry does. Its ``outcome`` says what the
#: driver did with the preserved checkout: :data:`HOST_RESTART_CONTINUE` arms
#: one automatic continuation, :data:`HOST_RESTART_REPEATED` leaves it for a
#: person.
HOST_RESTART_DIRTY_EVENT = "epic_node_host_restart"
HOST_RESTART_CONTINUE = "continue"
#: The waiting reason of preserved work the host restarted under twice: a
#: person continues it, since the automatic continuation was spent.
HOST_RESTART_REPEATED_CONTINUATION_REASON = (
    "awaiting_continuation — the host restarted under this node twice"
)

#: The driver's whole waiting reason for a node no person has armed
#: (``DriverStatus.QUEUED``). Owned here so the tick that writes it and the
#: bucket that reads it are one string — the console's own copy,
#: ``ARM_WAIT_REASON`` in frontend/src/lib/epic/waiting.ts, is pinned to it by
#: ``tests/test_needs_you_merged_and_stalled.py``.
ARM_WAIT_REASON = "waiting for human arm"
#: The arm marker Linear admission requires (``_LINEAR_READY_LABEL`` in
#: graph/adapters/linear_native.py, pinned to this by the same test).
ARM_LABEL = "kew:ready"
#: ``TerminalSignal.STALLED`` by value — the driver stopped, nothing will move
#: until a person acts and a driver runs again. Spelled as a string because
#: this module imports no ``kew.graph``; pinned to the enum by
#: ``tests/test_needs_you_merged_and_stalled.py``.
STALLED_SIGNAL = "stalled"


@dataclass(frozen=True)
class StallBucket:
    """One waiting-reason prefix, its operator label, and what to do about it.

    ``kind`` is the attention kind the reason maps to, or ``None`` when no
    person is waited on — a dependency wait or a state the next tick clears by
    itself is a stall *cause* without being anybody's job.

    ``command`` is the template above that clears this reason, and ``""`` where
    no kew command does: raising a cap is a config edit, and a merge decision
    happens on the host. Empty is the honest answer there — a surface with no
    command to print says nothing rather than naming a verb that does not exist
    (#2244: a row read "resume with `kew epic resume ...`", which is not a
    command this CLI has ever had).
    """

    prefix: str
    label: str
    hint: str
    kind: AttentionKind | None
    command: str = ""


#: Order is significant: the first matching prefix wins.
STALL_BUCKETS: tuple[StallBucket, ...] = (
    StallBucket(
        "in_review",
        "in_review awaiting merge",
        "merge to resume",
        AttentionKind.MERGE_READY,
    ),
    # An in-review PR whose head is not the one its newest round reviewed
    # (#2561). The next review round is kew's move, so nobody is waited on —
    # and a merge-ready row here would offer a commit no verdict covers.
    StallBucket(
        "review pending",
        "review pending on a new head",
        "kew reviews the new head on a coming tick",
        None,
    ),
    StallBucket(
        "merged_unclosed",
        "merged PR with an open issue",
        "the merge pass closes the issue on this tick",
        None,
    ),
    # Preserved work the host restarted under twice (#2964): the dirty twin
    # of the repeated-restart bucket below. The checkout still holds the
    # work, so the command is the one that resumes it, not a fresh retry.
    StallBucket(
        HOST_RESTART_REPEATED_CONTINUATION_REASON,
        "the host restarted under this node twice",
        "check why the host keeps going down, then kew epic continue the node",
        AttentionKind.HELD,
        command=EPIC_CONTINUE_COMMAND,
    ),
    StallBucket(
        "awaiting_continuation",
        "awaiting preserved-work continuation",
        "kew epic continue <name> <node>",
        AttentionKind.HELD,
        command=EPIC_CONTINUE_COMMAND,
    ),
    StallBucket(
        CONTINUATION_AMBIGUOUS_REASON,
        "preserved-work history is ambiguous",
        "resolve the continuation history before continuing or abandoning it",
        AttentionKind.HELD,
    ),
    StallBucket(
        "awaiting_publication",
        "awaiting host publication",
        "restore GitHub access or open the preserved branch PR",
        AttentionKind.PUBLICATION_HELD,
    ),
    # A remediation whose own publication was deferred (#2362). Distinct from
    # the `awaiting_publication` row above: that one is an implementation run
    # the host could not push at all, this one is a review remediation the host
    # HELD for a person because its diff touched a protected path. Both wait on
    # the same kind, because what a person does about either is the same —
    # look at the preserved checkout the reason names and decide.
    StallBucket(
        PUBLICATION_HELD_HOLD_PREFIX,
        "held on a deferred remediation publication",
        "review the preserved checkout its reason names, then kew epic unhold",
        AttentionKind.PUBLICATION_HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # A declaration-only round whose remediation declared nothing (#2872): a
    # person adds the Behavior changes lines its reason names to the PR body.
    StallBucket(
        TEST_DECLARATIONS_HOLD_PREFIX,
        "held until the edited tests are declared",
        "add a Behavior changes line per named test to the PR body, then "
        "kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # An ordinary round whose remediation changed nothing (#3233): another one
    # would end the same way, so a person reads the findings its reason names.
    StallBucket(
        REMEDIATION_NO_CHANGE_HOLD_PREFIX,
        "held: a remediation changed nothing and the findings still stand",
        "read the findings, fix the branch or answer them, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # A remediation at a head pushed nothing and nothing it left earns a
    # re-review of that head (#3475): another one would end the same way.
    StallBucket(
        REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
        "held: a remediation pushed nothing and the verdict still stands",
        "read the round its reason names, fix the branch or the PR body, then "
        "kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # A declaration round's flagged-test record was not written, so the round
    # was not recorded (#2872): fix what refused the write, then release.
    StallBucket(
        DECLARATION_RECORD_FAILED_HOLD_PREFIX,
        "held: a review round's flagged-test record was not written",
        "fix the log directory the reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # The PR body stayed unreadable after a declaration round's remediation
    # (#2872). Clears itself on the first read that succeeds.
    StallBucket(
        PR_BODY_UNREADABLE_HOLD_PREFIX,
        "held until the PR body can be read again",
        "check gh auth and the network; the hold clears itself once the body reads",
        AttentionKind.HELD,
    ),
    # An implementer's acceptance dispute (#2731): a person amends the contract
    # or refers the dispute to the author step, and either releases the hold.
    StallBucket(
        ACCEPTANCE_DISPUTED_HOLD_REASON,
        "implementer disputes its acceptance contract",
        "kew epic acceptance amend <name> <node> --reason TEXT (or --author)",
        AttentionKind.HELD,
    ),
    # A node that ran out of a resource again after the driver spent an
    # automatic resource retry, at its head or the cap across heads (#3191).
    # The release leaves it FAILED, and a person relaunches it with
    # `kew epic retry` once the cause is fixed.
    StallBucket(
        RESOURCE_RETRIES_SPENT_HOLD_PREFIX,
        "held after an automatic resource retry",
        "find why it runs out of a resource, then kew epic unhold and kew epic retry",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        "awaiting_no_change",
        "awaiting a no-change decision",
        "confirm or reject the recorded no-change outcome",
        AttentionKind.HELD,
    ),
    # A Linear node nobody has armed yet (#2538). The driver's whole reason is
    # this fixed sentence (``_FIXED_WAITING_REASONS`` in graph/driver/tick.py,
    # which reads it from here); arming is a label on the tracker item
    # (``linear_admission_status`` in graph/adapters/linear_native.py), which
    # is why the hint names the marker — the console's own wording for it
    # (frontend/src/lib/epic/waiting.ts) — and no kew subcommand.
    #
    # Nobody, in the table: on an epic whose driver is running, an unarmed
    # node is the operator's queue to release when they choose, and #2209 drew
    # it quiet on purpose. On a STALLED epic it is what stopped the drain, and
    # :func:`classify_epic_wait` is where that reading lives.
    StallBucket(
        ARM_WAIT_REASON,
        "awaiting a human arm",
        f"arm it: add the {ARM_LABEL} label to the item in its tracker",
        None,
    ),
    # The one terminal failure that asks a person (#2812): a node the host
    # restarted under twice. The first restart re-admitted it on its own, so
    # nobody was told; the second is a host that keeps dying under this node.
    # Before the plain ``failed-terminal`` bucket, which it is a case of. Its
    # own kind, not ``held``: no hold stands on the node, and the surfaces
    # draw a person's move off the kind, never off this reason (#2971).
    StallBucket(
        HOST_RESTART_REPEATED_REASON,
        "the host restarted under this node twice",
        "check why the host keeps going down, then kew epic retry the node",
        AttentionKind.HOST_RESTART_REPEATED,
        command=EPIC_RETRY_COMMAND,
    ),
    # The same, when the node keeps preserved work a continuation can resume
    # (#2960). Its reason does not start with the one above, so order between
    # the two does not matter; both precede plain ``failed-terminal``.
    StallBucket(
        HOST_RESTART_REPEATED_PRESERVED_REASON,
        "the host restarted under this node twice; its work is preserved",
        "check why the host keeps going down, then kew epic continue the node",
        AttentionKind.HELD,
        command=EPIC_CONTINUE_COMMAND,
    ),
    # Ambiguous preserved work: continue, abandon and retry all refuse it, so
    # the row names no command an operator could paste.
    StallBucket(
        HOST_RESTART_REPEATED_AMBIGUOUS_REASON,
        "the host restarted under this node twice; its preserved work is ambiguous",
        "more than one preserved continuation is recorded; resolve the history "
        "before continuing or abandoning it",
        AttentionKind.HELD,
    ),
    # Preserved work a continuation or abandon took up after the journal was
    # written: executing or re-admitted work is nobody's move (#2960).
    StallBucket(
        HOST_RESTART_REPEATED_TAKEN_UP_REASON,
        "the host restarted under this node twice; its preserved work was taken up",
        "a continuation owns the preserved work, or the node was re-admitted",
        None,
    ),
    StallBucket(
        "failed-terminal", "failed-terminal", "kew epic retry the node(s)", None
    ),
    StallBucket(
        BUDGET_HOLD_PREFIX,
        "held on budget",
        "raise the budget cap",
        AttentionKind.BUDGET_EXHAUSTED,
    ),
    # A daily or weekly cap stopped a review or a remediation (#3205). Clears
    # itself when the window moves on or the cap is raised.
    StallBucket(
        WINDOW_BUDGET_HOLD_PREFIX,
        "held on the daily or weekly cap",
        "wait for the window to move on, or raise the cap its reason names",
        AttentionKind.BUDGET_EXHAUSTED,
    ),
    StallBucket(
        REVIEW_BUDGET_HOLD_PREFIX,
        "held on the review budget",
        "raise the [review] cap its reason names",
        AttentionKind.BUDGET_EXHAUSTED,
    ),
    # A remediation refused for its PR head's own lineage (#3193). Before the
    # bare admission prefix, which it extends: this hold names the head and
    # clears itself on the first tick the PR head moves, so the operator's
    # move is a pushed fix, and an unhold stays the other way out.
    StallBucket(
        ADMISSION_REFUSED_HEAD_HOLD_PREFIX,
        "held on a refused PR head",
        "push a fix (the hold clears when the PR head moves), or kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        ADMISSION_REFUSED_HOLD_PREFIX,
        "held on admission refusal",
        "fix the cause, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        CONTAINMENT_HOLD_PREFIX,
        "held on containment",
        "map [sandbox] images.<runner>, then kew epic unhold",
        AttentionKind.SANDBOX_REFUSED,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # The driver's own pre-dispatch refusals (#1957). Bucketed since #2217:
    # they are the one hold family the overlay does not route to HELD by reason
    # — so the unbucketed fallback was their only route into this stream, and
    # the dominant-cause stall line had no label for them at all.
    #
    # Both hints say RE-CHECK rather than naming a repair, because one prefix
    # has to serve two states and the bucket cannot tell them apart. A refusal
    # the frontier just hit is live: the issuer really is unreachable, the
    # worktree really is on disk. A refusal that survived a later successful
    # dispatch into the review lifecycle is stale by construction — the PR is
    # open and reviewed, so neither condition blocks anything any more — and
    # all that is left to do is clear the row the node is wedged on
    # (``overlay._remediation_wedged``). ``kew epic unhold`` is the action in
    # both; what differs is only whether something needs fixing first.
    StallBucket(
        CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
        "held on a credential-issuer refusal",
        "re-check the issuer its reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
        "held on a pre-existing-worktree refusal",
        "re-check the worktree its reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # The round cap (#2244). Unbucketed, it reached every surface as the
    # generic HELD fallback: no label at all for `kew epic status`'s
    # dominant-cause line, and nothing but the raw ``review_rounds_exhausted``
    # for the console to print. The cap it reached is a count of review rounds,
    # not money, and the hint says what the release also does, because the
    # unhold restarts the review cycle in the same transaction
    # (``epic_holds.REVIEW_CYCLE_RESET_HOLD_REASONS``) — the count the cap is
    # read from only restarts at a retry marker.
    StallBucket(
        ROUNDS_EXHAUSTED_HOLD_REASON,
        "the review round cap was reached",
        "kew epic unhold, which also restarts the review cycle",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # The pre-review gate's own bound (#3201): the head kept failing the merge
    # gate's static checks or ui-checks after its remediated rounds. Its
    # release restarts the review cycle, like the round cap's.
    StallBucket(
        PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX,
        "the head kept failing the merge gate's checks before review",
        "fix the check its reason names on the branch, then kew epic unhold, "
        "which also restarts the review cycle",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
        "held on a refused review sandbox",
        # Not "map an image": the refusal that produced this hold is far more
        # often an image that was never built on this host (#2043), and the
        # reason itself carries the command for the layer that refused.
        "run the build or mapping its reason names, then kew epic unhold",
        AttentionKind.SANDBOX_REFUSED,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # A residual-only round whose head's merge gate paths stayed unread (#3422).
    StallBucket(
        REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX,
        "held: the merge gate paths of the head could not be read",
        "fix the checkout or git failure its reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # The two halves of one reviewer usage limit (#2219). The hold comes first:
    # neither prefix is a prefix of the other, but reading them in this order is
    # what keeps that true of any later edit to either string.
    StallBucket(
        REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
        "held on a reviewer usage limit that is not resetting",
        "raise the provider usage limit, or pin [review] runner to another provider",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        REVIEW_RATE_LIMITED_WAIT_PREFIX,
        "waiting out a reviewer usage limit",
        "the round runs again once the limit resets",
        # Nobody is needed while a provider window is just resetting: this is a
        # stall *cause* without being anybody's job, like a dependency wait. The
        # bucket above is the same incident once the wait has outlasted any real
        # limit — THAT is the one that names a person, and it names them once.
        None,
    ),
    # A round kew re-runs by itself next tick (#2147, #3174): nobody's job
    # while its bound is unspent, and the hold the bound ends in names one.
    StallBucket(
        REVIEW_RETRYING_WAIT_PREFIX,
        "re-reviewing a round that took no verdict",
        "the round runs again on the next tick",
        None,
    ),
    # A push (remediation, merge-main step, ci-fix) an operator's merge verify
    # holds back (#3198): the claim
    # ends with the verify, or lapses one TTL after a killed one.
    StallBucket(
        MERGE_CLAIMED_WAIT_PREFIX,
        "waiting on an operator's merge verify of its head",
        "the step runs once the verify ends (kew merge claim list)",
        None,
    ),
    StallBucket(
        REVIEW_LAUNCH_FAILED_HOLD_PREFIX,
        "held on a reviewer that never started",
        "fix what its reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    # A reviewer that ran and left no verdict, naming why (#3174).
    StallBucket(
        REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX,
        "held on a reviewer that answered more than once",
        "read the reviewer log its run names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX,
        "held on a reviewer run that failed without a verdict",
        "read the exit and reviewer log its reason names, then kew epic unhold",
        AttentionKind.HELD,
        command=EPIC_UNHOLD_COMMAND,
    ),
    StallBucket(
        "blocked", "blocked on an upstream node", "resolve the upstream node", None
    ),
    # Both aged-out forms of "waiting on a PR" (`stall_days`): the node is
    # still one open PR and one person, so neither may map to nobody.
    StallBucket(
        "review-stalled", "review-stalled", "check the PR", AttentionKind.MERGE_READY
    ),
    StallBucket("ci-stalled", "ci-stalled", "check the PR", AttentionKind.HELD),
    StallBucket("cancelled", "cancelled", "reopen or drop the node", None),
)

#: The fallback for a HELD node whose reason no bucket names — every honored
#: hold still needs an operator to clear it, so it must not go silent.
UNBUCKETED_HELD_HINT = "fix the cause, then kew epic unhold"
#: And the command that hint names, for a surface that prints one. A reason no
#: bucket claims is still a row in ``epic_holds``, and an unhold is still what
#: removes it, so the fallback is a filled command rather than nothing.
UNBUCKETED_HELD_COMMAND = EPIC_UNHOLD_COMMAND


@dataclass(frozen=True)
class WaitClassification:
    """What one waiting reason earns: the kind, the operator label, the action.

    ``label`` is the bucket's operator wording and is ``""`` for the unbucketed
    HELD fallback — there, the reason itself is the only thing anyone can read,
    so a surface prints that rather than a label nobody wrote.

    ``command`` is the bucket's template, still unfilled: this table knows which
    command clears a reason, and only the caller knows the epic and item to put
    in it. :func:`fill_command` is the one that joins the two.
    """

    kind: AttentionKind
    label: str
    action: str
    command: str = ""


def bucket_for(reason: str) -> StallBucket | None:
    """The first bucket whose prefix the reason carries, or None."""
    return next((b for b in STALL_BUCKETS if reason.startswith(b.prefix)), None)


#: Operator words for the hold reasons no bucket claims. WORDS only: these
#: reasons stay unbucketed (the generic HELD fallback classifies them), but the
#: one sentence a wait is printed in (``kew.service.attention.wait_sentence``,
#: #2539) must not print ``review_crash`` raw where the console used to say
#: "the reviewer crashed".
UNBUCKETED_HOLD_LABELS: tuple[tuple[str, str], ...] = (
    ("ci_pending", "CI checks are still running"),
    ("review_crash", "the reviewer crashed"),
    ("review_unparseable", "the reviewer verdict could not be read"),
    ("review_pr_closed", "the PR under review was closed"),
    ("review_residuals_unfiled", "the review left residuals nobody filed"),
    (
        "base_integration_conflict",
        "the base branch no longer merges into the branch",
    ),
    ("base_integration_failed", "the base-integration step could not run"),
)


#: What each part of a ``merge_gate_touched`` reason holds the head for: the
#: one hold names every risk the head carries (#3421), so its words name the
#: ones THIS reason names. A part neither prefix claims is gate paths.
_MERGE_GATE_PART_LABELS: tuple[tuple[str, str], ...] = (
    (MERGE_GATE_INTEGRITY_PART, "its review left test-integrity flags unanswered"),
    (MERGE_GATE_UNCHECKED_PART, "its review could not run some checks"),
)


def _merge_gate_label(reason: str) -> str:
    """The words for a ``merge_gate_touched`` reason, from the parts it names."""
    default = "the PR edits the merge gate's own definition"
    labels: list[str] = []
    for part in merge_gate_parts(reason) or ("",):
        said = next((w for p, w in _MERGE_GATE_PART_LABELS if part.startswith(p)), "")
        label = said or default
        if label not in labels:
            labels.append(label)
    return " and ".join(labels)


def wait_label(reason: str) -> str:
    """The operator words for a waiting reason: its bucket's label, else the
    unbucketed hold's (:data:`UNBUCKETED_HOLD_LABELS`, or a
    ``merge_gate_touched`` reason's own parts), else ``""``."""
    bucket = bucket_for(reason)
    if bucket is not None:
        return bucket.label
    if reason.startswith(MERGE_GATE_TOUCHED_HOLD_PREFIX):
        return _merge_gate_label(reason)
    return next((w for p, w in UNBUCKETED_HOLD_LABELS if reason.startswith(p)), "")


#: The publication category a deferral records when the publication filter held
#: the diff for a person, and the words the driver's waiting reason carries the
#: first held path in (``kew.graph.driver.tick._awaiting_publication_reason``).
PUBLICATION_REVIEW_REQUIRED = "publication_review_required"
PROTECTED_PATH_MARK = "protected path "
#: How much of a recorded path a hint repeats: it is diff text, so bounded.
_HINT_PATH_LEN = 160


#: The resolver's cause token a containment hold leads with
#: (``kew.dispatch_preflight._containment_hold_reason``):
#: ``sandbox_containment: [credential_missing] Sandbox: ...``.
_CONTAINMENT_CAUSE = re.compile(re.escape(CONTAINMENT_HOLD_PREFIX) + r": \[([\w-]+)\]")

#: What clears each containment refusal, keyed on the ledger-stable token the
#: resolver raises it with (``kew.sandbox.REFUSAL_*``; a token missing here
#: gets the generic hint, never the image one). One ``sandbox_containment``
#: prefix covers every one of them, and their messages share words — the
#: unparseable-proxy refusal mentions credentials too — so the token, never
#: the prose, picks the hint (#2682). An unmapped image keeps the bucket's own.
_CONTAINMENT_HINTS: dict[str, str | None] = {
    "image_unmapped": None,
    "credential_missing": (
        "export the runner's credential the reason names in the host "
        "environment (or route the node to a runner that holds one), then "
        "kew epic unhold"
    ),
    "credential_undeclared": (
        "declare the runner's required_credentials, or route the node to a "
        "runner that declares one, then kew epic unhold"
    ),
    "proxy_unparseable": (
        "fix the proxy variable the reason names, or set [sandbox] "
        'proxy_environment = "none", then kew epic unhold'
    ),
    "host_free_space": "free disk space on the host, then kew epic unhold",
    "checkout_over_quota": (
        "shrink the checkout below the quota the reason names, then kew epic unhold"
    ),
    "runtime_unavailable": (
        "start the container runtime (Docker or OrbStack), then kew epic unhold"
    ),
    "placement_unavailable": (
        "wait for or free a [sandbox] placement instance, or change the "
        "placement policy, then kew epic unhold"
    ),
    "openshell-runtime-unavailable": (
        "set [sandbox] runtime to one this build can launch, then kew epic unhold"
    ),
    "openshell-egress-uncertified": "run kew sandbox certify, then kew epic unhold",
    "openshell-policy-unpackaged": (
        "route the node to a runner kew ships an OpenShell policy for, then "
        "kew epic unhold"
    ),
    "broker_provider_missing": (
        "create the runner's provider on the OpenShell gateway and map it under "
        "[sandbox.openshell] providers, then kew epic unhold"
    ),
}
#: A token this build has no hint for: the reason itself names the remedy.
_CONTAINMENT_UNKNOWN_HINT = "fix what the reason names, then kew epic unhold"


def containment_cause(reason: str) -> str:
    """The resolver's cause token a containment hold carries, or ``""``."""
    match = _CONTAINMENT_CAUSE.match(reason)
    return match.group(1) if match else ""


def _containment_hint(reason: str) -> str | None:
    """The remedy for this containment refusal's own cause (#2682).

    A hold that carries no cause token keeps the bucket's hint.
    """
    cause = containment_cause(reason)
    if not cause:
        return None
    return _CONTAINMENT_HINTS.get(cause, _CONTAINMENT_UNKNOWN_HINT)


def protected_path(reason: str) -> str:
    """The held path a publication waiting reason names, or ``""``."""
    _, mark, rest = reason.partition(PROTECTED_PATH_MARK)
    if not mark:
        return ""
    return rest.split(";", 1)[0].strip()[:_HINT_PATH_LEN]


def _publication_hint(reason: str) -> str | None:
    """A protected-path hold names the path; GitHub access is not the cause."""
    if PUBLICATION_REVIEW_REQUIRED not in reason:
        return None
    path = protected_path(reason)
    where = f"the protected path {path}" if path else "the held paths"
    return (
        f"review {where} in the preserved branch, then open its PR by hand and "
        "reconcile, or kew finalize the run"
    )


def _refused_head_hint(reason: str) -> str | None:
    """The refused head is named, so the operator knows which push clears it."""
    head = admission_refused_head(reason)
    if head is None:
        return None
    return (
        f"push a fix over head {head} (the hold clears when the PR head moves), "
        "or kew epic unhold"
    )


#: Per-category hints (#2682): a bucket's fixed hint is right for the wait the
#: bucket names, but one prefix can cover causes with different remedies. Each
#: entry reads the recorded reason and answers its own cause, or None to leave
#: the bucket's hint in force. First matching prefix wins, like the buckets.
_CAUSE_HINTS = (
    (CONTAINMENT_HOLD_PREFIX, _containment_hint),
    ("awaiting_publication", _publication_hint),
    (ADMISSION_REFUSED_HEAD_HOLD_PREFIX, _refused_head_hint),
)


def cause_hint(reason: str) -> str | None:
    """The hint the reason's recorded cause earns, or None for the bucket's."""
    return next(
        (hint(reason) for prefix, hint in _CAUSE_HINTS if reason.startswith(prefix)),
        None,
    )


def classify_wait(reason: str, *, held: bool) -> WaitClassification | None:
    """The kind, label and action one waiting reason earns, or None for nobody.

    The action is the reason's own cause hint where :func:`cause_hint` has one,
    else the bucket's fixed hint.

    An unbucketed reason on a HELD node still needs an operator to clear the
    hold, so it falls back to the generic kind rather than going silent; the
    CI gate is the one hold that is a wait, not a person.
    """
    if is_ci_pending_hold(reason):
        return None
    bucket = bucket_for(reason)
    if bucket is None:
        if not held:
            return None
        return WaitClassification(
            AttentionKind.HELD, "", UNBUCKETED_HELD_HINT, UNBUCKETED_HELD_COMMAND
        )
    if bucket.kind is None:
        return None
    action = cause_hint(reason) or bucket.hint
    return WaitClassification(bucket.kind, bucket.label, action, bucket.command)


#: What may stand in a filled command's argument slot. An epic ref and a node id
#: come from a tracker, which is untrusted text, and the whole point of the field
#: is that an operator pastes it into a shell — so anything that is not a plain
#: argument (whitespace, a quote, a shell metacharacter, the ``<`` of a leftover
#: placeholder) makes the command empty rather than making it a surprise.
#:
#: A leading ``-`` is excluded for the same reason the rest is: an argument slot
#: that fills with ``--all`` stops being the argument the row names and becomes
#: an option to the subcommand before it — ``kew epic run --all`` runs every
#: epic, not the one the row is about. Dashes are fine anywhere else, which is
#: where real refs like ``go-live-2026-09`` carry them.
_RUNNABLE_ARG = re.compile(r"[A-Za-z0-9._:/@][A-Za-z0-9._:/@-]*")


def fill_command(template: str, /, **values: str) -> str:
    """*template* with its ``{token}``s filled from *values* — or ``""``.

    Empty — meaning "this row has no command to print" — when the template is
    empty, when a token it names has no usable value here, or when any token
    survives the fill. A command is served only when it is runnable as printed:
    the field exists because the console printed ``kew epic continue <name>
    <node>`` and ``kew epic resume go-live-2026-09 SFD-232`` as if either could
    be pasted (#2244), and a half-filled template is the first of those.
    """
    if not template:
        return ""
    filled = template
    for token, value in values.items():
        placeholder = "{" + token + "}"
        if placeholder not in filled:
            continue
        if not _RUNNABLE_ARG.fullmatch(value or ""):
            return ""
        filled = filled.replace(placeholder, value)
    return "" if "{" in filled else filled


def failed_node_command(reason: str | None, *, epic: str, item: str) -> str:
    """The command that recovers a FAILED node whose stuck reason is *reason*.

    Served on the node itself, so a surface never infers one from a missing
    attention (#2960): its bucket's own command when it has one (continue for
    preserved work), retry for a plain terminal failure, and ``""`` when no
    command recovers it — an ambiguous history, or preserved work a
    continuation owns or abandon already re-admitted. Filled by
    :func:`fill_command`, so an unsafe argument serves ``""`` too.
    """
    bucket = bucket_for(reason or "failed-terminal")
    if bucket is None:
        return ""
    template = bucket.command
    if not template and bucket.prefix == "failed-terminal":
        template = EPIC_RETRY_COMMAND
    return fill_command(template, epic=epic, item=item)


#: An arm wait on a STALLED epic: a person's move, and the command after it.
#: The label alone moves nothing there — the driver exited when it ran out of
#: moves — so the armed node is picked up only by a driver run again.
STALLED_ARM_WAIT = WaitClassification(
    AttentionKind.HELD,
    "awaiting a human arm",
    f"arm it: add the {ARM_LABEL} label to the item in its tracker,"
    " then restart the stalled driver",
    EPIC_RUN_COMMAND,
)


def classify_epic_wait(
    reason: str, *, held: bool, signal: object = None
) -> WaitClassification | None:
    """:func:`classify_wait`, read on an epic whose driver signal is *signal*.

    The one wait whose answer depends on the epic is an arm (#2538). go-live
    stalled with five unarmed nodes on its banner and a legend reading "needs
    you 0": the driver had stopped, and those labels were the whole of what
    would start it again. So on a STALLED epic an arm wait is a person's move,
    with ``kew epic run`` — the restart that picks the armed node up — as its
    command. Anywhere else it is nobody's, exactly as the table says.

    The needs-you set and the node join both read journaled waits through
    this, so a stalled epic's row and its DAG diamond are one answer.
    """
    classified = classify_wait(reason, held=held)
    if (
        classified is None
        and str(signal or "") == STALLED_SIGNAL
        and reason.startswith(ARM_WAIT_REASON)
    ):
        return STALLED_ARM_WAIT
    return classified
