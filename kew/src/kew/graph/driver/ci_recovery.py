"""Auto-ci-fix recovery pass for the epic driver (F5).

When a tick's merge pass holds a node on ``ci_pending`` because its PR's CI
went red, this pass decides — per node, at most once per tick — what to do:

* **infra-dead** (zero executed steps across the run: billing failure /
  runner-offline) → park the node with an infra diagnostic. NOT fixable; no
  agent run is dispatched (see the 2026-07-20 zero-step billing-red incident).
* **code-failure** (a step ran and something under our control went red) →
  dispatch a bounded ci-fix run reusing the existing ``dispatch_ci_fix``
  machinery, provided the node is within budget.
* **still pending / unknown** → keep holding, unchanged.
* **profile refused** (the active ``ci_fix`` prompt profile refused the launch
  before any runner, #3023) → replace ``ci_pending`` with an operator hold that
  names the refusal and its remedy, and skip the node until it is released:
  every later attempt would refuse identically.

The whole pass is gated by the caller on ``[epic] auto_ci_fix`` (default
False): when the operator has not opted in, this pass never runs and the
driver's behaviour is exactly as before. Everything the pass touches the
network for is injected (``classify`` / ``dispatch_fix``) so it is unit-tested
with zero real ``gh`` calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from kew import subprocess_utils
from kew.epic_holds import ADMISSION_REFUSED_HOLD_PREFIX
from kew.graph.driver.overlay import admission_refused_hold_reason
from kew.graph.driver.result import StuckNode
from kew.loops.ci import CiFailureClass, _gh_run_jobs, classify_ci_failure
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
from kew.prompt_refusals import PromptRefusal
from kew.runners import resolve_runner

if TYPE_CHECKING:
    from kew.budget import BudgetExceeded
    from kew.config import DispatchConfig
    from kew.graph.domain import Graph
    from kew.graph.driver.facts import NodeFacts
    from kew.notifier import Notifier
    from kew.run_store import RunStore

# The stuck reason surfaced for an infra-dead park. Deliberately explicit so an
# operator reading `kew epic status` sees WHY no fix was attempted.
INFRA_DEAD_REASON = "ci-infra-dead (zero executed steps — billing/runner-offline)"

RecoveryDecision = Literal["park", "dispatch", "hold"]

# (node_facts) -> failure class, or None when the run isn't classifiable yet
# (still pending, or gh couldn't be reached). None keeps the node held.
ClassifyFn = Callable[["NodeFacts"], CiFailureClass | None]
# (node_facts) -> True iff a ci-fix run was actually dispatched, or the
# refusal of the active ci_fix prompt profile that stopped it (#3023).
DispatchFixFn = Callable[["NodeFacts"], "bool | PromptRefusal"]

# The hold a refused ci_fix profile earns: an operator hold, never cleared by
# the merge pass, so the node is not re-attempted until it is released.
_PROFILE_HOLD_PREFIX = (
    f"{ADMISSION_REFUSED_HOLD_PREFIX}: {PROMPT_CONFIGURATION_REFUSAL}"
)

_logger = logging.getLogger(__name__)


@dataclass
class CiRecoveryOutcome:
    """What the recovery pass did this tick."""

    # Infra-dead nodes to surface as stuck (removed from `held`).
    parked: list[StuckNode] = field(default_factory=list)
    # Node ids a bounded ci-fix run was dispatched for (still held meanwhile).
    dispatched: list[str] = field(default_factory=list)
    # Node id -> the profile hold a refused ci_fix prompt profile earned.
    profile_held: dict[str, str] = field(default_factory=dict)
    # Node id -> why no fix was dispatched: an operator's merge verify claims
    # the head the fix would push onto (#3198). Still held meanwhile.
    claimed: dict[str, str] = field(default_factory=dict)


def _merge_claim_wait(store: RunStore, facts: NodeFacts, now: datetime) -> str | None:
    """The wait a merge claim on the node's PR head imposes on a fix, or None."""
    from kew.merge_claim import merge_claim_wait_reason

    if facts.pr_number is None or not facts.head_oid:
        return None
    claim = store.live_merge_claim(facts.pr_number, facts.head_oid, now=now)
    return None if claim is None else merge_claim_wait_reason(claim, action="ci-fix")


