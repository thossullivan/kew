"""Which ``epic_holds`` reasons actually take a node out of the frontier.

One owner for the hold-reason vocabulary and the predicate over it, because two
readers have to agree on it:

* the driver overlay (:mod:`kew.graph.driver.overlay`) routes a matching hold to
  ``HELD``, which is what keeps the node out of the dispatch frontier;
* the operator verbs (:mod:`kew.service.verbs`) refuse ``kew epic retry`` on a
  matching hold, because re-admitting a node the overlay will hold anyway
  reports a recovery that did not happen (#2154);
* ``kew epic unhold`` (:mod:`kew.cli.epic`) asks which released reasons have to
  restart the review cycle with them, because retry — the marker's only other
  writer — is exactly what it now refuses;
* the merge pass (:mod:`kew.graph.driver.merge`) asks before writing its own
  ``ci_pending`` hold, so it never upserts an honored reason another writer owns
  down to the one reason that clears itself.

They must not drift. A reason the overlay does *not* match leaves the node
FAILED and dispatchable — ``credential_issuer_refused:`` and
``preexisting_worktree_refused:``, written by the driver's frontier refusal
path, are exactly that — so refusing retry on one would take away the node's
documented recovery path and hand back nothing but a trip through
``kew epic unhold``. Being unmatched here is also why a dispatch can SUCCEED
while one of them stands, so those two have their own vocabulary below
(``DISPATCH_REFUSAL_HOLD_PREFIXES``), their own clear, and — once the node is
past dispatch and the recovery they protect no longer exists — their own retry
refusal (``is_dispatch_refusal_hold``): see #2217.

``kew.service`` may not import ``kew.graph`` (import-linter contract), so the
shared vocabulary lives here rather than in the overlay that reads it.
"""

from __future__ import annotations

from collections.abc import Iterable

# The CI gate written while a node's checks are still running (#782/#819).
CI_PENDING_HOLD_REASON = "ci_pending"

#: The same gate with the merge state that earned it still on it (#2303):
#: ``ci_pending:dirty`` is a tree a person has to rebase, ``ci_pending:behind``
#: is a base the factory can catch up to on its own, and both used to be the
#: one word ``ci_pending`` — so no surface could tell a conflict from a stale
#: base, and the console had to guess which of the two a held node meant.
#: Matched by PREFIX from here on, exactly as the budget and containment holds
#: are, which is what lets the detail ride along without any reader having to
#: enumerate the merge states GitHub can report.
CI_PENDING_HOLD_PREFIX = CI_PENDING_HOLD_REASON


def ci_pending_hold_reason(merge_state: str | None) -> str:
    """``ci_pending``, with *merge_state* on it when GitHub named one.

    The suffix is lower-cased GitHub ``mergeStateStatus`` (``dirty``,
    ``behind``, ``blocked``, …). ``None``/``UNKNOWN`` adds nothing: a state
    nobody could read is not a detail, and writing the word ``unknown`` into a
    hold reason would make an unread fact look like an observed one.
    """
    state = (merge_state or "").strip().lower()
    if state in ("", "unknown"):
        return CI_PENDING_HOLD_REASON
    return f"{CI_PENDING_HOLD_PREFIX}:{state}"


def is_ci_pending_hold(reason: str | None) -> bool:
    """Whether this hold reason is the merge pass's own self-clearing CI gate.

    One predicate for every reader that used to compare to the bare word, so
    adding the merge state to the reason could not quietly turn a held node
    into an unrecognised one — which is the hold the overlay routes to HELD,
    the row the stall clock reads, and the only reason the merge pass is
    allowed to overwrite.
    """
    if reason is None:
        return False
    return reason == CI_PENDING_HOLD_REASON or reason.startswith(
        f"{CI_PENDING_HOLD_PREFIX}:"
    )


def ci_pending_merge_state(reason: str | None) -> str | None:
    """The merge state carried by a ``ci_pending:<state>`` reason, else None."""
    if not is_ci_pending_hold(reason) or reason == CI_PENDING_HOLD_REASON:
        return None
    return str(reason).split(":", 1)[1] or None


# Epic-hold reason prefix written by the budget preflight (#661). The full
# reason carries observed/cap numbers + the operator hint; readers only need the
# prefix to route the node to HELD.
BUDGET_HOLD_PREFIX = "budget_preflight"

# Epic-hold reason prefix a daily or weekly cap refusal of a governed review or
# remediation writes (#3205): ``budget: daily cap $50.00 reached ($51.20
# spent)``. Routed to HELD like the preflight hold; the driver's reconcile pass
# clears it once the window admits a launch again — the UTC day rolls over,
# the week's oldest spend ages out, or the cap is raised.
WINDOW_BUDGET_HOLD_PREFIX = "budget:"

# Epic-hold reason prefix written by a containment refusal (#1144): the node's
# runner could not be contained on a host that CAN contain (no mapped image, no
# declared in-container credential), so no agent spawned. Routed to HELD for the
# same reason as the budget hold — the node is intact and re-admittable the
# moment the operator maps an image and un-holds it — but never auto-cleared:
# kew cannot observe a config it never got far enough to resolve.
CONTAINMENT_HOLD_PREFIX = "sandbox_containment"

