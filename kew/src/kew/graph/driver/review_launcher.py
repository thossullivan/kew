"""The concrete production ``ReviewerLauncher`` — the container-backed,
NON-PUSHING reviewer that closes the last wiring seam of the adversarial-review
stage (#977 epic, issue #991).

``dispatch_reviewer`` already materializes a fresh detached clone at the recorded
head SHA and masks the head-ref auto-load surface; every other component of the
stage was built and unit-tested. The one missing piece was a concrete launcher
that actually runs the fresh-context reviewer against that checkout — so in
production ``advance()`` was always called with ``reviewer_launcher=None`` and the
whole pipeline short-circuited to a loud no-op (the exact "built but unwired"
failure #991 exists to close).

This module is that launcher. It is the read-only twin of
``_run_sandboxed_container``: it routes through the IDENTICAL shared
``run_container_and_stream`` primitive (argv-build → launch → streamed telemetry,
with the #1017 bare-runtime invariant enforced in one place, #1023) but swaps the
exit-0 ``git push`` + ``gh pr create`` broker for ``collect_stream_verdict``. A
reviewer is read-only by construction — it never pushes a branch or opens a PR;
the broker is simply never called. Its verdict is the one its own typed result
event carried in the runner stream, as the host captured it (#2021): nothing
the reviewed code can write in the checkout is read as a verdict.

Fail-closed, never a tick-crashing raise: any infrastructure failure (no sandbox
runtime, a launch/stream error) is logged loudly and yields the crash-path
sidecar (no verdict written), which the drive pass routes to HELD (blocking) or
DEGRADED (advisory) — never a silent accept. The launcher swallows its own
container exceptions so a reviewer that cannot run never brings down the driver
tick; ``dispatch_reviewer`` re-raises anything the launcher lets escape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from kew.budget import run_ceiling_usd, window_admission_gate
from kew.command_outcome import PR_HEAD_MOVED
from kew.credential_surface import exception_text
from kew.epic_holds import safe_hold_reason
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
    BaseIntegrator,
)
from kew.graph.driver.overlay import admission_refused_hold_reason
from kew.ids import branch_slug, provider_from_config
from kew.model_eol_snapshot import check_lifecycle
from kew.openshell_launch import is_openshell
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
from kew.review_contract import (
    RATE_LIMITED_CATEGORY,
    STREAM_LAYER,
    CollectedSidecar,
    Finding,
    collect_stream_verdict,
    credential_refusal_sidecar,
    criteria_contract_in_force,
    infrastructure_failure_sidecar,
    parse_verdict_sidecar,
    stored_declared_tests,
)
from kew.review_dispatch import (
    ReviewerLaunchContext,
    ReviewerLauncher,
    reviewer_received_prompt,
)
from kew.review_outcome import ReviewOutcome
from kew.review_stream import (
    STREAM_VERDICT_RUNNERS,
    discard_capture,
    read_capture,
)
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord
from kew.runners.registry import runner_required_credentials
from kew.sandbox import (
    DEFAULT_IMAGE_RUNNER,
    POSTURE_SANDBOXED,
    REFUSAL_IMAGE_UNMAPPED,
    REFUSAL_RUNTIME_UNAVAILABLE,
    RUNTIME_TIMEOUT,
    ContainedImageUnresolvedError,
    ContainerStartFailed,
    SandboxImageUnmapped,
    SandboxRuntimeUnavailable,
    attest_flow_posture,
    detect_runtime_cached,
    emit_resolved_posture,
    remove_prompt_handoff,
    require_certified_boundary,
    resolve_runner_image,
    write_prompt_handoff,
)
from kew.tracking import (
    CONTAINER_NEVER_STARTED_CATEGORIES,
    STREAM_SILENT_CATEGORY,
    DispatchResult,
)

if TYPE_CHECKING:
    from kew.command_outcome import CommandOutcome
    from kew.config import DispatchConfig
    from kew.graph.driver.review_stage import RemediationContext, Remediator
    from kew.run_store import RunStore
    from kew.worktree import ConflictInfo

_logger = logging.getLogger(__name__)

#: The governed resolve the merge-main step runs: ``kew.resolve.dispatch_resolve``
#: unless a caller hands another (keyword arguments ``pr_number``, ``config``,
#: ``store``, ``on_conflict`` and ``expected_head_sha``).
ResolveFn = Callable[..., "CommandOutcome"]


def review_runner(config: DispatchConfig) -> str:
    """The runner the reviewer actually runs as (#1163).

    The ``[review] runner`` pin when set, else the default image runner. ONE
    resolution site: the image, the container args, the forwarded credentials,
    the run row's ``runner_name`` and the stream parser all derive from this
    single value, so they can never describe different agents (the #1104 class).
    """
    return config.review.runner or DEFAULT_IMAGE_RUNNER


def review_model(config: DispatchConfig) -> str:
    """The model the reviewer actually runs on (#2302 review).

    The ``[review] model`` pin when set, else ``[agent] model``, else nothing
    and the runner's own default. ONE resolution site, for the same reason
    :func:`review_runner` is one: the launched model, the run row's ``model``
    and the launchpad's reading of this lane all derive from it, and reading
    only the ``[review]`` pin reported '' for a run that really uses the agent
    model. ``kew.service.dispatch_plan`` mirrors this (it may not import
    ``kew.graph``) and ``tests/test_service_dispatch_plan.py`` holds the two
    together, so the mirror cannot drift into a second answer.
    """
    return config.review.model or config.agent.model or ""


#: The flow name the reviewer's own posture records carry. Dispatch attests
#: ``sandbox_resolved`` for every outcome including a refusal (#1144); the
#: reviewer — the single most exposed launch kew makes, because it executes the
#: reviewed PR's tests beside a reusable credential — attested nothing at all
#: until epic node N12, so its exposure was the one launch invisible on the
#: ledger.
REVIEW_POSTURE_FLOW = "adversarial review"

#: Why an OpenShell reviewer launch was refused when the refusal came from the
#: launch layer rather than the egress gate (which carries its own token). A
#: machine-readable cause beside the sentence, for the reason dispatch records
#: one: ``refused`` alone cannot tell "this build cannot launch that runner
#: here" from "this boundary is unproven".
REFUSAL_OPENSHELL_REVIEW_UNLAUNCHABLE = "openshell-review-unlaunchable"


def _attest_review_refusal(
    store: RunStore,
    ctx: ReviewerLaunchContext,
    config: DispatchConfig,
    *,
    runner: str,
    reason: str,
) -> None:
    """Record a reviewer launch that resolved no container boundary at all.

    Written through ``emit_resolved_posture`` rather than
    :func:`~kew.sandbox.attest_flow_posture`, exactly as dispatch's
    ``_attest_sandbox_refusal`` is: the flow writer warns that the agent "runs
    on this machine with no container boundary", which is false here — nothing
    ran. ``runtime`` is ``None`` for the same reason it is on a refused
    dispatch: no boundary was crossed, so naming one would claim a container
    that does not exist.
    """
    emit_resolved_posture(
        store,
        runner=runner,
        config=config.sandbox,
        posture="refused",
        runtime=None,
        # No image either: a refusal that never resolved one must not name the
        # tag it would have resolved, and a refusal that did has already told
        # the operator which one in its own sentence.
        image=None,
        reason=reason,
        run_id=ctx.run_id,
        checkout_dir=None,
        repo=config.project.repo or None,
        issue_num=int(ctx.item_id) if ctx.item_id.isdigit() else None,
        item_id=ctx.item_id,
    )


def _openshell_review_refusal(
    config: DispatchConfig, *, runner: str, store: RunStore
) -> tuple[str, str] | None:
    """Why this host cannot give the reviewer an OpenShell sandbox, or ``None``.

    Two questions, asked in the order whose answer the operator can act on
    first: can this build launch this runner through the gateway at all, and is
    there a fresh certification for the REVIEWER's boundary — its own policy
    digest, not the dispatch one a `kew sandbox certify` without `--flow review`
    produces (epic node N12).

    Both are asked before any worktree or container access, and a refusal here
    never falls back to the docker route: the whole point of routing the
    reviewer through the gateway is that its credential stays in a provider, and
    a silent docker fallback would hand the real token to the one launch that
    executes the reviewed PR's code.

    The gateway itself is probed as part of the certification match, which reads
    the running gateway's version — so an absent CLI and an unreachable gateway
    arrive here as a refusal rather than as a check that was skipped.
    """
    from kew.openshell_egress import REVIEW_FLOW
    from kew.openshell_launch import OpenShellLaunchRefused, require_launchable_runner

    try:
        require_launchable_runner(runner, flow=REVIEW_FLOW)
    except OpenShellLaunchRefused as exc:
        return exception_text(exc), REFUSAL_OPENSHELL_REVIEW_UNLAUNCHABLE
    try:
        require_certified_boundary(
            config.sandbox, runner=runner, store=store, flow=REVIEW_FLOW
        )
    except SandboxRuntimeUnavailable as exc:
        return exception_text(exc), exc.reason or (
            REFUSAL_OPENSHELL_REVIEW_UNLAUNCHABLE
        )
    return None


def _reviewer_prompt(ctx: ReviewerLaunchContext) -> str:
    """The pinned prompt plus the host-owned verdict channel instructions.

    One rendering, shared with the dispatch's ``effective_prompt_sha`` (#3024),
    so the attested sha is over exactly the text written here.
    """
    return reviewer_received_prompt(ctx.prompt, ctx.head_sha)


def _record_pending(
    store: RunStore,
    ctx: ReviewerLaunchContext,
    config: DispatchConfig,
    log_file: Path,
    model: str,
    runner: str,
) -> None:
    """Write the ``pending`` reviewer run row (keyed to the reservation
    ``run_id``, ``adversarial_review`` kind) so ``_finalize_sandbox_run`` can
    fold the streamed telemetry into it — this is how the reviewer's spend
    accrues to the review pool (``check_review_budget`` sums review-kind rows)
    across rounds (F5).

    The dispatcher PID and its exclusive group commit with this row. A live
    dispatcher protects the interval before its container exists.

    ``container_name`` is the fallback signal the driver's orphan reconciler
    (#1041 ``_reconcile_reservations``) needs (#1059): the sandbox reviewer
    finalizes its ``pending`` row ONLY through an in-process ``finally``, so a
    reviewer host process that dies hard between here and that ``finally`` (a
    SIGKILL from the circuit breaker's process-group kill, a driver restart, a
    crash) never writes a terminal row. Without a recorded liveness signal that
    orphan reads as undeterminable (``default_run_liveness`` -> ``None``) and is
    never reaped, so it pins its review reservation and steals a fleet worker
    slot forever. The reviewer container is named ``kew-<slug>`` (see
    ``sandbox.build_run_argv``); recording it lets the reconciler prove the
    container is gone and reap the row to ``killed`` — regardless of age."""
    from kew.process_groups import exclusive_owner_pid, identity_at_admission

    owner = exclusive_owner_pid()
    record = RunRecord(
        run_id=ctx.run_id,
        pgid=owner,
        owner_pid=owner,
        **identity_at_admission(owner),
        container_runtime=config.sandbox.runtime,
        issue_num=int(ctx.item_id) if ctx.item_id.isdigit() else 0,
        item_id=ctx.item_id,
        branch=f"{config.branches.prefix}{branch_slug(ctx.item_id)}",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        log_file=log_file,
        model=model,
        # The reviewer's row names its runner for the same reason the
        # dispatch row does (#1147): ``kew status`` buckets each run's cost
        # by this column (``_status_cost_by_runner``), so an empty one files
        # the reviewer's spend under "unknown" instead of the agent that
        # actually spent it. Since #1163 that is the RESOLVED review runner,
        # not a hardcoded claude — a kimi reviewer's spend files under kimi.
        runner_name=runner,
        repo=config.project.repo or None,
        container_name=f"kew-{branch_slug(ctx.item_id)}",
        # Read like the rest of this row: callers that launch a reviewer for
        # liveness bookkeeping hand in a context carrying only run and item.
        prompt_profile_version_id=getattr(ctx, "prompt_profile_version_id", None),
    )
    store.admit_run_with_identity(
        record,
        # Synthetic by taxonomy: adversarial_review is not tracker work, so
        # its identity records no item — naming the reviewed node here would
        # assert work this run was never admitted for. The reviewed item
        # already lives in RunRecord.item_id for accounting.
        store.build_admission_identity(
            ctx.run_id,
            tracker=config.tracker.type,
            item_id=None,
            repo=config.project.repo or None,
        ),
        # The daily and weekly caps, in the transaction that inserts this row
        # (#3205): a raise rolls the row back and ``dispatch_reviewer`` holds
        # the node under the cap that refused it.
        on_admit=window_admission_gate(
            store,
            config.budget,
            run_ceiling_usd(config, ADVERSARIAL_REVIEW_RUN_KIND),
            run_id=ctx.run_id,
        ),
    )


def _record_review_failure(
    store: RunStore,
    ctx: ReviewerLaunchContext,
    config: DispatchConfig,
    *,
    model: str,
    runner: str,
    error_category: str,
) -> None:
    """Persist a terminal row when review setup fails before ``pending``.

    The reservation is created before the launcher is called. A preflight
    return that skips ``_record_pending`` therefore still needs a run row, or
    the attempt is indistinguishable from a child that never launched.
    """
    now = datetime.now(UTC)
    record = RunRecord(
        run_id=ctx.run_id,
        issue_num=int(ctx.item_id) if ctx.item_id.isdigit() else 0,
        item_id=ctx.item_id,
        branch=f"{config.branches.prefix}{branch_slug(ctx.item_id)}",
        status="failure",
        exit_code=-1,
        started_at=now,
        finished_at=now,
        error_category=error_category,
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        log_file=Path(config.logging.dir).resolve() / f"review-{ctx.run_id}.log",
        model=model,
        runner_name=runner,
        repo=config.project.repo or None,
    )
    store.admit_run_with_identity(
        record,
        # Synthetic by taxonomy: adversarial_review is not tracker work, so
        # its identity records no item — naming the reviewed node here would
        # assert work this run was never admitted for. The reviewed item
        # already lives in RunRecord.item_id for accounting.
        store.build_admission_identity(
            ctx.run_id,
            tracker=config.tracker.type,
            item_id=None,
            repo=config.project.repo or None,
        ),
    )


class StoreBound[Ctx, Out]:
    """A production seam built over one ``RunStore`` handle (#2110).

    SQLite handles are thread-affine, so a driver that runs several reviews or
    remediations at once gives each worker thread its own handle. ``bind_store``
    rebuilds the same seam over that handle; calling the object runs the seam
    built over the handle it was bound to.
    """

    def __init__(
        self, build: Callable[[RunStore], Callable[[Ctx], Out]], store: RunStore
    ) -> None:
        self._build = build
        self._call = build(store)

    def __call__(self, ctx: Ctx) -> Out:
        return self._call(ctx)

    def bind_store(self, store: RunStore) -> StoreBound[Ctx, Out]:
        return StoreBound(self._build, store)


def sandbox_reviewer_launcher(
    config: DispatchConfig,
    store: RunStore,
) -> StoreBound[ReviewerLaunchContext, CollectedSidecar]:
    """Build the production reviewer launcher (see the module docstring).

    The returned callable satisfies the ``ReviewerLauncher`` protocol
    ``dispatch_reviewer`` expects. It routes the argv-build → launch → stream
    through the shared ``run_container_and_stream`` primitive (#1023) and NEVER
    pushes or opens a PR — the exit-0 broker is replaced by
    ``collect_stream_verdict``.
    """
    return StoreBound(partial(_reviewer_launch, config), store)


def _reviewer_launch(config: DispatchConfig, store: RunStore) -> ReviewerLauncher:
    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        # The reviewer runs as the pinned ``[review] runner`` (#1069), and every
        # runner-shaped decision below derives from this one name.
        runner = review_runner(config)
        model = review_model(config)
        if not runner_required_credentials(runner):
            _logger.error(
                "review: runner %r declares no approved container credential; "
                "reviewer refuses before worktree or container access for %s",
                runner,
                ctx.item_id,
            )
            _record_review_failure(
                store,
                ctx,
                config,
                model=model,
                runner=runner,
                error_category="credential_missing",
            )
            return credential_refusal_sidecar(ctx.log_dir, ctx.run_id, runner)
        if runner not in STREAM_VERDICT_RUNNERS:
            # The verdict channel is the runner's typed terminal result event
            # (#2021). A runner without one has nowhere unforgeable to put its
            # verdict, so it is refused before any worktree or container access.
            _logger.error(
                "review: runner %r has no typed final result kew can bind a "
                "verdict to; reviewer refuses for %s",
                runner,
                ctx.item_id,
            )
            _record_review_failure(
                store,
                ctx,
                config,
                model=model,
                runner=runner,
                error_category="sandbox_refused",
            )
            return infrastructure_failure_sidecar(
                ctx.log_dir,
                ctx.run_id,
                failure_layer="runner",
                error_category="sandbox_refused",
                reason=(
                    f"review runner {runner!r} has no verdict channel; "
                    f"supported: {', '.join(sorted(STREAM_VERDICT_RUNNERS))}"
                ),
            )
        # The OpenShell route is gated BEFORE the runtime probe and before any
        # worktree or container access, and it never falls back to docker
        # (epic node N12). A reviewer on the gateway keeps its credential in a
        # provider and sees only a placeholder; a fallback would hand the real
        # token to the one launch that executes the reviewed PR's code.
        if is_openshell(config.sandbox):
            refused = _openshell_review_refusal(config, runner=runner, store=store)
            if refused is not None:
                reason, token = refused
                _logger.error(
                    "review: the openshell reviewer boundary is refused for %s — %s",
                    ctx.item_id,
                    reason,
                )
                _attest_review_refusal(store, ctx, config, runner=runner, reason=token)
                _record_review_failure(
                    store,
                    ctx,
                    config,
                    model=model,
                    runner=runner,
                    error_category="sandbox_refused",
                )
                return infrastructure_failure_sidecar(
                    ctx.log_dir,
                    ctx.run_id,
                    failure_layer="runtime",
                    error_category="sandbox_refused",
                    reason=reason,
                )
        resolved = detect_runtime_cached(config.sandbox)
        if resolved is None:
            _logger.error(
                "review: sandbox runtime %r unavailable — reviewer cannot run; "
                "verdict is fail-closed (no sidecar written) for %s",
                config.sandbox.runtime,
                ctx.item_id,
            )
            _attest_review_refusal(
                store, ctx, config, runner=runner, reason=REFUSAL_RUNTIME_UNAVAILABLE
            )
            _record_review_failure(
                store,
                ctx,
                config,
                model=model,
                runner=runner,
                error_category="launch_failed",
            )
            return infrastructure_failure_sidecar(
                ctx.log_dir,
                ctx.run_id,
                failure_layer="runtime",
                error_category="launch_failed",
                reason=f"sandbox runtime {config.sandbox.runtime!r} unavailable",
            )
        # Same runner-derived image resolution as dispatch (#1144), through the
        # one shared resolver — the reviewer must never end up in a different
        # image than the runner it claims to be. This IS the fail-closed gate
        # for a pinned non-claude reviewer (#1163): what decides whether it can
        # run contained is whether ``[sandbox.images]`` maps it, not whether its
        # name happens to be claude. #1069's invariant survives intact — an
        # unmapped runner refuses rather than silently reviewing with claude.
        try:
            image = resolve_runner_image(config.sandbox, runner)
            # The reviewer launches by the same resolved immutable ID as
            # dispatch (#1550 review F1 / #1552 re-review F-3): the parity
            # invariant (#1144) is about IDENTITY, and two tag reads can
            # diverge across a retag. Unresolvable refuses fail-closed like
            # every other reviewer-layer failure.
            from kew.dispatch_sandbox import _resolve_launch_image_id

            # Pass the REVIEWER's runner, not the default: the refusal names a
            # build command, and naming the claude Dockerfile for an opencode
            # image tells the operator to build a tag that lies about what is
            # inside it (round-1 review, Major 2).
            image = _resolve_launch_image_id(config, image, runner=runner)
        except SandboxImageUnmapped as exc:
            _logger.error(
                "review: no [sandbox.images] mapping for review.runner %r (%s) "
                "— reviewer cannot run; verdict is fail-closed (no sidecar "
                "written) for %s",
                runner,
                exc,
                ctx.item_id,
            )
            _attest_review_refusal(
                store, ctx, config, runner=runner, reason=REFUSAL_IMAGE_UNMAPPED
            )
            _record_review_failure(
                store,
                ctx,
                config,
                model=model,
                runner=runner,
                error_category="sandbox_refused",
            )
            return infrastructure_failure_sidecar(
                ctx.log_dir,
                ctx.run_id,
                failure_layer="image",
                error_category="sandbox_refused",
                reason=f"no sandbox image mapped for review runner {runner!r}",
            )
        except ContainedImageUnresolvedError as exc:
            # Same fail-closed shape as an unmapped image: the reviewer layer
            # never crashes the dispatch it reviews; the verdict is an
            # infrastructure failure, not a silent tag launch.
            _logger.error("review: %s (for %s)", exc, ctx.item_id)
            _attest_review_refusal(
                store, ctx, config, runner=runner, reason=REFUSAL_IMAGE_UNMAPPED
            )
            _record_review_failure(
                store,
                ctx,
                config,
                model=model,
                runner=runner,
                error_category="sandbox_refused",
            )
            return infrastructure_failure_sidecar(
                ctx.log_dir,
                ctx.run_id,
                failure_layer="image",
                error_category="sandbox_refused",
                reason=str(exc),
            )

        # The boundary this review resolved, on the ledger, before anything is
        # written into the reviewed checkout (epic node N12). Emitted on BOTH
        # routes: ``openshell`` records ``openshell-certified`` because the gate
        # above is the only way here, and docker records the direct route's
        # permanent ``operator-managed-unverified`` — so the gap between the two
        # is at least visible rather than absent.
        attest_flow_posture(
            store,
            flow=REVIEW_POSTURE_FLOW,
            runner=runner,
            config=config.sandbox,
            posture=POSTURE_SANDBOXED,
            image=image,
            run_id=ctx.run_id,
            checkout_dir=ctx.worktree,
            repo=config.project.repo or None,
            issue_num=int(ctx.item_id) if ctx.item_id.isdigit() else None,
            item_id=ctx.item_id,
        )

        # Deliver the pinned prompt into the mounted checkout; the container
        # entrypoint feeds the prompt file to the runner on stdin. The verdict
        # channel instructions are host-appended, so a base-ref prompt copy
        # from before #2021 still asks for the stream block.
        write_prompt_handoff(ctx.worktree, _reviewer_prompt(ctx))

        lifecycle = getattr(config, "model_eol", None)
        entries = getattr(lifecycle, "snapshot_models", {}) if lifecycle else {}
        for warning in check_lifecycle([model], entries):
            _logger.warning("model-eol: %s", warning)
        log_dir = Path(config.logging.dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"review-{ctx.run_id}.log"
        # Only this launch's stream may produce the capture collected below.
        discard_capture(log_file)

        _record_pending(store, ctx, config, log_file, model, runner)
        # Local import: ``dispatch_sandbox`` (via ``dispatch_core``) imports
        # heavily; importing it lazily keeps this module cheap and avoids an
        # import cycle through the driver package.
        from kew.dispatch_sandbox import (
            _emit_sandbox_hard_stop,
            config_with_run_cap,
            run_container_and_stream,
        )

        exit_code = -1
        failure_layer: str | None = None
        issue_num = int(ctx.item_id) if ctx.item_id.isdigit() else 0
        result = DispatchResult(
            exit_code=exit_code,
            issue_num=issue_num,
            log_file=log_file,
            run_id=ctx.run_id,
        )
        try:
            # The shared primitive builds the argv with the BARE runtime name
            # (the #1017 invariant, enforced in one place) and finalizes any
            # launch/stream error under its named category before re-raising.
            # The runner is the same one the image was resolved from above, so
            # the container args (#1145), the injected credentials (#1146) and
            # the stream parser (#1147) always describe the one agent the image
            # is for: an opencode reviewer gets opencode's flags and only
            # ``OPENROUTER_API_KEY``, never the claude token (#1163).
            # The reviewer can execute acceptance only against an installed
            # environment; the image has none. Install the lockfile's
            # dependencies into the checkout first, in the same image and
            # network, without runner credentials. A failed install is
            # reported and the review still runs (degraded acceptance).
            from kew.review_environment import (
                ProvisioningUnconfirmed,
                provision_review_environment,
            )

            try:
                environment_ready = provision_review_environment(
                    config,
                    image=image,
                    runner=runner,
                    checkout_dir=ctx.worktree,
                    slug=branch_slug(ctx.item_id),
                )
            except ProvisioningUnconfirmed as exc:
                # An install container that cannot be proven gone must not
                # share the host with the review: fail closed, no launch.
                _logger.error(
                    "review: %s for %s — reviewer not launched", exc, ctx.item_id
                )
                if exc.timed_out:
                    # A daemon too slow to answer, not a fault: the stage
                    # retries the launch before it holds the node (#3246).
                    result.error_category = RUNTIME_TIMEOUT
                raise
            _logger.info(
                "review: acceptance environment for %s %s",
                ctx.item_id,
                "provisioned" if environment_ready else "not provisioned",
            )
            # The frontend's dependencies, built from the BASE lockfile and
            # mounted read-only (#2559); none when the PR changes the lockfile.
            from kew.review_dispatch import _REVIEW_BASE_REF
            from kew.review_frontend import prepare_frontend_mount

            frontend_mounts = prepare_frontend_mount(
                config,
                image=image,
                runner=runner,
                checkout_dir=ctx.worktree,
                slug=branch_slug(ctx.item_id),
                lock_sha256=ctx.frontend_lock_sha256,
                base_ref=_REVIEW_BASE_REF,
            )
            exit_code = run_container_and_stream(
                # The container argv carries the run cap (#1462); a reviewer's
                # is ``max_review_usd``, never the implementation cap.
                config_with_run_cap(
                    config, run_ceiling_usd(config, ADVERSARIAL_REVIEW_RUN_KIND)
                ),
                store,
                image=image,
                runner=runner,
                checkout_dir=ctx.worktree,
                slug=branch_slug(ctx.item_id),
                run_id=ctx.run_id,
                model=model,
                log_file=log_file,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                result=result,
                read_only_mounts=frontend_mounts,
            )
            _emit_sandbox_hard_stop(
                store,
                config,
                result,
                run_id=ctx.run_id,
                issue_num=issue_num,
                item_id=ctx.item_id,
                model=model,
            )
        except Exception as exc:
            # Fail-closed: never let a container failure crash the driver tick.
            _logger.exception(
                "review: reviewer container failed for %s — fail-closed verdict",
                ctx.item_id,
            )
            # Records only THAT the run raised; which layer (and category) is
            # classified from the launch evidence below.
            failure_layer = "launch"
            if isinstance(exc, ContainerStartFailed):
                # The create or start itself refused: named by its class, so a
                # create the runtime never answered (#3103) is never read as a
                # reviewer that ran.
                result.error_category = exc.category
        finally:
            _finalize_review_run(
                store,
                ctx.run_id,
                exit_code,
                model=model,
                config=config,
                runner=runner,
            )
            # Never let the mounted prompt file survive into the collected diff.
            remove_prompt_handoff(ctx.worktree)
        if (
            result.launched is not True
            and result.error_category in CONTAINER_NEVER_STARTED_CATEGORIES
        ):
            # A start the runtime refused, or a container that refused its
            # agent's startup, returns rather than raises (#3186); it is still a
            # reviewer that never ran — never a failed run's missing verdict.
            failure_layer = _CONTAINER_LAYER
        # Read-only: no push, no PR. The verdict is the one the reviewer's own
        # result event carried, as the host captured it from the stream; no
        # file in the reviewed checkout is read (#2021).
        collected = collect_stream_verdict(
            read_capture(log_file),
            head_sha=ctx.head_sha,
            exit_code=exit_code,
            dest_dir=ctx.log_dir,
            run_id=ctx.run_id,
            plan_criterion_ids=ctx.criterion_ids,
            integrity_flags=ctx.integrity_flags,
            contract_test_ids=ctx.contract_test_ids,
            declared_tests=ctx.declared_tests,
            # Only a reviewer shown the pinned prompt was told the coverage
            # contract (#3419); an older deployed copy reviews as before.
            require_coverage=criteria_contract_in_force(ctx.prompt_artifact_sha),
        )
        if (
            failure_layer is None
            and result.error_category == STREAM_SILENT_CATEGORY
            and collected.error_category != RATE_LIMITED_CATEGORY
        ):
            # The watchdog stopped a reviewer that went quiet (#2240): no
            # verdict was refused, so the stage retries the round. A usage
            # limit the stream already reported outranks the silence: that
            # round waits out the limit and records its credential (#3330).
            collected = replace(
                collected,
                failure_layer=STREAM_LAYER,
                error_category=STREAM_SILENT_CATEGORY,
            )
        if failure_layer is not None:
            stored = store.get(ctx.run_id)
            # Which failure this was is decided by the launch evidence on
            # *result*, never by the fact that an exception reached here: the
            # stage's hold text names one or the other to the operator.
            category, failure_layer = _review_launch_failure(stored, result)
            if stored is not None:
                stored.error_category = category
                store.save(stored)
            collected = replace(
                collected,
                failure_layer=failure_layer,
                error_category=category,
            )
        return collected

    return _launch


def _load_sidecar_findings(
    sidecar_path: str | None, criterion_ids: tuple[str, ...] = ()
) -> list[Finding]:
    """Load the evidenced findings from a round's host-stored sidecar (#1030).

    Returns ``[]`` when the path is unrecorded (pre-v32 round) or the stored file
    is missing/unparseable — the remediator treats that as "nothing to act on"
    and no-ops loudly rather than dispatching an empty remediation.

    *criterion_ids* are the recorded criteria that round was judged against
    (#2291), off its own ``review_rounds`` row. They are what makes each
    finding's ``criterion`` admissible on this second parse: omit them and the
    brief loses the failing criterion ids the round actually established.
    """
    if not sidecar_path:
        return []
    path = Path(sidecar_path)
    if not path.is_file():
        return []
    return parse_verdict_sidecar(
        path.read_bytes(),
        plan_criterion_ids=criterion_ids,
        declared_tests=stored_declared_tests(path),
    ).findings


def _remediation_preflight_failure(
    config: DispatchConfig, *, runner: str, store: RunStore
) -> str | None:
    """Defer containment refusals to review's terminal record before credentials.

    ``store`` is the ledger the OpenShell egress gate reads its certification
    from; resolving without it refused every OpenShell remediation on "no
    ledger is available" (#2389 review).
    """
    from kew.remediation_containment import resolve_remediation_containment
    from kew.runner_preflight import runner_preflight_failure
    from kew.sandbox import SandboxRuntimeUnavailable

    try:
        resolve_remediation_containment(config, runner=runner, store=store)
    except SandboxRuntimeUnavailable as exc:
        # Not a silent default: dispatch_review records the refusal and its
        # classified outcome, and the review stage consumes that outcome to
        # hold the node. Say so here so the deferral is visible in the log.
        _logger.warning(
            "review remediation: containment refusal for runner %r deferred to "
            "review dispatch; its outcome will hold the node (%s)",
            runner,
            exc,
        )
        return None
    return runner_preflight_failure(config, runner_name=runner)


def sandbox_remediator(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
) -> StoreBound[RemediationContext, ReviewOutcome | None]:
    """Build the production remediator — the last unwired seam of the adversarial
    review stage (#1030).

    On a NEEDS_CHANGES verdict the driver parks the node in ``remediating`` and,
    every tick, called this seam; with none wired the node stalled there forever
    (the exact "built but unwired" failure this closes). The returned callable
    dispatches the adapted ``kew review`` remediation path (#987) in governed mode
    — the run carries the node's ``item_id``, uses run_kind ``review_remediation``,
    reserves under the review pseudo-epic, and draws from the review budget pool —
    feeding it the HOST-STORED sidecar findings (never the deletable PR comments,
    F10). Its commit moves the PR head, and the next tick re-reviews at round N+1.

    Fail-closed like ``sandbox_reviewer_launcher``. A runner preflight refusal
    creates an operator hold. The returned outcome lets the driver hold other
    admission refusals even when audit writes fail. Other failures remain retryable.
    """
    return StoreBound(partial(_remediator, config, epic_ref=epic_ref), store)


def _remediator(
    config: DispatchConfig, store: RunStore, *, epic_ref: str
) -> Remediator:
    def _remediate(ctx: RemediationContext) -> ReviewOutcome | None:
        standing = next(
            (
                h.reason
                for h in store.list_epic_holds(epic_ref)
                if h.item_id == ctx.item_id
            ),
            None,
        )
        if standing is not None:
            # Defensive since #2217: the overlay now reads a hold-blocked
            # REMEDIATING node as HELD (``overlay._remediation_wedged``), so
            # the drive pass does not reach this seam for one. Getting here
            # means a hold landed after that status was computed — say so
            # rather than returning the silent no-op that hid #2201/#2202 for
            # two hours.
            _logger.warning(
                "review: %s is remediating but a hold stands on it — no "
                "remediation dispatched: %s",
                ctx.item_id,
                safe_hold_reason(standing),
            )
            return None
        if ctx.pr_number is None:
            _logger.error(
                "review: %s is remediating but has no PR number — cannot dispatch "
                "remediation; node stays remediating",
                ctx.item_id,
            )
            return None
        # Fail-closed for the WHOLE body (#1002): a sidecar read/parse error or a
        # dispatch failure must never crash the driver tick — the node simply
        # stays `remediating` and retries next tick.
        outcomes: list[ReviewOutcome] = []
        try:
            findings = _load_sidecar_findings(ctx.sidecar_path, ctx.criterion_ids)
            if not findings:
                _logger.error(
                    "review: %s is remediating but no evidenced findings could be "
                    "loaded from sidecar %r — node stays remediating",
                    ctx.item_id,
                    ctx.sidecar_path,
                )
                return None
            from kew.runners import resolve_runner

            resolved = resolve_runner(config)
            failure = _remediation_preflight_failure(
                config, runner=resolved.runner.name, store=store
            )
            if failure is not None:
                reason = admission_refused_hold_reason(failure, ctx.item_id)
                if provider_from_config(config) == "github":
                    store.hold_epic_node(epic_ref, ctx.item_id, reason)
                else:
                    store.hold_epic_node_item(epic_ref, ctx.item_id, reason)
                _logger.error(
                    "review: remediation dispatch refused for %s — %s "
                    "(node held for operator)",
                    ctx.item_id,
                    failure,
                )
                return None
            # Local import: kew.review pulls in dispatch_core (heavy) — keep this
            # module cheap and avoid an import cycle through the driver package.
            from kew.review import dispatch_review

            command = dispatch_review(
                pr_number=ctx.pr_number,
                config=config,
                store=store,
                item_id=ctx.item_id,
                findings=findings,
                on_outcome=outcomes.append,
                reservation_held=ctx.reservation_held,
                declaration=ctx.declaration,
                proposal=ctx.proposal,
            )
            if not outcomes and command.exit_code != 0:
                _logger.error(
                    "review: remediation for %s exited %s (%s) without a "
                    "classified outcome",
                    ctx.item_id,
                    command.exit_code,
                    command.kind,
                )
        except Exception:
            _logger.exception(
                "review: remediation dispatch failed for %s — fail-closed "
                "(retained outcome governs admission; otherwise retries next tick)",
                ctx.item_id,
            )
        return outcomes[-1] if outcomes else None

    return _remediate


def sandbox_base_integrator(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    resolve: ResolveFn | None = None,
) -> StoreBound[BaseIntegrationContext, BaseIntegrationOutcome]:
    """Build the production merge-main step for the review stage (#2149).

    The governed ``kew resolve`` path is already the merge-main step this needs:
    it checks the PR head out, integrates the base, and publishes the
    integration under a kew-owned producer — so the new head binds and the next
    remediation is not refused the way it was after an operator's own merge
    commit (#2124). What it could not do is report WHICH paths conflicted when
    resolution failed, which is why it now takes an ``on_conflict`` observer.

    Fail-closed like the other seams here: any raise, and any non-zero exit, is
    an integration that did NOT land, never a branch reported as current.

    *resolve* replaces ``kew.resolve.dispatch_resolve`` as the resolve the step
    runs; None is the governed path.
    """
    return StoreBound(
        partial(_base_integrator, config, epic_ref=epic_ref, resolve=resolve), store
    )


def _base_integrator(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    resolve: ResolveFn | None = None,
) -> BaseIntegrator:
    def _integrate(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        if resolve is None:
            # Local import: kew.resolve pulls in dispatch_core (heavy) — keep
            # this module cheap and avoid an import cycle through the driver.
            from kew.resolve import dispatch_resolve as governed
        else:
            governed = resolve

        conflicts: list[ConflictInfo] = []
        command = governed(
            pr_number=ctx.pr_number,
            config=config,
            store=store,
            on_conflict=conflicts.append,
            # Pinned to the head the stage checked for an operator (#3200).
            expected_head_sha=ctx.head_sha,
        )
        if command.exit_code == 0:
            return BaseIntegrationOutcome("integrated")
        if command.category == PR_HEAD_MOVED:
            return BaseIntegrationOutcome("head_moved", detail=command.reason)
        if command.category == PROMPT_CONFIGURATION_REFUSAL:
            # The resolve profile refused before any runner (#3023): the
            # conflicts it was called for are not why the step did not land.
            return BaseIntegrationOutcome(
                "failed", detail=f"{PROMPT_CONFIGURATION_REFUSAL}: {command.reason}"
            )
        paths = tuple(conflicts[-1].conflicted_files) if conflicts else ()
        detail = f"kew resolve {command.kind}, exit {command.exit_code}"
        if command.reason:
            # Why it did not land — a resolution check names itself (#3200).
            detail = f"{detail}: {command.reason}"
        if paths:
            return BaseIntegrationOutcome(
                "conflicted", detail=detail, conflicted_paths=paths
            )
        return BaseIntegrationOutcome("failed", detail=detail)

    return _integrate


#: The category for a reviewer container that DID start — or whose start was
#: never acknowledged — and then died. Deliberately NOT ``launch_failed``: the
#: review stage holds that category under "the reviewer never started" and
#: publishes the sentence to the PR (#2043), which a mid-stream death would make
#: a falsehood. Unmapped there, so such a round keeps the crash path's
#: ``review_unparseable`` — the honest reading when a reviewer ran and left no
#: admissible verdict.
_REVIEW_STREAM_FAILED = "review_stream_failed"


#: The layer of a reviewer whose container kew could not create or start
#: (#2973). Its categories (``tracking.CONTAINER_NEVER_STARTED_CATEGORIES``:
#: a refused create or start, a create the runtime never answered, a refused
#: agent startup) are set only where the container provably never ran its
#: reviewer, and the hold names the container to look at.
_CONTAINER_LAYER = "container"


def _review_launch_failure(
    stored: RunRecord | None, result: DispatchResult
) -> tuple[str, str]:
    """The ``(error_category, failure_layer)`` a reviewer run that raised earns.

    ``launch_failed`` is load-bearing prose downstream, so it is reserved for a
    failure the host's OWN launch evidence proves landed before the container
    ran: ``result.launched is False`` — the fresh default, kept through every
    pre-stream failure (argv build, the launch itself, an unconfirmed
    provisioning container), and set when exec failed before the agent's first
    event. Observed workload bytes, or a startup acknowledgement that never
    arrived, both leave that claim unproven: the row's own terminal category
    stands (``sandbox_stream_error`` / ``sandbox_startup_unknown``) under the
    ``stream`` layer, and the crash path asserts nothing about a launch it
    cannot see.
    """
    if stored is not None and stored.status == "refused":
        return stored.error_category, "launch"
    recorded = stored.error_category if stored is not None else ""
    for category in (recorded, result.error_category):
        if (
            category in CONTAINER_NEVER_STARTED_CATEGORIES
            and result.launched is not True
        ):
            # The shared sandbox path already named the container that never
            # started; the generic fallback below would erase that (#2973).
            return category, _CONTAINER_LAYER
    if result.launched is False:
        return "launch_failed", "launch"
    return recorded or _REVIEW_STREAM_FAILED, "stream"


def _finalize_review_run(
    store: RunStore,
    run_id: str,
    exit_code: int,
    *,
    model: str,
    config: DispatchConfig,
    runner: str,
) -> None:
    """Terminal-update the reviewer run row with parsed cost/token telemetry
    (reuses the sandbox finalizer so the review pool accounts spend the same way
    the implementation pool does).

    *model*/*config* carry the reviewer's pricing context so its planned bound
    (#1092) is priced against the same authoritative token totals — reviewer
    rows belong in the planning-accuracy pair like any other run.

    *runner* is the resolved review runner — the same one ``_launch`` resolved
    the image from and ran the container as — so the parser reading the
    reviewer's stream is the agent that produced it (#1147). A cost-blind
    reviewer would silently uncap ``max_review_usd``, which sums these rows, so
    this must move with the ``[review] runner`` pin (#1163)."""
    from kew.dispatch_core import _finalize_sandbox_run
    from kew.pricing import build_pricing_overrides

    stored = store.get(run_id)
    if stored is not None and stored.status == "refused":
        return
    _finalize_sandbox_run(
        store,
        run_id,
        exit_code=exit_code,
        model=model,
        overrides=build_pricing_overrides(config.pricing.overrides),
        runner=runner,
    )
