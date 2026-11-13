"""The one canonical repository the host is allowed to trust (I07).

Reading a repository runs code from it. ``git fetch`` against a local path
starts ``upload-pack`` *in that path*, reading that path's configuration, and a
checkout an agent has been writing for the length of a dispatch is not a
repository the host may read. So the rule the broker already follows for writes
— never point host git at an agent-writable directory — applies just as hard to
reads, and this module is where it is enforced once.

Everything downstream consumes a :class:`CanonicalRepository` rather than a
path. That is the point of the type: a function that takes one cannot be handed
the agent's checkout by a caller who forgot, because the only way to get one is
through :func:`provision`, which refuses a source the host cannot prove it owns.

## What the canonical repository holds

The trusted base commit and nothing else. It is fetched shallow, from the
source, into a freshly created bare repository — bare because nothing here
needs a working tree, and empty-but-for-the-base because a destination that
already held the agent's objects could not notice a promotion that never wrote
them.

## Why the base is passed in

The package's manifest names a base too, and that name is agent-written. It is
checked against the host's own trusted base in :func:`promote`, and the package
is refused when they differ: the manifest is evidence of what the agent
*believes*, never of what the host should publish onto.
"""

from __future__ import annotations

import os
import re
import stat as stat_module
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from kew import subprocess_utils
from kew.credential_surface import redact_credentials
from kew.publication import publication_git_env
from kew.publication_apply import apply_to_tree, trusted_git_env
from kew.publication_filter import filter_artifact
from kew.subprocess_utils import run, run_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from kew.publication_artifact import Artifact
    from kew.publication_filter import ManifestEntry

__all__ = [
    "VERIFICATION_EXACT_COMMIT",
    "VERIFICATION_EXACT_TREE",
    "CanonicalRepository",
    "CanonicalRepositoryError",
    "CommitFacts",
    "CommitIntentLike",
    "HostGitPreparation",
    "PromotedTree",
    "commit_tree",
    "fetch_commit",
    "publication_write_env",
    "promote",
    "provision",
    "read_commit",
]

#: The verification a promoted tree carries: the package was applied twice, in
#: the canonical repository, and both applications produced this exact tree.
VERIFICATION_EXACT_TREE = "exact_tree"

#: The verification a materialized commit carries: the commit object named by
#: a persisted intent was created in the canonical repository, read back, and
#: found to hold exactly that tree, parent, identity and message.
VERIFICATION_EXACT_COMMIT = "exact_commit"

_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# A source git resolves over a transport rather than by reading a directory
# here. Anything else is treated as a local path and has to prove the host owns
# it before a single byte of its configuration is read.
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+")
_SCP_LIKE = re.compile(r"[^/@:]+@[^/@:]+:\S+")
_DIR_MODE = 0o700


class HostGitPreparation:
    """Isolate Git configuration while preparing fresh verification repositories.

    Template-free repositories contain no local filter definitions. Published
    attributes can name filters, but cannot define executable filter commands.
    """

    def __init__(self, root: Path) -> None:
        self.home = root / "git-home"
        self.hooks = root / "git-hooks"
        self.home.mkdir()
        self.hooks.mkdir()

    def run(
        self,
        cmd: list[str],
        *,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # Reuse the host boundary's GIT_* scrub. Explicit fetch auth, when
        # supplied by _fetch_env, survives; ambient credential helpers do not.
        isolated = trusted_git_env() if env is None else env.copy()
        isolated.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TEMPLATE_DIR="",
            GIT_TERMINAL_PROMPT="0",
        )
        isolated.pop("SSH_ASKPASS", None)
        isolated.pop("SSH_ASKPASS_REQUIRE", None)
        return subprocess_utils.run(
            [
                cmd[0],
                "-c",
                f"core.hooksPath={self.hooks}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.attributesFile={os.devnull}",
                *cmd[1:],
            ],
            check=check,
            env=isolated,
            cwd=self.home,
        )


