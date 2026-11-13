"""One operator-performed merge through the serial merge queue (#2203).

The driver's own merge pass (:mod:`kew.graph.driver.merge`) will only merge on a
*verified human PR approval* from a non-author with write permission. In a repo
where one person authors every agent PR, GitHub cannot produce that approval:
the author cannot approve their own PR, so the pass correctly never fires and
every READY node waits for a merge that will never come. The owner's decision after
the trust review of #2148 was that the OPERATOR performs the merge instead —
by hand, one node at a time, naming the head they mean.

``kew epic merge <epic> <item> --head <sha>`` is that act, and this module is
its gate. The operator's authority replaces the forge approval and NOTHING
else: every other invariant the merge pass proves still has to hold, and each
one is proven here before the merge command runs.

1. ``[epic] merge_queue = true``. The queue is the only thing that proves the
   COMBINED tree is green, and without it this command would merge a head
   verified only against itself — the exact hole #2148 exists to close. With
   the queue off the command refuses rather than degrading to a weaker gate.
2. ``<item>`` is a node of ``<epic>``, by the epic's own journaled membership.
   Holds are keyed on the epic string exactly as typed, so a mistyped epic name
   would otherwise make the hold gate below VACUOUS — no row is ever found
   under a name nothing writes — and would attest the merge under an epic the
   node was never in. ``kew epic unhold`` refuses a wrong epic name; so does
   this.
3. The node carries no hold. A held node is one the driver has already refused
   to advance; releasing it is ``kew epic unhold``'s job, and a hold an
   operator merge stepped over would be a hold nobody ever read.
4. The node is still LIVE WORK by the overlay's own provider precedences
   (:func:`kew.graph.driver.overlay.settled_status`): the tracker item is not
   closed, and no zero-diff judgment is still deciding what the absence of a
   diff meant. The driver's merge pass only ever classifies IN_REVIEW/HELD
   candidates, so a node reading DONE, CANCELLED or AWAITING_NO_CHANGE is one
   it would never merge — and an item a human CANCELLED can keep an open,
   green, READY-reviewed PR behind it, which every other gate here passes.
   Merging that PR would land work the operator's own tracker says was
   abandoned, and attest it. An item kew could not READ refuses too: both
   readers fold a failed item read into the empty state, which the precedence
   reads as live work, so the emptiness is refused here instead.
5. The latest review round is READY — or is a NEEDS_CHANGES round whose
   residuals the stop rule accepted (#2112) — and its ``head_sha`` is EXACTLY
   ``--head``. A verdict recorded against another head has reviewed other code.
6. The PR head on the forge is EXACTLY ``--head``, read fresh (never from a
   tick batch), the PR is still open, and it targets the base and repository
   the tree is verified on — required readable under ``gate = "verify"``.
7. CI is green on BOTH gates the driver's merge pass applies to that head:
   GitHub's own computed ``mergeStateStatus`` (``required_checks_green``) AND
   the head's own check runs (``head_checks``, #2033). Both, because
   ``mergeStateStatus`` counts a SKIPPED required check as passing and
   private-repository CI skips an agent PR's self-hosted tests until a human
   adds ``ci-approved`` — through the first gate alone a PR whose tests never
   ran reads green. The operator's authority replaces the APPROVAL, not the
   tests: a red, pending, unreadable or never-run check refuses here exactly as
   it holds there.
8. The #2184 merge queue verifies the combined tree for that head against the
   base, live, under the repo-wide queue lock: the contained verify command,
   the gate-definition check (a PR that edits what the gates ARE is held until
   a human acknowledges its diff), and the base-OID re-read that proves nothing
   landed underneath the verification. A verification on record from an earlier
   tick would NOT do — the base moves, and a stale green is a green for a tree
   that no longer exists.
9. Every fact another actor can have moved under the verification (which can
   run for half an hour) is re-proven INSIDE the lease against a fresh read:
   the PR's open state, the forge head, its base branch, both CI gates, the
   item's own live-work state, the hold, and the review verdict. The hold and
   the verdict especially — the driver's own sticky ``ci_pending`` hold and
   the review stage's rounds are both written on paths that never take the
   queue lock, so a concurrent tick can hold this very node, or record a
   NEEDS_CHANGES round against this very head, WHILE the verification runs; a
   gate read only at the start would merge a node the driver has just refused
   and attest the superseded READY round. The round the attestation names is
   the RE-PROVEN one. A verifier that ran an acceptance contract binds its row
   id and version. The merge rechecks both immediately before merging. Main is
   also re-read immediately before the merge. The merge argv binds the head at
   GitHub with ``--match-head-commit``. Afterward, main's landed tree must equal
   the verified candidate tree before attestation.

Only then does the merge run, INSIDE the queue lease — released at the verdict
instead, a second merge could land on a base the verified tree did not contain.
The merge is attested as an ``epic_operator_merge`` event naming the actor, the
head and the base, so an operator-authority merge is as readable on the chain
as the driver's own ``epic_merge``.

TRUST BOUNDARY: not one of these facts is agent-writable. No label is read —
not ``ready-to-merge``, not ``pr-pending-review``, not any other. The review
verdict comes from the round table the review stage writes under its own
policy, the heads and the check-run conclusions come from GitHub, the
combined-tree greenness comes from a gate the operator configured, and the
authority comes from the human who typed the command.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from kew.audit import Actor, emit
from kew.config import DispatchConfig
from kew.graph.driver.facts import (
    GitHubFactsReader,
    NodeFacts,
    RunFn,
    read_no_change_facts,
)
from kew.graph.driver.merge import (
    REVIEW_SKIP_HEAD_MOVED,
    REVIEW_SKIP_NOT_READY,
)
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BASE_MOVED,
    MERGE_VERIFY_BASE_UNREADABLE,
    MergeVerifyResult,
    acceptance_contract_change,
    acceptance_contract_versions,
    verify_combined_tree,
)
from kew.graph.driver.overlay import DriverStatus, settled_status
from kew.ids import GITHUB_ID_RE
from kew.merge_queue_attention import (
    ATTENTION_EVENT,
    MAIN_UNVERIFIED,
    attention_payload,
)
from kew.merge_verdict import MERGE_VERDICT_REFUSED, check_merge_verdict
from kew.review_residuals import acceptance_on_record
from kew.run_store import RunStore
from kew.service.epics import epic_member_ids
from kew.subprocess_utils import run as _default_run

_logger = logging.getLogger(__name__)

#: The attested event one operator merge writes.
OPERATOR_MERGE_EVENT = "epic_operator_merge"

#: ``[epic] merge_queue`` is off, so no combined tree is ever verified.
QUEUE_DISABLED = "merge-queue-disabled"

#: The node is not a member of the named epic — or the epic has no journaled
#: membership at all, which for a merge is the same refusal.
NOT_AN_EPIC_MEMBER = "not-an-epic-member"

#: The node carries a driver hold; `kew epic unhold` owns releasing it.
NODE_HELD = "node-held"

#: The node is not live work: the tracker item is closed (DONE/CANCELLED) or a
#: zero-diff judgment is still deciding what the absence of a diff meant. The
#: driver's merge pass only classifies IN_REVIEW/HELD candidates, so none of
#: these is a node it would ever merge.
NODE_NOT_LIVE = "node-not-live"

#: The tracker item's own state could not be read, so whether the node is still
#: live work is unknown — and unknown is never merged.
NODE_STATE_UNREADABLE = "node-state-unreadable"

#: The item states both providers' readers report for an item they READ. A
#: failed read folds into the empty state (`GitHubFactsReader._fetch_issue`
#: returns `{}` on a non-zero `gh issue view`; `LinearFactsReader._issue_state`
#: maps an unresolvable workflow-state to `""`), and `settled_status` reads an
#: empty state as live work — so the emptiness has to refuse HERE.
_READABLE_ITEM_STATES = frozenset({"OPEN", "CLOSED"})

#: The settled provider statuses this gate refuses. HELD is settled too, but it
#: is not listed: a stored hold is :data:`NODE_HELD`'s refusal (which names the
#: reason and the release command) and the facts-computed ``merged_unclosed``
#: HELD is :data:`PR_NOT_OPEN`'s, both of them sharper than this one.
_NOT_LIVE_STATUSES = frozenset(
    {
        DriverStatus.DONE,
        DriverStatus.CANCELLED,
        DriverStatus.AWAITING_NO_CHANGE,
    }
)

#: ``--head`` is not a full 40-character object name.
HEAD_NOT_A_SHA = "head-not-a-sha"

#: No PR is linked to the node, so there is nothing to merge.
NO_PR = "no-pr"

#: The linked PR is not open (already merged, or closed unmerged).
PR_NOT_OPEN = "pr-not-open"

#: The forge head could not be read at all — never read as "unchanged".
FORGE_HEAD_UNREADABLE = "forge-head-unreadable"

#: The forge head is not ``--head``.
FORGE_HEAD_MOVED = "forge-head-moved"

#: The PR has no branch name, so there is no tree to verify.
NO_BRANCH = "no-branch"

#: The PR merges into another branch or repository than the one verified.
PR_BASE_MISMATCH = "pr-base-mismatch"

#: The PR's base could not be read where ``gate = "verify"`` needs it.
PR_BASE_UNREADABLE = "pr-base-unreadable"

#: A PR url's repository: ``<scheme>://<host>/<owner>/<repo>/pull/<n>``.
_PR_URL_REPO_RE = re.compile(r"\Ahttps://[^/]+/([^/]+/[^/]+)/pull/\d+/?\Z")

#: GitHub's own computed ``mergeStateStatus`` is not one of the green ones.
REQUIRED_CHECKS_NOT_GREEN = "required-checks-not-green"

#: The head's own check runs are not green — failed, still running, unreadable,
#: or never run at all, which is the #2033 hole ``mergeStateStatus`` cannot see.
HEAD_CHECKS_NOT_GREEN = "head-checks-not-green"

#: The one ``head_checks`` state that clears the #2033 gate.
_CHECKS_GREEN = "success"

#: The merge queue withheld the merge; its own reason is carried as the detail.
QUEUE_REFUSED = "merge-queue-refused"

#: ``gh pr merge`` itself failed.
MERGE_FAILED = "merge-failed"

#: GitHub accepted the merge, but main does not hold the verified candidate tree.
LANDED_TREE_MISMATCH = "landed-tree-mismatch"

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

#: The READY verdict, as the merge pass spells it.
_VERDICT_READY = "READY"

_FULL_SHA_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")


class OperatorMergeRefused(RuntimeError):
    """One named refusal to merge. ``reason`` is the stable machine token."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class OperatorMergeOutcome:
    """What the merge that happened was, for the operator-facing report."""

    pr_number: int
    head: str
    base: str
    branch: str
    actor: str