# Epic-hold reason prefix written by a detached dispatch child that admission
# refused before any agent spawned (#1658). The child runs with its streams on
# DEVNULL, so the hold IS the refusal's only durable trace; routed to HELD for
# the same reason as the containment hold — the node is intact and
# re-admittable the moment the operator fixes what was refused — and likewise
# never auto-cleared, because kew cannot observe a fix it never got far enough
# to resolve. Clears on `kew epic unhold`.
ADMISSION_REFUSED_HOLD_PREFIX = "admission_refused"

# The review stage's remediation refusals are about one PR head, so they carry
# it: ``admission_refused@<head12>: <detail>`` (#3193). Unlike a dispatch
# child's refusal, kew CAN observe the change that may clear one — a new head —
# so the tick releases a head-scoped hold the first time the PR head differs,
# and the new head is reviewed again. A reason with no head stays operator-only.
ADMISSION_REFUSED_HEAD_LEN = 12
ADMISSION_REFUSED_HEAD_HOLD_PREFIX = f"{ADMISSION_REFUSED_HOLD_PREFIX}@"


def admission_refused_reason_prefix(head: str | None) -> str:
    """The prefix an ``admission_refused`` reason starts with, head-scoped or not."""
    if not head:
        return ADMISSION_REFUSED_HOLD_PREFIX
    return f"{ADMISSION_REFUSED_HEAD_HOLD_PREFIX}{head[:ADMISSION_REFUSED_HEAD_LEN]}"


def admission_refused_head(reason: str | None) -> str | None:
    """The abbreviated head an ``admission_refused`` reason was written for.

    ``None`` for any other reason and for a reason that carries no head — the
    dispatch child's refusals, and holds written before the head was carried.
    """
    if reason is None or not reason.startswith(ADMISSION_REFUSED_HEAD_HOLD_PREFIX):
        return None
    head = reason[len(ADMISSION_REFUSED_HEAD_HOLD_PREFIX) :].split(":", 1)[0].strip()
    return head or None


# The two reasons the driver's OWN frontier refusal path writes (#1957): the
# credential issuer refused the node's route, or a dirty worktree for it is
# already on disk. Deliberately outside :func:`holds_node_out_of_frontier` —
# see the module docstring: leaving the node dispatchable IS the documented
# recovery, and `kew epic retry` stays available on it for the same reason —
# for exactly as long as that recovery exists (:func:`is_dispatch_refusal_hold`).
#
# That also makes them the one hold family a LATER DISPATCH can outlive, and
# #2217 is what that cost: #2201/#2202 were refused, dispatched successfully
# forty minutes later, reviewed, and returned NEEDS_CHANGES — and the stale
# refusal row then blocked remediation for two hours, because the remediator
# refuses to run while ANY hold stands. The dispatch that disproves the refusal
# now clears it (``tick._clear_stale_dispatch_refusal_hold``), and a hold that
# does survive into the review lifecycle reads HELD instead of going silent
# (``overlay._remediation_wedged``) — and reads STALLED rather than buying the
# epic a PROGRESSING signal for ``epic.stall_days``
# (``tick._hold_is_a_live_wait``), because no pass left will clear that row.
CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX = "credential_issuer_refused"
PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX = "preexisting_worktree_refused"

#: The family above, as passed to
#: ``RunStore.remove_epic_hold(only_reason_prefixes=...)``: the release is
#: narrowed to these two reasons INSIDE the delete transaction, because every
#: other hold is somebody else's (an operator, a budget, a merge gate) and
#: clears on its own terms — a dispatch must never un-hold one.
DISPATCH_REFUSAL_HOLD_PREFIXES = (
    CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
    PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
)


def is_dispatch_refusal_hold(reason: str | None) -> bool:
    """Whether this hold reason is one of the driver's own dispatch refusals.

    The companion to :func:`holds_node_out_of_frontier` for the one family that
    sits outside it. The overlay never routes these to HELD *by reason*, so the
    predicate above must keep saying no to them; a reader that has to know the
    row is the driver's own superseded bookkeeping — the tick clearing it after
    a dispatch disproves it, the tick's signal refusing to read a node wedged
    on one as progress, the retry refusing once nothing will dispatch the node
    again (#2217) — asks this instead.
    """
    if reason is None:
        return False
    return reason.startswith(DISPATCH_REFUSAL_HOLD_PREFIXES)


# The round-cap review hold: the driver writes it when the node's review cycle
# reaches ``review.max_rounds``, and the count that earns it is the node's own
# persisted round history (see REVIEW_CYCLE_RESET_HOLD_REASONS below).
ROUNDS_EXHAUSTED_HOLD_REASON = "review_rounds_exhausted"

# Governed-review hold reasons (N3/#977). In blocking mode every non-READY
# review outcome lands the node in HELD: needs-human with an attested reason;
# the overlay routes each of these reasons to HELD exactly as it does
# ci_pending / budget_preflight, so a held node stays out of the dispatch
# frontier (and never derives back to TODO) until an operator un-holds it.

# The two holds a review earns when its reviewer NEVER STARTED — the sandbox
# refused the launch (#2147), or the lane failed before the container ran
# (#2043). Held under their own reasons because ``review_unparseable`` sends an
# operator looking for a reviewer verdict that never existed: the incident that
# named this issue read a missing agent image as a verdict-channel defect. Both
# reasons carry detail — the fix for the layer that refused — so, like the
# review-budget and merge-main holds, they are matched by PREFIX.
REVIEW_SANDBOX_REFUSED_HOLD_PREFIX = "review_sandbox_refused"
REVIEW_LAUNCH_FAILED_HOLD_PREFIX = "review_launch_failed"

