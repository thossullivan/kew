"""Provision a review checkout's locked dependencies before the reviewer starts.

A contained adversarial review can execute the project's acceptance commands
only when the checkout carries an installed environment: the review image
ships ``uv`` and a Python interpreter, nothing else, and the reviewer must not
download anything itself. So the host installs the lockfile's dependencies
once, inside the same image, network, and hardening the reviewer gets.

The install executes the PR's own build backend, so it never sees the trusted
review inputs: it runs against a disposable copy of the tree (no ``.git``, no
``.kew-*`` files, no runner-loaded surfaces), copied under the sandbox's
checkout quota and the host's free-space floor (estimated before, charged as
written during), and bounded by the container handle's wall-clock, transcript,
quota and free-space caps while it runs. The environment is pinned to
``kew/.venv`` for the install and the check; the check prints the interpreter
it runs from before the project's own modules import, and it must be that
environment's. Only that directory moves into the checkout, through paths
verified to contain no symlink on either side. Provisioning receives no runner
credential.

The review must not start beside anything this module cannot vouch for: a
provisioning launch the runtime client did not see created and ended, a
container the daemon does not positively confirm gone, or a checkout that
cannot be made truthful (an environment the PR planted that cannot be removed).
Each raises :class:`ProvisioningUnconfirmed`, and the disposable copy is then
retained rather than removed, since a container may still hold it; a
cancellation after a launch retains it for the same reason. A failed
provisioning otherwise leaves no ``.venv`` at all, so the reviewer's absence
check tells the truth even when the PR tracked one.

Known limit: the environment that moves back is the PR's own dependency tree,
built by the PR's build backend, which can also replace the interpreter binary
the check runs. The check is therefore a usability smoke test of that tree,
not proof of its authenticity; the reviewer executes the PR's code when it
runs the PR's tests in any case, so this adds no authority beyond that. What
provisioning gains is separation during the install, credential exclusion,
and resource monitoring.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import BinaryIO, NamedTuple

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.credential_surface import exception_text
from kew.openshell_egress import PROVISION_FLOW
from kew.openshell_launch import is_openshell
from kew.progress import _warn
from kew.review_dispatch import _HEAD_AUTOLOAD_DIRS, _HEAD_AUTOLOAD_FILES
from kew.sandbox import (
    CHECKOUT_QUOTA_BYTES,
    CHECKOUT_QUOTA_ENTRIES,
    HOST_FREE_SPACE_FLOOR_BYTES,
    TRANSCRIPT_BYTE_CAP,
    WORK_MOUNT_PATH,
    ContainerHandle,
    _argv_cidfile,
    _argv_runtime_and_name,
    build_run_argv,
    proxy_redaction_scope,
    redact_credentials,
    resource_caps,
)

_logger = logging.getLogger(__name__)

#: One bounded install; a stalled registry must not consume the review budget.
PROVISION_TIMEOUT_SECONDS = 900.0
#: The environment check imports the test runner and the project; seconds.
CHECK_TIMEOUT_SECONDS = 120.0
#: After the transcript ends, how long the runtime client may take to exit
#: before it is killed and the run is treated as unconfirmed.
CLIENT_EXIT_GRACE_SECONDS = 30.0
#: The project directory inside the checkout that carries ``uv.lock``.
PROJECT_SUBDIR = "kew"
#: What the reviewer's environment runs against, on the container side.
CONTAINER_PROJECT = f"{WORK_MOUNT_PATH}/{PROJECT_SUBDIR}"
#: The one environment uv may install into or run from, on the container
#: side: pinned so a workspace root or a uv setting in the PR cannot point
#: the install or the check at another directory.
CONTAINER_ENVIRONMENT = f"{CONTAINER_PROJECT}/.venv"
#: The only environment variable the provisioning containers receive beyond the
#: baseline their boundary gives every run of this kind: the sandbox's own
#: agent marker and the image's own names on docker, and the exec allowlist
#: :data:`kew.openshell.SANDBOX_EXEC_ENV_NAMES` on the gateway — five names
#: (``PATH``, ``HOME``, the locale three) that hold no credential, forwarded
#: there by :func:`provision_exec_environment` because the gateway CLI is a
#: host program that needs them to run at all.
PROVISION_ENVIRONMENT = {"UV_PROJECT_ENVIRONMENT": CONTAINER_ENVIRONMENT}
#: The file every environment uv builds carries, and the proof that what came
#: back off the gateway IS that environment. Read as the landing marker of the
#: subtree download, which is how the host tells the CLI's two copy semantics
#: apart without guessing one (``openshell_checkout._landed_root``).
ENVIRONMENT_MARKER = "pyvenv.cfg"
#: The line the isolated interpreter prints before the project's own modules
#: import; a CPU-cap retry's first attempt (a runtime diagnostic) never
#: carries it. The environment's ``pyvenv.cfg`` and standard library are the
#: PR's dependency tree too, which is the documented limit of this check.
INTERPRETER_TAG = "kew-interpreter "
#: The uv every provisioning container runs, by absolute path: where the agent
#: image installs it (``UV_INSTALL_DIR=/usr/local/bin``) and the one binary the
#: provisioning policy governs. A bare ``uv`` is resolved through PATH, and a
#: checkout on PATH let the PR's own root ``uv`` run the install (#2637).
UV_PROGRAM = "/usr/local/bin/uv"
#: Locked install into ``kew/.venv``: no interpreter download (the image's
#: Python or nothing), wheels copied so the venv does not point into the
#: container's ephemeral HOME cache.
UV_SYNC_COMMAND = (
    "sync",
    "--locked",
    "--all-groups",
    "--no-managed-python",
    "--no-python-downloads",
    "--link-mode",
    "copy",
    "--project",
    CONTAINER_PROJECT,
)
#: The check the reviewer's own commands rely on. Started isolated and without
#: site initialisation (``-I -S``), the interpreter prints its tagged path
#: before ``.pth`` files, ``sitecustomize`` or any project import run; then
#: site initialises the environment and the runner and the project import
#: offline.
UV_CHECK_COMMAND = (
    "run",
    "--offline",
    "--no-sync",
    "--no-python-downloads",
    "--project",
    CONTAINER_PROJECT,
    "python",
    "-I",
    "-S",
    "-c",
    f"import sys; print({INTERPRETER_TAG!r} + sys.executable, flush=True); "
    "import site; site.main(); import pytest, kew",
)
#: Never copied: the git store, any environment, the review's own files, and
#: every runner-loaded surface the reviewer sees masked (the review's own
#: list, so the two can never drift apart).
_COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    ".kew-*",
    *sorted(_HEAD_AUTOLOAD_DIRS),
    *sorted(_HEAD_AUTOLOAD_FILES),
)
_COPY_SKIP_DIRS = frozenset({".git", ".venv"})
_COPY_CHUNK_BYTES = 1024 * 1024
#: Charged per copied file on top of its bytes: the allocation a small file
#: occupies whatever its length.
_FILE_ALLOCATION_OVERHEAD_BYTES = 4096
_TRANSCRIPT_TAIL_LINES = 40
_CONTAINER_PROBE_TIMEOUT_SECONDS = 10.0
_CONTAINER_REMOVE_TIMEOUT_SECONDS = 30.0


class ProvisioningUnconfirmed(RuntimeError):
    """The review must not start: a launch the client did not see through, a
    container not proven gone, or a checkout that cannot be made truthful.

    *timed_out* marks a runtime call the daemon did not answer in time: a
    loaded host, not a fault anyone must fix, so the review stage retries the
    launch on a later tick before it holds the node (#3246).
    """

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class _CopyQuotaExceeded(RuntimeError):
    """The copy passed the sandbox quota or the free-space floor, or met a
    source it must not read (not an OSError: copytree collects those and keeps
    going; this stops it)."""


class _LaunchState:
    """Whether a provisioning container may exist that nobody has confirmed gone.

    Set before the runtime client starts and cleared only after the daemon
    confirmed the container gone and the client's exit carried the creation
    record. While set, the provisioner retains the disposable copy (a
    container may still hold it) and fails closed (the reviewer must not start
    beside it), whatever exception, or none, brought it to its exit.
    """

    uncertain: bool = False


def _describe(exc: BaseException) -> str:
    """The exception's type and text, or its type alone when text fails."""
    try:
        return redact_credentials(f"{type(exc).__name__}: {exc}")
    except BaseException:  # noqa: BLE001 — a str() that raises must not matter
        return type(exc).__name__


class Transcript(NamedTuple):
    """What one provisioning container produced."""

    exit_code: int
    #: The interpreter path from the first tagged line, empty when none.
    interpreter: str
    tail: list[str]
    #: The host cap that ENDED this run, empty when none did. Docker reports
    #: its caps through the client's exit code, because the handle kills the
    #: container and the runtime then exits non-zero; an OpenShell exec is a
    #: plain pipe whose client can have exited cleanly as the cap fired, so
    #: the breach is carried here rather than inferred from a number.
    capped: str = ""


def provision_review_environment(
    config: DispatchConfig,
    *,
    image: str,
    runner: str,
    checkout_dir: Path,
    slug: str,
    timeout_seconds: float = PROVISION_TIMEOUT_SECONDS,
) -> bool:
    """Install the checkout's locked dependencies into ``kew/.venv`` in *image*.

    Returns whether a validated ``kew/.venv`` now exists in the checkout. Every
    failure is reported with its reason, leaves no ``.venv`` behind, and
    returns False, except the cases :class:`ProvisioningUnconfirmed` names:
    those, and any exit at all while a launch is still uncertain, raise.
    """
    checkout = Path(checkout_dir)
    project = checkout / PROJECT_SUBDIR
    lock = project / "uv.lock"
    refusal, allocated = _trusted_side_refusal(checkout, project, lock)
    scratch_root: Path | None = None
    launch = _LaunchState()
    failure: BaseException | None = None
    try:
        if refusal is None:
            scratch_root = Path(
                tempfile.mkdtemp(prefix=f"kew-review-env-{slug}-", dir=checkout.parent)
            )
            scratch = scratch_root / "work"
            shutil.copytree(
                checkout,
                scratch,
                symlinks=True,
                ignore=_COPY_IGNORE,
                copy_function=_BoundedCopier(CHECKOUT_QUOTA_BYTES, charged=allocated),
            )
            refusal = _install(
                config, image, runner, scratch, slug, timeout_seconds, launch
            )
        if refusal is None:
            refusal = _promote(scratch_root, project)
    except ProvisioningUnconfirmed:
        raise
    except Exception as exc:  # noqa: BLE001 — classified below by the launch state
        failure = exc
    finally:
        if scratch_root is not None:
            _dispose_scratch(scratch_root, containers_gone=not launch.uncertain)
    if launch.uncertain:
        # Whatever happened, a container may exist that nobody confirmed gone.
        raise ProvisioningUnconfirmed(
            "a provisioning container's launch was left uncertain"
            + (f" ({_describe(failure)})" if failure is not None else "")
        ) from failure
    if failure is not None:
        refusal = f"provisioning did not complete ({_describe(failure)})"
    if refusal is not None:
        _warn(
            redact_credentials(
                f"Review environment: {refusal}; the reviewer runs without an "
                "installed environment."
            )
        )
        _drop_planted_environments(checkout, project, keep_project=False)
        return False
    _drop_planted_environments(checkout, project, keep_project=True)
    _logger.info("Review environment: installed %s into %s", lock, project / ".venv")
    return True


def _install(
    config: DispatchConfig,
    image: str,
    runner: str,
    scratch: Path,
    slug: str,
    timeout_seconds: float,
    launch: _LaunchState,
) -> str | None:
    """Sync and check the environment in the copy; the refusal reason, else None."""
    if is_openshell(config.sandbox):
        return _install_openshell(
            config, image, runner, scratch, slug, timeout_seconds, launch
        )
    sync = _run_bounded(
        _argv(config, image, runner, scratch, f"{slug}-env", UV_SYNC_COMMAND),
        scratch,
        timeout_seconds,
        label="uv sync",
        launch=launch,
        proxy_environment=config.sandbox.proxy_environment,
    )
    refusal = _sync_refusal(sync)
    if refusal is not None:
        return refusal
    if not (scratch / PROJECT_SUBDIR / ".venv").is_dir():
        return "uv sync succeeded but kew/.venv is absent"
    check = _run_bounded(
        _argv(config, image, runner, scratch, f"{slug}-env-check", UV_CHECK_COMMAND),
        scratch,
        CHECK_TIMEOUT_SECONDS,
        label="environment check",
        launch=launch,
        proxy_environment=config.sandbox.proxy_environment,
    )
    return _check_refusal(check)


def _sync_refusal(sync: Transcript) -> str | None:
    """Why the locked install did not complete, else None."""
    if sync.capped:
        return f"the locked install was ended by a host cap ({sync.capped})"
    if sync.exit_code != 0:
        return f"uv sync exited {sync.exit_code}"
    return None


def _check_refusal(check: Transcript) -> str | None:
    """Why the built environment is not usable, else None.

    Shared by both container boundaries, so the reviewer is told the same thing
    about the same observation whichever runtime provisioned it.
    """
    if check.capped:
        return f"the environment check was ended by a host cap ({check.capped})"
    if check.exit_code != 0:
        return (
            f"the environment check exited {check.exit_code} (the runner or the "
            "project does not import offline)"
        )
    # The tagged line is printed by the isolated interpreter before the
    # project's modules import; anything else in the transcript proves nothing.
    interpreter = os.path.normpath(check.interpreter) if check.interpreter else ""
    if not interpreter.startswith(CONTAINER_ENVIRONMENT + "/"):
        return (
            f"the environment check ran {check.interpreter!r}, not an "
            f"interpreter under {CONTAINER_ENVIRONMENT}"
        )
    return None


# ── The OpenShell boundary (epic node N12) ───────────────────────────────────
# Same install, same copy, same caps — a different container boundary, and one
# property the docker path cannot have: the provisioning sandbox is created with
# NO OpenShell provider, so there is no credential in it at all, not even the
# placeholder a dispatch carries. It runs under its own egress policy
# (docker/openshell/policy-review-provision-claude-code.yaml), which permits the
# package index and nothing else, and governs uv alone — so the PR's own build
# backends, which uv executes, reach no host whatever.
#
# The sandbox is never shared with the review. The reviewer gets its own, under
# its own policy, after this one is deleted.

#: Gateway name for one provisioning sandbox. A namespace of its own, so an
#: operator reading the gateway can tell a provisioning sandbox from the
#: reviewer's ``kew-<slug>`` without reading kew's ledger.
PROVISION_SANDBOX_PREFIX = "kew-review-env-"


def _provision_sandbox_name(slug: str) -> str:
    return f"{PROVISION_SANDBOX_PREFIX}{slug}"


def _provision_labels() -> dict[str, str]:
    """The flow label on it, spelled from the same constants the launch uses.

    Not a literal: an operator reading the gateway tells a provisioning sandbox
    from a reviewer's by this label, and a second spelling of either half is how
    it would come to name a flow kew no longer has.
    """
    from kew.openshell_launch import FLOW_LABEL

    return {FLOW_LABEL: PROVISION_FLOW}


def _install_openshell(
    config: DispatchConfig,
    image: str,
    runner: str,
    scratch: Path,
    slug: str,
    timeout_seconds: float,
    launch: _LaunchState,
    client: object | None = None,
) -> str | None:
    """Sync and check the environment inside its own OpenShell sandbox.

    The gateway owns create / upload / exec / download / delete, so the host
    mounts nothing: the copy is uploaded, uv runs against it inside the sandbox,
    and the environment it built is downloaded back beside the copy through the
    N7 realization caps. Only then does ``_promote`` move it into the reviewed
    checkout, exactly as it does on docker — which is how the reviewer's own
    sandbox ends up with the ``.venv`` without the two ever sharing one.

    ``launch.uncertain`` is the same contract as on docker: set before the
    gateway is asked for anything and cleared only once the sandbox is CONFIRMED
    deleted. A delete whose outcome the gateway could not report leaves it set,
    so the copy is retained and the review does not start beside a sandbox that
    may still hold it.
    """
    from kew.openshell import OpenShellClient
    from kew.openshell_launch import CHECKOUT_PATH, runner_policy_path

    gateway = client or OpenShellClient.from_settings(config.sandbox.openshell)
    name = _provision_sandbox_name(slug)
    policy = runner_policy_path(runner, flow=PROVISION_FLOW)
    launch.uncertain = True
    try:
        gateway.sandbox_create(  # type: ignore[attr-defined]
            name,
            image=image,
            # No provider, deliberately and not by omission: nothing in this
            # sandbox authenticates to anything, so there is no credential for
            # the gateway to broker into it.
            providers=(),
            policy_path=policy,
            labels=_provision_labels(),
        )
        gateway.sandbox_upload(name, scratch, CHECKOUT_PATH)  # type: ignore[attr-defined]
        refusal = _openshell_install(gateway, name, timeout_seconds)
        if refusal is None:
            refusal = _openshell_environment_back(gateway, name, scratch)
    finally:
        launch.uncertain = not _release_provision_sandbox(gateway, name)
    return refusal


def _openshell_install(
    gateway: object, name: str, timeout_seconds: float
) -> str | None:
    """Run the locked install and the environment check in the sandbox."""
    sync = _openshell_exec(gateway, name, UV_SYNC_COMMAND, timeout_seconds, "uv sync")
    refusal = _sync_refusal(sync)
    if refusal is not None:
        return refusal
    return _check_refusal(
        _openshell_exec(
            gateway,
            name,
            UV_CHECK_COMMAND,
            CHECK_TIMEOUT_SECONDS,
            "environment check",
        )
    )


def provision_exec_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """What the provisioning exec's client is launched with: baseline + the pin.

    The gateway CLI is a host program like any other: handed a dict, the exec
    chokepoint (``kew.openshell.sandbox_exec_environment``) intersects it with
    the admitted names, so a caller that passes the pin ALONE launches the
    client with neither ``PATH`` nor ``HOME`` — the two names every other
    ``sandbox_exec`` call site forwards and that
    ``SANDBOX_EXEC_ENV_NAMES`` documents as required. The baseline is
    therefore taken from the host environment through that same chokepoint,
    and the pin is added to it: still no credential (the intersection admits
    no name that could hold one), still the only variable this exec receives
    beyond the baseline.
    """
    from kew.openshell import sandbox_exec_environment

    return {**sandbox_exec_environment(environment), **PROVISION_ENVIRONMENT}


class _ExecCap:
    """How a provisioning exec is ended by a host cap, and whether it was.

    A cap breach has to end the WORKLOAD and not just the host's reading of
    it: the docker path kills the container on the same breach, while an
    ``openshell sandbox exec`` left running keeps producing output that
    ``OpenShellExec.wait`` then takes off the pipe into one host string. So
    the client is killed (the host's own reader and buffer) and the sandbox is
    deleted, which is how a workload ends on this runtime.

    The reason is retained because the exit code cannot be relied on to carry
    it — a killed client's code is a signal, and a workload that finished as
    the cap fired could still exit 0 — and the reviewer must not start on an
    environment built by a run the host cut off.
    """

    def __init__(self, gateway: object, name: str, handle: object) -> None:
        self._gateway = gateway
        self._name = name
        self._handle = handle
        self.reason = ""

    def __call__(self, reason: str) -> None:
        self.reason = reason
        process = getattr(self._handle, "process", None)
        if process is not None:
            with contextlib.suppress(Exception):
                process.kill()
        _release_provision_sandbox(self._gateway, self._name)


def _openshell_exec(
    gateway: object,
    name: str,
    command: tuple[str, ...],
    timeout_seconds: float,
    label: str,
) -> Transcript:
    """One bounded ``uv`` exec in the provisioning sandbox.

    ``retain`` carries the environment pin across the exec's name allowlist
    (``kew.openshell.sandbox_exec_environment``). It is not a credential and
    nothing here has one — the pin is the ONLY variable this exec receives
    beyond the baseline, and without it uv would install into whatever
    directory the PR's own workspace settings named.

    The read is :func:`kew.sandbox.bounded_pipe_lines`, so the wall clock and
    the transcript cap bound this exec exactly as ``ContainerHandle`` bounds
    the docker one: a build backend that stays alive and silent is cut off at
    the deadline rather than holding the review stage, and either breach ends
    the sandbox instead of leaving it to run.
    """
    from kew.openshell_launch import CHECKOUT_PATH
    from kew.sandbox import bounded_pipe_lines

    handle = gateway.sandbox_exec(  # type: ignore[attr-defined]
        name,
        workdir=CHECKOUT_PATH,
        argv=[UV_PROGRAM, *command],
        env=provision_exec_environment(os.environ),
        retain=tuple(PROVISION_ENVIRONMENT),
    )
    cap = _ExecCap(gateway, name, handle)
    interpreter, tail = _read_transcript(
        bounded_pipe_lines(
            handle.stdout,
            wall_clock_seconds=timeout_seconds,
            end_workload=cap,
            transcript_cap=TRANSCRIPT_BYTE_CAP,
        )
    )
    exit_code = handle.wait(CLIENT_EXIT_GRACE_SECONDS + timeout_seconds)
    if exit_code != 0 or cap.reason:
        _logger.warning(
            "Review environment: %s exited %s; last output: %s",
            label,
            exit_code,
            redact_credentials(" | ".join(tail))[-400:],
        )
    return Transcript(exit_code, interpreter, tail, cap.reason)


def _openshell_environment_back(
    gateway: object, name: str, scratch: Path
) -> str | None:
    """Download the built environment beside the copy; the refusal, else None.

    The destination is the realized sibling of the copy (node N7), which sits
    inside the disposable root — so the tree is removed with it and no
    provisioning download outlives the review that asked for it.

    :data:`ENVIRONMENT_MARKER` is what identifies the downloaded tree whichever
    copy semantic the gateway CLI used, since the install that produced it had
    to write one.
    """
    from kew.openshell_checkout import (
        RealizedCheckoutRefused,
        realize_sandbox_subtree,
        realized_checkout_dir,
    )
    from kew.openshell_launch import CHECKOUT_PATH

    destination = realized_checkout_dir(scratch)
    try:
        realized = realize_sandbox_subtree(
            gateway,  # type: ignore[arg-type]
            name,
            source=f"{CHECKOUT_PATH}/{PROJECT_SUBDIR}/.venv",
            destination=destination,
            marker=ENVIRONMENT_MARKER,
        )
    except RealizedCheckoutRefused as exc:
        return f"the built environment was not realized on this host ({exc})"
    target = scratch / PROJECT_SUBDIR / ".venv"
    if target.exists() or target.is_symlink():
        return f"{target} already exists in the copy; the download is not moved"
    os.replace(realized, target)
    return None


def _release_provision_sandbox(gateway: object, name: str) -> bool:
    """Delete the provisioning sandbox; whether it is CONFIRMED gone.

    An absent sandbox is the outcome being asked for, so ``not found`` is
    confirmation. Everything else — a delete that errored, a delete whose reply
    was cut off — answers False, and the caller then retains the copy and fails
    closed rather than letting the review start beside a sandbox that may still
    be writing into it.
    """
    from kew.openshell import OpenShellError, OpenShellNotFound

    try:
        gateway.sandbox_delete(name)  # type: ignore[attr-defined]
    except OpenShellNotFound:
        return True
    except OpenShellError as exc:
        _warn(
            "Review environment: the provisioning sandbox "
            f"{name} was not confirmed deleted ({exception_text(exc)})"
        )
        return False
    return True


def _promote(scratch_root: Path | None, project: Path) -> str | None:
    """Move the validated venv into the checkout; the refusal reason, else None."""
    if scratch_root is None:
        return "no environment was built"
    scratch = scratch_root / "work"
    venv = scratch / PROJECT_SUBDIR / ".venv"
    refusal = _promotion_refusal(scratch_root, scratch, venv)
    if refusal is not None:
        return f"{refusal}; the environment is not promoted"
    target = project / ".venv"
    if target.is_symlink():
        return "kew/.venv in the checkout is a symlink; the environment is not promoted"
    if target.exists():
        _remove_planted(target)
    os.rename(venv, target)
    return None


def _dispose_scratch(scratch_root: Path, *, containers_gone: bool) -> None:
    """Remove the disposable copy, or retain it when a container may hold it.

    While a provisioning container is not confirmed gone it may still have the
    copy mounted and be writing into it; removing the tree then would race a
    live writer (a directory swapped for a symlink between a failed unlink and
    its retry). The copy is retained and reported instead.
    """
    if not containers_gone:
        _warn(
            "Review environment: the disposable copy is retained at "
            f"{scratch_root}: a provisioning container is not confirmed gone"
        )
        return
    _remove_scratch(scratch_root)


def _remove_scratch(scratch_root: Path) -> None:
    """Remove the disposable copy; a retained tree is reported, never silent.

    Called only once every provisioning container is confirmed gone, so no
    concurrent writer exists. The PR's build backend can have left entries
    the host cannot unlink as they are (a directory without write permission,
    for one), so permissions are restored on the way down and the removal is
    verified.
    """

    def _restore_and_retry(func: object, path: str, exc: BaseException) -> None:
        parent = os.path.dirname(path)
        try:
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IRWXU)
            if os.path.isdir(path) and not os.path.islink(path):
                os.chmod(path, os.stat(path).st_mode | stat.S_IRWXU)
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            raise exc from None

    try:
        shutil.rmtree(scratch_root, onexc=_restore_and_retry)
    except OSError as exc:
        _warn(
            f"Review environment: the disposable copy is retained at "
            f"{scratch_root}: {_describe(exc)}"
        )
        return
    if scratch_root.exists():
        _warn(f"Review environment: the disposable copy is retained at {scratch_root}")


