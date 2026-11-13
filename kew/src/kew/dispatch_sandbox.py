"""Sandboxed dispatch execution, extracted from ``dispatch_core`` (Task 5).

This module owns the container-execution sub-surface of the dispatch core: the
``pending``-row write (``_record_sandbox_start``), the telemetry folders
(``_telemetry_from_evidence`` and friends), the live-tailing loop
(``_stream_sandbox_log``), the terminal row update (``_finalize_sandbox_run``),
and the two-layer launcher (``_run_sandboxed`` / ``_run_sandboxed_container``).

Every telemetry path here reads the container's stdout through the RESOLVED
runner's ``parse_line`` — the same parser the host path uses (#1147) — so kew
keeps one interpretation of a stream rather than a claude-only second one.
Telemetry parsing suppresses adapter display because raw events can contain credentials.
The terminal update reads the derived sidecar without replaying a transcript (I06T).

It is a pure seam extraction: the sandbox lifecycle and its exit-path finalization
ordering (#725/#761/#SFD-152/#SFD-155) are UNCHANGED, and the seams the review
launcher (#992) reaches — ``_stream_sandbox_log`` and ``_finalize_sandbox_run`` —
keep their signatures and their importable-from-``dispatch_core`` names via the
re-export in ``dispatch_core``.

Container entrypoint contract
----------------------------
``docs/sandbox-entrypoint-contract.md`` is the CANONICAL text for the interface
this module drives: mounted checkout, prompt-file→stdin handoff, ``"$@"``
extra-args slot, one JSON event per line on stdout, and the reviewer verdict
sidecar. Change it there first — the launcher and the stream-json parsers below
implement that page, they do not re-specify it.

Collaborators that live in (or are imported into) ``kew.dispatch_core`` — and in
particular the ones the dispatch/sandbox suites ``mock.patch`` at
``kew.dispatch_core.*`` (``_launch_container``, ``_stream_sandbox_log``,
``_push_and_open_pr``, ``_post_agent_report``, ``_detect_sandbox_runtime``) — are
reached as attributes of the ``kew.dispatch_core`` module at call time. That
mirrors the Wave 1 precedent (``run_store_reservations``): it both preserves the
patch targets and breaks the import cycle (``dispatch_core`` imports these names
back at load time for the re-export).
The dispatch core module is deliberately imported only at call time below. It
re-exports this module's seams, so importing it while this module is still
initializing makes a cold ``dispatch_sandbox`` import order-dependent.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew import __version__, publication_canonical
from kew.audit import (
    Actor,
    SidecarPacketEvidenceUnavailable,
    append_sidecar_evidence,
    emit,
    matching_sidecar_packet_evidence,
)
from kew.breaker import MAX_DISCOUNTED_COMPACTIONS, RunBaseline, baseline_payload
from kew.cancellation import DeferredSIGINT
from kew.claim_retention import require_claim_recovery
from kew.credential_handoff import require_credential_route
from kew.credential_issuer import BoundedRunKey
from kew.dispatch_admission import PendingAdmission, execution_cancellation
from kew.evidence import (
    TRUSTED_BASE_FACT,
    EvidenceError,
    EvidenceKind,
    Producer,
    ProducerKind,
    import_runner_evidence,
    record_evidence,
)
from kew.exit_signal import (
    DIAGNOSTIC_TAIL_LINES,
    DiagnosticTail,
    name_signal_exit,
)
from kew.ids import branch_slug
from kew.inference_transport import resolve_inference_transport
from kew.model_wait import model_wait_for
from kew.openshell_broker import broker_auth_path, launch_provider
from kew.orchestrator import (
    _requested_service_tier,
    _should_trip_budget,
    sidecar_switchyard_context,
)
from kew.pricing import build_pricing_overrides, infer_provider
from kew.progress import _warn, display_suppressed
from kew.provenance import orchestrator_provenance
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    require_publication_launch_credentials,
)
from kew.publication_shadow_hook import run_shadow_comparison
from kew.review_stream import ReviewResultCapture
from kew.run_credentials import (
    bounded_run_credentials,
    credential_boundary,
    credential_identity,
    launch_environment,
    require_credential_lifetime,
)
from kew.run_store_artifacts import ArtifactStoreError
from kew.runner_fields import exception_text, service_tier_token
from kew.runner_quota import QUOTA_EXHAUSTED_CATEGORY, is_quota_exhausted
from kew.runners import AgentRunner, StreamEvent, get_runner
from kew.runners.protocol import RUN_TOTALS_KEY, RunTotals
from kew.runners.registry import record_runner_launch, record_runner_token_warning
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.sandbox import (
    CONTAINER_START_FAILED,
    KEW_LEDGER_LABEL,
    KEW_RUN_LABEL,
    RUNTIME_CONTROL_TIMEOUT_SECONDS,
    WALL_CLOCK_LIMIT_SECONDS,
    ContainedExecution,
    ContainerCreateTimedOut,
    ContainerStartFailed,
    ContainerStream,
    KillStatus,
    LaunchObservedStream,
    SandboxImageUnmapped,
    WorkerNetnsFilterError,
    build_container_cleanup_argv,
    build_run_argv,
    build_worker_agent_exec_argv,
    build_worker_netns_filter_helper_argv,
    build_worker_netns_filter_install_argv,
    build_worker_netns_filter_readback_argv,
    capture_container_launch,
    capture_container_stream_failure,
    container_absent,
    detect_runtime_cached,
    finalize_container_hard_stop,
    is_startup_ack,
    never_started_container,
    proxy_redaction_scope,
    read_never_started_container,
    redact_container_observations,
    redact_credentials,
    remove_never_started_container,
    remove_prompt_handoff,
    resource_caps,
    runtime_daemon_identity,
    settle_never_started_start,
    validate_worker_netns_filter_readback,
    worker_netns_filter_ruleset,
    worker_netns_filter_table,
    write_prompt_handoff,
)
from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    RetainedPrompt,
    SensitivePromptError,
    default_root,
    retain_prompt,
)
from kew.stop_recovery import container_daemon_key
from kew.subprocess_utils import run as _run
from kew.subprocess_utils import run_bytes as _run_bytes
from kew.switchyard_supervisor import SwitchyardSupervisorError
from kew.tracking import (
    STREAM_SILENT_CATEGORY,
    DispatchResult,
    partial_dispatch_result,
    preserve_dispatch_result,
    startup_refused,
)
from kew.transcript_metrics import (
    RunnerTotals,
    StreamDerivation,
    first_edit_metrics_for,
    read_derived_evidence,
    sanitized_log_banner,
    write_container_evidence,
    write_derived_evidence,
)
from kew.usage import event_contributes_usage
from kew.worktree import has_uncommitted_changes

# Sandbox error category strings — used in _finalize_sandbox_run calls
# so the run history carries a named classification, not just an exit code.
_SANDBOX_NO_CHANGE_CATEGORY = "sandbox_no_change_needed"
#: A contained run whose host publication failed after a successful exit.
SANDBOX_PUBLICATION_FAILED_CATEGORY = "sandbox_push_failed"

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.dispatch_placement import HeldPlacement
    from kew.fleet_placement import Placement
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.openshell_checkout import CheckoutRealization
    from kew.readiness import RealizedRouteProfile, SidecarPacketEvidence
    from kew.run_store import EpicReservation, RunRecord, RunStatus, RunStore
    from kew.sandbox import ContainerHandle

_logger = logging.getLogger(__name__)

_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WorkerNetnsFilterEvidence:
    """Verified worker filter identity after its helper was removed."""

    worker_id: str
    helper_id: str
    image_id: str
    table: str
    readback: str


def _require_filter_command(
    result: subprocess.CompletedProcess[str], purpose: str
) -> subprocess.CompletedProcess[str]:
    if result.returncode == 0:
        return result
    detail = redact_credentials(
        result.stderr or result.stdout or "no diagnostic"
    ).strip()[-500:]
    raise WorkerNetnsFilterError(f"{purpose} failed: {detail}")


def _require_filter_bytes_command(
    result: subprocess.CompletedProcess[bytes], purpose: str
) -> subprocess.CompletedProcess[bytes]:
    if result.returncode == 0:
        return result
    raw = result.stderr or result.stdout or b"no diagnostic"
    detail = redact_credentials(raw.decode("utf-8", errors="replace")).strip()[-500:]
    raise WorkerNetnsFilterError(f"{purpose} failed: {detail}")


def _remove_filter_helper(runtime: str, helper_id: str) -> None:
    removed = _run(
        build_container_cleanup_argv(runtime=runtime, container_ids=(helper_id,)),
        check=False,
        timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS,
    )
    _require_filter_command(removed, "worker filter helper cleanup")
    inventory = _run(
        [runtime, "container", "ls", "--all", "--quiet", "--no-trunc"],
        check=False,
        timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS,
    )
    _require_filter_command(inventory, "worker filter helper cleanup readback")
    identities = inventory.stdout.splitlines()
    if any(_FULL_CONTAINER_ID.fullmatch(identity) is None for identity in identities):
        raise WorkerNetnsFilterError("worker filter helper inventory was malformed")
    if helper_id in identities:
        raise WorkerNetnsFilterError("worker filter helper retained its exact ID")


def _create_filter_helper(
    runtime: str,
    image_id: str,
    worker_id: str,
    owner: str,
    proxy_environment: str,
) -> str:
    argv = build_worker_netns_filter_helper_argv(
        runtime=runtime,
        image_id=image_id,
        worker_id=worker_id,
        owner=owner,
        proxy_environment=proxy_environment,
    )
    execution = ContainedExecution.from_argv(argv)
    handle = _CreatedContainerHandle(runtime, execution.name)
    with _own_container_launch(handle):
        created = _run(
            argv, check=False, timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS
        )
        _require_filter_command(created, "worker filter helper creation")
        helper_id = created.stdout.strip()
        if _FULL_CONTAINER_ID.fullmatch(helper_id) is None:
            raise WorkerNetnsFilterError("worker filter helper returned no full ID")
        return helper_id


def install_worker_netns_filter(
    *,
    runtime: str,
    image_id: str,
    worker_id: str,
    owner: str,
    proxy_address: str,
    proxy_port: int,
    on_helper_created: Callable[[str], None] | None = None,
    on_helper_removed: Callable[[str], None] | None = None,
    proxy_environment: str = "none",
) -> WorkerNetnsFilterEvidence:
    """Install, read back, and remove the helper for one worker-only filter."""
    helper_id = _create_filter_helper(
        runtime, image_id, worker_id, owner, proxy_environment
    )

    readback = ""
    with _own_filter_helper(runtime, helper_id, on_helper_removed):
        if on_helper_created is not None:
            on_helper_created(helper_id)
        started = _run(
            [runtime, "start", helper_id],
            check=False,
            timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS,
        )
        _require_filter_command(started, "worker filter helper startup")
        installed = _run_bytes(
            build_worker_netns_filter_install_argv(
                runtime=runtime,
                helper_id=helper_id,
            ),
            stdin=worker_netns_filter_ruleset(
                owner=owner,
                proxy_address=proxy_address,
                proxy_port=proxy_port,
            ),
            timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS,
        )
        _require_filter_bytes_command(installed, "worker filter installation")
        observed = _run(
            build_worker_netns_filter_readback_argv(
                runtime=runtime,
                helper_id=helper_id,
                owner=owner,
            ),
            check=False,
            timeout=_WORKER_FILTER_COMMAND_TIMEOUT_SECONDS,
        )
        _require_filter_command(observed, "worker filter readback")
        readback = observed.stdout
        validate_worker_netns_filter_readback(
            readback,
            owner=owner,
            proxy_address=proxy_address,
            proxy_port=proxy_port,
        )

    return WorkerNetnsFilterEvidence(
        worker_id=worker_id,
        helper_id=helper_id,
        image_id=image_id,
        table=worker_netns_filter_table(owner),
        readback=readback,
    )


def _finish_filter_helper(
    runtime: str, helper_id: str, on_removed: Callable[[str], None] | None
) -> None:
    _remove_filter_helper(runtime, helper_id)
    if on_removed is not None:
        on_removed(helper_id)


@contextmanager
def _own_filter_helper(
    runtime: str, helper_id: str, on_removed: Callable[[str], None] | None
) -> Iterator[None]:
    handle = _CreatedContainerHandle(runtime, helper_id, helper_id)
    try:
        yield
    except BaseException as exc:
        _stop_failed_container(handle, exc)
        evidence = container_launch_failure(exc)
        assert evidence is not None
        if evidence.cleanup_confirmed:
            try:
                _finish_filter_helper(runtime, helper_id, on_removed)
            except BaseException:
                exc.container_launch_failure = replace(  # type: ignore[attr-defined]
                    evidence, cleanup_confirmed=False
                )
                _logger.debug("Filter helper cleanup remains unconfirmed during unwind")
        raise
    else:
        try:
            _finish_filter_helper(runtime, helper_id, on_removed)
        except BaseException as exc:
            _stop_failed_container(handle, exc)
            raise


def start_agent_after_worker_netns_filter(
    *,
    runtime: str,
    image_id: str,
    worker_id: str,
    owner: str,
    proxy_address: str,
    proxy_port: int,
    start_agent: Callable[[], None],
) -> WorkerNetnsFilterEvidence:
    """Run agent startup only after verified filtering and helper removal."""
    handle = _CreatedContainerHandle(runtime, worker_id, worker_id)
    with _own_container_launch(handle):
        evidence = install_worker_netns_filter(
            runtime=runtime,
            image_id=image_id,
            worker_id=worker_id,
            owner=owner,
            proxy_address=proxy_address,
            proxy_port=proxy_port,
        )
        start_agent()
        return evidence


def _sandbox_route_profile(
    run_id: str,
    *,
    runner_name: str,
    model: str,
    tracker: str,
    image: str | None,
    image_id: str | None,
    transport: ResolvedInferenceTransport | None = None,
    service_tier: str | None = None,
    credential: BoundedRunKey | None = None,
    broker_provider: str | None = None,
) -> RealizedRouteProfile | None:
    """The realized profile of a contained attempt, or None when not contained.

    Records the same owned facts the host path records — source tracker,
    inference provider, requested model, containment — differing only in the
    identity a container can own: the image ID the runtime resolved the tag to.
    An unresolvable image REFUSES the dispatch upstream (the caller resolves
    the ID before any row is written), so by the time this composes, the ID is
    real — never the tag dressed up as immutable evidence.
    """
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )
    from kew.runners.capabilities import capability_manifest

    if image is None:
        return None
    try:
        capability_manifest(runner_name)
    except ValueError:
        return None
    # The caller resolved the immutable image ID (and refused the dispatch if
    # it could not) and resolved the transport BEFORE this capture — so the
    # profile records the endpoint the money actually goes through, not None
    # dressed up as evidence (#1550 review F1/F3).
    boundary = RealizedLaunchBoundary(
        containment=Containment.CONTAINED,
        source_provider=tracker or None,
        inference_provider=infer_provider(model, runner_name) or None,
        image_ref=image,
        image_id=image_id,
        endpoint=None if transport is None else (transport.endpoint or None),
        service_tier=service_tier or None,
        auth_path=_attempt_auth_path(runner_name, transport, broker_provider),
    )
    return compose_realized_profile(
        run_id=run_id,
        attempt_number=1,
        route=compose_route_profile(
            runner=runner_name, requested_model=model, transport=transport
        ),
        # The broker wins over the issued key: on the OpenShell runtime the
        # minted secret is handed to a gateway provider, never to the child, so
        # `issued:openrouter:<hash>` would name a path this launch did not take.
        boundary=(
            boundary if broker_provider else credential_boundary(boundary, credential)
        ),
        credential_issuer=credential_identity(credential),
    )


def _record_placed_profile(
    image_id: str,
    *,
    store: RunStore,
    run_id: str,
    runner_name: str,
    model: str,
    tracker: str,
    image: str,
    transport: ResolvedInferenceTransport | None,
    service_tier: str | None,
    credential: BoundedRunKey | None,
    broker_provider: str | None,
) -> None:
    """Attest a PLACED attempt's boundary with the instance's own image ID.

    Called by the placement once the instance holds the image and before its
    container is launched, which is the remote equivalent of the moment the
    local path resolves its ID: the same "immediately before one paid child
    launch" the profile is defined by. *image_id* is what THAT machine's image
    store resolved the tag to, because that is the only answer describing the
    bytes the agent runs — this machine's ID for the same tag names bytes no
    container on the instance ever reads, and the tag they share names build
    inputs rather than content (#2526 review).

    The publication-credential gate runs here too, for the same reason it runs
    in :func:`_record_ready_sidecar_profile`: it reads the attempt's profile,
    so a deferred profile defers the gate with it — and it runs BEFORE the
    container, so a launch whose output could not be published never happens.
    A refusal from either step propagates out of the placement and unwinds the
    dispatch, exactly as the sidecar's deferred gate does from its own launch;
    the admitted row is terminalized by ``PendingAdmission`` on the way out.
    """
    profile = _sandbox_route_profile(
        run_id,
        runner_name=runner_name,
        model=model,
        tracker=tracker,
        image=image,
        image_id=image_id,
        transport=transport,
        service_tier=service_tier,
        credential=credential,
        broker_provider=broker_provider,
    )
    if profile is None:
        raise RuntimeError("the placed attempt's route profile is unavailable")
    identity = store.get_admission_identity(run_id)
    if identity is not None and hasattr(store, "record_attempt_admission"):
        store.record_attempt_admission(profile, identity)
    else:
        store.record_route_profile(profile)
    require_publication_launch_credentials(
        store, run_id=run_id, run_kind="dispatch", attempt_number=1
    )


def _placed_attestation(
    placed: bool, attest: Callable[[str], None]
) -> Callable[[str], None] | None:
    """*attest*, or None when this run is not placed on an instance.

    A function so the launch site stays a sequence of calls: a local placement
    passes no attestation at all, because the local launcher already resolved
    and recorded the immutable ID of the image it launches.
    """
    if placed:
        return attest
    return None


def _profile_recorded_after_launch(
    deferred: bool, transport: ResolvedInferenceTransport | None
) -> bool:
    """Whether this attempt's realized profile is recorded AFTER its launch.

    Two launches cannot describe their own boundary at admission. A sidecar
    attempt's topology is not verified until the proxy is up
    (:func:`_record_ready_sidecar_profile`), and a PLACED attempt's container
    is launched from the mutable tag against another machine's image store, so
    the immutable ID that describes its bytes is the one that instance resolves
    — read before its container starts and attested there
    (:func:`_record_placed_profile`). Recording this machine's ID for either
    would attest bytes the agent never ran (#1550 F1, #2526 review).
    """
    if deferred:
        return True
    return transport is not None and transport.sidecar


def _record_ready_sidecar_profile(
    transport: ResolvedInferenceTransport,
    supervisor: Any,
    *,
    store: RunStore,
    run_id: str,
    runner_name: str,
    model: str,
    tracker: str,
    image: str,
    image_id: str,
    service_tier: str | None,
    on_ready: Callable[[], None] | None = None,
) -> None:
    """Record one route only after its attempt topology is verified."""
    profile = _sandbox_route_profile(
        run_id,
        runner_name=runner_name,
        model=model,
        tracker=tracker,
        image=image,
        image_id=image_id,
        transport=transport,
        service_tier=service_tier,
    )
    if profile is None:
        raise RuntimeError("sidecar attempt route profile is unavailable")
    identity = store.get_admission_identity(run_id)
    if identity is not None and hasattr(store, "record_attempt_admission"):
        store.record_attempt_admission(profile, identity)
    else:
        store.record_route_profile(profile)
    stored_profile = store.require_route_profile(run_id, 1)
    record = store.get(run_id)
    append_sidecar_evidence(
        store,
        supervisor.topology_evidence(
            run_id=run_id,
            attempt_number=1,
            profile_fingerprint=stored_profile.fingerprint,
            stage="admission",
        ),
        actor=Actor("policy", "sidecar-supervisor"),
        issue_num=None if record is None else record.issue_num,
        item_id=None if record is None else record.item_id,
    )
    require_publication_launch_credentials(
        store,
        run_id=run_id,
        run_kind="dispatch",
        attempt_number=1,
    )
    if on_ready is not None:
        on_ready()


def _record_sandbox_start(
    store: RunStore | None,
    *,
    run_id: str | None = None,
    issue_num: int,
    item_id: str,
    branch: str,
    title: str,
    model: str,
    runner_name: str,
    log_file: Path,
    tracker: str = "github",
    repo: str | None = None,
    worker_cap: int = 0,
    cap_exclude_issue: int | None = None,
    admit_budget_check: Callable[[], None] | None = None,
    image: str | None = None,
    image_id: str | None = None,
    runtime: str | None = None,
    transport: ResolvedInferenceTransport | None = None,
    service_tier: str | None = None,
    credential: BoundedRunKey | None = None,
    reservation: EpicReservation | None = None,
    reservation_pinned: bool = False,
    broker_provider: str | None = None,
    self_check: bool = False,
    prompt_profile_version_id: int | None = None,
    profile_deferred: bool = False,
) -> str:
    """Write the "pending" run row for a sandboxed dispatch (#739).

    This row is the epic driver's liveness signal: ``_reconcile_reservations``
    treats a reservation with no run row past the startup grace as a dead
    spawn and fails the node with a ``dispatch_child_died`` diagnostic — so a
    slow-but-alive container that never wrote this row would be wrongly
    declared dead. ``run_tracked_agent`` writes this row for host runs; the
    sandboxed path must do the same.

    ``worker_cap`` > 0 makes this row an atomic worker-slot admission (#745),
    mirroring ``run_tracked_agent``: it is inserted only if the fleet is below
    the cap in the same transaction as the count, else ``WorkerCapReached`` is
    raised (no row written) so the caller requeues.

    ``runner_name`` is the RESOLVED runner behind the container image (#1147),
    not a constant: it was hardcoded to ``"claude-code"``, so run history
    asserted a falsehood about every contained non-claude run. The stored value
    is what ``kew status`` attributes spend with — ``_status_cost_by_runner``
    buckets every run's cost by this column — so a wrong one moved a contained
    opencode run's dollars onto claude-code's line. It is a plain column on the
    record; the admission call below is untouched by it.

    I01: the row goes through ``admit_run_with_identity`` — the same normalized
    primitive the host path uses — so the immutable admission identity
    (``tracker`` + opaque ``item_id`` + delivery ``repo`` + epic node) is
    written in the same transaction as the run row. ``tracker`` and ``repo``
    are passed in rather than re-derived here: this function has no config, and
    guessing the tracker from the id's shape is exactly the inference this
    identity exists to replace.

    ``repo`` lands on the run ROW as well as the identity, exactly as
    ``_save_pending_run`` does it (#2097). The row is the only repository
    binding a detached canonical publication gets, because it deliberately
    never reads its worktree's origin. Stamping only the identity left that
    publication with no slug to resolve, and left the two dispatch paths
    disagreeing on the very column ``_label_request_superseded`` matches
    stop-label ownership on — so a newer contained run of an item could not
    supersede an older run's pending label reset.

    ``self_check`` is whether the prompt handed to this container carried the
    pre-publish self-check (#2454) — the contained path's half of the fact the
    host path records through ``run_tracked_agent``, so the round-1 READY rate
    can be read for every dispatch rather than only the uncontained ones.

    ``profile_deferred`` says this attempt's realized profile is recorded after
    its launch rather than in this transaction, which is what a run PLACED on
    an instance needs: the bytes its container runs are named by that
    instance's image store, not this one's
    (:func:`_profile_recorded_after_launch`).

    I03C: ``image``/``runtime`` carry the containment boundary this row is
    admitted for, so the same transaction also records the attempt's realized
    route profile — the resolved IMAGE ID, never the mutable tag. They default
    to ``None`` for callers that are not a contained dispatch; such a caller
    records no profile rather than a host profile it has no evidence for.
    """
    run_id = run_id or str(uuid.uuid4())
    if store is not None:
        from kew.graph.driver.review_detach import held_reservation
        from kew.process_groups import exclusive_owner_pid, identity_at_admission
        from kew.run_store import RunRecord

        owner = exclusive_owner_pid()
        record = RunRecord(
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch,
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            runner_name=runner_name,
            model=model,
            log_file=log_file,
            title=title,
            repo=repo,
            container_name=f"kew-{branch_slug(item_id)}",
            pgid=owner,
            owner_pid=owner,
            **identity_at_admission(owner),
            container_runtime=runtime,
            self_check=self_check,
            prompt_profile_version_id=prompt_profile_version_id,
        )
        # Fleet concurrency is keyed on item_id (A3): exclude this run's own
        # item so it never counts itself. exclude_item wins over the legacy
        # int exclude_issue, which collapses every Linear row onto the 0
        # sentinel.
        profile = (
            None
            if _profile_recorded_after_launch(profile_deferred, transport)
            else _sandbox_route_profile(
                run_id,
                runner_name=runner_name,
                model=model,
                tracker=tracker,
                image=image,
                image_id=image_id,
                transport=transport,
                service_tier=service_tier,
                credential=credential,
                broker_provider=broker_provider,
            )
        )
        admitted = store.admit_run_with_identity(
            record,
            store.build_admission_identity(
                run_id, tracker=tracker, item_id=item_id, repo=repo
            ),
            cap=worker_cap,
            exclude_issue=cap_exclude_issue,
            exclude_item=item_id,
            # The held review-stage slot this run starts under (#2459) is not
            # a second run against it — same rule as the host path.
            exclude_run=held_reservation(),
            on_admit=admit_budget_check if worker_cap > 0 else None,
            reservation=reservation,
            reservation_pinned=reservation_pinned,
            enrich=(
                store.route_profile_enricher(profile)
                if profile is not None and hasattr(store, "route_profile_enricher")
                else None
            ),
        )
        if not admitted:
            from kew.dispatch_core import WorkerCapReached

            raise WorkerCapReached(issue_num)
    return run_id


def config_with_run_cap(
    config: DispatchConfig, budget_override_usd: float | None
) -> DispatchConfig:
    """*config*, carrying a runtime override as the run cap (#1462).

    The container argv reads its cap from ``config.agent.max_budget_usd``, so a
    caller holding an override (``max_remediation_usd``, #993) hands the runner
    a copy carrying it — the override then wins in the container as it does in
    the host argv. The caller's config is never mutated.
    """
    if budget_override_usd is None:
        return config
    capped = config.model_copy(deep=True)
    capped.agent.max_budget_usd = budget_override_usd
    return capped


@dataclass(frozen=True)
class ContainedCostGovernor:
    """The host path's mid-run cost ceilings, applied to a contained run (#1462).

    ``budget_check`` is the run cap (``budget_watchdog(..., contained=True)``)
    and ``anomaly_threshold_usd`` the circuit breaker's ceiling, both decided
    by :func:`kew.orchestrator._should_trip_budget` exactly as on the host. The
    stream fold reports the streamed cost and compaction count after each
    usage-bearing line; on a trip the container is stopped through its handle
    and the category lands on the run's result.
    """

    budget_check: Callable[[float], bool] | None = None
    anomaly_threshold_usd: float | None = None
    anomaly_compaction_allowance_usd: float = 0.0
    #: The least time between two kill requests for one run: a refused kill is
    #: asked again on a later line, not on every line of a burst.
    kill_retry_seconds: float = 1.0
    #: The breaker baseline the anomaly ceiling came from, for its audit event.
    baseline: RunBaseline | None = None

    def watch(self, result: DispatchResult, handle: Any) -> CostObserver | None:
        """A per-line ``(cost, compactions)`` observer, True once the run has
        tripped; None when neither ceiling is armed."""
        if self.budget_check is None and self.anomaly_threshold_usd is None:
            return None
        return _CostStop(self, result, handle).observe


#: What a contained cost stop is recorded as. The host's ``_classify_attempt``
#: turns a watchdog trip into ``budget_stop`` (#859): the item fails and the
#: checkout is kept. ``budget_exceeded`` would complete a clean tree and delete
#: it, and a contained agent commits inside the container, so its clean clone
#: is exactly where the work is.
_CONTAINED_BUDGET_STOP = "budget_stop"
_COST_STOP_CATEGORIES = frozenset({_CONTAINED_BUDGET_STOP, "circuit_breaker"})

#: The governor's per-line observer: ``(cost, compactions)`` in, True once the
#: run has tripped and its container is being stopped.
CostObserver = Callable[[float, int], bool]


@dataclass
class _CostStop:
    """One run's trip decision and the kill it owes the container.

    The cost it judges is a high-water mark, so nothing the stream says later
    lowers it. Once tripped, a later line asks the runtime again — at most once
    per ``kill_retry_seconds`` — until it accepts the kill
    (``ContainerHandle.kill`` latches only on acceptance), and each answer is
    recorded on the result.
    """

    governor: ContainedCostGovernor
    result: DispatchResult
    handle: Any
    peak_usd: float = 0.0
    tripped: bool = False
    stopped: bool = False
    asked_at: float | None = None
    _now: Callable[[], float] = time.monotonic

    def observe(self, cost: float, compactions: int) -> bool:
        self.peak_usd = max(self.peak_usd, cost)
        if not self.tripped:
            # The host counts compactions on the result the breaker reads (#859).
            self.result.compaction_count = compactions
            self.tripped = _should_trip_budget(
                self.peak_usd,
                self.governor.budget_check,
                self.governor.anomaly_threshold_usd,
                self.result,
                self.governor.anomaly_compaction_allowance_usd,
                max_discounted_compactions=MAX_DISCOUNTED_COMPACTIONS,
            )
            if not self.tripped:
                return False
            if self.result.budget_exceeded:
                self.result.error_category = _CONTAINED_BUDGET_STOP
            # The trip was judged on the raw figure; what is recorded is bounded.
            self.result.estimated_cost_usd = _recordable_usd(
                self.result.estimated_cost_usd
            )
            _logger.warning(
                "contained run stopped at $%.2f: %s",
                self.peak_usd,
                self.result.error_category,
            )
        self._ask_for_kill()
        return True

    def _ask_for_kill(self) -> None:
        now = self._now()
        if self.stopped or (
            self.asked_at is not None
            and now - self.asked_at < self.governor.kill_retry_seconds
        ):
            return
        self.asked_at = now
        status = _stop_container(self.handle)
        if status.value != self.result.cost_stop_kill:
            _logger.warning("contained cost stop: kill %s", status.value)
        self.result.cost_stop_kill = status.value
        self.stopped = status is KillStatus.ACCEPTED


def _stop_container(handle: Any) -> KillStatus:
    """Stop the container itself, never just its local runtime client.

    Killing the ``docker run`` client leaves the container running, so the
    stop goes through the handle's own ``kill`` — as ``_terminate_group`` does
    for a contained host-path run. On OpenShell that kill deletes the sandbox,
    and the agent's work with it, so the work is downloaded first: a cost stop
    keeps it, where a cap breach does not.

    A kill another path already landed (a cap breach, a cancel) is the stop:
    the handle refuses a second one, and that refusal is not a running
    container."""
    if _killed_elsewhere(handle):
        return KillStatus.ACCEPTED
    return _kill_container(handle, reason="tripped its cost cap")


def _killed_elsewhere(handle: Any) -> bool:
    """Whether a kill has already landed on *handle*, by whichever path."""
    return getattr(handle, "kill_accepted", False) is True


def _kill_container(handle: Any, *, reason: str) -> KillStatus:
    """The handle's own kill, after downloading OpenShell work; its answer only.

    A container another path already stopped is refused here as the handle
    refuses it, never reported as this caller's accepted kill."""
    realize = getattr(handle, "realize_checkout", None)
    if callable(realize):
        realize()
    kill = getattr(handle, "kill", None)
    if not callable(kill):
        _logger.warning("contained run %s but has no kill handle", reason)
        return KillStatus.REFUSED
    status = kill()
    return status if isinstance(status, KillStatus) else KillStatus.UNKNOWN


#: How often the silence watchdog compares the clock with the last line.
_SILENCE_POLL_SECONDS = 5.0


def no_output_seconds(config: DispatchConfig) -> float:
    """``[agent] no_output_minutes`` in seconds; 0 turns the watchdog off."""
    return 60.0 * config.agent.no_output_minutes


def _pause_until_done(done: threading.Event, seconds: float) -> bool:
    return done.wait(seconds)


@dataclass(frozen=True)
class SilencePacing:
    """The clock a :class:`StreamSilenceWatch` reads and the pause between its
    checks. *pause* returns True once the watch is disarmed; a test injects
    one that checks only when the test says so."""

    now: Callable[[], float] = time.monotonic
    pause: Callable[[threading.Event, float], bool] = _pause_until_done
    poll_seconds: float = _SILENCE_POLL_SECONDS


class StreamSilenceWatch:
    """Stop a contained run whose model call went silent for too long (#2240).

    The stream consumer blocks in ``for line in handle``, so the watch runs on a
    thread beside it. Every line the wrapped stream yields is output and resets
    the clock, and is folded through the runner's :mod:`kew.model_wait` state:
    the clock counts only while a model call is outstanding, never while a tool
    call is open, after a turn's ``result``, or while a background task runs.
    Once *limit_seconds* of such silence pass, the container is stopped by its
    handle, as a cost stop is. Every poll checks the silence again first, so a
    stop whose kill did not land is retried only while the run is still silent.

    The watch disarms when the stream's lines run out — before the consumer
    waits on the exit — so a container that wrote its last line and exits
    slowly is never called silent, and no kill (with OpenShell's download of
    the work) starts beside the stream's own end.

    The thread only kills: the run row is written on the consumer's own thread
    when :meth:`watching` closes (the store's connection is not shared), and
    only for a kill this watch landed on a run no cost stop ended — a stream
    that ended by itself, or a container another path already stopped, is
    never named silent.
    """

    def __init__(
        self,
        handle: Any,
        *,
        limit_seconds: float,
        store: RunStore | None,
        run_id: str,
        runner: str,
        result: DispatchResult | None = None,
        pacing: SilencePacing | None = None,
    ) -> None:
        self._handle = handle
        self._limit = limit_seconds
        self._store = store
        self._run_id = run_id
        self._result = result
        self._pacing = pacing or SilencePacing()
        self._turns = model_wait_for(runner)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._last = self._pacing.now()
        self._tripped = False
        self.stopped = False

    def observe(self, stream: Any) -> _SilenceTap:
        """Wrap *stream* so every line it yields counts as output."""
        return _SilenceTap(stream, self)

    def heard(self, line: str) -> None:
        with self._lock:
            self._last = self._pacing.now()
            self._turns.observe(line)

    def disarm(self) -> None:
        """Stop checking. Under the lock, so a check that already began
        finishes its kill and none starts after this returns."""
        with self._lock:
            self._done.set()

    def silenced(self) -> bool:
        """Whether this watch stopped the run and no cost stop did."""
        return self.stopped and not (
            self._result is not None and _cost_stopped(self._result)
        )

    @contextmanager
    def watching(self) -> Iterator[None]:
        if self._limit <= 0:
            yield
            return
        thread = threading.Thread(target=self._watch, daemon=True)
        thread.start()
        try:
            yield
        finally:
            self.disarm()
            thread.join()
            self._record()

    def _record(self) -> None:
        """Name the silence on the run row, never raising over the stream's
        own outcome: a failed write is logged and the result still names it."""
        if not self.silenced():
            return
        try:
            _record_stream_silence(self._store, self._run_id, self._limit)
        except Exception as exc:  # noqa: BLE001 — the stream's outcome stands
            _logger.warning(
                "run %s: stream silence not recorded on the run row (%s)",
                self._run_id,
                type(exc).__name__,
            )

    def _watch(self) -> None:
        try:
            while not self._pacing.pause(self._done, self._pacing.poll_seconds):
                with self._lock:
                    if self._done.is_set() or self._check():
                        return
        except Exception:  # noqa: BLE001 — logged; the stream runs on unwatched
            _logger.warning(
                "contained run %s: the no-output watchdog failed and no longer "
                "watches this run",
                self._run_id,
                exc_info=True,
            )

    def _silent(self) -> bool:
        return (
            self._turns.outstanding and self._pacing.now() - self._last >= self._limit
        )

    def _check(self) -> bool:
        """One poll, under the lock; True once there is nothing left to do."""
        if not self._silent():
            # Output resumed, or the model call ended: a stop not yet landed
            # is called off.
            self._tripped = False
            return False
        if _killed_elsewhere(self._handle):
            # Another path stopped it — the run timeout, a cap breach, a
            # cancel — before this attempt, even after an unsettled one of
            # this watch's own: not this watch's stop.
            return True
        if not self._tripped:
            self._tripped = True
            _logger.warning(
                "contained run %s: its model call wrote nothing for %.0f "
                "minutes; stopping it",
                self._run_id,
                self._limit / 60,
            )
        self.stopped = _settle_kill(self._handle) is KillStatus.ACCEPTED
        return self.stopped


def _settle_kill(handle: Any) -> KillStatus:
    """This watch's own kill; a kill of unknown outcome (a runtime call that
    timed out) is settled by the handle's own read of the runtime, when it has
    one. ACCEPTED comes only from those two answers, never from a kill another
    path landed."""
    status = _kill_container(handle, reason="went silent")
    if status is not KillStatus.UNKNOWN:
        return status
    resolve = getattr(handle, "_resolve_unknown_kill", None)
    if not callable(resolve):
        return status
    settled = resolve()
    return settled if isinstance(settled, KillStatus) else KillStatus.UNKNOWN


@dataclass
class _SilenceTap:
    """A container stream that tells its :class:`StreamSilenceWatch` each line.

    Keeps the consumer's contract (iterate, then ``wait``); anything else is
    read through to the wrapped stream, as the taps under it expose it. The
    watch is disarmed the moment the lines end, however they end."""

    stream: Any
    watch: StreamSilenceWatch

    def __getattr__(self, name: str) -> Any:
        if name in ("stream", "watch"):
            raise AttributeError(name)
        return getattr(self.stream, name)

    def __iter__(self) -> Iterator[str]:
        try:
            for line in self.stream:
                self.watch.heard(line)
                yield line
        finally:
            self.watch.disarm()

    def wait(self) -> int:
        return int(self.stream.wait())


def _record_stream_silence(
    store: RunStore | None, run_id: str, limit_seconds: float
) -> None:
    """Name the silence on the run row; the terminal update keeps it."""
    if store is None:
        return
    stored = store.get(run_id)
    if stored is None:
        return
    stored.error_category = STREAM_SILENT_CATEGORY
    store.save(stored)
    _warn(
        f"Run {run_id} wrote nothing for {limit_seconds / 60:.0f} minutes; "
        f"its container was stopped ({STREAM_SILENT_CATEGORY})"
    )


@dataclass
class _SandboxTelemetry:
    """Cost/token/model telemetry folded from a sandbox stream's ``StreamEvent``s.

    Host runs get this via ``run_tracked_agent``; contained rows were blind, so
    cost tracking, ``kew report``, budget accounting, and the breaker's
    per-(model, complexity) buckets all under-counted sandboxed runs (#761).
    All fields default to zero/``None`` — the fallback for a stream that
    reported nothing. ``from_result_event`` distinguishes "the run genuinely
    reported these zeros" from "the stream never reached a terminal result
    event", so the terminal update can preserve live-flushed telemetry instead
    of overwriting it with a truncated replay. ``has_native_cost`` draws the
    other distinction the host path draws with ``reported_cost_usd is None``:
    a runner that reported NO provider-billed figure (lemonade always, pi and
    hermes when the provider omits one) is not a runner that ran for free, so
    ``cost_usd == 0.0`` alone must never be read as the run's spend.
    """

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reported_model: str | None = None
    reported_service_tier: str | None = None
    from_result_event: bool = False
    has_native_cost: bool = False
    # What the cost governor prices (#1462), as the host's watchdog does: the
    # per-turn usage and per-step native cost exactly as streamed. A result
    # event restates the run over the fields above; it never touches these, so
    # a forged or early summary line cannot lower the cost the governor sees.
    streamed_input_tokens: int = 0
    streamed_output_tokens: int = 0
    streamed_cache_creation_tokens: int = 0
    streamed_cache_read_tokens: int = 0
    streamed_cost_usd: float = 0.0
    compaction_count: int = 0
    result_events: int = 0


def _resolve_stream_parser(runner: str) -> AgentRunner | None:
    """The runner whose ``parse_line`` reads this sandbox stream, or None.

    Fail-SAFE, not fail-closed, and deliberately so: this resolution also runs
    during terminal finalization, where raising would leave the run row
    "pending" forever and pin the node's reservation (#739). The fail-CLOSED
    gate is at launch (``run_container_and_stream`` → ``get_runner``), so an
    unknown runner never reaches a container in the first place; reaching here
    with an unresolvable name means kew lost the name after the run, and the
    right response is a loud log plus zero telemetry, not a stuck row.
    """
    try:
        return get_runner(runner)
    except ValueError:
        _logger.warning(
            "sandbox telemetry: unknown runner %r — recording no stream "
            "telemetry for this run",
            runner,
        )
        return None


def _capture_reported_model(live: _SandboxTelemetry, raw: object) -> None:
    """Record the model a runner says is doing the work, normalized."""
    if isinstance(raw, str) and raw:
        from kew.dispatch_core import _normalize_reported_model

        live.reported_model = _normalize_reported_model(raw)


def _accumulate_native_cost(live: _SandboxTelemetry, event: StreamEvent) -> bool:
    """Sum a ``step`` event's native per-step cost into *live* (#1082).

    Mirrors ``orchestrator._accumulate_native_step_cost``: opencode/pi report
    the provider-billed figure per step, so a run's spend exists ONLY as this
    sum; claude-code sends ``None`` here and restates its total on the result
    event instead. ``bool`` is rejected explicitly (it is an ``int`` subclass),
    and so is a negative step: a bill never refunds, and a negative figure
    would buy back spend under the cost governor (#1462).
    """
    cost = event.metadata.get("cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        return False
    live.cost_usd += float(cost)
    live.streamed_cost_usd += float(cost)
    live.has_native_cost = True
    return True


def _apply_run_totals(live: _SandboxTelemetry, event: StreamEvent) -> None:
    """Overwrite *live* from a result event's :class:`RunTotals`, when it has one.

    A runner that restates the whole run (claude-code's ``modelUsage``) is
    authoritative and replaces the per-turn accumulation. One that does not
    (opencode, pi, lemonade, hermes) leaves the accumulated per-step figures
    standing — overwriting those with an absent summary is precisely the
    zeroing #761 was about.

    Each half of the summary is applied only when the runner actually reported
    it, cost and tokens alike: claude-code ALWAYS attaches a ``RunTotals`` (a
    result event IS its run summary), so an error/abort result carrying neither
    ``modelUsage`` nor ``usage`` arrives here all-zero — and taking those zeros
    would re-zero a run whose turns demonstrably reported tokens.
    """
    totals = event.metadata.get(RUN_TOTALS_KEY)
    if not isinstance(totals, RunTotals):
        return
    if totals.cost_usd is not None:
        live.cost_usd = totals.cost_usd
        live.has_native_cost = True
    if _reports_tokens(totals):
        live.input_tokens = totals.input_tokens
        live.output_tokens = totals.output_tokens
        live.cache_creation_tokens = totals.cache_creation_tokens
        live.cache_read_tokens = totals.cache_read_tokens
    _capture_reported_model(live, totals.model)


def _reports_tokens(totals: RunTotals) -> bool:
    """True when a run summary carries any token figure at all.

    An all-zero summary is a runner saying nothing about tokens, not a run that
    used none — see ``_apply_run_totals``.
    """
    return bool(
        totals.input_tokens
        or totals.output_tokens
        or totals.cache_creation_tokens
        or totals.cache_read_tokens
    )


def _fold_stream_event(live: _SandboxTelemetry, event: StreamEvent) -> bool:
    """Fold one parsed ``StreamEvent`` into *live*, mirroring the host path.

    Deliberately the SAME event grammar ``orchestrator._handle_stream_event``
    reads, so a contained run and a host run derive one run's telemetry the same
    way rather than kew keeping two interpretations of ``StreamEvent`` (#1147):
    ``metadata["model"]`` names the model, ``usage`` events accumulate tokens
    (``step`` is excluded — runners emit a paired usage+step per turn and
    summing both double-counts, #310/#311), ``step`` events sum the native
    per-step cost, and a ``result`` event applies the run summary if it carries
    one.

    Returns True when the event moved a cost/token figure — the signal
    ``_stream_sandbox_log`` uses to decide the row is worth refreshing.
    """
    _capture_reported_model(live, event.metadata.get("model"))
    if "service_tier" in event.metadata:
        live.reported_service_tier = (
            service_tier_token(event.metadata["service_tier"]) or None
        )
    if event.event_type == "result":
        live.from_result_event = True
        live.result_events += 1
        _apply_run_totals(live, event)
        return True
    if event.event_type == "step":
        return _accumulate_native_cost(live, event)
    if event.event_type == "compaction":
        # Counted for the breaker's compaction allowance, as the host's
        # ``_handle_stream_event`` counts it (#859).
        live.compaction_count += 1
        return False
    if not event_contributes_usage(event):
        return False
    live.input_tokens += event.input_tokens
    live.output_tokens += event.output_tokens
    live.cache_creation_tokens += event.cache_creation_tokens
    live.cache_read_tokens += event.cache_read_tokens
    # Never negative, so the governor's estimate only ever grows.
    live.streamed_input_tokens += max(0, event.input_tokens)
    live.streamed_output_tokens += max(0, event.output_tokens)
    live.streamed_cache_creation_tokens += max(0, event.cache_creation_tokens)
    live.streamed_cache_read_tokens += max(0, event.cache_read_tokens)
    return True


def _accumulate_stream_line(
    live: _SandboxTelemetry,
    line: str,
    runner: AgentRunner,
    derivation: StreamDerivation | None = None,
) -> bool:
    """Fold one container stream line into *live* via ``runner.parse_line`` (#1147).

    The sandbox path used to read claude-shaped JSON fields directly, so every
    non-claude image recorded zero cost and zero tokens — the #761 blindness
    class. Routing through the runner's OWN parser is what makes a contained
    opencode/pi run's spend land on the row, and it is the same parser the host
    path (``orchestrator._process_line``) already uses.

    Container stdout is another process's untrusted output: a parser that trips
    on a malformed or interleaved line must not kill the tail loop (which would
    abort a live run under ``sandbox_stream_error``) or the terminal replay.
    *derivation*, when given, reads each parsed event for the agent's
    ``KEW_NEEDS_HUMAN`` stop exactly as the host path does (#2601).
    Returns True when the line moved a cost/token figure."""
    try:
        events = runner.parse_line(line)
    except Exception:  # noqa: BLE001 - untrusted container output, never fatal
        _logger.debug("sandbox telemetry: unparsable stream line ignored")
        return False
    contributed = False
    for event in events:
        if derivation is not None:
            derivation.record_message(event.event_type, event.message, event.metadata)
        contributed = _fold_stream_event(live, event) or contributed
    return contributed


def _telemetry_from_evidence(log_file: Path | None) -> _SandboxTelemetry:
    """A finished sandbox run's telemetry from its DERIVED evidence (I06T).

    This used to re-read the whole transcript through the runner's parser,
    which is why the transcript had to exist. The fold now happens once, live,
    in ``_stream_sandbox_log`` — the same ``parse_line`` over the same lines,
    at the only moment the raw bytes are allowed to be in memory — and the
    totals ride to the finalizer in the sidecar instead of in a log.

    ``from_result_event`` is False when the stream carried no terminal result
    event at all, and equally when there is no sidecar (a legacy contained run,
    or one whose sidecar could not be written): the caller must then keep
    whatever the live path already flushed rather than treating an absent
    derivation as a run that reported zeros.
    """
    evidence = read_derived_evidence(log_file)
    if evidence is None:
        return _SandboxTelemetry()
    totals = evidence.totals
    return _SandboxTelemetry(
        cost_usd=totals.cost_usd,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_creation_tokens=totals.cache_creation_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        reported_model=totals.reported_model,
        reported_service_tier=totals.reported_service_tier,
        from_result_event=totals.from_result_event,
        has_native_cost=totals.has_native_cost,
    )


def _runner_totals(live: _SandboxTelemetry) -> RunnerTotals:
    """The closed numeric fact set a sidecar may carry, from live telemetry."""
    return RunnerTotals(
        cost_usd=live.cost_usd,
        input_tokens=live.input_tokens,
        output_tokens=live.output_tokens,
        cache_creation_tokens=live.cache_creation_tokens,
        cache_read_tokens=live.cache_read_tokens,
        reported_model=live.reported_model,
        reported_service_tier=live.reported_service_tier,
        from_result_event=live.from_result_event,
        has_native_cost=live.has_native_cost,
    )


#: The most a contained run's row, stop reason or breaker fact records as its
#: cost. Container output can claim any figure, including sums that overflow
#: to infinity, and strict JSON readers (the console, jq) reject ``Infinity``.
#: The governor still judges the raw figure; only what is written is bounded.
MAX_RECORDED_COST_USD = 1e12


def _recordable_usd(cost: float) -> float:
    """*cost* bounded for writing: a non-finite or larger figure is recorded
    as :data:`MAX_RECORDED_COST_USD`, never as ``inf``/``nan``."""
    if not math.isfinite(cost):
        return MAX_RECORDED_COST_USD
    return min(cost, MAX_RECORDED_COST_USD)


# How often (wall-clock seconds) the streaming loop flushes accumulated live
# telemetry to the run row. Cheap SQLite writes, but no need to hammer the store
# on every assistant turn — a couple seconds keeps the surface fresh without
# turning the tail loop into a write storm (#SFD-155).
_SANDBOX_LIVE_FLUSH_SECS = 2.0


def _planned_bound_usd(
    tokens: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> float | None:
    """The planned half of a sandbox row's cost pair (#1092): *tokens* priced at
    *model*'s bound, or None when there is no usable rate to price against.

    Mirrors ``DispatchResult.compute_cost`` on the host path — an unpriced model
    records "no bound" rather than a fabricated $0 one, so unpriced runs stay out
    of the planning-accuracy ratio instead of dragging every mixed bucket toward
    a spurious over-run.
    """
    from kew.pricing import is_priced_model

    if not model or not is_priced_model(model, overrides):
        return None
    return _priced_tokens_usd(
        model,
        overrides,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_creation_tokens=tokens.cache_creation_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
    )


def _priced_tokens_usd(
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """Token counts priced at *model*, or None when no finite price exists.

    The counts come from container output, which is untrusted: a count past
    what a float holds raises out of the pricing arithmetic (#1462). The
    parser bounds each figure (``protocol_int``), so only an absurd sum gets
    here; the governor reads None as unbounded spend, and a row write records
    :data:`MAX_RECORDED_COST_USD`.
    """
    from kew.pricing import compute_cost_from_tokens

    try:
        cost = compute_cost_from_tokens(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            overrides=overrides,
        )
    except (OverflowError, ValueError) as exc:
        _logger.warning(
            "sandbox telemetry: usage counts too large to price (%s)",
            type(exc).__name__,
        )
        return None
    return cost if math.isfinite(cost) else None


def _mark_unpriced_accounting(
    stored: RunRecord,
    tokens: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Make "we could not price this" an explicit fact, not a $0.00 claim.

    The sandbox twin of ``dispatch_core._mark_unpriced_accounting``. A run that
    burned tokens against a model with no usable rate records $0.00, which the
    ledger cannot tell from a run that genuinely cost nothing — local inference
    under a free provider prefix is $0 *by design* and stays readable as such,
    because ``is_priced_model`` counts a free prefix as priced (#1565).
    """
    from kew.pricing import is_priced_model

    if tokens.has_native_cost:
        return
    if not (tokens.input_tokens or tokens.output_tokens):
        return
    if model and is_priced_model(model, overrides):
        return
    stored.accounting_status = "unreconciled"


def _effective_cost_usd(
    tokens: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> float | None:
    """A sandbox row's spend: the runner's native figure, else a token estimate.

    Mirrors ``DispatchResult.compute_cost``'s preference on the host path — a
    provider-billed number beats a rate-card estimate whenever the stream
    produced one (#1082), and a runner that never reports one (lemonade; pi and
    hermes when the provider omits it) is priced from its tokens rather than
    recorded as free. claude-code reports nothing until its result event, so it
    is estimate-priced for the whole run and native-priced at the end.

    Used by BOTH the live flush and the terminal update, deliberately: a
    terminal update that took ``cost_usd`` raw would finalize every
    no-native-cost runner at $0.00, throwing away the estimate the live path
    already wrote and under-counting daily/per-issue budget spend.

    None means neither source exists — no native figure and no model to price
    against — so the caller leaves the recorded cost alone instead of
    overwriting a real number with a fabricated zero. A figure too large to be
    a finite cost — native or priced — is recorded as
    :data:`MAX_RECORDED_COST_USD`: the row never holds an unbounded number, and
    budget surfaces still see the run as spending the most a row can say.
    """
    if tokens.has_native_cost:
        return _recordable_usd(tokens.cost_usd)
    if not model:
        return None
    priced = _priced_tokens_usd(
        model,
        overrides,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_creation_tokens=tokens.cache_creation_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
    )
    return _recordable_usd(math.inf if priced is None else priced)


def _write_live_sandbox_telemetry(
    store: RunStore,
    run_id: str,
    live: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Refresh the in-flight run row with accumulated live telemetry (#SFD-155).

    Leaves status/timestamps untouched — only the token/cost/model fields the
    runs surface reads — so a running contained row stops showing false zeros.
    Cost prefers the runner's own native figure when the stream has already
    reported one (opencode/pi bill per step) and falls back to a token-derived
    estimate otherwise (claude-code reports only at the end);
    ``_finalize_sandbox_run`` replaces it with the authoritative figure at
    completion."""
    stored = store.get(run_id)
    if stored is None:
        return
    stored.input_tokens = live.input_tokens
    stored.output_tokens = live.output_tokens
    stored.cache_creation_tokens = live.cache_creation_tokens
    stored.cache_read_tokens = live.cache_read_tokens
    stored.total_tokens = (
        live.input_tokens
        + live.output_tokens
        + live.cache_creation_tokens
        + live.cache_read_tokens
    )
    live_cost = _effective_cost_usd(live, model, overrides)
    if live_cost is not None:
        stored.estimated_cost_usd = live_cost
    # The planned half of the pair (#1092), kept in step with the token columns
    # written just above. Provisional: this is the token subset seen so far, and
    # ``_finalize_sandbox_run`` recomputes it from the authoritative totals.
    stored.planned_cost_usd = _planned_bound_usd(live, model, overrides)
    if live.reported_model:
        stored.reported_model = live.reported_model
    if live.reported_service_tier:
        stored.reported_service_tier = live.reported_service_tier
    store.save(stored)


def _stream_sandbox_log(
    handle: ContainerStream,
    log_file: Path,
    store: RunStore | None,
    run_id: str,
    *,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    runner: str,
    on_cost: CostObserver | None = None,
    _now: Callable[[], float] = time.monotonic,
) -> int:
    """Fold the container stream into evidence, writing a sanitized log (I06T).

    The container's stdout is raw provider output. It is folded twice as it
    arrives — through the runner's own ``parse_line`` for cost/tokens (#1147)
    and through :class:`StreamDerivation` for first-edit, tool, terminal and
    report facts — and then dropped. What lands on disk is the sanitized
    progress log plus the derived-evidence sidecar the finalizer, the report
    publisher and the first-edit columns all read; nothing re-reads the log,
    because there is no longer anything in it to re-read.

    Live telemetry still flushes to the run row at most every
    ``_SANDBOX_LIVE_FLUSH_SECS``. Returns the container exit code.

    *runner* names the parser (#1147): the container is running THAT agent, so
    its stream is read by that agent's ``parse_line`` rather than assumed to be
    claude stream-json.

    *on_cost* is a :meth:`ContainedCostGovernor.watch` observer: it sees the
    streamed cost and compaction count after every usage-bearing line and
    stops the container on a trip, asking again on every later line until the
    runtime accepts. The stream is still read to its end, so the usage the
    agent reported on its way down is folded too."""
    with sandbox_log_fold(
        log_file,
        store,
        run_id,
        model=model,
        overrides=overrides,
        runner=runner,
        on_cost=on_cost,
        _now=_now,
    ) as fold:
        for line in handle:
            fold(line)
    return handle.wait()


@contextmanager
def sandbox_log_fold(
    log_file: Path,
    store: RunStore | None,
    run_id: str,
    *,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    runner: str,
    on_cost: CostObserver | None = None,
    _now: Callable[[], float] = time.monotonic,
) -> Iterator[Callable[[str], None]]:
    """The per-line fold :func:`_stream_sandbox_log` applies, as a callable.

    Split out so a run whose agent is NOT on this machine folds its stream
    through exactly this code (:mod:`kew.dispatch_placement`, #2526). A remote
    placement relays its lines to a callback rather than over a pipe, and a
    second fold written against that shape would be a second set of evidence
    rules — a sanitized log and a derived-evidence sidecar that drifted from
    the ones every consumer downstream reads.

    The sidecar is written when the context closes, on EVERY exit path
    including a stream that died mid-run: it is the only durable evidence this
    fold produces.
    """
    live = _SandboxTelemetry()
    derivation = StreamDerivation()
    parser = _resolve_stream_parser(runner)
    # Seed the last-flush clock a full interval in the past so the FIRST
    # usage-bearing line updates the row immediately (no initial zero window
    # once the agent starts reporting).
    flushed_at = [_now() - _SANDBOX_LIVE_FLUSH_SECS]
    # The credential this run launches under: a usage warning seen under
    # another one is for a token since replaced (#3329).
    record_runner_launch(store, runner)
    tripped = [False]
    try:
        with log_file.open("w", encoding="utf-8") as fh:
            fh.write(sanitized_log_banner())

            def fold(line: str) -> None:
                safe_line = derivation.observe(line)
                if safe_line is not None:
                    fh.write(safe_line + "\n")
                if parser is None:
                    return
                # Fold telemetry whether or not a store is attached: the fold
                # is how the totals reach the sidecar, and this is the only
                # moment the raw line exists. Only the periodic row FLUSH
                # needs a store.
                results = live.result_events
                moved = _accumulate_stream_line(live, line, parser, derivation)
                # Once tripped, every line re-asks for a kill not yet accepted.
                if not (moved or tripped[0]):
                    return
                # A tripped run flushes every line: its row must carry the
                # whole usage observed, and no result event will restate it.
                tripped[0] = _cost_tripped(
                    on_cost,
                    live,
                    model,
                    overrides,
                    ends_run=live.result_events != results,
                    tripped=tripped[0],
                )
                if not moved or store is None:
                    return
                now = _now()
                if tripped[0] or now - flushed_at[0] >= _SANDBOX_LIVE_FLUSH_SECS:
                    _write_live_sandbox_telemetry(store, run_id, live, model, overrides)
                    flushed_at[0] = now

            yield fold
    finally:
        derivation.record_totals(_runner_totals(live))
        write_derived_evidence(log_file, derivation.evidence())
        _record_stream_quota_warning(store, derivation, runner, run_id)


def _record_stream_quota_warning(
    store: RunStore | None, derivation: StreamDerivation, runner: str, run_id: str
) -> None:
    """Record the stream's usage warning against the runner's token (#3329).

    Every contained run — a dispatch and a reviewer alike — folds through
    here, so a warning either kind's stream carried is recorded once, before
    the refusal it foretells stalls reviews and dispatches.

    Advisory, and read off a stream the agent can write: whatever goes wrong
    recording it is logged, and never fails the run it rode in on.
    """
    warning = derivation.quota_warning
    if store is None or warning is None:
        return
    try:
        record = record_runner_token_warning(
            store, warning.with_runner(warning.runner or runner), run_id=run_id
        )
        if record is not None:
            _logger.warning("provider warning: %s", record.reason())
    except Exception as exc:  # noqa: BLE001 — an advisory write never fails a run
        _logger.warning(
            "run %s: usage warning not recorded (%s)", run_id, type(exc).__name__
        )


def _cost_watch(
    governor: ContainedCostGovernor | None, result: DispatchResult, handle: Any
) -> CostObserver | None:
    """The governor's observer for this container, stopping it by its handle."""
    if governor is None:
        return None
    return governor.watch(result, handle)


def _cost_tripped(
    on_cost: CostObserver | None,
    live: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    *,
    ends_run: bool = False,
    tripped: bool = False,
) -> bool:
    """Report the streamed cost to the governor; True once it has tripped.

    A line that carries the run's result is not judged unless the run already
    tripped: the run has finished, as the host's watchdog never trips an
    exited process. codex reports its last usage on that same
    ``turn.completed`` line; the usage still counts toward the row. Only a
    line that carries a result is spared: a line after it that spends is
    judged normally (#1798)."""
    if on_cost is None or (ends_run and not tripped):
        return tripped
    return on_cost(_governed_cost_usd(live, model, overrides), live.compaction_count)


def _governed_cost_usd(
    tokens: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> float:
    """The cost the governor judges: the streamed native step cost or the
    streamed tokens priced at *model*, whichever is higher.

    The host's watchdog prices accumulated tokens and never reads the runner's
    own total (``orchestrator._budget_tripped``), so a runner-reported figure
    can raise this but never lower it — and a result event, which is the run's
    summary at its end, moves neither half: a finished run is not stopped by
    its own closing line.

    Fails closed: tokens with no finite price are judged as unbounded spend,
    so a line too large to price stops the run rather than the fold.
    """
    estimate: float | None = 0.0
    if model:
        estimate = _priced_tokens_usd(
            model,
            overrides,
            input_tokens=tokens.streamed_input_tokens,
            output_tokens=tokens.streamed_output_tokens,
            cache_creation_tokens=tokens.streamed_cache_creation_tokens,
            cache_read_tokens=tokens.streamed_cache_read_tokens,
        )
    if estimate is None:
        return math.inf
    return max(tokens.streamed_cost_usd, estimate)


def _is_diagnostic_line(line: str) -> bool:
    """Whether *line* is a runtime's own output, not the agent's protocol.

    Every runner's protocol is one JSON object per line, and the entrypoint's
    startup acknowledgement is kew's own; anything else on the merged pipe is
    what an aborting runtime printed on its way down.
    """
    return not line.lstrip().startswith("{") and not is_startup_ack(line)


@dataclass
class _DiagnosticTap:
    """A container stream that keeps its last non-protocol lines (#3191).

    Keeps the stream consumer's contract (iterate, then ``wait``) unchanged,
    as :class:`kew.review_stream.ReviewResultTap` does, so every run kind's
    merged stream says what preceded a signal exit.
    """

    stream: Any
    diagnostic: DiagnosticTail
    #: Reads every protocol line for the runner's own budget stop (#1462).
    budget: RunnerBudgetLatch | None = None

    @property
    def handle(self) -> Any:
        """The container handle under the wrapped stream, as
        :class:`kew.sandbox.LaunchObservedStream` exposes it: lifecycle
        operations (identity, the CPU-cap relaunch, hard-stop cleanup) read the
        handle off the stream they were given, so the tap must not hide it."""
        return self.stream.handle

    @property
    def result(self) -> Any:
        """The run result the wrapped stream records its launch on."""
        return self.stream.result

    def __iter__(self) -> Iterator[str]:
        for line in self.stream:
            if _is_diagnostic_line(line):
                self.diagnostic.add(line)
            elif self.budget is not None:
                self.budget.observe(line)
            yield line

    def wait(self) -> int:
        return int(self.stream.wait())


def _checkout_has_no_changes(
    checkout_dir: Path,
    base_branch: str,
    *,
    publish: str = "checkout",
    dangerously_skip_containment: bool = False,
) -> bool:
    """True when the checkout has no meaningful agent work to publish.

    Checks both uncommitted changes (dirty worktree / untracked files) and
    committed changes (commits on the agent branch ahead of the base). Used
    to detect zero-change container completions so they can be classified as
    ``no_change_needed`` instead of pushed to a PR that would fail.
    """
    from kew.publication import select_publication_mode

    if (
        select_publication_mode(
            publish,
            dangerously_skip_containment=dangerously_skip_containment,
        )
        == "canonical"
    ):
        # Canonical publication proves no-change through its verified artifact.
        # This legacy preflight must never point Git at the checkout.
        return False
    if has_uncommitted_changes(checkout_dir):
        return False
    # Check whether the branch has any commits beyond the base ref.
    base_ref = f"origin/{base_branch}"
    result = _run(
        ["git", "rev-list", "--count", f"{base_ref}..HEAD"],
        cwd=checkout_dir,
        check=False,
    )
    if result.returncode != 0:
        return False  # Can't determine — assume work exists (safe default)
    count = (result.stdout or "").strip()
    return count in ("", "0")


def _finalize_sandbox_run(
    store: RunStore | None,
    run_id: str,
    *,
    exit_code: int,
    runner: str = "",
    error_category: str = "",
    model: str = "",
    overrides: dict[str, tuple[float, float]] | None = None,
    status: RunStatus | None = None,
    reason: str = "",
    cost_floor_usd: float = 0.0,
) -> None:
    """Terminal-update the sandboxed run row — must run on EVERY exit path,
    or the row stays "pending" forever and the driver never releases the
    node's reservation.

    *cost_floor_usd* is the least cost the row may record: a cost-stopped
    run's cost at the stop (:func:`_cost_stop_floor_usd`), which no line the
    agent wrote afterwards can lower (#1462).

    Reads the run's cost/token/model telemetry from the derived-evidence
    sidecar the streaming loop wrote, so contained rows stop recording zero
    cost and the wrong model (#761) without the transcript that used to be
    replayed for it (I06T). *runner* no longer selects a parser here — the
    runner that parsed the stream is the one that tailed it — it only names the
    producer the derived facts are imported into the ledger under, so a runner
    claim stays distinguishable from a host attestation forever.

    *model*/*overrides* price the planned bound (#1092) against the SAME
    authoritative token totals written below. The live streaming path flushes
    only periodically and never after its last line, so its provisional bound
    covers a token subset; recomputing here keeps both halves of the pair
    describing one run. Passing no *model* leaves the recorded bound alone
    (nothing to reprice against).

    A stream that ended before any result event (launch/stream error, killed
    container) has NO authoritative telemetry to write: the live-flushed
    token/cost/bound fields are kept as-is rather than zeroed, since a run that
    demonstrably spent tokens must not be re-recorded as free."""
    require_claim_recovery(store, run_id)
    if store is None:
        return
    stored = store.get(run_id)
    if stored is None:
        return
    if stored.status == "pending" and stored.error_category == "killed":
        # Client exit alone cannot prove that the container stopped.
        store.reconcile_host_stops()
        return
    now = datetime.now(UTC)
    # *status* overrides the derived one so a finished run whose publication
    # failed lands directly in awaiting_publication: one write, no window in
    # which the row reads as a plain success (review round 18).
    stored.status = status or ("success" if exit_code == 0 else "failure")
    stored.exit_code = exit_code
    stored.finished_at = now
    stored.duration_seconds = (now - stored.started_at).total_seconds()
    if error_category:
        stored.error_category = error_category
    _record_terminal_telemetry(stored, model, overrides, cost_floor_usd)
    metrics = first_edit_metrics_for(stored.log_file)
    stored.steps_to_first_edit = metrics.steps_to_first_edit
    stored.tokens_to_first_edit = metrics.tokens_to_first_edit
    store.save(stored)
    if reason:
        emit(
            store,
            "sandbox_execution_failed",
            actor=Actor("policy", "sandbox"),
            run_id=run_id,
            issue_num=stored.issue_num,
            payload={
                "exit_code": exit_code,
                "error_category": error_category,
                "reason": reason,
            },
        )
    _import_contained_runner_evidence(store, stored, runner)


def _import_contained_runner_evidence(
    store: RunStore, stored: RunRecord, runner: str
) -> None:
    """Import a contained run's derived stream facts into the ledger (I06).

    The host path does this from ``dispatch_core._import_runner_evidence``; a
    contained run finalizes here instead, and a receipt that could describe a
    host run but not the same run in a container would be the containment
    blind spot (#761) reappearing in the evidence layer.

    Telemetry, so best-effort — a ledger that cannot take these must never
    leave a terminal row unwritten. An absent derivation is imported as an
    explicit ``evidence_unavailable`` phase, not as a run that did nothing.
    """
    evidence = read_derived_evidence(stored.log_file)
    producer = Producer(
        kind=ProducerKind.RUNNER, name=runner or "unknown", version=__version__
    )
    facts: dict[str, Any] = (
        {"phase": "evidence_unavailable"}
        if evidence is None
        else {
            "phase": evidence.terminal_type or "no_terminal_event",
            "detail": (
                f"steps={evidence.steps} tool_uses={evidence.tool_use_count} "
                f"report={'yes' if evidence.report else 'no'}"
            ),
        }
    )
    import_runner_evidence(
        store,
        EvidenceKind.RUNNER_PROGRESS,
        actor=_LAUNCH_ACTOR,
        producer=producer,
        facts=facts,
        run_id=stored.run_id,
        issue_num=stored.issue_num,
        item_id=stored.item_id,
    )


def _record_terminal_telemetry(
    stored: RunRecord,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    cost_floor_usd: float,
) -> None:
    """Write the run's derived telemetry onto *stored*, never below the floor.

    A cost-stopped run's agent can still write a summary after the stop that
    restates the run as nearly free; the row keeps the cost it was stopped at.
    """
    telemetry = _telemetry_from_evidence(stored.log_file)
    if telemetry.from_result_event:
        _apply_terminal_telemetry(stored, telemetry, model, overrides)
    if cost_floor_usd > stored.estimated_cost_usd:
        stored.estimated_cost_usd = cost_floor_usd


def _apply_terminal_telemetry(
    stored: RunRecord,
    telemetry: _SandboxTelemetry,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Overwrite *stored*'s cost/token/model columns with the run's
    authoritative result-event telemetry (#761), repricing the planned bound
    (#1092) from the same totals. Only ever called with telemetry that came
    from a real result event — see ``_finalize_sandbox_run``.

    Cost goes through ``_effective_cost_usd`` for the same reason the host path
    keeps ``DispatchResult.compute_cost``'s fallback: a runner that reports
    tokens but no provider-billed figure would otherwise finalize at $0.00 and
    silently discard the token-bound estimate the live path already wrote."""
    cost = _effective_cost_usd(telemetry, model, overrides)
    if cost is not None:
        stored.estimated_cost_usd = cost
    if model:
        stored.planned_cost_usd = _planned_bound_usd(telemetry, model, overrides)
    stored.input_tokens = telemetry.input_tokens
    stored.output_tokens = telemetry.output_tokens
    stored.cache_creation_tokens = telemetry.cache_creation_tokens
    stored.cache_read_tokens = telemetry.cache_read_tokens
    stored.total_tokens = (
        telemetry.input_tokens
        + telemetry.output_tokens
        + telemetry.cache_creation_tokens
        + telemetry.cache_read_tokens
    )
    if telemetry.reported_model:
        stored.reported_model = telemetry.reported_model
    if telemetry.reported_service_tier:
        stored.reported_service_tier = telemetry.reported_service_tier
    _mark_unpriced_accounting(stored, telemetry, model, overrides)


def host_producer() -> Producer:
    """The host itself as an evidence producer — kew's own code and version."""
    return Producer(
        kind=ProducerKind.HOST, name="kew.dispatch_sandbox", version=__version__
    )


def _artifact_recorder(store: RunStore) -> Callable[..., Any]:
    """The store's normalized artifact writer, or a refusal.

    ``RunStore`` is a Protocol; the artifact index arrived with I06 as a mixin
    on the SQLite implementation. A store without it cannot index the prompt,
    and an unindexed prompt is one no receipt can name — so this refuses rather
    than retaining bytes nothing points at.
    """
    recorder = getattr(store, "record_artifact", None)
    if not callable(recorder):
        raise LaunchEvidenceRefused("store cannot record a prompt artifact row")
    typed: Callable[..., Any] = recorder
    return typed


_LAUNCH_ACTOR = Actor("policy", "contained-dispatch")

_TRUSTED_BASE_FACT = TRUSTED_BASE_FACT

_HEX = frozenset("0123456789abcdef")


class LaunchEvidenceRefused(RuntimeError):
    """The launch evidence a contained run needs could not be recorded."""


TRUSTED_BASE_UNAVAILABLE = "unavailable"


def _trusted_base_observation(checkout_dir: Path) -> tuple[str, str]:
    """The commit the container will start from, as the HOST observed it.

    Read here, before the container exists, because after launch the checkout
    is agent-writable: a base read afterwards is a base the agent could have
    rewritten, and I06P/I07 bind a package and an artifact tree to this exact
    commit.

    Returns ``(value, source)``. An unreadable or non-SHA answer is recorded as
    an explicit :data:`TRUSTED_BASE_UNAVAILABLE` observation with an
    ``unobservable`` source — never as a plausible-looking sha, and never as
    silence. The consumer that needs a real base (I06P's package, I07's
    artifact tree) refuses on the typed gap; a dispatch that can still be
    described honestly is not this node's to stop.
    """
    sha = ""
    failed = True
    try:
        result = _run(["git", "rev-parse", "HEAD"], cwd=checkout_dir, check=False)
    except (OSError, ValueError):
        result = None  # no git on PATH, or a subprocess allowlist that refuses it
    if result is not None:
        sha = (result.stdout or "").strip()
        failed = result.returncode != 0
    if failed or len(sha) != 40 or not set(sha) <= _HEX:
        _warn(
            f"No trusted base commit observable in {checkout_dir} — recording it "
            "as unavailable; artifact lineage for this run will refuse."
        )
        return TRUSTED_BASE_UNAVAILABLE, "unobservable"
    return sha, "host_checkout"


def _placed_base_observation(checkout_dir: Path, placement: Placement) -> str:
    """The base a REMOTE placement extracts against, and ``""`` for a local one.

    The observation itself is :func:`_trusted_base_observation`'s and is taken
    before the agent exists for its reason: once a container starts, a local
    checkout is agent-writable, so a base read afterwards is a base the agent
    could have rewritten. What this adds is that the local path does not pay
    for it — a placement on this machine publishes from the checkout the agent
    wrote, with no artifact to bind to a base — so ``placement = local`` makes
    exactly the ``git`` calls it made before placement existed (#2526).

    An UNOBSERVABLE base refuses the placement, before admission, rather than
    travelling as the :data:`TRUSTED_BASE_UNAVAILABLE` marker. The instance
    extracts its workspace against this value and the artifact that comes back
    is checked against it, so a marker in its place would be a package nothing
    could bind to a commit — the lineage refusal the local path takes later,
    taken here instead, where nothing has been spent.
    """
    if placement.site is not None and placement.site.is_local:
        return ""
    from kew.dispatch_placement import DispatchPlacementRefused

    base, source = _trusted_base_observation(checkout_dir)
    if base == TRUSTED_BASE_UNAVAILABLE or source == "unobservable":
        raise DispatchPlacementRefused(
            f"this dispatch was not placed on {placement.site} because no "
            f"trusted base commit is observable in {checkout_dir}: an instance "
            "extracts its work against that commit, and an artifact kew cannot "
            "bind to one is not a tree it would publish"
        )
    return base


@contextmanager
def _held_placement(
    config: DispatchConfig,
    store: RunStore,
    *,
    checkout_dir: Path,
    runner: str,
    run_kind: str,
    transport: ResolvedInferenceTransport | None,
) -> Iterator[tuple[HeldPlacement, str]]:
    """Where this dispatch runs, held for the block, and the base it ships.

    Both are pre-admission refusals (:class:`DispatchPlacementRefused`), so
    both are taken here, ahead of the admission the caller enters next: the
    instance is claimed (:func:`kew.dispatch_placement.hold_contained_placement`)
    and an unobservable base releases that claim on its way out.
    """
    from kew.dispatch_placement import hold_contained_placement

    with hold_contained_placement(
        config, store, runner=runner, run_kind=run_kind, transport=transport
    ) as held:
        yield held, _placed_base_observation(checkout_dir, held.placement)


def protected_prompt_root(config: DispatchConfig) -> Path:
    """Where this project's exact compiled prompts are retained.

    Anchored on the LOG directory — the one host-owned directory kew already
    owns, where the durable record itself lives and which is never mounted into
    a container — rather than on the process's cwd. The protected artifact and
    the normalized row that indexes it then travel together: a record moved to
    another log dir does not leave its prompts orphaned behind a path nothing
    points at, and a second project (or a test) pointed at its own log dir gets
    its own protected tree instead of writing into the developer's checkout.

    The layout under that anchor is I06's
    (:func:`kew.sensitive_prompt.default_root`), unchanged: ``.kew/private/
    prompts``, owner-only, outside SQLite, never staged by a backup.
    """
    return default_root(Path(config.logging.dir).resolve())


def _capture_launch_evidence(
    store: RunStore | None,
    *,
    config: DispatchConfig,
    run_id: str,
    issue_num: int,
    item_id: str,
    repo: str | None,
    prompt: str,
    checkout_dir: Path,
) -> None:
    """Record the exact prompt and the trusted base BEFORE the provider runs.

    Two facts, both of which stop being knowable once the container starts:

    * the **exact compiled prompt** — retained by :mod:`kew.sensitive_prompt`
      as owner-only bytes outside SQLite, indexed by a normalized artifact row,
      and named in the ledger by digest only (I06). The prompt is written into
      the mounted checkout for the entrypoint to read, and the checkout is
      agent-writable, so the copy the host attests to must be taken from the
      string it compiled, not read back from the mount.
    * the **trusted base commit** the package, artifact tree and receipt of
      I06P/I07 are later bound to.

    Fail-closed: both are required evidence, so a store that cannot record them
    refuses the launch rather than running an attempt no receipt can describe.
    The caller finalizes the admitted row before re-raising, so a refusal never
    leaves a ``pending`` row pinning a node's reservation.
    """
    if store is None:
        raise LaunchEvidenceRefused("a contained dispatch requires a run store")
    now = datetime.now(UTC)
    base_sha, base_source = _trusted_base_observation(checkout_dir)
    try:
        retained = retain_prompt(
            protected_prompt_root(config),
            run_id=run_id,
            attempt_number=1,
            text=prompt,
            now=now,
        )
        _artifact_recorder(store)(
            run_id=run_id,
            attempt_number=1,
            kind=PROMPT_ARTIFACT_KIND,
            digest=retained.digest,
            byte_length=retained.byte_length,
            locator=retained.locator,
            created_at=now,
        )
    except (SensitivePromptError, ArtifactStoreError, OSError) as exc:
        raise LaunchEvidenceRefused(
            f"exact prompt not retained: {exception_text(exc)}"
        ) from exc
    _record_launch_facts(
        store,
        retained=retained,
        base_sha=base_sha,
        base_source=base_source,
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        now=now,
    )


def _record_launch_facts(
    store: RunStore,
    *,
    retained: RetainedPrompt,
    base_sha: str,
    base_source: str,
    run_id: str,
    issue_num: int,
    item_id: str,
    repo: str | None,
    now: datetime,
) -> None:
    """Append the two required launch evidence rows, or refuse the launch."""
    envelope: dict[str, Any] = {
        "run_id": run_id,
        "issue_num": issue_num,
        "item_id": item_id,
        "repo": repo,
        "now": now,
    }
    try:
        record_evidence(
            store,
            EvidenceKind.ARTIFACT,
            actor=_LAUNCH_ACTOR,
            producer=host_producer(),
            facts={
                **retained.artifact_facts(),
                "locator": retained.locator,
                "attempt_number": 1,
            },
            **envelope,
        )
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_LAUNCH_ACTOR,
            producer=host_producer(),
            facts={
                "fact": _TRUSTED_BASE_FACT,
                "value": base_sha,
                "source": base_source,
                "attempt_number": 1,
            },
            **envelope,
        )
    except EvidenceError as exc:
        raise LaunchEvidenceRefused(
            f"launch evidence was not recorded: {exception_text(exc)}"
        ) from exc


def _capture_launch_evidence_or_finalize(
    store: RunStore | None,
    finalize: Callable[[int, str], None],
    **kwargs: Any,
) -> None:
    """Capture launch evidence, finalizing the admitted row on a refusal.

    The row is written before this runs, so an unwinding refusal would
    otherwise leave a ``pending`` row pinning the node's reservation — the
    exact stuck-forever failure ``_finalize_sandbox_run`` exists to prevent.
    """
    try:
        _capture_launch_evidence(store, **kwargs)
    except LaunchEvidenceRefused:
        finalize(-1, "launch_evidence_refused")
        raise


def _require_checkout_generation_or_finalize(
    checkout_dir: Path,
    expected_generation: str | None,
    finalize: Callable[..., None],
    *,
    boundary: str,
) -> None:
    """Refuse and finalize when admission replaced a bound checkout."""
    if expected_generation is None:
        return
    from kew.dispatch_core import (
        _CheckoutIdentityRefused,
        _require_checkout_generation,
    )

    try:
        _require_checkout_generation(
            checkout_dir, expected_generation, boundary=boundary
        )
    except _CheckoutIdentityRefused:
        finalize(-1, "checkout_identity_changed", status="refused")
        raise


def _run_sandboxed(
    *,
    prompt: str,
    self_check: bool = False,
    prompt_profile_version_id: int | None = None,
    checkout_dir: Path,
    config: DispatchConfig,
    image: str,
    runner: str,
    issue_num: int,
    item_id: str,
    branch: str,
    title: str = "",
    model: str = "",
    store: RunStore | None = None,
    tracker: str = "github",
    worker_cap: int = 0,
    cap_exclude_issue: int | None = None,
    admit_budget_check: Callable[[], None] | None = None,
    run_id: str | None = None,
    publication_base_sha: str = "",
    epic_ref: str | None = None,
    checkout_generation: str | None = None,
    sidecar_ready: Callable[[], None] | None = None,
    on_admitted: Callable[[str], None] | None = None,
    on_launch: Callable[[], None] | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    cost_governor: ContainedCostGovernor | None = None,
) -> DispatchResult:
    """Run dispatch inside a container; broker git push + gh pr create from the host.

    Container mounts only checkout_dir. On exit code 0 the host runs
    `git push` and `gh pr create` so the agent never needs a GitHub token.

    ``worker_cap``/``cap_exclude_issue`` forward the #745 atomic worker-slot
    admission down to the ``pending``-row write (``_record_sandbox_start``).
    ``tracker`` names which work tracker ``item_id`` belongs to — the same
    dispatch-time value ``execute_dispatch`` validated the id against — so the
    admission identity records the id's namespace instead of re-deriving it
    from the id's shape (I01).

    ``image`` is the caller's runner-derived image (``SandboxDecision.image``,
    #1144). An empty one means the caller lost it somewhere between the
    decision and here: refuse rather than guess, because the only guess
    available is the claude image and that is the #1104 incident. ``runner`` is
    the resolved runner name behind that image; it rides down to the container
    args build (#1145) and to the credential env allowlist (#1146), so the
    flags, the credentials and the image all describe the same agent.

    ``cost_governor`` is the run's mid-flight cost authority (#1462): the
    admission gate only refuses new work, so without it a contained run that
    passed its cap mid-run ran to completion.
    """
    run_id = run_id or str(uuid.uuid4())
    if not image:
        raise SandboxImageUnmapped(
            "Sandbox: no container image resolved for this dispatch — refusing "
            "to launch (the sandbox decision carries the runner's image; see "
            "[sandbox] images in docs/configuration.md)."
        )
    # Probe result gates availability only; argv[0] must be the BARE
    # allowlisted runtime name, never the shutil.which() path (2026-07-16
    # argv/allowlist incident — second resolution site, missed by the
    # sandbox.py fix in #692).
    # These three sandbox-runtime shims are aliased imports in dispatch_core
    # (renamed from kew.sandbox), so mypy does not treat them as re-exported
    # attributes — but the dispatch/sandbox suites patch them at
    # kew.dispatch_core.*, so they must be reached through the module here.
    from kew.dispatch_core import _detect_sandbox_runtime  # type: ignore[attr-defined]

    resolved = _detect_sandbox_runtime(config.sandbox)
    runtime = config.sandbox.runtime if resolved is not None else None
    if runtime is None:
        # This path is only reached once a sandboxed launch was already decided
        # on, so a runtime that has since vanished is a hard failure — there is
        # no "downgrade to the host" here. `dangerously_skip_containment` is
        # resolved earlier, in resolve_effective_sandbox; by the time we are
        # launching a container, the posture question is settled.
        _warn(
            f"Sandbox runtime '{config.sandbox.runtime}' not found "
            "— sandbox unavailable"
        )
        return DispatchResult(
            exit_code=1,
            issue_num=issue_num,
            log_file=Path("/dev/null"),
            error_category="sandbox_runtime_missing",
        )

    # Prompt handoff (#695): write the prompt into the mounted checkout dir so
    # the container entrypoint feeds it to the agent on stdin. See
    # docs/sandbox-entrypoint-contract.md for the full contract.
    # Through the shared helper: the checkout is agent-writable, and a plain
    # write follows a symlink planted there out of the checkout entirely.
    write_prompt_handoff(checkout_dir, prompt)

    result = None
    try:
        result = _run_sandboxed_container(
            prompt=prompt,
            self_check=self_check,
            prompt_profile_version_id=prompt_profile_version_id,
            checkout_dir=checkout_dir,
            config=config,
            image=image,
            runner=runner,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch,
            title=title,
            model=model,
            store=store,
            tracker=tracker,
            worker_cap=worker_cap,
            cap_exclude_issue=cap_exclude_issue,
            admit_budget_check=admit_budget_check,
            run_id=run_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            publication_base_sha=publication_base_sha,
            epic_ref=epic_ref,
            checkout_generation=checkout_generation,
            sidecar_ready=sidecar_ready,
            on_admitted=on_admitted,
            on_launch=on_launch,
            cost_governor=cost_governor,
        )
        return result
    except Exception as exc:
        result = partial_dispatch_result(exc)
        raise
    finally:
        # Delete the mounted prompt handoff host-side on every exit path —
        # success, failure, or kill (#725) — so it never lands in a PR branch.
        # Tolerant of it already being gone, mirroring container teardown.
        with preserve_dispatch_result(result):
            remove_prompt_handoff(checkout_dir)


def build_agent_container_argv(
    config: DispatchConfig,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    inference_transport: ResolvedInferenceTransport | None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
) -> list[str]:
    """The container-run argv for one agent. THE argv-build site.

    The runner's own flags become the container command (``"$@"`` for the
    entrypoint, contract element 3): ``build_command()`` minus the binary and
    minus the prompt, which travels via the mounted file and never argv (#695).
    ``model`` is the effective (routed) model the caller resolved, so routing
    reaches the container and not just the run row (#761). An unknown runner
    raises out of ``get_runner`` rather than falling back to claude flags.

    ``runtime`` is passed as the BARE name from config, never a
    ``shutil.which()`` path: argv[0] goes through the subprocess allowlist,
    which matches names only, so a resolved ``/usr/local/bin/docker`` is
    rejected at launch. Enforcing that once is what killed the #1017 bug class
    — which is why every contained caller builds its argv here, rather than
    assembling its own and re-deriving the invariant.

    ``inference_transport`` comes from the caller because the two callers
    resolve it at different moments: the sandbox launcher resolves it inline,
    while ``run_tracked_agent`` has already resolved AND admitted one, and
    re-resolving there would discard that admission.
    """
    import kew.dispatch_core as dispatch_core

    agent_args = get_runner(runner).container_args(config=config, model=model)
    return dispatch_core._build_sandbox_argv(  # type: ignore[attr-defined]
        runtime=config.sandbox.runtime,
        image=image,
        checkout_dir=checkout_dir,
        slug=slug,
        network=config.sandbox.network,
        proxy_environment=config.sandbox.proxy_environment,
        runner=runner,
        inference_transport=inference_transport,
        command=agent_args,
        caps=resource_caps(config.sandbox),
        read_only_mounts=read_only_mounts,
    )


def _require_sidecar_packet_evidence(
    store: RunStore | None, *, worker_image_id: str, proxy_image_id: str
) -> SidecarPacketEvidence:
    if store is None:
        raise SwitchyardSupervisorError("sidecar packet evidence is unavailable")
    try:
        return matching_sidecar_packet_evidence(
            store, worker_image_id=worker_image_id, proxy_image_id=proxy_image_id
        )
    except SidecarPacketEvidenceUnavailable as exc:
        raise SwitchyardSupervisorError(exception_text(exc)) from exc


def _create_registered_sidecar(
    store: RunStore | None, run_id: str | None, supervisor: Any, bootstrap: list[str]
) -> str:
    if run_id is not None:
        _permit_container_creation(store, run_id)
        _permit_container_replacement(store, run_id, bootstrap[0])
        if store is not None:
            store.set_container_identity(run_id, supervisor.worker_name, None)
    handle = _CreatedContainerHandle(bootstrap[0], supervisor.worker_name)
    with _own_container_launch(handle):
        worker_id: str = supervisor.create_worker(
            bootstrap,
            on_identity=partial(
                _own_sidecar_identity, handle, store, run_id, supervisor
            ),
        )
        if store is not None and run_id is not None:
            _permit_container_creation(store, run_id)
        return worker_id


def _own_sidecar_identity(
    handle: _CreatedContainerHandle,
    store: RunStore | None,
    run_id: str | None,
    supervisor: Any,
    worker_id: str,
) -> None:
    """Hand the worker's full id to its cleanup before anything else can fail.

    A worker whose ``start`` then fails is still ``created``; only an id lets
    the unwind remove it, and an unconfirmed removal would also keep the
    supervisor from tearing down the proxy, network and volume (#2973).
    """
    handle.container_id = worker_id
    _register_sidecar_identity(store, run_id, supervisor, worker_id)


def _register_sidecar_identity(
    store: RunStore | None, run_id: str | None, supervisor: Any, worker_id: str
) -> None:
    """Commit the creation identity before startup, inspection, or readiness."""
    if store is None or run_id is None:
        return
    store.set_container_identity(run_id, supervisor.worker_name, worker_id)
    record = store.get(run_id)
    if record is not None and record.log_file is not None:
        record.log_file.parent.mkdir(parents=True, exist_ok=True)
        with record.log_file.with_suffix(".events.jsonl").open(
            "a", encoding="utf-8"
        ) as events:
            events.write(
                json.dumps(
                    {
                        "type": "container_identity",
                        "run_id": run_id,
                        "attempt_number": record.attempt_number,
                        "owner_pid": record.owner_pid,
                        "container_id": worker_id,
                        "container_name": supervisor.worker_name,
                    }
                )
                + "\n"
            )
            events.flush()
    _permit_container_creation(store, run_id)


def prepare_sidecar_worker(
    config: DispatchConfig,
    *,
    supervisor: Any,
    image_id: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    inference_transport: ResolvedInferenceTransport,
    store: RunStore | None = None,
    packet_evidence: SidecarPacketEvidence | None = None,
    agent_command: list[str] | None = None,
    run_id: str | None = None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
) -> ContainedExecution:
    """Create a sleeping worker, verify NF-1, then return its agent command."""
    if not inference_transport.sidecar or inference_transport.endpoint is None:
        raise ValueError("sidecar worker requires one verified transport")
    credential_name = inference_transport.worker_credential_env_name
    if credential_name is None:
        raise ValueError("sidecar worker has no synthetic client identity")
    worker_config = config.model_copy(deep=True)
    worker_config.inference_transport.switchyard.endpoint = inference_transport.endpoint
    agent_args = agent_command or get_runner(runner).container_args(
        config=worker_config, model=model
    )
    environment = supervisor.worker_environment(credential_name)
    if runner == "claude-code":
        environment["ANTHROPIC_BASE_URL"] = inference_transport.endpoint
    bootstrap = build_run_argv(
        runtime=config.sandbox.runtime,
        image=image_id,
        checkout_dir=checkout_dir,
        slug=slug,
        network=supervisor.network_name,
        runner=runner,
        inference_transport=inference_transport,
        entrypoint="/bin/sleep",
        command=["infinity"],
        operation="create",
        container_name=supervisor.worker_name,
        owner=supervisor.owner,
        credential_environment=environment,
        proxy_environment=config.sandbox.proxy_environment,
        caps=resource_caps(config.sandbox),
        read_only_mounts=read_only_mounts,
    )
    worker_id = _create_registered_sidecar(store, run_id, supervisor, bootstrap)
    handle = _CreatedContainerHandle(
        config.sandbox.runtime, supervisor.worker_name, worker_id
    )
    with _own_container_launch(handle):
        endpoint = inference_transport.endpoint.removeprefix("http://")
        proxy_address, separator, raw_port = endpoint.rpartition(":")
        if not separator or not raw_port.isdigit():
            raise ValueError("sidecar worker endpoint is malformed")
        filter_evidence = install_worker_netns_filter(
            runtime=config.sandbox.runtime,
            image_id=image_id,
            worker_id=worker_id,
            owner=supervisor.owner,
            proxy_address=proxy_address,
            proxy_port=int(raw_port),
            on_helper_created=supervisor.note_filter_helper,
            on_helper_removed=supervisor.note_filter_helper_removed,
            proxy_environment=config.sandbox.proxy_environment,
        )
        supervisor.note_filter_helper(filter_evidence.helper_id)
        supervisor.note_filter_helper_removed(filter_evidence.helper_id)
        supervisor.note_filter_evidence(
            worker_id=filter_evidence.worker_id,
            helper_id=filter_evidence.helper_id,
            image_id=filter_evidence.image_id,
            readback=filter_evidence.readback,
        )
        if packet_evidence is None:
            packet_evidence = _require_sidecar_packet_evidence(
                store,
                worker_image_id=image_id,
                proxy_image_id=supervisor.proxy_image_id,
            )
        supervisor.note_packet_evidence(packet_evidence)
        return ContainedExecution(
            argv=tuple(
                build_worker_agent_exec_argv(
                    runtime=config.sandbox.runtime,
                    worker_id=worker_id,
                    command=agent_args,
                )
            ),
            name=supervisor.worker_name,
            runtime=config.sandbox.runtime,
            checkout_dir=checkout_dir,
            container_id=worker_id,
            prepared=True,
        )


def _run_sidecar_container_and_stream(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    run_id: str,
    model: str,
    log_file: Path,
    run_kind: str,
    result: DispatchResult,
    transport: ResolvedInferenceTransport,
    sidecar_ready: Callable[[ResolvedInferenceTransport, Any], None] | None,
    overrides: dict[str, tuple[float, float]] | None,
    admission: PendingAdmission,
    on_launch: Callable[[], None] | None = None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
    cost_governor: ContainedCostGovernor | None = None,
) -> int:
    with (
        execution_cancellation(admission),
        preserve_dispatch_result(result),
    ):
        supervisor = None
        try:
            try:
                with _owned_sidecar_context(
                    sidecar_switchyard_context(config, transport, worker_image_id=image)
                ) as supervisor:
                    bound = replace(
                        transport,
                        endpoint=supervisor.endpoint,
                        config_sha256=supervisor.config_sha256,
                    )
                    result_code = run_container_and_stream(
                        config,
                        store,
                        image=image,
                        runner=runner,
                        checkout_dir=checkout_dir,
                        slug=slug,
                        run_id=run_id,
                        model=model,
                        log_file=log_file,
                        run_kind=run_kind,
                        result=result,
                        inference_transport=bound,
                        sidecar_ready=sidecar_ready,
                        _sidecar_supervisor=supervisor,
                        _admission=admission,
                        on_launch=on_launch,
                        read_only_mounts=read_only_mounts,
                        cost_governor=cost_governor,
                    )
            except BaseException:
                try:
                    _record_terminal_sidecar(store, run_id, supervisor)
                except BaseException:
                    _logger.debug(
                        "Sidecar terminal evidence deferred during launch unwind"
                    )
                raise
            else:
                _record_terminal_sidecar(store, run_id, supervisor)
            return result_code
        except (SwitchyardSupervisorError, WorkerNetnsFilterError) as exc:
            if container_launch_failure(exc) is not None:
                raise
            _finalize_sandbox_run(
                store,
                run_id,
                exit_code=-1,
                error_category="sidecar_lifecycle_error",
                model=model,
                overrides=overrides,
            )
            raise


def _record_terminal_sidecar(
    store: RunStore | None, run_id: str, supervisor: Any
) -> None:
    profile = store.get_route_profile(run_id, 1) if store is not None else None
    if store is not None and profile is not None and supervisor is not None:
        record = store.get(run_id)
        append_sidecar_evidence(
            store,
            supervisor.topology_evidence(
                run_id=run_id,
                attempt_number=1,
                profile_fingerprint=profile.fingerprint,
                stage="terminal",
            ),
            actor=Actor("policy", "sidecar-supervisor"),
            issue_num=None if record is None else record.issue_num,
            item_id=None if record is None else record.item_id,
        )


@contextmanager
def _owned_sidecar_context(context: Any) -> Iterator[Any]:
    supervisor = context.__enter__()
    try:
        yield supervisor
    except BaseException as exc:
        evidence = container_launch_failure(exc)
        if evidence is None or evidence.cleanup_confirmed:
            try:
                context.__exit__(type(exc), exc, exc.__traceback__)
            except BaseException:
                exc.container_launch_failure = (  # type: ignore[attr-defined]
                    replace(evidence, cleanup_confirmed=False)
                    if evidence is not None
                    else ContainerLaunchFailure(False)
                )
                _logger.debug(
                    "Sidecar cleanup remains unconfirmed during launch unwind"
                )
        raise
    else:
        context.__exit__(None, None, None)


def _permit_container_creation(store: RunStore | None, run_id: str) -> None:
    from kew.dispatch_core import _fleet_or_run_stopped

    if store is None:
        return
    record = store.get(run_id)
    if (
        record is None
        or record.status != "pending"
        or _fleet_or_run_stopped(store, record)
    ):
        raise RuntimeError("container launch refused after stop")


def _permit_container_replacement(
    store: RunStore | None, run_id: str, runtime: str
) -> None:
    """A retry cannot discard an earlier target whose death remains unproven."""
    from kew.stop_recovery import container_target, record_container_state

    if store is None:
        return
    previous = store.get(run_id)
    if previous is None or container_target(previous) is None:
        return
    if record_container_state(runtime, previous) not in {"absent", "stopped"}:
        store.request_run_stop(run_id)
        raise RuntimeError("previous container death unproven; retaining identity")


def _container_owned_elsewhere(
    store: RunStore, run_id: str, name: str, container_id: str
) -> bool:
    """A pending run other than *run_id* that names this container owns it.

    A pending row with the name and no id yet is mid-creation, so it owns
    whatever holds the name until it records which container is its own.
    """
    return any(
        row.run_id != run_id
        and (
            row.container_id == container_id
            or (row.container_name == name and row.container_id is None)
        )
        for row in store.list_pending()
    )


STALE_REMOVAL_INTENDED = "stale_container_removal_intended"
STALE_REMOVED = "stale_container_removed"


def _stale_removal_key(event: str, container_id: str) -> str:
    return f"{event}:{container_id}"


def _labelled_create_argv(argv: list[str], store: RunStore, run_id: str) -> list[str]:
    """Stamp the container with the ledger and run creating it (#2973).

    ``kew-<item>`` names are host-wide, so a leftover is only ever traced to
    an owner through these labels. A legacy ledger with no identity labels
    nothing, and so can never reclaim anything.
    """
    ledger = store.read_ledger_id()
    if ledger is None:
        return argv
    labels = [
        "--label",
        f"{KEW_LEDGER_LABEL}={ledger}",
        "--label",
        f"{KEW_RUN_LABEL}={run_id}",
    ]
    return [*argv[:2], *labels, *argv[2:]]


def _proven_dead_here(
    store: RunStore, run_id: str, labels: Mapping[str, str]
) -> str | None:
    """The run that left this container, when this ledger proves it dead.

    The labels must name this ledger and a run it recorded that is no longer
    pending (or this run's own earlier attempt, which
    :func:`_permit_container_replacement` already proved dead). A container
    without labels, or another ledger's, belongs to someone kew cannot see.
    """
    ledger = store.read_ledger_id()
    owner = labels.get(KEW_RUN_LABEL)
    if ledger is None or labels.get(KEW_LEDGER_LABEL) != ledger or not owner:
        return None
    record = store.get(owner)
    if record is None or (owner != run_id and record.status == "pending"):
        return None
    return owner


def _remove_stale_created_container(
    store: RunStore, run_id: str, runtime: str, name: str
) -> bool:
    """Free the item's container name from a never-started leftover (#2973).

    A container left ``created`` keeps ``kew-<item>``, and every later create
    of that name is refused. Only a never-started one that this ledger proves
    is its own dead run's is removed; a running one fails the state read and
    is never touched. The intent is recorded before the removal, so a removal
    whose audit is lost is completed by :func:`_complete_stale_removals`.
    """
    stale = read_never_started_container(runtime, name)
    if stale is None:
        return False
    stale_id = stale.container_id
    owner = _proven_dead_here(store, run_id, stale.labels)
    if owner is None or _container_owned_elsewhere(store, run_id, name, stale_id):
        return False
    daemon = runtime_daemon_identity(runtime)
    if daemon is None and not _finished_run(store, owner):
        # This run's own earlier attempt is reclaimed only on a named daemon;
        # its committed create is removed by the id its row records instead.
        _warn(f"Sandbox: stale container {name!r} kept: {runtime} daemon unnamed")
        return False
    # A finished run's leftover goes by its id even when a loaded daemon cannot
    # be named (#3315): its intent then names no daemon, so only the removal
    # audited right after ``rm`` records it.
    audit = _stale_removal_audit(store, run_id, runtime, name, stale_id, daemon)
    return _audited_removal(store, runtime, audit)


def _finished_run(store: RunStore, run_id: str) -> bool:
    """The ledger records *run_id* as finished: nothing of it can still start."""
    record = store.get(run_id)
    return record is not None and record.status != "pending"


def _stale_removal_audit(
    store: RunStore,
    run_id: str,
    runtime: str,
    name: str,
    container_id: str,
    daemon: str | None,
) -> dict[str, Any]:
    """The audit fields shared by a removal's intent and its record."""
    record = store.get(run_id)
    return {
        "run_id": run_id,
        "issue_num": None if record is None else record.issue_num,
        "item_id": None if record is None else record.item_id,
        "actor_type": "policy",
        "actor_id": "container-launch",
        "payload": {
            "container_name": name,
            "container_id": container_id,
            "state": "created",
            "runtime": Path(runtime).name,
            "daemon": daemon,
        },
    }


def _audited_removal(store: RunStore, runtime: str, audit: dict[str, Any]) -> bool:
    """Record the intent, remove the never-started container by id, record it.

    Nothing is removed under an intent that could not be recorded. A removal
    whose own record is lost is completed by :func:`_complete_stale_removals`
    once the daemon its intent names confirms the id gone; an intent that names
    no daemon stays open.
    """
    name = audit["payload"]["container_name"]
    try:
        _append_stale_removal(store, STALE_REMOVAL_INTENDED, audit)
    except Exception as exc:
        _warn(f"Sandbox: container {name!r} kept: {exception_text(exc)}")
        return False
    if not remove_never_started_container(runtime, audit["payload"]["container_id"]):
        return False
    try:
        _append_stale_removal(store, STALE_REMOVED, audit)
    except Exception as exc:
        _warn(
            f"Sandbox: removal of container {name!r} awaits its audit: "
            f"{exception_text(exc)}"
        )
    return True


def _append_stale_removal(store: RunStore, event: str, audit: dict[str, Any]) -> None:
    store.append_audit_event_once(
        dedup_key=_stale_removal_key(event, audit["payload"]["container_id"]),
        timestamp=datetime.now(UTC).isoformat(),
        event=event,
        **audit,
    )


def _complete_stale_removals(store: RunStore, runtime: str) -> None:
    """Record the removal of every intended stale container now gone.

    An intent without its removal is a reclaim interrupted between ``rm`` and
    its audit. The removal is recorded once the runtime answers that the
    container no longer exists; one still present is left for its next reclaim.
    Absence is only read from the runtime and daemon the intent names: another
    daemon never held that id, so its "no such container" proves nothing.
    """
    done: set[str] = set()
    outstanding = _outstanding_stale_removals(store, done)
    if not outstanding:
        return
    daemon = runtime_daemon_identity(runtime)
    for intent, payload in outstanding:
        stale_id = payload["container_id"]
        if stale_id in done or not _absent_where_intended(runtime, daemon, payload):
            continue
        audit = {
            "run_id": intent.run_id,
            "issue_num": intent.issue_num,
            "item_id": intent.item_id,
            "actor_type": intent.actor_type,
            "actor_id": intent.actor_id,
            "payload": payload,
        }
        _append_stale_removal(store, STALE_REMOVED, audit)
        done.add(stale_id)


def _outstanding_stale_removals(
    store: RunStore, done: set[str]
) -> list[tuple[Any, dict[str, Any]]]:
    """Each intent with no recorded removal; *done* gets the recorded ids."""
    done.update(
        event.payload["container_id"]
        for event in store.list_audit_events(None, event=STALE_REMOVED)
        if isinstance(event.payload, dict)
        and isinstance(event.payload.get("container_id"), str)
    )
    return [
        (intent, intent.payload)
        for intent in store.list_audit_events(None, event=STALE_REMOVAL_INTENDED)
        if isinstance(intent.payload, dict)
        and isinstance(intent.payload.get("container_id"), str)
        and intent.payload["container_id"] not in done
    ]


def _absent_where_intended(
    runtime: str, daemon: str | None, payload: Mapping[str, Any]
) -> bool:
    """The runtime and daemon the intent names answer that its id is gone."""
    return (
        daemon is not None
        and payload.get("runtime") == Path(runtime).name
        and payload.get("daemon") == daemon
        and container_absent(runtime, payload["container_id"])
    )


def _create_freeing_stale_name(
    argv: list[str],
    store: RunStore,
    run_id: str,
    name: str,
    credential: BoundedRunKey | None,
    *,
    deadline: float,
) -> str:
    """Create once more only after a refused name was freed from a leftover.

    A create that times out is retried, after whatever it may have left is
    cleared, before the run is failed as :class:`ContainerCreateTimedOut`.
    The retries also stop at *deadline* (#3186), a ceiling per container
    attempt on :data:`launch_clock` — by default ``WALL_CLOCK_LIMIT_SECONDS``
    from this attempt's launch, not the run's configured timeout — and no
    retry starts that could end past it.
    """
    try:
        _complete_stale_removals(store, argv[0])
    except Exception as exc:
        # Before the create nothing names a container of this run, so a
        # failure here must not reach the launch unwind's stop by name.
        _warn(f"Sandbox: stale container audit deferred: {exception_text(exc)}")
    argv = _labelled_create_argv(argv, store, run_id)
    for attempt in range(1, CREATE_TIMEOUT_ATTEMPTS + 1):
        try:
            return _create_once_freeing_stale_name(
                argv, store, run_id, name, credential
            )
        except subprocess.TimeoutExpired as exc:
            backoff = CREATE_TIMEOUT_BACKOFF_SECONDS * 2 ** (attempt - 1)
            if not _create_retry_fits(
                attempt, backoff + exc.timeout, deadline
            ) or not _clear_timed_out_create(store, run_id, argv, name):
                raise ContainerCreateTimedOut(name, attempt, exc.timeout) from exc
            _record_create_retry(store, run_id, name, attempt, exc)
            launch_wait(backoff)
    raise AssertionError("unreachable: the last create attempt returns or raises")


def _create_retry_fits(attempt: int, cost: float, deadline: float) -> bool:
    """May another create follow attempt *attempt*?

    Only while attempts remain and one more full attempt — the backoff wait and
    a create that again runs to its timeout, together *cost* — still ends
    by the attempt's *deadline*.
    """
    return attempt < CREATE_TIMEOUT_ATTEMPTS and launch_clock() + cost <= deadline


def run_deadline(seconds: float = WALL_CLOCK_LIMIT_SECONDS) -> float:
    """The create-retry ceiling for a container attempt launching now (#3186):
    *seconds* (``WALL_CLOCK_LIMIT_SECONDS``, the container's own wall-clock
    limit) on :data:`launch_clock` from this attempt's launch. Per attempt, and
    not the run's configured timeout."""
    return launch_clock() + seconds


#: How many times one launch tries a ``create`` the runtime did not answer in
#: time. A loaded host is slow to create, not wrong: the next attempt usually
#: lands, and a failure here is infrastructure, not the run (#3103).
CREATE_TIMEOUT_ATTEMPTS = 3
#: The first wait before a timed-out create is tried again; it doubles each time.
CREATE_TIMEOUT_BACKOFF_SECONDS = 2.0
CREATE_RETRIED = "container_create_retried"
#: The clock a container attempt's create-retry ceiling is read on, and the wait between
#: create retries (#3186). Process boundaries, named once, so a test pins the
#: clock instead of sitting out a backoff.
launch_clock: Callable[[], float] = time.monotonic
launch_wait: Callable[[float], None] = time.sleep


def _create_once_freeing_stale_name(
    argv: list[str],
    store: RunStore,
    run_id: str,
    name: str,
    credential: BoundedRunKey | None,
) -> str:
    """Create, and create once more only after the name was freed.

    A failed create whose container this run's own create committed (#3007)
    is not a conflict with anyone: that container is removed by its id and the
    create runs again (#3246). Only a container from another run goes down the
    stale-name path, which removes it only when its run is proven dead.
    """
    from kew.sandbox import create_registered_container

    try:
        return create_registered_container(argv, credential=credential)
    except ContainerStartFailed as exc:
        if not _removed_own_committed_create(store, run_id, argv, name, exc) and not (
            exc.name_conflict
            and _remove_stale_created_container(store, run_id, argv[0], name)
        ):
            raise
    return create_registered_container(argv, credential=credential)


def _removed_own_committed_create(
    store: RunStore,
    run_id: str,
    argv: list[str],
    name: str,
    exc: ContainerStartFailed,
) -> bool:
    """Remove a never-started container this run's own create left (#3246).

    Ownership is the ledger and run labels, and the id is persisted on the
    row before anything is removed under it (:func:`_adopt_ambiguous_create`).
    Nothing is removed by name. A removal that does not land raises a create
    failure that is not a name refusal, so the launch unwind still owns the
    adopted container rather than certifying the create clean.
    """
    from kew.sandbox import _argv_cidfile

    runtime = argv[0]
    adopted = _adopt_ambiguous_create(store, run_id, runtime, name)
    if adopted is None:
        return False
    if not _removed_adopted_id(store, run_id, runtime, name, adopted):
        raise ContainerStartFailed(
            name, "the container this run's create left could not be removed"
        ) from exc
    _warn(
        f"Sandbox: removed container {adopted[:12]} a failed create of {name!r} "
        "left under this run; creating once more"
    )
    cidfile = _argv_cidfile(argv)
    if cidfile is not None:
        cidfile.unlink(missing_ok=True)
    return True


def _removed_adopted_id(
    store: RunStore, run_id: str, runtime: str, name: str, adopted: str
) -> bool:
    """Remove this run's own adopted create by its id, audited, and forget it.

    The audit names the daemon this run recorded before its create. Once the
    container is gone the row's id is cleared, so a crash before the next
    create returns leaves recovery the cidfile (#3315), never the removed id.
    """
    daemon = store.get_meta(container_daemon_key(run_id))
    audit = _stale_removal_audit(store, run_id, runtime, name, adopted, daemon)
    if not _audited_removal(store, runtime, audit):
        return False
    store.set_container_identity(run_id, name, None)
    return True


def _clear_timed_out_create(
    store: RunStore, run_id: str, argv: list[str], name: str
) -> bool:
    """Free the name and cidfile a timed-out create may have taken (#3103).

    The daemon may have made the container after its client was killed. This
    run's own never-started container is removed through the stale-container
    path, or, when that refuses, by the id its labels prove; otherwise the
    runtime must answer that the name is free. Anything else (a running
    holder, an unreadable daemon) is left alone and no retry follows. The
    client may have left its cidfile, which the next create would refuse, so
    it goes once nothing holds the name.
    """
    from kew.sandbox import _argv_cidfile

    runtime = argv[0]
    if not _remove_stale_created_container(store, run_id, runtime, name):
        # A daemon too loaded to name refuses the audited reclaim; this run's
        # own create is still removed, by the id its labels prove (#3246).
        adopted = _adopt_ambiguous_create(store, run_id, runtime, name)
        freed = (
            _removed_adopted_id(store, run_id, runtime, name, adopted)
            if adopted is not None
            else container_absent(runtime, name)
        )
        if not freed:
            return False
    cidfile = _argv_cidfile(argv)
    if cidfile is not None:
        cidfile.unlink(missing_ok=True)
    return True


def _record_create_retry(
    store: RunStore,
    run_id: str,
    name: str,
    attempt: int,
    exc: subprocess.TimeoutExpired,
) -> None:
    """Leave a trace of each create retried, so a slow host shows up (#3103)."""
    _warn(
        f"Sandbox: create of container {name!r} timed out after {exc.timeout:g}s "
        f"(attempt {attempt} of {CREATE_TIMEOUT_ATTEMPTS}); retrying"
    )
    record = store.get(run_id)
    try:
        store.append_audit_event_once(
            dedup_key=f"{CREATE_RETRIED}:{run_id}:{attempt}",
            timestamp=datetime.now(UTC).isoformat(),
            event=CREATE_RETRIED,
            run_id=run_id,
            issue_num=None if record is None else record.issue_num,
            item_id=None if record is None else record.item_id,
            actor_type="policy",
            actor_id="container-launch",
            payload={
                "container_name": name,
                "attempt": attempt,
                "timeout_seconds": exc.timeout,
            },
        )
    except Exception as audit_exc:
        _warn(f"Sandbox: create retry unrecorded: {exception_text(audit_exc)}")


def _record_container_daemon(store: RunStore, run_id: str, runtime: str) -> None:
    """Record which daemon this run's container is created on (#3124 L1).

    A container id means something only on its own daemon, so recovery
    trusts "no such container" only from the daemon recorded here
    (:func:`kew.stop_recovery.absence_refusal`). Recorded before the create,
    so a container the create commits is always covered. A daemon that cannot
    be named records nothing, and recovery treats the run as it treats a
    legacy one.
    """
    try:
        daemon = runtime_daemon_identity(runtime)
        if daemon is not None:
            store.set_meta(container_daemon_key(run_id), daemon)
    except Exception as exc:
        _warn(f"Sandbox: container daemon unrecorded: {exception_text(exc)}")


def prepare_registered_container(
    argv: list[str],
    store: RunStore | None,
    run_id: str,
    *,
    credential: BoundedRunKey | None = None,
    deadline: float | None = None,
) -> ContainedExecution:
    """Persist creation intent, then the full id before permitting execution.

    *deadline* is the create-retry ceiling for this container attempt on
    :data:`launch_clock` (:func:`run_deadline`); a caller with none starts
    the ceiling here.
    """
    from kew.sandbox import _argv_cidfile

    execution = ContainedExecution.from_argv(argv)
    if store is None:
        return execution
    _permit_container_creation(store, run_id)
    _permit_container_replacement(store, run_id, argv[0])
    _record_container_daemon(store, run_id, execution.runtime)
    cidfile = _argv_cidfile(argv)
    store.set_container_identity(
        run_id, execution.name, None, None if cidfile is None else str(cidfile)
    )
    handle = _CreatedContainerHandle(execution.runtime, execution.name, cidfile=cidfile)
    with _own_container_launch(handle):
        try:
            identity = _create_freeing_stale_name(
                argv,
                store,
                run_id,
                execution.name,
                credential,
                deadline=run_deadline() if deadline is None else deadline,
            )
        except ContainerStartFailed as exc:
            if not exc.name_conflict:
                handle.adopted_id = _adopt_ambiguous_create(
                    store, run_id, execution.runtime, execution.name
                )
            raise
        handle.container_id = identity
        store.set_container_identity(run_id, execution.name, identity)
        _permit_container_creation(store, run_id)
        return replace(
            execution,
            argv=(execution.runtime, "start", "--attach", "--interactive", identity),
            container_id=identity,
            prepared=True,
        )


def _register_sandbox_client(
    store: RunStore | None,
    run_id: str,
    log_file: Path,
    proc: subprocess.Popen[str],
) -> None:
    """Persist container and process identities before the same startup gate opens."""
    from kew.dispatch_core import _record_retry_pgid
    from kew.orchestrator import _announce_spawn

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.with_suffix(".events.jsonl").open("a", encoding="utf-8") as events:
        if store is not None:
            record = store.get(run_id)
            if record is None or record.container_id is None:
                raise RuntimeError(
                    "container identity unavailable at client registration"
                )
            events.write(
                json.dumps(
                    {
                        "type": "container_identity",
                        "pid": proc.pid,
                        "container_id": record.container_id,
                        "container_name": record.container_name,
                    }
                )
                + "\n"
            )
            events.flush()
        register = (
            partial(_record_retry_pgid, store, run_id) if store is not None else None
        )
        _announce_spawn(proc, events, register)


def _prepare_owned_container(
    handle: _CreatedContainerHandle,
    argv: list[str],
    store: RunStore | None,
    run_id: str,
    credential: BoundedRunKey | None = None,
    deadline: float | None = None,
) -> ContainedExecution:
    """Own the created container and recheck the lifetime creation consumed."""
    execution = prepare_registered_container(
        argv, store, run_id, credential=credential, deadline=deadline
    )
    handle.container_id = execution.container_id
    handle.owned = True
    require_credential_lifetime(credential, WALL_CLOCK_LIMIT_SECONDS)
    return execution


def _launch_openshell_sandbox(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    run_id: str,
    log_file: Path,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    admission: PendingAdmission | None,
    credential: BoundedRunKey | None,
    transport: ResolvedInferenceTransport | None,
    sidecar_supervisor: Any,
    run_kind: str,
) -> Any:
    """Launch one attempt into an OpenShell sandbox (epic node N6).

    The gateway owns create / upload / exec / delete, so there is no argv to
    build here and no daemon to register a container id with; everything the
    stream consumer then does is the docker path's, because the handle keeps
    ``ContainerHandle``'s contract. The creation permit is rechecked the same
    way — before the create, and again once the sandbox exists and before the
    agent is exec'd into it, because create / id read / whole-checkout upload
    is exactly the window the docker path's second check covers.

    Neither Switchyard topology is launched here. A sidecar one is refused for
    the reason :data:`~kew.openshell_launch.SIDECAR_LAUNCH_REFUSAL` states, and
    a sidecar-less one for the reason
    :data:`~kew.openshell_launch.SWITCHYARD_LAUNCH_REFUSAL` states: the gateway
    exec carries no endpoint override and the packaged egress policy permits
    only the runner's upstream host, so the transport that was attested would
    not be the one the agent used — it would talk straight past Switchyard to
    the upstream. Admission refuses both first
    (:func:`kew.sandbox.resolve_transport_sandbox`), so reaching either raise
    means a caller resolved containment some other way; they are kept because
    neither a docker command line nor an undeliverable endpoint may reach the
    gateway on any path, and each raise reads the same sentence as its
    admission refusal.

    ``separate_stderr`` is not threaded through: ``openshell sandbox exec``
    gives one merged stream, so the second pipe a governed review wants
    (#2053) does not exist on this runtime. Since epic node N12 routes the
    adversarial reviewer here, that is a LIVE limitation rather than a note
    about a shape the pilot avoids: a review on this runtime reads its verdict
    off the same pipe the container's stderr lands on, so a runtime warning
    from the reviewed code can fail a genuine round under the contract's
    "any line that is not a typed event" rule. It fails the round CLOSED — a
    forged or stray line is never read as a verdict — and the remedy is the
    gateway growing a second pipe, not anything this module can thread.

    ``run_kind`` decides which packaged policy the sandbox is created under
    (epic node N12). An adversarial review is a different boundary from a
    dispatch on the same image — it executes the reviewed PR's own tests — so
    it is created under the reviewer policy and certified against that policy's
    digest. Derived here from the kind the caller already threads rather than
    taken as a policy path, so the launch and the certification gate cannot
    name two different files.
    """
    from kew.inference_transport import direct_route
    from kew.openshell_launch import (
        SIDECAR_LAUNCH_REFUSAL,
        SWITCHYARD_LAUNCH_REFUSAL,
        OpenShellLaunchRefused,
        launch_openshell_container,
        policy_flow_for_run_kind,
    )

    if sidecar_supervisor is not None:
        raise OpenShellLaunchRefused(SIDECAR_LAUNCH_REFUSAL)
    if not direct_route(transport):
        raise OpenShellLaunchRefused(SWITCHYARD_LAUNCH_REFUSAL)
    _permit_container_creation(store, run_id)
    try:
        return launch_openshell_container(
            config,
            store,
            run_id=run_id,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            admission=admission,
            credential=credential,
            on_spawn=partial(_register_openshell_client, store, run_id, log_file),
            permit=partial(_permit_container_creation, store, run_id),
            policy_flow=policy_flow_for_run_kind(run_kind),
        )
    except BaseException as exc:
        _record_unreleased_sandbox(exc)
        raise


def _record_unreleased_sandbox(exc: BaseException) -> None:
    """Turn a launch unwind's unconfirmed sandbox delete into cleanup evidence.

    ``kew.openshell_launch`` cannot build :class:`ContainerLaunchFailure` (this
    module imports it, not the other way round), so it names the sandbox on
    the error and the evidence is attached here. Unconfirmed evidence is what
    keeps the run pending instead of finalizing it ``sandbox_launch_error``:
    a terminal run is one no recovery comes back for, and the sandbox may still
    hold the run's provider (#2625).
    """
    from kew.openshell_launch import UNRELEASED_SANDBOX_ATTR
    from kew.runtime_names import OPENSHELL_RUNTIME

    name = getattr(exc, UNRELEASED_SANDBOX_ATTR, None)
    if isinstance(name, str) and name:
        _record_container_cleanup(
            exc, container_launch_failure(exc), (OPENSHELL_RUNTIME, name), False
        )


class OpenShellSandboxUnreleased(RuntimeError):
    """A finished OpenShell run whose sandbox delete could not be confirmed."""


def _register_openshell_client(
    store: RunStore | None,
    run_id: str,
    log_file: Path,
    proc: subprocess.Popen[str],
    name: str,
    identity: str,
) -> None:
    """Persist the sandbox identity and the client pid before startup.

    The docker counterpart (``_register_sandbox_client``) REQUIRES a container
    id on the row and raises without one; here the row carries the sandbox
    name and no id by design (``openshell_launch._record_identity``), so the
    event carries the gateway's own id instead of a column that will never be
    filled. ``_check_spawn_permit`` knows that about the gateway runtime;
    before it did, the id it demanded of every runtime refused this
    registration on every store-backed launch (PR #2401 review).

    Everything after that — the retry pgid and the spawn announcement — is the
    same bookkeeping, because the client process is an ordinary child whichever
    runtime it talks to. Including its group: ``OpenShellClient.sandbox_exec``
    starts it with ``start_new_session=True``, so the pid recorded here as
    ``attempt_pgid`` is a group a stop holding only the run row can signal.
    """
    from kew.dispatch_core import _record_retry_pgid
    from kew.orchestrator import _announce_spawn

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.with_suffix(".events.jsonl").open("a", encoding="utf-8") as events:
        events.write(
            json.dumps(
                {
                    "type": "container_identity",
                    "pid": proc.pid,
                    "runtime": OPENSHELL_RUNTIME,
                    "sandbox_id": identity,
                    "container_name": name,
                }
            )
            + "\n"
        )
        events.flush()
        register = (
            partial(_record_retry_pgid, store, run_id) if store is not None else None
        )
        _announce_spawn(proc, events, register)


def _launch_streaming_container(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    run_id: str,
    log_file: Path,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    transport: ResolvedInferenceTransport,
    sidecar_supervisor: Any,
    sidecar_ready: Callable[[ResolvedInferenceTransport, Any], None] | None,
    admission: PendingAdmission | None = None,
    credential: BoundedRunKey | None = None,
    separate_stderr: bool = False,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
    run_kind: str,
) -> Any:
    """Launch one attempt's container, on whichever runtime is configured.

    ``run_kind`` has no default on purpose. It is what decides the OpenShell
    policy a sandbox is created under (epic node N12), and the default any
    absent value would take is the DISPATCH boundary — so a reviewer launch
    that forgot to pass it would run under a policy its certification was never
    bound to, which is the one mismatch the flow split exists to prevent.
    """
    import kew.dispatch_core as dispatch_core
    from kew.openshell_launch import is_openshell
    from kew.runners.registry import preflight_runner_workspace

    # The contained lane's workspace admission, and the host lane's rule
    # (``orchestrator._preflight_runner_workspace``) asked of the same
    # function: a tree refused on one lane must be refused on the other, or
    # naming a runtime is a way around the check (PR #2461 review).
    preflight_runner_workspace(runner, checkout_dir)
    if is_openshell(config.sandbox):
        return _launch_openshell_sandbox(
            config,
            store,
            run_id=run_id,
            log_file=log_file,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            admission=admission,
            credential=credential,
            transport=transport,
            sidecar_supervisor=sidecar_supervisor,
            run_kind=run_kind,
        )
    if sidecar_supervisor is None:
        argv = build_agent_container_argv(
            config,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            inference_transport=transport,
            read_only_mounts=read_only_mounts,
        )
        from kew.sandbox import _argv_cidfile

        execution = ContainedExecution.from_argv(argv)
        handle = _CreatedContainerHandle(
            execution.runtime, execution.name, cidfile=_argv_cidfile(argv), owned=False
        )
        require_credential_lifetime(credential, WALL_CLOCK_LIMIT_SECONDS)
        with _own_container_launch(handle):
            return dispatch_core._launch_container(  # type: ignore[attr-defined]
                argv,
                admission=admission,
                on_spawn=partial(_register_sandbox_client, store, run_id, log_file),
                prepare=partial(
                    _prepare_owned_container,
                    handle,
                    store=store,
                    run_id=run_id,
                    credential=credential,
                    # A ceiling per container attempt, from this launch: the
                    # create retries never run past it.
                    deadline=run_deadline(),
                ),
                env=launch_environment(os.environ, credential, contained=True),
                credential=credential,
                separate_stderr=separate_stderr,
            )
    else:
        _permit_container_creation(store, run_id)
        execution = prepare_sidecar_worker(
            config,
            supervisor=sidecar_supervisor,
            image_id=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            inference_transport=transport,
            store=store,
            run_id=run_id,
            read_only_mounts=read_only_mounts,
        )
    handle = _CreatedContainerHandle(
        execution.runtime, execution.name, execution.container_id
    )
    with _own_container_launch(handle):
        if store is not None:
            store.set_container_identity(run_id, execution.name, execution.container_id)
        _permit_container_creation(store, run_id)
        if sidecar_ready is not None and sidecar_supervisor is not None:
            sidecar_ready(transport, sidecar_supervisor)
        require_credential_lifetime(credential, WALL_CLOCK_LIMIT_SECONDS)
        return dispatch_core._launch_container(  # type: ignore[attr-defined]
            list(execution.argv),
            admission=admission,
            env=launch_environment(os.environ, credential, contained=True),
            credential=credential,
            container_name=execution.name,
            container_id=execution.container_id,
            checkout_dir=execution.checkout_dir,
            on_spawn=partial(_register_sandbox_client, store, run_id, log_file),
            separate_stderr=separate_stderr,
        )


@dataclass(frozen=True)
class ContainerLaunchFailure:
    """Cleanup evidence carried by the unchanged launch exception."""

    cleanup_confirmed: bool
    owners: frozenset[tuple[str, str]] = frozenset()
    # The runtime reported the container ``created`` with no process before
    # cleanup removed it: kew made it and could never start it (#2973).
    never_started: bool = False


def container_launch_failure(exc: BaseException) -> ContainerLaunchFailure | None:
    evidence = getattr(exc, "container_launch_failure", None)
    return evidence if isinstance(evidence, ContainerLaunchFailure) else None


def _stop_failed_container(handle: Any, exc: BaseException) -> None:
    """Attempt both termination and waiting, preserving the original failure."""
    previous = container_launch_failure(exc)
    owner = _container_cleanup_owner(handle)
    if previous is not None and owner in previous.owners:
        return
    if _refused_create(handle, exc):
        _record_container_cleanup(exc, previous, owner, True)
        return
    if _ambiguous_create(handle, exc):
        _settle_ambiguous_create(handle, exc, previous, owner)
        return
    killed = KillStatus.UNKNOWN
    waited = False
    try:
        killed = handle.kill()
    except BaseException:
        _logger.debug("Container termination remains unconfirmed during launch unwind")
    try:
        handle.wait(timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS, cleanup_identity=False)
        waited = True
    except BaseException:
        _logger.debug("Container wait remains unconfirmed during launch unwind")
    stopped = killed is KillStatus.ACCEPTED and waited
    never_started = not stopped and _never_started(handle)
    _record_container_cleanup(
        exc,
        previous,
        owner,
        stopped or _created_container_removed(handle),
        never_started=never_started,
    )


def _adopt_ambiguous_create(
    store: RunStore, run_id: str, runtime: str, name: str
) -> str | None:
    """Claim the container a failed ``create`` may still have committed (#3007).

    The daemon can make the container and its client still exit nonzero. A
    container under the name that is ``created`` with no process, and labelled
    with this ledger and this run, is this create's own: its id is persisted
    on the row, so recovery reads it by id from here on, and it is returned
    for the unwind to remove. Anything else stays unclaimed.
    """
    stale = read_never_started_container(runtime, name)
    ledger = store.read_ledger_id()
    if (
        stale is None
        or ledger is None
        or stale.labels.get(KEW_LEDGER_LABEL) != ledger
        or stale.labels.get(KEW_RUN_LABEL) != run_id
    ):
        return None
    try:
        store.set_container_identity(run_id, name, stale.container_id)
    except Exception as exc:
        # Unpersisted, the id is not claimed: nothing is removed under it, and
        # the create's own failure is the one that propagates.
        _warn(f"Sandbox: container {name!r} kept: {exception_text(exc)}")
        return None
    return stale.container_id


def _removed_adopted_create(handle: Any) -> bool:
    """Remove the container a failed create committed, by the id it adopted."""
    adopted = getattr(handle, "adopted_id", None)
    if adopted is None or never_started_container(handle.runtime, adopted) is None:
        return False
    return remove_never_started_container(handle.runtime, adopted)


def _settle_ambiguous_create(
    handle: Any,
    exc: BaseException,
    previous: ContainerLaunchFailure | None,
    owner: tuple[str, str],
) -> None:
    """Certify a failed create clean only when it provably left nothing.

    A create committed under this run's own labels is removed by the id it
    was adopted under (#3007), and then the run fails as a container start.
    """
    removed = _removed_adopted_create(handle)
    clean = removed or _create_left_name_free(handle, exc)
    _record_container_cleanup(exc, previous, owner, clean, never_started=removed)


def _create_left_name_free(handle: Any, exc: BaseException) -> bool:
    """A failed create that left nothing under the name made nothing (#2913).

    The runtime answering that no container holds the name proves there is
    nothing to clean, so the run is finalized with the create's own reason.
    A timed-out create is excluded: its daemon may still commit the container
    after the client was killed (#3103).
    """
    return (
        not isinstance(exc, ContainerCreateTimedOut)
        and getattr(handle, "adopted_id", None) is None
        and container_absent(handle.runtime, handle.name)
    )


def _refused_create(handle: Any, exc: BaseException) -> bool:
    """A create the runtime refused made nothing, so nothing is stopped by name.

    Only a name refusal proves that: the daemon checks the name before it
    makes anything.
    """
    failed = _failed_create(handle, exc)
    return failed is not None and failed.name_conflict


def _ambiguous_create(handle: Any, exc: BaseException) -> bool:
    """Any other failed create may have been committed before its client failed.

    The daemon can make the container and the client still exit nonzero, so
    the name is neither stopped (it may not be ours) nor certified as clean:
    the cidfile and the pending row stay for recovery (#2973).
    """
    failed = _failed_create(handle, exc)
    return failed is not None and not failed.name_conflict


def _failed_create(handle: Any, exc: BaseException) -> ContainerStartFailed | None:
    if (
        isinstance(handle, _CreatedContainerHandle)
        and handle.container_id is None
        and isinstance(exc, ContainerStartFailed)
    ):
        return exc
    return None


def _never_started(handle: Any) -> bool:
    """Read the state of the container this registration created, by its id."""
    if not isinstance(handle, _CreatedContainerHandle) or handle.container_id is None:
        return False
    return never_started_container(handle.runtime, handle.container_id) is not None


def _created_container_removed(handle: Any) -> bool:
    """Remove a container the kill could not stop, under its own registration.

    A never-started container refuses ``kill`` and would otherwise keep the
    item's name, so every later create of it fails (#2973). Only the full id
    this registration created is removed, never a name.
    """
    if not isinstance(handle, _CreatedContainerHandle) or handle.container_id is None:
        return False
    try:
        return handle.remove()
    except BaseException as exc:
        _warn(
            f"Sandbox: removal of container {handle.name!r} unconfirmed: "
            f"{exception_text(exc)}"
        )
        return False


def _container_cleanup_owner(handle: Any) -> tuple[str, str]:
    if isinstance(handle, _CreatedContainerHandle):
        return handle.runtime, handle.name
    return "stream", str(id(handle))


def _record_container_cleanup(
    exc: BaseException,
    previous: ContainerLaunchFailure | None,
    owner: tuple[str, str],
    confirmed: bool,
    *,
    never_started: bool = False,
) -> None:
    owners = previous.owners if previous is not None else frozenset()
    exc.container_launch_failure = ContainerLaunchFailure(  # type: ignore[attr-defined]
        cleanup_confirmed=confirmed
        and (previous is None or previous.cleanup_confirmed),
        owners=owners | {owner},
        never_started=never_started
        or (previous is not None and previous.never_started),
    )


@dataclass
class _CreatedContainerHandle:
    """Own a daemon container before a streaming client handle exists."""

    runtime: str
    name: str
    container_id: str | None = None
    cidfile: Path | None = None
    owned: bool = True
    # The id of a container a failed create committed under this run's
    # labels (#3007): read by name, persisted, then removed by this id.
    adopted_id: str | None = None

    def _recorded_target(self) -> str | None:
        """This registration's container id: the one it recorded, else the
        full id its cidfile holds. Never the name, which is host-wide and may
        be another run's container (#1963)."""
        if self.container_id is not None:
            return self.container_id
        if self.cidfile is None:
            return None
        try:
            candidate = self.cidfile.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            _warn(
                f"Sandbox: no container id recorded for {self.name!r} "
                f"({exception_text(exc)}); nothing is stopped by its name"
            )
            return None
        return candidate if _FULL_CONTAINER_ID.fullmatch(candidate) else None

    def kill(self) -> KillStatus:
        target = self._recorded_target()
        if target is None:
            return KillStatus.UNKNOWN
        result = _run(
            [self.runtime, "kill", target],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        return KillStatus.ACCEPTED if result.returncode == 0 else KillStatus.REFUSED

    def wait(self, timeout: float, *, cleanup_identity: bool = False) -> None:
        target = self._recorded_target()
        if target is None:
            return
        _run([self.runtime, "wait", target], check=True, timeout=timeout)

    def cleanup_identity(self) -> None:
        if self.cidfile is not None:
            self.cidfile.unlink(missing_ok=True)

    def remove(self) -> bool:
        """Remove only the container this registration created, by its id."""
        if self.container_id is None:
            return False
        result = _run(
            [self.runtime, "rm", "-f", self.container_id],
            check=False,
            timeout=RUNTIME_CONTROL_TIMEOUT_SECONDS,
        )
        return result.returncode == 0


@contextmanager
def _own_container_launch(handle: _CreatedContainerHandle) -> Iterator[None]:
    """Keep daemon ownership until the next launch stage accepts it."""
    try:
        yield
    except BaseException as exc:
        if not handle.owned:
            raise
        _stop_failed_container(handle, exc)
        evidence = container_launch_failure(exc)
        assert evidence is not None
        _cleanup_container_identity(handle, evidence.cleanup_confirmed)
        raise


def _deliver_container_launch(
    handle: ContainerHandle, on_launch: Callable[[], None] | None
) -> None:
    from kew.dispatch_core import _invoke_launch

    try:
        _invoke_launch(on_launch)
    except BaseException as exc:
        _stop_failed_container(handle, exc)
        raise


def launch_error_category(exc: BaseException) -> str:
    """Name a container that could not be created or started as just that.

    A start whose client could not even be spawned leaves the container kew
    created in ``created``; the cleanup evidence says so (#2973).
    """
    if isinstance(exc, ContainerStartFailed):
        return exc.category
    evidence = container_launch_failure(exc)
    if evidence is not None and evidence.never_started:
        return CONTAINER_START_FAILED
    return "sandbox_launch_error"


def _finalize_launch_marker_failure(
    exc: BaseException,
    store: RunStore | None,
    run_id: str,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> None:
    """Preserve launch evidence while durable recovery blocks finalization.

    A row the admission's cancellation already terminalized is left alone."""
    evidence = container_launch_failure(exc)
    if evidence is None or not evidence.cleanup_confirmed:
        return
    record = store.get(run_id) if store is not None else None
    if record is not None and record.status != "pending":
        return
    try:
        _finalize_sandbox_run(
            store,
            run_id,
            exit_code=-1,
            error_category=launch_error_category(exc),
            model=model,
            overrides=overrides,
            reason=str(exc),
        )
    except BaseException:
        _logger.debug("Container launch finalization deferred until claim recovery")


def _cleanup_container_identity(
    handle: ContainerHandle | _CreatedContainerHandle,
    permitted: bool,
    result: DispatchResult | None = None,
) -> None:
    """Preserve identity until container termination has been confirmed.

    Launch evidence is retained on *result* first, so a cleanup failure can
    never erase what the container was observed to start."""
    if result is not None:
        capture_container_launch(handle, result)
    if not permitted:
        return
    if result is not None:
        settle_never_started_start(handle, result)
    cleanup_identity = getattr(handle, "cleanup_identity", None)
    if callable(cleanup_identity):
        try:
            cleanup_identity()
        except BaseException:
            _logger.debug("Container identity cleanup deferred")
    _capture_realized_checkout(handle, result)


def _require_sandbox_released(handle: object) -> None:
    """Refuse to let a run finish while its OpenShell sandbox may still exist.

    On this runtime releasing the sandbox IS the cleanup, and the release
    already settled an unknown outcome by reading the sandbox back and retried
    a delete that did not land. A release that is still unconfirmed raises
    here, carrying the unconfirmed cleanup evidence every launch path already
    honours: the run stays pending with its reservation, and the driver's
    recovery — which finds a present sandbox whose agent has ended — is what
    brings the work home and deletes it (#2625). Finishing the run instead
    would leave a credential-attached sandbox behind a terminal row nothing
    ever reads again.

    Every other runtime's handle has no ``released`` fact and passes.
    """
    from kew.openshell_launch import OpenShellSandboxHandle
    from kew.runtime_names import OPENSHELL_RUNTIME

    if not isinstance(handle, OpenShellSandboxHandle) or handle.released:
        return
    exc = OpenShellSandboxUnreleased(
        f"openshell sandbox {handle.sandbox_name} could not be confirmed deleted; "
        "the run stays pending until a recovery releases it"
    )
    _record_container_cleanup(
        exc, None, (OPENSHELL_RUNTIME, handle.sandbox_name), False
    )
    raise exc


def _capture_realized_checkout(
    handle: ContainerHandle | _CreatedContainerHandle, result: DispatchResult | None
) -> None:
    """Carry an OpenShell handle's realized checkout onto the result (N7).

    Read HERE, at the one seam both the success and the failure arms run
    through, because that is also where the realization happens: on this
    runtime releasing the sandbox and bringing the work back are one step, and
    the download has to precede the delete. Every other runtime has no
    realization to report and leaves the result untouched.

    The outcome has to be copied rather than raised: ``cleanup_identity``
    above swallows what it raises, so a refusal that stayed on the handle
    would be a refusal nobody could act on — and the caller acting on it is
    what keeps a run whose work never came back from publishing the launch
    snapshot instead.
    """
    from kew.openshell_checkout import CheckoutRealization

    realization = getattr(handle, "realization", None)
    # The TYPE, not just the attribute name: every launch double in the test
    # suites is a bare ``MagicMock``, which answers any attribute with another
    # mock — so a name check alone reads a realization off every docker handle
    # a test ever built.
    if result is None or not isinstance(realization, CheckoutRealization):
        return
    _write_realization_evidence(result.log_file, realization)
    result.realized_checkout_refusal = realization.refusal
    if realization.checkout is None:
        return
    try:
        generation = _bind_realized_generation(realization.checkout.path)
    except Exception as exc:  # noqa: BLE001 - any failure here is a refusal
        # The refusal IS the record, and it is what refuses publication below:
        # a tree kew could not bind an identity to is not one it will push.
        result.realized_checkout_refusal = (
            f"the realized checkout {realization.checkout.path} could not be "
            f"bound to a checkout generation ({exception_text(exc)})"
        )
    else:
        result.realized_checkout_generation = generation
        result.realized_checkout = realization.checkout.path


def _bind_realized_generation(realized: Path) -> str:
    """Bind a checkout generation to the realized tree, and return it.

    Bound here rather than inside the realization because
    :mod:`kew.openshell_checkout` is reached from the sandbox layer, which may
    not import :mod:`kew.worktree` (``lint-imports``). The moment is the same
    either way: nothing writes to this tree between the download that made it
    and the publication that reads it.

    A generation is bound at all because the publication tail checks one
    before it pushes, and the original checkout's binds the ORIGINAL's inode
    and an out-of-tree sidecar — so carrying it across the swap would fail the
    gate, and carrying nothing would silently retire it.
    """
    from kew import worktree

    return worktree.write_checkout_generation(realized)


def _write_realization_evidence(
    log_file: Path | None, realization: CheckoutRealization
) -> None:
    """Append the realization event beside the run's log, fail-soft.

    The same ``.events.jsonl`` sidecar the sandbox identity is recorded in
    (``_register_openshell_client``): this runtime's facts have no run_store
    column, and the denial events are the account of what the gateway refused
    the agent — which is evidence whether or not the checkout came back, and
    is gone from the gateway the moment the sandbox is deleted.
    """
    if log_file is None:
        return
    try:
        with (
            Path(log_file)
            .with_suffix(".events.jsonl")
            .open("a", encoding="utf-8") as events
        ):
            events.write(json.dumps(realization.as_event()) + "\n")
    except OSError as exc:
        _logger.warning("realized-checkout evidence could not be written: %s", exc)


def run_container_and_stream(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    run_id: str,
    model: str,
    log_file: Path,
    run_kind: str,
    result: DispatchResult,
    inference_transport: ResolvedInferenceTransport | None = None,
    sidecar_ready: Callable[[ResolvedInferenceTransport, Any], None] | None = None,
    _sidecar_supervisor: Any = None,
    _admission: PendingAdmission | None = None,
    credential: BoundedRunKey | None = None,
    _credentials_resolved: bool = False,
    on_launch: Callable[[], None] | None = None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
    cost_governor: ContainedCostGovernor | None = None,
    _silence_pacing: SilencePacing | None = None,
) -> int:
    """Build the sandbox argv, launch, stream telemetry, and finalize launch/
    stream errors — the ONE container-run primitive both callers route through.

    Callers own the run-row write (``_record_sandbox_start`` for dispatch,
    ``_record_pending`` for the reviewer) and the runtime presence-check BEFORE
    calling this; this primitive owns only argv-build → launch → stream, and it
    re-raises launch/stream failures after finalizing the (already-recorded)
    row under its named category (``sandbox_launch_error`` /
    ``sandbox_stream_error``) so the run history names the layer that failed.

    Launch callables, including test doubles, implement
    ``sandbox.launch_container(argv, *, container_name=None, container_id=None,
    checkout_dir=None, admission=None, on_spawn=None, prepare=None)``.
    This primitive always supplies
    ``admission``; sidecar launches also supply the existing container identity.
    The client registration callback executes before the workload can start.

    The #1017 invariant lives HERE, in the single argv-build site exercised by
    BOTH callers: ``argv[0]`` is the canonical BARE ``config.sandbox.runtime``
    name, never the resolved ``shutil.which()`` path. The subprocess allowlist
    matches bare names only, so a resolved ``/usr/local/bin/docker`` is rejected
    at launch — enforcing this once kills the D bug class (#1017) for good.

    ``image``/``runner`` are supplied by the caller: the image is resolved from
    the runner (#1144) and is deliberately NOT read from ``config.sandbox.image``
    here (that field names the claude image only, so reading it would let a
    non-claude runner land in the claude CLI's image — the #1104 incident), and
    the runner name is what the container args are derived from (#1145) and what
    keys the credential env allowlist (#1146) — so the container is handed its
    OWN provider's credentials and no other runner's. Both coming from the
    caller is what stops image, flags and credentials describing different
    runners.

    ``cost_governor`` carries the run cap and the breaker ceiling (#1462): a
    trip stops the container through its handle and records the category on
    ``result``.
    """
    # The runner's own flags become the container command ("$@" for the
    # entrypoint, contract element 3): build_command() minus the binary and
    # minus the prompt, which travels via the mounted file and never argv
    # (#695). ``model`` is the effective (routed) model the caller resolved, so
    # routing reaches the container, not just the run row (#761). An unknown
    # runner raises out of get_runner rather than falling back to claude flags.
    #
    # Build and launch share one failure arm: the caller has already written the
    # ``pending`` row, so an argv that cannot even be BUILT must finalize it too
    # — otherwise the row sits pending forever on a refusal that never launched.
    with (
        proxy_redaction_scope(config.sandbox.proxy_environment),
        _admission or PendingAdmission(store, run_id) as admission,
        execution_cancellation(admission),
    ):
        overrides = build_pricing_overrides(config.pricing.overrides)
        agent_runner = get_runner(runner)
        transport = _stream_transport(config, agent_runner, model, inference_transport)
        require_credential_route(agent_runner, transport)
        if _needs_issued_credentials(transport, _credentials_resolved):
            return _run_issued_container(
                config,
                store,
                image=image,
                runner=runner,
                checkout_dir=checkout_dir,
                slug=slug,
                run_id=run_id,
                model=model,
                log_file=log_file,
                run_kind=run_kind,
                result=result,
                transport=transport,
                sidecar_ready=sidecar_ready,
                admission=admission,
                on_launch=on_launch,
                read_only_mounts=read_only_mounts,
                cost_governor=cost_governor,
            )
        if transport.sidecar and _sidecar_supervisor is None:
            return _run_sidecar_container_and_stream(
                config,
                store,
                image=image,
                runner=runner,
                checkout_dir=checkout_dir,
                slug=slug,
                run_id=run_id,
                model=model,
                log_file=log_file,
                run_kind=run_kind,
                result=result,
                transport=transport,
                sidecar_ready=sidecar_ready,
                overrides=overrides,
                admission=admission,
                on_launch=on_launch,
                read_only_mounts=read_only_mounts,
                cost_governor=cost_governor,
            )

        model = transport.model
        handle = _launch_stream_handle(
            config,
            store,
            run_id=run_id,
            log_file=log_file,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            transport=transport,
            sidecar_supervisor=_sidecar_supervisor,
            sidecar_ready=sidecar_ready,
            admission=admission,
            credential=credential,
            run_kind=run_kind,
            overrides=overrides,
            read_only_mounts=read_only_mounts,
        )
        cleanup_identity_permitted = True
        stream_failed = False
        try:
            _deliver_container_launch(handle, on_launch)
            # A runtime client alone proves nothing. Observe the daemon signal
            # when available; otherwise observe the existing workload stream.
            capture_container_launch(handle, result)
            diagnostic = DiagnosticTail()
            tap = _DiagnosticTap(
                LaunchObservedStream(handle, result),
                diagnostic,
                RunnerBudgetLatch(runner, config),
            )
            silence = StreamSilenceWatch(
                handle,
                limit_seconds=no_output_seconds(config),
                store=store,
                run_id=run_id,
                runner=runner,
                result=result,
                pacing=_silence_pacing,
            )
            with silence.watching():
                exit_code = _stream_with_review_capture(
                    silence.observe(tap),
                    log_file,
                    store,
                    run_id,
                    model=model,
                    overrides=overrides,
                    runner=runner,
                    run_kind=run_kind,
                    on_cost=_cost_watch(cost_governor, result, handle),
                )
            capture_container_launch(handle, result)
            result.exit_code = exit_code
            finalize_container_hard_stop(
                handle,
                result,
                returncode=exit_code,
                timeout_seconds=WALL_CLOCK_LIMIT_SECONDS,
            )
            _name_container_signal_exit(handle, result, diagnostic)
            classify_runner_budget_stop(tap.budget, result)
            _classify_stream_silence(silence, result)
            result.duration_seconds = result.enforced_duration_seconds or 0.0
            write_container_evidence(
                log_file,
                oom_probe_status=result.oom_probe_status or None,
                oom_probe_diagnostic=result.oom_probe_diagnostic or None,
                resource_cap_observations=result.resource_cap_observations,
                resource_cap_observations_truncated=(
                    result.resource_cap_observations_truncated
                ),
            )
            return exit_code
        except BaseException as exc:
            stream_failed = True
            cleanup_identity_permitted = _container_stream_failed(
                store, run_id, handle, result, exc, run_kind, model, overrides
            )
            raise
        finally:
            _cleanup_container_identity(handle, cleanup_identity_permitted, result)
            if not stream_failed:
                _require_sandbox_released(handle)


def _classify_stream_silence(
    silence: StreamSilenceWatch, result: DispatchResult
) -> None:
    """Record a run the silence watchdog stopped under its own category
    (#2240), over the bare signal exit its kill left; a cost stop stands."""
    if silence.silenced():
        result.error_category = STREAM_SILENT_CATEGORY


class RunnerBudgetLatch:
    """Whether any result event of this run said the runner stopped at its own
    run cap (#1462). Sticky: a later line can add to it, never take it back.

    claude carries ``--max-budget-usd`` into the container and stops itself
    there, reporting a budget subtype or a total at/over the cap — the two
    signals the host's ``categorize`` reads (``ClaudeCodeRunner._classify_error``).
    Reading only the last line let an agent that wrote one more line after
    that result unsay it, so every protocol line is parsed by the runner's own
    ``parse_line``, and the highest total any result event reported is kept.

    That parse is the line's SECOND: the stream fold already read it live and
    narrated it to the console, so this one is muted (``display_suppressed``).
    Nothing an untrusted line holds escapes :meth:`observe`. A total the parser
    hands on that no float holds reads as unbounded, at or over any cap; a line
    the parser itself refuses (claude refuses a bare non-finite
    ``total_cost_usd``) is skipped as unreadable.
    """

    def __init__(self, runner: str, config: DispatchConfig) -> None:
        self._parser = _resolve_stream_parser(runner)
        self._cap_usd = config.agent.max_budget_usd
        self.peak_total_usd = 0.0
        self.subtype: str | None = None
        self.unreadable = 0

    @property
    def stopped(self) -> bool:
        if self.subtype is not None:
            return True
        return bool(self._cap_usd) and self.peak_total_usd >= self._cap_usd

    def observe(self, line: str) -> None:
        if self._parser is None:
            return
        try:
            with display_suppressed():
                events = self._parser.parse_line(line)
        except Exception as exc:  # noqa: BLE001 - untrusted agent output
            # The fold logged this line already; one record per line is enough.
            self.unreadable += 1
            _logger.debug(
                "sandbox budget latch: protocol line unreadable (%s), "
                "%d so far; the budget stop is read from the other lines",
                type(exc).__name__,
                self.unreadable,
            )
        else:
            # Outside the try: a fault here is kew's own, and must not pass as
            # an unreadable line while a run past its cap goes on to publish.
            for event in events:
                if event.event_type == "result":
                    self._observe_result(event.metadata)

    def _observe_result(self, metadata: Mapping[str, object]) -> None:
        subtype = metadata.get("subtype")
        if isinstance(subtype, str) and "budget" in subtype and self.subtype is None:
            self.subtype = subtype
        total = metadata.get("total_cost_usd")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            self.peak_total_usd = max(self.peak_total_usd, _reported_total_usd(total))


def _reported_total_usd(total: float) -> float:
    """A runner-reported run total as a float, failing closed: a figure no
    float holds, or one that is not finite, is unbounded spend (#1462)."""
    try:
        value = float(total)
    except OverflowError:
        return math.inf
    return value if math.isfinite(value) else math.inf


#: The categories a runner's own budget stop may replace: none, or the generic
#: reading of an exit. A specific finding (``resource``, a hard stop) stands.
_BUDGET_STOP_REPLACES = frozenset({"", "permanent", "transient"})


def classify_runner_budget_stop(
    latch: RunnerBudgetLatch | None, result: DispatchResult
) -> None:
    """Record a run the agent stopped at its own run cap as a cost stop (#1462).

    Where no governor is armed (the reviewer launcher, a remote placement)
    nothing else would see the stop, and a run that stopped mid-task — often
    reporting success — would publish as one.
    """
    if latch is None or not latch.stopped or _cost_stopped(result):
        return
    if result.error_category not in _BUDGET_STOP_REPLACES:
        _logger.warning(
            "contained run stopped at its own run cap, kept as %s",
            result.error_category,
        )
        return
    result.error_category = _CONTAINED_BUDGET_STOP
    result.subtype = latch.subtype or "runner_budget"
    # The stop is recorded at the cost the runner said it stopped at.
    result.estimated_cost_usd = max(
        result.estimated_cost_usd, _recordable_usd(latch.peak_total_usd)
    )
    _logger.warning(
        "contained run stopped at its own run cap: %s (reported $%.2f)",
        result.subtype,
        latch.peak_total_usd,
    )


def _name_container_signal_exit(
    handle: Any, result: DispatchResult, diagnostic: DiagnosticTail
) -> None:
    """Give a contained 134/137 its signal, last output and category (#3191).

    The same rule the host runner applies (:func:`kew.exit_signal.name_signal_exit`),
    read after the hard stop is classified so a confirmed OOM is ``resource``.
    The last output is the merged stream's non-protocol lines, then a
    separated stderr pipe's (a review's), whichever the run had.
    """
    separated = getattr(handle, "diagnostic_lines", None)
    lines = diagnostic.lines() + (separated if isinstance(separated, list) else [])
    result.diagnostic_tail = lines[-DIAGNOSTIC_TAIL_LINES:]
    reason = name_signal_exit(result)
    if reason:
        _warn(f"Runner {reason}")


def _separates_container_stderr(run_kind: str) -> bool:
    """Whether this run's client needs the container's stderr on a second pipe.

    Only a governed review reads a VERDICT from the stream it folds, where any
    line that is not a typed event fails the round (#2047). The client merges
    the container's stderr into that same pipe by default, so a stray runtime
    warning failed genuine rounds (#2053); a review therefore gets two pipes,
    and every other run keeps the single merged operator view it always had.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    return run_kind == ADVERSARIAL_REVIEW_RUN_KIND


def _stream_with_review_capture(
    stream: ContainerStream,
    log_file: Path,
    store: RunStore | None,
    run_id: str,
    *,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    runner: str,
    run_kind: str,
    on_cost: CostObserver | None = None,
) -> int:
    """Stream the container, capturing a reviewer's terminal result (#2021).

    An adversarial review's verdict is the runner's own typed result event as
    the host reads it here, from the container's stdout. The capture sees each
    raw line before the sanitizing consumer drops it, and lands beside the host
    log, which is never mounted into the container. It is written on every
    exit path, so a stream that died still leaves its (refusing) record.
    """
    import kew.dispatch_core as dispatch_core
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    # Named only when armed, so an unwatched run calls the seam as it always has.
    governed: dict[str, Any] = {} if on_cost is None else {"on_cost": on_cost}
    if run_kind != ADVERSARIAL_REVIEW_RUN_KIND:
        return dispatch_core._stream_sandbox_log(
            stream,
            log_file,
            store,
            run_id,
            model=model,
            overrides=overrides,
            runner=runner,
            **governed,
        )
    capture = ReviewResultCapture()
    try:
        return dispatch_core._stream_sandbox_log(
            capture.tap(stream),
            log_file,
            store,
            run_id,
            model=model,
            overrides=overrides,
            runner=runner,
            **governed,
        )
    finally:
        capture.write(log_file)


def _launch_stream_handle(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    run_id: str,
    log_file: Path,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    transport: ResolvedInferenceTransport,
    sidecar_supervisor: Any,
    sidecar_ready: Callable[[ResolvedInferenceTransport, Any], None] | None,
    admission: PendingAdmission,
    credential: BoundedRunKey | None,
    run_kind: str,
    overrides: dict[str, tuple[float, float]] | None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
) -> Any:
    """Launch the streaming container, finalizing a launch failure by its layer.

    A created container's cleanup evidence decides; an interruption without
    one is terminalized by the admission's cancellation path, not here."""
    try:
        return _launch_streaming_container(
            config,
            store,
            run_id=run_id,
            log_file=log_file,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            model=model,
            transport=transport,
            sidecar_supervisor=sidecar_supervisor,
            sidecar_ready=sidecar_ready,
            admission=admission,
            credential=credential,
            separate_stderr=_separates_container_stderr(run_kind),
            read_only_mounts=read_only_mounts,
            run_kind=run_kind,
        )
    except BaseException as exc:
        if container_launch_failure(exc) is not None:
            _finalize_launch_marker_failure(exc, store, run_id, model, overrides)
            raise
        if isinstance(exc, Exception):
            _logger.debug("sandbox launch failed for %s run %s", run_kind, run_id)
            _finalize_sandbox_run(
                store,
                run_id,
                exit_code=-1,
                error_category=launch_error_category(exc),
                model=model,
                overrides=overrides,
            )
        raise


def _container_stream_failed(
    store: RunStore | None,
    run_id: str,
    handle: Any,
    result: DispatchResult,
    exc: BaseException,
    run_kind: str,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
) -> bool:
    """Finalize a stream failure and say whether identity cleanup may proceed.

    Container cleanup evidence decides first and keeps identity until the
    termination is confirmed. An interruption cancels the launch under deferred
    SIGINT and leaves terminalization to the admission. Any other failure keeps
    its launch evidence on the exception and finalizes under its own category.
    """
    result.exit_code = -1
    if isinstance(exc, Exception):
        capture_container_stream_failure(handle, result, exc)
        exc.dispatch_result = result  # type: ignore[attr-defined]
        _release_sandbox_before_finalize(handle, result, exc)
    evidence = container_launch_failure(exc)
    if evidence is not None:
        _finalize_launch_marker_failure(exc, store, run_id, model, overrides)
        return evidence.cleanup_confirmed
    if not isinstance(exc, Exception):
        cancel_launch = getattr(handle, "cancel_launch", None)
        if callable(cancel_launch):
            with DeferredSIGINT():
                cancel_launch()
        return True
    _logger.debug("sandbox stream failed for %s run %s", run_kind, run_id)
    try:
        _finalize_sandbox_run(
            store,
            run_id,
            exit_code=-1,
            error_category=result.error_category,
            # A stream that failed after a cost trip is still that stop (#1462).
            status=_failed_run_status(result),
            reason=_cost_stop_reason(result) or result.stop_reason,
            model=model,
            overrides=overrides,
            cost_floor_usd=_cost_stop_floor_usd(result),
        )
    except Exception as finalize_exc:
        finalize_exc.dispatch_result = result  # type: ignore[attr-defined]
        raise
    return True


def _release_sandbox_before_finalize(
    handle: Any, result: DispatchResult, exc: Exception
) -> None:
    """Release an OpenShell sandbox BEFORE its failed run can be finalized.

    The failure arm finalizes the row terminal, and the identity cleanup — on
    this runtime the realization and the delete — ran only afterwards, so a
    delete that could not be confirmed was left behind a terminal row with no
    cleanup evidence (#2625). Releasing first lets an unconfirmed delete become
    UNCONFIRMED cleanup evidence on *exc*, which keeps the run pending with
    its reservation, exactly as :func:`_require_sandbox_released` does for a
    run that finished. Both steps are idempotent, so the cleanup the caller
    still runs afterwards repeats nothing. Unconfirmed evidence already on
    *exc* is left alone: it withholds cleanup, and this would perform one.
    """
    from kew.openshell_launch import OpenShellSandboxHandle
    from kew.runtime_names import OPENSHELL_RUNTIME

    if not isinstance(handle, OpenShellSandboxHandle):
        return
    previous = container_launch_failure(exc)
    if previous is not None and not previous.cleanup_confirmed:
        return
    _cleanup_container_identity(handle, True, result)
    if not handle.released:
        _record_container_cleanup(
            exc, previous, (OPENSHELL_RUNTIME, handle.sandbox_name), False
        )


def _attempt_auth_path(
    runner_name: str,
    transport: ResolvedInferenceTransport | None,
    broker_provider: str | None,
) -> str | None:
    """WHICH credential path one contained attempt authenticated through.

    Three mutually exclusive boundaries, checked in the order of narrowing
    ownership: a sidecar mints a credential per attempt, a gateway provider
    holds one for the life of the run (OpenShell), or the launch was handed an
    environment variable.

    The sidecar is first because its ownership is the narrowest: the supervisor
    mints that credential for this attempt and no gateway provider holds it, so
    a sidecar launch brokers nothing even on the OpenShell runtime
    (:func:`kew.openshell_broker.broker_route` returns it no provider at all).
    Reading the broker first attested a sidecar attempt as authenticating
    through the operator's long-lived provider.

    The env variable is last because on the brokered runtime those variables
    hold a placeholder — reading them would report ``env:<NAME>`` for a launch
    that was handed no value.
    """
    if transport is not None and transport.sidecar:
        return "synthetic:per-attempt"
    if broker_provider:
        return broker_auth_path(broker_provider)
    return _contained_auth_path(runner_name)


def _contained_auth_path(runner_name: str) -> str | None:
    """Which env credential this contained launch authenticates with, or None.

    A container cannot inherit CLI login, so the populated env NAME is the
    whole auth-path identity here — never the value.
    """
    from kew.sandbox import runner_credential_env_vars

    credential = next(
        (
            name
            for name in runner_credential_env_vars(runner_name)
            if os.environ.get(name)
        ),
        None,
    )
    return None if credential is None else f"env:{credential}"


def _require_sandbox_store(store: RunStore | None) -> None:
    """Refuse a paid contained run that would leave no realized-route row.

    The same rule the host path enforces (#1550 review F5, re-review F-6).
    Every production caller opens a store.
    """
    if store is None:
        raise ValueError(
            "sandboxed dispatch requires a store so the realized route "
            "profile is captured"
        )


def _resolve_launch_inputs(
    config: DispatchConfig, *, image: str, runner: str, model: str
) -> tuple[str, ResolvedInferenceTransport, str, str | None, AgentRunner]:
    """Everything the capture must know before a row is written or money moves.

    (immutable image ID, resolved transport, effective model, service tier,
    resolved runner) — resolved once so the profile, the argv, the credentials
    and the spend describe the same launch (#1550 review F1/F3). The runner
    comes back with them because the bounded key is minted from its declared
    credential names, and resolving it again later would put a fail-closed
    runner lookup between the admission and its run row.
    """
    image_id = _resolve_launch_image_id(config, image, runner=runner)
    agent_runner = get_runner(runner)
    transport = resolve_inference_transport(config, agent_runner, model)
    require_credential_route(agent_runner, transport)
    tier = _requested_service_tier(config, runner) or None
    return image_id, transport, transport.model, tier, agent_runner


def _resolve_launch_image_id(config: DispatchConfig, image: str, *, runner: str) -> str:
    """Resolve *image* to the immutable ID this dispatch will launch, or refuse.

    One seam for the fail-closed rule (#1550 review F1): the profile records
    this ID and the container argv launches by it, so capture and execution
    cannot describe different bytes. No runtime, an image that was never built,
    or an unreadable daemon each refuse the dispatch before any row is written
    or money moves — and ``require_image_id`` says which of the three it was.

    ``runner`` is REQUIRED, with no default. It selects the packaged Dockerfile
    named by missing-image guidance. A default could build Claude Code under an
    OpenCode tag. The reviewer once omitted this argument, so an omission
    remains a type error.
    """
    from kew.sandbox import image_id_runtime, require_image_id

    # ``image_id_runtime``, not the probed runtime directly: on ``openshell``
    # the gateway has no image surface to inspect, and the ID is read from the
    # driver underneath it (see that function).
    return require_image_id(
        image_id_runtime(config.sandbox, detect_runtime_cached(config.sandbox)),
        image,
        runner=runner,
        refusal="the dispatch is refused before any charge",
    )


def _emit_sandbox_hard_stop(
    store: RunStore | None,
    config: DispatchConfig,
    result: DispatchResult,
    *,
    run_id: str,
    issue_num: int,
    item_id: str,
    model: str,
    baseline: RunBaseline | None = None,
) -> None:
    """Emit the same breaker fact for the direct sandbox dispatch path: a hard
    stop, or the cost governor's anomaly trip (#1462), as the host's
    ``_maybe_emit_breaker`` does for both, with the same baseline facts."""
    rule = result.breaker_rule or result.hard_stop
    if not rule or not config.breaker.enabled:
        return
    emit(
        store,
        "circuit_breaker",
        actor=Actor("policy", rule),
        issue_num=issue_num,
        item_id=item_id,
        run_id=run_id,
        repo=config.project.repo or None,
        payload={
            "rule": rule,
            "scope": "run",
            "reason": redact_credentials(result.resource_cap_reason) or None,
            "resource_cap_code": result.resource_cap_code or None,
            "resource_cap_observations": redact_container_observations(
                result.resource_cap_observations
            ),
            "resource_cap_observations_truncated": (
                result.resource_cap_observations_truncated
            ),
            "oom_probe_status": result.oom_probe_status or None,
            "oom_probe_diagnostic": redact_credentials(result.oom_probe_diagnostic)
            or None,
            "observed_usd": _breaker_observed_usd(result),
            # How the runtime answered a cost kill (#1462); None for a hard stop.
            "kill_status": result.cost_stop_kill or None,
            "model": model,
            "complexity": "",
            **baseline_payload(
                baseline,
                tripped_threshold_usd=result.breaker_threshold_usd,
                terminal_observed=result.terminal_observed,
            ),
        },
    )


@contextmanager
def _hard_stop_audited(emit_hard_stop: Callable[[], None]) -> Iterator[None]:
    """Emit the run's breaker fact when the block ends, however it ends.

    A stream that fails after the cost governor tripped raises past the code
    that read the trip, so the ``circuit_breaker`` fact health counts would be
    lost with it; this emits it on that path too (#1462)."""
    try:
        yield
    finally:
        emit_hard_stop()


def _breaker_observed_usd(result: DispatchResult) -> float:
    """The cost at the trip for a cost kill; a hard stop prices nothing."""
    if not result.breaker_rule:
        return 0.0
    return round(_recordable_usd(result.estimated_cost_usd or 0.0), 4)


def _credential_refusal_result(
    result: DispatchResult,
    *,
    finalize: Callable[..., None],
    store: RunStore | None,
    refusal: CredentialPublicationRefused,
    issue_num: int,
    item_id: str,
    repo: str | None,
) -> DispatchResult:
    """Persist and return the dedicated non-releasable result."""
    finalize(1, CREDENTIAL_PUBLICATION_REFUSAL, status="refused")
    emit(
        store,
        CREDENTIAL_PUBLICATION_REFUSAL,
        actor=Actor("policy", "credential-publication"),
        run_id=result.run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        payload={"reason": refusal.reason, "publication_allowed": False},
    )
    result.exit_code = 1
    result.error_category = CREDENTIAL_PUBLICATION_REFUSAL
    _warn(f"Contained publication refused: {refusal}")
    return result


def _run_sandbox_shadow(
    *,
    config: DispatchConfig,
    checkout_dir: Path,
    slug: str,
    image_id: str,
    store: RunStore,
    run_id: str,
    issue_num: int,
    item_id: str,
    branch: str,
    base_branch: str,
    runner: str,
) -> None:
    """Observe the shadow comparison without affecting publication."""
    try:
        run_shadow_comparison(
            config,
            checkout_dir=checkout_dir,
            slug=slug,
            image=image_id,
            store=store,
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            branch=branch,
            base_branch=base_branch,
            runner=runner,
            provenance=orchestrator_provenance(),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 — a shadow must never break a dispatch
        _warn(
            f"shadow comparison failed and was ignored: {type(exc).__name__}: "
            f"{exception_text(exc)}"
        )


def _require_initial_sandbox_publication_credentials(
    store: RunStore,
    run_id: str,
    transport: ResolvedInferenceTransport,
    *,
    deferred: bool = False,
) -> None:
    """Run the launch gate here, unless this attempt's profile is not written yet.

    The gate reads the attempt's realized profile, so it goes wherever that
    profile goes: a sidecar attempt runs it from
    :func:`_record_ready_sidecar_profile` and a PLACED attempt from
    :func:`_record_placed_profile` — in both cases still before the container
    the gate protects (:func:`_profile_recorded_after_launch`).
    """
    if not _profile_recorded_after_launch(deferred, transport):
        require_publication_launch_credentials(
            store, run_id=run_id, run_kind="dispatch", attempt_number=1
        )


def _sandbox_execution_failed(exit_code: int, result: DispatchResult) -> bool:
    """A wrapper's clean exit cannot make an unstarted agent successful, and a
    run kew stopped for cost never publishes, whatever its exit (#1462)."""
    return exit_code != 0 or startup_refused(result) or _cost_stopped(result)


def _cost_stopped(result: DispatchResult) -> bool:
    """Whether kew's governor or the runner's own cap stopped this run."""
    return (
        result.budget_exceeded
        or result.breaker_stopped
        or result.error_category in _COST_STOP_CATEGORIES
    )


def _cost_stop_reason(result: DispatchResult) -> str:
    """The audited reason of a cost stop: what stopped it, at what cost, and
    how the runtime answered kew's kill (#1462). "" for any other failure."""
    if not _cost_stopped(result):
        return ""
    kill = result.cost_stop_kill or "not requested"
    reason = (
        f"{result.error_category} ({result.subtype or 'cost'}) at "
        f"${_recordable_usd(result.estimated_cost_usd):.2f}; container kill {kill}"
    )
    # The signal exit the kill produced, when it was named, rides along.
    return f"{reason}; {result.stop_reason}" if result.stop_reason else reason


def _cost_stop_floor_usd(result: DispatchResult) -> float:
    """The least cost a cost-stopped run's row may record: its cost at the
    stop, bounded as every recorded cost is. A run the breaker judged at all
    keeps its trip cost too (#1798). 0.0 for any other run."""
    if not (_cost_stopped(result) or result.breaker_rule):
        return 0.0
    return _recordable_usd(result.estimated_cost_usd or 0.0)


def _failed_run_status(result: DispatchResult) -> RunStatus:
    """The row status a failed contained run lands in: a cost stop's is the
    host's (``budget_stop``, and ``killed`` for a breaker kill), which is what
    health's guard count and the budget surfaces read."""
    if result.error_category == _CONTAINED_BUDGET_STOP:
        return "budget_stop"
    if result.error_category == "circuit_breaker":
        return "killed"
    return "failure"


def _sandbox_refuses_publication(
    exit_code: int,
    result: DispatchResult,
    log_file: Path,
    finalize: Callable[..., None],
) -> bool:
    """Finalize and return True when this run must not reach publication."""
    if _sandbox_execution_failed(exit_code, result) and _sandbox_quota_exhausted(
        exit_code, result, log_file
    ):
        finalize(exit_code, result.error_category, status="refused")
        return True
    if _sandbox_execution_failed(exit_code, result):
        finalize(
            exit_code,
            result.error_category,
            status=_failed_run_status(result),
            reason=_cost_stop_reason(result) or result.stop_reason,
            cost_floor_usd=_cost_stop_floor_usd(result),
        )
        return True
    return _sandbox_needs_human(result, log_file, finalize)


def _sandbox_quota_exhausted(
    exit_code: int, result: DispatchResult, log_file: Path
) -> bool:
    """Read a contained run the provider refused for quota as such (#2643).

    The rejection is read from the derived evidence the stream fold wrote, as
    on the host. The row lands ``refused`` — never a failure the epic overlay
    latches — and dispatch's collect step holds the runner's lane.
    """
    evidence = read_derived_evidence(log_file)
    rejection = None if evidence is None else evidence.quota_exhausted
    if rejection is None or not is_quota_exhausted(
        category=result.error_category, exit_code=exit_code, rejection=rejection
    ):
        return False
    result.error_category = QUOTA_EXHAUSTED_CATEGORY
    result.quota_rejection = rejection
    return True


def _sandbox_needs_human(
    result: DispatchResult, log_file: Path, finalize: Callable[..., None]
) -> bool:
    """Refuse publication of a run whose agent stopped for a human (#2601).

    The prompts tell the agent to end ``KEW_NEEDS_HUMAN: <reason>`` rather
    than publish — a policy gate it could not make pass, say. Such a run exits
    0, so without this the contained path pushed the change the agent refused.
    The stop is read from the derived evidence the stream fold wrote, and the
    run reaches dispatch's collect step as ``needs_human``, as on the host.
    """
    evidence = read_derived_evidence(log_file)
    reason = None if evidence is None else evidence.needs_human
    if reason is None:
        return False
    result.error_category = "needs_human"
    result.subtype = reason or "unspecified"
    finalize(
        result.exit_code or 1, "needs_human", status="failure", reason=result.subtype
    )
    return True


#: Why this run reached no publishable tree. One category for every
#: realization refusal; the realization's own ``refusal_code`` names which
#: check refused, and it rides the finalized row's reason.
_REALIZED_CHECKOUT_CATEGORY = "sandbox_realized_checkout_refused"

#: What a run on the OpenShell runtime that produced no realization at all is
#: refused with. Reached when the handle never got to realize — a cleanup the
#: stream arm did not permit, for instance — and never silently treated as "so
#: publish the uploaded tree": that tree is the launch snapshot, and pushing it
#: would land a PR holding none of the agent's work.
_NO_REALIZATION_REFUSAL = (
    "the agent's work was never downloaded off its OpenShell sandbox, so the "
    "only tree on this host is the one the launch uploaded"
)


def _publication_checkout(
    config: DispatchConfig,
    result: DispatchResult,
    finalize: Callable[..., None],
    *,
    checkout_dir: Path,
    checkout_generation: str | None,
) -> tuple[Path, str | None] | None:
    """The checkout publication runs against, or None once it is refused.

    Identity on every runtime but OpenShell, where the agent edited an
    uploaded copy and the tree to publish is the realized one
    (:mod:`kew.openshell_checkout`). The generation travels with it: the
    original's binds the original's inode and an out-of-tree sidecar, so the
    realized tree carries its own, bound when it was realized — the gate stays
    live across the swap instead of being dropped at it.
    """
    from kew.openshell_launch import is_openshell

    if not is_openshell(config.sandbox):
        return checkout_dir, checkout_generation
    if result.realized_checkout is None:
        refusal = result.realized_checkout_refusal or _NO_REALIZATION_REFUSAL
        result.exit_code = 1
        result.error_category = _REALIZED_CHECKOUT_CATEGORY
        finalize(1, _REALIZED_CHECKOUT_CATEGORY, status="failure", reason=refusal)
        _warn(f"OpenShell publication refused: {refusal}")
        return None
    return result.realized_checkout, result.realized_checkout_generation


def _finalize_sandbox_with_pricing(
    store: RunStore | None,
    run_id: str,
    model: str,
    overrides: dict[str, tuple[float, float]] | None,
    code: int,
    error_category: str = "",
    status: RunStatus | None = None,
    reason: str = "",
    cost_floor_usd: float = 0.0,
) -> None:
    """Finalize the run with its admitted pricing context and startup result."""
    _finalize_sandbox_run(
        store,
        run_id,
        exit_code=code,
        error_category=error_category,
        model=model,
        overrides=overrides,
        status=status,
        reason=reason,
        cost_floor_usd=cost_floor_usd,
    )


def _run_sandboxed_container(
    *,
    prompt: str,
    self_check: bool = False,
    prompt_profile_version_id: int | None = None,
    checkout_dir: Path,
    config: DispatchConfig,
    image: str,
    runner: str,
    issue_num: int,
    item_id: str,
    branch: str,
    title: str,
    model: str,
    store: RunStore | None,
    tracker: str = "github",
    worker_cap: int = 0,
    cap_exclude_issue: int | None = None,
    admit_budget_check: Callable[[], None] | None = None,
    run_id: str | None = None,
    publication_base_sha: str = "",
    epic_ref: str | None = None,
    checkout_generation: str | None = None,
    sidecar_ready: Callable[[], None] | None = None,
    on_admitted: Callable[[str], None] | None = None,
    on_launch: Callable[[], None] | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    cost_governor: ContainedCostGovernor | None = None,
) -> DispatchResult:
    """Record the start row, run the shared container primitive, and broker
    push+PR on exit 0.

    Split out of ``_run_sandboxed`` so the caller's ``finally`` owns the
    host-side prompt-file cleanup for every exit path (#725). The argv build,
    launch, and stream (and their error finalization) live in the shared
    ``run_container_and_stream`` primitive (#1023, Wave 1); this caller keeps
    only the record-start and the exit-0 broker tail.
    """
    # The routed effective model (what execute_dispatch resolved for the
    # banner), falling back to the config default — so routing actually
    # reaches the container, not just the run row (#761). Recorded on the row
    # and passed to the primitive so `model` (routed) matches what runs.
    from kew.dispatch_core import _invoke_admitted, _project_repo_slug
    from kew.dispatch_placement import (
        RemoteInputs,
        placed_remotely,
        run_placed_dispatch,
    )
    from kew.hosts.remote_dispatch import DISPATCH_RUN_KIND
    from kew.run_store_work import capture_dispatch_reservation

    reservation = capture_dispatch_reservation(
        store,
        epic_ref,
        item_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
    )
    run_id = run_id or str(uuid.uuid4())
    effective_model = model or config.agent.model or ""

    slug = branch_slug(item_id)
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"sandbox-{slug}.log"

    # Boundary and transport resolve BEFORE any row is written or money moves
    # (#1550 review F1/F3): the profile must record the immutable image ID the
    # launch will use and the endpoint the spend goes through. An unresolvable
    # ID refuses the dispatch — a mutable tag is not evidence of what ran.
    _require_sandbox_store(store)
    assert store is not None  # nosec B101 - required immediately above
    # Same resolution the host path uses (#2097): ``project.repo`` is optional
    # ("auto-detected from gh"), so an unset one falls back to the project
    # origin here, in the parent that holds the checkout. Both dispatch paths
    # must agree on this column — stop-label supersede matches runs on it.
    delivery_repo = _project_repo_slug(config)
    image_id, resolved_transport, effective_model, tier, agent_runner = (
        _resolve_launch_inputs(
            config, image=image, runner=runner, model=effective_model
        )
    )
    # WHERE this agent runs, decided before admission (#2526): a ``gcp`` pin
    # with no free instance refuses the dispatch here, with no row written, no
    # reservation spent and nothing charged. ``local`` — the default — asks the
    # ledger nothing and constructs no provider; the launch below is then
    # byte-for-byte the one this path has always made.
    # Named, never defaulted: this launcher admits and records a ``dispatch``
    # row (``_record_sandbox_start``), and the placement, the remote launch and
    # its credential fence all read this same kind (#2526 review).
    run_kind = DISPATCH_RUN_KIND
    # The instance is CLAIMED, not just chosen, and held for the whole
    # dispatch: another process deciding at the same moment reads it as taken
    # instead of picking it too and losing the host lock after admission
    # (#2526 review).
    with (
        _held_placement(
            config,
            store,
            checkout_dir=checkout_dir,
            runner=runner,
            run_kind=run_kind,
            transport=resolved_transport,
        ) as (held, trusted_base),
        PendingAdmission(store, run_id) as admission,
        execution_cancellation(admission),
    ):
        # A placed run's container is launched from the mutable TAG against the
        # instance's image store, so this machine's immutable ID does not
        # describe its bytes: the profile and the publication gate that reads
        # it are deferred to the attestation the placement makes on the
        # instance, before its container starts (#2526 review).
        placed = placed_remotely(held.placement)
        # The bounded key lives exactly as long as the paid launch. It is
        # minted before the admitted row records its identity and revoked
        # when this context closes, which is BEFORE any publication below:
        # publication admission requires confirmed revocation evidence for
        # this run's issued key, so a key still live here would hold the
        # PR instead of landing it (#1957 round 10).
        with bounded_run_credentials(
            run_id=run_id,
            config=config,
            store=store,
            model=effective_model,
            credential_names=agent_runner.required_credentials,
            timeout_seconds=WALL_CLOCK_LIMIT_SECONDS,
            direct=resolved_transport.mode == "direct",
            # This path launches a container by definition, so a minted key on
            # the OpenShell runtime goes into this run's gateway provider.
            contained=True,
        ) as credential:
            broker_provider = launch_provider(
                config.sandbox,
                runner,
                run_id=run_id,
                issued=credential is not None,
                sidecar=resolved_transport.sidecar,
            )
            # The pending row, the reservation binding and the dispatcher's owner
            # evidence commit in this one transaction, before the claim hook runs.
            run_id = _record_sandbox_start(
                store,
                run_id=run_id,
                issue_num=issue_num,
                item_id=item_id,
                branch=branch,
                title=title,
                model=effective_model,
                runner_name=runner,
                log_file=log_file,
                tracker=tracker,
                repo=delivery_repo,
                worker_cap=worker_cap,
                cap_exclude_issue=cap_exclude_issue,
                admit_budget_check=admit_budget_check,
                image=image,
                image_id=image_id,
                transport=resolved_transport,
                service_tier=tier,
                self_check=self_check,
                prompt_profile_version_id=prompt_profile_version_id,
                reservation=reservation,
                reservation_pinned=expected_reservation_run_id is not None,
                runtime=config.sandbox.runtime,
                credential=credential,
                broker_provider=broker_provider,
                # A placed run's boundary is attested on the instance, from the
                # image ID that machine resolved (#2526 review).
                profile_deferred=placed,
            )

            _invoke_admitted(on_admitted, run_id)

            finalize = partial(
                _finalize_sandbox_with_pricing,
                store,
                run_id,
                effective_model,
                build_pricing_overrides(config.pricing.overrides),
            )

            try:
                _require_initial_sandbox_publication_credentials(
                    store, run_id, resolved_transport, deferred=placed
                )
            except CredentialPublicationRefused as exc:
                return _credential_refusal_result(
                    DispatchResult(
                        exit_code=1,
                        issue_num=issue_num,
                        log_file=log_file,
                        run_id=run_id,
                    ),
                    finalize=finalize,
                    store=store,
                    refusal=exc,
                    issue_num=issue_num,
                    item_id=item_id,
                    repo=delivery_repo,
                )

            _require_checkout_generation_or_finalize(
                checkout_dir,
                checkout_generation,
                finalize,
                boundary="contained admission handoff",
            )
            _capture_launch_evidence_or_finalize(
                store,
                finalize,
                config=config,
                run_id=run_id,
                issue_num=issue_num,
                item_id=item_id,
                repo=delivery_repo,
                prompt=prompt,
                checkout_dir=checkout_dir,
            )

            _require_checkout_generation_or_finalize(
                checkout_dir,
                checkout_generation,
                finalize,
                boundary="contained paid launch",
            )

            result = DispatchResult(
                exit_code=-1,
                issue_num=issue_num,
                log_file=log_file,
                run_id=run_id,
            )

            record_sidecar_ready = partial(
                _record_ready_sidecar_profile,
                store=store,
                run_id=run_id,
                runner_name=runner,
                model=effective_model,
                tracker=tracker,
                image=image,
                image_id=image_id,
                service_tier=tier,
                on_ready=sidecar_ready,
            )

            launch_here = partial(
                run_container_and_stream,
                config,
                store,
                # The captured immutable ID, never the tag: what the profile claims
                # and what launches must be the same bytes (#1550 review F1).
                image=image_id,
                runner=runner,
                checkout_dir=checkout_dir,
                slug=slug,
                run_id=run_id,
                model=effective_model,
                log_file=log_file,
                run_kind=run_kind,
                result=result,
                inference_transport=resolved_transport,
                sidecar_ready=record_sidecar_ready,
                _admission=admission,
                credential=credential,
                _credentials_resolved=True,
                on_launch=on_launch,
                cost_governor=cost_governor,
            )

            attest_placed = _placed_attestation(
                placed,
                partial(
                    _record_placed_profile,
                    store=store,
                    run_id=run_id,
                    runner_name=runner,
                    model=effective_model,
                    tracker=tracker,
                    image=image,
                    transport=resolved_transport,
                    service_tier=tier,
                    credential=credential,
                    broker_provider=broker_provider,
                ),
            )

            with preserve_dispatch_result(result):
                # The placement decided before admission is carried out here
                # (#2526). A local one is ``launch_here`` and nothing else.
                # The breaker fact is emitted however the run ends, a stream
                # that failed after a cost trip included (#1462).
                with _hard_stop_audited(
                    partial(
                        _emit_sandbox_hard_stop,
                        store,
                        config,
                        result,
                        run_id=run_id,
                        issue_num=issue_num,
                        item_id=item_id,
                        model=effective_model,
                        baseline=getattr(cost_governor, "baseline", None),
                    )
                ):
                    exit_code = run_placed_dispatch(
                        config,
                        store,
                        placement=held.placement,
                        provider=held.provider,
                        inputs=RemoteInputs(
                            run_id=run_id,
                            runner=runner,
                            slug=slug,
                            model=effective_model,
                            # The TAG on this path: an instance resolves it against
                            # its own image store, where the local immutable ID
                            # names nothing.
                            image_tag=image,
                            checkout_dir=checkout_dir,
                            base_sha=trusted_base,
                            log_file=log_file,
                            scratch=log_dir / "placed",
                            run_kind=run_kind,
                            transport=resolved_transport,
                            # The same bounded key the local container would
                            # receive, never the operator's ambient one.
                            credential=credential,
                            issue_num=issue_num,
                            item_id=item_id,
                            repo=delivery_repo,
                        ),
                        result=result,
                        local_launch=launch_here,
                        on_launch=on_launch,
                        attest_launch=attest_placed,
                    )
                # Failed execution or entrypoint preparation cannot reach
                # publication, and nor can an agent that stopped for a human.
                if _sandbox_refuses_publication(exit_code, result, log_file, finalize):
                    return result

                # The realized checkout replaces the uploaded one for every
                # step below (N7). Resolved inside the credential context, so
                # a refusal finalizes before publication is reached at all.
                realized = _publication_checkout(
                    config,
                    result,
                    finalize,
                    checkout_dir=checkout_dir,
                    checkout_generation=checkout_generation,
                )
                if realized is None:
                    return result
                checkout_dir, checkout_generation = realized

        # The bounded key is revoked above. Publication runs outside that
        # context and inside the admission, which still owns cancellation.
        with preserve_dispatch_result(result):
            # Exit 0: first check whether the agent made any real changes. A
            # zero-change completion (agent found nothing to do) should NOT attempt
            # push+PR — a branch with no diff causes `gh pr create` to fail, which
            # the exception handler below misclassifies as a push failure (#958).
            _require_checkout_generation_or_finalize(
                checkout_dir,
                checkout_generation,
                finalize,
                boundary="contained publication",
            )
            base_branch = config.branches.base or "main"
            if _checkout_has_no_changes(
                checkout_dir,
                base_branch,
                publish=config.publication.publish,
                dangerously_skip_containment=config.sandbox.dangerously_skip_containment,
            ):
                finalize(0, _SANDBOX_NO_CHANGE_CATEGORY)
                # Carry the run identity and the classification back out (I10B): the
                # host records the contained zero-change exit as the SAME durable
                # no-change candidate the host-broker path records, and it can only
                # bind that judgment to the attempt it names here.
                result.error_category = _SANDBOX_NO_CHANGE_CATEGORY
                return result

            # Shadow mode (W1.1 / D5), default OFF. Runs the host-owned publication
            # broker beside this dispatch and compares the tree it WOULD publish
            # against the one the push below is about to. It publishes nothing, blocks
            # nothing, and cannot raise — see kew.publication_shadow_hook.
            #
            # Placed HERE, after the zero-change check and before the push, because
            # that is the only point where the legacy path's answer is settled and not
            # yet acted on. Earlier and there may be nothing to compare; later and the
            # comparison would be describing a publication that already happened.
            # Belt as well as braces. run_shadow_comparison is contracted never to
            # raise and its own tests prove it — but the cost of that contract ever
            # breaking is a run that did the work and lost the PR, and "an
            # observability feature ate my publication" is the one outcome that would
            # justify never turning shadow mode on. Interrupts still pass through:
            # Ctrl-C is the operator, not a shadow failure.
            _run_sandbox_shadow(
                config=config,
                checkout_dir=checkout_dir,
                slug=slug,
                # The shadow's extraction container launches by the same resolved ID.
                image_id=image_id,
                store=store,
                run_id=run_id,
                issue_num=issue_num,
                item_id=item_id,
                branch=branch,
                base_branch=base_branch,
                runner=runner,
            )

            # The row is NOT success until the host broker actually lands the
            # branch + PR on GitHub (#SFD-152). Finalizing success before the push let
            # a push/`gh pr create` failure leave a "success" row with no PR. Order it
            # after the broker; a broker failure finalizes a distinct failure category.
            return _publish_completed_sandbox(
                result,
                finalize=finalize,
                checkout_dir=checkout_dir,
                branch=branch,
                item_id=item_id,
                title=title,
                config=config,
                store=store,
                run_id=run_id,
                publication_base_sha=publication_base_sha,
                image_id=image_id,
                log_file=log_file,
                issue_num=issue_num,
                epic_ref=epic_ref,
            )


def _sandbox_publication_failure(
    result: DispatchResult,
    *,
    finalize: Callable[..., None],
    exc: Exception,
    store: RunStore | None,
    run_id: str,
    epic_ref: str | None,
    item_id: str,
    branch: str,
    issue_num: int,
    config: DispatchConfig,
    publication_base_sha: str,
    checkout_dir: Path,
) -> DispatchResult:
    """Park an ordinary publication failure without losing implementation."""
    import kew.dispatch_core as dispatch_core
    from kew.publication import publication_failure

    step, detail = publication_failure(exc)
    if dispatch_core._contained_failure_parks(store, run_id):
        finalize(
            0,
            dispatch_core._publication_error_category(exc),
            status="awaiting_publication",
        )
        try:
            dispatch_core._mint_publication_continuation(
                store,
                epic_ref=epic_ref,
                item_id=item_id,
                branch=branch,
                issue_num=issue_num,
                repo=config.project.repo or None,
                publish=config.publication.publish,
                base=publication_base_sha,
                run_id=run_id,
                worktree_dir=checkout_dir,
            )
        except Exception as mint_exc:  # noqa: BLE001 — the host retries this
            _warn(
                "contained publication failure parked, but its continuation "
                f"could not be minted here: {mint_exc}"
            )
    else:
        finalize(0)
    if isinstance(exc, publication_canonical.PublicationHeld):
        result.error_category = "publication_held"
        result.subtype = "\n".join(exc.flags)
    else:
        result.error_category = SANDBOX_PUBLICATION_FAILED_CATEGORY
        result.subtype = f"{step}\n{detail}"
    return result


def _publish_completed_sandbox(
    result: DispatchResult,
    *,
    finalize: Callable[..., None],
    checkout_dir: Path,
    branch: str,
    item_id: str,
    title: str,
    config: DispatchConfig,
    store: RunStore | None,
    run_id: str,
    publication_base_sha: str,
    image_id: str,
    log_file: Path,
    issue_num: int = 0,
    epic_ref: str | None = None,
) -> DispatchResult:
    no_changes: list[publication_canonical.CanonicalNoChange] = []
    try:
        import kew.dispatch_core as dispatch_core

        pr_url = dispatch_core._push_and_open_pr(
            checkout_dir,
            branch,
            item_id,
            title,
            config,
            store=store,
            run_id=run_id,
            base_sha=publication_base_sha,
            runtime=config.sandbox.runtime,
            image=image_id,
            log_file=log_file,
            on_no_change=no_changes.append,
        )
    except CredentialPublicationRefused as exc:
        return _credential_refusal_result(
            result,
            finalize=finalize,
            store=store,
            refusal=exc,
            issue_num=issue_num,
            item_id=item_id,
            repo=config.project.repo or None,
        )
    except Exception as exc:
        return _sandbox_publication_failure(
            result,
            finalize=finalize,
            exc=exc,
            store=store,
            run_id=run_id,
            epic_ref=epic_ref,
            item_id=item_id,
            branch=branch,
            issue_num=issue_num,
            config=config,
            publication_base_sha=publication_base_sha,
            checkout_dir=checkout_dir,
        )
    if pr_url is not None:
        # Carried on the result itself: best-effort persistence must not be
        # the only record of a PR that exists (review round 10).
        result.published_pr_url = pr_url
    if pr_url is None:
        result.canonical_no_change = no_changes[0] if no_changes else None
        finalize(0, _SANDBOX_NO_CHANGE_CATEGORY)
        result.error_category = _SANDBOX_NO_CHANGE_CATEGORY
        return result
    finalize(0)
    # Publish the agent's own report after the branch and PR exist.
    dispatch_core._post_agent_report(pr_url, log_file, config, result=result)
    return result


def _run_issued_container(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    run_id: str,
    model: str,
    log_file: Path,
    run_kind: str,
    result: DispatchResult,
    transport: ResolvedInferenceTransport,
    sidecar_ready: Callable[[ResolvedInferenceTransport, Any], None] | None,
    admission: PendingAdmission,
    on_launch: Callable[[], None] | None = None,
    read_only_mounts: tuple[tuple[Path, str], ...] = (),
    cost_governor: ContainedCostGovernor | None = None,
) -> int:
    """Own bounded issuance for the shared read-only review launch path."""
    limit = config.review.max_review_usd if run_kind == "adversarial_review" else None
    with bounded_run_credentials(
        run_id=run_id,
        config=config,
        store=store,
        model=model,
        credential_names=get_runner(runner).required_credentials,
        timeout_seconds=WALL_CLOCK_LIMIT_SECONDS,
        limit_usd=limit,
        direct=transport.mode == "direct",
        contained=True,
    ) as credential:
        if credential is not None and store is not None:
            profile = _sandbox_route_profile(
                run_id,
                runner_name=runner,
                model=model,
                tracker=config.tracker.type,
                image=image,
                image_id=image,
                transport=transport,
                credential=credential,
                broker_provider=launch_provider(
                    config.sandbox,
                    runner,
                    run_id=run_id,
                    issued=True,
                    sidecar=transport.sidecar,
                ),
            )
            if profile is not None:
                store.record_route_profile(profile)
        return run_container_and_stream(
            config,
            store,
            image=image,
            runner=runner,
            checkout_dir=checkout_dir,
            slug=slug,
            run_id=run_id,
            model=model,
            log_file=log_file,
            run_kind=run_kind,
            result=result,
            inference_transport=transport,
            sidecar_ready=sidecar_ready,
            _admission=admission,
            credential=credential,
            _credentials_resolved=True,
            on_launch=on_launch,
            read_only_mounts=read_only_mounts,
            cost_governor=cost_governor,
        )


def _needs_issued_credentials(
    transport: ResolvedInferenceTransport, resolved: bool
) -> bool:
    return not resolved and transport.mode == "direct"


def _stream_transport(
    config: DispatchConfig,
    runner: AgentRunner,
    model: str,
    transport: ResolvedInferenceTransport | None,
) -> ResolvedInferenceTransport:
    return transport or resolve_inference_transport(config, runner, model)
