"""The tick orchestrator — `advance()` — for the Epic DAG Phase 2a driver.

`advance()` composes every earlier primitive (lock, overlay, merge pass,
frontier, dispatch) into one idempotent tick, and reports a tri-state
`TerminalSignal` so the poll wrapper (`kew epic run`) knows when to stop.

`advance()` itself is a thin orchestrator: all the real logic lives in the
``_...`` helpers below, each kept small enough to stay at xenon rank B.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from collections.abc import (
    Set as AbstractSet,
)
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from kew import credential_issuer, subprocess_utils, work_dirs
from kew.audit import Actor
from kew.audit import emit as _emit_audit
from kew.audit import emit_continuation_evidence as _emit_continuation_evidence
from kew.config import project_root
from kew.continuation_recovery import (
    CLEAN_HOST_DEATH_CLOSE_ACTOR,
    CheckoutAdmissionRefusal,
    ContinuationHistory,
    continuation_histories,
    preserved_host_death_reason,
    recorded_continuation_generation,
    recorded_run_checkout,
    recorded_run_checkout_generation,
    recorded_run_checkouts,
)
from kew.continuation_recovery import current_continuation as _current_continuation
from kew.continuation_recovery import (
    require_continuation_generation as _require_recorded_continuation_generation,
)
from kew.drive import DRIVE_RUNNING, DriveStateError, drive_hold, read_drive
from kew.drive_host_load import DRIVE_HOST_LOAD_KEY, host_load_hold
from kew.epic_holds import (
    ADMISSION_REFUSED_HOLD_PREFIX,
    CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
    DISPATCH_REFUSAL_HOLD_PREFIXES,
    PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
    admission_refused_head,
    admission_refused_reason_prefix,
    is_ci_pending_hold,
    is_dispatch_refusal_hold,
)
from kew.exit_signal import RESOURCE_CATEGORY
from kew.fleet_placement import is_remote
from kew.graph.build import build_graph
from kew.graph.domain import Graph, Node, NodeStatus
from kew.graph.driver import journal
from kew.graph.driver.base_integration import BaseIntegrator
from kew.graph.driver.ci_recovery import (
    CiRecoveryOutcome,
    ClassifyFn,
    DispatchFixFn,
    ci_recovery_pass,
    default_classify,
    default_dispatch_fix,
)
from kew.graph.driver.credentials import driver_key_cleanup, reconcile_driver_keys
from kew.graph.driver.dispatch import (
    REVIEW_SATURATED,
    AdmissionGuard,
    ChildSpawnedError,
    ContinuationRegistrationResult,
    DispatchResult,
    DispatchSink,
    can_continue_claimed,
    continue_claimed_child,
    reap_dispatch_children,
    reserve_and_dispatch,
    resolve_kew_console_script,
)
from kew.graph.driver.excluded_conflict import (
    excluded_by_rule,
    hold_excluded_conflicts,
)
from kew.graph.driver.facts import (
    GitHubFactsReader,
    NoChangeFacts,
    NodeFacts,
    ReviewPressure,
    prime_issue_snapshots_from,
    read_no_change_facts,
    read_review_pressure,
    resolve_linked_pr,
)
from kew.graph.driver.frontier import (
    FrontierRank,
    apply_review_backpressure,
    compute_frontier,
    frontier_rank,
    priority_of,
)
from kew.graph.driver.lock import (
    refresh_lock_heartbeat,
    single_driver_lock,
    stalled_holder,
)
from kew.graph.driver.merge import (
    MERGED_OUT_OF_BAND_REASON,
    REVIEW_BLOCK_REASONS,
    MergeVerdict,
    is_human_ci_gate,
    is_self_healing_hold,
    merge_pass,
)
from kew.graph.driver.merge_queue import (
    MERGE_VERIFY_BASE_MOVED,
    MERGE_VERIFY_FAILED,
    MERGE_VERIFY_HEAD_MOVED,
    MergeVerifier,
    is_queue_contention,
    merge_queue_verifier,
)
from kew.graph.driver.overlay import (
    DriverStatus,
    ReviewOverlay,
    current_attempt_runs,
    has_successful_open_pr,
    overlay_status,
    starts_fresh_work,
)
from kew.graph.driver.resource_retry import retry_resource_failures
from kew.graph.driver.result import (
    ADMISSION_HELD_PREFIX,
    PrLink,
    StuckNode,
    TerminalSignal,
    TickResult,
    TickSummary,
)
from kew.graph.driver.review_detach import ReviewDetacher, reap_review_children
from kew.graph.driver.review_stage import (
    Remediator,
    ReviewStageOutcome,
    drive_review_stage,
    recorded_gate_skips,
)
from kew.graph.port import DependencySource
from kew.graph.validate import ValidationReport, validate
from kew.human_review import ReviewPolicy
from kew.ids import (
    InvalidItemIdError,
    branch_slug,
    ensure_closes_marker,
    item_id_sort_key,
    item_ref,
    provider_from_config,
    validate_item_id,
)
from kew.no_change import EFFECT_INTENT, NoChangeError
from kew.policy import repo_root as _git_repo_root
from kew.pseudo_epics import (
    DELEGATION_EPIC_REF,
    PSEUDO_EPIC_REFS,
    REVIEW_EPIC_REF,
)
from kew.review_audit import VERDICT_MERGED_UNREVIEWED, emit_review_verdict
from kew.review_residuals import acceptance_on_record
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    CI_FIX_RUN_KIND,
    EPIC_TICK_OUTCOME_EMPTY_GRAPH,
    PR_SUBJECT_RUN_KINDS,
    RESOLVE_RUN_KIND,
    REVIEW_RUN_KINDS,
    REVIEW_STAGE_RUN_KIND,
    EpicHold,
    EpicRetry,
    ReviewRound,
    continuation_checkout_matches,
)
from kew.run_store_work import DISPATCH_RESERVATION_BOUND, TRACKER_WORK_RUN_KINDS
from kew.runner_quota import RunnerQuotaHold, active_runner_quota_hold
from kew.runners.registry import (
    active_runner_token_warnings,
    resolve_runner,
    runner_credential_fingerprint,
)
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.service.verbs import (
    ContinuationAlreadyClaimed,
    ContinuationFleetFull,
    ContinuationSelectionDenied,
    EpicResumeDenied,
    claim_continuation,
    select_awaiting_continuation,
)
from kew.stop_recovery import CONTAINER_START_FAILED
from kew.subprocess_utils import preflight_tools
from kew.verify_lease import StandingDeferral, standing_deferrals, verify_deferral
from kew.waits import (
    ARM_WAIT_REASON,
    CONTINUATION_AMBIGUOUS_REASON,
    HOST_RESTART_CONTINUE,
    HOST_RESTART_DIRTY_EVENT,
    HOST_RESTART_REAPED,
    HOST_RESTART_REASON,
    HOST_RESTART_REPEATED,
    HOST_RESTART_REPEATED_CONTINUATION_REASON,
    PROTECTED_PATH_MARK,
    PUBLICATION_REVIEW_REQUIRED,
)
from kew.worktree import (
    CheckoutBusy,
    claim_checkout,
    has_commits_ahead_of_base,
    has_uncommitted_changes,
    read_checkout_generation,
    release_checkout_claim,
    remote_branch_exists,
    remove_checkout_tree,
    require_checkout_generation,
    write_checkout_generation,
)

if TYPE_CHECKING:
    from kew.budget import BudgetExceeded
    from kew.config import DispatchConfig
    from kew.linear.tracker import LinearTracker
    from kew.openshell_launch import CheckoutMerge
    from kew.prompt_refusals import PromptRefusal
    from kew.review_dispatch import ReviewerLauncher
    from kew.run_store import (
        ContinuationRecord,
        EpicReservation,
        RunRecord,
        RunStore,
    )


_logger = logging.getLogger(__name__)
_IMPLEMENTATION_RUN_KINDS = TRACKER_WORK_RUN_KINDS - PR_SUBJECT_RUN_KINDS


class EmptyEpicGraphError(RuntimeError):
    """The epic's tracker query resolved to ZERO nodes — there is no work to
    drive, so the tick must error instead of reporting `COMPLETE done=0`.

    A driver must never declare success for work that does not exist (a typo'd
    epic name or a wrong-tracker cwd both resolve empty). Raised by `advance()`
    BEFORE any tick state is journaled; the CLI catches it and prints the
    tracker-specific diagnostic (label vs Linear project), mirroring
    `cli._load_epic_graph`, then exits 2. `COMPLETE` stays reachable only for a
    non-empty graph whose nodes are all DONE.
    """

    def __init__(self, name: str) -> None:
        message = (
            f"issue #{name} has no sub-issues"
            if name.isdigit()
            else f"epic {name!r}: tracker query resolved zero nodes"
        )
        super().__init__(message)
        self.name = name


# DriverStatus values that block a node from ever being (re)dispatched.
_NON_FRONTIER_STATUSES = frozenset(
    {
        DriverStatus.FAILED,
        DriverStatus.CANCELLED,
        DriverStatus.BLOCKED,
        DriverStatus.QUEUED,
        DriverStatus.IN_REVIEW,
        DriverStatus.AWAITING_PUBLICATION,
        DriverStatus.AWAITING_CONTINUATION,
        DriverStatus.HELD,
        # Governed-review lifecycle (#977): a node under review or remediation
        # has an open PR and a live review run — it must never re-enter the
        # dispatch frontier (a second implementation run would race the code the
        # reviewer is examining).
        DriverStatus.ADVERSARIAL_REVIEW,
        DriverStatus.REMEDIATING,
        # A recorded zero-diff outcome (I10B) that has not settled. Nothing was
        # published and nothing was written to the provider, but redispatching
        # would re-run work the host already judged to be absent — and would do
        # it while a person is being asked what that absence meant.
        DriverStatus.AWAITING_NO_CHANGE,
    }
)

# DriverStatus -> the frontier-safe NodeStatus sentinel (NON-NEGOTIABLE #3):
# anything that is not a raw provider DONE/TODO reads as BLOCKED so it can
# never satisfy a dependent (in particular: CANCELLED must not read as DONE).
_FRONTIER_STATUS_MAP: dict[DriverStatus, NodeStatus] = {
    DriverStatus.DONE: NodeStatus.DONE,
    DriverStatus.TODO: NodeStatus.TODO,
    DriverStatus.QUEUED: NodeStatus.QUEUED,
}


def _empty_tick_result(
    *, signal: TerminalSignal, remaining: int = 0, locked_out: bool = False
) -> TickResult:
    return TickResult(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=remaining,
        signal=signal,
        locked_out=locked_out,
    )


#: What every node's reason reads as while the graph itself is malformed. The
#: tick that writes it computed no overlay status and no per-node reason
#: (``_journal_invalid`` passes neither), so this prefix is also how a LATER
#: reader tells that snapshot apart from a real one — see
#: ``attention_journal.read_journaled_observations``, which refuses to compose
#: attention events off it.
INVALID_GRAPH_REASON_PREFIX = "invalid-graph:"


def _invalid_graph_reason(graph: Graph, report: ValidationReport) -> str:
    parts: list[str] = []
    if report.cycles:
        parts.append(f"cycles={report.cycles}")
    if report.dangling:
        parts.append(f"dangling={sorted(report.dangling)}")
    if report.self_edges:
        parts.append(f"self-edges={sorted(report.self_edges)}")
    return INVALID_GRAPH_REASON_PREFIX + ";".join(parts)


def _invalid_graph_result(graph: Graph, report: ValidationReport) -> TickResult:
    """All nodes are unactionable while the graph itself is malformed."""
    reason = _invalid_graph_reason(graph, report)
    stuck = tuple(
        StuckNode(node_id=nid, reason=reason, blast_radius=())
        for nid in sorted(graph.nodes)
    )
    return TickResult(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=stuck,
        done=(),
        remaining=len(graph.nodes),
        signal=TerminalSignal.STALLED,
    )


def _newest_run(runs: list[RunRecord]) -> RunRecord | None:
    if not runs:
        return None
    return max(runs, key=lambda r: (r.started_at, r.finished_at or r.started_at))


def _run_belongs_to_reservation(run: RunRecord, reserved_at: datetime) -> bool:
    """Return whether an item fallback can decide this reservation generation."""
    # Refusals need explicit ownership, regardless of their category.
    # Standalone PR flows and gone-item decisions do not acquire reservations.
    # Their timestamps therefore cannot establish ownership of another run.
    if (
        run.status == "refused"
        or run.run_kind in {CI_FIX_RUN_KIND, RESOLVE_RUN_KIND, "review"}
        or run.error_category in {"delegation_item_gone", "reservation_replaced"}
    ):
        return False
    return run.started_at >= reserved_at or (
        run.finished_at is not None and run.finished_at >= reserved_at
    )


def resolve_reservation_run(
    store: RunStore, reservation: EpicReservation
) -> RunRecord | None:
    """Resolve the run row that decides a reservation's fate (Task 6).

    Primary: a child pinned to the parent reservation at admission.
    Next: an unbound run keyed exactly to the reservation's ``run_id``.
    Then: an unpinned child bound through its admission audit event.
    This includes refused rows. A binding to another generation blocks fallback.
    Fallback: the newest eligible unbound ``item_id`` run belonging to THIS
    reservation generation. Unbound refusals cannot establish ownership.
    Standalone review, resolve, CI-fix, and gone-item decisions cannot use this
    fallback because they do not acquire the item's reservation.
    A terminal row from a PRIOR delegation can never release the
    live reservation. A row belongs to this generation when it either

    - started at or after the reservation (the normal case: the child is
      spawned after the reservation is written — this alone still gates the
      dead-child reclaim, so a stale pending row from a prior crashed run does
      not mask a genuinely-dead child), or
    - is a TERMINAL row that finished at or after the reservation. A prior
      delegation is always released before this item is re-reserved, so its
      run finished strictly before ``reserved_at``; only the current
      generation's run can finish at/after it. Keying the terminal case on
      ``finished_at`` admits a current-generation row that started a hair
    before ``reserved_at`` was stamped (the two clocks are independent:
    ``reserved_at`` is stamped in-process at reserve time, ``started_at`` by
    the child) instead of silently dropping it (#972 review Finding 1).

    A CI-fix run never belongs to a governed reservation generation.

    Shared by the driver-tick reconciler and ``kew doctor`` so the two never
    disagree about whether a reservation is stale (#972 review Finding 6).
    """
    exact = _unbound_reservation_run(store, reservation)
    pinned: list[RunRecord] = []
    owned: list[RunRecord] = []
    legacy: list[RunRecord] = []
    for candidate in store.list_by_item(reservation.item_id):
        bindings = store.list_audit_events(
            event=DISPATCH_RESERVATION_BOUND, run_id=candidate.run_id, limit=None
        )
        if bindings:
            rank = _reservation_binding_rank(candidate, reservation, bindings)
            if rank:
                (pinned if rank == 2 else owned).append(candidate)
        elif _run_belongs_to_reservation(candidate, reservation.reserved_at):
            legacy.append(candidate)
    return _newest_run(pinned) or exact or _newest_run(owned) or _newest_run(legacy)


def _unbound_reservation_run(
    store: RunStore, reservation: EpicReservation
) -> RunRecord | None:
    """Retain exact-ID legacy ownership only when no binding contradicts it."""
    run = store.get(reservation.run_id)
    if run is None or run.run_kind == CI_FIX_RUN_KIND:
        return None
    if run.error_category == "reservation_replaced" or store.list_audit_events(
        event=DISPATCH_RESERVATION_BOUND, run_id=run.run_id, limit=None
    ):
        return None
    return run


def _reservation_binding_rank(
    candidate: RunRecord, reservation: EpicReservation, bindings: list[Any]
) -> int:
    """Prefer parent-pinned children; foreign or conflicting bindings own nothing."""
    if candidate.run_kind == CI_FIX_RUN_KIND:
        return 0
    expected = {
        "reservation_run_id": reservation.run_id,
        "epic_ref": reservation.epic_ref,
        "item_id": reservation.item_id,
    }
    if not all(
        all((event.payload or {}).get(key) == value for key, value in expected.items())
        for event in bindings
    ):
        return 0
    return (
        2
        if any((event.payload or {}).get("pinned") is True for event in bindings)
        else 1
    )


# A dispatch child writes its "pending" run row within seconds of being
# spawned, well BEFORE the agent itself starts running (see
# `run_tracked_agent`). A reservation with NO run row at all after this
# generous grace is therefore not a slow-starting child — it is a dead
# spawn (the detached child crashed before writing that row: a bad import,
# a `load_config` failure, or being killed outright; its stdio is DEVNULL,
# so this failure is otherwise silent) and must be reclaimed, or it would
# pin the node IN_PROGRESS forever and `kew epic run` would poll forever.
_RESERVATION_STARTUP_GRACE_SECONDS = 600

# The pseudo-epics whose reservations the driver reconcile sweeps. Both come
# from the shared registry (kew.pseudo_epics, #979-F1) so the same list drives
# the driver reconcile and `kew doctor`; ``_DELEGATION_EPIC_REF`` keeps its
# in-module alias for the many existing references below.
#
# Parking review work under a distinct ref — never the node's own epic — keeps a
# live reviewer OUT of the epic's ``reserved_item_ids`` set, so overlay
# precedence 2 (reservation → IN_PROGRESS, which is kind-blind) cannot corrupt
# the node's status while a reviewer holds a slot. The tick sweeps it exactly as
# it sweeps delegation, so a reviewer that dies before releasing is reclaimed
# rather than pinned forever.
_DELEGATION_EPIC_REF = DELEGATION_EPIC_REF


def _record_dispatch_child_died(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    *,
    now: datetime,
) -> None:
    """Persist a terminal ``dispatch_child_died`` RunRecord for a dead spawn,
    naming the executable the driver would have spawned (#808).

    A dispatch child that never wrote its own ``pending`` run row within the
    startup grace crashed before startup — with DEVNULL stdio it fails
    silently. Rather than let the node re-admit and (re)spawn the same broken
    child forever, this writes a terminal failure keyed to the reservation's
    ``run_id`` so the overlay reconciles the node to FAILED with a diagnostic
    category — mirroring ``cli._record_dispatch_child_refusal`` for a child
    that started and refused. The resolved ``kew`` console script is named in
    the record title (and a warning log) so the operator can see WHICH kew was
    spawned: a wrong/stale executable off PATH is the usual cause this fix
    (venv-first resolution) targets.
    """
    from kew.run_store import RunRecord

    exe = resolve_kew_console_script() or "kew (unresolved)"
    _logger.warning(
        "epic %s: dispatch child for %s wrote no run row within %ds — spawned "
        "executable %s appears to have died before startup",
        reservation.epic_ref,
        reservation.item_id,
        _RESERVATION_STARTUP_GRACE_SECONDS,
        exe,
    )
    store.save(
        RunRecord(
            run_id=reservation.run_id,
            issue_num=reservation.issue_num,
            item_id=reservation.item_id,
            branch=f"{config.branches.prefix}{branch_slug(reservation.item_id)}",
            status="failure",
            exit_code=-1,
            started_at=reservation.reserved_at,
            finished_at=now,
            error_category="dispatch_child_died",
            title=f"dispatch child never started (spawned {exe})",
            repo=config.project.repo or None,
        )
    )


# A liveness probe answers "is this pending run's process/container still
# alive?" with a THREE-state verdict: True (provably alive), False (provably
# dead), or None (undeterminable). The reconciler reaps ONLY on a positive
# ``False`` — never on ``None`` — so a run whose liveness cannot be proven is
# left alone (the concurrent-state invariant: prove dead, never infer it from
# state-shape, so a live/mid-startup run is never falsely reaped).
RunLivenessProbe = Callable[["RunRecord"], "bool | None"]


def _owner_alive(run: RunRecord) -> bool | None:
    """Only the recorded exclusive dispatch owner can establish liveness."""
    if _host_died_under(run):
        return False
    if not run.pgid or run.pgid <= 1 or run.owner_pid != run.pgid:
        return None
    from kew.process_groups import owner_alive

    return owner_alive(
        run.pgid,
        run.owner_pid,
        run.owner_start_time,
        run.owner_ppid,
        run.owner_command,
        run.owner_start_token,
    )


#: Why an OpenShell run is finalized with no exit code of its own. The gateway
#: restarts every sandbox it governs: the processes inside die and the files
#: survive, so the agent is gone while its checkout is still there to recover.
OPENSHELL_GATEWAY_RESTART = "openshell_gateway_restart"


def _container_alive(
    record: RunRecord, config: DispatchConfig, *, now: datetime | None = None
) -> bool | None:
    """Use the durable id and reject a renamed container as uncertain."""
    from kew import sandbox
    from kew.run_liveness import launch_runtime
    from kew.stop_recovery import (
        CONTAINER_INSPECT_FORMAT,
        container_target,
        inspected_container_state,
    )

    if launch_runtime(record, config) == OPENSHELL_RUNTIME:
        return _openshell_workload_alive(record, config, now=now)
    identity = container_target(record)
    if identity is None:
        return None
    runtime_name = launch_runtime(record, config)
    runtime = sandbox.detect_runtime_cached(
        config.sandbox.model_copy(update={"runtime": runtime_name})
    )
    if runtime is None:
        return None
    subprocess_utils.TOOL_PATHS.setdefault(runtime_name, runtime)
    try:
        result = subprocess_utils.run(
            [runtime_name, "inspect", "--format", CONTAINER_INSPECT_FORMAT, identity],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    state = inspected_container_state(result, identity, record.container_name or "")
    return {"running": True, "stopped": False, "absent": False}.get(state)


def _openshell_sandbox_state(record: RunRecord, config: DispatchConfig) -> str:
    """What the gateway says about this run's sandbox, by its recorded name."""
    from kew.run_liveness import container_state_probe
    from kew.stop_recovery import record_container_state

    return record_container_state(
        OPENSHELL_RUNTIME,
        record,
        probe=container_state_probe(OPENSHELL_RUNTIME, config),
    )


def _openshell_exec_ended_without_exit(record: RunRecord) -> bool:
    """Did the exec stream end without ever producing an exit code?

    The workload on this runtime is one ``openshell sandbox exec`` whose client
    process kew owns, so an empty registered client group means the stream is
    over. ``pending`` is how "no exit code" is spelled in the ledger — the
    column is NOT NULL and carries a ``-1`` placeholder until an exit is
    observed, at which point the row is no longer pending.

    A run with no registered client group at all has not reached its exec yet
    and answers false, so a launch window is never read as an ended stream.
    """
    from kew.stop_recovery import contained_client_is_dead

    return record.status == "pending" and contained_client_is_dead(record)


def _openshell_gateway_restarted(record: RunRecord, config: DispatchConfig) -> bool:
    """Is this run's sandbox present while its exec stream is over?

    That pairing is the gateway-restart signature. A restart re-creates every
    sandbox's processes, so the agent kew exec'd is gone while the sandbox —
    and the checkout inside it — is still there. The sandbox being present is
    what separates it from an ordinary teardown, and the exec stream being over
    with no exit code is what separates it from a live run.

    The runtime the run LAUNCHED under is asked first, and the gateway last: a
    docker run has no sandbox to ask about, and even an OpenShell one is not
    worth a gateway call until its exec stream is known to be over.

    The local client being gone is NOT the agent being gone (#2625): the
    client can die on this host while the remote exec runs on, and recovering
    then would download a live checkout and finalize a running run. So a
    sandbox that is still up is asked about its agent too, and only an agent
    proven ended reads as this death.
    """
    from kew.run_liveness import launch_runtime

    if launch_runtime(record, config) != OPENSHELL_RUNTIME:
        return False
    if not _openshell_exec_ended_without_exit(record):
        return False
    state = _openshell_sandbox_state(record, config)
    if state == "stopped":
        return True
    return state == "running" and _openshell_remote_agent(record, config) == "ended"


def _openshell_remote_agent(record: RunRecord, config: DispatchConfig) -> str:
    """Ask the sandbox itself whether the agent kew exec'd is still running.

    ``running`` / ``ended`` / ``unknown``, from
    :func:`kew.openshell_launch.agent_exec_state`. A client that cannot even
    be built reads ``unknown``, which proves nothing either way.
    """
    from kew.openshell import OpenShellClient, OpenShellError
    from kew.openshell_launch import agent_exec_state

    if not record.container_name:
        return "unknown"
    try:
        client = OpenShellClient.from_settings(config.sandbox.openshell)
    except (OpenShellError, OSError, ValueError) as exc:
        _logger.warning(
            "openshell sandbox %s cannot be asked about its agent: %s",
            record.container_name,
            exc,
        )
        return "unknown"
    return agent_exec_state(client, record.container_name)


def _openshell_exec_may_start(record: RunRecord, now: datetime | None) -> bool:
    """Whether a sandbox whose exec stream is not over may still run an agent.

    A registered exec may: its client is still there. A sandbox that never got
    one — the leader died between the create and the exec (#3208) — may only
    within the startup grace; after it the sandbox is an empty shell, and
    reading it alive would pin its reservation until the reaper deleted it.
    """
    from kew.process_groups import attempt_groups

    if attempt_groups(record.attempt_pgid, record.log_file):
        return True
    started = record.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    age = (now or datetime.now(UTC)) - started
    return age <= timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)


def _openshell_workload_alive(
    record: RunRecord, config: DispatchConfig, *, now: datetime | None = None
) -> bool | None:
    """Liveness on the gateway runtime: the exec, not the sandbox.

    A sandbox outlives the exec that ran in it, so presence alone is not
    liveness here — reading it as such is what pinned a reservation forever
    after a gateway restart. Presence plus an ended exec stream is death;
    presence with an exec that may still be running is alive; anything the
    gateway could not state stays undeterminable, so nothing is reaped on a
    failed probe. An ended exec STREAM is only kew's local client: the agent's
    own state is asked of the sandbox before the run reads dead.
    """
    state = _openshell_sandbox_state(record, config)
    if state in {"absent", "stopped"}:
        return False
    if state != "running":
        return None
    if not _openshell_exec_ended_without_exit(record):
        return _openshell_exec_may_start(record, now)
    # The local client is gone; whether the agent is, only the sandbox can
    # say (#2625). An agent that may still run keeps the run alive.
    return {"running": True, "ended": False}.get(
        _openshell_remote_agent(record, config)
    )


def default_run_liveness(config: DispatchConfig) -> RunLivenessProbe:
    """Probe the owning dispatch group across retries and contained startup.

    Attempt groups are termination targets and never establish run liveness.
    """
    return partial(_run_owner_liveness, config)


def _run_owner_liveness(config: DispatchConfig, run: RunRecord) -> bool | None:
    if is_remote(run):
        # A run placed on another machine is UNPROBEABLE from here, never dead
        # (#2398). Both probes below read this machine: the pgid is this
        # kernel's process table and the container is this daemon's. A VM's
        # agent is absent from both, and "absent" is exactly what they return as
        # *provably dead* — so an unguarded probe would retire every remote run
        # from the fleet count and admit a replacement beside it, which is how a
        # three-agent cap becomes six. ``None`` keeps the slot: prove dead,
        # never infer, and a remote death is proven by the placement's own
        # outcome (``RemoteRunInterrupted``), which writes the terminal row.
        return None
    if run.container_name:
        return _contained_run_alive(run, config)
    return _owner_alive(run)


def _contained_run_alive(run: RunRecord, config: DispatchConfig) -> bool | None:
    """Container death cannot release a live or unresolved dispatch owner."""
    owner = _owner_alive(run)
    if owner is True:
        return True
    container = _container_alive(run, config)
    if container is False and _owner_may_be_alive(run):
        return None
    return container


def _reap_orphaned_run(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    now: datetime,
    precommit_check: Callable[[], None] | None = None,
) -> bool:
    """Mark a pending run whose owning process/container is provably dead as
    ``killed`` and attest the reap (#1041).

    A dispatch the epic driver owned that dies WITH the driver (the driver is
    killed, or its sandbox container is torn down) leaves its ``pending`` run row
    pinned forever: no terminal row is ever written, so the terminal-run release
    below never fires and the reservation starves admissions silently. This
    writes the missing terminal row (surfacing the node as FAILED so an operator
    retries) and emits an attested ``orphaned_run_reaped`` audit event naming the
    epic and run, so the reap is never silent — before the reservation is
    released by the caller.

    ``run`` is a snapshot taken before the liveness probe: a dispatch child that
    terminated NORMALLY in that window has already written its own terminal row
    (its process group then disappears, so the pgid probe reads dead). This
    re-reads the row and reaps ONLY a still-``pending`` one — never clobbering a
    fresh terminal outcome with a bogus ``killed``/137 that would mis-report a
    finished node as FAILED (concurrent-state invariant: prove dead AND still
    pending before reaping). The caller releases the reservation either way,
    but continuation ownership may only be reset when this function actually
    reaps the pending row.
    """
    fresh = store.get(run.run_id)
    if fresh is None or fresh.status != "pending":
        return False
    run = fresh
    if not _host_died_under(run):
        never_started = _reap_never_started(
            store, config, reservation, run, now=now, precommit_check=precommit_check
        )
        if never_started is not None:
            return never_started
    return _reap_dead_owner(
        store, config, reservation, run, now=now, precommit_check=precommit_check
    )


def _reap_dead_owner(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    now: datetime,
    precommit_check: Callable[[], None] | None,
) -> bool:
    """Finalize a pending run whose owner is dead and attest the reap."""
    # Callers prove death before entering reconciliation. Recheck a recorded
    # host owner before finalization; an exited attempt never proves owner death.
    if _orphan_finalization_is_blocked(run, store, config):
        return False
    category, reason = _reap_category(store, reservation, run)
    finalized = store.finalize_orphaned_pending(
        run.run_id,
        error_category=category,
        finished_at=now,
        precommit_check=precommit_check,
        reservation=reservation,
        expected_owner=run,
        retry_reason=(HOST_RESTART_REASON if category == HOST_RESTART_REAPED else None),
        retry_repo=config.project.repo or None,
    )
    if precommit_check is not None:
        precommit_check()
    if not finalized:
        return False
    _logger.warning(
        "epic %s: reaping orphaned pending run %s for %s — its process/container "
        "is no longer live (driver killed mid-dispatch?)",
        reservation.epic_ref,
        run.run_id,
        reservation.item_id,
    )
    _mark_reaped(run, category, now)
    _attest_orphaned_run(store, config, reservation, run, reason=reason)
    if category == HOST_RESTART_REAPED:
        _readmit_after_host_restart(store, config, reservation)
    return True


def _mark_reaped(run: RunRecord, category: str, now: datetime) -> None:
    run.status = "killed"
    run.error_category = category
    run.exit_code = 137  # 128 + SIGKILL, matching a kill -9'd owner
    run.finished_at = now


def _reap_never_started(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    now: datetime,
    precommit_check: Callable[[], None] | None,
) -> bool | None:
    """Reap a dead run whose container never started (#3124); None if it did.

    The rule is :func:`kew.stop_recovery.never_started_verdict`: an owner
    proven dead past the startup grace, no attempt ever registered, and a
    container ``created`` with pid 0 (or none at all). Such a container
    refuses ``kill`` and is never reported dead, so the ordinary orphan proof
    would retain its slot for good. The removal is recorded before it is
    made, and the reap writes the same ``never_started_run_reaped`` event
    admission and ``kew settle`` write. False when the container was removed
    but the row could not be finalized; the removal is recorded as such.
    """
    from kew.run_store_liveness import reap_never_started

    def finalize(category: str) -> bool:
        finalized = store.finalize_orphaned_pending(
            run.run_id,
            error_category=category,
            finished_at=now,
            precommit_check=precommit_check,
            reservation=reservation,
            expected_owner=run,
        )
        if precommit_check is not None:
            precommit_check()
        return finalized

    reap = reap_never_started(
        store,
        run,
        now,
        finalize=finalize,
        actor=("policy", "driver_reconcile"),
        epic_ref=reservation.epic_ref,
        repo=config.project.repo or None,
    )
    if not reap.reaped:
        return False if reap.removed is not None else None
    _logger.warning(
        "epic %s: run %s for %s: its container %s never started and its owner "
        "is dead; container removed and run reaped",
        reservation.epic_ref,
        run.run_id,
        reservation.item_id,
        run.container_name,
    )
    _mark_reaped(run, reap.category or CONTAINER_START_FAILED, now)
    return True


def _attest_orphaned_run(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    reason: str,
) -> None:
    """Record one terminal orphan decision after its state is durable."""
    _emit_audit(
        store,
        "orphaned_run_reaped",
        actor=Actor("policy", "driver_reconcile"),
        run_id=run.run_id,
        issue_num=reservation.issue_num,
        item_id=reservation.item_id,
        repo=config.project.repo or None,
        payload={
            "epic_ref": reservation.epic_ref,
            "reason": reason,
            "reserved_at": reservation.reserved_at.isoformat(),
        },
    )


def _reap_category(
    store: RunStore, reservation: EpicReservation, run: RunRecord
) -> tuple[str, str]:
    """The reaped run's error category, and the reason its reap is attested with.

    A run the host died under is reaped for ``host_restart`` (#2812), any other
    for ``process_not_live``.
    """
    if not _host_died_under(run):
        return "orphaned_pending_reaped", "process_not_live"
    return _host_restart_category(store, reservation, run), HOST_RESTART_REASON


def _host_restart_category(
    store: RunStore, reservation: EpicReservation, run: RunRecord
) -> str:
    """How a run the host died under is finalized (#2812).

    The host dying is not the node failing, so an implementation run of a real
    epic is re-admitted once (:data:`HOST_RESTART_REAPED`). A node the host has
    already died under once is failed instead (:data:`HOST_RESTART_REPEATED`):
    a host that keeps going down under the same node is a person's to look at.
    A reviewer or pseudo-epic run keeps the ordinary orphan outcome. A
    preserved continuation shares the node's host-death count.
    """
    if run.run_kind != "dispatch" or reservation.epic_ref in PSEUDO_EPIC_REFS:
        return "orphaned_pending_reaped"
    earlier = _host_deaths_recorded(
        store, reservation.epic_ref, reservation.item_id, excluding_run=run.run_id
    )
    return HOST_RESTART_REPEATED if earlier else HOST_RESTART_REAPED


def _host_deaths_recorded(
    store: RunStore, epic_ref: str, item_id: str, *, excluding_run: str
) -> bool:
    """Has the host already died under this node once (#2812, #2964)?

    A clean death leaves its ``host_restart`` retry, a dirty one its
    :data:`HOST_RESTART_DIRTY_EVENT`; both spend the one host-death budget.
    *excluding_run*'s own dirty marker is not an earlier death: it is written
    before the preservation it budgets, so a driver that died part-way through
    finds it on the next tick and must reach the same outcome again.
    """
    retried = any(
        (event.payload or {}).get("reason") == HOST_RESTART_REASON
        for event in store.list_audit_events(
            limit=None, event="epic_node_retry", item_id=item_id, epic_ref=epic_ref
        )
    )
    return retried or any(
        event.run_id != excluding_run
        for event in store.list_audit_events(
            limit=None,
            event=HOST_RESTART_DIRTY_EVENT,
            item_id=item_id,
            epic_ref=epic_ref,
        )
    )


def _dirty_host_restart_outcome(
    store: RunStore, reservation: EpicReservation, run: RunRecord
) -> str | None:
    """What a host death under preserved dirty work does to the node (#2964).

    ``None`` when the host did not die under *run*, or the run is not one the
    host-death budget covers. Else :data:`HOST_RESTART_CONTINUE` the first
    time — the work is continued automatically once — and
    :data:`HOST_RESTART_REPEATED` the second, which leaves it for a person.
    Read before the preservation writes anything.
    """
    if not _host_died_under(run):
        return None
    category = _host_restart_category(store, reservation, run)
    if category == HOST_RESTART_REAPED:
        return HOST_RESTART_CONTINUE
    return HOST_RESTART_REPEATED if category == HOST_RESTART_REPEATED else None


def _record_dirty_host_restart(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    owned: Any | None,
) -> None:
    """Spend the node's host-death budget on preserved dirty work (#2964).

    Written strictly BEFORE the preservation's first write, keyed by the dead
    run: a driver that dies at any later boundary leaves the marker behind,
    so the death is never forgotten, and the next tick — which still finds the
    run's reservation, or its pending row — recomputes the same outcome
    (:func:`_host_deaths_recorded` skips the run's own marker) and appends
    nothing new. The marker arms only a continuation whose original run it
    names, so one written for work that is never preserved arms nothing.
    *owned* is the continuation the dead run was continuing, if any.
    """
    outcome = _dirty_host_restart_outcome(store, reservation, run)
    if outcome is None:
        return
    original_run_id = run.run_id if owned is None else owned.original_run_id
    _emit_audit(
        store,
        HOST_RESTART_DIRTY_EVENT,
        actor=Actor("policy", "driver_reconcile"),
        run_id=run.run_id,
        issue_num=reservation.issue_num,
        item_id=reservation.item_id,
        repo=config.project.repo or None,
        payload={
            "epic_ref": reservation.epic_ref,
            "node": reservation.item_id,
            "reason": HOST_RESTART_REASON,
            "outcome": outcome,
            "original_run_id": original_run_id,
        },
        dedup_key=(
            f"host-restart:{reservation.epic_ref}:{reservation.item_id}:{run.run_id}"
        ),
        strict=True,
    )


def _readmit_after_host_restart(
    _store: RunStore, _config: DispatchConfig, reservation: EpicReservation
) -> None:
    """Log a node re-admitted because its run died with its host (#2812).

    The reap transaction wrote both the retry marker and its audit event. This
    function performs no write that a driver crash could separate from them.
    """
    _logger.warning(
        "epic %s: %s re-admitted once — the host restarted under its run",
        reservation.epic_ref,
        reservation.item_id,
    )


def _owner_may_be_alive(run: RunRecord) -> bool:
    """Recheck the dispatch owner, including contained retry backoff."""
    return bool((run.pgid or run.owner_pid) and _owner_alive(run) is not False)


def _cleanup_blocked_by_owner(run: RunRecord) -> bool:
    return not run.cleanup_runtime and _owner_may_be_alive(run)


def _orphan_finalization_is_blocked(
    run: RunRecord, store: RunStore, config: DispatchConfig
) -> bool:
    """Return whether a run still owns work that cannot be finalized."""
    if _cleanup_blocked_by_owner(run):
        return True
    return not _kill_orphaned_attempt(run, store, config)


def _host_owner_and_attempts_dead(run: RunRecord) -> bool:
    """Use the shared host proof for death before the first attempt."""
    from kew.stop_recovery import pending_owner_is_dead

    return not run.container_name and pending_owner_is_dead(run, datetime.now(UTC))


def _kill_orphaned_attempt(
    run: RunRecord, store: RunStore, config: DispatchConfig
) -> bool:
    """Persist intent, stop the container by id, stop the client, then prove both."""
    # A dispatcher can die inside the claim hook before any attempt exists.
    # Main's complete host-death proof also validates empty spawn evidence.
    if _host_owner_and_attempts_dead(run):
        return True
    if run.container_name:
        runtime = run.cleanup_runtime or run.container_runtime or config.sandbox.runtime
        if not store.request_orphan_cleanup(run, runtime):
            return False
        config = config.model_copy(deep=True)
        config.sandbox.runtime = runtime
        if not _stop_orphaned_container(run, config):
            return False
    return _orphaned_processes_stopped(run, store, config)


def _orphaned_processes_stopped(
    run: RunRecord, store: RunStore, config: DispatchConfig
) -> bool:
    """Stop the orphan's attempt groups, then prove its client and container dead."""
    from kew.fleet import stop_orphaned_attempt
    from kew.stop_recovery import contained_client_is_dead

    if _host_died_under(run):
        # Every host process of an earlier boot is gone, and the pids recorded
        # for them may name strangers now: signal none of them (#2812).
        return not run.container_name or _container_alive(run, config) is False
    if not stop_orphaned_attempt(run, store):
        return False
    return not run.container_name or (
        (contained_client_is_dead(run) or _openshell_never_execd(run, config))
        and _container_alive(run, config) is False
    )


def _openshell_never_execd(run: RunRecord, config: DispatchConfig) -> bool:
    """An OpenShell run whose leader died before it registered an exec (#3208).

    There is no client to prove dead: the exec is the only process the run
    would start, and the sandbox it would start in is the one just deleted.
    """
    from kew.process_groups import attempt_groups
    from kew.run_liveness import launch_runtime

    return launch_runtime(run, config) == OPENSHELL_RUNTIME and not attempt_groups(
        run.attempt_pgid, run.log_file
    )


def _stop_orphaned_container(run: RunRecord, config: DispatchConfig) -> bool:
    """Inspect and signal only the durable id before touching the runtime client."""
    from kew.stop_recovery import container_target

    state = _container_alive(run, config)
    if state is not True:
        return state is False
    identity = container_target(run)
    if identity is None:
        return False
    try:
        subprocess_utils.run(
            [config.sandbox.runtime, "kill", identity], check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _logger.warning("Cannot stop orphaned container for %s: %s", run.run_id, exc)
        return False
    return _container_alive(run, config) is False


def _host_died_under(run: RunRecord) -> bool:
    """Did this machine reboot under the run's owner (#2812)?

    The rule is :func:`kew.run_liveness.host_died_under`, shared with
    ``kew epic continue`` so the command a host death offers accepts the
    same evidence that reaped the run (#2960).
    """
    from kew.run_liveness import host_died_under

    return host_died_under(run)


def _pending_run_is_dead(
    run: RunRecord, probe: RunLivenessProbe, *, now: datetime
) -> bool:
    """Whether a ``pending`` run should be reaped as an orphan (#1041).

    Fires ONLY on a positively-dead probe (``False`` — never ``None``, so a
    mid-startup or unprobeable run survives; concurrent-state invariant: prove
    dead, never infer from state-shape). The startup window is the trap: a host
    run carrying an owner ``pgid`` is dead the instant its process group
    is gone, but a sandboxed run's ``inspect`` reads "no such container" during
    the launch window too — before the container exists — so a contained
    run must additionally have aged past the startup grace before a
    not-found verdict is trusted, or a just-launched container would be reaped.
    """
    if run.cleanup_runtime or _host_died_under(run):
        return True
    if run.container_name and _owner_alive(run) is False:
        return now - run.started_at > timedelta(
            seconds=_RESERVATION_STARTUP_GRACE_SECONDS
        )
    if probe(run) is not False:
        return False
    if not run.container_name and run.pgid and run.pgid > 0:
        return True
    return now - run.started_at > timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)


def _recorded_worktree_path(
    reservation: EpicReservation,
    run: RunRecord,
    config: DispatchConfig,
) -> Path | None:
    """Resolve a run's configured path without inspecting agent-written bytes."""
    return _recorded_worktree_candidate(
        reservation, run, config, resolve_candidate=False
    )


def _recorded_worktree_candidate(
    reservation: EpicReservation,
    run: RunRecord,
    config: DispatchConfig,
    *,
    resolve_candidate: bool = True,
) -> Path | None:
    """Resolve the deterministic, configured path for a recorded run."""
    item_id = reservation.item_id
    try:
        item_id = validate_item_id(item_id, provider_from_config(config))
    except InvalidItemIdError:
        return None
    slug = branch_slug(item_id)
    expected_branch = f"{config.branches.prefix}{slug}"
    if run.branch != expected_branch:
        return None

    root = Path(config.branches.worktree_base).expanduser().resolve(strict=False)
    candidate = root / "agent" / f"issue-{slug}"
    if not candidate.is_relative_to(root):
        return None
    if not resolve_candidate:
        return candidate
    configured = candidate
    candidate = configured.resolve(strict=False)
    root = Path(config.branches.worktree_base).expanduser().resolve(strict=False)
    if candidate != configured or not candidate.is_relative_to(root):
        return None
    if not candidate.is_dir():
        return None
    return candidate


def _checkout_holds_work(
    config: DispatchConfig, worktree: Path, branch: str, *, base_sha: str = ""
) -> bool:
    """Does a recorded worktree hold work an operator could lose?

    Canonical recovery compares against the producer's recorded trusted base.
    It never runs Git inside the checkout an agent has written. Checkout
    publication counts a HEAD the operator repository cannot find on its
    freshly fetched base, or on *branch* as origin publishes it, as work too
    — never the checkout's own refs, which the agent can rewrite.
    """
    if config.publication.publish != "canonical":
        # A committed but unpublished change leaves a clean tree (#2971).
        return has_uncommitted_changes(worktree) or has_commits_ahead_of_base(
            worktree,
            config.branches.base or "main",
            operator_repo=_git_repo_root(project_root()),
            branch=branch,
        )
    from kew.dispatch_core import canonical_checkout_has_work

    return canonical_checkout_has_work(
        worktree, repo=config.project.repo or None, base_sha=base_sha
    )


_SMALL_FILE_LIMIT = 4096


def _read_small_regular(path: Path, limit: int = _SMALL_FILE_LIMIT) -> str | None:
    """Read a small regular file as text without following links or blocking.

    A FIFO, a device, a symlink or an oversized file reads as None: the
    recovery tick must never hang or grow on an agent-planted file (review
    round 10 of #1440).
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _logger.warning("could not open %s as a regular file: %s", path, exc)
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return os.read(descriptor, limit).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        _logger.warning("could not read %s: %s", path, exc)
        return None
    finally:
        os.close(descriptor)


def _recorded_head_branch(candidate: Path) -> str | None:
    """The branch the checkout's own HEAD file names, read as data.

    A plain ``.git`` directory or a ``gitdir:`` file are both followed as
    text; nothing executes and no git process resolves inside the checkout.
    """
    dot_git = candidate / ".git"
    try:
        kind = os.lstat(dot_git).st_mode
    except OSError as exc:
        _logger.warning("%s has no readable .git entry: %s", candidate, exc)
        return None
    if stat.S_ISREG(kind):
        pointer = _read_small_regular(dot_git)
        if pointer is None or not pointer.startswith("gitdir:"):
            return None
        git_dir = Path(pointer[len("gitdir:") :].strip())
        if not git_dir.is_absolute():
            git_dir = candidate / git_dir
    elif stat.S_ISDIR(kind):
        git_dir = dot_git
    else:
        return None  # a symlinked .git is indeterminate, not inspected
    head = _read_small_regular(git_dir / "HEAD")
    if head is None:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else None


def _git_worktree_matches(
    candidate: Path, expected_branch: str, *, config: DispatchConfig | None = None
) -> bool:
    """Verify identity before inspecting a candidate worktree.

    Under canonical publication the identity is the recorded reservation
    path alone; git is not asked, because the checkout is the agent's and
    the branch it reports is its own claim (review round 9 of #1440).
    """
    if config is not None and config.publication.publish == "canonical":
        return (
            candidate.is_dir() and _recorded_head_branch(candidate) == expected_branch
        )

    try:
        top_level = subprocess_utils.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
        )
        branch = subprocess_utils.run(
            ["git", "-C", str(candidate), "branch", "--show-current"],
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if top_level.returncode != 0 or branch.returncode != 0:
        return False
    try:
        actual_top_level = Path(top_level.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    return actual_top_level == candidate and branch.stdout.strip() == expected_branch


def _validated_recorded_worktree(
    reservation: EpicReservation,
    run: RunRecord,
    config: DispatchConfig,
) -> Path | None:
    """Return the only worktree this reservation is allowed to inspect.

    Dispatch worktrees have a deterministic path under the configured root, but
    the path is not trusted merely because the item id can form a filename.
    The recorded branch, worktree root, and Git worktree metadata must all
    agree before any status command runs. A missing, escaped, detached, or
    wrong-branch worktree is indeterminate and falls back to the existing dead
    run recovery path; it is never searched or cleaned up.
    """
    candidate = _recorded_worktree_candidate(reservation, run, config)
    if candidate is None or not _git_worktree_matches(
        candidate, run.branch, config=config
    ):
        return None
    return candidate


def _continuation_owned_by_run(
    store: RunStore, epic_ref: str, run_id: str
) -> Any | None:
    """Find the unique live continuation generation owned by a child run."""
    matches = [
        record
        for record in store.list_continuations(epic_ref)
        if record.state == "continuing" and record.continuation_run_id == run_id
    ]
    if len(matches) > 1:
        raise _PreexistingWorktreeRefusal(
            f"continuation child {run_id!r} has multiple managed owners in "
            f"epic {epic_ref!r}"
        )
    return matches[0] if matches else None


def _is_successful_continuation(run: RunRecord | None, owned: Any | None) -> bool:
    """Return whether current success still belongs to a continuation."""
    return run is not None and run.status == "success" and owned is not None


def _is_pending_run(run: RunRecord | None) -> bool:
    """A pending row retains its reservation when the reaper declines it."""
    return run is not None and run.status == "pending"


def _run_item_id(run: RunRecord) -> str:
    """Return a run's durable item id, including the legacy GitHub fallback."""
    return run.item_id or str(run.issue_num)


def _orphan_run_matches_reservation(
    store: RunStore,
    reservation: EpicReservation,
    supplied: RunRecord,
    actual: RunRecord,
) -> bool:
    """Return whether both child views belong to the reservation.

    Either proof will do: the run carries the reservation's own id, or a
    pinned ``dispatch_reservation_bound`` event names this reservation for it.
    A real dispatch child has an id of its own and only the binding (#2812).
    """
    if (
        supplied.run_id,
        _run_item_id(supplied),
        supplied.branch,
        _run_item_id(actual),
    ) != (
        actual.run_id,
        reservation.item_id,
        actual.branch,
        reservation.item_id,
    ):
        return False
    if actual.run_id == reservation.run_id:
        return True
    bindings = store.list_audit_events(
        event=DISPATCH_RESERVATION_BOUND, run_id=actual.run_id, limit=None
    )
    return _reservation_binding_rank(actual, reservation, bindings) == 2


def _owned_recovery_matches(
    reservation: EpicReservation,
    owned: Any,
    owned_worktree: Path,
    worktree: Path,
    expected_branch: str,
    expected_worktree: Path,
) -> bool:
    """Return whether a continuation owns the full reserved checkout tuple."""
    return (
        owned.epic_ref,
        owned.item_id,
        owned.continuation_run_id,
        owned.branch,
        owned_worktree,
        worktree,
    ) == (
        reservation.epic_ref,
        reservation.item_id,
        reservation.run_id,
        expected_branch,
        expected_worktree,
        expected_worktree,
    )


def _reservation_is_unique(
    store: RunStore,
    reservation: EpicReservation,
) -> bool:
    """Return whether the store still has exactly the supplied reservation."""
    matches = [
        current
        for current in store.list_epic_reservations(reservation.epic_ref)
        if current.run_id == reservation.run_id
    ]
    return matches == [reservation]


def _claimed_run_matches(
    store: RunStore,
    reservation: EpicReservation,
    run: RunRecord | None,
    expected_branch: str,
) -> bool:
    """Return whether an available child still carries its reserved identity."""
    if run is None:
        return True
    actual = store.get(run.run_id)
    return (
        actual is not None
        and run.branch == expected_branch
        and _orphan_run_matches_reservation(store, reservation, run, actual)
    )


def _owned_worktree(record: Any, item_id: str) -> Path:
    """Return a continuation's canonical path or raise a named refusal."""
    try:
        return Path(record.worktree_identity).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} continuation has an unreadable worktree identity"
        ) from exc


def _require_claimed_orphan_identity(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    owned: Any | None,
    worktree: Path,
    *,
    run: RunRecord | None,
) -> None:
    """Require one complete claimed recovery ownership tuple."""
    expected_worktree, expected_branch = _configured_checkout_target(
        config, reservation.item_id
    )
    if not _reservation_is_unique(store, reservation) or not _claimed_run_matches(
        store, reservation, run, expected_branch
    ):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} has conflicting reservation or "
            "child-run ownership"
        )
    if owned is None:
        return
    owned_worktree = _owned_worktree(owned, reservation.item_id)
    if not _owned_recovery_matches(
        reservation,
        owned,
        owned_worktree,
        worktree,
        expected_branch,
        expected_worktree,
    ):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} continuation does not match its "
            "reservation, child run, branch, and checkout"
        )