def _drop_planted_environments(
    checkout: Path, project: Path, *, keep_project: bool
) -> None:
    """Leave no ``.venv`` the PR planted at the checkout or project root.

    The reviewer's guidance reads ``kew/.venv`` absence as "provisioning
    failed"; a tracked or leftover environment would turn that into a false
    success and hand the reviewer PR-planted binaries as its runner. Nothing
    is removed through a symlinked ancestor (that would reach outside the
    checkout), and a checkout that cannot be made truthful stops the review.
    """
    for path in (checkout, project):
        if path.is_symlink():
            raise ProvisioningUnconfirmed(
                f"{path} is a symlink; the review checkout cannot be made truthful"
            )
    targets = [checkout / ".venv"]
    if not keep_project:
        targets.append(project / ".venv")
    for target in targets:
        _remove_planted(target)


def _remove_planted(target: Path) -> None:
    """Remove one planted entry of any kind, or stop the review.

    A directory is checked and walked first: a mount point at or inside it
    (something only the host could have placed there) is never traversed,
    since removal would reach whatever is mounted.
    """
    try:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        elif target.is_dir():
            mounted = _mount_point_at_or_inside(target)
            if mounted is not None:
                raise ProvisioningUnconfirmed(
                    f"{mounted} is a mount point at or inside {target}; the "
                    "review checkout cannot be made truthful"
                )
            shutil.rmtree(target)
    except OSError as exc:
        raise ProvisioningUnconfirmed(
            f"{target} could not be removed ({_describe(exc)}); the review checkout "
            "cannot be made truthful"
        ) from exc
    if target.exists() or target.is_symlink():
        raise ProvisioningUnconfirmed(
            f"{target} is still present; the review checkout cannot be made truthful"
        )


