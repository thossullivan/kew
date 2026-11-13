"""`kew drive install`: the drive as a per-user launchd agent (#2810).

A detached ``kew drive`` dies with its host; the operator learns it is gone
only when they look. On macOS ``kew drive install`` writes a LaunchAgent that
starts the drive at login (``RunAtLoad``) and restarts it whenever it exits
abnormally (``KeepAlive`` / ``SuccessfulExit: false``) — a ``kew drive stop``
exits 0 and stays stopped.

No credential goes into the plist. The agent's environment is a minimal
``PATH`` (the kew binary's directory, operator-owned Homebrew directories and
the system's), the project (``KEW_PROJECT``) and the ``KEW_*`` config
variables in force at install, so the agent's drive resolves the same ledger,
lock and budget cap the install did. With ``--env-file`` the agent sources
that file when it starts, so the secret stays in the file the operator already
keeps it in, and the plist only names the file's path. The project and config
variables are re-applied after it, so the file cannot retarget the agent, and
a plist that would hold a credential is refused, never written.

The agent runs unattended with the operator's rights, so everything it runs,
sources or writes must be something only the operator can change: the env
file (checked at install and again before each start), the kew binary, the
plist and the log. Install and uninstall only replace, stop or delete a job
whose plist is the one kew writes for this project.

The install is recorded in the ledger (``drive:agent``) so ``kew drive status``
can say when an installed agent's plist has gone missing, is not loaded, or is
disabled — not only that the drive is not running. A reinstall keeps the
previous plist beside the new one and its own progress beside it too
(``.<label>.switch``) until the new job loads, so a kill partway is settled by
the next kew process rather than losing the working agent (#2870). Install,
uninstall and status each hold the agent lock (an ``flock`` beside the plist,
:func:`agent_lock`) while they touch it, so only a switch whose install has
died is ever settled — never one still in progress. The lock and the switch
follow the label, which only the project picks, so two installs of one
project under different ledgers still take turns.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from kew.drive import DriveStateError

DRIVE_AGENT_KEY = "drive:agent"
# Kew's mark on the plists it writes, and their format: an unrecorded plist
# is claimed only when it carries it.
AGENT_MARK = "KEW_DRIVE_AGENT"
AGENT_MARK_VERSION = "1"
# The SHA-256 of the whole definition kew wrote (:func:`definition_hash`), in
# its environment, where ``launchctl print`` shows it for the loaded job.
DEFINITION_HASH = "KEW_AGENT_DEFINITION_SHA256"
LABEL_PREFIX = "dev.kew.drive."
# How long one launchctl call may take; one that runs over has failed.
LAUNCHCTL_TIMEOUT_S = 30
# launchd waits this long before restarting a drive that exited abnormally, so
# a drive that keeps refusing (another one holds the lock) is not a hot loop.
THROTTLE_S = 60
# A shell that sources the env file into the environment and execs the drive.
# The paths travel as positional arguments, never spliced into the script.
# Before sourcing it re-checks what install checked: the file and its directory
# are the operator's, neither group- nor world-writable, and the file is no
# symlink — so a later chmod or swap is refused, not run. 78 is EX_CONFIG;
# launchd retries it once a THROTTLE_S, so fixing the file brings it back.
# After sourcing it re-applies the pins (``NAME=VALUE`` exported, a bare
# ``NAME`` unset) up to ``--``, so the file cannot point the agent at another
# project or ledger. The file is sourced inside a function, so a ``set --`` in
# it cannot touch the pins or the program.
_SOURCE_AND_EXEC = (
    'f="$1"; shift; d="$(dirname "$f")"; u="$(id -u)"; '
    'ok() { [ -n "$(find "$1" -prune -type "$2" -user "$u" '
    '! -perm -g+w ! -perm -o+w)" ]; }; '
    'if ! ok "$f" f || ! ok "$d" d; then '
    'echo "kew drive: refusing to source $f: it and $d must be yours and not '
    "group- or world-writable, and it must not be a symlink "
    '(chmod 600 $f; chmod go-w $d)" >&2; exit 78; fi; '
    'src() { set -a; . "$1"; set +a; }; src "$f"; '
    'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do case "$1" in '
    '*=*) export "$1" || exit 78 ;; *) unset "$1" || exit 78 ;; esac; shift; '
    'done; shift; exec "$@"'
)
_SHELL_PREFIX = ["/bin/sh", "-c", _SOURCE_AND_EXEC, "kew-drive"]
# A repository the plist may name: ``owner/name``, or a URL with one of these
# schemes and no userinfo, query or fragment.
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REPO_SCHEMES = ("https", "ssh")
# Credential forms no plist kew writes may hold, whatever put them there.
_CREDENTIAL_SHAPES = {
    "a URL with userinfo": re.compile(rb"[A-Za-z][A-Za-z0-9+.-]*://[^/\s@<>]+@"),
    "a token": re.compile(
        rb"(?<![A-Za-z0-9])(?:gh[opsur]_|github_pat_|sk-ant-|lin_api_|xox[bp]-)"
        rb"[A-Za-z0-9_-]{8,}"
    ),
}
_PIN = re.compile(r"[A-Z_][A-Z0-9_]*(=.*)?", re.DOTALL)
# The agent's PATH: the kew binary's directory, then these when they exist and
# only the operator (or root) can write them, then the system's.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# How launchd runs every agent kew writes.
_LAUNCH_POLICY: dict[str, Any] = {
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": THROTTLE_S,
    "ProcessType": "Background",
}

AGENT_LOADED = "loaded"
AGENT_NOT_LOADED = "not loaded"
AGENT_DISABLED = "disabled"
AGENT_MISSING = "missing"
AGENT_UNCHECKED = "unchecked"
AGENT_UNREADABLE = "unreadable"


class AgentRefused(ValueError):
    """An install or uninstall kew will not carry out; the message says why
    and what fixes it."""


class AgentBusy(AgentRefused):
    """Another kew process holds the agent lock: an install or uninstall is
    in progress."""


# The agent lock this context holds, which every launchctl call's keeper holds.
_HELD_LOCK: ContextVar[int | None] = ContextVar("kew_drive_agent_lock", default=None)


@contextmanager
def agent_lock(
    label: str, home: Path | None = None, *, create: bool = False
) -> Iterator[None]:
    """Hold ``label``'s agent lock (``.<label>.lock`` beside its plist, 0600)
    for the context, or raise ``AgentBusy`` at once when another kew process
    holds it. The kernel drops it when its holder dies, so a switch journalled
    while it is free was left by a kill (#2870). The lock follows the label,
    not the ledger, so installs of one project under different ledgers take
    turns. The LaunchAgents directory is made when ``create``; otherwise,
    without one there is no plist or switch to guard, nothing is made and no
    lock is held — so no launchctl call that changes launchd may run
    (:func:`_launchctl` refuses one).

    Each launchctl call run while it is held runs under a keeper process that
    holds it too (:func:`_launchctl`), so a launchctl that outlives a killed
    holder keeps it held until it exits, and no other process settles the
    switch under it (#2930)."""
    agents = _agents_dir(home) if create else agent_plist(label, home).parent
    if not create:
        if not os.path.lexists(agents):
            yield
            return
        _check_private(agents, "LaunchAgents directory", directory=True, root_ok=False)
    path = agents / f".{label}.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentBusy(
                f"another kew process is installing or removing the drive "
                f"agent ({path} is held); retry once it finishes"
            ) from None
        held = _HELD_LOCK.set(fd)
        try:
            yield
        finally:
            _HELD_LOCK.reset(held)
    finally:
        os.close(fd)


class AgentStore(Protocol):
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...


def agent_label(project: Path) -> str:
    """One label per project directory, readable and collision-free."""
    name = re.sub(r"[^A-Za-z0-9-]+", "-", project.name).strip("-") or "project"
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:10]
    return f"{LABEL_PREFIX}{name}.{digest}"


