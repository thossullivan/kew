"""Place one agent run on a host: the container runs there, the work comes back.

Epic #2338 node 5. Every dispatch, review and remediation container used to run
on the operator machine, and three concurrent agents plus a host-side test run
drove it to load 19 on 12 cores. Merge verifies already run on a Compute Engine
instance (:mod:`kew.merge_verify`); this is the same transport carrying an
*agent* instead of a pytest invocation.

The shape, and every part of it is deliberate:

* **Code goes up as a git bundle**, written from the run's own checkout and
  shipped by :meth:`~kew.hosts.provider.HostProvider.ship` — the identical
  transport ``kew verify`` uses, so there is one way code reaches a host.
  The ONE thing a bundle cannot carry rides beside it: the prompt handoff is
  deliberately untracked, so it travels on the session's stdin
  (:func:`prompt_prelude`) and is written into the instance's clone.
* **The container argv is kew's own.** :func:`build_agent_container_argv` is
  THE argv-build site; remote dispatch calls it with the *remote* checkout
  path and changes nothing else, so the caps, the egress allowlist, the
  read-only mounts and the credential-by-name forwarding are the local ones.
  A second argv builder here would be a second containment posture.
* **The event stream is relayed, not summarised.** The container's JSON stdout
  comes back line by line over the ssh channel, so the host's existing event
  parsing and startup acknowledgement see exactly what a local container's
  pipe gives them. The exit code rides the same explicit marker the host
  contract already uses: ``gcloud``'s own status cannot tell a failed job from
  a failed connection.
* **The work comes back as data.** The finished checkout is extracted ON the
  instance by the image's own ``kew-extract-artifact``, and the resulting
  artifact directory is returned to the operator machine, where
  :func:`kew.publication_artifact.read_artifact` consumes it exactly as it
  consumes a local container's. Nothing on an instance can push or merge.

The credential exception, which is the whole security surface of this module:
an instance holds no GitHub, Linear, Google or cloud credential, and
:mod:`kew.hosts.environment` refuses every credential-shaped *job* variable by
name. The ONE exception is the runner's own declared model credential, and it
is fenced four ways — only for a **dispatch** run kind, only names the runner
itself declares, never written to the instance's disk and never on a command
line (it travels on the ssh session's stdin, and on to the container's own
stdin — never ``docker run --env``, whose value the daemon stores in the
container's config on the instance's disk), and recorded in the launch audit
event **by name**.

Failure is explicit. An unreachable instance, a lost ssh session and a spot
pre-emption mid-run all end the run as :class:`RemoteRunInterrupted`, with its
own error category, never as a silent success — and the operator machine's
checkout is only ever read.

A launch the instance's own daemon refuses is explicit as well. Both of the
prerequisites that launch needs are probed ON the instance before the agent is
launched — the pinned image (:func:`ensure_agent_image`) and the egress network
the argv names (:func:`ensure_agent_network`) — and a refusal that happens
anyway ends the placement naming the daemon's own reason, read off the
session's stderr, rather than as a bare exit 125 with an empty event stream.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import ceil
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.credential_surface import exception_text, redact_credentials
from kew.fleet_placement import (
    abandoned_after_seconds,
    instance_is_occupied,
    record_placement,
)
from kew.hosts.environment import credential_shape, refuse_unusable_name
from kew.hosts.errors import CredentialVariableRefused, HostError, HostTimeout
from kew.hosts.gcp import (
    EXIT_MARKER,
    SHALLOW_REF_PREFIX,
    GcpProvider,
    marker_command,
    remote_session_environment,
)
from kew.hosts.provider import HostProvider, Job, JobResult
from kew.image_inputs import AGENT_IMAGE_INPUTS_LABEL

logger = logging.getLogger(__name__)

#: The transient ``systemd`` unit that removes a placed container at its
#: deadline is this prefix and the container's name (#2550).
REAPER_UNIT_PREFIX = "kew-reap-"

#: How far past its operator-side ceiling a placed container may run before the
#: instance removes it: room for the operator's own stop, which keeps the
#: verdict, to go first.
REAPER_GRACE_SECONDS = 300.0

#: The deadline a placed run with no ceiling of its own still gets.
REMOTE_DEADLINE_SECONDS = 4 * 3600.0

#: A full commit id, SHA-1 or SHA-256: the only base a shallow ship can cut at.
_GIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: The ONE run kind a model credential may reach. Review and remediation runs
#: are dispatches of a sort, but the owner decision names dispatch, and a rule
#: that admits "dispatch-like" admits whatever the next caller calls itself.
DISPATCH_RUN_KIND = "dispatch"

#: The audit event one remote launch writes. Its payload names the host, the
#: instance and the credential NAMES — never a value.
REMOTE_LAUNCH_EVENT = "dispatch_remote_launch"

#: What the instance's extraction writes, beside the shipped checkout.
REMOTE_ARTIFACT_DIR = "artifact"

#: Bounds for the plumbing around the agent: probing the image, extracting the
#: artifact, reading it back, stopping a container a lost session left behind.
#: The agent's own ceiling is the caller's.
IMAGE_PROBE_TIMEOUT = 120.0
IMAGE_BUILD_TIMEOUT = 3600.0
EXTRACT_TIMEOUT = 900.0
FETCH_TIMEOUT = 900.0
CONTAINER_STOP_TIMEOUT = 120.0

#: How many of the session's last stderr lines are kept as a diagnostic. ssh
#: writes its own failures there and so does the remote shell, and that text is
#: usually the only explanation an interruption has — but a step that floods
#: the pipe must not grow this process's memory, so only the tail is kept.
STDERR_TAIL_LINES = 40

#: How much of that tail is retained in a message that reports it. The tail is
#: text from another machine, and a message carrying all of it is a message
#: nothing reads; the same bound :mod:`kew.sandbox` keeps a build tail to.
STDERR_TAIL_CHARS = 2000

#: Networks every container runtime already has, so a probe for one would ask
#: the daemon about something that cannot be missing: ``none`` is the
#: extraction container's, and the rest are the runtime's own defaults.
BUILT_IN_NETWORKS = frozenset({"none", "host", "bridge", "default"})

#: How long a finished session waits for its stderr drain to reach EOF. Both
#: pipes close when ssh exits, so the drain is normally already there; a
#: session that somehow holds one open must not hold the relay instead.
STDERR_SETTLE_SECONDS = 5.0

#: What the remote prelude prints on **stdout** when the session's stdin ended
#: before it had read every handoff line it was told to expect. A LINE and not
#: an exit status, because no status can carry this: ``agent-entrypoint.sh``
#: exits 78 itself when the agent executable is missing, and a container
#: failure read as a failed handoff is retried as an interruption instead of
#: failed. Only kew's own prelude prints this, and it prints BEFORE any
#: container is launched, so nothing the agent writes later is read as it.
HANDOFF_FAILURE_MARKER = "KEW_HOST_HANDOFF_FAILED"

#: The status the prelude exits with after printing that marker. It makes the
#: prelude's own exit non-zero and nothing else reads it — the marker above is
#: the signal, and it is read before any exit code the session reports.
HANDOFF_FAILURE_EXIT = 78

#: The shell variable the prelude reads one handoff line into before using it.
#: Never a file, never an argument.
_HANDOFF_VARIABLE = "__kew_handoff"

#: Printed, then exited: what every handoff step does when its line is absent.
_HANDOFF_FAILED = (
    f"{{ printf '%s\\n' {HANDOFF_FAILURE_MARKER}; exit {HANDOFF_FAILURE_EXIT}; }}"
)

#: The read that takes exactly ONE base64 line off the session's stdin. Shared
#: by every handoff step, so the prompt and the credential fail the same
#: recognisable way rather than two ways a reader has to learn separately.
_HANDOFF_READ = f"IFS= read -r {_HANDOFF_VARIABLE} || {_HANDOFF_FAILED}; "

#: Dropped once the handoff has been consumed, by every prelude that reads one.
_HANDOFF_DONE = f"unset {_HANDOFF_VARIABLE}; "

#: The marker line the host contract's wrapper prints after the job. Anchored
#: at both ends: a line the agent itself emitted that merely *contains* the
#: marker is an event line, not an outcome.
_MARKER_LINE = re.compile(rf"\A{EXIT_MARKER}=(-?\d+)\Z")

#: Errors that closing a pipe onto a child that has already gone raises.
_DEAD_PIPE = (BrokenPipeError, OSError, ValueError)

#: A full content-addressed image ID as a runtime prints it. The same shape
#: :func:`kew.sandbox._read_inspected_id` accepts for a local inspect, because
#: the attestation it feeds cannot tell the two machines apart: a prefix, a
#: tag or a partial digest is not an immutable boundary on either of them.
_REMOTE_IMAGE_ID = re.compile(r"\bsha256:([0-9a-f]{64})\b")

#: What the instance's image probe prints: the ID, then the image's inputs
#: label behind a marker, so the label is read off the same answer as the ID.
#: Each lookup is guarded by ``with``: Docker 29 fails a bare
#: ``index .Config.Labels`` on an image with no ``Labels`` key at all (exit 1,
#: which would read as an absent image), and prints ``<no value>`` for a key
#: missing from labels the image does have. Guarded, an image without the
#: label prints nothing after ``inputs=``.
_REMOTE_IMAGE_PROBE_FORMAT = (
    '{{.Id}} inputs={{with index .Config "Labels"}}'
    f'{{{{with index . "{AGENT_IMAGE_INPUTS_LABEL}"}}}}{{{{.}}}}{{{{end}}}}'
    "{{end}}"
)
_REMOTE_IMAGE_PROBE = re.compile(r"\bsha256:([0-9a-f]{64}) inputs=(<no value>|\S*)")
#: What a runtime prints for a label it does not have. Read as no label, never
#: as the label's value, whichever format produced it.
_NO_LABEL_ANSWERS = frozenset({"", "<no value>"})


class AuditLedger(Protocol):
    """The one store capability a remote launch needs: one chained append."""

    def append_audit_event(self, **kwargs: Any) -> int | None: ...


class RemoteDispatchError(HostError):
    """A remote placement could not be carried out."""


class RemoteRunInterrupted(RemoteDispatchError):
    """The run ended without an outcome: the host or its session went away.

    Its own category on purpose. An unreachable instance, a dropped ssh
    session and a spot pre-emption are the three ways a remote agent stops
    without saying anything, and all three are *interruptions* — the run did
    not fail its task, it never finished one. Recording them as a failure
    would spend a retry budget on a verdict no agent reached, and recording
    them as a success would publish nothing while claiming work was done.
    """

    #: Recorded on the run row, so a remote interruption is distinguishable
    #: from a local container that exited non-zero.
    error_category = "remote_host_interrupted"

    def __init__(self, message: str, *, relayed: Sequence[str] = ()) -> None:
        super().__init__(message)
        #: The event lines that DID arrive before the session went away. Kept
        #: because they are the only record of how far the agent got.
        self.relayed: tuple[str, ...] = tuple(relayed)


@dataclass(frozen=True)
class ModelCredential:
    """One model credential bound for a remote container's environment.

    The name is evidence and is recorded; the value is neither compared nor
    repr'd, so no accidental log line, run row or ledger payload can carry it.
    """

    name: str
    value: str = field(repr=False, compare=False)

    def __str__(self) -> str:
        """The NAME. A credential that prints itself is a credential leaked."""
        return self.name


@dataclass(frozen=True)
class RemoteSite:
    """What the shipped instance says about itself, read once per placement.

    The container's mounts and ``--user`` are absolute paths and real ids ON
    THE INSTANCE, and this machine knows neither: the ssh login's home differs
    from the operator's, and ``Path.absolute()`` here would resolve a relative
    remote path against the wrong machine's working directory.
    """

    #: The shipped checkout, as an absolute path on the instance.
    checkout: str

    #: The ssh login's uid and gid, so the artifact is written for the login
    #: that has to read it back rather than for root.
    uid: int
    gid: int

    @property
    def artifact(self) -> str:
        """Where extraction writes on the instance: beside the checkout.

        A plain absolute path, normalised here rather than left for the remote
        shell: it becomes a bind-mount source, and a runtime takes a relative
        or ``..``-bearing mount source for a named volume.
        """
        return str(PurePosixPath(self.checkout).parent / REMOTE_ARTIFACT_DIR)


@dataclass(frozen=True)
class RemoteImage:
    """The bytes the instance will launch, and whether it had to build them.

    ``image_id`` is read off the INSTANCE's own image store, which is the only
    machine whose answer describes what runs: the operator's immutable ID names
    bytes in a different store, and the tag they share names inputs rather than
    content. A placement attests this ID, so a probe that cannot produce one
    refuses the placement rather than letting the attestation fall back to the
    tag (:func:`ensure_agent_image`).
    """

    image_id: str
    built: bool


@dataclass(frozen=True)
class RemoteLaunch:
    """The attested facts of one remote launch. Names only, never values."""

    host: str
    instance: str
    image: str
    runner: str
    credential_names: tuple[str, ...] = ()
    image_built: bool = False
    #: The immutable ID the INSTANCE resolved :attr:`image` to. Empty only for
    #: a launch record built by hand; every placement resolves one.
    image_id: str = ""

    def audit_payload(self) -> dict[str, Any]:
        """What the launch audit event records about where this run went."""
        return {
            "host": self.host,
            "instance": self.instance,
            "image": self.image,
            "runner": self.runner,
            # By NAME. The acceptance asks for the credential the container
            # received, and the name is the whole of what may be recorded.
            "credentials": list(self.credential_names),
            "image_built": self.image_built,
            # The instance's own resolution of the tag, so the event names the
            # bytes that ran and not the bytes this machine happens to hold.
            "image_id": self.image_id,
        }


@dataclass(frozen=True)
class RemoteDispatchResult:
    """What a finished remote placement hands back, as data."""

    launch: RemoteLaunch
    exit_code: int
    relayed: tuple[str, ...]
    artifact_dir: Path

    #: The tail of what the session wrote to stderr, redacted and bounded. A
    #: container that exits non-zero without relaying one event line put its
    #: whole explanation there, and an exit code alone cannot carry it.
    stderr: str = ""

    #: Why the instance's idle stop could not be armed when the placement let
    #: the host go, or empty when it was armed or rightly left to another run.
    #: The run's verdict stands either way; this is the VM that may still be
    #: billing, carried to the caller rather than left in a log line (#2627).
    release_error: str = ""

    @property
    def ok(self) -> bool:
        """True iff the remote container exited zero."""
        return self.exit_code == 0


def permit_model_credentials(
    requested: Mapping[str, str], *, runner: str, run_kind: str
) -> tuple[ModelCredential, ...]:
    """The credentials that may reach *runner*'s container on a remote host.

    Four fences, and a refusal names which one it hit:

    * Nothing at all outside a **dispatch** run kind. A review or remediation
      job on a VM gets no credential, so the exception cannot widen by a
      caller renaming what it is doing.
    * Only names the runner itself **declares** (``runner_credential_env_vars``
      — the single runner→credential table). A GitHub-shaped name is refused
      for the same job that a model credential passes, because it is not in
      the declaration, and so is another runner's model credential.
    * Every name is checked as a *name* first: it is interpolated into the
      remote shell's prelude, where — unlike a value — it cannot be quoted.
    * An empty value is refused rather than exported blank: a container that
      receives the variable set to nothing fails authentication deep inside
      the agent instead of here, where the operator can see why.
    """
    if not requested:
        return ()
    if run_kind.strip() != DISPATCH_RUN_KIND:
        name = sorted(requested)[0]
        raise CredentialVariableRefused(
            name,
            f"the run kind is {run_kind!r} and only a {DISPATCH_RUN_KIND!r} run "
            "may carry the runner's model credential to a host",
        )
    from kew.sandbox import runner_credential_env_vars

    declared = frozenset(runner_credential_env_vars(runner))
    permitted: list[ModelCredential] = []
    for name in sorted(requested):
        refuse_unusable_name(name)
        if name not in declared:
            raise CredentialVariableRefused(name, _not_declared(name, runner, declared))
        if not requested[name]:
            raise CredentialVariableRefused(
                name,
                "it was requested with an empty value, which authenticates nothing",
            )
        permitted.append(ModelCredential(name=name, value=requested[name]))
    return tuple(permitted)


def _not_declared(name: str, runner: str, declared: frozenset[str]) -> str:
    """Why *name* is not a credential *runner*'s container may receive."""
    shape = credential_shape(name)
    known = ", ".join(sorted(declared)) or "none"
    return f"{runner} declares no such credential (it declares: {known})" + (
        f" and {shape}" if shape else ""
    )


