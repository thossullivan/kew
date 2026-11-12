"""Approval-gated, CI-green merge pass (Epic DAG Phase 2a driver primitive 4).

NON-NEGOTIABLE #4: the only trigger for a merge is a *verified human PR
approval* — GitHub's own `reviewDecision`/`latestReviews` computation. A
label (``ready-to-merge``, ``auto-mergeable``, or any other) NEVER
contributes to that decision; see `is_verified_approval`. There is no
autonomous, no-human merge path in this module, and none should be added.

CI-greenness is read from GitHub's own computed `mergeStateStatus` via
`GitHubFactsReader.required_checks_green`, and then from the head's own check
runs via `GitHubFactsReader.head_checks` (#2033): GitHub reports a skipped
required check as passing, and private-repository CI skips an agent PR's
self-hosted tests until a human adds `ci-approved`. Both must be green. The
check-run decision is `kew.ci_checks`, shared with the loop ``ci_green`` gate.

The merge argv is fixed and MUST always be exactly::

    gh pr merge <pr> --repo <repo> --squash --match-head-commit <sha>

and MUST NEVER include ``--admin`` or ``--force``: those flags let `gh`
merge over a red/pending required check or a stale base, which would
silently defeat the CI-green gate above.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from kew.audit import Actor, emit
from kew.ci_checks import AWAITING_APPROVAL_EVIDENCE, HUMAN_MERGE_EVIDENCE
from kew.claim_retention import LINEAR_CLAIM_RECOVERY, record_retained_claim
from kew.epic_holds import (
    CI_PENDING_HOLD_REASON,
    ci_pending_hold_reason,
    holds_node_out_of_frontier,
    is_ci_pending_hold,
)
from kew.epic_node_states import MERGED_NOT_CLOSED_REASON
from kew.graph.driver.facts import (
    GitHubFactsReader,
    NodeFacts,
    RepositoryPermission,
    RunFn,
)
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_HEAD_MOVED,
    MERGE_VERIFY_HEAD_UNREADABLE,
    MERGE_VERIFY_NO_BRANCH,
    MergeVerifier,
    MergeVerifyResult,
    _unchecked_refusal,
)
from kew.ids import GITHUB_ID_RE, InvalidItemIdError, validate_item_id
from kew.merge_verdict import (
    MERGE_VERDICT_REFUSED,
    OVERRIDABLE_REFUSALS,
    REFUSED_NEEDS_CHANGES,
    judge_head,
)
from kew.pr_text import break_closing_references
from kew.review_residuals import acceptance_on_record
from kew.run_store import RunStore
from kew.subprocess_utils import run as _default_run

if TYPE_CHECKING:
    from kew.linear.tracker import LinearTracker
    from kew.run_store import ReviewRound

_logger = logging.getLogger(__name__)

MergeAction = Literal["merged", "held", "stuck", "skip"]

# The READY verdict is the only one that clears the governed-review merge gate.
_VERDICT_READY = "READY"

# Skip reasons the blocking-mode governed-review invariant (N1) returns when a
# policy-covered candidate must NOT merge. All three mean "awaiting re-review or
# re-approval", not a hard stall — `tick._is_awaiting_merge` classifies them as
# AWAITING_MERGES so the driver signal stays honest.
REVIEW_SKIP_NOT_READY = "review-not-ready"
REVIEW_SKIP_HEAD_MOVED = "review-head-moved"
REVIEW_SKIP_STALE_APPROVAL = "review-stale-approval"

#: The one merge verdict rule's standing-verdict skips (#3196): a NEEDS_CHANGES
#: or NEEDS_FULL attested on the head awaits re-review, exactly like a
#: non-READY round. Its other refusals (an unknown verdict, an unreadable
#: ledger or verdict row) are a broken pipeline and stay STALLED.
MERGE_VERDICT_SKIPS = frozenset(
    f"{MERGE_VERDICT_REFUSED}-{code}" for code in OVERRIDABLE_REFUSALS
)

REVIEW_BLOCK_REASONS = (
    frozenset(
        {REVIEW_SKIP_NOT_READY, REVIEW_SKIP_HEAD_MOVED, REVIEW_SKIP_STALE_APPROVAL}
    )
    | MERGE_VERDICT_SKIPS
)

# The issue label the dispatcher adds when a node's PR opens for review
# (`config.labels.pr_pending_review`). The merge pass strips it when it
# observes the PR closed-unmerged so a stale label can't wedge a retried
# node as IN_REVIEW in the overlay (#753).
_PENDING_REVIEW_LABEL = "pr-pending-review"

# The verdict reason `_handle_merged` returns once it has done the terminal
# bookkeeping for a PR that merged out-of-band (Linear: `tracker.close` verified
# CLOSED; GitHub: relies on the squash `Closes #N` auto-close). The action is
# ``skip`` — kew didn't merge — but the node IS now terminal-done, so the tick
# must fold it into its post-action summary alongside `merged` (SFD-139).
MERGED_OUT_OF_BAND_REASON = "already-merged-out-of-band"

#: GitHub has not finished computing the PR's mergeability. A held verdict that
#: clears itself: no hold row is written, so there is no stall clock, and the
#: next pass re-polls a fresh answer (#2183).
MERGEABILITY_UNKNOWN_REASON = "mergeability-unknown"

#: The held verdict `_head_checks_hold` writes while the head's own check runs
#: are still in progress — or while `gh` could not be read, which
#: `GitHubFactsReader.head_checks` reports as ``pending`` too. Self-healing in
#: exactly the way `MERGEABILITY_UNKNOWN_REASON` is: no hold row, no stall
#: clock, and the next pass re-polls (#2183).
CI_PENDING_VERDICT_PREFIX = "ci-pending: "

#: The same still-pending read over a node an earlier red-CI pass already holds
#: on a sticky ``ci_pending`` row. NOT self-healing — the row, and the stall
#: clock that reads it, are live — so it gets its own reason word rather than
#: sharing `CI_PENDING_VERDICT_PREFIX` with the transient one (#2183).
STICKY_CI_PENDING_VERDICT_PREFIX = "ci-pending-held: "


def is_self_healing_hold(reason: str) -> bool:
    """Whether a ``held`` reason is one GitHub clears by itself (#2183).

    The two transient holds — mergeability still being computed, and head
    checks still running — write no hold row, so they have no stall clock and
    the PR under them is still open awaiting the same merge. Every OTHER
    non-``ci_pending`` hold (an unverifiable head, a refused containment
    posture, a gate that could not run) names a broken pipeline an operator
    must fix, and STALLED is the signal that says so.

    Still-pending checks over a node an earlier red-CI pass already holds are
    NOT one of the two: that node has a live row and a running stall clock, so
    it gets `STICKY_CI_PENDING_VERDICT_PREFIX` and a ``ci_pending`` held reason
    instead (`_pending_checks_verdict`).
    """
    return reason == MERGEABILITY_UNKNOWN_REASON or reason.startswith(
        CI_PENDING_VERDICT_PREFIX
    )


@dataclass(frozen=True)
class MergeVerdict:
    """The outcome of classifying/acting on one graph node in a merge pass."""

    node_id: str
    action: MergeAction
    reason: str
    held_reason: str | None = None


def _author_login(pr: dict[str, Any]) -> str | None:
    author = pr.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) and login else None


@dataclass(frozen=True)
class ApprovalDecision:
    """Evidence resolved once for a single merge decision."""

    review: dict[str, Any]
    reviewer: str
    actor_type: str
    permission: str


PermissionResolver = Callable[[str], RepositoryPermission | None]
_MERGE_PERMISSIONS = frozenset({"write", "maintain", "admin"})


def _latest_approval(pr: dict[str, Any]) -> dict[str, Any] | None:
    approvals = [
        review
        for review in pr.get("latestReviews") or []
        if isinstance(review, dict) and review.get("state") == "APPROVED"
    ]
    if not approvals:
        return None

    def submitted_at(review: dict[str, Any]) -> str:
        value = review.get("submittedAt")
        return value if isinstance(value, str) else ""

    return max(approvals, key=submitted_at)


def _human_reviewer_login(review: dict[str, Any], pr_author: str) -> str | None:
    login = _author_login(review)
    review_author = review.get("author")
    if login is None or login == pr_author or not isinstance(review_author, dict):
        return None
    typename = review_author.get("__typename")
    is_bot = review_author.get("is_bot")
    if typename == "Bot" or is_bot is True:
        return None
    return login if typename == "User" or is_bot is False else None


def _authorized_permission(
    login: str, resolve_permission: PermissionResolver
) -> RepositoryPermission | None:
    try:
        evidence = resolve_permission(login)
    except Exception:
        return None
    if evidence is None or evidence.login != login or evidence.actor_type != "User":
        return None
    return evidence if evidence.permission in _MERGE_PERMISSIONS else None


def _approval_decision(
    pr: dict[str, Any],
    approvers: list[str],
    resolve_permission: PermissionResolver,
) -> ApprovalDecision | None:
    """Resolve one fail-closed human approval and its current authority.

    A verified approval requires ALL of:
      - GitHub's current review decision is APPROVED.
      - The newest latest approving review has a present, non-author User.
      - A fresh permission read grants write, maintain, or admin.
      - A configured allowlist additionally contains the reviewer.
    """
    if pr.get("reviewDecision") != "APPROVED":
        return None
    author = _author_login(pr)
    review = _latest_approval(pr)
    if author is None or review is None:
        return None
    login = _human_reviewer_login(review, author)
    if login is None:
        return None
    if approvers and login not in approvers:
        return None
    evidence = _authorized_permission(login, resolve_permission)
    if evidence is None:
        return None
    return ApprovalDecision(review, login, evidence.actor_type, evidence.permission)


def is_verified_approval(
    pr: dict[str, Any],
    approvers: list[str],
    resolve_permission: PermissionResolver | None = None,
) -> bool:
    """True iff `pr` carries a genuine, non-author, human-verified approval.

    Missing permission resolution fails closed. A label never contributes.
    """
    if resolve_permission is None:
        return False
    return _approval_decision(pr, approvers, resolve_permission) is not None


def _strip_pending_review_label(
    issue_num: int, *, repo: str, run: RunFn, label: str
) -> None:
    """Remove a stale `pr-pending-review` label after a closed-unmerged PR.

    Best-effort (``check=False``): `gh` fails harmlessly if the label is
    already absent. This mirrors the dispatcher's label add on PR open so a
    rotting label can't wedge the node as IN_REVIEW (#753); the overlay's
    fail-safe (`_open_status`) no longer trusts the label alone regardless.
    """
    run(
        [
            "gh",
            "issue",
            "edit",
            str(issue_num),
            "--repo",
            repo,
            "--remove-label",
            label,
        ],
        check=False,
    )


def _clear_hold_and_strip_label(
    item_id: str,
    *,
    provider: str,
    repo: str,
    run: RunFn,
    store: RunStore,
    pending_review_label: str,
) -> None:
    """Post-terminal bookkeeping for a PR that's already MERGED or CLOSED.

    A terminal PR that kew didn't just merge itself can still carry a sticky
    ``ci_pending`` hold (the normal path in CI-dark repos: the driver holds the
    node, an operator hand-merges) — and that hold wins overlay precedence over
    CLOSED/DONE, wedging the whole epic (#782). Clear it, and strip the stale
    ``pr-pending-review`` label the same way ``_do_merge`` / the closed-unmerged
    path does (#753). Only the GitHub issue carries the label; a Linear item
    has none.
    """
    _clear_hold(store, item_id, provider=provider)
    if provider == "github":
        _strip_pending_review_label(
            int(item_id), repo=repo, run=run, label=pending_review_label
        )


def _handle_merged(
    node_id: str,
    *,
    item_id: str,
    facts_key: int | str,
    provider: str,
    tracker: LinearTracker | None,
    reader: GitHubFactsReader,
    repo: str,
    run: RunFn,
    store: RunStore,
    pending_review_label: str,
) -> MergeVerdict:
    """Terminal bookkeeping for a PR kew observes already MERGED (out-of-band).

    Mirror ``_do_merge``'s hold and label bookkeeping. Then verify that the
    provider item is closed. GitHub performs an idempotent issue close when a
    missing PR keyword prevented auto-close. Linear uses ``tracker.close``.
    A failed close returns ``stuck / merged-not-closed``.
    """
    _clear_hold_and_strip_label(
        item_id,
        provider=provider,
        repo=repo,
        run=run,
        store=store,
        pending_review_label=pending_review_label,
    )
    if not _verify_closed(
        item_id,
        facts_key,
        repo=repo,
        reader=reader,
        run=run,
        tracker=tracker,
    ):
        return MergeVerdict(node_id, "stuck", MERGED_NOT_CLOSED_REASON)
    return MergeVerdict(node_id, "skip", MERGED_OUT_OF_BAND_REASON)


def _handle_closed_unmerged(
    node_id: str,
    *,
    item_id: str,
    provider: str,
    tracker: LinearTracker | None,
    repo: str,
    run: RunFn,
    store: RunStore,
    pending_review_label: str,
) -> MergeVerdict:
    """Clear stale holds for a closed PR and retain the Linear claim."""
    if tracker is not None:
        _clear_hold_and_strip_label(
            item_id,
            provider=provider,
            repo=repo,
            run=run,
            store=store,
            pending_review_label=pending_review_label,
        )
        record_retained_claim(store, item_id, reason="closed-unmerged")
        _logger.warning(
            "Linear claim retained for %s: %s", item_id, LINEAR_CLAIM_RECOVERY
        )
    return MergeVerdict(node_id, "stuck", "closed-unmerged")


def _verify_closed(
    item_id: str,
    facts_key: int | str,
    *,
    repo: str,
    reader: GitHubFactsReader,
    run: RunFn,
    tracker: LinearTracker | None,
) -> bool:
    """Ensure the work item is CLOSED after a successful merge.

    Re-check first. Close an open Linear item through ``tracker.close``. Close
    an open GitHub issue through ``gh issue close --reason completed``. Re-read
    once more instead of trusting either close command's return value.
    """
    # The reader is provider-dispatched: the GitHub reader takes an int key,
    # the Linear reader a str item id. facts_key is the matching type at
    # runtime; the shared param is typed GitHubFactsReader (int) for both.
    if reader.issue_facts(facts_key).state == "CLOSED":  # type: ignore[arg-type]
        return True
    if tracker is not None:
        tracker.close(item_id)
    else:
        run(
            [
                "gh",
                "issue",
                "close",
                str(facts_key),
                "--repo",
                repo,
                "--reason",
                "completed",
            ],
            check=False,
        )
    return reader.issue_facts(facts_key).state == "CLOSED"  # type: ignore[arg-type]


def _attest(
    store: RunStore,
    *,
    issue_num: int,
    item_id: str,
    repo: str,
    reviewer: str,
    actor_type: str,
    permission: str,
    pr_number: int,
    sha: str,
) -> None:
    """Write the merge attestation. `trigger` is always "approval" in 2a.

    ``item_id`` addresses the event by durable work-item key (hashed on rows
    written from v77 on); ``issue_num`` stays the 0-sentinel for a
    non-numeric tracker.
    """
    emit(
        store,
        "epic_merge",
        actor=Actor("human", reviewer),
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload={
            "trigger": "approval",
            "actor": reviewer,
            "actor_type": actor_type,
            "permission": permission,
            "sha": sha,
            "pr": pr_number,
        },
    )


def _hold_node(
    store: RunStore, epic_ref: str, item_id: str, reason: str, *, provider: str
) -> None:
    """Hold via the item-id façade for GitHub (issue number rides as its str)
    or the item-keyed method for a non-numeric tracker."""
    if provider == "github":
        store.hold_epic_node(epic_ref, item_id, reason)
    else:
        store.hold_epic_node_item(epic_ref, item_id, reason)


def _hold_ci_pending(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    *,
    provider: str,
    merge_state: str | None = None,
) -> None:
    """Write the merge pass's own ``ci_pending`` hold — never over someone else's.

    ``hold_epic_node`` UPSERTS the reason, and ``ci_pending`` is the one reason
    this pass owns: it means "the PR is not green/mergeable right now", it is
    re-derived from ``mergeStateStatus`` on every pass, and it clears by itself
    the moment that state changes (:func:`_release_ci_hold`). Every other honored
    reason belongs to a different writer, says something this one cannot — the
    conflicting paths of a merge-main step (#2149), the pool and spend of a
    budget refusal, the image an operator has to map — and clears only on a
    ``kew epic unhold``. Overwriting one destroyed that detail and downgraded an
    unhold-only hold to a self-clearing one, so the node looked recoverable and
    was not.

    The refusal is not silent: the caller still returns a ``held`` verdict with
    ``held_reason="ci_pending"``, so the tick reports the node held and the stall
    clock (which reads the hold ROW) is unchanged — it just keeps running against
    the reason that is actually blocking the node.

    ``merge_state`` is GitHub's ``mergeStateStatus`` when this pass read one, and
    it rides along on the reason (``ci_pending:dirty``, ``ci_pending:behind``,
    #2303) so a surface can tell the conflict a person has to rebase from the
    stale base the factory catches up to on its own. Every reader matches the
    gate with :func:`kew.epic_holds.is_ci_pending_hold`, so the detail is
    additive: the hold still clears itself, still routes to HELD, and is still
    the one reason this pass may overwrite.
    """
    existing = next(
        (h.reason for h in store.list_epic_holds(epic_ref) if h.item_id == item_id),
        None,
    )
    if not is_ci_pending_hold(existing) and holds_node_out_of_frontier(existing):
        _logger.info(
            "epic %s: not downgrading hold %r to %s — the node is already held "
            "for a reason this pass does not own",
            item_id,
            existing,
            CI_PENDING_HOLD_REASON,
        )
        return
    _hold_node(
        store,
        epic_ref,
        item_id,
        ci_pending_hold_reason(merge_state),
        provider=provider,
    )


def _clear_hold(store: RunStore, item_id: str, *, provider: str) -> None:
    if provider == "github":
        store.clear_epic_hold(item_id)
    else:
        store.clear_epic_hold_item(item_id)


def _squash_argv(pr_number: int, repo: str, head_oid: str, title: object) -> list[str]:
    """The squash merge, its subject the PR's title when the PR facts carry one.

    The agent PR's title states what the product now does (#3187); pinning it
    as the subject lands that sentence on main whatever the repository's
    default squash message is. A closing keyword with a reference in it is
    broken, so the commit on main closes no other issue. With no title in the
    facts the squash keeps GitHub's default subject, and the log says so.
    """
    argv = ["gh", "pr", "merge", str(pr_number), "--repo", repo, "--squash"]
    argv += ["--match-head-commit", head_oid]
    folded = " ".join(title.split()) if isinstance(title, str) else ""
    if not folded:
        _logger.warning(
            "merge: PR #%s has no title in its facts; squashing under GitHub's "
            "default subject",
            pr_number,
        )
        return argv
    return [*argv, "--subject", f"{break_closing_references(folded)} (#{pr_number})"]


def _do_merge(
    node_id: str,
    *,
    item_id: str,
    facts_key: int | str,
    provider: str,
    tracker: LinearTracker | None,
    pr_number: int,
    head_oid: str,
    reviewer: str,
    actor_type: str,
    permission: str,
    reader: GitHubFactsReader,
    store: RunStore,
    run: RunFn,
    repo: str,
    pending_review_label: str,
    title: object = None,
) -> MergeVerdict:
    result = run(_squash_argv(pr_number, repo, head_oid, title), check=False)
    if result.returncode != 0:
        # A human may have merged between our classification and command.
        # Re-read exactly once; reconcile that terminal fact through the
        # external path and never attest it as a kew-performed merge.
        if reader.pr_facts(pr_number).get("state") == "MERGED":
            return _handle_merged(
                node_id,
                item_id=item_id,
                facts_key=facts_key,
                provider=provider,
                tracker=tracker,
                reader=reader,
                repo=repo,
                run=run,
                store=store,
                pending_review_label=pending_review_label,
            )
        return MergeVerdict(node_id, "stuck", "merge-failed")

    _clear_hold_and_strip_label(
        item_id,
        provider=provider,
        repo=repo,
        run=run,
        store=store,
        pending_review_label=pending_review_label,
    )

    if not _verify_closed(
        item_id, facts_key, repo=repo, reader=reader, run=run, tracker=tracker
    ):
        return MergeVerdict(node_id, "stuck", MERGED_NOT_CLOSED_REASON)

    # A non-numeric item attests under the 0 sentinel (issue_num) but is
    # addressable by its durable item_id (v24, hashed on rows from v77 on).
    _attest(
        store,
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        reviewer=reviewer,
        actor_type=actor_type,
        permission=permission,
        pr_number=pr_number,
        sha=head_oid,
    )
    return MergeVerdict(node_id, "merged", "approved+ci-green")


def _approval_submitted_at(decision: ApprovalDecision) -> str | None:
    """The `submittedAt` of the verified approving review, or None.

    Derives from the same lookup as `_verified_reviewer` — the timestamp instead
    of the login, so the invariant can prove the approval postdates the verdict
    (F6). A missing/empty `submittedAt` reads as "no timestamp" → the approval
    cannot be proven fresh, so the gate blocks (fail closed).
    """
    submitted = decision.review.get("submittedAt")
    return submitted if isinstance(submitted, str) and submitted else None


def _approval_postdates(approved_at_iso: str, verdict_at: datetime) -> bool:
    """True iff the approval timestamp is strictly after the verdict's.

    An unparseable or naive approval timestamp fails closed (never postdates) —
    the gate would rather ask for a re-approval than merge on an unprovable one.
    """
    try:
        approved = datetime.fromisoformat(approved_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if approved.tzinfo is None:
        return False
    return approved > verdict_at


def _review_block_reason(
    latest: ReviewRound | None,
    *,
    head_oid: str,
    decision: ApprovalDecision,
    residuals_accepted: bool = False,
) -> str | None:
    """The governed-review invariant, as a single reason string or None (pass).

    merge only if latest verdict READY ∧ verdict.head_sha == current head SHA ∧
    the approval postdates the verdict. The checks run in that order so the most
    specific "why not" wins: not-ready before head-moved before stale-approval.
    ``residuals_accepted`` is the stop rule's attested acceptance of the latest
    NEEDS_CHANGES round (#2112), which reads as READY; the head and approval
    checks still apply to it unchanged.
    """
    ready = latest is not None and (
        latest.verdict == _VERDICT_READY or residuals_accepted
    )
    if latest is None or not ready:
        return REVIEW_SKIP_NOT_READY
    if latest.head_sha != head_oid:
        return REVIEW_SKIP_HEAD_MOVED
    approved_at = _approval_submitted_at(decision)
    if approved_at is None or not _approval_postdates(approved_at, latest.recorded_at):
        return REVIEW_SKIP_STALE_APPROVAL
    return None


def _attest_review_skip(
    store: RunStore,
    *,
    item_id: str,
    repo: str,
    reason: str,
    pr_number: int,
    head_sha: str,
) -> None:
    """Attest a governed-review merge block (Task 11). The per-round verdict
    event is Task 13's richer fact; this records that the merge pass refused to
    merge a candidate and why, so the refusal is never silent."""
    emit(
        store,
        "review_merge_blocked",
        actor=Actor("policy", "epic-driver"),
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload={"reason": reason, "pr": pr_number, "head_sha": head_sha},
    )


def _node_has_review_hold(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """True iff *item_id* carries a ``review_*`` hold under *epic_ref*.

    A ``review_*`` hold means the review subsystem already touched this node
    (e.g. ``review_pr_closed``, or a future review-pool budget refusal), so it
    is NOT a pre-policy node even with zero recorded rounds — the grandfather
    must not fire for it (#980 Finding 2, defense-in-depth)."""
    return any(
        h.item_id == item_id and h.reason.startswith("review_")
        for h in store.list_epic_holds(epic_ref)
    )


def _node_has_review_run(store: RunStore, item_id: str) -> bool:
    """True iff a governed-review run was ever recorded for *item_id*.

    A review run row means a reviewer was dispatched for this node — so it is
    NOT a pre-policy node even with zero recorded rounds. A round is only
    written once a reviewer produced a parseable verdict; every attempt that
    ended short of one (a sandbox refusal the review stage defers for a bounded
    retry (#2147), a launch failure, a reviewer still in flight) leaves this row
    and NO ``review_*`` hold, which would otherwise re-arm the PRE_POLICY
    grandfather and auto-merge a blocking-review node with zero verdicts."""
    from kew.run_store import REVIEW_RUN_KINDS

    return any(r.run_kind in REVIEW_RUN_KINDS for r in store.list_by_item(item_id))


def _merge_verdict_refusal(store: RunStore, item_id: str, head_oid: str) -> str | None:
    """The one merge verdict rule (#3196) for *head_oid*, as a skip reason or None.

    Runs on every merge the driver makes, whatever the epic's review mode and
    whether or not the node is PRE_POLICY grandfathered. A NEEDS_CHANGES reads
    as READY only while the node's latest round is that head's accepted one, on
    both of its records (#2151); the rounds are read only for a NEEDS_CHANGES.
    """
    check = judge_head(store, head_oid)
    if check.refusal == REFUSED_NEEDS_CHANGES and _accepted_round_at(
        store, item_id, head_oid
    ):
        check = judge_head(store, head_oid, accepted=True)
    return None if check.refusal is None else f"{MERGE_VERDICT_REFUSED}-{check.refusal}"


def _accepted_round_at(store: RunStore, item_id: str, head_oid: str) -> bool:
    """Whether the node's latest round is an accepted NEEDS_CHANGES on *head_oid*."""
    latest = store.latest_review_round(item_id)
    return (
        latest is not None
        and latest.head_sha == head_oid
        and latest.verdict != _VERDICT_READY
        and acceptance_on_record(store, item_id, latest)
    )


def _is_pre_policy(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """Zero rounds, no review hold and no review run: the PRE_POLICY grandfather."""
    return (
        store.review_round_count(item_id) == 0
        and not _node_has_review_hold(store, epic_ref, item_id)
        and not _node_has_review_run(store, item_id)
    )


def _blocked_merge(
    node_id: str,
    reason: str | None,
    *,
    item_id: str,
    head_oid: str,
    pr_number: int,
    store: RunStore,
    repo: str,
) -> MergeVerdict | None:
    """The attested ``skip`` for a merge gate's *reason*, or None when it passed."""
    if reason is None:
        return None
    _logger.warning(
        "epic %s: merge BLOCKED by the merge gate for %s (PR #%s): %s",
        item_id,
        item_id,
        pr_number,
        reason,
    )
    _attest_review_skip(
        store,
        item_id=item_id,
        repo=repo,
        reason=reason,
        pr_number=pr_number,
        head_sha=head_oid,
    )
    return MergeVerdict(node_id, "skip", reason)


def _verdict_gate(
    node_id: str,
    *,
    item_id: str,
    head_oid: str,
    pr_number: int,
    store: RunStore,
    repo: str,
) -> MergeVerdict | None:
    """The merge gate of an epic whose review is not blocking: the one merge
    verdict rule alone (#3196), with no READY round required."""
    return _blocked_merge(
        node_id,
        _merge_verdict_refusal(store, item_id, head_oid),
        item_id=item_id,
        head_oid=head_oid,
        pr_number=pr_number,
        store=store,
        repo=repo,
    )


def _review_gate(
    node_id: str,
    *,
    item_id: str,
    decision: ApprovalDecision,
    head_oid: str,
    pr_number: int,
    store: RunStore,
    repo: str,
    epic_ref: str,
) -> MergeVerdict | None:
    """Blocking-mode governed-review invariant (N1 — the load-bearing gate).

    Returns a loud, attested `skip` verdict when a policy-covered candidate must
    NOT merge, or None when the invariant passes OR the node is PRE_POLICY
    grandfathered (no verdict recorded — the flag flipped after it was already in
    review), in which case merge proceeds under the pre-existing approval+CI
    rules. Either way the one merge verdict rule (#3196) applies on top, so a
    grandfathered head with an attested NEEDS_CHANGES still does not merge.
    Enforced at the merge pass itself, not the status label: HELD is a
    live merge candidate, so gating only the transition would leave the
    HELD-is-mergeable hole open.

    The grandfather keys on *zero rounds AND no review hold AND no review run*:
    a node the review subsystem has already held (a ``review_*`` reason) or
    already dispatched a reviewer for is never treated as pre-policy even with
    zero rounds, so neither a review refusal that HELDs before writing round #1
    (#980 Finding 2) nor one the review stage defers for a bounded retry without
    holding (#2147) can be merged unreviewed.
    """
    reason = None
    if not _is_pre_policy(store, epic_ref, item_id):
        latest = store.latest_review_round(item_id)
        reason = _review_block_reason(
            latest,
            head_oid=head_oid,
            decision=decision,
            # Both the chain event and the round's own mark, never one alone: a
            # half-written acceptance is a state this gate's readers disagree
            # about, so it waits for the review stage to finish it (#2151).
            residuals_accepted=acceptance_on_record(store, item_id, latest),
        )
    if reason is None:
        reason = _merge_verdict_refusal(store, item_id, head_oid)
    return _blocked_merge(
        node_id,
        reason,
        item_id=item_id,
        head_oid=head_oid,
        pr_number=pr_number,
        store=store,
        repo=repo,
    )


def _merge_if_still_ready(
    review_gate: Callable[[], MergeVerdict | None],
    merge: Callable[..., MergeVerdict],
    **fresh: Any,
) -> MergeVerdict:
    """Re-run the governed-review gate at the moment of merging, then merge.

    The gate's first run reads the latest round before the CI reads and, in the
    merge queue, before a verification that can run for many minutes. A round
    recorded in that window is otherwise lost — an operator's ``kew epic
    send-back`` above all (#3070), which takes only the item's review
    reservation, a lock this pass never holds; and after its verification the
    queue re-reads the head, never the verdict. So the latest round is re-read
    here, immediately before ``gh pr merge``, and the merge goes ahead only if
    it is still READY for this exact head. Where the epic's review is not
    blocking, *review_gate* is the merge verdict rule alone, re-read here for
    the same reason. *fresh* overrides what the merge was bound with at
    classification, such as the PR title a verification re-read.
    """
    refusal = review_gate()
    return refusal if refusal is not None else merge(**fresh)


# Head-check states that wait on a human, not on CI (#2033).
_HUMAN_CI_GATES = frozenset({"awaiting_approval", "no_evidence"})


def is_human_ci_gate(reason: str) -> bool:
    """Whether a ``skip`` reason is a head-check human gate (#2033)."""
    return reason.startswith(AWAITING_APPROVAL_EVIDENCE) or (
        reason in HUMAN_MERGE_EVIDENCE
    )


def _has_ci_pending_hold(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """Whether *item_id* already carries a ``ci_pending`` hold ROW — the row the
    stall clock reads, so the one fact that decides whether a held verdict is
    still self-healing."""
    return any(
        hold.item_id == item_id and is_ci_pending_hold(hold.reason)
        for hold in store.list_epic_holds(epic_ref)
    )


def _release_ci_hold(
    store: RunStore, epic_ref: str, item_id: str, *, provider: str
) -> None:
    """Clear only a ``ci_pending`` hold this node carries; others stay."""
    if _has_ci_pending_hold(store, epic_ref, item_id):
        _clear_hold(store, item_id, provider=provider)


def _pending_checks_verdict(
    node_id: str,
    evidence: str,
    *,
    store: RunStore,
    epic_ref: str,
    item_id: str,
) -> MergeVerdict:
    """The held verdict for a head whose own checks are still in progress — or
    whose checks could not be read at all, which ``head_checks`` reports as
    ``pending`` too (#2183).

    With no hold row under the node this is self-healing: nothing is persisted,
    so there is no stall clock and the next pass re-polls a fresh answer. A
    ``ci_pending`` row an earlier red-CI pass wrote is a different node, and
    this pass neither clears it nor may call it transient. It does not clear it
    because an unreadable ``gh`` arrives here as ``pending`` as well, so a
    release would reset a real red-CI stall clock on every failed read and the
    clock could never expire. And it may not call it transient because the row
    is what both operator surfaces read: reported self-healing over a live row,
    ``kew epic advance`` answered AWAITING_MERGES with no PR under it while
    ``kew epic status`` read the node held — and stalled once the clock
    expired — so `kew epic run` polled forever instead of exiting for the
    repair the node owes. The row and its clock stay, and the verdict carries
    ``held_reason="ci_pending"`` so both readings come off that one row.
    """
    if _has_ci_pending_hold(store, epic_ref, item_id):
        return MergeVerdict(
            node_id,
            "held",
            f"{STICKY_CI_PENDING_VERDICT_PREFIX}{evidence}",
            CI_PENDING_HOLD_REASON,
        )
    return MergeVerdict(node_id, "held", f"{CI_PENDING_VERDICT_PREFIX}{evidence}")


def _head_checks_hold(
    node_id: str,
    checks: tuple[str, str],
    *,
    store: RunStore,
    epic_ref: str,
    item_id: str,
    provider: str,
) -> MergeVerdict | None:
    """Hold a CLEAN PR whose head's own checks are not green (#2033).

    Tests awaiting ``ci-approved``, or a head with no CI evidence at all, is a
    human merge gate: a ``skip`` the tick counts as AWAITING_MERGES, like a
    missing approval, re-evaluated every pass. A ``ci_pending`` hold an earlier
    pass left is released, so no ``kew epic unhold`` is needed. Pending (or
    unreadable) is still settling: a transient hold with no hold row, like an
    UNKNOWN mergeability — unless a red-CI pass already wrote the sticky row,
    which stays and keeps its stall clock (`_pending_checks_verdict`). A
    failure, or no successful check at all, is red CI: the sticky ``ci_pending``
    hold the BLOCKED path uses.
    """
    state, evidence = checks
    if state == "success":
        return None
    if state in _HUMAN_CI_GATES:
        _release_ci_hold(store, epic_ref, item_id, provider=provider)
        return MergeVerdict(node_id, "skip", evidence)
    if state == "pending":
        return _pending_checks_verdict(
            node_id, evidence, store=store, epic_ref=epic_ref, item_id=item_id
        )
    _hold_ci_pending(store, epic_ref, item_id, provider=provider)
    return MergeVerdict(
        node_id, "held", f"ci-{state}: {evidence}", CI_PENDING_HOLD_REASON
    )


def _verified_merge(
    node_id: str,
    *,
    item_id: str,
    pr_number: int,
    pr: dict[str, Any],
    head_oid: str,
    reader: GitHubFactsReader,
    merge_verify: MergeVerifier,
    merge: Callable[..., MergeVerdict],
) -> MergeVerdict:
    """Merge *inside* the merge queue's lease, or refuse (#2148).

    The lease is the queue's repo-wide lock, and ``merge()`` runs while it is
    still held: released at the verdict instead, a second epic driver could
    verify — and then merge — a combined tree built on a base that does not yet
    contain the merge this lease is about to release, which is the whole
    invalidation the lock exists to prevent.

    The head re-read after the verification also carries the PR's title, and
    a title there is the squash subject (#3187): a verification runs for many
    minutes, and a title corrected meanwhile is the one that lands.
    """
    branch = pr.get("headRefName")
    if not isinstance(branch, str) or not branch:
        # No branch name, no tree to verify. Fail closed rather than merge an
        # unverified head (the PR read is degraded, not the PR).
        _logger.error(
            "merge queue: %s has no headRefName on PR #%s — not merging",
            item_id,
            pr_number,
        )
        return MergeVerdict(node_id, "held", MERGE_VERIFY_NO_BRANCH)
    with merge_verify(
        item_id=item_id, pr_number=pr_number, head_oid=head_oid, branch=branch
    ) as verified:
        refusal, fresh = _merge_verify_gate(
            node_id,
            item_id=item_id,
            pr_number=pr_number,
            head_oid=head_oid,
            reader=reader,
            verified=verified,
        )
        if refusal is not None:
            return refusal
        title = fresh.get("title")
        return (
            merge(title=title) if isinstance(title, str) and title.strip() else merge()
        )


def _unrun_checks_gate(
    node_id: str,
    *,
    store: RunStore,
    provider: str,
    epic_ref: str,
    item_id: str,
    head_oid: str,
) -> MergeVerdict | None:
    """Refuse a no-queue merge of a READY that names checks nobody ran (#3421).

    No ``kew verify`` stands between this path and ``gh pr merge``, so a
    ``full_suite`` the review sandbox could not run is as unrun here as Docker.
    This path reads no diff of the head, so it cannot know a ``kew verify`` of
    it answers every browser check the diff needs (``www/`` has one no verify
    runs): a browser check is a person's, held and released by the head's
    receipt, whatever verify is on record.
    """
    refusal = _unchecked_refusal(
        store,
        provider=provider,
        epic_ref=epic_ref,
        item_id=item_id,
        head_oid=head_oid,
        verify_gate=False,
        ui_checkable=False,
    )
    if refusal is None:
        return None
    return MergeVerdict(node_id, refusal.action, refusal.reason)


def _merge_verify_gate(
    node_id: str,
    *,
    item_id: str,
    pr_number: int,
    head_oid: str,
    reader: GitHubFactsReader,
    verified: MergeVerifyResult,
) -> tuple[MergeVerdict | None, dict[str, Any]]:
    """The serial merge queue's verdict (#2148): merge, or refuse to, with the
    PR facts re-read after the verification (empty when it never got there).

    The verdict is None only when the combined tree verified green AND the head is
    still the head that was verified — a verification can take many minutes, and
    merging a head nobody verified is the hole this gate exists to close. The
    re-read goes through ``reader.fresh_pr_facts``, NOT ``pr_facts``: the latter
    is served from this tick's ``prepare`` batch, so it would hand back the very
    head the classification used and this check could never fire. An unreadable
    re-read is not "unchanged" — it holds, because a head we cannot read is a
    head we cannot bind. The merge argv's ``--match-head-commit`` then makes the
    same binding atomic at GitHub.
    """
    if not verified.ok:
        return MergeVerdict(node_id, verified.action, verified.reason), {}
    fresh = reader.fresh_pr_facts(pr_number)
    current = fresh.get("headRefOid")
    if not isinstance(current, str) or not current:
        _logger.error(
            "merge queue: %s verified but PR #%s's head is unreadable — not merging",
            item_id,
            pr_number,
        )
        return MergeVerdict(node_id, "held", MERGE_VERIFY_HEAD_UNREADABLE), fresh
    if current != head_oid:
        _logger.warning(
            "merge queue: %s head moved from %s to %s during verification — "
            "not merging",
            item_id,
            head_oid[:12],
            current[:12],
        )
        return MergeVerdict(node_id, "skip", MERGE_VERIFY_HEAD_MOVED), fresh
    return None, fresh


def _classify_open(
    node_id: str,
    *,
    item_id: str,
    facts_key: int | str,
    provider: str,
    tracker: LinearTracker | None,
    pr_number: int,
    pr: dict[str, Any],
    base: str,
    epic_ref: str,
    reader: GitHubFactsReader,
    approvers: list[str],
    store: RunStore,
    run: RunFn,
    repo: str,
    blocking_review: bool,
    pending_review_label: str,
    gated_checks: Sequence[str],
    merge_verify: MergeVerifier | None,
) -> MergeVerdict:
    decision = _approval_decision(pr, approvers, reader.repository_permission)
    if decision is None:
        return MergeVerdict(node_id, "skip", "awaiting-approval")

    # Governed-review invariant (N1): in blocking mode a policy-covered candidate
    # merges only against a current, evidenced READY verdict with a fresh
    # approval. In every mode the one merge verdict rule (#3196) applies too.
    # Runs AFTER the human-approval gate (an unapproved PR is already skipped)
    # and BEFORE the CI-green merge, so an approved+green HELD node with a
    # non-READY verdict is skipped, not merged. The same gate runs again
    # immediately before the merge itself (`_merge_if_still_ready`).
    gate_facts: dict[str, Any] = {
        "item_id": item_id,
        "head_oid": str(pr.get("headRefOid")),
        "pr_number": pr_number,
        "store": store,
        "repo": repo,
    }
    review_gate: Callable[[], MergeVerdict | None] = (
        partial(
            _review_gate, node_id, decision=decision, epic_ref=epic_ref, **gate_facts
        )
        if blocking_review
        else partial(_verdict_gate, node_id, **gate_facts)
    )
    blocked = review_gate()
    if blocked is not None:
        return blocked

    if reader.required_checks_green(base, pr):
        not_ran = _head_checks_hold(
            node_id,
            reader.head_checks(str(pr.get("headRefOid")), gated_checks),
            store=store,
            epic_ref=epic_ref,
            item_id=item_id,
            provider=provider,
        )
        if not_ran is not None:
            return not_ran
        merge = partial(
            _merge_if_still_ready,
            review_gate,
            partial(
                _do_merge,
                node_id,
                item_id=item_id,
                facts_key=facts_key,
                provider=provider,
                tracker=tracker,
                pr_number=pr_number,
                head_oid=str(pr.get("headRefOid")),
                reviewer=decision.reviewer,
                actor_type=decision.actor_type,
                permission=decision.permission,
                reader=reader,
                store=store,
                run=run,
                repo=repo,
                pending_review_label=pending_review_label,
                title=pr.get("title"),
            ),
        )
        if merge_verify is None:
            unrun = _unrun_checks_gate(
                node_id,
                store=store,
                provider=provider,
                epic_ref=epic_ref,
                item_id=item_id,
                head_oid=str(pr.get("headRefOid")),
            )
            return unrun if unrun is not None else merge()
        # The merge runs INSIDE the queue's lease, never after it (#2148).
        return _verified_merge(
            node_id,
            item_id=item_id,
            pr_number=pr_number,
            pr=pr,
            head_oid=str(pr.get("headRefOid")),
            reader=reader,
            merge_verify=merge_verify,
            merge=merge,
        )

    merge_state = pr.get("mergeStateStatus")
    if merge_state == "UNKNOWN":
        # Transient: GitHub is still computing mergeability. Not sticky —
        # no hold_epic_node call, so the next pass re-polls fresh.
        return MergeVerdict(node_id, "held", MERGEABILITY_UNKNOWN_REASON)

    # BLOCKED/BEHIND/DIRTY/UNSTABLE (or any other non-green value): a real
    # required-check-red/pending or stale-base condition. Sticky hold so
    # the overlay surfaces it until CI/base state changes.
    _hold_ci_pending(
        store, epic_ref, item_id, provider=provider, merge_state=str(merge_state)
    )
    return MergeVerdict(
        node_id,
        "held",
        f"ci-{str(merge_state).lower()}",
        ci_pending_hold_reason(str(merge_state)),
    )


def _classify_node(
    node_id: str,
    *,
    repo: str,
    base: str,
    epic_ref: str,
    provider: str,
    tracker: LinearTracker | None,
    reader: GitHubFactsReader,
    approvers: list[str],
    store: RunStore,
    run: RunFn,
    blocking_review: bool,
    pending_review_label: str,
    gated_checks: Sequence[str],
    merge_verify: MergeVerifier | None,
    observed: NodeFacts | None = None,
) -> MergeVerdict:
    try:
        item_id = validate_item_id(node_id, provider)
    except InvalidItemIdError:
        return MergeVerdict(node_id, "stuck", "non-int-id")

    # The GitHub facts reader is keyed on the int issue number; the Linear
    # reader on the item id itself. PR numbers are always GitHub-native.
    facts_key: int | str = int(item_id) if provider == "github" else item_id

    # Provider-dispatched key (int for GitHub, str item id for Linear); see
    # _verify_closed's note. The shared reader param is typed as the GitHub one.
    # The tick hands over the facts its overlay already read (#2582): the PR
    # number is all this needs, and re-reading the issue per candidate was one
    # GitHub call per node per tick. `_verify_closed` still re-reads, fresh.
    facts = (
        observed if observed is not None else reader.issue_facts(facts_key)  # type: ignore[arg-type]
    )
    if facts.pr_number is None:
        return MergeVerdict(node_id, "skip", "no-pr")

    pr = reader.pr_facts(facts.pr_number)  # fetched exactly once per node
    state = pr.get("state")

    if state == "MERGED":
        return _handle_merged(
            node_id,
            item_id=item_id,
            facts_key=facts_key,
            provider=provider,
            tracker=tracker,
            reader=reader,
            repo=repo,
            run=run,
            store=store,
            pending_review_label=pending_review_label,
        )
    if state == "CLOSED":
        return _handle_closed_unmerged(
            node_id,
            item_id=item_id,
            provider=provider,
            tracker=tracker,
            repo=repo,
            run=run,
            store=store,
            pending_review_label=pending_review_label,
        )
    if state != "OPEN":
        # Defensive: e.g. an empty dict from a gh fetch failure. Fail
        # closed (stuck) rather than silently skip a node that may in
        # fact be mergeable.
        return MergeVerdict(node_id, "stuck", "pr-state-unknown")

    return _classify_open(
        node_id,
        item_id=item_id,
        facts_key=facts_key,
        provider=provider,
        tracker=tracker,
        pr_number=facts.pr_number,
        pr=pr,
        base=base,
        epic_ref=epic_ref,
        reader=reader,
        approvers=approvers,
        store=store,
        run=run,
        repo=repo,
        blocking_review=blocking_review,
        pending_review_label=pending_review_label,
        gated_checks=gated_checks,
        merge_verify=merge_verify,
    )


def merge_pass(
    nodes: list[str],
    *,
    repo: str,
    base: str,
    epic_ref: str,
    reader: GitHubFactsReader,
    approvers: list[str],
    store: RunStore,
    run: RunFn | None = None,
    tracker: LinearTracker | None = None,
    blocking_review: bool = False,
    pending_review_label: str = _PENDING_REVIEW_LABEL,
    gated_checks: Sequence[str],
    merge_verify: MergeVerifier | None = None,
    observed: Mapping[str, NodeFacts] | None = None,
) -> list[MergeVerdict]:
    """Classify (and act on) each node id in `nodes` for this merge pass.

    One `MergeVerdict` per node, in input order. Each node's linked PR is
    fetched once via `reader.pr_facts` — a node the merge queue verified green
    additionally pays one deliberately uncached `reader.fresh_pr_facts` read for
    the post-verification head check. When *tracker* is a
    ``LinearTracker`` the node ids are validated as Linear item ids and the
    post-merge close goes through ``tracker.close``; otherwise the GitHub path
    (numeric ids, ``gh issue close``) runs unchanged.

    When *blocking_review* is True (a review-enabled BLOCKING epic), every
    policy-covered OPEN candidate is additionally gated by the governed-review
    invariant (N1, `_review_gate`): merge only against a current, evidenced READY
    verdict with a fresh approval. PRE_POLICY nodes (no verdict) are unaffected.
    Every OPEN candidate, in any mode and grandfathered or not, is gated by the
    one merge verdict rule (#3196, `_merge_verdict_refusal`): no merge while the
    newest real verdict attested for its head is NEEDS_CHANGES or NEEDS_FULL.

    When *merge_verify* is wired (``[epic] merge_queue``), the last gate before
    each merge is the serial merge queue (#2148, `_verified_merge`): the base
    merged into this head must pass the project's verify command, one candidate
    at a time, and the merge itself runs while the queue's lease is still held.
    Candidates are classified in input order, which the tick sorts, so the
    queue's order is the epic's node order.

    *observed* is the tick's own facts per node id, read once by its overlay;
    a node in it is classified on those rather than re-reading its issue.
    """
    provider = "linear" if tracker is not None else "github"
    run_fn = run or getattr(reader, "run", _default_run)
    return [
        _classify_node(
            node_id,
            repo=repo,
            base=base,
            epic_ref=epic_ref,
            provider=provider,
            tracker=tracker,
            reader=reader,
            approvers=approvers,
            store=store,
            run=run_fn,
            blocking_review=blocking_review,
            pending_review_label=pending_review_label,
            gated_checks=gated_checks,
            merge_verify=merge_verify,
            observed=(observed or {}).get(node_id),
        )
        for node_id in nodes
    ]