# The two holds a reviewer that DID run earns when its capture is malformed in
# a way that names itself (#3174): a successful run that spoke more than once
# after its one retry is spent, and a failed run that left no verdict. Both
# carry the stream's refusal text as detail, so they are matched by PREFIX.
REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX = "review_multiple_results"
REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX = "review_failed_no_verdict"

# The hold a reviewer that ran earns when its verdict is inadmissible for any
# other reason. It carries what was wrong as detail whenever kew knows it
# (#3186) — two results with different verdict blocks, a run that raised after
# it was recorded — so it too is matched by PREFIX; bare only when kew knows
# nothing more.
REVIEW_UNPARSEABLE_HOLD_PREFIX = "review_unparseable"

# The WAITING reason a reviewer round a provider usage limit cut off earns
# (#2219) — deliberately NOT in ``REVIEW_HOLD_REASONS`` below, and not a hold at
# all: no reviewer verdict exists, nothing on this host is broken, and the next
# tick after the limit resets re-reviews. It lives here because the hold-reason
# vocabulary is also what the attention buckets read, and the wait has to name
# itself there as the one waiting state that is nobody's job yet.
REVIEW_RATE_LIMITED_WAIT_PREFIX = "review_rate_limited"

# The WAITING reason of a reviewer round kew runs again by itself on the next
# tick: a sandbox that refused before launch with refusals left (#2147), or a
# runner that answered more than once with its one retry unspent (#3174). Not a
# hold and nobody's job, like the usage-limit wait above: the node carries no
# hold row, and only the hold its bound ends in names a person. The hold that
# would follow is named after the dash, so the operator words say which.
REVIEW_RETRYING_WAIT_PREFIX = "review_retrying"

# The WAITING reason of a remediation, merge-main step or ci-fix held back by
# an operator's merge claim (#3198): ``kew verify --pr`` (or ``kew merge claim
# take``) holds the PR head while it verifies it. Not a hold and nobody's job:
# the claim ends with the verify, or lapses one TTL after it was killed, and
# the next tick takes the step.
MERGE_CLAIMED_WAIT_PREFIX = "merge_claimed"

# ...and the HOLD the same incident earns once the wait above has run longer
# than any provider limit resets in (``review_stage._MAX_RATE_LIMIT_WAIT``).
# This one IS a hold: a limit still rejecting every round after that is not
# resetting on its own, so a person has to raise it or point `[review] runner`
# somewhere else. It is also the only thing that bounds the retry — a node
# waiting in ADVERSARIAL_REVIEW carries no hold row, and the stall clocks all
# run off a hold row (`tick._held_signal`) or an IN_REVIEW PR
# (`tick._stale_review_stuck`), so nothing else would ever reach it. Carries
# the elapsed wait as detail, so it is matched by PREFIX like the budget holds.
REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX = "review_rate_limit_exhausted"

# The hold a residual-only round earns when the merge gate paths of its head
# cannot be read on ``review_stage._MAX_BODY_UNREAD_PASSES`` ticks in a row
# (#3422): the stop rule cannot tell whether the head owes a confirming review,
# so it neither accepts nor remediates, and a read that never succeeds would
# otherwise wait forever with no hold row. Carries the head and the failed
# read as detail, so it is matched by PREFIX.
REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX = "review_gate_paths_unread"


def review_gate_paths_unread_hold_reason(head_sha: str, reads: int, error: str) -> str:
    """The ``review_gate_paths_unread`` reason: the head, the count, the read."""
    return safe_hold_reason(
        f"{REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX}: the merge gate paths at "
        f"{head_sha[:12]} went unread on {reads} ticks in a row, so whether its "
        "residuals need a confirming review is unknown — fix the checkout the "
        f"read names, then kew epic unhold ({error})"
    )


# The hold a head earns when the pre-review gate (#3201) keeps failing it: the
# merge gate's static checks, or its ui-checks on the merge candidate, failed
# ``pre_review_gate.MAX_GATE_ROUNDS`` remediated rounds in one review cycle. Not
# the round cap: no reviewer read the head, and an operator looking for the
# reviewer findings ``review_rounds_exhausted`` implies finds none. Carries the
# check and its first failing line as detail, so it is matched by PREFIX. Its
# release restarts the review cycle (``REVIEW_CYCLE_RESET_HOLD_REASONS``), for
# the round cap's reason: the bound is read from the cycle's own rounds.
PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX = "pre_review_gate_exhausted"


def pre_review_gate_exhausted_hold_reason(check: str, rounds: int, line: str) -> str:
    """The ``pre_review_gate_exhausted`` reason for a head that failed *check*.

    The line is cut to fit the echo cap here, from its end, so its start (the
    rule, the file, the module) survives the cap's middle cut.
    """
    head = (
        f"{PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX}: {check} still fail after "
        f"{rounds} rounds: "
    )
    shown = " ".join((line or "no output").split())
    room = HOLD_REASON_MAX_CHARS - len(head)
    if len(shown) > room:
        shown = shown[: room - len(_HOLD_REASON_ELLIPSIS)] + _HOLD_REASON_ELLIPSIS
    return safe_hold_reason(head + shown)


