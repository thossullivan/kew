"""A typed client over the ``openshell`` CLI (v0.1.2).

``[sandbox] runtime = "openshell"`` names a third container boundary (epic
``openshell-runtime``). This module is the only place kew shells out to that
CLI: every later node of the epic — the runtime backend, the launch path, the
doctor probe — calls these methods rather than assembling argv of its own, so
the flag spellings below are corrected in ONE place when the CLI moves.

Three rules are structural, not stylistic, and each came out of the
2026-10-03 feasibility spike (OpenShell v0.1.2, Docker driver on OrbStack):

**No credential value is ever an argument.** ``provider_create`` takes
credential environment variable *names* and forwards their values through the
child environment, because ``--credential NAME=$(cat …)`` puts the token in the
process list where any local ``ps`` reads it. The argv carries the name only.
``tests/test_openshell_client.py`` sweeps every public call on the client and
asserts it, reading the inventory of calls off the class itself so a method
added by a later node of this epic cannot sit outside the sweep.

**Every call has a deadline.** A gateway that accepts a connection and then
says nothing would otherwise hang a dispatch with no reason recorded. Blocking
calls take a timeout and raise :class:`OpenShellTimeout`; the one streaming
call returns a handle whose ``wait`` *requires* one.

**Every failure is typed and says what ran.** :class:`OpenShellError` carries
the command (redacted), the exit code and the CLI's trimmed stderr, and the
subclasses separate the cases a caller acts on differently — the CLI is not
installed, the gateway was never reached, the call broke in flight so its
outcome is unknown, the named object does not exist. Nothing here answers a
question it could not determine: there is no "unknown" sandbox state, no clean
default on a failed probe, and no "nothing happened" on a failure that cannot
prove it — only a raise that names the cause.

Argv shapes are written against v0.1.2 as observed by the spike; the recorded
outputs under ``tests/fixtures/openshell/`` pin what kew parses, so a CLI change
fails a fixture test rather than a live dispatch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from kew import subprocess_utils
from kew.credential_environment import child_environment
from kew.credential_surface import redact_credentials

#: The allowlisted executable name. Resolved to an absolute path by
#: ``subprocess_utils.resolve_cmd``, exactly like the docker calls, so a path
#: in config can never become the thing kew launches.
CLI = "openshell"

#: Gateway selection. ``OPENSHELL_GATEWAY`` is the CLI's own variable and wins
#: over ``[sandbox.openshell] gateway``; whichever supplies the endpoint, kew
#: passes it as ``--gateway-endpoint`` so the argv in an error names the target
#: that was actually contacted.
GATEWAY_ENV = "OPENSHELL_GATEWAY"
GATEWAY_FLAG = "--gateway-endpoint"

GATEWAY_FROM_ENVIRONMENT = "environment"
GATEWAY_FROM_CONFIG = "config"
GATEWAY_FROM_CLI_DEFAULT = "cli-default"

#: Machine-readable output. ``sandbox get`` / ``provider get`` are parsed, never
#: scraped from the human table, whose columns are not a contract.
JSON_OUTPUT_FLAGS = ("-o", "json")

#: Uploads are never filtered by ``.gitignore``. The sandbox receives the
#: standalone checkout kew prepared, and a gitignore-aware copy would silently
#: drop the very files a run was told to work on.
NO_GIT_IGNORE_FLAG = "--no-git-ignore"

#: Names a credential environment variable to read in the child. The value
#: never appears on the command line (see the module docstring).
CREDENTIAL_ENV_FLAG = "--credential-env"

#: Read-only questions: version, status, logs.
PROBE_TIMEOUT_SECONDS = 10.0
#: Calls that create or delete a sandbox or provider through the gateway.
CONTROL_TIMEOUT_SECONDS = 60.0
#: Whole-checkout copies in or out of a sandbox.
TRANSFER_TIMEOUT_SECONDS = 600.0
#: How long a SIGKILLed exec is waited on, so it is reaped rather than left a
#: zombie holding its pipe. Short: the signal is already delivered by then.
KILL_REAP_TIMEOUT_SECONDS = 5.0

#: Upper bound on the stderr an error carries, so a CLI that prints a stack
#: trace cannot flood a run row or an audit payload.
MAX_STDERR_CHARS = 500

#: Sandbox statuses that mean the workload is over, in ONE place: the launch
#: lifecycle settles an uncertain delete by them, and the runtime backend maps
#: them to ``stopped`` for the recovery sweeps. Anything else — ``running``, or
#: a word this client has never seen — is NOT read as termination, because a
#: wrong "stopped" releases the reservation of a sandbox that may still hold a
#: live agent.
TERMINAL_STATUSES = frozenset({"stopped", "exited", "deleted", "terminated", "failed"})

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

#: stderr substrings that mean the dial itself failed, so not one byte of the
#: request reached the gateway and nothing on it moved. Kept distinct from a
#: refusal: an unreachable gateway is a host problem to report, while a refusal
#: is an answer about the request.
_UNREACHABLE_MARKERS = (
    "connection refused",
    "dial tcp",
    "no route to host",
    "no such host",
)

#: stderr substrings that place the break *after* the request went on the wire.
#: These say nothing about whether the gateway acted: a ``sandbox delete`` whose
#: reply is cut off has usually already deleted the sandbox, so calling it an
#: unreachable gateway would tell a caller the opposite of what happened. Tested
#: after the dial markers above, which is why a connect-phase
#: ``dial tcp …: i/o timeout`` stays an unreachable gateway.
#:
#: The connection-break spellings are Go's, because the CLI is a Go client: a
#: gateway that hangs up once the request is on the wire surfaces as an EOF, a
#: broken pipe, a closed idle connection or an HTTP/2 GOAWAY, and each of those
#: can be a mutation that already committed.
_INDETERMINATE_MARKERS = (
    "connection reset",
    "context deadline exceeded",
    "i/o timeout",
    "broken pipe",
    "server closed idle connection",
    "use of closed network connection",
    "goaway",
)

#: Go's bare ``EOF`` and ``unexpected EOF``, matched on word boundaries so a
#: sandbox name or URL that merely contains those letters is not read as a
#: severed connection.
_EOF_RE = re.compile(r"\beof\b")

#: stderr substrings that mean the gateway answered "that object is not here".
_ABSENT_MARKERS = (
    "not found",
    "no such sandbox",
    "no such provider",
    "does not exist",
    "unknown sandbox",
    "unknown provider",
)


#: The ONLY names an ``openshell sandbox exec`` forwards into a sandbox,
#: besides the credential names its caller declares as carried. v0.1.2
#: forwards the CLIENT's environment into the sandbox, so this one call's
#: environment IS the contained agent's environment — and an allowlist is the
#: only shape that can bound it. A denylist of credential NAMES handed the
#: agent every host secret kew had never heard of: a CI token, a cloud key,
#: the operator's IDE session token (PR #2401 review). The docker boundary has
#: always worked this way — :func:`kew.sandbox.build_run_argv` names every
#: ``-e`` it passes and a container inherits nothing else — so this is the
#: same rule in the gateway's spelling, not a new one.
#:
#: Each name is here because one side of the call needs it:
#:
#: * ``PATH`` — the in-sandbox launcher runs git, the agent resolves its tools,
#:   and the CLI resolves its own helpers. NEVER the host's value: the client's
#:   environment is the sandbox's, so a host PATH naming ``.`` or a checkout
#:   directory let a committed ``bin/claude`` or root ``uv`` run in place of
#:   the governed binary (#2637). It always crosses as :data:`SANDBOX_PATH`,
#:   which :func:`sandbox_exec_environment` sets whatever the caller handed
#:   over; dropping it instead, on a gateway that replaces the image's PATH,
#:   would leave the launcher unable to find git at all.
#: * ``HOME`` — the CLI reads its own gateway configuration under it. The
#:   launcher repoints HOME before the agent starts, so the host's value is
#:   never where the agent writes.
#: * ``LANG`` / ``LC_ALL`` / ``LC_CTYPE`` — the runners emit the UTF-8 JSON
#:   event stream the host parses.
#:
#: Two absences are decisions rather than omissions. ``OPENSHELL_GATEWAY`` and
#: any other gateway control variable: the endpoint already travels as
#: ``--gateway-endpoint`` (:meth:`OpenShellClient._argv`), so the child needs
#: none of it, and a gateway credential forwarded here would be handed to the
#: very agent the gateway is containing. The ten proxy variables: ``[sandbox]
#: proxy_environment`` governs those, and its default ``"none"`` makes the
#: docker argv BLANK every one of them
#: (:func:`kew.sandbox._container_proxy_environment_args`) — this runtime has
#: no proxy argv, so not forwarding them is the only way to honour it.
#:
#: ``proxy_environment = "inherit"`` is not forwarded either, and that is the
#: absence this list will not quietly fix: a proxy URL carries userinfo, an
#: inherited one would point the agent at a destination no packaged egress
#: policy permits, and widening the allowlist per config setting is how an
#: allowlist stops bounding anything. What changed with that decision is what
#: kew SAYS about it — the posture no longer attests ten inherited variables
#: and ten more exposed credential names for an exec that forwards none of
#: them (:func:`kew.sandbox._attested_proxy_variables`), and the operator who
#: set it is warned at dispatch that it cannot take effect on this runtime
#: (:func:`kew.sandbox.openshell_ignored_argv_settings_warning`) — PR #2401
#: review.
SANDBOX_EXEC_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

#: The fixed ``PATH`` every sandbox exec runs under: the image's system
#: directories, where the governed runner shims (``/usr/bin/<program>``), git
#: and ``uv`` (``/usr/local/bin``) live, and nothing the checkout can write
#: into.
SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def sandbox_exec_environment(
    environment: Mapping[str, str], *, carried: Sequence[str] = ()
) -> dict[str, str]:
    """Narrow one exec's environment to the names that may cross the boundary.

    ``carried`` names the variables this exec deliberately takes in besides
    :data:`SANDBOX_EXEC_ENV_NAMES` — the runner's credential variables, which
    hold :data:`kew.openshell_broker.PLACEHOLDER_CREDENTIAL_VALUE` and not a
    credential. A name absent from both lists does not cross, whatever the
    caller put in ``environment``: this is the chokepoint, so a caller that
    hands over a whole ``os.environ`` leaks nothing through it. ``PATH`` is
    the one admitted name whose VALUE is not the caller's: it is always
    :data:`SANDBOX_PATH`, so no host PATH selects a binary inside the sandbox.
    """
    admitted = (*SANDBOX_EXEC_ENV_NAMES, *carried)
    crossing = {name: environment[name] for name in admitted if name in environment}
    crossing["PATH"] = SANDBOX_PATH
    return crossing


def _trim(text: str) -> str:
    """Collapse, redact and bound text before it is carried in an error."""
    collapsed = " ".join(redact_credentials(text).split())
    if len(collapsed) <= MAX_STDERR_CHARS:
        return collapsed
    return collapsed[:MAX_STDERR_CHARS] + "…"


class OpenShellError(RuntimeError):
    """One failed ``openshell`` call: what ran, how it failed, what it said.

    ``command`` is the resolved argv with credential forms redacted. It is safe
    to log because the client never puts a credential value in an argument in
    the first place — the redaction is the belt to that braces.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        reason: str,
        exit_code: int | None = None,
        stderr: str = "",
    ) -> None:
        self.command = tuple(redact_credentials(part) for part in command)
        self.exit_code = exit_code
        self.stderr = _trim(stderr)
        code = "no exit code" if exit_code is None else f"exit {exit_code}"
        said = f": {self.stderr}" if self.stderr else ""
        super().__init__(f"{reason}: `{' '.join(self.command)}` ({code}){said}")