def _continuation_issue_num(item_id: str) -> int:
    """Return the GitHub number or the Linear ``0`` sentinel for an item id."""
    if not item_id.isascii() or not item_id.isdigit():
        return 0
    with suppress(ValueError):
        return int(item_id)
    return 0


def _emit_continuation_state(
    store: RunStore,
    config: DispatchConfig,
    record: Any,
    *,
    event: str,
    phase: str,
    outcome: str | None = None,
    run_id: str | None = None,
) -> None:
    """Write one deduplicated recovery state transition to the audit chain."""
    issue_num = _continuation_issue_num(record.item_id)
    _emit_continuation_evidence(
        store,
        event,
        actor=Actor("policy", "continuation_reconcile"),
        phase=phase,
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=record.continuation_run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation,
        state=record.state,
        outcome=outcome,
        run_id=run_id,
        issue_num=issue_num,
        repo=config.project.repo or None,
    )


def _attest_awaiting_continuation(
    store: RunStore, config: DispatchConfig, record: Any
) -> None:
    _emit_continuation_state(
        store,
        config,
        record,
        event="continuation_detected",
        phase="detection",
        outcome="awaiting_continuation",
        run_id=record.original_run_id,
    )


def _attest_continuing_continuation(
    store: RunStore, config: DispatchConfig, record: Any
) -> None:
    """Attest run progress; the claim transaction already records its event."""
    continuation_run_id = record.continuation_run_id
    if not continuation_run_id:
        return
    run = store.get(continuation_run_id)
    if run is None:
        return
    _emit_continuation_state(
        store,
        config,
        record,
        event="continuation_started",
        phase="start",
        outcome=run.status,
        run_id=continuation_run_id,
    )
    if run.status == "awaiting_publication":
        _emit_continuation_state(
            store,
            config,
            record,
            event="continuation_publication",
            phase="publication",
            outcome=run.error_category or run.status,
            run_id=continuation_run_id,
        )


def _attest_terminal_continuation(
    store: RunStore, config: DispatchConfig, record: Any
) -> None:
    continuation_run_id = record.continuation_run_id
    run = store.get(continuation_run_id) if continuation_run_id else None
    outcome = "success" if run is None or run.exit_code == 0 else "failure"
    if run is not None and run.error_category:
        outcome = run.error_category
    _emit_continuation_state(
        store,
        config,
        record,
        event="continuation_terminal",
        phase="terminal",
        outcome=outcome,
        run_id=continuation_run_id,
    )


def _attest_continuation_states(
    store: RunStore, config: DispatchConfig, epic_ref: str
) -> None:
    """Attest durable continuation states observed by this driver tick."""
    for record in store.list_continuations(epic_ref):
        if record.state == "awaiting_continuation":
            _attest_awaiting_continuation(store, config, record)
        elif record.state == "continuing":
            _attest_continuing_continuation(store, config, record)
        elif record.state == "terminal":
            _attest_terminal_continuation(store, config, record)


def _fresh_reopenable_terminal_run(
    store: RunStore, continuation_run_id: str
) -> RunRecord | None:
    """Return fresh terminal state only when a tick still owns reopening."""
    fresh = store.get(continuation_run_id)
    if fresh is None or fresh.status in {"pending", "success"}:
        return None
    return fresh


def _settle_terminal_continuation(
    store: RunStore,
    config: DispatchConfig,
    record: Any,
    run: RunRecord,
    *,
    reservation: EpicReservation | None = None,
) -> bool:
    """Reopen a claim whose child wrote a non-success terminal run before exit.

    The normal resume caller performs both success finalization and failure
    reopening after its dispatch callback returns. A driver tick may safely
    reconcile failure/publication states, but a successful run row precedes
    host publication, so success remains owned by the resume wrapper.

    Return whether the reservation caller may release its claim.
    """
    continuation_run_id = record.continuation_run_id
    if not continuation_run_id:
        return True
    if run.status == "success":
        # A successful RunRecord only proves that the agent finished. Host
        # publication and the resume wrapper still have to complete before the
        # continuation's reservation can be released. The wrapper owns the
        # success finalization, so a concurrent tick must leave this claim
        # intact rather than making publication race a stale generation.
        return False
    if run.error_category == HOST_RESTART_REPEATED:
        # Only a clean checkout's reap writes this on a continuation run (a
        # dirty one is preserved), and that reap died before it closed the
        # continuation (#2964). Finish the close: there are no bytes left to
        # validate, and reopening would offer a clean checkout as preserved.
        _finalize_repeated_host_restart_continuation(
            store,
            config,
            record,
            continuation_run_id,
            issue_num=_continuation_issue_num(record.item_id),
        )
        return True
    configured, candidate, checkout_generation = (
        _claim_and_validate_continuation_checkout(
            store,
            config,
            record,
            boundary="terminal continuation settlement",
        )
    )
    try:
        fresh = _fresh_reopenable_terminal_run(store, continuation_run_id)
        if fresh is None:
            return False
        run = fresh
        if reservation is not None:
            _require_claimed_orphan_identity(
                store,
                config,
                reservation,
                record,
                candidate,
                run=run,
            )
        changed = store.mark_continuation_awaiting(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            record.generation,
            continuation_run_id,
            precommit_check=_generation_precommit_check(
                candidate,
                checkout_generation,
                item_id=record.item_id,
                boundary="terminal continuation reopening",
            ),
        )
        _require_checkout_generation(
            candidate,
            checkout_generation,
            item_id=record.item_id,
            boundary="terminal continuation reopening",
        )
        if not changed:
            return True
    finally:
        release_checkout_claim(configured)
    if run.status == "awaiting_publication":
        event = "continuation_publication"
        phase = "publication"
    else:
        event = "continuation_terminal"
        phase = "terminal"
    _emit_continuation_evidence(
        store,
        event,
        actor=Actor("policy", "driver_reconcile"),
        phase=phase,
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=continuation_run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation,
        state="awaiting_continuation",
        outcome=run.error_category or run.status,
        run_id=continuation_run_id,
        issue_num=_continuation_issue_num(record.item_id),
        repo=config.project.repo or None,
    )
    return True


def _finalize_dirty_orphan(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    worktree: Path,
    generation: str,
    owned: Any | None,
    *,
    now: datetime,
) -> bool | None:
    """Move a positively-dead dirty orphan out of ``pending``.

    Return ``None`` once the row is finalized, with *run* updated to the
    written terminal state; otherwise the preservation's own result.
    """
    # Keep the recorded tokens/cost intact, but stop budget.py from treating a
    # positively-dead run as an in-flight worst-case reservation. The CAS is
    # also the ownership re-check: host publication may outlive the container
    # and persist success after the liveness probe but before this write.
    if (
        _owner_may_be_alive(run) and not run.cleanup_runtime
    ) or not _kill_orphaned_attempt(run, store, config):
        return False
    _record_dirty_host_restart(store, config, reservation, run, owned)
    duration_seconds = (now - run.started_at).total_seconds()
    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="orphan finalization",
    )
    finalized = store.finalize_orphaned_pending(
        run.run_id,
        expected_owner=run,
        error_category="orphaned_pending_preserved",
        finished_at=now,
        duration_seconds=duration_seconds,
        precommit_check=_generation_precommit_check(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="orphan finalization",
        ),
    )
    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="orphan finalization write",
    )
    if not finalized:
        current = store.get(run.run_id)
        if _is_successful_continuation(current, owned):
            return True
        # A publisher terminalized the row between the liveness probe and this
        # write, so no continuation is recorded for the dirty worktree. Name it
        # here — otherwise the worktree stays on disk unreferenced and silent.
        _logger.warning(
            "epic %s: run %s went terminal before its dirty worktree could be "
            "preserved — %s is left on disk without a continuation record",
            reservation.epic_ref,
            run.run_id,
            worktree,
        )
        return False
    run.status = "killed"
    run.error_category = "orphaned_pending_preserved"
    run.exit_code = 137
    run.finished_at = now
    run.duration_seconds = duration_seconds
    return None


def _preserve_claimed_dirty_orphan(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    worktree: Path,
    generation: str,
    owned: Any | None,
    *,
    now: datetime,
) -> bool:
    """Record dirty orphaned work and release only its old reservation.

    The run's partial telemetry remains crash evidence, but the row is moved
    out of ``pending`` once liveness has positively proven its owner dead. A
    pending row is valued at the full runtime reservation by budget preflight;
    leaving a confirmed-dead row pending would therefore book sunk cost at the
    run cap and can wedge the continuation behind the per-item limit. The
    continuation row remains the overlay's non-dispatchable state, while the
    released reservation lets the claim transaction reserve a worker slot when
    an operator explicitly resumes the preserved work.
    """
    if _finalized_as_preserved(run):
        # An earlier tick proved this run dead and finalized it, then died
        # before recording the continuation below.
        _record_dirty_host_restart(store, config, reservation, run, owned)
    else:
        outcome = _finalize_dirty_orphan(
            store, config, reservation, run, worktree, generation, owned, now=now
        )
        if outcome is not None:
            return outcome

    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="orphan continuation handoff",
    )

    if owned is not None:
        # A continuation can crash after its own pending row is written. Reopen
        # the original recovery identity instead of inserting a second record
        # keyed by the continuation run id.
        changed = store.mark_continuation_awaiting(
            owned.epic_ref,
            owned.item_id,
            owned.original_run_id,
            owned.branch,
            owned.worktree_identity,
            owned.generation,
            run.run_id,
            precommit_check=_generation_precommit_check(
                worktree,
                generation,
                item_id=reservation.item_id,
                boundary="orphan continuation reopening",
            ),
        )
        _require_checkout_generation(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="orphan continuation reopening",
        )
        if not changed:
            _logger.warning(
                "continuation %s/%s lost ownership while preserving dirty work",
                owned.epic_ref,
                owned.item_id,
            )
            return True
        _emit_continuation_state(
            store,
            config,
            owned,
            event="continuation_started",
            phase="start",
            outcome=run.status,
            run_id=run.run_id,
        )
        _emit_continuation_evidence(
            store,
            "continuation_detected",
            actor=Actor("policy", "driver_reconcile"),
            phase="detection",
            epic_ref=owned.epic_ref,
            item_id=owned.item_id,
            original_run_id=owned.original_run_id,
            continuation_run_id=run.run_id,
            branch=owned.branch,
            worktree_identity=owned.worktree_identity,
            generation=owned.generation,
            state="awaiting_continuation",
            outcome="crash_with_dirty_work",
            run_id=run.run_id,
            issue_num=reservation.issue_num,
            repo=config.project.repo or None,
        )
    else:
        created = store.record_continuation(
            reservation.epic_ref,
            reservation.item_id,
            run.run_id,
            run.branch,
            worktree,
            checkout_generation=generation,
            precommit_check=_generation_precommit_check(
                worktree,
                generation,
                item_id=reservation.item_id,
                boundary="orphan continuation recording",
            ),
        )
        _require_checkout_generation(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="orphan continuation recording",
        )
        _emit_continuation_evidence(
            store,
            "continuation_detected",
            actor=Actor("policy", "driver_reconcile"),
            phase="detection",
            epic_ref=created.epic_ref,
            item_id=created.item_id,
            original_run_id=created.original_run_id,
            continuation_run_id=None,
            branch=created.branch,
            worktree_identity=created.worktree_identity,
            generation=created.generation,
            state=created.state,
            outcome="crash_with_dirty_work",
            run_id=run.run_id,
            issue_num=reservation.issue_num,
            repo=config.project.repo or None,
        )
    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="orphan preservation completion",
    )
    store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
    _logger.warning(
        "epic %s: preserving dirty worktree for %s after dead run %s; "
        "awaiting explicit continuation",
        reservation.epic_ref,
        reservation.item_id,
        run.run_id,
    )
    return True


def _recover_openshell_gateway_restart(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
) -> bool:
    """Bring a restarted sandbox's checkout home so it can be continued.

    A gateway restart is the one death on this runtime that leaves the work
    somewhere kew can still reach: the agent's processes are gone and its files
    are not, so the bytes are in the sandbox and nowhere else. They come back
    to the HOST DISPATCH CHECKOUT — ``<worktree_base>/agent/issue-<slug>``, the
    one path the continuation machinery will read — and they come back HERE,
    ahead of :func:`_preserve_dirty_orphan`, so that pass finds them as this
    run's own dirty work and offers them as a continuation instead of reaping
    the node FAILED with the work stranded on a gateway.

    The order is not a preference. That pass finalizes the run — a clean orphan
    is reaped and its reservation released on the spot — so a download after it
    would be recovering work for a run nothing is waiting on any more; and it
    binds the checkout's generation to every write it makes, so a download
    afterwards would be a write under a bound generation, which the recovery
    path (correctly) refuses.

    The sandbox is released only once its work is on the host. A download that
    failed leaves it on the gateway and says so — in the log and on the audit
    event — because fetching it by hand is the only recovery left: this same
    tick goes on to finalize the run, so no pending row survives for a later
    tick to retry against. That is the trade taken deliberately. This returns
    either way rather than raising, so the reservation is never held open
    waiting for a sandbox, and the sandbox is deliberately left behind for the
    operator who comes for it.

    Which failure it was is attested, not just that there was one. A merge that
    failed part-way leaves the host checkout holding some of the sandbox's
    files, and anything that reads that checkout next — the orphan path, or the
    operator — reads exactly that tree. So ``checkout_merge`` carries
    ``part_merged`` beside the ``checkout_recovered: false``, because whoever
    decides what to do with the tree needs to know it is half the sandbox's and
    half theirs rather than untouched.

    The event names the ``checkout`` considered whether or not bytes reached
    it, so an operator reading one failure apart from another knows which tree
    the verdict is about.
    """
    if run.container_name is None or not _openshell_gateway_restarted(run, config):
        return False  # not this runtime, or not this death
    if not run.container_id:
        # No recorded gateway id: nothing proves the sandbox answering to this
        # name is the one this run created, and a download and delete of
        # another run's sandbox is the outcome worse than none (#2625).
        _logger.error(
            "epic %s: openshell sandbox %s has no gateway id on run %s, so it "
            "is not recovered; it is left on the gateway",
            reservation.epic_ref,
            run.container_name,
            run.run_id,
        )
        return False
    sandbox = run.container_name
    merge, checkout = _download_openshell_checkout(
        store, config, reservation, run, sandbox
    )
    if merge == "merged":
        from kew.runtime_backend import backend_for

        backend_for(OPENSHELL_RUNTIME, openshell=config.sandbox.openshell).cleanup(
            sandbox
        )
    else:
        _log_sandbox_left_holding_work(reservation, run, sandbox, merge)
    _emit_audit(
        store,
        OPENSHELL_GATEWAY_RESTART,
        actor=Actor("policy", "driver_reconcile"),
        run_id=run.run_id,
        issue_num=reservation.issue_num,
        item_id=reservation.item_id,
        repo=config.project.repo or None,
        payload={
            "epic_ref": reservation.epic_ref,
            "reason": OPENSHELL_GATEWAY_RESTART,
            "sandbox": sandbox,
            "checkout": "" if checkout is None else str(checkout),
            "checkout_recovered": merge == "merged",
            "checkout_merge": merge,
        },
    )
    _logger.warning(
        "epic %s: run %s lost its agent to an openshell gateway restart; "
        "checkout merge: %s into %s",
        reservation.epic_ref,
        run.run_id,
        merge,
        "no checkout this run recorded" if checkout is None else checkout,
    )
    return merge == "merged"


def _log_sandbox_left_holding_work(
    reservation: EpicReservation,
    run: RunRecord,
    sandbox: str,
    merge: CheckoutMerge,
) -> None:
    """Name the tree the operator is left with, not just the failure.

    A part-way merge and an unmerged recovery both keep the sandbox, and an
    operator reading "did not reach the host" against a checkout that quietly
    holds half the sandbox's files would hand-merge onto a tree they believe is
    their own. Which refusal it was is logged where it was decided; this is the
    one line that says what state the host is in.
    """
    _logger.error(
        "epic %s: openshell sandbox %s was restarted and its checkout %s; it is "
        "left on the gateway with run %s's work",
        reservation.epic_ref,
        sandbox,
        "did not reach the host, which is as it was"
        if merge == "unmerged"
        else "merged only part-way, so the host checkout holds some of the "
        "sandbox's files and not the rest",
        run.run_id,
    )


def _download_openshell_checkout(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    sandbox: str,
) -> tuple[CheckoutMerge, Path | None]:
    """Merge the sandbox checkout into the HOST dispatch checkout for this item.

    The destination is ``<worktree_base>/agent/issue-<slug>`` and nowhere else,
    because the destination is what decides whether the work can be continued.
    A contained dispatch launched from a standalone clone under the system
    temp directory (``dispatch_core._materialize_checkout``), and recovering
    into that clone is recovering into a path no continuation can name: every
    continuation boundary — :func:`_validated_recorded_worktree`,
    :func:`_claim_and_validate_continuation_checkout`,
    :func:`_configured_checkout_target` — requires the deterministic host path,
    so bytes anywhere else are reachable to nothing but a human with the
    random directory name.

    The path has to be FREE. Whatever already stands there belongs to some
    other dispatch of this item, and merging one run's sandbox into another
    run's tree — then attesting it recovered and deleting the sandbox holding
    the only copy — is the one outcome worse than not recovering at all. So a
    present path refuses the merge, with the sandbox kept, and the operator is
    told which tree is in the way.

    The path considered is returned alongside the verdict whether or not the
    merge happened: it is what the event and the log name.

    Every refusal before :func:`kew.openshell_launch.download_checkout` is
    ``"unmerged"`` — each is decided before a byte is written, so nothing on
    the host is touched. Only the download itself can report a tree it got
    part-way through. ``"unmerged"`` is also what a full download gets when the
    tree it handed back is not one a continuation can be read from
    (:func:`_bind_readable_recovered_checkout`), because that refusal takes the
    downloaded tree back off the host; a binding that then fails reports
    ``"part_merged"``, because by then the tree is staying.
    """
    try:
        configured, expected_branch = _configured_checkout_target(
            config, reservation.item_id
        )
        base = _recovery_launch_evidence(
            store, config, reservation, run, expected_branch
        )
    except (_PreexistingWorktreeRefusal, CheckoutAdmissionRefusal) as exc:
        _logger.error(
            "epic %s: run %s has no usable launch evidence, so the work in its "
            "restarted sandbox stays there: %s",
            reservation.epic_ref,
            run.run_id,
            exc,
        )
        return "unmerged", None
    if base is None:
        return "unmerged", None
    return _merge_sandbox_under_claim(
        store, config, reservation, run, sandbox=sandbox, checkout=configured, base=base
    ), configured


def _recovery_launch_evidence(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    expected_branch: str,
) -> str | None:
    """The publication base to re-bind the recovered checkout at, or a refusal.

    ``None`` refuses the recovery outright; ``""`` is the complete answer
    under checkout publication, which binds no base at all.

    Two things are required of a run before its sandbox may be brought home.
    Its branch has to be the one the host path belongs to, because the
    recovered tree is bound to that path under that branch. And it has to
    carry its own launch checkout binding, which is what makes it a dispatch
    with an attested checkout rather than a row naming a sandbox; the
    directory that binding names is NOT read — a contained run's clone is
    routinely gone by the time its sandbox is recovered — but under canonical
    publication the base recorded against it is the base the recovered
    checkout is compared with, and a continuation cannot be minted without
    one.
    """
    if run.branch != expected_branch:
        _logger.error(
            "epic %s: run %s is on branch %s, not the %s its host checkout "
            "belongs to, so its restarted sandbox keeps the work",
            reservation.epic_ref,
            run.run_id,
            run.branch,
            expected_branch,
        )
        return None
    bound = recorded_run_checkout(
        store, run_id=run.run_id, item_id=reservation.item_id, branch=run.branch
    )
    if bound is None:
        _logger.error(
            "epic %s: run %s bound no launch checkout, so the work in its "
            "restarted sandbox stays there",
            reservation.epic_ref,
            run.run_id,
        )
        return None
    if config.publication.publish != "canonical":
        return ""
    return _recovered_canonical_base(store, reservation, run, bound)


def _recovered_canonical_base(
    store: RunStore,
    reservation: EpicReservation,
    run: RunRecord,
    bound: tuple[Path, str],
) -> str | None:
    """The base the dead run recorded for its launch checkout, under canonical.

    Read off the launch binding by its recorded generation rather than by
    staring at the directory, because the directory is a contained dispatch's
    clone and may well be gone. The base travels with the run, not with the
    clone: the recovered tree is the same work cut from the same base, so it
    is re-bound at that base and the canonical comparison that decides whether
    there is anything to preserve has something to compare against.

    ``None`` when nothing was recorded. A canonical continuation cannot be
    minted without a base, so recovering the bytes would strand them at the
    host path with no way to continue them — and the sandbox, which can still
    be fetched by hand, is the better place to leave them.
    """
    checkout, generation = bound
    base = _recorded_checkout_base(
        store,
        run_id=run.run_id,
        item_id=reservation.item_id,
        expected_branch=run.branch,
        candidate=checkout,
        generation=generation,
    )
    if not base:
        _logger.error(
            "epic %s: run %s recorded no publication base for its launch "
            "checkout, so its restarted sandbox keeps the work",
            reservation.epic_ref,
            run.run_id,
        )
        return None
    return base


def _merge_sandbox_under_claim(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    sandbox: str,
    checkout: Path,
    base: str,
) -> CheckoutMerge:
    """Hold the host checkout's lease for the whole recovery.

    The claim covers the emptiness check, the download and the binding as one
    boundary: a concurrent dispatch that materializes the host checkout
    between the check and the merge would otherwise have its fresh tree
    overwritten by a sandbox it knows nothing about.
    """
    try:
        claim_checkout(checkout)
    except CheckoutBusy as exc:
        _logger.error(
            "epic %s: run %s host checkout %s is claimed elsewhere, so its "
            "sandbox's work stays in the sandbox: %s",
            reservation.epic_ref,
            run.run_id,
            checkout,
            exc,
        )
        return "unmerged"
    try:
        return _merge_sandbox_into_host_checkout(
            store,
            config,
            reservation,
            run,
            sandbox=sandbox,
            checkout=checkout,
            base=base,
        )
    finally:
        release_checkout_claim(checkout)


def _merge_sandbox_into_host_checkout(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    sandbox: str,
    checkout: Path,
    base: str,
) -> CheckoutMerge:
    """Download into a free host checkout path and bind what landed.

    A download that brought nothing back takes its own half-made directory
    with it: an empty tree left standing at the host dispatch path is read by
    every later pass as an existing checkout with an untrusted identity, which
    refuses the item's next dispatch over work that is not there. Removing it
    is safe precisely because this path was proven free a moment ago, so
    nothing but this recovery could have created it.
    """
    if os.path.lexists(checkout):
        _logger.error(
            "epic %s: a tree already stands at host checkout %s, so run %s's "
            "restarted sandbox %s keeps its work rather than merging into "
            "another dispatch's checkout",
            reservation.epic_ref,
            checkout,
            run.run_id,
            sandbox,
        )
        return "unmerged"
    merge = _download_into_free_checkout(config, sandbox, checkout)
    if merge == "unmerged":
        if os.path.lexists(checkout):
            remove_checkout_tree(checkout)
        return "unmerged"
    if merge == "part_merged":
        return "part_merged"
    return _bind_readable_recovered_checkout(
        store,
        config,
        reservation,
        run,
        sandbox=sandbox,
        checkout=checkout,
        base=base,
    )


def _bind_readable_recovered_checkout(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    sandbox: str,
    checkout: Path,
    base: str,
) -> CheckoutMerge:
    """Prove the orphan path can read the recovered tree before claiming it home.

    ``"merged"`` is what deletes the sandbox and attests
    ``checkout_recovered``, so it may only be said of a tree the continuation
    machinery will actually accept. A sandbox hands back whatever state its
    agent left: a detached HEAD from a half-finished rebase or a ``git
    bisect``, or the agent's own branch instead of the item's. The identity
    gate every continuation boundary reads — :func:`_validated_recorded_worktree`
    — refuses exactly those, so a tree that lands here and fails it is worse
    than one that never landed: :func:`_preserve_dirty_orphan` raises on the
    present-but-untrusted path instead of preserving anything, the run keeps
    its reservation on a ``pending`` row no later pass can finalize, and the
    sandbox that held the only other copy has already been deleted.

    So the SAME predicate is asked here, before any of that is committed, and a
    tree that fails it is removed and reported ``"unmerged"``. Removing it is
    both safe and necessary: this path was proven free moments ago so nothing
    but this download can have created it, and a tree left standing at the host
    dispatch path is read by every later dispatch of the item as a checkout
    with an untrusted identity, which refuses the dispatch over work that is
    not the item's. The host is therefore left exactly as it was, which is what
    ``"unmerged"`` means — and the sandbox, not being merged, is kept with the
    work for the operator the log names it to.
    """
    if _validated_recorded_worktree(reservation, run, config) is not None:
        return _bind_recovered_checkout(
            store, config, reservation, run, checkout=checkout, base=base
        )
    _logger.error(
        "epic %s: run %s's restarted sandbox %s handed back a tree at %s that "
        "is not a checkout of branch %s (a detached HEAD or another branch), "
        "so nothing downstream could read it as recovered work; the host "
        "checkout is left as it was and the sandbox keeps the work",
        reservation.epic_ref,
        run.run_id,
        sandbox,
        checkout,
        run.branch,
    )
    remove_checkout_tree(checkout)
    return "unmerged"


def _download_into_free_checkout(
    config: DispatchConfig, sandbox: str, checkout: Path
) -> CheckoutMerge:
    """Ask the gateway for the sandbox's checkout, reporting what landed."""
    from kew.openshell import OpenShellClient, OpenShellError
    from kew.openshell_launch import download_checkout

    try:
        gateway = OpenShellClient.from_settings(config.sandbox.openshell)
    except (OpenShellError, OSError, ValueError) as exc:
        _logger.error(
            "openshell sandbox %s cannot be reached to recover %s: %s",
            sandbox,
            checkout,
            exc,
        )
        return "unmerged"
    return download_checkout(gateway, sandbox, checkout)


def _bind_recovered_checkout(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    checkout: Path,
    base: str,
) -> CheckoutMerge:
    """Give the recovered tree the identity the orphan path reads it by.

    Recovered bytes nobody can attest are not recovered work: every boundary
    downstream — the producer selection, the byte comparison, the continuation
    insert — matches a checkout by the generation its producing run bound to
    it, and this tree has no such binding because this run never had a host
    checkout to bind. So the tree is minted with a fresh generation and that
    generation is recorded against THIS run, under the same writer a launch
    uses, which is what lets :func:`_preserve_dirty_orphan` recognize the
    sandbox's work as this run's and offer it as a continuation.

    A binding that fails reports ``part_merged``: the sandbox's files are on
    the host, which is not "untouched", but nothing downstream can read them,
    so the sandbox keeps the whole copy for the operator.
    """
    from kew.dispatch_core import _bind_publication_base
    from kew.publication import PublicationError

    try:
        write_checkout_generation(checkout)
        _bind_publication_base(
            store,
            publish=config.publication.publish,
            base=base,
            run_id=run.run_id,
            issue_num=reservation.issue_num,
            item_id=reservation.item_id,
            branch=run.branch,
            checkout=checkout,
        )
    except (CheckoutAdmissionRefusal, PublicationError, OSError, ValueError) as exc:
        _logger.error(
            "epic %s: run %s recovered its sandbox into %s but could not bind "
            "the checkout to the run, so nothing downstream can read it as "
            "recovered work: %s",
            reservation.epic_ref,
            run.run_id,
            checkout,
            exc,
        )
        return "part_merged"
    return "merged"


