"""The exact-boundary live readiness probe (I05B).

Static capability profiles (I03), realized launch boundaries (I03C) and the
persisted probe ledger (I05A) all answer questions about a route without ever
calling a provider. They are therefore *configured, live-unverified*: every
declared fact can be right while the leg still cannot complete a single turn.

This module closes exactly that gap and nothing wider. One explicit operator
action — ``kew doctor route-live-confirm`` — spends at most
:data:`PROBE_CEILING_USD` on ONE bounded call across the exact boundary a
dispatch would launch, and records the answer as durable evidence for that
boundary's fingerprint. It is not a certification, it never resumes, it never
retries, and no read-only surface can reach it.

Resolving WHICH leg a confirm would cross spends nothing, and read-only
surfaces need that answer (the dispatch launchpad, #2302), so it lives in
:mod:`kew.probe_route` and is imported here. The split is what lets a web page
ask "what would run?" while the code that can take a lease and make a call
stays behind this module's single door.

What the probe deliberately does NOT reuse from dispatch is the *task* shape:
no work prompt, no permission skipping, no customer checkout, and none of the
tracker or publication credentials a real run is handed. The boundary is
exact; the work is empty.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from kew import subprocess_utils, work_dirs
from kew.budget import BudgetExceeded, check_window_budget, window_budget_hold_reason
from kew.config import DispatchConfig
from kew.credential_surface import redact_credentials
from kew.inference_transport import ResolvedInferenceTransport
from kew.probe_route import PROBE_PROMPT, ProbeRoute, resolve_probe_route
from kew.progress import _warn, display_suppressed
from kew.readiness import ProbeCost
from kew.run_credentials import launch_environment
from kew.run_store import SqliteRunStore
from kew.run_store_readiness import (
    ProbeLeaseHeldError,
    ProbeState,
    ProbeVerdict,
    StoredReadinessProbe,
)
from kew.runner_fields import exception_text
from kew.runners.capabilities import CostSignal, capability_manifest
from kew.runners.protocol import RUN_TOTALS_KEY, AgentRunner, RunTotals
from kew.sandbox import proxy_redaction_scope

if TYPE_CHECKING:
    from kew.switchyard_supervisor import SidecarAttemptSupervisor

PROBE_TIMEOUT_SECONDS: Final = 90
"""Hard wall-clock cap on the one provider call, enforced by kew, not asked for.

Ninety seconds is far longer than one "reply OK" turn needs and far shorter
than any dispatch timeout, so a hung leg is reported as hung rather than
becoming an unbounded charge on a diagnostics command.
"""

PROBE_CEILING_USD: Final = 0.02
"""The conservative planned ceiling one probe may cost.

Declared before the call, persisted as the probe's planned cost, and used as
the stand-in whenever the actual spend is unknowable (I05A). A ceiling this
low is only honest because the call is one tool-free turn.
"""

PROBE_CEILING_SOURCE: Final = "doctor route live-confirm declared ceiling"

PROBE_EXPECTED_REPLY: Final = "OK"


class LiveStatus(StrEnum):
    """What kew can say about one exact boundary's *live* validity right now.

    ``UNVERIFIED`` is the honest default and the point of the whole slice: a
    route whose every static check passes is still unverified until a call has
    actually crossed the boundary. ``REFUSED`` means kew declined to spend —
    a refusal is never evidence about the route.
    """

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNVERIFIED = "unverified"
    REFUSED = "refused"


class LiveProbeUnsupported(RuntimeError):
    """kew cannot express a single-turn, tool-free call for this runner."""


# ---------------------------------------------------------------------------
# The bounded call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeBound:
    """How ONE runner's CLI expresses a single-turn, budget-capped call.

    Deliberately a declaration rather than a call into
    ``AgentRunner.build_command``: that composes the *dispatch* shape —
    permission skipping, the operator's whole budget, a work prompt — and a
    probe must carry none of it. A runner with no entry here cannot be probed
    at all (:class:`LiveProbeUnsupported`) rather than probed unbounded.
    """

    stream: tuple[str, ...]
    one_turn: tuple[str, ...]
    budget_flag: str
    model_flag: str
    prompt_flag: str

    def args(self, *, model: str, ceiling: float, prompt: str | None) -> list[str]:
        """Flags for one bounded call; ``prompt=None`` for the mounted handoff."""
        args = [*self.stream, *self.one_turn, self.budget_flag, str(ceiling)]
        if model:
            args.extend([self.model_flag, model])
        if prompt is not None:
            args.extend([self.prompt_flag, prompt])
        return args


PROBE_BOUNDS: Final[Mapping[str, ProbeBound]] = {
    # Claude Code. ``--max-turns 1`` is the ENFORCED bound; "no tools" is not a
    # second flag but a consequence — a turn spent calling a tool is a turn
    # that never emits the exact reply the probe demands, so a tool-using
    # answer fails the probe rather than passing it quietly. Note the absence
    # of ``--dangerously-skip-permissions``: without it a non-interactive turn
    # cannot get tool permission in the first place.
    "claude-code": ProbeBound(
        stream=("--verbose", "--output-format", "stream-json"),
        one_turn=("--max-turns", "1"),
        budget_flag="--max-budget-usd",
        model_flag="--model",
        prompt_flag="-p",
    ),
}


def probe_bound(runner_name: str) -> ProbeBound:
    """The declared single-turn bound for *runner_name*, or refuse."""
    bound = PROBE_BOUNDS.get(runner_name)
    if bound is None:
        raise LiveProbeUnsupported(
            f"kew cannot express a bounded one-turn call for runner "
            f"{runner_name!r}, so it will not spend on one. Supported: "
            f"{', '.join(sorted(PROBE_BOUNDS))}"
        )
    return bound


@dataclass(frozen=True)
class ProbeLaunch:
    """One bounded call's argv, environment, and the resources it must release.

    ``workspace`` is an empty host-owned repository created for this probe and
    deleted with it: no customer checkout is ever mounted or entered.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    workspace: Path
    container_name: str | None = None
    runtime: str | None = None
    proxy_environment: str = "inherit"
    # A prepared worker's full id; a ``docker run`` launch records its own in
    # the cidfile its argv names instead.
    container_id: str | None = None