class OpenShellMissing(OpenShellError):
    """The CLI is not installed, not allowlisted, or could not be launched."""


class OpenShellOutcomeUnknown(OpenShellError):
    """The call broke after it was sent; whether the gateway acted is unknown.

    The one thing a caller must not read from this is "nothing happened". A
    mutating call — create, delete, upload — may have committed on the gateway
    before the connection broke or the deadline passed, so the state has to be
    re-read (:meth:`OpenShellClient.sandbox_get`) rather than assumed.
    """


class OpenShellTimeout(OpenShellOutcomeUnknown):
    """The call exceeded its deadline, so its outcome is unknown."""


class OpenShellGatewayUnreachable(OpenShellError):
    """The gateway was never reached, so nothing is known about the sandbox."""


class OpenShellNotFound(OpenShellError):
    """The gateway answered that the named sandbox or provider is not there."""


class OpenShellVersionMismatch(OpenShellError):
    """The installed CLI is not the version this client was written against."""


def _is_indeterminate(lowered: str) -> bool:
    """True when casefolded stderr places the break after the request was sent."""
    if _EOF_RE.search(lowered):
        return True
    return any(marker in lowered for marker in _INDETERMINATE_MARKERS)


def _classify(command: Sequence[str], exit_code: int, stderr: str) -> OpenShellError:
    """Map a non-zero exit to the narrowest error its stderr supports.

    v0.1.2 does not document distinct exit codes, so stderr is the only signal
    available. An unrecognised failure stays the base class rather than being
    guessed into a specific one.
    """
    lowered = stderr.casefold()
    if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return OpenShellGatewayUnreachable(
            command,
            reason="the openshell gateway is unreachable",
            exit_code=exit_code,
            stderr=stderr,
        )
    if _is_indeterminate(lowered):
        return OpenShellOutcomeUnknown(
            command,
            reason=(
                "the openshell call broke after it was sent, so whether the "
                "gateway acted is unknown"
            ),
            exit_code=exit_code,
            stderr=stderr,
        )
    if any(marker in lowered for marker in _ABSENT_MARKERS):
        return OpenShellNotFound(
            command,
            reason="openshell has no such sandbox or provider",
            exit_code=exit_code,
            stderr=stderr,
        )
    return OpenShellError(
        command,
        reason="openshell refused the call",
        exit_code=exit_code,
        stderr=stderr,
    )


