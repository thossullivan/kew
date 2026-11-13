"""Where a contained dispatch's agent actually runs (#2526).

Epic #2338 node 8. Nodes 5 and 6 built the two halves and left them
unconnected: :func:`kew.hosts.remote_dispatch.place_dispatch_on_host` could run
one agent container on a Compute Engine instance and bring its work back, and
:func:`kew.fleet_placement.place_run` could decide which instance a run belongs
on — but the dispatch launch site consulted neither, so every agent ran on the
operator machine whatever ``[sandbox] placement`` said. This module is the wire
between them, and it is deliberately the ONLY wire:

* **The decision happens before admission.** :func:`place_contained_dispatch`
  is asked where this run goes before any row is written or money moves, so a
  ``gcp`` pin with no free instance refuses the dispatch rather than admitting
  it and then discovering there is nowhere to put it. The authoritative fleet
  cap stays where it already is — the atomic count-then-insert in
  ``RunStore.try_admit_run`` — so this answers WHERE and never WHETHER.
* **``local`` is byte-for-byte today's path.** A local placement returns
  :func:`kew.dispatch_sandbox.run_container_and_stream` unchanged and
  constructs no host provider, reads no instance pool and asks the ledger
  nothing: the default must not acquire a cloud dependency.
* **A remote placement streams and stops exactly as a local one does.** The
  relayed event lines go through the same fold a local pipe does
  (:func:`kew.dispatch_sandbox.sandbox_log_fold`), to the same log file, with
  the same live telemetry flushes — so there is one set of evidence rules.
* **Publication never moves to the VM.** What comes back is the verified
  artifact :mod:`kew.publication_artifact` reads, and it is applied into the
  operator machine's own run checkout through the git plumbing the publication
  broker already uses (:func:`kew.publication_apply.apply_to_tree`). Every step
  after that — the zero-change check, shadow mode, the push and the PR — runs on
  the operator machine against a checkout that now holds the agent's work, so
  there is one publication path rather than a second remote one.

The credential fence is :func:`kew.hosts.remote_dispatch.permit_model_credentials`'s
and is not restated here. What this module adds is the one thing that fence
cannot check for itself: a VM may receive exactly the credential VALUES a
contained launch of the same runner would put in a container on this machine,
and nothing else. That is one function, not a second reading of the
environment (:func:`host_model_credentials`) — the local launch strips the
operator's ambient inference key and substitutes this run's bounded, per-run,
spend-capped one, and a placement that read ``os.environ`` instead handed the
instance the ambient key while the run row attested the bounded one.

Two launches cannot be placed at all, whatever the fleet's capacity: a run
whose inference transport needs a sidecar process beside its container (that
process lives on this machine and a VM's container has no route to it), and a
run on the OpenShell runtime (its sandbox is created through the gateway here,
and there is no container argv to hand an instance's daemon).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import CalledProcessError
from typing import TYPE_CHECKING, Any, NoReturn

from kew import subprocess_utils
from kew.config import PLACEMENT_LOCAL, PLACEMENT_PREFER_GCP, DispatchConfig
from kew.fleet_placement import (
    LOCAL_SITE,
    FleetOccupancy,
    Placement,
    RunSite,
    gcp_sites,
    place_run,
    read_occupancy,
    resolved_policy,
)
from kew.hosts.errors import HostBusy, HostError
from kew.hosts.remote_dispatch import DISPATCH_RUN_KIND, REMOTE_ARTIFACT_DIR
from kew.run_credentials import launch_environment
from kew.runtime_backend import OPENSHELL_RUNTIME
from kew.sandbox import (
    REFUSAL_PLACEMENT_UNAVAILABLE,
    WALL_CLOCK_LIMIT_SECONDS,
    SandboxRuntimeUnavailable,
    runner_credential_env_vars,
)

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from collections.abc import Callable, Iterator

    from kew.credential_issuer import BoundedRunKey
    from kew.dispatch_sandbox import RunnerBudgetLatch
    from kew.hosts.provider import HostProvider
    from kew.hosts.remote_dispatch import RemoteLaunch
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.publication_artifact import Artifact
    from kew.publication_filter import FilterVerdict
    from kew.tracking import DispatchResult

logger = logging.getLogger(__name__)

#: How much of a placement's own explanation is kept as the finalized row's
#: reason. The text comes from another machine's stderr by way of an
#: interruption message, and a reason nothing can display is a reason nobody
#: reads; the bound :mod:`kew.hosts.remote_dispatch` keeps its stderr tail to.
_REASON_CHARS = 2000


class DispatchPlacementRefused(SandboxRuntimeUnavailable):
    """This dispatch has nowhere to run, so it was never started.

    Raised BEFORE admission, which is the whole point: the run has no row, no
    reservation and no spend, so the refusal costs a queue wait and the next
    tick asks again. A ``gcp`` pin that finds every instance at ``max_runs``
    lands here, because an explicit pin is not a preference.

    Subclasses :class:`~kew.sandbox.SandboxRuntimeUnavailable` so it lands in
    the dispatch's existing **prerequisite-refusal** arm rather than its
    failure arm (``kew.dispatch_core._refuse_or_fail`` routes on that type).
    That is not a convenience: the two arms differ in what they do to the
    caller. A refusal releases this dispatch's reservation and reports a
    refusal, which is what "come back next tick" has to be; the failure arm
    would spend an attempt of the node's retry budget on a verdict no agent
    ever reached, and put the node in a blast-radius report for a cloud that
    was merely busy. Its own ``reason`` token keeps the cause queryable on the
    audit chain: nothing here is missing a runtime.
    """

    _DEFAULT_REASON = REFUSAL_PLACEMENT_UNAVAILABLE


class PlacedRunKindRefused(RuntimeError):
    """The admitted run this placement is for is not a dispatch run.

    Only a ``dispatch`` run may carry the runner's model credential to an
    instance (:func:`kew.hosts.remote_dispatch.permit_model_credentials`), and
    that fence reads the run kind its caller hands it. This is the check that
    the kind handed over is the one the ledger admitted: the row is read back
    before any credential is derived, so a launch that described itself as a
    dispatch while its row says otherwise never reaches the credential sink
    (#2526 review).
    """

    #: Recorded on the run row, so this is distinguishable from an instance
    #: that failed and from work that could not be written back.
    error_category = "remote_host_run_kind_refused"


class RemoteWorkRefused(RuntimeError):
    """A remote placement's work is not a tree kew will publish.

    Raised when the package does not describe the base this run was cut
    from, or git refused to write it into the operator's checkout — both mean
    the tree that would be published is not the one the agent produced — when
    the publication filter refuses the work (a driver ``.gitattributes``, a
    traversal, a mode escalation: #2636), and over *content* only when the
    output secret scan found a credential this machine holds in it (#2569):
    the artifact reader has already re-derived every digest, so a finding is
    the agent's work, not a damaged package.
    """

    #: Recorded on the run row, so this is distinguishable from an agent that
    #: failed its task and from a session that went away.
    error_category = "remote_host_artifact_refused"


def host_model_credentials(
    runner: str, credential: BoundedRunKey | None = None
) -> dict[str, str]:
    """What a LOCAL contained launch of *runner* would put in its container.

    Derived, not re-derived: the names come from the single runner->credential
    table (``runner_credential_env_vars``) and the values from
    :func:`kew.run_credentials.launch_environment` with ``contained=True`` —
    the one function the local contained launch builds its own child
    environment with. So a VM is offered exactly the values a contained local
    run of the same runner would receive, and the two cannot drift.

    That equality is the whole point of going through ``launch_environment``
    rather than reading ``os.environ`` (#2526 review). The local launch strips
    the operator's ambient ``OPENROUTER_API_KEY`` and substitutes this run's
    bounded, per-run, spend-capped *credential*; reading the environment
    directly handed the instance the ambient key instead — widening inference
    exposure, escaping the bounded key's cap, and doing it while the run row
    attested the issued key as what authenticated the attempt.

    Empty values are left out rather than forwarded blank: a container handed
    the variable set to nothing fails authentication deep inside the agent
    instead of here.
    """
    environment = launch_environment(os.environ, credential, contained=True)
    return {
        name: value
        for name in runner_credential_env_vars(runner)
        if (value := environment.get(name, ""))
    }


def _bounded_inference_route(
    runner: str, transport: ResolvedInferenceTransport | None
) -> bool:
    """Whether this launch's inference credential is minted per run.

    Asked BEFORE admission, where no key exists yet, so it asks the same
    question ``bounded_run_credentials`` will ask when it mints one
    (:func:`kew.credential_handoff.direct_openrouter_route`). On such a route
    the ambient inference key is not what a container receives — on this
    machine or on an instance — and the bounded key that replaces it is minted
    inside the admission, or the dispatch is refused for want of a provisioning
    key before anything launches. Either way the placement may count on one.

    The runner has to DECLARE the inference variable as well: a bounded key is
    minted for any direct OpenRouter model, but only a declared name is handed
    to the container (:func:`kew.sandbox.build_run_argv` forwards exactly the
    declared ones), and a credential this launch would not pass is not a
    credential the placement may be admitted on.
    """
    from kew.credential_handoff import direct_openrouter_route
    from kew.credential_issuer import INFERENCE_ENV

    declared = runner_credential_env_vars(runner)
    if INFERENCE_ENV not in declared:
        return False
    model = "" if transport is None else transport.model
    direct = transport is None or transport.mode == "direct"
    return direct_openrouter_route(model, declared, direct=direct)


def _unplaceable_remotely(
    config: DispatchConfig,
    runner: str,
    transport: ResolvedInferenceTransport | None,
    run_kind: str,
) -> str | None:
    """Why this launch cannot run on a VM at all, or None when it can.

    Four refusals, and none of them is about capacity — a host with room
    still cannot run these:

    * Any **run kind but** ``dispatch``. Only a dispatch run may carry a model
      credential to an instance, so a review, remediation, resolve or CI fix
      placed there would have nothing to authenticate with — and placing one
      anyway would leave the credential fence as the only thing between it and
      the dispatch exception (#2526 review).
    * The **OpenShell runtime** has no container argv to place. Its sandboxes
      are created through the gateway on this machine
      (:mod:`kew.openshell_launch`) and the ``openshell`` CLI has no ``run``
      subcommand, so THE argv-build site refuses it outright
      (:func:`kew.sandbox.build_run_argv`). Placing it anyway started the
      instance and shipped the checkout before that refusal was reached —
      post-admission, out of the one arm a refusal may land in, and not even
      a :class:`~kew.hosts.errors.HostError` the placement could name
      (#2526 review).
    * A **sidecar** transport runs a proxy process beside the container on this
      machine. The container reaches it over a loopback route that exists only
      here, so the same argv on an instance would launch an agent with nothing
      to talk to.
    * **No declared credential for the launch to hand over.** The one
      exception to "no credentials on a VM" is the runner's own model
      credential: either a value a contained launch would really pass
      (:func:`host_model_credentials`) or the bounded key a per-run route is
      about to mint (:func:`_bounded_inference_route`).
    """
    if run_kind != DISPATCH_RUN_KIND:
        return _not_a_dispatch(run_kind)
    if config.sandbox.runtime == OPENSHELL_RUNTIME:
        return (
            f"the sandbox runtime is {OPENSHELL_RUNTIME!r}, whose sandboxes are "
            "created through the gateway on this machine and which has no "
            "container argv an instance's daemon could be handed"
        )
    if transport is not None and transport.sidecar:
        return (
            "this run's inference transport runs a sidecar beside its container "
            "on this machine, which a container on an instance cannot reach"
        )
    if not host_model_credentials(runner) and not _bounded_inference_route(
        runner, transport
    ):
        return (
            f"no {runner} model credential is set in this machine's environment, "
            "and nothing else may carry one to an instance"
        )
    return None


def _not_a_dispatch(run_kind: str) -> str:
    """Why a run of *run_kind* is never placed on an instance."""
    return (
        f"a {run_kind!r} run is never placed on an instance: only a "
        f"{DISPATCH_RUN_KIND!r} run may carry a model credential to one"
    )


def place_contained_dispatch(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    runner: str,
    run_kind: str,
    transport: ResolvedInferenceTransport | None = None,
) -> Placement:
    """Where this dispatch goes, decided before anything is admitted.

    ``local`` returns immediately and asks the ledger nothing: the default
    placement must not acquire a read, a pool or a cloud dependency.

    *run_kind* is required and has no default, for the reason
    :func:`~kew.hosts.remote_dispatch.place_dispatch_on_host`'s is: a default
    would resolve the ``dispatch`` policy for whatever launch forgot to say what
    it is. Anything but a dispatch is never placed remotely.

    A run that cannot be placed remotely *at all* (:func:`_unplaceable_remotely`)
    is sent to this machine under ``prefer-gcp`` — that is what a preference
    means — and refused under a ``gcp`` pin, because an operator who named the
    host by name is owed the refusal rather than a silent demotion.

    Occupancy is read with no liveness probe, deliberately. The probe can only
    prove a container on THIS machine dead, and counting an unprobeable row as
    free is how a second agent lands on an instance that already has one. An
    instance whose host lock another kew job holds is counted full as well
    (:func:`_with_held_instances`), because no ledger row says so.
    """
    policy = resolved_policy(config, run_kind)
    if policy == PLACEMENT_LOCAL:
        return Placement(LOCAL_SITE, policy, "placement is local")
    blocked = _unplaceable_remotely(config, runner, transport, run_kind)
    if blocked is not None:
        return _here_or_refused(policy, blocked)
    placement, held = _place_on_ledger(config, store, run_kind=run_kind)
    if placement.waits and _swept_dead_rows(store):
        placement, held = _place_on_ledger(config, store, run_kind=run_kind)
    if held and not placed_remotely(placement):
        placement = replace(
            placement,
            reason=f"{placement.reason}; held by another kew job: {', '.join(held)}",
        )
    if placement.waits:
        return _refuse(placement)
    return placement


def _place_on_ledger(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    run_kind: str,
) -> tuple[Placement, list[str]]:
    """One occupancy read, and where it places the run."""
    occupancy, held = _with_held_instances(config, read_occupancy(store))
    return place_run(config, occupancy, run_kind=run_kind), held


def _swept_dead_rows(store: Any) -> bool:  # noqa: ANN401 — RunStore protocol
    """Finalize provably dead ``pending`` rows; whether any were.

    Asked only before a refusal. Admission is where dead rows are otherwise
    swept, and a strict placement refuses BEFORE admission, so one crashed
    placed run's row filled its instance for good and every later strict
    placement refused on it (#2627). The sweep proves death the way admission
    does; a store that cannot sweep (a test fake) leaves the refusal as it was.
    """
    sweep = getattr(store, "sweep_dead_pending", None)
    if sweep is None:
        return False
    return bool(sweep())


@dataclass(frozen=True)
class HeldPlacement:
    """A placement, and the claim that keeps its instance this run's.

    *provider* is the one whose host lock is held for the instance the
    placement names, and None for a run placed on this machine. The launch has
    to go through THAT object: the claim is re-entrant only for the provider
    and thread holding it, and a second provider for the same instance would
    contend with this run's own hold (:meth:`~kew.hosts.provider.HostProvider.claim`).
    """

    placement: Placement
    provider: HostProvider | None = None


@contextmanager
def hold_contained_placement(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    runner: str,
    run_kind: str,
    transport: ResolvedInferenceTransport | None = None,
) -> Iterator[HeldPlacement]:
    """:func:`place_contained_dispatch`, with its instance claimed for the block.

    The decision alone is a read — ledger rows and a lock probe — and the
    instance was only recorded and locked inside the remote launch, AFTER
    admission. Two dispatch processes deciding together (the epic driver
    starts one per node) therefore both picked the same free instance, and
    the loser was refused by the host lock in the failure arm: an attempt
    spent, where ``prefer-gcp`` should have run it here and a ``gcp`` pin
    should have waited (#2526 review).

    So the chosen instance's host lock is TAKEN here, before admission, and
    held until the block ends. The lock is the test-and-set the read is not:
    every other placement's probe now reads the instance as held, and a
    placement that loses the race between its probe and its claim decides
    again, before anything is admitted — the next instance, this machine
    under a preference, or the pre-admission refusal under a pin. A local
    placement claims nothing and constructs no provider.
    """
    with ExitStack() as claims:
        yield _claimed_placement(
            claims,
            config,
            store,
            runner=runner,
            run_kind=run_kind,
            transport=transport,
        )


def _claimed_placement(
    claims: ExitStack,
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    runner: str,
    run_kind: str,
    transport: ResolvedInferenceTransport | None,
) -> HeldPlacement:
    """Decide, and claim what was decided; decide again over a lost claim.

    Bounded by the pool: each lost claim is an instance the next decision's
    probe reads as held, so the pool runs out before the attempts do. A pool
    that keeps changing hands faster than that ends the way a full one does.
    """
    lost: list[str] = []
    for _ in range(len(gcp_sites(config)) + 1):
        placement = place_contained_dispatch(
            config, store, runner=runner, run_kind=run_kind, transport=transport
        )
        if not placed_remotely(placement):
            return HeldPlacement(placement)
        site = placement.site or LOCAL_SITE
        provider = provider_for(config, site)
        try:
            claims.enter_context(provider.claim())
        except HostBusy:
            lost.append(site.label())
            continue
        return HeldPlacement(placement, provider)
    reason = f"every claimed instance was taken by another kew job: {', '.join(lost)}"
    return HeldPlacement(_here_or_refused(resolved_policy(config, run_kind), reason))


def _here_or_refused(policy: str, reason: str) -> Placement:
    """This machine under ``prefer-gcp``; the pre-admission refusal under a pin."""
    if policy == PLACEMENT_PREFER_GCP:
        return Placement(LOCAL_SITE, policy, reason)
    return _refuse(Placement(None, policy, reason))


def require_local_placement(config: DispatchConfig, *, run_kind: str) -> Placement:
    """The placement of a contained launch that can only ever run HERE.

    ``run_tracked_agent``'s contained execution — review remediation, resolve,
    CI fix and every other flow that launches through it — builds its
    container on this machine and has no remote arm. Without this, a ``gcp``
    pin governing its run kind was silently ignored: the run went local while
    the operator had pinned it elsewhere. Asked before admission, so a pin is
    refused with no row written, and a ``prefer-gcp`` policy runs here, which
    is what a preference means (#2526 review).

    Reads only the config: no ledger, no provider, no cloud.
    """
    policy = resolved_policy(config, run_kind)
    if policy == PLACEMENT_LOCAL:
        return Placement(LOCAL_SITE, policy, "placement is local")
    reason = (
        _not_a_dispatch(run_kind)
        if run_kind != DISPATCH_RUN_KIND
        else "this launch path runs its container on this machine only"
    )
    return _here_or_refused(policy, reason)


def _with_held_instances(
    config: DispatchConfig, occupancy: FleetOccupancy
) -> tuple[FleetOccupancy, list[str]]:
    """*occupancy*, with every instance another kew job holds counted full.

    The ledger counts dispatch rows, and a host lock is also taken by work that
    writes none — a sharded verify shard, a remediation, a dispatch in another
    process between its claim and its row. Such an instance would read as free
    here and refuse the placed run with ``HostBusy`` only AFTER admission, in
    the failure arm: an attempt spent, the claim marker undelivered, and a
    ``prefer-gcp`` run failed instead of sent here (#2526 review). Counted
    full, it is skipped the way a busy instance is, so a pin waits and a
    preference falls back — before anything is admitted.

    Returns the held instances' labels too, so the reason names them.
    """
    cap = config.hosts.gcp.max_runs
    held = [
        site
        for site in gcp_sites(config)
        if occupancy.on(site) < cap and provider_for(config, site).held_by_a_job()
    ]
    if not held:
        return occupancy, []
    per_site = {**occupancy.per_site, **dict.fromkeys(held, cap)}
    return FleetOccupancy(per_site), [site.label() for site in held]


def placed_remotely(placement: Placement) -> bool:
    """Whether this placement puts the agent on an instance rather than here.

    One reading of "the site is not this machine", so a caller preparing a
    launch does not spell it out again: the attestation a placed run owes
    (:func:`run_placed_dispatch`'s ``attest_launch``) and the profile it must
    not write at admission are the same question, and two spellings of it
    could disagree.
    """
    return not (placement.site or LOCAL_SITE).is_local


def _refuse(placement: Placement) -> NoReturn:
    """Raise the pre-admission refusal for a placement that found nowhere."""
    raise DispatchPlacementRefused(
        f"this dispatch was not started: {placement.reason} "
        f"(placement policy {placement.policy!r})"
    )


def provider_for(config: DispatchConfig, site: RunSite) -> HostProvider:
    """The host provider bound to the instance *site* names.

    A function of its own, and the ONLY way this module reaches a provider, so
    "a local placement constructs no host provider" is one call site to audit
    rather than a condition spread through the launch.
    """
    from kew.hosts import provider_on_instance

    return provider_on_instance(config, provider=site.host, instance=site.instance)


class _Relayed:
    """The local stream fold, plus whether the agent itself ever spoke.

    The remote placement hands each relayed line here and this hands it
    straight to :func:`kew.dispatch_sandbox.sandbox_log_fold`'s callable, so a
    placed run's sanitized log, derived-evidence sidecar and live telemetry are
    produced by the same code a local container's stream is.

    Everything runs on the CALLING thread, deliberately. The ledger is one
    SQLite connection owned by the dispatch, and a relay on a worker thread
    would have the placement write its own run row from the wrong one.

    :attr:`observed` is the remote equivalent of the daemon's launch signal: a
    line the agent wrote is proof its container ran, and silence is UNKNOWN
    rather than "it never started" — the distinction that keeps a retry from
    discarding work that may have happened.

    The claim-launch marker rides on that same proof. The local launcher
    delivers it once a container handle exists
    (``dispatch_sandbox._deliver_container_launch``), and the marker is what
    stops a refusal from rolling the tracker claim back — so a placement that
    never reached an agent must not have fired it. The first relayed line is
    the earliest moment an instance's container is known to be running, and
    :meth:`deliver` is idempotent so the caller can fire it for a container
    that ran silently without firing it twice.
    """

    def __init__(
        self,
        fold: Callable[[str], None],
        on_launch: Callable[[], None] | None = None,
        budget: RunnerBudgetLatch | None = None,
    ) -> None:
        self._fold = fold
        self._on_launch = on_launch
        self.observed = False
        self.launched = False
        # Reads every protocol line for the runner's own budget stop (#1462).
        self.budget = budget

    def deliver(self) -> None:
        """Deliver the claim-launch marker, once, for a container that ran."""
        from kew.dispatch_core import _invoke_launch

        if self.launched:
            return
        self.launched = True
        _invoke_launch(self._on_launch)

    def __call__(self, line: str) -> None:
        self.observed = True
        # Before the fold, not after: the marker is the claim's, and a line
        # the agent wrote is the proof its container is running. An escape
        # here stops that container (``_relay_placed_agent``), exactly as a
        # local marker failure stops the one it was delivered for.
        self.deliver()
        if self.budget is not None and line.lstrip().startswith("{"):
            self.budget.observe(line)
        # Newline-stripped, as a local container's pipe gives it: the relay
        # reads whole lines off the ssh session and keeps their terminator.
        self._fold(line.rstrip("\n"))


def _materialize_remote_work(
    checkout_dir: Path, *, base_sha: str, artifact_dir: Path, index_file: Path
) -> str:
    """Write the instance's finished work into the OPERATOR's run checkout.

    This is the step that keeps publication on this machine. The instance
    extracted its workspace into a manifest-and-blobs artifact, which
    :func:`~kew.publication_artifact.read_artifact` re-hashes here; the entries
    then go into a git tree through :func:`~kew.publication_apply.apply_to_tree`
    — the broker's own plumbing, where an attacker-chosen path is a string in
    an index rather than a traversal on a filesystem. Once the tree is scanned,
    :func:`~kew.publication_materialize.write_tree_bytes` writes the scanned
    blob bytes over the run's checkout with no git checkout machinery, so no
    filter, attribute conversion or hook runs between the scan and the disk
    (#2569 review). Everything afterwards (the zero-change check,
    shadow mode, the push, the PR) then runs against a checkout holding the
    agent's work, byte-for-byte the path a local container's run takes.

    The base is compared rather than trusted. ``apply_to_tree`` starts from
    *base_sha*'s tree, so an artifact that describes some OTHER base would have
    its changes layered onto a tree it was never diffed against — a publication
    silently missing or re-adding whatever moved in between.

    Untracked files the artifact does not name are left alone, which is what
    the checkout's own generation nonce and prompt handoff depend on: they are
    this machine's, they are not in the base, and a reset that removed them
    would refuse the publication it was setting up.
    """
    from kew.publication_apply import ApplyFailed, apply_to_tree
    from kew.publication_artifact import ArtifactRejected, read_artifact
    from kew.publication_materialize import MaterializeRefused, write_tree_bytes

    try:
        artifact = read_artifact(artifact_dir)
        if artifact.base_sha != base_sha:
            raise RemoteWorkRefused(
                f"the instance returned work against base {artifact.base_sha!r} "
                f"but this run was cut from {base_sha!r}, so applying it would "
                "publish a tree the agent never produced"
            )
        verdict = _filter_remote_work(
            checkout_dir, base_sha=base_sha, artifact=artifact
        )
        # The verdict, not the artifact: an untracked dependency install or
        # cache the filter scoped out never reaches the disk. A flagged entry
        # IS written — ``reviewed=True`` is not a person's approval here but
        # the local publication's own filter holding it for one, downstream.
        tree = apply_to_tree(
            checkout_dir,
            base_sha=base_sha,
            approved=verdict.approved(reviewed=True),
            deletions=verdict.deletions(reviewed=True),
            content=artifact.content,
            index_file=index_file,
        )
        _scan_remote_work(checkout_dir, base_sha=base_sha, tree=tree)
        write_tree_bytes(checkout_dir, base_sha=base_sha, tree=tree)
    except (
        ArtifactRejected,
        ApplyFailed,
        MaterializeRefused,
        OSError,
        CalledProcessError,
    ) as exc:
        raise RemoteWorkRefused(
            f"the work returned by the instance could not be written into "
            f"{checkout_dir}: {exc}"
        ) from exc
    return tree


def _filter_remote_work(
    checkout_dir: Path, *, base_sha: str, artifact: Artifact
) -> FilterVerdict:
    """Refuse placed work the publication filter refuses, before any write (#2636).

    The same :func:`~kew.publication_filter.filter_artifact` call the host's
    promotion makes, against the base tree's paths and modes, so a
    ``.gitattributes`` that introduces a driver, a traversal or a mode
    escalation never reaches the operator's working tree. Review flags are NOT
    refusals: that work is written, and the local publication's own filter
    (:func:`~kew.publication_checkout.filter_checkout_range`, which raises
    ``PublicationHeld``) holds it for a person exactly as for a local run.

    Returns the verdict, whose entries and deletions — not the artifact's —
    are what gets written: the filter scopes out files beneath an untracked
    ``.venv``, ``node_modules`` or generated cache, and those must not land
    on the operator's disk either.
    """
    from kew.publication_filter import filter_artifact

    existing = _base_modes(checkout_dir, base_sha)
    verdict = filter_artifact(
        artifact, existing_modes=existing, destination_paths=existing.keys()
    )
    if verdict.refused:
        reasons = "; ".join(
            f"{r.path}: {r.rule} ({r.detail})" for r in verdict.rejections
        )
        raise RemoteWorkRefused(
            "refusing the work returned by the instance: the publication "
            f"filter refused {reasons}"
        )
    return verdict


def _base_modes(checkout_dir: Path, base_sha: str) -> dict[str, str]:
    """Every path in *base_sha*'s tree with its mode, read with no driver."""
    from kew.publication_apply import inert_git_env

    listing = subprocess_utils.run(
        ["git", "ls-tree", "-r", "-z", f"{base_sha}^{{tree}}"],
        cwd=checkout_dir,
        check=True,
        env=inert_git_env(),
    ).stdout
    modes: dict[str, str] = {}
    for record in listing.split("\0"):
        meta, _, path = record.partition("\t")
        if meta and path:
            modes[path] = meta.split()[0]
    return modes


#: The identity of the throwaway commit the placed work is scanned as. Never
#: published and never referenced: it exists so the scan reads exactly the
#: objects a push of that tree onto the base would send.
_SCAN_IDENTITY = {
    "GIT_AUTHOR_NAME": "kew",
    "GIT_AUTHOR_EMAIL": "kew@invalid.local",
    "GIT_COMMITTER_NAME": "kew",
    "GIT_COMMITTER_EMAIL": "kew@invalid.local",
}


def _scan_remote_work(checkout_dir: Path, *, base_sha: str, tree: str) -> None:
    """Refuse placed work that carries a credential, before the checkout sees it.

    The same output secret scan a local publication runs
    (:func:`kew.publication_scan.scan_output`), over what a push of the
    instance's tree onto the base would transfer (#2569). Work an instance
    returns is data from another machine; a credential in it — a model key the
    agent echoed into a file, a token it found on the VM — is refused HERE,
    before anything is written into the operator's working tree, rather
    than first meeting a scan at push time with the file already on disk.
    """
    from kew.publication_apply import inert_git_env
    from kew.publication_scan import (
        OutputScanError,
        OutputScanTarget,
        output_scan_values,
        scan_output,
    )

    # Unsigned: ``commit.gpgSign`` would run ``gpg.program`` (or wait on a
    # pinentry prompt) for a commit nothing ever publishes.
    head = subprocess_utils.run(
        [
            "git",
            "commit-tree",
            "--no-gpg-sign",
            tree,
            "-p",
            base_sha,
            "-m",
            "kew placed-work scan",
        ],
        cwd=checkout_dir,
        check=True,
        env=inert_git_env(**_SCAN_IDENTITY),
    ).stdout.strip()
    target = OutputScanTarget(
        repository=checkout_dir, base_sha=base_sha, head_sha=head, env=inert_git_env()
    )
    try:
        scan = scan_output(target, output_scan_values())
    except OutputScanError as exc:
        raise RemoteWorkRefused(
            "the work returned by the instance could not be secret-scanned, so "
            f"it is not written into {checkout_dir}: {exc}"
        ) from exc
    if scan.verdict != "clean":
        raise RemoteWorkRefused(
            "refusing the work returned by the instance: the output secret "
            f"scan found {scan.describe()}"
        )


@dataclass(frozen=True)
class RemoteInputs:
    """Everything a remote placement needs that is not the config or the store.

    A record rather than a dozen keyword arguments threaded through three
    functions: every field is read by more than one of them, and a launch that
    described one run's checkout with another run's slug is a mistake the local
    path cannot make because it resolves all of this exactly once.
    """

    run_id: str
    runner: str
    slug: str
    model: str
    #: The image TAG, never this machine's immutable id — see
    #: :func:`_remote_arguments`.
    image_tag: str
    checkout_dir: Path
    #: The commit the operator machine observed in that checkout before launch.
    #: What the instance extracts against and what the artifact must name.
    base_sha: str
    log_file: Path
    #: Host-owned directory the bundle and the returned artifact live under.
    #: Never inside the checkout, which is what gets bundled.
    scratch: Path
    #: The kind the caller admitted this run as. Required, never defaulted:
    #: it is what the credential fence reads, and it is checked against the
    #: admitted row before any credential is derived (:func:`_require_dispatch_row`).
    run_kind: str
    transport: ResolvedInferenceTransport | None = None
    #: This run's bounded, per-run, spend-capped inference key, or None when
    #: the route mints none. The VALUE the instance's container receives for
    #: the runner's declared inference variable — see
    #: :func:`host_model_credentials`, which is the one place it is read.
    credential: BoundedRunKey | None = None
    #: What the ``dispatch_remote_launch`` audit event files this launch under,
    #: so the chain links it to the work item the way every other dispatch
    #: event on it does.
    issue_num: int | None = None
    item_id: str | None = None
    repo: str | None = None


def _remote_arguments(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    inputs: RemoteInputs,
    on_line: Callable[[str], None],
    workspace: Path,
    attest_launch: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The standing arguments one remote placement is made with.

    The image is the TAG and not the immutable id the local launch uses: the
    id names bytes in THIS machine's image store, and the instance resolves the
    same tag against its own
    (:func:`~kew.hosts.remote_dispatch.ensure_agent_image` builds it there when
    it is absent).

    Which is exactly why *attest_launch* exists. A caller that records a
    containment boundary for this run cannot record this machine's immutable
    ID — it describes a different image store — so the placement hands it the
    ID the INSTANCE resolved the tag to, once, after the instance holds the
    image and before any container is launched (#2526 review).

    *store* and *inputs.run_id* travel together so the placement records WHERE
    this run went before it touches the instance, which is what stops another
    process's release from arming an idle stop under it.
    """
    return {
        "config": config,
        "checkout_dir": inputs.checkout_dir,
        "workspace": workspace,
        "image": inputs.image_tag,
        "runner": inputs.runner,
        "slug": inputs.slug,
        "model": inputs.model,
        "base_sha": inputs.base_sha,
        "run_kind": inputs.run_kind,
        "transport": inputs.transport,
        "credentials": host_model_credentials(inputs.runner, inputs.credential),
        "timeout_seconds": WALL_CLOCK_LIMIT_SECONDS,
        "on_line": on_line,
        "store": store,
        "run_id": inputs.run_id,
        "attest_launch": _launch_attestation(store, inputs, attest_launch),
    }


def _launch_attestation(
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    inputs: RemoteInputs,
    attest_launch: Callable[[str], None] | None,
) -> Callable[[RemoteLaunch], None]:
    """What a placement attests once its launch is resolved and not yet run.

    Two records, in this order. The caller's containment boundary first, from
    the instance's image ID and nothing else of the launch record: a boundary
    composed from the host name or the tag would describe something other than
    the bytes the agent runs, and a boundary that cannot be recorded refuses
    the launch before anything crosses to the instance.

    Then the ``dispatch_remote_launch`` audit event
    (:func:`~kew.hosts.remote_dispatch.record_remote_launch`) — the one record
    of the model credential crossing to a VM, by name, and of the immutable
    bytes the instance launched (#2526 review). Written here, before the
    container session that carries the credential opens, so a placed run can
    never have handed a credential over with no chained event saying so; and
    from THIS caller, because it is the only one holding the run's ledger.
    """
    from kew.hosts.remote_dispatch import record_remote_launch

    def attest(launch: RemoteLaunch) -> None:
        if attest_launch is not None:
            attest_launch(launch.image_id)
        record_remote_launch(
            store,
            launch,
            run_id=inputs.run_id,
            issue_num=inputs.issue_num,
            item_id=inputs.item_id,
            repo=inputs.repo,
        )

    return attest


def run_placed_dispatch(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    placement: Placement,
    inputs: RemoteInputs,
    result: DispatchResult,
    local_launch: Callable[[], int],
    on_launch: Callable[[], None] | None = None,
    attest_launch: Callable[[str], None] | None = None,
    provider: HostProvider | None = None,
) -> int:
    """Run this dispatch's agent where *placement* put it, and return its exit.

    A local placement is *local_launch* and nothing else — a thunk the caller
    built from today's arguments, so the default path cannot drift from this
    module, and nothing here constructs a provider, reads an instance pool or
    touches the cloud. Neither callback fires on that path: *local_launch* owns
    both, because the local launcher already delivers the claim marker and
    already attested its boundary with this machine's own image ID.

    *on_launch* is the claim-launch marker and fires only once the instance's
    container is known to be running — the first relayed line, or a clean
    outcome for a container that relayed nothing. *attest_launch* receives the
    instance's immutable image ID before the container is launched.

    Every way a remote placement ends without a verdict — an unreachable
    instance, a lost session, a pre-emption, work that could not be written
    back — becomes a non-zero exit carrying its own ``error_category`` on
    *result*, so the caller finalizes it exactly as it finalizes a local
    container that exited non-zero.

    *provider* is the one :func:`hold_contained_placement` claimed the
    instance with, so the launch re-enters that claim rather than contending
    with it; omitted, one is built for the placement's site.
    """
    site = placement.site or LOCAL_SITE
    if site.is_local:
        return local_launch()
    logger.info(
        "placing dispatch %s on %s: %s", inputs.run_id, site.label(), placement.reason
    )
    try:
        _require_dispatch_row(store, inputs)
        return _run_on_host(
            config,
            store,
            site=site,
            inputs=inputs,
            result=result,
            on_launch=on_launch,
            attest_launch=attest_launch,
            provider=provider,
        )
    except (HostError, RemoteWorkRefused, PlacedRunKindRefused) as exc:
        return _failed_placement(result, site, exc)


def _require_dispatch_row(
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    inputs: RemoteInputs,
) -> None:
    """Refuse a placement whose admitted row is not a dispatch run.

    Read back from the ledger rather than trusted from *inputs*: the kind the
    credential fence is handed has to be the kind the run was admitted as, and
    both have to be ``dispatch``. Checked before the instance is touched and
    before :func:`host_model_credentials` derives anything.
    """
    row = store.get(inputs.run_id)
    admitted = None if row is None else row.run_kind
    if admitted == inputs.run_kind == DISPATCH_RUN_KIND:
        return
    raise PlacedRunKindRefused(
        f"run {inputs.run_id} was admitted as {admitted!r} and launched as "
        f"{inputs.run_kind!r}; only a {DISPATCH_RUN_KIND!r} run may carry a "
        "model credential to an instance, so nothing was placed"
    )


def _failed_placement(result: DispatchResult, site: RunSite, exc: Exception) -> int:
    """Record a placement that reached no verdict, as a failed execution."""
    result.error_category = getattr(exc, "error_category", "") or "remote_host_failed"
    result.stop_reason = f"{site.label()}: {exc}"[:_REASON_CHARS]
    result.exit_code = 1
    logger.warning("the dispatch placed on %s did not finish: %s", site.label(), exc)
    return 1


def _run_on_host(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    site: RunSite,
    inputs: RemoteInputs,
    result: DispatchResult,
    on_launch: Callable[[], None] | None,
    attest_launch: Callable[[str], None] | None = None,
    provider: HostProvider | None = None,
) -> int:
    """One placed dispatch: relay it, then bring its work onto this machine."""
    provider = provider or provider_for(config, site)
    inputs.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"placed-{inputs.slug}-", dir=inputs.scratch)
    )
    try:
        exit_code = _relay_placed_dispatch(
            config,
            store,
            inputs=inputs,
            provider=provider,
            result=result,
            workspace=workspace,
            on_launch=on_launch,
            attest_launch=attest_launch,
        )
        if exit_code == 0:
            tree = _materialize_remote_work(
                inputs.checkout_dir,
                base_sha=inputs.base_sha,
                artifact_dir=workspace / REMOTE_ARTIFACT_DIR,
                index_file=workspace / "apply-index",
            )
            logger.info(
                "the work placed on %s is now tree %s in %s",
                site.label(),
                tree,
                inputs.checkout_dir,
            )
        return exit_code
    finally:
        # The bundle and the returned artifact are this placement's alone; the
        # work they carried is in the checkout by now, or the run has no verdict.
        shutil.rmtree(workspace, ignore_errors=True)


def _relay_placed_dispatch(
    config: DispatchConfig,
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    inputs: RemoteInputs,
    provider: HostProvider,
    result: DispatchResult,
    workspace: Path,
    on_launch: Callable[[], None] | None = None,
    attest_launch: Callable[[str], None] | None = None,
) -> int:
    """Place the dispatch, folding its stream through the LOCAL consumer.

    The claim-launch marker is delivered from the relay, never ahead of it. An
    instance kew could not reach, an image it could not build, a credential
    the fence refused and a daemon that refused the container all end here
    with no agent having run, and the local path fires that marker only once a
    container handle exists — so a placement that reached no agent must not
    attest a launch (#2526 review). The late delivery covers the one case the
    stream cannot: a container that ran, relayed nothing and still reached an
    exit marker, which ``place_dispatch_on_host`` only returns for after the
    daemon has accepted it.
    """
    from kew.dispatch_sandbox import (
        RunnerBudgetLatch,
        classify_runner_budget_stop,
        sandbox_log_fold,
    )
    from kew.hosts.remote_dispatch import place_dispatch_on_host
    from kew.pricing import build_pricing_overrides

    inputs.log_file.parent.mkdir(parents=True, exist_ok=True)
    with sandbox_log_fold(
        inputs.log_file,
        store,
        inputs.run_id,
        model=inputs.model,
        overrides=build_pricing_overrides(config.pricing.overrides),
        runner=inputs.runner,
    ) as fold:
        relayed = _Relayed(fold, on_launch, RunnerBudgetLatch(inputs.runner, config))
        try:
            placed = place_dispatch_on_host(
                provider,
                **_remote_arguments(
                    config,
                    store,
                    inputs=inputs,
                    on_line=relayed,
                    workspace=workspace,
                    attest_launch=attest_launch,
                ),
            )
        finally:
            result.launched = True if relayed.observed else None
    relayed.deliver()
    if placed.release_error:
        # The verdict stands; the VM that may still be billing is said where
        # the operator reads this run, not only in a logger (#2627).
        logger.error("run %s: %s", inputs.run_id, placed.release_error)
        with inputs.log_file.open("a", encoding="utf-8") as log:
            log.write(f"kew: {placed.release_error}\n")
    result.exit_code = placed.exit_code
    # No governor watches a placed run; its own run cap is the only stop.
    classify_runner_budget_stop(relayed.budget, result)
    return placed.exit_code