def new_probe_id() -> str:
    """A fresh probe id that is also a valid container-name component."""
    return f"readiness-probe-{uuid.uuid4().hex[:12]}"


def create_probe_workspace(probe_id: str) -> Path:
    """An EMPTY host-owned git repository for one probe, and nothing else.

    Host-owned because the operator's machine created it and owns its lifetime;
    empty because a probe proves the boundary, not the ability to read someone's
    code. A git repository rather than a bare directory so runners that refuse
    to start outside one still launch at their production boundary.
    """
    workspace = work_dirs.make_work_dir("kew-readiness-probe-")
    # `kew doctor` can run outside a dispatch preflight, so bind the resolved
    # git path the same way `kew sandbox smoke` does rather than widening the
    # allowlist. No git on PATH leaves a plain empty directory, which is still
    # an honest empty workspace.
    found = shutil.which("git")
    if found is not None:
        subprocess_utils.TOOL_PATHS.setdefault("git", found)
        subprocess_utils.run(["git", "init", "--quiet", str(workspace)], check=False)
    return workspace


def probe_env() -> dict[str, str]:
    """The child environment: inference credentials in, customer access out."""
    env = launch_environment(subprocess_utils.strip_publication_credentials(), None)
    # A probe answers a question; it never publishes one.
    env["KEW_READINESS_PROBE"] = "1"
    return env


def build_probe_launch(route: ProbeRoute, *, probe_id: str) -> ProbeLaunch:
    """Compose the exact bounded call for *route*, host or contained."""
    if _route_uses_sidecar(route):
        raise LiveProbeUnsupported(
            "a sidecar probe requires its isolated topology lifecycle"
        )
    bound = probe_bound(route.runner_name)
    workspace = create_probe_workspace(probe_id)
    env = probe_env()
    if route.image is None:
        return _host_launch(route, bound, workspace=workspace, env=env)
    return _contained_launch(route, bound, workspace=workspace, env=env, slug=probe_id)


def _host_launch(
    route: ProbeRoute, bound: ProbeBound, *, workspace: Path, env: dict[str, str]
) -> ProbeLaunch:
    requirement = subprocess_utils.RUNNER_REQUIREMENTS.get(route.runner_name)
    if requirement is None:
        raise LiveProbeUnsupported(
            f"runner {route.runner_name!r} declares no CLI to probe"
        )
    argv = [
        requirement[0],
        *bound.args(model=route.model, ceiling=PROBE_CEILING_USD, prompt=PROBE_PROMPT),
    ]
    return ProbeLaunch(argv=tuple(argv), env=env, cwd=workspace, workspace=workspace)


def _contained_launch(
    route: ProbeRoute,
    bound: ProbeBound,
    *,
    workspace: Path,
    env: dict[str, str],
    slug: str,
) -> ProbeLaunch:
    from kew.sandbox import build_run_argv, write_prompt_handoff

    assert route.image is not None  # nosec B101 — caller branched on it
    runtime = route.sandbox.runtime or "docker"
    # The container's entrypoint feeds the mounted prompt file to the CLI, so
    # the prompt travels as a file exactly as it does for a dispatch.
    write_prompt_handoff(workspace, PROBE_PROMPT)
    argv = build_run_argv(
        runtime=runtime,
        image=route.image,
        checkout_dir=workspace,
        slug=slug,
        network=route.network,
        proxy_environment=route.proxy_environment,
        runner=route.runner_name,
        command=bound.args(model=route.model, ceiling=PROBE_CEILING_USD, prompt=None),
        caps=route.caps,
    )
    return ProbeLaunch(
        argv=tuple(argv),
        env=launch_environment(env, None, contained=True),
        cwd=workspace,
        workspace=workspace,
        container_name=f"kew-{slug}",
        runtime=runtime,
        proxy_environment=route.proxy_environment,
    )