def _argument(value: str, field: str) -> str:
    """Refuse an argument that is blank or would be read as a flag.

    A sandbox name is caller-supplied and lands in argv next to real flags, so
    a value like ``--gateway-endpoint=…`` would redirect the call instead of
    naming a sandbox.
    """
    if not value.strip():
        raise ValueError(f"openshell {field} cannot be blank")
    if value.startswith("-"):
        raise ValueError(
            f"openshell {field} cannot start with '-': {value!r} reads as a flag"
        )
    return value


def _credential_env_name(name: str) -> str:
    """Refuse anything that is not a plain environment variable name."""
    if not _ENV_NAME_RE.match(name):
        raise ValueError(
            f"openshell credential must be an environment variable NAME, "
            f"not {name!r} — values never travel in argv"
        )
    return name


def _label_pair(key: str, value: str) -> str:
    """One ``key=value`` label, with the key proven not to be a flag."""
    if "=" in key:
        raise ValueError(f"openshell label key cannot contain '=': {key!r}")
    return f"{_argument(key, 'label key')}={value}"


@dataclass(frozen=True)
class Gateway:
    """Which gateway the client talks to, and which input decided that."""

    #: Empty when neither input supplied one, leaving the CLI's own default.
    endpoint: str
    #: ``environment``, ``config`` or ``cli-default``.
    source: str