def _dead_run_has_no_host_checkout(
    store: RunStore,
    reservation: EpicReservation,
    run: RunRecord,
    configured: Path,
) -> bool:
    """Whether this run's own checkouts are all somewhere other than *configured*.

    A CONTAINED dispatch's checkout is the standalone clone under the system
    temp directory that was uploaded to its sandbox, so the host dispatch path
    holds nothing of that run's: whatever stands there was left by some other
    dispatch of the same item. Preservation has nothing to read and no
    business deciding anything about that tree — and reading it anyway is what
    wedged the reconciler. An unrelated leftover carries no binding naming
    this run, so producer selection refused it, and the refusal came out of
    ``advance()`` on every tick: the run stayed ``pending``, its reservation
    stayed held, and nothing in the epic advanced again.

    A gateway-restart recovery that merged a sandbox into the host path binds
    this run there too (:func:`_bind_recovered_checkout`), so a recovered run
    answers false and its work IS preserved and offered as a continuation.

    A run with no recorded checkout at all answers false as well: it predates
    this evidence or never bound any, and the existing refusal — which exists
    to stop a fresh dispatch discarding retained work — stays in force for it.
    """
    recorded = recorded_run_checkouts(
        store, run_id=run.run_id, item_id=reservation.item_id, branch=run.branch
    )
    if not recorded:
        return False
    return not any(
        continuation_checkout_matches(checkout, configured) for checkout in recorded
    )


def _hold_for_contained_clone(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
) -> bool:
    """Bring a contained host-restart run's dirty clone home, or hold (#2971).

    A contained dispatch works in the standalone clone it launched from, under
    the system temp directory, so the host dispatch path holds nothing of its
    and :func:`_dead_run_has_no_host_checkout` would let a host restart reap
    it and re-admit a fresh dispatch over whatever the clone holds. So the
    recorded clone is read first. Clean: nothing is lost, and the ordinary
    path re-admits the node. Dirty: its tree is copied to the host dispatch
    path and bound to this run, the same way a gateway restart's sandbox is
    brought home (:func:`_bind_recovered_checkout`), so
    :func:`_preserve_dirty_orphan` preserves it as an awaiting continuation.
    The clone itself is left as it was.

    ``True`` holds the node while its container is not yet proved stopped.
    A clone whose state cannot be proved — a recorded clone that is gone, or
    a contained run that recorded no clone at all, included — or whose work
    cannot be put where a continuation can read it raises the refusal
    instead: the reconciler writes the node's hold
    (:func:`_hold_unreconcilable_reservation`), because a retry would discard
    that work and a silent pending row reads as live work forever.

    A PSEUDO-EPIC's run is read the same way: its dirty clone is brought home
    and preserved under the pseudo-epic before its reservation is released,
    as a delegated run's dirty host checkout already is. The reconciler
    writes no hold for a pseudo-epic (:func:`_hold_unreconcilable_reservation`),
    so a refusal there keeps the run pending and the reservation held, with
    the reason logged on every pass, until a person recovers the clone and
    releases it with ``kew epic unhold`` (:func:`pseudo_epic_clone_refusal`).
    A reviewer's run with no recorded clone, or a pseudo-epic run whose
    recorded clone is gone, is the exception: a reviewer records its
    container but never owns a clone, and gone bytes are nothing to keep a
    reservation over, so its fleet slot would only leak (#1059).
    """
    if not run.container_name or not _host_died_under(run):
        return False
    if not _kill_orphaned_attempt(run, store, config):
        return True
    configured, expected_branch = _configured_checkout_target(
        config, reservation.item_id
    )
    clone = _recorded_contained_clone(store, reservation, run, configured)
    if clone is None:
        return False
    base = _recovery_launch_evidence(store, config, reservation, run, expected_branch)
    if base is None:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} clone {clone[0]} has no recorded "
            "base, so whether it holds work cannot be proved"
        )
    if not _contained_clone_holds_work(config, reservation, run, clone, base):
        return False
    return _copy_clone_home_under_claim(
        store, config, reservation, run, clone=clone, checkout=configured, base=base
    )


def _recorded_contained_clone(
    store: RunStore, reservation: EpicReservation, run: RunRecord, configured: Path
) -> tuple[Path, str] | None:
    """The run's one recorded clone and generation, when it has bytes to read.

    ``None`` when the ordinary path reads the run's checkout itself: a
    binding at the host path. More than one binding is a refusal: which tree
    is the run's cannot be proved. So is a recorded clone that is gone: the
    binding proves a checkout existed, and nothing now says whether it held
    work. And so is no binding at all: a contained launch binds its clone
    before its pending row is written, so a contained row without one
    predates that binding or skipped it, and its clone — somewhere under the
    system temp directory — is neither found nor proved clean. Each is
    released only by a person (:func:`_clone_hold_released`). A reviewer's run
    with no binding never owned a clone (:func:`_run_owns_no_clone`), and a
    pseudo-epic run's gone clone has no bytes left to keep: each is reaped
    and its reservation released. A delegated dispatch with no binding is
    not excepted — its clone may hold work, so it keeps its reservation.
    """
    recorded = recorded_run_checkouts(
        store, run_id=run.run_id, item_id=reservation.item_id, branch=run.branch
    )
    if not recorded:
        # A reviewer never binds a clone: refusing would keep its fleet slot
        # held forever (#1059). Any other run may own one somewhere.
        if _run_owns_no_clone(reservation, run) or _clone_hold_released(
            store, reservation, run
        ):
            return None
        raise _PreexistingWorktreeRefusal(
            # The guidance ends the reason: a surface capping it keeps the tail.
            f"{item_ref(reservation.item_id)}: run {run.run_id} recorded no clone, "
            "so whether its clone under "
            f"{work_dirs.configured_root() / '<ledger id>' / 'kew-sandbox-*'} held "
            "work cannot be "
            f"proved; {_CLONE_RECOVERY_GUIDANCE}"
        )
    if any(
        continuation_checkout_matches(checkout, configured) for checkout in recorded
    ):
        return None
    bound = recorded_run_checkout(
        store, run_id=run.run_id, item_id=reservation.item_id, branch=run.branch
    )
    if bound is None:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} has more than one recorded clone"
        )
    clone, generation = bound
    if not os.path.lexists(clone):
        # A pseudo-epic has no hold to release, and bytes that are gone
        # cannot be recovered by anyone: its run is reaped as it was.
        if reservation.epic_ref in PSEUDO_EPIC_REFS or _clone_hold_released(
            store, reservation, run
        ):
            return None
        raise _PreexistingWorktreeRefusal(
            # The guidance ends the reason: a surface capping it keeps the tail.
            f"{item_ref(reservation.item_id)}: run {run.run_id}'s recorded clone "
            f"{clone} is gone after the host restart, so whether it held work "
            f"cannot be proved; {_CLONE_RECOVERY_GUIDANCE}"
        )
    return clone.resolve(strict=False), generation


#: The tail of an unprovable-clone hold's reason, and how its release is
#: recognised: short enough that ``safe_hold_reason``'s capped tail keeps it.
_CLONE_RECOVERY_GUIDANCE = "recover its work by hand; kew epic unhold re-dispatches it"

#: Run kinds that read a PR and never own a clone of their own.
_CLONELESS_RUN_KINDS = frozenset(
    {"review", ADVERSARIAL_REVIEW_RUN_KIND, REVIEW_STAGE_RUN_KIND}
)


def _run_owns_no_clone(reservation: EpicReservation, run: RunRecord) -> bool:
    """Is this a review-pool reviewer, which records a container but no clone?

    Only that proves an unbound contained run left no bytes behind. The
    synthetic epic alone proves nothing: a delegated dispatch launched before
    clone binding existed owns a clone no row names (#2971).
    """
    return reservation.epic_ref == REVIEW_EPIC_REF and (
        run.run_kind in _CLONELESS_RUN_KINDS
    )


def pseudo_epic_clone_refusal(
    store: RunStore, epic_ref: str, item_id: str
) -> EpicHold | None:
    """A pseudo-epic's unprovable-clone refusal, as a hold a person can release.

    The reconciler writes no hold row under a pseudo-epic
    (:func:`_hold_unreconcilable_reservation`), so ``kew epic unhold`` would
    find nothing to clear and the delegated run would stay pending, its
    reservation held, forever. This names the refusal instead: the item's
    reservation under *epic_ref* whose contained run this machine rebooted
    under, still pending. Its reason ends with the recovery guidance, so
    the release the CLI attests is the one :func:`_clone_hold_released`
    reads. ``None`` when no such run stands.
    """
    if epic_ref not in PSEUDO_EPIC_REFS:
        return None
    for reservation in store.list_epic_reservations(epic_ref):
        if reservation.item_id != item_id:
            continue
        run = resolve_reservation_run(store, reservation)
        if (
            run is None
            or run.status != "pending"
            or not run.container_name
            or not _host_died_under(run)
        ):
            continue
        return EpicHold(
            issue_num=int(item_id) if item_id.isdigit() else 0,
            epic_ref=epic_ref,
            reason=(
                f"{PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX}: run {run.run_id}'s "
                f"clone cannot be proved clean; {_CLONE_RECOVERY_GUIDANCE}"
            ),
            held_at=run.started_at,
            item_id=item_id,
        )
    return None


def _clone_hold_released(
    store: RunStore, reservation: EpicReservation, run: RunRecord
) -> bool:
    """Has a person released this run's unprovable-clone hold (#2971)?

    A gone or unrecorded clone never becomes provable, so the refusal would
    re-hold the node on every tick after ``kew epic unhold`` and wedge it with
    its run pending. The release is the person's word that the work is
    accounted for: an attested ``epic_node_unhold`` of that reason, for this
    node, since this run started, lets the ordinary path reap the run and
    re-admit the node.
    """
    released = store.list_audit_events(
        limit=None,
        event="epic_node_unhold",
        item_or_issue=reservation.item_id,
        since=run.started_at,
    )
    return any(
        (event.payload or {}).get("epic_ref") == reservation.epic_ref
        and str((event.payload or {}).get("reason", "")).endswith(
            _CLONE_RECOVERY_GUIDANCE
        )
        for event in released
    )


def _contained_clone_holds_work(
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    bound: tuple[Path, str],
    base: str,
) -> bool:
    """Whether the run's own clone holds work, read under its bound identity."""
    clone, generation = bound
    if not _git_worktree_matches(clone, run.branch, config=config):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} clone {clone} is not a checkout "
            f"of {run.branch}"
        )
    _require_checkout_generation(
        clone, generation, item_id=reservation.item_id, boundary="clone inspection"
    )
    return _checkout_holds_work(config, clone, run.branch, base_sha=base)


def _copy_clone_home_under_claim(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    clone: tuple[Path, str],
    checkout: Path,
    base: str,
) -> bool:
    """Copy the dirty clone to a FREE host path and bind it; ``True`` holds.

    The host path's lease covers the emptiness check, the copy and the
    binding, as :func:`_merge_sandbox_under_claim` does for a sandbox.
    Whatever already stands there is another dispatch's, so it is never
    merged into; a copy or binding that fails takes its own tree back off.
    """
    try:
        claim_checkout(checkout)
    except CheckoutBusy as exc:
        raise _PreexistingWorktreeRefusal(
            f"host checkout {checkout} is claimed elsewhere: {exc}"
        ) from exc
    try:
        if os.path.lexists(checkout):
            raise _PreexistingWorktreeRefusal(
                f"a tree already stands at host checkout {checkout}"
            )
        try:
            shutil.copytree(clone[0], checkout, symlinks=True)
        except (OSError, shutil.Error) as exc:
            remove_checkout_tree(checkout)
            raise _PreexistingWorktreeRefusal(
                f"clone {clone[0]} could not be copied to {checkout}: {exc}"
            ) from exc
        if not _bind_copied_clone(store, config, reservation, run, checkout, base):
            remove_checkout_tree(checkout)
            raise _PreexistingWorktreeRefusal(
                f"the copy of clone {clone[0]} at {checkout} could not be bound"
            )
    finally:
        release_checkout_claim(checkout)
    return False


def _bind_copied_clone(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    checkout: Path,
    base: str,
) -> bool:
    """Bind the copied clone only once the continuation gate would read it."""
    if _validated_recorded_worktree(reservation, run, config) is None:
        return False
    merge = _bind_recovered_checkout(
        store, config, reservation, run, checkout=checkout, base=base
    )
    return merge == "merged"


def _finalized_as_preserved(run: RunRecord) -> bool:
    """The row a dirty orphan's preservation writes before its continuation."""
    return run.status == "killed" and run.error_category == "orphaned_pending_preserved"


def _preserved_without_continuation(
    store: RunStore, epic_ref: str, run: RunRecord
) -> bool:
    """A run finalized as preserved whose continuation was never recorded.

    Preservation finalizes the dead run, then records its continuation in a
    second transaction. A driver that dies between the two leaves the dirty
    checkout referenced by nothing: the run is terminal, so no later tick
    re-inspects it as pending work.
    """
    if not _finalized_as_preserved(run):
        return False
    return not any(
        run.run_id in (record.original_run_id, record.continuation_run_id)
        for record in store.list_continuations(epic_ref)
    )


def _preserve_dirty_orphan(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    now: datetime,
    settle_clean: bool = False,
) -> bool:
    """Bind, compare, and preserve dead-run bytes under one checkout claim.

    A run already finalized as preserved (see
    :func:`_preserved_without_continuation`) was proven dead and stopped
    before that write committed; only its continuation is recorded again.
    """
    if not _finalized_as_preserved(run):
        if _hold_for_contained_clone(store, config, reservation, run):
            return True
        if not _kill_orphaned_attempt(run, store, config):
            return False
    configured = _recorded_worktree_path(reservation, run, config)
    if configured is None:
        configured, _ = _configured_checkout_target(config, reservation.item_id)
        return _refuse_untrusted_checkout_under_claim(reservation.item_id, configured)
    if _dead_run_has_no_host_checkout(store, reservation, run, configured):
        return False
    claim_checkout(configured)
    try:
        owned = _continuation_owned_by_run(store, reservation.epic_ref, run.run_id)
        if owned is not None:
            _require_continuation_checkout_generation(store, owned, configured)
        worktree = _validated_recorded_worktree(reservation, run, config)
        if worktree is None:
            _refuse_present_untrusted_checkout(reservation.item_id, configured)
            return False
        _require_claimed_orphan_identity(
            store,
            config,
            reservation,
            owned,
            worktree,
            run=run,
        )
        _, base, generation = _orphan_run_for_preexisting_work(
            store,
            reservation.item_id,
            run.branch,
            worktree,
            config,
            run=run,
            owned=owned,
        )
        _require_checkout_generation(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="orphan identity matching",
        )
        holds_work = _checkout_holds_work(config, worktree, run.branch, base_sha=base)
        _require_checkout_generation(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="orphan byte comparison",
        )
        if not holds_work:
            return _settle_claimed_clean_orphan(
                store,
                config,
                reservation,
                run,
                worktree,
                generation,
                owned,
                now=now,
                enabled=settle_clean,
            )
        return _preserve_claimed_dirty_orphan(
            store,
            config,
            reservation,
            run,
            worktree,
            generation,
            owned,
            now=now,
        )
    finally:
        release_checkout_claim(configured)


def _settle_claimed_clean_orphan(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    worktree: Path,
    generation: str,
    owned: Any | None,
    *,
    now: datetime,
    enabled: bool,
) -> bool:
    """Reap a clean orphan without releasing its validated checkout claim."""
    if not enabled:
        return False
    fresh = store.get(run.run_id)
    if fresh is None or fresh.status != "pending":
        if _is_successful_continuation(fresh, owned):
            return True
        store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
        return True
    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="clean orphan finalization",
    )
    reaped = _reap_orphaned_run(
        store,
        config,
        reservation,
        fresh,
        now=now,
        precommit_check=_generation_precommit_check(
            worktree,
            generation,
            item_id=reservation.item_id,
            boundary="clean orphan finalization",
        ),
    )
    _require_checkout_generation(
        worktree,
        generation,
        item_id=reservation.item_id,
        boundary="clean orphan finalization write",
    )
    if not reaped:
        current = store.get(run.run_id)
        if _is_pending_run(current) or _is_successful_continuation(current, owned):
            return True
    if reaped and owned is not None:
        _reopen_reaped_continuation(
            store,
            config,
            reservation,
            fresh,
            owned,
            worktree,
            generation,
        )
    store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
    return True


def _reconcile_dead_pending(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    *,
    now: datetime,
) -> None:
    fresh = store.get(run.run_id)
    if fresh is None or fresh.status != "pending":
        owned = _continuation_owned_by_run(
            store, reservation.epic_ref, reservation.run_id
        )
        if _is_successful_continuation(fresh, owned):
            return
        store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
        return
    run = fresh
    if _cleanup_blocked_by_owner(run):
        return
    _recover_openshell_gateway_restart(store, config, reservation, run)
    if _preserve_dirty_orphan(
        store, config, reservation, run, now=now, settle_clean=True
    ):
        return
    owned = _continuation_owned_by_run(store, reservation.epic_ref, run.run_id)
    if owned is None:
        _reap_orphaned_run(store, config, reservation, run, now=now)
    else:
        configured, candidate, checkout_generation = (
            _claim_and_validate_continuation_checkout(
                store,
                config,
                owned,
                boundary="dead-run continuation reconciliation",
            )
        )
        try:
            _require_claimed_orphan_identity(
                store,
                config,
                reservation,
                owned,
                candidate,
                run=run,
            )
            reaped = _reap_orphaned_run(
                store,
                config,
                reservation,
                run,
                now=now,
                precommit_check=_generation_precommit_check(
                    candidate,
                    checkout_generation,
                    item_id=reservation.item_id,
                    boundary="dead-run continuation finalization",
                ),
            )
            if reaped:
                _reopen_reaped_continuation(
                    store,
                    config,
                    reservation,
                    run,
                    owned,
                    candidate,
                    checkout_generation,
                )
            else:
                current = store.get(run.run_id)
                if _is_successful_continuation(current, owned):
                    return
        finally:
            release_checkout_claim(configured)
    current = store.get(run.run_id)
    if _is_pending_run(current):
        return
    store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)


def _reopen_reaped_continuation(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    owned: Any,
    candidate: Path,
    checkout_generation: str,
) -> None:
    """Reopen one clean reaped continuation under its checkout claim.

    A continuation the host died under for the second time is not reopened
    (#2964): its node failed, and a stale awaiting record would offer a
    clean checkout back as preserved work. It is finalized instead.
    """
    reaped = store.get(run.run_id)
    if reaped is not None and reaped.error_category == HOST_RESTART_REPEATED:
        _finalize_repeated_host_restart_continuation(
            store, config, owned, run.run_id, issue_num=reservation.issue_num
        )
        return
    changed = store.mark_continuation_awaiting(
        owned.epic_ref,
        owned.item_id,
        owned.original_run_id,
        owned.branch,
        owned.worktree_identity,
        owned.generation,
        run.run_id,
        precommit_check=_generation_precommit_check(
            candidate,
            checkout_generation,
            item_id=owned.item_id,
            boundary="dead-run continuation reopening",
        ),
    )
    _require_checkout_generation(
        candidate,
        checkout_generation,
        item_id=owned.item_id,
        boundary="dead-run continuation reopening",
    )
    if not changed:
        return
    _emit_continuation_evidence(
        store,
        "continuation_terminal",
        actor=Actor("policy", "driver_reconcile"),
        phase="terminal",
        epic_ref=owned.epic_ref,
        item_id=owned.item_id,
        original_run_id=owned.original_run_id,
        continuation_run_id=run.run_id,
        branch=owned.branch,
        worktree_identity=owned.worktree_identity,
        generation=owned.generation,
        state="awaiting_continuation",
        outcome="crash_without_preserved_work",
        run_id=run.run_id,
        issue_num=reservation.issue_num,
        repo=config.project.repo or None,
    )


def _finalize_repeated_host_restart_continuation(
    store: RunStore,
    config: DispatchConfig,
    owned: Any,
    run_id: str,
    *,
    issue_num: int | None,
) -> None:
    """Close the continuation whose run the host died under twice (#2964)."""
    finalized = store.finalize_continuation(
        owned.epic_ref,
        owned.item_id,
        owned.original_run_id,
        owned.branch,
        owned.worktree_identity,
        owned.generation,
        run_id,
    )
    if not finalized:
        return
    _emit_continuation_evidence(
        store,
        "continuation_terminal",
        actor=Actor("policy", CLEAN_HOST_DEATH_CLOSE_ACTOR),
        phase="terminal",
        epic_ref=owned.epic_ref,
        item_id=owned.item_id,
        original_run_id=owned.original_run_id,
        continuation_run_id=run_id,
        branch=owned.branch,
        worktree_identity=owned.worktree_identity,
        generation=owned.generation,
        state="terminal",
        outcome=HOST_RESTART_REPEATED,
        run_id=run_id,
        issue_num=issue_num,
        repo=config.project.repo or None,
    )


def _reconcile_missing_run(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    *,
    now: datetime,
) -> None:
    """Reclaim an aged reservation whose child never wrote a run row."""
    if _missing_run_may_still_start(store, reservation, now=now):
        return
    owned = _continuation_owned_by_run(store, reservation.epic_ref, reservation.run_id)
    if owned is None:
        _record_dispatch_child_died(store, config, reservation, now=now)
    else:
        configured, candidate, checkout_generation = (
            _claim_and_validate_continuation_checkout(
                store,
                config,
                owned,
                boundary="missing-run continuation reconciliation",
            )
        )
        try:
            # The missing-row decision predates this checkout claim. Preserve
            # any child that arrived while the claimant was waiting.
            fresh = store.get(reservation.run_id)
            if fresh is not None:
                _require_claimed_orphan_identity(
                    store,
                    config,
                    reservation,
                    owned,
                    candidate,
                    run=fresh,
                )
                return
            _require_claimed_orphan_identity(
                store,
                config,
                reservation,
                owned,
                candidate,
                run=None,
            )
            _record_dispatch_child_died(store, config, reservation, now=now)
            _reopen_missing_run_continuation(
                store,
                config,
                reservation,
                owned,
                candidate,
                checkout_generation,
            )
        finally:
            release_checkout_claim(configured)
    store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)


def _missing_run_may_still_start(
    store: RunStore, reservation: EpicReservation, *, now: datetime
) -> bool:
    """Whether a reservation with no run row may still be a holder starting up.

    A hand-run ``kew review`` records its process when it reserves (#3208),
    and checks out and builds before its run row exists. That process, probed,
    decides: alive keeps the reservation at any age, provably dead gives it
    back now. Anything nobody can probe is judged by the startup grace.
    """
    from kew.review_occupancy import standalone_reservation_owner

    owner = standalone_reservation_owner(store, reservation.run_id)
    if owner is not None:
        return owner
    return now - reservation.reserved_at <= timedelta(
        seconds=_RESERVATION_STARTUP_GRACE_SECONDS
    )


def _reopen_missing_run_continuation(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    owned: Any,
    candidate: Path,
    checkout_generation: str,
) -> None:
    """Reopen one missing child while its checkout identity remains claimed."""
    changed = store.mark_continuation_awaiting(
        owned.epic_ref,
        owned.item_id,
        owned.original_run_id,
        owned.branch,
        owned.worktree_identity,
        owned.generation,
        reservation.run_id,
        precommit_check=_generation_precommit_check(
            candidate,
            checkout_generation,
            item_id=owned.item_id,
            boundary="missing-run continuation reopening",
        ),
    )
    _require_checkout_generation(
        candidate,
        checkout_generation,
        item_id=owned.item_id,
        boundary="missing-run continuation reopening",
    )
    if not changed:
        return
    _emit_continuation_evidence(
        store,
        "continuation_terminal",
        actor=Actor("policy", "driver_reconcile"),
        phase="terminal",
        epic_ref=owned.epic_ref,
        item_id=owned.item_id,
        original_run_id=owned.original_run_id,
        continuation_run_id=reservation.run_id,
        branch=owned.branch,
        worktree_identity=owned.worktree_identity,
        generation=owned.generation,
        state="awaiting_continuation",
        outcome="crash_before_run_row",
        run_id=reservation.run_id,
        issue_num=reservation.issue_num,
        repo=config.project.repo or None,
    )


def _stage_may_be_reaped(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    stage: RunRecord,
    *,
    now: datetime,
    probe: RunLivenessProbe,
) -> bool:
    """Whether a pending ``review_stage`` row goes on to the ordinary dead-run
    reap (#2459). An ownerless row is settled here instead; an owned one only
    once every run its dead child left pending is reaped first."""
    if not stage.owner_pid:
        _reap_unstarted_stage(store, reservation, stage, now=now)
        return False
    if not _pending_run_is_dead(stage, probe, now=now):
        return False
    return _reap_stage_children(store, config, reservation, stage, now=now, probe=probe)


def _reap_unstarted_stage(
    store: RunStore, reservation: EpicReservation, run: RunRecord, *, now: datetime
) -> None:
    """Reclaim a detached review child that never claimed its row (#2459).

    The tick writes the ``review_stage`` row before it spawns the child, and
    the child records itself as owner the moment it starts — so an ownerless
    row past the startup grace is a child that never ran, the stage's form of
    a dispatch child that never wrote a row. There is no process to stop.
    """
    if now - run.started_at <= timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS):
        return
    reaped = store.finalize_orphaned_pending(
        run.run_id,
        error_category="review_stage_never_started",
        finished_at=now,
        reservation=reservation,
        expected_owner=run,
    )
    fresh = store.get(run.run_id)
    if reaped or (fresh is not None and fresh.status != "pending"):
        store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)


def _reap_stage_children(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    stage: RunRecord,
    *,
    now: datetime,
    probe: RunLivenessProbe,
) -> bool:
    """Reap the runs a dead detached review child left ``pending`` (#2459).

    The reviewer or remediation a stage child starts writes its own row, owned
    by the child, and takes no reservation of its own — the stage row's
    reservation covers it. So when the child dies, nothing else would ever
    revisit those rows. Each is reaped here under the same proof the stage row
    needed; while any is not yet provably dead (a reviewer container still
    inside its startup grace, say) the stage keeps its reservation and slot,
    and this returns False for the next tick to try again.
    """
    if stage.owner_pid is None:
        return True
    for run in store.list_by_item(reservation.item_id):
        if (
            run.run_id == stage.run_id
            or run.status != "pending"
            or run.owner_pid != stage.owner_pid
            or run.owner_start_time != stage.owner_start_time
        ):
            continue
        if not _pending_run_is_dead(run, probe, now=now):
            return False
        _reap_orphaned_run(store, config, reservation, run, now=now)
        fresh = store.get(run.run_id)
        if fresh is not None and fresh.status == "pending":
            return False
    return True


def _terminal_reservation_may_release(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
    continuation_owner: Any | None,
    *,
    now: datetime,
) -> bool:
    """Settle what a terminal run left behind; may its reservation go?"""
    if _preserved_without_continuation(store, reservation.epic_ref, run):
        # The driver died between finalizing this run and recording its
        # continuation; finish the preservation before the release.
        _preserve_dirty_orphan(store, config, reservation, run, now=now)
    if continuation_owner is None:
        # Never release over preserved work that still has no continuation.
        _restore_preserved_continuation(store, config, reservation, run)
        return True
    if run.status == "success":
        # The tracked run reaches success before host publication and
        # before resume_continuation can finalize its generation.
        return False
    return _settle_terminal_continuation(
        store,
        config,
        continuation_owner,
        run,
        reservation=reservation,
    )


def _reconcile_one_reservation(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    *,
    now: datetime,
    probe: RunLivenessProbe,
) -> None:
    """Reconcile one reservation while preserving its generation boundary."""
    from kew.review_occupancy import hand_run_review_running

    # A hand-run ``kew review`` finishes its run row before it publishes
    # (#3208); while its process lives, it gives the reservation back itself.
    if hand_run_review_running(store, reservation):
        return
    continuation_owner = _continuation_owned_by_run(
        store, reservation.epic_ref, reservation.run_id
    )
    exact_run = store.get(reservation.run_id)
    # A claimed continuation can have no child row while its original item row
    # is still the newest row; ordinary dispatches use the generation fallback.
    run = (
        exact_run
        if continuation_owner is not None
        else resolve_reservation_run(store, reservation)
    )
    if run is None:
        _reconcile_missing_run(store, config, reservation, now=now)
        return
    if run.status != "pending":
        if _terminal_reservation_may_release(
            store, config, reservation, run, continuation_owner, now=now
        ):
            store.release_epic_node_item(reservation.item_id, run_id=reservation.run_id)
        return
    if run.run_kind == REVIEW_STAGE_RUN_KIND and not _stage_may_be_reaped(
        store, config, reservation, run, now=now, probe=probe
    ):
        return
    if _pending_run_is_dead(run, probe, now=now):
        # Re-read before reaping so a terminal row written in the probe window
        # keeps its own outcome.
        _reconcile_dead_pending(store, config, reservation, run, now=now)


def _restore_preserved_continuation(
    store: RunStore,
    config: DispatchConfig,
    reservation: EpicReservation,
    run: RunRecord,
) -> None:
    """Record the continuation a crash kept a preserved run from recording.

    :func:`_preserve_claimed_dirty_orphan` terminalizes the run before it
    records the continuation, and releases the reservation last. A driver
    killed between the first two writes leaves an ``orphaned_pending_preserved``
    run, its reservation still held, and nothing offering its work: releasing
    that reservation would fail the node with the work still on disk. So the
    missing record is rebuilt first, from the run's own checkout under its
    claim and generation, exactly as the interrupted pass would have written
    it. A checkout that no longer proves the run's identity, or no longer holds
    the work (cleaned since the crash, #3004 F1), raises the refusal instead,
    and the reconciler holds the node with the reservation kept.
    """
    if run.error_category != "orphaned_pending_preserved":
        return
    if any(
        record.item_id == reservation.item_id
        and (record.original_run_id == run.run_id or record.state != "terminal")
        for record in store.list_continuations(reservation.epic_ref)
    ):
        return
    configured = _recorded_worktree_path(reservation, run, config)
    if configured is None:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(reservation.item_id)} run {run.run_id} preserved dirty "
            "work but recorded no continuation, and has no host checkout"
        )
    claim_checkout(configured)
    try:
        worktree = _validated_recorded_worktree(reservation, run, config)
        if worktree is None:
            raise _PreexistingWorktreeRefusal(
                f"{item_ref(reservation.item_id)} run {run.run_id} preserved "
                f"dirty work but recorded no continuation, and {configured} no "
                "longer proves the run's checkout"
            )
        _, base, generation = _terminal_run_for_preexisting_work(
            store,
            reservation.item_id,
            run.branch,
            worktree,
            config,
            required_run_id=run.run_id,
        )
        if not _checkout_holds_work(config, worktree, run.branch, base_sha=base):
            # Cleaned since the crash: nothing proves where the work went.
            raise _PreexistingWorktreeRefusal(
                f"{item_ref(reservation.item_id)} run {run.run_id} preserved "
                f"dirty work but recorded no continuation, and {configured} no "
                "longer holds it"
            )
        created = store.record_continuation(
            reservation.epic_ref,
            reservation.item_id,
            run.run_id,
            run.branch,
            worktree,
            checkout_generation=generation,
            precommit_check=_generation_precommit_check(
                worktree,
                generation,
                item_id=reservation.item_id,
                boundary="preserved continuation restoring",
            ),
        )
    finally:
        release_checkout_claim(configured)
    _emit_continuation_evidence(
        store,
        "continuation_detected",
        actor=Actor("policy", "driver_reconcile"),
        phase="detection",
        epic_ref=created.epic_ref,
        item_id=created.item_id,
        original_run_id=created.original_run_id,
        continuation_run_id=None,
        branch=created.branch,
        worktree_identity=created.worktree_identity,
        generation=created.generation,
        state=created.state,
        outcome="crash_with_dirty_work",
        run_id=run.run_id,
        issue_num=reservation.issue_num,
        repo=config.project.repo or None,
    )


def _reconcile_reservations(
    name: str,
    store: RunStore,
    config: DispatchConfig,
    *,
    now: datetime,
    liveness: RunLivenessProbe | None = None,
) -> set[str]:
    """Release reservations whose newest run row has already terminated,
    reap reservations whose newest run is ``pending`` but whose owning
    process/container is provably dead (#1041), and reclaim reservations for a
    dispatch child that died before ever writing a run row (see
    `_RESERVATION_STARTUP_GRACE_SECONDS`).

    An atomic reservation is written BEFORE the dispatch child's run row
    exists, so "no runs yet" must NOT normally be treated as terminated —
    only a newest row whose status is no longer "pending" means the
    dispatch finished (successfully or not) and the reservation is stale.
    The exception is a reservation with NO run row at all whose
    `reserved_at` is older than the startup grace: that dispatch never
    happened (or crashed immediately). The reservation is dead, not just
    young — so a terminal ``dispatch_child_died`` diagnostic is persisted
    against its ``run_id`` (naming the spawned executable) BEFORE the
    reservation is released, surfacing the node as FAILED instead of leaving
    it to re-spawn the same broken child every grace window.

    Matching is keyed on ``run_id`` (Task 6): pre-spawn failures (budget
    refusal, dead child) write their terminal row using the reservation's
    ``run_id``, so ``store.get(reservation.run_id)`` finds them directly.
    For normal dispatches whose child generates its own ``run_id``, the
    fallback is ``list_by_item`` filtered to eligible runs belonging to THIS
    reservation generation (started at/after the reservation, or terminal and
    finished at/after it) — this excludes terminal rows from a PRIOR delegation of the
    same item that would otherwise prematurely release the live reservation
    (see :func:`resolve_reservation_run`).

    Released via ``release_epic_node_item`` keyed on BOTH ``item_id`` and the
    reservation's ``run_id`` (generation-conditional): this reconciler now runs
    concurrently with the watch loop and other epic drivers (Task 5), so a
    plain item_id delete could race a live re-claimed reservation another actor
    wrote between our check and our act (#972 review Finding 2). Passing
    ``run_id`` releases only the generation this pass verified terminal — a
    concurrently re-reserved live generation survives. The ``item_id`` key
    still keeps a non-numeric tracker's reservations off the ``0`` sentinel.

    The ``pending``-but-dead case (#1041): a dispatch that dies WITH the epic
    driver that owned it (the driver is killed, or its sandbox container is torn
    down) never writes a terminal row — its ``pending`` row would otherwise pin
    the reservation forever, silently starving admissions. When the ``liveness``
    probe proves the run's process/container is no longer live, the pending run
    is reaped (marked ``killed`` + an attested ``orphaned_run_reaped`` event) and
    its reservation released. Reaping fires ONLY on a positive dead verdict — an
    undeterminable probe (``None``) leaves a possibly-live/mid-startup run alone.

    Returns the set of item ids still reserved after reconciliation.
    """
    store.reap_cancelled_runs()
    probe = liveness if liveness is not None else default_run_liveness(config)
    for reservation in store.list_epic_reservations(name):
        try:
            _reconcile_one_reservation(store, config, reservation, now=now, probe=probe)
        except (_PreexistingWorktreeRefusal, CheckoutAdmissionRefusal) as exc:
            _hold_unreconcilable_reservation(store, config, name, reservation, exc)
            continue
        _clear_settled_reservation_refusal_hold(store, name, reservation)
    return {r.item_id for r in store.list_epic_reservations(name)}


def _clear_settled_reservation_refusal_hold(
    store: RunStore, name: str, reservation: EpicReservation
) -> None:
    """Drop the reconciler's own refusal hold once its reservation settles.

    :func:`_hold_unreconcilable_reservation` keeps the reservation and holds
    the node. When the operator clears what refused it, a later pass settles
    that reservation — the work preserved for continuation, or the node
    released — and nothing else would ever retire the hold: the overlay
    would read the claimed continuation HELD over live work, and the
    remediator refuses to run while any hold stands (#2217 pattern).

    Only a pass that released THIS generation disproves the refusal; one
    that left it in place (a run not yet provably dead) re-tested nothing.
    The release is narrowed to the reconciler's prefix inside the delete
    transaction, so no other writer's hold is touched.
    """
    if name in PSEUDO_EPIC_REFS or any(
        r.item_id == reservation.item_id and r.run_id == reservation.run_id
        for r in store.list_epic_reservations(name)
    ):
        return
    released = store.remove_epic_hold(
        name,
        reservation.item_id,
        only_reason_prefixes=(PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,),
    )
    if released is not None:
        _logger.info(
            "epic %s: node %s un-held — its reservation settled, so the "
            "earlier refusal no longer stands (was: %s)",
            name,
            reservation.item_id,
            released.reason,
        )


def _hold_unreconcilable_reservation(
    store: RunStore,
    config: DispatchConfig,
    name: str,
    reservation: EpicReservation,
    exc: Exception,
) -> None:
    """Hold the one item a checkout refusal is about; never fail the tick.

    A refusal here is a statement about one reservation's checkout, and it was
    reaching ``advance()`` unhandled: a single item whose checkout could not be
    identified took down the whole pass, so no other reservation in the epic
    was reconciled, no node was dispatched, and the next tick repeated it. The
    frontier already converts this exact refusal family into a hold on the node
    it is about (:func:`_reserve_frontier_node`); a reserved node never reaches
    the frontier, so the reconciler has to write that hold itself or the
    refusal has no durable trace at all.

    The hold carries the frontier's own reason prefix, so every reader that
    buckets a stall reason reads this the same way, and
    :func:`_clear_stale_dispatch_refusal_hold` retires it once a dispatch of
    the node disproves it. The reservation is deliberately left alone: kew
    could not establish what the checkout holds, so releasing it here would
    invite a fresh dispatch over bytes nothing has accounted for. The hold is
    what makes that visible — ``kew epic abandon`` is the recovery.

    A PSEUDO-EPIC's reservation gets the log line and no hold. A hold row is
    unique per item and both its removal and the operator's ``kew epic
    unhold`` are scoped by epic ref, so one written under ``delegation`` or
    ``review`` would occupy the item's only hold row under a ref the item's
    real epic can never clear — turning a refusal that needs attention into a
    node nothing can release.
    """
    reason = f"{PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX}: {exc}"
    _logger.error(
        "epic %s: reservation for %s could not be reconciled; holding the node: %s",
        name,
        reservation.item_id,
        exc,
    )
    if name in PSEUDO_EPIC_REFS:
        return
    with suppress(Exception):
        _hold_refused_frontier_node(
            store,
            name,
            reservation.item_id,
            reason,
            provider=provider_from_config(config),
        )


def reconcile_epic_reservations(
    name: str, store: RunStore, config: DispatchConfig, *, now: datetime
) -> set[str]:
    """Public entry to the reservation reconciler (N1).

    The epic driver reconciles its own reservations inside each tick; the
    delegation watch loop parks reservations under a pseudo-epic no driver ever
    ticks, so it reuses this exact reconciler (terminal-run release + dead-child
    reclaim past the startup grace) scoped to the delegation pseudo-epic rather
    than re-implementing the lifecycle. Returns the item ids still reserved.
    """
    return _reconcile_reservations(name, store, config, now=now)