class CanonicalRepositoryError(RuntimeError):
    """The canonical repository could not be provisioned, read, or trusted."""


@dataclass(frozen=True)
class CanonicalRepository:
    """A bare, host-owned repository holding exactly the trusted base."""

    path: Path
    base_sha: str

    def base_state(self) -> dict[str, str]:
        """Every path at the base, with its mode — what the filter compares to.

        Without it the filter's collision and mode-escalation rules never fire,
        and a promotion would come back clean because nobody asked rather than
        because the answer was clean.
        """
        listing = self._git(
            ["ls-tree", "-r", "-z", f"{self.base_sha}^{{tree}}"], "ls-tree"
        )
        state: dict[str, str] = {}
        for record in listing.split("\0"):
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if parts and path:
                state[path] = parts[0]
        return state

    def base_blob_oids(self) -> dict[str, str]:
        """Return each base path's object id from this host-owned repository."""
        return self.tree_blob_oids(self.base_sha)

    def tree_blob_oids(self, commit_sha: str) -> dict[str, str]:
        """Each path's object id in one commit's tree, from THIS repository.

        The base is the everyday argument (:meth:`base_blob_oids`). Another
        commit is read when a publication has to say what some other tree
        already holds at a path — the base a review checkout merged (#2362).
        """
        if not _OID.fullmatch(commit_sha):
            raise CanonicalRepositoryError(f"{commit_sha!r} is not a commit id")
        listing = self._git(
            ["ls-tree", "-r", "-z", f"{commit_sha}^{{tree}}"], "ls-tree"
        )
        objects: dict[str, str] = {}
        for record in listing.split("\0"):
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and path:
                objects[path] = parts[2]
        return objects

    def read_blob(self, oid: str) -> bytes:
        """Read one blob by object id from this repository."""
        if not _OID.fullmatch(oid):
            raise CanonicalRepositoryError(f"blob {oid!r} is not an object id")
        result = run_bytes(
            ["git", "-C", str(self.path), "cat-file", "blob", oid], env=_read_env()
        )
        if result.returncode != 0:
            detail = redact_credentials(
                (result.stderr or b"").decode("utf-8", errors="replace")
            )
            raise CanonicalRepositoryError(f"cat-file blob failed: {detail[:300]}")
        return result.stdout or b""

    def holds_tree(self, tree_sha: str) -> bool:
        """True when *tree_sha* is a tree object in THIS repository."""
        return self._holds(tree_sha, "tree")

    def holds_commit(self, commit_sha: str) -> bool:
        """True when *commit_sha* is a commit object in THIS repository."""
        return self._holds(commit_sha, "commit")

    def _holds(self, oid: str, kind: str) -> bool:
        if not _OID.fullmatch(oid):
            return False
        result = run(
            ["git", "-C", str(self.path), "cat-file", "-t", oid],
            check=False,
            env=_read_env(),
        )
        return result.returncode == 0 and (result.stdout or "").strip() == kind

    def _git(self, args: list[str], what: str) -> str:
        result = run(["git", "-C", str(self.path), *args], check=False, env=_read_env())
        if result.returncode != 0:
            detail = redact_credentials(result.stderr or "").strip()[:300]
            raise CanonicalRepositoryError(f"{what} failed: {detail}")
        return result.stdout or ""


@dataclass(frozen=True)
class PromotedTree:
    """The immutable tree a preserved package became, and how it was checked."""

    base_sha: str
    tree_sha: str
    entry_count: int
    deletion_count: int
    changed_paths: tuple[str, ...] = ()
    verification: str = VERIFICATION_EXACT_TREE


def _read_env() -> dict[str, str]:
    """Ambient ``GIT_*`` dropped, for the reason ``apply_to_tree`` drops it.

    ``GIT_DIR`` beats ``-C``, so an inherited one would answer questions about
    a repository this module never named — and ``GIT_CONFIG_*`` is how kew
    carries the publication token, which has no business in a read.
    """
    return trusted_git_env(GIT_TERMINAL_PROMPT="0")