def _mount_point_at_or_inside(root: Path) -> Path | None:
    """*root* itself or the first directory under it that is a mount point.

    The device baseline is *root*'s parent (the checkout's own filesystem), so
    a directory mounted at *root* is found as well as one inside it. Both the
    platform's mount test and a device change count; a bind mount of the same
    filesystem that the platform does not report is the documented limit.
    """
    device = os.lstat(root.parent).st_dev
    if os.path.ismount(root) or os.lstat(root).st_dev != device:
        return root
    for current, dirs, _files in os.walk(root):
        for name in dirs:
            path = Path(current) / name
            if path.is_symlink():
                continue
            if os.path.ismount(path) or os.lstat(path).st_dev != device:
                return path
    return None


def _trusted_side_refusal(
    checkout: Path, project: Path, lock: Path
) -> tuple[str | None, int]:
    """Why the checkout cannot be provisioned at all, and the bytes the copy's
    directories and links will occupy (charged before any file is copied).

    The checkout is the PR's tree: a symlinked project directory would turn the
    later promotion into a write wherever the PR pointed it, so it is refused
    before anything runs; the copy is bounded by the sandbox's quota and the
    host's free-space floor, since it happens before any container guard.
    """
    for path in (checkout, project):
        if path.is_symlink():
            return f"{path} is a symlink", 0
    if not project.is_dir():
        return f"no project directory at {project}", 0
    if not lock.is_file():
        return f"no lockfile at {lock}", 0
    return _copy_budget_refusal(checkout)


