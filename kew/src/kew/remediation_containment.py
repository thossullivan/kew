"""The containment DECISION for the remediation flows (W1.1 / D2, node 3d).

Review remediation and conflict resolution run an agent against an EXISTING PR
branch and then publish to it from the host, bound to the head the work was cut
from (#1290). This module answers one question for them — can this run be
contained, and if not, precisely why — and nothing else.

Execution is deliberately NOT here. It belongs to
:func:`kew.orchestrator.run_agent`, which swaps its execution step for a
container while keeping retry, budget, breaker, pause and cost accounting
shared with the host path. This module once carried its own launcher too; that
was written before measuring what a parallel contained lifecycle costs, which
turned out to be the per-run cost governor (#1462). One execution path, one
lifecycle, one place a run can be stopped.

Required containment fails closed unless ``allow_uncontained_flows = true``.
Auto mode retains host fallback and records the prerequisite gap.

Sidecar routes use the dispatch credential gate.
Direct routes require host credentials when containment is available.
Other remediation resolution preserves the historical host fallback.

``store`` is the ledger the OpenShell egress gate reads its certification
from, and it is REQUIRED rather than defaulted: resolving without it refused
every OpenShell remediation on "no ledger is available" while the operator's
fresh proof sat on that exact store, and the auto sub-path — which does not
delegate to :func:`~kew.sandbox.resolve_effective_sandbox` — returned a
sandboxed posture with the gate never consulted at all (#2389 review).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kew.inference_transport import direct_route
from kew.openshell_launch import is_openshell
from kew.sandbox import (
    CONTAINED_POSTURES,
    POSTURE_SANDBOXED,
    SandboxImageUnmapped,
    SandboxRuntimeUnavailable,
    detect_runtime_cached,
    emit_resolved_posture,
    require_certified_boundary,
    resolve_effective_sandbox,
    resolve_runner_image,
    resolve_transport_sandbox,
    runner_credential_env_vars,
    runner_native_containment,
    runner_uncontainable,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.run_store import RunStore

__all__ = [
    "RemediationContainment",
    "resolve_remediation_containment",
]


@dataclass(frozen=True)
class RemediationContainment:
    """Whether one remediation run can be contained, and why not when it cannot.

    ``image`` is set only for ``posture="sandboxed"``. ``reason`` is set only
    when containment did NOT happen, and it names the specific obstacle rather
    than the category: an operator reading the ledger needs to tell "this host
    has no runtime" from "kew has no image for this runner", because one is
    theirs to fix and the other is not.
    """

    posture: str
    image: str | None = None
    reason: str | None = None

    @property
    def contained(self) -> bool:
        """True when the agent will not run directly on the operator's machine."""
        return self.posture in CONTAINED_POSTURES


def resolve_remediation_containment(
    config: DispatchConfig,
    *,
    runner: str,
    store: object | None,
    inference_transport: ResolvedInferenceTransport | None = None,
) -> RemediationContainment:
    """Decide containment using the selected route's worker credentials.

    Two arms. A selected sidecar route and required containment both resolve
    through the dispatch admission, which RAISES the specific refusal; anything
    else falls through to :func:`_resolve_permitted_host_fallback`, which
    answers with a posture and a reason instead.

    ``store`` is keyword-only and has no default on purpose — see the module
    docstring. Pass ``None`` only where there genuinely is no ledger.
    """
    if inference_transport is not None and inference_transport.sidecar:
        decision = resolve_transport_sandbox(
            config.sandbox,
            runner=runner,
            inference_transport=inference_transport,
            gateway_launch=False,
            store=store,
        )
        return RemediationContainment(posture=decision.posture, image=decision.image)

    if config.sandbox.enabled is True and not config.sandbox.uncontained_flows_allowed:
        # ``gateway_launch=False`` is what carries this flow's shape into the
        # resolver, and it decides the runner-posture question there too: a
        # runner that defers to kew's boundary on the configured runtime
        # (``codex`` under ``openshell``) is asked the NO-RUNTIME question when
        # that boundary has no launch path for a container argv, so it keeps
        # its own posture instead of being refused for a boundary neither side
        # was ever going to hold (:func:`kew.sandbox._offered_runtime`,
        # PR #2461 review round 2).
        decision = resolve_effective_sandbox(
            config.sandbox,
            runner=runner,
            store=store,
            direct=direct_route(inference_transport),
            gateway_launch=False,
        )
        return RemediationContainment(posture=decision.posture, image=decision.image)

    return _resolve_permitted_host_fallback(config, runner, store)