#: The queue seam: :func:`kew.graph.driver.merge_queue.verify_combined_tree`,
#: injectable so the refusal paths can be driven without a real clone. Keyword
#: arguments only, exactly as the real one is called below.
Verifier = Callable[..., AbstractContextManager[MergeVerifyResult]]


def _refuse(reason: str, detail: str) -> NoReturn:
    _logger.warning("kew epic merge refused (%s): %s", reason, detail)
    raise OperatorMergeRefused(reason, detail)


def _checked_head(head: str) -> str:
    """``--head`` as a lowercase full sha, or a refusal.

    An abbreviation would compare unequal to every fact this gate reads, and
    ``--match-head-commit`` needs the full name anyway.
    """
    if not _FULL_SHA_RE.match(head.strip()):
        _refuse(
            HEAD_NOT_A_SHA,
            f"--head must be a full 40-character commit sha, got {head!r}",
        )
    return head.strip().lower()


def _queue_gate(config: DispatchConfig) -> None:
    """Refuse unless the epic opted into the serial merge queue (#2148)."""
    if not config.epic.merge_queue:
        _refuse(
            QUEUE_DISABLED,
            "[epic] merge_queue = false, so no combined tree is ever verified "
            "— this command will not merge a head proven green only against "
            "itself; set [epic] merge_queue = true",
        )