# ---------------------------------------------------------------------------
# Executing it, once, under a hard cap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeExecution:
    """What one bounded call actually did. No interpretation yet."""

    exit_code: int
    reply: str = ""
    reported_model: str = ""
    reported_cost_usd: float | None = None
    timed_out: bool = False
    detail: str = ""
    # Defaults preserve direct/native execution. Sidecar lifecycle failures
    # distinguish an unattempted call from an unverified resource release.
    call_attempted: bool = True
    cleanup_acknowledged: bool = True

    def __post_init__(self) -> None:
        """Redact provider text and diagnostics before judgment or retention."""
        for name in ("reply", "reported_model", "detail"):
            object.__setattr__(self, name, redact_credentials(getattr(self, name)))


def _terminate(launch: ProbeLaunch, proc: subprocess.Popen[str]) -> None:
    """Stop the call at the cap, container first, then the process group.

    Killing the local client does not stop a container, so a contained probe is
    reached by the id its launch recorded, as a dispatch kill switch reaches
    one. With no id recorded the daemon has not started it, and killing the
    client keeps it from starting (#1963).
    """
    target = _recorded_container_id(launch)
    if target is not None and launch.runtime is not None:
        subprocess_utils.run([launch.runtime, "kill", target], check=False)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):  # already gone — nothing to reap
        proc.kill()


def _recorded_container_id(launch: ProbeLaunch) -> str | None:
    """The contained probe's full container id, or None while none is recorded."""
    if launch.container_id is not None or launch.container_name is None:
        return launch.container_id
    from kew.sandbox import _argv_cidfile

    cidfile = _argv_cidfile(list(launch.argv))
    if cidfile is None:
        return None
    try:
        identity = cidfile.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        _warn(
            f"Readiness probe: no container id recorded for {launch.container_name} "
            f"({exception_text(exc)}); the container is not killed by its name"
        )
        return None
    return identity if re.fullmatch(r"[0-9a-f]{64}", identity) else None


def execute_probe(
    launch: ProbeLaunch,
    runner: AgentRunner,
    *,
    timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
    on_started: Callable[[subprocess.Popen[str]], None] | None = None,
) -> ProbeExecution:
    """Run the one call, bounded, and report what came back.

    ``on_started`` fires with the live process BEFORE anything is read or
    waited on — that ordering is what makes a process death mid-call
    recoverable: the handles needed to clean the corpse up are already durable
    when the crash window opens.

    A contained launch that the runtime refused for its CPU cap — the daemon
    lost CPUs between the probe of its count and this launch — is retried
    once at one CPU, under the same deadline, through the same guard dispatch
    uses. The refused launch never reached the provider, so the one-call
    invariant holds; without the retry the probe recorded a REFUTED result
    for a boundary that was never exercised.
    """
    with proxy_redaction_scope(launch.proxy_environment, environment=launch.env):
        deadline = time.monotonic() + timeout_seconds
        execution, lines = _execute_probe_once(
            launch, runner, deadline=deadline, on_started=on_started
        )
        retry = _cpu_cap_retry_launch(launch, execution, lines)
        if retry is None:
            return execution
        execution, _ = _execute_probe_once(
            retry, runner, deadline=deadline, on_started=on_started
        )
        return execution


def _cpu_cap_retry_launch(
    launch: ProbeLaunch, execution: ProbeExecution, lines: Sequence[str]
) -> ProbeLaunch | None:
    """The one-CPU relaunch after a confirmed pre-creation CPU-cap refusal."""
    if launch.container_name is None or execution.timed_out:
        return None
    from kew.sandbox import _cpu_cap_rejection_maximum, _cpu_cap_retry_argv

    maximum = None
    for line in lines:
        found = _cpu_cap_rejection_maximum(line)
        if found is not None:
            maximum = found
    if maximum is None:
        return None
    retry_argv = _cpu_cap_retry_argv(list(launch.argv), execution.exit_code, maximum)
    if retry_argv is None:
        return None
    return replace(launch, argv=tuple(retry_argv))