REVIEW_HOLD_REASONS = frozenset(
    {
        "review_crash",
        "review_budget",
        "review_unparseable",
        ROUNDS_EXHAUSTED_HOLD_REASON,
        "review_pr_closed",
        "review_residuals_unfiled",
        # Only after the bounded retry a pre-launch sandbox refusal earns is
        # spent (#2147): the first refusals are retried, never held.
        REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
        REVIEW_LAUNCH_FAILED_HOLD_PREFIX,
        REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX,
        REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX,
        # Only after the unbounded-looking rate-limit wait is spent (#2219):
        # the wait itself is no hold at all.
        REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
        # Only after the bounded unread-gate-paths wait is spent (#3422).
        REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX,
    }
)

# The merge-main step's holds (#2149). When a review-stage branch stops merging
# into its base the driver integrates the base itself; a conflict nothing could
# resolve, or a step that could not run at all, HOLDs the node. Both reasons
# carry detail — the conflicting paths, the failure — so, like the budget holds,
# they are matched by PREFIX. Routed to HELD because the hold is the ONLY thing
# that makes them sticky: unmatched, the node keeps reading its review status,
# the review stage finds it due again on the very next tick and spends another
# governed `kew resolve` run reaching the same unresolvable answer, forever.
# Never auto-cleared: an operator resolves the conflict on the branch (or fixes
# what refused the step) and un-holds. A `kew epic retry` is refused for the
# same reason it is on every other honored hold — re-admitting the node would
# report a recovery that did not happen.
BASE_INTEGRATION_CONFLICT_HOLD_PREFIX = "base_integration_conflict"
BASE_INTEGRATION_FAILED_HOLD_PREFIX = "base_integration_failed"

# The merge queue's gate-definition hold (#2184). The queue verifies a combined
# tree by running a command the PR's own files define — the gate scripts, the
# build/test configuration they read, the container they build — so a PR that
# edits one of `[epic] merge_gate_paths` can decide what proves it. Carries the
# touched paths, so it is matched by PREFIX like the budget holds. Routed to
# HELD because the whole point is that no further automated pass may clear it:
# a human reads the diff and runs `kew epic unhold`. Nothing auto-clears it —
# the PR's head moving does not make the edit safe.
MERGE_GATE_TOUCHED_HOLD_PREFIX = "merge_gate_touched"

# The head the gate hold was written against rides in the reason, between the
# prefix and the paths: ``merge_gate_touched@<head oid>: <paths>``. It is what
# makes the release below head-scoped, and it stays in front of the paths so the
# 160-char echo cap (``safe_hold_reason``) can never truncate it away.
_MERGE_GATE_HEAD_SEP = "@"


def merge_gate_hold_reason(head_oid: str, named_paths: str) -> str:
    """The ``merge_gate_touched`` reason for one head and its touched paths."""
    return (
        f"{MERGE_GATE_TOUCHED_HOLD_PREFIX}{_MERGE_GATE_HEAD_SEP}{head_oid}"
        f": {named_paths}"
    )


def merge_gate_reviewed_head(reason: str | None) -> str | None:
    """The head OID a ``merge_gate_touched`` reason was written against.

    ``None`` for any other reason, and for a reason written before the head was
    carried — an unparseable receipt releases nothing, which is the safe way to
    read one (the node is simply held again for a human).
    """
    prefix = f"{MERGE_GATE_TOUCHED_HOLD_PREFIX}{_MERGE_GATE_HEAD_SEP}"
    if reason is None or not reason.startswith(prefix):
        return None
    head = reason[len(prefix) :].split(":", 1)[0].strip()
    return head or None


# One ``merge_gate_touched`` hold names every risk its head carries (#3421),
# each a part of the reason after the head, joined by ``; ``: the gate paths
# the PR edits (named as they are), and these two.
MERGE_GATE_PARTS_SEP = "; "
MERGE_GATE_INTEGRITY_PART = "unanswered test-integrity flags "
MERGE_GATE_UNCHECKED_PART = "review sandbox could not run: "


def merge_gate_parts(reason: str | None) -> tuple[str, ...]:
    """The risks a ``merge_gate_touched`` reason names, in order; ``()`` for any
    other reason, or one that names its head and nothing else."""
    if reason is None or merge_gate_reviewed_head(reason) is None:
        return ()
    named = reason.split(":", 1)[1].strip()
    return tuple(part for part in named.split(MERGE_GATE_PARTS_SEP) if part)


# Where the release of a gate-definition hold is recorded (#2184). The hold is
# DERIVED — every merge pass re-intersects the PR's unchanged diff with
# `[epic] merge_gate_paths` — so clearing the `epic_holds` row releases nothing:
# the very next pass re-derives the identical hold, and `kew epic unhold` is an
# infinite loop the node never merges out of. `kew epic unhold` therefore writes
# the released reason to this `meta` key in the SAME transaction as the delete,
# and the queue reads it back before holding. Keyed per epic node and matched on
# the head the reason carries, so it is a receipt for ONE reviewed diff and not
# an exemption: a push moves the head, the receipt stops matching and the new
# diff is held for a human exactly as the first one was.
MERGE_GATE_ACK_META_PREFIX = "merge_gate_reviewed:"


