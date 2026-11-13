"""DelegateDriver: kew supervises ONE Claude Code session pursuing a loop.

Not a polymorphic driver (spec R-arch M3) — a plain procedure that shares the
LoopStore and GateEvaluator with the (deferred) provide path. It launches one
``run_tracked_agent`` against the issue, maps the agent's step events to
iteration rows, then runs the verification gate before stamping a terminal
status.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import audit, dispatch_core
from kew.audit import Actor, resolve_actor
from kew.budget import BudgetExceeded
from kew.config import DispatchConfig
from kew.continuation_recovery import (
    CheckoutAbandonment,
    CheckoutGenerationRefusal,
    checkout_abandonment,
    record_checkout_replacement,
    record_checkout_withdrawal,
    require_unchanged_checkout_binding,
)
from kew.dispatch_core import build_repo_map, run_tracked_agent
from kew.inference_transport import (
    TransportAdmissionError,
    require_adaptive_route_admission,
    resolve_inference_transport,
)
from kew.loops.verification import (
    DEFAULT_GUARDED_PATHS,
    HOST_VERIFICATION_POSTURE,
    UNCONTAINED_VERIFICATION_POSTURE,
    CiGreenCondition,
    CommandCondition,
    Gate,
    GateEvaluator,
    GateVerdict,
    HostVerificationAdmission,
    published_changed_paths,
    resolve_host_verification_admission,
)
from kew.models import GhIssue
from kew.pricing import build_pricing_overrides, is_priced_model
from kew.progress import _warn
from kew.prompt import build_prompt
from kew.prompt_addendum import PromptConfigurationError, profile_version_id
from kew.publication import PublicationError
from kew.publication_canonical import (
    CanonicalNoChange,
    CanonicalPublication,
    PublicationHeld,
    acceptance_contracts,
    publication_remote,
)
from kew.remediation_containment import record_remediation_refusal
from kew.run_store import IterationRecord, LoopRecord, LoopStatus
from kew.runner_fields import runner_text
from kew.runners import resolve_runner
from kew.runners.registry import (
    ResolvedRoute,
    _get_runner_factories,
    runner_accepts_model,
)
from kew.sandbox import (
    POSTURE_UNCONTAINED,
    UNCONTAINED_BANNER,
    SandboxRuntimeUnavailable,
    runner_native_containment,
)
from kew.self_check import rendered_self_check
from kew.service.prompt_profiles import launch_addendum
from kew.subprocess_utils import run
from kew.tracking import LaunchState
from kew.worktree import (
    CheckoutAdmissionRefusal,
    CheckoutDirectoryBinding,
    adopt_legacy_checkout_generation,
    capture_checkout_generation,
    claim_checkout,
    cleanup_worktree,
    dispatch_checkout_path,
    operator_repo_root,
    read_checkout_generation,
    release_checkout_claim,
    require_checkout_claim,
    retire_legacy_checkout_generation,
    reuse_or_restore_worktree,
    setup_worktree,
)

if TYPE_CHECKING:
    from kew.run_store import RunStore


@dataclass(frozen=True)
class _Step:
    """One parsed step line from the run's event file."""

    input_tokens: int
    output_tokens: int
    cost_usd: float
    ts: float  # the step event's own timestamp (orchestrator prepends it)


@dataclass
class _PublicationBinding:
    """Checkout generation and publication identity prepared before a loop launch."""

    run_id: str
    base_sha: str
    lineage_run_id: str
    abandonment: CheckoutAbandonment | None = None
    worker_launched: bool = False
    checkout_generation: str = ""
    checkout_binding: CheckoutDirectoryBinding | None = None

    def require_generation(self, checkout: Path, *, boundary: str) -> None:
        """Use the regular dispatch reader at every loop identity boundary."""
        dispatch_core._require_checkout_generation(
            checkout, self.checkout_generation, boundary=boundary
        )

    def mark_worker_launch(self, checkout: Path) -> None:
        """Validate each paid attempt before disabling unused checkout cleanup."""
        self.require_generation(checkout, boundary="loop paid launch")
        self.worker_launched = True


@dataclass(frozen=True)
class _VerificationTarget:
    """Exact tree and publication facts passed to the verification gate."""

    base_ref: str
    changed_paths: list[str]
    agent_written_checkout: bool
    published_sha: str | None = None
    publication_base_sha: str | None = None
    publication_remote: str | None = None


@dataclass(frozen=True)
class _PreviousPublicationBinding:
    """Recorded owner and checkout generation for a loop continuation."""

    lineage_run_id: str
    base_sha: str
    checkout_generation: str
    event_id: int


_COMPLEXITY_MAX_STEPS: dict[str, int] = {
    "simple": 80,
    "medium": 200,
    "complex": 400,
}
_DEFAULT_MAX_STEPS = 200


def max_steps_for_complexity(complexity: str) -> int:
    """Return the default step cap for a given complexity label."""
    return _COMPLEXITY_MAX_STEPS.get(complexity, _DEFAULT_MAX_STEPS)


def build_loop_from_issue(
    issue: GhIssue,
    config: DispatchConfig,
    *,
    runner_name: str,
    model: str = "",
    complexity: str = "medium",
    max_steps_override: int | None = None,
) -> LoopRecord:
    """Build a LoopRecord from an issue dict + config defaults.

    Guardrail caps come from config: ``wall_clock_secs`` from
    ``agent.timeout_minutes`` (mandatory non-null, spec R2 — the load-bearing
    bound), ``budget_usd`` from ``agent.max_budget_usd`` (a display target +
    best-effort cap, spec R10/R11). ``max_budget_usd`` defaults to 10.0 and the
    config validator rejects values <= 0, so it is never None — no ``or None``.

    ``max_steps`` is scaled by ``complexity`` (simple/medium/complex) so the
    default cap matches realistic runner behaviour for each tier.  An explicit
    ``max_steps_override`` from dispatch-time ``--max-steps`` wins over the
    complexity default.

    ``runner_name``/``model`` are the loop's DURABLE route (#1111): both halves
    of the resolved ``[routing]`` decision are recorded on the record so resume
    continues on the same route instead of re-resolving the config default.
    ``complexity`` is recorded with them — it is the tier that decision was made
    at, so a later re-resolve (a mid-flight route override) uses the same tier.
    """
    num = issue.get("number")
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    goal = f"#{num}: {title}".strip()
    if body:
        goal = f"{goal}\n\n{body}"
    steps = (
        max_steps_override
        if max_steps_override is not None
        else max_steps_for_complexity(complexity)
    )
    return LoopRecord(
        loop_id=str(uuid.uuid4()),
        source="issue",
        goal_text=goal,
        runner_name=runner_name,
        model=model,
        complexity=complexity,
        created_at=datetime.now(UTC),
        issue_num=num if isinstance(num, int) else None,
        item_id=str(num) if num is not None else None,
        repo=config.project.repo or None,
        budget_usd=config.agent.max_budget_usd,
        wall_clock_secs=config.agent.timeout_minutes * 60,
        max_steps=steps,
    )


def _prepare_worktree(
    loop: LoopRecord,
    config: DispatchConfig,
    *,
    base_sha: str = "",
    store: RunStore | None = None,
    binding: _PublicationBinding | None = None,
) -> Path:
    """Set up the worktree for the loop's issue (seam for tests)."""
    assert loop.issue_num is not None, "delegate loops are issue-sourced (v1)"
    if store is None or binding is None:
        raise CheckoutAdmissionRefusal(
            "loop checkout setup requires a run store and run identity to consume "
            "abandonment; use `kew epic continue` or `kew epic abandon` for recovery, "
            "then dispatch the loop with durable tracking"
        )
    checkout = dispatch_checkout_path(config, issue_num=loop.issue_num)
    claim_checkout(checkout)
    try:
        binding.abandonment = checkout_abandonment(
            store,
            epic_ref=None,
            item_id=str(loop.issue_num),
            branch=_loop_branch(loop, config),
            checkout=checkout,
            capture_generation=partial(capture_checkout_generation, checkout),
            retire_legacy_generation=partial(
                retire_legacy_checkout_generation, checkout
            ),
        )
        created = setup_worktree(
            issue_num=loop.issue_num,
            config=config,
            base_sha=base_sha,
            abandonment=binding.abandonment,
        )
        binding.checkout_generation = read_checkout_generation(created)
        if binding.abandonment is not None:
            record_checkout_replacement(
                store,
                created,
                abandonment=binding.abandonment,
                run_id=binding.run_id,
                issue_num=loop.issue_num,
                item_id=str(loop.issue_num),
                repo=config.project.repo or None,
                epic_ref=None,
                branch=_loop_branch(loop, config),
                generation=binding.checkout_generation,
            )
        return created
    except BaseException:
        release_checkout_claim(checkout)
        raise


def _loop_branch(loop: LoopRecord, config: DispatchConfig) -> str:
    """The branch name of this loop's worktree (Node I2, #507).

    Kept in lockstep with the worktree machinery: both ``setup_worktree`` (run)
    and ``reuse_or_restore_worktree`` (resume) derive the branch from
    ``f"{config.branches.prefix}{issue_num}"`` for an issue-sourced loop (no
    ``item_id`` is threaded, so the slug is the numeric issue). Threading this
    into ``run_tracked_agent`` populates ``RunRecord.branch`` so ``sync_outcomes``
    can link the loop's spend to its PR — otherwise the branch is stored empty.
    """
    assert loop.issue_num is not None, "delegate loops are issue-sourced (v1)"
    return f"{config.branches.prefix}{loop.issue_num}"