def bundle_checkout(checkout_dir: Path, workspace: Path, *, base_sha: str) -> Path:
    """Bundle the run's checkout into *workspace*: *base_sha* up to ``HEAD`` only.

    The operator's checkout is only ever READ. What ships is the commits the
    secret scan read and the base the instance extracts against, with their
    trees (#2550, #2569), and nothing else. A depth-limited fetch could not
    promise that — a merge whose other parent forked behind the base reaches
    commits older than the base within any depth that reaches the base
    itself — so the pack is cut from that object list
    (:func:`_shipped_objects`) into a throwaway repository, and the commits
    whose parents were left out travel as refs under
    :data:`~kew.hosts.gcp.SHALLOW_REF_PREFIX` so the clone on the instance
    can mark itself shallow (:func:`~kew.hosts.gcp.clone_command`).

    ``HEAD`` is resolved ONCE, and the scan, the object list and the bundle
    all name that commit (#2569): a checkout whose ``HEAD`` moved between the
    scan and the pack would otherwise ship a commit nothing scanned.

    ``HEAD`` is named explicitly in the bundle because ``git bundle create``
    writes a ``HEAD`` entry only for a ref it is handed by name, and a bundle
    without one clones into an empty working tree — the same scar
    :func:`kew.merge_verify.write_bundle` carries.

    A bundle an earlier ship left in *workspace* — a ship killed before its
    caller removed the workspace — is deleted before anything else, so a
    refused ship can never leave the previous run's bundle looking like its own.
    """
    source = Path(checkout_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    bundle = workspace / "repo.bundle"
    bundle.unlink(missing_ok=True)
    if not (source / ".git").exists():
        raise RemoteDispatchError(
            f"cannot ship {source}: it is not a git checkout, so there is "
            "nothing to bundle"
        )
    if subprocess_utils.ensure_tool("git") is None:
        raise RemoteDispatchError(
            "a run cannot be placed on a host without git on PATH: the"
            " checkout travels as a git bundle"
        )
    head = _shipped_head(source, base_sha)
    objects = _shipped_objects(source, base_sha, head)
    _scan_shipped(source, base_sha, head, objects)
    shallow = workspace / "shallow-ship"
    shutil.rmtree(shallow, ignore_errors=True)
    try:
        _git(shallow.parent, "init", "--quiet", str(shallow), what=source)
        _pack_into(source, shallow, objects)
        boundaries = _shipped_boundaries(source, base_sha, head)
        (shallow / ".git" / "shallow").write_text(
            "".join(f"{sha}\n" for sha in boundaries), encoding="ascii"
        )
        _git(shallow, "update-ref", "refs/heads/kew-ship", head, what=source)
        _git(shallow, "symbolic-ref", "HEAD", "refs/heads/kew-ship", what=source)
        refs = [f"{SHALLOW_REF_PREFIX}{index}" for index in range(len(boundaries))]
        for ref, sha in zip(refs, boundaries, strict=True):
            _git(shallow, "update-ref", ref, sha, what=source)
        _git(shallow, "bundle", "create", str(bundle), "HEAD", *refs, what=source)
    except BaseException:
        bundle.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(shallow, ignore_errors=True)
    return bundle


def _git(cwd: Path, *args: str, what: Path) -> str:
    """Run one git command for the bundle, or refuse naming *what* is shipped."""
    try:
        result = subprocess_utils.run(["git", *args], cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteDispatchError(f"cannot bundle {what}: {exc}") from exc
    if result.returncode != 0:
        raise RemoteDispatchError(
            f"cannot bundle {what}: git {args[0]}: {(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip()


def _shipped_head(source: Path, base_sha: str) -> str:
    """The ONE commit this ship carries: ``HEAD``, resolved once, over *base_sha*.

    A base ``HEAD`` does not descend from has no bounded history to ship:
    shipping it would mean shipping everything, so it is refused.
    """
    if not _GIT_OID.fullmatch(base_sha):
        raise RemoteDispatchError(
            f"cannot ship {source}: the base {base_sha!r} is not a commit id"
        )
    head = _git(source, "rev-parse", "--verify", "HEAD^{commit}", what=source)
    if not _GIT_OID.fullmatch(head):
        raise RemoteDispatchError(f"cannot ship {source}: HEAD is not a commit")
    _git(source, "merge-base", "--is-ancestor", base_sha, head, what=source)
    return head


def _object_ids(listing: str) -> list[str]:
    """The object ids of a ``rev-list --objects`` listing, paths dropped."""
    return [line.split(" ", 1)[0] for line in listing.splitlines() if line]


def _shipped_objects(source: Path, base_sha: str, head: str) -> list[str]:
    """Every object the bundle may hold: the whole tree of every shipped commit.

    The shipped commits are ``base..head``'s and the base itself. Their trees
    are listed whole, not as ``rev-list --objects base..head`` lists them: that
    leaves out every object the base's history also reaches, and a side branch
    that forked behind the base and was merged in can still name a blob the
    base replaced — reachable only from behind the base, yet needed by a
    commit that travels, so ``git bundle create`` could not read it (#2569).

    What this adds to the scanned range is only what a shipped tree names and
    the base's history reaches — published already, as the scan assumes of
    the base. Commits behind the base, and objects no shipped tree names,
    never travel.
    """
    commits = _git(source, "rev-list", f"{base_sha}..{head}", what=source).split()
    return _commit_objects(source, [*commits, base_sha])


def _commit_objects(source: Path, commits: Sequence[str]) -> list[str]:
    """Each of *commits* and every object of its whole tree, deduplicated."""
    result = subprocess_utils.run_bytes(
        ["git", "rev-list", "--objects", "--no-walk", "--stdin"],
        stdin="".join(f"{oid}\n" for oid in commits).encode("ascii"),
        cwd=source,
    )
    if result.returncode != 0:
        raise RemoteDispatchError(
            f"cannot bundle {source}: git rev-list: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return list(dict.fromkeys(_object_ids(result.stdout.decode("utf-8", "replace"))))


def _pack_into(source: Path, shallow: Path, objects: Sequence[str]) -> None:
    """Pack exactly *objects* out of *source* into *shallow*'s object store.

    Written straight into the throwaway repository's pack directory: the
    operator's checkout is only read, and no object outside the list exists
    in the repository the bundle is cut from, so none can reach the bundle.
    """
    pack_base = shallow / ".git" / "objects" / "pack" / "pack"
    result = subprocess_utils.run_bytes(
        ["git", "pack-objects", "--quiet", str(pack_base)],
        stdin="".join(f"{oid}\n" for oid in objects).encode("ascii"),
        cwd=source,
    )
    if result.returncode != 0:
        raise RemoteDispatchError(
            f"cannot bundle {source}: git pack-objects: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )


def _shipped_boundaries(source: Path, base_sha: str, head: str) -> list[str]:
    """The shipped commits whose parents were not shipped: the shallow boundary.

    The base always, and any commit above it with a parent outside
    ``base..head`` — the first commit of a branch that forked behind the base
    and was merged in. Marking those shallow is what lets the instance's clone
    walk the history it was sent without asking for a parent it was not.
    """
    listing = _git(
        source, "rev-list", "--parents", f"{base_sha}..{head}", what=source
    ).splitlines()
    shipped = {line.split()[0] for line in listing if line} | {base_sha}
    boundaries = [base_sha]
    for line in listing:
        commit, *parents = line.split()
        if any(parent not in shipped for parent in parents):
            boundaries.append(commit)
    return boundaries


def _scan_shipped(
    source: Path, base_sha: str, head: str, objects: Sequence[str]
) -> None:
    """Refuse to ship objects that carry a credential this machine holds.

    The publication scan (:func:`kew.publication_scan.scan_output`), over
    EXACTLY the *objects* the bundle packs, less the base commit and its own
    tree — the published state a push's scan trusts too (#2569 review). Not
    the range a push of *head* would transfer: that leaves out everything
    the base's history reaches, and a side commit merged in from behind the
    base can name a blob the base removed, which the bundle packs.
    """
    from kew.publication_scan import (
        OutputScanError,
        OutputScanTarget,
        output_scan_values,
        scan_output,
    )

    trusted = frozenset(_commit_objects(source, [base_sha]))
    target = OutputScanTarget(
        repository=source,
        base_sha=base_sha,
        head_sha=head,
        objects=tuple(oid for oid in objects if oid not in trusted),
    )
    try:
        scan = scan_output(target, output_scan_values())
    except OutputScanError as exc:
        raise RemoteDispatchError(
            f"cannot ship {source}: the secret scan could not complete: {exc}"
        ) from exc
    if scan.verdict != "clean":
        raise RemoteDispatchError(
            f"refusing to ship {source} to a host: the secret scan found "
            f"{scan.describe()}"
        )


def read_prompt_handoff(checkout_dir: Path) -> str:
    """The prompt the local path wrote into *checkout_dir*, read to carry it up.

    ``git bundle create HEAD`` carries COMMITTED objects, and the prompt
    handoff is deliberately untracked — ``.git/info/exclude``, so no agent
    commits it into its own PR branch (#725) — so the one file the container's
    entrypoint must read is exactly the one the bundle cannot carry. Without
    it the entrypoint exits 64 on an instance kew has already paid to start
    and claimed, so a missing prompt refuses the placement HERE instead.
    """
    from kew.sandbox import PROMPT_FILENAME

    path = Path(checkout_dir) / PROMPT_FILENAME
    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RemoteDispatchError(
            f"cannot place a dispatch without its prompt handoff ({path}): "
            f"{exc}. A git bundle carries committed objects and that file is "
            "deliberately untracked, so it is read here and handed to the "
            "instance on the session's stdin"
        ) from exc
    if not prompt.strip():
        raise RemoteDispatchError(
            f"the prompt handoff {path} is empty, and an agent with no prompt "
            "does no work: the container's entrypoint would exit 64 on the "
            "instance, so the placement is refused here"
        )
    return prompt


def prompt_prelude(site: RemoteSite) -> str:
    """The remote shell that takes the prompt off stdin into the clone.

    It arrives the way the credential does — one base64 line on the session's
    stdin, never on a command line — because a dispatch prompt is longer than
    an argv is guaranteed to hold, and a review prompt embeds comment bodies
    anyone with access to the pull request can author.

    And the instance's clone gets the SAME worktree-local exclude the local
    path writes. ``git clone`` carries no ``.git/info/exclude``, and the
    artifact producer enumerates untracked work with ``--exclude-standard``,
    so without this the prompt kew just wrote would come back as the agent's
    work and be published into the branch — #725, on a different machine.

    The write is :func:`kew.sandbox.write_prompt_handoff`'s rule in shell,
    because it writes into a directory an agent can modify and a plain ``>``
    FOLLOWS a symlink: ``rm -f`` first (that removes a link, never its
    target), then ``set -C``, whose ``O_EXCL`` refuses a link planted in the
    gap rather than writing through it. The instance's clone is fresh from
    each ``ship()``, so this is depth and not the only barrier — but the
    barrier a reader can check is the one on the write itself.

    A step of this that fails is a failed handoff and says so: the container
    has not started, and a run that never started is an interruption rather
    than a verdict.
    """
    from kew.sandbox import PROMPT_FILENAME

    checkout = PurePosixPath(site.checkout)
    target = shlex.quote(str(checkout / PROMPT_FILENAME))
    info = shlex.quote(str(checkout / ".git" / "info"))
    exclude = shlex.quote(str(checkout / ".git" / "info" / "exclude"))
    write = (
        f"rm -f {target} && (umask 077; set -C; "
        f'printf %s "${_HANDOFF_VARIABLE}" | base64 -d > {target})'
    )
    ignore = f"printf '%s\\n' {shlex.quote(PROMPT_FILENAME)} >> {exclude}"
    return (
        _HANDOFF_READ
        + f"{{ mkdir -p {info} && {write} && {ignore}; }} || {_HANDOFF_FAILED}; "
        + _HANDOFF_DONE
    )


def prompt_handoff(prompt: str) -> str:
    """The prompt as the one base64 line :func:`prompt_prelude` reads first."""
    return base64.b64encode(prompt.encode("utf-8")).decode("ascii") + "\n"


def read_remote_site(provider: HostProvider) -> RemoteSite:
    """Ask the shipped instance for the absolute checkout path and login ids.

    One round trip through the host contract's own ``run()`` verb rather than
    a hand-built ssh call, so it is bounded, locked and marker-checked like
    every other remote step.
    """
    result = provider.run(
        Job(
            name="remote-site",
            command='printf "%s %s %s" "$(id -u)" "$(id -g)" "$PWD"',
            timeout_seconds=IMAGE_PROBE_TIMEOUT,
        )
    )
    fields = result.log.split()
    if not result.ok or len(fields) < 3:
        raise RemoteDispatchError(
            f"instance {provider.address} did not report its checkout path and "
            f"login ids, so the container's mounts cannot be addressed: "
            f"{result.log.strip()[:500]!r}"
        )
    uid, gid, checkout = fields[0], fields[1], fields[-1]
    if not uid.isdigit() or not gid.isdigit() or not checkout.startswith("/"):
        raise RemoteDispatchError(
            f"instance {provider.address} reported an unusable site "
            f"({uid!r}, {gid!r}, {checkout!r})"
        )
    return RemoteSite(checkout=checkout, uid=int(uid), gid=int(gid))


@dataclass(frozen=True)
class _InstanceImage:
    """What the instance's image store says about a tag: its ID and label."""

    image_id: str
    #: The image's :data:`~kew.image_inputs.AGENT_IMAGE_INPUTS_LABEL`, or
    #: ``None`` when it carries none.
    inputs_digest: str | None


def _inspect_remote_image(
    provider: HostProvider, *, image: str, runtime: str
) -> _InstanceImage | None:
    """The immutable ID the instance resolves *image* to, and the inputs label
    that image carries, or None when absent.

    ``--format`` rather than the full inspect payload: the job's log is
    combined stdout and stderr, so a JSON document would have to be separated
    from whatever ssh wrote beside it, while an ID has a shape
    (:data:`_REMOTE_IMAGE_ID`) that can simply be recognised. Exactly one
    distinct ID is required — two would mean the log held an answer about some
    other image, and a boundary attested from an ambiguous probe attests
    nothing. The label is read off the same answer
    (:data:`_REMOTE_IMAGE_PROBE_FORMAT`); an answer without one is an image
    that carries none.

    ``None`` means the image is ABSENT, which is the one answer a build can
    fix. A probe that succeeded and still produced no single ID is a daemon
    saying something kew cannot attest, so it refuses here rather than paying
    for an hour-long rebuild that would be probed exactly the same way.
    """
    probe = provider.run(
        Job(
            name="image-probe",
            command=(
                f"{shlex.quote(runtime)} image inspect --format "
                f"{shlex.quote(_REMOTE_IMAGE_PROBE_FORMAT)} {shlex.quote(image)}"
            ),
            timeout_seconds=IMAGE_PROBE_TIMEOUT,
        )
    )
    if not probe.ok:
        return None
    found = set(_REMOTE_IMAGE_ID.findall(probe.log))
    if len(found) != 1:
        raise RemoteDispatchError(
            f"instance {provider.address} holds image {image} but named "
            f"{len(found)} immutable IDs for it, so what a container there "
            "would run cannot be attested and the placement is refused"
        )
    labels = {
        label
        for _id, label in _REMOTE_IMAGE_PROBE.findall(probe.log)
        if label not in _NO_LABEL_ANSWERS
    }
    return _InstanceImage(
        image_id=f"sha256:{found.pop()}",
        inputs_digest=labels.pop() if len(labels) == 1 else None,
    )


def _stale_pin(image: str, found: _InstanceImage) -> str | None:
    """Why the instance's *image* is not the build its pin tag names, or None.

    The rule is :func:`kew.sandbox.pinned_inputs_label_problem`, the one a
    local launch applies, so a placed launch and a local one agree on which
    image a pin tag may name.
    """
    from kew.sandbox import pinned_inputs_label_problem

    return pinned_inputs_label_problem(image, found.inputs_digest)


def ensure_agent_image(
    provider: HostProvider, *, image: str, runner: str, runtime: str
) -> RemoteImage:
    """Make the pinned agent image exist on the instance, and name its bytes.

    Probed by the digest-pinned tag the launch will name, because that is the
    only question that matters: nothing publishes a kew agent image, so a
    missing pin cannot be pulled and the instance builds it. What it builds
    from is THIS machine's packaged image inputs — the ``docker/`` context
    installed with kew, validated against its manifest and its input digest
    pin, and streamed to the instance's ``docker build -`` on the session's
    stdin (:func:`packaged_build_context`). Nothing in the shipped checkout is
    executed on the instance's host (#2569): a ``uv run kew sandbox build``
    there ran whatever the branch under review had made of kew, as the ssh
    login, beside the daemon. A build that fails refuses the placement here
    rather than letting ``docker run --pull=never`` fail later as a registry
    error.

    What comes back is the INSTANCE's immutable ID for that tag, not a bare
    "it is there" (#2526 review). The containment profile this placement
    attests has to name the bytes the agent runs, and the tag resolves against
    the instance's own image store: the operator machine's ID for the same tag
    names bytes in a store the container never reads, and a tag is not an
    immutable boundary on either machine. A probe that answers ``ok`` without
    producing one ID refuses the placement here, before the agent runs, rather
    than leaving the attestation to describe a different image.

    The pin tag only names inputs, so the image it names on the instance must
    also carry the inputs label those inputs give
    (:func:`kew.sandbox.pinned_inputs_label_problem`). One that does not — an
    image built before the label existed, or a retag — is stale: kew built
    the pin there and builds it again from its packaged inputs, the same as an
    absent one. A rebuild whose image still does not carry the label refuses
    the placement.
    """
    found = _inspect_remote_image(provider, image=image, runtime=runtime)
    if found is not None and _stale_pin(image, found) is None:
        return RemoteImage(image_id=found.image_id, built=False)
    dockerfile, context, label = packaged_build_context(runner, image)
    build = relay_remote_session(
        provider,
        image_build_command(runtime, image=image, dockerfile=dockerfile, label=label),
        handoff=context,
        timeout=IMAGE_BUILD_TIMEOUT,
    )
    if not build.ok:
        tail = "\n".join(line.rstrip("\n") for line in build.lines)[-2000:]
        raise RemoteDispatchError(
            f"instance {provider.address} has no usable image {image} and could "
            f"not build one for {runner} (exit {build.exit_code}): "
            f"{redact_credentials(tail)}{_because(build.stderr)}"
        )
    built = _inspect_remote_image(provider, image=image, runtime=runtime)
    if built is None:
        raise RemoteDispatchError(
            f"instance {provider.address} reported a successful build of "
            f"{image} for {runner} but still holds no such image, so the "
            "placement is refused rather than launched against nothing"
        )
    stale = _stale_pin(image, built)
    if stale is not None:
        raise RemoteDispatchError(
            f"instance {provider.address} rebuilt {image} for {runner}, but the "
            f"image it now holds {stale}, so the placement is refused"
        )
    return RemoteImage(image_id=built.image_id, built=True)


def packaged_build_context(runner: str, image: str) -> tuple[str, str, str]:
    """*runner*'s packaged image context as a base64 tar, its Dockerfile, and
    the inputs label an image built from it carries.

    The context is the one :func:`kew.sandbox.build_agent_image` builds from
    on this machine, through the same validation — the packaged manifest and
    the input digest — and *image* must be the tag those inputs pin: building
    them under any other tag would attest the pin's bytes under a name that
    does not describe them. Only ``docker/`` is packed, which is everything
    the packaged Dockerfiles copy. Base64 so the handoff is text lines. The
    label is read from the same validated context, inside the same refusal, so
    an unreadable context is a placement refused, never a stray exception.
    """
    from kew.image_inputs import inputs_label
    from kew.sandbox import (
        _validate_packaged_image_tag,
        packaged_agent_image_context,
        packaged_image_dockerfile,
    )

    try:
        context = packaged_agent_image_context()
        _validate_packaged_image_tag(context, runner, image)
        dockerfile = packaged_image_dockerfile(runner)
        label = inputs_label(context)
    except (ValueError, OSError) as exc:
        raise RemoteDispatchError(
            f"cannot build {image} for {runner} on a host from kew's packaged "
            f"image inputs: {exc}"
        ) from exc
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(context / "docker", arcname="docker", filter=_portable_member)
    encoded = base64.encodebytes(buffer.getvalue()).decode("ascii")
    return dockerfile.relative_to(context).as_posix(), encoded, label


def _portable_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """A context member owned by nobody in particular: the build sets owners."""
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def image_build_command(
    runtime: str, *, image: str, dockerfile: str, label: str
) -> str:
    """The remote build: the context off stdin, into the runtime's ``build -``.

    A PIPELINE under ``bash -c`` with ``pipefail``, for the reason
    :func:`_artifact_fetch_command` gives: a decode that failed must not be
    reported as the build's own status. The context never touches the
    instance's disk outside the daemon's own build cache. *label* is the
    image's inputs label, the one a launch of the pin tag reads back.
    """
    build = (
        f"{shlex.quote(runtime)} build -f {shlex.quote(dockerfile)} "
        f"--label {shlex.quote(label)} -t {shlex.quote(image)} -"
    )
    return f"bash -c {shlex.quote(f'set -o pipefail; base64 -d | {build}')}"


def container_network(argv: Sequence[str]) -> str | None:
    """The network *argv* attaches its container to, when kew has to ask for it.

    Read off the argv rather than re-derived from ``[sandbox] network``, so the
    network probed is the one the launch will name — the single-derivation rule
    :func:`launched_container` follows for the container's name — and the argv
    builder emits the flag and its value as two words, which is the one form
    read here. ``None`` when there is nothing to probe: ``network = "open"``
    omits the flag altogether, and a built-in network cannot be absent.
    """
    words = list(argv)
    if "--network" not in words[:-1]:
        return None
    name = words[words.index("--network") + 1]
    return None if name in BUILT_IN_NETWORKS else name


#: The metadata server's entry for the instance's DEFAULT service account.
#: Not the listing one level up: with no account attached, the listing
#: answers 200 with an empty body, while ``default/`` answers 404 with
#: ``Metadata-Flavor: Google`` (observed on kew-verify, 2026-10-09, an
#: instance with ``serviceAccounts: null``, which the listing probe refused).
#: Link-local and answered only on the instance itself. Addressed
#: by its fixed IP, never ``metadata.google.internal`` (#2569 review): a name
#: is whatever the instance's resolver says it is, and a resolver that points
#: it elsewhere could answer the probe with a forged "no account" 404.
_SERVICE_ACCOUNTS_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/"
)

#: Exit codes of :func:`_identity_probe`: an attached account, and an answer
#: the probe could not read.
_IDENTITY_ATTACHED = 3
_IDENTITY_UNKNOWN = 4

#: How long the identity probe may take: one request to a link-local server.
IDENTITY_PROBE_TIMEOUT = 60.0


#: The marker the probe prints after the response headers: the status curl
#: read, on a line of its own, so a header can never be mistaken for it.
_IDENTITY_STATUS = "KEW_METADATA_STATUS="


def _identity_probe() -> str:
    """The remote check that the instance has no cloud identity to lend.

    Exactly one answer is clean (#2569): a ``404`` that carries the metadata
    server's own ``Metadata-Flavor: Google`` response header — the server
    saying the instance has no default service account. Every other answer
    is not:

    * a ``200`` is the account's own entry, so an account is attached — an
      EMPTY ``200`` too, because only the server's refusal says "none";
    * any other status, or a ``404`` without that header, is something that is
      not the metadata server answering — a proxy's ``403``, a captive page —
      and kew cannot vouch for an instance it did not hear from;
    * no curl, no answer, a timeout: unknown, never clean.

    ``-q`` first, so no ``.curlrc`` on the instance can add an option;
    ``--noproxy '*'`` so no proxy variable routes the request anywhere but
    the link-local server. No ``-L``: a redirect is not the server's answer.
    """
    request = (
        "curl -q --noproxy '*' -s -m 5 -H 'Metadata-Flavor: Google' "
        f"-o /dev/null -D - -w '\\n{_IDENTITY_STATUS}%{{http_code}}\\n' "
        f"{_SERVICE_ACCOUNTS_URL}"
    )
    return (
        f"command -v curl >/dev/null 2>&1 || exit {_IDENTITY_UNKNOWN}; "
        f"answer=$({request}) || exit {_IDENTITY_UNKNOWN}; "
        f"status=$(printf '%s\\n' \"$answer\" | sed -n 's/^{_IDENTITY_STATUS}//p'); "
        "printf '%s\\n' \"$answer\" | tr -d '\\r' | "
        f"grep -qix 'metadata-flavor: google' || exit {_IDENTITY_UNKNOWN}; "
        f'case "$status" in 404) exit 0 ;; 200) exit {_IDENTITY_ATTACHED} ;; '
        f"*) exit {_IDENTITY_UNKNOWN} ;; esac"
    )


def refuse_instance_identity(provider: HostProvider) -> None:
    """Refuse to start a run on an instance that could hand out a cloud token.

    The boundary is that no cloud credential is on a VM (#2550). An instance
    with a service account attached serves a token to anything that asks its
    metadata server — including a container on its network — so the run is
    refused before any credential is handed over.

    Asked over a session of its own, NOT as a :meth:`HostProvider.run` job
    (#2569): a job runs inside the shipped checkout, and this question is
    asked before anything is shipped, so on a fresh instance the job's
    ``cd`` failed and every placement there was refused as unprovable.
    """
    outcome = relay_remote_session(
        provider,
        f"sh -c {shlex.quote(_identity_probe())}",
        timeout=IDENTITY_PROBE_TIMEOUT,
    )
    if outcome.ok:
        return
    reason = (
        "has a service account attached, so its metadata server hands a cloud "
        "token to anything on it"
        if outcome.exit_code == _IDENTITY_ATTACHED
        else f"could not prove it has no service account (exit {outcome.exit_code})"
    )
    raise RemoteDispatchError(
        f"refusing to place a run on instance {provider.address}: it {reason}. "
        "Remove the instance's service account (gcloud compute instances "
        "set-service-account --no-service-account --no-scopes) and retry"
    )


def ensure_agent_network(
    provider: HostProvider, *, argv: Sequence[str], runtime: str
) -> None:
    """Refuse the placement when the launch's network is absent on the instance.

    The second instance prerequisite beside the image, and one a placement
    never supplies for itself: the default ``[sandbox] network = "allowlist"``
    attaches ``kew-allowlist``, which the provisioning recipe creates as a
    plain bridge whose egress the operator manages (#2607) — but an instance
    provisioned before that, or by hand, may still lack it, and a custom name
    is never created. A daemon without it refuses ``docker run`` with exit 125
    before the agent writes a line.

    On the operator's own machine that refusal lands on their own terminal.
    Placed on an instance it arrived as an empty event stream, so the question
    is asked ahead of the launch — and ahead of an image build that may take an
    hour — and a missing network refuses the placement naming the command that
    creates one, which is the fix ``kew doctor`` prints locally (docs/hosts.md).

    A probe that did not answer "present" is not read as "absent" on its exit
    status alone: :func:`_network_refusal` asks what actually happened first.
    """
    name = container_network(argv)
    if name is None:
        return
    probe = provider.run(
        Job(
            name="network-probe",
            command=f"{shlex.quote(runtime)} network inspect {shlex.quote(name)}",
            timeout_seconds=IMAGE_PROBE_TIMEOUT,
        )
    )
    if probe.ok:
        return
    raise RemoteDispatchError(
        _network_refusal(provider, name=name, runtime=runtime, probe=probe)
    )


def _network_refusal(
    provider: HostProvider, *, name: str, runtime: str, probe: JobResult
) -> str:
    """Why the placement is refused, after asking why the probe did not answer.

    A non-zero ``network inspect`` is not proof the network is absent. An
    instance that cold-started answers the same way while its dockerd is still
    coming up, and so does a login that is not in the ``docker`` group — and
    since the network is now the FIRST runtime command of a placement, there is
    no later image probe or build whose log would have carried the daemon's own
    sentence. Reading every such failure as "create the network" sends an
    operator to provision something that already exists, or cannot be reached
    to be provisioned, and throws away the one line that says so.

    So the daemon is asked whether it is answering at all before it is blamed
    for a gap, and only an instance whose daemon answers is told to create a
    network. The probe's own output travels with the refusal either way,
    because on this path it is the whole of the diagnosis.
    """
    reached = provider.run(
        Job(
            name="daemon-probe",
            command=f"{shlex.quote(runtime)} info",
            timeout_seconds=IMAGE_PROBE_TIMEOUT,
        )
    )
    client = PurePosixPath(runtime).name
    said = probe.log.strip()[-2000:] or reached.log.strip()[-2000:]
    if not reached.ok:
        return (
            f"instance {provider.address} could not be asked whether its "
            f"{name!r} network exists, because {client} there reached no "
            f"daemon, so the placement is refused knowing nothing about the "
            f"network{_because(said)}"
        )
    return (
        f"instance {provider.address} has no {name!r} network, so its daemon "
        f"would refuse the agent container before it ran: create one there "
        f"({client} network create --driver bridge {name}) and restrict its "
        f"egress — kew installs no rules on it — or set "
        f'[sandbox] network = "open" to dispatch with full egress'
        f"{_because(said)}"
    )


#: The remote shell variable credential *index* waits in, still base64.
_CREDENTIAL_VARIABLE = "__kew_credential_{index}"

#: ``$0`` of the in-container handoff shell, so ``ps`` inside it names it.
_CONTAINER_HANDOFF_NAME = "kew-credential-handoff"

#: The shell the in-container handoff runs under. Every packaged agent image
#: is Debian-based, so it has one, and ``base64`` beside it.
_CONTAINER_SHELL = "/bin/sh"


def credential_prelude(credentials: Sequence[ModelCredential]) -> str:
    """The remote shell that takes each credential off stdin and HOLDS it.

    The value reaches the instance on the ssh session's **stdin** and lives
    only in the login shell's memory, still base64-encoded, in a variable
    that is never exported: it is never written to the instance's disk, never
    appears on a command line, and is never in the environment the container
    runtime's client reads. :func:`credential_launch` then pipes it to the
    container's own stdin.

    Base64 on the wire so the handoff is byte-exact for any value a provider
    might mint: ``read`` is line-oriented, and a credential is not required to
    be one line of printable text.
    """
    if not credentials:
        return ""
    steps = [
        _HANDOFF_READ
        + f'{_CREDENTIAL_VARIABLE.format(index=index)}="${_HANDOFF_VARIABLE}"; '
        for index, _credential in enumerate(credentials)
    ]
    return "".join(steps) + _HANDOFF_DONE


def credential_handoff(credentials: Sequence[ModelCredential]) -> str:
    """What is written to the ssh session's stdin: one base64 line per name.

    In the same order :func:`credential_prelude` reads them, because the
    prelude reads positionally — it never names a value.
    """
    return "".join(
        base64.b64encode(credential.value.encode("utf-8")).decode("ascii") + "\n"
        for credential in credentials
    )


def credential_launch(credentials: Sequence[ModelCredential]) -> str:
    """The pipe that hands the held credentials to the container's stdin.

    ``printf`` is a shell builtin, so the values are on no command line; the
    pipe is memory, so they are on no disk; and the daemon never sees them at
    all — ``docker run --env NAME`` made the daemon store ``NAME=value`` in
    the container's config on the instance's disk, where ``docker inspect``
    printed it back (#2569). The container's own handoff shell
    (:func:`container_handoff_script`) reads them in this order.
    """
    if not credentials:
        return ""
    held = " ".join(
        f'"${_CREDENTIAL_VARIABLE.format(index=index)}"'
        for index, _credential in enumerate(credentials)
    )
    return f"printf '%s\\n' {held} | "


def container_handoff_script(names: Sequence[str]) -> str:
    """The shell the container runs first: credentials off stdin, then the agent.

    Each line is decoded and exported into THIS process's environment, which
    the image's own entrypoint is then exec'd into — so the agent sees
    exactly the variables a local ``--env`` would have given it, and the
    container's config holds the names of nothing. Stdin is closed onto
    ``/dev/null`` for the entrypoint, which is what it had before: nothing but
    this handoff ever read it.

    A line that is missing prints the same handoff-failure marker the remote
    prelude prints and the agent never starts, so a cut session reads as an
    interruption rather than an agent that failed authentication.
    """
    steps = []
    for name in names:
        refuse_unusable_name(name)
        steps.append(
            _HANDOFF_READ
            + f'{name}="$(printf %s "${_HANDOFF_VARIABLE}" | base64 -d)" '
            + f"|| {_HANDOFF_FAILED}; export {name}; "
        )
    return "".join(steps) + _HANDOFF_DONE + 'exec "$@" </dev/null'


def credential_stdin_argv(
    argv: Sequence[str],
    *,
    image: str,
    command: Sequence[str],
    handed_over: Sequence[str],
    entrypoint: Sequence[str],
) -> list[str]:
    """*argv* taking its handed-over credentials on stdin, not from ``--env``.

    The handed-over names' ``-e NAME`` words are removed — that is the form
    the daemon resolves and STORES in the container's config. In their place
    the container is started interactive (``-i``, so the pipe from
    :func:`credential_launch` reaches it) with the handoff shell as its
    entrypoint, and the image's own *entrypoint* and the original command as
    that shell's arguments. Every other word — caps, mounts, network, the
    image itself — crosses unchanged.

    The image is found by POSITION — the word before the container *command*
    the argv builder appended — never by searching for its value (#2569
    review): a model name or flag equal to the image string would otherwise
    split the argv in the wrong place.

    An argv with nothing to hand over is returned as it is.
    """
    words = list(argv)
    if not handed_over:
        return words
    kept = frozenset(handed_over)
    options, command = _split_at_image(words, image=image, command=command)
    original = list(entrypoint)
    filtered: list[str] = []
    stream = iter(options)
    for word in stream:
        if word in ("-e", "--env"):
            name = next(stream, "")
            if name not in kept:
                filtered.extend((word, name))
        elif word == "--entrypoint":
            original = [next(stream, "")]
        else:
            filtered.append(word)
    if not original or not original[0]:
        raise RemoteDispatchError(
            f"the image {image!r} names no entrypoint, so the credential "
            "handoff has nothing to hand the agent on to"
        )
    return [
        *filtered,
        "-i",
        "--entrypoint",
        _CONTAINER_SHELL,
        image,
        "-c",
        container_handoff_script(list(handed_over)),
        _CONTAINER_HANDOFF_NAME,
        *original,
        *command,
    ]


def _split_at_image(
    words: Sequence[str], *, image: str, command: Sequence[str]
) -> tuple[list[str], list[str]]:
    """*words* split around the image: the run options, and the *command*.

    The argv builder ends every container argv with the image and then the
    command, so the image is the word ``len(command)`` from the end. Anything
    else is an argv this does not understand and is refused.
    """
    at = len(words) - len(command) - 1
    if at < 0 or words[at] != image or list(words[at + 1 :]) != list(command):
        raise RemoteDispatchError(
            f"the agent argv does not end with its image {image!r} and the "
            "runner's command, so the credential cannot be handed to the "
            "container"
        )
    return list(words[:at]), list(words[at + 1 :])


def withheld_prelude(names: Sequence[str]) -> str:
    """The remote shell that drops every name this launch did NOT hand over.

    A bare ``--env NAME`` makes the daemon read NAME from the shell that runs
    it, and on an instance that is the VM's login environment, not this
    machine's. :func:`placed_environment_argv` already keeps such a name off
    the argv; this is the other half, so a value the instance's own profile
    exports is gone from the shell before ``docker run`` could read it either
    way (#2526 review).
    """
    for name in names:
        refuse_unusable_name(name)
    return f"unset {' '.join(names)}; " if names else ""


def placed_environment_argv(
    argv: Sequence[str], *, handed_over: Sequence[str]
) -> list[str]:
    """*argv* importing from the remote shell ONLY the names handed over.

    The local builder forwards every name the runner declares, and every
    proxy name the ``inherit`` policy found, as a bare ``-e NAME``: on this
    machine the daemon reads each from the environment kew built for it. On an
    instance it reads them from the VM's login shell instead, so a declared
    name the operator had no value for — and an inherited proxy the operator's
    value of never crossed — would be filled in with whatever the instance
    holds, while the ``dispatch_remote_launch`` audit names only the handed-over
    credential (#2526 review). Here the argv and the audit agree:

    * a bare name that was handed over is kept;
    * a bare proxy name that was not is pinned blank (``-e NAME=``), exactly as
      the non-inherit policy pins it, so the instance's daemon or client config
      cannot inject one either;
    * any other bare name that was not is dropped.

    Assignments (``-e NAME=value``) are kew's own and cross unchanged.
    """
    from kew.sandbox import CONTROLLED_PROXY_NAMES

    kept = frozenset(handed_over)
    placed: list[str] = []
    words = iter(argv)
    for word in words:
        if word not in ("-e", "--env"):
            placed.append(word)
            continue
        name = next(words, "")
        if "=" in name or name in kept:
            placed.extend((word, name))
        elif name in CONTROLLED_PROXY_NAMES:
            placed.extend((word, f"{name}="))
    return placed


def withheld_names(runner: str, handed_over: Sequence[str]) -> list[str]:
    """Every declared and proxy name a placed launch must not import."""
    from kew.sandbox import CONTROLLED_PROXY_NAMES, runner_credential_env_vars

    kept = frozenset(handed_over)
    candidates = (*runner_credential_env_vars(runner), *CONTROLLED_PROXY_NAMES)
    return [name for name in dict.fromkeys(candidates) if name not in kept]


def host_side_paths_relocated(argv: Sequence[str], *, site: RemoteSite) -> list[str]:
    """*argv* with its HOST-side scratch paths pointed at the running host.

    Exactly one word needs this, and it is not a containment flag: the
    ``--cidfile`` the runtime writes the container id into is a path on
    whichever machine runs the container. The local builder roots it in this
    machine's temporary directory — on a Mac, a ``/var/folders/...`` path that
    does not exist on a Linux instance — so the file is re-rooted in the
    shipped workspace, keeping the name the local argv chose. Every other
    word, including every mount, cap and environment name, crosses unchanged.
    """
    relocated = list(argv)
    workspace = PurePosixPath(site.checkout).parent
    for index, word in enumerate(relocated[:-1]):
        if word == "--cidfile":
            name = PurePosixPath(relocated[index + 1]).name
            relocated[index + 1] = str(workspace / name)
    return relocated


def reaper_prelude(
    container: RemoteContainer | None, *, deadline_seconds: float
) -> str:
    """Arm the instance's own removal of *container* once its budget is spent.

    The operator side's ceiling and its cleanup both live in this process and
    on this ssh channel: an operator process that dies, or a session and a
    cleanup call that both fail, would leave a credential-bearing container
    running with nothing left to end it (#2550). The deadline therefore lives
    ON the instance, as a transient ``systemd`` timer named for the container
    (the run's lease on that name), armed before the container starts. It is
    one ``&&`` chain on its own; :func:`remote_agent_script` chains what
    follows it with ``&&`` too, so a deadline that could not be armed starts
    nothing.
    Any earlier timer under the same name is stopped and its failed state
    cleared first (``--collect`` unloads the removal even when it found nothing
    to remove), so a stale one left by a previous run of the same slug can
    neither remove this run's container early nor make the unit name refuse
    the new timer. The idle stop powers the instance off, which ends the container
    too, so neither stop can leave it running past its budget.

    A launch whose argv names no container cannot be reaped by name, so it is
    refused rather than started without a deadline.
    """
    if container is None:
        raise RemoteDispatchError(
            "the agent argv names no container, so the instance could not "
            "remove it at its deadline; a credential-bearing container is never "
            "started without one"
        )
    unit = _reaper_unit(container)
    seconds = max(1, ceil(deadline_seconds))
    return (
        f"{_disarm_reaper(unit)} && "
        f"sudo systemd-run --quiet --collect --on-active={seconds}s --unit={unit} "
        f"{_container_removal(container)}"
    )


def _reaper_unit(container: RemoteContainer) -> str:
    """The quoted name of the transient unit that reaps *container*."""
    return shlex.quote(f"{REAPER_UNIT_PREFIX}{container.name}")


def _container_removal(container: RemoteContainer) -> str:
    """The instance command that forgets *container* by name."""
    return f"{shlex.quote(container.runtime)} rm -f {shlex.quote(container.name)}"


def _disarm_reaper(unit: str) -> str:
    """Stop the reaper *unit* and clear its failed state; never fails itself.

    One group, so arming a new deadline and clearing a finished run's both
    spell the stop the same way.
    """
    return (
        f"{{ sudo systemctl stop {unit}.timer {unit}.service >/dev/null 2>&1; "
        f"sudo systemctl reset-failed {unit}.timer {unit}.service "
        ">/dev/null 2>&1 || true; }"
    )


def remote_deadline(timeout_seconds: float | None) -> float:
    """How long the instance lets a placed container live: its budget plus grace.

    The operator ceiling plus :data:`REAPER_GRACE_SECONDS`, so the operator's
    own stop — which keeps the verdict — always gets the first try. A run with
    no ceiling still gets :data:`REMOTE_DEADLINE_SECONDS`: no credential-bearing
    container is ever started without one.
    """
    budget = REMOTE_DEADLINE_SECONDS if timeout_seconds is None else timeout_seconds
    return budget + REAPER_GRACE_SECONDS


def remote_agent_command(
    argv: Sequence[str],
    *,
    site: RemoteSite,
    credentials: Sequence[ModelCredential],
    deadline_seconds: float,
    withheld: Sequence[str] = (),
) -> str:
    """The remote line that runs kew's own container argv on the instance.

    :func:`remote_agent_script` under its own ``sh -c`` (#2569 review): the
    login shell ssh starts may be an interactive-configured bash that read a
    ``.bashrc``, and an ``xtrace`` that file turned on (with ``BASH_XTRACEFD``
    pointing at a file) would write every expanded word of the handoff —
    the encoded credential included — to that file. A non-interactive ``sh``
    reads no rc file, and the script turns ``xtrace`` off before anything
    else in case the login shell exported it.
    """
    script = remote_agent_script(
        argv,
        site=site,
        credentials=credentials,
        deadline_seconds=deadline_seconds,
        withheld=withheld,
    )
    return f"sh -c {shlex.quote(script)}"


def remote_agent_script(
    argv: Sequence[str],
    *,
    site: RemoteSite,
    credentials: Sequence[ModelCredential],
    deadline_seconds: float,
    withheld: Sequence[str] = (),
) -> str:
    """The shell script that runs kew's own container argv on the instance.

    Quoted word for word: the argv is the one
    :func:`kew.dispatch_sandbox.build_agent_container_argv` built, and it
    crosses an ssh channel as text, so every word is handed to the remote
    shell as a literal rather than re-parsed by it.

    ONE ``&&`` chain from the ``cd`` to the launch (#2569 review): the
    prelude's own steps are ``;``-separated, so it is grouped, and a reaper
    that could not arm its deadline — or a ``cd`` that failed — leaves the
    container unstarted rather than falling through to ``docker run``.

    *withheld* names are unset before anything is exported
    (:func:`withheld_prelude`).
    """
    if not argv:
        raise RemoteDispatchError("a remote launch needs a container argv")
    quoted = " ".join(
        shlex.quote(word) for word in host_side_paths_relocated(argv, site=site)
    )
    # The prompt is read off stdin FIRST, so the handoff payload is written in
    # this order too: the prelude reads positionally and never names a value.
    prelude = (
        withheld_prelude(withheld)
        + prompt_prelude(site)
        + credential_prelude(credentials)
    )
    reaper = reaper_prelude(
        launched_container(argv, site=site), deadline_seconds=deadline_seconds
    )
    launch = credential_launch(credentials)
    return (
        f"set +x; cd {shlex.quote(site.checkout)} && {reaper} && "
        f"{{ {prelude}}} && {launch}{quoted}"
    )


def remote_agent_handoff(prompt: str, credentials: Sequence[ModelCredential]) -> str:
    """Every base64 line one launch's prelude reads, in the order it reads them.

    One function, because the prelude and the payload are two halves of the
    same positional contract: a line added to one and not the other leaves the
    credential decoding the prompt.
    """
    return prompt_handoff(prompt) + credential_handoff(credentials)


@dataclass(frozen=True)
class RemoteContainer:
    """What has to be reached ON the instance to stop a launched container."""

    runtime: str
    name: str
    cidfile: str | None = None


def launched_container(
    argv: Sequence[str], *, site: RemoteSite
) -> RemoteContainer | None:
    """The runtime, name and id file of the container *argv* launches there.

    Read off the RELOCATED argv rather than rebuilt from the slug, so what a
    lost session's cleanup stops is what was launched — the same rule the
    local watchdog's single derivation site carries. ``None`` when the argv
    names no container to stop, because a cleanup that cannot name its target
    must not replace the placement's own verdict with a KeyError.
    """
    from kew.sandbox import _argv_runtime_and_name

    relocated = host_side_paths_relocated(argv, site=site)
    try:
        runtime, name = _argv_runtime_and_name(relocated)
    except (ValueError, IndexError):
        logger.warning(
            "a remote launch argv names no --name, so a container it leaves "
            "running on the instance cannot be stopped by kew"
        )
        return None
    cidfile = None
    if "--cidfile" in relocated[:-1]:
        cidfile = relocated[relocated.index("--cidfile") + 1]
    return RemoteContainer(runtime=runtime, name=name, cidfile=cidfile)


def _remove_on_instance(
    provider: HostProvider, *, command: str, name: str, job: str
) -> JobResult | None:
    """Run one removal on the instance; ``None`` when it could not be asked.

    Shared by the pre-launch sweep and the post-interruption stop because both
    ask the instance the same question — forget this container name — and a
    second spelling of it is a second thing to keep right. Neither caller may
    raise: one has a placement to start, the other has a verdict to keep.
    """
    try:
        return provider.run(
            Job(name=job, command=command, timeout_seconds=CONTAINER_STOP_TIMEOUT)
        )
    except HostError as exc:
        logger.warning(
            "could not remove container %s on %s, so its name may still be "
            "taken there: %s",
            name,
            provider.address,
            exc,
        )
        return None


def sweep_remote_container(
    provider: HostProvider, container: RemoteContainer | None
) -> None:
    """Free the per-item container name on the instance BEFORE the launch.

    The local launcher's own pre-launch sweep
    (:func:`kew.orchestrator._sweep_stale_container`), for the same reason and
    one more that only a remote placement has: :func:`stop_remote_container`
    runs in the operator process, so an operator that was killed — or a
    machine that slept until its ssh session was gone — leaves ``kew-<slug>``
    on the instance with nothing left to remove it. The daemon then refuses
    every later placement for that item with exit 125 before the agent writes
    a line, which reads as an interruption with an empty event stream rather
    than as the name collision it is.

    Only the NAME is removed, not the id file: this launch's ``--cidfile`` is
    unique per launch and does not exist yet, and a previous launch's is not a
    path this argv can name.
    """
    if container is None:
        return
    result = _remove_on_instance(
        provider,
        command=f"{shlex.quote(container.runtime)} rm -f {shlex.quote(container.name)}",
        name=container.name,
        job="sweep-container",
    )
    if result is not None and not result.ok:
        # Usually "no such container", which is the expected case and costs
        # nothing. It can also be "removal already in progress", and then the
        # launch below fails 125 on the name — so the reason is on record.
        logger.debug(
            "pre-launch removal of container %s on %s exited %d: %s",
            container.name,
            provider.address,
            result.exit_code,
            result.log.strip()[-500:],
        )


def stop_remote_container(
    provider: HostProvider, container: RemoteContainer | None
) -> None:
    """Stop and remove the container a session that did not survive left running.

    The ceiling, and a dropped session, end the LOCAL ssh process group — and
    that is all they end. The container was started by a ``docker run`` on the
    instance, which the instance's daemon owns, so it keeps running: it holds
    the CPU the next placement came for, and it holds the ``--name kew-<slug>``
    the next attempt asks for, which the runtime then refuses outright. The id
    file the local argv asked for was re-rooted onto the instance
    (:func:`host_side_paths_relocated`) and is removed here rather than left to
    accumulate in the shipped workspace; the NAME is the handle, because a file
    read back off the instance would be a second answer to a question the argv
    already answered.

    Best effort, and loud only in the log. The placement already has its
    verdict — a timeout, an interruption — and a cleanup that fails (a
    pre-empted instance has no daemon left to ask) must not replace it.
    """
    if container is None:
        return
    command = f"{shlex.quote(container.runtime)} rm -f {shlex.quote(container.name)}"
    if container.cidfile is not None:
        command += f"; rm -f {shlex.quote(container.cidfile)}"
    result = _remove_on_instance(
        provider, command=command, name=container.name, job="stop-container"
    )
    if result is None:
        return
    if not result.ok:
        # Ordinary, and not a problem: a container that had already exited was
        # removed by its own ``--rm``, and a removal of a name the daemon no
        # longer knows exits non-zero. An instance that could not answer at
        # all is the case worth a warning, and the removal logged it.
        logger.debug(
            "nothing to stop for container %s on %s: %s",
            container.name,
            provider.address,
            result.log.strip()[-500:],
        )


def clear_finished_run(
    provider: HostProvider, container: RemoteContainer | None, *, site: RemoteSite
) -> None:
    """Leave nothing of a finished placed run on the instance (#2609).

    The last step of a placement whose artifact is already on this machine.
    The container ended itself, but the reaper timer armed for it would still
    fire hours later, and the shipped checkout, the extracted artifact and the
    container id file would stay on the instance's disk until the next ship.

    The container name is removed BEFORE its timer is disarmed, so a container
    that somehow outlived its session is never left running without a
    deadline. Runs under the placement's claim, so no other run on this
    instance has shipped into the same checkout yet.

    Best effort, and loud only in the log: the run already has its verdict,
    and the next ship's sweep is the backstop for anything left here.
    """
    paths = [site.checkout, site.artifact]
    steps = []
    if container is not None:
        steps.append(f"{_container_removal(container)} >/dev/null 2>&1")
        steps.append(_disarm_reaper(_reaper_unit(container)))
        if container.cidfile is not None:
            paths.append(container.cidfile)
    # The step's own status: a timer still armed, or a path still there, is a
    # failure worth naming rather than a clean exit of the last ``rm``.
    # The files go whatever the timer says; the timer is asked last.
    verdict = "rm -rf -- " + " ".join(shlex.quote(path) for path in paths)
    if container is not None:
        verdict += f" && ! systemctl is-active --quiet {_reaper_unit(container)}.timer"
    command = "; ".join([*steps, verdict])
    try:
        result = provider.run(
            Job(
                name="clear-finished-run",
                command=command,
                timeout_seconds=CONTAINER_STOP_TIMEOUT,
            )
        )
    except HostError as exc:
        logger.warning(
            "could not clear the finished run off instance %s; the next ship "
            "clears its checkout: %s",
            provider.address,
            exc,
        )
        return
    if not result.ok:
        logger.warning(
            "clearing the finished run off instance %s exited %d, so its reaper "
            "timer or its checkout may still be there until the next ship: %s",
            provider.address,
            result.exit_code,
            result.log.strip()[-500:],
        )


class _SessionStderr:
    """The session's stderr, drained on a thread and kept as a bounded tail.

    The pipe must be READ. ssh's own diagnostics and anything the remote step
    writes to stderr land in it, and an unread pipe blocks the remote step dead
    as soon as the kernel buffer fills — under the default unbounded ceiling
    for as long as the host's claim and the disarmed idle stop last, which is
    to say for ever.

    Merging it into stdout is not the alternative: stdout is the stream the
    host parses as the agent's own events, and one stray line in it fails a
    governed round (#2053). So it keeps its own pipe, that pipe is drained, and
    the tail is kept — an interruption's only explanation is usually there.
    """

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def drain(self, stream: IO[str] | None) -> None:
        """Start pulling lines off *stream* (``None``: nothing to drain)."""
        if stream is None:
            return
        self._thread = threading.Thread(
            target=self._pump, args=(stream,), name="kew-remote-stderr", daemon=True
        )
        self._thread.start()

    def settle(self, timeout: float = STDERR_SETTLE_SECONDS) -> None:
        """Wait, bounded, for the drain to reach EOF before its tail is read."""
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def tail(self) -> str:
        """The last lines the session wrote to stderr, as one block of text."""
        with self._lock:
            return "".join(self._lines).strip()

    def _pump(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                with self._lock:
                    self._lines.append(line)
        except _DEAD_PIPE:
            logger.debug("remote session stderr drain ended early", exc_info=True)


def _tail(stderr: _SessionStderr) -> str:
    """The session's stderr tail, as anything that reports it may hold it.

    Redacted first, like every other container stream kew retains. The tail is
    whatever the remote step wrote, and the agent container's own stderr is
    part of that — so a credential the container prints there would otherwise
    be recorded in full in the retained interruption message. Redaction runs
    BEFORE the truncation, because a tail cut mid-secret is still a secret.

    Taken ONCE per session and passed on as data, because every explanation of
    an outcome — an exception's message and a refused launch's alike — must be
    the same text, redacted and bounded the same way.
    """
    return redact_credentials(stderr.tail)[-STDERR_TAIL_CHARS:]


def _because(tail: str) -> str:
    """*tail* as a trailing clause, when the session wrote to stderr at all."""
    return f": {tail}" if tail else ""


def _ssh_session(provider: HostProvider, command: str) -> subprocess.Popen[str]:
    """One ssh session to the instance whose stdout this process reads live.

    The provider owns the argv (:meth:`GcpProvider.ssh_argv`), so the session
    is the same one every other remote step opens. Only a provider that can
    address a remote machine has one: ``local`` runs its container through the
    ordinary dispatch path and never reaches here.
    """
    if not isinstance(provider, GcpProvider):
        raise RemoteDispatchError(
            f"host {provider.name!r} cannot stream a remote container: remote "
            'dispatch needs a provider that addresses a machine ("gcp")'
        )
    return subprocess_utils.popen(
        provider.ssh_argv(marker_command(command)),
        env=remote_session_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _write_handoff(session: subprocess.Popen[str], handoff: str) -> None:
    """Write the handoff lines to the session's stdin, then close it.

    Closed either way, including when there is nothing to hand off: a remote
    step left holding a live stdin it will never read is a step that can block
    on it.

    A broken pipe is swallowed on purpose: it means the session had already
    gone away, and the placement's verdict belongs to the missing exit marker
    (an interruption) rather than to the write that noticed it first.
    """
    stdin = session.stdin
    if stdin is None:
        return
    try:
        if handoff:
            stdin.write(handoff)
            stdin.flush()
    except _DEAD_PIPE:
        logger.debug("ssh session closed before the handoff completed")
    finally:
        with contextlib.suppress(*_DEAD_PIPE):
            stdin.close()


@contextlib.contextmanager
def _ceiling(
    session: subprocess.Popen[str], *, timeout: float | None
) -> Iterator[Callable[[], bool]]:
    """End *session* if it outlives *timeout*; yield whether that happened.

    A timer rather than ``wait(timeout=...)`` because the relay is blocked on
    the child's stdout, not on its exit: ending the session is what unblocks
    the read. ``None`` leaves the session unbounded, exactly as an unbounded
    job is unbounded on the local host.

    What it ends is the session's whole process GROUP
    (:func:`_end_session_group`) — the pid alone is not the session.
    """
    if timeout is None:
        yield lambda: False
        return
    expired = threading.Event()

    def end_it() -> None:
        expired.set()
        _end_session_group(session)

    timer = threading.Timer(timeout, end_it)
    timer.daemon = True
    timer.start()
    try:
        yield expired.is_set
    finally:
        timer.cancel()


def _marker_exit(line: str) -> int | None:
    """The exit code *line* carries, or ``None`` when it is not the marker."""
    found = _MARKER_LINE.match(line.strip())
    return None if found is None else int(found.group(1))


@dataclass
class _RelayedSession:
    """What one relayed ssh session produced."""

    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    handoff_failed: bool = False


def _read_relayed(
    session: subprocess.Popen[str], on_line: Callable[[str], None] | None
) -> _RelayedSession:
    """Read the session's stdout, relaying every line that is not the marker.

    The marker is kept OFF the relayed stream: the host's event parsing sees
    exactly the bytes a local container's pipe would give it, and the outcome
    is read from the marker rather than from ``gcloud``'s status, which cannot
    tell a failed job from a failed connection.
    """
    relayed = _RelayedSession()
    if session.stdout is None:
        return relayed
    for line in session.stdout:
        code = _marker_exit(line)
        if code is not None:
            relayed.exit_code = code
            continue
        if line.strip() == HANDOFF_FAILURE_MARKER and not relayed.lines:
            # kew's own prelude, before any container was launched. Guarded on
            # "nothing has arrived yet" rather than on the line alone, so an
            # agent that printed these exact bytes mid-run is relayed as the
            # event line it is.
            relayed.handoff_failed = True
            continue
        if not line.strip():
            # The host contract's wrapper prints a newline ahead of the
            # marker, and a JSON event stream has no meaningful empty line.
            # Relaying the wrapper's own blank would put a byte in the
            # stream that no local container ever produced.
            continue
        relayed.lines.append(line)
        if on_line is not None:
            on_line(line)
    return relayed


@dataclass(frozen=True)
class RelayedOutcome:
    """What one relayed session produced: its outcome and its explanation.

    The stderr tail travels WITH the exit code because the two are read
    together. A remote step that fails before it writes a single stdout line —
    a daemon refusing the launch, an entrypoint finding no agent — puts its
    whole explanation on stderr, and an outcome carrying only "exit 125" and an
    empty stream threw that explanation away (#2437 review). Already redacted
    and bounded (:func:`_tail`), so a caller may put it straight in a message.
    """

    #: What the exit marker carried: the remote step's own status.
    exit_code: int

    #: Every line the session relayed on stdout, the marker excluded.
    lines: tuple[str, ...]

    #: The tail of what it wrote to stderr, or ``""`` if it wrote none.
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """True iff the remote step exited zero."""
        return self.exit_code == 0


def relay_remote_session(
    provider: HostProvider,
    command: str,
    *,
    handoff: str = "",
    timeout: float | None = None,
    on_line: Callable[[str], None] | None = None,
) -> RelayedOutcome:
    """Run *command* on the instance, relaying its stdout as it arrives.

    Returns the session's :class:`RelayedOutcome` — its exit code, every
    relayed line, and the tail of what it wrote to stderr. A session that ends
    without the marker is an :class:`RemoteRunInterrupted` — a lost ssh
    session and a pre-empted spot instance both look exactly like this, and
    neither is a clean zero.

    The stderr tail comes back with a NORMAL outcome too, not only with the
    exceptions: a step whose exit code is the whole of what arrived is exactly
    the step whose explanation is on the other pipe.

    The ceiling is armed around the whole session rather than passed to
    ``wait()``: the relay blocks on the child's stdout, so a container that
    hangs *silently* never reaches a wait to time out and would hold the
    host's lock forever. The ceiling ends the session's whole process group,
    and the last holder of the pipe's write end going is what the read loop is
    waiting for — ending only the ``gcloud`` wrapper leaves its ``ssh`` child
    holding that pipe and the read waiting on it (:func:`_end_session_group`).

    The session's stderr is drained the whole time (:class:`_SessionStderr`):
    it is its own pipe so nothing it carries is read as an agent event, and an
    undrained pipe blocks the remote step as soon as the kernel buffer fills.

    The cleanup guard opens on the line after the session does, so the handoff
    is inside it: a handoff past the pipe buffer BLOCKS until the remote step
    reads it, and an escape in that window — a ``KeyboardInterrupt`` at the
    terminal, which no longer reaches a session of its own group — has to
    reach :func:`_close_session` as well, or it leaves a detached ssh session
    and the container behind it running on the instance.

    The ceiling is armed BEFORE the handoff for the same reason: a handoff
    past the pipe buffer blocks until the remote step reads it, so a remote
    side that never reads stdin would hold the claimed host — with its idle
    stop disarmed — for as long as it declines to, and the caller would get a
    clean exit code rather than the :class:`HostTimeout` its ceiling asked
    for. Inside the ceiling the kill breaks the pipe, the blocked write ends
    as a dead pipe (:func:`_write_handoff`) and the verdict is the timeout.
    """
    session = _ssh_session(provider, command)
    stderr = _SessionStderr()
    try:
        stderr.drain(session.stderr)
        with _ceiling(session, timeout=timeout) as expired:
            _write_handoff(session, handoff)
            relayed = _read_relayed(session, on_line)
            status = session.wait()
        stderr.settle()
        tail = _tail(stderr)
        if expired():
            raise HostTimeout(
                f"a remote container on {provider.address} outran its "
                f"{timeout}s ceiling, so it has no exit code{_because(tail)}"
            )
    finally:
        _close_session(session)
    if relayed.handoff_failed:
        raise RemoteRunInterrupted(
            f"the session to {provider.address} ended before the prompt and "
            f"credential handoff completed, so the container never "
            f"started{_because(tail)}",
            relayed=relayed.lines,
        )
    if relayed.exit_code is None:
        raise RemoteRunInterrupted(
            f"the session to {provider.address} ended with no exit marker after "
            f"{len(relayed.lines)} relayed event line(s) (ssh exit {status}), so "
            f"the run was interrupted and is NOT a success{_because(tail)}",
            relayed=relayed.lines,
        )
    return RelayedOutcome(
        exit_code=relayed.exit_code, lines=tuple(relayed.lines), stderr=tail
    )


def _end_session_group(session: subprocess.Popen[str]) -> None:
    """Kill the session's whole process group, not just the pid kew holds.

    ``gcloud compute ssh`` is a WRAPPER: the process kew holds is the wrapper,
    and the ``ssh`` it spawns is the one actually holding the write end of the
    session's stdout. Killing the wrapper's pid alone leaves that child alive
    with the pipe open, so the relay's read never reaches EOF, the ceiling it
    was serving never produces a verdict and the orphan cleanup never runs —
    the host's claim stays held with its idle stop disarmed, which is for ever
    and is the exact hang the ceiling exists to prevent.

    The group is safe to signal because :func:`_ssh_session` starts the session
    in a new one (``start_new_session=True``), so the group is the session's
    own and nothing of kew's is in it. Best effort, and ``OSError`` is the whole
    of it: a group that has already gone (``ProcessLookupError``) is the
    outcome asked for, and a group this process may not signal is not a reason
    to lose the verdict the caller is on its way to raising.
    """
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(session.pid), signal.SIGKILL)
    with contextlib.suppress(*_DEAD_PIPE):
        session.kill()


def _close_session(session: subprocess.Popen[str]) -> None:
    """End a session still running, then close its pipes.

    The relay leaves no live session behind, however it leaves: the ceiling
    kills the group itself, but an interruption, a ``KeyboardInterrupt`` or any
    other escape arrives here instead — and a new session of its own
    (:func:`_ssh_session`) is no longer ended by the terminal's own signal.
    A dead child's pipes raise on close, so each close is suppressed.
    """
    if session.poll() is None:
        _end_session_group(session)
    for stream in (session.stdout, session.stderr, session.stdin):
        if stream is not None:
            with contextlib.suppress(*_DEAD_PIPE):
                stream.close()


def extract_artifact_on_host(
    provider: HostProvider,
    *,
    config: DispatchConfig,
    site: RemoteSite,
    image: str,
    slug: str,
    base_sha: str,
) -> None:
    """Extract the finished checkout ON the instance, with the image's producer.

    The same second container the operator machine runs — ``--network none``,
    the checkout read-only, no runner credential — built by kew's own
    :func:`kew.sandbox.build_extract_argv` with the instance's paths and the
    ssh login's ids, so the artifact is written for the login that has to read
    it back rather than for root.

    Two things make that argv runnable on the other machine, and both are the
    same facts the agent launch already carries:

    * The argv is put through :func:`host_side_paths_relocated`. Extraction is
      a ``docker run`` like any other, so its ``--cidfile`` is this machine's
      scratch path — on a Mac a ``/var/folders/...`` one no Linux instance has
      — and the runtime refuses to start a container whose id file it cannot
      create.
    * The artifact directory is **created by this login**, mode 0700, not left
      for the daemon to create as the bind mount's source. A mount source the
      daemon creates is owned by root, and the producer runs as
      ``--user <login uid>``: it would have nowhere to write the work back to.
      The ``rm -rf`` stays, because the producer refuses a directory that
      already has content — a retry would otherwise mix two artifacts.
    """
    from kew.sandbox import build_extract_argv

    argv = host_side_paths_relocated(
        build_extract_argv(
            runtime=config.sandbox.runtime,
            image=image,
            checkout_dir=Path(site.checkout),
            artifact_dir=Path(site.artifact),
            slug=slug,
            base_sha=base_sha,
            uid=site.uid,
            gid=site.gid,
        ),
        site=site,
    )
    quoted = " ".join(shlex.quote(word) for word in argv)
    artifact = shlex.quote(site.artifact)
    result = provider.run(
        Job(
            name="extract-artifact",
            command=f"rm -rf {artifact} && mkdir -m 700 {artifact} && {quoted}",
            timeout_seconds=EXTRACT_TIMEOUT,
        )
    )
    if not result.ok:
        raise RemoteDispatchError(
            f"the work on instance {provider.address} could not be extracted "
            f"(exit {result.exit_code}): {result.log.strip()[-2000:]}"
        )


def _artifact_fetch_command(site: RemoteSite) -> str:
    """The remote command that writes the artifact directory as base64 bytes.

    A PIPELINE, so it runs under an explicit ``bash -c`` with ``pipefail``:
    the host contract's exit marker carries ``$?``, and for a pipeline that is
    the LAST stage's status. Without ``pipefail`` a ``tar`` that could not read
    the instance's artifact directory came back as a clean zero — base64
    succeeding at encoding nothing — and tar's own explanation, on stderr, was
    then discarded by the decode path that failed on the empty payload instead
    (#2437 review). ``bash`` rather than the login shell because ``pipefail``
    is not POSIX: a shell that silently ignored the option would restore the
    exact bug the option is here to close.
    """
    pipeline = f"set -o pipefail; tar -C {shlex.quote(site.artifact)} -cf - . | base64"
    return f"bash -c {shlex.quote(pipeline)}"


def fetch_artifact(
    provider: HostProvider, *, site: RemoteSite, destination: Path
) -> Path:
    """Bring the instance's artifact directory back as data, and unpack it.

    Returned over the ssh channel rather than as a second transport: the
    artifact is a few files and a manifest, and one channel is one thing to
    reason about. Unpacked with tar's ``data`` filter, so a member naming an
    absolute path, a link out of the directory or a device is refused before
    :func:`kew.publication_artifact.read_artifact` ever reads it — the
    artifact is data from another machine, and this is the boundary.
    """
    outcome = relay_remote_session(
        provider,
        _artifact_fetch_command(site),
        timeout=FETCH_TIMEOUT,
    )
    if not outcome.ok:
        raise RemoteDispatchError(
            f"cannot read the artifact back from instance {provider.address} "
            f"(exit {outcome.exit_code}){_because(outcome.stderr)}"
        )
    try:
        payload = base64.b64decode(
            "".join(line.strip() for line in outcome.lines), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise RemoteDispatchError(
            f"instance {provider.address} returned an artifact kew cannot decode: {exc}"
        ) from exc
    if destination.exists() and any(destination.iterdir()):
        # The producer's own rule, kept on this side of the wire: a directory
        # that already has content would leave blobs the new manifest does not
        # reference, and a re-run that mixed two artifacts is unreadable.
        raise RemoteDispatchError(
            f"refusing to unpack an artifact into {destination}: it already "
            "has content, so the two would be mixed"
        )
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    archive = destination.parent / f"{destination.name}.tar"
    archive.write_bytes(payload)
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")  # nosec B202 — data filter
    except (tarfile.TarError, OSError) as exc:
        raise RemoteDispatchError(
            f"the artifact returned by instance {provider.address} could not be "
            f"unpacked: {exc}"
        ) from exc
    finally:
        archive.unlink(missing_ok=True)
    return destination


def record_remote_launch(
    store: AuditLedger,
    launch: RemoteLaunch,
    *,
    run_id: str,
    issue_num: int | None = None,
    item_id: str | None = None,
    repo: str | None = None,
    now: datetime | None = None,
) -> int | None:
    """Append the launch audit event for a run placed on a remote host.

    A new attested fact rides in the ``payload`` dict, never as a column or a
    hash preimage slot. The payload names the host, the instance and the
    credential NAMES: that is what the acceptance asks for, and the name is
    the whole of what may ever be recorded about a credential.
    """
    moment = now or datetime.now(UTC)
    return store.append_audit_event(
        timestamp=moment.isoformat(),
        event=REMOTE_LAUNCH_EVENT,
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        repo=repo,
        actor_type="host",
        actor_id=launch.instance,
        payload=launch.audit_payload(),
    )


def _release(
    provider: HostProvider,
    *,
    store: Any = None,
    run_id: str | None = None,
    config: DispatchConfig | None = None,
) -> str:
    """Release the host, returning a failure rather than raising over the run.

    A release that fails costs money; raising here would cost the run's own
    verdict, which is the thing the operator came for. So the failure is
    RETURNED — empty when the stop was armed or rightly skipped — and the
    placement hands it back on its result, where the caller surfaces it; a
    warning in a log nobody reads let a dispatch report success over a VM left
    running (#2627).

    A release ARMS the instance's idle stop, so it is skipped entirely while the
    ledger still shows another run on that instance (#2398). The LEDGER, not the
    lock this run is holding: a placement is recorded before its claim is taken,
    so another process can already have this instance on a row of its own, and a
    run queued for the instance holds it with no container to see. Arming the
    stop against either takes the machine down under work attached to it, while
    skipping one costs idle minutes. The still-``pending`` row of the run doing
    the releasing is excluded, so a lone run still arms its own stop and no VM is
    left billing. What makes those rows carry an instance at all is
    :func:`kew.fleet_placement.record_placement`, which every placement through
    :func:`place_dispatch_on_host` performs on the way in.

    Without a *store* the question cannot be asked and the stop is armed as
    before: this is the single-run path N5 shipped, and leaving a VM up is the
    failure an idle stop exists to prevent. With a *config* the question is also
    age-bounded, so a run killed on the instance cannot pin it up for good —
    nothing on this machine can prove a remote container dead, so without the
    bound no later release would ever arm a stop.
    """
    if store is not None and instance_is_occupied(
        store,
        instance=provider.address,
        host=provider.name,
        exclude_run_id=run_id,
        abandoned_after=(None if config is None else abandoned_after_seconds(config)),
    ):
        logger.info(
            "not arming the idle stop on host %s: another run is still in "
            "flight on that instance",
            provider.address,
        )
        # The stop is that run's to arm, but this run's lease on the instance
        # would refuse it the host until this run's deadline (#2627).
        try:
            provider.drop_hold()
        except HostError as exc:
            logger.warning("could not drop the hold on %s: %s", provider.address, exc)
        return ""
    try:
        provider.release()
    except HostError as exc:
        logger.error("could not release host %s: %s", provider.address, exc)
        return (
            f"could not arm the idle stop on {provider.name} instance "
            f"{provider.address}; it may keep running and billing: "
            f"{exception_text(exc)}"
        )
    return ""


def _refused_launch(outcome: RelayedOutcome) -> str | None:
    """What the runtime's exit says, when it says no container ever ran.

    The LOCAL rule, asked rather than restated: a host-side launch's exit is
    read with :func:`kew.sandbox.container_client_failure`, and 125 is the
    status Docker and Podman reserve for "the daemon refused this" — a missing
    egress network, a name the last placement left taken, a cap the daemon will
    not grant — rather than for anything an agent decided.

    Narrowed to a session that relayed NOTHING, which a refused launch always
    is: an agent that streamed its events and then exited 125 itself reached a
    verdict, and a verdict is the caller's to read.
    """
    from kew.sandbox import container_client_failure

    if outcome.lines:
        return None
    return container_client_failure(
        subprocess.CompletedProcess([], outcome.exit_code, "", outcome.stderr)
    )


def _relay_placed_agent(
    provider: HostProvider,
    argv: Sequence[str],
    *,
    site: RemoteSite,
    prompt: str,
    credentials: Sequence[ModelCredential],
    timeout_seconds: float | None,
    on_line: Callable[[str], None] | None,
    withheld: Sequence[str] = (),
) -> RelayedOutcome:
    """Relay the agent container's stream, and stop it if the session does not.

    Every way this ends without an exit code — the ceiling, a dropped session,
    a pre-emption — leaves a container the instance's daemon is still running,
    so the cleanup is attached HERE, to the one call that launches it, rather
    than left to whichever caller notices the exception. The sweep is attached
    to the same call for the mirror-image reason: a cleanup that never ran,
    because the operator process it lives in died, is a name this launch has to
    free for itself before the daemon refuses it.
    """
    container = launched_container(argv, site=site)
    sweep_remote_container(provider, container)
    # The instance holds itself for as long as the container may live, so a
    # placement whose operator is killed mid-run still keeps the next one off
    # this checkout and still powers the instance off afterwards (#2627).
    # After the sweep, never before it: the sweep is a job, and a job re-takes
    # the lease for its own short ceiling (#2687 review).
    provider.hold(remote_deadline(timeout_seconds))
    try:
        outcome = relay_remote_session(
            provider,
            remote_agent_command(
                argv,
                site=site,
                credentials=credentials,
                deadline_seconds=remote_deadline(timeout_seconds),
                withheld=withheld,
            ),
            handoff=remote_agent_handoff(prompt, credentials),
            timeout=timeout_seconds,
            on_line=on_line,
        )
    except BaseException:
        # Every escape, not only the ceiling and the interruption: ``on_line``
        # is the caller's own consumer and the claim-launch marker fires from
        # it (:class:`kew.dispatch_placement._Relayed`), so an exception
        # raised while the container is still running has to end it here too
        # — exactly as the local launcher stops its container when the marker
        # it just delivered raises (#2526 review).
        stop_remote_container(provider, container)
        raise
    refused = _refused_launch(outcome)
    if refused is not None:
        # No container to stop: the daemon never made one. What it DID say is
        # on the session's stderr, and it is the whole of the diagnosis.
        raise RemoteDispatchError(
            f"the daemon on instance {provider.address} refused the agent "
            f"container ({refused}), so no agent ran and the placement reached "
            f"no verdict{_because(outcome.stderr)}"
        )
    return outcome


def refuse_unpackaged_image(runner: str, image: str) -> None:
    """Refuse a placement whose image is not the tag kew's packaged inputs pin.

    The credential handoff starts the image's entrypoint itself, and the one
    it starts is read off kew's packaged Dockerfile
    (:func:`_image_entrypoint`): a custom image would have ITS entrypoint
    silently replaced by the stock one (#2569 review). Until the entrypoint is
    read from the image itself, only the packaged image is placed — refused
    before anything is shipped or an instance is claimed for it.
    """
    from kew.sandbox import _expected_packaged_image_pin, packaged_agent_image_context

    try:
        expected = _expected_packaged_image_pin(packaged_agent_image_context(), runner)
    except (ValueError, OSError) as exc:
        raise RemoteDispatchError(
            f"cannot place {runner} on a host: kew's packaged image inputs "
            f"could not be read: {exc}"
        ) from exc
    if image != expected:
        raise RemoteDispatchError(
            f"cannot place {runner} on a host with image {image!r}: only kew's "
            f"packaged image ({expected}) runs there, because the entrypoint a "
            "placed container starts is read from kew's packaged inputs, not "
            "from the image"
        )


def _image_entrypoint(runner: str) -> tuple[str, ...]:
    """The entrypoint *runner*'s packaged image runs, or a refused placement."""
    from kew.sandbox import packaged_image_entrypoint

    try:
        return packaged_image_entrypoint(runner)
    except (ValueError, OSError) as exc:
        raise RemoteDispatchError(
            f"cannot hand a credential to a placed {runner} container: {exc}"
        ) from exc


def _attest_launch(
    attest: Callable[[RemoteLaunch], None] | None, launch: RemoteLaunch
) -> None:
    """Hand the resolved launch to the caller's attestation, if it has one."""
    if attest is not None:
        attest(launch)


def _run_placed_agent(
    provider: HostProvider,
    *,
    config: DispatchConfig,
    bundle: Path,
    image: str,
    runner: str,
    slug: str,
    model: str,
    base_sha: str,
    transport: Any,
    prompt: str,
    credentials: Sequence[ModelCredential],
    destination: Path,
    timeout_seconds: float | None,
    on_line: Callable[[str], None] | None,
    attest_launch: Callable[[RemoteLaunch], None] | None = None,
) -> RemoteDispatchResult:
    """One placement, from a brought-up instance to an artifact on this machine."""
    from kew.dispatch_sandbox import build_agent_container_argv
    from kew.runners import get_runner

    provider.ensure_up()
    refuse_instance_identity(provider)
    provider.ship(bundle)
    site = read_remote_site(provider)
    handed_over = [credential.name for credential in credentials]
    # The container imports exactly the names this launch handed over, which
    # are exactly the names the launch audit records — never one the VM's own
    # login environment would have supplied (#2526 review).
    argv = placed_environment_argv(
        build_agent_container_argv(
            config,
            image=image,
            runner=runner,
            checkout_dir=Path(site.checkout),
            slug=slug,
            model=model,
            inference_transport=transport,
        ),
        handed_over=handed_over,
    )
    # The credential goes to the container's stdin, never ``--env``: the
    # daemon writes an ``--env`` value into the container's config on the
    # instance's disk (#2569).
    argv = credential_stdin_argv(
        argv,
        image=image,
        command=get_runner(runner).container_args(config=config, model=model),
        handed_over=handed_over,
        entrypoint=_image_entrypoint(runner) if handed_over else (),
    )
    # Both prerequisites first, and the network before the image: the network
    # is the instance's own and kew cannot make it, so a gap there is refused
    # before a build that may take an hour is paid for.
    ensure_agent_network(provider, argv=argv, runtime=config.sandbox.runtime)
    remote_image = ensure_agent_image(
        provider, image=image, runner=runner, runtime=config.sandbox.runtime
    )
    launch = RemoteLaunch(
        host=provider.name,
        instance=provider.address,
        image=image,
        runner=runner,
        credential_names=tuple(handed_over),
        image_built=remote_image.built,
        image_id=remote_image.image_id,
    )
    # Here and nowhere else: every fact about where this agent runs — the
    # instance, the credential names and the immutable bytes of its image — is
    # resolved, and nothing has been launched yet. A caller that attests a
    # containment boundary attests it from this, because the operator
    # machine's own image ID describes a different store (#2526 review).
    _attest_launch(attest_launch, launch)
    outcome = _relay_placed_agent(
        provider,
        argv,
        site=site,
        prompt=prompt,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        on_line=on_line,
        withheld=withheld_names(runner, handed_over),
    )
    extract_artifact_on_host(
        provider, config=config, site=site, image=image, slug=slug, base_sha=base_sha
    )
    artifact_dir = fetch_artifact(provider, site=site, destination=destination)
    clear_finished_run(provider, launched_container(argv, site=site), site=site)
    return RemoteDispatchResult(
        launch=launch,
        exit_code=outcome.exit_code,
        relayed=outcome.lines,
        artifact_dir=artifact_dir,
        stderr=outcome.stderr,
    )


def place_dispatch_on_host(
    provider: HostProvider,
    *,
    config: DispatchConfig,
    checkout_dir: Path,
    workspace: Path,
    image: str,
    runner: str,
    slug: str,
    model: str,
    base_sha: str,
    run_kind: str,
    transport: Any = None,
    credentials: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    on_line: Callable[[str], None] | None = None,
    store: Any = None,
    run_id: str | None = None,
    attest_launch: Callable[[RemoteLaunch], None] | None = None,
) -> RemoteDispatchResult:
    """Run one agent container on *provider* and bring its work back as data.

    *checkout_dir* is only ever READ — it is bundled, and the container runs
    over the instance's clone of that bundle. Whatever ends the placement, the
    operator machine's checkout is exactly as it was.

    *run_kind* is required and has no default. It is what the module's first
    credential fence reads — nothing but a ``dispatch`` run may carry the
    runner's model credential to a host — and a default would have made that
    fence opt-in: a review or remediation caller that simply omitted the
    argument would have been handed the dispatch exception it is excluded from
    (#2437 review). The caller always knows what it is placing.

    Credentials are permitted and the prompt handoff is read BEFORE the
    instance is touched, so a refused credential and a missing prompt never
    start a VM, and a :class:`CredentialVariableRefused` reaches the caller as
    itself rather than as an interruption.

    *store* and *run_id* are the fleet ledger and this run's id. Given both,
    this placement RECORDS itself on the ledger — the host and the instance the
    run is going to — and that record is what lets the release at the end of
    every placement on this instance see the OTHER runs on it, so a finished run
    never arms the idle stop that would take the machine down under one still
    working (#2398). The write and the read are deliberately the same path: the
    ledger's ``run_host`` defaults to ``local``, so a guard reading a column
    nobody wrote would find every instance free. Omitted, nothing is recorded
    and the release behaves as it did when an instance could only ever hold one
    run. Given both but with no row to write — a *run_id* never admitted, or
    whose row is already gone — the placement raises
    :class:`~kew.fleet_placement.PlacementNotRecorded` instead of starting an
    agent on a machine the guard would read as free; it is raised before the
    instance is touched, so nothing has been spent on it. Given a *store*, a
    takeover of a dead holder's lease on the instance goes on its audit trail
    before anything ships; without one, a provider that can take a lease
    over is refused before the instance is touched (#2874).

    *attest_launch* is called with the resolved :class:`RemoteLaunch` once the
    instance holds the image and before any container is launched, so a caller
    that records a containment boundary records the INSTANCE's immutable image
    ID — the bytes the agent runs — rather than the operator machine's ID for
    the same tag. Raising from it refuses the placement before the agent runs.
    """
    refuse_unpackaged_image(runner, image)
    permitted = permit_model_credentials(
        credentials or {}, runner=runner, run_kind=run_kind
    )
    prompt = read_prompt_handoff(Path(checkout_dir))
    bundle = bundle_checkout(Path(checkout_dir), Path(workspace), base_sha=base_sha)
    if store is not None and run_id:
        # Before the claim, not after it: a run queued behind another holder is
        # already attached to this instance, and the holder's release must see
        # it. Over-reporting an instance as busy costs idle minutes; the other
        # way costs the run on it.
        record_placement(
            store, run_id=run_id, host=provider.name, instance=provider.address
        )
    # A dead holder's lease is taken over only with the takeover on the
    # dispatch ledger's audit trail before anything ships (#2874), and that
    # ledger is proved present and writable before the host is touched: with
    # no store, a provider that can take over is refused here, before
    # ``ensure_up`` could resume an instance or cancel its idle stop.
    from kew.host_takeovers import record_lapsed_leases

    record_lapsed_leases(store, [provider], config=config)
    try:
        with provider.claim():
            release_error = ""
            try:
                placed = _run_placed_agent(
                    provider,
                    config=config,
                    bundle=bundle,
                    image=image,
                    runner=runner,
                    slug=slug,
                    model=model,
                    base_sha=base_sha,
                    transport=transport,
                    prompt=prompt,
                    credentials=permitted,
                    destination=Path(workspace) / REMOTE_ARTIFACT_DIR,
                    timeout_seconds=timeout_seconds,
                    on_line=on_line,
                    attest_launch=attest_launch,
                )
            finally:
                release_error = _release(
                    provider, store=store, run_id=run_id, config=config
                )
            return replace(placed, release_error=release_error)
    except (RemoteDispatchError, HostTimeout, CredentialVariableRefused):
        # A refusal this module made is already named for what it is — a
        # missing image, an undecodable artifact, an interruption. Only the
        # provider's own failures (an instance kew could not bring up, reach
        # or ship to) become interruptions here.
        raise
    except HostError as exc:
        raise RemoteRunInterrupted(
            f"the run placed on {provider.name} instance {provider.address} was "
            f"interrupted and reached no outcome: {exc}"
        ) from exc