def _allocated_size(st: os.stat_result) -> int:
    """Bytes the entry occupies on disk, never less than its length."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is None:
        return int(st.st_size)
    return max(int(st.st_size), int(blocks) * 512)


def _copy_budget_refusal(checkout: Path) -> tuple[str | None, int]:
    """Why the copy would break the sandbox quota or the free-space floor, and
    the allocation of the directories and links the copy will create itself.

    Every copied entry counts (directories and links included; the walk
    follows no link) and sizes are the allocated sizes of files, directories
    and links alike, so the bound is at least what the copy will occupy. What
    ``copytree`` creates without the copier (directories, links of any kind)
    is the structure the copier starts charged with; regular files it charges
    again as they are written.
    """
    total = 0
    structure = 0
    entries = 0
    for root, dirs, files in os.walk(checkout):
        dirs[:] = [d for d in dirs if d not in _COPY_SKIP_DIRS]
        entries += len(dirs) + len(files)
        structure += _sum_allocations(root, dirs)
        regular, links = _split_links(root, files)
        structure += links
        total += regular
        if total + structure > CHECKOUT_QUOTA_BYTES or entries > CHECKOUT_QUOTA_ENTRIES:
            return (
                "the checkout exceeds the sandbox quota "
                f"({CHECKOUT_QUOTA_BYTES} bytes or "
                f"{CHECKOUT_QUOTA_ENTRIES} entries)"
            ), structure
    free = shutil.disk_usage(checkout.parent).free
    if free - total - structure < HOST_FREE_SPACE_FLOOR_BYTES:
        return (
            f"copying {total + structure} bytes would leave the host under its "
            f"free-space floor ({HOST_FREE_SPACE_FLOOR_BYTES} bytes)"
        ), structure
    return None, structure


def _sum_allocations(root: str, names: list[str]) -> int:
    """Allocated bytes of the named entries under *root* (an entry that
    vanished mid-walk counts nothing; the copy will not find it either)."""
    total = 0
    for name in names:
        try:
            total += _allocated_size(os.lstat(os.path.join(root, name)))
        except OSError:
            continue
    return total


def _split_links(root: str, names: list[str]) -> tuple[int, int]:
    """Allocated bytes of the named non-directory entries: (files, links)."""
    files = 0
    links = 0
    for name in names:
        try:
            st = os.lstat(os.path.join(root, name))
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            links += _allocated_size(st)
        else:
            files += _allocated_size(st)
    return files, links


class _BoundedCopier:
    """A ``copytree`` copy function that charges bytes as they are written.

    The pre-copy walk is an estimate; this is the enforcement while the copy
    runs. Each regular file is streamed in chunks and every chunk is charged
    against the quota and checked against the host's free-space floor before
    it is written, so a file that grew since the walk stops the copy at the
    bound rather than after it. A source that is not a regular file (a FIFO
    would block the host before any container timeout applies) is refused
    without being opened for reading. *charged* is what the walk already
    attributed to directories and links, which ``copytree`` creates without
    this function.
    """

    def __init__(self, limit: int, *, charged: int = 0) -> None:
        self._limit = limit
        self._charged = charged

    def _charge(self, amount: int, dst_dir: str) -> None:
        self._charged += amount
        if self._charged > self._limit:
            raise _CopyQuotaExceeded(
                f"the copy exceeded the sandbox quota ({self._limit} bytes)"
            )
        if shutil.disk_usage(dst_dir).free - amount < HOST_FREE_SPACE_FLOOR_BYTES:
            raise _CopyQuotaExceeded(
                "the copy reached the host free-space floor "
                f"({HOST_FREE_SPACE_FLOOR_BYTES} bytes)"
            )

    def __call__(self, src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> str:
        dst_dir = os.path.dirname(dst)
        source = self._open_regular(src)
        # The file object owns the descriptor from here; a destination that
        # fails to open closes the source exactly once, through the context.
        with source, open(dst, "wb") as destination:
            self._charge(_FILE_ALLOCATION_OVERHEAD_BYTES, dst_dir)
            while chunk := source.read(_COPY_CHUNK_BYTES):
                self._charge(len(chunk), dst_dir)
                destination.write(chunk)
        shutil.copystat(src, dst)
        return str(dst)

    @staticmethod
    def _open_regular(src: str | os.PathLike[str]) -> BinaryIO:
        """Open a regular file for reading, owning its descriptor exactly once.

        The raw file object takes the descriptor first; the buffered reader is
        built around it afterwards, so a failure at either step closes the
        descriptor once, through whichever object owns it at that moment.
        """
        flags = (
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(src, flags)
        try:
            regular = stat.S_ISREG(os.fstat(fd).st_mode)
            if not regular:
                raise _CopyQuotaExceeded(f"{os.fspath(src)} is not a regular file")
            raw = io.FileIO(fd, "rb", closefd=True)
        except BaseException:
            os.close(fd)
            raise
        try:
            return io.BufferedReader(raw)
        except BaseException:
            raw.close()
            raise


def _promotion_refusal(scratch_root: Path, scratch: Path, venv: Path) -> str | None:
    """Why the built environment must not be promoted, else None.

    The PR's code ran in the copy, so every path component between the copy
    root and the venv is checked to be a real directory: a component replaced
    by a symlink would make the host's rename follow it.
    """
    for path in (scratch, scratch / PROJECT_SUBDIR, venv):
        if path.is_symlink():
            return f"{path} is a symlink after the install"
        if not path.is_dir():
            return f"{path} is not a directory after the install"
    expected = scratch_root.resolve() / "work" / PROJECT_SUBDIR / ".venv"
    if venv.resolve() != expected:
        return f"{venv} does not resolve inside the copy"
    return None


def _argv(
    config: DispatchConfig,
    image: str,
    runner: str,
    mount: Path,
    slug: str,
    command: tuple[str, ...],
) -> list[str]:
    return build_run_argv(
        runtime=config.sandbox.runtime,
        image=image,
        checkout_dir=mount,
        slug=slug,
        network=config.sandbox.network,
        proxy_environment=config.sandbox.proxy_environment,
        runner=runner,
        entrypoint=UV_PROGRAM,
        command=list(command),
        # The explicit mapping replaces the runner's credential allowlist (an
        # absent mapping would inject the runner's own names): no runner
        # credential reaches the install, only the environment pin.
        credential_environment=PROVISION_ENVIRONMENT,
        caps=resource_caps(config.sandbox),
    )


def _run_bounded(
    argv: list[str],
    mount: Path,
    timeout_seconds: float,
    *,
    label: str,
    proxy_environment: str = "none",
    launch: _LaunchState | None = None,
) -> Transcript:
    """Run one provisioning container under the sandbox's resource caps.

    The handle enforces the wall clock (*timeout_seconds*), the transcript
    byte cap, the mounted copy's quota, and the host free-space floor exactly
    as it does for the reviewer, and performs the guarded CPU-cap retry.

    *launch* is marked uncertain before the runtime client starts and marked
    certain again only on the normal path, after the daemon confirmed the
    container gone and the client's exit carried the creation record. Every
    other way out (a streaming error, a client killed or lingering, an exit
    without a creation record, a handle that could not be built around the
    started client, a cancellation, a failure inside the confirmation step or
    while building a message) leaves it uncertain, and the provisioner then
    retains the copy and fails closed, whatever the exception. Only a launch
    the client saw through counts: an aborted launch is unconfirmed even when
    the daemon lists no container afterwards, since a creation request still
    in flight can complete after the probe.
    """
    with proxy_redaction_scope(proxy_environment):
        launch = launch if launch is not None else _LaunchState()
        runtime, name = _argv_runtime_and_name(argv)
        launch.uncertain = True
        proc = subprocess_utils.popen(argv)
        handle: ContainerHandle | None = None
        try:
            handle = ContainerHandle(
                proc,
                name,
                runtime,
                checkout_dir=mount,
                wall_clock_seconds=timeout_seconds,
                cidfile=_argv_cidfile(argv),
                launch_argv=argv,
                cpu_retry_launcher=subprocess_utils.popen,
            )
            interpreter, tail = _stream(handle)
            exit_code = handle.wait(timeout=CLIENT_EXIT_GRACE_SECONDS)
        except Exception as exc:
            _confirm_gone(handle, proc, runtime, name)
            raise ProvisioningUnconfirmed(
                f"the launch of provisioning container {name!r} was aborted "
                f"({_describe(exc)}) before the client saw its end; a creation "
                "still in flight cannot be excluded"
            ) from exc
        except BaseException:
            _confirm_gone(handle, proc, runtime, name)
            raise
        _confirm_gone(handle, proc, runtime, name)
        _require_seen_through(handle, exit_code, name)
        launch.uncertain = False
        if exit_code != 0:
            _logger.warning(
                "Review environment: %s exited %s; last output: %s",
                label,
                exit_code,
                redact_credentials(" | ".join(tail))[-400:],
            )
        return Transcript(exit_code, interpreter, tail)


def _stream(handle: ContainerHandle) -> tuple[str, list[str]]:
    """Consume the docker handle's transcript, under its own caps."""
    return _read_transcript(handle)