def profile_refusal_hold_reason(refusal: PromptRefusal, item_id: str) -> str:
    """The operator hold a refused ``ci_fix`` prompt profile earns (#3023)."""
    return admission_refused_hold_reason(
        f"{PROMPT_CONFIGURATION_REFUSAL}: {refusal.reason}", item_id
    )


def _profile_held_ids(store: RunStore, epic_ref: str | None) -> set[str]:
    """Nodes a refused prompt profile holds: no ci-fix until it is released."""
    if epic_ref is None:
        return set()
    return {
        h.item_id
        for h in store.list_epic_holds(epic_ref)
        if (h.reason or "").startswith(_PROFILE_HOLD_PREFIX)
    }


def decide_ci_recovery(
    failure_class: CiFailureClass | None, *, budget_ok: bool
) -> RecoveryDecision:
    """Pure policy: given a node's CI failure class and whether it is within
    budget, decide whether to park it, dispatch a fix, or keep holding.

    * ``None`` (not yet classifiable) → ``hold`` — never act on an unknown.
    * ``infra_dead`` → ``park`` — never fixable, so no agent run.
    * ``code_failure`` within budget → ``dispatch``; over budget → ``hold``.
    """
    if failure_class is None:
        return "hold"
    if failure_class == "infra_dead":
        return "park"
    return "dispatch" if budget_ok else "hold"


def _budget_refusal(
    store: RunStore, config: DispatchConfig, now: datetime
) -> BudgetExceeded | None:
    """The ci-fix admit gate's refusal of one more fix run, or None.

    The same gate ``dispatch_ci_fix`` admits under, asked before the run's row
    exists, so a fix the gate would refuse never checks out a worktree.
    """
    from kew.budget import BudgetExceeded, check_pr_subject_budget

    try:
        check_pr_subject_budget(
            store,
            config.budget,
            now=now,
            pending_reservation_usd=config.agent.max_budget_usd,
            count_admitting_run=True,
        )
    except BudgetExceeded as exc:
        return exc
    return None


def ci_recovery_pass(
    held_ci_ids: list[str],
    *,
    graph: Graph,
    node_facts: dict[str, NodeFacts],
    config: DispatchConfig,
    store: RunStore,
    now: datetime,
    classify: ClassifyFn,
    dispatch_fix: DispatchFixFn,
    epic_ref: str | None = None,
) -> CiRecoveryOutcome:
    """Run the auto-ci-fix / infra-park decision over this tick's red-CI nodes.

    ``held_ci_ids`` are node ids held on ``ci_pending`` this tick (the node id
    is the durable item id). Each is classified and acted on at most once —
    the per-node-per-tick attempt guard is simply that this loop visits each id
    once. Callers gate the whole pass on ``config.epic.auto_ci_fix``.

    A code failure the ci-fix admit gate would refuse is not dispatched this
    tick, so no worktree is checked out. The node stays on its ``ci_pending``
    hold unchanged (the closed-PR cleanup and the stall_days path still see
    it), and the refusal is logged as a warning naming the node.

    A fix the active ``ci_fix`` profile refused (#3023) holds the node under
    *epic_ref* with the refusal and its remedy, replacing ``ci_pending``; a node
    on that hold is skipped until an operator releases it.

    A node whose PR head an operator's merge verify claims (#3198) is not
    classified or fixed this tick: the fix would push onto the head the verify
    is testing. It stays on ``ci_pending`` and is looked at again once the
    claim ends; ``claimed`` names it with the wait.
    """
    outcome = CiRecoveryOutcome()
    profile_held = _profile_held_ids(store, epic_ref)
    for nid in held_ci_ids:
        facts = node_facts.get(nid)
        if facts is None or facts.pr_number is None or nid in profile_held:
            continue  # no linked PR to classify, or held for the operator
        if wait := _merge_claim_wait(store, facts, now):
            # A fix pushes onto the head a merge verify is testing: not now.
            outcome.claimed[nid] = wait
            _logger.info("ci recovery: %s not dispatched — %s", nid, wait)
            continue
        failure_class = classify(facts)
        refusal = (
            _budget_refusal(store, config, now)
            if failure_class == "code_failure"
            else None
        )
        decision = decide_ci_recovery(failure_class, budget_ok=refusal is None)
        if decision == "park":
            blast = tuple(sorted(graph.transitive_dependents(nid)))
            outcome.parked.append(StuckNode(nid, INFRA_DEAD_REASON, blast))
        elif decision == "dispatch":
            _record_dispatch(outcome, nid, dispatch_fix(facts), store, epic_ref)
        elif refusal is not None:
            _logger.warning(
                "ci recovery: %s not dispatched, ci-fix budget refused: %s",
                nid,
                refusal,
            )
        # "hold" (and a dispatch that no-op'd) leaves the node held unchanged.
    return outcome