def _withdraw_loop_replacement(
    loop: LoopRecord,
    config: DispatchConfig,
    store: RunStore,
    checkout: Path,
    binding: _PublicationBinding | None,
    *,
    reason: str,
) -> None:
    """Remove an unused replacement before recording its withdrawal."""
    if binding is None or binding.abandonment is None or binding.worker_launched:
        return
    try:
        cleanup_worktree(checkout, _loop_branch(loop, config))
        if checkout.exists() or checkout.is_symlink():
            _warn(f"Loop checkout cleanup preserved {checkout}")
            return
        record_checkout_withdrawal(
            store,
            checkout,
            abandonment=binding.abandonment,
            run_id=binding.run_id,
            issue_num=loop.issue_num or 0,
            item_id=str(loop.issue_num),
            repo=config.project.repo or None,
            reason=reason,
        )
    except Exception as exc:
        _warn(f"Loop checkout cleanup could not attest withdrawal: {exc}")
    finally:
        release_checkout_claim(checkout)


class _VerificationTargetError(RuntimeError):
    """The loop could not identify an exact tree for verification."""


class _CheckoutChangedPathsError(_VerificationTargetError):
    """The admitted checkout compatibility path could not read its diff."""


def _checkout_changed_paths(worktree: Path, base_ref: str) -> list[str]:
    """Read the diff only for the explicitly admitted checkout path."""
    try:
        result = run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            check=False,
            cwd=worktree,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise _CheckoutChangedPathsError(
            f"git diff failed computing changed paths: {exc}"
        ) from exc
    if result.returncode != 0:
        raise _CheckoutChangedPathsError(
            f"git diff failed (rc={result.returncode}) computing changed paths"
        )
    return [path for path in result.stdout.strip().splitlines() if path]


def _canonical_verification_target(
    binding: _PublicationBinding,
    publication: CanonicalPublication | CanonicalNoChange | None,
    remote: str,
) -> _VerificationTarget:
    """Build gate inputs from one exact canonical publication result."""
    if isinstance(publication, CanonicalPublication):
        published_sha = publication.commit_sha
        # Publication paths compare with the commit's parent.
        # They are cumulative only when the original base is that parent.
        changed_paths = (
            list(publication.changed_paths)
            if publication.parent_shas == (binding.base_sha,)
            else None
        )
    elif isinstance(publication, CanonicalNoChange):
        published_sha = publication.base_sha
        changed_paths = [] if published_sha == binding.base_sha else None
    else:
        raise _VerificationTargetError("canonical publication result is unavailable")
    if changed_paths is None:
        try:
            changed_paths = published_changed_paths(
                remote=remote,
                base_sha=binding.base_sha,
                published_sha=published_sha,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _VerificationTargetError(
                f"canonical verification diff failed: {exc}"
            ) from exc
    if not changed_paths:
        raise _VerificationTargetError("canonical loop produced no changes")
    return _VerificationTarget(
        base_ref=binding.base_sha,
        changed_paths=changed_paths,
        agent_written_checkout=True,
        published_sha=published_sha,
        publication_base_sha=binding.base_sha,
        publication_remote=remote,
    )


def _checkout_verification_target(
    config: DispatchConfig, worktree: Path
) -> _VerificationTarget:
    """Build gate inputs from the admitted checkout compatibility path."""
    base_ref = f"origin/{config.branches.base or 'main'}"
    return _VerificationTarget(
        base_ref=base_ref,
        changed_paths=_checkout_changed_paths(worktree, base_ref),
        agent_written_checkout=True,
    )


def _record_loop_checkout_binding(
    store: RunStore,
    loop: LoopRecord,
    worktree: Path,
    binding: _PublicationBinding,
    config: DispatchConfig,
) -> None:
    """Persist a mode-independent loop generation before any paid launch."""
    require_unchanged_checkout_binding(
        store,
        event="loop_checkout_bound",
        run_id=binding.run_id,
        branch=_loop_branch(loop, config),
        checkout=worktree,
        generation=binding.checkout_generation,
    )
    inserted = store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="loop_checkout_bound",
        run_id=binding.run_id,
        issue_num=loop.issue_num,
        item_id=str(loop.issue_num or 0),
        actor_type="policy",
        actor_id="loop-checkout",
        payload={
            "loop_id": loop.loop_id,
            "item_id": str(loop.issue_num or 0),
            "branch": _loop_branch(loop, config),
            "checkout": str(worktree.resolve()),
            "checkout_id": binding.checkout_generation,
            "lineage_run_id": binding.lineage_run_id or binding.run_id,
            "base_sha": binding.base_sha,
        },
    )
    if inserted is None:
        raise RuntimeError("loop checkout binding was not durably recorded")


def _previous_loop_checkout_binding(
    store: RunStore, loop_id: str, item_id: str, branch: str, worktree: Path
) -> _PreviousPublicationBinding | None:
    """Read the latest host binding without adopting the current checkout."""
    events = [
        event
        for event in store.list_audit_events(limit=None, event="loop_checkout_bound")
        if _publication_event_matches_loop(event, loop_id)
    ]
    if not events:
        return None
    event = max(events, key=lambda event: event.id)
    return _parse_loop_checkout_binding(event, item_id, branch, worktree)


def _parse_loop_checkout_binding(
    event: Any, item_id: str, branch: str, worktree: Path
) -> _PreviousPublicationBinding:
    """Validate one recorded loop binding against its complete checkout identity."""
    payload = event.payload or {}
    expected = {
        "item_id": item_id,
        "branch": branch,
        "checkout": str(worktree.resolve()),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            "loop checkout binding identity mismatch; start a fresh loop with "
            "kew dispatch <issue> --loop"
        )
    if any(
        not isinstance(payload.get(key), str) or not payload[key]
        for key in ("checkout_id", "lineage_run_id")
    ):
        raise RuntimeError(
            "loop checkout binding is unreadable; start a fresh loop with "
            "kew dispatch <issue> --loop"
        )
    base = payload.get("base_sha")
    if not isinstance(base, str):
        raise RuntimeError(
            "loop checkout base is unreadable; start a fresh loop with "
            "kew dispatch <issue> --loop"
        )
    return _PreviousPublicationBinding(
        lineage_run_id=payload["lineage_run_id"],
        base_sha=base,
        checkout_generation=payload["checkout_id"],
        event_id=int(event.id),
    )


def _publication_event_matches_loop(event: Any, loop_id: str) -> bool:
    """Return whether one base event belongs to this loop."""
    return isinstance(event.payload, dict) and event.payload.get("loop_id") == loop_id


def _recorded_binding_for_event(
    store: RunStore,
    event: Any,
    *,
    item_id: str,
    branch: str,
    worktree: Path,
) -> _PreviousPublicationBinding | None:
    """Read one prior binding only when its full identity still verifies."""
    prior_run_id = event.run_id
    if not prior_run_id:
        return None
    payload = event.payload
    if not isinstance(payload, dict):
        return None
    checkout_generation = payload.get("checkout_id")
    if not isinstance(checkout_generation, str) or not checkout_generation:
        return None
    base_sha = dispatch_core._recorded_publication_base(
        store,
        run_id=prior_run_id,
        item_id=item_id,
        branch=branch,
        checkout=worktree,
        generation=checkout_generation,
    )
    if not base_sha:
        return None
    return _PreviousPublicationBinding(
        lineage_run_id=prior_run_id,
        base_sha=base_sha,
        checkout_generation=checkout_generation,
        event_id=int(event.id),
    )


def _select_previous_binding(
    loop_id: str, found: list[_PreviousPublicationBinding]
) -> _PreviousPublicationBinding:
    """Require one readable publication owner for an evidenced loop."""
    if not found:
        raise PublicationError(
            "push", f"loop {loop_id} has no readable publication base"
        )
    owners = {(binding.lineage_run_id, binding.base_sha) for binding in found}
    if len(owners) > 1:
        raise PublicationError(
            "push", f"loop {loop_id} has conflicting publication owners"
        )
    return max(found, key=lambda binding: binding.event_id)


def _parse_step_line(line: str) -> _Step | None:
    """Parse one .events.jsonl line; return a _Step or None if not a step event."""
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict) or ev.get("type") != "step":
        return None
    return _Step(
        input_tokens=int(ev.get("input") or 0),
        output_tokens=int(ev.get("output") or 0),
        cost_usd=float(ev.get("cost_usd") or 0.0),
        ts=float(ev.get("ts") or 0.0),
    )


def _read_steps(event_file: Path | None) -> list[_Step]:
    """Reconstruct steps from the run's .events.jsonl ('step' lines)."""
    if event_file is None or not event_file.exists():
        return []
    parsed = (_parse_step_line(ln) for ln in event_file.read_text().splitlines())
    return [s for s in parsed if s is not None]


def _is_priced(model: str, config: DispatchConfig) -> bool:
    """True if the model has a pricing entry (or is a known-free provider).

    Fail-closed signal for spec R2: an unpriced model silently estimates $0, so
    a loop must refuse to start against one rather than run with no cost bound.
    """
    return is_priced_model(model, build_pricing_overrides(config.pricing.overrides))