def merge_gate_ack_key(epic_ref: str, item_id: str) -> str:
    """The ``meta`` key one epic node's gate-release receipt lives under."""
    return f"{MERGE_GATE_ACK_META_PREFIX}{epic_ref}:{item_id}"


# The hold a remediation whose publication was DEFERRED earns (#2362). The
# host held the publication for a person (a protected path in its diff), so the
# remediation's work sits in a preserved checkout and the PR head never moved —
# which is exactly the shape the driver reads as "this round still needs
# remediating". It dispatched another one every tick: three runs and ~$44 on
# #2352 before an operator noticed, with nothing held and nothing in
# ``kew epic watch``. Routed to HELD so the node leaves the review lifecycle
# until a person deals with the deferral, and matched by PREFIX because the
# reason carries the preserved checkout path for them to look at.
#
# Not auto-cleared, and re-derived like the merge-main holds: the recovery is
# to publish that checkout (which moves the head, so the next tick reviews the
# new one and never reaches this hold) or to discard it — the driver reads the
# discard off the absent path the reason names, which is what lets the release
# that follows stick. A bare ``kew epic unhold`` against an unchanged head and
# a checkout still on disk is re-held on the next pass, for the same reason a
# bare release of a base-integration conflict is.
PUBLICATION_HELD_HOLD_PREFIX = "publication_held"


def publication_held_hold_reason(detail: str) -> str:
    """The ``publication_held`` reason naming what a person has to look at."""
    return safe_hold_reason(f"{PUBLICATION_HELD_HOLD_PREFIX}: {detail}")


#: Where one deferred review publication's evidence lives, keyed by the run
#: that produced it: the reviewed head it was cut at and the preserved checkout
#: it left behind. The run row records the deferral as ``awaiting_publication``
#: and that status is all it can hold, so the two facts the driver needs to
#: name the hold — and to tell a LIVE deferral from a stale row whose head has
#: since moved on — ride here instead.
DEFERRED_PUBLICATION_META_PREFIX = "review_publication_deferred:"


def deferred_publication_key(run_id: str) -> str:
    """The ``meta`` key one run's deferred-publication evidence lives under."""
    return f"{DEFERRED_PUBLICATION_META_PREFIX}{run_id}"


# The hold a declaration-only round earns once its remediation spent a run
# (#2872). The reviewer rejected only that edited or removed tests are
# undeclared; the remediation fixed nothing because nothing was wrong, and
# wrote no Behavior changes line either, so a second one would loop at the same
# head. Matched by PREFIX because the reason names the tests to declare. A
# person adds the lines to the PR body and runs `kew epic unhold`: the next
# pass reads them there and re-reviews the head instead of re-holding it.
TEST_DECLARATIONS_HOLD_PREFIX = "test_declarations_missing"


def declarations_missing_hold_reason(node_ids: tuple[str, ...]) -> str:
    """The ``test_declarations_missing`` reason naming the tests to declare.

    A node id is only useful whole — it is what the Behavior changes line must
    name — so the reason never lets :func:`safe_hold_reason` cut one: it names
    the ids that fit under the cap in full and counts the rest, which the
    driver's log line and the round's flagged-test record (``<sidecar>
    .declarations``) carry in full.
    """
    shown: list[str] = []
    for node_id in node_ids:
        candidate = _declarations_reason([*shown, node_id], len(node_ids))
        if len(candidate) > HOLD_REASON_MAX_CHARS:
            break
        shown.append(node_id)
    return safe_hold_reason(_declarations_reason(shown, len(node_ids)))


def _declarations_reason(shown: list[str], total: int) -> str:
    # Terse on purpose: a real node id runs to ~100 characters, and at least
    # one has to fit whole beside the frame.
    head = f"{TEST_DECLARATIONS_HOLD_PREFIX}: {total} undeclared"
    if not shown:
        return f"{head}, named in the round's flagged-test record"
    rest = total - len(shown)
    return f"{head}: {', '.join(shown)}" + (f" +{rest} more" if rest else "")


# The hold an ordinary NEEDS_CHANGES round earns once a remediation of it ran
# and moved nothing (#3233): the findings stand on the head they were filed
# against, so another remediation would read the same brief and end the same
# way, once per tick. Matched by PREFIX because the reason names the findings
# left standing. A person fixes the branch or answers the findings, then runs
# `kew epic unhold`.
REMEDIATION_NO_CHANGE_HOLD_PREFIX = "remediation_no_change"


def remediation_no_change_hold_reason(
    summaries: tuple[str, ...], *, moved_to: str | None = None
) -> str:
    """The ``remediation_no_change`` reason: how many findings stand, and the first.

    *moved_to* is the PR's head when it is no longer the one the no-op
    published — a push or a base integration since — so the reason does not
    read as if nothing had touched the branch. The summaries are the
    reviewer's words, so the reason is cut by :func:`safe_hold_reason` like any
    other untrusted text.
    """
    head = f"{REMEDIATION_NO_CHANGE_HOLD_PREFIX}: a remediation changed nothing"
    if moved_to:
        head += f" and the head has moved since, to {moved_to[:12]}"
    if not summaries:
        return safe_hold_reason(f"{head}; the round's findings still stand")
    noun = "finding stands" if len(summaries) == 1 else "findings stand"
    lead = f"{head}; {len(summaries)} {noun}: "
    # The cap would otherwise cut the middle of the line, which is where the
    # head and the count sit: the reviewer's words give way instead.
    summary = " ".join(summaries[0].split())
    room = HOLD_REASON_MAX_CHARS - len(lead)
    if len(summary) > room:
        width = room - len(_HOLD_REASON_ELLIPSIS)
        summary = _head_at_word(summary, width) + _HOLD_REASON_ELLIPSIS
    return safe_hold_reason(lead + summary)


