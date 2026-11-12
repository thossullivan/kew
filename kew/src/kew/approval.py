"""Approval gate: pre-dispatch cost estimation and hold/confirm decisions.

Budget (budget.py) is law; approval is policy on top. The gate estimates a
dispatch's cost from run history and requires explicit human sign-off when
the estimate meets or exceeds the configured [approval] threshold_usd.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer

from kew.approval_basis import FEW_RUNS, MEDIAN, NO_HISTORY
from kew.audit import bind_policy_context, emit, record_current_policy, resolve_actor
from kew.labels import _fetch_issue_labels, _update_labels, release_approval_hold

if TYPE_CHECKING:
    from kew.config import BudgetConfig, DispatchConfig
    from kew.run_store import RunStore

#: How many recent matching runs feed the median.
_HISTORY_WINDOW = 10
#: Minimum matching runs for a historical estimate; below this we fall
#: back to the per-issue budget cap (the worst case).
_MIN_MATCHES = 3


@dataclass
class Estimate:
    """A pre-dispatch cost estimate and the evidence behind it."""

    usd: float
    basis: str


def estimate_dispatch_cost(
    store: RunStore | None,
    *,
    complexity: str,
    model: str,
    budget: BudgetConfig,
) -> Estimate:
    """Estimate the cost of a dispatch from run history.

    Median actual cost of the last 10 non-pending runs matching *complexity*
    and *model*. When *complexity* is empty, matches on model only. Fewer
    than 3 matches falls back to ``budget.per_issue_usd`` (the worst case),
    with the basis recording why.
    """
    cap = budget.per_issue_usd
    if store is None:
        return Estimate(usd=cap, basis=NO_HISTORY.format(cap=cap))

    matching = [
        r
        for r in store.list_all()  # ordered oldest -> newest
        if r.status != "pending"
        and r.model == model
        and (not complexity or r.complexity == complexity)
    ]
    recent = matching[-_HISTORY_WINDOW:]

    if len(recent) >= _MIN_MATCHES:
        med = float(statistics.median(r.estimated_cost_usd for r in recent))
        return Estimate(
            usd=med,
            basis=MEDIAN.format(count=len(recent), median=med),
        )
    if recent:
        return Estimate(
            usd=cap,
            basis=FEW_RUNS.format(count=len(recent), cap=cap),
        )
    return Estimate(usd=cap, basis=NO_HISTORY.format(cap=cap))


def log_approval_event(
    store: RunStore | None,
    *,
    event: str,
    issue_num: int,
    estimate: Estimate,
    threshold_usd: float,
    repo: str | None = None,
) -> None:
    """Record one approval event in audit_events.

    Events: approval_hold, approval_granted, approval_confirmed,
    approval_declined. Best-effort — logging failures never affect the
    dispatch outcome. The actor is the approver (resolved here), which
    may differ from the dispatcher recorded on the run row.
    """
    emit(
        store,
        event,
        actor=resolve_actor(),
        issue_num=issue_num,
        repo=repo,
        payload={
            "estimate_usd": round(estimate.usd, 6),
            "basis": estimate.basis,
            "threshold_usd": threshold_usd,
        },
    )


def confirm_dispatch(
    *, issue_num: int, estimate: Estimate, threshold_usd: float
) -> bool:
    """Prompt the operator to confirm an at-or-above-threshold dispatch."""
    return typer.confirm(
        f"Dispatch #{issue_num} estimated at ${estimate.usd:.2f} "
        f"({estimate.basis}) — at or above approval threshold "
        f"${threshold_usd:.2f}. Dispatch anyway?"
    )


def _authorized(config: DispatchConfig, actor_id: str) -> bool:
    """Gate 3: optional approver allowlist (empty = any actor on the host)."""
    allow = config.approval.approvers
    return not allow or actor_id in allow


def _hold_estimate(store: RunStore, issue_num: int) -> tuple[float | None, str]:
    """(estimate_usd, basis) carried from the open approval_hold event, if any."""
    from kew.report import open_approval_holds

    hold = next(
        (h for h in open_approval_holds(store) if h.issue_num == issue_num), None
    )
    return (hold.estimate_usd, hold.basis) if hold else (None, "")


def remote_approve(
    store: RunStore,
    *,
    issue_num: int,
    reason: str,
    config: DispatchConfig,
    source: str = "remote-comment",
) -> str:
    """Release a held issue from a remote (comment-driven) approval.

    Returns 'released' | 'not_held' | 'gh_error' | 'unauthorized'. Idempotent:
    keyed on the needs-approval LABEL (re-read via gh), so a double-comment after
    release no-ops. 'gh_error' means the gh labels fetch failed; 'not_held' means
    labels fetched but the hold label is absent.
    """
    actor = resolve_actor()
    if not _authorized(config, actor.id):
        return "unauthorized"
    current_labels = _fetch_issue_labels(issue_num, config)
    if current_labels is None:
        return "gh_error"
    if config.approval.label not in current_labels:
        return "not_held"
    estimate_usd, basis = _hold_estimate(store, issue_num)
    emit(
        store,
        "approval_granted",
        actor=actor,
        issue_num=issue_num,
        repo=config.project.repo or None,
        payload={
            "source": source,
            "reason": reason,
            "estimate_usd": estimate_usd,
            "basis": basis,
        },
    )
    release_approval_hold(issue_num, config)
    return "released"


def remote_deny(
    store: RunStore,
    *,
    issue_num: int,
    reason: str,
    config: DispatchConfig,
    source: str = "remote-comment",
) -> str:
    """Deny a held issue: audit + remove needs-approval (no ready, no close).

    Returns 'denied' | 'not_held' | 'gh_error' | 'unauthorized'. 'gh_error' means
    the gh labels fetch failed; 'not_held' means labels fetched but the hold label
    is absent.
    """
    actor = resolve_actor()
    if not _authorized(config, actor.id):
        return "unauthorized"
    current_labels = _fetch_issue_labels(issue_num, config)
    if current_labels is None:
        return "gh_error"
    if config.approval.label not in current_labels:
        return "not_held"
    estimate_usd, basis = _hold_estimate(store, issue_num)
    emit(
        store,
        "approval_declined",
        actor=actor,
        issue_num=issue_num,
        repo=config.project.repo or None,
        payload={
            "source": source,
            "reason": reason,
            "estimate_usd": estimate_usd,
            "basis": basis,
        },
    )
    _update_labels(issue_num, config, remove=[config.approval.label])
    return "denied"


def apply_approval_decision(
    action: str, *, issue_num: int, reason: str, config: DispatchConfig, source: str
) -> str:
    """Bind the governance policy, open a short-lived store, record the policy
    snapshot, and apply the approve/decline decision. Shared by the CLI and the
    TUI so both write identically-bound audit events. Returns the status string.
    """
    from pathlib import Path

    from kew.audit import DEFAULT_ANCHORS_PATH
    from kew.config import project_root
    from kew.ledger_identity import canonical_project_root, open_managed_store

    if action not in ("approve", "deny"):
        raise ValueError(f"unknown approval action: {action!r}")
    bind_policy_context(config)
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    root = canonical_project_root(project_root())
    fn = remote_approve if action == "approve" else remote_deny
    with open_managed_store(
        project_root=root,
        db_path=db_path.resolve(),
        anchors_path=root / DEFAULT_ANCHORS_PATH,
        read_only=False,
        check_same_thread=True,
    ) as store:
        record_current_policy(store)
        return fn(
            store, issue_num=issue_num, reason=reason, config=config, source=source
        )