def _record_dispatch(
    outcome: CiRecoveryOutcome,
    nid: str,
    result: bool | PromptRefusal,
    store: RunStore,
    epic_ref: str | None,
) -> None:
    """Fold one dispatch attempt's result into *outcome*.

    A profile refusal replaces the node's ``ci_pending`` hold with the profile
    hold, so the next tick skips it instead of refusing again.
    """
    if isinstance(result, PromptRefusal):
        reason = profile_refusal_hold_reason(result, nid)
        outcome.profile_held[nid] = reason
        if epic_ref is not None:
            store.hold_epic_node_item(epic_ref, nid, reason)
        _logger.warning("ci recovery: held %s — %s", nid, reason)
    elif result:
        outcome.dispatched.append(nid)


# ---------------------------------------------------------------------------
# Production defaults (real gh). Injected fakes replace these in tests.
# ---------------------------------------------------------------------------


def _resolve_failed_run_id(repo: str, sha: str) -> str | None:
    """The newest failed Actions run id for commit ``sha``, or None.

    Uses the completed-runs-for-a-head-sha endpoint and picks the first
    ``conclusion == "failure"`` run. A gh failure returns None (unclassifiable
    → the pass keeps the node held rather than acting on a guess).
    """
    result = subprocess_utils.run(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={sha}&status=completed",
            "--jq",
            '.workflow_runs[] | select(.conclusion=="failure") | .id',
        ],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line
    return None


def default_classify(repo: str) -> ClassifyFn:
    """Real classifier: resolve the node's failed run, fetch its jobs, and
    classify infra-dead vs code-failure. Returns None on any gh failure."""

    def _classify(facts: NodeFacts) -> CiFailureClass | None:
        if not facts.head_oid:
            return None
        run_id = _resolve_failed_run_id(repo, facts.head_oid)
        if run_id is None:
            return None
        try:
            jobs = _gh_run_jobs(repo, run_id)
        except Exception:  # noqa: BLE001 - any gh failure => unclassifiable
            return None
        return classify_ci_failure(jobs)

    return _classify


def default_dispatch_fix(
    config: DispatchConfig, store: RunStore, notifier: Notifier | None = None
) -> DispatchFixFn:
    """Real dispatcher: resolve the node's failed run and reuse
    ``dispatch_ci_fix``. Returns True iff a fix run was actually dispatched
    (``dispatch_ci_fix`` exit 0/1; skips and pre-launch refusals return False),
    or the :class:`~kew.prompt_refusals.PromptRefusal` of a refused ``ci_fix``
    profile (#3023)."""

    def _dispatch(facts: NodeFacts) -> bool | PromptRefusal:
        from kew.ci_fix import attempt_ci_fix
        from kew.runner_preflight import runner_preflight_failure

        if not facts.head_oid:
            return False
        resolved = resolve_runner(config)
        failure = runner_preflight_failure(config, runner_name=resolved.runner.name)
        if failure is not None:
            _logger.error(
                "ci recovery: fix dispatch failed for issue %s — %s (node stays held)",
                facts.issue_num,
                failure,
            )
            return False
        run_id = _resolve_failed_run_id(config.project.repo, facts.head_oid)
        if run_id is None:
            return False
        result = attempt_ci_fix(
            run_id=run_id, config=config, notifier=notifier, store=store
        )
        if isinstance(result, PromptRefusal):
            return result
        return result in (0, 1)

    return _dispatch