# The hold a node earns when a review remediation ended at head H without
# pushing anything, and nothing it left in the PR body earns a re-review of H
# (#3475): another remediation at H would read the same brief and end the
# same way, once every couple of minutes, to the branch's run cap. Catches the
# runs ``remediation_no_change`` cannot judge — one with no completed outcome
# — and the round a re-review of H found after one such run. Matched by PREFIX
# because the reason names the head and round. A person reads the round,
# pushes a fix or proposes lines in the PR body, then runs `kew epic unhold`:
# a changed proposed block earns H one re-review (#3493).
REMEDIATION_NO_PROGRESS_HOLD_PREFIX = "remediation_no_progress"


def remediation_no_progress_hold_reason(head: str, round_num: int) -> str:
    """The ``remediation_no_progress`` reason: the stale verdict's head and round."""
    return safe_hold_reason(
        f"{REMEDIATION_NO_PROGRESS_HOLD_PREFIX}: round {round_num} at "
        f"{head[:12]} stands, its remediation pushed nothing — push a fix or "
        "propose lines in the PR body, then kew epic unhold"
    )


# The two holds the declaration path earns when the host cannot tell what is
# declared (#2872). Matched by PREFIX: each reason names its failure.
#
# ``declaration_record_failed`` — a NEEDS_CHANGES round whose flagged-test
# record could not be written beside its sidecar is NOT recorded: a round
# standing without the record reads as ordinary findings and is remediated at
# the same head with the ordinary brief, the loop #2872 ends. A person fixes
# what refused the write and runs `kew epic unhold`; with no round standing at
# the head, the next pass reviews it again and writes the record then.
#
# ``pr_body_unreadable`` — after a declaration round's remediation, the PR body
# could not be read on that many passes in a row, so whether its lines landed
# is unknown. The review stage keeps reading it while held and clears the hold
# itself on the first read that succeeds.
DECLARATION_RECORD_FAILED_HOLD_PREFIX = "declaration_record_failed"
PR_BODY_UNREADABLE_HOLD_PREFIX = "pr_body_unreadable"


def declaration_record_failed_hold_reason(error: str) -> str:
    """The ``declaration_record_failed`` reason: the failure and the recovery.

    The record's path is the driver's log line's to name: a stored sidecar's
    name alone would push the failure past the reason cap.
    """
    return safe_hold_reason(
        f"{DECLARATION_RECORD_FAILED_HOLD_PREFIX}: flagged-test record not "
        f"written ({error}), so the round was not recorded — fix the log "
        "directory, then kew epic unhold to review the head again"
    )


def declarations_not_written_hold_reason(pr_number: int | None) -> str:
    """The ``declaration_record_failed`` reason for a verdict whose accepted
    Behavior changes lines did not land in the PR body (#3475): the verdict
    is not recorded, so a READY stands only with its lines in place."""
    return safe_hold_reason(
        f"{DECLARATION_RECORD_FAILED_HOLD_PREFIX}: accepted Behavior changes "
        f"lines not written to PR #{pr_number}, so the round was not recorded "
        "— check gh auth, then kew epic unhold"
    )


def pr_body_unreadable_hold_reason(pr_number: int | None, reads: int) -> str:
    """The ``pr_body_unreadable`` reason: the failed read and its recovery."""
    return safe_hold_reason(
        f"{PR_BODY_UNREADABLE_HOLD_PREFIX}: PR #{pr_number} body unread on "
        f"{reads} passes in a row, so whether its Behavior changes lines "
        "landed is unknown — check gh auth and the network; the hold clears "
        "itself on the first read that succeeds"
    )


def is_pr_body_unreadable_hold(reason: str | None) -> bool:
    """True for the one declaration hold the review stage clears itself."""
    return bool(reason) and str(reason).startswith(PR_BODY_UNREADABLE_HOLD_PREFIX)


# ``review_budget`` is the one review hold that carries detail — the pool, the
# spend and the key to raise (#2153) — so it is ALSO matched by prefix, like the
# budget/containment holds. Matched bare-only, a detailed reason routed nowhere:
# the exhausted node kept reading REMEDIATING and was re-dispatched, refused and
# dropped every tick with nothing but a log line to show for it.
REVIEW_BUDGET_HOLD_PREFIX = "review_budget"


# The hold an implementer's acceptance dispute earns (#2731). The implementer
# never edits its node's acceptance contract; when it believes a contract test
# is wrong it says so in its result, and the node waits here for a person. Not
# a failure and not a review round: a person amends the contract by hand
# (``kew epic acceptance amend``) or refers the dispute to the author step, and
# either releases this hold. Written verbatim as the issue names it.
ACCEPTANCE_DISPUTED_HOLD_REASON = "acceptance-disputed"