def _membership_gate(store: RunStore, *, epic_ref: str, item_id: str) -> None:
    """Refuse unless *item_id* is a node of *epic_ref* (#2203 review).

    Nothing downstream would notice a wrong epic name: holds are keyed on the
    string as typed, so a name no driver ever wrote has no hold rows and the
    hold gate passes vacuously, and the attested event would then name an epic
    this node was never part of. `kew epic unhold` refuses an epic it has no row
    for; the merge refuses an epic it has no NODE for, which is the same idea
    one level up.

    Membership is :func:`kew.service.epics.epic_member_ids` — the epic's latest
    journaled tick snapshot, which is also the only membership record that is
    NOT agent-writable: the `epic:<name>` labels the tracker adapters resolve
    membership from are, and this module reads no label.
    """
    members = epic_member_ids(store, epic_ref)
    if item_id in members:
        return
    known = (
        f"its nodes are {', '.join(sorted(members))}"
        if members
        else "no tick of that epic has been journaled — check the epic name"
    )
    _refuse(
        NOT_AN_EPIC_MEMBER,
        f"{item_id} is not a node of epic {epic_ref!r}: {known}",
    )


def _hold_gate(store: RunStore, *, epic_ref: str, item_id: str) -> None:
    """Refuse while the node is held — `kew epic unhold` releases holds."""
    for hold in store.list_epic_holds(epic_ref):
        if hold.item_id == item_id:
            _refuse(
                NODE_HELD,
                f"the node is held ({hold.reason}) since "
                f"{hold.held_at:%Y-%m-%d %H:%M UTC}; release it with "
                f"`kew epic unhold {epic_ref} {item_id}` once it is resolved",
            )