def _resolve_permitted_host_fallback(
    config: DispatchConfig, runner: str, store: object | None
) -> RemediationContainment:
    """Contain this run if every prerequisite is there, else name the one missing.

    The arm host execution is permitted on — ``enabled = "auto"``, or
    ``allow_uncontained_flows = true``. Ordered so that the most specific answer
    wins: a runner that contains itself is contained whatever the container
    settings say, and a runner that can never be contained is not a
    missing-image problem.
    """
    native = runner_native_containment(runner)
    if native is not None:
        return RemediationContainment(posture=native)

    uncontainable = runner_uncontainable(runner)
    if uncontainable is not None:
        return RemediationContainment(posture="uncontained_flow", reason=uncontainable)

    if config.sandbox.enabled is False:
        return RemediationContainment(
            posture="uncontained_flow", reason="[sandbox] enabled = false"
        )

    # Asked before the runtime is probed, and for the same reason the strict
    # branch above passes ``gateway_launch=False``: a gateway runtime can be
    # installed and reachable and still not be a boundary these flows can
    # launch into, because their execution step is a container argv
    # (:func:`kew.dispatch_core._contained_execution_for`) and the gateway has
    # no argv. Auto mode answers that the way it answers every other
    # prerequisite gap — host fallback with the gap recorded — rather than
    # resolving ``sandboxed`` and failing at the launch.
    if is_openshell(config.sandbox):
        # The egress gate is still asked first, as on every other path
        # (#2319): it is the OpenShell gap a shipped command clears, and it
        # must not be hidden behind this build limitation. Either way the run
        # falls back to the host with the gap recorded — nothing is claimed.
        try:
            require_certified_boundary(config.sandbox, runner=runner, store=store)
        except SandboxRuntimeUnavailable as exc:
            return RemediationContainment(posture="uncontained_flow", reason=str(exc))
        return RemediationContainment(
            posture="uncontained_flow",
            reason=(
                f"sandbox runtime {config.sandbox.runtime!r} has no container "
                "argv for remediation flows to launch"
            ),
        )

    if detect_runtime_cached(config.sandbox) is None:
        return RemediationContainment(
            posture="uncontained_flow",
            reason=f"sandbox runtime {config.sandbox.runtime!r} not found",
        )

    try:
        image = resolve_runner_image(config.sandbox, runner)
    except SandboxImageUnmapped:
        return RemediationContainment(
            posture="uncontained_flow",
            reason=f"no [sandbox.images] mapping for runner {runner!r}",
        )

    return _resolve_remediation_credential(config, runner, image, store)


def _resolve_remediation_credential(
    config: DispatchConfig, runner: str, image: str, store: object | None
) -> RemediationContainment:
    required = runner_credential_env_vars(runner)
    if not required:
        return RemediationContainment(
            posture="uncontained_flow",
            reason=f"runner {runner!r} declares no in-container credential",
        )
    if not _any_env_set(required):
        # A missing direct credential cannot inherit the sidecar worker token.
        resolve_transport_sandbox(
            config.sandbox, runner=runner, store=store, gateway_launch=False
        )
        return RemediationContainment(
            posture="uncontained_flow",
            reason=(
                f"no {runner} credential in the environment "
                "(containers cannot inherit CLI login)"
            ),
        )

    return _certified_remediation_posture(config, runner, image, store)


def _certified_remediation_posture(
    config: DispatchConfig, runner: str, image: str, store: object | None
) -> RemediationContainment:
    """Contain only a boundary kew has seen hold, else record the gap.

    Last of the prerequisites, so the obstacle named is the one the operator
    can act on: a host with no runtime has nothing to certify in the first
    place. Auto mode keeps its host fallback here as it does for every other
    gap — the point of the gate is that kew must not CLAIM a boundary it
    cannot prove, and a recorded fallback claims nothing.
    """
    try:
        require_certified_boundary(config.sandbox, runner=runner, store=store)
    except SandboxRuntimeUnavailable as exc:
        return RemediationContainment(posture="uncontained_flow", reason=str(exc))
    return RemediationContainment(posture=POSTURE_SANDBOXED, image=image)


def _any_env_set(names: tuple[str, ...]) -> bool:
    import os

    return any(os.environ.get(name) for name in names)


def record_remediation_refusal(
    store: RunStore | None,
    config: DispatchConfig,
    exc: SandboxRuntimeUnavailable,
    *,
    runner: str,
    run_id: str,
    run_kind: str,
    issue_num: int,
    branch: str,
    item_id: str | None = None,
) -> None:
    """Save the refusal and both audit events without inspecting a checkout.

    Both audit writes require confirmation. Any failure raises to the caller,
    which reports it and finalizes the refusal. Partial recording is possible.
    """
    from kew.dispatch_core import _emit_early_exit
    from kew.run_store import RunRecord

    if store is not None:
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=issue_num,
                # Preserve an explicit no-item marker. None keeps the legacy
                # issue identity used by conflict resolution.
                item_id=item_id if item_id is not None else str(issue_num),
                branch=branch,
                runner_name=runner,
                run_kind=run_kind,
                status="refused",
                exit_code=2,
                started_at=now,
                finished_at=now,
                error_category="containment_refused",
                repo=config.project.repo or None,
            )
        )
    emit_resolved_posture(
        store,
        runner=runner,
        config=config.sandbox,
        posture="refused",
        runtime=exc.runtime,
        image=exc.image,
        reason=exc.reason,
        run_id=run_id,
        checkout_dir=None,
        repo=config.project.repo or None,
        issue_num=issue_num,
        item_id=item_id,
        strict=True,
    )
    _emit_early_exit(
        store,
        issue_num=issue_num,
        item_id=item_id,
        complexity="medium",
        runner=runner,
        model="",
        exit_code=2,
        duration_seconds=0.0,
        error_category="containment_refused",
        repo=config.project.repo or None,
        run_id=run_id,
        reason=exc.reason,
        strict=True,
    )