@dataclass(frozen=True)
class GatewayStatus:
    """A gateway that answered, with the first line of what it said."""

    gateway: Gateway
    detail: str


@dataclass(frozen=True)
class Sandbox:
    """One sandbox as the gateway reports it under ``-o json``.

    ``status`` is whatever the CLI said, never a value this client invented: an
    unparseable payload raises instead of reporting an "unknown" sandbox, which
    a caller would read as a fact about the container.
    """

    name: str
    status: str
    identity: str
    image: str
    labels: Mapping[str, str]
    raw: Mapping[str, object]


@dataclass(frozen=True)
class Provider:
    """One credential provider: its profile and the env names it reads."""

    name: str
    profile: str
    credential_names: tuple[str, ...]
    raw: Mapping[str, object]


@dataclass(frozen=True)
class OpenShellExec:
    """A streaming ``sandbox exec`` in flight.

    stdin is closed (``DEVNULL``): the agent inside the sandbox is driven by
    its prompt, and an open stdin would make the host a channel into it.
    ``wait`` requires a deadline, because this is the one call the client
    cannot bound itself — the caller owns how long the command may run.
    """

    command: tuple[str, ...]
    process: subprocess.Popen[str]
    #: Output ``wait`` had to take off the pipe because the caller had not read
    #: it. Empty for a caller that streams :attr:`stdout` itself; kept rather
    #: than dropped, because the deadline is about how long the exec ran and
    #: this is still what it said.
    drained: list[str] = field(default_factory=list, compare=False)

    @property
    def stdout(self) -> IO[str]:
        """The merged stdout/stderr stream of the exec."""
        stream = self.process.stdout
        if stream is None:
            raise OpenShellError(
                self.command, reason="openshell exec produced no output stream"
            )
        return stream

    def wait(self, timeout: float) -> int:
        """Wait for the exec, killing it when it overruns ``timeout``.

        The child writes into a pipe, so waiting on the exit status alone
        deadlocks the moment the exec prints more than the pipe buffer holds
        and nobody is reading it: the child blocks in ``write``, kew blocks in
        ``wait``, and ``timeout`` then kills a command that had already
        finished its work. The pipe is drained here while the wait runs, so an
        exec is bounded by how long it took and never by how much it printed.
        """
        try:
            leftover = self._wait_draining(timeout)
        except subprocess.TimeoutExpired as exc:
            raise self._killed(timeout, exc) from exc
        if leftover:
            self.drained.append(leftover)
        return self.process.wait()

    def _wait_draining(self, timeout: float) -> str:
        """Wait with the pipe kept empty, returning whatever was still unread.

        A caller that streamed :attr:`stdout` to EOF leaves nothing to read;
        one that closed it (or never had a pipe) is waited on directly, because
        ``communicate`` cannot select on a closed stream.
        """
        stream = self.process.stdout
        if stream is None or stream.closed:
            self.process.wait(timeout=timeout)
            return ""
        stdout, _ = self.process.communicate(timeout=timeout)
        return stdout or ""

    def _killed(
        self, timeout: float, expired: subprocess.TimeoutExpired
    ) -> OpenShellTimeout:
        """Kill the overrunning exec, reap it, and name the deadline it blew.

        The reap is not bookkeeping: ``kill`` only posts the signal, so without
        the wait the child stays a zombie holding its pipe for as long as the
        dispatch lives. SIGKILL cannot be blocked, so the only thing that can
        outlast the short reap deadline is a stop/core-dump state the kernel
        owns — reporting the overrun then matters more than waiting on it.
        """
        self.process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=KILL_REAP_TIMEOUT_SECONDS)
        return OpenShellTimeout(
            self.command,
            reason=f"openshell exec exceeded {timeout:g}s and was killed",
            stderr=_stream_text(expired.stdout),
        )


@dataclass(frozen=True)
class _Completed:
    """A successful call: the argv that ran, and what it printed."""

    command: tuple[str, ...]
    stdout: str


class OpenShellSettings(Protocol):
    """The ``[sandbox.openshell]`` shape, structurally.

    Declared rather than imported so this module stays below ``kew.config`` and
    is usable from a test with a two-field stub.
    """

    gateway: str
    version: str