def _verdict_gate(store: RunStore, *, item_id: str, head: str) -> int:
    """The review verdict bound to *head*, as its round number, or a refusal.

    READY, or a NEEDS_CHANGES round whose residuals the stop rule accepted on
    BOTH of its records (#2151) — a half-written acceptance is refused, exactly
    as the driver's own gate refuses it.
    """
    latest = store.latest_review_round(item_id)
    ready = latest is not None and (
        latest.verdict == _VERDICT_READY or acceptance_on_record(store, item_id, latest)
    )
    if latest is None or not ready:
        recorded = (
            "no review round is recorded"
            if latest is None
            else (f"the latest review round (#{latest.round}) is {latest.verdict}")
        )
        _refuse(
            REVIEW_SKIP_NOT_READY,
            f"{recorded} — only a READY verdict, or a NEEDS_CHANGES round whose "
            "residuals the stop rule accepted, may be merged",
        )
    if latest.head_sha != head:
        _refuse(
            REVIEW_SKIP_HEAD_MOVED,
            f"review round #{latest.round} verdict {latest.verdict} was recorded "
            f"against head {latest.head_sha[:12]}, not {head[:12]} — that verdict "
            "reviewed other code",
        )
    # The one merge verdict rule (#3196): the newest real attested verdict on
    # this head must not be NEEDS_CHANGES (unless it is the round accepted
    # above) or NEEDS_FULL.
    check = check_merge_verdict(store, head, accepted=latest.verdict != _VERDICT_READY)
    if check.refusal is not None:
        _refuse(f"{MERGE_VERDICT_REFUSED}-{check.refusal}", check.detail)
    return latest.round


def _open_gate(pr: dict[str, Any], *, pr_number: int) -> None:
    """Refuse unless the PR is still OPEN.

    Read on EVERY fresh read, not only the first: a merge or a close by an actor
    outside this session lands on a path that never takes the queue lock, and
    the head oid of a merged PR is unchanged, so :func:`_bound_head` cannot see
    it. Without this, a PR merged mid-verification would still be handed to
    ``gh pr merge``. An unreadable state refuses too — it is never read as OPEN.
    """
    state = pr.get("state")
    if state != "OPEN":
        _refuse(
            PR_NOT_OPEN,
            f"PR #{pr_number} is {state or 'unreadable'}, not OPEN",
        )


def _bound_head(pr: dict[str, Any], *, pr_number: int, head: str) -> None:
    """Refuse unless the PR's head is exactly *head*.

    An unreadable head is NOT an unchanged head: a head kew cannot read is a
    head kew cannot bind, so it refuses.
    """
    current = pr.get("headRefOid")
    if not isinstance(current, str) or not current:
        _refuse(
            FORGE_HEAD_UNREADABLE,
            f"PR #{pr_number}'s head could not be read from the forge, so it "
            f"cannot be proven to be {head[:12]}",
        )
    if current.lower() != head:
        _refuse(
            FORGE_HEAD_MOVED,
            f"PR #{pr_number}'s head on the forge is {current[:12]}, not "
            f"{head[:12]} — re-review the current head and merge that",
        )


def _read_base(pr: dict[str, Any]) -> tuple[str | None, str | None]:
    """The PR's base branch, and the repository its url names; None unread."""
    ref = pr.get("baseRefName")
    url = pr.get("url")
    named = _PR_URL_REPO_RE.match(url) if isinstance(url, str) else None
    return (
        ref if isinstance(ref, str) and ref else None,
        named.group(1) if named else None,
    )


def _base_gate(
    pr: dict[str, Any], *, pr_number: int, repo: str, base: str, required: bool
) -> None:
    """Refuse unless the PR merges into *repo*'s *base*, the tree verified.

    A PR retargeted to ``release`` would otherwise be verified as *base* plus
    its head and merged into ``release``. Under ``gate = "verify"``
    (*required*) the verify is all that proves the tree, so a base that cannot
    be read refuses too; under the GitHub gate the checks GitHub ran are on
    the PR's real base, and only a base read as another one refuses.
    """
    ref, owner = _read_base(pr)
    if (ref is None or owner is None) and required:
        _refuse(
            PR_BASE_UNREADABLE,
            f"PR #{pr_number}'s base branch or repository could not be read, so "
            f"it cannot be proven to merge into {repo}'s {base}",
        )
    if ref is not None and ref != base:
        _refuse(
            PR_BASE_MISMATCH,
            f"PR #{pr_number} targets {ref}, not {base} — the tree verified is "
            f"{base} plus its head",
        )
    if owner is not None and owner.lower() != repo.lower():
        _refuse(PR_BASE_MISMATCH, f"PR #{pr_number} merges into {owner}, not {repo}")