def _read_transcript(lines: Iterable[str]) -> tuple[str, list[str]]:
    """Consume the transcript: the first tagged interpreter path and the tail.

    Takes any line source, because two boundaries feed it: the docker handle,
    which enforces the caps itself, and the OpenShell exec's pipe, which is
    bounded by :func:`kew.sandbox.bounded_pipe_lines`. One reader, so the
    interpreter line is recognised the same way on both and the check cannot
    be stricter on one.
    """
    interpreter = ""
    tagged = False
    tail: deque[str] = deque(maxlen=_TRANSCRIPT_TAIL_LINES)
    for line in lines:
        text = line.rstrip("\n")
        if not tagged and text.startswith(INTERPRETER_TAG):
            tagged = True
            interpreter = redact_credentials(text[len(INTERPRETER_TAG) :].strip())
        tail.append(redact_credentials(text))
    return interpreter, list(tail)


def _require_seen_through(handle: ContainerHandle, exit_code: int, name: str) -> None:
    """The client exited on its own and the daemon had acknowledged the creation."""
    if exit_code < 0 or handle.container_id is None:
        how = (
            "by signal"
            if exit_code < 0
            else "without a creation record from the daemon"
        )
        raise ProvisioningUnconfirmed(
            f"the runtime client for provisioning container {name!r} exited "
            f"{exit_code} {how}; a creation still in flight cannot be excluded"
        )