# The hold a node earns when its run runs out of a resource again after the
# driver has spent an automatic resource retry it would need (#3191): the one
# retry the failed run's head earns, or the node's cap across heads. The retry
# is keyed per head, and every relaunch clones the current main, so without
# the cap a node that always runs out of memory buys one more paid run each
# time main moves. Not auto-cleared: kew cannot see the fix (a smaller test
# pool, a raised memory cap). A person releases it with `kew epic unhold`; the
# node then reads FAILED, the driver does not hold it again for the same run,
# and `kew epic retry` relaunches it by hand.
RESOURCE_RETRIES_SPENT_HOLD_PREFIX = "resource_retries_spent"

#: How much of a head a ``resource_retries_spent`` reason names.
RESOURCE_RETRY_HEAD_LEN = 12

_RESOURCE_RETRY_RECOVERY = "find the cause, then kew epic unhold and kew epic retry"


def resource_retry_head_hold_reason(head: str) -> str:
    """The reason for a second ``resource`` failure from one head."""
    where = head[:RESOURCE_RETRY_HEAD_LEN] if head else "an unrecorded head"
    return safe_hold_reason(
        f"{RESOURCE_RETRIES_SPENT_HOLD_PREFIX}: ran out of a resource again "
        f"at {where}, after its automatic retry there; {_RESOURCE_RETRY_RECOVERY}"
    )


def resource_retry_cap_hold_reason(retries: int) -> str:
    """The reason for a ``resource`` failure once the node's cap is spent."""
    return safe_hold_reason(
        f"{RESOURCE_RETRIES_SPENT_HOLD_PREFIX}: ran out of a resource again "
        f"after {retries} automatic retries; {_RESOURCE_RETRY_RECOVERY}"
    )


#: Every hold-reason prefix :func:`holds_node_out_of_frontier` routes to HELD.
#: ``docs/cli/epic.md`` lists them where it says which holds ``kew epic
#: retry`` refuses; ``tests/test_docs_truthfulness.py`` keeps that list equal.
FRONTIER_HOLD_PREFIXES: tuple[str, ...] = (
    BUDGET_HOLD_PREFIX,
    WINDOW_BUDGET_HOLD_PREFIX,
    CONTAINMENT_HOLD_PREFIX,
    ADMISSION_REFUSED_HOLD_PREFIX,
    REVIEW_BUDGET_HOLD_PREFIX,
    REVIEW_SANDBOX_REFUSED_HOLD_PREFIX,
    REVIEW_LAUNCH_FAILED_HOLD_PREFIX,
    REVIEW_MULTIPLE_RESULTS_HOLD_PREFIX,
    REVIEW_FAILED_NO_VERDICT_HOLD_PREFIX,
    REVIEW_UNPARSEABLE_HOLD_PREFIX,
    REVIEW_RATE_LIMIT_EXHAUSTED_HOLD_PREFIX,
    REVIEW_GATE_PATHS_UNREAD_HOLD_PREFIX,
    PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX,
    BASE_INTEGRATION_CONFLICT_HOLD_PREFIX,
    BASE_INTEGRATION_FAILED_HOLD_PREFIX,
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    PUBLICATION_HELD_HOLD_PREFIX,
    TEST_DECLARATIONS_HOLD_PREFIX,
    DECLARATION_RECORD_FAILED_HOLD_PREFIX,
    PR_BODY_UNREADABLE_HOLD_PREFIX,
    REMEDIATION_NO_CHANGE_HOLD_PREFIX,
    REMEDIATION_NO_PROGRESS_HOLD_PREFIX,
    RESOURCE_RETRIES_SPENT_HOLD_PREFIX,
)


def holds_node_out_of_frontier(reason: str | None) -> bool:
    """Whether this hold reason takes the node out of the lifecycle entirely.

    One predicate for every hold the driver *honors*: the CI-pending gate, the
    governed-review reasons (N3, including the two a reviewer that never
    started earns, #2043/#2147, and the spent rate-limit wait, #2219), the
    prefixed budget/containment/admission child refusals
    (#661/#1144/#1658), the merge-main step's two failures
    (#2149), the merge queue's gate-definition hold (#2184) and a remediation
    whose own publication was deferred for a person (#2362), a
    declaration-only round whose remediation declared nothing — or whose
    record or PR body could not be written or read (#2872) — an ordinary
    round whose remediation changed nothing (#3233) or pushed nothing
    (#3475), an
    implementer's acceptance dispute (#2731), and a node that ran out of a
    resource again after its automatic retries (#3191). They differ in
    how they clear, never in what they mean here.
    """
    if reason is None:
        return False
    return (
        is_ci_pending_hold(reason)
        or reason == ACCEPTANCE_DISPUTED_HOLD_REASON
        or reason in REVIEW_HOLD_REASONS
        or reason.startswith(FRONTIER_HOLD_PREFIXES)
    )


def is_another_writers_hold(reason: str | None) -> bool:
    """Whether this honored hold belongs to a writer other than the merge pass.

    Every honored hold except the merge pass's own self-clearing
    ``ci_pending``: a hold the conflict hold must never replace, and one the
    merge-main step must never run past (#2869).
    """
    return not is_ci_pending_hold(reason) and holds_node_out_of_frontier(reason)