# Terminal-status mapping kept table-driven so DelegateDriver.run stays rank <= B.
def _verdict_to_status(verdict: GateVerdict) -> tuple[LoopStatus, str]:
    if verdict.sabotage:
        return "needs_human", verdict.evidence
    if verdict.passed:
        return "achieved", verdict.evidence
    return "needs_human", verdict.evidence


# Run error_category -> terminal status, checked BEFORE the gate.
_ERROR_STATUS: dict[str, LoopStatus] = {
    "budget_exceeded": "budget_limited",
    "budget_stopped": "budget_limited",
    "budget_stop": "budget_limited",
    "transient": "needs_human",  # 124 inner timeout => hung, not a retry (spec)
    "permanent": "needs_human",
    "killed": "needs_human",
}


def _pre_gate_status(
    loop: LoopRecord, result: Any, steps: list[_Step]
) -> tuple[LoopStatus, str] | None:
    """Fail-closed short-circuits checked BEFORE the verification gate.

    Returns a terminal ``(status, reason)`` if the run must NOT reach the gate,
    else ``None`` (the run is clean and real — proceed to the gate). Extracted
    from ``_decide`` to keep both at xenon rank <= B.

    Order is load-bearing: ``paused`` first; then the step cap (a runaway loop is
    a human-review event regardless of the gate); then run-level failures
    (mapped/unmapped ``error_category``, nonzero exit) — a clean worktree would
    otherwise pass the gate and stamp ``achieved`` for a run that never ran; then
    the zero-step guard, placed last so a 0-step run that ALSO errored reports
    the specific error rather than the generic "no steps".
    """
    if result.paused:
        return "paused", "paused by operator"
    if len(steps) > loop.max_steps:
        return "needs_human", f"exceeded max_steps ({len(steps)} > {loop.max_steps})"
    forced = _ERROR_STATUS.get(result.error_category or "")
    if forced is not None:
        return forced, runner_text(f"run ended: {result.error_category}")
    if result.error_category:
        return "needs_human", runner_text(f"run errored: {result.error_category}")
    if result.exit_code != 0:
        return "needs_human", f"run exited nonzero ({result.exit_code})"
    if not steps:
        return "needs_human", "no steps taken (agent produced no work)"
    return None


def _build_default_gate(config: DispatchConfig) -> Gate | None:
    """Production gate from config, or None if it cannot verify (fail closed).

    Mode ``ci_green`` (config.commands.verify_loop_mode) gates on a real GitHub
    CI pass for the pushed HEAD commit and REPLACES the command condition; it
    requires ``project.repo`` (None return -> needs_human). Otherwise the gate
    runs the operator command (``verify_loop`` then ``ci``) as ``sh -c`` (Plan 3
    security invariant) — NEVER ``commands.verify`` (prompt-only) nor anything
    issue/agent-sourced. The diff-guard always applies on top.
    """
    if config.commands.verify_loop_mode == "ci_green":
        repo = config.project.repo
        if not repo:
            return None
        return GateEvaluator(
            [
                CiGreenCondition(
                    repo=repo,
                    timeout_secs=config.commands.ci_poll_timeout_seconds,
                    gated_checks=config.ci.approval_gated_checks,
                )
            ],
            guarded_paths=DEFAULT_GUARDED_PATHS,
        )
    verify_cmd = config.commands.verify_loop or config.commands.ci
    if not verify_cmd:
        return None
    return GateEvaluator(
        [
            CommandCondition(
                verify_cmd, needs_writes=config.commands.verify_loop_needs_writes
            )
        ],
        guarded_paths=DEFAULT_GUARDED_PATHS,
    )


def _no_gate_reason(config: DispatchConfig) -> str:
    """Operator-facing reason a config produced no usable gate (fail closed).

    Distinguishes the ``ci_green``-without-``project.repo`` misconfiguration from
    a plain missing verify command, so the needs_human message points the
    operator at the right remediation instead of always claiming "no verify
    command configured".
    """
    if config.commands.verify_loop_mode == "ci_green" and not config.project.repo:
        return "ci_green verify mode requires project.repo to be configured"
    return "no verify command configured"


def _loop_verification_admission(
    config: DispatchConfig, gate: Gate | None = None
) -> HostVerificationAdmission:
    """Resolve local execution authority from the gate and publication mode."""
    if (
        config.publication.publish == "canonical"
        and isinstance(gate, GateEvaluator)
        and gate.verification_admission is not None
    ):
        return gate.verification_admission
    gate_executes_candidate = bool(
        getattr(gate, "requires_uncontained_host_execution", False)
    )
    return resolve_host_verification_admission(
        agent_written_checkout=(
            config.publication.publish == "checkout" or gate_executes_candidate
        ),
        dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
    )


def _announce_verification_posture(admission: HostVerificationAdmission) -> None:
    """Print the host-verification posture before checkout or agent work."""
    if admission.refusal is not None:
        print(f"Security posture: {admission.refusal}.")
        return
    assert admission.posture is not None
    if admission.posture not in {
        HOST_VERIFICATION_POSTURE,
        UNCONTAINED_VERIFICATION_POSTURE,
    }:
        print(f"Security posture: {admission.posture.replace('_', ' ')}.")
        return
    print(
        "Security posture: uncontained host verification admitted by "
        "[sandbox] dangerously_skip_containment = true (#1444)."
    )


def resolve_loop_agent_posture(config: DispatchConfig, *, runner: str) -> str:
    """Admit a loop's agent launch on the host, or refuse it (#1977).

    A loop launches its agent through ``run_tracked_agent`` with no contained
    image: kew has no contained loop launch. The agent is therefore on the
    host, and the host is admitted exactly as ordinary dispatch admits it: a
    runner that contains itself runs under its own boundary (``native``), and
    anything else needs ``[sandbox] dangerously_skip_containment = true``
    (``uncontained``). Without the flag this raises before any checkout, agent
    work or spend.
    """
    native = runner_native_containment(runner)
    if native is not None:
        return native
    if config.sandbox.dangerously_skip_containment:
        return POSTURE_UNCONTAINED
    raise SandboxRuntimeUnavailable(
        "first-class loops launch the agent on this machine (kew has no "
        f"contained loop launch), and runner {runner!r} does not contain "
        "itself. Set [sandbox] dangerously_skip_containment = true to run this "
        "loop with no container boundary, or route the loop to a runner that "
        "contains itself."
    )


def _announce_agent_posture(posture: str) -> None:
    """Print the loop agent's execution posture before checkout or agent work."""
    if posture == POSTURE_UNCONTAINED:
        _warn(
            f"{UNCONTAINED_BANNER}\nReason containment was unavailable: "
            "first-class loops have no contained launch (#1977)."
        )
        return
    print(f"Security posture: loop agent runs under {posture} containment.")


_RESUMABLE: frozenset[str] = frozenset({"needs_human", "budget_limited", "paused"})


def _validate_resume(
    loop: LoopRecord, resolved: Any, gate: Gate | None, config: DispatchConfig
) -> None:
    """Raise ValueError if the loop cannot be resumed (fail-closed pre-flight).

    Kept in a helper so resume() stays a mostly straight-line body (xenon <= B).
    """
    if loop.status not in _RESUMABLE:
        raise ValueError(
            f"loop {loop.loop_id} is {loop.status!r}; only "
            f"{sorted(_RESUMABLE)} are resumable"
        )
    rejection = loop_route_rejection(resolved, publish=config.publication.publish)
    if rejection is not None:
        raise ValueError(f"cannot resume {loop.loop_id}: {rejection}")
    _require_loop_transport_admission(loop, resolved, config)
    if gate is None:
        raise ValueError(f"cannot resume {loop.loop_id}: {_no_gate_reason(config)}")
    if not _is_priced(resolved.model, config):
        raise ValueError(
            f"cannot resume {loop.loop_id}: model {resolved.model!r} is not priced"
        )
    failure = _resolved_runner_preflight_failure(config, resolved)
    if failure is not None:
        raise ValueError(f"cannot resume {loop.loop_id}: {failure}")


def _resolved_runner_preflight_failure(
    config: DispatchConfig, resolved: ResolvedRoute
) -> str | None:
    """Check the runner carried by the route that will launch."""
    from kew.runner_preflight import runner_preflight_failure

    return runner_preflight_failure(config, runner_name=_route_runner_name(resolved))


def _require_loop_transport_admission(
    loop: LoopRecord, resolved: Any, config: DispatchConfig
) -> None:
    """Apply the adaptive transport gate before loop state/worktree machinery."""
    transport = resolve_inference_transport(config, resolved.runner, resolved.model)
    try:
        require_adaptive_route_admission(transport, config)
    except TransportAdmissionError as exc:
        raise ValueError(f"cannot run loop {loop.loop_id}: {exc}") from exc


def _apply_bumps(
    loop: LoopRecord,
    budget_usd: float | None,
    max_steps: int | None,
    timeout_minutes: int | None,
) -> None:
    """Overwrite the loop's caps with any provided non-None bumps."""
    if budget_usd is not None:
        loop.budget_usd = budget_usd
    if max_steps is not None:
        loop.max_steps = max_steps
    if timeout_minutes is not None:
        loop.wall_clock_secs = timeout_minutes * 60


