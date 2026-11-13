"""The OpenShell launch lifecycle: create, upload, exec, delete.

``[sandbox] runtime = "openshell"`` replaces the container boundary and nothing
else (epic ``openshell-runtime``, node N6). Host-owned publication, the
publication filter, the audit chain and launching by immutable image ID are all
unchanged; what changes is that the agent runs inside a gateway-governed
sandbox instead of under ``docker run``.

Five decisions shape this module, and each one is why a piece of it looks the
way it does:

**The handle is the docker handle.** :class:`OpenShellSandboxHandle` subclasses
:class:`kew.sandbox.ContainerHandle` over the ``openshell sandbox exec`` client
process, which is an ordinary ``Popen`` with a merged stdout. The startup
acknowledgement, the wall clock, the transcript-byte cap, the agent-event
observation and the admission handshake are therefore the SAME code on both
runtimes rather than a second implementation free to drift. Only the calls that
name a container are overridden, because those are what the gateway spells
differently.

**The checkout lands at /work.** The in-sandbox launcher
(``docker/kew-openshell-launch``, node N3) reads the prompt from
``/work/.kew-prompt.txt`` by default, derives the published checkout from that
file's directory, and bootstraps git with ``safe.directory /work``. Uploading
anywhere else would break the prompt handoff and leave the agent's own commits
dying on "dubious ownership" — the #742 incident, reintroduced on one runtime.

**Deletion is not left to a cleanup pass.** A sandbox outlives the exec that
ran in it, so every exit path deletes: the base class's ``wait`` runs the
identity cleanup, which here IS the delete; a cap breach and a launch unwind go
through ``kill``; an interruption goes through ``cancel_launch``; and
:func:`launch_openshell_container` deletes itself when create / upload / exec
fails part-way. The delete is idempotent and never raises — an already-absent
sandbox is the outcome being asked for.

**The work comes back before the delete.** The agent edits an UPLOADED copy,
so the host checkout holds none of its work and the sandbox holding it is
deleted as soon as the run ends. ``cleanup_identity`` therefore realizes the
checkout first (:mod:`kew.openshell_checkout`, node N7) and deletes second; the
realized tree, not the uploaded one, is what publication is given. A delete
that happens WITHOUT a realization — a cap breach, a launch unwind, an
interruption, all of which end the workload by deleting the sandbox — still
reads the denial log on its way out, because that log exists nowhere else
afterwards. And because the retry ladder re-enters one host checkout,
:func:`launch_openshell_container` retires the previous attempt's realized tree
before it creates a sandbox: without that, every attempt after the first lost
its work to a destination-occupied refusal.

**An uncertain delete is settled by reading.**
:class:`kew.openshell.OpenShellOutcomeUnknown` documents the one thing a caller
must not conclude from it: that nothing happened. A delete whose reply was cut
off has usually already committed, so the handle re-reads the sandbox
(``sandbox get``) rather than guessing — the same tri-state the docker handle
gets from ``docker events`` / ``docker inspect``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from kew import work_dirs
from kew.openshell import (
    TERMINAL_STATUSES,
    OpenShellClient,
    OpenShellError,
    OpenShellExec,
    OpenShellNotFound,
    OpenShellOutcomeUnknown,
)
from kew.openshell_egress import (
    DISPATCH_FLOW,
    LAUNCHER_PATH,
    REVIEW_FLOW,
    PinnedPolicy,
    materialized_policy,
)
from kew.runtime_names import OPENSHELL_RUNTIME
from kew.sandbox import (
    CHECKOUT_QUOTA_BYTES,
    CHECKOUT_QUOTA_ENTRIES,
    HOST_FREE_SPACE_FLOOR_BYTES,
    TRANSCRIPT_BYTE_CAP,
    WALL_CLOCK_LIMIT_SECONDS,
    WATCHDOG_INTERVAL_SECONDS,
    ContainerHandle,
    KillStatus,
    checkout_usage,
)

if TYPE_CHECKING:
    from kew.config import DispatchConfig, SandboxConfig
    from kew.credential_issuer import BoundedRunKey
    from kew.dispatch_admission import PendingAdmission
    from kew.openshell_checkout import CheckoutRealization
    from kew.run_store import RunStore

logger = logging.getLogger(__name__)

#: Where the checkout is uploaded, and the exec's working directory. Fixed by
#: the in-sandbox launcher's contract, not by preference — see the module
#: docstring.
CHECKOUT_PATH = "/work"

#: What a recovery download did to the HOST checkout — the three states an
#: operator has to be able to tell apart, because each leaves them a different
#: tree to come back to:
#:
#: ``"merged"``
#:     The sandbox's checkout is on the host. The only verdict that deletes the
#:     sandbox and attests ``checkout_recovered``.
#: ``"unmerged"``
#:     Nothing arrived; the host checkout is exactly as it was. The sandbox is
#:     kept so the work can be fetched by hand.
#: ``"part_merged"``
#:     The merge failed part-way: the host checkout holds some of the sandbox's
#:     files and not the rest, which is a tree neither side produced. The
#:     sandbox is kept — it still has the whole copy — and the driver attests
#:     this rather than the ``"unmerged"`` an operator reads as "untouched".
CheckoutMerge = Literal["merged", "unmerged", "part_merged"]

#: Sandbox names are ``kew-<run slug>-<run token>`` — one namespace with the
#: container names the docker path uses, so an operator reading the gateway sees
#: the same identity the run row carries.
SANDBOX_NAME_PREFIX = "kew-"

#: Hex digits of :func:`sandbox_run_token` a sandbox name carries. A gateway is
#: shared by every project an operator runs, and a slug alone (``kew-42``) is
#: the same name in every repository with an item 42: one project's recovery
#: then read, downloaded and DELETED another's live sandbox (#2625).
SANDBOX_RUN_TOKEN_LENGTH = 12

#: The attribute a launch unwind sets on its in-flight error when the sandbox
#: it created could not be confirmed deleted. Its value is the sandbox name.
#: An attribute rather than a wrapper exception, so the operator still sees the
#: launch failure itself; ``kew.dispatch_sandbox`` turns it into the cleanup
#: evidence that keeps the run pending (#2625).
UNRELEASED_SANDBOX_ATTR = "openshell_unreleased_sandbox"

#: Labels carried on every sandbox kew creates, so an orphan on the gateway can
#: be traced back to the run that made it without reading kew's ledger.
RUN_LABEL = "kew.run"
RUNNER_LABEL = "kew.runner"
#: Which boundary this sandbox is — ``dispatch`` or ``review`` (epic node N12).
#: On the label as well as in the policy, so an operator reading the gateway can
#: tell a reviewer's sandbox from a dispatch's without reading kew's ledger.
FLOW_LABEL = "kew.flow"

#: The program the launcher execs, per runner. An explicit table rather than a
#: guess from the runner name: ``claude-code`` runs ``claude``, and a runner
#: with no entry is refused rather than launched under a program nobody
#: installed. ``tests/test_openshell_launch.py`` pins it against the shim each
#: runner's OpenShell policy governs — a program outside that set would run
#: ungoverned.
RUNNER_PROGRAMS: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}

#: Where every governed runner shim lives in the agent images. The launcher is
#: handed ``<dir>/<program>``, never a bare name: a name resolved through PATH
#: inside the sandbox is chosen by whatever PATH holds, and a checkout on it
#: let a committed ``bin/claude`` stand in for the governed runner (#2637).
RUNNER_PROGRAM_DIR = "/usr/bin"

#: Every spelling of "which model" the runners' ``container_args`` produce. One
#: of them MUST be in the exec argv: the routed model reaches the container
#: through this flag alone, and a launch without it silently runs the runner
#: CLI's own default — billing and attesting a model the run never used.
MODEL_FLAGS = ("--model", "-m")


class OpenShellLaunchRefused(RuntimeError):
    """kew refuses to launch this dispatch into an OpenShell sandbox.

    Raised before any sandbox exists, for the facts the host can settle on
    its own: a runner with no program, a runner with no policy, an argv that
    would start the agent without naming a model, or a topology this runtime
    has no launch path for — and for a certification that does not match the
    policy bytes and gateway this launch would use, which reads the gateway's
    version and self-report but creates nothing (#2624).
    """


#: Why a sidecar Switchyard topology has no launch path on this runtime. The
#: sidecar path builds its worker with :func:`kew.sandbox.build_run_argv` — a
#: docker ``create --init --pull=never`` — and installs a netns filter through
#: the daemon; neither exists on the gateway, and the ``openshell`` CLI has no
#: ``create`` subcommand to absorb the mistake.
SIDECAR_LAUNCH_REFUSAL = (
    "a sidecar Switchyard worker is a docker topology; the OpenShell runtime "
    "has no launch path for it. Route this dispatch through "
    '[inference_transport] mode = "direct", or set [sandbox] runtime = '
    '"docker" to keep the sidecar.'
)

#: Why a caller that launches its agent as a container argv has no launch path
#: on this runtime. The tracked remediation flows (``kew review``, ``kew
#: resolve``, ``kew ci-fix``, graph ``plan_run``) swap one EXECUTION STEP for a
#: container argv inside :func:`kew.orchestrator.run_agent`, rather than
#: handing the whole launch to this module; there is no gateway sandbox for
#: that argv to be an argv of.
#:
#: The remedy names the runtime switch FIRST and bounds where the flow
#: exception applies, because ``allow_uncontained_flows`` does not clear this
#: refusal for every flow that can see it. ``kew review`` and ``kew resolve``
#: resolve through :mod:`kew.remediation_containment`, which takes the host
#: fallback when the flag is set and never reaches here. ``kew ci-fix`` and
#: graph ``plan_run`` resolve through
#: :func:`kew.sandbox.resolve_flow_containment`, which has no host fallback
#: arm at all: the flag is read nowhere on their path, so an operator who
#: followed the old sentence set it and got this same refusal back (PR #2401
#: review). That is the documented behaviour of the flag — ``[sandbox]
#: allow_uncontained_flows`` is the remediation flows' exception
#: (``docs/configuration.md``), not a general one — so the message is what was
#: wrong here, not the gate.
ARGV_LAUNCH_REFUSAL = (
    "this flow runs its agent as a container command line, and an OpenShell "
    "sandbox is reached through the gateway rather than a command line. Set "
    '[sandbox] runtime = "docker" (or "podman") to contain this flow — the '
    "one remedy that clears this refusal for every flow. [sandbox] "
    "allow_uncontained_flows = true admits a host run for kew review and kew "
    "resolve only; kew ci-fix and an epic's plan run resolve through the "
    "dispatch admission, which has no host fallback, and refuse whatever it "
    "says."
)


#: Why a Switchyard transport WITHOUT a sidecar has no launch path here
#: either. On docker the endpoint reaches the agent as part of the launch —
#: ``kew.sandbox._container_environment_args`` assigns the runner's proven
#: endpoint override (``ANTHROPIC_BASE_URL`` for claude-code) in the container
#: argv — and there is no equivalent on this runtime: ``openshell sandbox
#: exec`` carries only the names :data:`kew.openshell.SANDBOX_EXEC_ENV_NAMES`
#: admits, and the packaged egress policies pin ONE upstream host apiece
#: (``docker/openshell/policy-*.yaml``), so a switchyard endpoint is not a
#: destination any of them permit.
#:
#: Both halves of that are why this is a refusal rather than a missing
#: injection. Admitted as ``sandboxed``, the launch put a switchyard transport
#: on the audit chain while the agent talked straight to the upstream its
#: policy DOES permit (PR #2401 review); injecting the endpoint instead would
#: trade that for a run that fails closed at the first model call on an egress
#: rule no operator can edit from kew's config.
SWITCHYARD_LAUNCH_REFUSAL = (
    "a Switchyard transport routes the agent through an endpoint this runtime "
    "cannot deliver: the OpenShell exec carries no endpoint override, and the "
    "packaged egress policy permits the runner's upstream host only. Route "
    'this dispatch through [inference_transport] mode = "direct", or set '
    '[sandbox] runtime = "docker" (or "podman") to keep Switchyard.'
)


def is_openshell(config: SandboxConfig) -> bool:
    """Whether this sandbox configuration launches through the gateway."""
    return config.runtime == OPENSHELL_RUNTIME


def gateway_launch_refusal(
    *, sidecar: bool, gateway_launch: bool, direct: bool
) -> str | None:
    """Why the gateway cannot launch THIS dispatch shape, or ``None`` when it can.

    One launch path exists on this runtime — create / upload / exec / delete
    through :func:`launch_openshell_container` — and exactly one dispatch shape
    reaches it: a DIRECT, non-sidecar streaming dispatch, whose whole launch
    ``kew.dispatch_sandbox`` hands over. The other three shapes resolve the
    same containment settings and then have nowhere to go, so they are refused.

    Asked by admission, not only at the launch seam, because a refusal that
    arrives after :func:`kew.sandbox.resolve_effective_sandbox` has already
    answered ``sandboxed`` has put that posture on the audit chain for a
    boundary this build was never going to hold (PR #2401 review). The
    sentences are constants for the same reason: the operator who hits the
    early refusal and the one who hits the late one need the same remedy.

    ``direct`` is :func:`kew.inference_transport.direct_route` — false for
    every Switchyard transport, sidecar or not. It is asked LAST of the three
    so the shape that cannot launch at all is named before the transport that
    cannot be delivered: an operator whose flow has no gateway launch path
    gains nothing from hearing about an endpoint.
    """
    if sidecar:
        return SIDECAR_LAUNCH_REFUSAL
    if not gateway_launch:
        return ARGV_LAUNCH_REFUSAL
    if not direct:
        return SWITCHYARD_LAUNCH_REFUSAL
    return None


#: Where the in-sandbox launcher records the agent's pid and kernel start time
#: before it execs the agent (``kew-openshell-launch``).
AGENT_EXEC_MARKER = "/tmp/kew-agent.exec"  # nosec B108 - a path inside the sandbox

#: The line :data:`AGENT_EXEC_PROBE` answers with: ``running``, ``ended`` or
#: ``absent`` (no marker was ever written).
AGENT_EXEC_STATE_PREFIX = "KEW_AGENT_EXEC"

#: Asks the sandbox whether the agent the marker names is still that process.
#: POSIX ``sh`` only, because it runs in every agent image. Field 22 of
#: ``/proc/<pid>/stat`` is the start time; the fields are counted after the
#: ``comm`` field's closing parenthesis, which may itself contain spaces. A
#: zombie has exited, so it reads ``ended``.
AGENT_EXEC_PROBE = (
    f"m={AGENT_EXEC_MARKER}; p={AGENT_EXEC_STATE_PREFIX}; "
    'if ! read -r pid start < "$m" 2>/dev/null; then echo "$p absent"; exit 0; fi; '
    'if ! read -r stat < "/proc/$pid/stat" 2>/dev/null; then '
    'echo "$p ended"; exit 0; fi; '
    "rest=${stat##*) }; set -- $rest; state=$1; shift 19; "
    'if [ "$state" != Z ] && [ -n "$start" ] && [ "$1" = "$start" ]; '
    'then echo "$p running"; else echo "$p ended"; fi'
)

AgentExecState = Literal["running", "ended", "unknown"]


def agent_exec_state(client: OpenShellClient, name: str) -> AgentExecState:
    """Is the agent this sandbox's launcher exec'd still running in it?

    Asked when kew's LOCAL ``sandbox exec`` client is gone: that client dying
    — killed with its host, OOM-killed, its session torn down — does not stop
    the remote agent, and reading the one as the other let recovery download a
    live checkout and finalize a running run (#2625). Only ``ended`` licenses
    a recovery; anything the sandbox could not state is ``unknown``.

    A sandbox with no marker at all reads ``ended``: the launcher writes it
    before anything else, so an exec without one never reached the agent — or
    ran in an image from before the marker, where this is the old reading.
    """
    try:
        output = client.sandbox_run(
            name, workdir="/", argv=("/bin/sh", "-c", AGENT_EXEC_PROBE)
        )
    except (OpenShellError, ValueError) as exc:
        logger.warning(
            "openshell sandbox %s could not say whether its agent is running (%s)",
            name,
            exc,
        )
        return "unknown"
    for line in output.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == AGENT_EXEC_STATE_PREFIX:
            if words[1] == "running":
                return "running"
            if words[1] in {"ended", "absent"}:
                return "ended"
    logger.warning("openshell sandbox %s answered its exec probe with nothing", name)
    return "unknown"


def sandbox_run_token(project: str, run_id: str) -> str:
    """A short digest binding a sandbox name to one project's one run.

    Hashed rather than spelled out: a repository slug and a run id are both
    longer than a gateway name wants to be, and neither is needed in clear —
    the ``kew.run`` label already carries the run id for an operator.
    """
    digest = hashlib.sha256(f"{project}\0{run_id}".encode()).hexdigest()
    return digest[:SANDBOX_RUN_TOKEN_LENGTH]


def sandbox_name(slug: str, *, project: str, run_id: str) -> str:
    """The gateway name for one run's sandbox.

    The slug keeps it readable; the token keeps it unique across every project
    and run sharing the gateway, so no two runs ever answer to the same name
    (#2625). Retry attempts of ONE run share it, which is safe: each attempt
    deletes its sandbox before the next creates one, and the run row records
    the gateway id of whichever sandbox is current.
    """
    if not run_id.strip():
        raise ValueError("an openshell sandbox name needs the run id it belongs to")
    token = sandbox_run_token(project, run_id)
    return f"{SANDBOX_NAME_PREFIX}{slug}-{token}"


def runner_program(runner: str) -> str:
    """The absolute governed path the in-sandbox launcher execs for ``runner``."""
    try:
        return f"{RUNNER_PROGRAM_DIR}/{RUNNER_PROGRAMS[runner]}"
    except KeyError:
        raise OpenShellLaunchRefused(
            f"runner {runner!r} has no program on the OpenShell runtime; "
            f"this build launches {', '.join(sorted(RUNNER_PROGRAMS))}"
        ) from None


def require_launchable_runner(runner: str, *, flow: str = DISPATCH_FLOW) -> str:
    """Return ``runner`` once this build can launch it through the gateway.

    The two facts a launch needs from the runner, asked in one call so
    admission can ask them too: a program the in-sandbox launcher can exec, and
    a packaged policy to create the sandbox under. Admission used to gate the
    runtime NAME alone, so a runner with neither resolved ``sandboxed`` and was
    then refused at launch — a sandboxed posture recorded on the chain for a
    boundary this build was never going to hold.

    ``flow`` selects WHICH policy must be packaged. The reviewer asks for the
    review one, because ``claude-code`` is the only runner that ships it: a
    reviewer pinned elsewhere has to be refused for that reason rather than
    admitted on the strength of a dispatch policy it will not run under.
    """
    runner_program(runner)
    runner_policy_path(runner, flow=flow)
    return runner


def launchable_runner_refusal(runner: str) -> str | None:
    """Why this build cannot launch ``runner`` here, or ``None`` when it can.

    The ASKING spelling of :func:`require_launchable_runner`, for the one
    caller that needs the answer rather than the refusal:
    :func:`kew.sandbox._offered_runtime`, which decides whether kew has a
    boundary to offer this dispatch at all before the runner is asked for its
    own posture. It cannot raise — a runner with no launch path here is a
    reason to ask the runner the no-runtime question, not a reason to refuse a
    run its own sandbox contains.

    Built on the raising form rather than beside it so there is one definition
    of launchable and one set of sentences.
    """
    try:
        require_launchable_runner(runner)
    except OpenShellLaunchRefused as exc:
        return str(exc)
    return None


def policy_flow_for_run_kind(run_kind: str) -> str:
    """Which OpenShell boundary one run KIND launches into (epic node N12).

    The adversarial reviewer is the one launch whose egress differs from a
    dispatch's on the same image, and the run kind is the fact already threaded
    to the launch seam that says so (:func:`_separates_container_stderr` reads
    it one line away). Deriving the flow here rather than passing a policy down
    from the reviewer keeps ONE answer: the certification gate the reviewer
    calls resolves the same flow from the same constant, so the file a sandbox
    is created under cannot differ from the file its proof was bound to.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

    return REVIEW_FLOW if run_kind == ADVERSARIAL_REVIEW_RUN_KIND else DISPATCH_FLOW


def runner_policy_path(runner: str, *, flow: str = DISPATCH_FLOW) -> Path:
    """The packaged OpenShell policy governing ``runner``'s egress on ``flow``.

    Refused rather than defaulted when absent: a sandbox created with no policy
    is a sandbox with no egress enforcement, which reads like containment and
    governs nothing.

    ``flow`` is the launch this sandbox is for, not a property of the runner
    (epic node N12). One image serves a dispatch and an adversarial review, and
    those are two boundaries: the reviewer executes the reviewed PR's tests, so
    it runs under its own policy and is certified against its own digest. The
    resolution is :func:`kew.openshell_egress.openshell_policy_path`'s, so the
    file a launch is governed by and the file a certification was bound to can
    never be two different answers to the same question.
    """
    from kew.openshell_egress import EgressProbeError, openshell_policy_path

    try:
        return openshell_policy_path(runner, flow=flow)
    except EgressProbeError as exc:
        raise OpenShellLaunchRefused(
            f"no OpenShell {flow} policy for runner {runner!r} ({exc}); a "
            "sandbox created without one would enforce no egress rule at all"
        ) from exc


def pin_runner_policy(runner: str, *, flow: str = DISPATCH_FLOW) -> PinnedPolicy:
    """Read the policy a launch is created under, once, refusing an absent one.

    The launch hashes and creates from THIS read (:func:`_create_sandbox`), so
    the bytes the certification is matched on and the bytes the gateway is
    handed cannot be two reads of a file that changed between them (#2624).
    """
    path = runner_policy_path(runner, flow=flow)
    try:
        return PinnedPolicy(path=path, data=path.read_bytes())
    except OSError as exc:
        raise OpenShellLaunchRefused(
            f"the OpenShell {flow} policy at {path} could not be read ({exc})"
        ) from exc


def require_launch_certification(
    store: RunStore | None,
    *,
    gateway: OpenShellClient,
    image: str,
    runner: str,
    policy: PinnedPolicy,
    flow: str,
) -> None:
    """Refuse a create unless a certification matches what it will launch.

    Admission matched a proof earlier, but against a policy read and a gateway
    client of its own; between that and this create the file can be swapped
    and the gateway can be another one (#2624). So the launch matches again, on
    the digest of the exact bytes it is about to hand over and on the gateway
    client it is about to hand them to. A proof that does not match is a
    refusal, never a launch on the strength of admission's answer.
    """
    from kew.credential_surface import exception_text
    from kew.sandbox import require_openshell_boundary_match

    remedy = (
        f"Run `kew sandbox certify --runtime {OPENSHELL_RUNTIME} --runner "
        f"{runner} --flow {flow}` against this gateway, then dispatch again."
    )
    if store is None:
        raise OpenShellLaunchRefused(
            f"no ledger is available to match the {OPENSHELL_RUNTIME} {flow} "
            f"egress certification against the policy this launch would run. "
            f"{remedy}"
        )
    try:
        require_openshell_boundary_match(
            store, image_id=image, policy_sha256=policy.sha256, client=gateway
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise OpenShellLaunchRefused(
            f"{exception_text(exc)} — the {flow} policy this launch would run "
            f"(sha256:{policy.sha256}) on this gateway is not a boundary a "
            f"certification proved. {remedy}"
        ) from exc


def require_model_flag(argv: Sequence[str], runner: str) -> tuple[str, ...]:
    """Return ``argv`` once it is proven to name a model, else refuse.

    The runners add the model flag only when the caller resolved a model, so a
    blank effective model produces an argv that runs the CLI's own default.
    This is the pilot runtime, and a dispatch that quietly ran a different
    model than the one attested is not evidence of anything — so here the
    omission is a refusal rather than a default.
    """
    named = {part.partition("=")[0] for part in argv}
    if named.isdisjoint(MODEL_FLAGS):
        raise OpenShellLaunchRefused(
            f"the OpenShell exec for runner {runner!r} names no model "
            f"({' / '.join(MODEL_FLAGS)}); the routed model would be replaced "
            "by the runner CLI's own default"
        )
    return tuple(argv)


def exec_argv(runner: str, nonce: str, agent_args: Sequence[str]) -> tuple[str, ...]:
    """The full ``sandbox exec`` command: launcher, nonce, program, agent args.

    The nonce is an ARGUMENT, not an environment variable: the gateway owns the
    child's environment, so an exported one is not guaranteed to survive, and
    the acknowledgement the host waits for would then never be printed.
    """
    return (
        LAUNCHER_PATH,
        nonce,
        runner_program(runner),
        *require_model_flag(agent_args, runner),
    )


class OpenShellSandboxHandle(ContainerHandle):
    """A :class:`~kew.sandbox.ContainerHandle` whose container is a sandbox.

    Everything the base class does to the stream is kept. The calls that name a
    container are replaced by their gateway spelling, and they all funnel into
    ONE idempotent delete, because on this runtime ending the workload and
    removing the container are the same call.
    """

    def __init__(
        self,
        exec_handle: OpenShellExec,
        *,
        client: OpenShellClient,
        name: str,
        identity: str,
        startup_nonce: str,
        host_checkout_dir: Path | None = None,
        wall_clock_seconds: float = WALL_CLOCK_LIMIT_SECONDS,
        transcript_byte_cap: int = TRANSCRIPT_BYTE_CAP,
        now: Callable[[], float] = time.monotonic,
        watchdog_interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(
            exec_handle.process,
            name,
            OPENSHELL_RUNTIME,
            # None, deliberately: the checkout quota walks a HOST directory,
            # and on this runtime the agent writes to an uploaded copy inside
            # the sandbox, so that walk would report a figure which cannot
            # move. The wall clock and the transcript cap, which do not depend
            # on the checkout, are the base class's unchanged — and are
            # forwarded here rather than fixed, so the two caps that CAN fire
            # on this runtime are exercisable without reaching past the class.
            checkout_dir=None,
            wall_clock_seconds=wall_clock_seconds,
            transcript_byte_cap=transcript_byte_cap,
            now=now,
            watchdog_interval_seconds=watchdog_interval_seconds,
            startup_nonce=startup_nonce,
        )
        self._client = client
        self._sandbox_identity = identity
        self._delete_lock = threading.Lock()
        self._deleted = False
        # The HOST checkout this sandbox was uploaded from — kept so the work
        # can be brought back beside it (node N7), and deliberately NOT the
        # base class's ``checkout_dir``: that one is the quota walk's target,
        # and this runtime has no live host tree to walk.
        self._host_checkout_dir = host_checkout_dir
        self._realize_lock = threading.Lock()
        self._realization: CheckoutRealization | None = None
        # The denial log, read once. A separate lock from the realization's
        # because the realization READS it while holding its own, and the
        # delete reads it while holding the delete lock.
        self._denial_lock = threading.Lock()
        self._denials: tuple[str, ...] | None = None

    @property
    def sandbox_identity(self) -> str:
        """The gateway's own id for this sandbox, as ``sandbox get`` gave it."""
        return self._sandbox_identity

    @property
    def realization(self) -> CheckoutRealization | None:
        """This run's realized checkout, or None before anything realized it."""
        return self._realization

    def realize_checkout(self) -> CheckoutRealization | None:
        """Bring the agent's work back to the host, once (node N7).

        None when this handle was given no host checkout to realize beside —
        the certification probe and the handle tests launch that way, and
        neither publishes anything.

        Idempotent, because the paths that release a sandbox can run more than
        once and a second download would land on a destination the first one
        filled. Never raises: the refusal travels on the returned
        :class:`~kew.openshell_checkout.CheckoutRealization`, because the
        caller that acts on it (the dispatch's publication gate) is reached
        through a cleanup seam that discards exceptions.
        """
        from kew.openshell_checkout import (
            realize_sandbox_checkout,
            released_realization,
        )

        with self._realize_lock:
            if self._host_checkout_dir is None or self._realization is not None:
                return self._realization
            denials = self._preserve_denials()
            if self._deleted:
                # A path that ended the workload by DELETING the sandbox got
                # here: there is nothing left to download from, and saying so
                # is a better record than a gateway "no such sandbox" dressed
                # up as a failed transfer. The denials the delete read while
                # the sandbox still existed are kept on it.
                self._realization = released_realization(self._name, denials)
                return self._realization
            self._realization = realize_sandbox_checkout(
                self._client,
                self._name,
                checkout_dir=self._host_checkout_dir,
                denials=denials,
            )
            return self._realization

    def _preserve_denials(self) -> tuple[str, ...]:
        """Read the sandbox's denial log once, while it still exists.

        ``openshell logs`` is the only account of what the gateway refused the
        agent, and on this runtime ending the workload IS deleting the sandbox
        — so every delete reads the log before it, not only the cleanup that
        realizes (PR #2451 review). Without that, an interrupt
        (``ContainerHandle.cancel_launch`` kills, then cleans up), a cap breach
        and a launch unwind all reached the realization with the sandbox
        already gone and recorded no denials at all.

        Nothing is read for a handle with no host checkout to realize beside:
        no realization is ever produced there, so the log would have nowhere
        to be recorded.
        """
        from kew.openshell_checkout import collect_denials

        with self._denial_lock:
            if self._host_checkout_dir is None:
                return ()
            if self._denials is None:
                self._denials = collect_denials(self._client, self._name)
            return self._denials

    def kill(self) -> KillStatus:
        """Delete the sandbox, which is how a workload is ended here."""
        return self._delete()

    def remove(self) -> bool:
        """Already done by :meth:`kill`: one gateway call ends and removes."""
        return self._delete() is KillStatus.ACCEPTED

    def cleanup_identity(self) -> None:
        """The hook the normal path runs — here it realizes, then releases.

        On docker this unlinks a host cidfile. There is no cidfile here, and
        the base class calls it from ``wait``, which makes it exactly the seam
        a gateway object needs: a run that finished cleanly releases its
        sandbox without any caller having to know the runtime.

        The download goes FIRST, and that order is the whole of node N7: the
        delete takes the agent's work with it, and a sandbox is the only place
        that work exists on this runtime. The realization is also what reads
        the denial events, for the same reason.

        The delete is in a ``finally``: a download that fails in a way the
        realization does not model must not cost this sandbox its deletion.
        Releasing the sandbox is the obligation that cannot be skipped — a
        realization that did not happen is a run that does not publish, which
        is recoverable, while a sandbox nobody deleted is an orphan holding a
        provider on the operator's gateway.

        A delete that could not be confirmed is NOT swallowed into a finished
        run (#2625): :attr:`released` stays false, and the dispatch reads that
        and keeps the run pending, so the sandbox — which may still hold the
        run's provider — stays something a recovery comes back for.
        """
        try:
            self.realize_checkout()
        finally:
            status = self._delete()
            if status is not KillStatus.ACCEPTED:
                logger.error(
                    "openshell sandbox %s was not confirmed deleted (%s); its "
                    "run is kept pending until it is",
                    self._name,
                    status.value,
                )

    def _cache_container_id(self) -> None:
        """Say why there is no daemon container id, rather than looking for one.

        The base class looks in a cidfile and, finding none, reports "container
        id file was not supplied" — which would send the OOM probe shelling out
        ``openshell events``, a subcommand that does not exist. A gateway
        sandbox id (``sbx_…``) is not a 64-hex container identity, so the
        honest answer is that this boundary exposes none, and the probe reports
        unavailable WITH that reason.

        Ownership is still accepted: the sandbox exists before the exec this
        handle wraps, so cleanup custody is a fact here even without an id.
        """
        self._container_identity_diagnostic = (
            f"openshell sandbox {self._name} ({self._sandbox_identity}) "
            "exposes no daemon container id"
        )
        if self._admission is not None:
            self._admission.take_ownership()

    def _resolve_unknown_kill(self) -> KillStatus:
        """Settle a delete of unknown outcome by READING the sandbox."""
        return settle_unknown_delete(self._client, self._name)

    def _delete(self) -> KillStatus:
        """Delete, settle an unknown outcome, report it, and never raise.

        :func:`release_sandbox` reads the sandbox back after a delete whose
        outcome is unknown and retries one that is still standing, so a single
        timed-out call is not reported as the release's outcome (#2625).
        """
        with self._delete_lock:
            if self._deleted:
                return KillStatus.ACCEPTED
            self._preserve_denials()
            outcome = release_sandbox(self._client, self._name)
            self._deleted = outcome is KillStatus.ACCEPTED
            return outcome

    @property
    def sandbox_name(self) -> str:
        """The gateway name of this handle's sandbox."""
        return self._name

    @property
    def released(self) -> bool:
        """Whether a delete of this handle's sandbox has been confirmed."""
        with self._delete_lock:
            return self._deleted


def launch_openshell_container(
    config: DispatchConfig,
    store: RunStore | None,
    *,
    run_id: str,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    model: str,
    admission: PendingAdmission | None = None,
    credential: BoundedRunKey | None = None,
    on_spawn: Callable[[subprocess.Popen[str], str, str], None] | None = None,
    permit: Callable[[], None] | None = None,
    client: OpenShellClient | None = None,
    policy_flow: str = DISPATCH_FLOW,
) -> OpenShellSandboxHandle:
    """Create the sandbox, upload the checkout, start the agent, stream it.

    The returned handle keeps :class:`~kew.sandbox.ContainerHandle`'s contract,
    so the caller streams, waits and finalizes exactly as it does on docker.

    ``image`` is the immutable image ID this dispatch was admitted for, never a
    tag: what the route profile claims and what launches must be the same bytes
    (#1550 review F1), and that is as true through a gateway as through a
    daemon.

    ``permit`` is re-asked after the sandbox exists and before the agent is
    exec'd into it, which is the docker path's second creation check
    (``prepare_registered_container``) in its gateway spelling. Create, the
    id read-back and a whole-checkout upload are not instant, and a stop
    landing inside that window must not reach an agent process: the caller's
    pre-create check cannot see it. The refusal unwinds through the same
    cleanup as any other mid-launch failure, so the sandbox is deleted.

    Every failure between the create and the exec deletes the sandbox before it
    propagates, including a create whose own outcome is unknown — the gateway
    may have made it. The original error is still what the caller sees.

    ``policy_flow`` is which boundary this launch is: a dispatch, or the
    adversarial reviewer's narrower one (epic node N12). It is a parameter
    rather than a branch on the caller, because the policy a sandbox is created
    under and the policy a certification was bound to must be the same file —
    :func:`kew.sandbox.require_openshell_egress_certification` resolves it
    through the same function with the same flow.
    """
    from kew.openshell_checkout import retire_realized_checkout
    from kew.run_credentials import openshell_launch_environment
    from kew.sandbox import runner_credential_env_vars

    name = sandbox_name(
        slug,
        project=config.project.repo or config.project.name,
        run_id=run_id,
    )
    gateway = client or OpenShellClient.from_settings(config.sandbox.openshell)
    nonce = uuid.uuid4().hex
    argv = exec_argv(runner, nonce, _agent_args(config, runner, model))
    # The retry ladder hands every attempt the SAME host checkout, so the tree
    # the last attempt realized beside it is still there — and the realization
    # this launch ends in refuses an occupied destination, which would discard
    # this attempt's work undownloaded (PR #2451 review). Retired here, before
    # any gateway state exists to unwind, because a launch from this checkout
    # is what supersedes it.
    retire_realized_checkout(checkout_dir)
    # The name reaches the row BEFORE the create, as the docker path records
    # its container name before creating it: a create whose outcome is unknown
    # and whose delete cannot be confirmed leaves a sandbox only the row can
    # lead a recovery back to (#2625).
    _record_identity(store, run_id, name, None)
    identity = _create_sandbox(
        gateway,
        name,
        store=store,
        image=image,
        runner=runner,
        run_id=run_id,
        config=config,
        credential=credential,
        policy_flow=policy_flow,
    )
    try:
        _record_identity(store, run_id, name, identity)
        gateway.sandbox_upload(name, checkout_dir, CHECKOUT_PATH)
        if permit is not None:
            permit()
        exec_handle = gateway.sandbox_exec(
            name,
            workdir=CHECKOUT_PATH,
            argv=argv,
            env=openshell_launch_environment(os.environ, runner),
            retain=runner_credential_env_vars(runner),
        )
    except BaseException as exc:
        _unwind_sandbox(gateway, name, exc)
        raise
    return _attach_handle(
        exec_handle,
        client=gateway,
        name=name,
        identity=identity,
        startup_nonce=nonce,
        host_checkout_dir=checkout_dir,
        admission=admission,
        credential=credential,
        on_spawn=on_spawn,
    )


def _agent_args(config: DispatchConfig, runner: str, model: str) -> list[str]:
    """The runner's own flags — the same ``"$@"`` the docker entrypoint gets."""
    from kew.runners.registry import get_runner

    return get_runner(runner).container_args(config=config, model=model)


def _create_sandbox(
    gateway: OpenShellClient,
    name: str,
    *,
    store: RunStore | None,
    image: str,
    runner: str,
    run_id: str,
    config: DispatchConfig,
    credential: BoundedRunKey | None,
    policy_flow: str = DISPATCH_FLOW,
) -> str:
    """Create the sandbox and return the gateway's own id for it.

    The id is read back rather than assumed, and that read is also the proof
    that the create committed: ``sandbox create`` prints nothing a caller can
    trust, and a create whose reply was cut off may still have made the object.

    What this create carries is an image, the run's provider, the runner's
    egress policy and labels — and NO resource cap, because the v0.1.2 argv
    surface kew's client is verified against has no memory or CPU flag and
    ``[sandbox] version`` exists to stop this module inventing one. So the
    per-container caps kew enforces on docker are not enforced here, and
    :func:`kew.sandbox._attested_caps` attests ``None`` rather than the ``8g`` /
    ``4`` of a cgroup that does not exist on this runtime (PR #2401 review).

    The policy is read ONCE, matched against a certification on this gateway
    by the digest of that read, and handed to the gateway as a private copy of
    the same bytes (#2624): the packaged path is never the thing created from.
    """
    from kew.openshell_broker import launch_provider

    policy = pin_runner_policy(runner, flow=policy_flow)
    require_launch_certification(
        store,
        gateway=gateway,
        image=image,
        runner=runner,
        policy=policy,
        flow=policy_flow,
    )
    provider = launch_provider(
        config.sandbox, runner, run_id=run_id, issued=credential is not None
    )
    try:
        with materialized_policy(policy.data) as policy_path:
            gateway.sandbox_create(
                name,
                image=image,
                providers=(provider,) if provider else (),
                policy_path=policy_path,
                labels={
                    RUN_LABEL: run_id,
                    RUNNER_LABEL: runner,
                    FLOW_LABEL: policy_flow,
                },
            )
    except OpenShellOutcomeUnknown as exc:
        _unwind_sandbox(gateway, name, exc)
        raise
    try:
        return gateway.sandbox_get(name).identity
    except BaseException as exc:
        _unwind_sandbox(gateway, name, exc)
        raise


def _record_identity(
    store: RunStore | None, run_id: str, name: str, identity: str | None
) -> None:
    """Record the sandbox NAME, and once it exists its gateway id, on the run.

    The name is the identity the gateway answers to. The gateway id
    (``sbx_…``) is what proves the sandbox answering to it is still the one
    this run created: recovery compares it before it downloads or deletes
    anything (#2625). It is stored in the container-id column under the
    gateway's own shape check — ``run_store`` validates it per runtime, so the
    64-hex rule docker readers rely on is not widened for them.
    """
    if store is not None:
        store.set_container_identity(run_id, name, identity, runtime=OPENSHELL_RUNTIME)


def _attach_handle(
    exec_handle: OpenShellExec,
    *,
    client: OpenShellClient,
    name: str,
    identity: str,
    startup_nonce: str,
    host_checkout_dir: Path,
    admission: PendingAdmission | None,
    credential: BoundedRunKey | None,
    on_spawn: Callable[[subprocess.Popen[str], str, str], None] | None,
) -> OpenShellSandboxHandle:
    """Wrap the running exec, deleting the sandbox if anything here fails."""
    from kew.run_credentials import require_credential_lifetime

    handle = OpenShellSandboxHandle(
        exec_handle,
        client=client,
        name=name,
        identity=identity,
        startup_nonce=startup_nonce,
        host_checkout_dir=host_checkout_dir,
    )
    try:
        require_credential_lifetime(credential, WALL_CLOCK_LIMIT_SECONDS)
        if on_spawn is not None:
            on_spawn(exec_handle.process, name, identity)
        if admission is not None:
            handle.accept_admission(admission)
    except BaseException:
        handle.cancel_launch()
        raise
    return handle


def download_checkout(
    gateway: OpenShellClient,
    name: str,
    destination: Path,
    *,
    quota_bytes: int = CHECKOUT_QUOTA_BYTES,
    quota_entries: int = CHECKOUT_QUOTA_ENTRIES,
    free_floor: int = HOST_FREE_SPACE_FLOOR_BYTES,
) -> CheckoutMerge:
    """Copy the sandbox's checkout back over the host checkout at ``destination``.

    The recovery half of the upload: a gateway restart kills the agent and
    keeps its files, so the work is in the sandbox and nowhere else until this
    runs. Called by the driver's gateway-restart recovery before the sandbox is
    deleted, so the bytes exist on the host when the orphan path looks for work
    to preserve.

    The copy is a MERGE, never a replacement. Deleting the host checkout first
    would take its directory identity with it — the recovery path binds a
    checkout by device, inode and an in-checkout nonce
    (:func:`kew.worktree.require_checkout_generation`), and a fresh directory
    is a different checkout that no claim covers. Merging also means a sandbox
    copy missing kew's own metadata leaves the host's in place rather than
    taking it away.

    A verdict (with the reason logged) rather than a raise on failure: the work
    not coming back is bad, and it must not stop the run from being finalized
    and its capacity released. Neither failing verdict deletes the sandbox —
    the copy is the thing in doubt, so the source it was copied from stays
    where an operator can reach it.

    Which failure it was is reported, because the two leave the operator a
    different host checkout: see :data:`CheckoutMerge`.

    A download that exits 0 having written nothing is a failure too, and the
    only one the gateway does not announce. ``"merged"`` is what deletes the
    sandbox holding the only copy of the work and attests
    ``checkout_recovered``, so it is claimed only once bytes have actually
    been seen in the staging tree — not merely because a copy of an empty
    directory succeeded.

    The checkout caps the realization applies hold here too (#2626): the
    staging device has to hold a cap-sized checkout above the free-space
    floor before the transfer starts, the tree that landed is measured
    against the byte and entry caps, and the host checkout's device has to
    hold what is about to be merged into it. A breach is ``"unmerged"`` —
    the work stays in the sandbox rather than on the host's disk.
    """
    with work_dirs.work_tempdir("kew-openshell-recover-") as staging:
        shortfall = _host_room_shortfall(Path(staging), quota_bytes, free_floor)
        if shortfall is not None:
            logger.error(
                "openshell sandbox %s was not asked for %s, so its work stays "
                "in the sandbox: %s",
                name,
                CHECKOUT_PATH,
                shortfall,
            )
            return "unmerged"
        try:
            gateway.sandbox_download(name, CHECKOUT_PATH, Path(staging))
            root = _downloaded_root(Path(staging))
        except (OpenShellError, OSError) as exc:
            logger.error(
                "openshell sandbox %s did not hand back %s, so its work stays "
                "in the sandbox (%s)",
                name,
                CHECKOUT_PATH,
                exc,
            )
            return "unmerged"
        if root is None:
            logger.error(
                "openshell sandbox %s reported %s handed back but left no "
                "files behind, so its work is treated as still in the "
                "sandbox rather than as recovered",
                name,
                CHECKOUT_PATH,
            )
            return "unmerged"
        breach = _recovery_cap_breach(
            root,
            destination,
            quota_bytes=quota_bytes,
            quota_entries=quota_entries,
            free_floor=free_floor,
        )
        if breach is not None:
            logger.error(
                "the checkout downloaded from openshell sandbox %s is not "
                "merged into %s, so the host checkout is as it was and the "
                "work stays in the sandbox: %s",
                name,
                destination,
                breach,
            )
            return "unmerged"
        return _merge_recovered_checkout(root, destination, name)


def _recovery_cap_breach(
    root: Path,
    destination: Path,
    *,
    quota_bytes: int,
    quota_entries: int,
    free_floor: int,
) -> str | None:
    """Why a downloaded checkout may not be merged, or ``None`` if it may.

    The same caps the realization refuses publication over
    (``kew.openshell_checkout._require_within_caps``), then the room the merge
    itself needs: it writes ``used`` bytes into the host checkout, which has
    to leave the free-space floor behind it.
    """
    used, entries = checkout_usage(
        root, stop_at_bytes=quota_bytes, stop_at_entries=quota_entries
    )
    if used > quota_bytes or entries > quota_entries:
        return (
            f"it breaches the checkout caps ({used} bytes / {entries} entries "
            f"vs {quota_bytes} bytes / {quota_entries} entries)"
        )
    return _host_room_shortfall(_nearest_existing(destination), used, free_floor)


def _nearest_existing(path: Path) -> Path:
    """*path*, or its closest existing ancestor: the device a write there lands on.

    The driver's recovery merges into a checkout path that does not exist yet,
    and a probe of the missing path would fail and skip the room check.
    """
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _host_room_shortfall(path: Path, needed: int, free_floor: int) -> str | None:
    """Why *path*'s device cannot take *needed* bytes above the floor, or ``None``.

    A failed probe is not read as a shortfall, for the reason
    ``kew.openshell_checkout._require_host_room`` gives: refusing over an
    unreadable ``statvfs`` would strand the agent's work over kew's own
    instrumentation.
    """
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        logger.warning(
            "the free space of %s could not be probed (%s); the recovery's "
            "host-room floor is not checked",
            path,
            exc,
        )
        return None
    if free - needed >= free_floor:
        return None
    return (
        f"the host has {free} bytes free at {path}, which cannot take "
        f"{needed} bytes and still leave the {free_floor} byte free-space floor"
    )


def _merge_recovered_checkout(
    root: Path, destination: Path, name: str
) -> CheckoutMerge:
    """Merge a downloaded checkout into the host checkout, reporting what landed.

    The merge is not atomic and cannot be made so: :func:`_merge_tree` copies
    every entry it can and raises :class:`shutil.Error` carrying only the ones
    it could not, so a host checkout holding an unwritable directory, or a
    filesystem that fills up mid-copy, ends with some of the sandbox's files
    in place and the rest still in the sandbox.

    That outcome has to be told apart from a merge that never started, because
    the orphan path running next reads the host checkout as the agent's work:
    a part-way merge is read as dirty and offered as a continuation, so the
    tree the operator would continue from is a mix neither side produced.
    Collapsing it into the same verdict as a refused download would attest
    ``checkout_recovered: false`` over it — which an operator reads as "the
    host checkout was left alone", the one thing that is not true.
    """
    try:
        _merge_tree(root, destination)
    except OSError as exc:
        if _any_entry_landed(root, destination):
            logger.error(
                "the checkout downloaded from openshell sandbox %s merged only "
                "part-way into %s (%s), so the host checkout now holds some of "
                "the sandbox's files and not the rest; the sandbox keeps the "
                "whole copy and any continuation starts from the mixed tree",
                name,
                destination,
                exc,
            )
            return "part_merged"
        logger.error(
            "the checkout downloaded from openshell sandbox %s could not be "
            "merged into %s (%s), so the host checkout is as it was and the "
            "work stays in the sandbox",
            name,
            destination,
            exc,
        )
        return "unmerged"
    return "merged"


def _merge_tree(root: Path, destination: Path) -> None:
    """Merge *root* into *destination* without writing through a symlink.

    ``shutil.copytree(..., dirs_exist_ok=True)`` follows a symlink it finds in
    the destination: a host ``escape -> /outside`` and a sandbox directory
    ``escape/`` wrote the sandbox's files into ``/outside`` (#2626). Here a
    destination entry that is a symlink is never written THROUGH: where the
    sandbox has something of that name, the link itself is removed and
    replaced, and its target is never touched. Files are created with
    ``O_EXCL | O_NOFOLLOW`` after the old entry is unlinked, so a link that
    appears between the check and the write fails the write rather than
    redirecting it, and a hard link's other names keep their bytes.

    Otherwise copytree's contract: every entry that can be copied is, and the
    ones that could not are raised together as :class:`shutil.Error`. A
    destination root that is a symlink, or not a directory, is refused whole;
    one that does not exist yet is created, as copytree created it — the
    driver's recovery merges into a host checkout path proven free.
    """
    if not os.path.lexists(destination):
        os.makedirs(destination)
    if not stat.S_ISDIR(os.lstat(destination).st_mode):
        raise NotADirectoryError(
            f"{destination} is not a directory (a symlink is refused, never followed)"
        )
    errors: list[tuple[str, str, str]] = []
    _merge_directory(root, destination, errors)
    if errors:
        raise shutil.Error(errors)


def _merge_directory(
    source: Path, destination: Path, errors: list[tuple[str, str, str]]
) -> None:
    """Merge one directory level, recording each entry that could not land."""
    with os.scandir(source) as listing:
        entries = sorted(listing, key=lambda entry: entry.name)
    for entry in entries:
        item, target = Path(entry.path), destination / entry.name
        try:
            mode = item.lstat().st_mode
            if stat.S_ISDIR(mode):
                _real_directory(target, mode)
                _merge_directory(item, target, errors)
            elif stat.S_ISLNK(mode):
                _clear_non_directory(target)
                os.symlink(os.readlink(item), target)
            elif stat.S_ISREG(mode):
                _clear_non_directory(target)
                _copy_file_exclusive(item, target)
            else:
                raise OSError(f"{item} is neither a file, a directory nor a symlink")
        except OSError as exc:
            errors.append((str(item), str(target), str(exc)))


def _real_directory(target: Path, mode: int) -> None:
    """Make *target* a real directory, replacing a symlink rather than following it."""
    if os.path.lexists(target):
        existing = os.lstat(target).st_mode
        if stat.S_ISDIR(existing):
            return
        if not stat.S_ISLNK(existing):
            raise FileExistsError(f"{target} exists and is not a directory")
        os.unlink(target)
    os.mkdir(target, stat.S_IMODE(mode) | stat.S_IRWXU)


def _clear_non_directory(target: Path) -> None:
    """Unlink whatever non-directory sits at *target*; a directory is an error."""
    if not os.path.lexists(target):
        return
    if stat.S_ISDIR(os.lstat(target).st_mode):
        raise IsADirectoryError(f"{target} is a directory on the host")
    os.unlink(target)


def _copy_file_exclusive(item: Path, target: Path) -> None:
    """Copy one regular file to a *target* that must not exist, keeping its stat."""
    source_stat = item.lstat()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with open(item, "rb") as reader:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            os.fchmod(writer.fileno(), stat.S_IMODE(source_stat.st_mode))
    os.utime(
        target,
        ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
        follow_symlinks=False,
    )


def _any_entry_landed(root: Path, destination: Path) -> bool:
    """Whether any of the downloaded tree's entries is now in the checkout.

    The question a failed merge leaves open. ``copytree`` reports the entries
    it could not copy, never the ones it already wrote, so the host checkout
    has to be read rather than inferred from the error.

    Asked of paths, not of contents: a file the host already had under the
    same name reads as landed even if the merge never reached it. That is the
    conservative direction — over-reporting a mixed tree costs an operator one
    needless hand-merge, where under-reporting one offers a continuation over
    a half-merged checkout while attesting the host was untouched.
    """
    return any(
        (destination / item.relative_to(root)).exists(follow_symlinks=False)
        for item in _recovered_entries(root)
    )


def _downloaded_root(staging: Path) -> Path | None:
    """The checkout inside a download, or ``None`` if none of it came back.

    v0.1.2 is not documented on whether ``sandbox download /work DEST`` fills
    ``DEST`` or creates ``DEST/work`` inside it, and the spike recorded no
    fixture for it. Both are accepted rather than guessed at, because guessing
    wrong copies an empty tree over the recovered work and reports success.

    The sibling hazard is a download that wrote nothing at all, or only the
    directory it was pointed at: there is no shape to pick, and copying it
    would succeed and report work recovered that never arrived. ``None`` says
    so, which is the same outcome as a download the gateway refused.
    """
    nested = staging / PurePosixPath(CHECKOUT_PATH).name
    root = _download_shape(staging, nested)
    return root if _holds_recovered_bytes(root) else None


def _download_shape(staging: Path, nested: Path) -> Path:
    """Which level of the download the checkout is at.

    Checkout metadata settles it whenever it came back: what was uploaded is a
    worktree with a contained ``.git`` directory, so the level carrying one is
    the checkout. That is the only test that stays right for the shapes the
    entry count cannot tell apart — a ``DEST/work`` delivered alongside
    anything the CLI wrote for itself, and a filled ``DEST`` whose checkout
    has a ``work`` directory of its own. Failing that, a lone ``DEST/work``
    directory is the nested shape and anything else is a filled ``DEST``.
    """
    if (staging / ".git").is_dir():
        return staging
    if (nested / ".git").is_dir():
        return nested
    unaccompanied = list(staging.iterdir()) == [nested]
    return nested if nested.is_dir() and unaccompanied else staging


def _holds_recovered_bytes(root: Path) -> bool:
    """Whether a downloaded tree carries files, not just empty directories.

    A directory on its own is not work: an empty tree merges cleanly and
    recovers nothing.
    """
    return next(_recovered_entries(root), None) is not None


def _recovered_entries(root: Path) -> Iterator[Path]:
    """The entries of a downloaded tree that make it a recovery.

    Symlinks count alongside files: the upload and the merge both preserve
    them, so one is part of the agent's work. One definition for both readers
    — what makes a download non-empty, and what a failed merge is checked for
    on the host — so the two can never disagree about what landed.
    """
    return (item for item in root.rglob("*") if item.is_file() or item.is_symlink())


def _unwind_sandbox(gateway: OpenShellClient, name: str, exc: BaseException) -> None:
    """Delete a sandbox during an unwind without replacing the real failure.

    Raising here would discard the launch error the operator needs and leave
    them holding a cleanup error instead. So an unconfirmed delete is attached
    to the in-flight error as :data:`UNRELEASED_SANDBOX_ATTR` rather than
    raised: the dispatch reads it as cleanup evidence and keeps the run
    pending, because a terminal run is one no recovery ever comes back for,
    and this sandbox may still hold the run's provider (#2625).
    """
    status = release_sandbox(gateway, name)
    if status is KillStatus.ACCEPTED:
        return
    logger.error(
        "openshell sandbox %s was left on the gateway after a failed launch "
        "(delete %s); the run stays pending until it is gone",
        name,
        status.value,
    )
    setattr(exc, UNRELEASED_SANDBOX_ATTR, name)


def release_sandbox(gateway: OpenShellClient, name: str) -> KillStatus:
    """Delete a sandbox and settle the outcome; never raise.

    ``accepted`` means the sandbox is proven gone or terminal. A delete whose
    outcome is unknown is settled by READING the sandbox, and a sandbox still
    standing after that read gets one more delete, settled the same way — so
    a single timed-out call is never the last word (#2625).
    """
    status = _delete_once(gateway, name)
    if status is KillStatus.UNKNOWN:
        status = settle_unknown_delete(gateway, name)
    if status is KillStatus.ACCEPTED:
        return status
    status = _delete_once(gateway, name)
    if status is KillStatus.UNKNOWN:
        status = settle_unknown_delete(gateway, name)
    return status


def _delete_once(gateway: OpenShellClient, name: str) -> KillStatus:
    """One ``sandbox delete``, as accepted / refused / unknown."""
    try:
        gateway.sandbox_delete(name)
    except OpenShellNotFound:
        # Created by this run and now absent: the thing being asked for has
        # happened. Unlike docker's "No such container", which can also mean
        # "not created yet", there is no such window here — the name reaches
        # a delete only after a create was attempted under it.
        return KillStatus.ACCEPTED
    except OpenShellOutcomeUnknown:
        return KillStatus.UNKNOWN
    except OpenShellError as exc:
        logger.warning("openshell refused to delete sandbox %s (%s)", name, exc)
        return KillStatus.REFUSED
    except ValueError as exc:
        logger.warning("openshell sandbox %s cannot be deleted (%s)", name, exc)
        return KillStatus.REFUSED
    return KillStatus.ACCEPTED


def settle_unknown_delete(gateway: OpenShellClient, name: str) -> KillStatus:
    """Settle a delete of unknown outcome by READING the sandbox."""
    try:
        sandbox = gateway.sandbox_get(name)
    except OpenShellNotFound:
        return KillStatus.ACCEPTED
    except (OpenShellError, ValueError) as exc:
        logger.warning(
            "openshell sandbox %s could not be read to settle its delete "
            "(%s); the outcome stays unknown",
            name,
            exc,
        )
        return KillStatus.UNKNOWN
    if sandbox.status.strip().casefold() in TERMINAL_STATUSES:
        return KillStatus.ACCEPTED
    return KillStatus.REFUSED