def resolve_gateway(configured: str, environment: Mapping[str, str]) -> Gateway:
    """Pick the gateway endpoint and record which input supplied it."""
    from_environment = environment.get(GATEWAY_ENV, "").strip()
    if from_environment:
        return Gateway(from_environment, GATEWAY_FROM_ENVIRONMENT)
    from_config = configured.strip()
    if from_config:
        return Gateway(from_config, GATEWAY_FROM_CONFIG)
    return Gateway("", GATEWAY_FROM_CLI_DEFAULT)


def _stream_text(stream: object) -> str:
    """Best-effort text for the partial output a timeout left behind."""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream if isinstance(stream, str) else ""


def _parse_json_object(completed: _Completed) -> dict[str, object]:
    """Parse ``-o json`` output, refusing anything that is not an object."""
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OpenShellError(
            completed.command,
            reason=f"openshell did not return JSON ({exc.msg})",
            stderr=completed.stdout,
        ) from exc
    if not isinstance(parsed, dict):
        raise OpenShellError(
            completed.command,
            reason=(
                f"openshell returned a JSON {type(parsed).__name__}, not an object"
            ),
            stderr=completed.stdout,
        )
    return parsed


def _required_text(
    completed: _Completed, payload: Mapping[str, object], *names: str
) -> str:
    """The first non-empty string among ``names``, or a raise naming them all.

    v0.1.2 spells some fields two ways across subcommands (``id``/``sandbox_id``,
    ``status``/``state``), so aliases are accepted — but a payload carrying
    neither is a parse failure, never a blank field a caller would believe.
    """
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise OpenShellError(
        completed.command,
        reason=f"openshell JSON has no {' / '.join(names)} field",
        stderr=completed.stdout,
    )


def _labels(completed: _Completed, payload: Mapping[str, object]) -> dict[str, str]:
    """Labels as strings. ``null`` or absent means none were set."""
    raw = payload.get("labels")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OpenShellError(
            completed.command,
            reason="openshell JSON labels are not an object",
            stderr=completed.stdout,
        )
    return {str(key): str(value) for key, value in raw.items()}


def _credential_names(
    completed: _Completed, payload: Mapping[str, object]
) -> tuple[str, ...]:
    """The env names a provider reads, required rather than defaulted to none.

    An empty tuple would say "this provider holds no credential", which is the
    one thing a provider cannot be; a payload without the field is a parse
    failure so the shape is fixed instead of being read as a fact.
    """
    for name in ("credentials", "credential_names"):
        raw = payload.get(name)
        if isinstance(raw, list):
            return tuple(str(entry) for entry in raw)
    raise OpenShellError(
        completed.command,
        reason="openshell JSON has no credentials list",
        stderr=completed.stdout,
    )


def _nested(completed: _Completed, payload: Mapping[str, object], *path: str) -> object:
    """The value at ``path`` in a report, or a raise naming the whole path.

    Absent is a parse failure rather than a default, for the same reason
    :func:`_credential_names` refuses an empty list: every field read through
    here is a containment fact, and "the gateway did not say" must never read
    as "the gateway said no".
    """
    cursor: object = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            raise OpenShellError(
                completed.command,
                reason=f"openshell JSON has no {'.'.join(path)} field",
                stderr=completed.stdout,
            )
        cursor = cursor[key]
    return cursor


def _required_flag(
    completed: _Completed, payload: Mapping[str, object], *path: str
) -> bool:
    """A boolean setting, refusing a truthy string that only looks like one."""
    value = _nested(completed, payload, *path)
    if not isinstance(value, bool):
        raise OpenShellError(
            completed.command,
            reason=f"openshell JSON {'.'.join(path)} is not a boolean",
            stderr=completed.stdout,
        )
    return value


def _required_count(
    completed: _Completed, payload: Mapping[str, object], *path: str
) -> int:
    """A non-negative integer. ``bool`` is not an integer here on purpose."""
    value = _nested(completed, payload, *path)
    if type(value) is not int or value < 0:
        raise OpenShellError(
            completed.command,
            reason=f"openshell JSON {'.'.join(path)} is not a count",
            stderr=completed.stdout,
        )
    return value


def _required_nested_text(
    completed: _Completed, payload: Mapping[str, object], *path: str
) -> str:
    """A non-empty string at ``path``."""
    value = _nested(completed, payload, *path)
    if not isinstance(value, str) or not value.strip():
        raise OpenShellError(
            completed.command,
            reason=f"openshell JSON {'.'.join(path)} is not a name",
            stderr=completed.stdout,
        )
    return value.strip()


