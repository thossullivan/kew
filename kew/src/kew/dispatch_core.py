"""Shared dispatch execution logic.

Provides execute_dispatch() -- the core dispatch function used by
both single-issue (_dispatch_one) and parallel (scheduler) paths.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from urllib.parse import quote, urlsplit

from kew import __version__, audit, work_dirs, worktree
from kew import sandbox as sandbox_network
from kew.acceptance_author import (
    AcceptanceAuthorRefused,
    AuthorRequest,
    BaseTestRunner,
    prepare_acceptance_contract,
    tested_criteria,
)
from kew.acceptance_dispute import DISPUTE_MARKER, hold_disputed_node
from kew.approval import (
    Estimate,
    confirm_dispatch,
    estimate_dispatch_cost,
    log_approval_event,
)
from kew.artifact_store import PackageStore, PackageStoreError
from kew.artifact_store import default_root as _packages_root
from kew.audit import (
    Actor,
    append_sidecar_evidence,
    configured_endpoint,
    current_policy_hash,
    record_current_policy,
    resolve_actor,
)
from kew.budget import (
    BudgetExceeded,
    check_budget,
    effective_budget_view,
    per_issue_overspend_note,
    runtime_cap_conflict_warning,
    window_admission_gate,
)
from kew.claim_retention import (
    RefusalDelivery,
    claim_recovery_pending,
    require_claim_recovery,
)
from kew.command_cleanup import finish_command_cleanup
from kew.config import DispatchConfig, RouteEntry, project_root
from kew.continuation_recovery import (
    CheckoutAbandonment,
    CheckoutGenerationRefusal,
    checkout_abandonment,
    record_checkout_replacement,
    record_checkout_withdrawal,
)
from kew.credential_handoff import (
    is_detached_credential_child,
    require_credential_route,
)
from kew.credential_issuer import BoundedRunKey, IssuerError
from kew.dependencies import find_blocked_dependents
from kew.dispatch_admission import (
    PendingAdmission,
    execution_cancellation,
    release_admission_reservation,
)
from kew.evidence import (
    AttemptedChange,
    EvidenceKind,
    Producer,
    ProducerKind,
    import_runner_evidence,
)
from kew.exit_signal import RESOURCE_CATEGORY
from kew.human_review import policy_from_config
from kew.ids import (
    GITHUB_CLOSES_RE,
    GITHUB_ID_RE,
    InvalidItemIdError,
    branch_slug,
    closes_marker,
    item_ref,
    validate_item_id,
)
from kew.inference_transport import (
    AdmittedInferenceTransport,
    ResolvedInferenceTransport,
    TransportAdmissionError,
    TransportPreflight,
    admit_inference_transport,
    direct_route,
    require_adaptive_route_admission,
    resolve_inference_transport,
)
from kew.journal_replay import attempted_changes as _replayed_attempts
from kew.journal_replay import replay_host_sequence
from kew.labels import (
    _fetch_issue_labels,
    mark_completed,
    mark_failed,
    mark_needs_approval,
    mark_needs_review_incomplete,
    mark_no_change_candidate,
    mark_ready_from_approval,
    reset_issue_to_ready,
    unblock_issue,
    update_pr_labels,
)
from kew.model_catalog import load_catalog
from kew.model_eol_snapshot import check_lifecycle
from kew.models import GhIssue
from kew.no_change import ATTESTED as NO_CHANGE_ATTESTED
from kew.no_change import CHECK_PASSED as NO_CHANGE_CHECK_PASSED
from kew.no_change import (
    DEFAULT_NO_CHANGE_POLICY,
    AttemptedChangeRecord,
    NamedCheck,
    NoChangeError,
    NoChangeEvidence,
    NoChangeIntent,
    NoChangeJudgment,
    judged_paths_from,
)
from kew.no_change_seal import NoChangeSealError, seal_no_change_package
from kew.notifier import NotificationEvent, Notifier, notify_approval_hold
from kew.orchestrator import (
    _requested_service_tier,
    managed_switchyard_context,
    reconcile_managed_switchyard_lifecycle,
    run_agent,
    sidecar_switchyard_context,
)
from kew.outcomes import tracked_files
from kew.overlap import CLAIM_ROLLBACK_FAILED, DispatchOutcome
from kew.pr_text import (
    ZERO_WIDTH_SPACE,
    PrBodyTooLong,
    agent_pr_body,
    agent_pr_title,
    unsendable,
)
from kew.pricing import (
    build_pricing_overrides,
    compute_prefix_key,
    effective_pricing,
    infer_provider,
    is_priced_model,
    require_pricing_key,
)
from kew.process_groups import HostIdentityUnavailable
from kew.progress import _error, _log, _success, _warn
from kew.prompt import build_prompt, refuse_custom_template_with_addendum
from kew.prompt_addendum import (
    PROMPT_CONFIGURATION_REFUSAL,
    ProfileAddendum,
    PromptConfigurationError,
    profile_version_id,
)
from kew.provenance import orchestrator_provenance as _orchestrator_provenance
from kew.publication import (
    PUBLICATION_CONFIGURATION_REFUSAL,
    PublicationError,
    PublicationStep,
    append_publication_failure,
    publication_failure,
    publication_git_env,
    publication_subprocess_env,
    select_publication_mode,
)
from kew.publication_artifact import ArtifactRejected
from kew.publication_canonical import (
    CanonicalNoChange,
    CanonicalPublication,
    PublicationHeld,
    acceptance_contracts,
    checkout_changed_paths,
    checkout_no_change_evidence,
    host_commit_identity,
    publication_remote,
    publish_checkout,
    remote_branch_sha,
)
from kew.publication_checkout import commit_checkout_worktree, filter_checkout_range
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    ensure_publication_credentials,
    publication_output_guard,
    require_publication_credentials,
    require_publication_launch_credentials,
)
from kew.publication_job import ABANDONED as PUBLICATION_ABANDONED
from kew.publication_job import AMBIGUOUS as PUBLICATION_AMBIGUOUS
from kew.publication_scan import OutputScanTarget
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    RealizedRouteProfile,
    compose_realized_profile,
    compose_route_profile,
)
from kew.repo_host import _origin_remote_url, repo_host
from kew.repomap import generate_repo_map
from kew.run_credentials import (
    RETRY_JITTER_MAX,
    CredentialLifetimeRefused,
    bounded_run_credentials,
    record_credential_attempt,
    require_retry_credential_lifetime,
    retry_backoff_seconds,
    run_timeout_seconds,
)
from kew.run_requests import LAUNCHPAD_MODEL_SOURCE, MODEL_SOURCE_ENV
from kew.run_store import ACCEPTANCE_AUTHOR_RUN_KIND, continuation_checkout_matches
from kew.run_store_work import (
    TRACKER_WORK_RUN_KINDS,
    DispatchReservationReplaced,
    capture_dispatch_reservation,
)
from kew.runner_fields import exception_text, require_public_model
from kew.runner_quota import (
    QUOTA_EXHAUSTED_CATEGORY,
    QuotaRejection,
    record_runner_quota_hold,
)
from kew.runners import PrefixKeyContext, ResolvedRoute, cache_governor, resolve_runner
from kew.runners.governor import (
    DEFAULT_EST_PROMPT_TOKENS,
    GovernorDecision,
    estimate_prompt_tokens,
)
from kew.runners.protocol import AgentRunner, PublicationStyle
from kew.runners.registry import (
    record_runner_launch,
    record_runner_token_warning,
    runner_credential_fingerprint,
    runner_credential_names,
)
from kew.sandbox import (
    PROMPT_FILENAME as _SANDBOX_PROMPT_FILENAME,
)
from kew.sandbox import (
    REFUSAL_CONTINUATION_HOST_ONLY,
    ContainedExecution,
    ContainerStartFailed,
    ResourceCapCode,
    SandboxDecision,
    SandboxRuntimeUnavailable,
    check_agent_image_staleness,
    check_host_capacity,
    check_runtime_memory_capacity,
    open_egress_warning,
    openshell_ignored_argv_settings_warning,
    redact_container_observations,
    redact_credentials,
    resolve_effective_sandbox,
    resolve_effective_sidecar_sandbox,
)
from kew.sandbox import (
    ContainedImageUnresolvedError as ContainedImageUnresolvedError,
)

# Sandbox-runtime shims kept here purely as patch targets: the sandbox
# execution now lives in dispatch_sandbox (Task 5), which reaches these back
# through the kew.dispatch_core module at call time, so the dispatch/sandbox
# suites' patches at kew.dispatch_core.* still land. Unused in this module's own
# code now — noqa keeps ruff from pruning the re-exported names.
from kew.sandbox import build_run_argv as _build_sandbox_argv  # noqa: F401
from kew.sandbox import detect_runtime_cached as _detect_sandbox_runtime  # noqa: F401
from kew.sandbox import launch_container as _launch_container  # noqa: F401
from kew.selector import _has_epic_label, get_complexity
from kew.self_check import rendered_self_check
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import (
    TOOL_PATHS,
    is_rate_limited,
    observe_runner_executable,
    run,
)
from kew.switchyard import SwitchyardProbeResult, probe_switchyard_endpoint
from kew.tracking import (
    STARTUP_REFUSED_CATEGORIES,
    DispatchResult,
    LaunchState,
    merge_launch_evidence,
    partial_dispatch_result,
    startup_refused,
)
from kew.transcript_metrics import (
    DISPATCH_REPORT_MARKER as _DISPATCH_REPORT_MARKER,
)
from kew.transcript_metrics import (
    DerivedRunEvidence,
    agent_report_for,
    first_edit_metrics_for,
    read_derived_evidence,
)
from kew.triage.record import record_extracted_plan
from kew.triage.schema import ItemPlan
from kew.worktree import (
    claim_checkout,
    cleanup_worktree,
    has_uncommitted_changes,
    operator_repo_root,
    release_checkout_claims,
    setup_worktree,
)

if TYPE_CHECKING:
    from kew.breaker import RunBaseline
    from kew.run_store import EpicReservation, RunRecord, RunStatus, RunStore

# Re-exported from dispatch_sandbox (Task 5 extraction) so callers importing
# these names from dispatch_core keep working unchanged — in particular the
# review launcher (#992), which imports _stream_sandbox_log / _finalize_sandbox_run
# from here, and the dispatch/sandbox suites, which patch and import these names
# at kew.dispatch_core.*. The extracted module reaches back at call time through
# the kew.dispatch_core module object, so these patches still take effect. The
# `as`-alias form marks them as explicit re-exports for ruff.
# Re-exported from dispatch_broker (Task 5 extraction): the host-brokered PR
# path (#518). Callers and the dispatch suites keep importing/patching these at
# kew.dispatch_core.*; the extracted module reaches its collaborators back
# through the kew.dispatch_core module object at call time.
from kew.dispatch_broker import (
    _broker_agent_work as _broker_agent_work,
)
from kew.dispatch_broker import (
    _broker_commit_message as _broker_commit_message,
)
from kew.dispatch_broker import (
    _finalize_brokered_pr as _finalize_brokered_pr,
)

# Re-exported from dispatch_preflight (Task 5 extraction): the budget-refusal
# helpers (#661/#891). The admission gates below call _refuse_budget, the
# delegate claim/run paths import _record_budget_refusal_row from here, and the
# suites import/patch these at kew.dispatch_core.*. The extracted module reaches
# mark_failed / _emit_early_exit / the refusal-status constants back through the
# kew.dispatch_core module object at call time.
from kew.dispatch_preflight import (
    _BUDGET_HOLD_PREFIX as _BUDGET_HOLD_PREFIX,
)
from kew.dispatch_preflight import (
    _CONTAINMENT_HOLD_PREFIX as _CONTAINMENT_HOLD_PREFIX,
)
from kew.dispatch_preflight import (
    _budget_hold_reason as _budget_hold_reason,
)
from kew.dispatch_preflight import (
    _budget_refusal_detail as _budget_refusal_detail,
)
from kew.dispatch_preflight import (
    _budget_refusal_message as _budget_refusal_message,
)
from kew.dispatch_preflight import (
    _record_budget_refusal_row as _record_budget_refusal_row,
)
from kew.dispatch_preflight import (
    _record_refusal_row as _record_refusal_row,
)
from kew.dispatch_preflight import (
    _refuse_budget as _refuse_budget,
)
from kew.dispatch_preflight import (
    _refuse_containment as _refuse_containment,
)
from kew.dispatch_sandbox import (
    _SANDBOX_LIVE_FLUSH_SECS as _SANDBOX_LIVE_FLUSH_SECS,
)
from kew.dispatch_sandbox import (
    _SANDBOX_NO_CHANGE_CATEGORY as _SANDBOX_NO_CHANGE_CATEGORY,
)
from kew.dispatch_sandbox import (
    SANDBOX_PUBLICATION_FAILED_CATEGORY,
    ContainedCostGovernor,
    _recordable_usd,
)
from kew.dispatch_sandbox import (
    _accumulate_stream_line as _accumulate_stream_line,
)
from kew.dispatch_sandbox import (
    _finalize_sandbox_run as _finalize_sandbox_run,
)
from kew.dispatch_sandbox import (
    _record_sandbox_start as _record_sandbox_start,
)
from kew.dispatch_sandbox import (
    _run_sandboxed as _run_sandboxed,
)
from kew.dispatch_sandbox import (
    _run_sandboxed_container as _run_sandboxed_container,
)
from kew.dispatch_sandbox import (
    _SandboxTelemetry as _SandboxTelemetry,
)
from kew.dispatch_sandbox import (
    _stream_sandbox_log as _stream_sandbox_log,
)
from kew.dispatch_sandbox import (
    _telemetry_from_evidence as _telemetry_from_evidence,
)
from kew.dispatch_sandbox import (
    _write_live_sandbox_telemetry as _write_live_sandbox_telemetry,
)
from kew.dispatch_sandbox import (
    run_container_and_stream as run_container_and_stream,
)

_logger = logging.getLogger(__name__)

# Fraction of the effective runtime cap at which the cost-aware retry guard
# treats a failed attempt as budget-stopped rather than retrying it (#642).
_BUDGET_GUARD_FRACTION = 0.9
WORKER_CAP_REACHED_MESSAGE = "Worker cap reached — cannot start another run."


class WorkerCapReached(Exception):
    """The fleet-wide worker cap was hit at the atomic slot insert (#745).

    Raised when ``try_admit_run`` refuses a dispatch that passed the cheap
    preflight but lost the count+insert race to concurrent dispatchers. The
    caller catches it, requeues the issue, and tears down the (unused)
    worktree — no run row was written, so there is no slot to release.
    """

    def __init__(self, issue_num: int) -> None:
        super().__init__(f"worker cap reached admitting #{issue_num}")
        self.issue_num = issue_num


class _CompletedAttemptCleanupFailure(Exception):
    """A completed attempt whose sidecar cleanup refused finalization."""

    def __init__(self, cause: Exception, result: DispatchResult) -> None:
        super().__init__(str(cause))
        self.result = result


class _CheckoutIdentityRefused(PublicationError, worktree.CheckoutAdmissionRefusal):
    """A paid launch lost the checkout generation bound before admission."""

    category = "checkout_identity_changed"


def _accumulated_cost_usd(
    result: DispatchResult,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    *,
    classifier_model: str | None = None,
    classifier_input_tokens: int = 0,
    classifier_output_tokens: int = 0,
) -> float:
    """Best-available accumulated cost for the run so far.

    ``compute_cost`` prefers the runner-reported total and falls back to a
    token-based estimate, so the budget guard works even for runners that
    never report a cost figure. Recomputed authoritatively after the loop.
    """
    result.compute_cost(
        model=model or None,
        overrides=overrides,
        classifier_model=classifier_model,
        classifier_input_tokens=classifier_input_tokens,
        classifier_output_tokens=classifier_output_tokens,
    )
    return result.estimated_cost_usd


# Categories that mean "the run stopped because it hit a budget cap" — these get
# the budget-tailored reason + next-step block (CS-8 / Group 4).
_BUDGET_CATEGORIES = frozenset({"budget_stopped", "budget_stop", "budget_exceeded"})

# Terminal status/category for a clean budget-preflight refusal (#891 / O2).
# Deliberately DISTINCT from "failure" and from the driver's
# "dispatch_child_died" diagnostic: an epic node the child refuses before any
# agent spawn writes THIS row (keyed to the driver's reservation) so the tick
# reconciles the reservation from the row instead of grace-timing it out as a
# dead corpse. "refused" is intentionally NOT in the overlay's
# `_TERMINAL_FAILURE_STATUSES`, so once the cap is raised and the hold clears
# the node returns to the dispatch frontier (TODO) rather than latching FAILED.
_REFUSAL_STATUS: RunStatus = "refused"
_BUDGET_REFUSAL_CATEGORY = "budget_refused"

# The same shape for a containment refusal (#1144): the runner could not be
# contained on a host that CAN contain, so no agent spawned. It shares
# ``_REFUSAL_STATUS`` — a clean refusal, re-admittable once the operator maps
# an image — but carries its own category so the budget-keyed delegate paths
# never mistake one for the other, and so `kew report` can count containment
# refusals separately from budget ones.
_CONTAINMENT_REFUSAL_CATEGORY = "containment_refused"


def _warn_if_cap_conflict(
    config: DispatchConfig,
    store: RunStore | None,
    issue_num: int,
    item_id: str | None = None,
) -> None:
    """Warn pre-dispatch when the runtime kill cap can overshoot per-issue budget.

    The preflight gate only sees *historical* spend, so a run under a low
    per-issue cap sails through on its first dispatch (prior spend $0). Surface
    the mismatch before the run starts (CS-7, #649).

    Only fires when the per-issue cap was *explicitly configured* — the shipped
    default (per-issue $15 vs kill cap $25) is expected, not a misconfiguration,
    and nagging on every default run would be noise.

    Best-effort: an advisory banner must never break a dispatch, so any store
    hiccup is swallowed.
    """
    if "per_issue_usd" not in config.budget.model_fields_set:
        return
    try:
        view = effective_budget_view(config, store, issue_num, item_id=item_id)
        msg = runtime_cap_conflict_warning(view)
    except Exception:
        return
    if msg:
        _warn(msg)


def _note_if_over_per_issue(
    config: DispatchConfig,
    store: RunStore | None,
    issue_num: int,
    item_id: str | None = None,
) -> None:
    """After a run, note when recorded issue spend has passed the per-issue cap.

    Call *after* the run record is saved so the spend total includes this run.
    The note tells the operator the next dispatch for this issue will be blocked
    at the budget preflight (CS-7, #649).

    Best-effort: a completion note must never break the terminal path, so any
    store hiccup is swallowed.
    """
    try:
        view = effective_budget_view(config, store, issue_num, item_id=item_id)
        note = per_issue_overspend_note(view)
    except Exception:
        return
    if note:
        _warn(note)


def _failure_reason(
    error_category: str,
    exit_code: int | None,
    budget_cap_usd: float | None,
    cost_usd: float | None,
    *,
    breaker_rule: str = "",
    breaker_threshold_usd: float | None = None,
) -> str | None:
    """Human phrase naming a hard-fail cause, or None when unknown (CS-8).

    Keyed off the already-tracked ``error_category`` so the operator sees the
    real reason instead of a bare exit code. Returning None lets the caller
    fall back to ``(exit N)``.
    """
    if error_category in _BUDGET_CATEGORIES:
        cap = budget_cap_usd or cost_usd
        return f"budget cap hit (${cap:.2f})" if cap else "budget cap hit"
    if error_category == "killed":
        return "killed by operator"
    if error_category == "circuit_breaker":
        return _breaker_reason(breaker_rule, cost_usd, breaker_threshold_usd)
    if error_category == "watchdog_kill":
        return f"watchdog kill (exit {exit_code})"
    if error_category == RESOURCE_CATEGORY:
        return f"resource exhaustion, OOM kill or abort (exit {exit_code})"
    if error_category in ("permanent", "transient", "exception"):
        return f"agent error (exit {exit_code})"
    return None


def _breaker_reason(
    rule: str, cost_usd: float | None, threshold_usd: float | None
) -> str:
    """Name the breaker rule that stopped a run, its cost and its bar (#1798)."""
    name = rule or "cost_anomaly"
    cost = f"${cost_usd:.2f}" if cost_usd else "cost unknown"
    if threshold_usd is None:
        return f"circuit breaker {name} ({cost}, threshold unknown)"
    return f"circuit breaker {name} ({cost} over the ${threshold_usd:.2f} threshold)"


def _failure_summary_line(
    *,
    issue_num: int,
    error_category: str,
    exit_code: int | None,
    total_attempts: int,
    budget_cap_usd: float | None = None,
    cost_usd: float | None = None,
    breaker_rule: str = "",
    breaker_threshold_usd: float | None = None,
) -> str:
    """One-line hard-fail summary naming the reason + attempt count (CS-8).

    Never prints ``(exit N)`` alone when a category is known.
    """
    attempts = max(1, total_attempts)
    plural = "attempt" if attempts == 1 else "attempts"
    reason = _failure_reason(
        error_category,
        exit_code,
        budget_cap_usd,
        cost_usd,
        breaker_rule=breaker_rule,
        breaker_threshold_usd=breaker_threshold_usd,
    )
    if reason:
        return f"Dispatch failed for #{issue_num} — {reason}, {attempts} {plural}"
    return f"Dispatch failed for #{issue_num} (exit {exit_code}), {attempts} {plural}"


def _next_step_lines(
    *,
    issue_num: int,
    error_category: str,
    log_file: Path | str | None,
    epic_ref: str | None = None,
    item_id: str | None = None,
) -> list[str]:
    """Category-tailored 'Next:' recovery block (CS-8): 2-3 lines, exact cmds."""
    lines = ["Next:"]
    if epic_ref and item_id:
        lines.append(f"  retry fresh: `kew epic retry {epic_ref} {item_id}`")
    elif error_category in _BUDGET_CATEGORIES:
        lines.append(
            f"  raise `agent.max_budget_usd` in kew.toml, then "
            f"`kew dispatch {issue_num}`"
        )
    else:
        lines.append(f"  retry fresh: `kew dispatch {issue_num}`")
    if log_file:
        lines.append(f"  log: {log_file}")
    return lines


def _print_failure_next_steps(
    *,
    issue_num: int,
    error_category: str,
    log_file: Path | str | None,
    epic_ref: str | None = None,
    item_id: str | None = None,
) -> None:
    """Emit the 'Next:' recovery block to the operator (CS-8)."""
    for line in _next_step_lines(
        issue_num=issue_num,
        error_category=error_category,
        log_file=log_file,
        epic_ref=epic_ref,
        item_id=item_id,
    ):
        _log(line)


def _terminal_outcome(
    result: DispatchResult,
    *,
    exit_code: int,
    changed_files: list[str] | None = None,
    category: str = "",
    detail: str = "",
) -> DispatchOutcome:
    """A nonzero worker outcome that keeps the runner's error category (#1832).

    The scheduler prints ``category — reason`` for every finished child, so a
    killed, timed-out, or needs-human run must name itself instead of the
    tautology ``failed — dispatch ended with exit code 1``.
    """
    # ``getattr``: some callers hand in a partial result (tests pass a
    # SimpleNamespace); a missing field degrades the line, never the outcome.
    category = category or getattr(result, "error_category", "") or "failed"
    attempts = max(int(getattr(result, "attempt_number", 0) or 0), 1)
    reason = f"run ended as {category} after {attempts} attempt(s)"
    detail = detail or getattr(result, "stop_reason", "")
    if detail:
        reason = f"{reason}; {detail}"
    log_file = getattr(result, "log_file", None)
    if log_file:
        reason = f"{reason}; log: {log_file}"
    return DispatchOutcome(
        exit_code=exit_code or 1,
        changed_files=list(changed_files or []),
        run_id=getattr(result, "run_id", None) or None,
        category=category,
        reason=redact_credentials(reason),
        launched=getattr(result, "launched", False) is not False,
    )


def _normalize_reported_model(value: str | None) -> str | None:
    """Normalize legacy identity inputs; preserve valid pricing ids unchanged.

    Runner adapters validate the complete protocol value before this boundary.
    Legacy callers still receive control stripping and the historical length cap.
    """
    from kew.runner_fields import reported_identity, reported_model_identity

    if not value:
        return None
    value = reported_identity(value, "model")
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:256]
    return reported_model_identity(cleaned) or None


def _require_public_route_model(value: object) -> None:
    """Check concrete route identities before admission and persistence."""
    if isinstance(value, str):
        require_public_model(value)


def _emit_early_exit(
    store: RunStore | None,
    *,
    issue_num: int,
    complexity: str,
    runner: str,
    model: str,
    exit_code: int,
    duration_seconds: float,
    error_category: str,
    repo: str | None,
    item_id: str | None = None,
    run_id: str | None = None,
    epic_ref: str | None = None,
    admission_reason: str | None = None,
    reason: str | None = None,
    strict: bool = False,
) -> None:
    """Emit ``dispatch_completed`` for paths that never created a run row.

    ``item_id`` carries the durable work-item key (``SFD-133``) on non-GitHub
    dispatches so the run-less early-exit row is findable by item. A claimed
    continuation may also pass its durable owner id and epic, allowing audit
    enrichment to retain the preserved-work lineage. ``item_id`` is threaded
    to the ``audit_events.item_id`` column (v24, hashed on rows from v77 on)
    AND kept present-as-None in the payload — the payload copy keeps the
    ``dispatch_completed`` schema uniform (mirroring ``reported_model``) and is
    asserted by tests.
    """
    payload = {
        **_orchestrator_provenance(),
        "complexity": complexity,
        "runner": runner,
        "model": model,
        "item_id": item_id,
        # No run/result on this path -> nothing reported (#251). Key kept
        # present-as-None so every dispatch_completed payload has a uniform
        # schema and consumers never KeyError.
        "reported_model": None,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 2),
        "cost_usd": 0.0,
        "tokens": 0,
        "attempt_number": 0,
        "error_category": error_category,
    }
    if epic_ref is not None:
        payload["epic_ref"] = epic_ref
    if admission_reason is not None:
        # Additive evidence only; this does not add a slot to the chained
        # entry-hash preimage.
        payload["transport_admission_reason"] = admission_reason
    payload["reason"] = reason or admission_reason or error_category
    audit.emit(
        store,
        "dispatch_completed",
        actor=resolve_actor(),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload=payload,
        strict=strict,
    )


class GitHubAuthError(Exception):
    """Raised when the GitHub CLI returns an authentication failure."""


_AUTH_KEYWORDS = ("401", "unauthorized", "bad credentials")

#: gh exits 4 when it holds no credential at all, and tells the user to log in.
_GH_NOT_LOGGED_IN_EXIT_CODE = 4
_NOT_LOGGED_IN_KEYWORDS = ("gh auth login", "not logged in")
_NOT_LOGGED_IN_MESSAGE = "GitHub CLI is not logged in — run `gh auth login`"
_REFUSED_CREDENTIAL_MESSAGE = "GitHub authentication failed — check your token"

#: The variables gh reads a token from ahead of its stored login, in gh's own
#: order, each with the message naming it. ``gh auth login`` refuses to run
#: while one is set, so the message names the variable, never its value.
_GH_TOKEN_ENV_MESSAGES = (
    (
        "GH_TOKEN",
        "GitHub authentication failed — GitHub refused the credential gh "
        "read from GH_TOKEN; replace or unset GH_TOKEN",
    ),
    (
        "GITHUB_TOKEN",
        "GitHub authentication failed — GitHub refused the credential gh "
        "read from GITHUB_TOKEN; replace or unset GITHUB_TOKEN",
    ),
)


def _refused_credential_message() -> str:
    """The bad-credential message, naming the variable gh read it from."""
    for name, message in _GH_TOKEN_ENV_MESSAGES:
        if os.environ.get(name):
            return message
    return _REFUSED_CREDENTIAL_MESSAGE


def _gh_auth_failure(result: subprocess.CompletedProcess[str]) -> str | None:
    """Return the operator-facing auth failure in *result*, or None.

    Exit 4 is gh holding no credential at all. Otherwise a 401 wins over a
    login hint: gh prints ``gh auth login`` beside a refused token too.
    """
    if result.returncode == _GH_NOT_LOGGED_IN_EXIT_CODE:
        return _NOT_LOGGED_IN_MESSAGE
    stderr_lower = (result.stderr or "").lower()
    if any(kw in stderr_lower for kw in _AUTH_KEYWORDS):
        return _refused_credential_message()
    if any(kw in stderr_lower for kw in _NOT_LOGGED_IN_KEYWORDS):
        return _NOT_LOGGED_IN_MESSAGE
    return None


def _check_github_connectivity(_config: DispatchConfig) -> bool:
    """Return True if GitHub API is reachable, False otherwise.

    Raises GitHubAuthError when the failure is an authentication error (a gh
    that was never logged in, or an expired or invalid token) rather than a
    network connectivity issue.
    Retries up to 3 times with backoff when rate-limited.
    """
    for attempt in range(3):
        try:
            result = run(["gh", "api", "rate_limit"], check=False, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            _logger.debug(
                "GitHub connectivity check failed: %s",
                redact_credentials(exception_text(exc)),
            )
            return False

        if result.returncode == 0:
            return True

        # Check auth before rate-limit — a response could match both patterns
        auth_failure = _gh_auth_failure(result)
        if auth_failure is not None:
            raise GitHubAuthError(auth_failure)

        if is_rate_limited(result):
            if attempt < 2:
                _warn("GitHub API rate limited, will retry")
                time.sleep(30 * (2**attempt))
                continue
            _error("GitHub API rate limited after retries")
            return False

        return False

    return False  # unreachable — loop always returns or continues


def check_pr_exists(branch: str, config: DispatchConfig) -> int | None:
    """Return the PR number for *branch* via REST, or ``None`` if absent.

    ``gh pr list`` uses GraphQL. Publication is deliberately kept on REST so
    an exhausted GraphQL budget cannot turn a completed implementation into a
    failed PR lookup (#1265/#1267).
    """
    repo = _project_repo_slug(config)
    if not repo:
        raise RuntimeError(
            "GitHub repository slug unavailable for PR lookup: project.repo is "
            "empty and the project origin did not resolve to owner/repo"
        )
    owner = repo.partition("/")[0]
    result = run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/{repo}/pulls",
            "-f",
            f"head={owner}:{branch}",
            "-f",
            "state=all",
            "-f",
            "per_page=1",
            "--jq",
            ".[0].number // empty",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"GitHub REST PR lookup failed (exit {result.returncode}): "
            f"{result.stderr or result.stdout}"
        )
    try:
        num = int(result.stdout.strip())
    except (ValueError, TypeError):
        return None
    return num if num > 0 else None


def _open_pull_requests_for_head(repo: str, branch: str) -> list[dict[str, Any]]:
    """Return the open PRs whose head is *branch*, over REST, or raise."""
    owner = repo.partition("/")[0]
    try:
        result = run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{repo}/pulls",
                "-f",
                f"head={owner}:{branch}",
                "-f",
                "state=open",
                "-f",
                "per_page=100",
            ],
            check=False,
            env=publication_subprocess_env(),
        )
    except Exception as exc:
        raise PublicationError("push", exc) from exc
    if result.returncode != 0:
        detail = redact_credentials(result.stderr or result.stdout or "").strip()
        raise PublicationError(
            "push",
            f"cannot look up open PRs for branch {branch!r} "
            f"(exit {result.returncode}): {detail[:200]}",
        )
    try:
        payload: Any = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, TypeError) as exc:
        raise PublicationError(
            "push", f"open PR lookup for branch {branch!r} was malformed"
        ) from exc
    if not isinstance(payload, list):
        raise PublicationError(
            "push", f"open PR lookup for branch {branch!r} was malformed"
        )
    return [pr for pr in payload if isinstance(pr, dict)]


def revoke_ci_approval(branch: str, config: DispatchConfig) -> tuple[int, ...]:
    """Remove the human CI approval label from every open PR on *branch*.

    The host calls this, as a publication's ``before_push`` hook, once it
    knows it has a new head to push to a branch that may already have a PR
    (#2033). A no-change publication never calls it. Private-repository CI
    runs an agent PR on the self-hosted runner only while
    ``labels.ci_approved`` is present, so
    clearing it first means the ``synchronize`` event for the new head is
    skipped until a human approves that head. A branch with no open PR (a
    first publication) has nothing to clear; this never adds the label.

    Fails closed: when the lookup or the removal fails, it raises
    :class:`PublicationError` and the caller does not push.
    """
    label = config.labels.ci_approved
    repo = _project_repo_slug(config)
    if not repo:
        raise PublicationError(
            "push",
            f"cannot clear the {label!r} label before pushing {branch!r}: "
            "project.repo is empty and the project origin did not resolve "
            "to owner/repo",
        )
    cleared: list[int] = []
    for pr in _open_pull_requests_for_head(repo, branch):
        number = pr.get("number")
        if not isinstance(number, int) or label not in extract_label_names(pr):
            continue
        try:
            result = run(
                [
                    "gh",
                    "api",
                    "--method",
                    "DELETE",
                    f"repos/{repo}/issues/{number}/labels/{quote(label, safe='')}",
                ],
                check=False,
                env=publication_subprocess_env(),
            )
        except Exception as exc:
            raise PublicationError("push", exc) from exc
        if result.returncode != 0:
            raise PublicationError(
                "push",
                f"could not remove the {label!r} label from PR #{number} "
                f"(exit {result.returncode}); new agent commits are not pushed "
                "while an old CI approval stands",
            )
        _log(f"PR #{number}: removed '{label}'; the new head needs human CI approval")
        cleared.append(number)
    return tuple(cleared)


def extract_label_names(obj: dict[str, Any]) -> list[str]:
    """Extract label name strings from a GitHub issue or PR dict."""
    raw = obj.get("labels", [])
    if not isinstance(raw, list):
        return []
    return [lbl.get("name", "") for lbl in raw if isinstance(lbl, dict)]


def extract_issue_number(pr_body: str, branch_name: str = "") -> int | None:
    """Extract issue number from PR body or branch name.

    Checks for GitHub closing keywords first, then falls back to
    extracting the trailing number from the branch name
    (e.g. ``agent/issue-42`` -> 42).
    """
    match = GITHUB_CLOSES_RE.search(pr_body)
    if match:
        return int(match["item_id"])
    if branch_name:
        branch_match = re.search(r"(\d+)$", branch_name)
        if branch_match:
            return int(branch_match.group(1))
    return None


def resolve_issue_number(
    pr_body: str,
    branch_name: str = "",
    *,
    store: RunStore | None = None,
) -> int | None:
    """Resolve a PR back to its issue number, store-first (A6).

    The run store is the canonical PR<->item join: ``find_run_by_branch``
    maps the PR's head branch to the run that opened it, which carries the
    durable ``issue_num``. Only when the store has no matching run (or no
    store is given) does this fall back to the GitHub-native
    :func:`extract_issue_number` (``Closes #N`` marker, then trailing branch
    digits).

    A Linear run carries ``issue_num == 0`` (the non-numeric-item sentinel);
    that is not a usable GitHub number, so those rows fall through to the gh
    fallback like an unmatched branch would.

    ``graph/driver/facts.py`` stays GitHub-native and does NOT route through
    here — its ``closedByPullRequestsReferences`` linkage is exempt (L2
    replaces facts wholesale for Linear).
    """
    if store is not None and branch_name:
        record = store.find_run_by_branch(branch_name)
        if record is not None and record.issue_num > 0:
            return record.issue_num
    return extract_issue_number(pr_body, branch_name)


@dataclass
class _TrackedRun:
    """Resolved per-run state shared across the extracted phases of
    ``run_tracked_agent``.

    Built once at the top of ``run_tracked_agent`` from the resolved runner/model
    and call arguments, then threaded read-only into each phase helper so the
    phases stay independently readable without re-deriving the run id, log path,
    or audit envelope (actor/endpoint/repo). ``dispatch_start`` is the shared
    monotonic clock used for every duration measurement.
    """

    config: DispatchConfig
    store: RunStore | None
    run_id: str
    issue_num: int
    item_id: str | None
    branch: str
    title: str
    complexity: str
    run_kind: str
    loop_id: str | None
    prefix_key: str | None
    # The INFERENCE provider (anthropic, openrouter, ...) — cache-prefix and
    # pricing currency. Never the work tracker: see ``source_tracker``.
    provider: str | None
    runner: AgentRunner
    effective_model: str
    actor: Actor
    endpoint: str | None
    repo: str | None
    log_file: Path
    dispatch_start: float
    transport: AdmittedInferenceTransport
    transport_health_check: Callable[[], bool] | None = None
    # Set when this run executes in a container: the image to run it in (#1462
    # / W1.1 node 3d). None means host execution. The argv is rebuilt per
    # attempt rather than stored, because a retry re-derives the container name
    # and the runner flags from the state at that moment.
    contained_image: str | None = None
    # The immutable image ID captured by THIS attempt's profile; the container
    # argv launches by this ID, never the tag (#1550 review F1). Set by
    # _launch_boundary each time an attempt's boundary is resolved.
    resolved_image_id: str | None = None
    # The resolved SandboxDecision posture for a host-side launch ("native",
    # "uncontained", "host", ...). A codex run that contains itself and an
    # operator's dangerously-uncontained run are different facts from plain
    # host execution, and the realized profile must not flatten them (#1550
    # review F6). None means the caller resolved no explicit posture.
    sandbox_posture: str | None = None
    # Injected-context sizes (spec D2 / R1-8), persisted on the pending row.
    # ``map_tokens`` is the repo map actually rendered; ``lesson_corpus_tokens``
    # stays None until Phase 2 ships that layer.
    map_tokens: int | None = None
    lesson_corpus_tokens: int | None = None
    # Whether the rendered dispatch prompt carried the pre-publish self-check
    # (#2454). Persisted on the pending row, like the sizes above.
    self_check: bool = False
    # The operator addendum version the rendered prompt carried (#3022).
    prompt_profile_version_id: int | None = None
    # The WORK TRACKER this run's item came from ("github" / "linear") — the
    # dispatch-time ``provider`` argument, which names a tracker even though
    # the same word names an inference provider one field up (#1509 / I01).
    # Persisted as ``run_admission_identity.source_tracker``.
    source_tracker: str = "github"
    # Revalidate the pre-admission checkout generation at each paid boundary.
    # None preserves legacy and review callers that do not own a canonical
    # checkout identity.
    paid_launch_check: Callable[[], None] | None = None
    # Set at spawn, even when streaming raises before run_agent returns.
    launched: bool = False
    launch_state: LaunchState | None = None
    admission: PendingAdmission | None = None
    credential: BoundedRunKey | None = None
    on_launch: Callable[[], None] | None = None
    reservation: EpicReservation | None = None
    reservation_pinned: bool = False
    #: The breaker baseline armed for this run, kept for its audit event (#1798).
    breaker_baseline: RunBaseline | None = None


def _tracked_runner_name(run: _TrackedRun) -> str:
    """Read a runner name safely for terminal/test-shaped tracked runs."""
    return str(getattr(run.runner, "name", ""))


@dataclass
class _RetryOutcome:
    """Outcome of ``run_tracked_agent``'s retry loop.

    ``early_return`` is a terminal ``DispatchResult`` produced by the loop's
    exception path that the caller must return immediately, skipping
    finalization (mirrors the original ``except`` block's early ``return``). On
    the normal path it is ``None`` and ``result`` / ``last_exit_code`` /
    ``total_attempts`` / ``pr_confirmed`` carry the loop's final state forward
    into ``_finalize_result``.
    """

    result: DispatchResult | None = None
    last_exit_code: int = 1
    total_attempts: int = 0
    pr_confirmed: bool = False
    early_return: DispatchResult | None = None


def _builtin_default_model() -> str:
    """kew's built-in model: the catalog row whose ``default_lanes`` has ``default``."""
    return load_catalog().lane_default("default") or ""


def _resolve_runner_and_model(
    config: DispatchConfig,
    runner: AgentRunner | None,
    model: str | None,
) -> tuple[AgentRunner, str]:
    """Resolve the runner/model pair, defaulting the model for cost estimates.

    Fills any missing runner/model from ``resolve_runner`` and defaults an unset
    model to the catalog's ``default`` lane (warning once so cost estimates
    stay meaningful). Returns the resolved runner and effective model.
    """
    if runner is None or model is None:
        resolved = resolve_runner(config)
        if runner is None:
            runner = resolved.runner
        if model is None:
            model = resolved.model
    effective_model = model or _builtin_default_model()
    if not model:
        _warn(
            f"No model configured — defaulting to {effective_model} for cost estimates"
        )
    return runner, effective_model


def _admit_tracked_transport(
    config: DispatchConfig,
    runner: AgentRunner,
    model: str,
    resolved: ResolvedInferenceTransport | None,
    preflight: TransportPreflight | None,
) -> AdmittedInferenceTransport:
    """Admit the bound flow transport or resolve one for other tracked callers."""
    resolved = resolved or resolve_inference_transport(config, runner, model)
    require_credential_route(runner, resolved)
    preflight = _tracked_transport_preflight(config, resolved, preflight)
    return admit_inference_transport(resolved, preflight=preflight, config=config)


def _admission_item_id(run: _TrackedRun) -> str | None:
    """The opaque work item this run was admitted for, or None if synthetic.

    Only tracker-work run kinds may contribute a source item. Other kinds can
    still carry ``RunRecord.item_id`` as a fleet or budget bucket, and reviews
    can carry a PR number in ``issue_num``; neither key identifies admitted
    tracker work.

    A GitHub dispatch may carry only ``issue_num``: on that tracker the item id
    and the issue number are the same value, so callers on the classic path
    never set ``item_id`` (``execute_dispatch`` does the same substitution
    before validating). That is a rename, not an inference. Any other tracker
    must supply its opaque id explicitly — deriving one from ``issue_num``
    would resurrect the numeric shim this identity exists to retire. The ``0``
    sentinel means "no work item", so it stays synthetic.
    An explicit empty ``item_id`` also means no tracker item, even with a PR number.
    """
    if run.run_kind not in TRACKER_WORK_RUN_KINDS or run.item_id == "":
        return None
    if run.item_id:
        return run.item_id
    if run.source_tracker == "github" and run.issue_num > 0:
        return str(run.issue_num)
    return None


def _profilable_runner(runner_name: str) -> bool:
    """True when this runner has a capability manifest to compose a profile from.

    False only for a runner kew does not know — in practice a test double, since
    ``get_runner`` fails closed on an unknown name long before a launch. Such a
    run records no profile rather than a fabricated one, and the pre-launch gate
    stands down for it in the same breath, so the two can never disagree.
    """
    from kew.runners.capabilities import capability_manifest

    try:
        capability_manifest(runner_name)
    except ValueError:
        return False
    return True


def _auth_path_identifier(run: _TrackedRun, runner_name: str) -> str | None:
    """WHICH credential this launch authenticated with — never the credential.

    An environment variable NAME is auth-path identity: it says the launch went
    through, say, the Bedrock path rather than a direct API key. The value is
    never read, and a runner authenticated some other way (a CLI login) has no
    env-var path, which is recorded as an unknown.
    """
    from kew.sandbox import runner_credential_env_vars

    if run.transport.resolved.sidecar:
        return "synthetic:per-attempt"
    names = run.transport.resolved.credential_env_names or runner_credential_env_vars(
        runner_name
    )
    present = next((name for name in names if os.environ.get(name)), None)
    if present is not None:
        return f"env:{present}"
    # A HOST launch with no populated credential env authenticates through the
    # runner's own CLI login (codex declares no env names at all; claude falls
    # back to its keychain session) — an honest, nameable path, not an unknown.
    # A contained launch cannot inherit CLI login, so absence there stays None.
    if run.contained_image is None:
        return "cli-login"
    return None


def _require_contained_image_id(run: _TrackedRun, *, refusal: str) -> str:
    """The immutable ID behind this attempt's image tag, or refuse saying why.

    Both contained sites go through here so the operator gets the remedy that
    matches the cause. ``contained_image`` is not None at either call site —
    both are inside a ``contained_image is None`` early return — so an empty
    one here is a programming error, not an operator-facing state.
    """
    from kew.sandbox import image_id_runtime, require_image_id

    assert run.contained_image is not None  # nosec B101 - call-site invariant
    return require_image_id(
        image_id_runtime(
            run.config.sandbox, _detect_sandbox_runtime(run.config.sandbox)
        ),
        run.contained_image,
        runner=run.runner.name,
        refusal=refusal,
    )


def _boundary_shared_facts(run: _TrackedRun, runner_name: str) -> dict[str, str | None]:
    """The owned facts host and contained launches record identically."""
    from kew.pricing import infer_provider
    from kew.run_store_work import TRACKER_WORK_RUN_KINDS as _WORK_KINDS

    return {
        # A planning bucket or review pass has no source work item, so it has
        # no source provider either — copying the default tracker in would
        # assert an admission that never happened (#1550 review F9).
        "source_provider": (
            (run.source_tracker or None) if run.run_kind in _WORK_KINDS else None
        ),
        # The inference provider is a property of the resolved route, not of
        # the caller remembering to pass one.
        "inference_provider": run.provider
        or infer_provider(run.effective_model, runner_name)
        or None,
        "endpoint": run.transport.resolved.endpoint or run.endpoint,
        "service_tier": _requested_service_tier(run.config, runner_name) or None,
        "auth_path": _auth_path_identifier(run, runner_name),
    }


def _host_boundary(
    run: _TrackedRun, runner_name: str, shared: dict[str, str | None]
) -> RealizedLaunchBoundary:
    """A host-side launch boundary, honest about its resolved posture.

    Task 10 requires the native executable's version, so the bounded probe
    runs here; its failure records unavailable rather than blocking a launch.
    The digest still identifies the exact bytes.
    """
    native = observe_runner_executable(runner_name, probe_version=True)
    posture = {
        "native": Containment.NATIVE,
        "uncontained": Containment.UNCONTAINED,
    }.get(run.sandbox_posture or "", Containment.HOST)
    return RealizedLaunchBoundary(
        containment=posture,
        runner_executable=None if native is None else native.path,
        runner_version=None if native is None else native.version,
        runner_digest=None if native is None else native.sha256,
        **shared,
    )


def _launch_boundary(run: _TrackedRun) -> RealizedLaunchBoundary:
    """Resolve the exact boundary this attempt is about to launch across.

    Host and contained launches record the SAME owned facts (tracker, inference
    provider, endpoint, tier, auth path) and differ only in the identity each
    can honestly own: a host launch identifies its native executable, a
    contained one identifies its resolved image ID. An image whose ID the
    runtime will not report REFUSES the attempt before any charge — the
    mutable tag alone is not evidence of what ran.
    """
    runner_name = _tracked_runner_name(run)
    shared = _boundary_shared_facts(run, runner_name)
    if run.contained_image is None:
        return _host_boundary(run, runner_name, shared)
    # Cleared before resolving so a refused or invalid capture can never
    # leave a previous attempt's ID (or an unvalidated one) behind for the
    # argv to launch — the binding is per attempt or not at all.
    image_id = run.resolved_image_id if run.transport.resolved.sidecar else None
    if image_id is None:
        run.resolved_image_id = None
        # Refuse rather than record UNKNOWN and launch anyway: the profile would
        # claim nothing while the attempt spent money across a boundary nobody can
        # name (#1550 review F1). require_image_id raises with the remedy that
        # matches the cause — build it, install a runtime, or fix the daemon.
        image_id = _require_contained_image_id(
            run, refusal="the attempt is refused before any charge"
        )
    boundary = RealizedLaunchBoundary(
        containment=Containment.CONTAINED,
        image_ref=run.contained_image,
        image_id=image_id,
        **shared,
    )
    # Assigned only after the boundary validated the ID: the LAUNCH binds to
    # this same value (see _contained_execution_for) — capturing one artifact
    # and running whatever the tag points at by then is the retag race this
    # field exists to end.
    run.resolved_image_id = image_id
    return boundary


def _attempt_route_profile(
    run: _TrackedRun, attempt_number: int
) -> RealizedRouteProfile | None:
    """Compose this attempt's realized profile, or None for an unknown runner."""
    runner_name = _tracked_runner_name(run)
    if not _profilable_runner(runner_name):
        return None
    return compose_realized_profile(
        run_id=run.run_id,
        attempt_number=attempt_number,
        route=compose_route_profile(
            runner=runner_name,
            requested_model=run.effective_model,
            transport=run.transport.resolved,
        ),
        boundary=(
            replace(_launch_boundary(run), auth_path=run.credential.auth_path)
            if run.credential is not None
            else _launch_boundary(run)
        ),
        credential_issuer=None if run.credential is None else run.credential.identity,
    )


def _record_sidecar_admission_evidence(
    run: _TrackedRun, *, attempt_number: int, fingerprint: str, supervisor: Any
) -> None:
    if not run.transport.resolved.sidecar:
        return
    if supervisor is None or run.store is None:
        raise RuntimeError("sidecar admission evidence owner is unavailable")
    append_sidecar_evidence(
        run.store,
        supervisor.topology_evidence(
            run_id=run.run_id,
            attempt_number=attempt_number,
            profile_fingerprint=fingerprint,
            stage="admission",
        ),
        actor=Actor("policy", "sidecar-supervisor"),
        issue_num=run.issue_num,
        item_id=run.item_id,
        repo=run.config.project.repo or None,
    )


def _admit_attempt_route_profile(
    run: _TrackedRun, attempt_number: int, *, sidecar_supervisor: Any = None
) -> None:
    """Bind THIS attempt to its own immutable profile before it is paid for.

    Attempt 1's profile is written inside the admission transaction; every
    later attempt resolves its boundary again here, immediately before the
    child starts, because a retry re-reads the routed model, the runner flags,
    and the image — none of which attempt 1's evidence describes any more.

    The closing ``require`` is the fail-closed gate: an attempt with no profile
    of its own does not launch. It stands down only for runners with no
    capability manifest (test doubles), which never had a profile to require.
    """
    store = run.store
    if store is None or not hasattr(store, "require_route_profile"):
        return
    if attempt_number > 1 or run.transport.resolved.sidecar:
        profile = _attempt_route_profile(run, attempt_number)
        if profile is not None:
            identity = store.get_admission_identity(run.run_id)
            if identity is not None and hasattr(store, "record_attempt_admission"):
                # Identity and profile land in ONE transaction before the
                # retry's provider call, so a crash window can never leave a
                # profiled paid attempt with no attempt identity (#1550 F4).
                store.record_attempt_admission(
                    profile, replace(identity, attempt_number=attempt_number)
                )
            else:
                store.record_route_profile(profile)
    if _profilable_runner(_tracked_runner_name(run)):
        record_credential_attempt(store, run.run_id, run.credential, attempt_number)
        stored_profile = store.require_route_profile(run.run_id, attempt_number)
        _record_sidecar_admission_evidence(
            run,
            attempt_number=attempt_number,
            fingerprint=stored_profile.fingerprint,
            supervisor=sidecar_supervisor,
        )
        require_publication_launch_credentials(
            store,
            run_id=run.run_id,
            run_kind=run.run_kind,
            attempt_number=attempt_number,
        )


def _run_transaction_gates(
    budget_gate: Callable[[], None] | None,
    attempt_gate: Callable[[], None] | None,
) -> None:
    """Evaluate both gates before the admission transaction commits."""
    if budget_gate is not None:
        budget_gate()
    if attempt_gate is not None:
        attempt_gate()


def _admission_budget_gate(
    run: _TrackedRun,
    caller_gate: Callable[[], None] | None,
    *,
    force_budget: bool,
    ceiling_usd: float,
) -> Callable[[], None] | None:
    """The budget check the admission transaction runs for *run* (#3205).

    The caller's own gate when it has one (dispatch's ``check_budget``, ci-fix's
    window gate): each already binds the daily and weekly caps. Otherwise the
    window check alone, so no run kind reaches a paid launch past a cap.
    """
    if caller_gate is not None:
        return caller_gate
    if force_budget or run.store is None:
        return None
    return window_admission_gate(
        run.store, run.config.budget, ceiling_usd, run_id=run.run_id
    )


def _save_pending_run(
    run: _TrackedRun,
    *,
    worker_cap: int,
    cap_exclude_issue: int | None,
    admit_budget_check: Callable[[], None] | None,
    on_admitting: Callable[[], None] | None = None,
) -> None:
    """Write the initial ``pending`` RunRecord, atomically claiming a worker slot.

    The TUI needs ``log_file`` set while the run is still "pending" so it can
    locate the companion ``.events.jsonl`` file, so the row is written up front.
    #745: with ``worker_cap`` > 0 the pending row IS the worker slot — count +
    insert run in one transaction, raising ``WorkerCapReached`` (no row
    written) if the fleet is already at the cap so the caller requeues.
    A disabled cap (``worker_cap`` == 0) permits unconditional admission.

    The dispatch's own PGID anchors liveness before any attempt starts.
    Retry callbacks record separate attempt groups and preserve this owner.

    I01: the row is written through ``admit_run_with_identity``, the same
    normalized primitive the contained path uses, so the run's immutable
    admission identity (tracker, opaque item id, delivery repo, epic node)
    lands in the same transaction as the run row. A run can therefore never
    exist without the identity that says what work it was admitted for.
    """
    if run.store is None:
        return

    from kew.process_groups import exclusive_owner_pid, identity_at_admission
    from kew.run_store import RunRecord

    owner_pid = exclusive_owner_pid()
    record_current_policy(run.store)  # retain preimage before the row refs it
    pending_row = RunRecord(
        run_id=run.run_id,
        issue_num=run.issue_num,
        item_id=run.item_id,
        branch=run.branch,
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        pgid=owner_pid,
        owner_pid=owner_pid,
        **identity_at_admission(owner_pid, require_token=run.contained_image is None),
        runner_name=_tracked_runner_name(run),
        model=run.effective_model,
        log_file=run.log_file,
        title=run.title,
        complexity=run.complexity,
        run_kind=run.run_kind,
        actor_type=run.actor.type,
        actor_id=run.actor.id,
        repo=run.repo,
        endpoint=run.endpoint,
        policy_hash=current_policy_hash(),
        requested_service_tier=(
            _requested_service_tier(run.config, _tracked_runner_name(run)) or None
        ),
        prefix_key=run.prefix_key,
        provider=run.provider,
        map_tokens=run.map_tokens,
        lesson_corpus_tokens=run.lesson_corpus_tokens,
        self_check=run.self_check,
        prompt_profile_version_id=run.prompt_profile_version_id,
        transport_mode=run.transport.resolved.mode,
        transport_endpoint=run.transport.resolved.endpoint,
        transport_route=run.transport.resolved.route,
        transport_managed=run.transport.resolved.managed,
        transport_expected_version=run.transport.resolved.expected_version or None,
        transport_observed_version=run.transport.observed_version,
        transport_config_sha256=run.transport.resolved.config_sha256,
        transport_preflight_verdict=run.transport.preflight_verdict,
    )
    # The dispatcher identity commits with admission, before any claim hook.
    # A killed hook owner must remain probeable even before an agent exists.
    # I03C: attempt 1's realized launch boundary joins the SAME transaction
    # through the identity module's bounded enricher hook, so a run can no
    # more exist without the profile of what it launched than without the
    # identity of what it was launched for.
    profile = None if run.transport.resolved.sidecar else _attempt_route_profile(run, 1)
    enrich = (
        run.store.route_profile_enricher(profile)
        if profile is not None and hasattr(run.store, "route_profile_enricher")
        else None
    )
    # Fleet concurrency is keyed on item_id (A3): exclude this run's own item
    # so it never counts itself. exclude_item wins over the legacy int
    # exclude_issue, which collapses every Linear row onto the 0 sentinel —
    # matching the sandbox path (`_record_sandbox_start`). A run started under
    # a detached review stage's held reservation does not count the stage row
    # that holds it, or under a small cap the stage could never start its own
    # remediation (#2459, #2628). Local import: graph.driver imports this module.
    from kew.graph.driver.review_detach import held_reservation

    admitted = run.store.admit_run_with_identity(
        pending_row,
        run.store.build_admission_identity(
            run.run_id,
            tracker=run.source_tracker,
            item_id=_admission_item_id(run),
            repo=run.repo,
        ),
        cap=worker_cap,
        exclude_issue=cap_exclude_issue,
        exclude_item=run.item_id,
        exclude_run=held_reservation(),
        on_admit=partial(_run_transaction_gates, admit_budget_check, on_admitting),
        enrich=enrich,
        reservation=run.reservation,
        reservation_pinned=run.reservation_pinned,
    )
    if not admitted:
        raise WorkerCapReached(run.issue_num)


class _BreakerBucket(Protocol):
    """What the breaker baseline reads off a run: its route and its store."""

    @property
    def config(self) -> DispatchConfig: ...
    @property
    def store(self) -> RunStore | None: ...
    @property
    def effective_model(self) -> str: ...
    @property
    def complexity(self) -> str: ...
    @property
    def run_kind(self) -> str: ...

    #: Where the computed baseline is kept for the breaker's audit event.
    breaker_baseline: RunBaseline | None


@dataclass
class _ContainedBreakerBucket:
    """The breaker bucket of a contained dispatch, which has no ``_TrackedRun``."""

    config: DispatchConfig
    store: RunStore | None
    effective_model: str
    complexity: str
    run_kind: str
    breaker_baseline: RunBaseline | None = None


def _breaker_reference_cost(run: _BreakerBucket) -> float | None:
    """What the rest of this route costs, for a bucket too fresh to judge itself.

    The same baseline read with the run-kind predicate *inverted*: the median
    over every kind the (model, complexity) route has run except this one. It
    answers "has this bucket been observed to cost at least what the route's
    other work costs" — a gate on arming, never a threshold (#1116).

    Excluding the run's own kind is what makes it evidence. Read across all
    kinds it would be a superset of the bucket, and on a route whose only kind
    is this one it would equal the bucket median exactly, so the gate would
    pass for free and arm buckets that were dormant before the rule existed
    (#1138 review). A route with no other kinds therefore has no reference, and
    the bucket stays dormant.

    Deliberately *not* ``estimate_dispatch_cost``: that population carries none
    of the baseline's exclusions, so sub-$0.01 no-op rows (#668) and killed runs
    count toward it. A history polluted with those yields a reference of cents,
    which any bucket clears — a gate that never says no. Sharing the baseline's
    own query also keeps this a one-column filtered scan instead of
    materializing every ``runs`` row into a record on the dispatch hot path.
    """
    if run.store is None:  # pragma: no cover -- caller gates on a live store
        return None
    med, _count = run.store.median_cost_for_bucket(
        run.effective_model, run.complexity, run.run_kind, other_kinds=True
    )
    return med


def _log_breaker_baseline(baseline: RunBaseline) -> None:
    """Record the armed threshold and which term produced it (#1116).

    Drills need to verify that the breaker is armed at a sane value without
    having to trip it, so the winning term is named explicitly.
    """
    from kew.breaker import baseline_basis

    if baseline.anomaly_threshold_usd is None:
        _logger.info(
            "Circuit-breaker cost-anomaly rule dormant for %s/%s/%s (%d samples)",
            baseline.model,
            baseline.complexity,
            baseline.run_kind,
            baseline.sample_count,
        )
        return
    _logger.info(
        "Circuit-breaker cost-anomaly threshold $%.2f for %s/%s/%s via %s (%s)",
        baseline.anomaly_threshold_usd,
        baseline.model,
        baseline.complexity,
        baseline.run_kind,
        baseline.threshold_source,
        baseline_basis(baseline),
    )


def _compute_breaker_baseline(run: _BreakerBucket) -> tuple[float | None, float]:
    """Compute the circuit-breaker cost-anomaly baseline once, before the loop.

    Returns ``(anomaly_threshold_usd, anomaly_compaction_allowance_usd)``.
    ``effective_model`` is fixed across attempts so the bucket is consistent.
    The compaction allowance (#859) is discounted before the anomaly rule fires
    and keys to the run-kind baseline median — which is what every armed
    threshold is proportionate to, on the under-sampled path as much as the
    established one, so the discount and the bar move together. A missing or
    failed baseline leaves the anomaly rule dormant (threshold ``None``).
    """
    anomaly_threshold_usd: float | None = None
    anomaly_compaction_allowance_usd: float = 0.0
    if run.config.breaker.enabled and run.store is not None:
        try:
            from kew.breaker import run_baseline

            _baseline = run_baseline(
                run.store,
                run.effective_model,
                run.complexity,
                run.run_kind,
                factor=run.config.breaker.cost_anomaly_factor,
                min_samples=run.config.breaker.min_samples,
                min_anomaly_threshold_usd=run.config.breaker.min_anomaly_threshold_usd,
                floor_multiple=run.config.breaker.anomaly_floor_multiple,
                # Passed as a thunk: it costs a second store read that every
                # path except an under-sampled-but-sampled bucket discards.
                reference_cost_usd=lambda: _breaker_reference_cost(run),
            )
            _log_breaker_baseline(_baseline)
            run.breaker_baseline = _baseline
            anomaly_threshold_usd = _baseline.anomaly_threshold_usd
            if _baseline.median_usd is not None:
                anomaly_compaction_allowance_usd = (
                    _baseline.median_usd
                    * run.config.breaker.compaction_allowance_factor
                )
        except Exception as exc:
            _logger.warning(
                "Circuit-breaker baseline unavailable, anomaly rule dormant: %s",
                redact_credentials(exception_text(exc)),
            )
            anomaly_threshold_usd = None
    return anomaly_threshold_usd, anomaly_compaction_allowance_usd


def _check_existing_pr(
    run: _TrackedRun,
    attempt: int,
    prev_result: DispatchResult | None,
    result: DispatchResult | None,
    store: RunStore | None,
) -> tuple[bool, DispatchResult | None]:
    """Retry-time duplicate-PR guard. If a PR already exists for this run's
    branch (an earlier attempt opened it), persist its identity (#648), label it
    pending-review, and signal the loop to stop. Returns ``(hit, result)`` — on
    a hit ``result`` is the effective result to finalize (the prior attempt's);
    otherwise ``(False, result)`` to proceed with the retry.

    Runs whose subject IS an open PR (a review, its remediation) skip the
    lookup: a PR on their branch is the work they were launched against, not
    evidence that an earlier attempt of this run opened one.
    """
    from kew.run_store import PR_SUBJECT_RUN_KINDS

    if run.run_kind in PR_SUBJECT_RUN_KINDS:
        return False, result
    config = run.config
    # On retries, check if a PR was already created (avoid duplicates)
    try:
        existing_pr = (
            check_pr_exists(run.branch, config) if attempt > 0 and run.branch else None
        )
    except Exception:
        existing_pr = None  # Lookup failed -- proceed with retry
    if not existing_pr:
        return False, result
    _success(f"PR already exists for #{run.issue_num} — skipping retry")
    # Persist the discovered PR identity at guard time (#648): the agent opened
    # this PR on an earlier attempt, so capture it now rather than waiting for a
    # later sync to backfill it.
    if config.project.repo:
        _persist_pr_url(
            store,
            run.run_id,
            f"https://{repo_host(project_root())}/{config.project.repo}/pull/"
            f"{existing_pr}",
        )
    update_pr_labels(existing_pr, config, add=[config.labels.pr_pending_review])
    return True, (prev_result if prev_result is not None else result)


def _check_operator_kill(
    result: DispatchResult, run: _TrackedRun, store: RunStore | None
) -> bool:
    """Honor operator stop intent without releasing pending termination ownership."""
    if store is None:
        return False
    stored_now = store.get(run.run_id)
    if _fleet_or_run_stopped(store, stored_now):
        _warn(f"Run for #{run.issue_num} was killed by `kew stop` — not retrying")
        result.error_category = "killed"
        return True
    return False


def _check_cost_guard(
    result: DispatchResult,
    run: _TrackedRun,
    budget_cap_usd: float | None,
    pricing_overrides: dict[str, tuple[float, float]] | None,
) -> bool:
    """Cost-aware budget guard (#642): before retrying ANY failure, compare
    accumulated cost against the effective runtime cap. A --max-budget-usd kill
    can escape subtype/exit classification (plain exit 1, or SIGTERM-143 read as
    watchdog_kill), and a retry would just burn the same cap again. Routes to
    the budget_stopped handling (worktree preserved, no auto-resume). Returns
    True when the cap is hit (mutating ``result.error_category``)."""
    if not budget_cap_usd:
        return False
    cost = _accumulated_cost_usd(
        result,
        _cost_model_for_result(run, result),
        pricing_overrides,
        classifier_model=result.classifier_overhead_model or None,
        classifier_input_tokens=result.classifier_overhead_input_tokens,
        classifier_output_tokens=result.classifier_overhead_output_tokens,
    )
    if cost < budget_cap_usd * _BUDGET_GUARD_FRACTION:
        return False
    _warn(
        f"Cost ${result.estimated_cost_usd:.2f} reached the budget "
        f"cap (${budget_cap_usd:.2f}) — not retrying (worktree preserved)"
    )
    result.error_category = "budget_stopped"
    return True


def _attempt_stops_without_retry(result: DispatchResult, run: _TrackedRun) -> bool:
    """A settled CI startup refusal releases its slot and ends this repair."""
    return result.paused or (run.run_kind == "ci_fix" and startup_refused(result))


def _classify_attempt(
    result: DispatchResult,
    run: _TrackedRun,
    store: RunStore | None,
    budget_cap_usd: float | None,
    pricing_overrides: dict[str, tuple[float, float]] | None,
) -> bool:
    """Classify a completed attempt's terminal status. Returns True when the
    retry loop must stop (mutating ``result.error_category`` for the budget /
    kill terminal states), False to allow another attempt."""
    # Operator pause: a cooperative pause stops the loop here. The agent was
    # SIGTERM'd (nonzero exit), but it must NOT be relaunched by the retry loop.
    if _attempt_stops_without_retry(result, run):
        return True
    # Budget watchdog (sustained over cap, debounced): terminal for this
    # dispatch. The live watchdog gave the runner a wrap-up grace to commit WIP
    # before SIGKILL, so mark this a graceful "budget_stop" — a distinct,
    # non-destructive, non-retriable terminal status whose outcome path commits
    # any leftover WIP to the branch (#859). Does NOT auto-resume (#401).
    if result.budget_exceeded:
        result.error_category = "budget_stop"
        return True
    # Circuit breaker (cost anomaly): also terminal — no retry.
    if result.breaker_rule:
        return True
    # Checkout quotas are terminal because a retry re-enters the same tree.
    # A confirmed OOM already reads as ``resource`` (#3191): it earns the one
    # retry ``_resource_retry_spent`` allows, so the cap's termination below
    # applies only to an OOM that was not classified (an intentional stop).
    # Host free-space and transcript state can change during retry backoff.
    if _unclassified_resource_cap(result):
        return _resource_cap_stops_retry(result)
    if _check_operator_kill(result, run, store):
        return True
    if result.exit_code == 0:
        return True
    if _check_cost_guard(result, run, budget_cap_usd, pricing_overrides):
        return True
    # The provider refused for quota: a retry hits the same wall (#2643).
    if result.error_category == QUOTA_EXHAUSTED_CATEGORY:
        _warn("Runner quota exhausted -- not retrying")
        return True
    # Permanent error -- no retry
    if result.error_category == "permanent":
        _warn("Permanent failure detected -- not retrying")
        return True
    return False


def _unclassified_resource_cap(result: DispatchResult) -> bool:
    """A host cap stopped the run, and it did not read as ``resource``."""
    return (
        result.hard_stop == "resource_cap"
        and result.error_category != RESOURCE_CATEGORY
    )


def _resource_cap_stops_retry(result: DispatchResult) -> bool:
    """Apply retry policy using stable cap codes, never operator text."""
    terminal = {
        ResourceCapCode.CHECKOUT_BYTES.value,
        ResourceCapCode.CHECKOUT_ENTRIES.value,
        ResourceCapCode.OOM.value,
    }
    retriable = {
        ResourceCapCode.HOST_FREE_SPACE.value,
        ResourceCapCode.TRANSCRIPT.value,
    }
    if result.resource_cap_code in retriable:
        _warn(
            redact_credentials(
                f"Host resource cap may clear -- retrying: {result.resource_cap_reason}"
            )
        )
        return False
    if result.resource_cap_code not in terminal:
        _warn(
            "Host resource cap has an unknown code -- not retrying: "
            f"{result.resource_cap_code or 'missing'}"
        )
        return True
    _warn(
        redact_credentials(
            f"Host resource cap hit -- not retrying: {result.resource_cap_reason}"
        )
    )
    return True


def _validate_session_result(
    run: _TrackedRun, result: DispatchResult, attempt_number: int
) -> None:
    """Reject invalid handles and retain only a fixed audit reason."""
    from kew.session_ids import capture_session_id

    if run.runner is None:
        if result.session_id is not None:
            result.session_id_rejection = "runner unavailable for session validation"
        result.session_id = None
    else:
        capture_session_id(result, result.session_id, run.runner.name)
    if result.session_id_rejection:
        audit.emit(
            run.store,
            "session_id_rejected",
            actor=run.actor,
            run_id=run.run_id,
            issue_num=run.issue_num,
            item_id=run.item_id,
            repo=run.repo,
            payload={
                "reason": result.session_id_rejection,
                "warning": result.session_id_warning,
                "attempt_number": attempt_number,
                "present": False,
            },
            dedup_key=f"session-id-rejected:{run.run_id}:{attempt_number}",
        )


def _resume_or_fresh(
    attempt: int, result: DispatchResult, run: _TrackedRun
) -> tuple[str | None, str]:
    """Decide the next attempt's strategy: resume the prior session when the
    runner supports it and we are within the resume budget, else start fresh."""
    _validate_session_result(run, result, attempt + 1)
    if (
        attempt < run.config.retry.max_resume_attempts
        and result.session_id
        and run.runner.supports_resume()
    ):
        return result.session_id, "resume"
    return None, "fresh"


class _AdmissionHookRefused(RuntimeError):
    """A post-admission hook failed before any runner could start."""


def _classify_retry_exception(
    exc: Exception,
) -> tuple[str, RunStatus, CredentialPublicationRefused | None]:
    if isinstance(exc, _AdmissionHookRefused):
        _error(f"Post-admission launch refused: {exc}")
        return "admission_hook_refused", "refused", None
    if isinstance(exc, CredentialLifetimeRefused):
        _error(f"Credential launch refused: {exc}")
        return "credential_lifetime_refused", "refused", None
    if isinstance(exc, _OperatorStopRequested):
        _warn(str(exc))
        return "killed", "killed", None
    if isinstance(exc, _CheckoutIdentityRefused):
        _error(redact_credentials(f"Checkout launch refused: {exception_text(exc)}"))
        return "checkout_identity_changed", "refused", None
    if isinstance(exc, CredentialPublicationRefused):
        _error(redact_credentials(f"Contained launch refused: {exception_text(exc)}"))
        return CREDENTIAL_PUBLICATION_REFUSAL, "refused", exc
    if isinstance(exc, ContainerStartFailed):
        _error(redact_credentials(exception_text(exc)))
        return exc.category, "failure", None
    _error(
        redact_credentials(
            f"Unexpected exception in run_tracked_agent: {exception_text(exc)}"
        )
    )
    return "exception", "failure", None


def _emit_credential_launch_refusal(
    run: _TrackedRun, refusal: CredentialPublicationRefused | None
) -> None:
    if refusal is None:
        return
    audit.emit(
        run.store,
        CREDENTIAL_PUBLICATION_REFUSAL,
        actor=Actor("policy", "credential-publication"),
        run_id=run.run_id,
        issue_num=run.issue_num,
        item_id=run.item_id,
        repo=run.repo,
        payload={"reason": refusal.reason, "publication_allowed": False},
    )


def _exception_result(
    run: _TrackedRun,
    total_attempts: int,
    completed_result: DispatchResult | None,
) -> DispatchResult:
    """Retain launch evidence even when the runner returned no result."""
    result = completed_result or DispatchResult(
        exit_code=1,
        issue_num=run.issue_num,
        log_file=run.log_file,
    )
    result.launched = merge_launch_evidence(
        result.launched, getattr(run, "launched", False)
    )
    launch_state = getattr(run, "launch_state", None)
    if launch_state is not None:
        LaunchState.observe(launch_state, result)
    result.run_id = run.run_id
    result.attempt_number = max(total_attempts, 1)
    _validate_session_result(run, result, result.attempt_number)
    if completed_result is not None:
        _finalize_cost(run, result)
    return result


def _audit_result_accounting(result: DispatchResult) -> tuple[float, int]:
    """Return the bounded accounting values used by terminal audit rows."""
    return round(result.estimated_cost_usd or 0.0, 6), result.total_tokens or 0


def _retry_exception_exit_code(exc: Exception) -> int:
    """Identity refusals use the admission failure exit code at every boundary."""
    return 2 if isinstance(exc, _CheckoutIdentityRefused) else 1


def _retain_exception_reason(
    result: DispatchResult, exc: Exception, category: str
) -> str:
    """Keep missing telemetry distinct from an execution or admission error."""
    result.stop_reason = result.stop_reason or str(exc)
    if result.error_category == "sandbox_startup_unknown":
        return result.error_category
    return category


def _persist_failed_launch_receipt(run: _TrackedRun) -> None:
    """A failure before cleanup ownership proves that no primitive started."""
    admission = run.admission
    if admission is None:
        return
    if not admission.cleanup_owned and admission.launch_cleanup is None:
        admission.record_startup(refused=True)
    admission.persist_receipt()


def _retry_exception_outcome(
    run: _TrackedRun,
    exc: Exception,
    total_attempts: int,
    completed_result: DispatchResult | None = None,
) -> _RetryOutcome:
    """Terminal handling for a mid-loop exception: mark the pending row failed
    (unless operator-killed), emit the terminal ``dispatch_completed`` audit
    row, and return an ``early_return`` result the caller finalizes as-is —
    identical to the original loop ``except`` block."""
    _persist_failed_launch_receipt(run)
    store = run.store
    require_claim_recovery(store, run.run_id)
    exc_category, failure_status, credential_refusal = _classify_retry_exception(exc)
    exc_exit_code = _retry_exception_exit_code(exc)
    failed_result = _exception_result(run, total_attempts, completed_result)
    exc_category = _retain_exception_reason(failed_result, exc, exc_category)
    # Mark the run as failed so it doesn't stay "pending" forever
    if store is not None:
        stored = store.get(run.run_id)
        if stored is not None:
            # Preserve operator stop intent until termination is confirmed.
            if _stop_requested(stored):
                exc_exit_code = stored.exit_code or 1
                exc_category = "killed"
            else:
                stored.status = failure_status
                stored.exit_code = exc_exit_code
                stored.finished_at = datetime.now(UTC)
                stored.duration_seconds = time.monotonic() - run.dispatch_start
                stored.error_category = exc_category
            if stored.status != "killed" or completed_result is not None:
                stored.attempt_number = max(total_attempts, 1)
                _copy_result_accounting(stored, failed_result)
                stored.session_id = failed_result.session_id
                stored.log_file = failed_result.log_file
                _save_attempt_record(store, stored)
    failed_result.exit_code = exc_exit_code
    failed_result.error_category = exc_category
    _emit_credential_launch_refusal(run, credential_refusal)
    # Only a failure before any completed attempt has zero accounting.
    # Cleanup and later startup failures retain accumulated measured usage.
    audit_cost_usd, audit_tokens = _audit_result_accounting(failed_result)
    audit.emit(
        store,
        "dispatch_completed",
        actor=run.actor,
        run_id=run.run_id,
        issue_num=run.issue_num,
        item_id=run.item_id,
        endpoint=run.endpoint,
        repo=run.repo,
        payload={
            **audit.transport_evidence(
                mode=run.transport.resolved.mode,
                endpoint=run.transport.resolved.endpoint,
                route=run.transport.resolved.route,
                managed=run.transport.resolved.managed,
                expected_version=run.transport.resolved.expected_version or None,
                observed_version=run.transport.observed_version,
                config_sha256=run.transport.resolved.config_sha256,
                preflight_verdict=run.transport.preflight_verdict,
                binary_sha256=run.transport.resolved.binary_sha256,
                credential_env_names=run.transport.resolved.credential_env_names,
            ),
            **_orchestrator_provenance(),
            "complexity": run.complexity,
            "runner": str(getattr(run.runner, "name", "")) if run.runner else "",
            "model": str(run.effective_model) if run.effective_model else "",
            # The run died before any model could be reported (#251); None
            # keeps the dispatch_completed payload schema uniform so
            # consumers never KeyError on this key.
            "reported_model": None,
            "exit_code": exc_exit_code,
            "duration_seconds": round(time.monotonic() - run.dispatch_start, 2),
            "cost_usd": audit_cost_usd,
            "tokens": audit_tokens,
            "attempt_number": total_attempts,
            "error_category": exc_category,
            "reason": failed_result.stop_reason,
            "loop_id": run.loop_id,
        },
    )
    return _RetryOutcome(early_return=failed_result)


def _retry_exception_or_refusal(
    run: _TrackedRun,
    exc: Exception,
    total_attempts: int,
    completed_result: DispatchResult | None = None,
) -> _RetryOutcome:
    """Finalize every retry exception, then preserve identity refusals."""
    if isinstance(exc, HostIdentityUnavailable):
        return _RetryOutcome(
            early_return=_host_identity_refusal(
                run, exc, total_attempts, completed_result
            )
        )
    outcome = _retry_exception_outcome(run, exc, total_attempts, completed_result)
    if isinstance(exc, _CheckoutIdentityRefused):
        raise exc
    return outcome


def _host_identity_refusal(
    run: _TrackedRun,
    exc: HostIdentityUnavailable,
    total_attempts: int,
    completed_result: DispatchResult | None = None,
) -> DispatchResult:
    """Finalize a denied host launch after its waiting child has been reaped."""
    require_claim_recovery(run.store, run.run_id)
    _error(str(exc))
    result = _exception_result(run, total_attempts, completed_result)
    result.exit_code = 2
    result.error_category = exc.reason
    if completed_result is None:
        result.accounting_status = "not-run"
    store = run.store
    if store is not None:
        stored = store.get(run.run_id)
        if stored is not None:
            stored.status = "refused"
            stored.exit_code = 2
            stored.error_category = exc.reason
            stored.finished_at = datetime.now(UTC)
            stored.attempt_number = max(total_attempts, 1)
            _copy_result_accounting(stored, result)
            store.save(stored)
        store.release_epic_node_item(
            run.item_id or str(run.issue_num), run_id=run.run_id
        )
    _emit_completion_audit(run, result, 2, total_attempts, None)
    return result


def _record_retry_pgid(store: RunStore, run_id: str, pid: int) -> Callable[[], None]:
    """Register, verify, then refuse release when a stop raced the spawn."""
    from kew.process_groups import group_alive

    previous = store.get(run_id)
    if (
        previous is not None
        and previous.attempt_pgid is not None
        and previous.attempt_pgid != pid
        and group_alive(previous.attempt_pgid) is not False
    ):
        # Preserve ownership through both completion and exception handling.
        # The new child still waits behind the gate and cannot execute work.
        store.request_run_stop(run_id)
        raise RuntimeError(
            f"Previous attempt group {previous.attempt_pgid} is not proven empty "
            f"for {run_id}; retaining ownership and refusing retry registration"
        )
    store.set_attempt_pgid(run_id, pid)
    _check_spawn_permit(store, run_id)
    stored = store.get(run_id)
    if stored is None or stored.attempt_pgid != pid or not stored.attempt_start_time:
        raise RuntimeError(f"Attempt registration was not confirmed for {run_id}")
    return partial(_check_spawn_permit, store, run_id)


def _check_spawn_permit(store: RunStore, run_id: str) -> None:
    """Re-read durable stop intent immediately before the permit write."""
    stored = store.get(run_id)
    if (
        stored is None
        or stored.status != "pending"
        or _fleet_or_run_stopped(store, stored)
    ):
        raise RuntimeError(f"Fleet stop requested during spawn for {run_id}")
    if stored.container_name:
        _check_container_spawn_identity(stored)
        return
    _check_host_spawn_identity(stored)


def _check_container_spawn_identity(stored: RunRecord) -> None:
    """Require the durable identity a stop needs, in this runtime's spelling.

    On a docker-compatible runtime the id column IS that identity: it is the
    only handle ``stop_recovery`` can inspect or kill with, so a row that has a
    container name and no id means the cidfile read-back never landed and the
    launch is refused. A gateway sandbox has no such id to hold —
    ``openshell_launch._record_identity`` records the NAME and leaves the
    column empty on purpose, because ``sbx_01HN4Q8ZJ2WME5T`` is not a 64-hex
    container identity — and the name is what the gateway's delete answers to.

    Asking every runtime for the id therefore refused every store-backed
    OpenShell launch at its own spawn registration: ``_announce_spawn``
    swallowed the raise, its ``killpg`` could not reach the client, and the
    launch blocked in ``proc.wait()`` for the agent's whole lifetime instead of
    returning a streaming handle (PR #2401 review). Only the gateway runtime is
    exempt; an unrecognized one still has to produce an id it cannot forge.
    """
    from kew.runtime_names import OPENSHELL_RUNTIME
    from kew.stop_recovery import valid_container_id

    if stored.container_runtime == OPENSHELL_RUNTIME:
        return
    if not valid_container_id(stored.container_id or ""):
        raise RuntimeError("container identity unavailable at client registration")


def _check_host_spawn_identity(stored: RunRecord) -> None:
    """A durable ancestry flag cannot replace either current kernel token."""
    from kew.process_groups import owner_alive, start_token_matches

    if (
        not start_token_matches(stored.owner_pid or 0, stored.owner_start_token)
        or owner_alive(
            stored.pgid or 0,
            stored.owner_pid,
            stored.owner_start_time,
            stored.owner_ppid,
            stored.owner_command,
            stored.owner_start_token,
        )
        is not True
    ):
        raise HostIdentityUnavailable("owner-identity-unavailable")
    if not stored.attempt_owner_verified or not start_token_matches(
        stored.attempt_pgid or 0, stored.attempt_start_token
    ):
        raise HostIdentityUnavailable("attempt-identity-unavailable")


def _fleet_or_run_stopped(store: RunStore, record: RunRecord | None) -> bool:
    """Turn a fleet latch into durable run intent before any further launch."""
    if record is not None and _stop_requested(record):
        return True
    if isinstance(store.get_fleet_stopped(), dict):
        if record is not None:
            store.request_run_stop(record.run_id)
        return True
    return False


class _OperatorStopRequested(RuntimeError):
    """A durable operator stop refused another attempt."""


def _refuse_stopped_attempt(run: _TrackedRun) -> None:
    """Check both stop sources before preparation and immediately before launch."""
    if run.store is not None and _fleet_or_run_stopped(
        run.store, run.store.get(run.run_id)
    ):
        raise _OperatorStopRequested(
            f"Fleet stop requested before attempt for {run.run_id}"
        )


def _stop_requested(record: RunRecord) -> bool:
    """Recognize stop intent while termination still owns a pending row."""
    return record.status == "killed" or record.error_category == "killed"


def _permit_retry_spawn(run: _TrackedRun, permit: Callable[[], None] | None) -> None:
    """Verify the final launch permit before transferring claim ownership."""
    if permit is not None:
        permit()
    if run.on_launch is not None:
        run.on_launch()


def _record_retry_spawn(run: _TrackedRun, pid: int) -> Callable[[], None] | None:
    """Register the waiting attempt, record provisional launch evidence for a
    host process, and return its claim-aware launch permit (None when neither a
    store nor a claim owner has anything to verify)."""
    permit = (
        _record_retry_pgid(run.store, run.run_id, pid)
        if run.store is not None
        else None
    )
    if run.contained_image is None:
        run.launched = True
        if run.launch_state is not None:
            run.launch_state.launched = True
    if permit is None and run.on_launch is None:
        return None
    return partial(_permit_retry_spawn, run, permit)


def _save_attempt_record(store: RunStore, record: RunRecord) -> None:
    """Persist a run row and its latest immutable attempt identity atomically."""
    from kew.run_store import AdmissionIdentity

    identity = store.get_admission_identity(record.run_id)
    if not isinstance(identity, AdmissionIdentity):
        # Compatibility for identity-less stores and legacy test doubles.
        store.save(record)
        return
    store.admit_run_with_identity(
        record,
        replace(identity, attempt_number=record.attempt_number),
    )


def _record_retry_managed_transport(
    run: _TrackedRun,
    store: RunStore | None,
    observed_version: str,
    config_sha256: str,
) -> None:
    """Persist and apply the managed Switchyard admission observed on retry."""
    if store is not None and not store.record_managed_transport_admission(
        run.run_id,
        observed_version=observed_version,
        config_sha256=config_sha256,
    ):
        raise RuntimeError("managed Switchyard admission state changed")
    run.transport = AdmittedInferenceTransport(
        resolved=replace(
            run.transport.resolved,
            config_sha256=config_sha256 or run.transport.resolved.config_sha256,
        ),
        observed_version=observed_version or None,
        preflight_verdict="ready",
    )


def budget_watchdog(
    cap_usd: float | None, *, runner_name: str, contained: bool
) -> Callable[[float], bool] | None:
    """A kew-side cost watchdog for this run, or None when the agent self-caps.

    claude-code receives ``--max-budget-usd`` in its argv and stops itself, so
    on the host kew has deliberately not double-enforced for it. A CONTAINED
    run carries the flag too (#1462), but kew still watches it: the stop must
    reach the container by name, and the cost kew records at the trip is the
    one its own budgets account. Containing the remediation flows without this
    would have silently removed ``max_remediation_usd`` — the cap #993 exists
    to provide.

    The returned predicate feeds ``run_tracked_agent(budget_check=...)``: kew
    polls accumulated cost and stops the agent gracefully so it can commit work
    in progress (#859). On a contained run that stop reaches the container by
    name rather than killing the local runtime client (#1462).
    """
    if runner_name == "claude-code" and not contained:
        return None
    if not cap_usd or cap_usd <= 0:
        return None
    return lambda cost: cost > cap_usd


def _contained_execution_for(
    run: _TrackedRun,
    worktree_dir: Path,
    *,
    sidecar_supervisor: Any = None,
    budget_override_usd: float | None = None,
) -> ContainedExecution | None:
    """The container this attempt runs in, or None for host execution.

    Rebuilt per attempt rather than once per run: the runner's flags and the
    routed model are read at launch, so a retry that resolved differently must
    not reuse the first attempt's argv.

    The already-ADMITTED transport is passed down instead of being re-resolved,
    which would throw away the admission ``run_tracked_agent`` performed.

    A runtime ``budget_override_usd`` (``max_remediation_usd``, #993) is the
    cap the container's own argv carries, as it is the host argv's (#1462).
    """
    if run.contained_image is None:
        return None
    from kew.dispatch_sandbox import build_agent_container_argv, config_with_run_cap

    config = config_with_run_cap(run.config, budget_override_usd)

    if run.resolved_image_id is None:
        # An unprofiled path (allow_unrecorded test runs) still may not launch
        # by tag: resolve here or refuse — never silently fall back.
        run.resolved_image_id = _require_contained_image_id(
            run, refusal="refusing rather than launching by tag"
        )
    if run.transport.resolved.sidecar:
        if sidecar_supervisor is None:
            raise RuntimeError("sidecar worker requires one attempt owner")
        from kew.dispatch_sandbox import prepare_sidecar_worker

        return prepare_sidecar_worker(
            config,
            supervisor=sidecar_supervisor,
            image_id=run.resolved_image_id,
            runner=run.runner.name,
            checkout_dir=worktree_dir,
            slug=branch_slug(run.item_id or str(run.issue_num)),
            model=run.effective_model,
            inference_transport=run.transport.resolved,
            store=run.store,
            run_id=run.run_id,
        )
    argv = build_agent_container_argv(
        config,
        # The captured immutable ID, never the mutable tag: what the profile
        # claims and what launches must be the same bytes (#1550 review F1).
        image=run.resolved_image_id,
        runner=run.runner.name,
        checkout_dir=worktree_dir,
        slug=branch_slug(run.item_id or str(run.issue_num)),
        model=run.effective_model,
        inference_transport=run.transport.resolved,
    )
    from kew.dispatch_sandbox import prepare_registered_container

    return prepare_registered_container(
        argv, run.store, run.run_id, credential=run.credential
    )


def _record_terminal_sidecar_evidence(
    run: _TrackedRun, supervisor: Any, attempt_number: int
) -> None:
    store = run.store
    if store is None or not hasattr(store, "get_route_profile"):
        return
    profile = store.get_route_profile(run.run_id, attempt_number)
    if profile is None:
        return
    append_sidecar_evidence(
        store,
        supervisor.topology_evidence(
            run_id=run.run_id,
            attempt_number=attempt_number,
            profile_fingerprint=profile.fingerprint,
            stage="terminal",
        ),
        actor=Actor("policy", "sidecar-supervisor"),
        issue_num=run.issue_num,
        item_id=run.item_id,
        repo=run.config.project.repo or None,
    )


@contextlib.contextmanager
def _sidecar_attempt_context(
    run: _TrackedRun, attempt_number: int = 1
) -> Iterator[Any]:
    """Prepare one fresh topology and bind its verified endpoint to one attempt."""
    if not run.transport.resolved.sidecar:
        yield None
        return
    if run.contained_image is None:
        raise RuntimeError("sidecar Switchyard requires a contained worker")
    run.resolved_image_id = _require_contained_image_id(
        run,
        refusal="the sidecar attempt is refused before any charge",
    )
    pending = run.transport.resolved
    supervisor = None
    try:
        with sidecar_switchyard_context(
            run.config,
            pending,
            worker_image_id=run.resolved_image_id,
        ) as supervisor:
            run.transport = AdmittedInferenceTransport(
                resolved=replace(
                    pending,
                    endpoint=supervisor.endpoint,
                    config_sha256=supervisor.config_sha256,
                ),
                observed_version=supervisor.observed_version,
                preflight_verdict="ready",
            )
            yield supervisor
    finally:
        try:
            if supervisor is not None:
                _record_terminal_sidecar_evidence(run, supervisor, attempt_number)
        finally:
            run.resolved_image_id = None


def _record_attempt_start(run: _TrackedRun, attempt_number: int) -> None:
    """Persist the retry identity before its pending posture can fail."""
    if attempt_number == 1:
        return
    store = run.store
    record = store.get(run.run_id) if store is not None else None
    if store is None or record is None:
        if not run.transport.resolved.sidecar:
            # Preserve explicitly unrecorded callers and legacy store doubles.
            return
        raise RuntimeError("sidecar attempt identity was not durably recorded")
    record.attempt_number = attempt_number
    _save_attempt_record(store, record)
    recorded = store.get(run.run_id)
    if recorded is None or recorded.attempt_number != attempt_number:
        route = "sidecar " if run.transport.resolved.sidecar else ""
        raise RuntimeError(f"{route}attempt identity was not durably recorded")


def _initial_tracked_posture_recorded(run: _TrackedRun, attempt_number: int) -> bool:
    """Reuse the caller's initial attestation, never an earlier retry's posture."""
    if attempt_number != 1:
        return False
    read = getattr(run.store, "list_audit_events", None)
    if not callable(read):
        return False
    events = read(event="sandbox_resolved", run_id=run.run_id, limit=1)
    return any(
        "posture" in event.payload and event.payload.get("attempt_number", 1) == 1
        for event in events
    )


def _attest_tracked_posture(
    run: _TrackedRun, worktree_dir: Path, *, attempt_number: int, admitted: bool = False
) -> None:
    """Record every launch; sidecars additionally attest their admission transition."""
    sidecar = run.transport.resolved.sidecar
    if sidecar and run.contained_image is None:
        return
    if not sidecar and (
        admitted or _initial_tracked_posture_recorded(run, attempt_number)
    ):
        return
    sandbox_network.attest_flow_posture(
        run.store,
        flow=run.run_kind,
        runner=run.runner.name,
        config=run.config.sandbox,
        posture=(
            sandbox_network.POSTURE_SANDBOXED
            if run.contained_image is not None
            else run.sandbox_posture or "host"
        ),
        image=run.contained_image,
        run_id=run.run_id,
        checkout_dir=worktree_dir,
        repo=run.repo,
        issue_num=run.issue_num,
        item_id=run.item_id,
        inference_transport=run.transport.resolved,
        certified=admitted,
        attempt_number=attempt_number,
    )


def _prior_launch_evidence(run: _TrackedRun) -> bool | None:
    return run.launch_state.launched if run.launch_state is not None else run.launched


def _settle_attempt_launch(
    run: _TrackedRun, result: DispatchResult, prior: bool | None
) -> None:
    """Replace provisional spawn evidence with the returned attempt's evidence."""
    settled = merge_launch_evidence(prior, result.launched)
    run.launched = settled is True
    if run.launch_state is not None:
        run.launch_state.launched = settled


def _run_retry_attempt(
    run: _TrackedRun,
    *,
    prompt: str,
    worktree_dir: Path,
    attempt: int,
    prev_result: DispatchResult | None,
    store: RunStore | None,
    budget_cap_usd: float,
    pricing_overrides: dict[str, tuple[float, float]] | None,
    resume_session_id: str | None,
    budget_override_usd: float | None,
    timeout_override_minutes: int | None,
    on_spawn: Callable[[int], Callable[[], None] | None] | None,
    should_pause: Callable[[], bool] | None,
    budget_check: Callable[[float], bool] | None,
    anomaly_threshold_usd: float | None,
    anomaly_compaction_allowance_usd: float,
    managed_switchyard: Any,
) -> tuple[DispatchResult, bool]:
    """Run one attempt after checking durable stop intent and launch evidence."""
    _refuse_stopped_attempt(run)
    prior_launch = _prior_launch_evidence(run)
    result: DispatchResult | None = None
    try:
        _record_attempt_start(run, attempt + 1)
        _attest_tracked_posture(run, worktree_dir, attempt_number=attempt + 1)
        with _sidecar_attempt_context(run, attempt + 1) as sidecar_supervisor:
            contained = None
            if run.transport.resolved.sidecar:
                contained = _contained_execution_for(
                    run,
                    worktree_dir,
                    sidecar_supervisor=sidecar_supervisor,
                    budget_override_usd=budget_override_usd,
                )
            _admit_attempt_route_profile(
                run,
                attempt + 1,
                sidecar_supervisor=sidecar_supervisor,
            )
            _attest_tracked_posture(
                run, worktree_dir, attempt_number=attempt + 1, admitted=True
            )
            if run.paid_launch_check is not None:
                run.paid_launch_check()
            if contained is None:
                contained = _contained_execution_for(
                    run, worktree_dir, budget_override_usd=budget_override_usd
                )
            require_retry_credential_lifetime(
                run.credential,
                run.config.retry,
                run_timeout_seconds(run.config, timeout_override_minutes),
                start_attempt=attempt,
            )
            _refuse_stopped_attempt(run)
            result = run_agent(
                admission=run.admission,
                runner=run.runner,
                prompt=prompt,
                worktree_dir=worktree_dir,
                config=run.config,
                issue_num=run.issue_num,
                contained=contained,
                resume_session_id=resume_session_id,
                model=run.effective_model,
                emit_events=True,
                budget_override_usd=budget_override_usd,
                timeout_override_minutes=timeout_override_minutes,
                # Always pass the pre-computed path so retries share one event file.
                log_file_path=run.log_file,
                on_spawn=on_spawn,
                should_pause=should_pause,
                budget_check=budget_check,
                anomaly_threshold_usd=anomaly_threshold_usd,
                anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
                inference_transport=run.transport.resolved,
                transport_health_check=run.transport_health_check,
                managed_switchyard=managed_switchyard,
                credential=run.credential,
            )
            if prev_result is not None:
                result.merge_usage(prev_result)
            _settle_attempt_launch(run, result, prior_launch)
    except Exception as exc:
        if result is not None:
            raise _CompletedAttemptCleanupFailure(exc, result) from exc
        raise
    assert result is not None
    return (
        result,
        _classify_attempt(result, run, store, budget_cap_usd, pricing_overrides)
        or _resource_retry_spent(result, prev_result),
    )


def _resource_retry_spent(
    result: DispatchResult, prev_result: DispatchResult | None
) -> bool:
    """A resource exhaustion earns one retry; a second in a row ends it (#2887)."""
    if result.error_category != RESOURCE_CATEGORY or prev_result is None:
        return False
    if prev_result.error_category != RESOURCE_CATEGORY:
        return False
    _warn("Resource exhaustion again after a retry -- not retrying")
    return True


def _cleanup_failure_results(
    exc: Exception, current_result: DispatchResult | None
) -> tuple[DispatchResult | None, DispatchResult | None]:
    """Select results for sidecar finalization and terminal accounting."""
    if isinstance(exc, _CompletedAttemptCleanupFailure):
        return exc.result, exc.result
    partial_result = partial_dispatch_result(exc)
    if partial_result is not None:
        if current_result is not None:
            partial_result.merge_usage(current_result)
        return partial_result, partial_result
    return current_result, current_result


def _enter_managed_retry_context(
    run: _TrackedRun,
    store: RunStore | None,
    managed_context: contextlib.AbstractContextManager[Any],
) -> Any:
    """Enter a retry-owned managed proxy and record its admission evidence."""
    managed_switchyard = managed_context.__enter__()
    try:
        if managed_switchyard is not None:
            _record_retry_managed_transport(
                run,
                store,
                managed_switchyard.observed_version or "",
                managed_switchyard.config_sha256 or "",
            )
    except BaseException:
        managed_context.__exit__(*sys.exc_info())
        raise
    return managed_switchyard


def _run_retry_loop(
    run: _TrackedRun,
    *,
    prompt: str,
    worktree_dir: Path,
    budget_override_usd: float | None,
    timeout_override_minutes: int | None,
    should_pause: Callable[[], bool] | None,
    budget_check: Callable[[float], bool] | None,
    anomaly_threshold_usd: float | None,
    anomaly_compaction_allowance_usd: float,
) -> _RetryOutcome:
    """Run the resume-escalating retry loop and classify its terminal outcome.

    Owns the full attempt loop ``run_tracked_agent`` ran inline: the
    PR-already-exists guard, the ``run_agent`` invocation, cross-attempt cost
    accumulation, the terminal-status classifiers (pause, budget watchdog,
    breaker, operator kill, cost-aware budget guard, permanent failure), and the
    resume/fresh backoff. A mid-loop exception is caught here and turned into a
    terminal ``early_return`` result exactly as the original ``except`` block
    did (marking the pending row failed and emitting the terminal audit row).
    """
    config = run.config
    store = run.store

    # Record the agent's process group on spawn so `kew stop` can find it.
    # With start_new_session=True the pid IS the pgid.
    on_spawn = partial(_record_retry_spawn, run)

    # Effective runtime cap: the same value handed to the runner as
    # --max-budget-usd (override wins over the config default). Used by the
    # cost-aware retry guard below (#642). Falsy/0 means "no cap set".
    _budget_cap_usd = (
        budget_override_usd
        if budget_override_usd is not None
        else config.agent.max_budget_usd
    )
    _pricing_overrides = build_pricing_overrides(config.pricing.overrides)

    last_exit_code = 1
    total_attempts = 0
    prev_result: DispatchResult | None = None
    pr_confirmed = False
    managed_context = managed_switchyard_context(config, run.transport.resolved)
    managed_switchyard = None
    managed_context_entered = False

    # Retry loop with resume escalation
    result: DispatchResult | None = None
    resume_session_id: str | None = None
    try:
        managed_switchyard = _enter_managed_retry_context(run, store, managed_context)
        managed_context_entered = True
        for attempt in range(config.retry.max_attempts):
            _refuse_stopped_attempt(run)
            hit, existing_result = _check_existing_pr(
                run, attempt, prev_result, result, store
            )
            if hit:
                assert existing_result is not None
                result = existing_result
                last_exit_code = 0
                pr_confirmed = True
                break

            # A skipped retry retains the last attempt's identity and posture.
            total_attempts = attempt + 1

            result, terminal = _run_retry_attempt(
                run,
                prompt=prompt,
                worktree_dir=worktree_dir,
                attempt=attempt,
                prev_result=prev_result,
                store=store,
                budget_cap_usd=_budget_cap_usd,
                pricing_overrides=_pricing_overrides,
                resume_session_id=resume_session_id,
                budget_override_usd=budget_override_usd,
                timeout_override_minutes=timeout_override_minutes,
                on_spawn=on_spawn,
                should_pause=should_pause,
                budget_check=budget_check,
                anomaly_threshold_usd=anomaly_threshold_usd,
                anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
                managed_switchyard=managed_switchyard,
            )
            last_exit_code = result.exit_code

            # Terminal-status classification (pause / budget watchdog / breaker /
            # operator kill / success / cost guard / permanent failure).
            if terminal:
                break

            is_last = attempt >= config.retry.max_attempts - 1
            if is_last:
                break

            # Save current result for cost accumulation on next attempt
            prev_result = result

            resume_session_id, strategy = _resume_or_fresh(attempt, result, run)

            wait = retry_backoff_seconds(config.retry, attempt)
            wait *= random.uniform(0.75, RETRY_JITTER_MAX)  # nosec B311 — retry jitter, not security
            # Ladder position + strategy so the operator can see the retry
            # unfolding in the foreground (CS-8). Runs in every dispatch mode:
            # single, --drain, and --workers all share this path.
            _warn(
                f"Attempt {attempt + 1}/{config.retry.max_attempts} failed "
                f"({result.error_category or 'error'}) — "
                f"retrying {strategy} in {wait:.0f}s..."
            )
            time.sleep(wait)
    except Exception as exc:
        result, completed_result = _cleanup_failure_results(exc, result)
        return _retry_exception_or_refusal(
            run, exc, total_attempts, completed_result=completed_result
        )
    finally:
        _close_managed_retry_context(
            managed_context,
            entered=managed_context_entered,
            managed=managed_switchyard,
            result=result,
            route=run.transport.resolved.route,
        )

    return _RetryOutcome(
        result=result,
        last_exit_code=last_exit_code,
        total_attempts=total_attempts,
        pr_confirmed=pr_confirmed,
    )


def _close_managed_retry_context(
    managed_context: contextlib.AbstractContextManager[object | None],
    *,
    entered: bool,
    managed: object | None,
    result: DispatchResult | None,
    route: str | None,
) -> None:
    """Close the retry-owned proxy, then consume its teardown-only evidence."""
    if entered:
        managed_context.__exit__(None, None, None)
    if managed is None or result is None:
        return
    reconcile_managed_switchyard_lifecycle(managed, result, route=route)


def _apply_completion_status(
    stored: RunRecord, result: DispatchResult, last_exit_code: int
) -> bool:
    """Map ``result.error_category`` → ``stored.status`` for the terminal row.
    Preserve operator stop intent and report it through the result.
    Pending stop rows retain ownership until fleet or reconciliation confirms death.
    Already finalized stop rows retain their terminal status and timestamp.
    """
    was_killed = _stop_requested(stored)
    if was_killed:
        result.error_category = "killed"
    elif result.error_category in {
        *STARTUP_REFUSED_CATEGORIES,
        "sandbox_startup_unknown",
    }:
        stored.status = "failure"
    elif result.error_category == "circuit_breaker":
        stored.status = "killed"
    elif result.error_category == "budget_stop":
        stored.status = "budget_stop"
    elif result.error_category == "budget_stopped":
        stored.status = "budget_stopped"
    elif result.error_category == "budget_exceeded":
        stored.status = "budget_exceeded"
    elif result.error_category == QUOTA_EXHAUSTED_CATEGORY:
        # Not the work's failure (#2643): the overlay never latches "refused".
        stored.status = _REFUSAL_STATUS
    elif result.error_category in ("permanent", "transient", "needs_human"):
        stored.status = "failure"
    else:
        # Detect budget exhaustion -- exit 0 but not a real success is caught
        # above; here a plain exit 0 is a real success.
        stored.status = "success" if last_exit_code == 0 else "failure"
    return was_killed


def _completion_error_category(stored: RunRecord, result: DispatchResult) -> str:
    """The completion row's category: the result's, or the flag on a success
    the cost-anomaly rule judged on its closing line (#1798), so the bucket
    median leaves that anomalous cost out."""
    from kew.breaker import FLAGGED_ANOMALY_CATEGORY

    category = result.error_category or ""
    if not category and stored.status == "success" and result.breaker_after_terminal:
        return FLAGGED_ANOMALY_CATEGORY
    return category


def _copy_result_accounting(stored: RunRecord, result: DispatchResult) -> None:
    """Copy every persisted accounting field from a measured result."""
    stored.input_tokens = result.input_tokens
    stored.output_tokens = result.output_tokens
    stored.cache_creation_tokens = result.cache_creation_tokens
    stored.cache_read_tokens = result.cache_read_tokens
    stored.total_tokens = result.total_tokens
    stored.estimated_cost_usd = result.estimated_cost_usd
    stored.planned_cost_usd = result.planned_cost_usd
    stored.classifier_overhead_model = result.classifier_overhead_model or None
    stored.classifier_overhead_input_tokens = result.classifier_overhead_input_tokens
    stored.classifier_overhead_output_tokens = result.classifier_overhead_output_tokens
    stored.classifier_overhead_cost_usd = result.classifier_overhead_cost_usd
    stored.switchyard_proxy_observation = _encoded_proxy_observation(result)
    stored.accounting_status = result.accounting_status or None


def _resolved_model_source(
    result: DispatchResult, reported_model: str | None
) -> str | None:
    """Provenance of the served model, shared by persistence and the audit event.

    An explicit source set by Switchyard reconciliation wins; otherwise a model
    only counts as runner-reported when the runner actually reported one.
    """
    return result.resolved_model_source or (
        "runner_reported" if reported_model else None
    )


def _effective_requested_tier(run: _TrackedRun, result: DispatchResult) -> str | None:
    """The governed service tier this run asked for, never a prompt-derived one."""
    return (
        result.requested_service_tier
        or _requested_service_tier(run.config, _tracked_runner_name(run))
        or None
    )


def _encoded_proxy_observation(result: DispatchResult) -> str | None:
    """Canonical JSON for the proxy observation column; None when unobserved."""
    if result.switchyard_proxy_observation is None:
        return None
    return json.dumps(
        result.switchyard_proxy_observation,
        sort_keys=True,
        separators=(",", ":"),
    )


def _classifier_overhead_payload(result: DispatchResult) -> dict[str, object]:
    """Classifier overhead for the audit event, labelled with its authority.

    Overhead is only ever proxy-observed, so the authority is stated explicitly
    rather than left to read as runner-reported usage.
    """
    return {
        "accounting_authority": (
            "switchyard_proxy_observation" if result.classifier_overhead_model else None
        ),
        "input_tokens": result.classifier_overhead_input_tokens,
        "model": result.classifier_overhead_model or None,
        "output_tokens": result.classifier_overhead_output_tokens,
        "cost_usd": round(result.classifier_overhead_cost_usd, 6),
    }


def _adaptive_accounting_audit_payload(
    result: DispatchResult,
) -> dict[str, object]:
    """Return the terminal adaptive reconciliation verdict and bounded reasons."""
    return {
        "accounting_status": result.accounting_status or None,
        "accounting_unreconciled_reasons": (
            result.accounting_unreconciled_reasons or []
        ),
    }


def _persist_completion(
    run: _TrackedRun,
    result: DispatchResult,
    outcome: _RetryOutcome,
    reported_model: str | None,
) -> None:
    """Record the terminal completion row in the store and note per-issue cap
    crossings. No-op without a store."""
    from kew.runner_fields import service_tier_token

    _validate_session_result(run, result, outcome.total_attempts)
    result.reported_service_tier = service_tier_token(result.reported_service_tier)
    reported_model = _normalize_reported_model(reported_model)
    store = run.store
    if store is None:
        return
    last_exit_code = outcome.last_exit_code
    total_attempts = outcome.total_attempts

    stored = store.get(run.run_id)
    if stored is not None:
        was_killed = _apply_completion_status(stored, result, last_exit_code)
        if not was_killed:
            stored.finished_at = datetime.now(UTC)
            stored.error_category = _completion_error_category(stored, result)
        stored.exit_code = last_exit_code
        stored.duration_seconds = time.monotonic() - run.dispatch_start
        _copy_result_accounting(stored, result)
        stored.session_id = result.session_id
        stored.attempt_number = total_attempts
        stored.log_file = result.log_file
        stored.complexity = run.complexity
        stored.reported_model = reported_model
        stored.resolved_model_source = _resolved_model_source(result, reported_model)
        stored.selected_tier = result.selected_tier or None
        stored.requested_service_tier = _effective_requested_tier(run, result)
        stored.reported_service_tier = result.reported_service_tier or None
        stored.transport_observed_version = run.transport.observed_version
        stored.transport_config_sha256 = run.transport.resolved.config_sha256
        stored.transport_preflight_verdict = run.transport.preflight_verdict
        _metrics = first_edit_metrics_for(result.log_file)
        stored.steps_to_first_edit = _metrics.steps_to_first_edit
        stored.tokens_to_first_edit = _metrics.tokens_to_first_edit
        _save_attempt_record(store, stored)

    # This run's cost is now in the store; note if the item's total spend
    # has passed the per-issue cap (the next dispatch will be blocked — CS-7).
    _note_if_over_per_issue(run.config, store, run.issue_num, run.item_id)


def _runner_producer(run: _TrackedRun) -> Producer:
    """The runner whose stream these facts were derived from, at kew's version."""
    name = str(getattr(run.runner, "name", "")) or "unknown"
    return Producer(kind=ProducerKind.RUNNER, name=name, version=__version__)


def _import_runner_evidence(run: _TrackedRun, result: DispatchResult) -> None:
    """Import the run's derived stream facts into the one evidence ledger (I06).

    Everything here came off a provider stream, so it goes in through
    ``import_runner_evidence`` and is stamped ``producer_kind=runner`` forever:
    a runner's account of its own work is evidence *of a claim*, never a host
    attestation. The facts are the closed, numeric, sanitized set the streaming
    derivation kept — no transcript, no reasoning, no report body (the report
    is published on the PR; the ledger records only that there was one).

    Telemetry, so best-effort: a ledger that cannot take these must not fail a
    dispatch that already ran. An absent derivation is recorded explicitly as
    ``evidence_unavailable`` rather than as a run that did nothing.
    """
    if run.store is None:
        return
    evidence = read_derived_evidence(result.log_file)
    envelope: dict[str, Any] = {
        "run_id": run.run_id,
        "issue_num": run.issue_num,
        "item_id": run.item_id,
        "repo": run.repo,
        "endpoint": run.endpoint,
    }
    producer = _runner_producer(run)
    if evidence is None:
        _emit_runner_progress(run, producer, "evidence_unavailable", None, envelope)
        return
    _emit_runner_progress(
        run,
        producer,
        evidence.terminal_type or "no_terminal_event",
        f"steps={evidence.steps} tool_uses={evidence.tool_use_count} "
        f"report={'yes' if evidence.report else 'no'}",
        envelope,
    )
    for metric, value, unit in _runner_usage_metrics(evidence):
        import_runner_evidence(
            run.store,
            EvidenceKind.RUNNER_USAGE,
            actor=run.actor,
            producer=producer,
            facts={"metric": metric, "value": value, "unit": unit},
            **envelope,
        )


def _emit_runner_progress(
    run: _TrackedRun,
    producer: Producer,
    phase: str,
    detail: str | None,
    envelope: dict[str, Any],
) -> None:
    facts: dict[str, Any] = {"phase": phase}
    if detail is not None:
        facts["detail"] = detail
    import_runner_evidence(
        run.store,
        EvidenceKind.RUNNER_PROGRESS,
        actor=run.actor,
        producer=producer,
        facts=facts,
        **envelope,
    )


def _runner_usage_metrics(
    evidence: DerivedRunEvidence,
) -> list[tuple[str, int, str]]:
    """The (metric, value, unit) triples a derived stream may contribute.

    First-edit metrics are omitted when the run never edited: a zero-edit run
    is NULL, never 0, and an evidence row asserting ``0`` would be the same lie
    the persisted columns are careful not to tell.
    """
    metrics = [
        ("steps", evidence.steps, "steps"),
        ("tool_uses", evidence.tool_use_count, "calls"),
    ]
    if evidence.first_edit.steps_to_first_edit is not None:
        metrics.append(
            ("steps_to_first_edit", evidence.first_edit.steps_to_first_edit, "steps")
        )
    if evidence.first_edit.tokens_to_first_edit is not None:
        metrics.append(
            ("tokens_to_first_edit", evidence.first_edit.tokens_to_first_edit, "tokens")
        )
    return metrics


def _emit_completion_audit(
    run: _TrackedRun,
    result: DispatchResult,
    last_exit_code: int,
    total_attempts: int,
    reported_model: str | None,
) -> None:
    """Structured ``dispatch_completed`` audit event — best-effort, never
    affects dispatch outcome."""
    from kew.runner_fields import service_tier_token

    reported_model = _normalize_reported_model(reported_model)
    audit.emit(
        run.store,
        "dispatch_completed",
        actor=run.actor,
        run_id=run.run_id,
        issue_num=run.issue_num,
        item_id=run.item_id,
        endpoint=run.endpoint,
        repo=run.repo,
        payload={
            **audit.transport_evidence(
                mode=run.transport.resolved.mode,
                endpoint=run.transport.resolved.endpoint,
                route=run.transport.resolved.route,
                managed=run.transport.resolved.managed,
                expected_version=run.transport.resolved.expected_version or None,
                observed_version=run.transport.observed_version,
                config_sha256=run.transport.resolved.config_sha256,
                preflight_verdict=run.transport.preflight_verdict,
                binary_sha256=run.transport.resolved.binary_sha256,
                credential_env_names=run.transport.resolved.credential_env_names,
            ),
            **_orchestrator_provenance(),
            "complexity": run.complexity,
            "runner": str(getattr(run.runner, "name", ""))
            if run.runner is not None
            else "",
            "model": str(run.effective_model) if run.effective_model else "",
            "reported_model": reported_model,
            "resolved_model_source": _resolved_model_source(result, reported_model),
            "admitted_route": run.transport.resolved.route,
            "selected_tier": result.selected_tier or None,
            "requested_service_tier": _effective_requested_tier(run, result),
            "reported_service_tier": service_tier_token(result.reported_service_tier)
            or None,
            "classifier_overhead": _classifier_overhead_payload(result),
            "classifier_evidence": result.classifier_evidence,
            "switchyard_proxy_observation": result.switchyard_proxy_observation,
            **_adaptive_accounting_audit_payload(result),
            "exit_code": last_exit_code,
            "duration_seconds": round(time.monotonic() - run.dispatch_start, 2),
            "cost_usd": round(result.estimated_cost_usd or 0.0, 6),
            "tokens": result.total_tokens or 0,
            "attempt_number": total_attempts,
            "error_category": result.error_category or "",
            "reason": result.stop_reason,
            "loop_id": run.loop_id,
        },
    )


def _optional_audit_text(value: str) -> str | None:
    """Return a populated audit string, or JSON null."""
    return redact_credentials(value) if value else None


def _maybe_emit_breaker(
    run: _TrackedRun,
    result: DispatchResult,
    *,
    baseline: RunBaseline | None = None,
) -> None:
    """Emit a ``circuit_breaker`` audit event when a breaker rule (cost anomaly,
    whether it stopped the run or flagged a finished one) or the wall-clock
    timeout kill fired, with the baseline behind the threshold (#1798).
    ``enabled = false`` disables all
    breaker rules, including naming the existing timeout kill — emit nothing when
    the breaker is off (#245)."""
    from kew.breaker import baseline_payload

    breaker_rule = ""
    if result.breaker_rule:
        breaker_rule = result.breaker_rule  # cost_anomaly
    elif result.hard_stop:
        breaker_rule = result.hard_stop  # wall_clock | resource_cap | hard_kill
    elif result.exit_code == 124:
        breaker_rule = "wall_clock"  # an unclassified timeout-wrapper exit
    if breaker_rule and run.config.breaker.enabled:
        audit.emit(
            run.store,
            "circuit_breaker",
            actor=Actor("policy", breaker_rule),
            issue_num=run.issue_num,
            item_id=run.item_id,
            run_id=run.run_id,
            repo=run.config.project.repo or None,
            payload={
                "rule": breaker_rule,
                "scope": "run",
                "reason": _optional_audit_text(result.resource_cap_reason),
                "resource_cap_code": _optional_audit_text(result.resource_cap_code),
                "resource_cap_observations": redact_container_observations(
                    result.resource_cap_observations
                ),
                "resource_cap_observations_truncated": (
                    result.resource_cap_observations_truncated
                ),
                "oom_probe_status": _optional_audit_text(result.oom_probe_status),
                "oom_probe_diagnostic": _optional_audit_text(
                    result.oom_probe_diagnostic
                ),
                "observed_usd": round(result.estimated_cost_usd or 0.0, 4),
                "model": run.effective_model,
                "complexity": run.complexity,
                **baseline_payload(
                    baseline,
                    tripped_threshold_usd=result.breaker_threshold_usd,
                    terminal_observed=result.terminal_observed,
                ),
            },
        )


def _cost_model_for_result(run: _TrackedRun, result: DispatchResult) -> str:
    """Choose the governed pricing identity while preserving route aliases."""
    from kew.runner_fields import REDACTED_IDENTITY

    if _normalize_reported_model(result.resolved_model) == REDACTED_IDENTITY:
        return run.effective_model
    transport = run.transport.resolved
    if (
        transport.mode == "switchyard"
        and transport.route
        and transport.route != "switchyard/passthrough"
        and result.resolved_model
        and not result.resolved_model.startswith("switchyard/")
    ):
        return result.resolved_model
    return run.effective_model


def _mark_unpriced_accounting(
    result: DispatchResult,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Make "we could not price this" an explicit fact, not a $0.00 claim.

    A run that burned tokens against a model with no usable rate records
    ``estimated_cost_usd = 0.0``, which is indistinguishable in the ledger from
    a run that genuinely cost nothing — local inference under a free provider
    prefix is priced at $0 *by design* and must stay readable as such (#1092).
    Left unmarked, an unknown price is summed as free by every consumer of the
    cost column: ``cost_per_merged_pr``, the ``cost_anomaly_factor`` baselines,
    and the published evidence packs (#1565).

    ``is_priced_model`` already draws the line, treating a free-provider prefix
    as priced. Only a run with no native cost figure, no usable rate, and real
    token usage is marked here. The reason is appended rather than assigned so
    an adaptive route's own reconciliation reasons survive alongside it.
    """
    if result.reported_cost_usd is not None:
        return
    if not (result.input_tokens or result.output_tokens):
        return
    if model and is_priced_model(model, overrides):
        return
    reasons = list(result.accounting_unreconciled_reasons or ())
    if "unpriced_model" not in reasons:
        reasons.append("unpriced_model")
    result.accounting_status = "unreconciled"
    result.accounting_unreconciled_reasons = reasons


def _finalize_cost(run: _TrackedRun, result: DispatchResult) -> None:
    """Finalize accumulated usage identically on normal and exceptional exits.

    A run the breaker judged, stopped or flagged, records at least the cost it
    crossed the bar at: a later line (a closing summary, a native per-step
    figure) cannot restate it as cheaper than the trip (#1798).
    """
    trip_floor_usd = (
        _recordable_usd(result.estimated_cost_usd) if result.breaker_rule else 0.0
    )
    pricing_overrides = build_pricing_overrides(run.config.pricing.overrides)
    cost_model = _cost_model_for_result(run, result)
    result.compute_cost(
        model=cost_model or None,
        overrides=pricing_overrides,
        classifier_model=result.classifier_overhead_model or None,
        classifier_input_tokens=result.classifier_overhead_input_tokens,
        classifier_output_tokens=result.classifier_overhead_output_tokens,
    )
    result.estimated_cost_usd = max(result.estimated_cost_usd, trip_floor_usd)
    _mark_unpriced_accounting(result, cost_model, pricing_overrides)


def _finalize_result(run: _TrackedRun, outcome: _RetryOutcome) -> DispatchResult:
    """Synthesize the terminal result, record completion, and emit audit events.

    Post-loop tail of ``run_tracked_agent``: fills a synthetic failure if the
    loop never produced a result, reflects a PR-guard success, stamps the run
    id / attempt count, computes cost, records the completion row in the store,
    and emits the ``dispatch_completed`` (plus optional ``circuit_breaker``)
    audit events. Behavior is identical to the original inline tail.
    """
    require_claim_recovery(run.store, run.run_id)
    result = outcome.result
    last_exit_code = outcome.last_exit_code
    total_attempts = outcome.total_attempts

    # If we never ran (shouldn't happen), produce a synthetic failure result
    if result is None:
        result = DispatchResult(
            exit_code=1,
            issue_num=run.issue_num,
            log_file=run.log_file,
            error_category="permanent",
            run_id=run.run_id,
        )

    # If PR guard broke the loop, reflect success in the result
    if outcome.pr_confirmed:
        result.exit_code = 0
        result.error_category = ""

    # Stamp the tracked run id so callers (e.g. the loop driver) get a valid FK.
    result.run_id = run.run_id
    # Surface the attempt count so the failure summary can name it (CS-8).
    result.attempt_number = total_attempts
    if not result.requested_service_tier:
        result.requested_service_tier = (
            _requested_service_tier(run.config, _tracked_runner_name(run)) or ""
        )
    if result.resolved_model and not result.resolved_model_source:
        result.resolved_model_source = "runner_reported"

    # Compute cost from accumulated tokens
    _finalize_cost(run, result)

    _reported_model = _normalize_reported_model(result.resolved_model)

    _persist_completion(run, result, outcome, _reported_model)
    _emit_completion_audit(run, result, last_exit_code, total_attempts, _reported_model)
    _import_runner_evidence(run, result)
    _maybe_emit_breaker(run, result, baseline=run.breaker_baseline)
    if result.breaker_after_terminal:
        _warn(f"#{run.issue_num} flagged. {_flagged_anomaly_note(result)}")
    _export_run_report_snapshot(run)

    return result


def _export_run_report_snapshot(run: _TrackedRun) -> None:
    """Decision-intelligence exhaust (#1578), best-effort by contract.

    Runs LAST in the terminal tail so the report can carry the completion row,
    the ``dispatch_completed`` event, and the imported runner evidence.
    Outcome fields that settle later (``pr_merged_at``, ``review_cycles``) are
    re-exported by ``sync_outcomes`` — this snapshot is the at-completion
    truth, not the final word. The enabled gate precedes the store read so
    the default-off majority pays nothing; the broad except is deliberate: a
    report failure must never fail a dispatch that already ran.
    """
    try:
        if run.store is None or not run.config.run_reports.enabled:
            return
        from kew.run_report_sink import export_run_report

        record = run.store.get(run.run_id)
        if record is not None:
            export_run_report(run.config, run.store, record, repo=run.repo)
    except Exception as exc:
        _logger.warning(
            "run-report snapshot skipped (non-fatal): %s",
            redact_credentials(exception_text(exc)),
        )


def _continuation_identity_is_claimed(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    worktree_identity: str,
    branch: str,
    run_id: str,
) -> bool:
    """Check that exactly one durable continuation owns this identity."""
    matches = [
        record
        for record in store.list_continuations(epic_ref)
        if record.item_id == item_id
        and record.branch == branch
        and continuation_checkout_matches(record.worktree_identity, worktree_identity)
        and record.state == "continuing"
        and record.continuation_run_id == run_id
    ]
    return len(matches) == 1


def _require_continuation_dispatch_parts(
    store: RunStore | None,
    epic_ref: str | None,
    worktree_dir: Path | None,
    branch: str | None,
    run_id: str | None,
) -> None:
    """Require the durable context needed for a continuation dispatch."""
    if (
        not all(value is not None for value in (worktree_dir, branch, run_id))
        or not branch
        or not run_id
    ):
        raise ValueError("continuation dispatch requires worktree, branch, and run id")
    if store is None or epic_ref is None:
        raise ValueError("continuation dispatch requires a store and epic identity")


def _require_continuation_kind(run_kind: str) -> None:
    """A continuation resumes an implementation; a PR-subject kind never does.

    Enforced before any store lookup: the run store exempts PR-subject rows
    from its retained-claim check (they share a continued branch's item and
    branch without being members of the family), so no continuation row may
    ever carry such a kind.
    """
    from kew.run_store import PR_SUBJECT_RUN_KINDS

    if run_kind in PR_SUBJECT_RUN_KINDS:
        raise ValueError(
            f"continuation dispatch cannot carry the PR-subject run kind {run_kind!r}"
        )


def _validate_continuation_dispatch_identity(
    *,
    store: RunStore | None,
    epic_ref: str | None,
    item_id: str,
    worktree_dir: Path | None,
    branch: str | None,
    run_id: str | None,
    run_kind: str = "dispatch",
) -> None:
    """Require a claimed continuation before reusing a worktree or run id.

    The continuation triplet is intentionally validated at both dispatch
    boundaries.  The CLI proves the filesystem state, while this boundary
    proves that the caller still owns the exact durable continuation generation
    it is about to turn into a ``RunRecord``.  In particular, a caller cannot
    use an arbitrary run id to overwrite an existing row or pair a preserved
    checkout with a different branch.
    """
    values = (worktree_dir, branch, run_id)
    if not any(value is not None for value in values):
        return
    _require_continuation_kind(run_kind)
    _require_continuation_dispatch_parts(store, epic_ref, worktree_dir, branch, run_id)

    assert store is not None
    assert epic_ref is not None
    assert worktree_dir is not None
    assert branch is not None
    assert run_id is not None
    worktree_identity = str(worktree_dir.expanduser().resolve(strict=False))
    if not _continuation_identity_is_claimed(
        store, epic_ref, item_id, worktree_identity, branch, run_id
    ):
        raise ValueError(
            "continuation dispatch does not match one claimed continuation"
        )
    if store.get(run_id) is not None:
        raise ValueError(
            f"continuation run id {run_id!r} already has a persisted run record"
        )


def _dispatch_log_item_id(issue_num: int, item_id: str | None) -> str:
    """Return a bounded filename-safe form without flattening opaque ids to 0."""
    raw = item_id or str(issue_num)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:128]
    return safe or str(issue_num)


def _require_recorded_store(store: RunStore | None, allow_unrecorded: bool) -> None:
    """Refuse a paid run that would leave no realized-route row at all.

    Exactly the silent-capture bypass the #1550 review refused. Production
    callers always have a store; ``allow_unrecorded`` exists for tests and
    deliberately unrecorded simulations, never as a default.
    """
    if store is None and not allow_unrecorded:
        raise ValueError(
            "run_tracked_agent: paid execution requires a store so the "
            "realized route profile is captured; pass allow_unrecorded=True "
            "only for deliberately unrecorded (test) runs"
        )


def _validate_tracked_run_identity(
    *,
    validate: bool,
    store: RunStore | None,
    epic_ref: str | None,
    item_id: str,
    worktree_dir: Path,
    branch: str,
    run_id: str | None,
    run_kind: str = "dispatch",
) -> None:
    """Validate a supplied continuation identity when the caller requires it."""
    if not validate or run_id is None:
        return
    _validate_continuation_dispatch_identity(
        store=store,
        epic_ref=epic_ref,
        item_id=item_id,
        worktree_dir=worktree_dir,
        branch=branch,
        run_id=run_id,
        run_kind=run_kind,
    )


def _probe_external_transport(
    config: DispatchConfig, resolved: ResolvedInferenceTransport
) -> SwitchyardProbeResult:
    return probe_switchyard_endpoint(
        endpoint=resolved.endpoint or "",
        route=resolved.route or "",
        timeout_seconds=float(
            config.inference_transport.switchyard.startup_timeout_seconds
        ),
    )


def _tracked_transport_preflight(
    config: DispatchConfig,
    resolved: ResolvedInferenceTransport,
    preflight: TransportPreflight | None,
) -> TransportPreflight | None:
    """Provide the default probe only for an unmanaged external transport."""
    if (
        resolved.mode == "switchyard"
        and not resolved.managed
        and not resolved.sidecar
        and preflight is None
    ):
        return partial(_probe_external_transport, config)
    return preflight


def _invoke_admitted(callback: Callable[[str], None] | None, run_id: str) -> None:
    """Notify the claim owner after the admission transaction commits."""
    if callback is not None:
        callback(run_id)


def _invoke_launch(callback: Callable[[], None] | None) -> None:
    """Notify the claim owner after the paid child starts."""
    if callback is not None:
        callback()


def _run_admission_hook(
    run: _TrackedRun, on_admitted: Callable[[str], None] | None
) -> DispatchResult | None:
    """Finalize hook failures through the existing pre-launch refusal path.

    A replaced reservation and a hook that started a tracker claim propagate
    unchanged: the dispatch pipeline records retention and terminalizes the
    admitted row itself. Every other hook failure is a refused startup.
    """
    if on_admitted is None:
        return None
    try:
        _invoke_admitted(on_admitted, run.run_id)
    except DispatchReservationReplaced:
        raise
    except Exception as exc:
        if claim_recovery_pending(run.store, run.run_id):
            raise
        outcome = _retry_exception_outcome(run, _AdmissionHookRefused(str(exc)), 0)
        release_admission_reservation(run.store, run.run_id)
        return outcome.early_return
    return None


def _require_local_contained_placement(
    config: DispatchConfig, run_kind: str, contained_image: str | None
) -> None:
    """Refuse, before admission, a contained run whose placement is not here.

    This core's contained execution (:func:`_contained_execution_for`) builds
    its container on this machine and has no remote arm, so the placement
    governing *run_kind* is honoured the only way it can be: ``prefer-gcp``
    runs here and a ``gcp`` pin refuses with no row written
    (:func:`kew.dispatch_placement.require_local_placement`, #2526 review). A
    host run has no container to place and is not asked.
    """
    if contained_image is None:
        return
    from kew.dispatch_placement import require_local_placement

    require_local_placement(config, run_kind=run_kind)


def run_tracked_agent(
    *,
    prompt: str,
    worktree_dir: Path,
    config: DispatchConfig,
    issue_num: int,
    store: RunStore | None = None,
    title: str = "",
    branch: str = "",
    model: str | None = None,
    runner: AgentRunner | None = None,
    complexity: str = "",
    run_kind: str = "dispatch",
    run_id: str | None = None,
    epic_ref: str | None = None,
    loop_id: str | None = None,
    budget_override_usd: float | None = None,
    timeout_override_minutes: int | None = None,
    should_pause: Callable[[], bool] | None = None,
    budget_check: Callable[[float], bool] | None = None,
    prefix_key: str | None = None,
    provider: str | None = None,
    source_tracker: str = "github",
    worker_cap: int = 0,
    cap_exclude_issue: int | None = None,
    item_id: str | None = None,
    admit_budget_check: Callable[[], None] | None = None,
    map_tokens: int | None = None,
    lesson_corpus_tokens: int | None = None,
    self_check: bool = False,
    prompt_profile_version_id: int | None = None,
    transport_preflight: TransportPreflight | None = None,
    transport_health_check: Callable[[], bool] | None = None,
    contained_image: str | None = None,
    sandbox_posture: str | None = None,
    allow_unrecorded: bool = False,
    validate_continuation: bool = True,
    paid_launch_check: Callable[[], None] | None = None,
    launch_state: LaunchState | None = None,
    on_admitted: Callable[[str], None] | None = None,
    on_admitting: Callable[[], None] | None = None,
    on_launch: Callable[[], None] | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    inference_transport: ResolvedInferenceTransport | None = None,
    force_budget: bool = False,
) -> DispatchResult:
    """Run an agent with RunRecord lifecycle, retry loop, and cost tracking.

    This is the shared orchestration core used by both execute_dispatch
    (issue dispatches) and dispatch_review (PR review dispatches).  Callers
    handle their own prep (validation, worktree, prompt building); this
    function handles execution + tracking.

    ``contained_image`` swaps the EXECUTION step for a container running that
    image, and changes nothing else — retry, budget, breaker, pause, worker cap
    and cost accounting all still apply (#1462). The alternative, a parallel
    contained lifecycle, is what left contained runs with no per-run cost
    governor; the remediation flows would have lost ``max_remediation_usd``
    (#993) to it. None means host execution.

    ``inference_transport`` binds a flow's initial attestation to its launch.
    Sidecar attempts certify that route only after worker admission succeeds.

    ``worker_cap`` > 0 makes the initial ``pending`` row an atomic worker-slot
    admission (#745): the row is inserted only if the fleet is below the cap in
    the same transaction as the count. If the cap was reached, ``WorkerCapReached``
    is raised without writing a row. Direct commands pass their configured cap.

    The daily and weekly caps bind every run admitted here (#3205): unless the
    caller's ``admit_budget_check`` already gates the windows in the admission
    transaction, :func:`kew.budget.check_window_budget` does, reserving this
    run's ceiling (``budget_override_usd`` or ``agent.max_budget_usd``). A
    refusal raises :class:`BudgetExceeded` with no row written.
    ``force_budget`` is a hand-run command's ``--force-budget`` and drops it.

    ``on_admitting`` runs inside the admission transaction, even with no worker cap.
    Its exception rolls back the run, identity, and repair slot together.
    ``on_admitted`` runs once after admission commits and before any launch,
    receiving the admitted run id. A hook exception finalizes the row as refused
    and releases the worker slot, unless the hook started a tracker claim: that
    exception propagates so the claim owner records retention and terminalizes
    the row itself. The returned result retains ``launched=False`` for public
    exit code 2. ``on_launch`` runs once the paid child has started.
    Cancellation finalizes admission before propagating the original exception.
    Protection covers preparation through the handoff to ``run_agent``.

    The body is a straight-line pipeline of extracted phases: runner/model
    resolution (``_resolve_runner_and_model``) → pending-row admission
    (``_save_pending_run``) → breaker baseline (``_compute_breaker_baseline``) →
    retry loop (``_run_retry_loop``) → result finalization
    (``_finalize_result``). The loop may short-circuit to a terminal result via
    ``_RetryOutcome.early_return`` (exception path); otherwise its outcome is
    finalized. Observable behavior is unchanged — the phases only name the
    structure the single body already had.
    """
    _require_recorded_store(store, allow_unrecorded)
    _require_local_contained_placement(config, run_kind, contained_image)
    dispatch_item_id = item_id or str(issue_num)
    reservation = capture_dispatch_reservation(
        store,
        epic_ref,
        dispatch_item_id,
        run_kind,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )
    _validate_tracked_run_identity(
        validate=validate_continuation,
        store=store,
        epic_ref=epic_ref,
        item_id=dispatch_item_id,
        worktree_dir=worktree_dir,
        branch=branch,
        run_id=run_id,
        run_kind=run_kind,
    )
    run_id = run_id or str(uuid.uuid4())
    dispatch_start = time.monotonic()

    # Resolve runner/model if not provided by caller
    runner, effective_model = _resolve_runner_and_model(config, runner, model)
    _require_public_route_model(effective_model)
    transport = _admit_tracked_transport(
        config, runner, effective_model, inference_transport, transport_preflight
    )
    effective_model = transport.resolved.model
    _require_public_route_model(effective_model)

    # Audit envelope: who dispatched, which repo, which configured endpoint.
    # Resolved once here; the run row records the dispatcher (audit events
    # carry their own actor — the approver/stopper may differ).
    _actor = resolve_actor()
    _endpoint = configured_endpoint(runner.name, effective_model) or None
    # Resolved from the project origin when config leaves it "auto-detected":
    # a canonical publication never reads its worktree's origin, so this row
    # is the only repository binding it gets (#2097).
    _repo = _project_repo_slug(config)

    # Pre-compute the log file path so we can record it on the initial
    # RunRecord save. The TUI needs log_file set while the run is still
    # "pending" so it can locate the companion .events.jsonl file.
    _ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    _log_dir = Path(config.logging.dir).resolve()
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_item_id = _dispatch_log_item_id(issue_num, item_id)
    _log_file = _log_dir / f"dispatch-{_log_item_id}-{_ts}.log"

    run = _TrackedRun(
        config=config,
        store=store,
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        branch=branch,
        title=title,
        complexity=complexity,
        run_kind=run_kind,
        loop_id=loop_id,
        prefix_key=prefix_key,
        provider=provider,
        runner=runner,
        effective_model=effective_model,
        actor=_actor,
        endpoint=_endpoint,
        repo=_repo,
        log_file=_log_file,
        dispatch_start=dispatch_start,
        transport=transport,
        transport_health_check=transport_health_check,
        map_tokens=map_tokens,
        lesson_corpus_tokens=lesson_corpus_tokens,
        self_check=self_check,
        prompt_profile_version_id=prompt_profile_version_id,
        source_tracker=source_tracker,
        contained_image=contained_image,
        sandbox_posture=sandbox_posture,
        paid_launch_check=paid_launch_check,
        launch_state=launch_state,
        on_launch=on_launch,
        reservation=reservation,
        reservation_pinned=expected_reservation_run_id is not None,
    )

    # The initial posture is attested before the key is minted: every key
    # lifecycle event carries this posture, and the admitted row carries the
    # key. It therefore runs before admission, like transport preflight, and
    # the retry loop's attempt 1 reuses it (_initial_tracked_posture_recorded).
    if not transport.resolved.sidecar:
        _attest_tracked_posture(run, worktree_dir, attempt_number=1)
    with (
        PendingAdmission(store, run_id) as admission,
        execution_cancellation(admission),
        bounded_run_credentials(
            run_id=run_id,
            config=config,
            store=store,
            model=effective_model,
            credential_names=runner.required_credentials,
            timeout_seconds=run_timeout_seconds(config, timeout_override_minutes),
            retry=config.retry,
            limit_usd=budget_override_usd,
            direct=transport.resolved.mode == "direct",
            # This core drives both boundaries: `contained_image` is the one
            # signal that says which. Without it an uncontained run on the
            # OpenShell runtime would push its minted key into a gateway
            # provider while the agent runs on the host with the value in its
            # own environment.
            contained=contained_image is not None,
        ) as credential,
    ):
        run.admission = admission
        run.credential = credential
        # Initial pending row / atomic worker-slot admission (#745). The
        # dispatcher identity, the reservation binding and any receipt slot
        # commit in this one transaction, before the claim hook runs.
        try:
            _save_pending_run(
                run,
                worker_cap=worker_cap,
                cap_exclude_issue=cap_exclude_issue,
                admit_budget_check=_admission_budget_gate(
                    run,
                    admit_budget_check if worker_cap > 0 else None,
                    force_budget=force_budget,
                    ceiling_usd=(
                        budget_override_usd
                        if budget_override_usd is not None
                        else config.agent.max_budget_usd
                    ),
                ),
                on_admitting=on_admitting,
            )
        except HostIdentityUnavailable as exc:
            return _host_identity_refusal(run, exc, 0)
        refusal = _run_admission_hook(run, on_admitted)
        if refusal is not None:
            return refusal
        # The credential this dispatch launches under: a usage warning seen
        # under another one is for a token since replaced (#3329).
        record_runner_launch(store, runner.name)

        # Compute the circuit-breaker baseline once before the retry loop.
        anomaly_threshold_usd, anomaly_compaction_allowance_usd = (
            _compute_breaker_baseline(run)
        )

        outcome = _run_retry_loop(
            run,
            prompt=prompt,
            worktree_dir=worktree_dir,
            budget_override_usd=budget_override_usd,
            timeout_override_minutes=timeout_override_minutes,
            should_pause=should_pause,
            budget_check=budget_check,
            anomaly_threshold_usd=anomaly_threshold_usd,
            anomaly_compaction_allowance_usd=anomaly_compaction_allowance_usd,
        )
        if outcome.early_return is not None:
            return outcome.early_return

        admission.persist_receipt()
        return _finalize_result(run, outcome)


def _get_changed_files(
    worktree_dir: Path,
    base_branch: str,
    *,
    repo: str | None = None,
    base_sha: str | None = None,
    publication: CanonicalPublication | None = None,
    publish: str = "canonical",
    dangerously_skip_containment: bool = False,
) -> list[str]:
    """Return changed paths through the configured publication boundary."""
    if publication is not None:
        return list(publication.changed_paths)
    mode = select_publication_mode(
        publish,
        dangerously_skip_containment=dangerously_skip_containment,
    )
    if mode == "checkout":
        return _get_checkout_changed_files(worktree_dir, base_branch)
    try:
        remote = publication_remote(repo)
        with work_dirs.work_tempdir("kew-publication-diff-") as temp:
            scratch = Path(temp)
            trusted_base = base_sha or remote_branch_sha(
                remote, base_branch, scratch=scratch / "query"
            )
            if not trusted_base:
                raise PublicationError(
                    "push", f"no trusted base for branch {base_branch!r}"
                )
            return list(
                checkout_changed_paths(
                    worktree_dir,
                    remote=remote,
                    base_sha=trusted_base,
                    scratch=scratch / "walk",
                )
            )
    except Exception as exc:  # noqa: BLE001 — best-effort outcome detail
        _warn(
            redact_credentials(
                f"Could not inspect changed files without trusting checkout git: "
                f"{exception_text(exc)}"
            )
        )
    return []


def _get_checkout_changed_files(worktree_dir: Path, base_branch: str) -> list[str]:
    """Read changes through the temporary checkout-rooted escape hatch."""
    try:
        result = run(
            ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
            check=False,
            cwd=worktree_dir,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [path for path in result.stdout.strip().split("\n") if path]
    except Exception as exc:  # noqa: BLE001 - best-effort outcome detail
        _warn(
            redact_credentials(
                f"Could not inspect checkout changes: {exception_text(exc)}"
            )
        )
    return []


def _is_epic_labelled_dependent(dep_num: int, config: DispatchConfig) -> bool:
    """Return True if dependent *dep_num* carries an ``epic:`` label.

    Epic-DAG nodes are owned by the driver, not the fleet — the fleet must
    never auto-unblock them, even if they show up as a resolved dependent.
    Fails open (returns False) if labels cannot be fetched, matching the
    existing best-effort semantics of auto-unblock.
    """
    label_names = _fetch_issue_labels(dep_num, config)
    if not label_names:
        return False
    return _has_epic_label([{"name": name} for name in label_names])


def _auto_unblock_dependents(issue_num: int, config: DispatchConfig) -> None:
    """Best-effort auto-unblock of issues that depended on *issue_num*."""
    try:
        to_unblock = find_blocked_dependents(issue_num, config)
        for dep_num in to_unblock:
            try:
                if _is_epic_labelled_dependent(dep_num, config):
                    continue
                unblock_issue(dep_num, config)
                _logger.info(
                    "Auto-unblocked #%d (dependency #%d resolved)", dep_num, issue_num
                )
            except Exception as exc:
                _warn(
                    redact_credentials(
                        f"Failed to unblock #{dep_num}: {exception_text(exc)}"
                    )
                )
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Failed to find blocked dependents for #{issue_num}: "
                f"{exception_text(exc)}"
            )
        )


_GOVERNOR_ERROR = "governor_error"


def _run_governor(
    *,
    resolved: ResolvedRoute,
    complexity: str,
    config: DispatchConfig,
    store: RunStore,
    repo_map_hash: str,
    est_prompt_tokens: int | None = None,
) -> tuple[ResolvedRoute, GovernorDecision]:
    """Call the cache-economics governor.

    The caller attests the outcome with ``_emit_routing_decision`` once it
    knows the route that launches (containment may refuse a swap).
    ``est_prompt_tokens`` is derived from the actual rendered prompt on the real
    dispatch path (spec D2 / R1-8). Callers without an assembled prompt (the
    ``--dry-run`` preview) pass ``None`` and let the governor use its fallback.
    """
    from datetime import UTC, datetime

    ctx = PrefixKeyContext(repo=config.project.repo or "", repo_map_hash=repo_map_hash)

    warm_by_provider: dict[str, set[str]] = {}
    now = datetime.now(UTC)
    for prov in ("anthropic", "openai", "google"):
        try:
            warm = store.warm_prefixes(prov, now)
            if warm:
                warm_by_provider[prov] = warm
        except Exception:  # nosec B110 — best-effort warm-prefix lookup; missing → provider treated as cold
            pass

    # Fall back to the governor's documented estimate when the caller has no
    # assembled prompt (dry-run), keeping the constant single-sourced.
    _est_tokens = (
        DEFAULT_EST_PROMPT_TOKENS if est_prompt_tokens is None else est_prompt_tokens
    )
    try:
        new_resolved, decision = cache_governor(
            proposed=resolved,
            complexity=complexity,
            config=config,
            ctx=ctx,
            warm_prefixes_by_provider=warm_by_provider,
            est_prompt_tokens=_est_tokens,
        )
    except Exception as exc:
        _logger.warning(
            "Governor failed, using proposed route: %s",
            redact_credentials(exception_text(exc)),
        )
        return resolved, GovernorDecision(
            proposed_model=resolved.model,
            proposed_runner=getattr(resolved.runner, "name", ""),
            proposed_provider=infer_provider(
                resolved.model, getattr(resolved.runner, "name", "")
            ),
            recommended_model=resolved.model,
            recommended_runner=getattr(resolved.runner, "name", ""),
            recommended_provider=infer_provider(
                resolved.model, getattr(resolved.runner, "name", "")
            ),
            candidates=[],
            reason=_GOVERNOR_ERROR,
            applied=False,
        )

    return new_resolved, decision


def _emit_routing_decision(
    store: RunStore,
    issue_num: int,
    decision: GovernorDecision,
    run_id: str | None = None,
) -> None:
    """Attest the governor's decision once, for the route that launches.

    A governor that raised is attested too, as ``reason=governor_error`` with
    ``applied=False``: the run launched on the proposed route.

    ``run_id`` names the run this route carries (C3/#2300), so the run page can
    read the routing decision off the ``audit_events.run_id`` index with every
    other event of that run instead of guessing from the issue number — which
    cannot tell one attempt from the next. The ``--dry-run`` preview launches
    nothing and passes None. Old rows keep their null; nothing is rewritten.
    """
    from kew.audit import emit, resolve_actor

    try:
        emit(
            store,
            "routing_decision",
            actor=resolve_actor(),
            run_id=run_id,
            issue_num=issue_num,
            payload=decision.to_audit_payload(),
        )
    except Exception as exc:
        _logger.warning(
            "Failed to emit routing_decision audit event: %s",
            redact_credentials(exception_text(exc)),
        )


#: Model-override sources the ledger attests by name; any other path that
#: passes ``--model`` is the CLI flag itself.
_MODEL_OVERRIDE_SOURCES = frozenset({LAUNCHPAD_MODEL_SOURCE})
_CLI_MODEL_SOURCE = "kew dispatch --model"


def _override_decision(resolved: ResolvedRoute, model: str) -> GovernorDecision:
    """The routing decision for a dispatch whose model an operator named.

    ``proposed_*`` is the configured route; ``recommended_*`` is what launches —
    the same runner with the named model. The source is the drive's word for a
    launchpad request (:data:`kew.run_requests.MODEL_SOURCE_ENV`), else the
    ``--model`` flag.
    """
    runner_name = getattr(resolved.runner, "name", "")
    source = os.environ.get(MODEL_SOURCE_ENV, "")
    if source not in _MODEL_OVERRIDE_SOURCES:
        source = _CLI_MODEL_SOURCE
    return GovernorDecision(
        proposed_model=resolved.model,
        proposed_runner=runner_name,
        proposed_provider=infer_provider(resolved.model, runner_name),
        recommended_model=model,
        recommended_runner=runner_name,
        recommended_provider=infer_provider(model, runner_name),
        candidates=[],
        reason="model_override",
        applied=True,
        override={"model": model, "source": source},
    )


def _attest_model_override(ctx: _DispatchContext, resolved: ResolvedRoute) -> None:
    """Attest an operator-named model on the route that launches (#2741).

    The override bypasses the governor on the host path and the contained path
    has no governor at all, so either launch would otherwise leave no
    ``routing_decision`` saying which model ran or who chose it.
    """
    if ctx.model_override and ctx.store is not None:
        _emit_routing_decision(
            ctx.store,
            ctx.issue_num,
            _override_decision(resolved, ctx.model_override),
            run_id=ctx.run_id,
        )


def _containment_refused_decision(
    decision: GovernorDecision, refusal: str
) -> GovernorDecision:
    """The decision as launched: the proposed route, not the refused swap."""
    return replace(
        decision,
        recommended_model=decision.proposed_model,
        recommended_runner=decision.proposed_runner,
        recommended_provider=decision.proposed_provider,
        est_savings_usd=0.0,
        reason="containment_refused",
        applied=False,
        containment_refusal=refusal,
    )


def _materialize_checkout(
    item_id: str, branch: str, config: DispatchConfig, *, base_sha: str = ""
) -> Path:
    """Create a standalone git checkout for sandboxed dispatch.

    Local clone into a directory under the work root (not a linked worktree)
    so the container gets a self-contained .git, never seeing the main repo's
    worktrees. The directory carries the branch-safe work-item slug so a
    Linear id (``sfd-12``) stays inside the filesystem-path charset.
    """
    repo_root = Path(
        run(["git", "rev-parse", "--show-toplevel"], check=True).stdout.strip()
    )

    checkout_dir = work_dirs.make_work_dir(f"kew-sandbox-{branch_slug(item_id)}-")
    claim_checkout(checkout_dir)

    try:
        base_branch = config.branches.base or "main"
        # Clone from the local repo + repoint origin at the upstream GitHub URL (so
        # the host's `git push` + `gh pr create` reach GitHub, not the clone source)
        # via the ONE shared worktree primitive the reviewer also uses (Wave 2).
        worktree.clone_local_checkout(repo_root, checkout_dir, base_branch)
        # Name and refresh the TRUE upstream base before cutting the agent branch
        # (#749). The narrow clone has the bootstrap commit and history but carries
        # no source refs. This fetch creates origin/<base> and catches an upstream
        # move between the bootstrap transfer and branch creation.
        worktree.fetch_ref(checkout_dir, base_branch, repo_root=repo_root)
        if base_sha:
            # Canonical publication diffs the checkout against this exact base, so
            # the agent starts on it even when the remote advanced after the
            # query, and never on rewritten history (review round 14).
            worktree.require_base_fetched(checkout_dir, base_sha, base_branch)
            start_point = base_sha
        else:
            start_point = _agent_branch_start_point(
                checkout_dir, branch, base_branch, repo_root=repo_root
            )
        run(
            ["git", "checkout", "-b", branch, start_point],
            cwd=checkout_dir,
            check=True,
        )
        worktree.write_checkout_generation(checkout_dir)
        _write_prompt_exclude(checkout_dir)
        return checkout_dir
    except BaseException:
        worktree.remove_checkout_tree(checkout_dir)
        raise


def _agent_branch_start_point(
    checkout_dir: Path, branch: str, base_branch: str, *, repo_root: Path
) -> str:
    """Pick the ref to cut the agent branch from at materialization.

    Reuse an existing remote agent branch only when it carries commits ahead
    of the default branch — real work salvaged from a prior attempt. A branch
    with zero ahead-commits is a stale, zero-work leftover (#743): a failed
    first attempt cut it from a since-superseded base, so reusing it as-is only
    preserves staleness. Reset those onto the freshly fetched ``origin/<base>``
    so a retry builds on current main, not the stale checkout.
    """
    base_ref = f"origin/{base_branch}"
    exists = run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=repo_root,
        check=False,
    )
    if exists.returncode != 0:
        return base_ref
    worktree.fetch_ref(checkout_dir, branch, repo_root=repo_root)
    ahead = run(
        ["git", "rev-list", "--count", f"{base_ref}..FETCH_HEAD"],
        cwd=checkout_dir,
        check=True,
    ).stdout.strip()
    return "FETCH_HEAD" if ahead not in ("", "0") else base_ref


def _write_prompt_exclude(checkout_dir: Path) -> None:
    """Exclude the mounted prompt handoff from version control (#725).

    Writes a worktree-local exclude (``.git/info/exclude``) — NOT the tracked
    ``.gitignore`` — so an agent's ``git add -A`` never commits
    ``.kew-prompt.txt`` into its PR branch, and the exclusion holds for
    arbitrary user repos that don't ignore it themselves.
    """
    exclude_file = checkout_dir / ".git" / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    with exclude_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{_SANDBOX_PROMPT_FILENAME}\n")


def _cleanup_dispatch_dir(path: Path, *, sandboxed: bool, branch: str) -> None:
    """Remove a dispatch's working directory.

    Every dispatch checkout is a standalone clone with its own ``.git``.
    Sandboxed clones live in temporary directories. Host clones live under
    ``branches.worktree_base``. Each cleanup path removes its clone.

    *branch* is the name kew created for this dispatch, threaded from the
    caller rather than read back out of *path*: the agent has written that
    directory, and a branch name taken from it fed a destructive host
    ``git branch -D`` (#1445).
    """
    if sandboxed:
        worktree.remove_checkout_tree(path)
        # The OpenShell runtime downloads the agent's work into a sibling of
        # this directory (``openshell_checkout.realized_checkout_dir``) and
        # publishes THAT. It is derived from this path rather than remembered,
        # so it is removed here whichever runtime ran: on every other one the
        # sibling never existed, and removing an absent tree is a no-op.
        from kew.openshell_checkout import realized_checkout_dir

        worktree.remove_checkout_tree(realized_checkout_dir(path))
    else:
        cleanup_worktree(path, branch)


def _extract_pr_url(stdout: str) -> str | None:
    """Pull the PR URL out of ``gh pr create`` stdout (it prints the URL)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("https://") and "/pull/" in line:
            return line
    return None


def _persist_pr_url(
    store: RunStore | None, run_id: str | None, url: str | None
) -> None:
    """Persist a PR URL onto its run record immediately (#648). Fail-soft.

    Captures PR identity at creation so a killed run can never leave an
    untracked orphan — no migration, ``pr_url`` already exists. A store/db
    hiccup must never derail the dispatch, so every failure is swallowed.
    """
    if store is None or not run_id or not url:
        return
    try:
        record = store.get(run_id)
        if record is not None and record.pr_url != url:
            record.pr_url = url
            store.save(record)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail dispatch
        _logger.debug(
            "pr_url persist failed for run %s: %s",
            run_id,
            redact_credentials(exception_text(exc)),
        )


RUN_REPORT_MARKER_PREFIX = "<!-- kew-dispatch-report run="


def _run_report_marker(run_id: str) -> str:
    """The HTML comment that ties a posted Dispatch Report to its run (#1761)."""
    safe = re.sub(r"[^A-Za-z0-9._:-]", "", run_id)
    return f"{RUN_REPORT_MARKER_PREFIX}{safe} -->"


#: Where any kew HTML marker in agent text opens: ``<!-- kew-review-verdict -->``,
#: a run's report marker, and every other ``<!--\s*kew`` comment kew reads.
_KEW_MARKER_OPEN = re.compile(r"<!--(?=\s*kew)", re.IGNORECASE)
#: Where report text would start markup that pings someone or leaves kew's
#: quote: a mention (``@`` before a name), raw HTML or an autolink (``<``
#: before a letter, ``/``, ``!`` or ``?``), a footnote (``[^``, which GitHub
#: renders below the comment), or an alert (``[!IMPORTANT]``, which would dress
#: report text as a GitHub alert box inside kew's quote). Broken on every line,
#: code included: kew models no code fence, because wherever such a model and
#: GitHub's parser disagree, live markup would get through; a zero-width space
#: is invisible in code too.
_ACTIVE_MARKUP = re.compile(r"(?<=@)(?=[\w-])|(?<=<)(?=[A-Za-z/!?])|(?<=\[)(?=[\^!])")


def _report_run(run_id: str) -> str:
    """*run_id* as the banner and closing line name it."""
    return re.sub(r"[^A-Za-z0-9._:-]", "", run_id) or "unknown"


#: How kew's posted agent report opens (:func:`_agent_report_banner`).
AGENT_REPORT_BANNER_PREFIX = "**Agent-authored report** from kew run `"


def _agent_report_banner(run_id: str) -> str:
    """The host-authored line above an agent's report, naming its run."""
    return (
        f"{AGENT_REPORT_BANNER_PREFIX}{_report_run(run_id)}`. The "
        "dispatch agent wrote the quoted text below; kew posts it as untrusted "
        "and acts on none of it."
    )


def _agent_report_footer(run_id: str) -> str:
    """The host-authored line below an agent's report: where its text ends."""
    return f"**End of the agent-authored report** from kew run `{_report_run(run_id)}`."


def _quoted_report(report: str) -> str:
    """*report* as one blockquote that no report text can leave or act from.

    Every line is quoted — any line break (``\\r``, ``\\u2028``) included —
    so all of the report renders inside the quote and none beside kew's own
    lines. A kew marker gets a zero-width space after its ``<!--``, so no
    marker parser reads it, and every mention, tag, footnote and alert gets one too
    (:data:`_ACTIVE_MARKUP`), on every line.
    """
    lines: list[str] = []
    for raw in report.splitlines():
        line = _KEW_MARKER_OPEN.sub(f"<!--{ZERO_WIDTH_SPACE}", raw)
        line = _ACTIVE_MARKUP.sub(ZERO_WIDTH_SPACE, line)
        lines.append(f"> {line}" if line.strip() else ">")
    return "\n".join(lines)


def _agent_report_comment(run_id: str, report: str) -> str:
    """The PR comment for an agent's *report*: kew's banner, the quoted
    report, kew's closing line, and the run's marker."""
    parts = (
        _agent_report_banner(run_id),
        _quoted_report(report),
        _agent_report_footer(run_id),
        _run_report_marker(run_id),
    )
    return "\n\n".join(parts) + "\n"


def _pr_comment_bodies(pr_num: int | str, config: DispatchConfig) -> list[str]:
    """The PR's comment bodies, one entry per comment.

    Any gh failure or unreadable output returns no bodies fail-soft: a failed
    check just means kew posts its report, and a duplicate report is far
    better than a silent gap (#664).
    """
    result = run(
        [
            "gh",
            "pr",
            "view",
            str(pr_num),
            "--repo",
            config.project.repo,
            "--json",
            "comments",
            "--jq",
            ".comments[].body | @json",
        ],
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        bodies = [json.loads(line) for line in (result.stdout or "").splitlines()]
    except ValueError as exc:
        _warn(f"Could not read the comments on PR {pr_num}: {exception_text(exc)}")
        return []
    return [body for body in bodies if isinstance(body, str)]


def _dispatch_report_exists(
    pr_num: int | str, config: DispatchConfig, marker: str
) -> bool:
    """True when a comment carrying the run *marker* already exists on the PR."""
    return any(marker in body for body in _pr_comment_bodies(pr_num, config))


def _run_report_on_pr(pr_num: int, config: DispatchConfig, run_id: str) -> bool:
    """True when the PR already holds a Dispatch Report for this run.

    That is a comment carrying this run's marker, or a report with no kew run
    marker at all: the agent's own ``gh pr comment``. Another run's marked
    report does not count, so a re-dispatch to an existing PR still gets its
    own report.
    """
    marker = _run_report_marker(run_id)
    return any(
        marker in body
        or (_DISPATCH_REPORT_MARKER in body and RUN_REPORT_MARKER_PREFIX not in body)
        for body in _pr_comment_bodies(pr_num, config)
    )


def _flagged_anomaly_note(result: DispatchResult) -> str:
    """One sentence for a run flagged on its closing line (#1798)."""
    threshold = result.breaker_threshold_usd or 0.0
    return (
        f"Cost anomaly: ${result.estimated_cost_usd:.2f} crossed the "
        f"${threshold:.2f} cost-anomaly threshold on the line that ended the "
        "run, so kew flagged it and kept the work for review instead of "
        "stopping it."
    )


def _fallback_report_body(
    item_id: str,
    complexity: str,
    result: DispatchResult,
    *,
    selected_model: str | None = None,
) -> str:
    """Machine-generated Dispatch Report kew posts when the agent didn't.

    Built from run telemetry, not the agent's own account — it flags that the
    agent never posted one and points the reviewer at the diff.
    """
    telemetry = [
        f"- Model: `{result.resolved_model or selected_model or 'unknown'}`",
        f"- Estimated cost: ${result.estimated_cost_usd:.2f}",
        f"- {result.total_tokens:,} tokens ({result.tool_uses} tool uses)",
        f"- Duration: {result.duration_seconds:.0f}s",
    ]
    if result.selected_tier:
        telemetry.append(f"- Switchyard tier: `{result.selected_tier}`")
    if result.requested_service_tier:
        telemetry.append(f"- Requested service tier: `{result.requested_service_tier}`")
    if result.reported_service_tier:
        telemetry.append(f"- Reported service tier: `{result.reported_service_tier}`")
    if result.breaker_after_terminal:
        telemetry.append(f"- {_flagged_anomaly_note(result)}")
    if result.classifier_overhead_model:
        telemetry.append(
            f"- Classifier overhead: `{result.classifier_overhead_model}` "
            f"({result.classifier_overhead_input_tokens:,} input / "
            f"{result.classifier_overhead_output_tokens:,} output tokens; "
            f"${result.classifier_overhead_cost_usd:.4f})"
        )
    return redact_credentials(
        f"{_DISPATCH_REPORT_MARKER}\n\n"
        f"**Issue:** {item_ref(item_id)}\n"
        f"**Complexity:** {complexity}\n\n"
        "### What was done\n"
        "_The agent did not post a Dispatch Report before its run ended, so "
        "kew generated this fallback from the run record. Review the diff "
        "directly — this reflects run telemetry, not the agent's own account "
        "of its work._\n\n"
        "### Run telemetry\n" + "\n".join(telemetry) + "\n\n"
        f"{_run_report_marker(result.run_id)}\n"
    )


def _post_dispatch_report_fallback(
    pr_num: int,
    item_id: str,
    complexity: str,
    result: DispatchResult,
    config: DispatchConfig,
    *,
    selected_model: str | None = None,
) -> None:
    """Post a machine-generated Dispatch Report when the agent posted none (#664).

    A headless run can finalize ``success`` while a background wrap-up task —
    the agent's own Dispatch Report comment — silently never ran (#663). After
    the PR is confirmed, kew fills that gap: if this run has no report on the
    PR (``_run_report_on_pr``), it posts a fallback built from the run record.
    Skips when the agent already posted, and swallows every gh error fail-soft
    — reporting must never derail a completed dispatch. A report this run already posted
    (``result.dispatch_report_posted``) skips the post without reading GitHub.
    """
    if result.dispatch_report_posted:
        return
    try:
        if _run_report_on_pr(pr_num, config, result.run_id):
            return
        posted = run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_num),
                "--repo",
                config.project.repo,
                "--body",
                _fallback_report_body(
                    item_id,
                    complexity,
                    result,
                    selected_model=selected_model,
                ),
            ],
            check=False,
            timeout=30,
        )
        if posted.returncode != 0:
            _warn(
                redact_credentials(
                    f"Could not post fallback Dispatch Report to PR #{pr_num}: "
                    + redact_credentials(posted.stderr or posted.stdout or "").strip()[
                        :200
                    ]
                )
            )
        else:
            result.dispatch_report_posted = True
            _log(
                f"Posted fallback Dispatch Report to PR #{pr_num} (agent posted none)."
            )
    except Exception as exc:  # noqa: BLE001 — reporting must not fail dispatch
        _logger.debug(
            "fallback report post failed for PR #%s: %s",
            pr_num,
            redact_credentials(exception_text(exc)),
        )


def _extract_sandbox_report(log_file: Path) -> str | None:
    """The agent's own Dispatch Report for the run that wrote *log_file*.

    The sandboxed prompt tells the agent to END its run with the structured
    report as its final message — it cannot post a comment itself (no gh, no
    GitHub token in the container). That final message is now captured while
    the stream is being read and stored, bounded and cleaned, in the run's
    derived-evidence sidecar; this reads it back from there.

    It no longer opens the log. A run whose sidecar is missing — anything
    recorded before I06T — returns None and takes the #664 machine fallback,
    which is the honest answer: the report is unavailable, and re-parsing the
    raw transcript to find it is exactly the free-text re-ingest that landed
    private reasoning in a PR comment's blast radius.
    """
    return agent_report_for(log_file)


def _post_agent_report(
    pr_url: str | None,
    log_file: Path,
    config: DispatchConfig,
    *,
    result: DispatchResult | None = None,
) -> bool:
    """Publish the sandboxed agent's own Dispatch Report on its PR (#758).

    The template's contract ("the host publishes the branch, PR, and this
    report after the run") had no host-side implementation — every contained
    run fell through to the machine fallback. Fail-soft throughout: reporting
    must never derail a completed dispatch.

    Returns whether this run's report is on the PR, and records that on
    *result* so a later step of the same run posts nothing (#1761). The body
    carries the run's marker; a comment already carrying it skips the post.
    """
    if result is not None and result.dispatch_report_posted:
        return True
    if pr_url is None:
        return False
    run_id = result.run_id if result is not None else ""
    marker = _run_report_marker(run_id)
    try:
        report = _extract_sandbox_report(log_file)
        if report is None:
            return False  # no report in the stream — the #664 fallback covers it
        if run_id and _dispatch_report_exists(pr_url, config, marker):
            _log(f"Agent Dispatch Report already on {pr_url}; not posting again.")
            return _record_report_posted(result)
        body = _agent_report_comment(run_id, report)
        if not _comment_agent_report(pr_url, body, config):
            return False
        return _record_report_posted(result)
    except Exception as exc:  # noqa: BLE001 — reporting must not fail dispatch
        _warn(
            redact_credentials(
                f"Could not post agent Dispatch Report to {pr_url}: "
                + exception_text(exc)[:200]
            )
        )
        return False


def _comment_agent_report(pr_url: str, body: str, config: DispatchConfig) -> bool:
    """Post *body* as a PR comment; warn and return False when gh refuses."""
    posted = run(
        [
            "gh",
            "pr",
            "comment",
            pr_url,
            "--repo",
            config.project.repo,
            "--body",
            body,
        ],
        check=False,
        timeout=30,
    )
    if posted.returncode != 0:
        _warn(
            redact_credentials(
                f"Could not post agent Dispatch Report to {pr_url}: "
                + redact_credentials(posted.stderr or posted.stdout or "").strip()[:200]
            )
        )
        return False
    _log(f"Posted agent Dispatch Report to {pr_url}.")
    return True


def _record_report_posted(result: DispatchResult | None) -> bool:
    """Mark this run's Dispatch Report as on its PR; always True."""
    if result is not None:
        result.dispatch_report_posted = True
    return True


def _git_push_branch(
    branch: str,
    work_dir: Path,
    *,
    repo: str | None = None,
    base_sha: str | None = None,
    base_branch: str | None = None,
    message: str = "kew: publish agent work",
    runtime: str | None = None,
    image: str | None = None,
    proxy_environment: str = "none",
    publish: str = "canonical",
    dangerously_skip_containment: bool = False,
    max_attempts: int = 3,
    delays: tuple[float, ...] = (20.0, 40.0),
    _sleep: Callable[[float], None] = time.sleep,
    on_no_change: Callable[[CanonicalNoChange], None] | None = None,
    store: RunStore | None = None,
    run_id: str | None = None,
    item_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> CanonicalPublication | CanonicalNoChange | None:
    """Push branch, retrying with bounded backoff for transient SSH/network errors.

    On final failure the exception message includes ``work_dir`` so operators
    can manually salvage the preserved checkout. ``before_push`` runs only when
    a head will be pushed, never for a no-change publication (#2033).
    """
    if store is not None and run_id:
        ensure_publication_credentials(
            store, run_id=run_id, policy=require_publication_credentials
        )
    mode = select_publication_mode(
        publish,
        dangerously_skip_containment=dangerously_skip_containment,
        store=store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        repo=repo,
    )
    if mode == "canonical":
        return _canonical_push_branch(
            branch,
            work_dir,
            repo=repo,
            base_sha=base_sha,
            message=message,
            runtime=runtime,
            image=image,
            proxy_environment=proxy_environment,
            max_attempts=max_attempts,
            delays=delays,
            sleep=_sleep,
            on_no_change=on_no_change,
            store=store,
            run_id=run_id,
            item_id=item_id,
            before_push=before_push,
        )
    _checkout_push_branch(
        branch,
        work_dir,
        repo=repo,
        base_sha=base_sha,
        base_branch=base_branch,
        max_attempts=max_attempts,
        delays=delays,
        sleep=_sleep,
        store=store,
        run_id=run_id,
        before_push=before_push,
    )
    return None


def _checkout_push_branch(
    branch: str,
    work_dir: Path,
    *,
    repo: str | None,
    base_sha: str | None,
    base_branch: str | None,
    max_attempts: int,
    delays: tuple[float, ...],
    sleep: Callable[[float], None],
    store: RunStore | None,
    run_id: str | None,
    before_push: Callable[[], object] | None = None,
) -> None:
    """Run the temporary checkout-rooted publication escape hatch."""

    head_sha = _guard_checkout_output(
        work_dir,
        branch,
        repo=repo,
        base_sha=base_sha,
        base_branch=base_branch,
        store=store,
        run_id=run_id,
    )
    if before_push is not None:
        before_push()
    last_result = None
    remote, push_env = _checkout_push_target(work_dir, repo)
    refspec = f"{head_sha}:refs/heads/{branch}"
    for attempt in range(max_attempts):
        try:
            last_result = run(
                ["git", "push", "--no-follow-tags", remote, refspec],
                cwd=work_dir,
                check=False,
                env=push_env,
            )
        except Exception as exc:
            raise PublicationError("push", exc) from exc
        if last_result.returncode == 0:
            return
        if attempt < max_attempts - 1:
            sleep(delays[attempt])
    assert last_result is not None
    output = redact_credentials(last_result.stderr or last_result.stdout or "").strip()[
        :300
    ]
    raise PublicationError(
        "push",
        f"git push failed after {max_attempts} attempts for branch {branch!r} "
        f"(checkout preserved at {work_dir} — push manually to salvage): {output}",
        host_paths=(work_dir,),
    )


def _checkout_push_target(
    work_dir: Path, repo: str | None
) -> tuple[str, dict[str, str]]:
    """The escape hatch's remote, and a push environment that reads objects
    exactly as the pre-push scan did (no replace refs, grafts, or hooks)."""
    publication_env = publication_git_env()
    remote = (
        f"https://{repo_host(work_dir)}/{repo}.git"
        if publication_env is not None and repo
        else "origin"
    )
    push_env = worktree.checkout_object_env(
        publication_env if publication_env is not None else os.environ
    )
    return remote, push_env


def _guard_checkout_output(
    work_dir: Path,
    branch: str,
    *,
    repo: str | None,
    base_sha: str | None,
    base_branch: str | None,
    store: RunStore | None,
    run_id: str | None,
) -> str:
    """Scan every object the escape hatch will push; return the scanned head.

    A continuation records no base, so the base then comes from the canonical
    remote's ``base_branch``, never from the untrusted checkout's refs.
    """
    head = run(
        worktree._checkout_read_command(
            work_dir, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
        ),
        check=False,
        env=worktree.checkout_object_env(),
    )
    head_sha = head.stdout.strip() if head.returncode == 0 else ""
    if head_sha and not base_sha:
        base_sha = _remote_base_sha(repo, base_branch)
    if not base_sha or not head_sha:
        raise PublicationError(
            "push",
            f"checkout publication of {branch!r} has no trusted base and head to "
            f"scan for credentials (checkout preserved at {work_dir})",
            host_paths=(work_dir,),
        )
    _require_base_in_checkout(work_dir, branch, base_sha)
    publication_output_guard(store, run_id=run_id)(
        OutputScanTarget(
            repository=work_dir,
            base_sha=base_sha,
            head_sha=head_sha,
            env=worktree.checkout_object_env(),
        )
    )
    filter_checkout_range(
        work_dir,
        base_sha=base_sha,
        head_sha=head_sha,
        env=worktree.checkout_object_env(),
    )
    return head_sha


def _require_base_in_checkout(work_dir: Path, branch: str, base_sha: str) -> None:
    """Refuse, and say what to do, when the scan base is absent from the checkout.

    Scanning needs the base object to know what the push leaves out. Once the
    canonical base moves, a continuation's checkout usually lacks the new tip;
    kew neither fetches into the agent checkout nor scans against its refs.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        return  # the scan itself refuses a base that is not a full object id
    present = run(
        worktree._checkout_read_command(
            work_dir, "cat-file", "-e", f"{base_sha}^{{commit}}"
        ),
        check=False,
        env=worktree.checkout_object_env(),
    )
    if present.returncode == 0:
        return
    raise PublicationError(
        "push",
        f"checkout publication of {branch!r} is refused because base commit "
        f"{base_sha} is not in the checkout, so its output cannot be scanned for "
        f"credentials (checkout preserved at {work_dir}). "
        "Publish canonically (the default: [publication] "
        'publish = "canonical"), '
        "or re-run the continuation from a fresh base.",
        host_paths=(work_dir,),
    )


def _remote_base_sha(repo: str | None, base_branch: str | None) -> str:
    """Query the canonical remote's base head from a host-owned scratch dir."""
    with work_dirs.work_tempdir("kew-publication-base-") as temp:
        return remote_branch_sha(
            publication_remote(repo),
            base_branch or "main",
            scratch=Path(temp) / "query",
        )


def _canonical_push_branch(
    branch: str,
    work_dir: Path,
    *,
    repo: str | None,
    base_sha: str | None,
    message: str,
    runtime: str | None,
    image: str | None,
    proxy_environment: str,
    max_attempts: int,
    delays: tuple[float, ...],
    sleep: Callable[[float], None],
    on_no_change: Callable[[CanonicalNoChange], None] | None,
    store: RunStore | None = None,
    run_id: str | None = None,
    item_id: str | None = None,
    before_push: Callable[[], object] | None = None,
) -> CanonicalPublication | CanonicalNoChange:
    remote = publication_remote(repo)
    with work_dirs.work_tempdir("kew-canonical-publication-") as temp:
        scratch = Path(temp)
        if not base_sha:
            raise PublicationError(
                "push",
                f"no trusted base recorded for {branch!r}; a dispatch cannot "
                "prove ownership of a remote branch without one (checkout "
                f"preserved at {work_dir})",
                host_paths=(work_dir,),
            )
        existing = remote_branch_sha(remote, branch, scratch=scratch / "query")
        owner = _publication_owner(
            store,
            run_id=run_id,
            item_id=item_id,
            branch=branch,
            checkout=work_dir,
            base_sha=base_sha,
        )
        if existing:
            # An existing head must belong to this checkout generation and
            # explicit run lineage, including its original trusted base.
            _require_own_publication(
                store,
                existing=existing,
                branch=branch,
                item_id=item_id,
                work_dir=work_dir,
                owner=owner,
            )
        parent = existing or base_sha
        lease = f"{branch}:{existing}"

        def _bind_intent(commit_sha: str, tree_sha: str) -> None:
            _record_publication_intent(
                store,
                run_id=run_id,
                item_id=item_id,
                branch=branch,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                parent_sha=parent,
                owner=owner,
            )

        try:
            publication = publish_checkout(
                work_dir,
                remote=remote,
                branch=branch,
                parents=(parent,),
                base_sha=parent,
                message=message,
                identity=host_commit_identity(),
                force_with_lease=lease,
                runtime=runtime,
                image=image,
                proxy_environment=proxy_environment,
                acceptance=acceptance_contracts(store, item_id),
                on_intent=_bind_intent,
                before_push=before_push,
                output_guard=publication_output_guard(store, run_id=run_id),
                push_attempts=max_attempts,
                push_delays=delays,
                _sleep=sleep,
                scratch=scratch / "publish",
            )
            _deliver_dispatch_no_change(
                publication, base_sha=base_sha, callback=on_no_change
            )
            return publication
        except PublicationHeld:
            raise
        except PublicationError as exc:
            if exc.step != "push":
                raise
            raise PublicationError(
                "push",
                f"canonical push failed after {max_attempts} attempts for branch "
                f"{branch!r} (checkout preserved at {work_dir}): {exception_text(exc)}",
                host_paths=(work_dir,),
            ) from exc


_PUBLICATION_INTENT_EVENT = "publication_intent"
_PUBLICATION_BASE_EVENT = "publication_base"
_PUBLICATION_LINEAGE_EVENT = "publication_lineage"


def _publication_owner(
    store: RunStore | None,
    *,
    run_id: str | None,
    item_id: str | None,
    branch: str,
    checkout: Path,
    base_sha: str,
) -> dict[str, str]:
    """Identify the checkout and explicit run lineage allowed to resume a head."""
    generation = _checkout_generation(checkout)
    if not run_id or not generation:
        raise PublicationError(
            "push", "publication requires a run and checkout generation"
        )
    owner = {
        "branch": branch,
        "checkout": _checkout_key(checkout),
        "checkout_id": generation,
        "base_sha": base_sha,
    }
    owner["lineage_run_id"] = _publication_lineage(
        store,
        run_id=run_id,
        item_id=item_id,
        owner=owner,
    )
    return owner


def _publication_event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            _warn(f"publication event {event.id} carries unreadable payload")
            return {}
    return payload if isinstance(payload, dict) else {}


def _publication_lineage(
    store: RunStore | None,
    *,
    run_id: str,
    item_id: str | None,
    owner: dict[str, str],
) -> str:
    """Read the newest exact continuation binding; fresh runs own only themselves."""
    if store is None:
        return run_id
    newest = -1
    lineage = run_id
    for event in store.list_audit_events(limit=None, event=_PUBLICATION_LINEAGE_EVENT):
        if event.run_id != run_id or event.item_id != item_id:
            continue
        payload = _publication_event_payload(event)
        if not _publication_checkout_owner_matches(payload, owner):
            continue
        original = payload.get("lineage_run_id")
        if isinstance(original, str) and original and event.id > newest:
            newest, lineage = event.id, original
    return lineage


def _publication_checkout_owner_matches(
    payload: dict[str, Any], owner: dict[str, str]
) -> bool:
    if "checkout" in owner:
        recorded_checkout = payload.get("checkout")
        if not isinstance(recorded_checkout, str) or not continuation_checkout_matches(
            recorded_checkout, owner["checkout"]
        ):
            return False
    return all(
        payload.get(key) == value for key, value in owner.items() if key != "checkout"
    )


def _bind_publication_lineage(ctx: _DispatchContext, record: Any, base: str) -> None:
    """A currently claimed continuation explicitly inherits its original run."""
    if (
        not base
        or record.state != "continuing"
        or record.continuation_run_id != ctx.run_id
    ):
        return
    assert ctx.worktree_dir is not None
    bind_publication_lineage(
        ctx.store,
        run_id=ctx.run_id,
        lineage_run_id=record.original_run_id,
        item_id=ctx.item_id,
        branch=ctx.branch,
        checkout=ctx.worktree_dir,
        base_sha=base,
    )


def bind_publication_lineage(
    store: RunStore | None,
    *,
    run_id: str,
    lineage_run_id: str,
    item_id: str,
    branch: str,
    checkout: Path,
    base_sha: str,
) -> None:
    """Bind a continuation run to one canonical publication owner.

    Dispatch continuations and loop continuations share this durable check.
    The binding must round-trip before publication can use it.
    """
    owner = _publication_owner(
        store,
        run_id=lineage_run_id,
        item_id=item_id,
        branch=branch,
        checkout=checkout,
        base_sha=base_sha,
    )
    audit.emit(
        store,
        _PUBLICATION_LINEAGE_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        item_id=item_id,
        payload=owner,
    )
    if (
        _publication_owner(
            store,
            run_id=run_id,
            item_id=item_id,
            branch=branch,
            checkout=checkout,
            base_sha=base_sha,
        )
        != owner
    ):
        raise PublicationError("push", "publication lineage was not durably recorded")


def _record_publication_intent(
    store: RunStore | None,
    *,
    run_id: str | None,
    item_id: str | None,
    branch: str,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    owner: dict[str, str] | None = None,
) -> None:
    """Bind the commit this publisher is about to push to its run, durably.

    Item scope uses ``scope = "item"`` and the tracker item as the audit key.
    Branch scope uses ``scope = "branch"`` and the PR head branch as the key.
    Publishers without a tracker item use branch scope, never the PR number.
    The audit envelope stores either key in ``item_id``.

    Written before the push. A retry that finds the branch already on the
    remote resumes only when the head is one of these commits; any other head
    is a stale branch from some other dispatch and is refused (review round 3).
    """
    if (
        store is None
        or not (item_id or branch)
        or getattr(store, "append_audit_event", None) is None
    ):
        raise PublicationError(
            "push",
            f"no durable store for the publication intent of {branch!r} "
            f"({commit_sha[:12]}); refusing to push what a retry could not "
            "recognise as its own",
        )
    from kew.audit import Actor, emit

    scope = "item" if item_id else "branch"
    scope_key = item_id or branch
    emit(
        store,
        _PUBLICATION_INTENT_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        item_id=scope_key,
        payload={
            "branch": branch,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "parent_sha": parent_sha,
            **(owner or {}),
            "scope": scope,
        },
        dedup_key=f"publication-intent:v1:{run_id}:{branch}:{commit_sha}",
    )
    # emit is best-effort by design; the intent is not, so read back THIS
    # run's own event. Commit membership alone is not ownership: another run's
    # intent for the same commit under the same item and branch would satisfy
    # it, and a dropped append would then push under evidence naming that run.
    if not _own_publication_intent_recorded(
        store,
        run_id=run_id,
        item_id=scope_key,
        branch=branch,
        commit_sha=commit_sha,
        owner=owner,
        scope=scope,
    ):
        raise PublicationError(
            "push",
            f"the publication intent for {branch!r} ({commit_sha[:12]}) was not "
            "durably recorded; refusing to push",
        )


def _own_publication_intent_recorded(
    store: RunStore,
    *,
    run_id: str | None,
    item_id: str,
    branch: str,
    commit_sha: str,
    owner: dict[str, str] | None,
    scope: str = "item",
) -> bool:
    """Whether a host-publication intent of *run_id* names *commit_sha*."""
    events = store.list_audit_events(
        limit=None, event=_PUBLICATION_INTENT_EVENT, item_id=item_id
    )
    return any(
        getattr(event, "run_id", None) == run_id
        and (getattr(event, "actor_type", None), getattr(event, "actor_id", None))
        == ("policy", "host-publication")
        and _intent_commit(
            event, item_id=item_id, branch=branch, owner=owner, scope=scope
        )
        == commit_sha
        for event in events
    )


def _record_publication_base(
    ctx: _DispatchContext, *, run_id: str, checkout: Path
) -> None:
    """Bind the trusted base a preserved checkout was cut from to the run that
    owns it (see :func:`_bind_publication_base`)."""
    _bind_publication_base(
        ctx.store,
        publish=ctx.config.publication.publish,
        base=ctx.publication_base_sha,
        run_id=run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        branch=ctx.branch,
        checkout=checkout,
    )


def _bind_publication_base(
    store: RunStore | None,
    *,
    publish: str,
    base: str,
    run_id: str,
    issue_num: int,
    item_id: str,
    branch: str,
    checkout: Path,
    context: dict[str, str] | None = None,
) -> None:
    """Bind the trusted base a preserved checkout was cut from to the run that
    owns it before launch or continuation creation.

    Keyed by that run id and the checkout's host-owned identity, so a later
    clone at the same path can never lend or borrow a base. Mandatory under
    canonical publication: the event is read back, and a continuation without
    it cannot be minted (the preserved bytes stay; the operator redispatches).
    The escape hatch needs no base.
    """
    generation = _checkout_generation(checkout)
    _require_checkout_generation(checkout, generation, boundary="base recording")
    if publish != "canonical":
        if store is not None and run_id:
            from kew.continuation_recovery import record_run_checkout_generation

            record_run_checkout_generation(
                store,
                run_id=run_id,
                item_id=item_id,
                branch=branch,
                checkout=checkout,
                generation=generation,
            )
        return
    if store is None or not run_id or not base:
        raise PublicationError(
            "unknown",
            f"no trusted base to bind the preserved checkout {checkout} to; the "
            "continuation is not minted",
            host_paths=(checkout,),
        )
    from kew.audit import Actor, emit
    from kew.continuation_recovery import require_unchanged_checkout_binding

    key = _checkout_key(checkout)
    if not generation:
        raise PublicationError(
            "unknown",
            f"the preserved checkout {checkout} has no readable identity; the "
            "continuation is not minted",
            host_paths=(checkout,),
        )
    require_unchanged_checkout_binding(
        store,
        event=_PUBLICATION_BASE_EVENT,
        run_id=run_id,
        branch=branch,
        checkout=checkout,
        generation=generation,
    )
    emit(
        store,
        _PUBLICATION_BASE_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        payload={
            "branch": branch,
            "base_sha": base,
            "checkout": key,
            "checkout_id": generation,
            **(context or {}),
        },
        # Append every observation. Identical repeats are valid. Any differing
        # base remains visible and makes all later reads fail closed.
    )
    # emit is best-effort by design; the binding is not, so read it back.
    if (
        _recorded_publication_base(
            store,
            run_id=run_id,
            item_id=item_id,
            branch=branch,
            checkout=checkout,
            generation=generation,
        )
        != base
    ):
        raise PublicationError(
            "unknown",
            f"the publication base for {checkout} was not durably recorded; the "
            "continuation is not minted",
            host_paths=(checkout,),
        )


def _recorded_publication_base(
    store: RunStore | None,
    *,
    run_id: str,
    item_id: str | None = None,
    branch: str,
    checkout: Path,
    generation: str | None = None,
) -> str:
    """The base run *run_id* bound *checkout* on *branch* to, or "" when it
    recorded none. The run id, the branch, the resolved path and the
    complete persisted directory evidence (*generation*) are the proof. Another
    run's base at the same path is never lent, and a
    directory that replaced the preserved one at that path matches nothing
    (review round 16)."""
    if store is None or not run_id:
        return ""
    lister = getattr(store, "list_audit_events", None)
    if lister is None:
        return ""
    wanted = _checkout_key(checkout)
    generation = generation or _checkout_generation(checkout)
    bases: set[str] = set()
    try:
        events = list(lister(limit=None, event=_PUBLICATION_BASE_EVENT))
    except Exception as exc:
        raise PublicationError(
            "unknown",
            f"publication base evidence for run {run_id!r} is unreadable: "
            f"{exception_text(exc)}",
        ) from exc
    for event in events:
        recorded = _base_event_sha(
            event,
            run_id=run_id,
            item_id=item_id,
            branch=branch,
            checkout=wanted,
            generation=generation,
        )
        if recorded:
            bases.add(recorded)
    if len(bases) > 1:
        raise PublicationError(
            "unknown",
            f"conflicting publication bases are recorded for run {run_id!r}, "
            f"item {item_id or '<unknown>'!r}, branch {branch!r}, checkout "
            f"{wanted}, and its bound generation",
        )
    return next(iter(bases), "")


def _continuation_base(ctx: _DispatchContext) -> str:
    """Read the sole continuation producer's immutable checkout base."""
    store = ctx.store
    if store is None or ctx.worktree_dir is None or not ctx.epic_ref:
        return ""
    wanted = _checkout_key(ctx.worktree_dir)
    records = _matching_continuation_records(ctx, wanted)
    if not records:
        return ""
    if len(records) != 1:
        raise PublicationError(
            "unknown",
            f"ambiguous continuation identity for {ctx.item_id!r} at "
            f"{ctx.worktree_dir}",
            host_paths=(ctx.worktree_dir,),
        )
    record = records[0]
    claimed = store.get_continuation(
        record.epic_ref,
        record.item_id,
        record.original_run_id,
        record.branch,
        record.worktree_identity,
    )
    if (
        claimed is None
        or claimed.generation != record.generation
        or claimed.state != "continuing"
        or claimed.continuation_run_id != ctx.run_id
    ):
        raise PublicationError("unknown", "continuation claim identity changed")
    claim_checkout(ctx.worktree_dir)
    ctx.checkout_generation = _continuation_checkout_generation(ctx, record)
    ctx.checkout_binding = _hold_checkout_binding(
        ctx.worktree_dir, ctx.checkout_generation
    )
    base = _continuation_record_base(ctx, record)
    _bind_publication_lineage(ctx, record, base)
    return base


def _continuation_checkout_generation(ctx: _DispatchContext, record: Any) -> str:
    """Check the recorded generation through the dispatch refusal boundary."""
    from kew.continuation_recovery import require_continuation_generation

    assert ctx.store is not None
    assert ctx.worktree_dir is not None
    try:
        return require_continuation_generation(
            ctx.store,
            record,
            validate_generation=partial(
                worktree.require_checkout_generation, ctx.worktree_dir
            ),
        )
    except CheckoutGenerationRefusal as exc:
        raise _CheckoutIdentityRefused("unknown", str(exc)) from exc


def _continuation_record_base(ctx: _DispatchContext, record: Any) -> str:
    """Read a legacy base or require the canonical producer contract."""
    if ctx.config.publication.publish == "canonical":
        return _recorded_continuation_producer_base(
            ctx,
            required_run_id=record.original_run_id,
        )
    assert ctx.store is not None
    assert ctx.worktree_dir is not None
    return _recorded_publication_base(
        ctx.store,
        run_id=record.original_run_id,
        item_id=ctx.item_id,
        branch=ctx.branch,
        checkout=ctx.worktree_dir,
    )


def _recorded_continuation_producer_base(
    ctx: _DispatchContext, *, required_run_id: str
) -> str:
    """Require one terminal implementation producer for this checkout."""
    assert ctx.store is not None
    assert ctx.worktree_dir is not None
    generation = _checkout_generation(ctx.worktree_dir)
    if not generation:
        raise PublicationError(
            "unknown",
            f"preserved checkout {ctx.worktree_dir} has no immutable generation",
            host_paths=(ctx.worktree_dir,),
        )
    producers = _terminal_implementation_producers(ctx)
    matches = _matching_recorded_producers(ctx, producers, generation)
    if len(matches) != 1:
        detail = "no" if not matches else "multiple"
        raise PublicationError(
            "unknown",
            f"{detail} terminal implementation producer has immutable publication "
            f"base evidence for {ctx.worktree_dir}",
            host_paths=(ctx.worktree_dir,),
        )
    producer, base = matches[0]
    if producer.run_id != required_run_id:
        raise PublicationError(
            "unknown",
            f"continuation names run {required_run_id!r}, but checkout identity "
            f"evidence names {producer.run_id!r}",
        )
    return base


def _terminal_implementation_producers(ctx: _DispatchContext) -> list[Any]:
    """Return terminal tracker-work runs, excluding every PR-subject kind."""
    assert ctx.store is not None
    from kew.run_store import PR_SUBJECT_RUN_KINDS

    implementation_kinds = TRACKER_WORK_RUN_KINDS - PR_SUBJECT_RUN_KINDS
    return [
        run
        for run in ctx.store.list_by_item(ctx.item_id)
        if run.branch == ctx.branch
        and run.status != "pending"
        and run.run_kind in implementation_kinds
    ]


def _matching_recorded_producers(
    ctx: _DispatchContext, producers: list[Any], generation: str
) -> list[tuple[Any, str]]:
    """Join eligible run rows to this checkout generation's base evidence."""
    assert ctx.store is not None
    assert ctx.worktree_dir is not None
    matches: list[tuple[Any, str]] = []
    for producer in producers:
        base = _recorded_publication_base(
            ctx.store,
            run_id=producer.run_id,
            item_id=ctx.item_id,
            branch=ctx.branch,
            checkout=ctx.worktree_dir,
            generation=generation,
        )
        if base:
            matches.append((producer, base))
    return matches


def _matching_continuation_records(ctx: _DispatchContext, wanted: str) -> list[Any]:
    """Retain current identities without discarding historical abandonment evidence."""
    assert ctx.store is not None
    assert ctx.epic_ref is not None
    return [
        record
        for record in ctx.store.list_continuations(ctx.epic_ref)
        if record.item_id == ctx.item_id
        and record.branch == ctx.branch
        and continuation_checkout_matches(
            record.worktree_identity,
            wanted,
            recorded_owner=(record.item_id, record.branch),
            candidate_owner=(ctx.item_id, ctx.branch),
        )
        and not ctx.store.continuation_was_abandoned(record)
    ]


def _checkout_generation(checkout: Path) -> str:
    """Read a complete snapshot for initial binding or producer lookup.

    Quarantine and removal revoke it. External directory replacement refuses
    even when the sidecar survives. An absent sidecar returns "".
    """
    return worktree.read_checkout_generation(checkout)


def _require_checkout_generation(
    checkout: Path, expected: str, *, boundary: str
) -> None:
    """Refuse when a claimed checkout changed identity across a handoff."""
    try:
        worktree.require_checkout_generation(checkout, expected)
    except CheckoutGenerationRefusal as exc:
        raise _CheckoutIdentityRefused(
            "unknown",
            f"checkout identity changed during {boundary} for {checkout}; {exc}",
            host_paths=(checkout,),
        ) from exc


def _bind_initial_checkout(ctx: _DispatchContext, checkout: Path) -> str:
    """Bind a new checkout before its first paid runner boundary."""
    generation = _checkout_generation(checkout)
    if generation:
        claim_checkout(checkout)
        ctx.checkout_binding = _hold_checkout_binding(checkout, generation)
    ctx.checkout_generation = generation
    if ctx.config.publication.publish == "canonical" and not ctx.allow_unrecorded:
        if ctx.run_id is None:
            raise PublicationError(
                "unknown",
                f"initial checkout {checkout} has no stable run identity",
                host_paths=(checkout,),
            )
        _record_publication_base(ctx, run_id=ctx.run_id, checkout=checkout)
        _require_checkout_generation(checkout, generation, boundary="base recording")
    elif generation and ctx.store is not None and ctx.run_id:
        from kew.continuation_recovery import record_run_checkout_generation

        record_run_checkout_generation(
            ctx.store,
            run_id=ctx.run_id,
            item_id=ctx.item_id,
            branch=ctx.branch,
            checkout=checkout,
            generation=generation,
        )
    _record_checkout_replacement(ctx, checkout, generation)
    return generation


def _hold_checkout_binding(
    checkout: Path, generation: str
) -> worktree.CheckoutDirectoryBinding:
    """Preserve the dispatch refusal category when directory admission fails."""
    try:
        return worktree.hold_checkout_generation(checkout, generation)
    except CheckoutGenerationRefusal as exc:
        raise _CheckoutIdentityRefused(
            "unknown", f"checkout identity changed during binding capture; {exc}"
        ) from exc


def _record_checkout_replacement(
    ctx: _DispatchContext, checkout: Path, generation: str
) -> None:
    """Share durable replacement consumption with loop dispatch."""
    record_checkout_replacement(
        ctx.store,
        checkout,
        abandonment=ctx.checkout_abandonment,
        run_id=ctx.run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        repo=ctx.config.project.repo or None,
        epic_ref=ctx.epic_ref,
        branch=ctx.branch,
        generation=generation,
    )


def _require_bound_checkout_generation(
    ctx: _DispatchContext,
    checkout: Path,
    generation: str,
    *,
    boundary: str,
    honor_unrecorded: bool = False,
) -> None:
    """Check every bound identity, including checkout and unrecorded launches."""
    if not generation and (
        ctx.config.publication.publish != "canonical"
        or (honor_unrecorded and ctx.allow_unrecorded)
    ):
        return
    _require_checkout_generation(checkout, generation, boundary=boundary)


def _checkout_key(checkout: Path) -> str:
    """One spelling of a checkout path for recording and lookup: the
    continuation record and its validator resolve symlinks, so this does too."""
    return str(Path(checkout).expanduser().resolve())


def _base_event_sha(
    event: Any,
    *,
    run_id: str,
    item_id: str | None,
    branch: str,
    checkout: str,
    generation: str,
) -> str:
    """The base one event binds to *checkout* (this generation of it) on
    *branch* for *run_id*, else ""."""
    if getattr(event, "run_id", None) != run_id:
        return ""
    if item_id is not None and getattr(event, "item_id", None) != item_id:
        return ""
    event_id = getattr(event, "id", "<unknown>")
    payload = _read_publication_base_payload(event, event_id, run_id)
    recorded_branch = payload.get("branch")
    if not isinstance(recorded_branch, str):
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} has an "
            "unreadable branch",
        )
    if recorded_branch != branch:
        return ""
    recorded_key = _publication_base_checkout_key(payload, event_id, run_id)
    if not continuation_checkout_matches(recorded_key, checkout):
        return ""
    return _publication_base_sha(
        payload,
        event_id=event_id,
        run_id=run_id,
        checkout=checkout,
        generation=generation,
    )


def _publication_base_sha(
    payload: dict[str, Any],
    *,
    event_id: object,
    run_id: str,
    checkout: str,
    generation: str,
) -> str:
    """Read one matching generation's SHA or raise a named refusal."""
    recorded_generation = payload.get("checkout_id")
    if not isinstance(recorded_generation, str) or not recorded_generation:
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} has an "
            "unreadable checkout generation",
        )
    if not generation or recorded_generation != generation:
        _warn(
            f"publication base event {event_id} names another generation of "
            f"{checkout}; the preserved checkout was replaced"
        )
        return ""
    recorded = payload.get("base_sha")
    if not isinstance(recorded, str) or re.fullmatch(r"[0-9a-f]{40}", recorded) is None:
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} has an "
            "unreadable base SHA",
        )
    return recorded


def _read_publication_base_payload(
    event: Any, event_id: object, run_id: str
) -> dict[str, Any]:
    """Read one publication-base payload or raise a named refusal."""
    payload: Any = getattr(event, "payload", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise PublicationError(
                "unknown",
                f"publication base event {event_id} for run {run_id!r} is unreadable",
            ) from None
    if not isinstance(payload, dict):
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} is unreadable",
        )
    return payload


def _publication_base_checkout_key(
    payload: dict[str, Any], event_id: object, run_id: str
) -> str:
    """Canonicalize one recorded checkout path or raise a named refusal."""
    recorded_checkout = payload.get("checkout")
    if not isinstance(recorded_checkout, str) or not recorded_checkout:
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} has an "
            "unreadable checkout path",
        )
    try:
        recorded_key = _checkout_key(Path(recorded_checkout))
    except (OSError, RuntimeError, ValueError) as exc:
        raise PublicationError(
            "unknown",
            f"publication base event {event_id} for run {run_id!r} has an "
            "unreadable checkout path",
        ) from exc
    return recorded_key


def _publication_intent_payload(event: Any) -> dict[str, Any] | None:
    """Decode current and legacy audit payload representations."""
    payload: Any = getattr(event, "payload", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            _warn(f"publication intent event {event.id} carries unreadable payload")
            return None
    return payload if isinstance(payload, dict) else None


def _intent_commit(
    event: Any,
    *,
    item_id: str,
    branch: str,
    owner: dict[str, str] | None = None,
    scope: str = "item",
) -> str | None:
    """The commit one intent event binds to *branch* for *item_id*, else None.

    The envelope proves item identity. Publication additionally requires the
    checkout generation, original base and explicit run lineage. Older intents
    without those fields cannot authorize publication; they are not migrated.
    """
    if getattr(event, "item_id", None) != item_id:
        return None
    payload = _publication_intent_payload(event)
    if not isinstance(payload, dict) or payload.get("branch") != branch:
        return None
    if payload.get("scope", "item") != scope:
        return None
    if owner is not None and not _publication_checkout_owner_matches(payload, owner):
        return None
    commit_sha = payload.get("commit_sha")
    return commit_sha if isinstance(commit_sha, str) else None


def _recorded_publication_intents(
    store: RunStore | None,
    *,
    item_id: str | None,
    branch: str,
    owner: dict[str, str] | None = None,
) -> set[str]:
    """Every commit a previous attempt of this item bound to *branch*."""
    if store is None or not item_id:
        return set()
    lister = getattr(store, "list_audit_events", None)
    if lister is None:
        return set()
    events = lister(limit=None, event=_PUBLICATION_INTENT_EVENT, item_id=item_id)
    found = (
        _intent_commit(event, item_id=item_id, branch=branch, owner=owner)
        for event in events
    )
    return {commit for commit in found if commit is not None}


def _require_own_publication(
    store: RunStore | None,
    *,
    existing: str,
    branch: str,
    item_id: str | None,
    work_dir: Path,
    owner: dict[str, str],
) -> None:
    """Refuse to build on a remote branch this dispatch did not push."""
    if existing in _recorded_publication_intents(
        store, item_id=item_id, branch=branch, owner=owner
    ):
        return
    if existing in _recorded_restored_lineage_intents(
        store,
        item_id=item_id,
        branch=branch,
        work_dir=work_dir,
        owner=owner,
    ):
        return
    raise PublicationError(
        "push",
        f"branch {branch!r} already exists on the remote at {existing[:12]} and no "
        "publication intent of this dispatch names it; a stale branch from another "
        f"dispatch is not resumed (checkout preserved at {work_dir}). Delete or "
        "rename the remote branch and retry.",
        host_paths=(work_dir,),
    )


def _recorded_restored_lineage_intents(
    store: RunStore | None,
    *,
    item_id: str | None,
    branch: str,
    work_dir: Path,
    owner: dict[str, str],
) -> set[str]:
    """Read prior-generation intents after a durable host restoration."""
    lineage_run_id = owner.get("lineage_run_id", "")
    base_sha = owner.get("base_sha", "")
    checkout_generation = owner.get("checkout_id", "")
    if not lineage_run_id or not base_sha or not checkout_generation:
        return set()
    rebound_base = _recorded_publication_base(
        store,
        run_id=lineage_run_id,
        item_id=item_id,
        branch=branch,
        checkout=work_dir,
        generation=checkout_generation,
    )
    if rebound_base != base_sha:
        return set()
    lineage_owner = {key: value for key, value in owner.items() if key != "checkout_id"}
    return _recorded_publication_intents(
        store,
        item_id=item_id,
        branch=branch,
        owner=lineage_owner,
    )


def _deliver_dispatch_no_change(
    publication: CanonicalPublication | CanonicalNoChange,
    *,
    base_sha: str | None,
    callback: Callable[[CanonicalNoChange], None] | None,
) -> None:
    """Deliver no-change evidence only when it names the dispatch base."""
    if not isinstance(publication, CanonicalNoChange):
        return
    if callback is None or base_sha is None:
        return
    if publication.base_sha == base_sha:
        callback(publication)


def _repo_identity_from_remote_url(remote_url: str) -> tuple[str, str] | None:
    """Extract a host and owner/repo slug from a Git remote URL.

    Detached publication cannot rely on the config object having gone through
    the foreground CLI's ``gh repo view`` auto-detection.  The worktree's
    ``origin`` is the remote the branch was just pushed to, so it is the
    authoritative publication target when config is unset.
    """
    value = remote_url.strip()
    if "://" not in value and ":" in value:
        authority, _, path = value.partition(":")
        host = authority.rpartition("@")[2]
    else:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not host:
            return None
        path = parsed.path
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return host, "/".join(parts)


def _repo_slug_from_remote_url(remote_url: str) -> str | None:
    """Extract an owner/repo slug from a Git remote URL."""
    identity = _repo_identity_from_remote_url(remote_url)
    return identity[1] if identity is not None else None


def _repo_slug_from_origin(work_dir: Path) -> str | None:
    """Read and parse the worktree's GitHub ``origin`` remote, fail-soft."""
    remote_url = _origin_remote_url(work_dir, run_fn=run)
    return _repo_slug_from_remote_url(remote_url) if remote_url is not None else None


def _project_repo_slug(config: DispatchConfig) -> str | None:
    """The project's owner/repo slug: configured first, else the project origin.

    ``project.repo`` is optional ("auto-detected from gh"), so every caller
    that needs a slug has to resolve the origin itself. Resolving it here, in
    the parent that holds the project checkout, is what lets a detached
    canonical publication — which deliberately never reads its worktree's
    origin — still find a slug on the run row (#2097).
    """
    return (config.project.repo or "").strip() or _repo_slug_from_origin(project_root())


def _run_row_repo(store: RunStore | None, run_id: str | None) -> str | None:
    """Read the slug the dispatching parent persisted on the run row."""
    if store is None or not run_id:
        return None
    try:
        record = store.get(run_id)
    except Exception as exc:  # noqa: BLE001 — publication must remain fail-closed
        _logger.warning(
            "run row %s unreadable while resolving the publication repo: %s",
            run_id,
            redact_credentials(exception_text(exc)),
        )
        return None
    stored = getattr(record, "repo", None)
    return stored.strip() or None if isinstance(stored, str) else None


def _publication_repo_sources(
    work_dir: Path,
    config: DispatchConfig,
    store: RunStore | None,
    run_id: str | None,
    *,
    allow_checkout: bool,
) -> list[tuple[str, Callable[[], str | None]]]:
    """The slug sources publication consults, in precedence order, each named.

    The names are what a failure reports, so this list is the single place
    that decides both what is tried and what the error may blame (#2097): a
    canonical publication never reads the worktree origin, so it is absent
    here — and from the error — unless ``allow_checkout``.
    """
    sources: list[tuple[str, Callable[[], str | None]]] = [
        ("project.repo", lambda: (config.project.repo or "").strip() or None)
    ]
    if allow_checkout:
        sources.append(
            ("the worktree origin", lambda: _repo_slug_from_origin(work_dir))
        )
    sources.append(("the run row repo", lambda: _run_row_repo(store, run_id)))
    return sources


def _publication_repo(
    work_dir: Path,
    config: DispatchConfig,
    store: RunStore | None,
    run_id: str | None,
    *,
    allow_checkout: bool = False,
) -> str | None:
    """Resolve the repo slug needed by host publication.

    Explicit config remains the strongest binding.  For detached children,
    ``origin`` then supplies the binding; a persisted run-row repo is the
    final fallback for callers that captured the parent-resolved identity.
    """
    for _name, resolve in _publication_repo_sources(
        work_dir, config, store, run_id, allow_checkout=allow_checkout
    ):
        resolved = resolve()
        if resolved:
            return resolved
    return None


def _create_pull_request(
    cmd: list[str],
    *,
    work_dir: Path,
    item_id: str,
) -> tuple[str, int | None]:
    """Run and validate the REST PR-create step as one typed boundary."""
    try:
        result = run(
            cmd,
            cwd=work_dir,
            check=False,
            env=publication_subprocess_env(),
        )
    except Exception as exc:
        raise PublicationError("pr_create", exc) from exc
    if result.returncode != 0:
        raise PublicationError(
            "pr_create",
            f"GitHub REST PR create failed for {item_ref(item_id)} "
            f"(exit {result.returncode}): "
            f"{redact_credentials(result.stderr or result.stdout or '').strip()[:200]}",
        )
    try:
        return _created_pr_identity(result.stdout, item_id)
    except Exception as exc:
        raise PublicationError("pr_create", exc) from exc


def _require_publication_repo(
    work_dir: Path,
    config: DispatchConfig,
    store: RunStore | None,
    run_id: str | None,
    mode: str,
) -> str:
    allow_checkout = mode == "checkout"
    repo = _publication_repo(
        work_dir,
        config,
        store,
        run_id,
        allow_checkout=allow_checkout,
    )
    if repo is None:
        tried = ", ".join(
            name
            for name, _resolve in _publication_repo_sources(
                work_dir, config, store, run_id, allow_checkout=allow_checkout
            )
        )
        raise PublicationError(
            "pr_create",
            f"GitHub repository slug unavailable for {mode} publication: "
            f"no owner/repo from {tried}",
        )
    return repo


def _pull_request_command(
    *,
    repo: str,
    branch: str,
    title: str,
    body: str,
    base: str,
    draft: bool,
) -> list[str]:
    return [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/pulls",
        "-f",
        f"title={title}",
        "-f",
        f"body={body}",
        "-f",
        f"head={branch}",
        "-f",
        f"base={base}",
        "-F",
        f"draft={'true' if draft else 'false'}",
    ]


def _agent_pr_text(
    item_id: str,
    title: str,
    report: str | None,
    *,
    pr_title: str | None,
    body: str | None,
) -> tuple[str, str]:
    """The PR's title and body, each overridable (#3187).

    A title or body GitHub would refuse is held for a person here, before any
    push, so no branch is left without its PR.
    """
    try:
        built = body if body is not None else agent_pr_body(item_id, title, report)
    except PrBodyTooLong as exc:
        raise PublicationHeld((f"pr_body_too_long: {exc}",)) from exc
    chosen = pr_title or agent_pr_title(item_id, title, report)
    why = unsendable(chosen, built)
    if why is not None:
        raise PublicationHeld(
            (
                f"pr_text_unsendable: {why}; nothing was pushed. Open the PR by "
                "hand from the preserved checkout, or discard it and re-dispatch",
            )
        )
    return chosen, built


def _push_and_open_pr(
    work_dir: Path,
    branch: str,
    item_id: str,
    title: str,
    config: DispatchConfig,
    *,
    draft: bool = False,
    body: str | None = None,
    extra_labels: tuple[str, ...] = (),
    store: RunStore | None = None,
    run_id: str | None = None,
    base_sha: str | None = None,
    runtime: str | None = None,
    image: str | None = None,
    log_file: Path | None = None,
    on_no_change: Callable[[CanonicalNoChange], None] | None = None,
    on_publication: (
        Callable[[CanonicalPublication | CanonicalNoChange], None] | None
    ) = None,
    pr_title: str | None = None,
) -> str | None:
    """Push the branch and open a PR from the host — the git ceremony.

    Shared by the sandboxed path (where the agent never has a GitHub token),
    the host-broker path (where a weak agent skipped it), and the incomplete-run
    rescue path (``draft=True``). ``body`` overrides default; ``extra_labels`` added
    alongside ``agent_pr``. Returns the created PR URL (from GitHub's REST
    response) and, when ``store``/``run_id`` are given, persists it to the run
    record at creation so the ledger matches reality even if the run is later
    killed (#648).

    The title and body are built from the run's report before anything is
    pushed (#3187); ``pr_title`` overrides the title. A body too long for
    GitHub is held for a person here, so no branch is pushed without its PR.
    """
    pr_title, pr_body = _agent_pr_text(
        item_id, title, agent_report_for(log_file), pr_title=pr_title, body=body
    )
    mode = select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        store=store,
        run_id=run_id,
        item_id=item_id,
        branch=branch,
        repo=config.project.repo or None,
    )
    publication = _git_push_branch(
        branch,
        work_dir,
        # A new head on a branch that already has an agent PR is new agent code
        # (#2033). A first publication has no PR yet, so nothing is cleared.
        before_push=partial(revoke_ci_approval, branch, config),
        repo=config.project.repo or None,
        base_sha=base_sha,
        base_branch=config.branches.base,
        message=_broker_commit_message(
            item_id, config, title=title, run_id=run_id, log_file=log_file
        ),
        runtime=runtime,
        image=image,
        proxy_environment=config.sandbox.proxy_environment,
        publish=mode,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
        on_no_change=on_no_change,
        store=store,
        run_id=run_id,
        item_id=item_id,
    )
    if on_publication is not None and publication is not None:
        on_publication(publication)
    if mode == "canonical" and _nothing_to_publish(publication, base_sha):
        return None
    repo = _require_publication_repo(work_dir, config, store, run_id, mode)
    cmd = _pull_request_command(
        repo=repo,
        branch=branch,
        title=pr_title,
        body=pr_body,
        base=config.branches.base or "main",
        draft=draft,
    )
    pr_url, pr_number = _create_pull_request(
        cmd,
        work_dir=work_dir,
        item_id=item_id,
    )
    _persist_pr_url(store, run_id, pr_url)
    _apply_created_pr_labels(
        repo,
        pr_number,
        pr_url,
        (config.labels.agent_pr, *extra_labels),
        work_dir,
    )
    return pr_url


def _nothing_to_publish(
    publication: CanonicalPublication | CanonicalNoChange | None,
    dispatch_base_sha: str | None,
) -> bool:
    """Distinguish an empty dispatch from an already-pushed intended tree."""
    if publication is None:
        return True
    return (
        isinstance(publication, CanonicalNoChange)
        and dispatch_base_sha is not None
        and publication.base_sha == dispatch_base_sha
    )


def _created_pr_identity(stdout: str | None, item_id: str) -> tuple[str, int | None]:
    """Parse the REST create response into the durable PR identity."""
    try:
        parsed: Any = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    payload = parsed if isinstance(parsed, dict) else {}
    url = payload.get("html_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"GitHub REST PR create returned no URL for {item_ref(item_id)}"
        )
    number = payload.get("number")
    return url, number if isinstance(number, int) else None


def _apply_created_pr_labels(
    repo: str,
    pr_number: int | None,
    pr_url: str,
    labels: tuple[str, ...],
    work_dir: Path,
) -> None:
    """Apply PR labels through REST without invalidating a created PR."""
    if pr_number is None:
        return
    cmd = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/issues/{pr_number}/labels",
    ]
    for label in labels:
        cmd.extend(["-f", f"labels[]={label}"])
    result = run(
        cmd,
        cwd=work_dir,
        check=False,
        env=publication_subprocess_env(),
    )
    if result.returncode != 0:
        _warn(
            f"PR {pr_url} opened but REST labeling failed; "
            "the published branch and PR are preserved."
        )


def _commit_all_if_dirty(
    worktree_dir: Path,
    item_id: str,
    config: DispatchConfig,
    *,
    base_sha: str | None = None,
    title: str | None = None,
    run_id: str | None = None,
    log_file: Path | None = None,
) -> bool:
    """Commit all changes if the worktree is dirty. Returns True if it committed.

    Shared by the broker (#518) and the incomplete-run rescue path. ``title``,
    ``run_id`` and ``log_file`` only shape the commit message (#2062).
    """
    mode = select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    )
    if mode == "canonical":
        return bool(
            _get_changed_files(
                worktree_dir,
                config.branches.base or "main",
                repo=config.project.repo or None,
                base_sha=base_sha,
                publish="canonical",
            )
        )
    # Plumbing under an inert git environment: nothing the checkout names runs
    # on the host while kew commits for it (#2634).
    return commit_checkout_worktree(
        worktree_dir,
        _broker_commit_message(
            item_id, config, title=title, run_id=run_id, log_file=log_file
        ),
    )


# --- zero-change outcomes (I10B) ---------------------------------------------
#
# A run that changed nothing writes NOTHING to the provider. It records one
# durable no-change candidate (kew.no_change) and stops: the item stays open,
# the dependents stay blocked, and I11 asks a person what the absence meant.
# The legacy path did the opposite — labelled, closed, auto-unblocked, and for
# a Linear item commented and closed through a tracker hook — all on evidence
# that is indistinguishable from "the agent never got as far as writing a file".

#: The host's own zero-diff observation, recorded as a named check so an
#: attestation says which check looked for work and found none.
_NO_CHANGE_CHECK = "host_zero_diff_observation"


def _no_change_base_sha(worktree_dir: Path | None, config: DispatchConfig) -> str:
    """The base commit the zero-diff run was working from, or "" if unreadable.

    ``origin/<base>`` first: that is the ref the workspace was cut from and the
    one a stale local branch would disagree with. Nothing is guessed — an
    unreadable base returns "" and the caller records no judgment at all,
    because a judgment bound to a base kew invented is worse than no record.
    """
    if worktree_dir is None:
        return ""
    if config.publication.publish == "canonical":
        _warn(
            "Canonical publication: the no-change base is not read from the checkout."
        )
        return ""
    base = config.branches.base or "main"
    for ref in (f"origin/{base}", base):
        try:
            result = run(["git", "rev-parse", ref], cwd=worktree_dir, check=False)
        # A missing workspace, an unresolvable git, a subprocess that died: the
        # run itself already succeeded, so every one of these is "the base
        # could not be read", never a reason to fail the dispatch.
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""
        sha = (result.stdout or "").strip()
        if result.returncode == 0 and len(sha) == 40:
            return sha
    return ""


def _no_change_attempts(
    store: RunStore, run_id: str
) -> tuple[AttemptedChangeRecord, ...]:
    """Every attempted change this run's ledger records.

    A ledger that cannot be replayed — or that carries no row for this run at
    all — reads as ONE unavailable attempt, never as an empty list. Suppressed
    work is exactly what an empty diff hides, so missing attempted-change
    evidence has to keep the outcome unattestable rather than quietly confirm
    that nothing was tried. Only a ledger that verified AND has something to
    say about this run may report "no attempts" as a fact.
    """
    unavailable = (
        AttemptedChangeRecord(
            subject=run_id,
            disposition=str(AttemptedChange.UNAVAILABLE),
            reason="this run's evidence ledger could not be replayed",
        ),
    )
    try:
        replayed = replay_host_sequence(store, run_id=run_id)
    except Exception:  # noqa: BLE001 - an unreadable ledger is a gap, not a crash
        return unavailable
    if not replayed.ok or not replayed.records:
        return unavailable
    return tuple(
        AttemptedChangeRecord(subject=subject, disposition=disposition)
        for subject, disposition in _replayed_attempts(replayed)
    )


def _no_change_package(
    store: RunStore, judgment: NoChangeJudgment
) -> dict[str, object]:
    """The sealed package this attempt produced, as evidence fields.

    Empty when nothing was sealed — which is itself a gap, and the reason the
    legacy paths cannot attest today: they publish through the agent, so no
    receipt names the work package a no-change would have to be empty of.
    """
    receipts = [
        receipt
        for receipt in store.list_artifact_receipts(run_id=judgment.run_id)
        if receipt.attempt_number == judgment.attempt_number
    ]
    if not receipts:
        return {}
    receipt = receipts[-1]
    fields: dict[str, object] = {
        "trusted_base_sha": receipt.base_sha,
        "package_digest": receipt.package_digest,
    }
    try:
        artifact = PackageStore(_packages_root(project_root())).load(
            receipt.package_digest
        )
    except (PackageStoreError, ArtifactRejected, OSError):
        return fields
    fields["package_scope"] = artifact.scope or ""
    fields["package_entry_count"] = len(artifact.entries)
    fields["package_deletion_count"] = len(artifact.deletions)
    return fields


def _no_change_evidence(
    store: RunStore,
    judgment: NoChangeJudgment,
    observed_base: str,
    judged_paths: tuple[str, ...] = (),
) -> NoChangeEvidence:
    """Assemble what this host actually observed about the zero-diff exit."""
    check = NamedCheck(
        name=_NO_CHANGE_CHECK,
        outcome=NO_CHANGE_CHECK_PASSED,
        # The digest of the observation itself: which run, which attempt, and
        # the base it read. Not a ledger row — this host appends no attested
        # fact of its own here — but a stable name for the bytes it saw.
        digest="sha256:"
        + hashlib.sha256(
            f"{judgment.judgment_id}\n{judgment.attempt_number}\n{observed_base}".encode()
        ).hexdigest(),
    )
    fields: dict[str, object] = {
        "trusted_base_sha": "",
        "observed_base_sha": observed_base,
        "package_digest": "",
        "package_scope": "",
        "package_entry_count": 0,
        "package_deletion_count": 0,
        "checks": (check,),
        "attempted_changes": _no_change_attempts(store, judgment.run_id),
        "judged_paths": judged_paths,
    }
    fields.update(_no_change_package(store, judgment))
    return NoChangeEvidence(**fields)  # type: ignore[arg-type]


def _attest_no_change(
    store: RunStore,
    judgment: NoChangeJudgment,
    observed_base: str,
    judged_paths: tuple[str, ...] = (),
) -> NoChangeJudgment:
    """Bind host evidence to the candidate, or say why it does not attest.

    Never raises: an unattestable candidate is the SAFE outcome (it stays
    unconfirmed and nothing downstream may act on it), so a gap is reported
    to the operator rather than failing a dispatch that already succeeded.
    """
    try:
        evidence = _no_change_evidence(store, judgment, observed_base, judged_paths)
        gaps = evidence.gaps()
    except NoChangeError as exc:
        gaps = (redact_credentials(exception_text(exc)),)
    if gaps:
        _log(
            f"no-change candidate {judgment.judgment_id} is not attested: "
            + "; ".join(gaps)
        )
        return judgment
    try:
        return store.attest_no_change(
            judgment_id=judgment.judgment_id,
            evidence=evidence,
            now=datetime.now(UTC),
        )
    except NoChangeError as exc:
        _warn(
            redact_credentials(
                f"no-change candidate {judgment.judgment_id} "
                f"could not be attested: {exception_text(exc)}"
            )
        )
        return judgment


def _seal_no_change_evidence(
    store: RunStore,
    judgment: NoChangeJudgment,
    worktree_dir: Path | None,
    canonical_no_change: CanonicalNoChange | None,
) -> None:
    """Seal the empty work package attestation reads, or say why it could not.

    Nothing else on either zero-change path seals a receipt — the host broker
    publishes through the agent and the contained path publishes inside the
    sandbox (#1610) — so without this the candidate lands carrying no evidence
    at all. Never raises: an unsealed package is a gap :func:`_attest_no_change`
    reports to the operator, not a reason to fail a dispatch that succeeded.
    """
    if worktree_dir is None:  # pragma: no cover - a judgment needs a base first
        return
    try:
        seal_no_change_package(
            store,
            worktree_dir=worktree_dir,
            packages_root=_packages_root(project_root()),
            base_sha=judgment.base_sha,
            source_kind=judgment.source_kind,
            source_id=judgment.source_id,
            run_id=judgment.run_id,
            attempt_number=judgment.attempt_number,
            now=datetime.now(UTC),
            verified_artifact=(
                None if canonical_no_change is None else canonical_no_change.artifact
            ),
            verified_tree_sha=(
                None if canonical_no_change is None else canonical_no_change.tree_sha
            ),
        )
    except NoChangeSealError as exc:
        _log(
            f"no-change candidate {judgment.judgment_id} sealed no work package: "
            f"{exception_text(exc)}"
        )
    except Exception as exc:  # noqa: BLE001 - a gap, never a failed dispatch
        _warn(
            redact_credentials(
                f"no-change candidate {judgment.judgment_id} could not seal a work"
                f" package: {exception_text(exc)}"
            )
        )


def _decide_no_change(store: RunStore, judgment: NoChangeJudgment) -> NoChangeJudgment:
    """Resolve an attested candidate against its policy, so there is something
    to confirm.

    Attestation only binds evidence; the state the human verbs act on is the
    one the POLICY puts the row in, and ``confirm_no_change`` refuses anything
    that is not ``pending_confirmation``. Never raises: a candidate that could
    not be attested has nothing to decide.
    """
    if judgment.state != NO_CHANGE_ATTESTED:
        return judgment
    try:
        return store.decide_no_change(
            judgment_id=judgment.judgment_id, now=datetime.now(UTC)
        )
    except NoChangeError as exc:
        _warn(
            redact_credentials(
                f"no-change candidate {judgment.judgment_id} was attested but not"
                f" decided: {exception_text(exc)}"
            )
        )
        return judgment


def _complete_no_changes(
    config: DispatchConfig,
    *,
    item_id: str,
    provider: str,
    store: RunStore | None,
    run_id: str,
    worktree_dir: Path | None,
    canonical_no_change: CanonicalNoChange | None = None,
    trusted_base_sha: str = "",
    item_text: str = "",
) -> NoChangeJudgment | None:
    """Record a zero-diff exit as a durable no-change candidate. Closes nothing.

    Shared by both zero-change paths — the host broker finding nothing to
    publish and a contained run whose checkout carried no work — so the two
    produce the SAME judgment for the same attempt instead of two dialects of
    "done". Returns the judgment, or ``None`` when there was nothing durable to
    bind it to; either way not one byte reaches the provider.
    """
    if canonical_no_change is not None:
        base_sha = canonical_no_change.base_sha
    elif config.publication.publish == "canonical":
        # Without the walker's evidence nothing is classified or sealed: the
        # legacy sealer reads the checkout with git (review round 10).
        _warn(
            f"{item_ref(item_id)} changed nothing, but no no-change outcome was"
            " recorded (no canonical evidence reached the judgment). Nothing"
            " was closed: the item stays open."
        )
        return None
    else:
        base_sha = _no_change_base_sha(worktree_dir, config)
    if store is None or not run_id or not base_sha:
        _warn(
            f"{item_ref(item_id)} changed nothing, but no no-change outcome was"
            " recorded (no run store, run id or readable base). Nothing was"
            " closed: the item stays open."
        )
        return None
    record = store.get(run_id)
    intent = NoChangeIntent(
        source_kind=provider,
        source_id=item_id,
        run_id=run_id,
        attempt_number=max(1, record.attempt_number if record else 1),
        base_sha=base_sha,
        item_id=item_id,
    )
    try:
        judgment = store.open_no_change_judgment(
            intent=intent, policy=DEFAULT_NO_CHANGE_POLICY, now=datetime.now(UTC)
        )
    except NoChangeError as exc:
        _warn(
            redact_credentials(
                f"{item_ref(item_id)} changed nothing, but the outcome stands"
                f" with an earlier judgment: {exception_text(exc)}"
            )
        )
        return None
    _seal_no_change_evidence(store, judgment, worktree_dir, canonical_no_change)
    # Resolved at the judged base in the host-owned project repository, never
    # in the agent's checkout; anything short of every name resolving records
    # none, and the judgment stays bound to its exact base.
    judged_paths = judged_paths_from(item_text, tracked_files(base_sha))
    judgment = _decide_no_change(
        store, _attest_no_change(store, judgment, base_sha, judged_paths)
    )
    _success(
        f"Completed {item_ref(item_id)} -- no changes to publish. Recorded"
        f" no-change candidate {judgment.judgment_id} ({judgment.state});"
        " the item stays open until the outcome is confirmed."
    )
    return judgment


def _qualifies_for_rescue(
    result: DispatchResult,
    worktree_dir: Path,
    config: DispatchConfig,
    *,
    base_sha: str | None = None,
) -> bool:
    """True when a failed run left coherent work worth rescuing to a draft PR.

    Narrow by design (v1): only max-steps exhaustion. The runner emits
    ``stop_reason == "aborted"`` ONLY when the loop hit its step ceiling at a
    clean turn boundary (a runtime error emits ``"error"``). A timeout is an
    external kill that can land mid-tool-write, so it is excluded here.
    Broadening to ``{"aborted", "timeout"}`` is the documented v2 seam.
    """
    if result.stop_reason != "aborted":
        return False
    base = config.branches.base or "main"
    changed = bool(
        _get_changed_files(
            worktree_dir,
            base,
            repo=config.project.repo or None,
            base_sha=base_sha,
            publish=config.publication.publish,
            dangerously_skip_containment=(config.sandbox.dangerously_skip_containment),
        )
    )
    mode = select_publication_mode(
        config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    )
    if mode == "canonical":
        return changed
    return has_uncommitted_changes(worktree_dir) or bool(changed)


_RESCUE_PR_BODY_PREFIX = (
    "⚠️ Reached `max_steps` without calling `finish`. kew brokered a "
    "commit so work isn't lost — **the agent may not have completed its "
    "task.** Review carefully before merging."
)


def _rescue_pr_body(item_id: str) -> str:
    """Rescue-draft PR body — banner + item-appropriate closing reference.

    GitHub items get ``Closes #N``; Linear items carry the bare id (no marker
    GitHub can't resolve — the link comes from the branch lookup, A6).
    """
    return f"{_RESCUE_PR_BODY_PREFIX}\n\n{closes_marker(item_id)}"


def _rescue_incomplete_run(
    worktree_dir: Path,
    branch: str,
    issue_num: int,
    title: str,
    config: DispatchConfig,
    *,
    item_id: str,
    store: RunStore | None = None,
    run_id: str | None = None,
    base_sha: str | None = None,
    log_file: Path | None = None,
) -> None:
    """Rescue an incomplete-but-coherent run into flagged DRAFT PR (#518).

    Commits any uncommitted work, pushes, opens *draft* PR labeled
    ``incomplete-run`` + ``agent-pr`` with review-carefully banner, flags
    issue ``agent-needs-help`` + ``incomplete-run`` (leaves it OPEN).

    Does NOT call ``mark_completed`` and does NOT unblock dependents —
    rescued run requires human gate, not completion. Worktree preserved for
    caller. Raises on push/PR failure so caller can fall back to hard-fail;
    everything after the draft PR is confirmed open is best-effort so a
    transient gh hiccup never relabels the issue agent-failed out from
    under an already-open draft (the draft PR + pushed branch stand either
    way).
    """
    if store is not None and run_id:
        ensure_publication_credentials(
            store, run_id=run_id, policy=require_publication_credentials
        )
    _commit_all_if_dirty(
        worktree_dir,
        item_id,
        config,
        base_sha=base_sha,
        title=title,
        run_id=run_id,
        log_file=log_file,
    )
    # Fatal (pre-confirmation): if push / PR-create fails there is no draft,
    # so propagate — the caller falls back to a hard-fail.
    _push_and_open_pr(
        worktree_dir,
        branch,
        item_id,
        title,
        config,
        draft=True,
        body=_rescue_pr_body(item_id),
        extra_labels=(config.labels.incomplete_run,),
        store=store,
        run_id=run_id,
        base_sha=base_sha,
        log_file=log_file,
    )

    # The draft PR is now open on the remote. Everything below is best-effort:
    # a transient gh hiccup must NOT relabel the issue agent-failed — the draft
    # + pushed branch are already safe (spec: "let the draft stand").
    try:
        # The incomplete-run label was set at create time via extra_labels; a
        # belt-and-suspenders re-apply guards against gh dropping an unknown
        # label.
        new_pr = check_pr_exists(branch, config)
        if isinstance(new_pr, int) and new_pr > 0:
            update_pr_labels(new_pr, config, add=[config.labels.incomplete_run])
        mark_needs_review_incomplete(issue_num, config)
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Rescued {item_ref(item_id)}: draft PR opened but post-PR labeling "
                f"failed (draft stands): {exception_text(exc)}"
            )
        )

    _warn(
        f"Rescued {item_ref(item_id)} -- draft PR opened from an incomplete run "
        f"(needs human review). Worktree preserved: {worktree_dir}"
    )


def _worker_cap_reached(
    store: RunStore,
    config: DispatchConfig,
    issue_num: int,
    item_id: str | None = None,
) -> tuple[bool, int, int]:
    """Fleet-wide worker-cap preflight check (#721).

    Returns ``(reached, live, cap)`` where *live* is the store-backed count of
    distinct in-flight work items (pending runs + active epic reservations,
    deduped) EXCLUDING the run being dispatched — so it never counts itself —
    and *cap* is ``parallel.max_workers``. *reached* is True when a new agent
    would push the fleet to (or past) the cap. A cap of 0 or less disables the
    check (unbounded).

    Exclusion keys on the durable ``item_id`` (A3), NOT ``issue_num``: every
    non-numeric item shares the ``0`` sentinel, so excluding by ``issue_num=0``
    would fail to drop a Linear item's own reservation and it would count
    itself. ``item_id`` defaults to ``str(issue_num)`` (the GitHub shim, where
    the two are byte-identical).
    """
    cap = config.fleet_concurrency()
    if cap <= 0:
        return False, 0, cap
    # Owner-liveness-aware (#1059, wired here by #1132): a hard-killed run
    # orphans its ``pending`` row, and the unprobed count treats that corpse as
    # a live agent — one crash permanently shrinks the fleet until someone runs
    # ``kew cleanup`` by hand. The probe is three-state and drops a row only on
    # a provable ``False``, so a run mid-startup is never falsely discounted.
    # Local import: ``graph.driver`` imports ``dispatch_core``, so a
    # module-level import here would cycle.
    from kew.graph.driver.tick import default_run_liveness

    live = store.live_run_count(
        exclude_item=item_id or str(issue_num),
        liveness=default_run_liveness(config),
    )
    return live >= cap, live, cap


def _holds_label_claim(epic_ref: str | None, provider: str, issue_num: int) -> bool:
    """True when the dispatch's claim is a classic-queue label on a GitHub issue.

    An epic-owned run (``epic_ref`` set) is claimed by the driver's reservation,
    never by label (#661), and a non-GitHub item has no issue to relabel; the
    ``0`` sentinel is never a GitHub number.
    """
    return epic_ref is None and provider == "github" and issue_num > 0


def _hand_claim_back(
    issue_num: int,
    config: DispatchConfig,
    store: RunStore | None,
    *,
    epic_ref: str | None,
    provider: str,
    item_id: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Return a classic-queue claim to ``ready``; name the issue when that fails.

    Every refusal that hands a claim back comes through here (#3045). A failed
    reset leaves the issue claimed with no run, and later drains skip it, so
    the failure is not swallowed: it is returned for the caller's outcome and
    recorded as a ``claim_rollback_failed`` audit event on the run, which is
    what the operator or the next drain recovers the issue from.

    Only a label claim is handed back here. An epic-owned run or a non-GitHub
    item returns None untouched: its claim is not a label, so a failed relabel
    cannot strand it.
    """
    if not _holds_label_claim(epic_ref, provider, issue_num):
        return None
    try:
        reset_issue_to_ready(issue_num, config)
    except Exception as exc:  # noqa: BLE001 — returned and recorded, never raised
        stranded = redact_credentials(
            f"{item_ref(item_id or str(issue_num))} is still claimed: "
            f"label reset failed: {exception_text(exc)}"
        )
    else:
        return None
    _error(stranded)
    with contextlib.suppress(Exception):
        audit.emit(
            store,
            CLAIM_ROLLBACK_FAILED,
            actor=resolve_actor(),
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=config.project.repo or None,
            payload={"reason": stranded},
        )
    return stranded


def _with_stranded_claim(
    outcome: DispatchOutcome, stranded: str | None, *, ended: str | None = None
) -> DispatchOutcome:
    """Report a refusal whose claim stayed put as a rollback failure.

    ``ended`` names how the run ended when the outcome's own category and
    reason do not: a killed or quota-refused run reads as a plain failure.
    """
    if stranded is None:
        return outcome
    head = ended or f"{outcome.category}: {outcome.reason}"
    return replace(
        outcome, category=CLAIM_ROLLBACK_FAILED, reason=f"{head}; {stranded}"
    )


def _requeue_over_cap(
    issue_num: int,
    epic_ref: str | None,
    store: RunStore,
    config: DispatchConfig,
    item_id: str | None = None,
    *,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    provider: str = "github",
    run_id: str | None = None,
) -> str | None:
    """Return an over-cap run to the queue without dispatching it (#721).

    ``epic_ref`` is the authoritative driver-owned signal (same contract as
    the budget-hold path, #661) — labels can be missing or malformed. A
    driver-dispatched node releases its atomic reservation so the driver
    re-admits it on a later tick (its labels must never be reset here).
    Anything else was already claimed by the caller. Restore ``ready`` here,
    before the worker reports completion. This survives scheduler parent exits.

    The reservation is released on the durable ``item_id`` (A3), NOT
    ``issue_num``: a Linear node's reservation is keyed on ``SFD-12``, so
    releasing on the ``0`` sentinel would leave it to dangle until grace expiry.
    ``item_id`` defaults to ``str(issue_num)`` (the GitHub shim).
    A caller without an expected reservation identity cannot release a reservation.
    Returns the stranded-claim reason when the classic claim stayed put.
    """
    release_id = item_id or str(issue_num)
    if epic_ref is not None:
        if expected_reservation_run_id is None:
            return None
        _record_refusal_row(
            store,
            config,
            epic_ref,
            issue_num,
            release_id,
            error_category="requeued_over_cap",
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            raise_on_replaced=True,
        )
        try:
            store.release_epic_node_item(release_id, run_id=expected_reservation_run_id)
        except Exception as exc:
            _warn(
                redact_credentials(
                    f"Failed to release reservation for {item_ref(release_id)}: "
                    f"{exception_text(exc)}"
                )
            )
        return None
    return _hand_claim_back(
        issue_num,
        config,
        store,
        epic_ref=epic_ref,
        provider=provider,
        item_id=release_id,
        run_id=run_id,
    )


def _enforce_worker_cap(
    store: RunStore | None,
    config: DispatchConfig,
    issue_num: int,
    epic_ref: str | None,
    item_id: str | None = None,
    *,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    on_refuse: Callable[[str], None] | None = None,
    provider: str = "github",
    run_id: str | None = None,
) -> DispatchOutcome | None:
    """Fleet-wide worker-cap preflight (#721).

    Returns a requeue ``DispatchOutcome`` (and requeues the item) when the
    fleet is already at ``parallel.max_workers`` live runs, otherwise None so
    dispatch proceeds. No-op without a store (per-process paths that don't
    track shared concurrency). Both the count exclusion and the requeue release
    key on the durable ``item_id`` (A3).
    """
    if store is None:
        return None
    reached, live, cap = _worker_cap_reached(store, config, issue_num, item_id)
    if not reached:
        return None
    display_id = item_id or str(issue_num)
    reason = f"worker cap reached ({live}/{cap} live)"
    _warn(
        f"Worker cap reached ({live}/{cap} live) — requeueing "
        f"{item_ref(display_id)} instead of dispatching"
    )
    if on_refuse is not None:
        on_refuse(reason)
    stranded = _requeue_over_cap(
        issue_num,
        epic_ref,
        store,
        config,
        item_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
        provider=provider,
        run_id=run_id,
    )
    return _with_stranded_claim(
        DispatchOutcome(
            launched=False,
            exit_code=1,
            changed_files=[],
            category="requeued_over_cap",
            reason=reason,
        ),
        stranded,
    )


@dataclass(frozen=True)
class _ProfileSnapshot:
    """The approved addendum one dispatch read off the host store (#3022).

    A holder rather than the bare value, so "read, and there is none" differs
    from "not read yet".
    """

    addendum: ProfileAddendum | None


@dataclass
class _DispatchContext:
    """Per-dispatch state shared across the extracted phases of
    ``execute_dispatch``.

    Built once at the top of ``execute_dispatch`` from the validated issue and
    call arguments, then threaded read-only into each phase helper so the
    phases stay independently readable without re-deriving item id, branch, or
    the refusal callback. ``refuse_rollback`` is the caller's best-effort
    claim-retention hook (see ``execute_dispatch``); ``start_monotonic``
    is the shared clock for early-exit dispatch-log durations.
    """

    config: DispatchConfig
    store: RunStore | None
    notifier: Notifier | None
    force_budget: bool
    approved: bool
    interactive: bool
    model_override: str | None
    merge_context: str | None
    epic_ref: str | None
    provider: str
    issue_num: int
    issue_title: str
    issue_body: str
    complexity: str
    item_id: str
    branch: str
    start_monotonic: float
    refuse_rollback: Callable[[str], None]
    terminal_failure: Callable[[str, str], None] | None
    run_kind: str
    fleet_worker: bool = False
    worktree_dir: Path | None = None
    run_id: str | None = None
    expected_reservation_run_id: str | None = None
    expected_reservation_generation: str | None = None
    on_admitted: Callable[[str], None] | None = None
    admitted_run_id: str | None = None
    claim_launch_started: bool = False
    container_cleanup_pending: bool = False
    publication_style: str = "agent"
    publication_base_sha: str = ""
    # The plan sha the ledger holds for this body, set once the plan is
    # recorded (#2290). None until then, and after a failed record: the prompt
    # only claims a ledger row when there is one.
    recorded_plan_sha: str | None = None
    checkout_generation: str = ""
    checkout_binding: worktree.CheckoutDirectoryBinding | None = None
    checkout_abandonment: CheckoutAbandonment | None = None
    root_checkout_before: tuple[str, str, str] | None = None
    # Test-only escape for the no-store-no-paid-run gate (#1550 review F5).
    allow_unrecorded: bool = False
    launch_state: LaunchState = field(default_factory=LaunchState)
    # The dispatch's one read of the approved addendum; see _dispatch_addendum.
    profile_snapshot: _ProfileSnapshot | None = None


@dataclass
class _LaunchResult:
    """Outcome of the runner-launch phase.

    ``early`` is a short-circuit ``DispatchOutcome`` (a pre-spawn refusal that
    lost the atomic admit race, or a mid-dispatch exception) that the caller
    must return immediately. On a live run it is ``None`` and ``result`` /
    ``worktree_dir`` / ``run_sandboxed`` carry the launched agent's result and
    workspace forward to result collection.
    """

    early: DispatchOutcome | None = None
    result: DispatchResult | None = None
    worktree_dir: Path | None = None
    run_sandboxed: bool = False


@dataclass
class _LaunchSetup:
    """Mutable workspace state retained if launch raises before completion."""

    worktree_dir: Path | None = None
    run_sandboxed: bool = False


def _capture_root_checkout_state(
    root: Path, *, base_ref: str = "main"
) -> tuple[str, str, str] | None:
    """Capture the operator checkout's HEAD, symbolic branch, and base ref.

    Dispatch worktrees have their own checkout, but all worktrees share the
    repository's ref database.  This small snapshot is deliberately taken from
    the operator project root, not from the assigned worktree, so a misplaced
    agent command cannot rewrite ``main`` without leaving evidence.
    """
    # Some callers (notably isolated characterization tests) intentionally do
    # not run subprocess preflight.  In that state there is no trustworthy
    # snapshot to take; the live dispatch path has already required git during
    # its admission gates, so this is only a compatibility guard for those
    # callers.
    if "git" not in TOOL_PATHS:
        return None
    head = run(["git", "-C", str(root), "rev-parse", "HEAD"], check=False)
    branch = run(
        ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"], check=False
    )
    ref = run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"refs/heads/{base_ref}"],
        check=False,
    )
    if head.returncode or branch.returncode or ref.returncode:
        return None
    return head.stdout.strip(), branch.stdout.strip(), ref.stdout.strip()


def _root_checkout_state_changed(
    before: tuple[str, str, str], after: tuple[str, str, str]
) -> bool:
    """Return whether the operator checkout or configured base ref moved."""
    return before != after


def _root_checkout_integrity_check(
    ctx: _DispatchContext, *, run_id: str | None = None
) -> str | None:
    """Audit and report operator-checkout movement after an agent run.

    Returns the incident reason so the caller publishes the same wording it
    attested, or ``None`` when the checkout is verified intact. A lost snapshot
    and a moved ref are distinct incidents; collapsing them to one boolean made
    the terminal failure claim the checkout had moved when it merely could not
    be read.
    """
    before = ctx.root_checkout_before
    if before is None:
        return None
    base_ref = ctx.config.branches.base or "main"
    after = _capture_root_checkout_state(project_root(), base_ref=base_ref)
    if after is None:
        after_payload = None
        reason = "could not capture operator checkout HEAD/ref state after dispatch"
    elif not _root_checkout_state_changed(before, after):
        return None
    else:
        after_payload = {
            "head": after[0],
            "branch": after[1],
            "ref": after[2],
        }
        reason = "operator checkout HEAD/ref state moved during dispatch"
    audit.emit(
        ctx.store,
        "root_checkout_integrity_violation",
        actor=resolve_actor(),
        run_id=run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        repo=ctx.config.project.repo or None,
        payload={
            "base_ref": base_ref,
            "before": {"head": before[0], "branch": before[1], "ref": before[2]},
            "after": after_payload,
            "reason": reason,
        },
    )
    _error(f"Dispatch integrity incident for {item_ref(ctx.item_id)}: {reason}")
    return reason


def _review_capacity_pressure(
    config: DispatchConfig, store: RunStore | None
) -> tuple[int, int] | None:
    """``(holds, limit)`` for the delivery repository, or None when unknowable.

    Deliberately local-only and read-only: this is a courtesy warning on the
    way into a dispatch, not the capacity decision. The decision is the atomic
    reservation the publication itself has to win, which counts reconciled
    remote obligations too and refuses outright when it cannot see them.
    """
    repo = config.project.repo
    counter = getattr(store, "pending_review_count", None)
    if not repo or counter is None:
        return None
    try:
        policy = policy_from_config(config)
        return counter(repo), policy.max_pending_reviews
    except Exception as exc:  # noqa: BLE001 — a warning may never sink a dispatch
        _logger.debug(
            "review capacity could not be read: %s",
            redact_credentials(exception_text(exc)),
        )
        return None


def _warn_if_review_capacity_full(
    config: DispatchConfig, store: RunStore | None, epic_ref: str | None = None
) -> None:
    """Tell a direct dispatcher the review queue is full, then let them through.

    Only a direct dispatch is warned. A supervised driver gets I15's frontier
    backpressure instead, which is a decision rather than a note.
    """
    pressure = (
        None if epic_ref is not None else _review_capacity_pressure(config, store)
    )
    if pressure is None:
        return
    holds, limit = pressure
    if holds < limit:
        return
    _warn(
        f"{config.project.repo} is holding {holds} of {limit} human reviews. "
        "This dispatch will run, but its publication stays held until a review "
        "slot frees."
    )


def _emit_admission_refusal(ctx: _DispatchContext, outcome: DispatchOutcome) -> None:
    """Audit a refusal that ends before the pending run row exists."""
    _emit_early_exit(
        ctx.store,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        complexity=ctx.complexity,
        runner="",
        model="",
        exit_code=outcome.exit_code,
        duration_seconds=time.monotonic() - ctx.start_monotonic,
        error_category=outcome.category,
        repo=ctx.config.project.repo or None,
        run_id=ctx.run_id,
        epic_ref=ctx.epic_ref,
        reason=outcome.reason,
    )


def _finish_refusal(ctx: _DispatchContext, reason: str) -> None:
    """Handle only admitted claims for reservation-bound children."""
    if ctx.expected_reservation_run_id is not None and ctx.admitted_run_id is None:
        return
    # The reason reaches claim-retention records; exception text can carry
    # proxy userinfo or other credentials, so it is redacted at this one seam.
    ctx.refuse_rollback(redact_credentials(reason))


def _rollback_admitted_claim(ctx: _DispatchContext) -> None:
    """Finish refusal handling for this child's unlaunched claim."""
    if ctx.container_cleanup_pending:
        return
    if (
        ctx.on_admitted is None
        or ctx.admitted_run_id is None
        or ctx.claim_launch_started
    ):
        return
    _finish_refusal(ctx, "dispatch ended before launch")
    if ctx.store is not None:
        row = ctx.store.get(ctx.admitted_run_id)
        if row is not None and row.status == "pending":
            ctx.store.save(
                replace(
                    row,
                    status="failure",
                    exit_code=1,
                    error_category="dispatch_child_refused",
                    finished_at=datetime.now(UTC),
                )
            )


def _mark_claim_launch_started(ctx: _DispatchContext) -> None:
    """Transfer the admitted claim to the runner at its launch boundary."""
    if (
        ctx.on_admitted is not None
        and ctx.store is not None
        and ctx.admitted_run_id is not None
    ):
        try:
            audit.emit(
                ctx.store,
                "linear_claim_launched",
                actor=Actor("policy", "dispatch-admission"),
                run_id=ctx.admitted_run_id,
                item_id=ctx.item_id,
                payload={"epic_ref": ctx.epic_ref},
                dedup_key=f"linear_claim_launched:{ctx.admitted_run_id}",
                strict=True,
            )
        except BaseException as marker_error:
            try:
                _finish_refusal(ctx, "launch marker delivery failed")
            except BaseException as retention_error:
                raise marker_error from retention_error
            raise
    ctx.claim_launch_started = True
    ctx.refuse_rollback = RefusalDelivery(None)


def _claim_after_admission(ctx: _DispatchContext, run_id: str) -> None:
    """Claim only after committed admission; retain failed remote handoffs."""
    if ctx.on_admitted is None:
        return
    ctx.admitted_run_id = run_id
    _check_dispatch_reservation(ctx)
    ctx.on_admitted(run_id)
    _check_dispatch_reservation(ctx)


def _check_dispatch_reservation(ctx: _DispatchContext) -> None:
    """Reject a stale parent identity at each pre-admission boundary."""
    if ctx.expected_reservation_run_id is not None:
        capture_dispatch_reservation(
            ctx.store,
            ctx.epic_ref,
            ctx.item_id,
            ctx.run_kind,
            expected_reservation_run_id=ctx.expected_reservation_run_id,
            expected_reservation_generation=ctx.expected_reservation_generation,
        )


def _record_prelaunch_refusal(
    ctx: _DispatchContext, category: str, *, exit_code: int = 1
) -> None:
    """Finish the callback before publishing terminal refusal evidence."""
    _finish_refusal(ctx, category)
    if ctx.expected_reservation_run_id is None:
        return
    _check_dispatch_reservation(ctx)
    assert ctx.store is not None and ctx.epic_ref is not None
    _record_refusal_row(
        ctx.store,
        ctx.config,
        ctx.epic_ref,
        ctx.issue_num,
        ctx.item_id,
        error_category=category,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
        raise_on_replaced=True,
        exit_code=exit_code,
    )


def _refuse_replaced_reservation(ctx: _DispatchContext) -> DispatchOutcome:
    """Record a fresh, unbound refusal without releasing or publishing anything."""
    from kew.run_store import RunRecord

    if ctx.admitted_run_id is not None:
        _finish_refusal(ctx, "reservation_replaced")
    ctx.run_id = str(uuid.uuid4())
    if ctx.store is not None:
        now = datetime.now(UTC)
        from kew.dispatch_preflight import record_owned_refusal

        record_owned_refusal(
            ctx.store,
            RunRecord(
                run_id=ctx.run_id,
                issue_num=ctx.issue_num,
                item_id=ctx.item_id,
                branch=ctx.branch,
                run_kind=ctx.run_kind,
                status="refused",
                exit_code=2,
                started_at=now,
                finished_at=now,
                error_category="reservation_replaced",
                repo=ctx.config.project.repo or None,
            ),
            epic_ref=ctx.epic_ref or "",
            expected_reservation_run_id=None,
        )
    outcome = DispatchOutcome(
        launched=False,
        exit_code=2,
        changed_files=[],
        run_id=ctx.run_id,
        category="reservation_replaced",
        reason="reservation_replaced",
    )
    _emit_admission_refusal(ctx, outcome)
    return outcome


def _warn_if_scheduler_does_not_own_cap_warning(ctx: _DispatchContext) -> None:
    """Warn direct dispatches when no scheduler parent owns the warning."""
    if ctx.fleet_worker:
        return
    _warn_if_cap_conflict(ctx.config, ctx.store, ctx.issue_num, ctx.item_id)


def _run_admission_gates(
    ctx: _DispatchContext,
) -> tuple[DispatchOutcome | None, Callable[[], None] | None]:
    """Pre-spawn admission gates: fleet kill-switch, worker cap, budget.

    Returns ``(early_outcome, admit_budget_check)``. A non-None ``early_outcome``
    is a refusal the caller must return immediately (callback already finished). On
    proceed it is ``None`` and ``admit_budget_check`` is the atomic admit-time
    reservation gate to fold into the runner launch (or ``None`` under --force /
    no store).
    """
    store = ctx.store
    config = ctx.config
    issue_num = ctx.issue_num
    item_id = ctx.item_id
    epic_ref = ctx.epic_ref
    complexity = ctx.complexity
    force_budget = ctx.force_budget

    # Fleet kill-switch latch — refuse to dispatch while stopped.
    if store is not None:
        latch = store.get_fleet_stopped()
        if isinstance(latch, dict):
            _record_prelaunch_refusal(ctx, "fleet_stopped")
            from kew.fleet import fleet_stop_message

            reason = fleet_stop_message(latch)
            _error(f"Refusing to dispatch #{issue_num}: {reason}")
            # Caller already claimed the issue (in-progress) — undo that.
            stranded = _hand_claim_back(
                issue_num,
                config,
                store,
                epic_ref=epic_ref,
                provider=ctx.provider,
                item_id=item_id,
                run_id=ctx.run_id,
            )
            outcome = _with_stranded_claim(
                DispatchOutcome(
                    launched=False,
                    exit_code=1,
                    changed_files=[],
                    category="fleet_stopped",
                    reason=reason,
                ),
                stranded,
            )
            _emit_admission_refusal(ctx, outcome)
            return outcome, None

    # Cap-conflict warning (CS-7): the runtime kill cap can overshoot the
    # per-issue budget before the preflight gate below ever sees the spend.
    # Fires regardless of --force so the mismatch is always surfaced.
    _warn_if_scheduler_does_not_own_cap_warning(ctx)

    # Human-review capacity (I14). A person dispatching directly is TOLD the
    # delivery repository's review queue is full; they are not stopped, because
    # a direct dispatch is a decision and this cap governs publication, not
    # work. The E5 publication that run produces still has to win the atomic
    # reservation before anything leaves the host, so the warning costs nothing
    # and hides nothing.
    _warn_if_review_capacity_full(config, store, epic_ref)

    # Fleet-wide worker cap (#721) — enforced at preflight so N live runs
    # block the N+1th dispatch regardless of which process dispatches. The
    # store-backed live count is the fleet's true concurrency (every worker
    # subprocess and every epic-driver child share one dispatch.db), unlike
    # the scheduler's per-process in-flight set. Requeue rather than spawn a
    # doomed (N+1)th agent; the cap releases as live runs finish.
    capped = _enforce_worker_cap(
        store,
        config,
        issue_num,
        epic_ref,
        item_id,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
        on_refuse=partial(_finish_refusal, ctx),
        provider=ctx.provider,
        run_id=ctx.run_id,
    )
    if capped is not None:
        _emit_admission_refusal(ctx, capped)
        return capped, None

    # Budget pre-check — cheap read-only snapshot of committed spend. The
    # atomic admit-time gate below (folded into try_admit_run) is what closes
    # the parallel-overshoot race the preflight alone left open (H2).
    if store is not None and not force_budget:
        try:
            check_budget(store, config.budget, issue_num, item_id=item_id)
        except BudgetExceeded as exc:
            _finish_refusal(ctx, str(exc))
            outcome = _refuse_budget(
                store,
                config,
                issue_num,
                epic_ref,
                complexity,
                exc,
                ctx.start_monotonic,
                item_id=item_id,
                run_id=ctx.run_id,
                expected_reservation_run_id=ctx.expected_reservation_run_id,
                expected_reservation_generation=ctx.expected_reservation_generation,
            )
            outcome.category = "budget_exceeded"
            outcome.reason = redact_credentials(exception_text(exc))
            return outcome, None

    # The admit-time reservation gate, folded into the same BEGIN IMMEDIATE
    # transaction as the worker-slot claim (H2). Pending rows are valued at the
    # runtime kill cap (worst case) so N contending admits against a
    # nearly-exhausted cap serialize and at most one squeaks under. Skipped
    # under --force (budget bypass) and when there is no store to reserve in.
    admit_budget_check: Callable[[], None] | None = None
    if store is not None and not force_budget:
        _reservation_store = store
        _reservation_item_id = item_id

        def _admit_budget_check() -> None:
            check_budget(
                _reservation_store,
                config.budget,
                issue_num,
                item_id=_reservation_item_id,
                pending_reservation_usd=config.agent.max_budget_usd,
            )

        admit_budget_check = _admit_budget_check

    _check_dispatch_reservation(ctx)
    return None, admit_budget_check


def _hold_or_confirm(
    ctx: _DispatchContext, estimate: Estimate, threshold: float
) -> DispatchOutcome | None:
    """Estimate meets the approval threshold: prompt interactively when a TTY is
    attached (confirm → proceed, decline → return to ready), otherwise hold the
    issue for out-of-band approval. Returns a ``DispatchOutcome`` on
    hold/decline (exit 0, claim NOT rolled back) or ``None`` to proceed."""
    config = ctx.config
    store = ctx.store
    issue_num = ctx.issue_num
    if ctx.interactive and sys.stdin.isatty():
        if confirm_dispatch(
            issue_num=issue_num, estimate=estimate, threshold_usd=threshold
        ):
            log_approval_event(
                store,
                event="approval_confirmed",
                issue_num=issue_num,
                estimate=estimate,
                threshold_usd=threshold,
                repo=config.project.repo or None,
            )
            return None
        _warn(
            f"Approval declined for #{issue_num} — returning to '{config.labels.ready}'"
        )
        _record_prelaunch_refusal(ctx, "approval_declined", exit_code=0)
        try:
            mark_ready_from_approval(issue_num, config)
        except Exception as exc:
            _warn(
                redact_credentials(
                    f"Failed to relabel #{issue_num} as ready: {exception_text(exc)}"
                )
            )
        log_approval_event(
            store,
            event="approval_declined",
            issue_num=issue_num,
            estimate=estimate,
            threshold_usd=threshold,
            repo=config.project.repo or None,
        )
        return DispatchOutcome(
            launched=False,
            exit_code=0,
            changed_files=[],
            category="approval_declined",
            reason="operator declined the dispatch estimate",
        )
    _record_prelaunch_refusal(ctx, "approval_hold", exit_code=0)
    _warn(
        f"Holding #{issue_num} for approval: estimated "
        f"${estimate.usd:.2f} ({estimate.basis}) meets threshold "
        f"${threshold:.2f} — release with "
        f"`kew dispatch {issue_num} --approve`"
    )
    try:
        mark_needs_approval(issue_num, config)
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Failed to apply approval hold to #{issue_num}: {exception_text(exc)}"
            )
        )
    log_approval_event(
        store,
        event="approval_hold",
        issue_num=issue_num,
        estimate=estimate,
        threshold_usd=threshold,
        repo=config.project.repo or None,
    )
    notify_approval_hold(
        config,
        issue_num=issue_num,
        estimate_usd=estimate.usd,
        basis=estimate.basis,
        threshold_usd=threshold,
    )
    return DispatchOutcome(
        launched=False,
        exit_code=0,
        changed_files=[],
        category="approval_hold",
        reason=f"estimated cost meets the ${threshold:.2f} approval threshold",
    )


def _run_approval_gate(ctx: _DispatchContext) -> DispatchOutcome | None:
    """Approval gate — policy on top of budget. Runs only when configured;
    --force (budget bypass) never bypasses it. The only ways past the
    gate are --approve, an interactive yes, or raising the threshold.

    Returns a ``DispatchOutcome`` when the issue is held or the interactive
    prompt is declined (both exit 0, claim intentionally NOT rolled back), or
    ``None`` to proceed.

    Ordering note: like the budget pre-check (which calls mark_failed before
    the GitHub connectivity check), the gate's label updates run before that
    check. If GitHub is unreachable the label swap fails best-effort (warned,
    never fatal) while the local approval event is still recorded — the
    structured log is authoritative. This is self-healing: an un-held issue
    keeps its claimed labels (no 'ready', so the queue won't re-pick it) until
    the orphan reset or a retry re-runs the gate. Kept consistent with the
    budget pre-check rather than reordered.
    """
    config = ctx.config
    store = ctx.store
    issue_num = ctx.issue_num
    complexity = ctx.complexity
    approved = ctx.approved
    model_override = ctx.model_override

    threshold = config.approval.threshold_usd
    if approved and threshold is None:
        _logger.debug(
            "--approve passed for #%d but [approval] is not configured — no-op",
            issue_num,
        )
        return None
    if threshold is None:
        return None

    gate_model = (
        model_override or resolve_runner(config, complexity=complexity).model or ""
    )
    estimate = estimate_dispatch_cost(
        store, complexity=complexity, model=gate_model, budget=config.budget
    )
    _check_dispatch_reservation(ctx)
    if approved:
        log_approval_event(
            store,
            event="approval_granted",
            issue_num=issue_num,
            estimate=estimate,
            threshold_usd=threshold,
            repo=config.project.repo or None,
        )
        return None
    if estimate.usd >= threshold:
        return _hold_or_confirm(ctx, estimate, threshold)

    return None


def _check_connectivity_gate(ctx: _DispatchContext) -> DispatchOutcome | None:
    """GitHub connectivity pre-check. Returns an exit-3 ``DispatchOutcome`` when
    GitHub is unreachable or auth-failed, or ``None`` to proceed.
    """
    config = ctx.config
    store = ctx.store
    issue_num = ctx.issue_num
    item_id = ctx.item_id
    complexity = ctx.complexity

    try:
        reachable = _check_github_connectivity(config)
    except GitHubAuthError as exc:
        _record_prelaunch_refusal(ctx, "github_auth", exit_code=3)
        _error(redact_credentials(f"{exception_text(exc)} — cannot dispatch"))
        _emit_early_exit(
            store,
            issue_num=issue_num,
            item_id=item_id,
            complexity=complexity,
            runner="",
            model="",
            exit_code=3,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category="github_auth",
            repo=config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
        )
        return DispatchOutcome(
            launched=False,
            exit_code=3,
            changed_files=[],
            category="github_auth",
            reason=redact_credentials(exception_text(exc)),
        )
    if not reachable:
        _record_prelaunch_refusal(ctx, "github_unreachable", exit_code=3)
        _error("GitHub API is unreachable — cannot dispatch")
        _emit_early_exit(
            store,
            issue_num=issue_num,
            item_id=item_id,
            complexity=complexity,
            runner="",
            model="",
            exit_code=3,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category="github_unreachable",
            repo=config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
        )
        return DispatchOutcome(
            launched=False,
            exit_code=3,
            changed_files=[],
            category="github_unreachable",
            reason="GitHub API is unreachable",
        )
    return None


@dataclass
class _EarlyRunInfo:
    """Runner/model captured as soon as they resolve, so an early-exit dispatch
    log entry can name them even when the failure is later than resolution."""

    runner: str = ""
    model: str = ""


def _preflight_pricing(ctx: _DispatchContext, resolved: ResolvedRoute) -> None:
    """Pricing preflight (#934): a governed dispatch must never meter an
    unpriced model at $0 — that corrupts budgets, the breaker baseline, and the
    cost-per-merged-PR headline. Fail closed (raise → caller refuses to
    dispatch) so an unpriced routable model aborts instead of running "for
    free". Only a concrete model string is checked: an empty model means the
    runner uses its own default, and free-provider models are $0 by design —
    both are exempt in ``require_pricing_key``. Config ``[pricing]`` overrides
    count as priced, matching how dispatch meters cost downstream.
    """
    config = ctx.config
    _preflight_model = ctx.model_override or resolved.model or ""
    if isinstance(_preflight_model, str) and _preflight_model:
        require_public_model(_preflight_model)
        require_pricing_key(
            _preflight_model,
            effective_pricing(build_pricing_overrides(config.pricing.overrides)),
        )
        lifecycle = getattr(config, "model_eol", None)
        entries = getattr(lifecycle, "snapshot_models", {}) if lifecycle else {}
        for warning in check_lifecycle([_preflight_model], entries):
            _warn(f"Model lifecycle: {warning}")


def _emit_sandbox_resolved(
    ctx: _DispatchContext,
    runner: str,
    *,
    posture: str,
    runtime: str | None,
    image: str | None,
    reason: str | None = None,
    egress_verification: str = "unavailable",
    egress_route: Literal["direct", "sidecar", "openshell"] = "direct",
    checkout_dir: Path | None = None,
    synthetic_credential_name: str | None = None,
    direct_transport: bool = True,
) -> None:
    """Attest one dispatch's containment posture on the audit ledger (#654).

    Emitted on EVERY outcome including a refusal, so a dispatch that never ran
    because its runner could not be contained leaves the same kind of record as
    one that ran guard-only — otherwise the ledger's "was this contained?"
    answer for a refused run is silence (#1144).

    ``reason`` is the machine-readable refusal cause (``SandboxRuntimeUnavailable
    .reason``), present only on ``posture="refused"``. Without it the several
    causes ``refused`` covers — no runtime under fail-closed, an unmapped image,
    an undeclared credential — are byte-identical on the chain, and a query
    cannot tell "this host cannot contain" from "kew cannot contain this
    runner". Present-as-None on every other posture so the payload schema stays
    uniform and consumers never KeyError.
    """
    sandbox_network.emit_resolved_posture(
        ctx.store,
        runner=runner,
        config=ctx.config.sandbox,
        posture=posture,
        runtime=runtime,
        image=image,
        reason=reason,
        run_id=ctx.run_id,
        checkout_dir=checkout_dir,
        repo=ctx.config.project.repo or None,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        egress_route=egress_route,
        synthetic_credential_name=synthetic_credential_name,
        certified=egress_verification
        in {"sidecar-certified", sandbox_network.OPENSHELL_ROUTE_EGRESS_VERIFICATION},
        egress_verification=egress_verification,
        direct_transport=direct_transport,
    )


def _ensure_host_only_sandbox_posture(
    ctx: _DispatchContext, runner: str, decision: SandboxDecision
) -> None:
    """Refuse a continuation when its resolved posture requires containment."""
    if not decision.sandboxed:
        return
    refusal = SandboxRuntimeUnavailable(
        "Sandbox containment is required, but a preserved continuation "
        "must run against its existing worktree",
        reason=REFUSAL_CONTINUATION_HOST_ONLY,
        runtime=decision.runtime,
        image=decision.image,
    )
    _emit_sandbox_resolved(
        ctx,
        runner,
        posture="refused",
        runtime=refusal.runtime,
        image=refusal.image,
        reason=refusal.reason,
        egress_verification="unavailable",
    )
    _warn(str(refusal))
    raise refusal


def _attest_sandbox_refusal(
    ctx: _DispatchContext,
    runner: str,
    exc: SandboxRuntimeUnavailable,
) -> None:
    """Record a resolved containment refusal before propagating it."""
    _emit_sandbox_resolved(
        ctx,
        runner,
        posture="refused",
        runtime=exc.runtime,
        image=exc.image,
        reason=exc.reason,
        egress_verification="unavailable",
    )
    _warn(redact_credentials(exception_text(exc)))


def _check_initial_host_capacity(
    ctx: _DispatchContext, runner: str, decision: SandboxDecision
) -> None:
    """Probe every device a launch writes to, before checkout materialization.

    The checkout is cloned under the work root (possibly a different device
    from the project); an ENOSPC inside the clone would otherwise surface
    as "internal error during dispatch" with no refusal row.
    """
    try:
        for anchor in _capacity_anchors():
            check_host_capacity(
                None,
                anchor=anchor,
                runtime=decision.runtime,
                image=decision.image,
            )
    except SandboxRuntimeUnavailable as exc:
        _attest_sandbox_refusal(ctx, runner, exc)
        raise


def _capacity_anchors() -> list[Path]:
    """The project device and the work root's device, deduplicated."""
    anchors = [Path.cwd(), work_dirs.work_root()]
    seen: list[Path] = []
    for anchor in anchors:
        if anchor not in seen:
            seen.append(anchor)
    return seen


def _report_sandbox_notice(decision: SandboxDecision) -> None:
    """Report a resolved containment notice at its required severity."""
    if decision.notice is None:
        return
    report = _log if decision.sandboxed else _warn
    report(decision.notice)


def _resolve_and_apply_sandbox(
    ctx: _DispatchContext,
    runner: str,
    *,
    host_only: bool = False,
    defer_attestation: bool = False,
    inference_transport: ResolvedInferenceTransport | None = None,
) -> SandboxDecision:
    """Resolve the effective sandbox posture (tri-state auto/true/false),
    surface any notice / egress warning, and record the attested posture on the
    audit ledger. ``auto`` sandboxes when a runtime is present and downgrades to
    the guard-only host path with a loud notice otherwise; ``true`` fails closed
    (raise → caller refuses to dispatch).

    ``runner`` is the already-resolved runner name: it picks the image, so the
    same committed config can no longer contain a non-claude node in the
    claude-only image just because a claude credential is in the env (#1144).
    ``host_only`` is used for an existing continuation worktree, which cannot
    be handed to the container path without replacing the preserved checkout.
    It is passed INTO the resolution as well as checked after it: a runner that
    holds its own boundary must be asked that question with no container on
    offer, or it defers to one this launch was never going to get and is then
    refused by :func:`_ensure_host_only_sandbox_posture`.
    """
    config = ctx.config
    try:
        sandbox_decision = _resolve_dispatch_sandbox(
            config, runner, inference_transport, ctx.store, host_only=host_only
        )
    except SandboxRuntimeUnavailable as exc:
        # Fail closed — but attest the refusal first, so `kew audit` shows the
        # run was refused for containment rather than showing nothing at all.
        # The facts come off the exception, not hardcoded: a refusal that
        # happened BECAUSE a runtime was present must not attest runtime=None,
        # which is exactly what the no-runtime refusal attests (#1144 review).
        _attest_sandbox_refusal(ctx, runner, exc)
        raise
    sandbox_decision = replace(
        sandbox_decision,
        egress_verification=_resolve_egress_verification(
            sandbox_decision,
            inference_transport,
        ),
    )
    if (
        sandbox_decision.egress_verification
        == sandbox_network.DIRECT_ROUTE_EGRESS_VERIFICATION
    ):
        _log(f"Egress verification: {sandbox_decision.egress_verification}")
    if host_only:
        _ensure_host_only_sandbox_posture(ctx, runner, sandbox_decision)
        _log(
            "Sandbox: preserved continuation uses the existing worktree with "
            f"{sandbox_decision.posture} posture"
        )
    _report_sandbox_notice(sandbox_decision)
    if sandbox_decision.sandboxed:
        check_runtime_memory_capacity(config.sandbox.runtime, config.sandbox)
        # Fails closed: a host under the free-space floor or a checkout over
        # quota would breach at the watchdog's first tick, before the
        # container exists (#1431 round 5).
        _check_initial_host_capacity(ctx, runner, sandbox_decision)
        # Loud warning if an operator opted into full egress with a credential
        # injected — one outbound request can exfiltrate the key (#935). Keyed
        # on containment ALONE: this is a credential-exfiltration warning, not
        # an image fact, so it must not ride on whether an image happened to be
        # resolved (#1144 review). The runner names WHICH credential is at risk
        # — the one this container is actually handed, by name (#1146).
        egress_warning = open_egress_warning(config.sandbox.network, runner=runner)
        if egress_warning:
            _warn(egress_warning)
        # The gateway runtime delivers no docker argv, so the caps and the
        # proxy-inherit setting have nowhere to land. The ledger records that
        # they did not apply; this is where the operator who configured one
        # hears it, rather than discovering it from a run report (PR #2401
        # review).
        ignored_settings = openshell_ignored_argv_settings_warning(config.sandbox)
        if ignored_settings:
            _warn(ignored_settings)
        # The staleness check is the one that genuinely needs the image. A
        # sandboxed decision always carries its runner's image — resolution
        # refuses to return sandboxed=True without one (#1144) — so the guard
        # is a type narrowing, not a silent skip.
        if sandbox_decision.image:
            stale_warning = check_agent_image_staleness(
                config.sandbox.runtime, sandbox_decision.image, runner=runner
            )
            if stale_warning:
                _warn(stale_warning)
    if not defer_attestation:
        _emit_sandbox_resolved(
            ctx,
            runner,
            posture=sandbox_decision.posture,
            runtime=sandbox_decision.runtime,
            image=sandbox_decision.image,
            egress_verification=sandbox_decision.egress_verification,
            egress_route=_contained_egress_route(inference_transport, sandbox_decision),
            direct_transport=direct_route(inference_transport),
        )
    return sandbox_decision


def _contained_egress_route(
    inference_transport: ResolvedInferenceTransport | None,
    decision: SandboxDecision | None = None,
) -> Literal["direct", "sidecar", "openshell"]:
    """Identify the route independently of its claimed verification value.

    Delegates the derivation itself to :func:`kew.sandbox.contained_egress_route`
    so this writer and :func:`kew.sandbox.attest_flow_posture` cannot disagree
    about which route a container ran under; all this adds is reading the
    posture and runtime off a :class:`SandboxDecision`.
    """
    return sandbox_network.contained_egress_route(
        posture=(
            sandbox_network.POSTURE_SANDBOXED
            if decision is not None and decision.sandboxed
            else ""
        ),
        runtime=decision.runtime if decision is not None else None,
        sidecar=inference_transport is not None and inference_transport.sidecar,
    )


def _resolve_egress_verification(
    decision: SandboxDecision,
    inference_transport: ResolvedInferenceTransport | None,
) -> str:
    """Classify evidence available before contained launch admission."""
    if not decision.sandboxed or decision.runtime is None:
        return "unavailable"
    route = _contained_egress_route(inference_transport, decision)
    if route == "sidecar":
        # Sidecar configuration is intent, not packet evidence. The launch
        # path upgrades this value only after prepare_sidecar_worker accepts
        # matching, fresh, passing certification for both immutable images.
        return sandbox_network.classify_egress_verification(route="sidecar")
    if route == sandbox_network.OPENSHELL_EGRESS_ROUTE:
        # Already proven: resolution refuses an OpenShell dispatch whose
        # boundary has no fresh passing certification, so reaching here means
        # the matching proof was read off the chain (#2319).
        return sandbox_network.classify_egress_verification(
            route=sandbox_network.OPENSHELL_EGRESS_ROUTE, certified=True
        )
    return sandbox_network.classify_egress_verification(route="direct")


def _resolve_dispatch_sandbox(
    config: DispatchConfig,
    runner: str,
    inference_transport: ResolvedInferenceTransport | None,
    store: object | None = None,
    host_only: bool = False,
) -> SandboxDecision:
    """Resolve direct or sidecar credential admission for one transport.

    ``store`` is the ledger the OpenShell egress gate reads its certification
    from. A caller without one still resolves every other runtime; an OpenShell
    dispatch is refused, because "no ledger to check" is not a proof.

    ``host_only`` is the continuation shape, forwarded so the resolver asks the
    runner's containment question with no boundary in it: this launch takes the
    preserved worktree on the host, so there is no container on offer for it to
    defer to. Without it a ``codex`` continuation on the gateway runtime
    resolved ``sandboxed`` and then died at
    :func:`_ensure_host_only_sandbox_posture`, where the same continuation on
    docker ran ``native`` (PR #2461 review round 3).
    """
    if inference_transport is not None and inference_transport.sidecar:
        return resolve_effective_sidecar_sandbox(
            config.sandbox,
            runner=runner,
            synthetic_credential_env_name=inference_transport.worker_credential_env_name,
            store=store,
            host_only=host_only,
        )
    return resolve_effective_sandbox(
        config.sandbox,
        runner=runner,
        store=store,
        direct=direct_route(inference_transport),
        host_only=host_only,
    )


def _attest_direct_sandbox_resolution(
    ctx: _DispatchContext,
    runner: str,
    decision: SandboxDecision,
    inference_transport: ResolvedInferenceTransport | None,
    checkout_dir: Path,
) -> None:
    """Attest direct egress before the contained launch.

    "Direct egress" is the route, not the transport: a switchyard launch
    without a sidecar is attested here too, which is why the transport's own
    ``direct`` fact travels separately (:func:`direct_route`).
    """
    if inference_transport is not None and inference_transport.sidecar:
        return
    _emit_sandbox_resolved(
        ctx,
        runner,
        posture=decision.posture,
        runtime=decision.runtime,
        image=decision.image,
        egress_verification=decision.egress_verification,
        egress_route=_contained_egress_route(inference_transport, decision),
        checkout_dir=checkout_dir,
        direct_transport=direct_route(inference_transport),
    )


def _attest_sidecar_launch(
    ctx: _DispatchContext,
    runner: str,
    decision: SandboxDecision,
    inference_transport: ResolvedInferenceTransport | None,
    checkout_dir: Path,
    *,
    admitted: bool = False,
) -> None:
    """Record pending setup or completed worker and profile admission."""
    if inference_transport is None or not inference_transport.sidecar:
        return
    _emit_sandbox_resolved(
        ctx,
        runner,
        posture=decision.posture,
        runtime=decision.runtime,
        image=decision.image,
        egress_verification="sidecar-certified" if admitted else "unavailable",
        checkout_dir=checkout_dir,
        egress_route="sidecar",
        synthetic_credential_name=inference_transport.worker_credential_env_name,
    )


def _bind_contained_checkout(
    ctx: _DispatchContext,
    checkout: Path,
    *,
    runner: str,
    decision: SandboxDecision,
    inference_transport: ResolvedInferenceTransport | None,
) -> str:
    """Bind the fresh contained checkout before the launch owns attestation."""
    try:
        generation = _bind_initial_checkout(ctx, checkout)
        _require_bound_checkout_generation(
            ctx,
            checkout,
            generation,
            boundary="contained launch handoff",
            honor_unrecorded=True,
        )
    except BaseException:
        # Checkout admission precedes sidecar setup; persist its pending
        # posture before propagating this refusal.
        _attest_sidecar_launch(ctx, runner, decision, inference_transport, checkout)
        raise
    return generation


def _launch_sandboxed(
    ctx: _DispatchContext,
    checkout_dir: Path,
    early: _EarlyRunInfo,
    admit_budget_check: Callable[[], None] | None,
    *,
    image: str,
    sandbox_decision: SandboxDecision,
    inference_transport: ResolvedInferenceTransport | None,
    checkout_generation: str | None = None,
) -> DispatchResult:
    """Sandboxed path: build the prompt against a standalone checkout and launch
    the container; the host brokers push/PR.

    ``image`` comes from the sandbox decision (runner-derived, #1144) — never
    from ``config.sandbox.image``, which only ever named the claude image. The
    same resolved runner (``early.runner``, what the decision keyed off) rides
    down with it, so the container args match the image (#1145) and the
    credential env allowlist names that runner's own keys (#1146) — image,
    flags and credentials cannot describe different agents."""
    config = ctx.config
    sidecar = bool(inference_transport is not None and inference_transport.sidecar)
    _attest_sidecar_launch(
        ctx, early.runner, sandbox_decision, inference_transport, checkout_dir
    )

    # Repo-map parity (spec D5 / R3-7): the sandbox path built prompts with
    # no map at all. Route it through the same seam as the host path.
    repo_map = _build_repo_map(ctx, checkout_dir)
    # The approved addendum comes off the host store, never `checkout_dir`.
    addendum = _dispatch_addendum(ctx)
    prompt = build_prompt(
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        provider=ctx.provider,
        issue_title=ctx.issue_title,
        issue_body=ctx.issue_body,
        complexity=ctx.complexity,
        config=config,
        has_coderabbit="coderabbit" in TOOL_PATHS,
        repo_map=repo_map,
        recently_merged_context=ctx.merge_context,
        sandboxed=True,
        publication_style=_sandboxed_publication_style(config),
        has_repo_skills=(
            early.runner == "claude-code"
            and (checkout_dir / ".claude" / "skills").is_dir()
        ),
        runner_name=early.runner,
        # getattr: the sandboxed launch is also driven with lighter contexts
        # (tests, older callers) that predate the plan fields (#2290 verify).
        recorded_plan_sha=getattr(ctx, "recorded_plan_sha", None),
        # The self-check quotes the rubric the OPERATOR's repo deploys, never
        # the one in `checkout_dir`: a retry or continuation cuts that checkout
        # from the prior attempt's branch, so the rubric there is the previous
        # agent's text and it is interpolated as instructions (#2454 round 2).
        rubric_root=operator_repo_root(),
        profile_addendum=addendum,
    )
    return _run_sandboxed(
        prompt=prompt,
        self_check=rendered_self_check(prompt),
        prompt_profile_version_id=profile_version_id(addendum),
        checkout_dir=checkout_dir,
        config=config,
        image=image,
        runner=early.runner,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        branch=ctx.branch,
        title=ctx.issue_title,
        model=early.model,
        store=ctx.store,
        tracker=ctx.provider,
        worker_cap=config.fleet_concurrency(),
        cap_exclude_issue=ctx.issue_num,
        admit_budget_check=admit_budget_check,
        run_id=ctx.run_id,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
        on_admitted=partial(_claim_after_admission, ctx),
        on_launch=partial(_mark_claim_launch_started, ctx),
        publication_base_sha=ctx.publication_base_sha,
        epic_ref=ctx.epic_ref,
        checkout_generation=checkout_generation or None,
        sidecar_ready=(
            partial(
                _attest_sidecar_launch,
                ctx,
                early.runner,
                sandbox_decision,
                inference_transport,
                checkout_dir,
                admitted=True,
            )
            if sidecar
            else None
        ),
        cost_governor=_contained_cost_governor(ctx, early),
    )


def _contained_cost_governor(
    ctx: _DispatchContext, early: _EarlyRunInfo
) -> ContainedCostGovernor:
    """The mid-run cost authority of a contained dispatch (#1462).

    The same two ceilings the host dispatch arms: the run cap through
    ``budget_watchdog`` (contained, so claude-code is watched too) and the
    circuit breaker's anomaly threshold off the run's (model, complexity,
    kind) bucket. Direct dispatch has no runtime override, so the cap is
    ``agent.max_budget_usd``.
    """
    config = ctx.config
    # getattr: the sandboxed launch is also driven with lighter contexts
    # (tests, older callers) that carry no bucket; theirs is dormant.
    bucket = _ContainedBreakerBucket(
        config=config,
        store=getattr(ctx, "store", None),
        effective_model=early.model or config.agent.model or "",
        complexity=getattr(ctx, "complexity", ""),
        run_kind=getattr(ctx, "run_kind", "dispatch"),
    )
    anomaly_threshold_usd, allowance_usd = _compute_breaker_baseline(bucket)
    return ContainedCostGovernor(
        budget_check=budget_watchdog(
            config.agent.max_budget_usd, runner_name=early.runner, contained=True
        ),
        anomaly_threshold_usd=anomaly_threshold_usd,
        anomaly_compaction_allowance_usd=allowance_usd,
        baseline=bucket.breaker_baseline,
    )


def _trusted_dispatch_base(config: DispatchConfig) -> str:
    """Resolve the dispatch base from the operator remote before agent launch."""
    try:
        remote = publication_remote(config.project.repo or None)
        with work_dirs.work_tempdir("kew-publication-base-") as temp:
            return remote_branch_sha(
                remote,
                config.branches.base or "main",
                scratch=Path(temp),
            )
    except Exception as exc:  # noqa: BLE001 — publication will fail closed later
        _warn(
            redact_credentials(
                f"Could not record the trusted publication base: {exception_text(exc)}"
            )
        )
        return ""


def build_repo_map(
    config: DispatchConfig, worktree_dir: Path, *, hint: str = ""
) -> str:
    """Generate the repo map from a workspace for agent navigation.

    The single shared seam every dispatch call site routes through — host,
    sandbox, and loop (spec D5, "delivery holes" R3-7) — so the map (and the
    future lessons layer) inject uniformly rather than only on the host path.
    Returns "" when ``include_repo_map`` is off. Failure is non-fatal: the
    dispatch continues without a map.
    """
    if not config.prompt.include_repo_map:
        return ""
    # "stable" mode drops the caller's per-issue hint HERE, at the shared
    # seam, so the map (and its hash, hence prefix_key) is constant between
    # main merges on every dispatch path — host, sandbox, and loop callers
    # keep passing their hints unconditionally and this single gate decides.
    if config.prompt.repo_map_mode != "hint":
        hint = ""
    try:
        return generate_repo_map(
            worktree_dir,
            max_tokens=config.prompt.repo_map_tokens,
            hint=hint,
        )
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Repo map generation failed, continuing without it: "
                f"{exception_text(exc)}"
            )
        )
        return ""


def _build_repo_map(ctx: _DispatchContext, worktree_dir: Path) -> str:
    """Ctx adapter over :func:`build_repo_map` for the host/sandbox paths — hint
    from the issue title+body, exactly as the host path has always built it."""
    return build_repo_map(
        ctx.config, worktree_dir, hint=f"{ctx.issue_title} {ctx.issue_body}"
    )


def _compute_prefix_routing(
    ctx: _DispatchContext,
    resolved: ResolvedRoute,
    runner: AgentRunner,
    repo_map_hash: str,
) -> tuple[str, str]:
    """Resolve the effective model + provider and derive the cache prefix key
    for the host run. Returns ``(provider, prefix_key)``."""
    eff_model = str(ctx.model_override or resolved.model or _builtin_default_model())
    runner_name = str(getattr(runner, "name", ""))
    provider = infer_provider(eff_model, runner_name)
    prefix_key = compute_prefix_key(
        repo=ctx.config.project.repo or "",
        runner=runner_name,
        model=eff_model,
        provider=provider,
        repo_map_hash=repo_map_hash,
    )
    return provider, prefix_key


_REFUSAL_DETAIL_MAX = 200


def _governed_swap_refusal(
    ctx: _DispatchContext,
    proposed: ResolvedRoute,
    governed: ResolvedRoute,
    attested_posture: str | None,
    worktree_dir: Path,
) -> str | None:
    """Admit a governor swap only when it launches under the attested posture.

    Returns ``None`` when the governed route may launch, else the short,
    redacted refusal cause. Containment was resolved and attested for the
    proposed runner before the governor ran (#1803). A swap to another runner
    re-resolves containment with the same rules (transport admission, refusal,
    the skip flag). The swap is refused, and the proposed route kept, when
    that resolution refuses, needs a container this host path cannot give, or
    yields a different posture. An admitted cross-runner swap is re-attested
    so the ledger names the runner that launches.
    """
    proposed_name = str(getattr(proposed.runner, "name", ""))
    governed_name = str(getattr(governed.runner, "name", ""))
    if governed_name == proposed_name:
        return None
    try:
        transport = _require_dispatch_transport_admission(
            ctx.config, governed.runner, governed.model
        )
        decision = _resolve_dispatch_sandbox(
            ctx.config, governed_name, transport, ctx.store
        )
    except Exception as exc:
        detail = redact_credentials(exception_text(exc))[:_REFUSAL_DETAIL_MAX]
        refusal = f"containment for {governed_name} did not resolve ({detail})"
    else:
        if not decision.sandboxed and decision.posture == attested_posture:
            _emit_sandbox_resolved(
                ctx,
                governed_name,
                posture=decision.posture,
                runtime=decision.runtime,
                image=decision.image,
                egress_verification=_resolve_egress_verification(decision, transport),
                checkout_dir=worktree_dir,
            )
            return None
        refusal = (
            f"{governed_name} would launch with posture {decision.posture!r}, "
            f"not the attested {attested_posture!r}"
        )
    _warn(
        f"Governor swap {proposed_name} -> {governed_name} refused: {refusal}; "
        f"keeping {proposed_name}"
    )
    return refusal


def _launch_host(
    ctx: _DispatchContext,
    worktree_dir: Path,
    resolved: ResolvedRoute,
    runner: AgentRunner,
    early: _EarlyRunInfo,
    admit_budget_check: Callable[[], None] | None,
    run_id: str | None = None,
    sandbox_posture: str | None = None,
    continuation_dispatch: bool = False,
    checkout_generation: str = "",
) -> DispatchResult:
    """Host path: build the prompt (with repo map), run the cache-economics
    governor, and launch the agent via ``run_tracked_agent``."""
    config = ctx.config
    repo_map = _build_repo_map(ctx, worktree_dir)
    # Resolved once: the governor can re-render the prompt, and both renders
    # must quote the same rubric.
    rubric_root = operator_repo_root()
    # The dispatch's one snapshot, off the host store, so both renders carry it.
    addendum = _dispatch_addendum(ctx)

    def _render_prompt(candidate: AgentRunner) -> str:
        return build_prompt(
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            provider=ctx.provider,
            issue_title=ctx.issue_title,
            issue_body=ctx.issue_body,
            complexity=ctx.complexity,
            config=config,
            has_coderabbit="coderabbit" in TOOL_PATHS,
            repo_map=repo_map,
            recently_merged_context=ctx.merge_context,
            publication_style=_effective_publication_style(config, candidate),
            has_repo_skills=(
                candidate.name == "claude-code"
                and (worktree_dir / ".claude" / "skills").is_dir()
            ),
            runner_name=candidate.name,
            recorded_plan_sha=getattr(ctx, "recorded_plan_sha", None),
            # Not `worktree_dir` — see the sandboxed path (#2454 round 2).
            rubric_root=rubric_root,
            profile_addendum=addendum,
        )

    # Build provisionally for governor token estimation.
    prompt = _render_prompt(runner)

    _repo_map_hash = hashlib.sha256(repo_map.encode()).hexdigest() if repo_map else ""

    # Injected-context accounting (spec D2 / R1-8): size the repo map actually
    # rendered into the prompt, and derive the governor's estimate from the whole
    # assembled prompt rather than a hard-coded constant. Lesson-corpus tokens
    # stay unset until Phase 2 ships that layer.
    _map_tokens = estimate_prompt_tokens(repo_map) if repo_map else None
    _est_prompt_tokens = estimate_prompt_tokens(prompt)

    # --- Cache-economics governor (Phase 2) ---
    if not ctx.model_override and ctx.store is not None:
        _gov_resolved, _gov_decision = _run_governor(
            resolved=resolved,
            complexity=ctx.complexity,
            config=config,
            store=ctx.store,
            repo_map_hash=_repo_map_hash,
            est_prompt_tokens=_est_prompt_tokens,
        )
        _refusal = _governed_swap_refusal(
            ctx, resolved, _gov_resolved, sandbox_posture, worktree_dir
        )
        if _refusal is None:
            resolved = _gov_resolved
            runner = resolved.runner
            prompt = _render_prompt(runner)
        else:
            _gov_decision = _containment_refused_decision(_gov_decision, _refusal)
        _emit_routing_decision(
            ctx.store, ctx.issue_num, _gov_decision, run_id=ctx.run_id
        )
    else:
        _attest_model_override(ctx, resolved)
    # --- End governor ---

    _require_final_runner_preflight(ctx, resolved, early)
    bound_checkout_generation = _bind_host_checkout_before_runner(
        ctx,
        worktree_dir,
        continuation_dispatch=continuation_dispatch,
        checkout_generation=checkout_generation,
    )

    _provider, _prefix_key = _compute_prefix_routing(
        ctx, resolved, runner, _repo_map_hash
    )

    # Mid-run budget watchdog (#1108). claude-code enforces max_budget_usd
    # natively via its --max-budget-usd argv flag; opencode/pi/hermes have no
    # native flag, so on the host path their configured run cap was decorative
    # — only the admission-time pools (per-issue/daily/weekly) bounded a runaway
    # run. Wire the same watchdog the loop path (#291) uses so the orchestrator
    # polls accumulated cost each tick and kills with the #859 WIP-commit grace,
    # finalizing under budget_exceeded / watchdog_budget. Direct dispatch passes
    # no budget_override, so the effective ceiling is config.agent.max_budget_usd
    # (validated > 0).
    # Host path only — sandboxed dispatch launches through _run_sandboxed, not
    # here — so contained is False and this keeps its existing behaviour.
    _budget_check = budget_watchdog(
        config.agent.max_budget_usd, runner_name=runner.name, contained=False
    )

    return run_tracked_agent(
        prompt=prompt,
        # Off the prompt that will actually launch — after the governor's
        # possible re-render, never before it (#2454).
        self_check=rendered_self_check(prompt),
        prompt_profile_version_id=profile_version_id(addendum),
        worktree_dir=worktree_dir,
        config=config,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        store=ctx.store,
        title=ctx.issue_title,
        branch=ctx.branch,
        model=ctx.model_override or resolved.model,
        runner=runner,
        complexity=ctx.complexity,
        run_kind=ctx.run_kind,
        run_id=run_id,
        epic_ref=ctx.epic_ref,
        prefix_key=_prefix_key,
        provider=_provider,
        source_tracker=ctx.provider,
        worker_cap=config.fleet_concurrency(),
        cap_exclude_issue=ctx.issue_num,
        admit_budget_check=admit_budget_check,
        force_budget=ctx.force_budget,
        budget_check=_budget_check,
        map_tokens=_map_tokens,
        sandbox_posture=sandbox_posture,
        allow_unrecorded=ctx.allow_unrecorded,
        validate_continuation=continuation_dispatch,
        paid_launch_check=partial(
            _require_bound_checkout_generation,
            ctx,
            worktree_dir,
            bound_checkout_generation,
            boundary="native paid launch",
            honor_unrecorded=True,
        ),
        on_admitted=partial(_claim_after_admission, ctx),
        on_launch=partial(_mark_claim_launch_started, ctx),
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
    )


def _record_checkout_withdrawal(
    ctx: _DispatchContext, checkout: Path, *, reason: str
) -> None:
    """Share withdrawal evidence with loop dispatch after verified removal."""
    record_checkout_withdrawal(
        ctx.store,
        checkout,
        abandonment=ctx.checkout_abandonment,
        run_id=ctx.run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        repo=ctx.config.project.repo or None,
        reason=reason,
    )


def _cleanup_refused_checkout(
    ctx: _DispatchContext,
    checkout: Path | None,
    sandboxed: bool,
    *,
    reason: str,
) -> None:
    """Remove unused setup and attest withdrawal only after verified cleanup."""
    if checkout is None or ctx.worktree_dir is not None:
        return
    try:
        _cleanup_dispatch_dir(checkout, sandboxed=sandboxed, branch=ctx.branch)
        if checkout.exists() or checkout.is_symlink():
            _warn(f"Checkout cleanup preserved {checkout} after {reason}")
            return
        _record_checkout_withdrawal(ctx, checkout, reason=reason)
    except Exception as exc:
        _warn(f"Checkout cleanup could not attest withdrawal after {reason}: {exc}")


def _launch_lost_budget(
    ctx: _DispatchContext,
    worktree_dir: Path | None,
    run_sandboxed_path: bool,
    exc: BudgetExceeded,
) -> _LaunchResult:
    """Lost the atomic reservation race after passing the cheap preflight (H2):
    the pending row was rolled back, so no run was admitted. Tear down the
    unused worktree/checkout and refuse exactly like the preflight (hold for
    epics, mark_failed for the classic queue)."""
    _check_dispatch_reservation(ctx)
    _finish_refusal(ctx, str(exc))
    outcome = _refuse_budget(
        ctx.store,  # type: ignore[arg-type]  # non-None: admit gate only built with a store
        ctx.config,
        ctx.issue_num,
        ctx.epic_ref,
        ctx.complexity,
        exc,
        ctx.start_monotonic,
        item_id=ctx.item_id,
        run_id=ctx.run_id,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
    )
    _cleanup_refused_checkout(
        ctx, worktree_dir, run_sandboxed_path, reason=f"budget_exceeded: {exc}"
    )
    outcome.category = "budget_exceeded"
    outcome.reason = redact_credentials(exception_text(exc))
    return _LaunchResult(early=outcome)


def _launch_over_cap(
    ctx: _DispatchContext, worktree_dir: Path | None, run_sandboxed_path: bool
) -> _LaunchResult:
    """Lost the atomic count+insert race after passing the cheap preflight
    (#745): no run row was written, so requeue exactly like the preflight
    over-cap path and tear down the unused worktree (a requeue is not a
    debug-worthy failure — never mark_failed, never preserve)."""
    _warn(
        f"Worker cap reached at admission — requeueing {item_ref(ctx.item_id)} "
        "instead of dispatching"
    )
    # Finish claim retention before the terminal row or reservation release.
    _finish_refusal(ctx, "worker cap reached during atomic admission")
    stranded = _requeue_over_cap(
        ctx.issue_num,
        ctx.epic_ref,
        ctx.store,  # type: ignore[arg-type]
        ctx.config,
        ctx.item_id,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
        provider=ctx.provider,
        run_id=ctx.run_id,
    )
    _cleanup_refused_checkout(
        ctx, worktree_dir, run_sandboxed_path, reason="worker_cap_reached"
    )
    outcome = _with_stranded_claim(
        DispatchOutcome(
            launched=False,
            exit_code=1,
            changed_files=[],
            category="requeued_over_cap",
            reason="worker cap reached during atomic admission",
        ),
        stranded,
    )
    _emit_admission_refusal(ctx, outcome)
    return _LaunchResult(early=outcome)


def _launch_refused_containment(
    ctx: _DispatchContext,
    worktree_dir: Path | None,
    run_sandboxed_path: bool,
    exc: SandboxRuntimeUnavailable,
    early: _EarlyRunInfo,
) -> _LaunchResult:
    """Retain the claim before recording a containment refusal and cleaning up."""
    _check_dispatch_reservation(ctx)
    _finish_refusal(ctx, str(exc))
    outcome = _refuse_containment(
        ctx.store,
        ctx.config,
        ctx.issue_num,
        ctx.epic_ref,
        ctx.complexity,
        early.runner,
        exc,
        ctx.start_monotonic,
        item_id=ctx.item_id,
        run_id=ctx.run_id,
        expected_reservation_run_id=ctx.expected_reservation_run_id,
        expected_reservation_generation=ctx.expected_reservation_generation,
    )
    outcome.category = _CONTAINMENT_REFUSAL_CATEGORY
    outcome.reason = redact_credentials(exception_text(exc))
    _cleanup_refused_checkout(
        ctx,
        worktree_dir,
        run_sandboxed_path,
        reason=f"containment_refused: {outcome.reason}",
    )
    return _LaunchResult(early=outcome)


def _launch_failed(
    ctx: _DispatchContext, exc: BaseException, early: _EarlyRunInfo
) -> _LaunchResult:
    """Finish claim retention before publishing a pre-launch exception."""
    from kew.dispatch_sandbox import container_launch_failure

    if container_launch_failure(exc) is not None:
        return _container_launch_marker_failed(ctx, exc)
    _finish_refusal(ctx, str(exc))
    _record_prelaunch_refusal(ctx, "exception")
    config = ctx.config
    _error(
        redact_credentials(
            f"Exception during dispatch of #{ctx.issue_num}: {exception_text(exc)}"
        )
    )
    with contextlib.suppress(Exception):
        mark_failed(ctx.issue_num, config, "internal error during dispatch")
    with contextlib.suppress(Exception):
        _emit_early_exit(
            ctx.store,
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            complexity=ctx.complexity,
            runner=early.runner,
            model=early.model,
            exit_code=1,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category="exception",
            repo=config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
        )
    return _LaunchResult(
        early=DispatchOutcome(
            launched=False,
            exit_code=1,
            changed_files=[],
            category="exception",
            reason=redact_credentials(exception_text(exc)),
        )
    )


def _record_issuer_refusal(ctx: _DispatchContext, early: _EarlyRunInfo) -> None:
    """Persist the pre-launch refusal and release only this run's reservation."""
    from kew.run_store import RunRecord

    if ctx.store is None or ctx.run_id is None:
        return
    now = datetime.now(UTC)
    record = ctx.store.get(ctx.run_id) or RunRecord(
        run_id=ctx.run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        branch=ctx.branch,
        status="refused",
        exit_code=2,
        started_at=now,
        runner_name=early.runner,
        model=early.model,
    )
    ctx.store.save(
        replace(
            record,
            status="refused",
            exit_code=2,
            finished_at=now,
            error_category="credential_issuer_refused",
        )
    )
    # Generation-conditional on the identity this caller actually acquired:
    # the driver's pin when it was given one, else this foreground run's own
    # reservation id. Never an unconditional administrative delete (#1951).
    ctx.store.release_epic_node_item(
        ctx.item_id, run_id=ctx.expected_reservation_run_id or ctx.run_id
    )


def _handle_launch_unwind(
    ctx: _DispatchContext, exc: BaseException, early: _EarlyRunInfo
) -> _LaunchResult:
    from kew.dispatch_sandbox import container_launch_failure

    if isinstance(exc, Exception):
        return _launch_failed(ctx, exc, early)
    if container_launch_failure(exc) is not None:
        _container_launch_marker_failed(ctx, exc)
    raise exc


def _container_launch_marker_failed(
    ctx: _DispatchContext, exc: BaseException
) -> _LaunchResult:
    from kew.dispatch_sandbox import (
        _finalize_sandbox_run,
        container_launch_failure,
        launch_error_category,
    )

    evidence = container_launch_failure(exc)
    assert evidence is not None
    ctx.container_cleanup_pending = not evidence.cleanup_confirmed
    try:
        _finish_refusal(ctx, "container launch delivery failed")
    except BaseException as retention_error:
        raise exc from retention_error
    if ctx.container_cleanup_pending:
        raise exc
    row = (
        ctx.store.get(ctx.admitted_run_id)
        if ctx.store is not None and ctx.admitted_run_id is not None
        else None
    )
    if row is not None and row.status == "pending":
        # The reason carries the runtime's own words on a failed create
        # (#2913), so the run, and the node it holds, say why.
        _finalize_sandbox_run(
            ctx.store,
            row.run_id,
            exit_code=-1,
            error_category=launch_error_category(exc),
            reason=str(exc),
        )
    # Only observed workload startup makes this an executed failure (exit 1);
    # a container that never ran its agent is a pre-launch refusal (exit 2).
    result = partial_dispatch_result(exc) if isinstance(exc, Exception) else None
    return _LaunchResult(
        early=DispatchOutcome(
            launched=result is not None and result.launched is not False,
            exit_code=1,
            changed_files=[],
            category=launch_error_category(exc),
            reason=str(exc),
        )
    )


def _launch_refused_transport(
    ctx: _DispatchContext,
    worktree_dir: Path | None,
    run_sandboxed_path: bool,
    exc: TransportAdmissionError | IssuerError,
    early: _EarlyRunInfo,
) -> _LaunchResult:
    """Record a stable adaptive-route refusal before any runner is spawned."""
    exit_code = 2 if isinstance(exc, IssuerError) else 1
    issuer_refused = isinstance(exc, IssuerError)
    if isinstance(exc, IssuerError):
        reason = exc.reason
        category = "credential_issuer_refused"
        message = reason
    elif isinstance(exc, RunnerPreflightRefused):
        # Same pre-spawn path, different fact: the RUNNER cannot launch here.
        # Naming it a transport refusal sent operators to Switchyard when the
        # fix was `codex login` (round-4 review).
        reason = exc.reason or "runner_preflight_refused"
        category = "runner_preflight_refused"
        message = f"Runner preflight refused: {reason}"
    else:
        reason = exc.reason or "transport_admission_refused"
        category = "transport_admission_refused"
        message = f"Switchyard route admission refused: {reason}"
    _finish_refusal(ctx, reason)
    # The refusal row is recorded while this run's reservation still exists:
    # releasing first made the next call re-validate a reservation that was
    # gone, so a pinned child reported a replaced reservation instead of the
    # refusal and its node was never held (governed review round 7,
    # 2026-09-22).
    _record_prelaunch_refusal(ctx, category, exit_code=exit_code)
    if issuer_refused:
        _record_issuer_refusal(ctx, early)
    _cleanup_refused_checkout(
        ctx, worktree_dir, run_sandboxed_path, reason=f"{category}: {reason}"
    )
    # On stderr as well as the tracker: the epic child builds its operator
    # hold from the captured stderr tail, so a message that only reached
    # mark_failed left the hold without the remedy (round-6 review).
    _error(message)
    with contextlib.suppress(Exception):
        mark_failed(ctx.issue_num, ctx.config, message)
    with contextlib.suppress(Exception):
        _emit_early_exit(
            ctx.store,
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            complexity=ctx.complexity,
            runner=early.runner,
            model=early.model,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category=category,
            repo=ctx.config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
            admission_reason=reason,
        )
    return _LaunchResult(
        early=DispatchOutcome(
            launched=False,
            exit_code=exit_code,
            changed_files=[],
            category=category,
            reason=reason,
        )
    )


def _record_credential_route_admission(
    ctx: _DispatchContext, runner: AgentRunner, transport: ResolvedInferenceTransport
) -> None:
    if not is_detached_credential_child():
        return
    decision = require_credential_route(runner, transport)
    audit.emit(
        ctx.store,
        "sandbox_resolved",
        actor=Actor("policy", "credential-issuer"),
        run_id=ctx.run_id,
        item_id=ctx.item_id,
        payload={
            "phase": "admission",
            "credential_route": decision.evidence(),
            "credential_route_digest": decision.digest,
        },
    )


def _require_dispatch_transport_admission(
    config: DispatchConfig, runner: AgentRunner, model: str
) -> ResolvedInferenceTransport:
    """Check adaptive transport admission before workspace setup."""
    resolved = resolve_inference_transport(config, runner, model)
    require_credential_route(runner, resolved)
    require_adaptive_route_admission(resolved, config)
    return resolved


class RunnerPreflightRefused(TransportAdmissionError):
    """The final route's runner cannot launch on this host.

    A subclass so the refusal rides the same pre-spawn path as a transport
    admission refusal, but stays distinguishable: the operator must be able
    to tell "codex is not logged in" from "the adaptive route was refused"
    in kew status, the epic hold, and the audit event (round-4 review).
    """


def _require_final_runner_preflight(
    ctx: _DispatchContext, resolved: ResolvedRoute, early: _EarlyRunInfo
) -> None:
    """Refuse when the final route cannot launch its selected runner.

    Host launches only. A contained launch runs the CLI inside the image, so
    the host binary is irrelevant there; the sandbox launcher checks the
    image, its identity and the credentials itself.
    """
    from kew.runner_preflight import runner_preflight_failure

    runner_name = str(getattr(resolved.runner, "name", ""))
    early.runner = runner_name
    early.model = resolved.model or ""
    ctx.publication_style = str(getattr(resolved.runner, "publication_style", "agent"))
    if is_detached_credential_child():
        require_credential_route(
            resolved.runner,
            resolve_inference_transport(
                ctx.config, resolved.runner, ctx.model_override or resolved.model
            ),
        )
    failure = runner_preflight_failure(ctx.config, runner_name=runner_name)
    if failure is not None:
        raise RunnerPreflightRefused(failure, reason=failure)


def _bind_host_checkout_before_runner(
    ctx: _DispatchContext,
    checkout: Path,
    *,
    continuation_dispatch: bool,
    checkout_generation: str,
) -> str:
    """Bind a fresh native checkout after final preflight and before launch."""
    if continuation_dispatch:
        _require_bound_checkout_generation(
            ctx,
            checkout,
            checkout_generation,
            boundary="continuation pre-launch handoff",
        )
        return checkout_generation
    generation = _bind_initial_checkout(ctx, checkout)
    _require_bound_checkout_generation(
        ctx,
        checkout,
        generation,
        boundary="native launch handoff",
        honor_unrecorded=True,
    )
    return generation


def _pinned_base(ctx: _DispatchContext) -> str:
    """The exact base a canonical checkout starts on; the checkout escape
    hatch keeps following the fetched head (review round 14)."""
    if ctx.config.publication.publish != "canonical":
        return ""
    return ctx.publication_base_sha


def _emit_host_setup_posture(
    ctx: _DispatchContext,
    runner: str,
    decision: SandboxDecision,
    checkout_dir: Path | None,
) -> None:
    """Attest a host posture with its checkout when setup succeeded."""
    _emit_sandbox_resolved(
        ctx,
        runner,
        posture=decision.posture,
        runtime=decision.runtime,
        image=decision.image,
        egress_verification=decision.egress_verification,
        checkout_dir=checkout_dir,
    )


def _host_checkout_abandonment(
    ctx: _DispatchContext, config: DispatchConfig
) -> CheckoutAbandonment | None:
    """Resolve exact abandonment authority under the checkout claim before setup."""
    if ctx.store is None:
        return None
    checkout = worktree.dispatch_checkout_path(
        config, issue_num=ctx.issue_num, item_id=ctx.item_id, provider=ctx.provider
    )
    claim_checkout(checkout)
    return checkout_abandonment(
        ctx.store,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
        branch=ctx.branch,
        checkout=checkout,
        capture_generation=partial(worktree.capture_checkout_generation, checkout),
        retire_legacy_generation=partial(
            worktree.retire_legacy_checkout_generation, checkout
        ),
    )


def _setup_host_worktree(
    ctx: _DispatchContext,
    config: DispatchConfig,
    runner: str,
    decision: SandboxDecision,
) -> Path:
    """Create a host checkout and always attest the resolved posture."""
    try:
        ctx.checkout_abandonment = _host_checkout_abandonment(ctx, config)
        checkout_dir = setup_worktree(
            issue_num=ctx.issue_num,
            config=config,
            item_id=ctx.item_id,
            provider=ctx.provider,
            base_sha=_pinned_base(ctx),
            abandonment=ctx.checkout_abandonment,
        )
    except BaseException:
        _emit_host_setup_posture(ctx, runner, decision, None)
        raise
    _emit_host_setup_posture(ctx, runner, decision, checkout_dir)
    return checkout_dir


def _record_dispatch_plan(ctx: _DispatchContext) -> None:
    """Record the acceptance criteria this dispatch launches against (#2290).

    Called once the run id exists and before the prompt is built, which is what
    makes both halves of the record true: the ``item_plans`` row and its chain
    events name the run that read the body (the classic GitHub path mints its
    run id here, not at admission), and the prompt below can name the plan sha
    the ledger actually holds instead of asserting a write it never saw.

    Extract-only, so it costs no model call and no network read beyond the
    issue read that already happened; the same body on a later run records
    nothing new. A failure leaves ``recorded_plan_sha`` unset and a warning —
    it never refuses the dispatch.
    """
    write = record_extracted_plan(
        ctx.store,
        item_id=ctx.item_id,
        body=getattr(ctx, "issue_body", ""),
        run_id=ctx.run_id,
        issue_num=ctx.issue_num,
        repo=ctx.config.project.repo or None,
    )
    ctx.recorded_plan_sha = None if write is None else write.current.plan_sha


def _routed_models(config: DispatchConfig) -> list[str]:
    """Every model the operator's routing names, the agent default included."""
    models = [config.agent.model]
    if config.routing is not None:
        for tier in ("default", "simple", "medium", "complex"):
            entry = getattr(config.routing, tier)
            if isinstance(entry, RouteEntry):
                models.append(entry.model)
    return [model for model in models if model]


def _launch_acceptance_author(
    ctx: _DispatchContext,
    request: AuthorRequest,
    *,
    admit_budget_check: Callable[[], None] | None,
    sandbox_posture: str | None,
) -> str:
    """Run the author as its own fresh ``acceptance_author`` run (#2728).

    A new run id and a new session: nothing of the implementer's context
    reaches it but the checkout at the base. It is a paid run like the
    implementer's, so it passes the same admission (budget pools, fleet worker
    cap), mid-run budget watchdog, posture record and checkout-identity check
    before launch. A run that did not exit cleanly refuses the contract.
    """
    route = resolve_runner(
        ctx.config.model_copy(
            update={
                "routing": None,
                "agent": ctx.config.agent.model_copy(
                    update={"runner": "", "model": request.model}
                ),
            }
        )
    )
    run_id = str(uuid.uuid4())
    result = run_tracked_agent(
        prompt=request.prompt,
        worktree_dir=request.worktree,
        config=ctx.config,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        store=ctx.store,
        title=ctx.issue_title,
        branch=ctx.branch,
        model=route.model,
        runner=route.runner,
        complexity=ctx.complexity,
        run_kind=ACCEPTANCE_AUTHOR_RUN_KIND,
        run_id=run_id,
        epic_ref=ctx.epic_ref,
        source_tracker=ctx.provider,
        worker_cap=ctx.config.fleet_concurrency(),
        cap_exclude_issue=ctx.issue_num,
        admit_budget_check=admit_budget_check,
        force_budget=ctx.force_budget,
        budget_check=budget_watchdog(
            ctx.config.agent.max_budget_usd,
            runner_name=route.runner.name,
            contained=False,
        ),
        sandbox_posture=sandbox_posture,
        allow_unrecorded=ctx.allow_unrecorded,
        validate_continuation=False,
        paid_launch_check=partial(
            _require_bound_checkout_generation,
            ctx,
            request.worktree,
            _checkout_generation(request.worktree),
            boundary="acceptance author paid launch",
            honor_unrecorded=True,
        ),
    )
    if result.exit_code != 0:
        raise AcceptanceAuthorRefused(
            f"the acceptance author run {run_id} exited {result.exit_code}"
        )
    return run_id


def _dispatch_addendum(ctx: _DispatchContext) -> ProfileAddendum | None:
    """The approved addendum this dispatch renders and records, read once.

    The first call reads :func:`launch_addendum` (the digest and block-opener
    checks) and keeps the result on *ctx*; every later call returns that same
    snapshot, so a profile change between the acceptance author and the
    implementer cannot split what one dispatch renders and records.
    """
    snapshot = getattr(ctx, "profile_snapshot", None)
    if not isinstance(snapshot, _ProfileSnapshot):
        snapshot = _ProfileSnapshot(launch_addendum(ctx.store))
        ctx.profile_snapshot = snapshot
    return snapshot.addendum


def _refuse_unrenderable_profile(ctx: _DispatchContext) -> None:
    """Run the prompt-profile refusals before any paid launch (#3022).

    The implementer's render runs them too, but only after the acceptance
    author has been paid for and its contract committed. Raising here, a
    :class:`PromptConfigurationError`, refuses the dispatch before either.
    """
    refuse_custom_template_with_addendum(ctx.config, _dispatch_addendum(ctx))


def _author_acceptance_contract(
    ctx: _DispatchContext,
    worktree_dir: Path,
    resolved: ResolvedRoute,
    *,
    admit_budget_check: Callable[[], None] | None,
    sandbox_posture: str | None,
    contained: bool = False,
    continuation_base: str | None = None,
    launch_author: Callable[..., str] = _launch_acceptance_author,
    run_test: BaseTestRunner | None = None,
) -> None:
    """Put the node's acceptance contract on its branch before the implementer.

    Only a driver dispatch of a node whose recorded plan has a ``kind = "test"``
    criterion; everything else launches as before. A contract already in the
    ledger is restored onto this checkout (a redispatch can start fresh at the
    base); the author runs when there is none, or its commit is gone.

    A *contained* dispatch refuses instead of authoring: the author run and its
    red-at-base pytest both execute agent-written code, and the only launch for
    them is the host's, so running them would breach the containment the
    implementer gets — and skipping them would ship the node with no contract.

    A continuation passes its *continuation_base*: its recorded contract is
    restored, and a missing one is authored only if the implementer has not
    committed yet; otherwise the continuation is refused (#2771).
    """
    if ctx.store is None or ctx.epic_ref is None or ctx.run_kind != "dispatch":
        return
    plan_row = ctx.store.current_item_plan(ctx.item_id)
    plan = (
        None if plan_row is None else ItemPlan.model_validate_json(plan_row.plan_json)
    )
    if not tested_criteria(plan):
        return
    prepare_acceptance_contract(
        store=ctx.store,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
        issue_num=ctx.issue_num,
        issue_body=ctx.issue_body,
        plan=plan,
        worktree=worktree_dir,
        implementer_model=ctx.model_override or resolved.model,
        routed_models=_routed_models(ctx.config),
        contained=contained,
        launch=partial(
            launch_author,
            ctx,
            admit_budget_check=admit_budget_check,
            sandbox_posture=sandbox_posture,
        ),
        continuation_base=continuation_base,
        run_test=run_test,
    )


def _launch_continuation(
    ctx: _DispatchContext,
    worktree_dir: Path,
    setup: _LaunchSetup,
    *,
    config: DispatchConfig,
    resolved: ResolvedRoute,
    runner: AgentRunner,
    early: _EarlyRunInfo,
    admit_budget_check: Callable[[], None] | None,
    inference_transport: ResolvedInferenceTransport | None,
) -> DispatchResult:
    """Launch the implementer on a claimed continuation's preserved checkout."""
    # A continuation is an operator-approved existing checkout. One run
    # owns it from here, and it publishes against the base it was cut
    # from (review round 12); containment resolves before the host
    # launch can touch it.
    claim_checkout(worktree_dir)
    generation = _checkout_generation(worktree_dir)
    ctx.publication_base_sha = _continuation_base(ctx)
    if not ctx.publication_base_sha and config.publication.publish == "canonical":
        # Nothing binds this directory to the continuation: it was never
        # minted, or another checkout replaced it at this path. Refuse
        # before an agent runs on it (review round 16).
        raise RuntimeError(
            f"{item_ref(ctx.item_id)}: no publication base is bound to the "
            f"preserved checkout {worktree_dir} (never minted, or "
            "replaced by a later checkout at the same path); redispatch "
            "instead of continuing it"
        )
    continuation_decision = _resolve_and_apply_sandbox(
        ctx,
        early.runner,
        host_only=True,
        defer_attestation=True,
        inference_transport=inference_transport,
    )
    _emit_sandbox_resolved(
        ctx,
        early.runner,
        posture=continuation_decision.posture,
        runtime=continuation_decision.runtime,
        image=continuation_decision.image,
        egress_verification=continuation_decision.egress_verification,
        checkout_dir=worktree_dir,
    )
    _require_bound_checkout_generation(
        ctx,
        worktree_dir,
        generation,
        boundary="continuation launch handoff",
    )
    setup.worktree_dir = worktree_dir
    _refuse_unrenderable_profile(ctx)
    _author_acceptance_contract(
        ctx,
        setup.worktree_dir,
        resolved,
        admit_budget_check=admit_budget_check,
        sandbox_posture=continuation_decision.posture,
        continuation_base=ctx.publication_base_sha or "",
    )
    return _launch_host(
        ctx,
        setup.worktree_dir,
        resolved,
        runner,
        early,
        admit_budget_check,
        run_id=ctx.run_id,
        sandbox_posture=continuation_decision.posture,
        continuation_dispatch=True,
        checkout_generation=generation,
    )


def _launch_at_contract(
    ctx: _DispatchContext,
    worktree_dir: Path,
    resolved: ResolvedRoute,
    *,
    admit_budget_check: Callable[[], None] | None,
    sandbox_posture: str | None,
    launch_implementer: Callable[[], DispatchResult],
    contained: bool = False,
    launch_author: Callable[..., str] = _launch_acceptance_author,
    run_test: BaseTestRunner | None = None,
) -> DispatchResult:
    """Prepare the node's acceptance contract, then start the implementer on it.

    The one ordering point for both workspaces (#2767): the author's launch,
    its red-at-base check and the contract commit all finish before
    ``launch_implementer`` runs, so the implementer starts at the contract
    commit, and a refused contract raises before it launches. The author launch
    and the base test are parameters so a test stands in for those processes
    instead of patching this module. The prompt-profile refusals run first, so
    a dispatch whose implementer render would be refused pays for no author.
    """
    _refuse_unrenderable_profile(ctx)
    _author_acceptance_contract(
        ctx,
        worktree_dir,
        resolved,
        admit_budget_check=admit_budget_check,
        sandbox_posture=sandbox_posture,
        contained=contained,
        launch_author=launch_author,
        run_test=run_test,
    )
    return launch_implementer()


def _launch_workspace(
    ctx: _DispatchContext,
    setup: _LaunchSetup,
    *,
    config: DispatchConfig,
    resolved: ResolvedRoute,
    runner: AgentRunner,
    early: _EarlyRunInfo,
    admit_budget_check: Callable[[], None] | None,
    inference_transport: ResolvedInferenceTransport | None = None,
) -> DispatchResult:
    """Resolve sandbox posture and launch the selected workspace."""
    ctx.run_id = ctx.run_id or str(uuid.uuid4())
    _record_dispatch_plan(ctx)
    if ctx.worktree_dir is not None:
        return _launch_continuation(
            ctx,
            ctx.worktree_dir,
            setup,
            config=config,
            resolved=resolved,
            runner=runner,
            early=early,
            admit_budget_check=admit_budget_check,
            inference_transport=inference_transport,
        )

    sandbox_decision = _resolve_and_apply_sandbox(
        ctx,
        early.runner,
        defer_attestation=True,
        inference_transport=inference_transport,
    )
    if sandbox_decision.sandboxed:
        # No host runner preflight here, on purpose: the CLI runs inside the
        # image, so `shutil.which` on the host says nothing about it, and a
        # host without the `claude` binary is a valid contained launch. The
        # sandbox launcher checks image, identity and credentials (round-4
        # review, Medium 2). The route's runner and model are still recorded.
        early.runner = str(getattr(resolved.runner, "name", ""))
        # The container runs the operator-named model when there is one (#2741).
        early.model = ctx.model_override or resolved.model or ""
        setup.run_sandboxed = True
        ctx.publication_base_sha = _trusted_dispatch_base(config)
        setup.worktree_dir = _materialize_checkout(
            ctx.item_id, ctx.branch, config, base_sha=_pinned_base(ctx)
        )
        try:
            check_host_capacity(
                setup.worktree_dir,
                runtime=sandbox_decision.runtime,
                image=sandbox_decision.image,
            )
        except SandboxRuntimeUnavailable as exc:
            _attest_sandbox_refusal(ctx, early.runner, exc)
            raise
        _attest_direct_sandbox_resolution(
            ctx,
            early.runner,
            sandbox_decision,
            inference_transport,
            setup.worktree_dir,
        )
        generation = _bind_contained_checkout(
            ctx,
            setup.worktree_dir,
            runner=early.runner,
            decision=sandbox_decision,
            inference_transport=inference_transport,
        )
        _attest_model_override(ctx, resolved)
        return _launch_at_contract(
            ctx,
            setup.worktree_dir,
            resolved,
            admit_budget_check=admit_budget_check,
            sandbox_posture=sandbox_decision.posture,
            contained=True,
            launch_implementer=partial(
                _launch_sandboxed,
                ctx,
                setup.worktree_dir,
                early,
                admit_budget_check,
                image=sandbox_decision.image or "",
                sandbox_decision=sandbox_decision,
                inference_transport=inference_transport,
                checkout_generation=generation,
            ),
        )

    ctx.publication_base_sha = _trusted_dispatch_base(config)
    # The checkout starts at the exact trusted base and the base is recorded
    # only once the claimed checkout exists at it (review round 13).
    setup.worktree_dir = _setup_host_worktree(
        ctx,
        config,
        early.runner,
        sandbox_decision,
    )
    # The realized profile records the resolved posture — native and
    # dangerously-uncontained launches are distinct facts from plain host
    # execution (#1550 review F6; wiring per the #1552 re-review F-1).
    return _launch_at_contract(
        ctx,
        setup.worktree_dir,
        resolved,
        admit_budget_check=admit_budget_check,
        sandbox_posture=sandbox_decision.posture,
        launch_implementer=partial(
            _launch_host,
            ctx,
            setup.worktree_dir,
            resolved,
            runner,
            early,
            admit_budget_check,
            run_id=ctx.run_id,
            sandbox_posture=sandbox_decision.posture,
        ),
    )


def _launch_runner(
    ctx: _DispatchContext,
    admit_budget_check: Callable[[], None] | None,
) -> _LaunchResult:
    """Set up the workspace (sandboxed checkout or worktree), build the prompt,
    and run the agent.

    Resolves the runner and effective sandbox posture, then either launches a
    sandboxed container against a standalone checkout (``_launch_sandboxed``) or
    a host worktree run (``_launch_host``, after the cache-economics governor).
    Returns a ``_LaunchResult``: on a live run its ``result`` / ``worktree_dir``
    / ``run_sandboxed`` are set; a lost admit race (BudgetExceeded /
    WorkerCapReached) or a mid-dispatch exception yields a short-circuit
    ``early`` outcome instead (workspace torn down, refusal callback finished).
    """
    config = ctx.config

    # Captured as soon as the runner resolves, so the early-exit dispatch log
    # entry can name it. Bound before the dispatch try so the admit-race
    # handlers can tear down whatever was set up, whichever path raised (#745).
    early = _EarlyRunInfo()
    setup = _LaunchSetup()

    try:
        resolved = resolve_runner(config, complexity=ctx.complexity)
        runner = resolved.runner
        early.runner = str(getattr(runner, "name", ""))
        early.model = resolved.model or ""
        ctx.publication_style = str(getattr(runner, "publication_style", "agent"))

        inference_transport = _require_dispatch_transport_admission(
            config, runner, ctx.model_override or resolved.model
        )
        _preflight_pricing(ctx, resolved)
        _record_credential_route_admission(ctx, runner, inference_transport)
        result = _launch_workspace(
            ctx,
            setup,
            config=config,
            resolved=resolved,
            runner=runner,
            early=early,
            admit_budget_check=admit_budget_check,
            inference_transport=inference_transport,
        )
    except BaseException as exc:
        return _classify_launch_unwind(ctx, setup, early, exc)

    return _LaunchResult(
        result=result,
        worktree_dir=setup.worktree_dir,
        run_sandboxed=setup.run_sandboxed,
    )


def _classify_launch_unwind(
    ctx: _DispatchContext,
    setup: _LaunchSetup,
    early: _EarlyRunInfo,
    exc: BaseException,
) -> _LaunchResult:
    """Container cleanup evidence decides first; then workload evidence takes
    precedence over a later exception's type; then the pre-launch refusals."""
    from kew.dispatch_sandbox import container_launch_failure

    if container_launch_failure(exc) is not None:
        return _handle_launch_unwind(ctx, exc, early)
    if isinstance(exc, DispatchReservationReplaced):
        raise exc
    result = partial_dispatch_result(exc) if isinstance(exc, Exception) else None
    if result is not None and result.launched is not False:
        _error(f"Container dispatch for #{ctx.issue_num} failed: {exc}")
        return _LaunchResult(
            result=result,
            worktree_dir=setup.worktree_dir,
            run_sandboxed=setup.run_sandboxed,
        )
    return _classify_prelaunch_refusal(ctx, setup, early, exc)


def _classify_prelaunch_refusal(
    ctx: _DispatchContext,
    setup: _LaunchSetup,
    early: _EarlyRunInfo,
    exc: BaseException,
) -> _LaunchResult:
    """Route an unlaunched unwind to its refusal handler, else the failure path."""
    if isinstance(exc, worktree.CheckoutAdmissionRefusal):
        return _launch_refused_checkout(
            ctx, setup.worktree_dir, setup.run_sandboxed, exc
        )
    if isinstance(exc, BudgetExceeded):
        return _launch_lost_budget(ctx, setup.worktree_dir, setup.run_sandboxed, exc)
    if isinstance(exc, WorkerCapReached):
        return _launch_over_cap(ctx, setup.worktree_dir, setup.run_sandboxed)
    if isinstance(exc, SandboxRuntimeUnavailable):
        return _launch_refused_containment(
            ctx, setup.worktree_dir, setup.run_sandboxed, exc, early
        )
    if isinstance(exc, TransportAdmissionError | IssuerError):
        return _launch_refused_transport(
            ctx, setup.worktree_dir, setup.run_sandboxed, exc, early
        )
    if isinstance(exc, PromptConfigurationError):
        return _launch_refused_prompt_config(
            ctx, setup.worktree_dir, setup.run_sandboxed, exc, early
        )
    return _handle_launch_unwind(ctx, exc, early)


def _launch_refused_prompt_config(
    ctx: _DispatchContext,
    worktree_dir: Path | None,
    run_sandboxed_path: bool,
    exc: PromptConfigurationError,
    early: _EarlyRunInfo,
) -> _LaunchResult:
    """Refuse a ``[prompt]`` configuration conflict without failing the item.

    The render is refused before any runner starts and only the operator's
    configuration can fix it, so nothing is marked failed: in a drain one such
    conflict would otherwise fail every ready issue. The claim goes back — the
    classic queue's issue to ``ready``, an epic node's reservation to its
    refusal row — and the message names the remedy.
    """
    reason = redact_credentials(exception_text(exc))
    _finish_refusal(ctx, reason)
    _record_prelaunch_refusal(ctx, PROMPT_CONFIGURATION_REFUSAL, exit_code=2)
    _cleanup_refused_checkout(
        ctx,
        worktree_dir,
        run_sandboxed_path,
        reason=f"{PROMPT_CONFIGURATION_REFUSAL}: {reason}",
    )
    _error(f"Prompt configuration refused: {reason}")
    stranded = _hand_claim_back(
        ctx.issue_num,
        ctx.config,
        ctx.store,
        epic_ref=ctx.epic_ref,
        provider=ctx.provider,
        item_id=ctx.item_id,
        run_id=ctx.run_id,
    )
    outcome = _with_stranded_claim(
        DispatchOutcome(
            launched=False,
            exit_code=2,
            changed_files=[],
            category=PROMPT_CONFIGURATION_REFUSAL,
            reason=reason,
        ),
        stranded,
    )
    with contextlib.suppress(Exception):
        _emit_early_exit(
            ctx.store,
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            complexity=ctx.complexity,
            runner=early.runner,
            model=early.model,
            exit_code=2,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category=outcome.category,
            repo=ctx.config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
            reason=outcome.reason,
        )
    return _LaunchResult(early=outcome)


def _record_checkout_refusal(ctx: _DispatchContext, category: str) -> None:
    """Finish the admitted row, or record a pre-admission refusal row.

    A refusal after admission must not replace the pending row: that row names
    the run's log file and start time, and it is not a pre-admission refusal
    (#2013). It is finished in place by one write conditional on ``pending``,
    so a terminal outcome another writer recorded first is kept, and a row
    an operator asked to stop stays ``pending``/killed for the stop
    reconciler to finish.

    Without an admitted row the refusal row is one conditional write too: it
    is inserted, or replaces only the driver's ``dispatch_child_died`` verdict
    (a GitHub epic child's run id is the reservation's, and a child alive to
    refuse proves that verdict wrong). Any other existing row is kept: the
    sandbox finalizes its admitted row before it raises, and a GitHub dispatch
    has no ``admitted_run_id``.
    """
    from kew.dispatch_preflight import refusal_record

    if ctx.store is None:
        return
    admitted_id = ctx.admitted_run_id or ctx.run_id
    if admitted_id is not None and ctx.store.finish_pending_refusal(
        admitted_id,
        status=_REFUSAL_STATUS,
        error_category=category,
        exit_code=1,
        finished_at=datetime.now(UTC),
    ):
        return
    if ctx.admitted_run_id is None and ctx.epic_ref is not None:
        ctx.store.save_over_dead_child(
            refusal_record(
                ctx.config,
                ctx.issue_num,
                ctx.item_id,
                error_category=category,
                run_id=ctx.run_id,
            )
        )


def _launch_refused_checkout(
    ctx: _DispatchContext,
    worktree_dir: Path | None,
    run_sandboxed_path: bool,
    exc: worktree.CheckoutAdmissionRefusal,
) -> _LaunchResult:
    """Record a prerequisite refusal and release only this dispatch's reservation.

    A checkout-identity refusal can arrive after the replacement checkout was
    bound (its continuation expiry names this run). Like the other pre-spawn
    refusal handlers it withdraws that checkout with verified cleanup, so the
    consumed abandonment does not strand the node behind a replacement that
    nothing ever ran in (governed review round 1, 2026-09-21).
    """
    outcome = DispatchOutcome(
        launched=False,
        exit_code=2,
        changed_files=[],
        category=exc.category,
        reason=str(exc),
    )
    ctx.run_id = ctx.run_id or str(uuid.uuid4())
    _cleanup_refused_checkout(
        ctx,
        worktree_dir,
        run_sandboxed_path,
        reason=f"checkout_refused: {exc.category}",
    )
    _error(outcome.reason)
    try:
        # Finish claim retention before the terminal row or reservation release.
        _finish_refusal(ctx, outcome.reason)
        _record_checkout_refusal(ctx, exc.category)
        _emit_admission_refusal(ctx, outcome)
    finally:
        # Generation-conditional on the identity this caller acquired: the
        # driver's pin when it was given one, else this run's own id (#1951).
        if ctx.store is not None and ctx.run_id is not None:
            ctx.store.release_epic_node_item(
                ctx.item_id, run_id=ctx.expected_reservation_run_id or ctx.run_id
            )
    return _LaunchResult(early=outcome)


def _collect_killed(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Terminal state: operator kill (`kew stop`). Not a failure — labels go
    back to ready, the worktree is preserved, and no failure comment is posted.
    """
    config = ctx.config
    issue_num = ctx.issue_num
    _warn(
        f"Run for #{issue_num} was killed by `kew stop`"
        f" — worktree preserved: {worktree_dir}"
    )
    run_id = result.run_id or None
    stranded = _hand_claim_back(
        issue_num,
        config,
        ctx.store,
        epic_ref=ctx.epic_ref,
        provider=ctx.provider,
        item_id=ctx.item_id,
        run_id=run_id,
    )
    return _with_stranded_claim(
        DispatchOutcome(
            exit_code=result.exit_code or 1,
            changed_files=[],
            run_id=run_id,
            launched=getattr(result, "launched", False) is not False,
        ),
        stranded,
        ended="killed: the run was stopped by `kew stop`",
    )


def _collect_quota_exhausted(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Terminal state: the provider refused the runner for quota (#2643).

    Not the work's failure. Like an operator kill, the labels go back to ready,
    the worktree is preserved and no failure comment is posted; the row is
    ``refused``. The rejection holds the runner's lane in the store, so the
    epic tick dispatches nothing more to it until the reset passes — instead
    of failing every node it touches in two seconds each.
    """
    rejection = result.quota_rejection or QuotaRejection()
    _error(f"Run for #{ctx.issue_num} refused: {rejection.describe()}")
    if ctx.store is not None:
        hold = record_runner_quota_hold(
            ctx.store,
            rejection,
            run_id=result.run_id or ctx.run_id or "",
            credential_fingerprint=runner_credential_fingerprint(rejection.runner),
            credential_names=runner_credential_names(rejection.runner),
        )
        if hold is not None:
            _warn(hold.reason())
    # The hold stands whether or not the claim goes back.
    run_id = result.run_id or ctx.run_id or None
    stranded = _hand_claim_back(
        ctx.issue_num,
        ctx.config,
        ctx.store,
        epic_ref=ctx.epic_ref,
        provider=ctx.provider,
        item_id=ctx.item_id,
        run_id=run_id,
    )
    _warn(f"Worktree preserved: {worktree_dir}")
    return _with_stranded_claim(
        DispatchOutcome(
            exit_code=result.exit_code or 1,
            changed_files=[],
            run_id=result.run_id or None,
            launched=getattr(result, "launched", False) is not False,
        ),
        stranded,
        ended=f"{QUOTA_EXHAUSTED_CATEGORY}: {rejection.describe()}",
    )


#: Terminal categories that are not the work's failure: the item goes back to
#: ready and nothing is published as failed.
_NOT_FAILED_COLLECTORS: dict[
    str, Callable[[_DispatchContext, DispatchResult, Path], DispatchOutcome]
] = {
    "killed": _collect_killed,
    QUOTA_EXHAUSTED_CATEGORY: _collect_quota_exhausted,
}


def _budget_stop_wip(ctx: _DispatchContext, worktree_dir: Path) -> tuple[bool, bool]:
    """(committed, uncommitted_work) for a budget stop.

    Canonical mode never commits in the checkout: the walker says whether work
    is there and the caller says where it is. Checkout mode commits as before.
    """
    config = ctx.config
    try:
        if config.publication.publish == "canonical":
            return False, canonical_checkout_has_work(
                worktree_dir,
                repo=config.project.repo or None,
                base_sha=ctx.publication_base_sha,
            )
        return (
            _commit_all_if_dirty(
                worktree_dir,
                ctx.item_id,
                config,
                base_sha=ctx.publication_base_sha or None,
                title=ctx.issue_title,
                run_id=ctx.run_id,
            ),
            False,
        )
    except Exception as exc:  # noqa: BLE001 — the stop proceeds; the reason is logged
        _warn(
            redact_credentials(
                f"Failed to commit WIP for budget-stopped #{ctx.issue_num}: "
                f"{exception_text(exc)}"
            )
        )
        # An indeterminate canonical probe is work to preserve, never "clean".
        return False, config.publication.publish == "canonical"


def _budget_stop_reason(*, committed: bool, uncommitted_work: bool) -> str:
    """The recorded reason says where the work is, not where we wish it were."""
    if committed:
        return "graceful budget stop (WIP committed to branch)"
    if uncommitted_work:
        return "graceful budget stop (uncommitted WIP preserved in the checkout only)"
    return "graceful budget stop (checkout clean)"


def _collect_budget_stop(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Terminal state: graceful budget stop (#859). The live watchdog gave the
    run a wrap-up grace before SIGKILL; commit whatever WIP the agent left so it
    lands on the branch (non-destructive), then mark the issue budget-stopped
    and non-retriable. Distinct from ``budget_stopped`` (the post-hoc cost-aware
    retry guard, #642), which cannot wrap up because the run already exited.

    On OpenShell the agent's work is in the realized checkout the cost stop
    downloaded (#1462), not the uploaded launch snapshot, so that tree is the
    one checked, committed and named.
    """
    config = ctx.config
    issue_num = ctx.issue_num
    branch = ctx.branch
    last_exit_code = result.exit_code
    worktree_dir = _published_checkout(result, worktree_dir)
    committed, uncommitted_work = _budget_stop_wip(ctx, worktree_dir)
    _error(
        _failure_summary_line(
            issue_num=issue_num,
            error_category="budget_stop",
            exit_code=last_exit_code,
            total_attempts=result.attempt_number,
            budget_cap_usd=config.agent.max_budget_usd,
            cost_usd=result.estimated_cost_usd,
        )
    )
    if committed:
        _warn(f"Committed WIP to branch {branch} before stop — work preserved")
    elif uncommitted_work:
        _warn(
            f"Uncommitted WIP remains ONLY in the preserved checkout at "
            f"{worktree_dir}; nothing reached branch {branch}"
        )
    else:
        _warn(f"Worktree preserved for debugging: {worktree_dir}")
    _print_failure_next_steps(
        issue_num=issue_num,
        error_category="budget_stop",
        log_file=result.log_file,
    )
    try:
        mark_failed(
            issue_num,
            config,
            _budget_stop_reason(committed=committed, uncommitted_work=uncommitted_work),
            extra_labels=[config.labels.budget_stop],
        )
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Failed to apply budget-stop label to #{issue_num}: "
                f"{exception_text(exc)}"
            )
        )
    base = config.branches.base or "main"
    return DispatchOutcome(
        exit_code=last_exit_code or 1,
        changed_files=_get_changed_files(
            worktree_dir,
            base,
            repo=config.project.repo or None,
            base_sha=ctx.publication_base_sha or None,
            publish=config.publication.publish,
            dangerously_skip_containment=(config.sandbox.dangerously_skip_containment),
        ),
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _collect_budget_stopped(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Terminal state: sustained over-budget (watchdog). Preserve session +
    worktree, label budget-stopped, do NOT auto-resume (#401).
    """
    config = ctx.config
    issue_num = ctx.issue_num
    last_exit_code = result.exit_code
    _error(
        _failure_summary_line(
            issue_num=issue_num,
            error_category="budget_stopped",
            exit_code=last_exit_code,
            total_attempts=result.attempt_number,
            budget_cap_usd=config.agent.max_budget_usd,
            cost_usd=result.estimated_cost_usd,
        )
    )
    _warn(f"Worktree preserved for debugging: {worktree_dir}")
    _print_failure_next_steps(
        issue_num=issue_num,
        error_category="budget_stopped",
        log_file=result.log_file,
    )
    try:
        mark_failed(
            issue_num,
            config,
            "sustained over per-run budget cap",
            extra_labels=[config.labels.budget_stopped],
        )
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Failed to apply budget-stopped label to #{issue_num}: "
                f"{exception_text(exc)}"
            )
        )
    return DispatchOutcome(
        exit_code=last_exit_code or 1,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def canonical_checkout_has_work(
    worktree_dir: Path, *, repo: str | None, base_sha: str
) -> bool:
    """Under canonical mode: does the checkout differ from the recorded base?

    Answered by the host walker against a fresh canonical repository, never by
    git inside the checkout (review round 5). No recorded base, or anything the
    walker or filter refuses, reads as work to preserve.
    """
    if not base_sha:
        _warn("No recorded publication base: treating the checkout as dirty.")
        return True
    try:
        with work_dirs.work_tempdir("kew-checkout-work-") as temp:
            changed = checkout_changed_paths(
                worktree_dir,
                remote=publication_remote(repo),
                base_sha=base_sha,
                scratch=Path(temp),
            )
    except Exception as exc:  # noqa: BLE001 — a probe; any refusal preserves
        _warn(
            redact_credentials(
                f"Could not extract the checkout, treating it as dirty: "
                f"{exception_text(exc)}"
            )
        )
        return True
    return bool(changed)


def canonical_checkout_no_change(
    worktree_dir: Path, *, repo: str | None, base_sha: str
) -> CanonicalNoChange | None:
    """The walker's evidence that the checkout adds nothing to the recorded
    base. ``None`` means work, no base, or an indeterminate probe; nothing
    closes on ``None`` (review round 11)."""
    if not base_sha:
        _warn("No recorded publication base: the checkout cannot be proved unchanged.")
        return None
    try:
        with work_dirs.work_tempdir("kew-checkout-work-") as temp:
            return checkout_no_change_evidence(
                worktree_dir,
                remote=publication_remote(repo),
                base_sha=base_sha,
                scratch=Path(temp),
            )
    except Exception as exc:  # noqa: BLE001 — a probe; any refusal preserves
        _warn(
            redact_credentials(
                f"Could not extract the checkout, treating it as work: "
                f"{exception_text(exc)}"
            )
        )
        return None


def _checkout_has_work(ctx: _DispatchContext, worktree_dir: Path) -> bool:
    if ctx.config.publication.publish == "canonical":
        return canonical_checkout_has_work(
            worktree_dir,
            repo=ctx.config.project.repo or None,
            base_sha=ctx.publication_base_sha,
        )
    return has_uncommitted_changes(worktree_dir)


def _effective_publication_style(
    config: DispatchConfig, runner: AgentRunner
) -> PublicationStyle:
    """The publication style the prompt is built from.

    Under canonical publication the host publishes every run, so an agent-style
    runner is told to edit and verify only; its credential is stripped in the
    child environment by the same predicate (review round 6).
    """
    if config.publication.publish == "canonical":
        return "host"
    return runner.publication_style


def _sandboxed_publication_style(config: DispatchConfig) -> PublicationStyle:
    """The publication style a container-sandboxed dispatch prompt is built from.

    Canonical publication is host publication for every runner, so an appended
    push instruction is refused here too (round 7). Otherwise the agent commits
    in the container and the host brokers the branch and PR.
    """
    return "host" if config.publication.publish == "canonical" else "agent"


def dispatch_publication_style(
    config: DispatchConfig, runner: AgentRunner, *, sandboxed: bool
) -> PublicationStyle:
    """The publication style *runner*'s dispatch prompt is built from.

    The sandboxed and host launch sites each ask their own half; the prompt
    catalog's preview (#3018) asks here, so it shows what a launch would build.
    """
    if sandboxed:
        return _sandboxed_publication_style(config)
    return _effective_publication_style(config, runner)


def _collect_budget_exceeded(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome:
    """Terminal state: budget exhaustion — exit 0 but not a real success. A
    dirty worktree fails (preserved for debugging); a clean one completes.
    """
    config = ctx.config
    issue_num = ctx.issue_num
    if config.publication.publish == "canonical":
        return _collect_canonical_budget_exceeded(
            ctx, result, worktree_dir, run_sandboxed_path
        )
    if _checkout_has_work(ctx, worktree_dir):
        _warn(
            f"Budget exceeded with uncommitted work "
            f"-- preserving worktree: {worktree_dir}"
        )
        mark_failed(
            issue_num,
            config,
            "budget exceeded with uncommitted work",
            extra_labels=[config.labels.budget_exceeded],
        )
        return _terminal_outcome(
            result,
            exit_code=1,
            category=getattr(result, "error_category", "") or "budget_exceeded",
            detail="uncommitted work preserved in the worktree",
        )
    _success(f"Completed #{issue_num} -- budget exceeded but worktree clean.")
    mark_completed(issue_num, config)
    _auto_unblock_dependents(issue_num, config)
    base = config.branches.base or "main"
    changed_files = _get_changed_files(
        worktree_dir,
        base,
        repo=config.project.repo or None,
        base_sha=ctx.publication_base_sha or None,
        publish=config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    )
    _cleanup_dispatch_dir(worktree_dir, sandboxed=run_sandboxed_path, branch=ctx.branch)
    return DispatchOutcome(
        exit_code=0,
        changed_files=changed_files,
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _collect_canonical_budget_exceeded(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome:
    """Budget exhaustion under canonical publication completes nothing by
    itself: a checkout the walker proves unchanged becomes a no-change
    candidate that waits for confirmation, anything else is preserved as
    work (review round 11)."""
    config = ctx.config
    issue_num = ctx.issue_num
    evidence = canonical_checkout_no_change(
        worktree_dir,
        repo=config.project.repo or None,
        base_sha=ctx.publication_base_sha,
    )
    if evidence is None:
        _warn(
            f"Budget exceeded with work, or no proof the checkout is unchanged "
            f"-- preserving worktree: {worktree_dir}"
        )
        mark_failed(
            issue_num,
            config,
            "budget exceeded with uncommitted work",
            extra_labels=[config.labels.budget_exceeded],
        )
        return _terminal_outcome(
            result,
            exit_code=1,
            category=getattr(result, "error_category", "") or "budget_exceeded",
            detail="work or no proof of an unchanged checkout; worktree preserved",
        )
    _warn(
        f"Budget exceeded for #{issue_num} with nothing to publish; recording a "
        "no-change candidate, the item stays open until it is confirmed."
    )
    _record_no_change(ctx, result, worktree_dir, evidence)
    _cleanup_dispatch_dir(worktree_dir, sandboxed=run_sandboxed_path, branch=ctx.branch)
    return DispatchOutcome(
        exit_code=0,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _mark_success_pr(
    ctx: _DispatchContext,
    result: DispatchResult,
    pr_num: object,
    pr_lookup_failed: bool,
) -> None:
    """Exit-0 with an existing/looked-up PR: apply the pending-review label and
    mark the issue completed (or error out when labelling failed)."""
    config = ctx.config
    issue_num = ctx.issue_num
    label_ok = not pr_lookup_failed
    if isinstance(pr_num, int) and pr_num > 0:
        label_ok = update_pr_labels(
            pr_num,
            config,
            add=[config.labels.pr_pending_review],
        )
    if label_ok:
        mark_completed(issue_num, config)
        _auto_unblock_dependents(issue_num, config)
        _success(f"Completed #{issue_num} -- PR created.")
        if isinstance(pr_num, int) and pr_num > 0:
            _post_success_report(ctx, result, pr_num)
    else:
        _error(
            f"Failed to label PR for issue #{issue_num} -- not marking issue completed"
        )


def _host_posts_agent_report(ctx: _DispatchContext, result: DispatchResult) -> bool:
    """Does kew post the agent's report, or did the agent post it itself?

    The host publishes under canonical publication and for a host-style runner
    (``host_publishes_for``), and for a contained run whose PR the sandbox
    opened (``published_pr_url``): those agents end with the report as their
    final message. Any other agent runs ``gh pr comment`` itself, so posting
    its final message again would put a second copy on the PR.
    """
    return (
        ctx.config.publication.publish == "canonical"
        or ctx.publication_style == "host"
        or bool(result.published_pr_url)
    )


def _post_success_report(
    ctx: _DispatchContext, result: DispatchResult, pr_num: int
) -> None:
    """Post the run's Dispatch Report once: the agent's own, else the fallback."""
    config = ctx.config
    if _host_posts_agent_report(ctx, result) and _post_agent_report(
        str(pr_num), result.log_file, config, result=result
    ):
        return
    # Truncation guard (#664): a headless run can finalize success while
    # the agent's own Dispatch Report comment silently never posted.
    # Fill the gap fail-soft.
    selected_model: str | None = None
    try:
        record = ctx.store.get(result.run_id) if ctx.store is not None else None
        selected_model = record.model if record is not None else None
    except Exception:  # noqa: BLE001  # nosec B110 — reporting fail-soft
        pass
    _post_dispatch_report_fallback(
        pr_num,
        ctx.item_id,
        ctx.complexity,
        result,
        config,
        selected_model=selected_model,
    )


def _publication_error_category(exc: Exception) -> str:
    """Stable category for a bounded, sanitized publication failure."""
    if isinstance(exc, PublicationHeld):
        return "publication_review_required"
    message = str(exc).lower()
    if "rate limit" in message or "rate-limit" in message or "http 429" in message:
        return "publication_rate_limited"
    return "publication_unavailable"


def _record_publication_continuation(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> None:
    """Mint one resume handle unless this run already owns its predecessor."""
    if ctx.checkout_generation:
        _require_checkout_generation(
            worktree_dir, ctx.checkout_generation, boundary="continuation registration"
        )
    _mint_publication_continuation(
        ctx.store,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
        branch=ctx.branch,
        issue_num=ctx.issue_num,
        repo=ctx.config.project.repo or None,
        publish=ctx.config.publication.publish,
        base=ctx.publication_base_sha,
        run_id=result.run_id,
        worktree_dir=worktree_dir,
    )


def _mint_publication_continuation(
    store: RunStore | None,
    *,
    epic_ref: str | None,
    item_id: str,
    branch: str,
    issue_num: int,
    repo: str | None,
    publish: str,
    base: str,
    run_id: str,
    worktree_dir: Path,
) -> None:
    """Mint one resume handle for *run_id*'s preserved checkout: the base
    binding first, then the idempotent continuation insert. Shared by the
    host deferral and the contained handler, so a run parked inside the
    sandbox already carries its handle when the host reaches it (round 18).
    """
    if store is None or epic_ref is None or not run_id:
        return
    already_owned = any(
        record.state == "continuing" and record.continuation_run_id == run_id
        for record in store.list_continuations(epic_ref)
    )
    if already_owned:
        return
    generation = _checkout_generation(worktree_dir)
    # The base this checkout was cut from is bound to the owning run before
    # the resume handle exists; without it nothing is minted (round 15).
    _bind_publication_base(
        store,
        publish=publish,
        base=base,
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        branch=branch,
        checkout=worktree_dir,
    )
    continuation = store.record_continuation(
        epic_ref,
        item_id,
        run_id,
        branch,
        worktree_dir,
        checkout_generation=generation,
        precommit_check=partial(
            _require_checkout_generation,
            worktree_dir,
            generation,
            boundary="publication continuation registration",
        ),
    )
    audit.emit_continuation_evidence(
        store,
        "continuation_detected",
        actor=Actor("policy", "host-publication"),
        phase="detection",
        epic_ref=continuation.epic_ref,
        item_id=continuation.item_id,
        original_run_id=continuation.original_run_id,
        continuation_run_id=None,
        branch=continuation.branch,
        worktree_identity=continuation.worktree_identity,
        generation=continuation.generation,
        state=continuation.state,
        outcome="publication_deferred",
        run_id=run_id,
        issue_num=issue_num,
        repo=repo,
    )


def _preserve_publication_work(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> None:
    """Mint the resume handle that keeps the deferred work reachable."""
    try:
        _record_publication_continuation(ctx, result, worktree_dir)
    except Exception as continuation_exc:  # noqa: BLE001 — best effort
        _warn(
            "Publication was deferred but its preserved-work "
            f"continuation could not be recorded: {continuation_exc}"
        )


def _normalized_publication_job(store: RunStore | None, run_id: str | None) -> Any:
    """The live normalized job this run attempt opened, or None (I14).

    Its existence is what decides which writer owns a deferral. Old rows have
    none and keep the legacy ``awaiting_publication`` encoding they were
    written under; a run whose publication the normalized lifecycle owns must
    not *also* overload the run status, because two writers for one fact is
    exactly how a restarted host ends up unable to say whether the push landed.
    """
    reader = getattr(store, "publication_job_for_attempt", None)
    if reader is None or store is None or not run_id:
        return None
    record = store.get(run_id)
    if record is None:
        return None
    try:
        job = reader(run_id=run_id, attempt_number=record.attempt_number)
    except Exception as exc:  # noqa: BLE001 — deferral may never raise
        _logger.debug(
            "normalized publication job could not be read: %s",
            redact_credentials(exception_text(exc)),
        )
        return None
    return job if job is not None and job.active else None


def _defer_onto_job(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    job: Any,
    *,
    category: str,
    step: str,
    detail: str,
) -> None:
    """Record the deferral on the normalized job, leaving the run status alone.

    The attempt is written the way every other publication step is — before
    what it describes — and the job moves to ``ambiguous``, which remembers the
    state it interrupted. That is strictly more than ``awaiting_publication``
    ever said: it distinguishes "the push may have landed" from "nothing left
    the host", which is the question recovery actually has.
    """
    store: Any = ctx.store
    now = datetime.now(UTC)
    store.record_publication_attempt(job_id=job.job_id, step=step, now=now)
    store.mark_publication_state(
        job_id=job.job_id,
        expected_state=job.state,
        state=PUBLICATION_AMBIGUOUS,
        detail=f"{category}: {detail}",
        now=now,
    )
    audit.emit(
        store,
        "publication_deferred",
        actor=Actor("policy", "host-publication"),
        run_id=result.run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        repo=ctx.config.project.repo or None,
        payload={
            "branch": ctx.branch,
            "category": category,
            "step": step,
            "error": detail,
            "worktree_preserved": True,
            "job_id": job.job_id,
        },
    )


def _contained_failure_parks(store: RunStore | None, run_id: str) -> bool:
    """Whether a contained run whose publication failed finalizes straight
    into ``awaiting_publication`` (one write, review round 18). A run a
    normalized publication job owns keeps its status: that writer owns the
    deferral."""
    if store is None or not run_id:
        return False
    return _normalized_publication_job(store, run_id) is None


def _already_awaiting_publication(store: RunStore, run_id: str) -> bool:
    """True when the contained path already parked this run durably (review
    round 17): the host then only preserves the work and mints its handle."""
    record = store.get(run_id)
    return record is not None and record.status == "awaiting_publication"


def _refuse_deferred_publication(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    exc: CredentialPublicationRefused,
) -> DispatchOutcome:
    """Record a non-releasable result without creating a retry deferral."""
    step, detail = publication_failure(exc)
    append_publication_failure(
        result.log_file,
        step=step,
        category=CREDENTIAL_PUBLICATION_REFUSAL,
        detail=detail,
    )
    store = ctx.store
    job = _normalized_publication_job(store, result.run_id)
    if store is not None and job is not None:
        publication_store: Any = store
        publication_store.mark_publication_state(
            job_id=job.job_id,
            expected_state=job.state,
            state=PUBLICATION_ABANDONED,
            detail=f"{CREDENTIAL_PUBLICATION_REFUSAL}: {detail}",
            now=datetime.now(UTC),
        )
    if store is not None and result.run_id:
        transitioned = store.transition_run_status(
            result.run_id,
            expected_status="success",
            new_status="refused",
            error_category=CREDENTIAL_PUBLICATION_REFUSAL,
        )
        if not transitioned:
            store.transition_run_status(
                result.run_id,
                expected_status="awaiting_publication",
                new_status="refused",
                error_category=CREDENTIAL_PUBLICATION_REFUSAL,
            )
    result.error_category = CREDENTIAL_PUBLICATION_REFUSAL
    audit.emit(
        store,
        CREDENTIAL_PUBLICATION_REFUSAL,
        actor=Actor("policy", "host-publication"),
        run_id=result.run_id,
        issue_num=ctx.issue_num,
        item_id=ctx.item_id,
        repo=ctx.config.project.repo or None,
        payload={"reason": exc.reason, "publication_allowed": False},
    )
    _warn(
        "Automatic publication refused by credential lineage policy; "
        f"worktree preserved: {worktree_dir}"
    )
    return _terminal_outcome(
        result,
        exit_code=1,
        category=CREDENTIAL_PUBLICATION_REFUSAL,
        detail="automatic publication refused by credential lineage policy; "
        "worktree preserved",
    )


def _ordinary_publication_deferral(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    exc: Exception,
) -> DispatchOutcome:
    """Persist a completed run as awaiting host publication and preserve work.

    The implementation run is already terminal and must never be charged or
    launched again merely because GitHub publication failed. The run row is
    the durable redispatch guard consumed by the epic overlay; the audit event
    records the state transition without copying provider output (which may
    contain sensitive request details).

    I14 cuts the writer over: when the normalized publication lifecycle owns
    this attempt, the deferral is written there and the run status is left
    exactly as the implementation left it. Only runs with no job — every run
    that exists today — still take the overloaded status.
    """
    category = _publication_error_category(exc)
    step, detail = publication_failure(exc)
    append_publication_failure(
        result.log_file,
        step=step,
        category=category,
        detail=detail,
    )
    store = ctx.store
    job = _normalized_publication_job(store, result.run_id)
    if store is not None and job is not None:
        _defer_onto_job(
            ctx, result, worktree_dir, job, category=category, step=step, detail=detail
        )
        _preserve_publication_work(ctx, result, worktree_dir)
    elif store is not None and result.run_id:
        transitioned = store.transition_run_status(
            result.run_id,
            expected_status="success",
            new_status="awaiting_publication",
            error_category=category,
        ) or _already_awaiting_publication(store, result.run_id)
        if transitioned:
            audit.emit(
                store,
                "publication_deferred",
                actor=Actor("policy", "host-publication"),
                run_id=result.run_id,
                issue_num=ctx.issue_num,
                item_id=ctx.item_id,
                repo=ctx.config.project.repo or None,
                payload={
                    "branch": ctx.branch,
                    "category": category,
                    "step": step,
                    "error": detail,
                    "worktree_preserved": True,
                },
            )
            _preserve_publication_work(ctx, result, worktree_dir)
        else:
            _log(
                f"Publication deferral for run {result.run_id} lost its state race;"
                " preserving the newer run state."
            )
    _warn(
        f"Completed implementation awaits host publication ({category}); "
        f"worktree preserved: {worktree_dir}"
    )
    return _terminal_outcome(
        result,
        exit_code=1,
        category=category,
        detail="completed implementation awaits host publication; worktree preserved",
    )


def _defer_publication(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    exc: Exception,
) -> DispatchOutcome:
    """Route publication failures to a retryable or non-releasable result."""
    if isinstance(exc, CredentialPublicationRefused):
        return _refuse_deferred_publication(ctx, result, worktree_dir, exc)
    return _ordinary_publication_deferral(ctx, result, worktree_dir, exc)


def _record_no_change(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    canonical_no_change: CanonicalNoChange | None = None,
) -> None:
    """Bind this attempt's zero-diff exit to one durable no-change candidate.

    The single seam every zero-change path goes through — host broker and
    contained run alike — so the run identity, provider and base the judgment
    is bound to cannot drift between them.
    """
    _complete_no_changes(
        ctx.config,
        item_id=ctx.item_id,
        provider=ctx.provider,
        store=ctx.store,
        run_id=result.run_id or ctx.run_id or "",
        worktree_dir=worktree_dir,
        canonical_no_change=canonical_no_change,
        item_text=f"{ctx.issue_title}\n{ctx.issue_body}",
        trusted_base_sha=ctx.publication_base_sha,
    )
    _release_no_change_claim(ctx)


def _release_no_change_claim(ctx: _DispatchContext) -> None:
    """Hand the item back once the zero-diff outcome is durable (#1611).

    The candidate settles the *outcome*; this settles the *claim*. Nothing is
    closed and nothing is commented — the label only says the run is over, so
    ``kew dispatch`` stops reading a finished item as in-flight and ``kew
    cleanup`` stops reading it as an orphan, while the confirm/reject decision
    stays with a person.

    Best-effort, and loud when it fails: the judgment is already recorded, so a
    tracker that will not take the relabel must not turn a correct exit-0 run
    into a failure — but it must never pass for a released claim either.
    """
    try:
        mark_no_change_candidate(ctx.issue_num, ctx.config)
    except Exception as exc:  # noqa: BLE001 — a label never sinks an exit-0 run
        _warn(
            redact_credentials(
                f"{item_ref(ctx.item_id)} recorded a no-change outcome, but its"
                f" dispatch claim could not be released: "
                f"{exception_text(exc)}. The item may still"
                " read as in-flight until its labels are cleared."
            )
        )


def _broker_or_complete(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome | None:
    """Exit-0 with no PR yet: broker the git ceremony on the host path, or
    record a no-op completion on the sandboxed path. Returns a short-circuit
    ``DispatchOutcome`` only when brokering fails (worktree preserved)."""
    config = ctx.config
    issue_num = ctx.issue_num
    issue_title = ctx.issue_title
    item_id = ctx.item_id
    branch = ctx.branch
    store = ctx.store
    no_changes: list[CanonicalNoChange] = []
    if not run_sandboxed_path:
        # The agent finished but may have left work uncommitted / unpushed
        # (common with weaker local runners). kew brokers the git ceremony.
        # The sandboxed path already brokers push+PR, so it is excluded here.
        try:
            brokered = _broker_agent_work(
                worktree_dir,
                branch,
                item_id,
                issue_title,
                config,
                store=store,
                run_id=result.run_id or None,
                base_sha=ctx.publication_base_sha or None,
                log_file=result.log_file,
                no_change=no_changes,
            )
        except Exception as exc:
            # Implementation completed. Publication is a distinct host-owned
            # phase: preserve both the commit/worktree and a durable
            # non-dispatchable state instead of relaunching the agent.
            _error(
                redact_credentials(
                    f"Failed to broker PR for #{issue_num}: {exception_text(exc)}"
                )
            )
            return _defer_publication(ctx, result, worktree_dir, exc)
        if brokered:
            _finalize_brokered_pr(
                issue_num,
                branch,
                config,
                item_id=item_id,
                complexity=ctx.complexity,
                result=result,
                store=store,
            )
        else:
            _record_no_change(
                ctx,
                result,
                worktree_dir,
                no_changes[0] if no_changes else None,
            )
    else:
        _record_sandboxed_exit(ctx, result, worktree_dir)
    return None


def _persisted_pr_url(store: RunStore | None, run_id: str | None) -> str:
    if store is None or not run_id:
        return ""
    record = store.get(run_id)
    return (getattr(record, "pr_url", "") or "") if record is not None else ""


def _pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)/?$", url.strip())
    return int(match.group(1)) if match else None


def _finalize_persisted_pr(
    ctx: _DispatchContext, result: DispatchResult, url: str
) -> None:
    """A PR this run created is finalized from that identity — labels,
    completion, dependents — when the lookup comes back empty; the URL is
    ours, not the agent's (review round 11)."""
    number = _pr_number_from_url(url)
    if number is None:
        _warn(
            f"PR lookup for {ctx.branch} found nothing and the run's PR {url} "
            "carries no number; the item stays claimed."
        )
        return
    _warn(
        f"PR lookup for {ctx.branch} found nothing, but the run created {url}; "
        "finalizing from it."
    )
    _mark_success_pr(ctx, result, number, False)


def _record_sandboxed_exit(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> None:
    """A sandboxed exit-0 with no PR found by lookup.

    The sandbox path published before this point. A PR the run record already
    holds is authoritative: an empty lookup right after creation is a lookup
    problem, not a no-change. Under canonical mode a no-change is recorded
    only with the walker's evidence; nothing here reads the checkout with
    git (review round 9).
    """
    persisted = getattr(result, "published_pr_url", "") or _persisted_pr_url(
        ctx.store, result.run_id or ctx.run_id
    )
    if persisted:
        _finalize_persisted_pr(ctx, result, persisted)
        return
    evidence = getattr(result, "canonical_no_change", None)
    if ctx.config.publication.publish == "canonical" and evidence is None:
        _warn(
            f"{item_ref(ctx.item_id)} exited without a PR and without canonical "
            "no-change evidence; nothing recorded, the item stays open."
        )
        return
    _record_no_change(ctx, result, worktree_dir, evidence)


def _handle_success_pr(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome | None:
    """Exit-0 success: look up the PR, then either mark completion via labels or
    broker the git ceremony. Returns a short-circuit ``DispatchOutcome`` only
    when brokering fails."""
    config = ctx.config
    issue_num = ctx.issue_num
    branch = ctx.branch
    pr_lookup_failed = False
    try:
        pr_num = check_pr_exists(branch, config)
    except Exception as e:
        _error(
            redact_credentials(
                f"Failed to check PR status for #{issue_num}: {exception_text(e)}"
            )
        )
        pr_num = None
        pr_lookup_failed = True
    if pr_num:
        early = _publish_existing_pr_continuation(
            ctx, result, worktree_dir, run_sandboxed_path
        )
        if early is not None:
            return early
        _mark_success_pr(ctx, result, pr_num, pr_lookup_failed)
        return None
    if (
        pr_lookup_failed
        and not run_sandboxed_path
        and (
            config.publication.publish == "canonical" or ctx.publication_style == "host"
        )
    ):
        # The remote read is not evidence that no work exists. Route through
        # the broker so host-owned runners commit before any later cleanup; a
        # second provider failure becomes durable awaiting_publication.
        return _broker_or_complete(ctx, result, worktree_dir, run_sandboxed_path)
    if pr_lookup_failed:
        _mark_success_pr(ctx, result, pr_num, pr_lookup_failed)
        return None
    return _broker_or_complete(ctx, result, worktree_dir, run_sandboxed_path)


def _publish_existing_pr_continuation(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome | None:
    """Publish host dispatch work before completing an existing PR.

    Canonical runs use their recorded base and publication ownership checks.
    An existing PR alone proves nothing about the current checkout's edits.
    Checkout mode retains the legacy continuation update.
    """
    if run_sandboxed_path:
        return None
    canonical = ctx.config.publication.publish == "canonical"
    if ctx.worktree_dir is None and not canonical:
        return None
    try:
        if canonical:
            _publish_canonical_pr_update(ctx, result, worktree_dir)
            return None
        select_publication_mode(
            ctx.config.publication.publish,
            dangerously_skip_containment=(
                ctx.config.sandbox.dangerously_skip_containment
            ),
            store=ctx.store,
            run_id=result.run_id or None,
            item_id=ctx.item_id,
            branch=ctx.branch,
            repo=ctx.config.project.repo or None,
        )
        worktree.publish_existing_pr_branch(
            worktree_dir,
            ctx.branch,
            before_push=partial(revoke_ci_approval, ctx.branch, ctx.config),
            commit_message=_continuation_commit_message(ctx, result),
            repo=ctx.config.project.repo or None,
            publish=ctx.config.publication.publish,
            dangerously_skip_containment=(
                ctx.config.sandbox.dangerously_skip_containment
            ),
            store=ctx.store,
            run_id=result.run_id or ctx.run_id,
            item_id=ctx.item_id,
        )
    except Exception as exc:
        _error(
            redact_credentials(
                f"Failed to publish work for existing PR of #{ctx.issue_num}: "
                f"{exception_text(exc)}"
            )
        )
        return _defer_publication(ctx, result, worktree_dir, exc)
    return None


def _continuation_commit_message(ctx: _DispatchContext, result: DispatchResult) -> str:
    """Message for a commit kew adds to the branch of an already-open PR (#2062)."""
    return _broker_commit_message(
        ctx.item_id,
        ctx.config,
        title=ctx.issue_title,
        run_id=result.run_id or ctx.run_id,
        log_file=result.log_file,
    )


def _publish_canonical_pr_update(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> None:
    """Bind an existing PR update to the dispatch's base and publication lineage."""
    _git_push_branch(
        ctx.branch,
        worktree_dir,
        before_push=partial(revoke_ci_approval, ctx.branch, ctx.config),
        repo=ctx.config.project.repo or None,
        base_sha=ctx.publication_base_sha or None,
        message=_continuation_commit_message(ctx, result),
        publish="canonical",
        store=ctx.store,
        run_id=result.run_id or ctx.run_id,
        item_id=ctx.item_id,
    )


def _notify_dispatch_success(ctx: _DispatchContext, result: DispatchResult) -> None:
    """Best-effort success notification — never affects the dispatch outcome."""
    notifier = ctx.notifier
    config = ctx.config
    issue_num = ctx.issue_num
    issue_title = ctx.issue_title
    if notifier is not None and config.alerting.notify_on_success:
        try:
            notifier.notify(
                NotificationEvent(
                    event_type="dispatch_succeeded",
                    severity="info",
                    message=(f"Dispatch succeeded for #{issue_num}: {issue_title}"),
                    issue_number=issue_num,
                )
            )
        except Exception as e:
            _error(
                redact_credentials(
                    f"Failed to send success notification for #{issue_num}: "
                    f"{exception_text(e)}"
                )
            )


def _notify_dispatch_failure(
    ctx: _DispatchContext, result: DispatchResult, last_exit_code: int
) -> None:
    """Best-effort failure notification — never affects the dispatch outcome."""
    notifier = ctx.notifier
    config = ctx.config
    issue_num = ctx.issue_num
    issue_title = ctx.issue_title
    if notifier is not None and config.alerting.notify_on_failure:
        try:
            notifier.notify(
                NotificationEvent(
                    event_type="dispatch_failed",
                    severity="error",
                    message=(
                        f"Dispatch failed for #{issue_num}: "
                        f"{issue_title} (exit {last_exit_code})"
                    ),
                    issue_number=issue_num,
                )
            )
        except Exception as e:
            _error(
                redact_credentials(
                    f"Failed to send failure notification for #{issue_num}: "
                    f"{exception_text(e)}"
                )
            )


def _published_checkout(result: DispatchResult, worktree_dir: Path) -> Path:
    """The tree whose diff describes what this dispatch actually published.

    The dispatch's own checkout on every runtime but OpenShell, where the agent
    edited an UPLOADED copy and its work came back into a realized sibling
    (:mod:`kew.openshell_checkout`). Publication already swapped to that tree
    (``dispatch_sandbox._publication_checkout``), so the outcome has to follow
    it: read off ``worktree_dir`` there, a successful OpenShell dispatch
    reported an EMPTY changed-file set — the launch snapshot holds none of the
    agent's edits — to the overlap coordinator that consumes it
    (PR #2451 review).

    A realization that refused leaves the path unset and never reaches here:
    the publication gate finalizes that run as a failure.
    """
    realized = result.realized_checkout
    return worktree_dir if realized is None else realized


def _collect_success(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome:
    """Terminal state: agent exited 0. Finalize the PR (label or broker), clean
    up the workspace, and notify."""
    config = ctx.config
    early = _handle_success_pr(ctx, result, worktree_dir, run_sandboxed_path)
    if early is not None:
        return early
    base = config.branches.base or "main"
    changed_files = _get_changed_files(
        _published_checkout(result, worktree_dir),
        base,
        repo=config.project.repo or None,
        base_sha=ctx.publication_base_sha or None,
        publish=config.publication.publish,
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    )
    _cleanup_dispatch_dir(worktree_dir, sandboxed=run_sandboxed_path, branch=ctx.branch)
    _notify_dispatch_success(ctx, result)
    return DispatchOutcome(
        exit_code=result.exit_code,
        changed_files=changed_files,
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _collect_hard_fail_or_rescue(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Terminal state: nonzero exit. A run that exhausted max_steps
    (stop_reason="aborted") with coherent work is rescued into a flagged DRAFT
    PR instead of discarded; otherwise it hard-fails (worktree preserved, issue
    marked failed).

    Fails safe: any error in rescue qualification OR brokering falls through to
    the hard-fail. This also covers sandboxed max-steps failures: there
    ``worktree_dir == checkout_dir`` (a real host-side git clone), so the host
    brokers the push exactly as designed — the sandboxed agent never opened its
    own PR on failure. Once a rescue is confirmed, the run is never downgraded
    to a hard-fail by later bookkeeping.
    """
    config = ctx.config
    issue_num = ctx.issue_num
    last_exit_code = result.exit_code
    if _try_rescue_incomplete(ctx, result, worktree_dir):
        return _terminal_outcome(
            result,
            exit_code=last_exit_code or 1,
            changed_files=_rescue_changed_files(ctx, worktree_dir),
            category="rescued_incomplete",
            detail="coherent incomplete work brokered into a draft PR",
        )
    _error(
        _failure_summary_line(
            issue_num=issue_num,
            error_category=result.error_category,
            exit_code=last_exit_code,
            total_attempts=result.attempt_number,
            budget_cap_usd=config.agent.max_budget_usd,
            cost_usd=result.estimated_cost_usd,
            breaker_rule=result.breaker_rule,
            breaker_threshold_usd=result.breaker_threshold_usd,
        )
    )
    _publish_terminal_failure(
        ctx, result.error_category or "failure", _terminal_failure_reason(result)
    )
    _warn(f"Worktree preserved for debugging: {worktree_dir}")
    _print_failure_next_steps(
        issue_num=issue_num,
        error_category=result.error_category,
        log_file=result.log_file,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
    )
    _notify_dispatch_failure(ctx, result, last_exit_code)
    return _terminal_outcome(
        result,
        exit_code=last_exit_code or 1,
        detail="worktree preserved for debugging",
    )


def _rescue_changed_files(ctx: _DispatchContext, worktree_dir: Path) -> list[str]:
    try:
        return _get_changed_files(
            worktree_dir,
            ctx.config.branches.base or "main",
            repo=ctx.config.project.repo or None,
            base_sha=ctx.publication_base_sha or None,
            publish=ctx.config.publication.publish,
            dangerously_skip_containment=(
                ctx.config.sandbox.dangerously_skip_containment
            ),
        )
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Could not inspect rescued publication paths: {exception_text(exc)}"
            )
        )
        return []


def _try_rescue_incomplete(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> bool:
    """Attempt to broker a coherent incomplete run into a draft PR."""
    if result.exit_code == 0:
        return False
    try:
        if not _qualifies_for_rescue(
            result,
            worktree_dir,
            ctx.config,
            base_sha=ctx.publication_base_sha or None,
        ):
            return False
        _rescue_incomplete_run(
            worktree_dir,
            ctx.branch,
            ctx.issue_num,
            ctx.issue_title,
            ctx.config,
            item_id=ctx.item_id,
            store=ctx.store,
            run_id=result.run_id or None,
            base_sha=ctx.publication_base_sha or None,
            log_file=result.log_file,
        )
        return True
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Rescue of incomplete #{ctx.issue_num} failed, "
                f"falling back to hard-fail: {exception_text(exc)}"
            )
        )
        return False


def _terminal_failure_reason(result: DispatchResult) -> str:
    """What the issue's failure comment says stopped the run."""
    if result.error_category == "circuit_breaker":
        return _breaker_reason(
            result.breaker_rule,
            result.estimated_cost_usd,
            result.breaker_threshold_usd,
        )
    return f"exit code {result.exit_code}"


def _publish_terminal_failure(
    ctx: _DispatchContext, category: str, reason: str
) -> None:
    if ctx.terminal_failure is None:
        mark_failed(ctx.issue_num, ctx.config, reason)
        return
    try:
        ctx.terminal_failure(category, reason)
    except Exception as exc:
        _warn(
            redact_credentials(
                f"Failed to publish terminal failure for {item_ref(ctx.item_id)}: "
                f"{exception_text(exc)}"
            )
        )


def _published_pr_for_dispatch(ctx: _DispatchContext) -> int | None:
    """The PR number this dispatch already published, or ``None``.

    Deliberately fail-closed: an unreadable remote answers ``None``. Only
    positive evidence that an artifact reached the remote may soften an
    integrity incident — reading a GitHub outage as "published" would disarm
    the check exactly when it matters.
    """
    try:
        return check_pr_exists(ctx.branch, ctx.config)
    except Exception as exc:  # noqa: BLE001 — unreadable is not published
        _warn(
            redact_credentials(
                f"Could not read PR status for {item_ref(ctx.item_id)} while"
                f" assessing an integrity incident: "
                f"{exception_text(exc)}. Treating the run as"
                " unpublished."
            )
        )
        return None


def _comment_integrity_incident(
    ctx: _DispatchContext, reason: str, pr_num: int
) -> None:
    """Report an integrity incident on a published item, changing no labels.

    A plain issue comment — never ``mark_failed`` — because the run's labels
    describe an artifact that exists. Fail-soft and loud: the audit event is
    the durable record, so a tracker that will not take the comment must not
    sink a run whose work is already on the remote. Non-GitHub items have no
    issue to comment on and keep only the audit event.
    """
    repo = (ctx.config.project.repo or "").strip()
    if not repo or not GITHUB_ID_RE.fullmatch(ctx.item_id):
        _warn(
            f"Integrity incident for {item_ref(ctx.item_id)} is recorded in the"
            " audit log only: no GitHub issue to comment on."
        )
        return
    body = (
        f"**Operator checkout integrity incident** — {reason}.\n\n"
        f"This run had already published PR #{pr_num}, so the incident does"
        " not relabel it: the run's own outcome stands (a clean exit keeps"
        " pr-pending-review; a non-zero exit or a kill keeps its normal"
        " handling). The"
        " incident is"
        " recorded as a `root_checkout_integrity_violation` audit event"
        " (`kew audit tail --event root_checkout_integrity_violation`) with the"
        " before/after `HEAD`/base-ref values —"
        " check the operator checkout before dispatching again."
    )
    try:
        posted = run(
            [
                "gh",
                "issue",
                "comment",
                ctx.item_id,
                "--repo",
                repo,
                "--body",
                body,
            ],
            check=False,
            timeout=30,
        )
        if posted.returncode != 0:
            _warn(
                redact_credentials(
                    f"Could not comment the integrity incident on"
                    f" {item_ref(ctx.item_id)}: "
                    + redact_credentials(posted.stderr or posted.stdout or "").strip()[
                        :200
                    ]
                )
            )
    except Exception as exc:  # noqa: BLE001 — a comment never sinks a published run
        _warn(
            redact_credentials(
                f"Could not comment the integrity incident on"
                f" {item_ref(ctx.item_id)}: {exception_text(exc)}"
            )
        )


def _collect_integrity_violation(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    reason: str,
) -> DispatchOutcome | None:
    """Decide what an operator-checkout integrity incident does to this run.

    The incident is always audited by ``_root_checkout_integrity_check``; the
    only question here is whether it may also rewrite the run's outcome. It may
    not once a PR exists. The violation happened in the *operator's* checkout,
    while the PR is reviewable work that reached the remote regardless —
    relabelling it ``agent-failed`` hid a finished node from ``kew epic
    status`` and asked a human to redo work already waiting for review
    (#1660). On that branch the incident becomes an audit event plus an issue
    comment, and ``None`` lets the normal terminal ladder settle the run.

    A run that published nothing keeps the original fail-closed terminal
    failure with its worktree preserved for incident review, and so does one
    whose PR lookup could not be answered.
    """
    pr_num = _published_pr_for_dispatch(ctx)
    if pr_num is not None:
        _comment_integrity_incident(ctx, reason, pr_num)
        _warn(
            f"Integrity incident for {item_ref(ctx.item_id)} recorded against"
            f" published PR #{pr_num}; its outcome and labels stand."
        )
        return None
    _publish_terminal_failure(ctx, "root_checkout_integrity_violation", reason)
    _warn(f"Worktree preserved for incident review: {worktree_dir}")
    return DispatchOutcome(
        exit_code=result.exit_code or 1,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _record_quota_warning(ctx: _DispatchContext, result: DispatchResult) -> None:
    """Record the provider's usage warning against the run's token (#3329).

    Whatever the run's outcome: a warning is about the token's window, not the
    work. A contained run's fold records its own (``sandbox_log_fold``).
    Advisory, and read off a stream the agent can write: whatever goes wrong
    recording it is logged, and the run goes on to its own outcome.
    """
    try:
        warning = result.quota_warning
        if warning is None or ctx.store is None:
            return
        record = record_runner_token_warning(
            ctx.store, warning, run_id=result.run_id or ctx.run_id or ""
        )
        if record is not None:
            _warn(f"Provider warning: {record.reason()}")
    except Exception as exc:  # noqa: BLE001 — an advisory write never fails a run
        _logger.warning(
            "run %s: usage warning not recorded (%s)",
            result.run_id or ctx.run_id,
            type(exc).__name__,
        )


def _collect_result(
    ctx: _DispatchContext,
    *,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome:
    """Translate the launched agent's ``DispatchResult`` into the final
    ``DispatchOutcome``: label transitions, worktree lifecycle, PR brokering,
    rescue, and notification.

    A terminal-state ladder: dispatches on ``result.error_category`` (killed /
    budget_stop / budget_stopped / budget_exceeded) before the success (exit 0)
    and hard-fail paths, each handled by a named phase helper. Behavior is
    identical to the inline tail these were extracted from — order preserved.

    An operator-checkout integrity incident is evaluated first but may only
    *short-circuit* the ladder for a run that published nothing: see
    :func:`_collect_integrity_violation`.
    """
    _record_quota_warning(ctx, result)
    if ctx.checkout_generation:
        _require_checkout_generation(
            worktree_dir, ctx.checkout_generation, boundary="result publication"
        )
    integrity_reason = _root_checkout_integrity_check(ctx, run_id=result.run_id or None)
    if integrity_reason is not None:
        incident = _collect_integrity_violation(
            ctx, result, worktree_dir, integrity_reason
        )
        if incident is not None:
            return incident
    special = _collect_special_category(ctx, result, worktree_dir, run_sandboxed_path)
    if special is not None:
        return special
    category = result.error_category
    if category == "needs_human":
        disputed = _collect_acceptance_dispute(ctx, result, worktree_dir)
        if disputed is not None:
            return disputed
        return _collect_needs_human(ctx, result, worktree_dir)
    if category:
        return _collect_hard_fail_or_rescue(ctx, result, worktree_dir)
    if result.exit_code == 0:
        return _collect_success(ctx, result, worktree_dir, run_sandboxed_path)
    return _collect_hard_fail_or_rescue(ctx, result, worktree_dir)


def _publication_step(name: str) -> PublicationStep:
    """A publication step name carried on a result, or ``unknown``."""
    if name in ("commit", "push", "pr_create"):
        return cast("PublicationStep", name)
    return "unknown"


def _collect_publication_category(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome | None:
    """A held or failed contained publication is deferred like a host one:
    the work is preserved and its continuation minted (review round 16)."""
    category = result.error_category
    if category == "publication_held":
        flags = tuple(line for line in result.subtype.splitlines() if line)
        return _defer_publication(ctx, result, worktree_dir, PublicationHeld(flags))
    if category == SANDBOX_PUBLICATION_FAILED_CATEGORY:
        step, _sep, detail = result.subtype.partition("\n")
        return _defer_publication(
            ctx, result, worktree_dir, PublicationError(_publication_step(step), detail)
        )
    return None


def _collect_special_category(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome | None:
    """Collect terminal categories with dedicated lifecycle handling."""
    category = result.error_category
    if category == CREDENTIAL_PUBLICATION_REFUSAL:
        _warn(
            "Credential-exposed output remains unpublished; "
            f"worktree preserved: {worktree_dir}"
        )
        return DispatchOutcome(
            exit_code=1,
            changed_files=[],
            run_id=result.run_id or None,
            launched=getattr(result, "launched", False) is not False,
        )
    not_failed = _NOT_FAILED_COLLECTORS.get(category)
    if not_failed is not None:
        return not_failed(ctx, result, worktree_dir)
    if category == "budget_stop":
        return _collect_budget_stop(ctx, result, worktree_dir)
    if category == "budget_stopped":
        return _collect_budget_stopped(ctx, result, worktree_dir)
    if category == "budget_exceeded":
        return _collect_budget_exceeded(ctx, result, worktree_dir, run_sandboxed_path)
    deferred = _collect_publication_category(ctx, result, worktree_dir)
    if deferred is not None:
        return deferred
    if category == _SANDBOX_NO_CHANGE_CATEGORY and result.exit_code == 0:
        # A no-change classification is only a success outcome on a clean exit.
        # The category is emitted only with exit 0 on the sandbox path today, so
        # this guard is defensive: a future non-zero emitter must fall through to
        # the hard-fail ladder rather than persist a candidate for a failed run.
        return _collect_no_change(ctx, result, worktree_dir, run_sandboxed_path)
    return None


def _collect_no_change(
    ctx: _DispatchContext,
    result: DispatchResult,
    worktree_dir: Path,
    run_sandboxed_path: bool,
) -> DispatchOutcome:
    """Terminal state: the agent exited 0 having changed nothing.

    The contained runner settles this host-side — ``_checkout_has_no_changes``
    reads the real checkout — and hands the classification back on an exit-0
    result, so there is nothing left to publish and nothing the remote could
    say that would change the answer. It lands here rather than in
    ``_collect_success`` for that reason, and it has to be caught before the
    error-category ladder at all: a named classification is not a failure, and
    reading it as one meant the contained path never reached the no-change
    seam. No candidate was recorded, the item was labelled failed, and the
    operator was told to retry a run that was correct (#1606).
    """
    _record_no_change(
        ctx,
        result,
        worktree_dir,
        result.canonical_no_change,
    )
    _cleanup_dispatch_dir(worktree_dir, sandboxed=run_sandboxed_path, branch=ctx.branch)
    _notify_dispatch_success(ctx, result)
    return DispatchOutcome(
        exit_code=0,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _collect_acceptance_dispute(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome | None:
    """Hold a node whose implementer disputes its acceptance contract (#2731).

    The implementer never edits the contract; its stop reason carries the
    dispute instead. The node goes to Needs-you as ``acceptance-disputed``.
    That is not the work's failure, so nothing is published as failed, and the
    node never reached review, so no review round is spent. None when the
    reason carries no dispute or the node has no contract to dispute.
    """
    dispute = hold_disputed_node(
        ctx.store,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
        reason=result.subtype or "",
        run_id=result.run_id or ctx.run_id or "",
        repo=ctx.config.project.repo or None,
    )
    if dispute is None:
        return None
    _warn(
        f"{item_ref(ctx.item_id)} disputes acceptance test {dispute.test}: held as"
        f" {DISPUTE_MARKER} for a person"
        f" (`kew epic acceptance amend`). Worktree preserved: {worktree_dir}"
    )
    return DispatchOutcome(
        exit_code=result.exit_code or 1,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _collect_needs_human(
    ctx: _DispatchContext, result: DispatchResult, worktree_dir: Path
) -> DispatchOutcome:
    """Persist and publish an agent request for human intervention."""
    category = "needs_human"
    reason = redact_credentials(result.subtype or "unspecified")[:500]
    _error(f"Agent requires human input: {reason}")
    _publish_terminal_failure(ctx, category, reason)
    _warn(f"Worktree preserved for debugging: {worktree_dir}")
    _print_failure_next_steps(
        issue_num=ctx.issue_num,
        error_category=category,
        log_file=result.log_file,
        epic_ref=ctx.epic_ref,
        item_id=ctx.item_id,
    )
    _notify_dispatch_failure(ctx, result, result.exit_code or 1)
    return DispatchOutcome(
        exit_code=result.exit_code or 1,
        changed_files=[],
        run_id=result.run_id or None,
        launched=getattr(result, "launched", False) is not False,
    )


def _validate_prepared_run_identity(
    *,
    store: RunStore | None,
    epic_ref: str | None,
    item_id: str,
    worktree_dir: Path | None,
    branch: str,
    branch_override: str | None,
    run_id: str | None,
    run_kind: str = "dispatch",
) -> None:
    """Validate either a continuation tuple or a supplied initial run ID."""
    continuation_input = worktree_dir is not None
    if branch_override is not None:
        continuation_input = True
    if continuation_input:
        _validate_continuation_dispatch_identity(
            store=store,
            epic_ref=epic_ref,
            item_id=item_id,
            worktree_dir=worktree_dir,
            branch=branch_override,
            run_id=run_id,
            run_kind=run_kind,
        )
        return
    if run_id is not None and store is not None and store.get(run_id) is not None:
        raise ValueError(f"initial run id {run_id!r} already has a persisted run row")


def _prepare_dispatch_context(
    *,
    issue: GhIssue,
    config: DispatchConfig,
    store: RunStore | None,
    notifier: Notifier | None,
    force_budget: bool,
    approved: bool,
    interactive: bool,
    model_override: str | None,
    merge_context: str | None,
    epic_ref: str | None,
    item_id: str | None,
    provider: str,
    on_refuse: Callable[[str], None] | None,
    on_terminal_failure: Callable[[str, str], None] | None,
    run_kind: str,
    worktree_dir: Path | None,
    run_id: str | None,
    branch_override: str | None,
    allow_unrecorded: bool = False,
    fleet_worker: bool = False,
) -> _DispatchContext | DispatchOutcome:
    """Validate the issue payload and build the per-dispatch ``_DispatchContext``.

    Returns a ``DispatchOutcome`` (exit 1) for malformed issue data.
    Invalid item identities invoke the refusal callback before raising.
    The callback records retained claims and preserves the original error.
    """
    raw_num = issue.get("number")
    raw_title = issue.get("title")
    if not isinstance(raw_num, int) or not isinstance(raw_title, str):
        _error(f"Malformed issue data: {issue!r}")
        return DispatchOutcome(
            launched=False,
            exit_code=1,
            changed_files=[],
            category="malformed_issue",
            reason="issue number or title has an invalid type",
        )

    issue_num: int = raw_num
    issue_title: str = raw_title
    issue_body: str = issue.get("body") or ""
    issue_labels = issue.get("labels", [])
    complexity = get_complexity(issue_labels)

    # Durable work-item currency (A5a/L3) threaded through worktree/prompt/
    # tracking/PR below. The classic GitHub host path derives it from the
    # numeric issue; the Linear driver (`_dispatch-node`) passes the opaque
    # tracker id explicitly, so branch, worktree, RunRecord.item_id, and the PR
    # body all carry "SFD-133" while ``issue_num`` stays the ``0`` sentinel
    # (``runs.issue_num`` is NOT NULL and 0 is never a real GitHub number).
    # Validate once at this boundary — reject, never coerce.
    #
    # Per-item budget caps key on the durable ``item_id`` (A3), threaded into
    # ``check_budget`` below: a Linear item rides ``issue_num=0`` but each keeps
    # its own per-item cap via ``list_by_item`` instead of collapsing onto the
    # ``0`` sentinel's shared bucket (#809). Daily/weekly caps stay issue-wide.
    refuse_rollback = RefusalDelivery(on_refuse)

    try:
        validated_item_id = validate_item_id(
            item_id if item_id is not None else str(issue_num), provider
        )
    except InvalidItemIdError:
        refuse_rollback("invalid item id")
        raise

    # Branch name — slugged from the item id so Linear ids (L3) stay path-safe;
    # GitHub numbers slug to themselves, keeping the branch byte-identical.
    branch = branch_override or (
        f"{config.branches.prefix}{branch_slug(validated_item_id)}"
    )
    _validate_prepared_run_identity(
        store=store,
        epic_ref=epic_ref,
        item_id=validated_item_id,
        worktree_dir=worktree_dir,
        branch=branch,
        branch_override=branch_override,
        run_id=run_id,
        run_kind=run_kind,
    )

    return _DispatchContext(
        config=config,
        store=store,
        allow_unrecorded=allow_unrecorded,
        notifier=notifier,
        force_budget=force_budget,
        approved=approved,
        interactive=interactive,
        model_override=model_override,
        merge_context=merge_context,
        epic_ref=epic_ref,
        provider=provider,
        issue_num=issue_num,
        issue_title=issue_title,
        issue_body=issue_body,
        complexity=complexity,
        item_id=validated_item_id,
        branch=branch,
        start_monotonic=time.monotonic(),
        refuse_rollback=refuse_rollback,
        terminal_failure=on_terminal_failure,
        run_kind=run_kind,
        fleet_worker=fleet_worker,
        worktree_dir=worktree_dir,
        run_id=run_id,
        root_checkout_before=_capture_root_checkout_state(
            project_root(), base_ref=config.branches.base or "main"
        ),
    )


def execute_dispatch(
    *,
    issue: GhIssue,
    config: DispatchConfig,
    store: RunStore | None = None,
    notifier: Notifier | None = None,
    force_budget: bool = False,
    approved: bool = False,
    interactive: bool = False,
    model_override: str | None = None,
    merge_context: str | None = None,
    epic_ref: str | None = None,
    item_id: str | None = None,
    allow_unrecorded: bool = False,
    fleet_worker: bool = False,
    provider: str = "github",
    worktree_dir: Path | None = None,
    branch: str | None = None,
    run_id: str | None = None,
    on_refuse: Callable[[str], None] | None = None,
    on_admitted: Callable[[str], None] | None = None,
    on_terminal_failure: Callable[[str, str], None] | None = None,
    run_kind: str = "dispatch",
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> DispatchOutcome:
    """Core dispatch function. Returns DispatchOutcome(exit_code, changed_files).

    exit_code: 0=success, 1=failure, 2=reservation replaced, 3=API unreachable.

    Handles: worktree setup, prompt building, budget pre-check,
    retry loop with resume escalation, cost accumulation across
    retries, run-store recording, label transitions, notification,
    and worktree cleanup.

    ``on_admitted`` claims the tracker item after the pending row and reservation
    binding commit. ``on_refuse(reason)`` finishes before terminal refusal evidence.
    The finalizer handles unlaunched claims even when reservation ownership changes.
    The caller records retained claims and ignores unattempted claims.
    Once the paid child spawns, the run retains the claim.

    ``fleet_worker`` transfers cap warnings to the scheduler parent. Workers
    still restore over-cap claims before returning their outcome.

    The body is a straight-line pipeline of extracted phases: admission gates
    (``_run_admission_gates``) → approval gate (``_run_approval_gate``) →
    connectivity gate (``_check_connectivity_gate``) → runner launch
    (``_launch_runner``) → result collection (``_collect_result``). Any gate may
    short-circuit with a ``DispatchOutcome``; otherwise the launched result is
    collected into the final outcome.
    """
    prepared = _prepare_dispatch_context(
        issue=issue,
        config=config,
        store=store,
        allow_unrecorded=allow_unrecorded,
        fleet_worker=fleet_worker,
        notifier=notifier,
        force_budget=force_budget,
        approved=approved,
        interactive=interactive,
        model_override=model_override,
        merge_context=merge_context,
        epic_ref=epic_ref,
        item_id=item_id,
        provider=provider,
        on_refuse=on_refuse,
        on_terminal_failure=on_terminal_failure,
        run_kind=run_kind,
        worktree_dir=worktree_dir,
        run_id=run_id,
        branch_override=branch,
    )
    if isinstance(prepared, DispatchOutcome):
        return prepared
    ctx = prepared
    ctx.on_admitted = on_admitted
    ctx.expected_reservation_run_id = expected_reservation_run_id
    ctx.expected_reservation_generation = expected_reservation_generation
    # The checkout lease ends with the run on every exit, preserved checkout
    # or not (review round 12); the bytes stay, the next run claims afresh.
    try:
        try:
            _check_dispatch_reservation(ctx)
            return _run_dispatch_pipeline(ctx)
        except DispatchReservationReplaced:
            return _refuse_replaced_reservation(ctx)
        finally:
            # An unrecovered claim keeps the checkout lease with its pending row.
            _rollback_admitted_claim(ctx)
            finish_command_cleanup(release_checkout_claims, command="dispatch")
    except Exception as exc:
        _error(f"Dispatch for {item_ref(ctx.item_id)} failed: {exc}")
        exc.launch_state = ctx.launch_state  # type: ignore[attr-defined]
        raise


def _run_dispatch_pipeline(ctx: _DispatchContext) -> DispatchOutcome:
    early, admit_budget_check = _run_admission_gates(ctx)
    if early is not None:
        return early

    early = _run_approval_gate(ctx)
    if early is not None:
        return early

    early = _check_connectivity_gate(ctx)
    if early is not None:
        return early

    try:
        select_publication_mode(
            ctx.config.publication.publish,
            dangerously_skip_containment=(
                ctx.config.sandbox.dangerously_skip_containment
            ),
            store=ctx.store,
            run_id=ctx.run_id,
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            branch=ctx.branch,
            repo=ctx.config.project.repo or None,
        )
    except PublicationError as exc:
        return _refuse_publication_configuration(ctx, exc)

    launch = _launch_runner(ctx, admit_budget_check)
    if launch.result is not None:
        ctx.launch_state.observe(launch.result)
    if launch.early is not None:
        if launch.result is None:
            ctx.launch_state.launched = merge_launch_evidence(
                ctx.launch_state.launched, launch.early.launched
            )
        return launch.early

    # Non-None on the launch success path (``early is None``): the runner ran,
    # so ``result`` is set and ``worktree_dir`` was materialized to a real Path.
    return _collect_result(
        ctx,
        result=cast("DispatchResult", launch.result),
        worktree_dir=cast("Path", launch.worktree_dir),
        run_sandboxed_path=launch.run_sandboxed,
    )


def _refuse_publication_configuration(
    ctx: _DispatchContext, exc: PublicationError
) -> DispatchOutcome:
    """Finish a publication-policy refusal before any agent launches."""
    _finish_refusal(ctx, redact_credentials(exception_text(exc)))
    _record_prelaunch_refusal(ctx, PUBLICATION_CONFIGURATION_REFUSAL)
    detail = f"publication configuration refused: {exception_text(exc)}"
    _error(
        redact_credentials(
            f"Publication configuration refused before dispatch: {exception_text(exc)}"
        )
    )
    with contextlib.suppress(Exception):
        _publish_terminal_failure(ctx, PUBLICATION_CONFIGURATION_REFUSAL, detail)
    with contextlib.suppress(Exception):
        _emit_early_exit(
            ctx.store,
            issue_num=ctx.issue_num,
            item_id=ctx.item_id,
            complexity=ctx.complexity,
            runner="",
            model="",
            exit_code=1,
            duration_seconds=time.monotonic() - ctx.start_monotonic,
            error_category=PUBLICATION_CONFIGURATION_REFUSAL,
            repo=ctx.config.project.repo or None,
            run_id=ctx.run_id,
            epic_ref=ctx.epic_ref,
        )
    return DispatchOutcome(
        launched=False,
        exit_code=1,
        changed_files=[],
        category=PUBLICATION_CONFIGURATION_REFUSAL,
        reason=detail,
    )
