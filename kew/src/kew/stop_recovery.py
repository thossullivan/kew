"""Bounded fleet-stop effects usable below the runner layer.

Only fixed container inspections, the removal of a dead run's stopped
container by its full id, and GitHub issue label operations run here. The
store records its intent before it asks for any removal. This module cannot
launch git, agents, containers, or arbitrary executables.
The store uses it after reopening, without importing dispatch or runner code.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from kew.credential_environment import child_environment
from kew.process_groups import (
    OwnerSnapshot,
    attempt_groups,
    attempt_identities_from_events,
    group_alive,
    host_stop_is_dead,
    owner_alive,
)
from kew.runtime_names import OPENSHELL_RUNTIME, require_backed_runtime

if TYPE_CHECKING:
    from kew.run_store import RunRecord

LabelReset = Callable[[int, dict[str, str]], None]
STARTUP_GRACE_SECONDS = 600

#: What a run whose container never started is finalized as (#2973, #3124).
CONTAINER_START_FAILED = "container_start_failed"

# Every registered container carries the ledger and run that created it, so a
# leftover holding a host-wide ``kew-<item>`` name can be traced to its owner
# before anything removes it (#2973).
KEW_LEDGER_LABEL = "io.kew.ledger"
KEW_RUN_LABEL = "io.kew.run"

#: The one read that tells a never-started container from any other: its full
#: id, its state and process, and the labels naming who created it.
NEVER_STARTED_INSPECT_FORMAT = (
    "{{.Id}} {{.State.Status}} {{.State.Pid}} {{json .Config.Labels}}"
)

#: How a caller hands in a container-state answer this module cannot reach for
#: itself: ``backend_for(runtime, openshell=…).state``, taking the container
#: identity and its recorded name. It is how ``openshell`` is answered, that
#: boundary being a gateway call rather than the fixed ``inspect`` below — and
#: the backend which makes it reaches the subprocess chokepoint, which sits
#: ABOVE the store in the layered-dispatch contract (``pyproject.toml``,
#: ``lint-imports``) while ``run_store`` imports this module. So the caller
#: that holds the configuration supplies the probe too, and one that holds
#: neither gets an unknown which says why — never a clean default.
ContainerStateProbe = Callable[[str, str], str]


def _tool(name: str) -> str:
    """Resolve only the three tools supported by these fixed operations."""
    if name not in {"docker", "podman", "gh"}:
        raise ValueError(f"Unsupported stop-recovery tool: {name}")
    path = shutil.which(name)
    if path is None:
        raise FileNotFoundError(f"Stop-recovery tool unavailable: {name}")
    return path


#: What tells one container daemon from another: Docker's engine id with its
#: host and data root, Podman's host with its storage root (rootless and
#: rootful Podman on one host keep separate containers).
DAEMON_IDENTITY_TEMPLATES: dict[str, str] = {
    "docker": "{{.ID}} {{.Name}} {{.DockerRootDir}}",
    "podman": "{{.Host.Hostname}} {{.Store.GraphRoot}}",
}

#: The meta key under which a run's launch records the daemon it created its
#: container on (:func:`container_daemon_key`).
CONTAINER_DAEMON_META = "container_daemon"


def container_daemon_key(run_id: str) -> str:
    """Where *run_id*'s launch recorded its container's daemon."""
    return f"{CONTAINER_DAEMON_META}:{run_id}"


def daemon_identity_answer(name: str, result: object) -> str | None:
    """One ``<runtime> info`` answer as a daemon identity, or ``None``."""
    template = DAEMON_IDENTITY_TEMPLATES[name]
    stdout = getattr(result, "stdout", None)
    if getattr(result, "returncode", 1) != 0 or not isinstance(stdout, str):
        return None
    fields = stdout.split()
    if len(fields) != len(template.split()) or "<no" in stdout:
        return None
    return f"{name}:{' '.join(fields)}"


def current_daemon_identity(runtime: str) -> str | None:
    """The daemon *runtime* answers from now, by the fixed ``info`` read."""
    name = Path(runtime).name
    template = DAEMON_IDENTITY_TEMPLATES.get(name)
    if template is None:
        return None
    try:
        result = subprocess.run(  # nosec B603 — fixed read-only daemon identity
            [_tool(name), "info", "--format", template],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=child_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning("Cannot name the %s daemon: %s", name, exc)
        return None
    return daemon_identity_answer(name, result)


def absence_refusal(runtime: str, recorded_daemon: str | None) -> str | None:
    """Why the runtime's "No such container" proves nothing; ``None`` if it does.

    A container id means something only on the daemon that holds it: asked of
    another daemon (a switched context, another host), it is always "no such
    container". So absence is trusted only from the daemon the run's launch
    recorded, and a daemon that cannot be named now proves nothing.
    """
    if recorded_daemon is None:
        # Legacy row: its launch predates recording the daemon, so the
        # runtime's absence answer is trusted as it was before this check.
        return None
    current = current_daemon_identity(runtime)
    if current is None:
        return "the runtime's daemon cannot be named, so its absence proves nothing"
    if current != recorded_daemon:
        return "the runtime answers from another daemon than the one the run used"
    return None


CONTAINER_INSPECT_FORMAT = "{{.Id}} {{.Name}} {{.State.Running}}"


def valid_container_id(identity: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", identity) is not None


def valid_gateway_sandbox_id(identity: str) -> bool:
    """The shape an OpenShell gateway sandbox id (``sbx_…``) may take.

    Deliberately not :func:`valid_container_id`: a gateway id is not a daemon
    container identity, and no docker reader may be handed one.
    """
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identity) is not None


def container_target(record: RunRecord) -> str | None:
    """Recover creation interrupted before the cidfile identity reached SQLite."""
    identity = record.container_id
    if identity is None and record.container_cidfile:
        try:
            identity = (
                Path(record.container_cidfile).read_text(encoding="utf-8").strip()
            )
        except OSError as exc:
            logging.getLogger(__name__).debug("Container identity unavailable: %s", exc)
    return identity if identity is not None and valid_container_id(identity) else None


def record_container_state(
    runtime: str, record: RunRecord, *, probe: ContainerStateProbe | None = None
) -> str:
    """The recorded container's state, asked of the runtime it launched under.

    The identity a row carries differs by runtime: a docker run records the
    64-hex container id (or a cidfile holding it), while an OpenShell run
    records the sandbox NAME and no id at all — so reaching for
    :func:`container_target` on a gateway row would answer "unknown" about a
    sandbox the gateway can describe perfectly well.
    """
    if runtime == OPENSHELL_RUNTIME:
        # The recorded gateway id rides where docker's name evidence does: the
        # sandbox answering to the name must still be the one this run made.
        return container_state(
            runtime,
            record.container_name or "",
            record.container_id or "",
            probe=probe,
        )
    identity = container_target(record)
    if identity is None:
        return "unknown"
    return container_state(runtime, identity, record.container_name or "")


def container_state(
    runtime: str,
    identity: str,
    name: str,
    *,
    probe: ContainerStateProbe | None = None,
) -> str:
    """Inspect only a full immutable id; names are comparison evidence.

    On ``openshell`` the identity IS the sandbox name and the question is a
    gateway call, so it is answered by the caller's ``probe`` — the OpenShell
    backend's own ``state`` — rather than by the fixed ``inspect`` below.
    ``name`` there is the recorded gateway sandbox id, the evidence the
    answering sandbox must carry.
    """
    if runtime == OPENSHELL_RUNTIME:
        return _gateway_sandbox_state(identity, name, probe)
    if not valid_container_id(identity):
        return "unknown"
    try:
        # The runtime must have a backend before it is inspected: a name this
        # build cannot drive is an unknown state that says so, never a clean
        # default. RuntimeBackendUnavailable is a ValueError, so it lands in
        # the handler below carrying its own message. (The registry, not
        # `backend_for`: the store layer must not reach the subprocess
        # chokepoint — see kew.runtime_names.)
        require_backed_runtime(runtime)
        result = subprocess.run(  # nosec B603 — fixed read-only inspection by id
            [_tool(runtime), "inspect", "--format", CONTAINER_INSPECT_FORMAT, identity],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=child_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning("Cannot inspect stopped container: %s", exc)
        return "unknown"
    return inspected_container_state(result, identity, name)


def _gateway_sandbox_state(
    name: str, gateway_id: str, probe: ContainerStateProbe | None
) -> str:
    """Ask the caller's gateway probe, failing closed to ``unknown`` with a reason.

    No probe means the caller could not name the gateway — a sweep holding a
    run row and no ``[sandbox.openshell]``. That is an unknown which says so,
    and it retains the run's capacity; the driver reconciler, which does hold
    the configuration, is what reaps the run.
    """
    if not name.strip():
        return "unknown"
    if probe is None:
        logging.getLogger(__name__).warning(
            "openshell sandbox %s cannot be inspected from here: no gateway "
            "backend was supplied, so its state stays unknown",
            name,
        )
        return "unknown"
    try:
        return probe(name, gateway_id)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning("Cannot inspect sandbox: %s", exc)
        return "unknown"


def inspected_container_state(
    result: subprocess.CompletedProcess[str], identity: str, name: str
) -> str:
    """Require both immutable identity and its recorded name before trusting state."""
    if result.returncode == 0:
        fields = result.stdout.strip().split()
        if len(fields) != 3:
            return "unknown"
        if fields[:2] != [identity, "/" + name]:
            logging.getLogger(__name__).warning("container_identity_mismatch")
            return "mismatch"
        return {"true": "running", "false": "stopped"}.get(fields[2], "unknown")
    if container_missing(result.stderr, identity, name):
        return "absent"
    return "unknown"


def container_missing(stderr: str, identity: str, name: str) -> bool:
    """Require an explicit absence response identifying the inspected container."""
    targets = "|".join(re.escape(value) for value in (identity, name) if value)
    pattern = (
        r"(?i:(?:Error(?: response from daemon)?:\s*)?"
        r"No such (?:container|object):\s*)(?:" + targets + ")"
    )
    return any(re.fullmatch(pattern, line.strip()) for line in stderr.splitlines())


def contained_client_is_dead(record: RunRecord) -> bool:
    """An empty registered client group can no longer create a container."""
    groups = attempt_groups(record.attempt_pgid, record.log_file)
    return bool(groups) and all(group_alive(group) is False for group in groups)


def pending_owner_is_dead(
    record: RunRecord,
    now: datetime,
    *,
    snapshot: OwnerSnapshot | None = None,
    remove_stopped: StoppedRemoval | None = None,
    daemon: str | None = None,
) -> bool:
    """Unknown identity or startup evidence always retains pending capacity.

    A contained run's container must be absent or, when it is stopped,
    removed through *remove_stopped*, which records its intent before it
    removes anything. Without one, a stopped container keeps its run.
    Absence counts only from *daemon*, the one the run's launch recorded
    (:func:`absence_refusal`).
    """
    if not record.container_name:
        return host_stop_is_dead(
            record.pgid,
            record.owner_pid,
            record.attempt_pgid,
            record.log_file,
            record.owner_start_time,
            record.owner_ppid,
            record.owner_command,
            record.owner_start_token,
            snapshot=snapshot,
        )
    if (
        owner_alive(
            record.pgid or 0,
            record.owner_pid,
            record.owner_start_time,
            record.owner_ppid,
            record.owner_command,
            record.owner_start_token,
            record.owner_boot_id,
            snapshot=snapshot,
        )
        is not False
    ):
        return False
    if now - record.started_at <= timedelta(seconds=STARTUP_GRACE_SECONDS):
        return False
    if not contained_client_is_dead(record):
        return False
    runtime = record.cleanup_runtime or record.container_runtime
    if runtime is None:
        return False
    return _container_gone_or_removed(runtime, record, remove_stopped, daemon)


def _container_gone_or_removed(
    runtime: str,
    record: RunRecord,
    remove_stopped: StoppedRemoval | None,
    daemon: str | None,
) -> bool:
    """Absent, or stopped and then removed by kew through *remove_stopped*.

    A stopped container still exists, so it frees no slot on its own
    (#3009 F2); only absence, or kew removing it, does. The caller has proven
    both its client and its owner dead.
    """
    state = record_container_state(runtime, record)
    if state == "stopped" and runtime in {"docker", "podman"}:
        return remove_stopped is not None and remove_stopped(runtime, record)
    return state == "absent" and absence_refusal(runtime, daemon) is None


def contained_stop_reason(
    record: RunRecord, now: datetime, probe: Callable[[], str]
) -> str:
    """Return finalization evidence only after both independent owners are gone."""
    groups = attempt_groups(record.attempt_pgid, record.log_file)
    if groups:
        if all(group_alive(group) is False for group in groups) and probe() in {
            "absent",
            "stopped",
        }:
            return "Registered runtime client dead; container absent or terminated"
        return ""
    # Malformed evidence is not a legacy row without a registration.
    if attempt_identities_from_events(record.log_file) is None:
        return ""
    if now - record.started_at <= timedelta(seconds=STARTUP_GRACE_SECONDS):
        return ""
    if probe() == "absent":
        return (
            "Legacy container confirmed absent past startup grace; "
            "client registration unavailable"
        )
    return ""


@dataclass(frozen=True)
class DeadContainer:
    """A dead run's container kew has proved it may remove, or proof of none.

    ``container_id`` is the full id to remove; None when the runtime answered
    that there is no container at all. ``state`` is what the runtime said of
    it (``created``, ``exited``, ...), or ``absent``.
    """

    runtime: str
    container_id: str | None
    state: str


#: How the store hands :func:`pending_owner_is_dead` a removal it can audit,
#: given the runtime and the run whose stopped container may go. True only
#: once that container is gone. Without one, a stopped container keeps its run.
StoppedRemoval = Callable[[str, "RunRecord"], bool]


def never_started_verdict(
    record: RunRecord,
    now: datetime,
    *,
    ledger_id: str | None,
    snapshot: OwnerSnapshot | None = None,
    daemon: str | None = None,
) -> DeadContainer | str:
    """The one rule for a run whose container never started, or why not.

    All of it must hold: the owner is proven dead (``False``, never an unknown
    ``None``); the startup grace has passed; no attempt group was registered
    and the log names no attempt identity; and the container is ``created``
    with pid 0, or absent. The container is found by its recorded id, or by
    its name AND labels naming this ledger and this run; an id read back from
    the cidfile alone must carry those labels too. A string is the reason the
    run is retained.

    A run placed on another host is always retained: this host's runtime
    cannot see that host's container, so its answer proves nothing there.
    Likewise "no such container" proves absence only from *daemon*, the one
    the run's launch recorded (:func:`absence_refusal`). Nothing is removed
    here: the caller records its intent first.
    """
    refused = never_started_refusal(record, now, snapshot)
    if refused is not None:
        return refused
    return never_started_container_verdict(record, ledger_id=ledger_id, daemon=daemon)


def never_started_container_verdict(
    record: RunRecord, *, ledger_id: str | None, daemon: str | None
) -> DeadContainer | str:
    """The container half of :func:`never_started_verdict`, for a caller
    that has already applied :func:`never_started_refusal`."""
    runtime = record.cleanup_runtime or record.container_runtime
    if runtime not in {"docker", "podman"} or not record.container_name:
        return "no container runtime recorded for this run"
    return _never_started_container(runtime, record, ledger_id, daemon)


def never_started_refusal(
    record: RunRecord, now: datetime, snapshot: OwnerSnapshot | None
) -> str | None:
    """Why *record* is outside the never-started rule before any inspect.

    Reads only the row and the process table, never the store, so a caller
    can apply it to every pending row before it reads anything else.
    """
    if record_placed_remotely(record):
        return "the run is placed on another host"
    alive = owner_alive(
        record.pgid or 0,
        record.owner_pid,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
        record.owner_boot_id,
        snapshot=snapshot,
    )
    if alive is None:
        return "owner liveness is unknown"
    if alive:
        return "owner is alive"
    if now - record.started_at <= timedelta(seconds=STARTUP_GRACE_SECONDS):
        return "still inside the startup grace"
    if (
        record.attempt_pgid is not None
        or attempt_identities_from_events(record.log_file) != {}
    ):
        return "an attempt was started or its evidence is unreadable"
    return None


def record_placed_remotely(record: RunRecord) -> bool:
    """Whether *record*'s agent runs on another host.

    The same rule as :func:`kew.fleet_placement.is_remote`, read straight off
    the row: the store layer may not import the placement module, which
    reaches the runners through the config.
    """
    from kew.run_store import LOCAL_RUN_HOST

    return (record.run_host or LOCAL_RUN_HOST) != LOCAL_RUN_HOST


def _never_started_container(
    runtime: str, record: RunRecord, ledger_id: str | None, daemon: str | None
) -> DeadContainer | str:
    """Find the run's container by id, or by name and its owner labels."""
    identity = container_target(record)
    target = identity or record.container_name or ""
    result = _inspect(runtime, target, NEVER_STARTED_INSPECT_FORMAT)
    if result is None:
        return "container state unreadable"
    if result.returncode != 0:
        return _absent_container(runtime, result, identity, record, daemon)
    read = _read_container(runtime, result.stdout, identity, record, ledger_id)
    if isinstance(read, str):
        return read
    if read.state != "created":
        return f"container is {read.state}, not created and never started"
    return read


def _absent_container(
    runtime: str,
    result: subprocess.CompletedProcess[str],
    identity: str | None,
    record: RunRecord,
    daemon: str | None,
) -> DeadContainer | str:
    """A failed inspect: proof of absence only as "no such container", and
    only from the daemon the run's launch recorded."""
    if not container_missing(
        result.stderr, identity or "", record.container_name or ""
    ):
        return "container state unreadable"
    return absence_refusal(runtime, daemon) or DeadContainer(runtime, None, "absent")


#: The states of a container with no process, which ``rm`` without force takes.
_REMOVABLE_STATES = frozenset({"created", "exited", "dead"})


def _read_container(
    runtime: str,
    stdout: str,
    identity: str | None,
    record: RunRecord,
    ledger_id: str | None,
) -> DeadContainer | str:
    """Read one inspect answer: a container with no process that is this run's.

    Only an id the row itself recorded is trusted on its own. A container
    found by name, or by an id read back from the cidfile, must carry labels
    naming this ledger and this run.
    """
    fields = stdout.strip().split(" ", 3)
    if len(fields) != 4 or not valid_container_id(fields[0]):
        return "container state unreadable"
    if fields[1] not in _REMOVABLE_STATES or fields[2] != "0":
        return f"container is {fields[1]}, not stopped"
    if identity is not None and fields[0] != identity:
        return "container identity mismatch"
    if identity is None or identity != record.container_id:
        refused = _label_refusal(fields[3], record.run_id, ledger_id)
        if refused is not None:
            return refused
    return DeadContainer(runtime, fields[0], fields[1])


def _label_refusal(text: str, run_id: str, ledger_id: str | None) -> str | None:
    """Why the labels do not name this run, or ``None`` when they do."""
    try:
        labels = json.loads(text)
    except ValueError:
        return "container labels unreadable"
    if (
        ledger_id is not None
        and isinstance(labels, Mapping)
        and labels.get(KEW_LEDGER_LABEL) == ledger_id
        and labels.get(KEW_RUN_LABEL) == run_id
    ):
        return None
    return "container under this name is not labelled as this run's"


def read_stopped_container(
    runtime: str, record: RunRecord, *, ledger_id: str | None
) -> DeadContainer | str:
    """The recorded container of a dead run, when it exists and is not running.

    Read by its full id, which must still answer to the recorded name. An id
    read back only from the cidfile must also carry this ledger's and this
    run's labels. A string is why it may not be removed.
    """
    identity = container_target(record)
    if identity is None:
        return "container identity unknown"
    if record_container_state(runtime, record) != "stopped":
        return "container is not stopped"
    result = _inspect(runtime, identity, NEVER_STARTED_INSPECT_FORMAT)
    if result is None or result.returncode != 0:
        return "container state unreadable"
    return _read_container(runtime, result.stdout, identity, record, ledger_id)


def _inspect(
    runtime: str, target: str, fmt: str
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # nosec B603 — fixed read-only container inspection
            [_tool(runtime), "inspect", "--type", "container", "--format", fmt, target],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=child_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning("Cannot inspect container: %s", exc)
        return None


def remove_stopped_container(runtime: str, identity: str) -> bool:
    """Remove a container that is not running by its full id, without force.

    ``rm`` without ``-f`` refuses a running container, so a start that lands
    between the read and the removal keeps its container. A refused ``rm``
    still counts when the runtime then answers that this exact id is gone.
    The caller records its intent to remove before it calls this.
    """
    try:
        result = subprocess.run(  # nosec B603 — fixed removal by full container id
            [_tool(runtime), "rm", identity],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=child_environment(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logging.getLogger(__name__).warning("Cannot remove container: %s", exc)
        return False
    if result.returncode == 0:
        return True
    gone = _inspect(runtime, identity, "{{.Id}}")
    return (
        gone is not None
        and gone.returncode != 0
        and container_missing(gone.stderr, identity, "")
    )


def reset_labels(issue_num: int, task: dict[str, str]) -> None:
    """Apply the persisted label intent; raise before acknowledging any failure."""
    if task["tracker"] != "github":
        return
    gh = _tool("gh")
    result = subprocess.run(  # nosec B603 — fixed GitHub issue label read
        [
            gh,
            "issue",
            "view",
            str(issue_num),
            "--repo",
            task["repo"],
            "--json",
            "labels",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env=child_environment(),
    )
    current = {label["name"] for label in json.loads(result.stdout)["labels"]}
    remove = [
        task[key] for key in ("in_progress", "agent_working") if task[key] in current
    ]
    if task["ready"] in current and not remove:
        return
    argv = [
        gh,
        "issue",
        "edit",
        str(issue_num),
        "--repo",
        task["repo"],
        "--add-label",
        task["ready"],
    ]
    for label in remove:
        argv.extend(["--remove-label", label])
    subprocess.run(  # nosec B603 — fixed GitHub issue label update
        argv,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env=child_environment(),
    )