@dataclass
class _Overlays:
    statuses: dict[str, DriverStatus]
    node_facts: dict[str, NodeFacts]
    stuck: list[StuckNode]
    continuation_reason_by_item: dict[str, str] = field(default_factory=dict)
    #: Each item's continuation history, as the continue selector reads it:
    #: what a repeated host death's reason is normalized against (#2960).
    continuation_histories: dict[str, ContinuationHistory] = field(default_factory=dict)
    #: Per-dependency acknowledged no-change dependency effects (I12).
    dependency_effects: dict[str, frozenset[str]] = field(default_factory=dict)
    #: The runs each node's host-restart retry marker supersedes (#2969).
    superseded_runs_by_item: dict[str, frozenset[str]] = field(default_factory=dict)


def _facts_key(item_id: str, provider: str) -> int | str:
    """The key the facts reader expects: the int issue number for the GitHub
    reader, the item id itself for the Linear reader."""
    return int(item_id) if provider == "github" else item_id


def _persisted_pr_url(store: RunStore, item_id: str) -> str | None:
    """Return the newest durable PR URL for a work item, if one exists."""
    runs = [
        run
        for run in store.list_by_item(item_id)
        if isinstance(run.pr_url, str) and run.pr_url
    ]
    newest = _newest_run(runs)
    return newest.pr_url if newest is not None else None


def _dirty_host_restart_outcomes(store: RunStore, record: Any) -> set[str]:
    """The outcomes of every dirty host death of *record*'s preserved work."""
    events = store.list_audit_events(
        limit=None,
        event=HOST_RESTART_DIRTY_EVENT,
        item_id=record.item_id,
        epic_ref=record.epic_ref,
    )
    return {
        str((event.payload or {}).get("outcome"))
        for event in events
        if (event.payload or {}).get("original_run_id") == record.original_run_id
    }


def _continuation_reason_map(
    store: RunStore, name: str, histories: Mapping[str, ContinuationHistory]
) -> dict[str, str]:
    """Return active preserved-work markers for this epic.

    Continuation records are written by dead-owner reconciliation or by the
    resume path. They must be read from the store on every overlay build so a
    long-running poller cannot redispatch dirty preserved work after an
    operator action. A ``continuing`` row normally also has an ordinary epic
    reservation; retaining it here is the fail-closed fallback if that
    reservation is briefly absent.

    An item whose history the continue selector refuses as ambiguous reads
    :data:`CONTINUATION_AMBIGUOUS_REASON`, which names no command: continue
    and abandon would both refuse it (#2960). An owned continuation dominates
    its item's other rows, whatever their order: continue refuses while
    another caller owns one, so the item reads in progress, with no command.
    """
    list_continuations = getattr(store, "list_continuations", None)
    if not callable(list_continuations):
        return {}
    records = list_continuations(name)

    reasons: dict[str, str] = {}
    for record in records:
        item_id = getattr(record, "item_id", None)
        state = getattr(record, "state", None)
        if not isinstance(item_id, str):
            continue
        repeated = state == "awaiting_continuation" and (
            HOST_RESTART_REPEATED in _dirty_host_restart_outcomes(store, record)
        )
        reason = _continuation_row_reason(
            name, item_id, state, histories.get(item_id), repeated=repeated
        )
        if reason is not None:
            reasons[item_id] = reason
    return reasons


def _continuation_row_reason(
    name: str,
    item_id: str,
    state: object,
    history: ContinuationHistory | None,
    *,
    repeated: bool = False,
) -> str | None:
    """One continuation row's waiting reason, or None when it sets none.

    *repeated* marks preserved work the host died under twice (#2964): its
    automatic continuation was spent, so a person continues it."""
    if state == "continuing":
        return "continuation_in_progress — preserved worktree is owned"
    if state != "awaiting_continuation" or (history and history.owned):
        # An owned row of the same item sets the reason, whatever the order.
        return None
    if history and history.ambiguous:
        return CONTINUATION_AMBIGUOUS_REASON
    lead = "awaiting_continuation — preserved worktree"
    if repeated:
        lead = f"{HOST_RESTART_REPEATED_CONTINUATION_REASON}; preserved worktree"
    return (
        f"{lead}; continue with "
        f"`kew epic continue {name} {item_id}` or abandon with "
        f"`kew epic abandon {name} {item_id} --reason TEXT`"
    )


def _continuation_histories(
    store: RunStore, name: str
) -> dict[str, ContinuationHistory]:
    """Each item's continuation history, as the continue selector reads it."""
    if not callable(getattr(store, "list_continuations", None)):
        return {}
    return continuation_histories(store, name)


def _persisted_pr_facts(
    facts: GitHubFactsReader, urls: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Read durable PR identities through the reader's bounded REST seam.

    LinearFactsReader keeps the GitHub reader as a private delegate.  Looking
    for the public seam first and the delegate second lets the driver reconcile
    both production readers without teaching the Linear adapter about driver
    lifecycle policy.  Missing seams and exceptions are unknown facts.
    """
    resolver = getattr(facts, "pr_facts_for_urls", None)
    if not callable(resolver):
        resolver = getattr(getattr(facts, "_gh", None), "pr_facts_for_urls", None)
    if not callable(resolver):
        return None
    try:
        resolved = resolver(urls)
    except Exception:
        return None
    if not isinstance(resolved, dict):
        return None
    return {
        url: payload
        for url, payload in resolved.items()
        if isinstance(url, str) and isinstance(payload, dict)
    }


def _persisted_linear_pr_urls(
    graph: Graph, *, provider: str, store: RunStore
) -> dict[str, str]:
    """Map local Linear nodes to their persisted GitHub PR URLs."""
    url_by_node: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.external:
            continue
        try:
            item_id = validate_item_id(node.id, provider)
        except InvalidItemIdError:
            continue
        url = _persisted_pr_url(store, item_id)
        if url is not None:
            url_by_node[node.id] = url
    return url_by_node


def _settle_merged_linear_nodes(
    graph: Graph,
    url_by_node: dict[str, str],
    pr_by_url: dict[str, dict[str, Any]],
    *,
    provider: str,
    store: RunStore,
    tracker: LinearTracker,
    config: DispatchConfig,
    now: datetime,
) -> list[str]:
    """Perform terminal bookkeeping for merged Linear nodes."""
    merged: list[str] = []
    for node_id, url in url_by_node.items():
        pr = pr_by_url.get(url)
        if pr is None or str(pr.get("state", "")).upper() != "MERGED":
            continue
        item_id = validate_item_id(node_id, provider)
        _kill_reviewer_reservations(store, item_id, config, now=now)
        store.clear_epic_hold_item(item_id)
        if graph.nodes[node_id].status is not NodeStatus.DONE:
            tracker.close(item_id)
            merged.append(node_id)
    return merged


def _reconcile_merged_linear(
    graph: Graph,
    *,
    provider: str,
    facts: GitHubFactsReader,
    store: RunStore,
    tracker: LinearTracker | None,
    config: DispatchConfig,
    now: datetime,
) -> list[str]:
    """Settle Linear items whose persisted GitHub PR is already merged.

    Linear PRs do not carry a ``Closes #N`` linkage, and a merged PR can fall
    out of the active PR listing before the Linear webhook or next graph read
    arrives.  Resolve the run-store URLs first, then perform terminal
    bookkeeping before the review overlay sees the nodes.  Any unavailable or
    partial REST facts return an empty action set: no reviewer is killed and no
    Linear item is closed on an unproven observation.
    """
    if provider != "linear" or tracker is None:
        return []

    url_by_node = _persisted_linear_pr_urls(graph, provider=provider, store=store)
    if not url_by_node:
        return []

    urls = list(dict.fromkeys(url_by_node.values()))
    pr_by_url = _persisted_pr_facts(facts, urls)
    if pr_by_url is None or any(url not in pr_by_url for url in urls):
        return []

    return _settle_merged_linear_nodes(
        graph,
        url_by_node,
        pr_by_url,
        provider=provider,
        store=store,
        tracker=tracker,
        config=config,
        now=now,
    )


def _review_reservation_kinds(store: RunStore) -> dict[str, str]:
    """``item_id`` -> run_kind of a LIVE review reservation (Task 10, #977).

    The reservation row carries no run_kind, so resolve the run row it points at
    (``resolve_reservation_run``) and read its kind. A reservation with no run
    row yet (reviewer mid-startup, before it writes its ``pending`` row) defaults
    to ``adversarial_review`` — the state the node is about to occupy. Only
    review-kind runs are mapped; nothing else parks under REVIEW_EPIC_REF.
    """
    kinds: dict[str, str] = {}
    for res in store.list_epic_reservations(REVIEW_EPIC_REF):
        run = resolve_reservation_run(store, res)
        kind = run.run_kind if run is not None else ADVERSARIAL_REVIEW_RUN_KIND
        if kind in REVIEW_RUN_KINDS:
            kinds[res.item_id] = kind
    return kinds


def _review_overlay_for(
    item_id: str,
    node_facts: NodeFacts,
    *,
    observed_status: NodeStatus,
    config: DispatchConfig,
    store: RunStore,
    review_res_kinds: dict[str, str],
    runs: Sequence[RunRecord] = (),
) -> ReviewOverlay | None:
    """Per-node `ReviewOverlay`, or None when the governed-review stage is off.

    A None overlay keeps the pre-existing IN_REVIEW path (advisory-mode/plain
    epics). When ``[review].adversarial`` is set, the node's live review
    reservation kind + persisted round state place it in the review lifecycle.
    """
    if not config.review.adversarial:
        return None
    latest = store.latest_review_round(item_id)
    return ReviewOverlay(
        enabled=True,
        reserved_kind=review_res_kinds.get(item_id),
        round_count=store.review_round_count(item_id),
        latest_verdict=latest.verdict if latest is not None else None,
        latest_head_sha=latest.head_sha if latest is not None else None,
        current_head_sha=node_facts.head_oid,
        observed_status=observed_status,
        # The SAME reading as the merge gate (#2151): both the chain event and
        # the round's own mark. On the chain event alone the node would read
        # READY here (IN_REVIEW), which the review stage does not drive — so the
        # mark the gate is waiting for would never be retried, and a half-written
        # or pre-v59 acceptance would sit unmergeable forever.
        residuals_accepted=acceptance_on_record(store, item_id, latest),
        attempted=any(run.run_kind in REVIEW_RUN_KINDS for run in runs),
    )


def _continuation_state_by_item(store: RunStore, epic_ref: str) -> dict[str, str]:
    """Return non-terminal continuation states keyed by durable item id.

    Multiple original runs may have records for one item. An awaiting record
    wins over a continuing one so preserved work can never be hidden by a newer
    state from a different generation.
    """
    states: dict[str, str] = {}
    for continuation in store.list_continuations(epic_ref):
        if continuation.state == "terminal":
            continue
        prior = states.get(continuation.item_id)
        if prior == "awaiting_continuation":
            continue
        states[continuation.item_id] = continuation.state
    return states


def _dependency_effect_grants(
    no_change_by_item: dict[str, NoChangeFacts],
) -> dict[str, frozenset[str]]:
    """Per-dependency id: the dependents an acknowledged no-change satisfied.

    This is the ONLY way a zero-diff outcome advances an epic edge (I12). It is
    read from the durable judgment, never from a provider status: nothing was
    written to the provider, so nothing the provider says is evidence that this
    outcome landed.
    """
    return {
        item_id: fact.acked_dependents
        for item_id, fact in no_change_by_item.items()
        if fact.acked_dependents
    }


def _record_effect_intents(
    store: RunStore, fact: NoChangeFacts, dependents: list[str], *, now: datetime
) -> None:
    """Write one durable intent per declared dependent, before anything moves.

    Written once. A dependent that joined the graph after the decision is not
    added to a recorded set — the list a restarted host resumes from must be
    the list the decision was made against; a late arrival waits for the
    judgment to complete and is satisfied by the node's own terminal state.
    """
    if not dependents or fact.effects or not fact.may_enqueue_effects:
        return
    store.enqueue_dependency_effects(  # type: ignore[attr-defined]
        judgment_id=fact.judgment_id, dependents=tuple(dependents), now=now
    )


def _apply_one_dependency_effect(
    store: RunStore, fact: NoChangeFacts, *, now: datetime
) -> None:
    """Acknowledge at most ONE dependent per tick, in the recorded order.

    One at a time is the exactly-once property: the store exposes only the
    lowest unsettled dependent, so a host that died mid-apply resumes the row
    it interrupted (acknowledging what it already did rather than doing it
    twice) and the next dependent stays invisible until this one settles.
    """
    if not fact.may_apply_effect:
        return
    effect = store.next_dependency_effect(fact.judgment_id)  # type: ignore[attr-defined]
    if effect is None:
        return
    if effect.state == EFFECT_INTENT:
        store.begin_dependency_effect(  # type: ignore[attr-defined]
            effect_id=effect.effect_id, now=now
        )
    store.ack_dependency_effect(  # type: ignore[attr-defined]
        effect_id=effect.effect_id,
        now=now,
        detail=(
            f"{effect.dependent_id} may proceed: the no-change outcome for"
            f" {fact.item_id} is authorized and acknowledged"
        ),
    )


def _reconcile_no_change_effects(
    graph: Graph, *, store: RunStore, provider: str, now: datetime
) -> None:
    """Record and advance the per-dependent effects an authorized no-change owes.

    Runs before the overlays so an effect acknowledged by THIS tick is visible
    to this tick's frontier. Nothing here reads provider state: a zero-diff
    outcome writes nothing remote, so only the durable judgment — authorized,
    then acknowledged by every authority — can move an edge. A refusal from the
    store (a converged set that no longer matches, a row another host is
    applying) holds the dependent rather than failing the tick: holding costs a
    tick, and the alternative is advancing an epic on a guess.
    """
    for item_id, fact in read_no_change_facts(store, provider).items():
        if item_id not in graph.nodes:
            continue
        dependents = sorted(graph.dependents_of(item_id), key=item_id_sort_key)
        try:
            _record_effect_intents(store, fact, dependents, now=now)
            _apply_one_dependency_effect(store, fact, now=now)
        except NoChangeError as exc:
            _logger.warning(
                "epic node %s: no-change dependency effect held — %s", item_id, exc
            )


def _no_change_state(
    no_change_by_item: dict[str, NoChangeFacts], item_id: str
) -> str | None:
    fact = no_change_by_item.get(item_id)
    return None if fact is None else fact.state


def _overlay_one_node(
    node: Node,
    *,
    prepared_node_facts: dict[str, NodeFacts],
    provider: str,
    store: RunStore,
    reserved_item_ids: set[str],
    held_reason_by_item: dict[str, str],
    retry_by_item: dict[str, EpicRetry],
    held_at_by_item: dict[str, datetime],
    config: DispatchConfig,
    review_res_kinds: dict[str, str],
    continuation_state_by_item: dict[str, str],
    reconciled_merged_items: set[str],
    continuation_reason_by_item: dict[str, str],
    no_change_by_item: dict[str, NoChangeFacts],
) -> tuple[str, DriverStatus, NodeFacts] | StuckNode:
    try:
        item_id = validate_item_id(node.id, provider)
    except InvalidItemIdError:
        return StuckNode(node_id=node.id, reason="non-int-id", blast_radius=())
    node_facts = prepared_node_facts[node.id]
    if item_id in reconciled_merged_items:
        # The Linear close mutation is the authoritative action for this tick;
        # the adapter may still be holding the pre-close GraphQL snapshot.
        node_facts = replace(
            node_facts,
            state="CLOSED",
            state_reason="COMPLETED",
        )
    runs = store.list_by_item(item_id)
    retry = retry_by_item.get(item_id)
    ds = overlay_status(
        node_facts,
        runs,
        reserved=item_id in reserved_item_ids,
        held_reason=held_reason_by_item.get(item_id),
        held_at=held_at_by_item.get(item_id),
        retried_at=retry.supersedes_before if retry is not None else None,
        superseded_run_ids=retry.superseded_run_ids if retry else frozenset(),
        continuation_state=continuation_state_by_item.get(item_id),
        no_change_state=_no_change_state(no_change_by_item, item_id),
        review=_review_overlay_for(
            item_id,
            node_facts,
            observed_status=node.status,
            config=config,
            store=store,
            review_res_kinds=review_res_kinds,
            runs=runs,
        ),
    )
    stale_publication_continuation = continuation_state_by_item.get(
        item_id
    ) == "awaiting_continuation" and has_successful_open_pr(node_facts, runs)
    if item_id in reconciled_merged_items:
        ds = DriverStatus.DONE
    elif item_id in continuation_reason_by_item and not stale_publication_continuation:
        ds = _continuation_marker_status(ds, continuation_reason_by_item[item_id])
    return node.id, ds, node_facts


def _continuation_marker_status(ds: DriverStatus, reason: str) -> DriverStatus:
    """Apply a continuation marker to the overlaid status *ds*."""
    if ds in (
        DriverStatus.IN_PROGRESS,
        # The continuation is a recovery handle created alongside the
        # completed run.  The run's publication state is authoritative and
        # must not be downgraded to the recovery verb (#1376).
        DriverStatus.AWAITING_PUBLICATION,
    ):
        return ds
    # A continuation marker is an operator-owned recovery gate. It wins
    # over a stale failure/TODO overlay but not over an actively reserved
    # continuation or a same-tick authoritative merge.
    if reason.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON):
        # The host died under this node twice (#2964): it failed, like
        # its clean twin, though its checkout still holds the work.
        return DriverStatus.FAILED
    return DriverStatus.HELD


def _materialize_node_facts(
    graph: Graph, provider: str, facts: GitHubFactsReader
) -> dict[str, NodeFacts]:
    """Read each valid internal node once after the reader's batch prepare."""
    prepared: dict[str, NodeFacts] = {}
    for node in graph.nodes.values():
        if node.external:
            continue
        try:
            item_id = validate_item_id(node.id, provider)
        except InvalidItemIdError:
            continue
        prepared[node.id] = facts.issue_facts(
            _facts_key(item_id, provider)  # type: ignore[arg-type]
        )
    return prepared


def _newest_implementation_run(store: RunStore, item_id: str) -> RunRecord | None:
    """Return the newest non-PR-subject run for branch-link write-back."""
    return next(
        (
            run
            for run in reversed(store.list_by_item(item_id))
            if run.run_kind not in PR_SUBJECT_RUN_KINDS
        ),
        None,
    )


def _write_back_branch_link(
    item_id: str, node_facts: NodeFacts, *, store: RunStore
) -> None:
    """Persist a branch-discovered PR on the newest implementation run."""
    if node_facts.pr_link_source != "branch" or not node_facts.pr_url:
        return
    implementation = _newest_implementation_run(store, item_id)
    if implementation is None:
        return
    from kew.dispatch_core import _persist_pr_url

    _persist_pr_url(store, implementation.run_id, node_facts.pr_url)


def _needs_closing_marker(observed: NodeFacts) -> bool:
    """An open dispatch PR (found by ledger or branch) with no closing reference."""
    return (
        observed.pr_link_source in {"ledger", "branch"}
        and observed.pr_has_closing_ref is False
        and observed.pr_state == "OPEN"
        and observed.pr_number is not None
    )


def _repair_closes_markers(
    node_facts: dict[str, NodeFacts], *, facts: GitHubFactsReader, repo: str
) -> None:
    """Restore missing closing markers on open dispatch PRs."""
    # The repair is GitHub-only (the caller gates on the provider) and needs
    # a `gh` runner; hand-built test readers may carry none.
    run = getattr(facts, "run", None)
    if run is None:
        return
    for item_id, observed in node_facts.items():
        number = observed.pr_number
        if number is not None and _needs_closing_marker(observed):
            _repair_one_marker(item_id, number, run=run, repo=repo)


def _repair_one_marker(item_id: str, number: int, *, run: Any, repo: str) -> None:
    """Read one PR body; append ``Closes #N`` when it carries no keyword."""
    body = _read_pr_body(repo, number, run)
    if body is None:
        _logger.warning(
            "#%s: could not inspect PR #%s closing keyword", item_id, number
        )
        return
    if "<!-- kew:no-autoclose -->" in body:
        return
    repaired = ensure_closes_marker(body, item_id)
    if repaired == body:
        return
    result = run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/pulls/{number}",
            "-f",
            f"body={repaired}",
        ],
        check=False,
    )
    if result.returncode != 0:
        _logger.warning(
            "#%s: could not restore the closing keyword on PR #%s", item_id, number
        )
        return
    _logger.warning(
        "#%s: PR #%s lacked a closing keyword; re-appended", item_id, number
    )


def _read_pr_body(repo: str, number: int, run: Any) -> str | None:
    """Read one PR body through the REST API."""
    endpoint = f"repos/{repo}/pulls/{number}"
    try:
        result = run(["gh", "api", "--method", "GET", endpoint], check=False)
        payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
    except (AttributeError, json.JSONDecodeError, TypeError, OSError) as exc:
        # None means "not observable"; the caller skips the repair, so the
        # reason has to be said here or the skip is silent (silence gate).
        _logger.warning("PR #%s body unreadable: %s", number, exc)
        return None
    if not isinstance(payload, dict) or "body" not in payload:
        _logger.warning("PR #%s body missing from the API response", number)
        return None
    body = payload["body"]
    return "" if body is None else body if isinstance(body, str) else None


def _clear_closed_pr_artifacts(
    epic_ref: str,
    item_id: str,
    node_facts: NodeFacts,
    *,
    facts: GitHubFactsReader,
    store: RunStore,
    pending_review_label: str,
) -> None:
    """Clear only stale review-label and ``ci_pending`` state."""
    hold = next(
        (h for h in store.list_epic_holds(epic_ref) if h.item_id == item_id),
        None,
    )
    has_label = pending_review_label in node_facts.labels
    has_ci_hold = hold is not None and is_ci_pending_hold(hold.reason)
    if not (has_label or has_ci_hold):
        return
    if has_label:
        facts.run(
            [
                "gh",
                "issue",
                "edit",
                item_id,
                "--repo",
                facts.repo,
                "--remove-label",
                pending_review_label,
            ],
            check=False,
        )
    if has_ci_hold:
        store.remove_epic_hold(epic_ref, item_id)


def _reconcile_github_links(
    epic_ref: str,
    node_facts: dict[str, NodeFacts],
    *,
    provider: str,
    facts: GitHubFactsReader,
    store: RunStore,
    config: DispatchConfig,
) -> None:
    """Apply tick-only branch write-back and closed-PR cleanup."""
    if provider != "github":
        return
    for node_id, observed in node_facts.items():
        _write_back_branch_link(node_id, observed, store=store)
        _forget_finished_review_checkouts(observed, config, store)
        if observed.pr_state == "CLOSED":
            _clear_closed_pr_artifacts(
                epic_ref,
                node_id,
                observed,
                facts=facts,
                store=store,
                pending_review_label=config.labels.pr_pending_review,
            )


def _release_moved_head_refusals(
    epic_ref: str,
    node_facts: dict[str, NodeFacts],
    *,
    provider: str,
    store: RunStore,
) -> None:
    """Release each ``admission_refused@<head>`` hold whose PR head has moved (#3193).

    The hold was about that head: a new one is new evidence, so it is eligible
    once — a refusal on it holds again under its own head. The delete is
    narrowed to the exact head the reason names inside the store transaction,
    so a hold another writer upserted for the new head in between survives.
    A hold with no head, or a PR head the tick could not read, is left alone.
    """
    holds = {hold.item_id: hold for hold in store.list_epic_holds(epic_ref)}
    for node_id, observed in node_facts.items():
        try:
            item_id = validate_item_id(node_id, provider)
        except InvalidItemIdError:
            continue
        hold = holds.get(item_id)
        refused = admission_refused_head(hold.reason if hold else None)
        current = observed.head_oid
        if refused is None or not current or current.startswith(refused):
            continue
        released = store.remove_epic_hold(
            epic_ref,
            item_id,
            only_reason_prefixes=(f"{admission_refused_reason_prefix(refused)}:",),
        )
        if released is not None:
            _logger.info(
                "epic %s: node %s un-held — the PR head moved from %s to %s (was: %s)",
                epic_ref,
                item_id,
                refused,
                current[:12],
                released.reason,
            )


def _forget_finished_review_checkouts(
    observed: NodeFacts, config: DispatchConfig, store: RunStore
) -> None:
    """Remove the review checkouts of a PR this tick saw merged or closed (#2961).

    Nothing reviews that PR again, so its live checkout and every preserved
    copy are disk with no reader — except, for a PR closed unmerged, the copy
    each deferred publication of it names (#3186). Best-effort: a
    removal that fails is logged and the next sighting tries again; it never
    fails the tick.
    """
    if observed.pr_number is None or observed.pr_state not in {"MERGED", "CLOSED"}:
        return
    from kew.review_checkout import forget_review_checkouts

    try:
        removed = forget_review_checkouts(
            config,
            observed.pr_number,
            store=store,
            merged=observed.pr_state == "MERGED",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never the tick
        # ``discard_checkout`` runs bounded git subprocesses (one can time out,
        # ``TimeoutExpired``) and retires the checkout identity (which raises
        # ``RuntimeError``): each is logged like a failed removal (#3186).
        _logger.warning(
            "could not remove the review checkouts of PR #%s: %s",
            observed.pr_number,
            exc,
        )
        return
    if removed:
        _logger.info(
            "removed %d review checkout(s) of %s PR #%s",
            len(removed),
            observed.pr_state.lower(),
            observed.pr_number,
        )


#: The stuck reason for a node whose linked PR closed without merging (#2689).
CLOSED_UNMERGED_REASON_PREFIX = "closed-unmerged"


def _closed_unmerged_stuck(
    graph: Graph,
    node_id: str,
    ds: DriverStatus,
    observed: NodeFacts,
    *,
    store: RunStore,
    retry: EpicRetry | None,
) -> StuckNode | None:
    """A ready node whose linked PR closed unmerged is stuck until restarted.

    The overlay reads such a node TODO (#753), so `kew epic status` listed it
    ready while the tick's dispatch was refused, silently, by the closed PR's
    branch still on the remote (#2689). Both read this one rule instead, so
    they agree, and the reason names the PR. ``kew epic retry`` is the restart:
    a retry marker newer than the newest implementation run says the operator
    wants the node started over, and the node re-enters the frontier. Its new
    PR then outranks the closed one (state before number, ``resolve_linked_pr``).
    The runs a host-restart marker binds are dropped before the newest is
    picked (#2969): a superseded run whose clock reads ahead must not stand in
    for the replacement whose PR closed, or the host marker would read as the
    operator's restart.
    """
    if ds not in (DriverStatus.TODO, DriverStatus.QUEUED):
        return None
    if observed.pr_number is None or observed.pr_state != "CLOSED":
        return None
    if retry is not None:
        newest = _newest_run(
            current_attempt_runs(store.list_by_item(node_id), retry.superseded_run_ids)
        )
        if newest is None or retry.supersedes_before > newest.started_at:
            return None
    return StuckNode(
        node_id=node_id,
        reason=(
            f"{CLOSED_UNMERGED_REASON_PREFIX}: PR #{observed.pr_number} "
            "closed without merging"
        ),
        blast_radius=tuple(sorted(graph.transitive_dependents(node_id))),
    )


def _build_overlays(
    graph: Graph,
    *,
    name: str,
    provider: str,
    store: RunStore,
    facts: GitHubFactsReader,
    reserved_item_ids: set[str],
    held_reason_by_item: dict[str, str],
    retry_by_item: dict[str, EpicRetry],
    config: DispatchConfig,
    reconciled_merged_items: set[str] | None = None,
    reconcile_links: bool = False,
) -> _Overlays:
    """Compute each epic-member node's `DriverStatus` + raw `NodeFacts`.

    External boundary nodes (`Node.external`) carry no facts of their own —
    they keep whatever `NodeStatus` `build_graph` already resolved for
    them, so they are skipped here (see `_frontier_status`'s default).
    """
    statuses: dict[str, DriverStatus] = {}
    node_facts: dict[str, NodeFacts] = {}
    stuck: list[StuckNode] = []
    reconciled_merged_items = reconciled_merged_items or set()
    prepare = getattr(facts, "prepare", None)
    if callable(prepare):
        prepare([node.id for node in graph.nodes.values() if not node.external])
    # Materialize provider facts before reading continuation overlays. Facts
    # adapters reconcile awaiting_publication from the same remote observation;
    # taking continuation snapshots first would retain the just-terminalized
    # recovery handle for this tick and still mask an open/merged PR.
    prepared_node_facts = _materialize_node_facts(graph, provider, facts)
    if reconcile_links:
        _reconcile_github_links(
            name,
            prepared_node_facts,
            provider=provider,
            facts=facts,
            store=store,
            config=config,
        )
        _release_moved_head_refusals(
            name, prepared_node_facts, provider=provider, store=store
        )
        held_reason_by_item = _holds_reason_map(store, name)
    held_at_by_item = _holds_held_at_map(store, name)
    review_res_kinds = _review_reservation_kinds(store)
    continuation_state_by_item = _continuation_state_by_item(store, name)
    histories = _continuation_histories(store, name)
    continuation_reason_by_item = _continuation_reason_map(store, name, histories)
    no_change_by_item = read_no_change_facts(store, provider)
    for node in graph.nodes.values():
        if node.external:
            continue
        outcome = _overlay_one_node(
            node,
            prepared_node_facts=prepared_node_facts,
            provider=provider,
            store=store,
            reserved_item_ids=reserved_item_ids,
            held_reason_by_item=held_reason_by_item,
            retry_by_item=retry_by_item,
            held_at_by_item=held_at_by_item,
            config=config,
            review_res_kinds=review_res_kinds,
            continuation_state_by_item=continuation_state_by_item,
            reconciled_merged_items=reconciled_merged_items,
            continuation_reason_by_item=continuation_reason_by_item,
            no_change_by_item=no_change_by_item,
        )
        if isinstance(outcome, StuckNode):
            stuck.append(outcome)
            continue
        nid, ds, nf = outcome
        statuses[nid] = ds
        node_facts[nid] = nf
        closed = _closed_unmerged_stuck(
            graph,
            nid,
            ds,
            nf,
            store=store,
            retry=retry_by_item.get(nid),
        )
        if closed is not None:
            stuck.append(closed)
    return _Overlays(
        statuses=statuses,
        node_facts=node_facts,
        stuck=stuck,
        continuation_reason_by_item=continuation_reason_by_item,
        continuation_histories=histories,
        dependency_effects=_dependency_effect_grants(no_change_by_item),
        superseded_runs_by_item=_superseded_runs(retry_by_item),
    )


def _superseded_runs(
    retry_by_item: Mapping[str, EpicRetry],
) -> dict[str, frozenset[str]]:
    """The runs each node's host-restart retry marker is bound to (#2969)."""
    return {
        item_id: retry.superseded_run_ids
        for item_id, retry in retry_by_item.items()
        if retry.superseded_run_ids
    }


@dataclass
class _ReviewRaceOutcome:
    """What `_handle_review_races` did this tick (F6/F7, #977).

    ``merged_unreviewed`` node ids saw their PR merged out-of-band while a
    reviewer was live → terminal-done (folded into the tick's ``closed``).
    ``held_pr_closed`` node ids had their PR closed/superseded mid-review → HELD
    review_pr_closed. Both lists' reviewers were killed + their reservations
    released.
    """

    merged_unreviewed: list[str]
    held_pr_closed: list[str]


def _hold_review_node(
    store: RunStore, epic_ref: str, item_id: str, reason: str, *, provider: str
) -> None:
    """Hold a node via the provider-appropriate façade (mirrors merge._hold_node)."""
    if provider == "github":
        store.hold_epic_node(epic_ref, item_id, reason)
    else:
        store.hold_epic_node_item(epic_ref, item_id, reason)


def _kill_reviewer_reservations(
    store: RunStore, item_id: str, config: DispatchConfig, *, now: datetime
) -> None:
    """Kill + reconcile every LIVE review reservation for *item_id* (F6/F7).

    Writes a terminal ``killed`` run row under each reservation's ``run_id``
    (run_kind preserved, so the F4 status filter still ignores it) and releases
    the reservation generation-conditionally (``run_id``-keyed, #972 semantics)
    so a concurrently re-reserved live generation can't be clobbered. This is the
    "reviewer killed + reconciled" side of the human-action races; process-level
    SIGKILL of a contained reviewer hooks here once Group C wires the launcher.

    A live hand-run ``kew review`` is not the drive's reviewer to kill (#3208):
    it is left to finish and gives its reservation back itself.
    """
    from kew.review_occupancy import hand_run_review_running

    for res in store.list_epic_reservations(REVIEW_EPIC_REF):
        if res.item_id != item_id or hand_run_review_running(store, res):
            continue
        run = resolve_reservation_run(store, res)
        store.save(_killed_reviewer_row(run, res, config, now=now))
        store.release_epic_node_item(res.item_id, run_id=res.run_id)


def _killed_reviewer_row(
    run: RunRecord | None,
    res: EpicReservation,
    config: DispatchConfig,
    *,
    now: datetime,
) -> RunRecord:
    """The terminal ``killed`` row for a reviewer cut short mid-review (F6/F7).

    Overwrites the terminal fields ON the live row when one is keyed to THIS
    reservation, rather than constructing a fresh record: a reviewer killed
    mid-review really did spend tokens, and a fresh record would zero the
    token/cost columns and null ``planned_cost_usd``, dropping the run's spend
    from the ledger and its cost pair from the planning-accuracy ratio (#1092).

    Only the exactly-``run_id``-keyed row is carried forward.
    ``resolve_reservation_run`` may fall back to the newest ``item_id`` row of
    the generation — a DIFFERENT run, whose telemetry copied under ``res.run_id``
    would double-count that spend. Nothing else is inherited from a fallback row
    either: the synthetic row is a reviewer kill by construction, so it is
    stamped ``ADVERSARIAL_REVIEW_RUN_KIND`` rather than borrowing (say) a
    ``dispatch`` kind that would land it in the wrong scorecard bucket.
    """
    from kew.run_store import RunRecord

    live = run if run is not None and run.run_id == res.run_id else None
    row = live or RunRecord(
        run_id=res.run_id,
        issue_num=res.issue_num,
        item_id=res.item_id,
        branch="",
        status="killed",
        exit_code=143,
        started_at=res.reserved_at,
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
    )
    row.branch = f"{config.branches.prefix}{branch_slug(res.item_id)}"
    row.status = "killed"
    row.exit_code = 143
    row.finished_at = now
    row.duration_seconds = (now - row.started_at).total_seconds()
    row.error_category = "review_out_of_band"
    row.title = "reviewer killed: human action mid-review"
    row.repo = config.project.repo or None
    return row


def _attest_merged_unreviewed(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    repo: str,
    pr_number: int | None,
    head_sha: str | None,
) -> None:
    """Attest an out-of-band merge of a node whose reviewer was still live (F6).

    The chain never goes silent on a bypassed gate — it records it as the
    canonical ``adversarial_review_verdict`` (Task 13) with verdict
    MERGED_UNREVIEWED and actor out_of_band, carrying the full attested payload
    (policy block, run_records spend, join keys) like every other verdict shape.
    """
    latest = store.latest_review_round(item_id)
    emit_review_verdict(
        store,
        config.review,
        pr_number=pr_number,
        item_id=item_id,
        head_sha=head_sha,
        review_round=latest.round if latest is not None else 0,
        verdict=VERDICT_MERGED_UNREVIEWED,
        sidecar_digest=latest.sidecar_digest if latest is not None else None,
        repo=repo,
        actor=Actor("human", "out_of_band"),
    )


def _review_race_for_node(
    node_id: str,
    item_id: str,
    *,
    provider: str,
    facts: GitHubFactsReader,
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    repo: str,
    now: datetime,
) -> str | None:
    """Resolve one live-reviewer node's human-action race, or None if no race.

    Keyed on a POSITIVELY-READ PR/issue state — never a state-shape inference,
    and never a failed read. A transient `gh` hiccup must not look like a
    vanished PR and supersede a live reviewer (#980 Finding 1, the #975
    concurrent-state-change false-positive class), so both reads fail toward
    "keep the reviewer, retry next tick":

    - issue read fails (``fetch_issue_or_none`` → None) → return None (survive);
    - issue read OK but no linked PR ref → *confirmed* vanished reference (the
      #753 wedge) → superseded (kill + HELD review_pr_closed);
    - PR read fails (empty ``pr_facts`` / no ``state``) → return None (survive);
    - PR OPEN → reviewer legitimately in flight → return None (survive);
    - PR MERGED → out-of-band merge (kill + attest MERGED_UNREVIEWED);
    - PR positively CLOSED → superseded (kill + HELD review_pr_closed, so the
      node never derives back to TODO).

    Returns "merged" / "pr_closed" / None.
    """
    fetch_issue = getattr(facts, "fetch_issue_or_none", None)
    if not callable(fetch_issue):
        # LinearFactsReader has no GitHub issue endpoint. Its persisted-PR REST
        # reconciliation runs before this race pass; without that batch, the
        # current PR state is unknown and the reviewer must survive.
        return None
    issue = fetch_issue(_facts_key(item_id, provider))
    if issue is None:
        return None  # transient issue-read failure — keep the reviewer, retry.
    branch_prefix = getattr(facts, "_branch_prefix", config.branches.prefix)
    branch = f"{branch_prefix}{branch_slug(item_id)}"
    closing_refs = issue.get("closedByPullRequestsReferences") or []
    link = resolve_linked_pr(
        item_id,
        branch,
        store=store,
        batch=facts,
        closing_refs=closing_refs,
    )
    if link.number is None:
        # The issue read succeeded and every resolver source returned empty.
        _kill_reviewer_reservations(store, item_id, config, now=now)
        _hold_review_node(
            store, epic_ref, item_id, "review_pr_closed", provider=provider
        )
        return "pr_closed"
    pr_number = link.number
    pr = link.pr
    pr_state = pr["state"]
    if pr_state == "UNKNOWN":
        return None
    if pr_state == "OPEN":
        return None  # reviewer legitimately live — survive the pass.
    if pr_state == "MERGED":
        _kill_reviewer_reservations(store, item_id, config, now=now)
        _attest_merged_unreviewed(
            store,
            config,
            item_id=item_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=str(pr["headRefOid"]) if pr.get("headRefOid") else None,
        )
        return "merged"
    # Positively-read CLOSED (the only remaining PR state): superseded + HELD so
    # a live reviewer's node cannot wedge back to TODO (#753).
    _kill_reviewer_reservations(store, item_id, config, now=now)
    _hold_review_node(store, epic_ref, item_id, "review_pr_closed", provider=provider)
    return "pr_closed"


def _handle_review_races(
    members: list[str],
    *,
    epic_ref: str,
    provider: str,
    facts: GitHubFactsReader,
    store: RunStore,
    config: DispatchConfig,
    repo: str,
    now: datetime,
) -> _ReviewRaceOutcome:
    """React to human actions taken while a reviewer/remediator is live (F6/F7).

    Iterates the epic's nodes that hold a LIVE review reservation — the
    authoritative "reviewer in flight" signal, NOT the derived node status (an
    out-of-band merge auto-closes the issue, so the node would already read DONE,
    not ADVERSARIAL_REVIEW). Each node's race is resolved by the PR's live state.
    """
    from kew.review_occupancy import standalone_reservation

    # A hand-run ``kew review`` is not the drive's reviewer to kill (#3208): it
    # gives its reservation back itself, or the reconciler proves it dead. Its
    # holder record commits with its reservation, so no reservation is ever
    # read here as a drive reviewer's for want of it.
    live_items = {
        res.item_id
        for res in store.list_epic_reservations(REVIEW_EPIC_REF)
        if standalone_reservation(store, res.run_id) is None
    }
    if not live_items:
        return _ReviewRaceOutcome([], [])
    merged: list[str] = []
    held: list[str] = []
    for node_id in members:
        try:
            item_id = validate_item_id(node_id, provider)
        except InvalidItemIdError:
            continue
        if item_id not in live_items:
            continue
        race = _review_race_for_node(
            node_id,
            item_id,
            provider=provider,
            facts=facts,
            store=store,
            config=config,
            epic_ref=epic_ref,
            repo=repo,
            now=now,
        )
        if race == "merged":
            merged.append(node_id)
        elif race == "pr_closed":
            held.append(node_id)
    return _ReviewRaceOutcome(merged, held)


def _merge_verifier(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    keepalive: Callable[[], None] | None,
) -> MergeVerifier | None:
    """The serial merge queue's verifier, or None when the epic hasn't opted in.

    ``repo_root`` must be the git toplevel for the same reason the review stage
    passes it: the scratch clone is cut from it (#1002). *keepalive* refreshes
    this driver's lock heartbeat while a verification blocks the tick — without it
    a verification longer than ``HEARTBEAT_STALE_SECONDS`` marks a healthy driver
    stale and invites a concurrent second driver of the same epic.
    """
    if not config.epic.merge_queue:
        return None
    return merge_queue_verifier(
        config,
        store,
        epic_ref=epic_ref,
        repo=repo,
        base=base,
        repo_root=_git_repo_root(project_root()),
        keepalive=keepalive,
    )


def _hold_excluded_epic_conflicts(
    name: str, overlays: _Overlays, *, config: DispatchConfig, store: RunStore
) -> None:
    """Hold the conflicting READY PRs of an epic the standing merge rule
    excludes (:func:`hold_excluded_conflicts`); any other epic is untouched."""
    rule = config.drive.merge
    if rule.enabled and excluded_by_rule(name, rule):
        hold_excluded_conflicts(
            overlays.statuses, overlays.node_facts, epic_ref=name, store=store
        )


def _run_merge_pass(
    name: str,
    statuses: dict[str, DriverStatus],
    *,
    repo: str,
    base: str,
    facts: GitHubFactsReader,
    approvers: list[str],
    store: RunStore,
    tracker: LinearTracker | None,
    blocking_review: bool,
    pending_review_label: str,
    gated_checks: Sequence[str],
    merge_verify: MergeVerifier | None,
    spent_facts: Collection[str] = (),
    observed: Mapping[str, NodeFacts] | None = None,
) -> list[MergeVerdict]:
    """Merge-pass candidates are every node currently IN_REVIEW or HELD.

    In a review-enabled BLOCKING epic (*blocking_review*), the merge pass also
    applies the governed-review invariant (N1) to each candidate — HELD stays a
    candidate on purpose, so an approved+green HELD node with a non-READY verdict
    is caught by the gate rather than auto-merged.

    ``spent_facts`` are the nodes whose head the review stage's merge-main step
    just moved (#2149): this tick's facts for them describe the branch BEFORE the
    step, so the pass has nothing it can honestly classify them on and skips them
    for this one tick. It re-reads them, fresh, on the next one.
    """
    candidates = sorted(
        (
            nid
            for nid, ds in statuses.items()
            if ds in (DriverStatus.IN_REVIEW, DriverStatus.HELD)
            and nid not in spent_facts
        ),
        key=item_id_sort_key,
    )
    # The sort is what makes the merge queue (#2148) serial *in a defined order*:
    # `merge_pass` classifies — and so verifies and merges — in this order, one
    # candidate at a time.

    return merge_pass(
        candidates,
        repo=repo,
        base=base,
        epic_ref=name,
        reader=facts,
        approvers=approvers,
        store=store,
        tracker=tracker,
        blocking_review=blocking_review,
        pending_review_label=pending_review_label,
        gated_checks=gated_checks,
        merge_verify=merge_verify,
        observed=observed,
    )


def _frontier_status(
    graph: Graph, statuses: dict[str, DriverStatus]
) -> dict[str, NodeStatus]:
    """The dependency-satisfaction view: DONE/TODO/QUEUED from the overlay, else
    BLOCKED (a non-DONE, non-TODO sentinel) — never the node's live
    `DriverStatus` verbatim, so CANCELLED/IN_REVIEW/etc. can never satisfy
    a dependent. Nodes with no overlay entry (external boundary nodes,
    and non-int epic members) keep their existing graph status.
    """
    result: dict[str, NodeStatus] = {}
    for nid, node in graph.nodes.items():
        ds = statuses.get(nid)
        result[nid] = (
            node.status
            if ds is None
            else _FRONTIER_STATUS_MAP.get(ds, NodeStatus.BLOCKED)
        )
    return result


def _overlay_graph(graph: Graph, frontier_status: dict[str, NodeStatus]) -> Graph:
    nodes = [replace(n, status=frontier_status[n.id]) for n in graph.nodes.values()]
    return Graph(nodes, graph.edges)


def _is_stale(timestamp_iso: str | None, *, now: datetime, stall_days: int) -> bool:
    """True iff `timestamp_iso` is more than `stall_days` old, as of `now`.

    Tolerates a missing/unparseable timestamp by treating it as not stale
    (fail toward "still progressing", never toward a false STALLED)."""
    if not timestamp_iso:
        return False
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        return False
    return (now - ts).days >= stall_days


def _dispatch_exclusions(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    *,
    in_flight: list[str],
    reserved_item_ids: set[str],
    already_stuck_ids: set[str],
) -> set[str]:
    """Node ids that must never be handed to `compute_frontier`/dispatch."""
    excluded = set(in_flight)
    excluded |= {nid for nid, ds in statuses.items() if ds in _NON_FRONTIER_STATUSES}
    # Reserved item ids ARE node ids (the graph is keyed on the item id).
    excluded |= reserved_item_ids
    excluded |= already_stuck_ids
    # External boundary nodes (out-of-epic deps) are observed for
    # dependency-satisfaction only — never dispatched by this epic's driver.
    excluded |= {nid for nid, n in graph.nodes.items() if n.external}
    return excluded


@dataclass(frozen=True)
class _DispatchBudget:
    """The tick's new-dispatch budget PLUS the three limiter values that floored
    it, so an admission-held node can name the constraint that actually bound it
    (#1041 review): the per-epic worker budget, the fleet worker cap, or this
    epic's fair share — not always the worker cap."""

    per_epic: int
    fleet_available: int
    fair_remaining: int
    fleet_cap: int
    fair_share: int
    active_epics: int
    live_runs: int
    cap_disabled: bool

    @property
    def budget(self) -> int:
        """The tightest of the three limiters, floored at zero (never negative);
        the per-epic budget alone when the fleet cap is disabled."""
        if self.cap_disabled:
            return self.per_epic
        return max(0, min(self.per_epic, self.fleet_available, self.fair_remaining))

    def admission_hold_reason(self) -> str:
        """Name the tightest limiter (with its values) that floored the budget,
        so a held node reports the REAL constraint. Ties prefer the fleet worker
        cap, then fair share, then the per-epic budget."""
        if self.cap_disabled:
            return f"{ADMISSION_HELD_PREFIX}: per-epic worker budget exhausted"
        candidates = [
            (
                self.fleet_available,
                f"worker cap {self.fleet_cap} consumed ({self.live_runs} live run(s))",
            ),
            (
                self.fair_remaining,
                f"fair share {self.fair_share} of {self.active_epics} "
                "active epic(s) reached",
            ),
            (self.per_epic, "per-epic worker budget exhausted"),
        ]
        _, label = min(candidates, key=lambda c: c[0])
        return f"{ADMISSION_HELD_PREFIX}: {label}"


def _compute_dispatch_budget(
    store: RunStore,
    *,
    epic_ref: str,
    fleet_cap: int,
    per_epic_budget: int,
    liveness: RunLivenessProbe | None = None,
) -> _DispatchBudget:
    """New-dispatch budget for this tick under the fleet-wide worker cap (F4/#864).

    ``fleet_cap`` is ``[parallel] max_workers`` shared across ALL
    concurrently-driven epics: a node reserved by any epic consumes one slot
    (reservations are counted fleet-wide and deduped by item id — see
    ``RunStore.live_run_count``). ``fleet_cap <= 0`` disables the cap, leaving
    only the per-epic budget (mirrors ``dispatch_core``'s unbounded contract).

    Fairness: when more than one epic is concurrently CONTENDING, no epic may
    grow its own footprint past its fair share ``ceil(fleet_cap /
    active_epics)`` — so a single epic can never hold every slot while another
    contending epic still has ready nodes; the lone contender keeps the whole
    cap. The returned budget is the tightest of the per-epic budget, the free
    fleet slots, and this epic's remaining fair share (floored at zero, never
    negative).

    The contender count spans every epic holding a live reservation AND every
    registered-but-not-yet-terminal epic (``RunStore.contending_epic_refs`` —
    seeded by ``kew epic plan``). Counting the cold-start siblings is what
    closes the F4 starvation gap (#864): without them the first epic to tick
    would see itself as the only active epic, take the whole cap, and
    permanently starve a ready sibling that never acquires a reservation to
    become visible. Both halves are probed with ``liveness`` (#2094) so a
    stale epic's leaked reservation cannot put it back in the denominator the
    contention age-out just took it out of.

    Best-effort by design: two epics ticking concurrently could both read the
    same free-slot count here — the authoritative last-slot guard is the atomic
    ``dispatch_core.try_admit_run`` insert. This gate just keeps an epic from
    spawning children that would only requeue, and enforces fairness up front.
    """
    if fleet_cap <= 0:
        return _DispatchBudget(
            per_epic=per_epic_budget,
            fleet_available=per_epic_budget,
            fair_remaining=per_epic_budget,
            fleet_cap=fleet_cap,
            fair_share=per_epic_budget,
            active_epics=1,
            live_runs=0,
            cap_disabled=True,
        )
    # Owner-liveness-aware (#1059, #2094): a hard-killed run (breaker
    # process-group kill, driver restart, crash) orphans its ``pending`` row and
    # the reservation backing it — the tick reconciler above reaps only THIS
    # epic's, so an epic abandoned mid-drive keeps both forever. Unprobed, that
    # leak would steal a fleet slot AND keep its epic in the fairness
    # denominator. Every read below takes the same probe so no one of them can
    # re-admit a contender the others just retired; a row that is not provably
    # dead still counts (prove dead, never infer).
    by_epic = store.live_reservation_counts_by_epic(liveness=liveness)
    live_runs = store.live_run_count(liveness=liveness)
    fleet_available = fleet_cap - live_runs
    contenders = (
        store.contending_epic_refs(liveness=liveness) | set(by_epic) | {epic_ref}
    )
    active_epics = len(contenders)
    fair_share = -(-fleet_cap // active_epics)  # ceil(fleet_cap / active_epics)
    fair_remaining = fair_share - by_epic.get(epic_ref, 0)
    return _DispatchBudget(
        per_epic=per_epic_budget,
        fleet_available=fleet_available,
        fair_remaining=fair_remaining,
        fleet_cap=fleet_cap,
        fair_share=fair_share,
        active_epics=active_epics,
        live_runs=live_runs,
        cap_disabled=False,
    )


def _fleet_dispatch_budget(
    store: RunStore,
    *,
    epic_ref: str,
    fleet_cap: int,
    per_epic_budget: int,
) -> int:
    """New-dispatch budget as a plain int (thin wrapper over
    :func:`_compute_dispatch_budget`; see it for the fleet-cap/fairness contract)."""
    return _compute_dispatch_budget(
        store, epic_ref=epic_ref, fleet_cap=fleet_cap, per_epic_budget=per_epic_budget
    ).budget


def _review_concurrency(
    store: RunStore,
    *,
    epic_ref: str,
    max_parallel: int,
    fleet_cap: int,
    liveness: RunLivenessProbe | None = None,
) -> int:
    """How many governed reviews/remediations this tick may run at once (#2110).

    ``--max-parallel`` bounds the epic and the fleet cap (with its fair share,
    :func:`_compute_dispatch_budget`) bounds the machine, read once on the tick
    thread before any worker starts. The floor is one: a full fleet still lets
    the stage attempt its nodes in turn, where each reviewer's atomic
    under-cap reservation refuses and retries next tick, as before. A
    ``max_parallel`` of 0 is admission closed by ``kew drive`` (#2616): no
    review or remediation starts, and the ones already running are still read.
    """
    if max_parallel <= 0:
        return 0
    if max_parallel <= 1:
        return 1
    return max(
        1,
        _compute_dispatch_budget(
            store,
            epic_ref=epic_ref,
            fleet_cap=fleet_cap,
            per_epic_budget=max_parallel,
            liveness=liveness,
        ).budget,
    )


class _PreexistingWorktreeRefusal(RuntimeError):
    """A dispatch target cannot be trusted without risking existing work."""


def _trusted_preexisting_worktree(
    configured: Path,
    candidate: Path,
    root: Path,
    expected_branch: str,
    config: DispatchConfig | None = None,
) -> bool:
    """Prove that a pre-existing path is the configured Git worktree."""
    return (
        configured == candidate
        and candidate.is_dir()
        and candidate.is_relative_to(root)
        and _git_worktree_matches(candidate, expected_branch, config=config)
    )


def _preexisting_continuation(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    expected_branch: str,
    candidate: Path,
) -> ContinuationRecord | None:
    """Return one exact continuation identity, refusing ambiguity."""
    records = [
        record
        for record in store.list_continuations(epic_ref)
        if _current_continuation(store, record, item_id)
    ]
    matching = [
        record
        for record in records
        if record.branch == expected_branch
        and continuation_checkout_matches(
            record.worktree_identity,
            candidate,
            recorded_owner=(record.item_id, record.branch),
            candidate_owner=(item_id, expected_branch),
        )
    ]
    if records and (len(records) != 1 or len(matching) != 1):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has ambiguous preserved-work identity"
        )
    return matching[0] if matching else None


def _validate_preexisting_worktree(
    configured: Path,
    candidate: Path,
    root: Path,
    expected_branch: str,
    item_id: str,
    config: DispatchConfig | None = None,
) -> bool:
    """Validate an existing candidate, returning false only when absent."""
    if not candidate.exists():
        return False
    if not _trusted_preexisting_worktree(
        configured, candidate, root, expected_branch, config
    ):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has an existing worktree with an untrusted identity"
        )
    return True


