"""Where each agent run goes, and how many may be in flight anywhere (#2398).

Epic #2338 node 6. Until a run could land on a Compute Engine instance, "how
many agents are running" and "how many agent processes this machine has" were
the same number, so one host-local count answered both. They come apart the
moment a VM can host a run: the Mac's count no longer sees the agents on the
cloud, and the owner's cap — about three concurrent agents, Mac and VMs
*together*, because VMs REPLACE Mac slots rather than add to them (owner
decision 2026-10-06) — would be silently exceeded.

Two numbers, read off two different things:

* **The fleet cap** (``[fleet] max_concurrent``, defaulting to today's
  ``[parallel] max_workers``) is checked against the LEDGER — every run in a
  running state, whatever host it sits on. That count already existed
  (:meth:`kew.run_store.RunStore.live_run_count`) and already spanned
  processes; what this module adds is that nothing about it is host-local.
* **Per-host capacity** (``[hosts.gcp] max_runs``) is checked against the same
  ledger grouped by placement, which is what makes ``prefer-gcp`` able to tell
  "the cloud has room" from "the cloud is full". It is one run per instance,
  and config refuses more than that, because that is what the host transport
  honours: a job holds its instance's lock for the whole session and every job
  on that machine shares one checkout, so a *second* run placed on a busy
  instance is refused rather than run beside the first.

The placement decision is deliberately best-effort, exactly as the epic
driver's dispatch budget is: two dispatchers could read the same free-slot
count. The AUTHORITATIVE last-slot guard stays where it already is — the
atomic cap-count-then-insert in ``RunStore.try_admit_run`` — and over-placing
on a host costs a queue wait, never a breached cap.

The third thing a placement is needed for is a stop, not a start: an idle stop
must never take down a machine that still has an agent on it, so
:func:`instance_is_occupied` is what the release path asks before arming one.

Both of those reads are only as good as the write behind them.
:func:`record_placement` is that write, and the placement path itself performs
it, because ``runs.run_host`` defaults to ``local``: a column no one writes does
not read as unknown, it reads as *every instance is free* — which is exactly the
answer that stops a VM with an agent still on it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kew.config import (
    PLACEMENT_GCP,
    PLACEMENT_LOCAL,
    PLACEMENT_PREFER_GCP,
    DispatchConfig,
)
from kew.run_store import LOCAL_RUN_HOST, RunRecord

#: Probes one pending run's owner, three-state: True alive, False *provably*
#: dead, None unprobeable. The same contract ``RunStore.live_run_count`` takes.
RunLiveness = Callable[[RunRecord], bool | None]


class PlacementNotRecorded(RuntimeError):
    """A placement could not be written to the ledger, so it was not made.

    Raised by :func:`record_placement` rather than returned, because every
    caller of it is about to attach a run to a machine on the strength of that
    write: the guard a release asks (:func:`instance_is_occupied`) reads the
    ledger, so a placement nothing recorded reads as *that instance is free*.
    """


@dataclass(frozen=True)
class RunSite:
    """One place an agent run can be: a host, and the machine within it.

    Frozen (so it keys a count) and instance-bearing because ``gcp`` is a POOL:
    ``[hosts.gcp] max_runs`` is per instance, so "on gcp" is not a fine enough
    answer to decide whether there is room for another run.
    """

    host: str = LOCAL_RUN_HOST
    instance: str = ""

    @property
    def is_local(self) -> bool:
        """True for the operator machine, which is the one host with no pool."""
        return self.host == LOCAL_RUN_HOST

    def label(self) -> str:
        """How this site reads to an operator: ``local`` or ``gcp/instance-1``."""
        return f"{self.host}/{self.instance}" if self.instance else self.host


#: The operator machine. One instance-less site, because there is one of it.
LOCAL_SITE = RunSite(LOCAL_RUN_HOST)


def run_site(record: RunRecord) -> RunSite:
    """The site *record* is placed on, as the ledger recorded it.

    A pre-v66 row carries no placement and reads as ``local``, which is where
    it ran: there was no other host when it was written.
    """
    return RunSite(record.run_host or LOCAL_RUN_HOST, record.run_host_instance or "")


def is_remote(record: RunRecord) -> bool:
    """True when this run's agent is NOT on the machine asking.

    What local probes must consult before they conclude anything: a container
    on a VM is absent from this machine's daemon, and "absent" is the local
    probe's word for *provably dead*.
    """
    return not run_site(record).is_local


def _live_pending(
    runs: Sequence[RunRecord], liveness: RunLiveness | None
) -> list[RunRecord]:
    """*runs*, minus the ones a probe says are PROVABLY dead.

    Prove dead, never infer: a ``None`` (unprobeable, mid-startup, or on
    another machine) stays counted, so an occupancy read never frees a slot or
    a VM a live agent still holds.
    """
    if liveness is None:
        return list(runs)
    return [run for run in runs if liveness(run) is not False]


@dataclass(frozen=True)
class FleetOccupancy:
    """How many in-flight runs sit on each site, read off the ledger.

    One snapshot answers both questions a placement asks — is the fleet full,
    and is this instance full — so a caller takes one read rather than two that
    can disagree.
    """

    per_site: Mapping[RunSite, int]

    @property
    def total(self) -> int:
        """In-flight runs anywhere: the fleet-wide number the cap bounds."""
        return sum(self.per_site.values())

    def on(self, site: RunSite) -> int:
        """In-flight runs on exactly *site*."""
        return self.per_site.get(site, 0)


def abandoned_after_seconds(config: DispatchConfig) -> float:
    """How old a ``pending`` row may get before it is a corpse, not a run.

    Twice the agent's own ceiling: a run kew is executing cannot outlive
    ``[agent] timeout_minutes`` by that margin, so a row older than this was
    stranded by a hard kill and no longer describes anything running. The same
    justification ``kew.service.work.PENDING_RUN_MAX_AGE_S`` gives for its own
    floor, derived from config here rather than imported, because this module
    sits below the service layer.

    Deliberately NOT used by the fleet cap. Age is inference, and the cap's rule
    is prove dead or keep the slot — over-admitting agents is the failure the cap
    exists to prevent. It is used by the idle-stop question, where the cost
    asymmetry runs the other way: a VM kept up by a corpse bills forever for
    nothing, and "nothing young enough to protect is on this instance" is not a
    claim about whether any process is alive.
    """
    return 2.0 * 60.0 * config.agent.timeout_minutes


def _younger_than(
    runs: Sequence[RunRecord], bound: float | None, now: datetime
) -> list[RunRecord]:
    """*runs*, minus rows that started longer than *bound* seconds ago.

    A naive stamp is read as UTC, the only timezone any kew writer stamps in —
    tolerated, never repaired. An unreadable age keeps the row: the filter's job
    is to release a VM a corpse pins, never to drop a row it cannot age.
    """
    if bound is None:
        return list(runs)
    kept = []
    for run in runs:
        started = run.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if (now - started).total_seconds() <= bound:
            kept.append(run)
    return kept


def read_occupancy(
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    exclude_run_id: str | None = None,
    liveness: RunLiveness | None = None,
    abandoned_after: float | None = None,
    now: datetime | None = None,
) -> FleetOccupancy:
    """Count the fleet's in-flight runs per site, off ``runs`` alone.

    ``pending`` IS the in-flight state: the row is written at admission and
    flipped off ``pending`` by every terminal path, so it is also the slot. That
    also makes this the "keep warm" signal: a run admitted and placed on an
    instance but not yet launched already has its row, so the instance it is
    queued for reads as occupied before its container exists.

    Epic reservations are deliberately NOT counted here — a reserved node has not
    been placed yet, so it belongs to no site — which is why this is the PER-HOST
    signal and ``live_run_count`` stays the fleet-cap signal.

    *exclude_run_id* drops one run so a caller can ask "is anything ELSE on
    this instance": the finishing run's own row is still ``pending`` while its
    release path runs. *abandoned_after* drops rows too old to describe a
    running agent (:func:`abandoned_after_seconds`); omitted, every row counts.
    """
    runs = [
        run
        for run in store.list_pending()
        if exclude_run_id is None or run.run_id != exclude_run_id
    ]
    live = _younger_than(
        _live_pending(runs, liveness), abandoned_after, now or datetime.now(UTC)
    )
    return FleetOccupancy(dict(Counter(run_site(run) for run in live)))


def gcp_sites(config: DispatchConfig) -> tuple[RunSite, ...]:
    """Every gcp instance a run may be placed on, in the pool's own order.

    The pool, not the single ``instance`` key: ``[hosts.gcp] instances`` is
    already the list a sharded verify spreads across, and placement spreads
    across the same machines rather than inventing a second pool.
    """
    gcp = config.hosts.gcp
    if not gcp.configured():
        return ()
    return tuple(RunSite(PLACEMENT_GCP, name) for name in gcp.pool())


def free_gcp_site(config: DispatchConfig, occupancy: FleetOccupancy) -> RunSite | None:
    """The first gcp instance with room for another run, or None if all are full.

    First-with-room rather than least-loaded, on purpose: a cold instance costs
    a start and a warm one costs nothing, so filling the pool in order keeps
    the tail of it stopped — which is the whole point of the idle stop.

    The capacity spent here is the capacity a host will honour: ``max_runs`` is
    bounded at 1 where it is read from config
    (:meth:`~kew.config.GcpHostConfig.runs_the_host_transport_honours`), so this
    never hands back an instance whose lock would refuse the run and never
    swallows the ``prefer-gcp`` fallback that refusal would land on (PR #2466
    review). Bounded THERE rather than clamped here, so an operator who writes a
    number kew cannot carry out is told at load.
    """
    cap = config.hosts.gcp.max_runs
    for site in gcp_sites(config):
        if occupancy.on(site) < cap:
            return site
    return None


@dataclass(frozen=True)
class Placement:
    """Where one run goes, and why — or that it has to wait.

    ``site is None`` means WAIT: the policy named a host and that host is full.
    It never means "fall back to the Mac"; only ``prefer-gcp`` says that, and
    when it does the site is the Mac.
    """

    site: RunSite | None
    policy: str
    reason: str

    @property
    def waits(self) -> bool:
        """True when no host could take this run now."""
        return self.site is None


def resolved_policy(config: DispatchConfig, run_kind: str | None = None) -> str:
    """The placement policy that governs *run_kind*.

    Delegates to :meth:`~kew.config.SandboxConfig.host_for`, which is where the
    pin-outranks-policy rule lives, so there is ONE answer to "what governs this
    run kind" rather than a config reader and a placement reader that could
    drift. *run_kind* of ``None`` asks the fleet question instead: the policy
    with no kind to pin against.
    """
    sandbox = config.sandbox
    if run_kind is None:
        return sandbox.placement_policy()
    return sandbox.host_for(run_kind)


def place_run(
    config: DispatchConfig,
    occupancy: FleetOccupancy,
    *,
    run_kind: str | None = None,
) -> Placement:
    """Decide which site *run_kind*'s next run goes to under the fleet policy.

    This answers WHERE, never WHETHER: the fleet cap is a separate question,
    checked against the ledger by the admission path, and a placement is only
    asked for once a run has a slot. Keeping them apart is what makes the cap
    host-blind — the fourth run waits on the cap whichever host it would have
    gone to.
    """
    policy = resolved_policy(config, run_kind)
    if policy == PLACEMENT_LOCAL:
        return Placement(LOCAL_SITE, policy, "placement is local")
    if policy not in (PLACEMENT_GCP, PLACEMENT_PREFER_GCP):
        # Unreachable through config, which refuses an unknown policy and an
        # unknown per-kind host at load. Spelled as a WAIT rather than a
        # fallback anyway: landing on the Mac would be the silent clean default
        # the load-time check exists to prevent, one layer further in.
        return Placement(None, policy, f"{policy!r} is not a placement kew makes")
    site = free_gcp_site(config, occupancy)
    if site is not None:
        return Placement(site, policy, f"{site.label()} has room")
    if policy == PLACEMENT_PREFER_GCP:
        return Placement(
            LOCAL_SITE,
            policy,
            f"every gcp instance is at max_runs = {config.hosts.gcp.max_runs}",
        )
    return Placement(None, policy, _gcp_unavailable(config))


def _gcp_unavailable(config: DispatchConfig) -> str:
    """Why a ``gcp`` placement found nowhere to go — unconfigured, or full."""
    if not gcp_sites(config):
        return (
            "placement is gcp but no gcp instance is configured "
            "([hosts.gcp] project, zone and instance)"
        )
    return (
        f"placement is gcp and every instance is at "
        f"max_runs = {config.hosts.gcp.max_runs}"
    )


def record_placement(
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    run_id: str,
    host: str,
    instance: str,
) -> None:
    """Write WHERE this run went onto the ledger (#2398).

    The WRITE half of every read in this module, and the reason it is a
    function rather than a line inside one placement: ``run_host`` and
    ``run_host_instance`` are columns, so a reader that nothing writes to does
    not report "unknown" — it reports ``local``, which reads as *this instance
    is free* and is the one answer that arms an idle stop under a working
    agent. The placement itself calls this (see
    :func:`kew.hosts.remote_dispatch.place_dispatch_on_host`), so the writer and
    :func:`instance_is_occupied` are the same path and cannot drift.

    Written BEFORE the container starts, and before the host claim it waits on:
    the instance a run is queued for has to read as occupied for the whole time
    that run is attached to it, not just while there is a container to see.
    Over-reporting an instance as busy costs a VM some idle minutes;
    under-reporting it costs the run on it.

    Raises :class:`PlacementNotRecorded` when the write matched no run row —
    the run was never admitted, or its row is already gone. The underlying
    ``UPDATE`` is a no-op to SQLite, and the ledger afterwards is
    indistinguishable from a run that is still local, so a silent return here
    would leave the guard with the one answer that stops a VM under a working
    agent. The caller is the placement path, which has not touched the instance
    yet, so refusing costs a queue wait and never a running agent.
    """
    if not store.set_run_site(run_id, host or LOCAL_RUN_HOST, instance or None):
        raise PlacementNotRecorded(
            f"no run row for {run_id!r}: the placement on "
            f"{RunSite(host or LOCAL_RUN_HOST, instance).label()} was not recorded, "
            "so nothing would hold that instance against an idle stop"
        )


def instance_is_occupied(
    store: Any,  # noqa: ANN401 — the RunStore protocol; kept loose for fakes
    *,
    instance: str,
    host: str = PLACEMENT_GCP,
    exclude_run_id: str | None = None,
    liveness: RunLiveness | None = None,
    abandoned_after: float | None = None,
    now: datetime | None = None,
) -> bool:
    """True when *instance* still has an in-flight run on it.

    What a release path asks before it arms an idle stop, and it asks about the
    LEDGER rather than about the lock the finishing run is holding. Those are
    different questions: a placement is recorded before its claim is taken
    (:func:`record_placement`), so a second process can have this instance's
    name on a row of its own while the first run is finishing — and an instance
    is also attached to a run that is queued for it and has no container yet.
    Arming a stop against either costs the run on the machine; skipping one
    costs idle minutes, which is the cheaper way to be wrong. It is the same
    read any per-instance capacity above 1 would need to be safe, and the
    reason that capacity cannot be raised without it.

    *host* is the provider the instance belongs to, defaulting to the one host
    that is a pool. It is a parameter rather than a constant so the caller can
    pass the SAME provider name :func:`record_placement` stored — a reader
    asking about ``gcp/vm-one`` while the writer recorded some other host's
    ``vm-one`` would find the instance free with a run on it.

    An unidentifiable instance (empty name) is reported occupied: it is the
    answer that cannot stop a running agent, and a stop skipped costs money
    while a stop taken wrongly costs the run.

    *abandoned_after* bounds how long a stranded row may hold the machine up
    (:func:`abandoned_after_seconds`). Without it a run killed on a VM pins that
    VM for good: nothing on this machine can prove its container dead, so no
    later release ever arms a stop and the instance bills forever.
    """
    if not instance.strip():
        return True
    occupancy = read_occupancy(
        store,
        exclude_run_id=exclude_run_id,
        liveness=liveness,
        abandoned_after=abandoned_after,
        now=now,
    )
    return occupancy.on(RunSite(host or LOCAL_RUN_HOST, instance.strip())) > 0


def placement_lines(runs: Sequence[RunRecord]) -> list[str]:
    """One ``#123  dispatch  gcp/instance-1`` line per in-flight run.

    Shared by ``kew status`` and anything else that reports the fleet, so the
    CLI and the console cannot describe a placement two ways.
    """
    return [
        f"  #{run.issue_num:<5} {run.run_kind:<10} {run_site(run).label()}"
        for run in runs
    ]