def _ci_gate(
    reader: GitHubFactsReader,
    pr: dict[str, Any],
    *,
    base: str,
    pr_number: int,
    head: str,
    gated_checks: Sequence[str],
    checks: bool = True,
) -> None:
    """Refuse unless *head* is CI-green on BOTH gates the merge pass applies.

    The operator's authority replaces the forge APPROVAL — never the tests, so
    neither gate is relaxed here. ``required_checks_green`` is GitHub's own
    computed ``mergeStateStatus``, and ``head_checks`` is the head's own check
    runs (#2033), needed because ``mergeStateStatus`` counts a skipped required
    check as passing: a private-repo agent PR whose self-hosted tests were
    skipped pending ``ci-approved`` reads CLEAN there while nothing ran. Any
    non-``success`` check state refuses, including ``pending`` and the
    unreadable read ``head_checks`` reports as ``pending`` — never green.

    ``checks=False`` is ``[drive.merge] gate = "verify"`` (#2814): the passing
    ``kew verify`` of the candidate the drive's verifier runs inside the lease
    stands in for GitHub's checks, which then are not read at all.
    """
    if not checks:
        return
    if not reader.required_checks_green(base, pr):
        _refuse(
            REQUIRED_CHECKS_NOT_GREEN,
            f"PR #{pr_number}'s mergeStateStatus is "
            f"{pr.get('mergeStateStatus') or 'unreadable'}, not a green one — "
            "required checks are red, pending, or the base moved",
        )
    state, evidence = reader.head_checks(head, gated_checks)
    if state != _CHECKS_GREEN:
        _refuse(
            HEAD_CHECKS_NOT_GREEN,
            f"{head[:12]}'s own check runs are {state}: {evidence} — an "
            "operator merge replaces the approval, not the tests",
        )


def _live_node_facts(
    reader: GitHubFactsReader,
    store: RunStore,
    *,
    facts_key: Any,
    provider: str,
    item_id: str,
) -> NodeFacts:
    """The node's fresh provider facts, proven to still be LIVE WORK, or a
    refusal (#2203 review).

    The driver's own merge pass only ever classifies candidates the overlay put
    in IN_REVIEW or HELD, so a node whose status is settled by the provider —
    DONE or CANCELLED for a closed item, AWAITING_NO_CHANGE while a zero-diff
    judgment is still deciding what the absence of a diff meant — is a node the
    pass would never merge. Nothing else here notices: an item a human CANCELLED
    can keep an open, green, READY-reviewed PR behind it, so every other gate
    passes and the merge would land work the tracker says was abandoned and
    attest it under this epic.

    An UNREADABLE item refuses before any of that, like every other unreadable
    fact in this module: both readers fold a failed item read into the empty
    state, and ``settled_status`` reads an empty state as live work — so left to
    the precedence alone, a tracker outage (or a `gh issue view` that merely
    hiccuped) would read exactly like a healthy OPEN item, and an item CANCELLED
    during the verification would be merged and attested whenever the in-lease
    re-read failed. A state kew could not read is not a state kew can clear.

    The precedence is :func:`kew.graph.driver.overlay.settled_status`, the same
    function the tick derives the overlay from — mirroring the rule here would
    let the two drift, and this gate exists because they must not. ``held_reason``
    is left unread: a stored hold is :func:`_hold_gate`'s refusal, which names
    the reason and how to release it.
    """
    facts = reader.issue_facts(facts_key)
    if facts.state not in _READABLE_ITEM_STATES:
        _refuse(
            NODE_STATE_UNREADABLE,
            f"{item_id}'s tracker item reads {facts.state or 'unreadable'}, so it "
            "cannot be proven to be live work — re-run once the tracker answers",
        )
    judgment = read_no_change_facts(store, provider).get(item_id)
    settled = settled_status(
        facts, no_change_state=None if judgment is None else judgment.state
    )
    if settled is not None and settled in _NOT_LIVE_STATUSES:
        item = "/".join(p for p in (facts.state, facts.state_reason) if p)
        pending = "" if judgment is None else f", no-change judgment {judgment.state}"
        _refuse(
            NODE_NOT_LIVE,
            f"{item_id} reads {settled.value}: the tracker item is "
            f"{item or 'unreadable'}{pending} — the driver's merge pass only ever "
            "considers a node the overlay reads as in_review or held",
        )
    return facts