def _recorded_checkout_base(
    store: RunStore,
    *,
    run_id: str,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    generation: str,
) -> str:
    """Read one immutable checkout binding through the managed audit store."""
    from kew.dispatch_core import _recorded_publication_base
    from kew.publication import PublicationError

    try:
        base = _recorded_publication_base(
            store,
            run_id=run_id,
            item_id=item_id,
            branch=expected_branch,
            checkout=candidate,
            generation=generation,
        )
    except PublicationError as exc:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has conflicting or unreadable publication "
            f"base evidence for preserved checkout {candidate}: {exc}"
        ) from exc
    return base


def _require_recorded_checkout_base(
    store: RunStore,
    *,
    run_id: str,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    generation: str,
) -> str:
    base = _recorded_checkout_base(
        store,
        run_id=run_id,
        item_id=item_id,
        expected_branch=expected_branch,
        candidate=candidate,
        generation=generation,
    )
    if not base:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has no immutable publication base evidence "
            f"for preserved checkout {candidate}"
        )
    return base


def _require_checkout_generation(
    candidate: Path, expected: str, *, item_id: str, boundary: str
) -> None:
    try:
        require_checkout_generation(candidate, expected)
    except CheckoutAdmissionRefusal as exc:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} checkout identity changed during {boundary}; "
            f"preserved candidate {candidate} was not admitted; {exc}"
        ) from exc


def _require_continuation_checkout_generation(
    store: RunStore, record: ContinuationRecord, checkout: Path
) -> None:
    """Expose generation refusals through the driver's recovery refusal type."""
    try:
        _require_recorded_continuation_generation(
            store,
            record,
            validate_generation=partial(require_checkout_generation, checkout),
        )
    except CheckoutAdmissionRefusal as exc:
        raise _PreexistingWorktreeRefusal(str(exc)) from exc


def _generation_precommit_check(
    candidate: Path,
    expected: str,
    *,
    item_id: str,
    boundary: str,
) -> Callable[[], None]:
    """Recheck the bound generation before committing recovery writes."""
    return partial(
        _require_checkout_generation,
        candidate,
        expected,
        item_id=item_id,
        boundary=boundary,
    )


def _configured_checkout_target(
    config: DispatchConfig, item_id: str
) -> tuple[Path, str]:
    """Return the deterministic claim target without trusting recorded bytes."""
    try:
        item_id = validate_item_id(item_id, provider_from_config(config))
    except InvalidItemIdError as exc:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has an invalid recovery identity"
        ) from exc
    slug = branch_slug(item_id)
    expected_branch = f"{config.branches.prefix}{slug}"
    root = Path(config.branches.worktree_base).expanduser().resolve(strict=False)
    return root / "agent" / f"issue-{slug}", expected_branch


def _refuse_untrusted_checkout_under_claim(item_id: str, configured: Path) -> bool:
    """Inspect an untrusted producer's expected checkout only under its lease."""
    claim_checkout(configured)
    try:
        _refuse_present_untrusted_checkout(item_id, configured)
        return False
    finally:
        release_checkout_claim(configured)


def _refuse_present_untrusted_checkout(item_id: str, configured: Path) -> None:
    """Fail loudly when an expected checkout path exists but is not trusted."""
    if os.path.lexists(configured):
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has a present checkout with an untrusted "
            f"identity at {configured}"
        )


def _claim_and_validate_continuation_checkout(
    store: RunStore,
    config: DispatchConfig,
    record: Any,
    *,
    boundary: str,
) -> tuple[Path, Path, str]:
    """Claim one continuation checkout and prove its immutable producer."""
    configured, expected_branch = _configured_checkout_target(config, record.item_id)
    claim_checkout(configured)
    try:
        candidate = Path(record.worktree_identity).expanduser().resolve(strict=False)
        _require_continuation_checkout_generation(store, record, candidate)
        root = Path(config.branches.worktree_base).expanduser().resolve(strict=False)
        if record.branch != expected_branch or not _validate_preexisting_worktree(
            configured,
            candidate,
            root,
            expected_branch,
            record.item_id,
            config,
        ):
            raise _PreexistingWorktreeRefusal(
                f"{item_ref(record.item_id)} has no trusted preserved checkout "
                f"during {boundary}"
            )
        _, _, generation = _terminal_run_for_preexisting_work(
            store,
            record.item_id,
            expected_branch,
            candidate,
            config,
            required_run_id=record.original_run_id,
        )
        _require_checkout_generation(
            candidate,
            generation,
            item_id=record.item_id,
            boundary=boundary,
        )
    except BaseException:
        release_checkout_claim(configured)
        raise
    return configured, candidate, generation


def _terminal_run_for_preexisting_work(
    store: RunStore,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    config: DispatchConfig,
    *,
    required_run_id: str | None = None,
) -> tuple[RunRecord, str, str]:
    """Find exactly one terminal producer bound to this checkout identity."""
    terminal_runs = _terminal_implementation_runs(store, item_id, expected_branch)
    if config.publication.publish != "canonical":
        return _checkout_preexisting_producer(
            store, terminal_runs, item_id, candidate, required_run_id=required_run_id
        )
    return _canonical_preexisting_producer(
        store,
        terminal_runs,
        item_id,
        expected_branch,
        candidate,
        required_run_id=required_run_id,
    )


def _terminal_implementation_runs(
    store: RunStore, item_id: str, expected_branch: str
) -> list[RunRecord]:
    """Return terminal implementation runs for one item and branch."""
    return [
        run
        for run in store.list_by_item(item_id)
        if run.branch == expected_branch
        and run.status != "pending"
        and run.run_kind in _IMPLEMENTATION_RUN_KINDS
    ]


def _current_orphan_producer(
    store: RunStore,
    run: RunRecord,
    item_id: str,
    expected_branch: str,
) -> RunRecord | None:
    """Return the exact current orphan row only when it is implementation work."""
    current = store.get(run.run_id)
    if current is None:
        return None
    if current.item_id != item_id or current.branch != expected_branch:
        return None
    if current.run_kind not in _IMPLEMENTATION_RUN_KINDS:
        return None
    return current


def _orphan_run_for_preexisting_work(
    store: RunStore,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    config: DispatchConfig,
    *,
    run: RunRecord,
    owned: Any | None,
) -> tuple[RunRecord, str, str]:
    """Select one orphan producer without requiring a dead pending run terminal."""
    producers = {
        row.run_id: row
        for row in _terminal_implementation_runs(store, item_id, expected_branch)
    }
    required_run_id = owned.original_run_id if owned is not None else run.run_id
    if owned is None:
        current = _current_orphan_producer(store, run, item_id, expected_branch)
        if current is not None:
            producers[current.run_id] = current
    candidates = list(producers.values())
    if config.publication.publish != "canonical":
        return _checkout_preexisting_producer(
            store,
            candidates,
            item_id,
            candidate,
            required_run_id=required_run_id,
        )
    return _canonical_preexisting_producer(
        store,
        candidates,
        item_id,
        expected_branch,
        candidate,
        required_run_id=required_run_id,
    )


def _checkout_preexisting_producer(
    store: RunStore,
    terminal_runs: list[RunRecord],
    item_id: str,
    candidate: Path,
    *,
    required_run_id: str | None,
) -> tuple[RunRecord, str, str]:
    """Return one producer and its immutable continuation checkout binding.

    A caller that names the producer (*required_run_id*) is answered by that
    run alone, before anything is counted: an item retried once after a host
    restart carries the first attempt's terminal row on the same branch, and
    counting it too would refuse the second attempt's dirty work on every
    pass (#2971). The named run is still held to its own recorded checkout
    binding at *candidate* (:func:`_checkout_producer_generation`), so an
    earlier run cannot be passed off as it. Unnamed, exactly one terminal run
    has to stand.
    """
    if required_run_id is not None:
        terminal_runs = [r for r in terminal_runs if r.run_id == required_run_id]
        if not terminal_runs:
            raise _PreexistingWorktreeRefusal(
                f"{item_ref(item_id)} continuation names run {required_run_id!r}, "
                "but no terminal run of that identity produced the checkout"
            )
    if len(terminal_runs) != 1:
        detail = "no" if not terminal_runs else "multiple"
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has dirty work but {detail} terminal run identities"
        )
    original = terminal_runs[0]
    return (
        original,
        "",
        _checkout_producer_generation(store, original, item_id, candidate),
    )


def _checkout_producer_generation(
    store: RunStore, original: RunRecord, item_id: str, candidate: Path
) -> str:
    """Read a continuation binding or the producer's prior launch binding."""
    records = [
        record
        for record in store.continuations_for_checkout(
            candidate, item_id=item_id, branch=original.branch
        )
        if record.original_run_id == original.run_id
    ]
    generations = {
        recorded_continuation_generation(store, record) for record in records
    }
    if len(generations) > 1:
        raise _PreexistingWorktreeRefusal(
            "conflicting continuation checkout generations"
        )
    generation = (
        generations.pop()
        if generations
        else recorded_run_checkout_generation(
            store,
            run_id=original.run_id,
            item_id=item_id,
            branch=original.branch,
            checkout=candidate,
        )
    )
    if not generation:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} producer has no recorded checkout generation"
        )
    return generation


def _canonical_preexisting_producer(
    store: RunStore,
    terminal_runs: list[RunRecord],
    item_id: str,
    expected_branch: str,
    candidate: Path,
    *,
    required_run_id: str | None,
) -> tuple[RunRecord, str, str]:
    """Select one canonical producer using complete immutable evidence."""
    generation = read_checkout_generation(candidate)
    if not generation:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} preserved checkout {candidate} has no immutable "
            "checkout generation"
        )
    matches: list[tuple[RunRecord, str]] = []
    for run in terminal_runs:
        base = _recorded_checkout_base(
            store,
            run_id=run.run_id,
            item_id=item_id,
            expected_branch=expected_branch,
            candidate=candidate,
            generation=generation,
        )
        if base:
            matches.append((run, base))
    if len(matches) != 1:
        detail = "no" if not matches else "multiple"
        identity_detail = (
            "; checkout identity changed or no immutable evidence matches the "
            "required identity"
            if required_run_id is not None and not matches
            else ""
        )
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has {detail} terminal implementation producer "
            f"with immutable publication base evidence for {candidate}"
            f"{identity_detail}"
        )
    original, base = matches[0]
    if required_run_id is not None and original.run_id != required_run_id:
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} continuation names run {required_run_id!r}, but "
            f"checkout identity evidence names {original.run_id!r}"
        )
    return original, base, generation


def _persist_preexisting_continuation(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    original_run_id: str,
    reservation_run_id: str,
    checkout_generation: str,
) -> ContinuationRegistrationResult:
    """Persist and attest the exact identity selected for dirty work."""
    _require_checkout_generation(
        candidate,
        checkout_generation,
        item_id=item_id,
        boundary="continuation registration",
    )
    created = store.register_continuation_and_release_reservation(
        epic_ref,
        item_id,
        original_run_id,
        expected_branch,
        candidate,
        reservation_run_id,
        checkout_generation=checkout_generation,
        precommit_check=_generation_precommit_check(
            candidate,
            checkout_generation,
            item_id=item_id,
            boundary="continuation registration",
        ),
    )
    _require_checkout_generation(
        candidate,
        checkout_generation,
        item_id=item_id,
        boundary="continuation ownership handoff",
    )
    if created is None:
        return "lost_claim"
    if created.state != "awaiting_continuation":
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} continuation identity changed during registration"
        )
    _emit_continuation_state(
        store,
        config,
        created,
        event="continuation_detected",
        phase="detection",
        outcome="preexisting_dirty_worktree",
        run_id=original_run_id,
    )
    return "registered"


def _pending_implementation_owners(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    expected_branch: str,
    liveness: RunLivenessProbe | None,
) -> list[RunRecord]:
    """Find pending branch owners that are live or not provably dead."""
    probe = liveness if liveness is not None else default_run_liveness(config)
    owners: list[RunRecord] = []
    for run in store.list_all():
        if (
            run.branch != expected_branch
            or run.status != "pending"
            or run.run_kind in PR_SUBJECT_RUN_KINDS
        ):
            continue
        try:
            verdict = probe(run)
        except Exception:  # pragma: no cover - a probe must fail closed
            verdict = None
        if verdict is not False:
            owners.append(run)
    return owners


def _reject_live_pending_implementation_owners(
    store: RunStore,
    config: DispatchConfig,
    *,
    item_id: str,
    expected_branch: str,
    liveness: RunLivenessProbe | None,
) -> None:
    owners = _pending_implementation_owners(
        store,
        config,
        item_id=item_id,
        expected_branch=expected_branch,
        liveness=liveness,
    )
    if owners:
        owner_ids = ", ".join(sorted(run.run_id for run in owners))
        raise _PreexistingWorktreeRefusal(
            f"{item_ref(item_id)} has pending implementation owner(s) for "
            f"{expected_branch} that are live or not provably dead: {owner_ids}"
        )


def _register_dirty_preexisting_worktree(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    item_id: str,
    expected_branch: str,
    candidate: Path,
    reservation_run_id: str,
    liveness: RunLivenessProbe | None,
) -> ContinuationRegistrationResult:
    """Record dirty bytes against their one immutable terminal producer."""
    original, base, generation = _terminal_run_for_preexisting_work(
        store, item_id, expected_branch, candidate, config
    )
    _require_checkout_generation(
        candidate,
        generation,
        item_id=item_id,
        boundary="producer identity matching",
    )
    holds_work = _checkout_holds_work(config, candidate, expected_branch, base_sha=base)
    _require_checkout_generation(
        candidate,
        generation,
        item_id=item_id,
        boundary="dirty byte comparison",
    )
    if not holds_work:
        return "not_registered"
    _reject_live_pending_implementation_owners(
        store,
        config,
        item_id=item_id,
        expected_branch=expected_branch,
        liveness=liveness,
    )
    return _persist_preexisting_continuation(
        store,
        config,
        epic_ref=epic_ref,
        item_id=item_id,
        expected_branch=expected_branch,
        candidate=candidate,
        original_run_id=original.run_id,
        reservation_run_id=reservation_run_id,
        checkout_generation=generation,
    )


def _register_preexisting_dirty_worktree(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    item_id: str,
    provider: str,
    reservation_run_id: str,
    liveness: RunLivenessProbe | None,
) -> ContinuationRegistrationResult:
    """Register dirty retry work before the child dispatch boundary.

    ``setup_worktree`` recreates an existing dispatch directory.  A retry must
    therefore acquire the durable continuation identity before it can reach the
    child at all.  The reservation guard calls this after its atomic node claim;
    a losing concurrent tick never inspects or registers the other owner's
    worktree.  The continuation registration and temporary reservation release
    are one conditional RunStore transaction.
    """
    item_id = validate_item_id(item_id, provider)
    slug = branch_slug(item_id)
    expected_branch = f"{config.branches.prefix}{slug}"
    root = Path(config.branches.worktree_base).expanduser().resolve(strict=False)
    configured = root / "agent" / f"issue-{slug}"
    claim_checkout(configured)
    try:
        candidate = configured.resolve(strict=False)
        record = _preexisting_continuation(
            store, epic_ref, item_id, expected_branch, candidate
        )
        if record is not None and record.state != "terminal":
            _require_continuation_checkout_generation(store, record, candidate)
        candidate_present = _validate_preexisting_worktree(
            configured, candidate, root, expected_branch, item_id, config
        )
        if record is not None and store.continuation_was_abandoned(record):
            _reject_live_pending_implementation_owners(
                store,
                config,
                item_id=item_id,
                expected_branch=expected_branch,
                liveness=liveness,
            )
            return "not_registered"
        if record is not None and record.state != "terminal":
            _, _, generation = _terminal_run_for_preexisting_work(
                store,
                item_id,
                expected_branch,
                candidate,
                config,
                required_run_id=record.original_run_id,
            )
            _reject_live_pending_implementation_owners(
                store,
                config,
                item_id=item_id,
                expected_branch=expected_branch,
                liveness=liveness,
            )
            return _persist_preexisting_continuation(
                store,
                config,
                epic_ref=epic_ref,
                item_id=item_id,
                expected_branch=expected_branch,
                candidate=candidate,
                original_run_id=record.original_run_id,
                reservation_run_id=reservation_run_id,
                checkout_generation=generation,
            )
        if not candidate_present:
            return "not_registered"
        return _register_dirty_preexisting_worktree(
            store,
            config,
            epic_ref=epic_ref,
            item_id=item_id,
            expected_branch=expected_branch,
            candidate=candidate,
            reservation_run_id=reservation_run_id,
            liveness=liveness,
        )
    finally:
        release_checkout_claim(configured)


def _reserve_frontier_node(
    nid: str,
    *,
    graph: Graph,
    name: str,
    store: RunStore,
    dispatch_sink: DispatchSink,
    branch_exists: Callable[[str], bool],
    config: DispatchConfig,
    provider: str,
    fleet_cap: int,
    liveness: RunLivenessProbe | None,
    admit: AdmissionGuard | None = None,
) -> tuple[str, StuckNode | None]:
    """Reserve and dispatch one frontier node, classifying refusal locally."""

    def register_continuation(
        reservation_run_id: str,
    ) -> ContinuationRegistrationResult:
        return _register_preexisting_dirty_worktree(
            store,
            config,
            epic_ref=name,
            item_id=nid,
            provider=provider,
            reservation_run_id=reservation_run_id,
            liveness=liveness,
        )

    try:
        outcome = reserve_and_dispatch(
            nid,
            epic_ref=name,
            store=store,
            sink=dispatch_sink,
            branch_exists=branch_exists,
            register_continuation=register_continuation,
            admit=admit,
            provider=provider,
            fleet_cap=fleet_cap,
        )
    except credential_issuer.IssuerError as exc:
        return (
            CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
            _refusal_stuck(
                nid, graph, CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX, exc.reason
            ),
        )
    except (_PreexistingWorktreeRefusal, CheckoutAdmissionRefusal) as exc:
        # Either way the target cannot be dispatched afresh without risking
        # retained work; the node is reported stuck and the tick goes on.
        return (
            PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
            _refusal_stuck(
                nid, graph, PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX, str(exc)
            ),
        )
    if outcome == "dispatched":
        # This attempt disproved any earlier refusal of the same node (#2217).
        _clear_stale_dispatch_refusal_hold(store, name, nid)
    return outcome, None


def _refusal_stuck(nid: str, graph: Graph, category: str, detail: str) -> StuckNode:
    """The stuck row for a frontier refusal, under its own reason prefix.

    The prefix leads because every reader of a waiting/stuck reason buckets on
    one (``attention.STALL_BUCKETS``, and `kew epic status`'s dominant-cause
    stall line through it). A bare detail fell through to the unbucketed
    fallback, so the refusal the frontier had just hit arrived unlabelled while
    the hold row written from the same refusal carried the prefix (#2217).
    """
    return StuckNode(
        node_id=nid,
        reason=f"{category}: {detail}" if detail else category,
        blast_radius=tuple(sorted(graph.transitive_dependents(nid))),
    )


def _hold_refused_frontier_node(
    store: RunStore, epic_ref: str, item_id: str, reason: str, *, provider: str
) -> None:
    """Hold a node the parent process refused before any child started.

    The refusal is a property of the node, not of this tick: the issuer route
    is unsupported, or a dirty worktree is already on disk. Reporting it
    without holding leaves the node READY, so every later tick repeats the
    whole refusal and writes one more terminal row for it.

    ``reason`` is the refusal's own already-prefixed reason
    (:func:`_refusal_stuck`), so the hold row and the stuck row an operator
    reads carry the same text.
    """
    if provider == "github":
        store.hold_epic_node(epic_ref, item_id, reason)
    else:
        store.hold_epic_node_item(epic_ref, item_id, reason)


def _clear_stale_dispatch_refusal_hold(
    store: RunStore, epic_ref: str, item_id: str
) -> None:
    """Drop the refusal hold a node that just dispatched is still carrying (#2217).

    :func:`_hold_refused_frontier_node` writes a hold the overlay deliberately
    does NOT honor, because leaving the node dispatchable is its recovery path.
    So the very next tick can dispatch the node successfully while the refusal
    row survives it — and that stale row is read as an operator park by every
    pass that honors a hold. On #2201/#2202 it blocked remediation for two
    hours after the PRs were open and reviewed. The dispatch that disproves the
    refusal is what clears it.

    ``only_reason_prefixes`` keeps the release to the driver's own two reasons
    AND inside the store's delete transaction: the detached dispatch child this
    very reservation started can upsert an ``admission_refused`` hold over the
    row, and deleting that one would un-hold a refusal nothing disproved.
    """
    released = store.remove_epic_hold(
        epic_ref, item_id, only_reason_prefixes=DISPATCH_REFUSAL_HOLD_PREFIXES
    )
    if released is None:
        return
    _logger.info(
        "epic %s: node %s un-held — this dispatch succeeded, so its earlier "
        "refusal no longer stands (was: %s)",
        epic_ref,
        item_id,
        released.reason,
    )


def _apply_frontier_outcome(
    outcome: str,
    nid: str,
    *,
    graph: Graph,
    statuses: dict[str, DriverStatus],
    dispatched: list[str],
    in_flight: list[str],
    stuck: list[StuckNode],
    admission_held: list[str],
) -> tuple[int, bool]:
    """Apply one reservation outcome and return (budget_used, fleet_full)."""
    if outcome == "dispatched":
        dispatched.append(nid)
        in_flight.append(nid)
        return 1, False
    if outcome == "continuation_registered":
        statuses[nid] = DriverStatus.AWAITING_CONTINUATION
        return 0, False
    if outcome == "non_int":
        stuck.append(
            StuckNode(
                node_id=nid,
                reason="non-int-id",
                blast_radius=tuple(sorted(graph.transitive_dependents(nid))),
            )
        )
        return 0, False
    if outcome == "fleet_full":
        # The atomic reservation is the capacity authority: the fleet can
        # fill between budget calculation and admission. Hold the node.
        admission_held.append(nid)
        return 0, True
    return 0, False


@dataclass(frozen=True)
class _ReviewBrake:
    """This tick's human-review backpressure, resolved once per tick.

    ``fresh`` is the set of ready ids whose dispatch would START new
    independent work — classified once from the run history the overlay
    already reads, so the split that REPORTS the brake and the guard that
    ENFORCES it at the dispatch door can never disagree with each other.
    """

    pressure: ReviewPressure
    fresh: frozenset[str]

    def split(self, frontier: list[str]) -> tuple[list[str], list[str]]:
        return apply_review_backpressure(
            frontier, saturated=self.pressure.saturated, fresh_work=self.fresh
        )

    def guard(self, item_id: str) -> DispatchResult | None:
        """The admission gate `reserve_and_dispatch` consults before reserving."""
        if self.pressure.saturated and item_id in self.fresh:
            return REVIEW_SATURATED
        return None


def _review_brake(
    store: RunStore, frontier: list[str], pressure: ReviewPressure
) -> _ReviewBrake:
    """Classify this tick's frontier against the delivery repo's review pressure.

    Below capacity nothing is classified and nothing is read: the brake is off
    and the ordinary frontier passes through untouched.
    """
    if not pressure.saturated:
        return _ReviewBrake(pressure, frozenset())
    return _ReviewBrake(
        pressure,
        frozenset(
            nid for nid in frontier if starts_fresh_work(store.list_by_item(nid))
        ),
    )


def _review_pressure(
    store: RunStore, *, config: DispatchConfig, repo: str, now: datetime
) -> ReviewPressure:
    """Read the delivery repository's authoritative human-review pressure (I15).

    The system policy is the one `[human_review]` states, bound to the epic's
    own canonical GitHub delivery repository — capacity belongs to a
    repository, never to a driver, so a busy repo cannot brake a quiet one.
    The store resolves any durable override (and its expiry) on top.
    """
    section = config.human_review
    system = ReviewPolicy(
        delivery_repo=repo,
        max_pending_reviews=section.max_pending_reviews,
        max_diff_lines=section.max_diff_lines,
        max_paths=section.max_paths,
        approvers=tuple(section.approvers) or tuple(config.approval.approvers),
    )
    return read_review_pressure(
        store, delivery_repo=repo, system_policy=system, now=now
    )


@dataclass(frozen=True)
class _FrontierOutcome:
    """What one dispatch pass did, and what it deliberately did not do."""

    dispatched: list[str]
    in_flight: list[str]
    stuck: list[StuckNode]
    admission_held: list[str]
    hold_reason: str | None
    # Ready nodes human-review backpressure withheld this tick (I15). Reported
    # separately from `admission_held` because the constraint is a different
    # one: people, not workers — and it names its own reason.
    review_withheld: list[str]
    # Why each dispatched node went when it did, in dispatch order (#2575).
    dispatch_ranks: tuple[FrontierRank, ...] = ()
    # Armed host-restart continuations admission kept waiting this tick
    # (#2964). A later tick claims them, so they are progress, not a stall.
    continuations_deferred: tuple[str, ...] = ()


#: No priority labels read: every node ranks as the default (#2575).
_NO_PRIORITIES: Mapping[str, int] = MappingProxyType({})


def _frontier_priorities(node_facts: Mapping[str, NodeFacts]) -> dict[str, int]:
    """Each node's priority rank, read off the labels the tick already fetched."""
    return {nid: priority_of(facts.labels) for nid, facts in node_facts.items()}


def _dispatch_ranks(
    graph: Graph, dispatched: Sequence[str], priorities: Mapping[str, int]
) -> tuple[FrontierRank, ...]:
    """The rank each dispatched node was chosen by, in dispatch order."""
    return tuple(frontier_rank(graph, nid, priorities) for nid in dispatched)