def _is_remote_url(source: str) -> bool:
    return bool(_URL.fullmatch(source) or _SCP_LIKE.fullmatch(source))


def _require_host_owned(source: str) -> None:
    """Refuse a local source the host cannot prove it owns.

    ``upload-pack`` runs inside the source and reads the source's config. A
    directory another uid owns, or one anybody else may write, is a directory
    whose configuration somebody else chooses — the agent-writable-metadata
    problem exactly, one transport removed.
    """
    path = Path(source)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CanonicalRepositoryError(
            f"source {source} cannot be read: {exc}"
        ) from exc
    if stat_module.S_ISLNK(info.st_mode):
        raise CanonicalRepositoryError(f"source {source} is a symlink")
    if not stat_module.S_ISDIR(info.st_mode):
        raise CanonicalRepositoryError(f"source {source} is not a repository directory")
    if info.st_uid != os.geteuid():
        raise CanonicalRepositoryError(
            f"source {source} is owned by uid {info.st_uid}, not the host process"
        )
    if info.st_mode & (stat_module.S_IWGRP | stat_module.S_IWOTH):
        raise CanonicalRepositoryError(
            f"refusing to read git metadata from {source}: it is writable by "
            "someone other than the host"
        )


def _fetch_env(source: str, git: HostGitPreparation | None = None) -> dict[str, str]:
    """The environment the fetch runs under, chosen by the source's kind.

    A URL gets the publication credential, because that is what it is for. A
    local path gets no ``GIT_*`` at all: handing a token to a directory is how
    a credential reaches a process with no business holding one.
    """
    if _is_remote_url(source):
        if git is not None:
            # Verification already has an operator-selected source. Do not
            # discover auth scope by running ambient Git in another checkout.
            parsed = urlsplit(source)
            if parsed.scheme == "https" and parsed.hostname:
                return publication_write_env(host=parsed.hostname)
            return _read_env()
        return publication_write_env()
    _require_host_owned(source)
    return _read_env()


def publication_write_env(*, host: str | None = None) -> dict[str, str]:
    """Return a sanitized git environment with only kew's auth header added."""
    env = _read_env()
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    auth = publication_git_env() if host is None else publication_git_env(host=host)
    if auth is None:
        return env
    try:
        index = int(auth.get("GIT_CONFIG_COUNT", "0")) - 1
    except ValueError:
        return env
    key = auth.get(f"GIT_CONFIG_KEY_{index}", "")
    value = auth.get(f"GIT_CONFIG_VALUE_{index}", "")
    if index < 0 or not key.startswith("http.") or not key.endswith(".extraHeader"):
        return env
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = key
    env["GIT_CONFIG_VALUE_0"] = value
    return env