def _execute_probe_once(
    launch: ProbeLaunch,
    runner: AgentRunner,
    *,
    deadline: float,
    on_started: Callable[[subprocess.Popen[str]], None] | None,
) -> tuple[ProbeExecution, list[str]]:
    """One launch, capped at *deadline*; returns the result and its raw lines.

    No time left means no launch at all: a retry that reaches the provider
    after the declared cap is a provider call nobody is watching.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return (
            ProbeExecution(
                exit_code=-1,
                timed_out=True,
                detail="no time left in the cap for another launch",
            ),
            [],
        )
    proc = subprocess_utils.popen(
        list(launch.argv),
        cwd=launch.cwd,
        env=dict(launch.env),
        start_new_session=True,
    )
    try:
        timed_out = threading.Event()

        def _cap() -> None:
            timed_out.set()
            _terminate(launch, proc)

        # Armed BEFORE on_started: the cap must hold from the moment the child
        # exists, not from whenever the caller's bookkeeping returns.
        watchdog = threading.Timer(remaining, _cap)
        watchdog.start()
        try:
            if on_started is not None:
                on_started(proc)
            lines = list(proc.stdout or ())
            exit_code = proc.wait()
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            return (
                ProbeExecution(
                    exit_code=exit_code,
                    timed_out=True,
                    detail=f"no reply within the {remaining:g}s cap",
                ),
                lines,
            )
        return _read_probe_output(runner, lines, exit_code), lines
    finally:
        # An unwind with the call still live — on_started raising, an unexpected
        # error mid-read, or a BaseException like SIGINT — must not orphan the
        # provider process. A normal return already reaped it via proc.wait(),
        # so poll() is not None and this is a no-op on the happy/timeout paths.
        if proc.poll() is None:
            _terminate(launch, proc)


def _read_probe_output(
    runner: AgentRunner, lines: Iterable[str], exit_code: int
) -> ProbeExecution:
    """Extract the reply and the runner's own end-of-run totals.

    Parsed through the runner's own adapter (muted — a diagnostics command
    must not narrate an agent transcript) so the probe reads the same stream
    contract dispatch does rather than a second parser that could disagree.
    """
    reply = ""
    totals: RunTotals | None = None
    with display_suppressed():
        for line in lines:
            for event in runner.parse_line(line):
                if event.event_type == "activity" and event.message.strip():
                    reply = event.message.strip()
                found = event.metadata.get(RUN_TOTALS_KEY)
                if isinstance(found, RunTotals):
                    totals = found
    return ProbeExecution(
        exit_code=exit_code,
        reply=reply,
        reported_model=(totals.model_display or totals.model or "")
        if totals is not None
        else "",
        reported_cost_usd=totals.cost_usd if totals is not None else None,
    )


# ---------------------------------------------------------------------------
# Judging it
# ---------------------------------------------------------------------------


def models_agree(requested: str, reported: str) -> bool:
    """Whether the model that answered is the model that was asked for.

    A provider may answer a family alias with the dated snapshot behind it
    (``claude-opus-5`` → ``claude-opus-5-20260401``), which is the same route.
    Anything else is a requested/reported MISMATCH: the boundary that answered
    is not the boundary the operator is being told about.
    """
    if not requested or not reported:
        return True
    return requested == reported or reported.startswith(requested)


def judge_probe(execution: ProbeExecution, *, requested_model: str) -> tuple[bool, str]:
    """Did this exact boundary complete one turn, honestly? With the reason."""
    if execution.timed_out:
        return False, execution.detail or "the call exceeded its cap"
    if execution.exit_code != 0:
        return False, f"the runner exited {execution.exit_code}"
    if execution.reply != PROBE_EXPECTED_REPLY:
        return False, (
            f"expected exactly {PROBE_EXPECTED_REPLY!r}, got {execution.reply[:80]!r}"
        )
    if not models_agree(requested_model, execution.reported_model):
        return False, (
            f"requested {redact_credentials(requested_model)!r} "
            f"but {execution.reported_model!r} answered"
        )
    return True, (
        f"{redact_credentials(requested_model)} replied "
        f"{PROBE_EXPECTED_REPLY} across the boundary"
    )


def interpret_probe_cost(runner_name: str, reported_usd: float | None) -> ProbeCost:
    """Turn a runner's reported spend into a cost with honest certainty.

    Three truths, kept apart because collapsing them is how $0 lies:

    * **priced** — the runner reports dollars: MEASURED, verbatim.
    * **unpriced** — local inference that genuinely costs nothing: MEASURED
      zero, because "free" here is a fact, not an absence.
    * **subscription / silent** — the call is real and metered somewhere kew
      cannot see: UNKNOWN, which I05A folds into spend at the declared ceiling
      rather than as a free call.
    """
    try:
        signal_ = capability_manifest(runner_name).cost
    except ValueError:
        return ProbeCost.unknown()
    if signal_ is CostSignal.UNPRICED:
        return ProbeCost.observed(0.0, source=f"{runner_name} inference is unpriced")
    if signal_ is CostSignal.UNAVAILABLE or reported_usd is None:
        return ProbeCost.unknown()
    return ProbeCost.observed(
        float(reported_usd), source=f"{runner_name} reported usage"
    )


# ---------------------------------------------------------------------------
# Supporting access — rechecked on every confirm, never cached
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportingAccess:
    """Whether the source and forge legs around the route are usable."""

    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def check_supporting_access(config: DispatchConfig) -> SupportingAccess:
    """Recheck source and forge auth/reachability. Every confirm, from scratch.

    Cached answers are exactly what this action exists to stop trusting: an
    operator asking "is this route live right now" is not asking what was true
    the last time something looked.
    """
    failures: list[str] = []
    forge = subprocess_utils.run(["gh", "auth", "status"], check=False)
    if forge.returncode != 0:
        failures.append("forge: gh is not authenticated (run: gh auth login)")
    tracker = getattr(config.tracker, "type", "github")
    if tracker == "linear" and not os.environ.get("LINEAR_API_KEY"):
        failures.append("source: LINEAR_API_KEY is not set for the linear tracker")
    elif tracker != "linear":
        failures.extend(_github_source_failures(config, forge.returncode))
    return SupportingAccess(failures=tuple(failures))


def _github_source_failures(config: DispatchConfig, forge_rc: int) -> list[str]:
    repo = config.project.repo.strip()
    if not repo:
        return ["source: no project.repo is configured"]
    if forge_rc != 0:
        return []  # already reported as a forge failure; one cause, one message
    reachable = subprocess_utils.run(
        ["gh", "repo", "view", repo, "--json", "name"], check=False
    )
    if reachable.returncode != 0:
        return [f"source: {repo} is not reachable through gh"]
    return []


# ---------------------------------------------------------------------------
# Reading stored evidence — the ONLY thing a read-only surface may call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveConfirmation:
    """The answer to "is this exact boundary live?", and what it cost to say."""

    status: LiveStatus
    fingerprint: str
    runner: str
    requested_model: str
    detail: str
    cost: ProbeCost
    probe_id: str | None = None
    spent: bool = False
    from_evidence: bool = False

    def __post_init__(self) -> None:
        """Redact diagnostics from live, refused, and stored confirmations."""
        for name in ("detail", "requested_model"):
            object.__setattr__(self, name, redact_credentials(getattr(self, name)))

    @property
    def confirmed(self) -> bool:
        return self.status is LiveStatus.CONFIRMED


def route_live_status(
    store: SqliteRunStore, fingerprint: str, *, now: datetime | None = None
) -> tuple[LiveStatus, StoredReadinessProbe | None]:
    """Stored live validity for one exact boundary. Reads only; never spends.

    This is the whole surface a web page, a TUI pane, or any other passive
    reader gets. Absent or expired evidence is ``UNVERIFIED`` — never
    "probably fine because the static checks passed".
    """
    lookup = store.lookup_readiness_probe(fingerprint, now=now)
    if lookup.verdict is not ProbeVerdict.HIT or lookup.probe is None:
        return LiveStatus.UNVERIFIED, lookup.probe
    probe = lookup.probe
    live = (
        LiveStatus.CONFIRMED
        if probe.state is ProbeState.SUCCEEDED
        else LiveStatus.REFUTED
    )
    return live, probe


def stranded_probes(
    store: SqliteRunStore, fingerprint: str
) -> list[StoredReadinessProbe]:
    """Probes on this boundary whose cleanup nobody has acknowledged yet."""
    return [
        probe
        for probe in store.list_probes_awaiting_cleanup()
        if probe.fingerprint == fingerprint
    ]


def reclaim_stranded_probes(
    store: SqliteRunStore, *, fingerprint: str, runtime: str | None = None
) -> list[str]:
    """Release what dead probes left behind, then acknowledge each — explicitly.

    Acknowledgement is a deliberate act with cleanup behind it, never something
    elapsed time confers: a lease that merely aged out says nobody reported an
    outcome, which is precisely when a container or process group is most
    likely still alive.
    """
    reclaimed: list[str] = []
    for probe in stranded_probes(store, fingerprint):
        _release_probe_resources(probe, runtime)
        store.acknowledge_probe_cleanup(probe.probe_id)
        reclaimed.append(probe.probe_id)
    return reclaimed


def _release_probe_resources(probe: StoredReadinessProbe, runtime: str | None) -> None:
    if probe.container_name and runtime:
        # By name: the probe row records no id, and the name is this probe's
        # own (``kew-readiness-probe-<random>``, or its sidecar's owner-unique
        # worker), so it cannot be another run's container.
        subprocess_utils.run([runtime, "kill", probe.container_name], check=False)
    if probe.pgid:
        # Already reaped is the common case; the acknowledgement is still the
        # truth we are recording, so a missing process is not an error here.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(probe.pgid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# The one spending action
# ---------------------------------------------------------------------------


def _refused(route: ProbeRoute, detail: str) -> LiveConfirmation:
    return LiveConfirmation(
        status=LiveStatus.REFUSED,
        fingerprint=route.fingerprint,
        runner=route.runner_name,
        requested_model=route.model,
        detail=detail,
        cost=ProbeCost.unknown(),
    )


def _route_uses_sidecar(route: ProbeRoute) -> bool:
    transport = route.profile.route.transport.value
    return isinstance(transport, Mapping) and transport.get("sidecar") is True


def _sidecar_packet_refusal(
    store: SqliteRunStore, config: DispatchConfig, route: ProbeRoute
) -> str | None:
    """Return why no matching terminal packet proof can authorize this probe."""
    if not _route_uses_sidecar(route):
        return None
    from kew.audit import (
        SidecarPacketEvidenceUnavailable,
        matching_sidecar_packet_evidence,
    )
    from kew.sandbox import image_id_runtime, require_image_id

    expected_worker = route.profile.image_id.value
    settings = config.inference_transport.switchyard
    sidecar = settings.sidecar
    if not isinstance(expected_worker, str) or sidecar is None or not sidecar.enabled:
        return "sidecar packet evidence cannot match an unresolved image"
    try:
        proxy_image_id = require_image_id(
            image_id_runtime(config.sandbox, route.sandbox.runtime),
            sidecar.image,
            runner="switchyard-sidecar",
            refusal="the readiness probe is refused before any charge",
        )
        # No lookup bound passed from here: the evidence window is read in
        # pages by the matcher itself, so proof piling up in it is proof, not
        # an overflow (#2389 review).
        matching_sidecar_packet_evidence(
            store,
            worker_image_id=expected_worker,
            proxy_image_id=proxy_image_id,
        )
    except (OSError, ValueError, RuntimeError, SidecarPacketEvidenceUnavailable) as exc:
        return exception_text(exc)
    return None


def _from_evidence(route: ProbeRoute, probe: StoredReadinessProbe) -> LiveConfirmation:
    status = (
        LiveStatus.CONFIRMED
        if probe.state is ProbeState.SUCCEEDED
        else LiveStatus.REFUTED
    )
    return LiveConfirmation(
        status=status,
        fingerprint=route.fingerprint,
        runner=route.runner_name,
        requested_model=route.model,
        detail=probe.detail or f"evidence from probe {probe.probe_id}",
        cost=ProbeCost.restore(
            probe.recorded_cost_usd,
            probe.recorded_cost_source,
            probe.recorded_cost_certainty,
        ),
        probe_id=probe.probe_id,
        from_evidence=True,
    )


def _pre_spend_refusal(
    store: SqliteRunStore,
    config: DispatchConfig,
    route: ProbeRoute,
    *,
    reclaim: bool,
    now: datetime | None,
) -> LiveConfirmation | None:
    """Everything that must hold before a single cent may be spent."""
    # Turn any lease that outlived its holder into an abandoned, UNacknowledged
    # probe first, so the stranded check below sees the crash instead of a
    # lease that merely looks free.
    store.reconcile_readiness_probes(now=now)
    if reclaim:
        reclaim_stranded_probes(
            store, fingerprint=route.fingerprint, runtime=route.sandbox.runtime
        )
    stranded = stranded_probes(store, route.fingerprint)
    if stranded:
        names = ", ".join(probe.probe_id for probe in stranded)
        return _refused(
            route,
            f"{len(stranded)} earlier probe(s) on this boundary never had their "
            f"cleanup acknowledged ({names}); an expired lease is not permission "
            "to spend again. Re-run with --reclaim to release them first.",
        )
    access = check_supporting_access(config)
    if not access.ok:
        return _refused(route, "; ".join(access.failures))
    # A probe is a paid call like any other run: the daily and weekly caps
    # stop it too, counting it at its ceiling (#3205).
    try:
        check_window_budget(
            store,
            config.budget,
            reservation_usd=PROBE_CEILING_USD,
            now=now,
            count_admitting_run=True,
        )
    except BudgetExceeded as exc:
        return _refused(route, window_budget_hold_reason(exc))
    return None


def confirm_route_live(
    store: SqliteRunStore,
    config: DispatchConfig,
    *,
    complexity: str | None = None,
    reclaim: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> LiveConfirmation:
    """Confirm ONE exact boundary with at most one bounded provider call.

    Fresh evidence answers without spending. Otherwise: recheck the supporting
    legs, take I05A's single-spend lease, make exactly one call, settle its
    real cost, and acknowledge cleanup. There is no retry — a failed probe is
    evidence, and evidence is the thing being collected.
    """
    route = resolve_probe_route(
        config, complexity=complexity, store=store, create_key=True
    )
    packet_refusal = _sidecar_packet_refusal(store, config, route)
    if packet_refusal is not None:
        return _refused(route, packet_refusal)
    if not force:
        status, probe = route_live_status(store, route.fingerprint, now=now)
        if status is not LiveStatus.UNVERIFIED and probe is not None:
            return _from_evidence(route, probe)
    refusal = _pre_spend_refusal(store, config, route, reclaim=reclaim, now=now)
    if refusal is not None:
        return refusal
    try:
        # A runner kew cannot bound is refused BEFORE the lease, so no row and
        # no charge exist for a call it was never going to make.
        probe_bound(route.runner_name)
    except LiveProbeUnsupported as exc:
        return _refused(route, exception_text(exc))
    return _spend_one_call(store, config, route, now=now)


def _spend_one_call(
    store: SqliteRunStore,
    config: DispatchConfig,
    route: ProbeRoute,
    *,
    now: datetime | None,
) -> LiveConfirmation:
    policy = "inherit" if route.image is None else route.proxy_environment
    with proxy_redaction_scope(policy, environment=probe_env()):
        probe_id = new_probe_id()
        try:
            store.lease_readiness_probe(
                probe_id=probe_id,
                fingerprint=route.fingerprint,
                planned_cost=ProbeCost.planned(PROBE_CEILING_USD, PROBE_CEILING_SOURCE),
                model=route.model,
                runner=route.runner_name,
                now=now,
            )
        except ProbeLeaseHeldError as exc:
            return _refused(route, exception_text(exc))
        try:
            if _route_uses_sidecar(route):
                execution = _run_leased_sidecar_probe(
                    store, config, route, probe_id=probe_id
                )
            else:
                launch = build_probe_launch(route, probe_id=probe_id)
        except (OSError, ValueError, RuntimeError) as exc:
            # Nothing was launched, so nothing was charged. Settling at a MEASURED
            # zero is the honest number here, and it also stops the reconciler from
            # later billing this dangling lease at the declared ceiling for a call
            # that never happened.
            return _settle(
                store,
                route,
                ProbeExecution(
                    exit_code=-1,
                    detail=f"the probe could not start: {exception_text(exc)}",
                ),
                probe_id=probe_id,
                now=now,
                cost=ProbeCost.observed(0.0, "no provider call was made"),
            )
        if _route_uses_sidecar(route):
            # Settlement errors are not startup errors and cannot imply a free call.
            return _settle(store, route, execution, probe_id=probe_id, now=now)
        with proxy_redaction_scope(launch.proxy_environment, environment=launch.env):
            try:
                execution = _run_leased_probe(store, route, launch, probe_id=probe_id)
            finally:
                shutil.rmtree(launch.workspace, ignore_errors=True)
            return _settle(store, route, execution, probe_id=probe_id, now=now)


def _sidecar_probe_launch(
    store: SqliteRunStore,
    config: DispatchConfig,
    route: ProbeRoute,
    transport: ResolvedInferenceTransport,
    supervisor: SidecarAttemptSupervisor,
    *,
    worker_image_id: str,
    workspace: Path,
    probe_id: str,
) -> ProbeLaunch:
    from kew.dispatch_sandbox import prepare_sidecar_worker
    from kew.sandbox import write_prompt_handoff

    write_prompt_handoff(workspace, PROBE_PROMPT)
    bound_transport = replace(
        transport,
        endpoint=supervisor.endpoint,
        config_sha256=supervisor.config_sha256,
    )
    contained = prepare_sidecar_worker(
        config,
        supervisor=supervisor,
        image_id=worker_image_id,
        runner=route.runner_name,
        checkout_dir=workspace,
        slug=probe_id,
        model=transport.model,
        inference_transport=bound_transport,
        store=store,
        agent_command=probe_bound(route.runner_name).args(
            model=transport.model,
            ceiling=PROBE_CEILING_USD,
            prompt=None,
        ),
    )
    env = launch_environment(probe_env(), None, contained=True)
    for name in transport.credential_env_names:
        env.pop(name, None)
    return ProbeLaunch(
        argv=contained.argv,
        env=env,
        cwd=workspace,
        workspace=workspace,
        container_name=contained.name,
        runtime=contained.runtime,
        proxy_environment=route.proxy_environment,
        container_id=contained.container_id,
    )


def _run_leased_sidecar_probe(
    store: SqliteRunStore,
    config: DispatchConfig,
    route: ProbeRoute,
    *,
    probe_id: str,
) -> ProbeExecution:
    """Run one bounded call inside the credential-isolated sidecar topology."""
    from kew.orchestrator import sidecar_switchyard_context

    transport = route.transport
    worker_image_id = route.profile.image_id.value
    if (
        transport is None
        or not transport.sidecar
        or not isinstance(worker_image_id, str)
    ):
        raise LiveProbeUnsupported("sidecar probe identity is incomplete")
    workspace = create_probe_workspace(probe_id)
    execution = ProbeExecution(
        exit_code=-1, call_attempted=False, cleanup_acknowledged=False
    )
    try:
        with sidecar_switchyard_context(
            config,
            transport,
            worker_image_id=worker_image_id,
        ) as supervisor:
            try:
                launch = _sidecar_probe_launch(
                    store,
                    config,
                    route,
                    transport,
                    supervisor,
                    worker_image_id=worker_image_id,
                    workspace=workspace,
                    probe_id=probe_id,
                )
            except Exception as exc:
                # Let the context prove cleanup even when preparation fails.
                execution = replace(
                    execution,
                    detail=f"the isolated sidecar probe could not start: "
                    f"{exception_text(exc)}",
                )
            else:
                execution = _run_leased_probe(store, route, launch, probe_id=probe_id)
    except Exception:
        return replace(
            execution,
            exit_code=-1,
            cleanup_acknowledged=False,
            detail="the isolated sidecar probe cleanup was not verified",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return replace(execution, cleanup_acknowledged=True)


def _run_leased_probe(
    store: SqliteRunStore,
    route: ProbeRoute,
    launch: ProbeLaunch,
    *,
    probe_id: str,
) -> ProbeExecution:
    def _attach(proc: subprocess.Popen[str]) -> None:
        store.attach_probe_execution(
            probe_id,
            container_name=launch.container_name,
            pgid=os.getpgid(proc.pid),
        )

    with proxy_redaction_scope(launch.proxy_environment, environment=launch.env):
        try:
            return execute_probe(launch, route.runner, on_started=_attach)
        except Exception as exc:
            # The call may or may not have reached the provider, so the cost is
            # unknown — which I05A settles at the declared ceiling rather than $0.
            # Catch broadly (not just OSError/ValueError/SubprocessError): an
            # unexpected failure must terminalize the lease in-process via _settle
            # rather than leak it to the reconciler. BaseException (SIGINT) still
            # propagates — and execute_probe's own finally has already killed the
            # call, so no provider process is orphaned on that path either.
            return ProbeExecution(
                exit_code=-1, detail=f"the probe could not run: {exception_text(exc)}"
            )


def _settle(
    store: SqliteRunStore,
    route: ProbeRoute,
    execution: ProbeExecution,
    *,
    probe_id: str,
    now: datetime | None,
    cost: ProbeCost | None = None,
) -> LiveConfirmation:
    succeeded, detail = judge_probe(execution, requested_model=route.model)
    if not execution.cleanup_acknowledged:
        succeeded, detail = False, execution.detail
    try:
        cost = cost or (
            interpret_probe_cost(route.runner_name, execution.reported_cost_usd)
            if execution.call_attempted
            else ProbeCost.observed(0.0, "no provider call was made")
        )
    except ValueError:
        if not _route_uses_sidecar(route):
            raise
        cost = ProbeCost.unknown()
        detail += "; reported cost was invalid, so spend is unavailable"
    detail = redact_credentials(detail)
    store.settle_readiness_probe(
        probe_id,
        succeeded=succeeded,
        recorded_cost=cost,
        detail=detail,
        now=now or datetime.now(UTC),
    )
    # Settled AND released: the resources this probe owned are gone, so it is
    # not left on the stranded list blocking the next honest confirm.
    if execution.cleanup_acknowledged:
        store.acknowledge_probe_cleanup(probe_id)
    return LiveConfirmation(
        status=LiveStatus.CONFIRMED if succeeded else LiveStatus.REFUTED,
        fingerprint=route.fingerprint,
        runner=route.runner_name,
        requested_model=route.model,
        detail=detail,
        cost=cost,
        probe_id=probe_id,
        spent=True,
    )


__all__: Sequence[str] = (
    "LiveConfirmation",
    "LiveProbeUnsupported",
    "LiveStatus",
    "PROBE_BOUNDS",
    "PROBE_CEILING_USD",
    "PROBE_EXPECTED_REPLY",
    "PROBE_PROMPT",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeBound",
    "ProbeExecution",
    "ProbeLaunch",
    "ProbeRoute",
    "SupportingAccess",
    "build_probe_launch",
    "check_supporting_access",
    "confirm_route_live",
    "create_probe_workspace",
    "execute_probe",
    "interpret_probe_cost",
    "judge_probe",
    "models_agree",
    "probe_bound",
    "probe_env",
    "reclaim_stranded_probes",
    "resolve_probe_route",
    "route_live_status",
    "stranded_probes",
)