def _resolve_runner_override(
    runner_name: str, model: str | None, config: DispatchConfig
) -> ResolvedRoute:
    """ResolvedRoute for an explicit runner name (+ optional model override).

    Native-ness and priced-ness are NOT checked here — _validate_resume owns
    those, so an override failure surfaces exactly like a normal resume failure.
    """
    factories = _get_runner_factories()
    if runner_name not in factories:
        raise ValueError(
            f"unknown runner {runner_name!r}; available: {', '.join(sorted(factories))}"
        )
    return ResolvedRoute(
        # `is not None` (not `or`): an explicit `--model ""` must propagate to the
        # priced check and fail closed, not silently fall back to the config model.
        runner=factories[runner_name](),
        model=model if model is not None else config.agent.model,
    )


def loop_route_rejection(
    resolved: ResolvedRoute, *, publish: str = "checkout"
) -> str | None:
    """Why this route cannot drive a first-class loop, or None if it can.

    v1 loops are runner-driven: only a runner declaring ``loop_style="native"``
    supervises its own steps. A routed non-native runner (#1111 — loops now
    resolve through ``[routing]`` like ordinary dispatch) fails closed with this
    reason rather than silently degrading to a single-shot dispatch.
    """
    publication_rejection = host_publication_route_rejection(
        resolved, "first-class loops", publish=publish
    )
    if publication_rejection is not None:
        return publication_rejection
    style = getattr(resolved.runner, "loop_style", None)
    if style == "native":
        return None
    name = getattr(resolved.runner, "name", "?")
    return (
        f"runner {name!r} is not native-loop-capable (loop_style={style!r}); "
        "first-class loops require a native-loop runner"
    )


def host_publication_route_rejection(
    resolved: ResolvedRoute, flow: str, *, publish: str = "checkout"
) -> str | None:
    if (
        publish == "canonical"
        or getattr(resolved.runner, "publication_style", "agent") != "host"
    ):
        return None
    return (
        f"{flow} does not support host-publication runners; "
        "v1 has no host publication broker for this flow"
    )


def _route_runner_name(resolved: ResolvedRoute) -> str:
    """The runner name a route runs under ("" if the runner declares none)."""
    return getattr(resolved.runner, "name", "") or ""


def _route_override_in_effect(store: RunStore, complexity: str) -> bool:
    """Is a DB route override in effect for a loop routed at *complexity*?

    Only the fields that can change THIS loop's route decision count: the tier's
    own ``routing`` entry, ``routing.default`` (used when the tier has no entry),
    and ``agent.model`` (the fallback model, and the whole model on the
    no-routing path). An override on some other tier is not this loop's business.
    """
    governing = {"agent.model", "routing.default"}
    if complexity:
        governing.add(f"routing.{complexity}")
    active = {str(row["field"]) for row in store.list_config_overrides()}
    return bool(governing & active)


def _config_route_for_loop(loop: LoopRecord, config: DispatchConfig) -> ResolvedRoute:
    """The config route AT THE LOOP'S TIER — never the bare default.

    Re-resolving without the recorded tier is the #1111 escalation by another
    door: a simple-tier loop would land back on ``[routing].default``. A pre-v35
    loop recorded no tier, so it resolves at the default tier, as it always did.
    """
    return resolve_runner(config, complexity=loop.complexity or None)


def _continuation_route(
    loop: LoopRecord, config: DispatchConfig, store: RunStore
) -> ResolvedRoute:
    """The route a flag-less resume continues on.

    Precedence:

    1. **A DB route override** (``config_overrides``) wins. That table is the
       #659 runtime-override surface — an explicit, authenticated act by a second
       actor (the web /config page, ``kew config set``) whose entire purpose is
       steering the effective route without a commit. If the record outranked it,
       an in-flight loop would be unsteerable except by killing and re-dispatching
       it (O1/#892 override parity). Resolved at the loop's recorded tier.
    2. **The loop's own recorded route** (#1111). Absent such an override, the
       record is the authority: re-resolving config here would drop a
       complexity-routed loop back onto ``[routing].default``/``[agent]`` from its
       second session on — the exact escalation this issue removed from the first
       session — and would also forget an operator's explicit ``--runner/--model``
       reassignment. A plain ``kew.toml`` edit therefore does NOT re-route a live
       loop; only the override surface does.
    3. **The config route at the loop's tier**, for a pre-v35 record that carries
       no route to continue — the old behaviour, for records with no better answer.
    """
    if _route_override_in_effect(store, loop.complexity):
        return _config_route_for_loop(loop, config)
    if not loop.runner_name or not loop.model:
        return _config_route_for_loop(loop, config)
    return _resolve_runner_override(loop.runner_name, loop.model, config)


def _inherited_model_for_runner_swap(
    loop: LoopRecord, runner_name: str, config: DispatchConfig
) -> str | None:
    """The model ``--runner X`` inherits when no ``--model`` was given.

    Operator ruling (#1111): neither direction may substitute silently. Dropping
    the loop's recorded model changes what runs behind the operator's back;
    inheriting it blindly mints a cross-provider route (``claude-code`` +
    ``openrouter/...``) that passes the priced check, reaches the runner, and is
    then written durably so every later resume repeats it. So the inherited
    model must be one the new runner can actually drive — otherwise refuse and
    make the operator name both halves with an explicit ``--model``.

    Returns None only for a pre-v35 loop with no recorded model AND no configured
    one, where there is nothing to validate and the priced check fails closed.
    """
    inherited = loop.model or config.agent.model
    if not inherited or runner_name not in _get_runner_factories():
        # An unknown runner name is _resolve_runner_override's error to raise,
        # with its own "available: ..." message — not a compatibility failure.
        return inherited or None
    if not runner_accepts_model(runner_name, inherited):
        source = "recorded" if loop.model else "configured"
        raise ValueError(
            f"cannot resume {loop.loop_id}: runner {runner_name!r} cannot run the "
            f"{source} model {inherited!r}; pass an explicit --model to name the "
            "route you want"
        )
    return inherited


def _resolve_resume_route(
    loop: LoopRecord,
    runner_name: str | None,
    model: str | None,
    config: DispatchConfig,
    store: RunStore,
) -> ResolvedRoute:
    """Pick the continuation route: a --runner override, else a DB route override,
    else the loop's own recorded route, with an optional --model swap in any case."""
    if runner_name is not None:
        # `is not None`, not `or`: an explicit `--model ""` must still reach the
        # priced check and fail closed rather than inherit anything.
        inherited = (
            model
            if model is not None
            else _inherited_model_for_runner_swap(loop, runner_name, config)
        )
        return _resolve_runner_override(runner_name, inherited, config)
    resolved = _continuation_route(loop, config, store)
    if model is not None:
        return ResolvedRoute(runner=resolved.runner, model=model)
    return resolved


def _build_continuation_prompt(
    goal_text: str, prior_reason: str | None, guidance: str
) -> str:
    """Continuation prompt: original goal + why it stopped + the human guidance."""
    return (
        f"{goal_text}\n\n"
        "--- This loop was paused for human review and is now being resumed. ---\n"
        f"Why it stopped: {prior_reason or 'unknown'}\n"
        f"Human guidance for this continuation: {guidance}\n"
        "Your prior work is already present in this worktree; continue from its "
        "current state. The same verification gate still decides success."
    )