@dataclass(frozen=True)
class GatewayReport:
    """The gateway's own account of itself, under ``status -o json``.

    The gateway — not the CLI on this host, and not a file this host wrote — is
    the authority on every fact here: its own build version, whether it is
    listening on TLS, whether it ships telemetry, whether it will act on an
    agent's own policy proposal, the digests it launches the supervisor and
    sandboxes from, and the Landlock ABI of the container VM kernel it enforces
    in.

    kew reads the running gateway rather than a config file because the two can
    disagree: a gateway started by somebody else's service manager never read
    this host's shell settings, so a file saying "telemetry off" proves nothing
    about the process (#2321).

    Every field is a setting the gateway *states*. The policy bytes a dispatch
    runs under are deliberately NOT here: kew hands each sandbox its policy file
    at ``sandbox create`` time (see :meth:`OpenShellClient.sandbox_create`), so
    the gateway holds no runner-keyed policy for kew to compare against and the
    boundary's policy digest is proven by the egress certification instead
    (``kew.sandbox.require_openshell_egress_certification``).
    """

    gateway: Gateway
    version: str
    tls: bool
    telemetry: bool
    agent_proposals: bool
    agent_approval: str
    supervisor_image: str
    sandbox_image: str
    landlock_abi: int
    raw: Mapping[str, object]