def _forge_gate(
    reader: GitHubFactsReader,
    facts: NodeFacts,
    *,
    head: str,
    repo: str,
    base: str,
    gated_checks: Sequence[str],
    checks: bool = True,
) -> tuple[int, str]:
    """The open, green PR bound to *head*, as ``(pr_number, branch)``, or a
    refusal.

    The PR read is ``fresh_pr_facts``, never ``pr_facts``: the latter can be
    served from a driver tick's batch, which is exactly the stale answer this
    gate must not accept.
    """
    if facts.pr_number is None:
        _refuse(NO_PR, "no pull request is linked to the node")
    pr_number = facts.pr_number
    pr = reader.fresh_pr_facts(pr_number)
    _open_gate(pr, pr_number=pr_number)
    _bound_head(pr, pr_number=pr_number, head=head)
    _base_gate(pr, pr_number=pr_number, repo=repo, base=base, required=not checks)
    _ci_gate(
        reader,
        pr,
        base=base,
        pr_number=pr_number,
        head=head,
        gated_checks=gated_checks,
        checks=checks,
    )
    branch = pr.get("headRefName")
    if not isinstance(branch, str) or not branch:
        _refuse(
            NO_BRANCH,
            f"PR #{pr_number} has no head branch name, so there is no combined "
            "tree to verify",
        )
    return pr_number, branch


def _reprove_gates(
    reader: GitHubFactsReader,
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    facts_key: Any,
    provider: str,
    pr_number: int,
    head: str,
    repo: str,
    base: str,
    gated_checks: Sequence[str],
    checks: bool = True,
) -> int:
    """Re-prove, INSIDE the lease, every gate another actor can have moved.

    Returns the re-proven review round — the one the attestation names, so the
    chain never names a round a later one has superseded.

    One verification can run for half an hour, so none of the facts read before
    it entered is still known when it returns:

    * the PR's own state — an actor outside this session can merge or close the
      PR while the tree is being verified, and a merged PR's head oid is
      unchanged, so the head binding below cannot see it;
    * the forge head — a push during the verification means nobody verified the
      tree that would be merged;
    * the PR's base — a PR retargeted during the verification would merge the
      verified head into a branch the tree was never built on;
    * both CI gates — the same push, or a re-run, can turn the head red or
      pending under us, and the head-check read is the one that sees a required
      job that was skipped rather than passed;
    * the item's own live-work state — a human can CANCEL the tracker item while
      the tree is verified (and a cancelled item leaves its PR open and green),
      and a zero-diff judgment is written on a path that never takes the queue
      lock either. Read only at the start, this command would merge work the
      tracker now says was abandoned;
    * the hold — the driver's sticky ``ci_pending`` hold is written on a path
      that never takes the queue lock, so a concurrent tick observing red CI can
      hold this very node mid-verification. Read only at the start, this command
      would then merge a node the driver has just refused to advance;
    * the review verdict — the review stage records a round on a path that
      never takes the queue lock either, so a NEEDS_CHANGES round landing
      mid-verification is exactly as live a refusal as a hold. Read only at the
      start, this command would merge against a verdict that no longer stands
      and attest the superseded READY round as its authority.
    """
    pr = reader.fresh_pr_facts(pr_number)
    _open_gate(pr, pr_number=pr_number)
    _bound_head(pr, pr_number=pr_number, head=head)
    _base_gate(pr, pr_number=pr_number, repo=repo, base=base, required=not checks)
    _ci_gate(
        reader,
        pr,
        base=base,
        pr_number=pr_number,
        head=head,
        gated_checks=gated_checks,
        checks=checks,
    )
    _live_node_facts(
        reader, store, facts_key=facts_key, provider=provider, item_id=item_id
    )
    _hold_gate(store, epic_ref=epic_ref, item_id=item_id)
    return _verdict_gate(store, item_id=item_id, head=head)


def _run_merge(
    run: RunFn,
    *,
    repo: str,
    pr_number: int,
    head: str,
    item_id: str,
    message: tuple[str, str] | None = None,
) -> None:
    """The fixed merge argv, or a refusal. Never ``--admin``, never ``--force``.

    ``--match-head-commit`` makes the head binding atomic at GitHub: a push
    that lands between the last re-read and this command loses the race here.
    *message* is the squash commit's ``(subject, body)``; omitted, GitHub's.
    """
    argv = [
        "gh",
        "pr",
        "merge",
        str(pr_number),
        "--repo",
        repo,
        "--squash",
        "--match-head-commit",
        head,
    ]
    if message is not None:
        argv += ["--subject", message[0], "--body", message[1]]
    result = run(argv, check=False)
    if result.returncode != 0:
        _refuse(
            MERGE_FAILED,
            f"`gh pr merge` refused PR #{pr_number} for {item_id} at "
            f"{head[:12]}: {(result.stderr or '').strip() or 'no stderr'}",
        )