@dataclass
class _FrontierWithhold:
    """Whether a ready node waits this tick: the admission budget is spent,
    or a running local merge verify defers its spawn (#3190). The lease is
    read at each spawn, so a verify that began after this tick's admission
    defers the launches after it; ``verify`` is the wait it named."""

    store: RunStore
    config: DispatchConfig
    verify: str | None = None

    def withholds(self, nid: str, budget: int) -> bool:
        if budget <= 0:
            return True
        deferral = verify_deferral(
            self.store, self.config, item_id=nid, kind="dispatch"
        )
        if deferral is None:
            return False
        self.verify = f"{ADMISSION_HELD_PREFIX}: {deferral}"
        return True


def _dispatch_frontier(
    graph: Graph,
    overlay_graph: Graph,
    frontier_status: dict[str, NodeStatus],
    statuses: dict[str, DriverStatus],
    *,
    provider: str,
    reserved_item_ids: set[str],
    already_stuck_ids: set[str],
    name: str,
    store: RunStore,
    dispatch_sink: DispatchSink,
    config: DispatchConfig,
    max_parallel: int,
    fleet_cap: int,
    branch_prefix: str,
    pressure: ReviewPressure,
    dependency_effects: dict[str, frozenset[str]],
    priorities: Mapping[str, int] = _NO_PRIORITIES,
    liveness: RunLivenessProbe | None = None,
    operator_stop: bool = False,
) -> _FrontierOutcome:
    in_flight = sorted(
        (nid for nid, ds in statuses.items() if ds is DriverStatus.IN_PROGRESS),
        key=item_id_sort_key,
    )
    excluded = _dispatch_exclusions(
        graph,
        statuses,
        in_flight=in_flight,
        reserved_item_ids=reserved_item_ids,
        already_stuck_ids=already_stuck_ids,
    )

    frontier = compute_frontier(
        overlay_graph,
        status=frontier_status,
        excluded=excluded,
        dependency_effects=dependency_effects,
        priorities=priorities,
    )
    # Human-review backpressure (I15): while the delivery repository is at (or
    # past) its effective cap, or its remote truth may not be counted from,
    # fresh independent work waits. Everything else in the frontier — nodes
    # this host has already started — passes through untouched.
    brake = _review_brake(store, frontier, pressure)
    frontier, review_withheld = brake.split(frontier)

    def branch_exists(item_id: str) -> bool:
        return remote_branch_exists(f"{branch_prefix}{branch_slug(item_id)}")

    dispatched: list[str] = []
    stuck: list[StuckNode] = []
    dispatch_budget = _compute_dispatch_budget(
        store,
        epic_ref=name,
        fleet_cap=fleet_cap,
        per_epic_budget=max_parallel - len(in_flight),
        liveness=liveness,
    )
    # A runner lane the provider refused for quota (#2643): nothing dispatches
    # into it until the reset, and the ready nodes say why they wait.
    quota_hold = _runner_quota_hold(store, config)
    budget = dispatch_budget.budget if quota_hold is None else 0
    # A continuation is a dispatch launch too: it yields to a running local
    # merge verify exactly as a fresh frontier node does (#3190).
    withhold = _FrontierWithhold(store, config)
    budget, continuations_deferred, retrying, fleet_full_at_admission = (
        _auto_continue_host_restarts(
            store,
            statuses,
            name=name,
            config=config,
            sink=dispatch_sink,
            fleet_cap=fleet_cap,
            budget=budget,
            in_flight=in_flight,
            withhold=withhold,
        )
    )
    # Ready frontier nodes we run out of admission budget for (#1041): a node
    # that IS dispatchable but is skipped because the worker cap is consumed.
    # Surfaced as a "waiting" reason so a tick that dispatches nothing under a
    # full cap SAYS WHY, instead of silently skipping ready work (#669).
    admission_held: list[str] = list(continuations_deferred)
    for nid in frontier:
        if withhold.withholds(nid, budget):
            admission_held.append(nid)
            continue

        outcome, refusal = _reserve_frontier_node(
            nid,
            graph=graph,
            name=name,
            store=store,
            dispatch_sink=dispatch_sink,
            branch_exists=branch_exists,
            config=config,
            provider=provider,
            fleet_cap=fleet_cap,
            liveness=liveness,
            admit=brake.guard,
        )
        if refusal is not None:
            _hold_refused_frontier_node(
                store, name, nid, refusal.reason, provider=provider
            )
            statuses[nid] = DriverStatus.HELD
            stuck.append(refusal)
            continue
        if outcome == REVIEW_SATURATED:
            # The door refused what the split already withheld. Report it
            # rather than skipping it silently (#669): a ready node that does
            # not dispatch has to say which limiter stopped it.
            review_withheld.append(nid)
            continue
        if outcome == "branch_exists":
            stuck.append(_branch_exists_stuck(nid, graph, branch_prefix))
            continue
        budget_used, fleet_full = _apply_frontier_outcome(
            outcome,
            nid,
            graph=graph,
            statuses=statuses,
            dispatched=dispatched,
            in_flight=in_flight,
            stuck=stuck,
            admission_held=admission_held,
        )
        budget -= budget_used
        if fleet_full:
            fleet_full_at_admission = True
            budget = 0
        # "reserved_conflict": another process beat us to it — skip quietly,
        # it is not this tick's problem to report.
    hold_reason = _frontier_hold_reason(
        quota_hold,
        dispatch_budget,
        fleet_cap=fleet_cap,
        fleet_full_at_admission=fleet_full_at_admission,
        admission_held=bool(admission_held),
        closed=_drive_closed_reason(store, max_parallel, operator_stop=operator_stop),
        verify=withhold.verify,
    )
    return _FrontierOutcome(
        dispatched=dispatched,
        in_flight=sorted(in_flight, key=item_id_sort_key),
        stuck=stuck,
        admission_held=sorted(admission_held, key=item_id_sort_key),
        hold_reason=hold_reason,
        review_withheld=sorted(review_withheld, key=item_id_sort_key),
        dispatch_ranks=_dispatch_ranks(overlay_graph, dispatched, priorities),
        continuations_deferred=(*continuations_deferred, *retrying),
    )


#: The actor the driver's automatic continuation claims under (#2964), so its
#: claims — and the attempts they spend — are told from an operator's.
AUTO_CONTINUE_ACTOR = "driver_auto_continue"


def _auto_continue_host_restarts(
    store: RunStore,
    statuses: dict[str, DriverStatus],
    *,
    name: str,
    config: DispatchConfig,
    sink: DispatchSink,
    fleet_cap: int,
    budget: int,
    in_flight: list[str],
    withhold: _FrontierWithhold,
) -> tuple[int, list[str], list[str], bool]:
    """Continue preserved work the host restarted under, once (#2964).

    Each continuation is admitted like fresh work: it spends one unit of this
    tick's budget (per-epic, worker cap and fair share), and the claim takes
    its worker slot under the fleet cap; *withhold* defers it, like a fresh
    node, while a local merge verify runs (#3190). Return the budget left over, the
    armed continuations admission deferred to a later tick — held like a
    ready node the budget ran out for, never dropped — the continuations whose
    spawn failed and that a later tick retries, and whether the fleet cap
    refused a claim.
    """
    if not can_continue_claimed(sink):
        return budget, [], [], False
    deferred: list[str] = []
    retrying: list[str] = []
    fleet_full = False
    for record in _auto_continuation_candidates(store, statuses, name):
        if withhold.withholds(record.item_id, budget):
            deferred.append(record.item_id)
            continue
        outcome = _auto_continue(store, record, config=config, sink=sink, cap=fleet_cap)
        if outcome == "fleet_full":
            budget, fleet_full = 0, True
            deferred.append(record.item_id)
        elif outcome in ("launched", "already_claimed"):
            budget -= 1
            statuses[record.item_id] = DriverStatus.IN_PROGRESS
            in_flight.append(record.item_id)
        elif outcome == "spawn_failed" and _auto_continuation_due(store, record):
            # The retry the cancelled claim left is a later tick's to make; a
            # tick that reported STALLED here would end `kew epic run` first.
            retrying.append(record.item_id)
    return budget, deferred, retrying, fleet_full


def _auto_continuation_candidates(
    store: RunStore, statuses: dict[str, DriverStatus], name: str
) -> list[Any]:
    """The armed continuations of nodes this tick shows waiting on them.

    The overlay shows a preserved continuation as HELD, its recovery gate; a
    node that also carries a real hold is the operator's, not ours.
    """
    held = _holds_reason_map(store, name)
    waiting = {
        nid
        for nid, ds in statuses.items()
        if ds in (DriverStatus.HELD, DriverStatus.AWAITING_CONTINUATION)
        and nid not in held
    }
    armed = {
        record.item_id
        for record in store.list_continuations(name)
        if record.item_id in waiting and _auto_continuation_due(store, record)
    }
    candidates = []
    for item_id in sorted(armed):
        record = _sole_awaiting_continuation(store, name, item_id)
        if record is not None and _auto_continuation_due(store, record):
            candidates.append(record)
    return candidates


def _sole_awaiting_continuation(store: RunStore, name: str, item_id: str) -> Any | None:
    """The one continuation ``kew epic continue`` would select for *item_id*.

    The driver resumes nothing an operator could not select unambiguously:
    two incomplete identities for one item, or an awaiting record beside a
    terminal one, stay the operator's to choose between.
    """
    try:
        return select_awaiting_continuation(store, name, item_id)
    except ContinuationSelectionDenied as exc:
        _logger.warning(
            "epic %s: %s not continued automatically: %s",
            name,
            item_id,
            str(exc).format(item_id=item_ref(item_id)),
        )
        return None


def _armed_continuations(
    store: RunStore, statuses: dict[str, DriverStatus], name: str
) -> list[str]:
    """The nodes whose preserved work the next tick may continue on its own."""
    return [r.item_id for r in _auto_continuation_candidates(store, statuses, name)]


def _auto_continuation_due(store: RunStore, record: Any) -> bool:
    """Is *record* preserved work the driver still continues on its own?

    Only work a FIRST host death preserved is armed, and only once: a second
    host death leaves it for a person. One claim the driver cancelled before
    its agent launched (a transient spawn failure, say) is retried once; a
    refusal that repeats, such as a deleted checkout, stops after the second
    cancellation and leaves the continuation waiting.
    """
    if record.state != "awaiting_continuation":
        return False
    outcomes = _dirty_host_restart_outcomes(store, record)
    if HOST_RESTART_CONTINUE not in outcomes or HOST_RESTART_REPEATED in outcomes:
        return False
    claims = [
        event
        for event in store.list_audit_events(
            limit=None,
            event="continuation_claimed",
            item_id=record.item_id,
            epic_ref=record.epic_ref,
        )
        if event.actor_id == AUTO_CONTINUE_ACTOR
        and (event.payload or {}).get("original_run_id") == record.original_run_id
    ]
    if not claims:
        return True
    return len(claims) == 1 and _claim_was_undone(store, claims[0].run_id)


#: What a claim cancelled before its agent launched leaves, per event: the
#: child's own cancellation, or missing-run reconciliation's verdict on a
#: child that died before it could write its run row (or cancel anything).
_UNLAUNCHED_CLAIM_EVIDENCE = (
    ("continuation_released", "claim_undone"),
    ("continuation_terminal", "crash_before_run_row"),
)


def _claim_was_undone(store: RunStore, claim_run_id: str | None) -> bool:
    """Was this claim cancelled before its agent launched?"""
    if claim_run_id is None:
        return False
    return any(
        (event.payload or {}).get("outcome") == outcome
        for name, outcome in _UNLAUNCHED_CLAIM_EVIDENCE
        for event in store.list_audit_events(
            limit=None, event=name, run_id=claim_run_id
        )
    )


def _auto_continue(
    store: RunStore,
    record: Any,
    *,
    config: DispatchConfig,
    sink: DispatchSink,
    cap: int,
) -> str:
    """Claim one armed continuation and hand it to a detached child.

    ``"launched"``, ``"fleet_full"`` when the worker cap refused the claim,
    ``"already_claimed"`` when a concurrent caller's claim won and still holds
    it, ``"spawn_failed"`` when the child did not launch, else ``"refused"``. A
    spawn that fails cancels the unlaunched claim, which spends one of the two
    attempts :func:`_auto_continuation_due` allows.
    """
    try:
        claimed = claim_continuation(
            store, record=record, config=config, cap=cap, actor_id=AUTO_CONTINUE_ACTOR
        )
    except ContinuationFleetFull:
        return "fleet_full"
    except ContinuationAlreadyClaimed as exc:
        # A concurrent caller won the claim CAS: its child holds the node, so
        # the node is in flight — not refused, and never STALLED.
        if exc.winner is None:
            _logger.warning(
                "epic %s: %s not continued: %s", record.epic_ref, record.item_id, exc
            )
            return "refused"
        _logger.info(
            "epic %s: %s already continued by run %s",
            record.epic_ref,
            record.item_id,
            exc.winner.continuation_run_id,
        )
        return "already_claimed"
    except EpicResumeDenied as exc:
        _logger.warning(
            "epic %s: %s not continued: %s", record.epic_ref, record.item_id, exc
        )
        return "refused"
    run_id = claimed.continuation_run_id
    assert run_id is not None  # a claim always names its owner run
    try:
        launched = continue_claimed_child(sink, claimed.item_id, run_id)
    except ChildSpawnedError as exc:
        # The child is running under this claim; rolling it back would let a
        # second claim start beside it in the same checkout. Reconciliation
        # settles the child's run like any other.
        _logger.warning(
            "epic %s: continuation of %s launched with failed bookkeeping: %s",
            record.epic_ref,
            record.item_id,
            exc.__cause__,
        )
        launched = True
    except Exception as exc:  # noqa: BLE001 - every spawn failure cancels the claim
        _logger.warning(
            "epic %s: continuation of %s failed to spawn: %s",
            record.epic_ref,
            record.item_id,
            exc,
        )
        launched = False
    if launched:
        _logger.warning(
            "epic %s: %s continued once — the host restarted under its dirty work",
            record.epic_ref,
            record.item_id,
        )
        return "launched"
    store.mark_continuation_awaiting(
        claimed.epic_ref,
        claimed.item_id,
        claimed.original_run_id,
        claimed.branch,
        claimed.worktree_identity,
        claimed.generation,
        run_id,
        reason="auto_continue_spawn_failed",
        cancel_unlaunched=True,
    )
    return "spawn_failed"


def _branch_exists_stuck(nid: str, graph: Graph, branch_prefix: str) -> StuckNode:
    """The stuck row for a ready node whose remote branch already exists.

    This node holds no reservation, so no driver dispatch owns that branch: it
    is left over, most often by a PR that closed unmerged (#2689). Skipping it
    quietly left the node READY in `kew epic status` and absent from every tick.
    """
    branch = f"{branch_prefix}{branch_slug(nid)}"
    return StuckNode(
        node_id=nid,
        reason=(
            f"branch-exists: remote branch '{branch}' already exists — delete it "
            f"(`git push origin --delete {branch}`) if no dispatch owns it"
        ),
        blast_radius=tuple(sorted(graph.transitive_dependents(nid))),
    )


def _frontier_hold_reason(
    quota_hold: RunnerQuotaHold | None,
    dispatch_budget: _DispatchBudget,
    *,
    fleet_cap: int,
    fleet_full_at_admission: bool,
    admission_held: bool,
    closed: str | None,
    verify: str | None = None,
) -> str | None:
    """Why ready frontier nodes were withheld this tick, binding limiter first.

    ``closed`` is why ``kew drive`` closed admission (#2616) — a per-epic
    budget of 0 — named as such rather than as an exhausted budget. It comes
    before a runner quota hold (#2878): the drive's hold or the host load it
    recorded keeps the node waiting whatever the quota does, so naming only the
    quota would hide an overloaded host. ``verify`` is a running local merge
    verify's deferral (#3190), named next for the same reason.
    """
    if admission_held and (closed or verify):
        return closed or verify
    if quota_hold is not None and admission_held:
        return quota_hold.reason()
    if fleet_full_at_admission:
        return (
            f"{ADMISSION_HELD_PREFIX}: worker cap {fleet_cap} consumed "
            "(reservation refused at admission)"
        )
    if admission_held:
        return dispatch_budget.admission_hold_reason()
    return None


def _drive_held_reason(store: RunStore) -> str | None:
    """What a standing drive hold says: the operator's ``kew drive hold``
    first — work waits for ``kew drive release`` whatever the host does — then
    the host-load episode the drive recorded (#2813). None when neither stands;
    an unreadable row is logged and named as no reason here, never as a calm
    host (the drive itself refuses it)."""
    try:
        held = drive_hold(store)
    except DriveStateError as exc:
        _logger.warning("drive hold unreadable: %s", exc)
        held = None
    if held is not None:
        since = held.since.isoformat(timespec="seconds")
        return (
            f"{ADMISSION_HELD_PREFIX}: admission held by `kew drive hold` since "
            f"{since}; `kew drive release` lifts it"
        )
    try:
        host = host_load_hold(store)
    except ValueError as exc:
        _logger.warning("%s unreadable: %s", DRIVE_HOST_LOAD_KEY, exc)
        return None
    return None if host is None else f"{ADMISSION_HELD_PREFIX}: {host.describe()}"


def _drive_closed_reason(
    store: RunStore, max_parallel: int, *, operator_stop: bool = False
) -> str | None:
    """Why this tick has a budget of 0: the operator's ``--max-parallel 0``
    (#3192), else ``kew drive``'s operator hold, else the host-load episode it
    recorded (#2813), else a ``[drive.load]`` threshold of 0. None while the
    budget is open."""
    if max_parallel > 0:
        return None
    if operator_stop:
        return (
            f"{ADMISSION_HELD_PREFIX}: admission closed by --max-parallel 0 "
            "(an operator stop); running work finishes"
        )
    return _drive_held_reason(store) or (
        f"{ADMISSION_HELD_PREFIX}: admission closed by kew drive "
        "(`kew drive hold`, or a [drive.load] threshold of 0)"
    )


def _drive_held_waiting(
    graph: Graph, frontier: Sequence[str], waiting: Sequence[StuckNode], store: RunStore
) -> tuple[StuckNode, ...]:
    """The ready nodes a running ``kew drive`` holds back — for its operator
    hold or for host load (#2813) — for ``kew epic status``: no live tick to
    say so, the hold rows do. A row a dead drive left behind is not read as
    standing."""
    if not frontier or read_drive(store).state != DRIVE_RUNNING:
        return ()
    reason = _drive_held_reason(store)
    if reason is None:
        return ()
    listed = {w.node_id for w in waiting}
    return _admission_held_waiting(
        graph, [nid for nid in frontier if nid not in listed], reason=reason
    )


#: The statuses whose next action is a review-stage launch a verify defers.
_REVIEW_STAGE_STATUSES = frozenset(
    {
        DriverStatus.ADVERSARIAL_REVIEW,
        DriverStatus.REMEDIATING,
        DriverStatus.IN_REVIEW,
    }
)


def _verify_deferred_reasons(
    graph: Graph,
    frontier: Sequence[str],
    statuses: Mapping[str, DriverStatus],
    waiting: Sequence[StuckNode],
    store: RunStore,
    provider: str,
) -> dict[str, str]:
    """Node -> why it waits on the local merge verify running now (#3190), for
    ``kew epic status``: no live tick to say so, this verify's
    ``launch_deferred`` events do. A ready node reads as admission-held, as
    the tick reports it; a review-stage node shows the deferral itself; a node
    held on ``ci_pending`` keeps its hold line, with its deferred ci-fix
    named after it. A node in any other state is not waiting on a launch,
    whatever was deferred."""
    deferred = standing_deferrals(store)
    if not deferred:
        return {}
    shown = {w.node_id: w.reason for w in waiting}
    reasons: dict[str, str] = {}
    for node_id in graph.nodes:
        try:
            found = deferred.get(validate_item_id(node_id, provider))
        except InvalidItemIdError:
            continue
        if found is None:
            continue
        reason = _deferred_line(
            found, node_id in frontier, statuses.get(node_id), shown.get(node_id)
        )
        if reason is not None:
            reasons[node_id] = reason
    return reasons


def _deferred_line(
    found: StandingDeferral,
    ready: bool,
    status: DriverStatus | None,
    shown: str | None,
) -> str | None:
    """The status line for one node a running verify deferred, or None."""
    if ready:
        return f"{ADMISSION_HELD_PREFIX}: {found.reason}"
    if status in _REVIEW_STAGE_STATUSES:
        return found.reason
    if status is DriverStatus.HELD and found.launch == "ci-fix":
        return found.reason if shown is None else f"{shown}; ci-fix {found.reason}"
    return None


#: The complexity routes a config may send a node down, besides its default.
_ROUTED_COMPLEXITIES = ("simple", "medium", "complex")


def _runner_quota_hold(
    store: RunStore, config: DispatchConfig
) -> RunnerQuotaHold | None:
    """The live quota hold on a runner lane this config dispatches to, or None.

    Every lane the config can route a node to is checked — the default and
    each complexity route — because the tick does not know a node's route
    until the child resolves it. A route that names no valid runner is left to
    the child to refuse, as before. A hold recorded under another credential
    than the one now in the environment does not apply: the next dispatch
    re-probes the provider with the new one.
    """
    lanes: set[str] = set()
    for complexity in (None, *_ROUTED_COMPLEXITIES):
        try:
            lanes.add(resolve_runner(config, complexity).runner.name)
        except ValueError:
            continue
    for lane in sorted(lanes):
        hold = active_runner_quota_hold(
            store, lane, credential_fingerprint=runner_credential_fingerprint(lane)
        )
        if hold is not None:
            return hold
    return None


def _admission_held_waiting(
    graph: Graph,
    admission_held: list[str],
    *,
    reason: str,
) -> tuple[StuckNode, ...]:
    """Turn ready-but-admission-held frontier nodes into "waiting" entries (#1041).

    A ready node an admission limiter kept from dispatching this tick would
    otherwise vanish from the report entirely (its deps ARE met, so
    `_waiting_nodes` never prints it) — the exact silent-skip the incident hit.
    ``reason`` (from :meth:`_DispatchBudget.admission_hold_reason`) names the
    BINDING limiter — per-epic budget, fleet worker cap, or fair share — so the
    line reaches stdout and the journal pointing at the real constraint, not
    always the worker cap (#669 stall-reason precedent)."""
    if not admission_held:
        return ()
    return tuple(
        StuckNode(nid, reason, tuple(sorted(graph.transitive_dependents(nid))))
        for nid in admission_held
    )


@dataclass
class _MergeOutcome:
    merged: list[str]
    held: list[str]
    stuck_by_id: dict[str, StuckNode]
    held_live: bool
    # Nodes closed by provider-specific reconciliation before overlays.
    closed: list[str]
    # GitHub issues closed after overlays. They count as close actions now but
    # reach DONE only through the next tick's provider CLOSED observation.
    deferred_closed: list[str] = field(default_factory=list)
    # The subset of `held` this tick holds only until GitHub answers: they carry
    # no hold row and no stall clock, and their PRs are still open awaiting a
    # human's merge (#2183). See `is_self_healing_hold` and
    # `_awaiting_merge_only`.
    held_transient: list[str] = field(default_factory=list)


def _classify_held_verdict(
    verdict: MergeVerdict,
    *,
    graph: Graph,
    holds_after: dict[str, EpicHold],
    now: datetime,
    stall_days: int,
) -> tuple[str | None, StuckNode | None, bool, bool]:
    """Return (held_id, stuck_node, held_live, self_healing) for a "held" verdict.

    ``self_healing`` marks a hold GitHub clears by itself (see
    `is_self_healing_hold`): no hold row, no stall clock, and the PR under it
    is still open awaiting a merge, so the signal must keep reading it that way
    (#2183).

    The row — `holds_after`, read back AFTER the pass — is what decides which
    of the two shapes this node is in, never the verdict word alone. A verdict
    `is_self_healing_hold` accepts describes only what the pass just observed;
    a node already held on ANY honored reason (a refused containment posture, a
    rounds-exhausted review, a base-integration conflict, a budget preflight)
    has the row and the running stall clock regardless, and folding it into the
    merge wait because GitHub happened not to have computed its mergeability
    this tick is the `ci_pending` divergence generalized to every hold: nothing
    is awaiting a human's merge, and `status` reads STALLED off the same row.
    """
    if is_queue_contention(verdict.reason):
        # The merge queue's repo-wide lock, or this PR's review checkout, is held
        # by another live process (#2148): real work in flight elsewhere, which
        # is the same reading `locked_out` gets for a contended epic-driver lock
        # — PROGRESSING, so `kew epic run` sleeps and re-ticks. Read as a stall,
        # the by-design serialization outcome ended the run with a repair exit
        # code (#2173 review round 4). No hold row, so no stall clock either: the
        # holder releases the flock (or dies) and the next tick wins it.
        return verdict.node_id, None, True, False
    hold = holds_after.get(verdict.node_id)
    if hold is None and not is_ci_pending_hold(verdict.held_reason):
        # Transient: no persisted hold row, so no stall clock — it simply
        # re-polls fresh next tick. Only the holds GitHub itself clears
        # (`is_self_healing_hold`: mergeability still being computed, head
        # checks still running) re-poll into a merge, though; the merge queue's
        # own held reasons (an unverifiable head, a refused containment posture,
        # a gate that could not run) name a broken pipeline an operator must
        # fix, and STALLED is the signal that says so — so they are NOT
        # self-healing (#2183).
        return (
            verdict.node_id,
            None,
            False,
            is_self_healing_hold(verdict.reason),
        )
    if hold is not None and _is_stale(
        hold.held_at.isoformat(), now=now, stall_days=stall_days
    ):
        blast = tuple(sorted(graph.transitive_dependents(verdict.node_id)))
        # The SAME word `_held_signal` gives `kew epic status` for this row
        # (`_aged_hold_reason`, #2150): `ci-stalled` for the CI gate, its own
        # reason for every other honored hold, so the two paths name one state
        # one way.
        return (
            None,
            StuckNode(verdict.node_id, _aged_hold_reason(hold.reason), blast),
            False,
            False,
        )
    return verdict.node_id, None, True, False


def _is_awaiting_merge(verdict: MergeVerdict) -> bool:
    """A merge candidate that is waiting on a human gate, not a broken pipeline —
    the AWAITING_MERGES signal, distinct from a genuine stall (Node I7).

    Covers a PR with no verified approval yet (``awaiting-approval``) and a
    candidate the governed-review invariant skipped (N1): a non-READY verdict,
    a post-verdict force-push, or a stale approval all mean the node is awaiting
    re-review or re-approval — an operator-visible waiting state, NOT a stall
    (`REVIEW_BLOCK_REASONS`, #977). Without this, an invariant-skipped candidate
    would read STALLED and misfire the blast-radius alarm.

    A merge-queue refusal (#2148) is the same kind of state: a red combined tree
    is now a recorded NEEDS_CHANGES round the review stage remediates next tick
    (which `[epic] merge_queue` requires to exist — see the config validator), and
    a head that moved during verification gets a fresh round on the new head —
    both are awaiting re-review, exactly like `REVIEW_SKIP_NOT_READY`, which is
    the reason the very next tick reports for the same node. A BASE that moved
    under the verification (#2184) is the same shape: nothing is broken, the
    combined tree the next tick builds is simply a different one. The queue's *held*
    outcomes are deliberately NOT here: an unconfigured verify command, a refused
    containment posture, an unreadable head, or a gate that could not run is a
    broken pipeline an operator must fix, and STALLED is the signal that says so.
    The one exception is live contention, which `_classify_held_verdict` reads as
    PROGRESSING rather than routing here — nothing is awaiting a merge, another
    driver is mid-verification."""
    if verdict.action != "skip":
        return False
    return (
        verdict.reason == "awaiting-approval"
        or verdict.reason in REVIEW_BLOCK_REASONS
        or verdict.reason.startswith(MERGE_VERIFY_FAILED)
        or verdict.reason == MERGE_VERIFY_HEAD_MOVED
        or verdict.reason == MERGE_VERIFY_BASE_MOVED
        # An approved CLEAN PR whose tests await `ci-approved`, or whose head
        # carries no CI evidence, waits for a human too (#2033).
        or is_human_ci_gate(verdict.reason)
    )


def _classify_held_verdicts(
    held_verdicts: list[MergeVerdict],
    *,
    graph: Graph,
    holds_after: dict[str, EpicHold],
    now: datetime,
    stall_days: int,
) -> tuple[list[str], list[str], dict[str, StuckNode], bool]:
    """Fold the "held" verdicts into (held, self-healing held, stuck, held_live).

    Split out of `_classify_verdicts` so each stays readable: the held branch is
    the only one of the four with its own sub-classification (see
    `_classify_held_verdict`).
    """
    held: list[str] = []
    transient: list[str] = []
    stuck_by_id: dict[str, StuckNode] = {}
    held_live = False
    for verdict in held_verdicts:
        held_id, stuck_node, live, self_healing = _classify_held_verdict(
            verdict,
            graph=graph,
            holds_after=holds_after,
            now=now,
            stall_days=stall_days,
        )
        if held_id is not None:
            held.append(held_id)
            if self_healing:
                transient.append(held_id)
        if stuck_node is not None:
            stuck_by_id[stuck_node.node_id] = stuck_node
        held_live = held_live or live
    return held, transient, stuck_by_id, held_live


def _classify_verdicts(
    verdicts: list[MergeVerdict],
    *,
    graph: Graph,
    holds_after: dict[str, EpicHold],
    now: datetime,
    stall_days: int,
    report_merged_closed: bool = False,
) -> _MergeOutcome:
    merged: list[str] = []
    closed: list[str] = []
    deferred_closed: list[str] = []
    stuck_by_id: dict[str, StuckNode] = {}
    held_verdicts: list[MergeVerdict] = []
    for verdict in verdicts:
        if verdict.action == "merged":
            merged.append(verdict.node_id)
        elif verdict.reason == MERGED_OUT_OF_BAND_REASON:
            closed.append(verdict.node_id)
            if not report_merged_closed:
                # GitHub waits for the next provider read before DONE.
                deferred_closed.append(verdict.node_id)
        elif verdict.action == "stuck":
            blast = tuple(sorted(graph.transitive_dependents(verdict.node_id)))
            stuck_by_id[verdict.node_id] = StuckNode(
                verdict.node_id, verdict.reason, blast
            )
        elif verdict.action == "held":
            held_verdicts.append(verdict)
        # "skip": awaiting approval (an AWAITING_MERGES signal, folded in
        # `_run_tick` via `_is_awaiting_merge`), already merged out-of-band
        # (handled above), or no PR yet — nothing else to report here.
    held, held_transient, held_stuck, held_live = _classify_held_verdicts(
        held_verdicts,
        graph=graph,
        holds_after=holds_after,
        now=now,
        stall_days=stall_days,
    )
    stuck_by_id.update(held_stuck)
    return _MergeOutcome(
        merged=merged,
        held=held,
        stuck_by_id=stuck_by_id,
        held_live=held_live,
        held_transient=held_transient,
        closed=closed,
        deferred_closed=deferred_closed,
    )


def _stale_review_stuck(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    now: datetime,
    stall_days: int,
    self_healing_holds: AbstractSet[str] = frozenset(),
) -> list[StuckNode]:
    """IN_REVIEW nodes whose PR hasn't been pushed to in `stall_days`.

    ``self_healing_holds`` carries the nodes THIS tick's merge pass rewrote
    IN_REVIEW -> HELD while GitHub finishes computing mergeability, or while
    the head's own checks finish (#2183). That rewrite is the tick's own
    bookkeeping, not a change of state: the PR under it is the same open PR,
    and its last push is the same last push, so the review stall clock keeps
    running. Skipping them let a transient hold hide a review already past
    `stall_days` — `advance` reported AWAITING_MERGES where `summarize`,
    reading the node as plain IN_REVIEW, reported `review-stalled`, and
    `kew epic run` polled forever instead of exiting with the repair code.
    """
    out: list[StuckNode] = []
    for nid, ds in statuses.items():
        if ds is not DriverStatus.IN_REVIEW and nid not in self_healing_holds:
            continue
        facts = node_facts.get(nid)
        if facts is None or not _is_stale(
            facts.pr_pushed_at, now=now, stall_days=stall_days
        ):
            continue
        blast = tuple(sorted(graph.transitive_dependents(nid)))
        out.append(StuckNode(nid, "review-stalled", blast))
    return out


def _failed_terminal_reason(
    nid: str, store: RunStore, superseded_run_ids: frozenset[str] = frozenset()
) -> str:
    """`failed-terminal (exit N, category)` from the node's newest terminal run
    (#661) — the exit code + error_category name WHY the node died so the
    operator can act without opening the log. Degrades to plain
    `failed-terminal` when the node has no terminal run row. Keyed on the
    durable item id (``nid``), so a non-numeric tracker never crashes here.
    A run a host-restart marker supersedes is not the reason (#2969)."""
    runs = store.list_by_item(nid)
    newest = _newest_run(
        [
            r
            for r in runs
            if r.status != "pending" and r.run_id not in superseded_run_ids
        ]
    )
    if newest is None:
        return "failed-terminal"
    detail = f"exit {newest.exit_code}"
    if newest.error_category:
        detail += f", {newest.error_category}"
    return f"failed-terminal ({detail})" + _launch_failure_detail(store, newest)


#: The categories whose run carries the runtime's own failure words (#2913),
#: and ``resource``, whose run carries the signal and the runner's last output
#: (#3191): a node released from its resource-retry hold still says why.
_LAUNCH_DETAIL_CATEGORIES = frozenset(
    {"container_start_failed", "runtime_timeout", RESOURCE_CATEGORY}
)


def _launch_failure_detail(store: RunStore, run: RunRecord) -> str:
    """Why a container could not be created or started, or what killed its
    runner, as the run recorded.

    The reason was redacted and bounded where it was read
    (:func:`kew.sandbox.runtime_stderr_detail`, :mod:`kew.exit_signal`);
    nothing is added for any other failure.
    """
    if run.error_category not in _LAUNCH_DETAIL_CATEGORIES:
        return ""
    events = store.list_audit_events(
        1, event="sandbox_execution_failed", run_id=run.run_id
    )
    reason = (events[0].payload or {}).get("reason") if events else None
    return f": {reason}" if isinstance(reason, str) and reason else ""


def _failed_node_reason(
    nid: str,
    store: RunStore,
    preserved: str,
    history: ContinuationHistory | None,
    superseded_run_ids: frozenset[str] = frozenset(),
) -> str:
    """A FAILED node's stuck reason: its awaiting reason when the host died
    under its dirty work twice (#2964), else its failed-terminal reason
    normalized against its continuation history (#2960)."""
    if preserved.startswith(HOST_RESTART_REPEATED_CONTINUATION_REASON):
        return preserved
    return preserved_host_death_reason(
        _failed_terminal_reason(nid, store, superseded_run_ids), history
    )


def _terminal_ds_stuck(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    store: RunStore,
    superseded_runs_by_item: Mapping[str, frozenset[str]],
    continuation_reasons: Mapping[str, str],
    histories: Mapping[str, ContinuationHistory] | None = None,
) -> list[StuckNode]:
    """FAILED/CANCELLED are terminal-but-blocking: surface their blast radius
    (via `stuck`) alongside listing them in `failed`/`cancelled` — an
    operator needs to see exactly what a dead node holds up (spec
    "Failure & surfacing").

    A node failed with its work still preserved after a second dirty host
    death (#2964) names the continuation that recovers it, not a fresh retry.
    A node the host died under twice while it kept resumable preserved work
    reads :data:`HOST_RESTART_REPEATED_PRESERVED_REASON`, whose command is
    continue, not retry — or :data:`HOST_RESTART_REPEATED_AMBIGUOUS_REASON`,
    with no command, when the continue selector would refuse it (#2960)
    (:func:`kew.continuation_recovery.preserved_host_death_reason`)."""
    by_item = histories or {}
    out: list[StuckNode] = []
    for nid, ds in statuses.items():
        if ds is DriverStatus.FAILED:
            reason = _failed_node_reason(
                nid,
                store,
                continuation_reasons.get(nid, ""),
                by_item.get(nid),
                superseded_runs_by_item.get(nid, frozenset()),
            )
        elif ds is DriverStatus.CANCELLED:
            reason = "cancelled"
        else:
            continue
        blast = tuple(sorted(graph.transitive_dependents(nid)))
        out.append(StuckNode(nid, reason, blast))
    return out


def _review_heads(
    nid: str, node_facts: dict[str, NodeFacts], store: RunStore
) -> tuple[str | None, ReviewRound | None]:
    """The PR head the facts read, and the newest review round (#2561)."""
    facts = node_facts.get(nid)
    pr_head = facts.head_oid if facts is not None else None
    return pr_head, store.latest_review_round(nid)


def _unreviewed_head(pr_head: str | None, latest: ReviewRound | None) -> str | None:
    """The PR head when the newest round reviewed a different one, else None.

    Either head can be the newer: a push after a verdict moves the PR head, and
    a round recorded after the facts were read moves the reviewed one (#2561).
    Both are a pairing no verdict covers, so neither may read as merge-ready.
    """
    if not pr_head or latest is None or not latest.head_sha:
        return None
    return pr_head if latest.head_sha != pr_head else None


def _in_review_reason(
    nid: str, node_facts: dict[str, NodeFacts], store: RunStore
) -> str:
    facts = node_facts.get(nid)
    pr = facts.pr_number if facts is not None else None
    unreviewed = _unreviewed_head(*_review_heads(nid, node_facts, store))
    base = (
        f"review pending on {unreviewed}"
        if unreviewed is not None
        else "in_review — awaiting merge"
    )
    return f"{base} (PR #{pr})" if pr is not None else base


def _ci_gate_reasons(verdicts: list[MergeVerdict]) -> dict[str, str]:
    """The merge pass's head-check human gates, by node (#2033)."""
    return {
        v.node_id: v.reason
        for v in verdicts
        if v.action == "skip" and is_human_ci_gate(v.reason)
    }


def _released_to_review(
    statuses: dict[str, DriverStatus], gate_reasons: dict[str, str]
) -> dict[str, DriverStatus]:
    """A node this tick's merge pass found at a head-check human gate had its
    stale ``ci_pending`` hold released, so it reads IN_REVIEW, not HELD: its
    line then names the gate like any other in-review PR (#2033)."""
    return {
        nid: DriverStatus.IN_REVIEW
        if nid in gate_reasons and ds is DriverStatus.HELD
        else ds
        for nid, ds in statuses.items()
    }


def _with_ci_gate_reasons(
    waiting: list[StuckNode], gate_reasons: dict[str, str]
) -> list[StuckNode]:
    """Name the head-check human gate on an in-review node's line (#2033):
    `in_review — awaiting ci-approved: tests did not run (…) (PR #N)`."""
    return [
        StuckNode(
            w.node_id,
            w.reason.replace("awaiting merge", gate_reasons[w.node_id], 1),
            w.blast_radius,
        )
        if w.node_id in gate_reasons and w.reason.startswith("in_review")
        else w
        for w in waiting
    ]


#: The empty review-stage reason map (see `_with_review_stage_reasons`), as an
#: immutable module-level default — the report takes two of these.
_NO_REVIEW_REASONS: Mapping[str, str] = MappingProxyType({})