#: Hold reasons whose release has to grant a fresh review allowance with it.
#:
#: The round cap is the one hold an operator cannot clear by clearing it. The
#: cap is read from the node's round count, the count only restarts at an
#: ``epic_retries`` marker, and ``kew epic retry`` is the only writer of that
#: marker — so a bare unhold hands the driver's next review pass the same
#: standing NEEDS_CHANGES verdict against the same head with the count
#: unchanged, and it re-HOLDs the node for the same reason (#2154). Now that
#: retry refuses a held node, the operator would have to land one inside the
#: window between the unhold and that pass to recover at all. So the release
#: restarts the cycle in the same transaction: the fresh review allowance a
#: retry grants (F15) rides with the release that needs it.
#:
#: Only the two round bounds: the reviewer round cap and the pre-review gate's
#: own bound (#3201), whose count is the cycle's gate rounds and so re-HOLDs
#: the same way. Every other honored reason clears against something the
#: operator changed outside kew — a raised ``review.budget``, a mapped sandbox
#: image, a repaired credential — and the released node makes real progress (a
#: fresh reviewer round) on the next pass without a reset.
#:
#: Matched as a reason or a ``<reason>:`` prefix (:func:`restarts_review_cycle`):
#: the gate's hold carries its check as detail.
REVIEW_CYCLE_RESET_HOLD_REASONS = frozenset(
    {ROUNDS_EXHAUSTED_HOLD_REASON, PRE_REVIEW_GATE_EXHAUSTED_HOLD_PREFIX}
)


def reason_in(reason: str, reasons: Iterable[str]) -> bool:
    """Whether *reason* is one of *reasons*, bare or carrying ``: <detail>``."""
    return any(reason == name or reason.startswith(f"{name}:") for name in reasons)


def restarts_review_cycle(reason: str) -> bool:
    """Whether releasing a hold for *reason* restarts the node's review cycle."""
    return reason_in(reason, REVIEW_CYCLE_RESET_HOLD_REASONS)


#: Cap on a hold reason echoed back to an operator. An ``admission_refused``
#: reason carries the tail of a detached child's stderr, so the string is
#: untrusted text on its way to a terminal or a TUI toast: flatten it to one
#: printable line and keep it short.
HOLD_REASON_MAX_CHARS = 160


#: How much of an over-long reason's END the cap keeps, behind an ellipsis.
#:
#: A reason runs past the cap because it carries a PATH — a preserved checkout
#: (``publication_held``), the touched files of a merge gate — and the part of
#: a path that identifies it is its tail. Cut from the right, the cap echoed
#: ``publication_held: /long/scratch/...deferral0...`` and dropped the one
#: directory the documented recovery tells the operator to open (#2436). So the
#: cut takes the MIDDLE out instead. The front still keeps the larger share:
#: the reason's prefix routes the hold (``holds_node_out_of_frontier``) and the
#: remedy a pre-launch refusal names is written there, ahead of any path.
HOLD_REASON_TAIL_CHARS = 72

_HOLD_REASON_ELLIPSIS = "..."

#: Where the cut may land inside its share: between words, or between the
#: segments of a path, so neither side of the ellipsis is a broken word.
_CUT_BOUNDARIES = " /"


def _head_at_word(flat: str, width: int) -> str:
    """The first *width* characters, cut back to the last word that fits whole.

    A path keeps the ``/`` it is cut after, so the prefix that routes the hold
    and the head of its path both survive. A share with no boundary in it (one
    long token) is cut where it falls: keeping nothing would lose more than a
    split word does.
    """
    head = flat[:width]
    if flat[width] not in _CUT_BOUNDARIES:
        at = max(head.rfind(" "), head.rfind("/"))
        if at > 0:
            head = head[: at + 1] if head[at] == "/" else head[:at]
    return head.rstrip()


def _tail_at_word(flat: str, width: int) -> str:
    """The last *width* characters, from the first word or path segment whole.

    The live Monitor printed ``the conflict...lve executed, exit 1)`` off a cut
    that took ``resolve`` apart (#2847 P10). A path keeps its ``/`` so the
    segment the cut starts on still reads as a path.
    """
    tail = flat[-width:]
    if flat[-width - 1] in _CUT_BOUNDARIES:
        return tail
    starts = [at for at in (tail.find(" "), tail.find("/")) if at >= 0]
    if not starts:
        return tail
    at = min(starts)
    return tail[at + 1 :] if tail[at] == " " else tail[at:]


def safe_hold_reason(reason: str) -> str:
    """Flatten an untrusted hold reason into one short printable line.

    Idempotent: every operator surface re-flattens the stored reason before
    echoing it, so an already-capped reason has to come back unchanged rather
    than losing another few characters per hop. The cut lands between words on
    both sides of the ellipsis, never inside one.
    """
    flat = "".join(char for char in " ".join(reason.split()) if char.isprintable())
    if len(flat) > HOLD_REASON_MAX_CHARS:
        head = (
            HOLD_REASON_MAX_CHARS - len(_HOLD_REASON_ELLIPSIS) - HOLD_REASON_TAIL_CHARS
        )
        flat = (
            _head_at_word(flat, head)
            + _HOLD_REASON_ELLIPSIS
            + _tail_at_word(flat, HOLD_REASON_TAIL_CHARS)
        )
    return flat or "unknown"