def _confirm_gone(
    handle: ContainerHandle | None, proc: object, runtime: str, name: str
) -> None:
    """Settle the client and confirm the container gone, or stay unconfirmed.

    Nothing that goes wrong here may look like an ordinary failure: an error
    while settling the client, while probing the daemon, or while writing a
    diagnostic leaves the launch uncertain, and the caller's exception (a
    cancellation included) is replaced by :class:`ProvisioningUnconfirmed`
    rather than by the incidental error. The launch state stays uncertain
    regardless, so even this classification is not what retention relies on.
    """
    try:
        if handle is None:
            _settle_started_client(proc)
            _require_container_gone(runtime, name, None)
        else:
            _settle_and_confirm(handle, runtime, name)
    except ProvisioningUnconfirmed:
        raise
    except Exception as exc:
        raise ProvisioningUnconfirmed(
            f"provisioning container {name!r} could not be confirmed gone: the "
            f"confirmation itself failed ({_describe(exc)})"
        ) from exc


def _settle_started_client(proc: object) -> None:
    """Make a runtime client that got no handle exit (kill, bounded wait)."""
    poll = getattr(proc, "poll", None)
    if callable(poll) and poll() is None:
        kill = getattr(proc, "kill", None)
        if callable(kill):
            kill()
        wait = getattr(proc, "wait", None)
        if callable(wait):
            wait(timeout=CLIENT_EXIT_GRACE_SECONDS)