def _init_bare(dest: Path, git: HostGitPreparation | None = None) -> None:
    if dest.exists() or dest.is_symlink():
        raise CanonicalRepositoryError(f"{dest} already exists")
    try:
        dest.mkdir(parents=True, mode=_DIR_MODE)
        run_git = git.run if git is not None else run
        run_git(["git", "init", "--bare", "--quiet", str(dest)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CanonicalRepositoryError(f"could not create {dest}: {exc}") from exc


def _require_base(
    dest: Path, base_sha: str, git: HostGitPreparation | None = None
) -> None:
    """The base is PRESENT, proven rather than assumed from an exit code.

    A fetch that silently brought nothing would otherwise fail much later,
    inside ``read-tree``, looking like a defect in the applier.
    """
    run_git = git.run if git is not None else run
    verify = run_git(
        [
            "git",
            "-C",
            str(dest),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{base_sha}^{{commit}}",
        ],
        check=False,
        env=_read_env(),
    )
    if verify.returncode != 0:
        raise CanonicalRepositoryError(f"{base_sha} is not in {dest} after the fetch")


def provision(
    dest: Path, *, source: str, base_sha: str, git: HostGitPreparation | None = None
) -> CanonicalRepository:
    """A bare repository at *dest* holding *base_sha*, fetched from *source*.

    *dest* must not exist. *source* is either a remote URL or a local path the
    host owns; anything else is refused before git reads a byte of it.
    """
    if not _OID.fullmatch(base_sha):
        raise CanonicalRepositoryError(f"base {base_sha!r} is not a commit id")
    env = _fetch_env(source, git)
    _init_bare(dest, git)
    run_git = git.run if git is not None else run
    fetch = run_git(
        [
            "git",
            "-C",
            str(dest),
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--no-tags",
            # End-of-options: source and base_sha are already validated
            # (host-owned/URL and OID), but the separator makes an
            # option-shaped value a hard fetch error rather than a flag even
            # if that validation ever regresses.
            "--",
            source,
            base_sha,
        ],
        check=False,
        env=env,
    )
    if fetch.returncode != 0:
        detail = redact_credentials(fetch.stderr or "").strip()[:300]
        raise CanonicalRepositoryError(f"could not fetch {base_sha}: {detail}")
    _require_base(dest, base_sha, git)
    return CanonicalRepository(path=dest, base_sha=base_sha)


def _run_fetch(
    command: list[str],
    *,
    fetch_env: dict[str, str],
    git: HostGitPreparation | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    if git is not None:
        return git.run(command, check=False, env=fetch_env)
    try:
        return run(command, check=False, env=fetch_env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CanonicalRepositoryError("the fetch timed out") from exc


def fetch_commit(
    repo: CanonicalRepository,
    *,
    source: str,
    sha: str,
    git: HostGitPreparation | None = None,
    depth: int = 1,
    timeout: float | None = None,
) -> None:
    """Fetch one additional commit into *repo* and prove it is present.

    *depth* commits of its history come with it; *timeout* bounds the fetch.
    """
    if not _OID.fullmatch(sha):
        raise CanonicalRepositoryError(f"parent {sha!r} is not a commit id")
    if depth < 1:
        raise CanonicalRepositoryError(f"fetch depth {depth} is not positive")
    result = _run_fetch(
        [
            "git",
            "-C",
            str(repo.path),
            "fetch",
            "--quiet",
            "--depth",
            str(depth),
            "--no-tags",
            "--",
            source,
            sha,
        ],
        fetch_env=_fetch_env(source, git),
        git=git,
        timeout=timeout,
    )
    if git is not None and result.returncode == 0:
        _require_base(repo.path, sha, git)
        return
    if result.returncode != 0 or not repo.holds_commit(sha):
        detail = redact_credentials(result.stderr or result.stdout or "").strip()[:300]
        raise CanonicalRepositoryError(f"could not fetch parent {sha}: {detail}")


def fetch_history(repo: CanonicalRepository, *, source: str, sha: str) -> None:
    """Fetch complete ancestry for one trusted commit into a canonical repo."""
    if not _OID.fullmatch(sha):
        raise CanonicalRepositoryError(f"history tip {sha!r} is not a commit id")
    command = ["git", "-C", str(repo.path), "fetch", "--quiet", "--no-tags"]
    if (repo.path / "shallow").is_file():
        command.append("--unshallow")
    command.extend(["--", source, sha])
    result = run(command, check=False, env=_fetch_env(source))
    if result.returncode != 0 or not repo.holds_commit(sha):
        detail = redact_credentials(result.stderr or result.stdout or "").strip()[:300]
        raise CanonicalRepositoryError(f"could not fetch history for {sha}: {detail}")


def _build(
    repo: CanonicalRepository,
    artifact: Artifact,
    approved: Sequence[ManifestEntry],
    deletions: Sequence[str],
    index_file: Path,
) -> str:
    index_file.unlink(missing_ok=True)
    return apply_to_tree(
        repo.path,
        base_sha=repo.base_sha,
        approved=approved,
        deletions=deletions,
        content=artifact.content,
        index_file=index_file,
    )


def promote(
    repo: CanonicalRepository,
    artifact: Artifact,
    *,
    scratch: Path,
    reviewed: bool = False,
    merged_base_digest: Callable[[str], str | None] | None = None,
) -> PromotedTree:
    """Turn a verified package into an immutable tree in *repo*.

    The tree is built TWICE, from two fresh indexes, and the promotion is
    refused unless both builds produce the same sha. That is what
    :data:`VERIFICATION_EXACT_TREE` records — not "git returned a tree" but
    "this package names exactly one tree, and this is it". A receipt that bound
    an unrepeatable tree would seal a coincidence.

    *merged_base_digest* is forwarded to the filter's review flags and nothing
    else: a write byte-identical to the base this checkout merged is not work
    anybody authored here (#2362). It changes no tree — the promoted sha is the
    same either way — only whether a person is asked about it.
    """
    if artifact.base_sha != repo.base_sha:
        raise CanonicalRepositoryError(
            f"the package applies to base {artifact.base_sha}, but the host's "
            f"trusted base is {repo.base_sha}"
        )
    scratch.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
    existing = repo.base_state()
    verdict = filter_artifact(
        artifact,
        existing_modes=existing,
        destination_paths=existing.keys(),
        merged_base_digest=merged_base_digest,
    )
    approved = verdict.approved(reviewed=reviewed)
    deletions = verdict.deletions(reviewed=reviewed)
    first = _build(repo, artifact, approved, deletions, scratch / "promote.index")
    second = _build(
        repo, artifact, approved, deletions, scratch / "promote.verify.index"
    )
    if first != second:
        raise CanonicalRepositoryError(
            f"the package does not name one tree: {first} then {second}"
        )
    if not repo.holds_tree(first):
        raise CanonicalRepositoryError(
            f"tree {first} is not in the canonical repository"
        )
    return PromotedTree(
        base_sha=repo.base_sha,
        tree_sha=first,
        entry_count=len(approved),
        deletion_count=len(deletions),
        changed_paths=tuple(sorted({*(entry.path for entry in approved), *deletions})),
    )


class IdentityLike(Protocol):
    """The two strings a commit needs to name a person."""

    @property
    def name(self) -> str: ...

    @property
    def email(self) -> str: ...


class CommitIntentLike(Protocol):
    """Everything a commit is, read structurally off whatever names it.

    Structural rather than an import of the publication lifecycle's commit
    intent, so that lifecycle stays off this module's import graph: I09A's
    worker is the one place that knows about both, and nothing product-facing
    may reach the gated lifecycle by way of the repository it happens to share.
    """

    @property
    def tree_sha(self) -> str: ...

    @property
    def parent_sha(self) -> str | None: ...

    @property
    def message(self) -> str: ...

    @property
    def author(self) -> IdentityLike: ...

    @property
    def committer(self) -> IdentityLike: ...

    @property
    def when(self) -> str: ...

    @property
    def extra_parents(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CommitFacts:
    """What a commit object in the canonical repository actually says."""

    sha: str
    tree_sha: str
    parents: tuple[str, ...]


def _commit_env(intent: CommitIntentLike) -> dict[str, str]:
    """The bounded environment ``commit-tree`` runs under.

    Two things are being kept out. The ambient ``GIT_*`` block — including the
    ``GIT_AUTHOR_*`` and ``GIT_COMMITTER_*`` a dispatch may have exported — is
    dropped by :func:`trusted_git_env` and then set from the intent alone, so
    the commit is dated by the persisted row rather than by whoever set a
    variable last. The user's and machine's config files are cut off entirely:
    a global ``commit.gpgsign`` would otherwise turn a deterministic object
    into one whose bytes change on every call, and the whole node rests on the
    same intent naming the same SHA forever.
    """
    return trusted_git_env(
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME=intent.author.name,
        GIT_AUTHOR_EMAIL=intent.author.email,
        GIT_AUTHOR_DATE=intent.when,
        GIT_COMMITTER_NAME=intent.committer.name,
        GIT_COMMITTER_EMAIL=intent.committer.email,
        GIT_COMMITTER_DATE=intent.when,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )


def commit_tree(repo: CanonicalRepository, intent: CommitIntentLike) -> str:
    """Create the commit *intent* names in *repo*, and return its sha.

    Idempotent by construction: a commit object is its own content hash, so
    running this twice on the same intent writes the same object and returns
    the same sha. That is what makes a crash between "the object was created"
    and "the row says so" recoverable — the retry cannot produce a sibling.

    The message travels on stdin as bytes, so nothing in it has to survive an
    argv round-trip or a locale's idea of encoding.
    """
    if not _OID.fullmatch(intent.tree_sha):
        raise CanonicalRepositoryError(f"tree {intent.tree_sha!r} is not a tree id")
    args = ["commit-tree", intent.tree_sha]
    if intent.parent_sha is not None:
        if not _OID.fullmatch(intent.parent_sha):
            raise CanonicalRepositoryError(
                f"parent {intent.parent_sha!r} is not a commit id"
            )
        args += ["-p", intent.parent_sha]
    for parent in intent.extra_parents:
        if not _OID.fullmatch(parent):
            raise CanonicalRepositoryError(f"parent {parent!r} is not a commit id")
        args += ["-p", parent]
    result = run_bytes(
        ["git", "-C", str(repo.path), *args, "-F", "-"],
        stdin=intent.message.encode("utf-8"),
        env=_commit_env(intent),
    )
    if result.returncode != 0:
        detail = redact_credentials(
            (result.stderr or b"").decode("utf-8", errors="replace")
        ).strip()
        raise CanonicalRepositoryError(f"commit-tree failed: {detail[:300]}")
    sha = (result.stdout or b"").decode("ascii", errors="replace").strip()
    if not _OID.fullmatch(sha):
        raise CanonicalRepositoryError(f"commit-tree returned {sha!r}")
    return sha


def read_commit(repo: CanonicalRepository, sha: str) -> CommitFacts:
    """The tree and parents *sha* names, read back out of *repo*.

    Reading the object back is the difference between "git exited zero" and
    "the commit says what the intent said". Only the second is evidence.
    """
    if not _OID.fullmatch(sha):
        raise CanonicalRepositoryError(f"{sha!r} is not a commit id")
    kind = run(
        ["git", "-C", str(repo.path), "cat-file", "-t", sha],
        check=False,
        env=_read_env(),
    )
    if kind.returncode != 0:
        detail = redact_credentials(kind.stderr or "").strip()[:300]
        raise CanonicalRepositoryError(f"cat-file failed for {sha}: {detail}")
    if (kind.stdout or "").strip() != "commit":
        raise CanonicalRepositoryError(f"{sha} is not a commit object")
    body = run(
        ["git", "-C", str(repo.path), "cat-file", "commit", sha],
        check=False,
        env=_read_env(),
    )
    if body.returncode != 0:
        detail = redact_credentials(body.stderr or "").strip()[:300]
        raise CanonicalRepositoryError(f"cat-file failed for {sha}: {detail}")
    return _parse_commit(sha, body.stdout or "")


def _parse_commit(sha: str, body: str) -> CommitFacts:
    """The header half of a commit object. The message is not this node's."""
    tree = ""
    parents: list[str] = []
    for line in body.splitlines():
        if not line:
            break  # the blank line before the message ends the headers
        field, _, value = line.partition(" ")
        if field == "tree":
            tree = value.strip()
        elif field == "parent":
            parents.append(value.strip())
    if not _OID.fullmatch(tree):
        raise CanonicalRepositoryError(f"commit {sha} names no tree")
    return CommitFacts(sha=sha, tree_sha=tree, parents=tuple(parents))