def _with_review_stage_reasons(
    waiting: list[StuckNode],
    reasons: Mapping[str, str],
    graph: Graph,
    *,
    stuck_ids: set[str],
) -> list[StuckNode]:
    """Name what the review stage actually did on the node's waiting line.

    Two reasons land here, and they share one shape: the stage acted, held
    nothing, and left the node's status unchanged — so the status-derived line
    would report the node as quietly in-review and hide the action.

    * a pre-launch sandbox refusal (#2147): the node still reads
      ADVERSARIAL_REVIEW and the next tick re-reviews it, but no reviewer is in
      flight, and reporting one hides an infrastructure failure behind a
      healthy-looking line;
    * a merge-main step (#2149): the node keeps its review status while the
      driver integrates the base, and the next tick reviews the moved head.

    The stage's reason replaces the status-derived one.
    """
    if not reasons:
        return waiting
    kept = [w for w in waiting if w.node_id not in reasons]
    return sorted(
        [
            *kept,
            *(
                StuckNode(nid, reason, tuple(sorted(graph.transitive_dependents(nid))))
                for nid, reason in reasons.items()
                if nid not in stuck_ids
            ),
        ],
        key=lambda s: item_id_sort_key(s.node_id),
    )


def _status_ci_gate_reasons(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    facts: GitHubFactsReader,
    gated_checks: Sequence[str],
) -> dict[str, str]:
    """`kew epic status` never runs the merge pass, so it reads the head
    checks of each approved, CLEAN in-review PR to show the same human gate
    the merge pass would (#2033). Reads only."""
    reasons: dict[str, str] = {}
    for nid, ds in statuses.items():
        nf = node_facts.get(nid)
        if (
            ds is not DriverStatus.IN_REVIEW
            or nf is None
            or nf.review_decision != "APPROVED"
            or nf.merge_state_status not in ("CLEAN", "HAS_HOOKS")
            or not nf.head_oid
        ):
            continue
        state, evidence = facts.head_checks(nf.head_oid, gated_checks)
        if state in ("awaiting_approval", "no_evidence"):
            reasons[nid] = evidence
    return reasons


#: What a node whose PR merged while its issue stayed open is waiting on:
#: nobody. The next tick's merge pass closes the issue. Named here because the
#: journal-backed watch reaches the same state by elimination — a ``held`` node
#: with no ``epic_holds`` row — and must say it in the same words (#2469).
MERGED_UNCLOSED_WAIT_REASON = (
    "merged_unclosed — the merge pass closes the issue on this tick"
)


def _held_wait_reason(
    nid: str,
    holds_by_item: dict[str, EpicHold],
    node_facts: dict[str, NodeFacts],
) -> str:
    facts = node_facts.get(nid)
    if facts is not None and facts.pr_state == "MERGED" and facts.state != "CLOSED":
        return MERGED_UNCLOSED_WAIT_REASON
    hold = holds_by_item.get(nid)
    return hold.reason if hold is not None else "held"