class OpenShellClient:
    """Every ``openshell`` call kew makes, with gateway and deadlines bound."""

    def __init__(
        self,
        *,
        gateway: Gateway,
        expected_version: str,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.expected_version = expected_version
        self._environment = environment

    @classmethod
    def from_settings(
        cls,
        settings: OpenShellSettings,
        environment: Mapping[str, str] | None = None,
    ) -> OpenShellClient:
        """Build a client from ``[sandbox.openshell]``."""
        source = os.environ if environment is None else environment
        return cls(
            gateway=resolve_gateway(settings.gateway, source),
            expected_version=settings.version,
            environment=environment,
        )

    # -- the one place argv is assembled and the one place it is run --------

    def _argv(self, arguments: Sequence[str]) -> list[str]:
        """Prefix the subcommand with the CLI name and the chosen gateway."""
        endpoint = self.gateway.endpoint
        gateway = [GATEWAY_FLAG, endpoint] if endpoint else []
        return [CLI, *gateway, *arguments]

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._environment is None else self._environment

    def _run(
        self,
        arguments: Sequence[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        retain: Sequence[str] = (),
    ) -> _Completed:
        """Run one bounded call, turning every failure into a typed error."""
        command = self._argv(arguments)
        try:
            completed = subprocess_utils.run(
                command,
                check=False,
                timeout=timeout,
                env=env,
                retain_credentials=retain,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenShellTimeout(
                command,
                reason=f"openshell exceeded {timeout:g}s, so its outcome is unknown",
                stderr=_stream_text(exc.stderr),
            ) from exc
        except ValueError as exc:
            raise OpenShellMissing(
                command,
                reason=f"the {CLI} CLI is not installed or not on the allowlist",
            ) from exc
        except OSError as exc:
            raise OpenShellMissing(
                command, reason=f"the {CLI} CLI could not be launched ({exc.strerror})"
            ) from exc
        if completed.returncode != 0:
            raise _classify(command, completed.returncode, completed.stderr)
        return _Completed(tuple(command), completed.stdout)

    # -- the CLI and the gateway itself -------------------------------------

    def version(self) -> str:
        """The installed CLI version, read from ``openshell --version``."""
        completed = self._run(["--version"], timeout=PROBE_TIMEOUT_SECONDS)
        match = _VERSION_RE.search(completed.stdout)
        if match is None:
            raise OpenShellError(
                completed.command,
                reason="openshell printed no version number",
                stderr=completed.stdout,
            )
        return match.group(0)

    def check_version(self) -> str:
        """Return the installed version, refusing one kew was not written for."""
        installed = self.version()
        if installed != self.expected_version:
            raise OpenShellVersionMismatch(
                self._argv(["--version"]),
                reason=(
                    f"openshell {installed} is installed; this client is written "
                    f"against {self.expected_version} "
                    f"(set [sandbox.openshell] version to accept it)"
                ),
            )
        return installed

    def status(self) -> GatewayStatus:
        """Prove the gateway answers, and carry the first line it said."""
        completed = self._run(["status"], timeout=PROBE_TIMEOUT_SECONDS)
        detail = next(
            (line.strip() for line in completed.stdout.splitlines() if line.strip()),
            "",
        )
        if not detail:
            raise OpenShellError(
                completed.command,
                reason="openshell status printed nothing, so the gateway is unproven",
            )
        return GatewayStatus(self.gateway, _trim(detail))

    def report(self) -> GatewayReport:
        """The gateway's self-report, as :class:`GatewayReport`.

        ``status -o json`` rather than a second subcommand: the same ``-o json``
        this client already reads sandboxes and providers through, so one
        answering call proves the gateway is reachable AND carries every setup
        fact ``kew doctor`` reports on.
        """
        completed = self._run(
            ["status", *JSON_OUTPUT_FLAGS], timeout=PROBE_TIMEOUT_SECONDS
        )
        payload = _parse_json_object(completed)
        return GatewayReport(
            gateway=self.gateway,
            version=_required_text(completed, payload, "version"),
            tls=_required_flag(completed, payload, "tls", "enabled"),
            telemetry=_required_flag(completed, payload, "telemetry", "enabled"),
            agent_proposals=_required_flag(completed, payload, "agent", "proposals"),
            agent_approval=_required_nested_text(
                completed, payload, "agent", "approval"
            ),
            supervisor_image=_required_nested_text(
                completed, payload, "images", "supervisor"
            ),
            sandbox_image=_required_nested_text(
                completed, payload, "images", "sandbox"
            ),
            landlock_abi=_required_count(completed, payload, "kernel", "landlock_abi"),
            raw=payload,
        )

    # -- sandboxes ----------------------------------------------------------

    def sandbox_create(
        self,
        name: str,
        *,
        image: str,
        providers: Iterable[str] = (),
        policy_path: Path | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Create a sandbox from an image, its providers and its policy file."""
        arguments = [
            "sandbox",
            "create",
            _argument(name, "sandbox name"),
            "--image",
            _argument(image, "image"),
        ]
        for provider in providers:
            arguments += ["--provider", _argument(provider, "provider name")]
        if policy_path is not None:
            arguments += ["--policy", _argument(str(policy_path), "policy path")]
        for key, value in (labels or {}).items():
            arguments += ["--label", _label_pair(key, value)]
        self._run(arguments, timeout=CONTROL_TIMEOUT_SECONDS)

    def sandbox_get(self, name: str) -> Sandbox:
        """Read one sandbox's state from ``sandbox get -o json``."""
        completed = self._run(
            [
                "sandbox",
                "get",
                _argument(name, "sandbox name"),
                *JSON_OUTPUT_FLAGS,
            ],
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        payload = _parse_json_object(completed)
        return Sandbox(
            name=_required_text(completed, payload, "name"),
            status=_required_text(completed, payload, "status", "state"),
            identity=_required_text(completed, payload, "id", "sandbox_id"),
            image=_required_text(completed, payload, "image"),
            labels=_labels(completed, payload),
            raw=payload,
        )

    def sandbox_delete(self, name: str) -> None:
        """Delete a sandbox. An absent one raises :class:`OpenShellNotFound`."""
        self._run(
            ["sandbox", "delete", _argument(name, "sandbox name")],
            timeout=CONTROL_TIMEOUT_SECONDS,
        )

    def sandbox_upload(self, name: str, source: Path, destination: str) -> None:
        """Copy a host path into the sandbox, ignoring ``.gitignore``."""
        self._run(
            [
                "sandbox",
                "upload",
                _argument(name, "sandbox name"),
                _argument(str(source), "upload source"),
                _argument(destination, "upload destination"),
                NO_GIT_IGNORE_FLAG,
            ],
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )

    def sandbox_download(self, name: str, source: str, destination: Path) -> None:
        """Copy a sandbox path back out to the host."""
        self._run(
            [
                "sandbox",
                "download",
                _argument(name, "sandbox name"),
                _argument(source, "download source"),
                _argument(str(destination), "download destination"),
            ],
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )

    def sandbox_exec(
        self,
        name: str,
        *,
        workdir: str,
        argv: Sequence[str],
        env: dict[str, str] | None = None,
        retain: Sequence[str] = (),
    ) -> OpenShellExec:
        """Start a command in the sandbox and stream its merged output.

        ``env`` is the CLIENT's environment, which v0.1.2 forwards into the
        sandbox. It is how the agent's credential variables arrive bound to
        ``kew.openshell_broker.PLACEHOLDER_CREDENTIAL_VALUE`` — names the
        runner CLIs refuse to start without, holding a value that
        authenticates nothing anywhere. ``None`` means "nothing but the
        baseline", which is what the egress certification probe wants.

        Whatever the caller supplies, only
        :data:`SANDBOX_EXEC_ENV_NAMES` and the names in ``retain`` cross:
        :func:`sandbox_exec_environment` is applied HERE, at the one call that
        reaches into a sandbox, rather than trusted to each caller. Before
        that this method forwarded whatever it was handed: the launch handed it
        the host environment minus a credential DENYLIST, and the certification
        probe handed it ``None``, which the subprocess chokepoint turns into
        the host environment minus the publication credentials. Either way
        every host secret those lists had never heard of reached the contained
        agent, while the docker boundary beside it forwarded the names it had
        enumerated and nothing else (PR #2401 review).

        ``start_new_session=True`` makes this child its own process-group
        leader, so its pid IS a pgid — which is what the launch registers on
        the run row as ``attempt_pgid``
        (``dispatch_sandbox._register_openshell_client``). Without it that
        column held a pid belonging to kew's OWN group: a later process holding
        only the row could not ``killpg`` the live client, and
        ``process_groups.attempts_are_dead`` reported a running attempt as dead
        — so a fleet stop missed it and a reconcile could finalize it (PR #2401
        review). The docker client beside it is launched the same way.

        ``retain`` names the variables that default scrubbing would otherwise
        drop out of ``env`` — ``OPENROUTER_API_KEY`` above all, which every
        boundary strips because it normally carries inference authority. On
        this call it carries the placeholder instead, and dropping the NAME is
        what makes an OpenRouter-backed runner exit before its first model
        call. The caller is responsible for the value being a placeholder;
        :func:`kew.run_credentials.openshell_launch_environment` is the one
        that builds it.
        """
        if not argv:
            raise ValueError("openshell sandbox exec needs a command to run")
        command = self._argv(
            [
                "sandbox",
                "exec",
                _argument(name, "sandbox name"),
                "--workdir",
                _argument(workdir, "workdir"),
                "--",
                *argv,
            ]
        )
        try:
            process = subprocess_utils.popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=sandbox_exec_environment(
                    self._environ() if env is None else env, carried=retain
                ),
                start_new_session=True,
                retain_credentials=retain,
            )
        except ValueError as exc:
            raise OpenShellMissing(
                command,
                reason=f"the {CLI} CLI is not installed or not on the allowlist",
            ) from exc
        except OSError as exc:
            raise OpenShellMissing(
                command, reason=f"the {CLI} CLI could not be launched ({exc.strerror})"
            ) from exc
        return OpenShellExec(
            tuple(redact_credentials(part) for part in command), process
        )

    def sandbox_run(self, name: str, *, workdir: str, argv: Sequence[str]) -> str:
        """Run one short command in the sandbox and return what it printed.

        The bounded sibling of :meth:`sandbox_exec`, for a probe rather than a
        workload: it waits under :data:`PROBE_TIMEOUT_SECONDS`, and a deadline
        raises :class:`OpenShellTimeout` like every other bounded call. The
        environment goes through :func:`sandbox_exec_environment` for the same
        reason the workload's does — nothing but the baseline crosses.
        """
        if not argv:
            raise ValueError("openshell sandbox exec needs a command to run")
        return self._run(
            [
                "sandbox",
                "exec",
                _argument(name, "sandbox name"),
                "--workdir",
                _argument(workdir, "workdir"),
                "--",
                *argv,
            ],
            timeout=PROBE_TIMEOUT_SECONDS,
            env=sandbox_exec_environment(self._environ()),
        ).stdout

    def logs(self, name: str, *, since: str = "") -> str:
        """The sandbox's logs, optionally only those newer than ``since``."""
        arguments = ["logs", _argument(name, "sandbox name")]
        if since:
            arguments += ["--since", _argument(since, "logs since")]
        return self._run(arguments, timeout=PROBE_TIMEOUT_SECONDS).stdout

    # -- credential providers ----------------------------------------------

    def provider_create(
        self, name: str, *, profile: str, env_names: Sequence[str]
    ) -> None:
        """Create a provider that reads its credentials from the environment.

        ``env_names`` are names, and only names. The values reach the CLI
        through the child environment this method builds, never through argv.
        """
        credentials = tuple(_credential_env_name(entry) for entry in env_names)
        if not credentials:
            raise ValueError(
                "openshell provider needs at least one credential "
                "environment variable name"
            )
        arguments = [
            "provider",
            "create",
            _argument(name, "provider name"),
            "--profile",
            _argument(profile, "provider profile"),
        ]
        for credential in credentials:
            arguments += [CREDENTIAL_ENV_FLAG, credential]
        self._run(
            arguments,
            timeout=CONTROL_TIMEOUT_SECONDS,
            env=self._credential_environment(credentials),
            retain=credentials,
        )

    def _credential_environment(self, names: Sequence[str]) -> dict[str, str]:
        """The child environment carrying the values, proven to still hold them.

        Two checks, both loud, because the failure they prevent is a provider
        created against an empty credential — which authenticates as nobody and
        surfaces later as an inference error. Values are never echoed.
        """
        source = self._environ()
        unset = [name for name in names if not source.get(name, "").strip()]
        if unset:
            raise ValueError(
                "openshell provider credentials are unset in this environment: "
                + ", ".join(unset)
            )
        environment = child_environment(source, retain=names)
        stripped = [name for name in names if name not in environment]
        if stripped:
            raise ValueError(
                "openshell provider credentials cannot be forwarded to a child: "
                + ", ".join(stripped)
                + " — management authority never crosses an execution boundary"
            )
        return environment

    def provider_delete(self, name: str) -> None:
        """Delete a provider. An absent one raises :class:`OpenShellNotFound`."""
        self._run(
            ["provider", "delete", _argument(name, "provider name")],
            timeout=CONTROL_TIMEOUT_SECONDS,
        )

    def provider_get(self, name: str) -> Provider:
        """Read one provider from ``provider get -o json``."""
        completed = self._run(
            [
                "provider",
                "get",
                _argument(name, "provider name"),
                *JSON_OUTPUT_FLAGS,
            ],
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        payload = _parse_json_object(completed)
        return Provider(
            name=_required_text(completed, payload, "name"),
            profile=_required_text(completed, payload, "profile"),
            credential_names=_credential_names(completed, payload),
            raw=payload,
        )
