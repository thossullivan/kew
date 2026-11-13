"""Subprocess utilities with executable allowlist.

All subprocess calls in kew go through this module.
Executables are resolved to absolute paths at preflight time.
Only allowlisted tools can be invoked.

RULE: never CALL subprocess directly from other kew modules — add the tool
to the allowlist here and call run() / popen() / run_with_backoff().
Importing `subprocess` elsewhere for types/exceptions is fine.

Three modules are exempt, and the exemption list is asserted — not just
described — by ``test_no_module_bypasses_the_subprocess_chokepoint``:

* ``process_groups.py`` — fixed read-only ps probes shared with the store.
  It cannot launch git or agents and must not import runner inspection code.
* ``stop_recovery.py`` — fixed runtime inspections and GitHub issue labels.
  Store recovery cannot import runner inspection code. It cannot launch git.
* ``runners/lemonade_agent.py`` — the bash tool runs a model-supplied shell
  command by design; an executable allowlist would defeat the tool rather than
  secure it (justification in situ).

The completeness of this chokepoint is load-bearing beyond style: the W1.1
host/agent git boundary detector observes *this module* to see every git
process kew launches (``tests/support/host_git_boundary.py``). A new bypass is
a blind spot in that assertion.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kew.credential_environment import child_environment

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.credential_issuer import BoundedRunKey


def exclusive_owner_pid() -> int | None:
    """Return our group only when this process owns it exclusively at launch.

    Detached dispatch children already start a new session. Library callers
    may share a shell group, which must never become a fleet kill target.
    """
    pid = os.getpid()
    return pid if os.getpgrp() == pid else None


# Populated at preflight time with absolute paths.
TOOL_PATHS: dict[str, str] = {}

_REQUIRED_TOOLS = {
    "gh": "https://cli.github.com",
    "git": "https://git-scm.com",
}

_OPTIONAL_TOOLS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
    "opencode": "https://opencode.ai",
    "pi": "npm install -g @earendil-works/pi-coding-agent",
    "hermes": "hermes install.sh (OpenRouter-only headless agent)",
    "docker": "https://docs.docker.com/get-docker/",
    "podman": "https://podman.io/docs/installation",
    "openshell": "https://github.com/NVIDIA/openshell",
    "gcloud": "https://cloud.google.com/sdk/docs/install",
    "kew-lemonade": "pip install -e . (kew provides kew-lemonade)",
}

# Configured-runner preflight: the CLI each runner shells out to + how to install
# it. Used by inspect_runner (health) and cli._preflight_runner (dispatch gate).
RUNNER_REQUIREMENTS: dict[str, tuple[str, str]] = {
    "claude-code": ("claude", "npm install -g @anthropic-ai/claude-code"),
    "codex": ("codex", "npm install -g @openai/codex"),
    "opencode": ("opencode", "https://opencode.ai"),
    "pi": ("pi", "npm install -g @earendil-works/pi-coding-agent"),
    "hermes": ("hermes", "hermes install.sh (OpenRouter-only headless agent)"),
    # kew ships the kew-lemonade CLI as a console script (pip install -e .).
    "lemonade": ("kew-lemonade", "pip install -e . (kew provides kew-lemonade)"),
}

# Credentials that let a child ACT on the operator's tracker, forge, or
# published artifacts, as opposed to merely talking to an inference provider.
# The readiness probe (#1516) proves ONE inference boundary and nothing else,
# so it is launched with every one of these removed BY NAME: a bounded
# "reply OK" call has no business holding a push token, and a probe that could
# publish would be a customer-visible write dressed up as a diagnostic.
PUBLICATION_CREDENTIAL_ENVS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "KEW_DETACHED_PUBLICATION_TOKEN",
    "LINEAR_API_KEY",
    "GIT_ASKPASS",
    "GIT_SSH_COMMAND",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
)


def strip_publication_credentials(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A copy of *env* (default ``os.environ``) with customer access removed.

    Names only — no value is ever read, so nothing can leak into a message, a
    run row, or the audit ledger. Inference credentials are deliberately left
    in place: they are the boundary under test.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return {
        key: value
        for key, value in source.items()
        if key not in PUBLICATION_CREDENTIAL_ENVS
    }


#: Claude Code auth paths that a container can NEVER inherit, so they are not in
#: the runner's ``required_credentials`` — but they do authenticate a HOST run,
#: which is what a reachability check is about.
#:
#: ``ANTHROPIC_API_KEY`` is deliberately absent: the runner declares it, and this
#: tuple exists only to add what the declaration cannot carry. Keeping it in both
#: places is how the two lists drift.
CLAUDE_HOST_ONLY_AUTH_ENVS: tuple[str, ...] = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


#: Every env var that satisfies Claude Code auth on this host (#2298).
#:
#: Composed from the runner's OWN declaration plus the host-only paths above,
#: rather than written out: this tuple used to be a hand-kept list that had
#: ``ANTHROPIC_API_KEY`` and not ``CLAUDE_CODE_OAUTH_TOKEN``, so every operator
#: on the subscription path — the common one, minted by ``claude setup-token`` —
#: was reported as having no credential, and ``kew health`` warned about a
#: working install. The declaration is the runner's to make; this just adds what
#: a container could not inherit anyway.
def _claude_auth_envs() -> tuple[str, ...]:
    from kew.runners.registry import runner_required_credentials

    declared = runner_required_credentials("claude-code")
    return tuple(dict.fromkeys(declared + CLAUDE_HOST_ONLY_AUTH_ENVS))


@dataclass(frozen=True)
class RunnerReachability:
    """Whether the configured runner can actually be invoked right now."""

    runner_name: str
    binary: str | None  # None when the runner has no known CLI requirement
    binary_present: bool
    credential_detected: bool | None  # None = N/A (non-claude runner)
    version: str | None = None
    version_supported: bool | None = None


@dataclass(frozen=True)
class PinnedExecutable:
    """An absolute executable path bound to its approved SHA-256 bytes."""

    path: Path
    sha256: str


def _credential_launch_environment(
    env: Mapping[str, str] | None,
    credential: BoundedRunKey | None,
    *,
    issued_key_handoff: bool,
    retain_credentials: Iterable[str] = (),
) -> dict[str, str]:
    """Inject only an explicitly issued credential at an execution boundary."""
    from kew.run_credentials import ISSUED_KEY_HASH_ENV, launch_environment

    source = os.environ if env is None else env
    result = child_environment(source, retain=retain_credentials)
    result = launch_environment(result, credential)
    if issued_key_handoff and credential is not None:
        result[ISSUED_KEY_HASH_ENV] = credential.key_hash
    return result


def _probe_claude_cli_auth() -> bool:
    """Check whether the Claude CLI has a stored login (OAuth / CLI auth)."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        return False
    try:
        result = subprocess.run(  # nosec B603 — read-only probe of known CLI tool
            # --json explicit: don't rely on the CLI's default output format
            # (a future default flip to text would break json.loads → false WARN)
            [claude_path, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_bounded_timeout([claude_path, "auth", "status"], 5),
            env=child_environment(),
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return False


def _probe_codex(binary_path: str) -> tuple[str | None, bool, bool]:
    """Return sanitized version and subscription-login health."""
    from kew.runners.codex import (
        MIN_CODEX_VERSION,
        codex_process_env,
        parse_codex_version,
    )

    env = child_environment(codex_process_env())
    parsed: tuple[int, int, int] | None = None
    try:
        version_result = subprocess.run(  # nosec B603 — known absolute CLI
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_bounded_timeout([binary_path, "--version"], 5),
            env=env,
        )
        if version_result.returncode != 0:
            return None, False, False
        parsed = parse_codex_version(version_result.stdout)
        if parsed is None:
            return None, False, False
    except (subprocess.TimeoutExpired, OSError):
        return None, False, False
    try:
        login_result = subprocess.run(  # nosec B603 — known absolute CLI
            [binary_path, "login", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_bounded_timeout([binary_path, "login", "status"], 5),
            env=env,
        )
        logged_in = login_result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        logged_in = False
    version = ".".join(str(part) for part in parsed) if parsed is not None else None
    return version, parsed is not None and parsed >= MIN_CODEX_VERSION, logged_in


@dataclass(frozen=True)
class RunnerExecutable:
    """The native executable one host attempt is about to launch.

    ``version`` and ``sha256`` are independently optional: a runner CLI that
    will not report a version is still identified by its bytes, and a binary
    kew cannot read (a wrapper on a noexec mount, a symlink it refuses to
    follow) is still identified by its path. Every field is "what was
    resolved", never a default — an unresolvable field stays ``None`` so the
    profile can record an explicit unknown instead of a guess.
    """

    path: str
    version: str | None = None
    sha256: str | None = None


_VERSION_TOKEN = re.compile(r"\b\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?\b")


def _probe_runner_version(binary_path: str) -> str | None:
    """First version-shaped token of ``<binary> --version``, or ``None``.

    Sanitized rather than stored raw: the value is persisted as launch
    evidence, and runner CLIs print banners, ANSI, and update nags around the
    number. Any probe failure is an unknown version, never a dispatch failure.
    """
    try:
        result = subprocess.run(  # nosec B603 — resolved absolute CLI, fixed argv
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_bounded_timeout([binary_path, "--version"], 5),
            env=child_environment(),
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_TOKEN.search(result.stdout or "")
    return match.group(0) if match else None


def observe_runner_executable(
    runner_name: str, *, probe_version: bool = True
) -> RunnerExecutable | None:
    """Resolve the executable a host launch of ``runner_name`` would exec.

    Returns ``None`` when the runner declares no CLI requirement or its binary
    is not on ``PATH`` — both are honestly "no executable identity", not a
    reason to refuse. Read at the launch boundary rather than at config time so
    a binary swapped between two attempts of the same run is visible as two
    different digests.

    Deliberately uncached: hashing a large runner bundle costs ~0.2s, which is
    noise against an agent run, and a digest cached across attempts would stop
    being evidence of what THIS attempt launched — the one thing it is for.
    """
    requirement = RUNNER_REQUIREMENTS.get(runner_name)
    if requirement is None:
        return None
    # The launch resolves argv[0] through the TOOL_PATHS allowlist
    # (``resolve_cmd``), so the observation must read the SAME entry — a fresh
    # ``shutil.which`` can describe different bytes than the child execs when
    # PATH or a CLI symlink moved after preflight. Absent from the allowlist
    # means the launch itself would refuse, so there is nothing to observe.
    found = TOOL_PATHS.get(requirement[0])
    if found is None:
        return None
    # Hash what actually executes: npm-style CLIs install a symlink into
    # PATH, and ``_executable_sha256`` refuses to follow one, so hashing the
    # PATH entry itself would report "unknown" for the common case.
    binary_path = Path(found).resolve()
    try:
        digest: str | None = _executable_sha256(binary_path)
    except (ValueError, OSError):
        digest = None
    version = _probe_runner_version(str(binary_path)) if probe_version else None
    return RunnerExecutable(path=str(binary_path), version=version, sha256=digest)


def inspect_runner(
    config: DispatchConfig,
    complexity: str | None = None,
    *,
    runner_name: str | None = None,
) -> RunnerReachability:
    """Inspect a routing tier's runner CLI + credential. May raise ValueError if
    the route can't resolve; callers catch it."""
    from kew.runners.registry import resolve_runner

    selected_name = runner_name or resolve_runner(config, complexity).runner.name
    requirement = RUNNER_REQUIREMENTS.get(selected_name)
    binary = requirement[0] if requirement is not None else None
    binary_path = shutil.which(binary) if binary is not None else None
    binary_present = binary is None or binary_path is not None
    credential_detected: bool | None = None
    version: str | None = None
    version_supported: bool | None = None
    if selected_name == "claude-code":
        credential_detected = any(os.environ.get(v) for v in _claude_auth_envs())
        if not credential_detected:
            credential_detected = _probe_claude_cli_auth()
    elif selected_name == "codex" and binary_path is not None:
        version, version_supported, credential_detected = _probe_codex(binary_path)
    return RunnerReachability(
        runner_name=selected_name,
        binary=binary,
        binary_present=binary_present,
        credential_detected=credential_detected,
        version=version,
        version_supported=version_supported,
    )


def inspect_runners(config: DispatchConfig) -> list[RunnerReachability]:
    """Reachability for every distinct runner any lane resolves to.

    "Any lane" includes the ``[review]`` lane (#2298). It has its own runner pin,
    independent of node routing, so a reachability reading taken over the routing
    tiers alone said nothing about the stage that holds every pull request — a
    reviewer runner missing from PATH read as a healthy control plane right up to
    the first review round.
    """
    tiers: list[str | None] = [None]
    if config.routing is not None:
        tiers += ["simple", "medium", "complex"]
    seen: dict[str, RunnerReachability] = {}
    for tier in tiers:
        try:
            reach = inspect_runner(config, tier)
        except ValueError:
            continue
        seen.setdefault(reach.runner_name, reach)
    review_runner = config.review.runner or "claude-code"
    if review_runner not in seen:
        with contextlib.suppress(ValueError):
            seen[review_runner] = inspect_runner(config, runner_name=review_runner)
    return list(seen.values())


def resolve_cmd(cmd: list[str]) -> list[str]:
    """Replace executable name with its resolved full path."""
    exe = cmd[0]
    if exe not in TOOL_PATHS:
        raise ValueError(
            f"Unknown executable {exe!r} — not in allowlist: {sorted(TOOL_PATHS)}"
        )
    return [TOOL_PATHS[exe], *cmd[1:]]


def _executable_sha256(path: Path) -> str:
    """Hash one non-symlink regular executable through a no-follow descriptor."""
    if not path.is_absolute():
        raise ValueError("Configured executable path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("Configured executable is unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Configured executable must be a regular file")
        if metadata.st_mode & 0o111 == 0:
            raise ValueError("Configured executable is not executable")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as executable:
            for chunk in iter(lambda: executable.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def pin_executable(path: Path, expected_sha256: str) -> PinnedExecutable:
    """Validate an operator-resolved executable without consulting ``PATH``."""
    normalized_digest = expected_sha256.lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized_digest) is None:
        raise ValueError("Configured executable digest must be SHA-256")
    observed_digest = _executable_sha256(path)
    if not hmac.compare_digest(observed_digest, normalized_digest):
        raise ValueError("Configured executable digest mismatch")
    return PinnedExecutable(path=path, sha256=normalized_digest)


def _pinned_cmd(pinned: PinnedExecutable, args: list[str]) -> list[str]:
    """Revalidate pinned bytes immediately before each execution boundary."""
    verified = pin_executable(pinned.path, pinned.sha256)
    return [str(verified.path), *args]


def popen_pinned(
    pinned: PinnedExecutable,
    args: list[str],
    *,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.STDOUT,
    text: bool = True,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
    retain_credentials: Iterable[str] = (),
) -> subprocess.Popen[str]:
    """Launch digest-bound absolute bytes outside the named-tool allowlist."""
    return subprocess.Popen(  # nosec B603 — absolute executable revalidated above
        _pinned_cmd(pinned, args),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=cwd,
        env=child_environment(env, retain=retain_credentials),
        start_new_session=start_new_session,
    )


#: The monotonic instant every :func:`run` in this context must finish by, or
#: ``None``. Set by :func:`subprocess_deadline`.
_DEADLINE: ContextVar[float | None] = ContextVar("subprocess_deadline", default=None)


@contextlib.contextmanager
def subprocess_deadline(seconds: float) -> Iterator[None]:
    """Bound every :func:`run` in this context by one overall deadline.

    A call's own ``timeout`` still applies when it is shorter; a call that
    names none is capped at the time left, and one started after the deadline
    raises :class:`subprocess.TimeoutExpired` without launching. A nested
    deadline never extends an outer one. The runner probes that call
    ``subprocess.run`` directly (Claude auth, Codex version and login, the
    generic ``--version``) are capped the same way. Used by read-only surfaces
    such as ``kew doctor``'s ``run_checks``, which must return in bounded time
    however many probes it reaches.
    """
    outer = _DEADLINE.get()
    deadline = time.monotonic() + seconds
    token = _DEADLINE.set(deadline if outer is None else min(outer, deadline))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _bounded_timeout(cmd: list[str], timeout: float | None) -> float | None:
    """``timeout`` capped by the active :func:`subprocess_deadline`, if any."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = _remaining_deadline(cmd, deadline)
    return remaining if timeout is None else min(timeout, remaining)


def run_pinned(
    pinned: PinnedExecutable,
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    retain_credentials: Iterable[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run digest-bound absolute bytes after checking for path replacement."""
    return subprocess.run(  # nosec B603 — absolute executable revalidated above
        _pinned_cmd(pinned, args),
        capture_output=capture,
        text=True,
        check=check,
        cwd=cwd,
        timeout=_bounded_timeout(args, timeout),
        env=child_environment(env, retain=retain_credentials),
    )


def pin_current_executable(path: Path) -> PinnedExecutable:
    """Bind ``path`` to the bytes it holds now, for an operator-named binary
    that has no digest recorded ahead of time (``kew epic run --kew-binary``)."""
    return PinnedExecutable(path=path, sha256=_executable_sha256(path))


def run_pinned_driver_tick(pinned: PinnedExecutable, args: list[str]) -> int:
    """Run one epic-driver tick in a pinned kew and return its exit code.

    The child stands in for the tick the parent would otherwise run in its own
    process, so it inherits the parent's environment verbatim — including the
    provisioning credential the driver issues run keys with. It gains no
    authority the parent did not already hold. Output streams straight to the
    parent's terminal, and there is no timeout: a tick runs as long as it runs.
    """
    return subprocess.run(  # nosec B603 — absolute executable revalidated above
        _pinned_cmd(pinned, args), check=False
    ).returncode


def spawn_pinned_drive(pinned: PinnedExecutable, args: list[str], log: Path) -> int:
    """Start ``kew drive`` detached on a pinned kew and return its pid.

    Like :func:`run_pinned_driver_tick`, the child stands in for the drive the
    parent would otherwise run in the foreground, so it inherits the parent's
    environment verbatim and gains no authority the parent did not hold. It
    runs in its own session, reading nothing, appending its output to ``log``.
    """
    with log.open("ab") as out:
        child = subprocess.Popen(  # nosec B603 — absolute executable revalidated above
            _pinned_cmd(pinned, args),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return child.pid


def spawn_pinned_request(
    pinned: PinnedExecutable,
    args: list[str],
    log: Path,
    actor: str,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start one queued run request (#2537) on a pinned kew, detached.

    The drive runs the request in the operator's place, exactly as it runs an
    epic tick (:func:`run_pinned_driver_tick`): the child inherits the drive's
    environment verbatim and gains no authority the drive did not hold. The one
    change is ``KEW_ACTOR``, set to the operator who queued it, so the audit
    rows the command writes name that operator. It runs in its own session,
    reading nothing, its output appended to ``log``. *extra_env* is what the
    request itself attests (the source of a launchpad model, #2741), never a
    credential.
    """
    env = dict(os.environ)
    env.update(extra_env or {})
    env["KEW_ACTOR"] = actor
    with log.open("ab") as out:
        return subprocess.Popen(  # nosec B603 — absolute executable revalidated above
            _pinned_cmd(pinned, args),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )


def popen(
    cmd: list[str],
    *,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.STDOUT,
    text: bool = True,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    start_new_session: bool = False,
    credential: BoundedRunKey | None = None,
    issued_key_handoff: bool = False,
    gate: bool = False,
    retain_credentials: Iterable[str] = (),
    pass_fds: Collection[int] = (),
) -> subprocess.Popen[str]:
    """Open a subprocess via the allowlist. Used for streaming output.

    With ``start_new_session=True`` the child becomes its own process-group
    leader (pgid == pid), so the whole agent process tree can be signalled
    via ``os.killpg`` (fleet kill switch). The descriptors in ``pass_fds``
    are inherited by the child (not with ``gate``).
    """
    resolved = resolve_cmd(cmd)
    launch_env = _credential_launch_environment(
        env,
        credential,
        issued_key_handoff=issued_key_handoff,
        retain_credentials=retain_credentials,
    )
    if gate:
        return _popen_gated(
            resolved,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            cwd=cwd,
            env=launch_env,
            start_new_session=start_new_session,
        )
    inherit: dict[str, Any] = {"pass_fds": tuple(pass_fds)} if pass_fds else {}
    return subprocess.Popen(  # nosec B603 — cmd from allowlisted executables via resolve_cmd
        resolved,
        **inherit,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=cwd,
        env=launch_env,
        start_new_session=start_new_session,
    )


_SPAWN_GATE_SCRIPT = (
    'IFS= read -r permit; [ "$permit" = 1 ] || exit 125; exec < /dev/null; exec "$@"'
)


class _GatedProcess(subprocess.Popen[str]):
    """A waiting child with a launch permit owned by its parent."""

    _kew_spawn_gate_fd: int | None = None


def _popen_gated(
    resolved: list[str],
    *,
    stdin: int | None,
    stdout: int | None,
    stderr: int | None,
    text: bool,
    cwd: str | Path | None,
    env: dict[str, str] | None,
    start_new_session: bool,
) -> subprocess.Popen[str]:
    """Hold an allowlisted command behind a pipe until registration succeeds.

    Owner death closes the pipe. The waiting child then exits without exec.
    The wrapper keeps its PID and session when it executes the agent.
    """
    if not start_new_session:
        raise ValueError("A spawn gate requires an exclusive process group")
    if stdin != subprocess.DEVNULL:
        raise ValueError("A spawn gate requires DEVNULL agent stdin")
    shell_path = shutil.which("sh")
    if shell_path is None:
        raise FileNotFoundError("sh is required for the spawn gate")
    read_fd, write_fd = os.pipe()
    try:
        proc = _GatedProcess(  # fixed wrapper; resolved arguments pass through "$@"
            [shell_path, "-c", _SPAWN_GATE_SCRIPT, "kew-spawn-gate", *resolved],
            stdin=read_fd,
            stdout=stdout,
            stderr=stderr,
            text=text,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    proc._kew_spawn_gate_fd = write_fd
    return proc


def release_spawn(
    proc: subprocess.Popen[str],
    *,
    registered: bool,
    before_release: Callable[[], None] | None = None,
) -> None:
    """Release or close the launch gate exactly once."""
    if not isinstance(proc, _GatedProcess):
        return
    fd = proc._kew_spawn_gate_fd
    if fd is None:
        return
    proc._kew_spawn_gate_fd = None
    try:
        if registered:
            if before_release is not None:
                before_release()
            os.write(fd, b"1\n")
    finally:
        os.close(fd)


def popen_bytes(
    cmd: list[str],
    *,
    stdin: int | None = subprocess.PIPE,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Open an allowlisted subprocess with binary streaming pipes."""
    return subprocess.Popen(  # nosec B603 — allowlisted command
        resolve_cmd(cmd),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=False,
        cwd=cwd,
        env=child_environment(env),
    )


def run(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    retain_credentials: Iterable[str] = (),
    credential: BoundedRunKey | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, resolving the executable via the allowlist."""
    return subprocess.run(  # nosec B603 — cmd from allowlisted executables via resolve_cmd
        resolve_cmd(cmd),
        capture_output=capture,
        text=True,
        check=check,
        cwd=cwd,
        timeout=_bounded_timeout(cmd, timeout),
        env=_credential_launch_environment(
            env,
            credential,
            issued_key_handoff=False,
            retain_credentials=retain_credentials,
        ),
    )


def process_start_time(pid: int) -> str | None:
    """Read ps lstart identity; an unavailable probe supplies no evidence."""
    if "ps" not in TOOL_PATHS:
        _register_optional_tool("ps")
    try:
        result = run(["ps", "-o", "lstart=", "-p", str(pid)], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logging.getLogger(__name__).log(
            logging.DEBUG, "cannot read process %s start time: %s", pid, exc
        )
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


class BoundedOutput:
    """Keep at most limit bytes, split equally between the head and tail."""

    marker = b"\n[... output omitted ...]\n"

    def __init__(self, limit: int) -> None:
        if limit < 2:
            raise ValueError("output limit must be at least two bytes")
        self.limit = limit
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        split = min(len(chunk), self.limit // 2 - len(self.head))
        self.head.extend(chunk[:split])
        tail_limit = self.limit - self.limit // 2
        suffix = chunk[split:]
        if len(suffix) >= tail_limit:
            self.tail[:] = suffix[-tail_limit:]
        else:
            del self.tail[: max(0, len(self.tail) + len(suffix) - tail_limit)]
            self.tail.extend(suffix)

    def text(self) -> str:
        middle = self.marker if self.total > self.limit else b""
        text = (self.head + middle + self.tail).decode("utf-8", errors="replace")
        # Match subprocess.run(text=True)'s universal newline conversion.
        return text.replace("\r\n", "\n").replace("\r", "\n")


class OutputBudgetExceeded(RuntimeError):
    """Execution stopped after one stream exceeded its byte budget."""

    def __init__(self, stream: str, outputs: dict[str, BoundedOutput]) -> None:
        super().__init__(
            f"output-budget refusal: {stream} exceeded {outputs[stream].limit} bytes"
        )
        self.stdout = outputs["stdout"].text()
        self.stderr = outputs["stderr"].text()
        self.cleanup_failed = False


def _remaining_deadline(cmd: list[str], deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(cmd, 0)
    return remaining


def _read_bounded_pipes(
    proc: subprocess.Popen[bytes],
    cmd: list[str],
    deadline: float,
    outputs: dict[str, BoundedOutput],
    monitor: Callable[[], None] | None,
    poll_interval: float,
) -> None:
    with selectors.DefaultSelector() as selector:
        assert proc.stdout is not None and proc.stderr is not None
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        # Keep checking after pipe closure: a quiet child can still be writing
        # to its checkout. The callback raises with its recorded breach reason.
        while selector.get_map() or proc.poll() is None:
            remaining = _remaining_deadline(cmd, deadline)
            if monitor is not None:
                monitor()
            if monitor is not None or not selector.get_map():
                remaining = min(remaining, poll_interval)
            ready = selector.select(remaining)
            for key, _ in ready:
                output = outputs[key.data]
                # Read only enough to prove overflow, never an unbounded line.
                size = min(65536, output.limit - output.total + 1)
                chunk = os.read(key.fd, size)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.append(chunk)
                if output.total > output.limit:
                    raise OutputBudgetExceeded(key.data, outputs)


def run_bounded(
    cmd: list[str],
    *,
    deadline: float,
    output_limit: int,
    monitor: Callable[[], None] | None = None,
    poll_interval: float = 2.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bound both pipes and poll resource checks independently of output."""
    outputs = {name: BoundedOutput(output_limit) for name in ("stdout", "stderr")}
    _remaining_deadline(cmd, deadline)
    with popen_bytes(cmd, stdin=subprocess.DEVNULL, env=env) as proc:
        try:
            _read_bounded_pipes(proc, cmd, deadline, outputs, monitor, poll_interval)
            # The reader already observed process exit. Do not reject a child
            # that finished within the deadline while its closed pipes waited.
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    return subprocess.CompletedProcess(
        cmd, code, outputs["stdout"].text(), outputs["stderr"].text()
    )


def run_bytes(
    cmd: list[str],
    *,
    stdin: bytes | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a subprocess with **binary** stdin/stdout, via the same allowlist.

    Separate from :func:`run` rather than a ``text=`` flag on it, because the
    return type genuinely differs: making ``run`` return
    ``CompletedProcess[str] | CompletedProcess[bytes]`` would push a narrowing
    burden onto every existing caller to serve one new one.

    Needed wherever the payload is arbitrary bytes rather than text — piping a
    blob into ``git hash-object``, for one, where decoding to ``str`` and back
    would corrupt any content that is not valid UTF-8. Never raises on a
    non-zero exit; binary callers want the stderr bytes.
    """
    return subprocess.run(  # nosec B603 — cmd from allowlisted executables via resolve_cmd
        resolve_cmd(cmd),
        input=stdin,
        capture_output=True,
        check=False,
        cwd=cwd,
        timeout=_bounded_timeout(cmd, timeout),
        env=child_environment(env),
    )


def is_rate_limited(result: subprocess.CompletedProcess[str]) -> bool:
    """Return True if gh CLI output indicates a GitHub API rate limit error."""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    return "rate limit" in (stderr + stdout).lower()


# A GitHub 5xx status line (gh surfaces API status as "HTTP <code>"). 4xx —
# including a plain 403/404/422 — is a client error and stays fail-fast; only
# 5xx server errors are retried.
_TRANSIENT_HTTP_RE = re.compile(r"HTTP 5\d\d", re.IGNORECASE)
# Go net/transport failures gh emits on a dropped/refused connection.
_TRANSIENT_CONN_MARKERS = (
    "connection reset",
    "connection refused",
    "broken pipe",
    "i/o timeout",
    "tls handshake timeout",
    "unexpected eof",
)


def is_transient_error(result: subprocess.CompletedProcess[str]) -> bool:
    """Return True if a *failed* gh CLI result looks like a transient blip worth
    retrying: a GitHub rate limit, a 5xx status, or a reset/refused connection.

    A successful result (returncode 0) is never transient — this guards against
    a response body that happens to contain one of these markers. Client errors
    (4xx other than rate limits) return False so the caller fails fast.
    """
    if result.returncode == 0:
        return False
    if is_rate_limited(result):
        return True
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    combined = stderr + stdout
    if _TRANSIENT_HTTP_RE.search(combined):
        return True
    lowered = combined.lower()
    return any(marker in lowered for marker in _TRANSIENT_CONN_MARKERS)


def run_with_backoff(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    max_attempts: int = 3,
    backoff_base: float = 60.0,
    _sleep: Callable[[float], None] = time.sleep,
    _run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, retrying with exponential backoff on transient GitHub
    failures — rate limits, 5xx statuses, and reset/refused connections.

    On each attempt, if the result looks transient (see ``is_transient_error``),
    waits ``backoff_base * 2^attempt`` seconds before the next attempt. Client
    errors (4xx other than rate limits) are not retried and fail fast.

    ``max_attempts`` is clamped to at least 1 so the loop always executes.
    ``_run`` preserves a caller's command boundary across every retry.
    """
    max_attempts = max(1, max_attempts)
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(max_attempts):
        last = (_run or run)(
            cmd, capture=capture, check=False, cwd=cwd, timeout=timeout
        )
        if not is_transient_error(last):
            if check and last.returncode != 0:
                raise subprocess.CalledProcessError(last.returncode, cmd)
            return last
        if attempt < max_attempts - 1:
            _sleep(backoff_base * (2**attempt))
    assert last is not None
    if check and last.returncode != 0:
        raise subprocess.CalledProcessError(last.returncode, cmd)
    return last


def find_timeout_cmd() -> str | None:
    """Find timeout or gtimeout (macOS coreutils)."""
    if shutil.which("timeout"):
        return "timeout"
    if shutil.which("gtimeout"):
        return "gtimeout"
    return None


def _register_optional_tool(tool: str) -> None:
    """Add an optional tool to the allowlist when it is installed."""
    path = shutil.which(tool)
    if path:
        TOOL_PATHS[tool] = path


def ensure_tool(tool: str) -> str | None:
    """Resolve *tool* now, registering it when preflight has not run yet.

    Returns its absolute path, or ``None`` when it is not installed — which is
    the answer an optional-tool probe needs (``gcloud``, like ``docker`` and
    ``gh``, is reported by ``kew doctor`` rather than being a dependency).

    Also the call for a READ path that must degrade rather than exit (C3/#2301:
    the outcome report's ``git log origin/main`` denominator):
    ``preflight_tools`` raises ``SystemExit`` when a *required* tool is absent,
    which is right for a dispatch and wrong for a report endpoint. Never a
    relaxation: the allowlist still holds only absolute paths this process
    resolved itself.
    """
    if tool not in TOOL_PATHS:
        _register_optional_tool(tool)
    return TOOL_PATHS.get(tool)


def preflight_tools(*, required: Collection[str] | None = None) -> None:
    """Resolve required tools to absolute paths. Exits on failure.

    *required* narrows the hard requirement to the tools one command actually
    shells out to. ``kew verify`` needs ``git`` and a shell and never touches
    GitHub, and refusing to run a merge verification on a machine without the
    GitHub CLI would be refusing it over a tool the job does not use. A tool
    left out of *required* is still registered when it is installed, so a
    command that narrows the requirement can still reach one that is there.
    """
    missing = register_tools(required=required)
    for tool, hint in missing.items():
        print(f"[error] {tool} not found. Install: {hint}", file=sys.stderr)
    if missing:
        raise SystemExit(2)


def register_tools(*, required: Collection[str] | None = None) -> dict[str, str]:
    """Register every installed tool and return the missing required ones.

    The silent half of :func:`preflight_tools`: it prints nothing and never
    exits, so a library caller (``kew doctor``'s ``run_checks``) can report a
    missing tool as a result. Returns ``{tool: install hint}``.
    """
    missing: dict[str, str] = {}

    for tool, hint in _REQUIRED_TOOLS.items():
        if required is not None and tool not in required:
            _register_optional_tool(tool)
            continue
        path = shutil.which(tool)
        if path is None:
            missing[tool] = hint
        else:
            TOOL_PATHS[tool] = path

    # Optional tools (runners, timeout, etc.)
    for tool in _OPTIONAL_TOOLS:
        _register_optional_tool(tool)

    timeout_cmd = find_timeout_cmd()
    if timeout_cmd:
        _register_optional_tool(timeout_cmd)

    _register_optional_tool("curl")

    # sh runs the verification-gate command (Plan 3). The command itself is
    # arbitrary user config (pytest/make/./verify.sh) so it cannot be allowlisted
    # by name; we allowlist the shell and pass the command as `sh -c "<cmd>"`.
    _register_optional_tool("sh")

    # ps is used by `kew stop` to verify a recorded pgid still belongs
    # to a kew-launched agent before signalling (guards PID reuse).
    _register_optional_tool("ps")

    _register_optional_tool("coderabbit")

    return missing