def _acceptance_contract_gate(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    verified: tuple[tuple[int, int], ...] | None,
) -> None:
    """Refuse when the acceptance contract changed after verification."""
    if verified is None:
        return
    current = acceptance_contract_versions(
        (store.current_acceptance_contract(epic_ref, item_id),)
    )
    changed = acceptance_contract_change(verified, current)
    if changed is not None:
        _refuse(
            QUEUE_REFUSED,
            f"acceptance contract changed while verification ran: {changed}; "
            "the node must verify again",
        )


class _GitReadError(RuntimeError):
    """A remote base or landed tree could not be read."""


def _git_read(run: RunFn, repo_root: Path, *args: str) -> str:
    result = run(
        ["git", "-C", str(repo_root), *args],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or "no stderr"
        raise _GitReadError(f"git {args[0]} exited {result.returncode}: {detail}")
    return (result.stdout or "").strip()


def _remote_base(run: RunFn, repo_root: Path, base: str) -> str:
    output = _git_read(run, repo_root, "ls-remote", "origin", f"refs/heads/{base}")
    sha = output.split()[0].lower() if output else ""
    if not _SHA_RE.fullmatch(sha):
        raise _GitReadError(f"origin has no readable {base} commit")
    return sha


def _landed_tree(run: RunFn, repo_root: Path, base: str) -> tuple[str, str]:
    landed = _remote_base(run, repo_root, base)
    _git_read(run, repo_root, "fetch", "--no-tags", "origin", landed)
    tree = _git_read(run, repo_root, "rev-parse", f"{landed}^{{tree}}").lower()
    if not _SHA_RE.fullmatch(tree):
        raise _GitReadError(f"{landed[:12]} has no readable tree")
    return landed, tree


def _landed_attention(
    store: RunStore,
    *,
    repo: str,
    pr_number: int,
    landed: str,
    message: str,
) -> None:
    """Persist the suspect-main hold and its operator attention event.

    A known *landed* commit is held as a mismatch, so the queue waits for main
    to move instead of verifying that unverified tree green and resuming; the
    failure is kept either way so the durable row says what to repair (#2883).
    """
    store.suspect_merge_queue_main(
        repo,
        f"PR #{pr_number} landed outside its verified tree",
        mismatch_on=landed or None,
        failure=message,
    )
    emit(
        store,
        ATTENTION_EVENT,
        actor=Actor(type="policy", id="merge-queue"),
        repo=repo,
        payload=attention_payload(
            MAIN_UNVERIFIED,
            repo=repo,
            pr=pr_number,
            sha=landed or None,
            message=message,
        ),
        dedup_key=f"{ATTENTION_EVENT}:{landed or pr_number}",
    )
    _logger.error(message)


def _attest(
    store: RunStore,
    *,
    actor: Actor,
    epic_ref: str,
    item_id: str,
    repo: str,
    base: str,
    branch: str,
    head: str,
    pr_number: int,
    review_round: int,
    trigger: str,
    verified_base: str,
    verified_tree: str,
    landed_sha: str,
) -> None:
    """Record the operator-authority merge on the audit chain.

    Best-effort like ``merge._attest``: the merge has already landed, and an
    unwritable ledger must not be reported as a merge that did not happen. The
    event repeats the verified base and tree with the landed commit. Readers can
    therefore prove the post-merge comparison from this event alone.

    A non-numeric tracker item attests under the chained 0 sentinel and stays
    addressable by ``item_id``, like every other epic event.
    """
    emit(
        store,
        OPERATOR_MERGE_EVENT,
        actor=actor,
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload={
            "trigger": trigger,
            "actor": actor.id,
            "actor_type": actor.type,
            "epic": epic_ref,
            "sha": head,
            "base": base,
            "branch": branch,
            "pr": pr_number,
            "review_round": review_round,
            "verified_base": verified_base,
            "verified_tree": verified_tree,
            "landed_sha": landed_sha,
        },
    )


def operator_merge(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    head: str,
    repo: str,
    base: str,
    repo_root: Path,
    reader: GitHubFactsReader,
    actor: Actor,
    provider: str,
    run: RunFn = _default_run,
    verify: Verifier = verify_combined_tree,
    message: tuple[str, str] | None = None,
    trigger: str = "operator",
    require_merge_queue: bool = True,
    checks: bool = True,
) -> OperatorMergeOutcome:
    """Merge one READY node at *head* through the merge queue, or refuse.

    Raises :class:`OperatorMergeRefused` for every refusal; the module docstring
    lists the nine facts that must hold. ``kew drive``'s standing merge rule
    (#2618) passes its own *verify* — the serial ``kew verify`` of the combined
    tree, under the same queue lock — and ``require_merge_queue=False``, since
    fact 1, ``[epic] merge_queue``, opts into the queue's OWN verifier.
    Under its ``gate = "verify"`` it also passes ``checks=False``: fact 7 is
    then the passing ``kew verify`` of the candidate *verify* runs (#2814).
    """
    checked = _checked_head(head)
    gated_checks = config.ci.approval_gated_checks
    if require_merge_queue:
        _queue_gate(config)
    _membership_gate(store, epic_ref=epic_ref, item_id=item_id)
    _hold_gate(store, epic_ref=epic_ref, item_id=item_id)
    _verdict_gate(store, item_id=item_id, head=checked)
    # The GitHub facts reader is keyed on the int issue number, the Linear one
    # on the item id itself — the same provider dispatch `_classify_node` makes.
    facts_key: int | str = int(item_id) if provider == "github" else item_id
    facts = _live_node_facts(
        reader, store, facts_key=facts_key, provider=provider, item_id=item_id
    )
    pr_number, branch = _forge_gate(
        reader,
        facts,
        head=checked,
        repo=repo,
        base=base,
        gated_checks=gated_checks,
        checks=checks,
    )
    with verify(
        config,
        store,
        epic_ref=epic_ref,
        repo=repo,
        base=base,
        repo_root=repo_root,
        item_id=item_id,
        pr_number=pr_number,
        head_oid=checked,
        branch=branch,
    ) as verified:
        if not verified.ok:
            _refuse(
                QUEUE_REFUSED,
                f"the merge queue did not verify {branch} on {base} at "
                f"{checked[:12]}: {verified.reason}",
            )
        if not verified.base_sha or not verified.tree_sha:
            _refuse(
                QUEUE_REFUSED,
                "the merge queue returned green without a bound base and "
                "candidate tree",
            )
        # The verification can run for half an hour, so the head, the CI state,
        # the item's live-work state, the hold and the verdict are all re-proven
        # against fresh reads before the merge — and the merge stays inside the
        # lease. The re-proven round is the one attested: a round recorded under
        # the verification is the authority this merge actually runs on.
        review_round = _reprove_gates(
            reader,
            store,
            epic_ref=epic_ref,
            item_id=item_id,
            facts_key=facts_key,
            provider=provider,
            pr_number=pr_number,
            head=checked,
            repo=repo,
            base=base,
            gated_checks=gated_checks,
            checks=checks,
        )
        try:
            current_base = _remote_base(run, repo_root, base)
        except _GitReadError as exc:
            _refuse(
                QUEUE_REFUSED,
                f"{MERGE_VERIFY_BASE_UNREADABLE}: cannot re-read {base} "
                f"immediately before merge: {exc}",
            )
        if current_base != verified.base_sha:
            _refuse(
                QUEUE_REFUSED,
                f"{MERGE_VERIFY_BASE_MOVED}: {base} moved from "
                f"{verified.base_sha[:12]} to {current_base[:12]} after verification; "
                "retry on the new base",
            )
        _acceptance_contract_gate(
            store,
            epic_ref=epic_ref,
            item_id=item_id,
            verified=verified.contracts,
        )
        _run_merge(
            run,
            repo=repo,
            pr_number=pr_number,
            head=checked,
            item_id=item_id,
            message=message,
        )
        try:
            landed_sha, actual_tree = _landed_tree(run, repo_root, base)
        except _GitReadError as exc:
            detail = f"cannot read the tree after merging PR #{pr_number}: {exc}"
            _landed_attention(
                store,
                repo=repo,
                pr_number=pr_number,
                landed="",
                message=detail,
            )
            _refuse(LANDED_TREE_MISMATCH, detail)
        if actual_tree != verified.tree_sha:
            detail = (
                f"PR #{pr_number} landed as {landed_sha[:12]} with tree "
                f"{actual_tree[:12]}, not verified tree {verified.tree_sha[:12]}"
            )
            _landed_attention(
                store,
                repo=repo,
                pr_number=pr_number,
                landed=landed_sha,
                message=detail,
            )
            _refuse(LANDED_TREE_MISMATCH, detail)
        _attest(
            store,
            actor=actor,
            epic_ref=epic_ref,
            item_id=item_id,
            repo=repo,
            base=base,
            branch=branch,
            head=checked,
            pr_number=pr_number,
            review_round=review_round,
            trigger=trigger,
            verified_base=verified.base_sha,
            verified_tree=verified.tree_sha,
            landed_sha=landed_sha,
        )
    return OperatorMergeOutcome(
        pr_number=pr_number, head=checked, base=base, branch=branch, actor=actor.id
    )