def agent_plist(label: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def _lstat_kind(path: Path, what: str, *, directory: bool) -> os.stat_result:
    """``path``'s own status, refused unless it is a real file (or directory)
    rather than a symlink to one."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise AgentRefused(f"no {what} at {path}") from None
    kind = "directory" if directory else "regular file"
    if stat.S_ISLNK(st.st_mode):
        raise AgentRefused(f"{what} {path} is a symlink; name the {kind} itself")
    if not (stat.S_ISDIR if directory else stat.S_ISREG)(st.st_mode):
        raise AgentRefused(f"{what} {path} is not a {kind}")
    return st


def _check_private(path: Path, what: str, *, directory: bool, root_ok: bool) -> None:
    """Refuse ``path`` unless it is a real file (or directory), not a symlink,
    owned by the operator (or root, when ``root_ok``) and writable by no one
    else."""
    st = _lstat_kind(path, what, directory=directory)
    owners = {os.getuid(), 0} if root_ok else {os.getuid()}
    if st.st_uid not in owners:
        raise AgentRefused(
            f"{what} {path} is owned by uid {st.st_uid}, not you; "
            f"another account could change what the agent runs as you"
        )
    if st.st_mode & 0o022:
        fix = "chmod go-w" if directory or root_ok else "chmod 600"
        raise AgentRefused(
            f"{what} {path} is group- or world-writable, so another account "
            f"could run code as you; `{fix} {path}`"
        )


def checked_env_file(path: Path) -> Path:
    """The env file the agent sources, absolute, once only the operator can
    change it or the directory it sits in."""
    absolute = Path(os.path.abspath(path))
    parent = Path(os.path.realpath(absolute.parent))
    _check_private(absolute, "env file", directory=False, root_ok=False)
    _check_private(parent, "env file directory", directory=True, root_ok=False)
    return parent / absolute.name


def checked_binary(path: Path) -> Path:
    """The kew the agent runs, pinned to its absolute real path, once only the
    operator (or root) can change it or the directory it sits in."""
    if not path.is_absolute():
        raise AgentRefused(f"kew binary {path} is relative; name it by absolute path")
    real = Path(os.path.realpath(path))
    _check_private(real, "kew binary", directory=False, root_ok=True)
    if not real.stat().st_mode & stat.S_IXUSR:
        raise AgentRefused(f"kew binary {real} is not executable")
    _check_private(real.parent, "kew binary directory", directory=True, root_ok=True)
    return real


def agent_search_path(binary: Path) -> str:
    """The agent's ``PATH``: never the installing shell's, which may name
    directories other accounts can write."""
    dirs = [str(binary.parent)]
    for tool_dir in _TOOL_DIRS:
        try:
            _check_private(
                Path(tool_dir), "tool directory", directory=True, root_ok=True
            )
        except AgentRefused:
            continue
        dirs.append(tool_dir)
    dirs += _SYSTEM_PATH
    return ":".join(dict.fromkeys(dirs))


def prepare_log(log: Path) -> None:
    """Make the log's directory (0700 when kew creates it) and the log itself
    (0600), and refuse either unless only the operator can change it: another
    account's log, or one in a directory others can write, could be swapped
    for a link the agent then writes through."""
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = Path(os.path.realpath(log.parent))
    _check_private(parent, "drive log directory", directory=True, root_ok=False)
    log = parent / log.name
    if not os.path.lexists(log):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        os.close(os.open(log, flags, 0o600))
    _check_private(log, "drive log", directory=False, root_ok=False)


def _write_new(path: Path, data: bytes, mode: int) -> None:
    """Write ``path`` whole: a fresh temporary file beside it, never through a
    link, then renamed over it."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _after_pins(argv: list[str]) -> list[str]:
    """The program after the wrapper's pins and ``--``; empty when they are
    not pins."""
    end = argv.index("--") if "--" in argv else -1
    if end < 0 or not all(_PIN.fullmatch(a) for a in argv[:end]):
        return []
    return argv[end + 1 :]


def _is_kew_program(argv: object) -> bool:
    """Whether ``argv`` is a program :meth:`AgentSpec.program` writes."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return False
    if argv[: len(_SHELL_PREFIX)] == _SHELL_PREFIX:
        argv = _after_pins(argv[len(_SHELL_PREFIX) + 1 :])
    if len(argv) < 2 or argv[1] != "drive" or not Path(argv[0]).is_absolute():
        return False
    pinned = argv.index("--kew-binary") + 1 if "--kew-binary" in argv else 0
    return 0 < pinned < len(argv) and argv[pinned] == argv[0]


def _foreign_identity(
    data: dict[str, Any], project: Path, *, marked: bool
) -> str | None:
    """Why a kew-shaped plist is not ``project``'s agent; None when it is.
    ``marked`` also asks for kew's ownership mark, which plists written before
    #2870 lack."""
    env = data.get("EnvironmentVariables")
    env = env if isinstance(env, dict) else {}
    runs = (data.get("WorkingDirectory"), env.get("KEW_PROJECT"))
    if runs != (str(project), str(project)):
        return f"it runs a drive for another project, not {project}"
    if any(data.get(key) != value for key, value in _LAUNCH_POLICY.items()):
        return "its launch policy is not the one kew writes"
    if marked and env.get(AGENT_MARK) != AGENT_MARK_VERSION:
        return f"it does not carry kew's mark {AGENT_MARK}={AGENT_MARK_VERSION}"
    return _foreign_log(data)


def _foreign_log(data: dict[str, Any]) -> str | None:
    """Why the plist's log is not the one absolute log kew writes both
    output and errors to; None when it is."""
    log = data.get("StandardOutPath")
    if not (isinstance(log, str) and Path(log).is_absolute()):
        return "its log is not an absolute path"
    if data.get("StandardErrorPath") != log:
        return "its output and errors do not go to one log"
    return None


def foreign_plist(
    path: Path, label: str, project: Path, *, marked: bool = False
) -> str | None:
    """Why ``path`` is not the agent kew writes under ``label`` for
    ``project``; None when it is."""
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception as exc:  # whatever plistlib makes of it, it is not kew's
        return f"it does not parse as a plist ({type(exc).__name__})"
    if not isinstance(data, dict) or data.get("Label") != label:
        return f"its Label is not {label}"
    if not _is_kew_program(data.get("ProgramArguments")):
        return "its ProgramArguments are not a kew drive"
    return _foreign_identity(data, project, marked=marked)


def _check_repo(repo: str | None) -> None:
    """Refuse a ``KEW_REPO`` that is not a credential-free identifier:
    ``owner/name``, or an ``https://`` or ``ssh://`` URL with no userinfo, no
    query and no fragment (where a token could ride along)."""
    if not repo or _REPO_NAME.fullmatch(repo):
        return
    url = urlsplit(repo)
    if (
        url.scheme in _REPO_SCHEMES
        and url.hostname
        and "@" not in url.netloc
        and not any(c in repo for c in "?#")
    ):
        return
    raise AgentRefused(
        "KEW_REPO must be owner/name, or an https:// or ssh:// URL with no "
        "user, query or fragment in it; set it to owner/name and keep the "
        "token in --env-file"
    )


def _check_printable(program: list[str]) -> None:
    """Refuse a program argument :func:`_loaded_job` cannot read back
    from ``launchctl print``, which lists one argument per line and ends the
    list at a ``}`` line: one that spans lines, or is ``}``. "Spans lines"
    means whatever :meth:`str.splitlines` splits on, ``\u2028`` and ``\x85``
    included, since that is how the listing is read. Read back wrong, a
    restore would look like another program and be booted out (#2955)."""
    for arg in program:
        if "".join(arg.splitlines()) != arg or arg.strip() == "}":
            raise AgentRefused(
                f"the agent's program argument {arg!r} would not read back from "
                "`launchctl print`; use one without a line break that is not `}`"
            )


@dataclass(frozen=True)
class AgentSpec:
    """What the agent runs: ``binary drive ARGS`` in ``project``."""

    project: Path
    binary: Path
    drive_args: tuple[str, ...]
    log: Path
    env_file: Path | None = None
    # The ``KEW_*`` config layer in force at install (``config_environment``).
    environment: Mapping[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return agent_label(self.project)

    def program(self) -> list[str]:
        argv = [str(self.binary), "drive", *self.drive_args]
        if self.env_file is None:
            return argv
        return [*_SHELL_PREFIX, str(self.env_file), *self.pins(), "--", *argv]

    def pins(self) -> list[str]:
        """What the wrapper re-applies after the env file: every variable that
        picks the project, ledger or caps, at its install value, or unset."""
        from kew.config import config_variables

        env = self.environment_variables()
        return [
            f"{name}={env[name]}" if name in env else name
            for name in config_variables()
        ]

    def environment_variables(self) -> dict[str, str]:
        return {
            **self.environment,
            "KEW_PROJECT": str(self.project),
            "PATH": agent_search_path(self.binary),
            AGENT_MARK: AGENT_MARK_VERSION,
        }

    def render(self) -> bytes:
        """The plist, refused (``AgentRefused``) if it would hold a
        credential."""
        _check_repo(self.environment.get("KEW_REPO"))
        _check_printable(self.program())
        data = self._plist()
        for form, shape in _CREDENTIAL_SHAPES.items():
            if shape.search(data):
                raise AgentRefused(
                    f"the agent's plist would hold {form}; keep credentials in "
                    "--env-file, not in KEW_* settings, --kew-binary or paths"
                )
        return data

    def _plist(self) -> bytes:
        definition: dict[str, Any] = {
            "Label": self.label,
            "ProgramArguments": self.program(),
            "WorkingDirectory": str(self.project),
            "EnvironmentVariables": self.environment_variables(),
            **_LAUNCH_POLICY,
            "StandardOutPath": str(self.log),
            "StandardErrorPath": str(self.log),
        }
        definition["EnvironmentVariables"][DEFINITION_HASH] = definition_hash(
            definition
        )
        return plistlib.dumps(definition)


def definition_hash(definition: Mapping[str, Any]) -> str:
    """The SHA-256 of a plist's whole definition, leaving out its own
    :data:`DEFINITION_HASH`: launch settings ``launchctl print`` does not show
    (``ThrottleInterval``, ``ProcessType``) change it too."""
    env = definition.get("EnvironmentVariables")
    env = dict(env) if isinstance(env, dict) else {}
    env.pop(DEFINITION_HASH, None)
    body = plistlib.dumps({**definition, "EnvironmentVariables": env})
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class InstalledAgent:
    """The install the ledger records."""

    label: str
    plist: Path

    def encode(self) -> str:
        return json.dumps({"label": self.label, "plist": str(self.plist)})

    @classmethod
    def parse(cls, raw: str | None) -> InstalledAgent | None:
        """The recorded install; None when none is recorded. An unreadable
        record raises: read as "not installed" it would hide the agent."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(label=str(data["label"]), plist=Path(data["plist"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise DriveStateError(
                f"{DRIVE_AGENT_KEY} holds an unreadable record: {exc!r}"
            ) from exc


def recorded_agent(store: AgentStore) -> InstalledAgent | None:
    return InstalledAgent.parse(store.get_meta(DRIVE_AGENT_KEY))


def record_agent(store: AgentStore, agent: InstalledAgent | None) -> None:
    store.set_meta(DRIVE_AGENT_KEY, "" if agent is None else agent.encode())


def launchd_available() -> bool:
    """Whether this host runs launchd agents: macOS with ``launchctl``."""
    if sys.platform != "darwin":
        return False
    from kew.subprocess_utils import ensure_tool

    return ensure_tool("launchctl") is not None


def _domain() -> str:
    return f"gui/{os.getuid()}"


class LaunchctlTimeout(RuntimeError):
    """A launchctl call ran past ``LAUNCHCTL_TIMEOUT_S``."""


# Runs launchctl as its child and waits for it, holding the agent lock it was
# handed all the while, so the lock lives exactly as long as that launchctl
# does, whatever launchctl does with the descriptors it inherits (#2930). It
# enforces the timeout itself, and relays launchctl's output only once
# launchctl has exited, so a kew that is gone cannot break launchctl's pipes.
_KEEPER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(sys.argv[2:], stdout=-1, stderr=-1)\n"
    "try:\n"
    "    out, err = child.communicate(timeout=float(sys.argv[1]))\n"
    "    rc = child.returncode\n"
    "except subprocess.TimeoutExpired:\n"
    "    child.kill()\n"
    "    child.communicate()\n"
    "    out, err, rc = b'', (sys.argv[2] + ' timed out').encode(), 124\n"
    "try:\n"
    "    sys.stdout.buffer.write(out)\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.buffer.write(err)\n"
    "    sys.stderr.flush()\n"
    "except OSError:\n"
    "    pass\n"
    "sys.exit(rc if rc >= 0 else 128 - rc)\n"
)
# How much longer than its launchctl the keeper may take before kew gives up
# waiting for it.
_KEEPER_GRACE_S = 10
# The launchctl verbs that change what launchd holds: each runs only under the
# agent lock's keeper, never without it (#2930).
_MUTATING_VERBS = frozenset({"bootout", "bootstrap"})


class LaunchctlStillRunning(RuntimeError):
    """kew stopped waiting for a launchctl its keeper still runs: a second
    interrupt, or a keeper past its own timeout. The keeper holds the agent
    lock until launchctl exits, so nothing is rolled back under it; the next
    kew process settles the switch once it has (#2930)."""


def _launchctl(*args: str) -> Any:
    """Run launchctl; with the agent lock held in this context, under a keeper
    (:data:`_KEEPER`, :func:`_kept`) that holds it, so a launchctl that
    outlives its kew still holds it until it exits (#2930)."""
    import subprocess  # nosec B404 — only for TimeoutExpired

    from kew.subprocess_utils import run

    held = _HELD_LOCK.get()
    if held is None and args[0] in _MUTATING_VERBS:
        raise RuntimeError(
            f"launchctl {args[0]} must run under the agent lock, and none is held"
        )
    if held is not None:
        return _kept(held, *args)
    try:
        return run(["launchctl", *args], check=False, timeout=LAUNCHCTL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise _timed_out(args[0]) from None


def _timed_out(verb: str) -> LaunchctlTimeout:
    return LaunchctlTimeout(f"launchctl {verb} timed out after {LAUNCHCTL_TIMEOUT_S}s")


def _kept(held: int, *args: str) -> Any:
    """Run launchctl under a keeper holding the lock descriptor ``held``, in
    a session of its own so a terminal's interrupt does not reach it. The
    keeper is never killed: on any exception kew waits for it, and so for
    launchctl, to exit before passing the exception on (:func:`_outlast`)."""
    import subprocess  # nosec B404 — CompletedProcess, PIPE and TimeoutExpired

    from kew.subprocess_utils import TOOL_PATHS, popen, resolve_cmd

    TOOL_PATHS["python"] = sys.executable
    keep = ["python", "-I", "-S", "-c", _KEEPER, str(LAUNCHCTL_TIMEOUT_S)]
    cmd = [*keep, *resolve_cmd(["launchctl", *args])]
    keeper = popen(
        cmd,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(held,),
    )
    try:
        out, err = _communicate(keeper)
    except subprocess.TimeoutExpired:
        raise _still_running(keeper, args[0]) from None
    except BaseException:
        _outlast(keeper, args[0])
        raise
    if keeper.returncode == 124 and (err or "").endswith(" timed out"):
        raise _timed_out(args[0])
    return subprocess.CompletedProcess(cmd, keeper.returncode, out, err)


# How often a wait for the keeper looks up for an interrupt: a SIGINT another
# thread takes does not wake a main thread blocked on the keeper's pipes.
_KEEPER_POLL_S = 0.1


def _communicate(keeper: Any) -> tuple[str, str]:
    """The keeper's output once it exits, waited for in slices short enough
    that an interrupt is raised promptly; ``TimeoutExpired`` once it runs
    past its own timeout and grace."""
    import subprocess  # nosec B404 — only for TimeoutExpired
    import time

    deadline = time.monotonic() + LAUNCHCTL_TIMEOUT_S + _KEEPER_GRACE_S
    while True:
        left = deadline - time.monotonic()
        try:
            out, err = keeper.communicate(timeout=max(0, min(_KEEPER_POLL_S, left)))
        except subprocess.TimeoutExpired:
            if left <= _KEEPER_POLL_S:
                raise
            continue
        return out, err


def _outlast(keeper: Any, verb: str) -> None:
    """Wait for the keeper, and so its launchctl, to exit after the wait for
    it was cut short; raise ``LaunchctlStillRunning`` when it will not, or a
    second interrupt stops the wait."""
    import subprocess  # nosec B404 — only for TimeoutExpired

    try:
        _communicate(keeper)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        raise _still_running(keeper, verb) from None


def _still_running(keeper: Any, verb: str) -> LaunchctlStillRunning:
    return LaunchctlStillRunning(
        f"launchctl {verb} is still running (its keeper, pid {keeper.pid}, "
        "holds the agent lock until it exits); nothing was rolled back — "
        "the next `kew drive install`, `status` or `uninstall` settles "
        "the reinstall once it has"
    )


# What ``launchctl print`` exits with when launchd holds no such service.
LAUNCHCTL_NOT_FOUND = 113


class LaunchdUnanswered(RuntimeError):
    """``launchctl print`` failed without launchd's not-found answer, so kew
    cannot tell what launchd holds."""


def _printed(label: str) -> str | None:
    """What ``launchctl print`` shows for ``label``; None only for launchd's
    own not-found answer. Every other failure raises, so a launchd that did
    not answer is never taken for one that holds nothing (#2955)."""
    target = f"{_domain()}/{label}"
    done = _launchctl("print", target)
    if done.returncode == 0:
        return str(done.stdout or "")
    if done.returncode == LAUNCHCTL_NOT_FOUND:
        return None
    raise LaunchdUnanswered(
        f"launchctl print {target} exited {done.returncode}: "
        f"{(done.stderr or done.stdout or '').strip()}; kew cannot tell whether "
        "the agent is loaded — rerun once launchd answers"
    )


def _loaded(label: str) -> bool:
    """Whether launchd holds ``label`` (:func:`_printed`)."""
    return _printed(label) is not None


def _print_block(lines: list[str], name: str) -> list[str] | None:
    """The lines of the ``name = { ... }`` block of ``launchctl print``."""
    if f"{name} = {{" not in lines:
        return None
    block = lines[lines.index(f"{name} = {{") + 1 :]
    return block[: block.index("}")] if "}" in block else None


def _print_value(lines: list[str], name: str) -> str | None:
    prefix = f"{name} = "
    return next(
        (ln.removeprefix(prefix) for ln in lines if ln.startswith(prefix)), None
    )


def _environment(pairs: list[str]) -> dict[str, str]:
    """An ``environment`` block's ``NAME => VALUE`` lines; launchd's own
    ``XPC_*`` entries are not the plist's. The lines arrive stripped, so an
    empty value prints as ``NAME =>`` with nothing after the arrow."""
    entries = (line.partition(" =>") for line in pairs)
    return {k: v.strip() for k, _, v in entries if not k.startswith("XPC_")}


# launchd's names for a ``KeepAlive`` dictionary's conditions, which
# ``launchctl print`` lists in its ``semaphores`` block as ``NAME => 0|1``.
_KEEPALIVE_CONDITIONS = {"SuccessfulExit": "successful exit"}


def _keepalive(value: object) -> tuple[bool, tuple[str, ...]]:
    """A plist's ``KeepAlive`` in the form ``launchctl print`` shows it:
    whether the job is kept alive, and its conditions. ``true`` has none, so
    it never equals ``{SuccessfulExit: false}``; a condition launchd has no
    known name for keeps its plist spelling, which no print shows, so a job
    defined with one never matches (fail closed)."""
    if not isinstance(value, dict):
        return bool(value), ()
    return True, tuple(
        sorted(
            f"{_KEEPALIVE_CONDITIONS.get(str(k), repr(k))} => {int(v)}"
            if isinstance(v, bool)
            else f"{k!r} => {v!r}"
            for k, v in value.items()
        )
    )


def _loaded_job(label: str) -> dict[str, Any] | None:
    """What launchd holds for ``label``, as ``launchctl print`` shows it, in
    the shape of :func:`_defined_job`; None when it holds no job or prints no
    program. A ``print`` launchd did not answer raises (:func:`_printed`)."""
    printed = _printed(label)
    if printed is None:
        return None
    lines = [line.strip() for line in printed.splitlines()]
    arguments = _print_block(lines, "arguments")
    if arguments is None:
        return None
    properties = set((_print_value(lines, "properties") or "").split(" | "))
    semaphores = tuple(sorted(_print_block(lines, "semaphores") or []))
    return {
        "program": arguments,
        "environment": _environment(_print_block(lines, "environment") or []),
        "working directory": _print_value(lines, "working directory"),
        "stdout path": _print_value(lines, "stdout path"),
        "stderr path": _print_value(lines, "stderr path"),
        "launch policy": {
            "run at load": "runatload" in properties,
            "keep alive": ("keepalive" in properties, semaphores),
        },
    }


def _text(value: object) -> str | None:
    return None if value is None else str(value).strip()


def _defined_job(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The operational fields of a plist's job that ``launchctl print``
    shows: program, environment, working directory, log paths and launch
    policy, ``KeepAlive`` in its exact form (:func:`_keepalive`)."""
    program = definition.get("ProgramArguments")
    environment = definition.get("EnvironmentVariables")
    return {
        "program": [str(a).strip() for a in program]
        if isinstance(program, list)
        else None,
        "environment": {str(k): str(v).strip() for k, v in environment.items()}
        if isinstance(environment, dict)
        else {},
        "working directory": _text(definition.get("WorkingDirectory")),
        "stdout path": _text(definition.get("StandardOutPath")),
        "stderr path": _text(definition.get("StandardErrorPath")),
        "launch policy": {
            "run at load": bool(definition.get("RunAtLoad")),
            "keep alive": _keepalive(definition.get("KeepAlive")),
        },
    }


def _mismatch(label: str, plist: Path) -> str | None:
    """Why the job launchd holds for ``label`` is not the one ``plist``
    defines (the same program, environment, working directory, log paths and
    launch policy, and for a plist kew stamped, the same
    :data:`DEFINITION_HASH`); None when it is."""
    try:
        definition = plistlib.loads(plist.read_bytes())
        wanted = _defined_job(definition)
    except Exception as exc:  # an unreadable plist defines no job launchd holds
        return f"{plist} does not read as a plist ({type(exc).__name__})"
    loaded = _loaded_job(label)
    if loaded is None:
        return f"launchd shows no program for {label}"
    if loaded["program"] != wanted["program"]:
        return f"launchd runs another program for {label} than {plist} names"
    # A plist kew stamped is the loaded job only when the job carries the
    # stamp of the plist's whole definition.
    if DEFINITION_HASH in wanted["environment"] and loaded["environment"].get(
        DEFINITION_HASH
    ) != definition_hash(definition):
        return (
            f"launchd holds another definition for {label} than {plist} "
            f"({DEFINITION_HASH} differs)"
        )
    stale = [name for name, value in wanted.items() if loaded[name] != value]
    if stale:
        return (
            f"launchd holds another {', '.join(stale)} for {label} than {plist} names"
        )
    return None


def _bootout(label: str) -> None:
    """Unload ``label``; one launchd still holds afterwards raises. An unload
    that finds nothing loaded is fine."""
    target = f"{_domain()}/{label}"
    done = _launchctl("bootout", target)
    if _loaded(label):
        raise RuntimeError(
            f"launchctl bootout {target} exited {done.returncode} and the agent "
            f"is still loaded: {(done.stderr or done.stdout or '').strip()}"
        )


def _agents_dir(home: Path | None) -> Path:
    agents = agent_plist("x", home).parent
    agents.mkdir(mode=0o755, parents=True, exist_ok=True)
    _check_private(agents, "LaunchAgents directory", directory=True, root_ok=False)
    return agents


def _ours_to_replace(
    plist: Path, spec: AgentSpec, recorded: InstalledAgent | None
) -> bool:
    """Whether the job at the spec's label is kew's own to boot out. A plist
    that is not the one kew writes for the spec's project is refused, never
    replaced."""
    if plist.is_symlink():
        raise AgentRefused(f"{plist} is a symlink; kew will not write through it")
    if plist.exists():
        foreign = foreign_plist(plist, spec.label, spec.project)
        if foreign is not None:
            raise AgentRefused(
                f"{plist} is not the agent kew writes ({foreign}); kew will not "
                "replace or stop it — move it aside first"
            )
        return True
    # The plist was deleted under an agent kew recorded installing. Booting
    # out a loaded one would leave nothing to put back if its replacement
    # fails to load (#2955).
    if recorded is None or recorded.label != spec.label:
        return False
    if _loaded(spec.label):
        raise AgentRefused(
            f"the recorded agent {spec.label} is loaded but its plist {plist} "
            "is gone; kew will not stop it with nothing to restore — run "
            "`kew drive uninstall`, then install"
        )
    return True


def _bootstrap(plist: Path) -> str | None:
    """Load ``plist``; why launchd refused (or ran out of time), or None when
    it loaded. A refusal may still have registered the job."""
    try:
        done = _launchctl("bootstrap", _domain(), str(plist))
    except LaunchctlTimeout as exc:
        return str(exc)
    if done.returncode == 0:
        return None
    return (
        f"launchctl bootstrap {plist} exited {done.returncode}: "
        f"{(done.stderr or done.stdout or '').strip()}"
    )


@dataclass(frozen=True)
class AgentSwitch:
    """A reinstall in flight (#2870), journalled beside the plist
    (:attr:`journal`) before the plist is replaced: whether the previous job
    was loaded, and whether it has been booted out for the new one. The
    previous plist's bytes are kept beside it (:attr:`saved`), so a fresh
    process can put them back."""

    plist: Path
    loaded: bool
    booted_out: bool = False

    @property
    def label(self) -> str:
        return self.plist.name.removesuffix(".plist")

    @property
    def saved(self) -> Path:
        return self.plist.with_name(f"{self.plist.name}.previous")

    @property
    def journal(self) -> Path:
        return _journal(self.plist)

    def encode(self) -> str:
        return json.dumps(
            {
                "plist": str(self.plist),
                "loaded": self.loaded,
                "booted_out": self.booted_out,
            }
        )

    @classmethod
    def parse(cls, raw: str, source: Path) -> AgentSwitch:
        try:
            data = json.loads(raw)
            return cls(
                Path(data["plist"]), bool(data["loaded"]), bool(data["booted_out"])
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DriveStateError(
                f"{source} holds an unreadable record: {exc!r}"
            ) from exc


def _journal(plist: Path) -> Path:
    return plist.with_name(f".{plist.name.removesuffix('.plist')}.switch")


def _note_switch(switch: AgentSwitch) -> None:
    _write_new(switch.journal, switch.encode().encode(), 0o600)


def _begin_switch(plist: Path, previous: bytes, loaded: bool) -> AgentSwitch:
    """Keep the previous plist beside it (0600) and journal the switch, both
    before the plist is replaced."""
    switch = AgentSwitch(plist, loaded)
    _write_new(switch.saved, previous, 0o600)
    try:
        _note_switch(switch)
    except BaseException:
        switch.saved.unlink(missing_ok=True)
        raise
    return switch


def _end_switch(switch: AgentSwitch) -> None:
    switch.journal.unlink(missing_ok=True)
    switch.saved.unlink(missing_ok=True)


def end_switch(label: str, home: Path | None = None) -> None:
    """Forget any reinstall in flight for ``label``: once its plist is
    removed there is no previous agent to put back."""
    _end_switch(AgentSwitch(agent_plist(label, home), loaded=False))


def _roll_back(switch: AgentSwitch, previous: bytes) -> str:
    """Put the previous plist back, and its job when it was loaded; what
    became of it. Once the previous job was booted out, whatever launchd
    holds is the replacement (a refused or timed-out bootstrap can still
    register it), so it is booted out before the previous bytes go back. The
    switch is settled only once launchd holds the previous job's own
    definition (or, when it was not loaded, none)."""
    label = switch.label
    if switch.booted_out:
        _bootout(label)
    _write_new(switch.plist, previous, 0o644)
    if _loaded(label) and not (
        switch.loaded and _mismatch(label, switch.plist) is None
    ):
        _bootout(label)
    if not switch.loaded:
        _end_switch(switch)
        return "the previous plist is restored"
    try:
        return _restore_job(switch)
    except (LaunchdUnanswered, LaunchctlTimeout) as exc:
        # launchd took the restore but did not say what it holds: whatever
        # it registered stays, and so do the journal and saved plist.
        return (
            f"the previous plist is restored, but kew could not confirm what "
            f"launchd holds ({exc}); the next `kew drive install` or `status` "
            "retries it"
        )


def _restore_job(switch: AgentSwitch) -> str:
    """Load the previous plist :func:`_roll_back` put back, unless launchd
    already holds it; what became of it. The switch is settled only once
    launchd holds the previous job's own definition."""
    label = switch.label
    if not _loaded(label):
        refused = _bootstrap(switch.plist)
        if refused is not None and _mismatch(label, switch.plist) is not None:
            # Neither job runs: the journal and saved plist stay, so the
            # next `kew drive install` or `status` retries the restore.
            _bootout(label)
            return (
                f"the previous plist is restored but did not load either "
                f"({refused}); the next `kew drive install` or `status` retries it"
            )
    mismatch = _mismatch(label, switch.plist)
    if mismatch is not None:
        return (
            f"the previous plist is restored, but {mismatch}; the next "
            "`kew drive install`, `status` or `uninstall` settles it"
        )
    _end_switch(switch)
    return "the previous agent is restored and loaded"


def _switch(store: AgentStore, agent: InstalledAgent, data: bytes) -> InstalledAgent:
    """Replace the plist kew wrote with ``data`` and load it. The previous
    job runs until it must make way for the new one's bootstrap; a refusal,
    a timeout, a failure, a loaded job that is not the new plist's, or (from
    a fresh process, :func:`recover_switch`) a kill puts the previous plist
    and job back."""
    previous = agent.plist.read_bytes()
    switch = _begin_switch(agent.plist, previous, _loaded(agent.label))
    try:
        _write_new(agent.plist, data, 0o644)
        record_agent(store, agent)
        # launchd keeps a loaded definition until it is booted out.
        _bootout(agent.label)
        switch = replace(switch, booted_out=True)
        _note_switch(switch)
        refused = _bootstrap(agent.plist)
    except LaunchctlStillRunning:
        raise  # rolled back only once that launchctl has exited
    except BaseException:
        _roll_back(switch, previous)
        raise
    # The switch is kept until launchd is seen holding the plist's own job.
    if refused is None:
        refused = _mismatch(agent.label, agent.plist)
    if refused is not None:
        raise RuntimeError(f"{refused}; {_roll_back(switch, previous)}")
    _end_switch(switch)
    return agent


def write_and_load(
    spec: AgentSpec,
    store: AgentStore,
    recorded: InstalledAgent | None = None,
    home: Path | None = None,
) -> InstalledAgent:
    """Write the plist, record it in ``store``, and (re)load it into this
    user's launchd domain.

    The install is recorded before it is loaded, so a load launchd refuses
    leaves an agent ``kew drive status`` reports as not loaded rather than one
    nothing knows of; an install killed before it records is still found at
    its plist (:func:`known_agent`). Replacing a plist kew wrote goes through
    :func:`_switch`, so neither a refusal nor a kill loses the working agent
    (#2870). Raises ``AgentRefused`` before touching anything it does not own,
    and ``RuntimeError`` naming launchctl's own words.
    """
    data = spec.render()
    plist = _agents_dir(home) / f"{spec.label}.plist"
    replacing = _ours_to_replace(plist, spec, recorded)
    agent = InstalledAgent(spec.label, plist)
    if replacing and plist.exists():
        return _switch(store, agent, data)
    _write_new(plist, data, 0o644)
    record_agent(store, agent)
    if replacing:
        _bootout(spec.label)
    refused = _bootstrap(plist)
    if refused is not None:
        raise RuntimeError(refused)
    return agent


def pending_switch(label: str, home: Path | None = None) -> AgentSwitch | None:
    """The reinstall a killed install left in flight for ``label``; None when
    none did."""
    journal = _journal(agent_plist(label, home))
    if not os.path.lexists(journal):
        return None
    return AgentSwitch.parse(journal.read_text(encoding="utf-8"), journal)


def _keep_loaded(switch: AgentSwitch) -> str:
    """Settle a switch whose plist launchd already runs: the new agent, or the
    previous one when an earlier restore loaded it but could not confirm it
    (#2955)."""
    saved = switch.saved
    restored = saved.is_file() and saved.read_bytes() == switch.plist.read_bytes()
    _end_switch(switch)
    if restored:
        return "an earlier restore had loaded the previous agent; it stays"
    return "an interrupted reinstall had loaded the new agent; it stays"


def recover_switch(project: Path, home: Path | None = None) -> str | None:
    """Settle a reinstall a killed ``kew drive install`` left in flight
    (#2870); what became of it, or None when there was none. A new agent is
    kept when launchd holds the job its plist defines; otherwise the previous
    plist goes back, and its job when it was loaded. The caller holds
    :func:`agent_lock`, so the install that journalled the switch is not
    still running it."""
    label = agent_label(project)
    switch = pending_switch(label, home)
    if switch is None or not launchd_available():
        return None
    if switch.plist != agent_plist(label, home):
        raise AgentRefused(
            f"the interrupted reinstall names {switch.plist}, not this "
            f"project's {agent_plist(label, home)}"
        )
    if switch.booted_out and _mismatch(label, switch.plist) is None:
        return _keep_loaded(switch)
    saved = switch.saved
    if (
        saved.is_symlink()
        or not saved.is_file()
        or foreign_plist(saved, label, project) is not None
    ):
        switch.journal.unlink(missing_ok=True)
        return (
            f"an interrupted reinstall left no previous plist kew wrote at "
            f"{saved}; the plist in place stays"
        )
    outcome = _roll_back(switch, saved.read_bytes())
    return f"an interrupted reinstall was rolled back: {outcome}"


def discovered_agent(project: Path, home: Path | None = None) -> InstalledAgent | None:
    """The agent kew writes for ``project``, found at its plist path when the
    ledger records none — an install killed between writing the plist and
    recording it (#2870) — or None when no plist there is kew's, marked, for
    this project (:func:`foreign_plist`)."""
    label = agent_label(project)
    plist = agent_plist(label, home)
    if plist.is_symlink() or not plist.is_file():
        return None
    if foreign_plist(plist, label, project, marked=True) is not None:
        return None
    return InstalledAgent(label, plist)


def known_agent(
    store: AgentStore, project: Path, home: Path | None = None
) -> tuple[InstalledAgent | None, bool]:
    """The install the ledger records, else the one found on disk; and
    whether the ledger records it."""
    agent = recorded_agent(store)
    if agent is not None:
        return agent, True
    return discovered_agent(project, home), False


def check_owned(
    agent: InstalledAgent,
    project: Path,
    home: Path | None = None,
    *,
    marked: bool = False,
) -> None:
    """Refuse an install that is not the agent kew writes for ``project``:
    another label, another path, or a plist kew did not write for it (or,
    when ``marked``, one without kew's mark)."""
    label = agent_label(project)
    if agent.label != label:
        raise AgentRefused(
            f"the recorded agent {agent.label} is not this project's ({label})"
        )
    if agent.plist != agent_plist(label, home):
        raise AgentRefused(
            f"the recorded plist {agent.plist} is not {agent_plist(label, home)}"
        )
    if agent.plist.is_symlink():
        raise AgentRefused(f"{agent.plist} is a symlink; kew will not remove it")
    foreign = (
        foreign_plist(agent.plist, label, project, marked=marked)
        if agent.plist.exists()
        else None
    )
    if foreign is not None:
        raise AgentRefused(f"{agent.plist} is not the agent kew writes ({foreign})")


def unload_and_remove(
    agent: InstalledAgent,
    project: Path,
    home: Path | None = None,
    *,
    recorded: bool = True,
) -> None:
    """Unload the agent (a running drive under it stops) and delete its plist,
    once :func:`check_owned` says both are kew's — an agent the ledger does not
    record only with kew's mark. The plist is checked again after the unload,
    immediately before it is deleted."""
    check_owned(agent, project, home, marked=not recorded)
    _bootout(agent.label)
    check_owned(agent, project, home, marked=not recorded)
    agent.plist.unlink(missing_ok=True)
    end_switch(agent.label, home)


def _disabled(label: str) -> bool:
    shown = _launchctl("print-disabled", _domain())
    out = shown.stdout or ""
    return re.search(rf'"{re.escape(label)}"\s*=>\s*(disabled|true)', out) is not None


def agent_state(agent: InstalledAgent) -> str:
    """What launchd makes of a recorded install: loaded, not loaded, disabled,
    missing (its plist is gone), or unchecked (no launchctl on this host)."""
    if not agent.plist.is_file():
        return AGENT_MISSING
    if not launchd_available():
        return AGENT_UNCHECKED
    if _disabled(agent.label):
        return AGENT_DISABLED
    if not _loaded(agent.label):
        return AGENT_NOT_LOADED
    return AGENT_LOADED