def _settle_and_confirm(handle: ContainerHandle, runtime: str, name: str) -> None:
    """The client has exited and the daemon confirms the container is gone.

    Both steps must succeed; anything else, including an error while settling
    the client, keeps the run unconfirmed rather than turning it into an
    ordinary failure that would let the reviewer launch.
    """
    try:
        settled = handle.settle_client(CLIENT_EXIT_GRACE_SECONDS)
    except Exception as exc:
        raise ProvisioningUnconfirmed(
            f"the runtime client for provisioning container {name!r} could "
            f"not be settled ({_describe(exc)}); the container cannot be confirmed gone"
        ) from exc
    if not settled:
        raise ProvisioningUnconfirmed(
            f"the runtime client for provisioning container {name!r} is "
            "still running; the container cannot be confirmed gone"
        )
    _require_container_gone(runtime, name, handle.container_id)


def _container_is_absent(runtime: str, name: str) -> tuple[bool | None, bool]:
    """Positive daemon evidence: True absent, False present, None unanswered;
    and whether that non-answer was the daemon timing out.

    The name filter is a substring match on both Docker and Podman, and the
    listed names carry a leading slash on Docker only, so the answer is the
    exact name comparison on the listed names, not the filter.
    """
    try:
        result = subprocess_utils.run(
            [
                Path(runtime).name,
                "ps",
                "-a",
                "--filter",
                f"name={name}",
                "--format",
                "{{.Names}}",
            ],
            check=False,
            timeout=_CONTAINER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn(
            "Review environment: the daemon did not answer the absence probe "
            f"for {name}: {_describe(exc)}"
        )
        return None, isinstance(exc, subprocess.TimeoutExpired)
    stdout = getattr(result, "stdout", None)
    if getattr(result, "returncode", 1) != 0 or not isinstance(stdout, str):
        _warn(
            f"Review environment: the absence probe for {name} exited "
            f"{getattr(result, 'returncode', None)}; absence is not confirmed"
        )
        return None, False
    listed = {
        part.strip().lstrip("/")
        for line in stdout.splitlines()
        for part in line.split(",")
        if part.strip()
    }
    return name not in listed, False


def _require_container_gone(runtime: str, name: str, container_id: str | None) -> None:
    """After a bounded run the daemon must confirm the container is gone.

    A present container is removed once, by the id its launch recorded, and
    probed again; an unanswered probe (daemon down, timeout) is not absence.
    Only a launch that recorded no id (its client died before the cidfile)
    removes by the name: a fixed per-item provisioning name that the item's
    next provisioning must find free. A container under the name that is not
    the recorded one is kept, and absence stays unconfirmed (#1963).
    """
    absent, timed_out = _container_is_absent(runtime, name)
    if absent is False:
        try:
            subprocess_utils.run(
                [Path(runtime).name, "rm", "-f", container_id or name],
                check=False,
                timeout=_CONTAINER_REMOVE_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ProvisioningUnconfirmed(
                f"provisioning container {name!r} could not be removed "
                f"({_describe(exc)})",
                timed_out=isinstance(exc, subprocess.TimeoutExpired),
            ) from exc
        absent, timed_out = _container_is_absent(runtime, name)
    if absent is True:
        return
    raise ProvisioningUnconfirmed(
        f"provisioning container {name!r} could not be confirmed gone"
        + (" (the daemon did not answer)" if absent is None else ""),
        timed_out=timed_out,
    )
