"""Bring a reviewed branch current with its base when it stops merging cleanly.

With siblings landing in parallel, a branch that reached READY (or is still in
review) falls behind ``main`` and eventually stops merging into it — shared
export barrels, fixtures and the factory shell were the three that actually
conflicted (#2096, #2102, #2131). Nothing in the driver could act on that: the
merge pass holds the node ``ci_pending`` and waits, so an operator merged
``main`` in by hand. That operator commit then hit #2124 — a head kew never
pushed binds to no producer, and the driver refuses to remediate a head it
cannot bind — so one manual merge cost a manual remediation too.

This module is the driver's own merge-main step (#2149). When a review-stage
node's PR reports GitHub's ``DIRTY`` merge state, the review stage spends the
node's one action for the tick here instead of on a reviewer or a remediator:
the governed ``kew resolve`` path integrates the base and publishes the
integration commit under a kew-owned producer, so the new head binds and the
node's next tick re-reviews the delta and remediates against it exactly as it
would have without the divergence.

Three outcomes, none of them silent:

``integrated``
    The base is in and the head moved (or ``kew resolve`` found the PR already
    mergeable, i.e. the ``DIRTY`` fact was stale). The node keeps its review
    status, so the next tick reads the new head and acts on it. This tick's facts
    are spent either way, so the merge pass — which runs after the review stage
    over those same facts — skips the node for this one tick; see
    ``review_stage.ReviewStageOutcome.integrated``.

    This is the one outcome that leaves the node un-held and due again, so it is
    the one that has to carry its own bound. Skipping the merge pass took away
    the ``ci_pending`` hold that used to stop the node by accident, and a step
    that reports ``integrated`` without making the PR mergeable would then spend
    a governed ``kew resolve`` run on every tick forever — reported as progress,
    with no hold row and therefore no stall clock. So every integrated step
    leaves a durable marker row carrying the head it read, and
    ``MAX_CONSECUTIVE_STEPS`` of them at that SAME head with no other work
    for the node in between HOLD it instead of running one more. A step that
    moved the head did work, however often the base moves again behind it.
``conflicted``
    The integration left conflicts nothing could resolve. The node is HELD with
    the conflicting paths named — the one fact an operator needs to act. The
    branch is untouched, so ``DIRTY`` is still true of it: the merge pass keeps
    reading the node, and ``merge._hold_ci_pending`` is what stops it rewriting
    this reason down to its own ``ci_pending``.
``failed``
    The step could not run at all: no PR number, a refused checkout, a rejected
    push, a raised seam. Also HELD, naming what failed. A merge-main step that
    could not run is never reported as a branch that is current.
``head_moved``
    The PR's head is no longer the one the stage checked for an operator
    (#3200), so ``kew resolve`` ran nothing. Not a hold and not a step: the
    node waits, and the next tick re-reads the new head and decides again.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from kew.epic_holds import (
    BASE_INTEGRATION_CONFLICT_HOLD_PREFIX,
    BASE_INTEGRATION_FAILED_HOLD_PREFIX,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.graph.driver.facts import NodeFacts
    from kew.run_store import RunRecord, RunStore

_logger = logging.getLogger(__name__)

#: GitHub's ``mergeStateStatus`` for a head that no longer merges into its base.
#: The ONLY state this step acts on. ``BEHIND`` is a branch that is merely stale
#: and still merges cleanly, ``BLOCKED``/``UNSTABLE`` are check states, and
#: ``UNKNOWN`` means GitHub has not finished computing mergeability — acting on
#: any of them would rewrite heads that had nothing wrong with them.
DIRTY_MERGE_STATE = "DIRTY"

#: Hold reasons. Both are operator-facing and sticky: the node is not re-tried
#: every tick, because every retry of an unresolvable conflict is another agent
#: run spent reaching the same answer. Sticky is not a property of the string —
#: it is `kew.epic_holds.holds_node_out_of_frontier` matching it, which is what
#: routes the held node to HELD and out of the review stage's due pass. So the
#: vocabulary has ONE owner (`kew.epic_holds`) and this module imports it; a
#: reason defined here alone would leave the node reading its review status and
#: re-dispatching `kew resolve` every tick.
INTEGRATION_CONFLICT_HOLD = BASE_INTEGRATION_CONFLICT_HOLD_PREFIX
INTEGRATION_FAILED_HOLD = BASE_INTEGRATION_FAILED_HOLD_PREFIX

#: How many conflicting paths a hold reason names before it summarizes the rest.
#: A hold reason lands in `kew epic status` output and a tracker comment, so the
#: full list of an epic-wide barrel conflict cannot be allowed to fill either.
MAX_NAMED_PATHS = 10


#: How many merge-main steps in a row the driver spends on one node WITHOUT the
#: head moving before it stops and HOLDs. "In a row" means with no other run for
#: the node and no new head in between: an honest step moves the head, and the
#: next tick reads a head no earlier step ever saw, which ends the streak
#: (:func:`consecutive_base_integrations`). So the cap is only ever reached by a
#: step that keeps reporting success and keeps leaving the SAME head
#: un-mergeable — and each one of those is an agent run. Above one because the
#: seam can legitimately report ``integrated`` on an unmoved head (a stale
#: ``DIRTY`` fact, an already-merged base): one or two of those are a busy epic,
#: not a loop.
MAX_CONSECUTIVE_STEPS = 3

#: ``RunRecord.error_category`` of the marker row an integrated step leaves.
#: That row is the ledger the bound counts. The step publishes through
#: ``kew resolve``, whose own run row exists only on the conflicted path, so
#: counting those alone would leave the clean path unbounded.
STEP_MARKER_CATEGORY = "base_integration_step"

#: Prefix of a marker row's ``title``; the head the step READ follows it. The
#: head is on the row because the bound is about progress, not about count: two
#: markers at different heads are two integrations that each moved the branch,
#: and only markers at the SAME head are a step going nowhere. ``RunRecord`` has
#: no head column, so the marker carries it here — a 12-hex prefix, the same
#: abbreviation git itself hands out, which keeps the row readable in run
#: history while staying unique across one branch's heads.
MARKER_TITLE_PREFIX = "merge-main step at "
MARKER_HEAD_CHARS = 12

#: ``RunRecord.error_category`` of the row written when the bound is spent. It
#: ENDS the streak as well as recording it, so the ``kew epic unhold`` the hold
#: reason tells the operator to run really is a recovery path: the released node
#: gets a fresh allowance instead of being re-held on the next tick having tried
#: nothing.
STEP_EXHAUSTED_CATEGORY = "base_integration_exhausted"


#: What the merge-main step records instead of running on a branch an
#: operator owns (#3200): the outcome's ``refused`` reason, and the ``reason``
#: of the :data:`INTEGRATION_SKIPPED_EVENT` audit row.
OPERATOR_OWNED_SKIP = "operator-owned: integration skipped"

#: What the step records when the PR's head moved between the stage's
#: ownership check and the resolve (#3200): it pushed nothing, and the next
#: tick decides again on the new head.
HEAD_MOVED_SKIP = "head moved: integration skipped"

#: Audit event of a merge-main step skipped on an operator-owned head, one per
#: head (the dedup key carries it), so a node waiting on its operator for many
#: ticks leaves one row, not one per tick.
INTEGRATION_SKIPPED_EVENT = "base_integration_skipped"

_PUBLICATION_INTENT_EVENT = "publication_intent"
_HEAD_ATTESTATION_EVENT = "publication_head_attested"


def operator_commit_at_head(
    store: RunStore, *, item_id: str, branch: str, head_sha: str
) -> bool:
    """Whether *head_sha* is an operator's commit, newer than kew's last push.

    Two readings, both from kew's own publication record. An operator ran
    ``kew publication attest`` on this head: they pushed their own commits on
    top of a bound head and bound them through that parent chain. Or kew has
    published onto *branch* and none of those publications is this head:
    someone else pushed on top. On 2026-10-25 the merge-main step pushed a
    conflict resolution onto two such branches and reintroduced a symbol the
    operator's own fix had removed.

    kew's publications are the host's ``publication_intent`` rows and the heads
    ``kew finalize`` bound: finalize is the operator saying the remote holds
    the agent's own held output, so that head is the agent's, not theirs.
    Both write a ``publication_head_attested`` row; only an attestation that
    binds through parents is the operator's (a finalize names none).

    A branch kew never recorded a publication on reads False: kew cannot tell
    whose that head is, and the step behaves as it always did.
    """
    scopes = {item_id, branch}
    operator_heads: set[str] = set()
    published: set[str] = set()
    for payload in _branch_claims(
        store, _HEAD_ATTESTATION_EVENT, "human", scopes=scopes, branch=branch
    ):
        target = operator_heads if _binds_through_parents(payload) else published
        target.add(payload["commit_sha"])
    if head_sha in operator_heads:
        return True
    published |= {
        payload["commit_sha"]
        for payload in _branch_claims(
            store, _PUBLICATION_INTENT_EVENT, "policy", scopes=scopes, branch=branch
        )
    }
    return bool(published) and head_sha not in published


def _binds_through_parents(payload: dict[str, Any]) -> bool:
    """Whether an attestation binds its head through a parent chain.

    ``kew publication attest`` binds an operator's commit through the parents
    its first-parent path reaches a bound head by; ``kew finalize`` binds the
    run's own published head directly, with no parents.
    """
    parents = payload.get("parent_shas")
    if isinstance(parents, list) and any(isinstance(p, str) for p in parents):
        return True
    return isinstance(payload.get("parent_sha"), str)


def _branch_claims(
    store: RunStore, event: str, actor_type: str, *, scopes: set[str], branch: str
) -> list[dict[str, Any]]:
    """The payloads of *actor_type*'s *event* rows under *scopes* that name a
    commit on *branch*."""
    claims: list[dict[str, Any]] = []
    for scope in sorted(scopes):
        rows = store.list_audit_events(
            limit=None, event=event, actor_type=actor_type, item_id=scope
        )
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            commit = payload.get("commit_sha")
            if payload.get("branch") == branch and isinstance(commit, str):
                claims.append(payload)
    return claims


@dataclass(frozen=True)
class BaseIntegrationContext:
    """Everything the merge-main seam needs to bring one PR head current."""

    item_id: str
    issue_num: int
    pr_number: int
    head_sha: str
    base: str


@dataclass(frozen=True)
class BaseIntegrationOutcome:
    """What the merge-main step did — see the module docstring for the kinds."""

    kind: Literal["integrated", "conflicted", "failed", "head_moved"]
    detail: str = ""
    conflicted_paths: tuple[str, ...] = field(default=())


# A seam so the review stage is drivable without a runner; the production
# wiring over the governed `kew resolve` path is `sandbox_base_integrator`.
BaseIntegrator = Callable[[BaseIntegrationContext], BaseIntegrationOutcome]


def base_integration_due(facts: NodeFacts) -> bool:
    """True when *facts* say this node's head no longer merges into its base.

    Requires a PR number and a head as well as the ``DIRTY`` state: the step
    publishes onto a PR branch at a known head, and a node missing either has
    nothing for it to act on.
    """
    return (
        (facts.merge_state_status or "").upper() == DIRTY_MERGE_STATE
        and facts.pr_number is not None
        and bool(facts.head_oid)
    )


def run_base_integration(
    item_id: str,
    facts: NodeFacts,
    *,
    base: str,
    integrator: BaseIntegrator,
) -> BaseIntegrationOutcome:
    """Call the merge-main seam for one node, fail-closed.

    A seam that raises is a step that did not run, so it is reported as
    ``failed`` (and held) rather than crashing the tick or — far worse — being
    read as a branch that is now current.
    """
    assert facts.pr_number is not None  # guarded by `base_integration_due`
    assert facts.head_oid is not None  # guarded by `base_integration_due`
    ctx = BaseIntegrationContext(
        item_id=item_id,
        issue_num=facts.issue_num,
        pr_number=facts.pr_number,
        head_sha=facts.head_oid,
        base=base,
    )
    try:
        return integrator(ctx)
    except Exception as exc:
        _logger.exception(
            "review: merge-main step for %s raised — node held, nothing published",
            item_id,
        )
        return BaseIntegrationOutcome(
            "failed", detail=f"{type(exc).__name__}: {exc}"[:200]
        )


def _named_paths(paths: tuple[str, ...]) -> str:
    named = ", ".join(paths[:MAX_NAMED_PATHS])
    extra = len(paths) - MAX_NAMED_PATHS
    return f"{named} (+{extra} more)" if extra > 0 else named


def base_integration_hold_reason(outcome: BaseIntegrationOutcome, *, base: str) -> str:
    """The durable reason a merge-main step that did not integrate is held under.

    The conflicting paths are IN the reason, not only in the log: the hold is
    what an operator reads, and "this branch conflicts" without the file names
    is the same dead end the manual merges started from.
    """
    if outcome.kind == "conflicted":
        where = (
            f" in {_named_paths(outcome.conflicted_paths)}"
            if outcome.conflicted_paths
            else ""
        )
        detail = f" ({outcome.detail})" if outcome.detail else ""
        return (
            f"{INTEGRATION_CONFLICT_HOLD}: {base} no longer merges into this PR "
            f"branch and the conflicts{where} were not resolved{detail}. Resolve "
            "them on the branch, then unhold the node."
        )
    return (
        f"{INTEGRATION_FAILED_HOLD}: merging {base} into this PR branch could "
        f"not be completed ({outcome.detail or 'no detail reported'}). The "
        "branch is unchanged."
    )


def base_integration_exhausted_hold_reason(
    *, base: str, attempts: int, head_sha: str
) -> str:
    """The reason a node is held once the step's bound is spent (#2149).

    Held under the ``failed`` prefix on purpose: from an operator's side this
    IS a merge-main step that did not do what it reported. The reason names the
    count AND the head it never moved, because those two together are what
    distinguish this from a single failure and from an honest run of
    integrations: the step kept claiming success while the branch stood still.
    """
    return (
        f"{INTEGRATION_FAILED_HOLD}: the merge-main step ran {attempts} times in "
        f"a row for this PR and reported success each time, but the head never "
        f"moved off {head_sha[:MARKER_HEAD_CHARS]} and the branch still "
        f"does not merge into {base}. Nothing further is retried automatically. "
        "Integrate the base on the branch yourself, then unhold the node."
    )


def marker_head(record: RunRecord) -> str:
    """The head a marker row was written at, or ``""`` for any other row.

    The marker's own writer owns this format (:func:`record_base_integration_step`),
    so the two live next to each other; a row whose title predates the format —
    or is not a marker at all — reads as no head, which the streak then treats
    as unmatched rather than as a match against everything.
    """
    if record.error_category != STEP_MARKER_CATEGORY:
        return ""
    if not record.title.startswith(MARKER_TITLE_PREFIX):
        return ""
    return record.title[len(MARKER_TITLE_PREFIX) :].strip().lower()


def consecutive_base_integrations(
    store: RunStore, item_id: str, *, head_sha: str
) -> int:
    """Merge-main steps at the END of this node's history that never moved it.

    The bound is on steps that change nothing, so the streak counts only the
    trailing markers written at *head_sha* — the head this tick is about to act
    on. An honest step moves the head, so the next tick's head matches no marker
    and the streak is back at zero however long the branch keeps integrating a
    base that keeps moving. Counting markers alone instead held a node that was
    working: the step outranks the review action it would otherwise be separated
    by (``review_stage._drive_one_node``), so a node that is ``DIRTY`` on every
    tick never gets a run of any other kind in between (#2149 review).

    Any other run for the node still ends the streak, because it is a tick the
    step did not spend. Resolve runs do not: they are the step's own agent run,
    written by ``kew resolve`` between the attempt and its marker. The exhausted
    marker ends it unconditionally, so a released node starts over rather than
    being re-held having tried nothing.
    """
    from kew.run_store import RESOLVE_RUN_KIND

    wanted = head_sha[:MARKER_HEAD_CHARS].lower()
    streak = 0
    for record in reversed(store.list_by_item(item_id)):
        if record.error_category == STEP_EXHAUSTED_CATEGORY:
            break
        if record.error_category == STEP_MARKER_CATEGORY:
            if marker_head(record) != wanted:
                break
            streak += 1
            continue
        if record.run_kind == RESOLVE_RUN_KIND:
            continue
        break
    return streak


def record_base_integration_step(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    issue_num: int,
    head_sha: str,
    exhausted: bool = False,
) -> None:
    """Leave the durable row that bounds the step — it has no other trace.

    An integrated step holds nothing and records no review round, so without
    this row the next tick cannot tell a first attempt from a hundredth. The
    clean-integration path of ``kew resolve`` writes no run row of its own, so
    this cannot be left to the seam.

    *head_sha* is the head the step READ, and it goes in the row rather than
    only in the log: the streak is over steps that left that head where it was,
    so the next tick needs to compare it (:func:`consecutive_base_integrations`).
    """
    from kew.ids import branch_slug
    from kew.run_store import RESOLVE_RUN_KIND, RunRecord

    now = datetime.now(UTC)
    category = STEP_EXHAUSTED_CATEGORY if exhausted else STEP_MARKER_CATEGORY
    store.save(
        RunRecord(
            run_id=str(uuid.uuid4()),
            issue_num=issue_num,
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            status="failure" if exhausted else "success",
            exit_code=1 if exhausted else 0,
            started_at=now,
            finished_at=now,
            error_category=category,
            run_kind=RESOLVE_RUN_KIND,
            repo=config.project.repo or None,
            # A marker, not an agent run: no model, no runner, no cost, so it
            # never lands in a cost baseline bucket a real run is judged against.
            title=f"{MARKER_TITLE_PREFIX}{head_sha[:MARKER_HEAD_CHARS].lower()}",
        )
    )