class DelegateDriver:
    """Supervise one Claude Code session pursuing a loop to a verified close."""

    def __init__(
        self,
        store: RunStore,
        *,
        config: DispatchConfig,
        gate: Gate | None = None,
    ) -> None:
        self._store = store
        self._config = config
        # Execution evidence for this driver invocation, independent of step events.
        self._launch_state = LaunchState()
        # An explicitly injected gate (incl. test fakes) is used as-is. When None,
        # build the production gate from config; ``_gate is None`` after this means
        # no verify command is configured -> fail closed in ``run``.
        self._verification_admission: HostVerificationAdmission | None = None
        self._agent_posture: str | None = None
        self._gate: Gate | None = (
            gate if gate is not None else _build_default_gate(config)
        )

    def _admit_agent_launch(self, loop: LoopRecord, *, runner: str) -> str | None:
        """Resolve the agent posture; on refusal record exit-2 evidence (#1977).

        Returns the refusal text, or None when the launch is admitted. The
        refusal writes the same pre-launch evidence as a refused remediation: a
        ``refused`` run row, a ``sandbox_resolved`` refusal and an exit-2
        ``dispatch_completed``.
        """
        try:
            self._agent_posture = resolve_loop_agent_posture(
                self._config, runner=runner
            )
        except SandboxRuntimeUnavailable as exc:
            print(f"Security posture: {exc}")
            run_id = str(uuid.uuid4())
            try:
                record_remediation_refusal(
                    self._store,
                    self._config,
                    exc,
                    runner=runner,
                    run_id=run_id,
                    run_kind="loop",
                    issue_num=loop.issue_num or 0,
                    branch=_loop_branch(loop, self._config),
                    item_id=str(loop.issue_num or 0),
                )
            except Exception as audit_exc:  # noqa: BLE001 -- the refusal still stands
                _warn(f"Failed to record loop refusal {run_id}: {audit_exc}")
            return str(exc)
        _announce_agent_posture(self._agent_posture)
        return None

    def _admitted_agent_posture(self) -> str:
        """The posture ``_admit_agent_launch`` admitted; never launch without one."""
        if self._agent_posture is None:
            raise RuntimeError("loop agent launch was not admitted (#1977)")
        return self._agent_posture

    def _admit_verification(
        self, loop: LoopRecord, *, runner: str
    ) -> HostVerificationAdmission:
        if self._config.publication.publish == "canonical" and isinstance(
            self._gate, GateEvaluator
        ):
            self._gate.prepare_verification(self._config.sandbox, runner=runner)
        admission = _loop_verification_admission(self._config, self._gate)
        self._verification_admission = admission
        _announce_verification_posture(admission)
        if admission.refusal is not None:
            audit.emit(
                self._store,
                "verification_checked",
                actor=resolve_actor(),
                issue_num=loop.issue_num,
                repo=loop.repo,
                payload={
                    "loop_id": loop.loop_id,
                    "passed": False,
                    "sabotage": False,
                    **admission.record_fields(),
                },
            )
        return admission

    def run(
        self, loop: LoopRecord, *, route: ResolvedRoute | None = None
    ) -> LoopRecord:
        verification_admission = self._admit_verification(
            loop, runner=route.runner.name if route is not None else loop.runner_name
        )
        if verification_admission.refusal is not None:
            return self._finalize(
                loop,
                "needs_human",
                verification_admission.refusal,
                [],
            )
        # The caller's route wins: dispatch resolves it through ``[routing]``
        # (#1111), and re-resolving here would silently revert to the ``[agent]``
        # default. No route supplied -> the config route, as before.
        resolved = route if route is not None else resolve_runner(self._config)
        agent_refusal = self._admit_agent_launch(
            loop, runner=_route_runner_name(resolved)
        )
        if agent_refusal is not None:
            return self._finalize(loop, "needs_human", agent_refusal, [])
        try:
            _require_loop_transport_admission(loop, resolved, self._config)
        except ValueError as exc:
            return self._finalize(loop, "needs_human", str(exc), [])
        refusal = self._route_refusal(resolved)
        if refusal is not None:
            return self._finalize(loop, "needs_human", refusal, [])

        try:
            binding = self._new_publication_binding()
        except (OSError, RuntimeError, ValueError) as exc:
            return self._finalize(
                loop,
                "needs_human",
                f"canonical publication setup failed: {exc}",
                [],
            )
        worktree = self._prepare_admitted_worktree(loop, binding)
        try:
            claim_checkout(worktree)
            # Persist the admitted route only after checkout admission succeeds.
            # A busy checkout must leave the loop's previous record untouched.
            self._record_route(loop, resolved)
            return self._run_session(
                loop,
                worktree,
                resolved,
                binding=binding,
                prompt_body=loop.goal_text,
                idx_offset=0,
                prior_step_count=0,
                prior_spent_usd=0.0,
                verification_admission=verification_admission,
                fresh_checkout=True,
            )
        finally:
            release_checkout_claim(worktree)

    def _route_refusal(self, resolved: ResolvedRoute) -> str | None:
        """The fail-closed reason a resolved route cannot drive a loop, or None.

        Checked in order: a non-native runner cannot drive its own loop; config
        produced no usable gate (spec anti-proxy); an unpriced model has no cost
        bound (R2); the resolved runner fails its preflight.
        """
        rejection = loop_route_rejection(
            resolved, publish=self._config.publication.publish
        )
        if rejection is not None:
            return rejection
        if self._gate is None:
            return _no_gate_reason(self._config)
        if not _is_priced(resolved.model, self._config):
            return (
                f"model {resolved.model!r} is not in the pricing table — "
                "refusing to run a loop with no cost bound"
            )
        return _resolved_runner_preflight_failure(self._config, resolved)

    def _prepare_admitted_worktree(
        self, loop: LoopRecord, binding: _PublicationBinding
    ) -> Path:
        """Materialize the loop checkout; a checkout admission refusal is
        finalized as needs_human, recorded as an exit-2 completion, and re-raised
        so the CLI boundary translates it (#1931)."""
        try:
            return _prepare_worktree(
                loop,
                self._config,
                base_sha=binding.base_sha,
                store=self._store,
                binding=binding,
            )
        except CheckoutAdmissionRefusal as exc:
            self._finalize(loop, "needs_human", str(exc), [])
            audit.emit(
                self._store,
                "dispatch_completed",
                actor=resolve_actor(),
                item_id=loop.item_id,
                issue_num=loop.issue_num,
                payload={
                    "exit_code": 2,
                    "error_category": exc.category,
                    "reason": str(exc),
                },
            )
            raise

    def _record_route(self, loop: LoopRecord, resolved: ResolvedRoute) -> None:
        """Persist the (runner, model) this session runs under on the loop.

        Written before the session starts, so a crash mid-run still leaves the
        route reconstructable. No-op when the record already names this route.
        """
        runner_name = _route_runner_name(resolved)
        if (loop.runner_name, loop.model) == (runner_name, resolved.model):
            return
        loop.runner_name = runner_name
        loop.model = resolved.model
        loop.updated_at = datetime.now(UTC)
        self._store.save_loop(loop)

    def resume(
        self,
        loop: LoopRecord,
        guidance: str,
        *,
        runner_name: str | None = None,
        model: str | None = None,
        budget_usd: float | None = None,
        max_steps: int | None = None,
        timeout_minutes: int | None = None,
    ) -> LoopRecord:
        """Resume a paused loop with operator guidance and optional cap bumps.

        Pre-flight validation raises ValueError (CLI surfaces) WITHOUT mutating
        the loop. (state / native-runner / gate / priced.)
        """
        resolved = _resolve_resume_route(
            loop, runner_name, model, self._config, self._store
        )
        _validate_resume(loop, resolved, self._gate, self._config)
        if loop.issue_num is None:  # also narrows the type for mypy
            raise ValueError(f"cannot resume {loop.loop_id}: loop has no issue_num")
        verification_admission = self._admit_verification(
            loop, runner=resolved.runner.name
        )
        if verification_admission.refusal is not None:
            raise ValueError(
                f"cannot resume {loop.loop_id}: {verification_admission.refusal}"
            )
        agent_refusal = self._admit_agent_launch(
            loop, runner=_route_runner_name(resolved)
        )
        if agent_refusal is not None:
            raise ValueError(f"cannot resume {loop.loop_id}: {agent_refusal}")
        worktree_path = dispatch_checkout_path(
            self._config,
            issue_num=loop.issue_num,
        )
        claim_checkout(worktree_path)
        try:
            return self._resume_claimed(
                loop,
                guidance,
                worktree_path,
                resolved,
                verification_admission,
                budget_usd=budget_usd,
                max_steps=max_steps,
                timeout_minutes=timeout_minutes,
            )
        finally:
            # Cover every prelaunch failure and the complete resumed session.
            # Preserving checkout bytes never extends the session's lease.
            release_checkout_claim(worktree_path)

    def _resume_claimed(
        self,
        loop: LoopRecord,
        guidance: str,
        worktree_path: Path,
        resolved: ResolvedRoute,
        verification_admission: HostVerificationAdmission,
        *,
        budget_usd: float | None,
        max_steps: int | None,
        timeout_minutes: int | None,
    ) -> LoopRecord:
        """Restore and run a continuation while the caller holds its lease."""
        assert loop.issue_num is not None
        branch = _loop_branch(loop, self._config)
        item_id = str(loop.issue_num)
        previous = self._previous_publication_binding(
            loop,
            worktree=worktree_path,
            branch=branch,
            item_id=item_id,
        )
        legacy = previous is None
        if previous is None:
            previous = self._legacy_resume_binding(loop, worktree_path)
        binding = _PublicationBinding(
            run_id=str(uuid.uuid4()),
            base_sha=previous.base_sha,
            lineage_run_id=previous.lineage_run_id,
        )
        # Worktree first (fail-closed) — if unrestorable, do NOT flip state. An
        # absent checkout is restored from the pushed branch and rebound; an
        # existing one on another branch is preserved, because a loop with a
        # recorded binding cannot rebind to the directory that would replace it
        # and would lose those commits (governed review round 2, 2026-09-21).
        worktree = reuse_or_restore_worktree(
            issue_num=loop.issue_num,
            config=self._config,
            expected_generation=previous.checkout_generation,
            on_restore=(
                None
                if legacy
                else partial(
                    self._bind_restored_publication,
                    loop,
                    previous=previous,
                    run_id=binding.run_id,
                )
            ),
            replace_existing=legacy,
        )
        binding.checkout_generation = read_checkout_generation(worktree)
        if legacy:
            self._adopt_legacy_checkout(loop, worktree, binding)
        if self._config.publication.publish == "canonical":
            self._require_current_publication_binding(loop, worktree, previous)
        _apply_bumps(loop, budget_usd, max_steps, timeout_minutes)
        prior_reason = loop.status_reason
        prior_step_count = loop.step_count
        prior_spent_usd = loop.spent_usd
        prior_iters = self._store.list_iterations(loop.loop_id)
        # idx_offset = (max prior idx) + 1, NOT len(prior): iteration idxs are not
        # guaranteed contiguous and there is no UNIQUE(loop_id, idx) constraint, so
        # len() could silently duplicate an idx and corrupt timeline ordering.
        idx_offset = max((it.idx for it in prior_iters), default=-1) + 1
        old_runner = loop.runner_name
        # Record BOTH halves of the route this session actually runs (#1111), and
        # take the runner from `resolved` rather than the --runner flag: on the
        # config-fallback path the resolved runner can differ from the one the
        # record names, and writing only the model there would leave a (stale
        # runner, fresh model) pair no session ever ran — which the NEXT resume
        # would read as authoritative and execute. Durability also means a
        # --model swap is not forgotten by the session after it.
        loop.runner_name = _route_runner_name(resolved)
        loop.model = resolved.model
        # A runner change is a route change whether or not an operator asked for
        # it, so the audit below keys off what changed, not off the flag.
        reassigned = loop.runner_name != old_runner
        # Flip -> pursuing + audit (the soft concurrency guard; loop is idle).
        self._store.clear_pause(loop.loop_id)
        loop.pause_requested = False
        loop.status = "pursuing"
        loop.finished_at = None
        loop.updated_at = datetime.now(UTC)
        self._store.save_loop(loop)
        audit.emit(
            self._store,
            "loop_resumed",
            actor=resolve_actor(),
            issue_num=loop.issue_num,
            repo=loop.repo,
            payload={
                "loop_id": loop.loop_id,
                "guidance": guidance,
                "budget_usd": loop.budget_usd,
                "max_steps": loop.max_steps,
            },
        )
        if reassigned:
            audit.emit(
                self._store,
                "loop_reassigned",
                actor=resolve_actor(),
                issue_num=loop.issue_num,
                repo=loop.repo,
                payload={
                    "loop_id": loop.loop_id,
                    "old_runner": old_runner,
                    "new_runner": loop.runner_name,
                    "model": resolved.model,
                },
            )
        prompt_body = _build_continuation_prompt(loop.goal_text, prior_reason, guidance)
        return self._run_session(
            loop,
            worktree,
            resolved,
            prompt_body=prompt_body,
            idx_offset=idx_offset,
            prior_step_count=prior_step_count,
            prior_spent_usd=prior_spent_usd,
            verification_admission=verification_admission,
            binding=binding,
        )

    def _run_session(
        self,
        loop: LoopRecord,
        worktree: Path,
        resolved: Any,
        *,
        prompt_body: str,
        idx_offset: int,
        prior_step_count: int,
        prior_spent_usd: float,
        verification_admission: HostVerificationAdmission,
        binding: _PublicationBinding | None = None,
        fresh_checkout: bool = False,
    ) -> LoopRecord:
        try:
            binding = binding or self._prepare_publication(loop, worktree)
            if fresh_checkout:
                self.bind_checkout(loop, worktree, binding=binding)
            else:
                self._bind_publication(loop, worktree, binding)
        except (OSError, RuntimeError, ValueError) as exc:
            _withdraw_loop_replacement(
                loop,
                self._config,
                self._store,
                worktree,
                binding,
                reason="loop publication setup failed before launch",
            )
            finished = self._finalize(
                loop,
                "needs_human",
                f"canonical publication setup failed: {exc}",
                [],
                prior_step_count=prior_step_count,
                prior_spent_usd=prior_spent_usd,
            )
            if isinstance(exc, CheckoutAdmissionRefusal):
                raise
            return finished
        result = self._launch_bound_session(
            loop,
            worktree,
            resolved,
            binding=binding,
            prompt_body=prompt_body,
            verification_admission=verification_admission,
            prior_step_count=prior_step_count,
            prior_spent_usd=prior_spent_usd,
        )
        self.launched = self.launched or result.launched
        if binding.abandonment is not None and not binding.worker_launched:
            return self._finalize(
                loop,
                "needs_human",
                "loop stopped before worker launch",
                [],
                prior_step_count=prior_step_count,
                prior_spent_usd=prior_spent_usd,
            )
        steps = _read_steps(result.event_file)
        try:
            publication = self._publish_iteration(loop, worktree, binding)
        except (OSError, RuntimeError, ValueError, PublicationHeld) as exc:
            self._record_iterations(loop, result.run_id, steps, idx_offset=idx_offset)
            finished = self._finalize(
                loop,
                "needs_human",
                f"canonical publication failed: {exc}",
                steps,
                prior_step_count=prior_step_count,
                prior_spent_usd=prior_spent_usd,
            )
            if isinstance(exc, dispatch_core._CheckoutIdentityRefused):
                raise
            return finished
        self._record_iterations(loop, result.run_id, steps, idx_offset=idx_offset)
        status, reason = self._decide(
            loop,
            worktree,
            result,
            steps,
            binding=binding,
            publication=publication,
        )
        return self._finalize(
            loop,
            status,
            reason,
            steps,
            prior_step_count=prior_step_count,
            prior_spent_usd=prior_spent_usd,
        )

    def _launch_bound_session(
        self,
        loop: LoopRecord,
        worktree: Path,
        resolved: Any,
        *,
        binding: _PublicationBinding,
        prompt_body: str,
        verification_admission: HostVerificationAdmission,
        prior_step_count: int,
        prior_spent_usd: float,
    ) -> Any:
        """Withdraw replacements only before the first paid launch boundary.

        A refusal before the launch (a changed checkout identity, a prompt
        configuration conflict, or a daily or weekly cap) finalizes the loop as
        ``needs_human`` — which ``_RESUMABLE`` admits — before it re-raises, so
        the loop is never left ``pursuing`` and resumes once the operator fixes
        the cause.
        """
        try:
            return self._launch(
                loop,
                worktree,
                resolved,
                run_id=binding.run_id,
                prompt_body=prompt_body,
                verification_admission=verification_admission,
                paid_launch_check=partial(binding.mark_worker_launch, worktree),
            )
        except (
            dispatch_core._CheckoutIdentityRefused,
            PromptConfigurationError,
            # A daily or weekly cap refused the launch at admission (#3205).
            BudgetExceeded,
        ) as exc:
            self._finalize(
                loop,
                "needs_human",
                str(exc),
                [],
                prior_step_count=prior_step_count,
                prior_spent_usd=prior_spent_usd,
            )
            raise
        finally:
            _withdraw_loop_replacement(
                loop,
                self._config,
                self._store,
                worktree,
                binding,
                reason="loop stopped before worker launch",
            )

    def _new_publication_binding(
        self, *, run_id: str | None = None
    ) -> _PublicationBinding:
        """Resolve the immutable base before creating a fresh checkout."""
        run_id = run_id or str(uuid.uuid4())
        if self._config.publication.publish != "canonical":
            return _PublicationBinding(run_id=run_id, base_sha="", lineage_run_id="")
        base_sha = dispatch_core._trusted_dispatch_base(self._config)
        return _PublicationBinding(
            run_id=run_id,
            base_sha=base_sha,
            lineage_run_id=run_id,
        )

    def _prepare_publication(
        self, loop: LoopRecord, worktree: Path
    ) -> _PublicationBinding:
        """Recover a prior base before a resumed loop launch."""
        run_id = str(uuid.uuid4())
        if self._config.publication.publish != "canonical":
            return _PublicationBinding(
                run_id=run_id,
                base_sha="",
                lineage_run_id="",
                checkout_generation=read_checkout_generation(worktree),
            )
        branch = _loop_branch(loop, self._config)
        item_id = str(loop.issue_num or 0)
        previous = self._previous_publication_binding(
            loop,
            worktree=worktree,
            branch=branch,
            item_id=item_id,
        )
        if previous is not None:
            return _PublicationBinding(
                run_id=run_id,
                base_sha=previous.base_sha,
                lineage_run_id=previous.lineage_run_id,
                checkout_generation=previous.checkout_generation,
            )
        binding = self._new_publication_binding(run_id=run_id)
        binding.checkout_generation = read_checkout_generation(worktree)
        return binding

    def bind_checkout(
        self,
        loop: LoopRecord,
        worktree: Path,
        *,
        binding: _PublicationBinding | None = None,
    ) -> None:
        """Record a fresh loop's checkout while the caller holds its lease.

        Preparation seams may return a directory without a generation.
        Capture that generation here, minting only when its sidecar is absent.
        Preserve any generation already captured during preparation.
        Resume must use its recorded binding instead of this fresh-run path.
        """
        require_checkout_claim(worktree)
        binding = binding or self._new_publication_binding()
        if not binding.checkout_generation:
            binding.checkout_generation = capture_checkout_generation(worktree)
        self._bind_publication(loop, worktree, binding)

    def _bind_publication(
        self,
        loop: LoopRecord,
        worktree: Path,
        binding: _PublicationBinding,
    ) -> None:
        """Persist a fresh checkout's exact base before its paid launch."""
        binding.checkout_binding = dispatch_core._hold_checkout_binding(
            worktree, binding.checkout_generation
        )
        binding.require_generation(worktree, boundary="loop base recording")
        _record_loop_checkout_binding(
            self._store, loop, worktree, binding, self._config
        )
        if (
            self._config.publication.publish != "canonical"
            or binding.lineage_run_id != binding.run_id
        ):
            return
        branch = _loop_branch(loop, self._config)
        item_id = str(loop.issue_num or 0)
        dispatch_core._bind_publication_base(
            self._store,
            publish="canonical",
            base=binding.base_sha,
            run_id=binding.run_id,
            issue_num=loop.issue_num or 0,
            item_id=item_id,
            branch=branch,
            checkout=worktree,
            context={"loop_id": loop.loop_id},
        )

    def _previous_publication_binding(
        self,
        loop: LoopRecord,
        *,
        worktree: Path,
        branch: str,
        item_id: str,
    ) -> _PreviousPublicationBinding | None:
        """Read this loop's original immutable base for a continuation."""
        recorded = _previous_loop_checkout_binding(
            self._store, loop.loop_id, item_id, branch, worktree
        )
        if recorded is not None:
            return recorded
        events = [
            event
            for event in self._store.list_audit_events(
                limit=None, event="publication_base"
            )
            if _publication_event_matches_loop(event, loop.loop_id)
        ]
        if not events:
            return None
        found: list[_PreviousPublicationBinding] = []
        for event in events:
            binding = _recorded_binding_for_event(
                self._store,
                event,
                item_id=item_id,
                branch=branch,
                worktree=worktree,
            )
            if binding is not None:
                found.append(binding)
        return _select_previous_binding(loop.loop_id, found)

    def _legacy_resume_binding(
        self, loop: LoopRecord, worktree_path: Path
    ) -> _PreviousPublicationBinding:
        """Admit a loop that predates recorded checkout bindings.

        Loops created before bindings were recorded have neither a
        ``loop_checkout_bound`` nor a ``publication_base`` event. In checkout
        mode nothing depends on a recorded base, so the resume adopts the
        loop's checkout as its binding once (``_adopt_legacy_checkout``). In
        canonical mode the recorded base is the publication authority, so the
        loop cannot continue; the refusal names a verb that exists.
        """
        if self._config.publication.publish == "canonical":
            raise RuntimeError(
                f"loop {loop.loop_id} has no recorded checkout binding and "
                "canonical publication needs its recorded base; start a fresh "
                "loop with kew dispatch <issue> --loop (the checkout at "
                f"{worktree_path} is preserved)"
            )
        # The caller holds the checkout claim. Adoption reads complete
        # evidence, binds a legacy one-token sidecar, or mints one for an
        # unbound directory. A checkout that is absent or cannot be bound
        # leaves the loop nothing to resume against, and the refusal names a
        # verb that acts on a loop.
        try:
            generation = adopt_legacy_checkout_generation(worktree_path)
        except CheckoutGenerationRefusal as exc:
            raise RuntimeError(
                f"loop {loop.loop_id} has no recorded checkout binding and its "
                f"checkout at {worktree_path} cannot be bound ({exc.cause}); "
                "the checkout is preserved; start a fresh loop with "
                "kew dispatch <issue> --loop"
            ) from exc
        if not generation:
            raise RuntimeError(
                f"loop {loop.loop_id} has no recorded checkout binding and no "
                f"checkout at {worktree_path}; start a fresh loop with "
                "kew dispatch <issue> --loop"
            )
        return _PreviousPublicationBinding(
            lineage_run_id="", base_sha="", checkout_generation=generation, event_id=0
        )

    def _adopt_legacy_checkout(
        self, loop: LoopRecord, worktree: Path, binding: _PublicationBinding
    ) -> None:
        """Record the adopted checkout so the next resume reads persisted evidence."""
        binding.checkout_generation = capture_checkout_generation(worktree)
        _record_loop_checkout_binding(
            self._store, loop, worktree, binding, self._config
        )
        audit.emit(
            self._store,
            "loop_checkout_adopted",
            actor=resolve_actor(),
            issue_num=loop.issue_num,
            repo=loop.repo,
            payload={
                "loop_id": loop.loop_id,
                "checkout": str(worktree.resolve()),
                "reason": "loop predates recorded checkout bindings",
            },
        )

    def _bind_restored_publication(
        self,
        loop: LoopRecord,
        worktree: Path,
        previous: _PreviousPublicationBinding,
        run_id: str,
    ) -> None:
        """Bind the restored checkout to the run that restored it.

        A recorded binding is immutable, so rebinding under the PRIOR run id
        always refused and a loop whose checkout was gone could never resume
        (governed review round 5, 2026-09-21). The resuming run records its own
        binding for the directory it just created; the lineage and the
        publication base still come from the previous one.
        """
        binding = _PublicationBinding(
            run_id=run_id,
            base_sha=previous.base_sha,
            lineage_run_id=previous.lineage_run_id,
            checkout_generation=read_checkout_generation(worktree),
        )
        self._bind_publication(loop, worktree, binding)

    def _require_current_publication_binding(
        self,
        loop: LoopRecord,
        worktree: Path,
        previous: _PreviousPublicationBinding,
    ) -> None:
        """Require the reused or restored checkout to carry durable base evidence."""
        base = dispatch_core._recorded_publication_base(
            self._store,
            run_id=previous.lineage_run_id,
            item_id=str(loop.issue_num or 0),
            branch=_loop_branch(loop, self._config),
            checkout=worktree,
        )
        if base != previous.base_sha:
            # A publication base is keyed by its run AND the checkout's host
            # identity, and both bindings are immutable, so a canonical loop
            # cannot adopt a directory that replaced the one its base names.
            # That is the contract, not a gap: canonical publication builds on
            # the exact tree whose base kew recorded (governed review round 6,
            # 2026-09-22). The work in the pushed branch is untouched.
            raise PublicationError(
                "push",
                f"loop {loop.loop_id} no longer has the checkout its "
                "publication base was recorded against, and a recorded base "
                "cannot move; start a fresh loop with kew dispatch <issue> --loop",
            )

    def _publish_iteration(
        self,
        loop: LoopRecord,
        worktree: Path,
        binding: _PublicationBinding,
    ) -> CanonicalPublication | CanonicalNoChange | None:
        """Publish one completed canonical loop iteration from the host."""
        if self._config.publication.publish != "canonical":
            return None
        binding.require_generation(worktree, boundary="loop host publication")
        branch = _loop_branch(loop, self._config)
        item_id = str(loop.issue_num or 0)
        if binding.lineage_run_id != binding.run_id:
            dispatch_core.bind_publication_lineage(
                self._store,
                run_id=binding.run_id,
                lineage_run_id=binding.lineage_run_id,
                item_id=item_id,
                branch=branch,
                checkout=worktree,
                base_sha=binding.base_sha,
            )
        publication: CanonicalPublication | CanonicalNoChange
        if self._loop_has_pr(loop, branch):
            observed = dispatch_core._git_push_branch(
                branch,
                worktree,
                # New agent code needs fresh human CI approval (#2033).
                before_push=partial(
                    dispatch_core.revoke_ci_approval, branch, self._config
                ),
                repo=self._config.project.repo or None,
                base_sha=binding.base_sha,
                message=f"kew: publish loop work for #{item_id}",
                publish="canonical",
                store=self._store,
                run_id=binding.run_id,
                item_id=item_id,
            )
            if not isinstance(observed, (CanonicalPublication, CanonicalNoChange)):
                raise PublicationError("push", "canonical loop publication disappeared")
            publication = observed
        else:
            observed_publications: list[CanonicalPublication | CanonicalNoChange] = []
            goal = loop.goal_text.splitlines()[0]
            dispatch_core._push_and_open_pr(
                worktree,
                branch,
                item_id,
                goal,
                self._config,
                store=self._store,
                run_id=binding.run_id,
                base_sha=binding.base_sha,
                on_publication=observed_publications.append,
                # A loop has no Dispatch Report: its PR is titled by its goal.
                pr_title=goal,
            )
            if len(observed_publications) != 1:
                raise PublicationError(
                    "push", "canonical loop publication produced no exact result"
                )
            publication = observed_publications[0]
        self._record_publication_fact(loop, binding, publication)
        return publication

    def _loop_has_pr(self, loop: LoopRecord, branch: str) -> bool:
        """Return whether an earlier run of this loop recorded its PR URL."""
        if loop.issue_num is None:
            return False
        return any(
            run.branch == branch and run.run_kind == "loop" and bool(run.pr_url)
            for run in self._store.list_by_issue(loop.issue_num)
        )

    def _record_publication_fact(
        self,
        loop: LoopRecord,
        binding: _PublicationBinding,
        publication: CanonicalPublication | CanonicalNoChange,
    ) -> None:
        """Record the exact canonical result before an iteration is gated."""
        if isinstance(publication, CanonicalPublication):
            event = "loop_iteration_published"
            payload: dict[str, object] = {
                "loop_id": loop.loop_id,
                "branch": _loop_branch(loop, self._config),
                "commit_sha": publication.commit_sha,
                "tree_sha": publication.tree_sha,
                "changed_paths": list(publication.changed_paths),
            }
        else:
            event = "checkout_no_change_evidence"
            payload = {
                "loop_id": loop.loop_id,
                "branch": _loop_branch(loop, self._config),
                "base_sha": publication.base_sha,
                "tree_sha": publication.tree_sha,
                "changed_paths": [],
            }
        audit.emit(
            self._store,
            event,
            actor=Actor("policy", "host-publication"),
            run_id=binding.run_id,
            issue_num=loop.issue_num,
            item_id=str(loop.issue_num or 0),
            repo=loop.repo,
            payload=payload,
        )
        recorded = self._store.list_audit_events(
            limit=None,
            event=event,
            item_id=str(loop.issue_num or 0),
        )
        if not any(
            candidate.run_id == binding.run_id and candidate.payload == payload
            for candidate in recorded
        ):
            raise PublicationError(
                "push", f"loop {loop.loop_id} publication fact was not recorded"
            )

    def _should_pause(self, loop_id: str) -> bool:
        try:
            rec = self._store.get_loop(loop_id)
        except Exception:  # noqa: BLE001 — fail-safe (orchestrator also guards)
            return False
        return bool(rec and rec.pause_requested)

    @property
    def launched(self) -> bool:
        """Retain execution evidence when tracked-agent teardown raises."""
        return self._launch_state.launched is not False

    @launched.setter
    def launched(self, value: bool) -> None:
        self._launch_state.launched = value

    def _launch(
        self,
        loop: LoopRecord,
        worktree: Path,
        resolved: Any,
        *,
        run_id: str,
        prompt_body: str,
        verification_admission: HostVerificationAdmission,
        paid_launch_check: Callable[[], None] | None = None,
    ) -> Any:
        # Repo-map parity (spec D5 / R3-7): the loop path built prompts with no
        # map at all — route it through the same seam as the host/sandbox paths.
        repo_map = build_repo_map(self._config, worktree, hint=prompt_body)
        # Off the host store, never `worktree` (#3022).
        addendum = launch_addendum(self._store)
        prompt = build_prompt(
            issue_num=loop.issue_num or 0,
            issue_title="",
            issue_body=prompt_body,
            complexity="",
            config=self._config,
            has_coderabbit=False,
            repo_map=repo_map,
            has_repo_skills=(
                str(getattr(resolved.runner, "name", "")) == "claude-code"
                and (worktree / ".claude" / "skills").is_dir()
            ),
            publication_style=(
                "host"
                if self._config.publication.publish == "canonical"
                else getattr(resolved.runner, "publication_style", "agent")
            ),
            runner_name=str(getattr(resolved.runner, "name", "")),
            # Same base-ref-wins rubric resolution as the dispatch paths, and
            # the same root: the operator's checkout, never `worktree`, which a
            # loop resume restores from the prior attempt's branch (#2454
            # round 2).
            rubric_root=operator_repo_root(),
            profile_addendum=addendum,
        )
        assert verification_admission.refusal is None
        assert verification_admission.posture is not None
        loop_started_payload = {
            "loop_id": loop.loop_id,
            "runner": loop.runner_name,
            **verification_admission.record_fields(),
        }
        audit.emit(
            self._store,
            "loop_started",
            actor=resolve_actor(),
            run_id=run_id,
            issue_num=loop.issue_num,
            item_id=str(loop.issue_num or 0),
            repo=loop.repo,
            payload=loop_started_payload,
        )
        # Load-bearing wall clock: the loop's wall_clock_secs governs the run
        # timeout (overrides config.agent.timeout_minutes for this run).
        timeout_min = max(1, math.ceil(loop.wall_clock_secs / 60))
        # Capture prior/cap as LOCALS — do not close over `loop` (a mid-run
        # _apply_bumps mutation of loop.budget_usd must not move the cap).
        prior_spent = loop.spent_usd
        cap = loop.budget_usd
        budget_check = (
            None if cap is None else (lambda run_cost: (prior_spent + run_cost) > cap)
        )
        return run_tracked_agent(
            launch_state=self._launch_state,
            prompt=prompt,
            # The loop renders the same dispatch template, so it can carry the
            # pre-publish self-check too; the row must say so rather than leave
            # a false absence behind (#2454).
            self_check=rendered_self_check(prompt),
            prompt_profile_version_id=profile_version_id(addendum),
            worktree_dir=worktree,
            config=self._config,
            issue_num=loop.issue_num or 0,
            branch=_loop_branch(loop, self._config),
            store=self._store,
            runner=resolved.runner,
            model=resolved.model,
            run_kind="loop",  # bucket loop spend apart from dispatch (#445)
            run_id=run_id,
            loop_id=loop.loop_id,
            item_id=str(loop.issue_num or 0),
            budget_override_usd=loop.budget_usd,
            timeout_override_minutes=timeout_min,
            should_pause=lambda: self._should_pause(loop.loop_id),
            budget_check=budget_check,
            # This host-created id binds publication and tracking. It does not
            # claim a preserved-work continuation, so epic validation does not apply.
            validate_continuation=False,
            paid_launch_check=paid_launch_check,
            # Attested as the run's initial posture before admission (#1977).
            sandbox_posture=self._admitted_agent_posture(),
        )

    def _record_iterations(
        self, loop: LoopRecord, run_id: str, steps: list[_Step], *, idx_offset: int = 0
    ) -> None:
        for i, step in enumerate(steps):
            idx = idx_offset + i
            # Per-step timestamp from the event's own ts (not a single now()).
            started = (
                datetime.fromtimestamp(step.ts, UTC) if step.ts else datetime.now(UTC)
            )
            self._store.add_iteration(
                IterationRecord(
                    iteration_id=str(uuid.uuid4()),
                    loop_id=loop.loop_id,
                    idx=idx,
                    started_at=started,
                    run_id=run_id,
                    kind="assistant_turn",
                    status="pursuing",
                    cost_usd=step.cost_usd,
                    tokens=step.input_tokens + step.output_tokens,
                    finished_at=started,
                )
            )
            audit.emit(
                self._store,
                "iteration_recorded",
                actor=resolve_actor(),
                issue_num=loop.issue_num,
                repo=loop.repo,
                payload={"loop_id": loop.loop_id, "idx": idx},
            )

    def _decide(
        self,
        loop: LoopRecord,
        worktree: Path,
        result: Any,
        steps: list[_Step],
        *,
        binding: _PublicationBinding,
        publication: CanonicalPublication | CanonicalNoChange | None,
    ) -> tuple[LoopStatus, str]:
        # Fail-closed short-circuits (pause / step-cap / run failure / zero-step)
        # are checked BEFORE the gate; only a clean, real run reaches it.
        pre = _pre_gate_status(loop, result, steps)
        if pre is not None:
            return pre
        try:
            target = self._verification_target(worktree, binding, publication)
        except _VerificationTargetError as exc:
            return "needs_human", str(exc)
        # The gate is non-None here because run() fails closed before launch.
        assert self._gate is not None
        verdict = self._gate.evaluate(
            worktree=worktree,
            base_ref=target.base_ref,
            changed_paths=target.changed_paths,
            agent_written_checkout=target.agent_written_checkout,
            dangerously_skip_containment=(
                self._config.sandbox.dangerously_skip_containment
            ),
            published_sha=target.published_sha,
            publication_base_sha=target.publication_base_sha,
            publication_remote=target.publication_remote,
            extra_guarded_paths=self._acceptance_paths(loop),
        )
        audit.emit(
            self._store,
            "verification_checked",
            actor=resolve_actor(),
            issue_num=loop.issue_num,
            repo=loop.repo,
            payload={
                "loop_id": loop.loop_id,
                "passed": verdict.passed,
                "sabotage": verdict.sabotage,
                "evidence": verdict.evidence,
                **(
                    self._verification_admission.record_fields()
                    if self._verification_admission
                    else {}
                ),
            },
        )
        return _verdict_to_status(verdict)

    def _acceptance_paths(self, loop: LoopRecord) -> tuple[str, ...]:
        """The node's acceptance test files: read-only to its implementer."""
        if loop.issue_num is None:
            return ()
        return tuple(
            contract.path
            for contract in acceptance_contracts(self._store, str(loop.issue_num))
        )

    def _verification_target(
        self,
        worktree: Path,
        binding: _PublicationBinding,
        publication: CanonicalPublication | CanonicalNoChange | None,
    ) -> _VerificationTarget:
        """Select canonical evidence or the admitted checkout compatibility path."""
        if self._config.publication.publish == "canonical":
            return _canonical_verification_target(
                binding,
                publication,
                publication_remote(self._config.project.repo or None),
            )
        return _checkout_verification_target(self._config, worktree)

    def _finalize(
        self,
        loop: LoopRecord,
        status: LoopStatus,
        reason: str,
        steps: list[_Step],
        *,
        prior_step_count: int = 0,
        prior_spent_usd: float = 0.0,
    ) -> LoopRecord:
        # Natural-finish-wins: clear any stale pause flag on every terminal transition
        # (a loop that finishes achieved/needs_human just after a late pause request
        # must not keep pause_requested=1 forever).
        self._store.clear_pause(loop.loop_id)
        # Sync in-memory so save_loop below does not re-persist the stale True.
        loop.pause_requested = False
        loop.status = status
        loop.status_reason = reason
        loop.step_count = prior_step_count + len(steps)
        loop.spent_usd = prior_spent_usd + sum(s.cost_usd for s in steps)
        loop.updated_at = datetime.now(UTC)
        loop.finished_at = datetime.now(UTC)
        self._store.save_loop(loop)
        if status == "needs_human":
            event = "needs_human_raised"
        elif status == "paused":
            event = "loop_paused"
        elif status == "budget_limited":
            event = "loop_budget_exceeded"
        else:
            event = "loop_finished"
        payload: dict[str, object] = {
            "loop_id": loop.loop_id,
            "status": status,
            "reason": reason,
        }
        if status == "budget_limited":
            payload["budget_usd"] = loop.budget_usd
            payload["spent_usd"] = loop.spent_usd
        audit.emit(
            self._store,
            event,
            actor=resolve_actor(),
            issue_num=loop.issue_num,
            repo=loop.repo,
            payload=payload,
        )
        return loop