def _unmet_dependencies(
    nid: str,
    graph: Graph,
    frontier_status: dict[str, NodeStatus],
    dependency_effects: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    """Which dependencies still owe this node, and by which route (I12): a
    dependency an acknowledged no-change effect has satisfied for THIS node is
    met, even though it never reads DONE. Includes out-of-epic dependencies,
    which are boundary nodes in the graph like any other."""
    granted = dependency_effects or {}
    return {
        dep
        for dep in graph.dependencies_of(nid)
        if frontier_status.get(dep) is not NodeStatus.DONE
        and nid not in granted.get(dep, frozenset())
    }


def _blocked_todo_reason(
    nid: str,
    graph: Graph,
    frontier_status: dict[str, NodeStatus],
    dependency_effects: dict[str, frozenset[str]] | None = None,
) -> str | None:
    unmet = _unmet_dependencies(nid, graph, frontier_status, dependency_effects)
    if not unmet:
        return None
    return "blocked on " + ", ".join(item_ref(dep) for dep in sorted(unmet))


_FIXED_WAITING_REASONS = {
    DriverStatus.AWAITING_PUBLICATION: (
        "awaiting_publication — completed branch preserved; retry publication"
    ),
    DriverStatus.AWAITING_CONTINUATION: (
        "awaiting_continuation — preserved dirty work; use `kew epic continue` "
        "or `kew epic abandon`"
    ),
    DriverStatus.ADVERSARIAL_REVIEW: (
        "adversarial_review — fresh-context reviewer in flight"
    ),
    DriverStatus.REMEDIATING: "remediating — acting on review findings",
    DriverStatus.AWAITING_NO_CHANGE: (
        "awaiting_no_change — the run changed nothing; confirm or reject the"
        " recorded no-change outcome"
    ),
    DriverStatus.BLOCKED: "blocked (blocked label)",
    DriverStatus.QUEUED: ARM_WAIT_REASON,
}


def _first_held_path(store: RunStore, run_id: str) -> str:
    """The first path the publication filter held this run's diff on, or "".

    Read off the newest ``publication_deferred`` event, whose ``error`` is the
    hold's own ``publication needs human review — <path>: <why>; ...`` text:
    the run row keeps only the category, and a hint naming no path sends the
    operator looking for one (#2682).
    """
    try:
        events = store.list_audit_events(1, event="publication_deferred", run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - an unread ledger names no path
        _logger.warning("publication hold of %s names no path: %s", run_id, exc)
        return ""
    if not events:
        return ""
    error = str(dict(events[0].payload or {}).get("error", ""))
    _, dash, flags = error.partition(" — ")
    path, colon, _ = flags.split("; ", 1)[0].partition(": ")
    path = path.strip()
    # A filter flag is ``<path>: <why>``; a hold about the PR text instead
    # (``pr_body_too_long: ...``) has a token there, not a path.
    looks_like_path = ("/" in path or "." in path) and not any(
        c.isspace() for c in path
    )
    return path if dash and colon and looks_like_path else ""


def _publication_category(store: RunStore, run: RunRecord) -> str:
    """The run's publication category, with the held path when there is one."""
    category = run.error_category or "publication unavailable"
    if category != PUBLICATION_REVIEW_REQUIRED:
        return category
    path = _first_held_path(store, run.run_id)
    return f"{category} on {PROTECTED_PATH_MARK}{path}" if path else category


def _awaiting_publication_reason(
    nid: str,
    store: RunStore,
    continuation_reason: str | None,
) -> str:
    """Name publication recovery from the authoritative implementation run.

    A preserved-work continuation is deliberately retained after a host
    publication failure, but it is a last-resort recovery handle.  The newest
    implementation run determines the primary operator action and category.
    """
    implementation_runs = [
        run
        for run in store.list_by_item(nid)
        if run.run_kind not in PR_SUBJECT_RUN_KINDS
    ]
    latest = _newest_run(implementation_runs)
    if latest is not None and latest.status == "awaiting_publication":
        category = _publication_category(store, latest)
        reason = (
            f"awaiting_publication — {category}; open an external PR and "
            f"reconcile, or use `kew finalize {latest.run_id}`"
        )
    else:
        reason = _FIXED_WAITING_REASONS[DriverStatus.AWAITING_PUBLICATION]
    if continuation_reason:
        reason += f"; recovery of last resort: {continuation_reason}"
    return reason


def _review_identity(
    nid: str, node_facts: dict[str, NodeFacts], store: RunStore
) -> str:
    """The head and review round that make this node's review state what it is.

    The PR head leads: a force-push after a READY verdict IS a new state, and
    the round number separates two verdicts on one head. When the newest round
    reviewed a different head than the PR's, both are named (#2561) — a bare
    ``head:`` there paired one round's number with another commit. A node with
    neither reads as no identity rather than a fake one.
    """
    pr_head, latest = _review_heads(nid, node_facts, store)
    head = pr_head or (latest.head_sha if latest is not None else None)
    parts = [f"head:{head}"] if head else []
    if latest is not None and _unreviewed_head(pr_head, latest) is not None:
        parts.append(f"reviewed:{latest.head_sha}")
    if latest is not None:
        parts.append(f"round:{latest.round}")
    return " ".join(parts)


def _publication_identity(nid: str, store: RunStore) -> str:
    """The run kew could not publish — the identity of a publication hold.

    Same authoritative run `_awaiting_publication_reason` names, so the event's
    identity and its reason can never be about two different runs.
    """
    latest = _newest_run(
        [
            run
            for run in store.list_by_item(nid)
            if run.run_kind not in PR_SUBJECT_RUN_KINDS
        ]
    )
    return f"run:{latest.run_id}" if latest is not None else ""


def _node_identities(
    reported: Iterable[StuckNode],
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    store: RunStore,
) -> dict[str, str]:
    """Per-node state identity for the attention stream (#2183).

    Only the nodes an event can be about (the waiting and stuck rows) are keyed
    — a read that walked every node would pay a per-node store query for rows
    nobody reports on.
    """
    identities: dict[str, str] = {}
    for nid in sorted({node.node_id for node in reported}):
        identity = (
            _publication_identity(nid, store)
            if statuses.get(nid) is DriverStatus.AWAITING_PUBLICATION
            else _review_identity(nid, node_facts, store)
        )
        if identity:
            identities[nid] = identity
    return identities


def _waiting_reason(
    nid: str,
    ds: DriverStatus,
    *,
    node_facts: dict[str, NodeFacts],
    store: RunStore,
    holds_by_item: dict[str, EpicHold],
    continuation_reason_by_item: dict[str, str],
    graph: Graph,
    frontier_status: dict[str, NodeStatus],
    dependency_effects: dict[str, frozenset[str]],
) -> str | None:
    """Human 'why is this node not progressing' line for a non-terminal node,
    or None if the node isn't in a waiting state (#661)."""
    if ds is DriverStatus.IN_REVIEW:
        return _in_review_reason(nid, node_facts, store)
    if ds is DriverStatus.AWAITING_PUBLICATION:
        return _awaiting_publication_reason(
            nid, store, continuation_reason_by_item.get(nid)
        )
    if ds in _FIXED_WAITING_REASONS:
        return _FIXED_WAITING_REASONS[ds]
    if ds is DriverStatus.HELD:
        return continuation_reason_by_item.get(nid) or _held_wait_reason(
            nid, holds_by_item, node_facts
        )
    if ds is DriverStatus.TODO:
        return _blocked_todo_reason(nid, graph, frontier_status, dependency_effects)
    return None


def _waiting_nodes(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    store: RunStore,
    holds_by_item: dict[str, EpicHold],
    continuation_reason_by_item: dict[str, str],
    frontier_status: dict[str, NodeStatus],
    stuck_ids: set[str],
    dependency_effects: dict[str, frozenset[str]] | None = None,
) -> list[StuckNode]:
    """Non-terminal, non-progressing nodes (IN_REVIEW / HELD / BLOCKED /
    queued/admission-held/blocked/dependency-blocked TODO) with a reason line —
    the "waiting" half of a STALLED explanation (#661). Nodes already surfaced
    as stuck are skipped so
    each node appears once."""
    out: list[StuckNode] = []
    for nid, ds in statuses.items():
        if nid in stuck_ids:
            continue
        reason = _waiting_reason(
            nid,
            ds,
            node_facts=node_facts,
            store=store,
            holds_by_item=holds_by_item,
            continuation_reason_by_item=continuation_reason_by_item,
            graph=graph,
            frontier_status=frontier_status,
            dependency_effects=dependency_effects or {},
        )
        if reason is None:
            continue
        blast = tuple(sorted(graph.transitive_dependents(nid)))
        out.append(StuckNode(nid, reason, blast))
    return sorted(out, key=lambda s: s.node_id)


def _reconcile_budget_holds(
    name: str, store: RunStore, config: DispatchConfig, *, now: datetime
) -> None:
    """Clear ``budget_preflight`` holds whose cap now admits the node (#661).

    Runs before overlays so raising ``budget.per_issue_usd`` un-holds the node
    on the very next tick: a hold that still exceeds is left in place; one that
    now passes ``check_budget`` is cleared and the node returns to the frontier.
    """
    from kew.budget import BudgetExceeded, check_budget

    for hold in store.list_epic_holds(name):
        if not hold.reason.startswith("budget_preflight"):
            continue
        try:
            # Per-item keyed (#809): the budget check reads the node's own spend
            # via ``item_id`` so a non-numeric node isn't gated on the shared
            # ``0`` sentinel bucket — the same currency the clear below uses.
            check_budget(
                store, config.budget, hold.issue_num, item_id=hold.item_id, now=now
            )
        except BudgetExceeded:
            continue
        store.clear_epic_hold_item(hold.item_id)


def _reconcile_review_budget_holds(
    name: str, store: RunStore, config: DispatchConfig, *, now: datetime
) -> None:
    """Clear ``review_budget`` holds whose pool now admits the node (#2153).

    The review mirror of :func:`_reconcile_budget_holds`: an exhausted review or
    remediation pool HOLDs the node rather than dropping it, and raising the
    named cap resumes it on the very next tick — no ``kew epic unhold`` needed.
    Each hold is re-checked against the cap ITS reason names, because both pools
    sum the same review rows and differ only in their limit.
    """
    from kew.budget import BudgetExceeded, check_review_budget, review_budget_hold_cap

    for hold in store.list_epic_holds(name):
        cap = review_budget_hold_cap(hold.reason, config.review)
        if cap is None:
            continue
        try:
            check_review_budget(store, hold.item_id, cap, now=now)
        except BudgetExceeded:
            continue
        _logger.info(
            "epic %s: node %s un-held — the raised review cap ($%.2f) admits it "
            "again (was: %s)",
            name,
            hold.item_id,
            cap,
            hold.reason,
        )
        store.clear_epic_hold_item(hold.item_id)


def _reconcile_window_budget_holds(
    name: str, store: RunStore, config: DispatchConfig, *, now: datetime
) -> None:
    """Clear daily and weekly cap holds once the window admits a launch (#3205).

    A governed review or remediation the caps refused holds its node under
    ``budget: <daily|weekly> cap ...``. The hold names no launch, so it is
    re-checked as the dearer of the two (the larger review ceiling counted as
    one more run): the UTC day rolling over, the week's oldest spend ageing
    out, or a raised cap clears it, and the next tick launches once.

    A hold whose window no longer binds while the other still does (held on
    ``daily``, still past the weekly cap after UTC midnight) is rewritten to
    name the window that binds now. Only a change of window rewrites it, so a
    standing hold is not rewritten each tick as spend moves, and the operator
    gets one attention event for the change.
    """
    from kew.budget import (
        BudgetExceeded,
        check_window_budget,
        is_window_budget_hold,
        window_budget_hold_limit,
    )

    ceiling = max(config.review.max_review_usd, config.review.max_remediation_usd)
    for hold in store.list_epic_holds(name):
        if not is_window_budget_hold(hold.reason):
            continue
        held_on = window_budget_hold_limit(hold.reason)
        try:
            check_window_budget(
                store,
                config.budget,
                reservation_usd=ceiling,
                now=now,
                count_admitting_run=True,
            )
        except BudgetExceeded as exc:
            if exc.which_limit != held_on:
                _rewrite_window_budget_hold(name, store, hold, exc)
            continue
        _logger.info(
            "epic %s: node %s un-held — the daily and weekly caps admit it "
            "again (was: %s)",
            name,
            hold.item_id,
            hold.reason,
        )
        store.clear_epic_hold_item(hold.item_id)


def _rewrite_window_budget_hold(
    name: str, store: RunStore, hold: EpicHold, exc: BudgetExceeded
) -> None:
    """Rewrite a cap hold to name the window *exc* says binds now (#3205).

    Conditional on the row still carrying the reason this pass read, so a
    hold another writer put there in the meantime is never replaced.
    """
    from kew.budget import window_budget_hold_reason

    reason = window_budget_hold_reason(exc)
    rewritten = store.hold_epic_node_unless(
        name,
        hold.item_id,
        reason,
        keep=lambda current: current != hold.reason,
    )
    if rewritten:
        _logger.info(
            "epic %s: node %s still held — now on the %s cap (was: %s)",
            name,
            hold.item_id,
            exc.which_limit,
            hold.reason,
        )


def _log_admission_refusals(store: RunStore, name: str) -> None:
    """Say out loud, every tick, which nodes a dispatch child refused (#1658).

    The child runs detached with all three streams on ``DEVNULL``, so the hold
    it wrote is the ONLY surviving trace of why admission refused the node —
    and a hold nobody reads is the same silence the exit code used to be. Read
    them back here, before the overlays, so the reason reaches the driver log
    within one tick of the refusal. Logged every tick on purpose: the node is
    stuck until an operator acts, and the driver log is where they look.
    """
    for hold in store.list_epic_holds(name):
        if hold.reason.startswith(ADMISSION_REFUSED_HOLD_PREFIX):
            _logger.warning(
                "epic %s: node %s held — %s (held since %s)",
                name,
                hold.item_id,
                hold.reason,
                hold.held_at.isoformat(),
            )


def _review_live(
    statuses: dict[str, DriverStatus], launched: Sequence[str] = ()
) -> bool:
    """Is the governed-review stage doing real work right now (#977, #2107)?

    True when any node sits in the review lifecycle (ADVERSARIAL_REVIEW /
    REMEDIATING) or when this tick just took a review-stage action on one —
    ``launched`` carries the remediations it dispatched and the merge-main
    steps it ran (#2149), neither of which is visible in the node's status.
    Both `advance()` and `summarize()` read it, so `epic status` and `epic run`
    can never disagree about whether a remediating epic is progressing — a
    read-only snapshot that missed it printed `Signal: stalled` for an epic
    whose every non-done node was remediating or blocked behind one, and
    `epic run` exits non-zero on STALLED (#2107).
    """
    return bool(launched) or any(
        ds in (DriverStatus.ADVERSARIAL_REVIEW, DriverStatus.REMEDIATING)
        for ds in statuses.values()
    )


def _terminal_signal(
    *,
    remaining: int,
    dispatched: list[str],
    in_flight: list[str],
    held_live: bool,
    review_live: bool = False,
    awaiting_merges: bool = False,
) -> TerminalSignal:
    if remaining == 0:
        return TerminalSignal.COMPLETE
    if dispatched or in_flight or held_live or review_live:
        # A live reviewer/remediator (ADVERSARIAL_REVIEW / REMEDIATING) is real
        # compute in flight — the governed-review stage is progressing, not a
        # stalled pipeline (#977).
        return TerminalSignal.PROGRESSING
    # Frontier empty, nothing in-flight, no live hold: normally STALLED. But if
    # the only thing keeping the epic from done is PRs awaiting a human merge
    # (and nothing is stuck), that's the benign human-gate state, not a broken
    # pipeline (Node I7). Both callers decide that with `_awaiting_merge_only`,
    # so a tick and a read-only snapshot of one epic state agree (#2183).
    if awaiting_merges:
        return TerminalSignal.AWAITING_MERGES
    return TerminalSignal.STALLED


def _holds_reason_map(store: RunStore, name: str) -> dict[str, str]:
    return {h.item_id: h.reason for h in store.list_epic_holds(name)}


def _retry_map(store: RunStore, name: str) -> dict[str, EpicRetry]:
    return {r.item_id: r for r in store.list_epic_retries(name)}


def _holds_held_at_map(store: RunStore, name: str) -> dict[str, datetime]:
    return {h.item_id: h.held_at for h in store.list_epic_holds(name)}


def _holds_by_item(store: RunStore, name: str) -> dict[str, EpicHold]:
    return {h.item_id: h for h in store.list_epic_holds(name)}


def _claimed_push(
    status: DriverStatus, facts: NodeFacts, hold: EpicHold | None, *, ci_fix: bool
) -> str | None:
    """The push the node is next due, as the drive would take it, or None."""
    from kew.graph.driver.base_integration import base_integration_due

    if base_integration_due(facts):
        return "merge-main step"
    if status is DriverStatus.REMEDIATING:
        return "remediation"
    if ci_fix and hold is not None and is_ci_pending_hold(hold.reason):
        return "ci-fix"
    return None


def _merge_claim_reasons(
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    *,
    store: RunStore,
    holds_by_item: dict[str, EpicHold],
    now: datetime,
    ci_fix: bool,
) -> dict[str, str]:
    """``merge_claimed`` waits for ``kew epic status``, which runs no stage.

    The node whose next push — a merge-main step, a remediation, a ci-fix — an
    operator's merge claim holds back (#3198) reads exactly as a tick reports
    it, rather than as an idle review or a plain ``ci_pending`` hold.
    """
    from kew.merge_claim import merge_claim_wait_reason

    lookup = getattr(store, "live_merge_claim", None)
    if not callable(lookup):
        return {}
    reasons: dict[str, str] = {}
    for node_id, status in statuses.items():
        facts = node_facts.get(node_id)
        if facts is None or facts.pr_number is None or not facts.head_oid:
            continue
        action = _claimed_push(status, facts, holds_by_item.get(node_id), ci_fix=ci_fix)
        claim = (
            None if action is None else lookup(facts.pr_number, facts.head_oid, now=now)
        )
        if action is not None and claim is not None:
            reasons[node_id] = merge_claim_wait_reason(claim, action=action)
    return reasons


def _ids_with_status(
    statuses: dict[str, DriverStatus], target: DriverStatus
) -> list[str]:
    return sorted(
        (nid for nid, ds in statuses.items() if ds is target), key=item_id_sort_key
    )


def _merge_stuck(*groups: list[StuckNode]) -> dict[str, StuckNode]:
    """First-wins merge of several StuckNode groups, keyed by node id."""
    merged: dict[str, StuckNode] = {}
    for group in groups:
        for s in group:
            merged.setdefault(s.node_id, s)
    return merged


def _done_and_remaining(
    graph: Graph, frontier_status: dict[str, NodeStatus]
) -> tuple[list[str], int]:
    """Epic-member-only done/remaining counts. External boundary nodes
    (out-of-epic deps, `Node.external`) are observed for dependency
    satisfaction only — they are not part of this epic and must not
    inflate its done/remaining totals."""
    member_ids = [nid for nid, n in graph.nodes.items() if not n.external]
    done = sorted(
        (nid for nid in member_ids if frontier_status[nid] is NodeStatus.DONE),
        key=item_id_sort_key,
    )
    return done, len(member_ids) - len(done)


def _post_tick_frontier(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    frontier_status: dict[str, NodeStatus],
    *,
    reserved_item_ids: set[str],
    stuck_ids: set[str],
    dependency_effects: dict[str, frozenset[str]],
    priorities: Mapping[str, int] = _NO_PRIORITIES,
) -> list[str]:
    """The ready frontier AFTER this tick's actions — what the next tick
    would dispatch. Pure recompute from the patched statuses; no gh calls."""
    in_flight = sorted(
        (nid for nid, ds in statuses.items() if ds is DriverStatus.IN_PROGRESS),
        key=item_id_sort_key,
    )
    excluded = _dispatch_exclusions(
        graph,
        statuses,
        in_flight=in_flight,
        reserved_item_ids=reserved_item_ids,
        already_stuck_ids=stuck_ids,
    )
    return list(
        compute_frontier(
            _overlay_graph(graph, frontier_status),
            status=frontier_status,
            excluded=excluded,
            dependency_effects=dependency_effects,
            priorities=priorities,
        )
    )


def _write_journal(
    name: str,
    graph: Graph,
    result: TickResult,
    *,
    store: RunStore,
    statuses: dict[str, DriverStatus],
    node_facts: dict[str, NodeFacts],
    frontier: list[str],
) -> tuple[int, bool]:
    """Shared journal-append glue: stamp, build, record. Runs inside the
    single-driver lock; append failure propagates (fails the tick loudly).
    Returns the journal row's id and whether this tick inserted it."""
    tick_ts = datetime.now(UTC).isoformat()
    # The snapshot alone, read tolerantly: a latest row that is not JSON is
    # no predecessor, not a failed tick on every later tick (#2949).
    prev_snapshot = store.latest_epic_tick_snapshot(name)
    snapshot = journal.build_snapshot(
        epic_ref=name,
        graph=graph,
        statuses=statuses,
        node_facts=node_facts,
        # Both terminal-stuck and non-progressing "waiting" reasons flow into
        # the snapshot's per-node stuck map, so the epics API DTO's existing
        # `stuck` field carries the reason for held/in_review/blocked nodes too
        # (#661, additive — no schema migration). stuck/waiting are disjoint.
        stuck={s.node_id: s.reason for s in (*result.waiting, *result.stuck)},
        frontier=frontier,
        signal=result.signal.value,
        tick_ts=tick_ts,
        prev_snapshot=prev_snapshot,
    )
    return journal.record_tick(
        store,
        epic_ref=name,
        snapshot=snapshot,
        actions=journal.build_actions(result),
        tick_ts=tick_ts,
        prev_snapshot=prev_snapshot,
    )


def _held_ci_ids(
    verdicts: list[MergeVerdict], merge_outcome: _MergeOutcome
) -> list[str]:
    """Node ids this tick held on a red-CI ``ci_pending`` hold and still holds
    (a stale one already promoted to ci-stalled has left ``held``)."""
    return [
        v.node_id
        for v in verdicts
        if v.action == "held"
        and is_ci_pending_hold(v.held_reason)
        and v.node_id in merge_outcome.held
    ]


def _admission_closed(_facts: NodeFacts) -> bool:
    """The ci-fix dispatcher while ``kew drive`` admits nothing: dispatch none,
    which leaves the node held unchanged (#2616)."""
    return False


def _ci_fix_dispatcher(
    dispatcher: DispatchFixFn | None, max_parallel: int
) -> DispatchFixFn | None:
    """``dispatcher``, or :func:`_admission_closed` at a per-epic budget of 0."""
    return dispatcher if max_parallel > 0 else _admission_closed


def _yielding_to_verify(
    dispatcher: DispatchFixFn,
    node_facts: Mapping[str, NodeFacts],
    store: RunStore,
    config: DispatchConfig,
) -> DispatchFixFn:
    """*dispatcher*, deferring each ci-fix launch while a local merge verify
    runs (#3190): the node stays held on ``ci_pending``, unchanged, and the
    first tick after the verify dispatches its fix. The deferral names the
    node by its id, as every other launch's does — the pass hands over the
    very facts object it read for that node. A closed admission already
    dispatches nothing, and records no deferral."""
    if dispatcher is _admission_closed:
        return dispatcher

    def _dispatch(facts: NodeFacts) -> bool | PromptRefusal:
        node_id = next(
            (nid for nid, seen in node_facts.items() if seen is facts),
            str(facts.issue_num),
        )
        if verify_deferral(store, config, item_id=node_id, kind="ci-fix"):
            return False
        return dispatcher(facts)

    return _dispatch


def _run_ci_recovery(
    verdicts: list[MergeVerdict],
    merge_outcome: _MergeOutcome,
    overlays: _Overlays,
    graph: Graph,
    *,
    config: DispatchConfig,
    store: RunStore,
    now: datetime,
    repo: str,
    classify: ClassifyFn | None,
    dispatch_fix: DispatchFixFn | None,
    epic_ref: str | None = None,
) -> CiRecoveryOutcome:
    """Auto-ci-fix / infra-park pass (F5), gated on ``[epic] auto_ci_fix``.

    With admission closed by ``kew drive`` (#2616) the fix dispatcher is
    :func:`_admission_closed`: classification and parking still run, and a
    code-failure node stays held until admission reopens.

    Runs only when the operator has opted in AND this tick actually held nodes
    on ``ci_pending``. Infra-dead nodes are pulled out of ``merge_outcome.held``
    (they become stuck); code-failure nodes get a bounded ci-fix run dispatched
    and stay held while it runs. Returns an empty outcome (no-op) otherwise.
    """
    if not config.epic.auto_ci_fix:
        return CiRecoveryOutcome()
    held_ci_ids = _held_ci_ids(verdicts, merge_outcome)
    if not held_ci_ids:
        return CiRecoveryOutcome()
    outcome = ci_recovery_pass(
        held_ci_ids,
        graph=graph,
        node_facts=overlays.node_facts,
        config=config,
        store=store,
        now=now,
        classify=classify or default_classify(repo),
        dispatch_fix=_yielding_to_verify(
            dispatch_fix or default_dispatch_fix(config, store),
            overlays.node_facts,
            store,
            config,
        ),
        epic_ref=epic_ref,
    )
    parked_ids = {s.node_id for s in outcome.parked}
    if parked_ids:
        merge_outcome.held = [h for h in merge_outcome.held if h not in parked_ids]
    return outcome


def _post_action_report(
    graph: Graph,
    overlays: _Overlays,
    merge_outcome: _MergeOutcome,
    *,
    dispatched: list[str],
    in_flight: list[str],
    dispatch_stuck: list[StuckNode],
    reserved_item_ids: set[str],
    store: RunStore,
    holds_by_item: dict[str, EpicHold],
    now: datetime,
    stall_days: int,
    awaiting_merge: list[str],
    ci_gate_reasons: dict[str, str],
    extra_stuck: tuple[StuckNode, ...] = (),
    admission_held: tuple[StuckNode, ...] = (),
    review_refusals: Mapping[str, str] = _NO_REVIEW_REASONS,
    remediating: Sequence[str] = (),
    integrated: Mapping[str, str] = _NO_REVIEW_REASONS,
    continuations_deferred: Sequence[str] = (),
) -> tuple[TickResult, dict[str, DriverStatus], list[str]]:
    """Fold THIS tick's own actions (dispatch + merge/close outcomes) back onto
    the pre-action overlay, then derive every reported field from that POST-action
    view — so stdout and the journal agree on what the tick just did (SFD-139).

    The merge pass runs on the tick-start snapshot, so a node it merges or
    closes still reads IN_REVIEW in `overlays.statuses`; reporting straight off
    that snapshot printed `in_review — awaiting merge` (and STALLED) for a node
    the tick had just closed. Patching first fixes both the summary and the
    frontier/signal: a merge that unblocks a dependent reads PROGRESSING, not
    STALLED. Returns `(result, patched_statuses, post_tick_frontier)`; the
    latter two feed the journal so it records the same patched view.
    """
    deferred_closed = set(merge_outcome.deferred_closed)
    patched = journal.apply_action_statuses(
        overlays.statuses,
        dispatched=tuple(dispatched),
        held=tuple(merge_outcome.held),
        merged=tuple(merge_outcome.merged),
        closed=tuple(
            node_id
            for node_id in merge_outcome.closed
            if node_id not in deferred_closed
        ),
    )
    patched = _released_to_review(patched, ci_gate_reasons)
    frontier_status = _frontier_status(graph, patched)
    stuck_by_id = _merge_stuck(
        # Infra-dead parks first so their explicit diagnostic wins (first-wins).
        list(extra_stuck),
        list(merge_outcome.stuck_by_id.values()),
        overlays.stuck,
        dispatch_stuck,
        _stale_review_stuck(
            graph,
            patched,
            overlays.node_facts,
            now=now,
            stall_days=stall_days,
            # `patched` carries this tick's own IN_REVIEW -> HELD rewrites; the
            # self-healing ones keep the stall clock they had before it (#2183).
            self_healing_holds=frozenset(merge_outcome.held_transient),
        ),
        _terminal_ds_stuck(
            graph,
            patched,
            store,
            overlays.superseded_runs_by_item,
            overlays.continuation_reason_by_item,
            overlays.continuation_histories,
        ),
    )
    waiting = _waiting_nodes(
        graph,
        patched,
        overlays.node_facts,
        store=store,
        holds_by_item=holds_by_item,
        continuation_reason_by_item=overlays.continuation_reason_by_item,
        frontier_status=frontier_status,
        stuck_ids=set(stuck_by_id),
        dependency_effects=overlays.dependency_effects,
    )
    waiting = _with_ci_gate_reasons(waiting, ci_gate_reasons)
    waiting = _with_deferred_continuation_reasons(
        waiting, admission_held, continuations_deferred
    )
    # Fold in ready-but-cap-held nodes (#1041) so a full-cap tick reports WHY it
    # skipped ready work. Skip any already surfaced as stuck/waiting (no dup).
    waiting_ids = {w.node_id for w in waiting}
    waiting = sorted(
        [
            *waiting,
            *(
                s
                for s in admission_held
                if s.node_id not in stuck_by_id and s.node_id not in waiting_ids
            ),
        ],
        key=lambda s: item_id_sort_key(s.node_id),
    )
    waiting = _with_review_stage_reasons(
        waiting, {**review_refusals, **integrated}, graph, stuck_ids=set(stuck_by_id)
    )
    post_frontier = _post_tick_frontier(
        graph,
        patched,
        frontier_status,
        reserved_item_ids=reserved_item_ids,
        stuck_ids=set(stuck_by_id),
        dependency_effects=overlays.dependency_effects,
        priorities=_frontier_priorities(overlays.node_facts),
    )
    done, remaining = _done_and_remaining(graph, frontier_status)
    # AWAITING_MERGES only when PRs awaiting a human merge are the sole reason
    # the epic isn't done: a stuck node (failed/cancelled/ci-stalled) still
    # needs an operator, so it stays STALLED (Node I7), and so does any other
    # waiting row the merge would not clear. `_awaiting_merge_only` is the same
    # reading `summarize` gives this epic — a tick that answered it from the
    # verdicts alone reported AWAITING_MERGES where `status` reported stalled.
    # A self-healing HOLD counts as an awaiting-merge candidate too — GitHub
    # still computing mergeability, or the head's own checks still running: its
    # PR is open and approved, and it is the same node `summarize` sees as
    # plain IN_REVIEW (#2183).
    awaiting_merges = bool(awaiting_merge or merge_outcome.held_transient) and (
        _awaiting_merge_only(
            waiting,
            stuck_by_id,
            patched,
            graph=graph,
            frontier_status=frontier_status,
            dependency_effects=overlays.dependency_effects,
            # `patched` carries this tick's own IN_REVIEW -> HELD rewrites; hand
            # the self-healing ones back so the closure still sees the open PR
            # under them (#2183).
            self_healing_holds=frozenset(merge_outcome.held_transient),
        )
    )
    # A merge-main step is review-stage work in progress just as a launched
    # remediation is (#2149): the node it ran for reads IN_REVIEW, so without
    # this a tick whose only action was integrating a base reports STALLED and
    # `kew epic run` exits non-zero on an epic that just made progress.
    review_live = _review_live(patched, [*remediating, *integrated])
    signal = _terminal_signal(
        remaining=remaining,
        # A non-empty post-action frontier means the tick's own merges unblocked
        # work the next tick will dispatch — progress, not a stall (matches
        # `summarize`'s frontier-as-progress reading). An armed continuation
        # admission deferred is the same: a later tick claims it (#2964).
        dispatched=[*dispatched, *post_frontier, *continuations_deferred],
        in_flight=in_flight,
        held_live=merge_outcome.held_live,
        review_live=review_live,
        awaiting_merges=awaiting_merges,
    )
    result = TickResult(
        dispatched=tuple(sorted(dispatched, key=item_id_sort_key)),
        merged=tuple(sorted(merge_outcome.merged, key=item_id_sort_key)),
        held=tuple(sorted(merge_outcome.held, key=item_id_sort_key)),
        in_flight=tuple(sorted(in_flight, key=item_id_sort_key)),
        failed=tuple(_ids_with_status(patched, DriverStatus.FAILED)),
        cancelled=tuple(_ids_with_status(patched, DriverStatus.CANCELLED)),
        stuck=tuple(sorted(stuck_by_id.values(), key=lambda s: s.node_id)),
        done=tuple(done),
        remaining=remaining,
        signal=signal,
        waiting=tuple(waiting),
        closed=tuple(sorted(merge_outcome.closed, key=item_id_sort_key)),
    )
    return result, patched, post_frontier


def _with_deferred_continuation_reasons(
    waiting: list[StuckNode],
    admission_held: tuple[StuckNode, ...],
    deferred: Sequence[str],
) -> list[StuckNode]:
    """Name the limiter an armed continuation waits on this tick (#2964).

    Its continuation wait stays first — it is the same row ``summarize``
    reports — and the admission reason that deferred the claim follows it.
    """
    limiters = {s.node_id: s.reason for s in admission_held if s.node_id in deferred}
    return [
        replace(w, reason=f"{w.reason}; {limiters[w.node_id]}")
        if w.node_id in limiters
        else w
        for w in waiting
    ]


def _journal_invalid(
    name: str, graph: Graph, result: TickResult, *, store: RunStore
) -> tuple[int, bool]:
    """Invalid graph mid-drain still journals: the page must show WHY the
    drain froze, not silently freeze (control-flow table row 3)."""
    return _write_journal(
        name, graph, result, store=store, statuses={}, node_facts={}, frontier=[]
    )


def _finish_heartbeat(
    store: RunStore,
    name: str,
    started: datetime,
    *,
    interval_s: float | None,
    expected_interval_s: float | None,
    signal: TerminalSignal,
    tick_id: int | None,
) -> None:
    ended = datetime.now(UTC)
    store.finish_epic_tick(
        name,
        tick_ts=ended.isoformat(),
        duration_s=(ended - started).total_seconds(),
        interval_s=interval_s,
        expected_interval_s=expected_interval_s,
        signal=signal.value,
        tick_id=tick_id,
    )


def _pr_links(node_facts: dict[str, NodeFacts]) -> tuple[PrLink, ...]:
    """Report each PR link's provenance and closing-reference fact."""
    return tuple(
        PrLink(
            node_id,
            facts.pr_number,
            facts.pr_link_source,
            facts.pr_has_closing_ref,
        )
        for node_id, facts in sorted(
            node_facts.items(), key=lambda kv: item_id_sort_key(kv[0])
        )
        if facts.pr_number is not None and facts.pr_link_source is not None
    )


def _apply_review_race_overlays(overlays: _Overlays, review_races: Any) -> None:
    """Keep review-race holds authoritative for the rest of this tick."""
    for nid in review_races.held_pr_closed:
        overlays.statuses[nid] = DriverStatus.HELD


def _admission_waiting_for_tick(
    graph: Graph, admission_held: list[str], hold_reason: str | None
) -> tuple[StuckNode, ...]:
    """Build admission-held waiting entries when the budget produced a reason."""
    if hold_reason is None:
        return ()
    return _admission_held_waiting(graph, admission_held, reason=hold_reason)


def _run_tick(
    name: str,
    graph: Graph,
    *,
    config: DispatchConfig,
    store: RunStore,
    facts: GitHubFactsReader,
    dispatch_sink: DispatchSink,
    max_parallel: int,
    now: datetime,
    approvers: list[str],
    repo: str,
    base: str,
    tracker: LinearTracker | None = None,
    ci_classifier: ClassifyFn | None = None,
    ci_fix_dispatcher: DispatchFixFn | None = None,
    reviewer_launcher: ReviewerLauncher | None = None,
    remediator: Remediator | None = None,
    base_integrator: BaseIntegrator | None = None,
    review_detacher: ReviewDetacher | None = None,
    run_liveness: RunLivenessProbe | None = None,
    revoked_hashes: set[str] | None = None,
    lock_keepalive: Callable[[], None] | None = None,
    operator_stop: bool = False,
) -> tuple[TickResult, tuple[int, bool]]:
    """The lock-held, graph-validated body of one tick. See `advance`.

    Returns the result and :func:`_write_journal`'s (journal id, inserted).

    *lock_keepalive* refreshes the driver lock's heartbeat mid-tick. `advance`
    supplies it; only the merge queue — the one step that can block the tick for
    longer than the lock's staleness window — uses it.
    """
    provider = provider_from_config(config)
    # One liveness probe for every reservation sweep this tick (#1041): reaps a
    # pending run whose owning process/container is provably dead across the
    # driven epic AND the delegation/review pseudo-epics.
    liveness = (
        run_liveness if run_liveness is not None else default_run_liveness(config)
    )
    reap_dispatch_children(dispatch_sink)
    reap_review_children(review_detacher)
    reserved_item_ids = _reconcile_reservations(
        name, store, config, now=now, liveness=liveness
    )
    # Task 5: also sweep the delegation pseudo-epic. A watcher that crashes
    # after dispatch leaves reservations pinned under _DELEGATION_EPIC_REF
    # until it restarts; the driver tick is the catch-all sweep site.
    _reconcile_reservations(
        _DELEGATION_EPIC_REF, store, config, now=now, liveness=liveness
    )
    # N2 (#977): also sweep the governed-review pseudo-epic. A reviewer or
    # remediation run parks its reservation under REVIEW_EPIC_REF; if it dies
    # before its terminal-path release, the reservation would pin a slot until
    # the next sweep. The returned set is discarded on purpose — review
    # reservations must NOT enter this epic's reserved_item_ids (they would
    # corrupt node status via the kind-blind overlay precedence).
    _reconcile_reservations(REVIEW_EPIC_REF, store, config, now=now, liveness=liveness)
    reconcile_driver_keys(store, now=now, seen=revoked_hashes)
    _attest_continuation_states(store, config, name)
    # Un-hold budget-preflight nodes whose cap now admits them BEFORE overlays,
    # so a raised cap re-admits the node to the frontier this very tick (#661).
    _reconcile_budget_holds(name, store, config, now=now)
    # Same contract for the review pools (#2153): a node held on an exhausted
    # review/remediation pool re-enters the review stage this tick once the
    # operator raises the cap its hold reason names.
    _reconcile_review_budget_holds(name, store, config, now=now)
    _reconcile_window_budget_holds(name, store, config, now=now)
    # A node whose run ran out of a resource is re-admitted once per head
    # (#3191), BEFORE overlays so it re-enters this very tick's frontier.
    retry_resource_failures(graph, epic_ref=name, store=store, config=config)
    # Surface a detached child's admission refusal in the driver log (#1658).
    # After the budget un-hold above, so a hold the raised cap just cleared is
    # not reported as still-refused.
    _log_admission_refusals(store, name)
    # I12: record and advance the per-dependent effects an authorized no-change
    # owes, BEFORE the overlays — an effect acknowledged here is a fact this
    # tick's own frontier may consume.
    _reconcile_no_change_effects(graph, store=store, provider=provider, now=now)
    merged_linear = _reconcile_merged_linear(
        graph,
        provider=provider,
        facts=facts,
        store=store,
        tracker=tracker,
        config=config,
        now=now,
    )
    overlays = _build_overlays(
        graph,
        name=name,
        provider=provider,
        store=store,
        facts=facts,
        reserved_item_ids=reserved_item_ids,
        held_reason_by_item=_holds_reason_map(store, name),
        retry_by_item=_retry_map(store, name),
        config=config,
        reconciled_merged_items={
            validate_item_id(node_id, provider) for node_id in merged_linear
        },
        reconcile_links=True,
    )
    if provider == "github":
        _repair_closes_markers(overlays.node_facts, facts=facts, repo=repo)

    # F6/F7 (#977): resolve human actions taken while a reviewer is live BEFORE
    # the merge pass — kill+reconcile the reviewer and either attest an
    # out-of-band MERGED_UNREVIEWED, or HELD review_pr_closed so a superseded
    # node never re-derives to TODO and re-enters the frontier (#753 class).
    review_races = _handle_review_races(
        [n.id for n in graph.nodes.values() if not n.external],
        epic_ref=name,
        provider=provider,
        facts=facts,
        store=store,
        config=config,
        repo=repo,
        now=now,
    )
    _apply_review_race_overlays(overlays, review_races)
    # #2869: the drive's merge pass never reaches an excluded epic, so its READY
    # PR's base conflict is held here — before the review stage resolves it and
    # before this tick journals — not one tick later, after the round.
    _hold_excluded_epic_conflicts(name, overlays, config=config, store=store)

    # #991: the connective tissue — dispatch a fresh-context reviewer for every
    # review-enabled node due one, collect + parse the sidecar, record the round,
    # emit the attested verdict, and drive the transitions #988 defined. Runs
    # BEFORE the merge pass so a node this pass HELDs (round cap / unparseable in
    # blocking mode) is caught by the merge-pass invariant rather than auto-merged.
    review_outcome = drive_review_stage(
        overlays.statuses,
        overlays.node_facts,
        epic_ref=name,
        config=config,
        store=store,
        repo=repo,
        # The reviewer worktree is cloned from repo_root, so it MUST be the git
        # toplevel — NOT Path.cwd(), which is the `kew/` package subdir when kew
        # is run from there, making `git clone <cwd>` fail (128) and crash the
        # tick (#1002). policy.repo_root walks up from the project to the .git
        # anchor, never from the cwd, so it names the copy `kew prompts` previews
        # (#3108).
        repo_root=_git_repo_root(project_root()),
        base=base,
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=base_integrator,
        worker_cap=config.fleet_concurrency(),
        max_concurrent=_review_concurrency(
            store,
            epic_ref=name,
            max_parallel=max_parallel,
            fleet_cap=config.fleet_concurrency(),
            liveness=liveness,
        ),
        now=now,
        # #2459: with a detacher wired, each due node's action runs in a
        # detached child holding its review reservation, so this returns in
        # seconds; the next tick reaps the child and reads what it recorded.
        detacher=review_detacher,
    )

    verdicts = _run_merge_pass(
        name,
        overlays.statuses,
        repo=repo,
        base=base,
        facts=facts,
        approvers=approvers,
        store=store,
        tracker=tracker,
        blocking_review=config.review.adversarial and config.review.blocking,
        pending_review_label=config.labels.pr_pending_review,
        gated_checks=config.ci.approval_gated_checks,
        merge_verify=_merge_verifier(
            config,
            store,
            epic_ref=name,
            repo=repo,
            base=base,
            keepalive=lock_keepalive,
        ),
        # A node the merge-main step just integrated is NOT a merge candidate
        # this tick (#2149): its head moved AFTER `facts` was read. Classified on
        # the pre-step `DIRTY` fact anyway, the pass re-held it `ci_pending` —
        # sticky, unreachable by the review stage's due pass, and refused by the
        # merge gate on the verdict the moved head had just staled, so the node
        # was never re-reviewed nor merged. A step that conflicted or could not
        # run leaves the branch as it was, so it stays a candidate.
        spent_facts=frozenset(review_outcome.integrated)
        | frozenset(review_outcome.detached),
        observed=overlays.node_facts,
    )
    merge_outcome = _classify_verdicts(
        verdicts,
        graph=graph,
        holds_after=_holds_by_item(store, name),
        now=now,
        stall_days=config.epic.stall_days,
        report_merged_closed=provider == "linear",
    )
    # Fold the review-race outcomes into this tick's action summary: a bypassed
    # (merged-unreviewed) node is terminal-done; a superseded one is HELD.
    merge_outcome.closed.extend(review_races.merged_unreviewed)
    merge_outcome.held.extend(review_races.held_pr_closed)
    merge_outcome.closed.extend(merged_linear)

    recovery = _run_ci_recovery(
        verdicts,
        merge_outcome,
        overlays,
        graph,
        config=config,
        store=store,
        now=now,
        repo=repo,
        classify=ci_classifier,
        dispatch_fix=_ci_fix_dispatcher(ci_fix_dispatcher, max_parallel),
        epic_ref=name,
    )

    frontier_status = _frontier_status(graph, overlays.statuses)
    already_stuck_ids = {s.node_id for s in overlays.stuck} | set(
        merge_outcome.stuck_by_id
    )
    pressure = _review_pressure(store, config=config, repo=repo, now=now)
    frontier_outcome = _dispatch_frontier(
        graph,
        _overlay_graph(graph, frontier_status),
        frontier_status,
        overlays.statuses,
        provider=provider,
        reserved_item_ids=reserved_item_ids,
        already_stuck_ids=already_stuck_ids,
        name=name,
        store=store,
        dispatch_sink=dispatch_sink,
        config=config,
        max_parallel=max_parallel,
        fleet_cap=config.fleet_concurrency(),
        branch_prefix=config.branches.prefix,
        pressure=pressure,
        dependency_effects=overlays.dependency_effects,
        priorities=_frontier_priorities(overlays.node_facts),
        liveness=liveness,
        operator_stop=operator_stop,
    )
    dispatched = frontier_outcome.dispatched
    admission_waiting = _admission_waiting_for_tick(
        graph, frontier_outcome.admission_held, frontier_outcome.hold_reason
    ) + _admission_held_waiting(
        graph, frontier_outcome.review_withheld, reason=pressure.reason()
    )

    result, patched, post_frontier = _post_action_report(
        graph,
        overlays,
        merge_outcome,
        dispatched=dispatched,
        in_flight=frontier_outcome.in_flight,
        dispatch_stuck=frontier_outcome.stuck,
        reserved_item_ids=reserved_item_ids,
        store=store,
        holds_by_item=_holds_by_item(store, name),
        now=now,
        stall_days=config.epic.stall_days,
        awaiting_merge=[v.node_id for v in verdicts if _is_awaiting_merge(v)],
        extra_stuck=tuple(recovery.parked),
        admission_held=admission_waiting,
        continuations_deferred=frontier_outcome.continuations_deferred,
        ci_gate_reasons=_ci_gate_reasons(verdicts),
        # A ci-fix an operator's merge claim held back reads `merge_claimed`,
        # not the bare `ci_pending` hold it sits on (#3198).
        review_refusals={**review_outcome.refused, **recovery.claimed},
        remediating=tuple(review_outcome.remediating),
        # A detached review child is review-stage work in flight exactly as a
        # merge-main step run in-tick was (#2459): same waiting line, and never
        # a STALLED tick while it runs.
        integrated={**review_outcome.integrated, **review_outcome.detached},
    )
    result = replace(
        result,
        dispatch_ranks=frontier_outcome.dispatch_ranks,
        gate_skipped=_tick_gate_skips(
            overlays, review_outcome, config=config, store=store
        ),
    )
    journaled = _write_journal(
        name,
        graph,
        result,
        store=store,
        statuses=patched,
        node_facts=overlays.node_facts,
        frontier=post_frontier,
    )
    return result, journaled


def _tick_gate_skips(
    overlays: _Overlays,
    review_outcome: ReviewStageOutcome,
    *,
    config: DispatchConfig,
    store: RunStore,
) -> tuple[tuple[str, str], ...]:
    """Every review-stage node's pre-review gate skip, for the tick line.

    A detached review child records its gate's skip in the run store, never
    on this tick's outcome (#3344), so the store is read for each node at its
    current head. A node the merge-main step just moved is left out: the head
    the tick read is no longer its head.
    """
    recorded = recorded_gate_skips(
        overlays.statuses, overlays.node_facts, config=config, store=store
    )
    skips = {
        node_id: why
        for node_id, why in recorded.items()
        if node_id not in review_outcome.integrated
    }
    skips.update(review_outcome.gate_skipped)
    return tuple(sorted(skips.items()))


def _lock_keepalive(lock_path: Path, holder: str | None) -> Callable[[], None]:
    """A mid-tick heartbeat refresh for this driver's own lock.

    The tick-end refresh alone leaves a tick that legitimately blocks for longer
    than ``HEARTBEAT_STALE_SECONDS`` looking like a hung driver, and a stale
    heartbeat is stolen automatically (#970) — two live drivers on one epic. A
    step that can block that long (the merge queue's combined-tree verification,
    #2148) beats this instead of going silent. ``refresh_lock_heartbeat`` is
    ownership-checked, so this never resurrects a lock genuinely stolen from us.
    """

    def _beat() -> None:
        if not refresh_lock_heartbeat(lock_path, holder):
            _logger.warning(
                "epic %s: mid-tick heartbeat refused — another driver holds the lock",
                holder,
            )

    return _beat


def _locked_out_signal(lock_path: Path) -> TerminalSignal:
    """A locked-out tick's signal. Another live driver ticking is progress; a
    live holder that stopped beating its heartbeat is never stolen from, so the
    epic is STALLED until a person kills it (#3331)."""
    if stalled_holder(lock_path) is not None:
        return TerminalSignal.STALLED
    return TerminalSignal.PROGRESSING


def _tick_outcome(result: TickResult) -> str:
    """What one finished tick DID, as an ``epic_tick_log`` outcome (#2304).

    Read in the order an operator cares about rather than by severity: a tick
    that dispatched is the one somebody was waiting for, even if it also merged
    something on the way past. A tick that did none of the three is ``nothing``
    — which is the roughly nine-in-ten case the change journal never recorded,
    and the whole reason this row exists.
    """
    if result.locked_out:
        return "locked_out"
    if result.dispatched:
        return "dispatched"
    if result.merged or result.closed:
        return "merged"
    if result.held:
        return "held"
    return "nothing"


class _TickLog:
    """One ``epic_tick_log`` row per tick ATTEMPT, opened on entry (#2304).

    The row is opened before the tick does anything and closed by
    :meth:`record` on every path that produces a result, or by ``__exit__``
    with the exception's class on the paths that raise — the empty graph and
    anything unclassified. A row left open (no ``ts_finished``) is a tick still
    running, which is the one state the strip draws as live, so a driver killed
    mid-tick leaves an honest open row rather than a missing one.

    Nothing closes THAT row from inside the dead process, so
    :meth:`sweep_abandoned` closes it from the next tick of the same epic —
    without it a strip paints that attempt as a tick still running for the rest
    of the epic's life.

    Every write here is BEST-EFFORT, unlike the change journal's append: this
    table is how an operator reads what the driver has been doing, and a
    diagnostic that can fail a tick is worse than a diagnostic with a gap. A
    failed write is logged and the tick proceeds.
    """

    def __init__(self, store: RunStore, epic_ref: str) -> None:
        self._store = store
        self._epic_ref = epic_ref
        self._row_id: int | None = None

    def __enter__(self) -> _TickLog:
        try:
            self._row_id = self._store.begin_epic_tick_log(
                self._epic_ref, datetime.now(UTC).isoformat()
            )
        except Exception:  # pragma: no cover - diagnostic must not fail a tick
            _logger.warning(
                "epic %s: could not open a tick-log row", self._epic_ref, exc_info=True
            )
        return self

    def sweep_abandoned(self) -> None:
        """Close rows this epic's earlier drivers left open (#2304).

        Call only while this process HOLDS the epic's single-driver lock: that
        is what makes the inference sound — no other driver can be mid-tick on
        this epic, so a row of it still open from before ours belongs to a tick
        nothing will ever close.

        The reader owns the other half of the bound. An epic whose driver dies
        and never returns has no next tick to run this sweep, so the strip also
        stops reading an open row as live once it is older than
        ``TICK_LIVE_MAX_MS`` (``frontend/src/lib/factory/tick-strip.ts``) AND
        the served ``driver_status`` no longer says ``live``. Both halves,
        because that constant is an age of the tick's START, not of the last
        heartbeat: on its own it would call a tick that legitimately outlasts
        it — the very case :func:`_lock_keepalive` exists to support —
        abandoned while this driver is demonstrably beating.
        """
        if self._row_id is None:
            return
        try:
            self._store.abandon_open_epic_tick_logs(
                self._epic_ref,
                before_id=self._row_id,
                ts_finished=datetime.now(UTC).isoformat(),
            )
        except Exception:  # pragma: no cover - diagnostic must not fail a tick
            _logger.warning(
                "epic %s: could not sweep abandoned tick-log rows",
                self._epic_ref,
                exc_info=True,
            )

    def reached(self) -> None:
        """Mark the open row as having reached its tick (#2882).

        Call right after ``begin_epic_tick`` commits. The row was opened before
        the lock, the preflight and the graph build, so an open row alone is no
        heartbeat: one blocked in preflight read as live while no tick ran.
        """
        if self._row_id is None:
            return
        try:
            self._store.mark_epic_tick_log_reached(self._row_id)
        except Exception:  # pragma: no cover - diagnostic must not fail a tick
            _logger.warning(
                "epic %s: could not mark tick-log row %d as reached",
                self._epic_ref,
                self._row_id,
                exc_info=True,
            )

    def record(
        self,
        result: TickResult,
        *,
        journal_tick_id: int | None = None,
        journaled: bool = False,
    ) -> None:
        """Close the row with what the tick did.

        *journaled* says whether this tick inserted the journal row
        *journal_tick_id* names; a quiet tick carries the id of the row it
        matched, and the change strip must not draw it as the change (#2949).
        It defaults to False: a row is the change only when its caller says
        its own append inserted it.

        ``n_merged`` counts the out-of-band-merged set (``closed``) alongside
        the merges kew performed: the strip's question is "did this tick take a
        node to done", and both answers are yes. The ``actions`` column of the
        journal row this points at keeps them apart (#2304).
        """
        self._close(
            outcome=_tick_outcome(result),
            signal=result.signal.value,
            n_dispatched=len(result.dispatched),
            n_merged=len(result.merged) + len(result.closed),
            n_held=len(result.held),
            n_stuck=len(result.stuck),
            journal_tick_id=journal_tick_id,
            journaled=journaled,
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None or self._row_id is None:
            return
        # The class name only — never str(exc), which carries repo paths and
        # occasionally a token-bearing URL into a row the console shows.
        #
        # The empty-graph outcome is read back by `list_epic_refs`, which keeps
        # a ref whose attempts all end here OFF the roster — a typo'd name is
        # not an epic — so writer and reader share the one constant (#2438).
        self._close(
            outcome=(
                EPIC_TICK_OUTCOME_EMPTY_GRAPH
                if issubclass(exc_type, EmptyEpicGraphError)
                else "failed"
            ),
            error_class=exc_type.__name__,
            # A raised tick closes with no journal id; a row its append did
            # commit is drawn off the journal itself (#2882).
            journaled=False,
        )

    def _close(self, *, outcome: str, **fields: Any) -> None:
        row_id, self._row_id = self._row_id, None
        if row_id is None:
            return
        try:
            self._store.finish_epic_tick_log(
                row_id,
                ts_finished=datetime.now(UTC).isoformat(),
                outcome=outcome,
                **fields,
            )
        except Exception:  # pragma: no cover - diagnostic must not fail a tick
            _logger.warning(
                "epic %s: could not close tick-log row %d",
                self._epic_ref,
                row_id,
                exc_info=True,
            )


def _with_graphql_points(name: str, result: TickResult, *readers: Any) -> TickResult:
    """Stamp and log the GraphQL points this tick's GitHub reads spent (#2582).

    Each reader meters its own queries (``graphql_points``); one that does not
    — a Linear epic's, a test double — contributes nothing, and a tick none of
    whose readers meter prints no figure rather than a false zero.
    """
    spent = [
        points
        for points in (getattr(reader, "graphql_points", None) for reader in readers)
        if isinstance(points, int) and not isinstance(points, bool)
    ]
    if not spent:
        return result
    _logger.info("epic %s: tick spent %d GitHub GraphQL points", name, sum(spent))
    return replace(result, graphql_points=sum(spent))


def advance(
    name: str,
    *,
    config: DispatchConfig,
    store: RunStore,
    deps: DependencySource,
    facts: GitHubFactsReader,
    dispatch_sink: DispatchSink,
    max_parallel: int,
    now: datetime,
    lock_path: Path,
    approvers: list[str],
    repo: str,
    base: str = "main",
    interval_s: float | None = None,
    expected_interval_s: float | None = None,
    holder: str | None = None,
    takeover: bool = False,
    tracker: LinearTracker | None = None,
    ci_classifier: ClassifyFn | None = None,
    ci_fix_dispatcher: DispatchFixFn | None = None,
    reviewer_launcher: ReviewerLauncher | None = None,
    remediator: Remediator | None = None,
    base_integrator: BaseIntegrator | None = None,
    review_detacher: ReviewDetacher | None = None,
    run_liveness: RunLivenessProbe | None = None,
    operator_stop: bool = False,
) -> TickResult:
    """Run one idempotent tick of the epic driver for epic `name`.

    ``operator_stop`` says a ``max_parallel`` of 0 is the operator's own
    ``--max-parallel 0``, so the held frontier names that and not
    ``kew drive`` (#3192).

    See the module docstring / spec for the full sequence. Returns a
    `TickResult` with a tri-state `signal`; never raises for anything the
    tick itself can classify as stuck (invalid graph, non-int ids, a dead
    merge) — those are reported, not thrown. The ONE exception is an EMPTY
    graph: zero nodes means the tracker query matched no work at all (a typo'd
    epic name or a wrong-tracker cwd), so `advance` raises `EmptyEpicGraphError`
    instead of falsely reporting `COMPLETE done=0` — the CLI turns it into an
    exit-2 diagnostic (#819). `advance` itself is a thin orchestrator: lock ->
    preflight -> build -> (empty guard) -> heartbeat(1) -> validate ->
    `_run_tick` -> heartbeat(2). Journaling/heartbeat are only engaged AFTER
    the lock is acquired, preflight succeeds, and the graph is non-empty
    (control-flow table, spec Decision 4); a journal-append failure fails the
    tick loudly.

    `_TickLog` is the one writer that deliberately sits OUTSIDE that table
    (#2304): it opens before the lock and closes on every path out, including
    the lock-out, the empty graph and anything that raises, because those are
    exactly the attempts the journal has never been able to record. Its writes
    are best-effort for the same reason the journal's are not — it is a
    diagnostic, and one that could fail a tick would be worse than a gap. Once
    the lock IS held it also sweeps the rows earlier drivers of this epic died
    holding open, which is the only point at which that is provable.

    Journal and heartbeat timestamps use the real wall clock, not
    `now` (which only freezes staleness math for this tick's overlays).

    The lock is per-epic (keyed by `lock_path`), so two epics tick concurrently;
    a `locked_out` result names a *different live* driver of THIS epic, and is
    STALLED when that driver stopped beating its heartbeat (#3331). A lock
    left behind by a *stale* holder (dead PID or heartbeat past the staleness
    threshold) is reclaimed automatically; `takeover=True` also reclaims an
    indeterminate legacy lock (#861/F2, #970).
    """
    reap_dispatch_children(dispatch_sink)
    with (
        _TickLog(store, name) as tick_log,
        single_driver_lock(lock_path, holder=holder, takeover=takeover) as got,
        ExitStack() as driver_scope,
    ):
        if not got:
            locked = _empty_tick_result(
                signal=_locked_out_signal(lock_path), locked_out=True
            )
            tick_log.record(locked, journal_tick_id=None, journaled=False)
            return locked
        # We hold the lock, so no other driver is mid-tick on this epic: any
        # row of it still open was left by one that died mid-tick (#2304).
        tick_log.sweep_abandoned()
        # Credential reconciliation (issuance recovery, revocation retries, the
        # issuer orphan sweep) belongs to the driver that HOLDS this epic's
        # lock; entering it beside the lock ran it in the locked-out process
        # too (governed review round 3, 2026-09-21).
        revoked_hashes = driver_scope.enter_context(driver_key_cleanup(store))

        preflight_tools()

        graph = build_graph(deps, name)
        # The GitHub-native adapter read every node's issue payload to build the
        # graph; hand it to the facts reader so the overlay pass does not read
        # each issue a second time.
        prime_issue_snapshots_from(deps, facts)
        if not graph.nodes:
            # Zero nodes = no work exists. Raise BEFORE `begin_epic_tick` so no
            # tick is journaled: a `COMPLETE done=0 remaining=0` row here would
            # falsely record success for an epic that never had any work (#819).
            raise EmptyEpicGraphError(name)

        started = datetime.now(UTC)
        store.begin_epic_tick(name, started.isoformat())
        tick_log.reached()
        # Durably record the tracker this epic is driven under (#927/N3). The
        # active tracker is ground truth — we only reach here having built a
        # graph from it — so `epic run --all` classification reads this instead
        # of guessing from item-id shape (a numeric Linear twin reads GitHub).
        store.record_epic_tracker(name, config.tracker.type)

        report = validate(graph)
        if not report.ok:
            result = _invalid_graph_result(graph, report)
            tick_id, inserted = _journal_invalid(name, graph, result, store=store)
            _finish_heartbeat(
                store,
                name,
                started,
                interval_s=interval_s,
                expected_interval_s=expected_interval_s,
                signal=result.signal,
                tick_id=tick_id,
            )
            tick_log.record(result, journal_tick_id=tick_id, journaled=inserted)
            return result

        result, (tick_id, inserted) = _run_tick(
            name,
            graph,
            config=config,
            store=store,
            facts=facts,
            dispatch_sink=dispatch_sink,
            max_parallel=max_parallel,
            now=now,
            approvers=approvers,
            repo=repo,
            base=base,
            tracker=tracker,
            ci_classifier=ci_classifier,
            ci_fix_dispatcher=ci_fix_dispatcher,
            reviewer_launcher=reviewer_launcher,
            remediator=remediator,
            base_integrator=base_integrator,
            review_detacher=review_detacher,
            run_liveness=run_liveness,
            revoked_hashes=revoked_hashes,
            lock_keepalive=_lock_keepalive(lock_path, holder),
            operator_stop=operator_stop,
        )
        result = _with_graphql_points(name, result, deps, facts)
        if not refresh_lock_heartbeat(lock_path, holder):
            # We no longer own the lock — a stealer reclaimed it mid-tick. The
            # refresh already logged loudly; the next tick's acquire will see the
            # thief's fresh lock and correctly fall to observer mode (#970).
            _logger.warning(
                "epic %s: lost driver lock during tick — another driver holds it",
                name,
            )
        _finish_heartbeat(
            store,
            name,
            started,
            interval_s=interval_s,
            expected_interval_s=expected_interval_s,
            signal=result.signal,
            tick_id=tick_id,
        )
        tick_log.record(result, journal_tick_id=tick_id, journaled=inserted)
        return result


def _invalid_summary(graph: Graph, report: ValidationReport) -> TickSummary:
    """`summarize`'s counterpart to `_invalid_graph_result`: a malformed
    graph leaves every node unactionable, so it all reads as stuck."""
    reason = _invalid_graph_reason(graph, report)
    stuck = tuple(
        StuckNode(node_id=nid, reason=reason, blast_radius=())
        for nid in sorted(graph.nodes)
    )
    return TickSummary(
        counts={},
        frontier=(),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=stuck,
        done=(),
        remaining=len(graph.nodes),
        signal=TerminalSignal.STALLED,
    )


def _aged_hold_reason(reason: str) -> str:
    """What a hold reads as once its stall clock expires.

    The CI gate is the only hold that ages into a diagnosis of its own —
    `ci-stalled`. Every other honored hold keeps its own reason: an aged
    `sandbox_containment` hold relabelled `ci-stalled` sent the operator to a
    PR that was never the problem, and cost the attention stream the hold's own
    kind and action — so the node stopped naming anybody at all (#2150).

    Shared with `_classify_held_verdict`, which ages the same rows on the
    `advance` path (#2183): one vocabulary, so `status` and `advance` cannot
    name one expired hold two ways.
    """
    return "ci-stalled" if is_ci_pending_hold(reason) else reason


def _held_signal(
    graph: Graph,
    statuses: dict[str, DriverStatus],
    *,
    store: RunStore,
    name: str,
    now: datetime,
    stall_days: int,
) -> tuple[bool, list[StuckNode]]:
    """Read-only stand-in for `_classify_held_verdict`'s staleness check.

    `summarize` (unlike `_run_tick`) never runs `merge_pass` — a status
    snapshot must never trigger a `gh` write — so HELD nodes are re-checked
    directly against the persisted hold row instead.
    """
    holds = _holds_by_item(store, name)
    held_live = False
    stuck: list[StuckNode] = []
    for nid, ds in statuses.items():
        if ds is not DriverStatus.HELD:
            continue
        hold = holds.get(nid)
        if hold is not None and _is_stale(
            hold.held_at.isoformat(), now=now, stall_days=stall_days
        ):
            blast = tuple(sorted(graph.transitive_dependents(nid)))
            stuck.append(StuckNode(nid, _aged_hold_reason(hold.reason), blast))
        elif _hold_is_a_live_wait(hold):
            held_live = True
    return held_live, stuck


def _hold_is_a_live_wait(hold: EpicHold | None) -> bool:
    """Whether a HELD node inside its stall clock is somebody's legitimate wait.

    Normally yes, and `held_live` is what says so: CI is running, an operator
    is reading a budget refusal, a self-healing merge hold re-polls next tick.

    A dispatch-refusal row is the exception (#2217). It is the driver's OWN
    superseded bookkeeping — written by a frontier refusal, cleared by the
    dispatch that disproves it (`_clear_stale_dispatch_refusal_hold`) — so a
    node still HELD on one is held by a row no further pass will clear: the
    overlay routes it there (precedence 8a, `overlay._remediation_wedged`)
    exactly because nothing can advance it. Nobody is waiting on anything, so
    it must not buy the epic a PROGRESSING signal, which `TerminalSignal`
    reserves for something that moved or a `ci_pending` gate inside its clock.
    That reading is the second #2217 symptom: #2201/#2202 read
    `signal=progressing` for two hours, and would have gone on reading it for
    `epic.stall_days` before the clock above re-reported them. The node keeps
    its place in `held`/`waiting` (so `kew epic status` and the attention
    stream still name the reason, unchanged); only the signal stops calling it
    progress.
    """
    return hold is None or not is_dispatch_refusal_hold(hold.reason)


def _status_counts(statuses: dict[str, DriverStatus]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ds in statuses.values():
        counts[ds.value] = counts.get(ds.value, 0) + 1
    return counts


def _status_values(statuses: dict[str, DriverStatus]) -> dict[str, str]:
    return {nid: ds.value for nid, ds in statuses.items()}


def _merge_pending_rows(
    waiting: list[StuckNode],
    statuses: dict[str, DriverStatus],
    self_healing_holds: AbstractSet[str],
) -> set[str]:
    """The waiting rows that ARE an open PR awaiting a human's merge.

    Normally that is exactly the IN_REVIEW rows. ``self_healing_holds`` carries
    the nodes THIS tick's merge pass rewrote IN_REVIEW -> HELD while GitHub
    finishes computing mergeability, or while the head's own check runs finish
    (#2183): the PR under them is still open and still awaiting the same merge,
    so they belong here too. `summarize` never runs the merge pass and so sees
    those nodes as plain IN_REVIEW — passing the rewrite back in is what keeps
    one epic state from getting two readings.
    """
    return {
        w.node_id
        for w in waiting
        if statuses.get(w.node_id) is DriverStatus.IN_REVIEW
        or w.node_id in self_healing_holds
    }


def _merge_freed(
    waiting: list[StuckNode],
    statuses: dict[str, DriverStatus],
    graph: Graph,
    frontier_status: dict[str, NodeStatus],
    dependency_effects: dict[str, frozenset[str]],
    self_healing_holds: AbstractSet[str] = frozenset(),
) -> set[str]:
    """The waiting nodes a human MERGE of the open PRs would by itself free.

    Seeded with the merge-pending rows (the PRs awaiting that merge, see
    `_merge_pending_rows`), then closed over the graph: a blocked TODO row joins
    only when every dependency still owing it is already in the set, so merging
    really does clear it. A TODO blocked on an out-of-epic issue nobody is
    reviewing never joins, and neither does a TODO with no unmet dependency at
    all — a ready node waiting on a parallelism cap is waiting on kew, not on a
    human's merge.
    """
    blocked = {
        w.node_id: _unmet_dependencies(
            w.node_id, graph, frontier_status, dependency_effects
        )
        for w in waiting
        if statuses.get(w.node_id) is DriverStatus.TODO
    }
    return _freed_closure(
        _merge_pending_rows(waiting, statuses, self_healing_holds), blocked
    )


def _freed_closure(seed: set[str], blocked: dict[str, set[str]]) -> set[str]:
    """Grow `seed` by every blocked node whose unmet dependencies it already
    covers, until a pass adds nothing — a node behind a node behind the PR is
    behind the PR too."""
    freed = set(seed)
    while True:
        newly = {
            nid for nid, unmet in blocked.items() if unmet and unmet <= freed
        } - freed
        if not newly:
            return freed
        freed |= newly


def _awaiting_merge_only(
    waiting: list[StuckNode],
    stuck_by_id: dict[str, StuckNode],
    statuses: dict[str, DriverStatus],
    *,
    graph: Graph,
    frontier_status: dict[str, NodeStatus],
    dependency_effects: dict[str, frozenset[str]],
    self_healing_holds: AbstractSet[str] = frozenset(),
) -> bool:
    """Whether open PRs awaiting a human merge are the ONLY thing left (#2183).

    One derivation for both callers, so `kew epic status` and `kew epic advance`
    can never read one epic state two ways: `_run_tick` gates it on the merge
    pass's own `_is_awaiting_merge` verdicts as well, while `summarize` — which
    never runs the merge pass — reads the same fact off the in-review waiting
    rows (`_in_review_reason`, and the head-check gate line `_with_ci_gate_reasons`
    rewrites onto it — a human gate is a human too).

    The one thing the two callers cannot see alike is a status the tick's own
    merge pass rewrote under them, so `_run_tick` passes those back in through
    ``self_healing_holds``: a self-healing HOLD (`is_self_healing_hold`) reads
    IN_REVIEW to `summarize` and HELD here, and reading it off the rewrite alone
    flipped an epic whose only other work was a PR awaiting a human merge to
    STALLED — `kew epic run` then exited with the repair code instead of polling
    (#2183).

    A stuck node still needs an operator, so it keeps the reading STALLED (Node
    I7), and so does any waiting row a merge would not free, because ONLY is the
    whole claim: an epic one `kew epic publish` away from moving, or one whose
    last node waits on an issue outside the epic, is not waiting on anyone's
    merge — and `kew epic run` sleeps and polls on AWAITING_MERGES forever
    rather than exiting for the repair such an epic actually owes. Without this,
    the documented AWAITING_MERGES state was unreachable from `kew epic status`,
    which called the normal human-merge gate `stalled` and sent operators
    looking for a break.
    """
    if stuck_by_id:
        return False
    freed = _merge_freed(
        waiting,
        statuses,
        graph,
        frontier_status,
        dependency_effects,
        self_healing_holds,
    )
    pending = _merge_pending_rows(waiting, statuses, self_healing_holds)
    return bool(pending) and all(w.node_id in freed for w in waiting)


def summarize(
    name: str,
    graph: Graph,
    *,
    config: DispatchConfig,
    store: RunStore,
    facts: GitHubFactsReader,
    now: datetime | None = None,
) -> TickSummary:
    """A read-only per-node status snapshot for `kew epic status`.

    Shares the overlay/frontier/stuck-detection helpers with `_run_tick` so
    `status` and `advance` never disagree about what a node's `DriverStatus`
    means — but never mutates the run-store (reservations are read exactly
    as persisted; no stale-reservation reconciliation) and never runs
    `merge_pass` (no `gh` writes from a status call). Callers are
    responsible for building `graph` themselves (see `_load_epic_graph`);
    an invalid graph is reported the same way `advance` reports one — as
    all-nodes-stuck.
    """
    when = now if now is not None else datetime.now(UTC)
    report = validate(graph)
    if not report.ok:
        return _invalid_summary(graph, report)

    provider = provider_from_config(config)
    reserved_item_ids = {r.item_id for r in store.list_epic_reservations(name)}
    overlays = _build_overlays(
        graph,
        name=name,
        provider=provider,
        store=store,
        facts=facts,
        reserved_item_ids=reserved_item_ids,
        held_reason_by_item=_holds_reason_map(store, name),
        retry_by_item=_retry_map(store, name),
        config=config,
    )
    frontier_status = _frontier_status(graph, overlays.statuses)
    overlay_graph = _overlay_graph(graph, frontier_status)

    in_flight = sorted(
        (
            nid
            for nid, ds in overlays.statuses.items()
            if ds is DriverStatus.IN_PROGRESS
        ),
        key=item_id_sort_key,
    )
    excluded = _dispatch_exclusions(
        graph,
        overlays.statuses,
        in_flight=in_flight,
        reserved_item_ids=reserved_item_ids,
        already_stuck_ids={s.node_id for s in overlays.stuck},
    )
    frontier = compute_frontier(
        overlay_graph,
        status=frontier_status,
        excluded=excluded,
        dependency_effects=overlays.dependency_effects,
        priorities=_frontier_priorities(overlays.node_facts),
    )

    held_live, held_stuck = _held_signal(
        graph,
        overlays.statuses,
        store=store,
        name=name,
        now=when,
        stall_days=config.epic.stall_days,
    )
    review_stuck = _stale_review_stuck(
        graph,
        overlays.statuses,
        overlays.node_facts,
        now=when,
        stall_days=config.epic.stall_days,
    )
    stuck_by_id = _merge_stuck(
        overlays.stuck,
        held_stuck,
        review_stuck,
        _terminal_ds_stuck(
            graph,
            overlays.statuses,
            store,
            overlays.superseded_runs_by_item,
            overlays.continuation_reason_by_item,
            overlays.continuation_histories,
        ),
    )
    waiting = _waiting_nodes(
        graph,
        overlays.statuses,
        overlays.node_facts,
        store=store,
        holds_by_item=_holds_by_item(store, name),
        continuation_reason_by_item=overlays.continuation_reason_by_item,
        frontier_status=frontier_status,
        stuck_ids=set(stuck_by_id),
        dependency_effects=overlays.dependency_effects,
    )

    waiting = _with_ci_gate_reasons(
        waiting,
        _status_ci_gate_reasons(
            overlays.statuses,
            overlays.node_facts,
            facts,
            config.ci.approval_gated_checks,
        ),
    )
    waiting = _with_review_stage_reasons(
        waiting,
        _verify_deferred_reasons(
            graph, frontier, overlays.statuses, waiting, store, provider
        ),
        graph,
        stuck_ids=set(stuck_by_id),
    )
    waiting = [*waiting, *_drive_held_waiting(graph, frontier, waiting, store)]
    waiting = _with_review_stage_reasons(
        waiting,
        _merge_claim_reasons(
            overlays.statuses,
            overlays.node_facts,
            store=store,
            holds_by_item=_holds_by_item(store, name),
            now=when,
            ci_fix=config.epic.auto_ci_fix,
        ),
        graph,
        stuck_ids=set(stuck_by_id),
    )

    done, remaining = _done_and_remaining(graph, frontier_status)
    identity = _node_identities(
        [*waiting, *stuck_by_id.values()],
        overlays.statuses,
        overlays.node_facts,
        store,
    )
    # No dispatch happens in a read-only snapshot, so stand `frontier` in for
    # `_terminal_signal`'s `dispatched` argument: a non-empty frontier means
    # there IS a path to progress (the next real `advance()` tick would
    # dispatch it) — the same thing `_run_tick` means by "something moved".
    # An armed host-restart continuation is that too: the next tick claims it
    # once admission lets it (#2964).
    signal = _terminal_signal(
        remaining=remaining,
        dispatched=[*frontier, *_armed_continuations(store, overlays.statuses, name)],
        in_flight=in_flight,
        held_live=held_live,
        review_live=_review_live(overlays.statuses),
        awaiting_merges=_awaiting_merge_only(
            waiting,
            stuck_by_id,
            overlays.statuses,
            graph=graph,
            frontier_status=frontier_status,
            dependency_effects=overlays.dependency_effects,
        ),
    )
    held_ids = sorted(
        (
            nid
            for nid, ds in overlays.statuses.items()
            if ds is DriverStatus.HELD and nid not in stuck_by_id
        ),
        key=item_id_sort_key,
    )

    return TickSummary(
        counts=_status_counts(overlays.statuses),
        frontier=tuple(frontier),
        in_flight=tuple(in_flight),
        held=tuple(held_ids),
        failed=tuple(_ids_with_status(overlays.statuses, DriverStatus.FAILED)),
        cancelled=tuple(_ids_with_status(overlays.statuses, DriverStatus.CANCELLED)),
        stuck=tuple(sorted(stuck_by_id.values(), key=lambda s: s.node_id)),
        done=tuple(done),
        remaining=remaining,
        signal=signal,
        waiting=tuple(waiting),
        pr_links=_pr_links(overlays.node_facts),
        identity=identity,
        statuses=_status_values(overlays.statuses),
        token_warnings=active_runner_token_warnings(store, now=when),
    )
